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


def _active_trip_names(exclude_trip=None):
	filters = {"docstatus": ["!=", 2]}
	if exclude_trip:
		filters["name"] = ["!=", exclude_trip]
	return frappe.get_all("Delivery Trip", filters=filters, pluck="name", ignore_permissions=True)


def _assigned_delivery_note_names(trip_names=None):
	"""Delivery Notes already used as a stop on any active (docstatus != 2)
	trip - same definition `get_pending_deliveries` needs, factored out so
	`create_trip`/`add_stops_to_trip` can reuse it instead of re-deriving it."""
	if trip_names is None:
		trip_names = _active_trip_names()
	return set(
		frappe.get_all(
			"Delivery Stop",
			filters={"parent": ["in", trip_names or [""]], "delivery_note": ["is", "set"]},
			pluck="delivery_note",
			ignore_permissions=True,
		)
	)


def _load_delivery_notes_for_stops(delivery_note_names):
	"""Fetch DN fields + resolved address-display text needed to build
	Delivery Stop rows. Shared by create_trip and add_stops_to_trip."""
	notes = frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", delivery_note_names]},
		fields=["name", "customer", "shipping_address_name", "customer_address", "grand_total", "contact_person"],
		ignore_permissions=True,
	)
	notes_by_name = {n.name: n for n in notes}

	missing = [n for n in delivery_note_names if n not in notes_by_name]
	if missing:
		frappe.throw(_("Delivery Note(s) not found: {0}").format(", ".join(missing)), frappe.DoesNotExistError)

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

	return notes_by_name, address_display_by_name


def _append_delivery_stops(trip, delivery_note_names, notes_by_name, address_display_by_name):
	for dn_name in delivery_note_names:
		dn = notes_by_name[dn_name]
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
	assigned_notes = _assigned_delivery_note_names()

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

	conflicts = [n for n in delivery_note_names if n in _assigned_delivery_note_names()]
	if conflicts:
		frappe.throw(_("Already assigned to another trip: {0}").format(", ".join(conflicts)))

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

	notes_by_name, address_display_by_name = _load_delivery_notes_for_stops(delivery_note_names)
	_append_delivery_stops(trip, delivery_note_names, notes_by_name, address_display_by_name)

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


@frappe.whitelist(allow_guest=True)
def list_trips_for_date(date, company=None):
	if not date:
		frappe.throw(_("Date is required."))
	day = getdate(date)

	filters = {
		"docstatus": ["!=", 2],
		"departure_time": ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]],
	}
	if company:
		filters["company"] = company

	trips = frappe.get_all(
		"Delivery Trip",
		filters=filters,
		fields=["name", "status", "docstatus", "driver", "driver_name", "vehicle", "departure_time", "total_distance", "uom"],
		order_by="departure_time asc",
		ignore_permissions=True,
	)

	trip_names = [t.name for t in trips]
	stop_counts = {}
	if trip_names:
		for row in frappe.db.sql(
			"""select parent, count(*) as stop_count
			from `tabDelivery Stop` where parent in %(names)s group by parent""",
			{"names": trip_names},
			as_dict=True,
		):
			stop_counts[row.parent] = row.stop_count

	for t in trips:
		t["stop_count"] = stop_counts.get(t.name, 0)

	return {"date": str(day), "trips": trips}


@frappe.whitelist(allow_guest=True)
def add_stops_to_trip(trip_name, delivery_note_names):
	delivery_note_names = frappe.parse_json(delivery_note_names) if isinstance(delivery_note_names, str) else (delivery_note_names or [])
	if not delivery_note_names:
		frappe.throw(_("Select at least one order to add."))

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	if trip.docstatus != 0:
		frappe.throw(_("Stops can only be added to a Draft trip."))

	already_on_trip = {s.delivery_note for s in trip.delivery_stops if s.delivery_note}
	dupes = [n for n in delivery_note_names if n in already_on_trip]
	if dupes:
		frappe.throw(_("Already on this trip: {0}").format(", ".join(dupes)))

	conflicts = [n for n in delivery_note_names if n in _assigned_delivery_note_names()]
	if conflicts:
		frappe.throw(_("Already assigned to another trip: {0}").format(", ".join(conflicts)))

	notes_by_name, address_display_by_name = _load_delivery_notes_for_stops(delivery_note_names)
	_append_delivery_stops(trip, delivery_note_names, notes_by_name, address_display_by_name)

	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)


@frappe.whitelist(allow_guest=True)
def remove_stops_from_trip(trip_name, delivery_note_names):
	delivery_note_names = frappe.parse_json(delivery_note_names) if isinstance(delivery_note_names, str) else (delivery_note_names or [])
	if not delivery_note_names:
		frappe.throw(_("Select at least one stop to remove."))

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	if trip.docstatus != 0:
		frappe.throw(_("Stops can only be removed from a Draft trip. Cancel the trip to free all its stops."))

	names = set(delivery_note_names)
	rows_to_remove = [s for s in trip.delivery_stops if s.delivery_note in names]
	if not rows_to_remove:
		frappe.throw(_("None of the given delivery notes are on this trip."))

	# Snapshot list - trip.remove() mutates trip.delivery_stops in place and
	# renumbers idx for the remaining rows, so iterate the snapshot, not the
	# live list.
	for row in rows_to_remove:
		trip.remove(row)

	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)


@frappe.whitelist(allow_guest=True)
def update_trip_assignment(trip_name, driver=None, vehicle=None):
	if not driver and not vehicle:
		frappe.throw(_("Provide a driver or vehicle to update."))

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	if trip.docstatus != 0:
		frappe.throw(_("Only a Draft trip can be reassigned. Cancel a published trip and create a new one instead."))

	if driver:
		driver_doc = frappe.db.get_value("Driver", driver, ["full_name", "address"], as_dict=True)
		if not driver_doc:
			frappe.throw(_("Driver {0} not found").format(driver), frappe.DoesNotExistError)
		trip.driver = driver
		trip.driver_name = driver_doc.full_name
		trip.driver_address = driver_doc.address or _default_address("Company", trip.company)

	if vehicle:
		if not frappe.db.exists("Vehicle", vehicle):
			frappe.throw(_("Vehicle {0} not found").format(vehicle), frappe.DoesNotExistError)
		trip.vehicle = vehicle

	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)


@frappe.whitelist(allow_guest=True)
def cancel_trip(trip_name):
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False

	if trip.docstatus == 2:
		frappe.throw(_("Trip {0} is already cancelled.").format(trip_name))

	if trip.docstatus == 0:
		# Draft was never submitted - core Frappe disallows a 0->2 docstatus
		# transition (DocstatusTransitionError). Deleting removes the trip
		# and its Delivery Stop rows in one call, which is exactly what frees
		# the linked Delivery Notes back into get_pending_deliveries' pool (a
		# stop only "counts" while its parent trip exists with docstatus != 2).
		# Nothing else needs cleanup: update_delivery_notes() only ever runs
		# from on_submit/on_cancel, neither of which a Draft trip has been
		# through, so the DNs were never touched.
		frappe.delete_doc("Delivery Trip", trip_name, ignore_permissions=True)
		frappe.db.commit()
		return {"trip": trip_name, "status": "Deleted"}

	# Submitted: DeliveryTrip.on_cancel() -> update_delivery_notes(delete=True)
	# loads each linked Delivery Note as a *fresh* Document instance and calls
	# note_doc.save() with no ignore_permissions kwarg. trip.flags.
	# ignore_permissions only covers the trip document itself - Document.
	# has_permission() only ever reads its own instance's flags, never a
	# global - so on a session whose underlying user lacks Delivery Note
	# write access, that inner save() raises frappe.PermissionError and the
	# whole cancel rolls back uncommitted. Elevate the session for this call
	# only, same idiom core uses in frappe/search/website_search.py.
	previous_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		trip.flags.ignore_permissions = True
		trip.cancel()
	finally:
		frappe.set_user(previous_user)

	frappe.db.commit()
	return {"trip": trip.name, "status": trip.status}


# ---------------------------------------------------------------------------
# Dev seed data
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def seed_tms_demo(reset=False):
	"""Create demo data for TMS dispatcher testing.

	Idempotent by default — skips anything that already exists.
	Pass reset=True to delete existing TMS demo docs first.

	Creates:
	  - 2 Drivers (each with Employee + geocoded home Address)
	  - 2 Vehicles
	  - 4 Customers with geocoded shipping Addresses
	  - 4 submitted Delivery Notes (one per customer)
	"""
	reset = frappe.parse_json(reset) if isinstance(reset, str) else bool(reset)
	company = "library"
	warehouse = "POSNET Stores - L"
	item_code = "24755"
	item_name = "WHISKY MACALLAN ERATH ESTUCHE 1*700ML"
	today = frappe.utils.today()

	created = []
	skipped = []

	def _ex(doctype, name):
		return bool(frappe.db.exists(doctype, name))

	if reset:
		# Delete existing demo DNs and trips that use them, then customers/drivers/vehicles
		for dn in frappe.get_all("Delivery Note", filters={"customer": ["like", "TMS Demo Cliente%"]}, ignore_permissions=True):
			if frappe.db.get_value("Delivery Note", dn.name, "docstatus") == 1:
				frappe.db.set_value("Delivery Note", dn.name, "docstatus", 2)
			frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)
		for dr in frappe.get_all("Driver", filters={"full_name": ["like", "TMS Demo%"]}, ignore_permissions=True):
			frappe.delete_doc("Driver", dr.name, force=True, ignore_permissions=True)
		for v in frappe.get_all("Vehicle", filters={"license_plate": ["like", "TMS-%"]}, ignore_permissions=True):
			frappe.delete_doc("Vehicle", v.name, force=True, ignore_permissions=True)
		for c in frappe.get_all("Customer", filters={"customer_name": ["like", "TMS Demo Cliente%"]}, ignore_permissions=True):
			frappe.delete_doc("Customer", c.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ── 1. Drivers (Employee → Driver → home Address) ─────────────────────────
	drivers_seed = [
		{"full_name": "TMS Demo Carlos Ramírez", "cell": "011-4444-0001", "lat": -34.6100, "lng": -58.4200, "addr_line": "Av. Rivadavia 5000, Flores"},
		{"full_name": "TMS Demo María González", "cell": "011-4444-0002", "lat": -34.5900, "lng": -58.4500, "addr_line": "Triunvirato 3200, Villa del Parque"},
	]
	for d in drivers_seed:
		exists = frappe.get_all("Driver", filters={"full_name": d["full_name"]}, ignore_permissions=True)
		if exists:
			skipped.append(f"Driver: {d['full_name']}")
			continue

		home_addr = frappe.get_doc({
			"doctype": "Address",
			"address_title": f"{d['full_name']} - Casa",
			"address_type": "Personal",
			"address_line1": d["addr_line"],
			"city": "Buenos Aires",
			"country": "Argentina",
			"custom_latitude": d["lat"],
			"custom_longitude": d["lng"],
		})
		home_addr.insert(ignore_permissions=True)

		first = d["full_name"].split()[2]
		last = " ".join(d["full_name"].split()[3:])
		emp = frappe.get_doc({
			"doctype": "Employee",
			"first_name": first,
			"last_name": last,
			"employee_name": d["full_name"],
			"company": company,
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2024-01-01",
			"gender": "Male",
			"status": "Active",
		})
		emp.flags.ignore_mandatory = True
		emp.insert(ignore_permissions=True)

		driver = frappe.get_doc({
			"doctype": "Driver",
			"full_name": d["full_name"],
			"employee": emp.name,
			"status": "Active",
			"cell_number": d["cell"],
			"address": home_addr.name,
		})
		driver.insert(ignore_permissions=True)
		created.append(f"Driver: {driver.name}")

	# ── 2. Vehicles ────────────────────────────────────────────────────────────
	vehicles_seed = [
		{"plate": "TMS-001-AA", "make": "Ford", "model": "Transit", "fuel": "Diesel"},
		{"plate": "TMS-002-BB", "make": "Volkswagen", "model": "Caddy", "fuel": "Diesel"},
	]
	for v in vehicles_seed:
		if frappe.db.exists("Vehicle", {"license_plate": v["plate"]}):
			skipped.append(f"Vehicle: {v['plate']}")
			continue
		veh = frappe.get_doc({
			"doctype": "Vehicle",
			"license_plate": v["plate"],
			"make": v["make"],
			"model": v["model"],
			"fuel_type": v["fuel"],
			"last_odometer": 0,
			"uom": "Unit",
		})
		veh.insert(ignore_permissions=True)
		created.append(f"Vehicle: {veh.name}")

	# ── 3. Customers + geocoded shipping addresses ─────────────────────────────
	customers_seed = [
		{"name": "TMS Demo Cliente 1", "area": "Palermo", "addr": "Av. Santa Fe 3000", "lat": -34.5883, "lng": -58.4314},
		{"name": "TMS Demo Cliente 2", "area": "San Telmo", "addr": "Defensa 500", "lat": -34.6217, "lng": -58.3731},
		{"name": "TMS Demo Cliente 3", "area": "Belgrano", "addr": "Av. Cabildo 2000", "lat": -34.5537, "lng": -58.4560},
		{"name": "TMS Demo Cliente 4", "area": "Recoleta", "addr": "Av. Alvear 1800", "lat": -34.5875, "lng": -58.3951},
	]
	for c in customers_seed:
		if not frappe.db.exists("Customer", c["name"]):
			cust = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": c["name"],
				"customer_type": "Individual",
				"customer_group": "Retail",
				"territory": "Argentina",
			})
			cust.insert(ignore_permissions=True)
			created.append(f"Customer: {cust.name}")
		else:
			skipped.append(f"Customer: {c['name']}")

		addr_title = f"{c['name']} - {c['area']}"
		if not frappe.db.exists("Address", {"address_title": addr_title}):
			addr = frappe.get_doc({
				"doctype": "Address",
				"address_title": addr_title,
				"address_type": "Shipping",
				"address_line1": c["addr"],
				"city": f"Buenos Aires",
				"country": "Argentina",
				"is_shipping_address": 1,
				"custom_latitude": c["lat"],
				"custom_longitude": c["lng"],
				"links": [{"link_doctype": "Customer", "link_name": c["name"]}],
			})
			addr.insert(ignore_permissions=True)
			created.append(f"Address: {addr.name}")

	# ── 4. Delivery Notes (force-submitted for demo) ───────────────────────────
	for i, c in enumerate(customers_seed, 1):
		existing_dn = frappe.get_all(
			"Delivery Note",
			filters={"customer": c["name"], "docstatus": 1},
			ignore_permissions=True,
		)
		if existing_dn:
			skipped.append(f"Delivery Note for {c['name']}")
			continue

		ship_addr = frappe.db.get_value(
			"Address",
			{"address_title": f"{c['name']} - {c['area']}", "address_type": "Shipping"},
			"name",
		)
		qty = float(i + 1)
		dn = frappe.get_doc({
			"doctype": "Delivery Note",
			"company": company,
			"customer": c["name"],
			"posting_date": today,
			"set_warehouse": warehouse,
			"shipping_address_name": ship_addr,
			"selling_price_list": "Standard Selling",
			"currency": "ARS",
			"conversion_rate": 1.0,
			"plc_conversion_rate": 1.0,
			"items": [{
				"item_code": item_code,
				"item_name": item_name,
				"qty": qty,
				"stock_qty": qty,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1.0,
				"warehouse": warehouse,
				"rate": 5000.0,
				"amount": 5000.0 * qty,
			}],
		})
		dn.flags.ignore_permissions = True
		dn.flags.ignore_validate = True
		dn.flags.ignore_mandatory = True
		dn.flags.ignore_links = True
		dn.insert(ignore_permissions=True)
		# Force-submit for demo: bypass accounting/stock movements
		frappe.db.sql(
			"UPDATE `tabDelivery Note` SET docstatus=1, status='To Deliver', customer_name=%s WHERE name=%s",
			(c["name"], dn.name),
		)
		frappe.db.commit()
		created.append(f"Delivery Note: {dn.name} (force-submitted)")

	frappe.db.commit()
	return {"created": created, "skipped": skipped}


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
