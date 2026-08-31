"""TMS (route planning + delivery) API.

Thin layer over core ERPNext `Driver` / `Vehicle` / `Delivery Trip` /
`Delivery Stop`. Route optimization is not reimplemented here - it already
exists in `DeliveryTrip.process_route()`, which calls the Google Maps
Directions API with `optimize_waypoints=True`. This module only exposes that
engine to the dispatcher (planner) and driver (mobile/web) frontends, and adds
proof-of-delivery capture (signature + recipient name/ID) on top of it.
"""

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_address_display
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime
from frappe.utils.file_manager import save_file

from erpnext.stock.doctype.delivery_trip.delivery_trip import sanitize_address


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _default_address(link_doctype, link_name):
	"""Same lookup as core `get_default_address`, but with ignore_permissions
	explicit per this project's API-key-auth rule (see root CLAUDE.md)."""
	if not link_name:
		return None
	rows = frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", link_doctype],
			["Dynamic Link", "link_name", "=", link_name],
			["disabled", "=", 0],
		],
		pluck="name",
		order_by="is_primary_address DESC",
		limit=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


def _get_current_driver():
	from erpnext.erpnext_integrations.ecommerce_api.company_context import acting_user

	user = acting_user()
	if not user or user == "Guest":
		frappe.throw(_("Login required"), frappe.AuthenticationError)

	employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if not employee:
		frappe.throw(_("No employee profile is linked to this account"))

	driver = frappe.db.get_value(
		"Driver",
		{"employee": employee},
		["name", "full_name", "cell_number", "address"],
		as_dict=True,
	)
	if not driver:
		frappe.throw(_("No driver profile is linked to this account"))

	return driver


def _require_owned_trip(trip_name, driver):
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False

	if trip.driver != driver.name:
		frappe.throw(_("You are not assigned to this trip"), frappe.PermissionError)

	return trip


def _stop_out(stop, address_geo=None):
	address_geo = address_geo or {}
	geo = address_geo.get(stop.address) or {}
	return {
		"idx": stop.idx,
		"customer": stop.customer,
		"customer_address": stop.customer_address,
		"address": stop.address,
		"delivery_note": stop.delivery_note,
		"grand_total": stop.grand_total,
		"contact": stop.contact,
		"visited": bool(stop.visited),
		"distance": stop.distance,
		"estimated_arrival": stop.estimated_arrival,
		# Prefer the optimized lat/lng from process_route(); fall back to the
		# geocode cache so the map has pins before a route has been optimized.
		"lat": stop.lat or geo.get("custom_latitude"),
		"lng": stop.lng or geo.get("custom_longitude"),
		"pod": {
			"recipient_name": stop.custom_pod_recipient_name,
			"recipient_id_number": stop.custom_pod_recipient_id_number,
			"signature": stop.custom_pod_signature,
			"notes": stop.custom_pod_notes,
			"captured_at": stop.custom_pod_captured_at,
		}
		if stop.visited
		else None,
	}


# ---------------------------------------------------------------------------
# Dispatcher / planner
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_planner_context():
	drivers = frappe.get_all(
		"Driver",
		filters={"status": "Active"},
		fields=["name", "full_name", "cell_number", "address"],
		ignore_permissions=True,
	)
	vehicles = frappe.get_all(
		"Vehicle",
		fields=["name", "license_plate", "make", "model"],
		ignore_permissions=True,
	)
	return {
		"drivers": drivers,
		"vehicles": vehicles,
		"google_maps_configured": bool(frappe.db.get_single_value("Google Settings", "api_key")),
		"today": str(getdate()),
	}


@frappe.whitelist(allow_guest=True)
def get_pending_deliveries(date=None, company=None):
	active_trips = frappe.get_all(
		"Delivery Trip", filters={"docstatus": ["!=", 2]}, pluck="name", ignore_permissions=True
	)
	assigned_notes = frappe.get_all(
		"Delivery Stop",
		filters={"parent": ["in", active_trips or [""]], "delivery_note": ["is", "set"]},
		pluck="delivery_note",
		ignore_permissions=True,
	)

	filters = {"docstatus": 1, "is_return": 0}
	if assigned_notes:
		filters["name"] = ["not in", assigned_notes]
	if company:
		filters["company"] = company
	if date:
		filters["posting_date"] = getdate(date)

	notes = frappe.get_all(
		"Delivery Note",
		filters=filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"shipping_address_name",
			"customer_address",
			"grand_total",
			"posting_date",
		],
		order_by="posting_date asc",
		ignore_permissions=True,
	)

	names = [n.name for n in notes]
	item_counts = {}
	if names:
		for row in frappe.db.sql(
			"""select parent, count(*) as item_count, sum(qty) as qty_total
			from `tabDelivery Note Item` where parent in %(names)s group by parent""",
			{"names": names},
			as_dict=True,
		):
			item_counts[row.parent] = {"item_count": row.item_count, "qty_total": row.qty_total}

	address_names = list({n.shipping_address_name or n.customer_address for n in notes if (n.shipping_address_name or n.customer_address)})
	geo_by_address = {}
	if address_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=["name", "custom_latitude", "custom_longitude"],
			ignore_permissions=True,
		):
			geo_by_address[row.name] = row

	out = []
	for n in notes:
		address_name = n.shipping_address_name or n.customer_address
		geo = geo_by_address.get(address_name) or {}
		out.append(
			{
				"delivery_note": n.name,
				"customer": n.customer,
				"customer_name": n.customer_name,
				"address": address_name,
				"grand_total": n.grand_total,
				"posting_date": n.posting_date,
				"item_count": (item_counts.get(n.name) or {}).get("item_count", 0),
				"qty_total": (item_counts.get(n.name) or {}).get("qty_total", 0),
				"geocoded": bool(geo.get("custom_latitude")),
				"lat": geo.get("custom_latitude"),
				"lng": geo.get("custom_longitude"),
			}
		)

	return {"deliveries": out}


@frappe.whitelist(allow_guest=True)
def geocode_address(address_name):
	address = frappe.db.get_value(
		"Address", address_name, ["custom_latitude", "custom_longitude"], as_dict=True
	)
	if not address:
		frappe.throw(_("Address {0} not found").format(address_name), frappe.DoesNotExistError)

	if address.custom_latitude and address.custom_longitude:
		return {"lat": address.custom_latitude, "lng": address.custom_longitude, "cached": True}

	api_key = frappe.db.get_single_value("Google Settings", "api_key")
	if not api_key:
		frappe.throw(_("Enter API key in Google Settings."))

	frappe.flags.ignore_permissions = True
	address_doc = frappe.get_doc("Address", address_name)
	frappe.flags.ignore_permissions = False

	address_display = get_address_display(address_doc.as_dict())
	address_str = sanitize_address(address_display)
	if not address_str:
		frappe.throw(_("Address {0} has no address lines to geocode").format(address_name))

	import googlemaps

	maps_client = googlemaps.Client(key=api_key)
	try:
		results = maps_client.geocode(address_str)
	except Exception as e:
		frappe.throw(_(str(e)))

	if not results:
		frappe.throw(_("Could not geocode address {0}").format(address_name))

	location = results[0]["geometry"]["location"]
	frappe.db.set_value(
		"Address",
		address_name,
		{
			"custom_latitude": location["lat"],
			"custom_longitude": location["lng"],
			"custom_geocoded_on": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()

	return {"lat": location["lat"], "lng": location["lng"], "cached": False}


@frappe.whitelist(allow_guest=True)
def create_trip(date, driver=None, vehicle=None, delivery_note_names=None, company=None):
	delivery_note_names = frappe.parse_json(delivery_note_names) if isinstance(delivery_note_names, str) else (delivery_note_names or [])
	if not delivery_note_names:
		frappe.throw(_("Select at least one order to plan a route."))

	company = company or frappe.defaults.get_user_default("Company")

	if not driver:
		active_drivers = frappe.get_all(
			"Driver", filters={"status": "Active"}, fields=["name", "full_name", "address"], ignore_permissions=True
		)
		if len(active_drivers) == 1:
			driver = active_drivers[0].name

	driver_doc = None
	driver_address = None
	if driver:
		driver_doc = frappe.db.get_value("Driver", driver, ["full_name", "address"], as_dict=True)
		driver_address = driver_doc.address if driver_doc else None

	if not driver_address and company:
		driver_address = _default_address("Company", company)

	if not vehicle:
		vehicles = frappe.get_all("Vehicle", pluck="name", ignore_permissions=True)
		if len(vehicles) == 1:
			vehicle = vehicles[0]

	trip = frappe.get_doc(
		{
			"doctype": "Delivery Trip",
			"company": company,
			"driver": driver,
			"driver_name": driver_doc.full_name if driver_doc else None,
			"driver_address": driver_address,
			"vehicle": vehicle,
			"departure_time": get_datetime(f"{getdate(date)} 08:00:00"),
			"delivery_stops": [],
		}
	)

	notes = frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", delivery_note_names]},
		fields=["name", "customer", "shipping_address_name", "customer_address", "grand_total", "contact_person"],
		ignore_permissions=True,
	)
	notes_by_name = {n.name: n for n in notes}

	address_names = list({(n.shipping_address_name or n.customer_address) for n in notes if (n.shipping_address_name or n.customer_address)})
	address_display_by_name = {}
	if address_names:
		for row in frappe.get_all(
			"Address", filters={"name": ["in", address_names]}, fields=["*"], ignore_permissions=True
		):
			# Passing a dict (not a name string) to get_address_display skips
			# its internal doc.check_permission() call - required here since
			# the API-key session may not hold read access on Address.
			address_display_by_name[row.name] = get_address_display(row)

	for dn_name in delivery_note_names:
		dn = notes_by_name.get(dn_name)
		if not dn:
			frappe.throw(_("Delivery Note {0} not found").format(dn_name), frappe.DoesNotExistError)
		address_name = dn.shipping_address_name or dn.customer_address
		trip.append(
			"delivery_stops",
			{
				"customer": dn.customer,
				"address": address_name,
				"customer_address": address_display_by_name.get(address_name),
				"delivery_note": dn.name,
				"grand_total": dn.grand_total,
				"contact": dn.contact_person,
			},
		)

	trip.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"trip": trip.name, "status": trip.status, "stop_count": len(trip.delivery_stops)}


@frappe.whitelist(allow_guest=True)
def optimize_trip(trip_name):
	trip = frappe.get_doc("Delivery Trip", trip_name)

	if not trip.driver_address:
		frappe.throw(_("Set a home/depot address on the driver (or company) before optimizing."))

	# process_route() calls self.save() internally with no ignore_permissions
	# kwarg, so it only bypasses the write-permission check if this is set on
	# the *document instance* beforehand (the global frappe.flags is not what
	# Document.has_permission reads).
	trip.flags.ignore_permissions = True
	trip.process_route(optimize=True)

	return get_trip_map_data(trip_name)


@frappe.whitelist(allow_guest=True)
def get_trip_map_data(trip_name):
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False

	address_names = list({s.address for s in trip.delivery_stops if s.address})
	geo_by_address = {}
	if address_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=["name", "custom_latitude", "custom_longitude"],
			ignore_permissions=True,
		):
			geo_by_address[row.name] = row

	stops = [_stop_out(s, geo_by_address) for s in sorted(trip.delivery_stops, key=lambda r: r.idx)]

	return {
		"trip": {
			"name": trip.name,
			"status": trip.status,
			"driver": trip.driver,
			"driver_name": trip.driver_name,
			"vehicle": trip.vehicle,
			"departure_time": trip.departure_time,
			"total_distance": trip.total_distance,
			"uom": trip.uom,
		},
		"stops": stops,
	}


@frappe.whitelist(allow_guest=True)
def publish_trip(trip_name):
	trip = frappe.get_doc("Delivery Trip", trip_name)

	if not trip.driver:
		frappe.throw(_("Assign a driver before publishing the route."))

	# submit() takes no ignore_permissions kwarg - it only reads whatever is
	# already set on the document instance's own flags.
	trip.flags.ignore_permissions = True
	trip.submit()
	frappe.db.commit()

	return {"trip": trip.name, "status": trip.status}


# ---------------------------------------------------------------------------
# Driver (mobile / driver web page)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def driver_get_my_trips(date=None):
	driver = _get_current_driver()

	filters = {"driver": driver.name, "docstatus": ["!=", 2]}
	if date:
		day = getdate(date)
		filters["departure_time"] = ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]]

	trips = frappe.get_all(
		"Delivery Trip",
		filters=filters,
		fields=["name", "status", "departure_time", "vehicle", "total_distance"],
		order_by="departure_time asc",
		ignore_permissions=True,
	)
	return {"driver": driver, "trips": trips}


@frappe.whitelist()
def driver_get_trip_stops(trip_name):
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	address_names = list({s.address for s in trip.delivery_stops if s.address})
	geo_by_address = {}
	if address_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=["name", "custom_latitude", "custom_longitude"],
			ignore_permissions=True,
		):
			geo_by_address[row.name] = row

	stops = [_stop_out(s, geo_by_address) for s in sorted(trip.delivery_stops, key=lambda r: r.idx)]

	return {
		"trip": {"name": trip.name, "status": trip.status, "departure_time": trip.departure_time},
		"stops": stops,
	}


@frappe.whitelist()
def driver_complete_stop(
	trip_name,
	stop_idx,
	recipient_name,
	recipient_id_number,
	signature_base64,
	notes=None,
	lat=None,
	lng=None,
):
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	if trip.docstatus != 1:
		frappe.throw(_("This route has not been published yet."))

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	if not recipient_name or not recipient_id_number:
		frappe.throw(_("Recipient name and ID/DNI are required to complete a delivery."))

	file_doc = save_file(
		f"pod-signature-{trip_name}-{stop_idx}.png",
		signature_base64,
		"Delivery Trip",
		trip_name,
		decode=True,
		is_private=1,
	)

	stop.custom_pod_recipient_name = recipient_name
	stop.custom_pod_recipient_id_number = recipient_id_number
	stop.custom_pod_signature = file_doc.file_url
	stop.custom_pod_notes = notes
	stop.custom_pod_captured_at = now_datetime()
	stop.custom_pod_captured_lat = flt(lat) if lat else None
	stop.custom_pod_captured_lng = flt(lng) if lng else None
	stop.visited = 1

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)
