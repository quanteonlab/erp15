"""TMS (route planning + delivery) API.

Thin layer over core ERPNext `Driver` / `Vehicle` / `Delivery Trip` /
`Delivery Stop`. Route optimization is not reimplemented here - it already
exists in `DeliveryTrip.process_route()`, which calls the Google Maps
Directions API with `optimize_waypoints=True`. This module only exposes that
engine to the dispatcher (planner) and driver (mobile/web) frontends, and adds
proof-of-delivery capture (signature + recipient name/ID) on top of it.
"""

import secrets
import string

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


def _resolve_pickup_address(company, driver_doc=None, pickup_warehouse=None):
	"""Depot/pickup address resolution for a trip, in priority order:

	1. Explicit `pickup_warehouse` (per-trip override) -> that Warehouse's
	   linked Address.
	2. `Company.custom_default_warehouse` -> its linked Address. This is the
	   "usual" depot for the company - a company can have several warehouses,
	   but normally only one is the default pickup point.
	3. `Driver.address` - kept for backward compatibility with setups that
	   have no company depot configured (e.g. driver departs from home).
	4. Legacy fallback: any Address dynamic-linked to the Company at all.

	Returns (address_name, resolved_from_warehouse_name_or_None).
	"""
	if pickup_warehouse:
		addr = _default_address("Warehouse", pickup_warehouse)
		if addr:
			return addr, pickup_warehouse

	default_warehouse = company and frappe.db.get_value("Company", company, "custom_default_warehouse")
	if default_warehouse:
		addr = _default_address("Warehouse", default_warehouse)
		if addr:
			return addr, default_warehouse

	if driver_doc and driver_doc.get("address"):
		return driver_doc["address"], None

	return _default_address("Company", company), None


def _get_current_driver():
	from erpnext.erpnext_integrations.ecommerce_api.company_context import acting_user, is_desk_admin

	user = acting_user()
	if not user or user == "Guest":
		frappe.throw(_("Login required"), frappe.AuthenticationError)

	employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if employee:
		driver = frappe.db.get_value(
			"Driver",
			{"employee": employee},
			["name", "full_name", "cell_number", "address"],
			as_dict=True,
		)
		if driver:
			return driver
		if is_desk_admin(user):
			return _ensure_driver_for_employee(employee, user)
		frappe.throw(_("No driver profile is linked to this account"))

	if is_desk_admin(user):
		return _ensure_admin_driver_profile(user)

	frappe.throw(_("No employee profile is linked to this account"))


def _driver_display_name(user: str) -> str:
	full = (frappe.db.get_value("User", user, "full_name") or "").strip()
	if full:
		return full
	local = user.split("@", 1)[0]
	return local.replace(".", " ").title() or user


def _ensure_driver_for_employee(employee: str, user: str) -> dict:
	full_name = frappe.db.get_value("Employee", employee, "employee_name") or _driver_display_name(user)
	driver = frappe.get_doc(
		{
			"doctype": "Driver",
			"full_name": full_name,
			"employee": employee,
			"status": "Active",
		}
	)
	driver.insert(ignore_permissions=True)
	frappe.db.commit()
	return frappe.db.get_value(
		"Driver",
		driver.name,
		["name", "full_name", "cell_number", "address"],
		as_dict=True,
	)


def _ensure_admin_driver_profile(user: str) -> dict:
	"""Desk admins may use driver APIs without a pre-existing Employee/Driver."""
	company = (
		frappe.defaults.get_user_default("Company", user)
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.db.get_value("Company", {}, "name")
	)
	display = _driver_display_name(user)
	parts = display.split()
	first_name = parts[0]
	last_name = " ".join(parts[1:]) if len(parts) > 1 else first_name

	emp = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"last_name": last_name,
			"employee_name": display,
			"company": company,
			"user_id": user,
			"date_of_birth": "1990-01-01",
			"date_of_joining": getdate(),
			"gender": "Other",
			"status": "Active",
		}
	)
	emp.flags.ignore_mandatory = True
	emp.insert(ignore_permissions=True)
	return _ensure_driver_for_employee(emp.name, user)


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
	`create_trip`/`add_stops_to_trip` can reuse it instead of re-deriving it.

	A stop whose only attempt resulted in "Not Home" does NOT keep its
	Delivery Note locked to the (now immutable, submitted) trip - the DN
	reappears in the pending pool so a new trip can be planned for it. The
	original stop row stays on the old trip as an audit record either way.
	"""
	if trip_names is None:
		trip_names = _active_trip_names()
	return set(
		frappe.get_all(
			"Delivery Stop",
			filters={
				"parent": ["in", trip_names or [""]],
				"delivery_note": ["is", "set"],
				"custom_outcome": ["!=", "Not Home"],
			},
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


def _ensure_tracking_code(dn_name):
	"""Generates Delivery Note.custom_tracking_code the first time a DN is
	attached to a trip - scoped to TMS-managed deliveries only, not every
	Delivery Note in the system."""
	existing = frappe.db.get_value("Delivery Note", dn_name, "custom_tracking_code")
	if existing:
		return existing

	length = cint(_load_tms_settings().get("tracking_code_length")) or 8
	alphabet = string.ascii_uppercase + string.digits
	for _attempt in range(10):
		code = "".join(secrets.choice(alphabet) for _ in range(length))
		if not frappe.db.exists("Delivery Note", {"custom_tracking_code": code}):
			frappe.db.set_value("Delivery Note", dn_name, "custom_tracking_code", code, update_modified=False)
			return code
	return None


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
		_ensure_tracking_code(dn_name)


def _stop_out(stop, address_geo=None):
	address_geo = address_geo or {}
	geo = address_geo.get(stop.address) or {}
	outcome = stop.get("custom_outcome") if hasattr(stop, "get") else getattr(stop, "custom_outcome", None)
	return {
		"idx": stop.idx,
		"customer": stop.customer,
		"customer_address": stop.customer_address,
		"address": stop.address,
		"delivery_note": stop.delivery_note,
		"grand_total": stop.grand_total,
		"contact": stop.contact,
		"visited": bool(stop.visited),
		"outcome": outcome or None,
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
			"outcome": outcome or None,
			"attempt_note": stop.get("custom_attempt_note") if hasattr(stop, "get") else getattr(stop, "custom_attempt_note", None),
			"photo_urls": frappe.parse_json(stop.custom_photo_urls) if getattr(stop, "custom_photo_urls", None) else [],
			"amount_due": getattr(stop, "custom_amount_due", None),
			"amount_collected": getattr(stop, "custom_amount_collected", None),
			"payment_method": getattr(stop, "custom_payment_method", None) or None,
			"cliente_debe": bool(getattr(stop, "custom_cliente_debe", 0)),
			"balance_after_stop": getattr(stop, "custom_balance_after_stop", None),
		}
		if (stop.visited or outcome)
		else None,
	}


# ---------------------------------------------------------------------------
# Dispatcher / planner
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_planner_context(company=None):
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

	company = company or frappe.defaults.get_user_default("Company")
	warehouses = []
	default_warehouse = None
	if company:
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 0, "disabled": 0},
			fields=["name", "warehouse_name"],
			order_by="warehouse_name asc",
			ignore_permissions=True,
		)
		default_warehouse = frappe.db.get_value("Company", company, "custom_default_warehouse")

	return {
		"drivers": drivers,
		"vehicles": vehicles,
		"warehouses": warehouses,
		"default_warehouse": default_warehouse,
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

	previous_attempts = set()
	if names:
		previous_attempts = set(
			frappe.get_all(
				"Delivery Stop",
				filters={"delivery_note": ["in", names], "custom_outcome": "Not Home"},
				pluck="delivery_note",
				ignore_permissions=True,
			)
		)

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
				"previous_attempt": n.name in previous_attempts,
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
def create_trip(date, driver=None, vehicle=None, delivery_note_names=None, company=None, pickup_warehouse=None):
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
	if driver:
		driver_doc = frappe.db.get_value("Driver", driver, ["full_name", "address"], as_dict=True)

	driver_address, resolved_warehouse = _resolve_pickup_address(company, driver_doc, pickup_warehouse)

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
			"custom_pickup_warehouse": resolved_warehouse,
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
			"pickup_warehouse": trip.custom_pickup_warehouse,
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
def update_trip_assignment(trip_name, driver=None, vehicle=None, pickup_warehouse=None):
	if not driver and not vehicle and not pickup_warehouse:
		frappe.throw(_("Provide a driver, vehicle, or pickup warehouse to update."))

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	if trip.docstatus != 0:
		frappe.throw(_("Only a Draft trip can be reassigned. Cancel a published trip and create a new one instead."))

	if driver or pickup_warehouse:
		driver_doc = None
		if driver:
			driver_doc = frappe.db.get_value("Driver", driver, ["full_name", "address"], as_dict=True)
			if not driver_doc:
				frappe.throw(_("Driver {0} not found").format(driver), frappe.DoesNotExistError)
			trip.driver = driver
			trip.driver_name = driver_doc.full_name
		elif trip.driver:
			driver_doc = frappe.db.get_value("Driver", trip.driver, ["full_name", "address"], as_dict=True)

		driver_address, resolved_warehouse = _resolve_pickup_address(trip.company, driver_doc, pickup_warehouse)
		trip.driver_address = driver_address
		trip.custom_pickup_warehouse = resolved_warehouse

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


@frappe.whitelist(allow_guest=True)
def create_driver_quick(full_name, cell_number=None, company=None):
	"""Quick-create a Driver (with a minimal Employee) from the dispatcher UI,
	so typing a name that doesn't exist yet doesn't require a trip to desk."""
	full_name = (full_name or "").strip()
	if not full_name:
		frappe.throw(_("Driver name is required."))

	existing = frappe.get_all("Driver", filters={"full_name": full_name}, limit=1, ignore_permissions=True)
	if existing:
		frappe.throw(_("A driver named {0} already exists.").format(full_name))

	company = company or frappe.defaults.get_user_default("Company")
	parts = full_name.split()
	first_name = parts[0]
	last_name = " ".join(parts[1:]) or first_name

	emp = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"last_name": last_name,
			"employee_name": full_name,
			"company": company,
			"date_of_birth": "1990-01-01",
			"date_of_joining": getdate(),
			"gender": "Other",
			"status": "Active",
		}
	)
	emp.flags.ignore_mandatory = True
	emp.insert(ignore_permissions=True)

	driver = frappe.get_doc(
		{
			"doctype": "Driver",
			"full_name": full_name,
			"employee": emp.name,
			"status": "Active",
			"cell_number": cell_number,
		}
	)
	driver.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"name": driver.name, "full_name": driver.full_name, "address": driver.address}


@frappe.whitelist(allow_guest=True)
def create_vehicle_quick(license_plate, make=None, model=None):
	"""Quick-create a Vehicle from the dispatcher UI. `make`/`model` are
	required by core Vehicle, so a placeholder fills in if left blank -
	dispatcher can fill in real values later from desk."""
	license_plate = (license_plate or "").strip()
	if not license_plate:
		frappe.throw(_("License plate is required."))

	if frappe.db.exists("Vehicle", license_plate):
		frappe.throw(_("A vehicle with plate {0} already exists.").format(license_plate))

	veh = frappe.get_doc(
		{
			"doctype": "Vehicle",
			"license_plate": license_plate,
			"make": make or "N/A",
			"model": model or "N/A",
			"fuel_type": "Petrol",
			"last_odometer": 0,
			"uom": "Unit",
		}
	)
	veh.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"name": veh.name, "license_plate": veh.license_plate}


# ---------------------------------------------------------------------------
# TMS Settings
#
# Reuses the "Table Extra Schema" doctype (scope -> JSON blob) the same way
# employee_api.py's group-permissions store does, instead of a new Single
# DocType - it's a settings blob with no list/report/audit needs of its own,
# so no new doctype or migration is warranted for it.
# ---------------------------------------------------------------------------

TMS_SETTINGS_SCOPE = "settings.tms"

TMS_SETTINGS_DEFAULTS = {
	"delivery_payment_mode": "same_driver_collects",  # same_driver_collects | separate_collector | optional_collect_at_delivery
	"require_signature": "always",  # always | never | per_outcome
	"require_photo_on_not_home": True,
	"allow_driver_reorder": False,
	"allow_driver_delivery_request": True,
	"print_template_delivery": None,
	"print_template_payment": None,
	"tracking_code_length": 8,
}


def _load_tms_settings():
	settings = dict(TMS_SETTINGS_DEFAULTS)
	if frappe.db.exists("Table Extra Schema", TMS_SETTINGS_SCOPE):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Table Extra Schema", TMS_SETTINGS_SCOPE)
		frappe.flags.ignore_permissions = False
		stored = frappe.parse_json(doc.columns_json) if doc.columns_json else {}
		if isinstance(stored, dict):
			settings.update(stored)
	return settings


@frappe.whitelist(allow_guest=True)
def get_tms_settings():
	return _load_tms_settings()


@frappe.whitelist(allow_guest=True)
def save_tms_settings(
	delivery_payment_mode=None,
	require_signature=None,
	require_photo_on_not_home=None,
	allow_driver_reorder=None,
	allow_driver_delivery_request=None,
	print_template_delivery=None,
	print_template_payment=None,
	tracking_code_length=None,
):
	current = _load_tms_settings()
	raw = {
		"delivery_payment_mode": delivery_payment_mode,
		"require_signature": require_signature,
		"require_photo_on_not_home": require_photo_on_not_home,
		"allow_driver_reorder": allow_driver_reorder,
		"allow_driver_delivery_request": allow_driver_delivery_request,
		"print_template_delivery": print_template_delivery,
		"print_template_payment": print_template_payment,
		"tracking_code_length": tracking_code_length,
	}
	bool_keys = {"require_photo_on_not_home", "allow_driver_reorder", "allow_driver_delivery_request"}
	for key, value in raw.items():
		if value is None:
			continue
		if key in bool_keys:
			current[key] = frappe.parse_json(value) if isinstance(value, str) else bool(value)
		elif key == "tracking_code_length":
			current[key] = cint(value) or TMS_SETTINGS_DEFAULTS["tracking_code_length"]
		else:
			current[key] = value

	payload = frappe.as_json(current)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", TMS_SETTINGS_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", TMS_SETTINGS_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Table Extra Schema", "scope": TMS_SETTINGS_SCOPE, "columns_json": payload})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return current


# ---------------------------------------------------------------------------
# Delivery Requests (driver-initiated "solicitar entrega")
# ---------------------------------------------------------------------------


@frappe.whitelist()
def driver_request_delivery(customer, delivery_note=None, note=None, photo_base64=None):
	if not _load_tms_settings().get("allow_driver_delivery_request"):
		frappe.throw(_("Driver-initiated delivery requests are disabled."))

	driver = _get_current_driver()

	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found").format(customer), frappe.DoesNotExistError)

	req = frappe.get_doc(
		{
			"doctype": "Delivery Request",
			"driver": driver.name,
			"customer": customer,
			"delivery_note": delivery_note or None,
			"note": note,
			"status": "Requested",
			"requested_at": now_datetime(),
		}
	)
	req.insert(ignore_permissions=True)

	if photo_base64:
		file_doc = save_file(
			f"delivery-request-{req.name}.png",
			photo_base64,
			"Delivery Request",
			req.name,
			decode=True,
			is_private=1,
		)
		req.db_set("photo", file_doc.file_url, update_modified=False)

	frappe.db.commit()
	return {"name": req.name, "status": req.status}


@frappe.whitelist(allow_guest=True)
def list_delivery_requests(status=None):
	filters = {"status": status} if status else {}
	requests = frappe.get_all(
		"Delivery Request",
		filters=filters,
		fields=["name", "driver", "customer", "delivery_note", "note", "photo", "status", "requested_at", "approved_by"],
		order_by="requested_at desc",
		ignore_permissions=True,
	)
	driver_names = list({r.driver for r in requests if r.driver})
	driver_labels = {}
	if driver_names:
		for d in frappe.get_all("Driver", filters={"name": ["in", driver_names]}, fields=["name", "full_name"], ignore_permissions=True):
			driver_labels[d.name] = d.full_name
	for r in requests:
		r["driver_name"] = driver_labels.get(r.driver)
	return {"requests": requests}


@frappe.whitelist(allow_guest=True)
def approve_delivery_request(name, trip_name=None):
	req = frappe.get_doc("Delivery Request", name)
	if req.status != "Requested":
		frappe.throw(_("This request was already {0}.").format(req.status.lower()))

	if trip_name and req.delivery_note:
		add_stops_to_trip(trip_name, [req.delivery_note])

	req.status = "Approved"
	req.approved_by = frappe.session.user
	req.save(ignore_permissions=True)
	frappe.db.commit()
	return {"name": req.name, "status": req.status}


@frappe.whitelist(allow_guest=True)
def reject_delivery_request(name, reason=None):
	req = frappe.get_doc("Delivery Request", name)
	if req.status != "Requested":
		frappe.throw(_("This request was already {0}.").format(req.status.lower()))

	req.status = "Rejected"
	req.approved_by = frappe.session.user
	if reason:
		req.note = f"{req.note or ''}\n\n[Rechazado] {reason}".strip()
	req.save(ignore_permissions=True)
	frappe.db.commit()
	return {"name": req.name, "status": req.status}


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
	  - 10 Customers with geocoded, zone-tagged shipping Addresses
	  - 10 submitted Delivery Notes (one per customer; 2 with a
	    requested_delivery_date for day-ahead planning demos)
	  - 1 Guest Preorder Sales Order per customer, confirmed (Tables > Pedidos
	    consistency - independent of the Delivery Note above, not derived from it)
	  - Company.custom_default_warehouse set (depot demo)
	  - 1 published trip (3 of the DNs) with a delivered stop (tracking code
	    + POD), a cliente-debe stop, and a driver login User
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
		demo_customer_ids = frappe.get_all(
			"Customer", filters={"customer_name": ["like", "TMS Demo Cliente%"]}, pluck="name", ignore_permissions=True
		)
		if demo_customer_ids:
			for so_name in frappe.get_all(
				"Sales Order", filters={"customer": ["in", demo_customer_ids]}, pluck="name", ignore_permissions=True
			):
				so_doc = frappe.get_doc("Sales Order", so_name)
				if so_doc.docstatus == 1:
					so_doc.flags.ignore_permissions = True
					so_doc.cancel()
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)
		for c in demo_customer_ids:
			frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)
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

	# ── 3. Customers + geocoded, zone-tagged shipping addresses ────────────────
	customers_seed = [
		{"name": "TMS Demo Cliente 1", "area": "Palermo", "addr": "Av. Santa Fe 3000", "lat": -34.5883, "lng": -58.4314, "zone": "Norte"},
		{"name": "TMS Demo Cliente 2", "area": "San Telmo", "addr": "Defensa 500", "lat": -34.6217, "lng": -58.3731, "zone": "Sur"},
		{"name": "TMS Demo Cliente 3", "area": "Belgrano", "addr": "Av. Cabildo 2000", "lat": -34.5537, "lng": -58.4560, "zone": "Norte"},
		{"name": "TMS Demo Cliente 4", "area": "Recoleta", "addr": "Av. Alvear 1800", "lat": -34.5875, "lng": -58.3951, "zone": "Centro"},
		{"name": "TMS Demo Cliente 5", "area": "Caballito", "addr": "Av. Rivadavia 4800", "lat": -34.6183, "lng": -58.4407, "zone": "Centro"},
		{"name": "TMS Demo Cliente 6", "area": "Flores", "addr": "Av. Directorio 2200", "lat": -34.6333, "lng": -58.4600, "zone": "Sur"},
		{"name": "TMS Demo Cliente 7", "area": "Villa Urquiza", "addr": "Av. Triunvirato 4500", "lat": -34.5722, "lng": -58.4880, "zone": "Norte"},
		{"name": "TMS Demo Cliente 8", "area": "Boedo", "addr": "Av. Boedo 1100", "lat": -34.6280, "lng": -58.4160, "zone": "Sur"},
		{"name": "TMS Demo Cliente 9", "area": "Almagro", "addr": "Av. Corrientes 4200", "lat": -34.6060, "lng": -58.4210, "zone": "Centro"},
		{"name": "TMS Demo Cliente 10", "area": "Núñez", "addr": "Av. Cabildo 4200", "lat": -34.5450, "lng": -58.4630, "zone": "Norte"},
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
				"custom_zone": c["zone"],
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
			"price_list_currency": "ARS",
			"conversion_rate": 1.0,
			"plc_conversion_rate": 1.0,
			# Last 2 customers demo day-ahead planning (Scenario 3).
			"custom_requested_delivery_date": frappe.utils.add_days(today, 1) if i > len(customers_seed) - 2 else None,
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
			"UPDATE `tabDelivery Note` SET docstatus=1, status='To Bill', customer_name=%s WHERE name=%s",
			(c["name"], dn.name),
		)
		frappe.db.commit()
		created.append(f"Delivery Note: {dn.name} (force-submitted)")

	# ── 5. Stock for the demo item (needed by the Guest Preorder → real
	#      Delivery Note path below, which - unlike the force-submitted DNs
	#      above - goes through normal stock validation on submit) ───────────
	STOCK_BUFFER = 50
	on_hand = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0)
	if on_hand < STOCK_BUFFER:
		se = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"posting_date": today,
			"to_warehouse": warehouse,
			"items": [{
				"item_code": item_code,
				"item_name": item_name,
				"qty": STOCK_BUFFER - on_hand,
				"uom": "Nos",
				"stock_uom": "Nos",
				"t_warehouse": warehouse,
				"basic_rate": 1,
			}],
			"remarks": "TMS demo - stock for guest-preorder-to-delivery-note flow",
		})
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		created.append(f"Stock: +{STOCK_BUFFER - on_hand:.0f} {item_code} in {warehouse}")

	# ── 6. Matching Guest Preorder per customer (Tables > Pedidos consistency) ──
	# Independent record, not derived from the Delivery Note above - same
	# customer name shows up in both places, but this isn't "the same order"
	# (Pedidos' own "Crear remito" action on this preorder would create a
	# second, separate Delivery Note if used).
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_guest_preorder_tag_fieldname,
		confirm_guest_preorder,
		create_guest_preorder,
	)

	tag_fn = _guest_preorder_tag_fieldname()
	for c in customers_seed:
		customer_id = frappe.db.get_value("Customer", {"customer_name": c["name"]}, "name")
		if not customer_id:
			continue
		if tag_fn and frappe.db.exists(
			"Sales Order", {"customer": customer_id, tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"]}
		):
			skipped.append(f"Guest Preorder for {c['name']}")
			continue
		res = create_guest_preorder(
			items=[{"item_code": item_code, "qty": 1, "rate": 5000.0}],
			customer=customer_id,
			company=company,
			guest_name=c["name"],
			is_delivery=1,
			guest_notes="TMS demo - pedido de referencia",
		)
		confirm_guest_preorder(res["preorder_name"])
		created.append(f"Guest Preorder: {res['preorder_name']} ({c['name']})")

	# ── 7. Company default depot (warehouse-based pickup demo) ─────────────────
	if frappe.db.get_value("Company", company, "custom_default_warehouse") != warehouse:
		frappe.db.set_value("Company", company, "custom_default_warehouse", warehouse, update_modified=False)
		created.append(f"Company default warehouse: {warehouse}")

	# ── 8. Driver login User (conductor app demo) ───────────────────────────────
	first_driver = frappe.db.get_value("Driver", {"full_name": drivers_seed[0]["full_name"]}, ["name", "employee"], as_dict=True)
	if first_driver and not frappe.db.get_value("Employee", first_driver.employee, "user_id"):
		login_email = "tms.demo.driver@example.com"
		if not frappe.db.exists("User", login_email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": login_email,
				"first_name": "TMS Demo",
				"last_name": "Driver",
				"enabled": 1,
				"send_welcome_email": 0,
			})
			user.flags.ignore_password_policy = True
			user.insert(ignore_permissions=True)
			from frappe.utils.password import update_password

			update_password(user=login_email, pwd="TmsDemo123!", logout_all_sessions=False)
			created.append(f"User: {login_email} (password: TmsDemo123!)")
		frappe.db.set_value("Employee", first_driver.employee, "user_id", login_email)

	# ── 9. One published trip (Juan/first driver) with a delivered stop + a
	#      cliente-debe stop - demos the dispatcher day board, the driver's
	#      trip history, and payment collection all at once ─────────────────
	demo_trip_dns = [
		frappe.db.get_value("Delivery Note", {"customer": c["name"], "docstatus": 1}, "name")
		for c in customers_seed[:3]
	]
	demo_trip_dns = [d for d in demo_trip_dns if d]
	already_planned = _assigned_delivery_note_names() if demo_trip_dns else set()
	demo_trip_dns = [d for d in demo_trip_dns if d not in already_planned]

	if demo_trip_dns and first_driver:
		driver_doc = frappe.db.get_value("Driver", first_driver.name, ["full_name", "address"], as_dict=True)
		vehicle_name = frappe.db.get_value("Vehicle", {"license_plate": vehicles_seed[0]["plate"]}, "name")

		trip = frappe.get_doc({
			"doctype": "Delivery Trip",
			"company": company,
			"driver": first_driver.name,
			"driver_name": driver_doc.full_name,
			"driver_address": driver_doc.address,
			"vehicle": vehicle_name,
			"departure_time": get_datetime(f"{today} 08:00:00"),
			"delivery_stops": [],
		})
		notes_by_name, address_display_by_name = _load_delivery_notes_for_stops(demo_trip_dns)
		_append_delivery_stops(trip, demo_trip_dns, notes_by_name, address_display_by_name)
		trip.insert(ignore_permissions=True)

		trip.flags.ignore_permissions = True
		trip.submit()

		# Stop 1: delivered, with POD + tracking code already generated above.
		stop1 = trip.delivery_stops[0]
		stop1.visited = 1
		stop1.custom_outcome = "Delivered"
		stop1.custom_pod_recipient_name = "Demo Recibió"
		stop1.custom_pod_recipient_id_number = "30111222"
		stop1.custom_pod_captured_at = now_datetime()
		stop1.custom_amount_due = stop1.grand_total
		stop1.custom_amount_collected = stop1.grand_total

		# Stop 2: delivered but the client owes half - cliente-debe demo.
		if len(trip.delivery_stops) > 1:
			stop2 = trip.delivery_stops[1]
			stop2.visited = 1
			stop2.custom_outcome = "Delivered"
			stop2.custom_pod_recipient_name = "Demo Recibió"
			stop2.custom_pod_recipient_id_number = "30333444"
			stop2.custom_pod_captured_at = now_datetime()
			stop2.custom_amount_due = stop2.grand_total
			stop2.custom_amount_collected = flt(stop2.grand_total) / 2
			stop2.custom_balance_after_stop = flt(stop2.grand_total) - flt(stop2.custom_amount_collected)
			stop2.custom_cliente_debe = 1

		trip.flags.ignore_validate_update_after_submit = True
		trip.save(ignore_permissions=True)
		created.append(f"Delivery Trip: {trip.name} (published, 1 delivered + 1 cliente-debe stop)")

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
def driver_record_stop_outcome(
	trip_name,
	stop_idx,
	outcome,
	recipient_name=None,
	recipient_id_number=None,
	signature_base64=None,
	notes=None,
	attempt_note=None,
	lat=None,
	lng=None,
	amount_collected=None,
	payment_method=None,
):
	"""Replaces/extends `driver_complete_stop` with an outcome enum.

	- Delivered: recipient name/ID + signature required (same rule the old
	  driver_complete_stop enforced). Marks the stop visited.
	- Not Home: a note is required. Deliberately does NOT mark the stop
	  visited - the linked Delivery Note stays open for replanning (see
	  `_assigned_delivery_note_names`).
	- Partial / Refused: a note + signature are required. Marks visited -
	  the attempt is resolved, even though nothing (or only some items)
	  changed hands.
	"""
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	if trip.docstatus != 1:
		frappe.throw(_("This route has not been published yet."))

	valid_outcomes = {"Delivered", "Not Home", "Partial", "Refused"}
	if outcome not in valid_outcomes:
		frappe.throw(_("Invalid outcome: {0}").format(outcome))

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	if outcome == "Delivered":
		if not recipient_name or not recipient_id_number:
			frappe.throw(_("Recipient name and ID/DNI are required to complete a delivery."))
		if not signature_base64:
			frappe.throw(_("A signature is required to complete a delivery."))
	elif outcome == "Not Home":
		if not attempt_note:
			frappe.throw(_("A note is required to record a failed delivery attempt."))
	else:  # Partial / Refused
		if not notes:
			frappe.throw(_("A note is required for a {0} outcome.").format(outcome))
		if not signature_base64:
			frappe.throw(_("A signature is required for a {0} outcome.").format(outcome))

	if signature_base64:
		file_doc = save_file(
			f"pod-signature-{trip_name}-{stop_idx}.png",
			signature_base64,
			"Delivery Trip",
			trip_name,
			decode=True,
			is_private=1,
		)
		stop.custom_pod_signature = file_doc.file_url

	stop.custom_outcome = outcome
	stop.custom_pod_recipient_name = recipient_name
	stop.custom_pod_recipient_id_number = recipient_id_number
	stop.custom_pod_notes = notes
	stop.custom_attempt_note = attempt_note
	stop.custom_pod_captured_at = now_datetime()
	stop.custom_pod_captured_lat = flt(lat) if lat else None
	stop.custom_pod_captured_lng = flt(lng) if lng else None
	stop.visited = 1 if outcome in ("Delivered", "Partial", "Refused") else 0

	# Payment: only relevant once something changed hands (Delivered/Partial).
	# "separate_collector" mode means someone else collects later, so the
	# driver isn't asked for these fields at all - ignore anything sent.
	if outcome in ("Delivered", "Partial"):
		settings = _load_tms_settings()
		amount_due = flt(stop.grand_total)
		stop.custom_amount_due = amount_due
		if settings.get("delivery_payment_mode") != "separate_collector" and amount_collected is not None:
			stop.custom_amount_collected = flt(amount_collected)
			stop.custom_payment_method = payment_method
			balance = amount_due - flt(amount_collected)
			stop.custom_balance_after_stop = balance
			stop.custom_cliente_debe = 1 if balance > 0 else 0

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)


@frappe.whitelist()
def driver_mark_cliente_debe(trip_name, stop_idx, amount, note=None):
	"""Flag an outstanding balance at a stop, independent of the delivery
	outcome itself - doesn't block or require POD to already exist."""
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	stop.custom_cliente_debe = 1
	stop.custom_balance_after_stop = flt(amount)
	if note:
		stop.custom_pod_notes = f"{stop.custom_pod_notes or ''}\n\n[Cliente debe] {note}".strip()

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)


@frappe.whitelist(allow_guest=True)
def list_cliente_debe_stops(date=None):
	"""Stops flagged 'cliente debe' with an outstanding balance - the
	"collector" view. Scoped by company only (no separate Collector identity
	exists in this system yet)."""
	filters = {"custom_cliente_debe": 1}
	trip_filters = {"docstatus": 1}
	if date:
		day = getdate(date)
		trip_filters["departure_time"] = ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]]

	trip_names = frappe.get_all("Delivery Trip", filters=trip_filters, pluck="name", ignore_permissions=True)
	if not trip_names:
		return {"stops": []}

	rows = frappe.get_all(
		"Delivery Stop",
		filters={**filters, "parent": ["in", trip_names]},
		fields=[
			"parent as trip_name",
			"idx",
			"customer",
			"customer_address",
			"delivery_note",
			"custom_amount_due",
			"custom_amount_collected",
			"custom_balance_after_stop",
		],
		ignore_permissions=True,
	)
	return {"stops": rows}


@frappe.whitelist()
def driver_settle_payment(trip_name, stop_idx, amount_collected, payment_method=None):
	"""Record a later payment collection against an existing stop (the
	"separate collector" flow)."""
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	collected_so_far = flt(stop.custom_amount_collected) + flt(amount_collected)
	balance = flt(stop.custom_amount_due) - collected_so_far

	stop.custom_amount_collected = collected_so_far
	stop.custom_payment_method = payment_method or stop.custom_payment_method
	stop.custom_balance_after_stop = balance
	stop.custom_cliente_debe = 1 if balance > 0 else 0

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)


# ---------------------------------------------------------------------------
# Print (delivery / payment / return receipts)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_delivery_print_data(trip_name, stop_idx):
	"""Merges the Delivery Note's own fields with the specific Delivery Stop
	row's POD/outcome/payment fields into one flat dict - the generic
	print_templates_api.get_print_data(doctype, docname) only sees the DN
	itself, but a delivery ticket needs the stop's signature/outcome/amount
	collected too, and those live on the Trip's child table."""
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	dn_data = {}
	line_items = []
	if stop.delivery_note:
		dn = frappe.get_doc("Delivery Note", stop.delivery_note)
		dn_data = dn.as_dict()
		line_items = [row.as_dict() for row in dn.items]

	stop_out = _stop_out(stop)

	return {
		"doc": {
			**dn_data,
			"trip_name": trip.name,
			"driver_name": trip.driver_name,
			"stop_customer": stop.customer,
			"stop_address": stop.customer_address,
			**(stop_out.get("pod") or {}),
		},
		"lineItems": line_items,
	}


# ---------------------------------------------------------------------------
# In-premise returns
# ---------------------------------------------------------------------------


@frappe.whitelist()
def driver_record_return_capture(trip_name, stop_idx, lines, signature_base64, notes=None, photo_base64_list=None):
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	lines = frappe.parse_json(lines) if isinstance(lines, str) else (lines or [])
	if not lines:
		frappe.throw(_("Add at least one returned item."))
	if not signature_base64:
		frappe.throw(_("A signature is required to capture a return."))

	capture = frappe.get_doc(
		{
			"doctype": "Mobile Return Capture",
			"delivery_trip": trip_name,
			"stop_idx": stop_idx,
			"customer": stop.customer,
			"status": "Captured",
			"captured_at": now_datetime(),
			"notes": notes,
			"lines": [
				{
					"item_code": line.get("item_code"),
					"description": line.get("description"),
					"qty": flt(line.get("qty")),
					"reason": line.get("reason"),
				}
				for line in lines
			],
		}
	)
	capture.insert(ignore_permissions=True)

	sig_file = save_file(
		f"return-signature-{capture.name}.png",
		signature_base64,
		"Mobile Return Capture",
		capture.name,
		decode=True,
		is_private=1,
	)
	capture.db_set("signature", sig_file.file_url, update_modified=False)

	photo_base64_list = frappe.parse_json(photo_base64_list) if isinstance(photo_base64_list, str) else (photo_base64_list or [])
	photo_urls = []
	for i, photo_b64 in enumerate(photo_base64_list, 1):
		photo_file = save_file(
			f"return-photo-{capture.name}-{i}.jpg",
			photo_b64,
			"Mobile Return Capture",
			capture.name,
			decode=True,
			is_private=1,
		)
		photo_urls.append(photo_file.file_url)
	if photo_urls:
		capture.db_set("photo_urls", frappe.as_json(photo_urls), update_modified=False)

	frappe.db.commit()
	return {"name": capture.name, "status": capture.status}


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
	"""Thin backward-compatible wrapper - a plain "Delivered" outcome."""
	return driver_record_stop_outcome(
		trip_name,
		stop_idx,
		outcome="Delivered",
		recipient_name=recipient_name,
		recipient_id_number=recipient_id_number,
		signature_base64=signature_base64,
		notes=notes,
		lat=lat,
		lng=lng,
	)


@frappe.whitelist()
def driver_upload_stop_photo(trip_name, stop_idx, image_base64):
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	if trip.docstatus != 1:
		frappe.throw(_("This route has not been published yet."))

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	existing = frappe.parse_json(stop.custom_photo_urls) if stop.custom_photo_urls else []
	if not isinstance(existing, list):
		existing = []

	file_doc = save_file(
		f"pod-photo-{trip_name}-{stop_idx}-{len(existing) + 1}.jpg",
		image_base64,
		"Delivery Trip",
		trip_name,
		decode=True,
		is_private=1,
	)
	existing.append(file_doc.file_url)

	stop.custom_photo_urls = frappe.as_json(existing)

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)
