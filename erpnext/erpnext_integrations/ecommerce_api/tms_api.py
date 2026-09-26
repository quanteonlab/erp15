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
from frappe.utils import cint, cstr, flt, get_datetime, getdate, now_datetime
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


def _clean_address_display(text):
	"""Address display sometimes stores literal ``<br>`` from ERPNext templates."""
	if not text:
		return text
	import re

	s = re.sub(r"<br\s*/?>", ", ", str(text), flags=re.IGNORECASE)
	s = re.sub(r"\s*,\s*", ", ", s)
	s = re.sub(r"(,\s*)+", ", ", s)
	return s.strip(" ,")


def _stop_detail_meta(stops):
	"""Batch-load Customer + Address fields used on the Entregas stop detail card."""
	customer_names = list({s.customer for s in stops if s.customer})
	address_names = list({s.address for s in stops if s.address})
	customer_meta = {}
	address_meta = {}
	if customer_names:
		fields = ["name", "customer_name", "customer_details"]
		if frappe.db.has_column("Customer", "custom_preferred_hours"):
			fields.append("custom_preferred_hours")
		for row in frappe.get_all(
			"Customer",
			filters={"name": ["in", customer_names]},
			fields=fields,
			ignore_permissions=True,
		):
			customer_meta[row.name] = row
	if address_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=["name", "address_line1", "address_line2", "city", "custom_latitude", "custom_longitude"],
			ignore_permissions=True,
		):
			address_meta[row.name] = row
	return customer_meta, address_meta


def _stops_payload(trip, geo_by_address=None):
	geo_by_address = geo_by_address or {}
	ordered = sorted(trip.delivery_stops, key=lambda r: r.idx)
	customer_meta, address_meta = _stop_detail_meta(ordered)
	# Merge address lat/lng into geo cache when missing
	for name, addr in address_meta.items():
		if name not in geo_by_address and (addr.get("custom_latitude") or addr.get("custom_longitude")):
			geo_by_address[name] = addr
	return [_stop_out(s, geo_by_address, customer_meta, address_meta) for s in ordered]


def _stop_out(stop, address_geo=None, customer_meta=None, address_meta=None):
	address_geo = address_geo or {}
	customer_meta = customer_meta or {}
	address_meta = address_meta or {}
	geo = address_geo.get(stop.address) or {}
	outcome = stop.get("custom_outcome") if hasattr(stop, "get") else getattr(stop, "custom_outcome", None)
	cust = customer_meta.get(stop.customer) or {}
	addr = address_meta.get(stop.address) or {}
	addr_line = addr.get("address_line1") or None
	return {
		"idx": stop.idx,
		"customer": stop.customer,
		"customer_name": cust.get("customer_name") or stop.customer,
		"customer_address": _clean_address_display(stop.customer_address),
		"address": stop.address,
		"address_line": _clean_address_display(addr_line) if addr_line else _clean_address_display(stop.customer_address),
		"preferred_hours": cust.get("custom_preferred_hours") or None,
		"comments": cust.get("customer_details") or None,
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
		fields=["name", "full_name", "cell_number", "address", "employee", "license_number"],
		ignore_permissions=True,
	)
	emp_ids = [d.employee for d in drivers if d.employee]
	plate_by_emp = {}
	user_by_emp = {}
	if emp_ids:
		for row in frappe.get_all(
			"Vehicle",
			filters={"employee": ["in", emp_ids]},
			fields=["employee", "license_plate", "name"],
			ignore_permissions=True,
		):
			if row.employee and row.employee not in plate_by_emp:
				plate_by_emp[row.employee] = row.license_plate or row.name
		for row in frappe.get_all(
			"Employee",
			filters={"name": ["in", emp_ids]},
			fields=["name", "user_id"],
			ignore_permissions=True,
		):
			user_by_emp[row.name] = row.user_id
	for d in drivers:
		d["license_plate"] = plate_by_emp.get(d.employee)
		d["user_id"] = user_by_emp.get(d.employee)
		d["has_user"] = bool(d.get("user_id"))

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
	"""Unassigned remitos ready for routing (reduced-bureaucracy queue).

	``date`` is an *as-of* ceiling — not an exclusive day bucket. Include every
	submitted DN that is not on an active trip whose **target ship date**
	(Sales Order ``delivery_date``, else DN ``posting_date``) is on or before
	``as_of``. Yesterday's unshipped work still shows when planning "today";
	future-dated work stays out until its target day.
	"""
	assigned_notes = _assigned_delivery_note_names()
	raw_date = date
	if isinstance(raw_date, str):
		raw_date = raw_date.strip()
		if raw_date.lower() in ("", "null", "undefined", "none"):
			raw_date = None
	try:
		as_of = getdate(raw_date) if raw_date else getdate()
	except Exception:
		as_of = getdate()
	# Lookback only on posting_date (fetch window). Due ≤ as_of is applied below.
	from frappe.utils import add_days

	since = add_days(as_of, -120)

	filters = {
		"docstatus": 1,
		"is_return": 0,
		"posting_date": [">=", since],
	}
	if assigned_notes:
		filters["name"] = ["not in", assigned_notes]
	if company and str(company).strip() and str(company).strip().lower() not in ("null", "undefined", "none"):
		filters["company"] = str(company).strip()

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
			"status",
		],
		order_by="posting_date asc, creation asc",
		limit_page_length=500,
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

	# Linked Sales Order status (guest preorder workflow) when present
	so_by_dn = {}
	if names:
		for row in frappe.db.sql(
			"""
			select parent as dn, against_sales_order as so
			from `tabDelivery Note Item`
			where parent in %(names)s and ifnull(against_sales_order, '') != ''
			group by parent
			""",
			{"names": names},
			as_dict=True,
		):
			so_by_dn[row.dn] = row.so
	so_status = {}
	so_names = list({v for v in so_by_dn.values() if v})
	if so_names:
		for row in frappe.get_all(
			"Sales Order",
			filters={"name": ["in", so_names]},
			fields=["name", "status", "delivery_date"],
			ignore_permissions=True,
		):
			so_status[row.name] = row

	address_names = list(
		{n.shipping_address_name or n.customer_address for n in notes if (n.shipping_address_name or n.customer_address)}
	)
	geo_by_address = {}
	if address_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=[
				"name",
				"address_line1",
				"address_line2",
				"city",
				"custom_latitude",
				"custom_longitude",
				"custom_zone",
			],
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
		so_name = so_by_dn.get(n.name)
		so = so_status.get(so_name) if so_name else None
		due = None
		if so and so.get("delivery_date"):
			due = so.get("delivery_date")
		else:
			due = n.posting_date
		due_d = getdate(due) if due else as_of
		# Not ready yet — target ship day is after the planning as-of.
		if due_d > as_of:
			continue
		overdue = due_d < as_of

		display = _rutas_order_display_status(
			dn_status=n.status,
			so_status=(so.get("status") if so else None),
			previous_attempt=n.name in previous_attempts,
			overdue=overdue,
		)

		# Prefer street line for the Órdenes table / map — not the Address doc name
		# (Frappe names look like "Title-City-…" and confuse dispatchers / geocoders).
		street_bits = [
			cstr(geo.get("address_line1") or "").strip(),
			cstr(geo.get("address_line2") or "").strip(),
			cstr(geo.get("city") or "").strip(),
		]
		street_display = ", ".join(b for b in street_bits if b) or address_name

		out.append(
			{
				"delivery_note": n.name,
				"customer": n.customer,
				"customer_name": n.customer_name,
				"address": street_display,
				"address_name": address_name,
				"grand_total": n.grand_total,
				"posting_date": n.posting_date,
				"due_date": str(due_d),
				"overdue": overdue,
				"status": display,
				"dn_status": n.status,
				"so_status": (so.get("status") if so else None),
				"sales_order": so_name,
				"item_count": (item_counts.get(n.name) or {}).get("item_count", 0),
				"qty_total": (item_counts.get(n.name) or {}).get("qty_total", 0),
				"geocoded": bool(geo.get("custom_latitude")),
				"lat": geo.get("custom_latitude"),
				"lng": geo.get("custom_longitude"),
				"zone": geo.get("custom_zone") or None,
				"previous_attempt": n.name in previous_attempts,
			}
		)

	# Overdue / oldest target dates first so the dispatcher clears the backlog.
	out.sort(key=lambda r: (r.get("due_date") or "", r.get("delivery_note") or ""))
	return {"deliveries": out, "as_of": str(as_of)}


def _rutas_order_display_status(dn_status=None, so_status=None, previous_attempt=False, overdue=False):
	"""Coarse status for the Rutas Órdenes table / filters."""
	if previous_attempt:
		return "retry"
	so = str(so_status or "").strip()
	if so in ("Preparado",):
		return "prepared"
	if so in ("Orden", "To Deliver and Bill", "To Deliver"):
		return "pending"
	if so in ("En Delivery",):
		return "in_delivery"
	if so in ("Completado", "Completed", "Closed"):
		return "completed"
	dn = str(dn_status or "").strip()
	if dn in ("Completed", "Closed"):
		return "completed"
	if overdue:
		return "overdue"
	return "pending"


@frappe.whitelist(allow_guest=True)
def update_pending_delivery_zone(delivery_note=None, zone=None):
	"""Set Address.custom_zone for the shipping address on a Delivery Note."""
	dn = cstr(delivery_note or "").strip()
	zone_label = cstr(zone or "").strip()
	if not dn:
		frappe.throw(_("Delivery Note is required."))
	if not frappe.db.exists("Delivery Note", dn):
		frappe.throw(_("Delivery Note not found."))
	addr_name = frappe.db.get_value("Delivery Note", dn, "shipping_address_name") or frappe.db.get_value(
		"Delivery Note", dn, "customer_address"
	)
	if not addr_name or not frappe.db.exists("Address", addr_name):
		frappe.throw(_("No shipping address on this Delivery Note."))
	if not frappe.db.has_column("Address", "custom_zone"):
		frappe.throw(_("Address.custom_zone is not available on this site."))
	frappe.db.set_value("Address", addr_name, "custom_zone", zone_label or None, update_modified=True)
	frappe.db.commit()
	return {"delivery_note": dn, "zone": zone_label or None, "address": addr_name}


@frappe.whitelist(allow_guest=True)
def update_pending_delivery_due(delivery_note=None, due_date=None):
	"""Update the planning due date for a pending remito (Sales Order.delivery_date)."""
	dn = cstr(delivery_note or "").strip()
	due = cstr(due_date or "").strip()
	if not dn:
		frappe.throw(_("Delivery Note is required."))
	if not due:
		frappe.throw(_("Due date is required."))
	try:
		due_d = getdate(due)
	except Exception:
		frappe.throw(_("Invalid due date."))
	if not frappe.db.exists("Delivery Note", dn):
		frappe.throw(_("Delivery Note not found."))

	so_names = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": dn, "against_sales_order": ["is", "set"]},
		pluck="against_sales_order",
		ignore_permissions=True,
	)
	so_names = list({s for s in so_names if s})
	if not so_names:
		frappe.throw(_("No Sales Order linked to this Delivery Note; cannot update due date."))
	for so in so_names:
		frappe.db.set_value("Sales Order", so, "delivery_date", due_d, update_modified=True)
	frappe.db.commit()
	return {"delivery_note": dn, "due_date": str(due_d), "sales_orders": so_names}


def _is_desk_admin_user():
	roles = set(frappe.get_roles(frappe.session.user))
	return bool(roles & {"Administrator", "System Manager"})


def _require_rutas_order_pin(pin=None):
	"""Gate claim/create-order actions on Rutas.

	When ``require_pin_for_order_actions`` is on:
	- desk admins (Administrator / System Manager) skip the PIN
	- if Admin PIN is configured, require a valid PIN
	- if Admin PIN is not configured, only desk admins may proceed (safer than open)
	"""
	settings = _load_tms_settings()
	if not settings.get("require_pin_for_order_actions", True):
		return

	if _is_desk_admin_user():
		return

	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
		_pin_configured,
		_verify_pin_value,
	)

	if not _pin_configured():
		frappe.throw(
			_("Admin PIN is not configured. Only System Manager can claim or create orders on Routes."),
			frappe.PermissionError,
		)

	if not pin or not _verify_pin_value(str(pin).strip()):
		frappe.throw(_("Incorrect Admin PIN"), frappe.AuthenticationError)


def _list_claimable_preorders(company=None):
	"""Confirmed guest preorders (Orden/Preparado) that still lack a Delivery Note."""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_delivery_note_for_sales_order,
		_guest_preorder_tag_fieldname,
		_is_guest_preorder_sales_order,
	)

	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return []

	filters = {
		tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"],
		"docstatus": 1,
		"status": ["in", ["To Deliver and Bill", "To Deliver", "Preparado", "Orden"]],
	}
	if company:
		filters["company"] = company

	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"shipping_address_name",
			"customer_address",
			"grand_total",
			"transaction_date",
			"delivery_date",
			"status",
			tag_fn,
		],
		order_by="transaction_date asc, creation asc",
		limit_page_length=200,
		ignore_permissions=True,
	)

	out = []
	for o in orders:
		if not _is_guest_preorder_sales_order(o):
			continue
		if _delivery_note_for_sales_order(o.name):
			continue
		# Skip already-in-delivery / completed display statuses
		display = str(o.status or "")
		if display in ("En Delivery", "Completado", "Closed", "Completed"):
			continue
		address_name = o.shipping_address_name or o.customer_address
		out.append(
			{
				"kind": "preorder",
				"preorder_name": o.name,
				"delivery_note": None,
				"customer": o.customer,
				"customer_name": o.customer_name,
				"address": address_name,
				"grand_total": o.grand_total,
				"posting_date": o.transaction_date or o.delivery_date,
				"status": display,
				"item_count": 0,
				"qty_total": 0,
				"geocoded": False,
				"lat": None,
				"lng": None,
				"previous_attempt": False,
			}
		)
	return out


@frappe.whitelist(allow_guest=True)
def list_claimable_orders(date=None, company=None):
	"""Pending submitted DNs not on active trips, plus confirmed guest preorders without a DN."""
	pending = get_pending_deliveries(date=date, company=company)
	deliveries = []
	for d in pending.get("deliveries") or []:
		row = dict(d)
		row["kind"] = "delivery_note"
		row["preorder_name"] = None
		deliveries.append(row)

	preorders = _list_claimable_preorders(company=company)
	# Attach geo for preorders that have an address
	address_names = list({p["address"] for p in preorders if p.get("address")})
	geo_by_address = {}
	if address_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=["name", "custom_latitude", "custom_longitude"],
			ignore_permissions=True,
		):
			geo_by_address[row.name] = row
	for p in preorders:
		geo = geo_by_address.get(p.get("address")) or {}
		p["geocoded"] = bool(geo.get("custom_latitude"))
		p["lat"] = geo.get("custom_latitude")
		p["lng"] = geo.get("custom_longitude")

	return {"orders": deliveries + preorders, "deliveries": deliveries, "preorders": preorders}


@frappe.whitelist(allow_guest=True)
def claim_orders_to_trip(
	delivery_notes=None,
	preorder_names=None,
	trip=None,
	driver=None,
	vehicle=None,
	pickup_warehouse=None,
	date=None,
	company=None,
	pin=None,
):
	"""Create remitos for claimable preorders, then add all DNs to a trip (or create one)."""
	def _as_name_list(raw):
		if raw is None:
			return []
		if isinstance(raw, str):
			raw = raw.strip()
			if not raw or raw in ("null", "undefined", "None"):
				return []
			try:
				parsed = frappe.parse_json(raw)
			except Exception:
				# Single name as bare string
				return [raw] if raw else []
			raw = parsed
		if not isinstance(raw, (list, tuple)):
			return [raw] if raw else []
		return [n for n in raw if n not in (None, "", "null", "undefined")]

	_require_rutas_order_pin(pin)

	delivery_notes = _as_name_list(delivery_notes)
	preorder_names = _as_name_list(preorder_names)

	if not delivery_notes and not preorder_names:
		frappe.throw(_("Select at least one order to claim."))

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_delivery_note_for_sales_order,
		_is_guest_preorder_sales_order,
	)
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	created_dns = []
	for so_name in preorder_names:
		if not frappe.db.exists("Sales Order", so_name):
			frappe.throw(_("Sales Order {0} not found").format(so_name))
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", so_name)
		frappe.flags.ignore_permissions = False
		if not _is_guest_preorder_sales_order(so):
			frappe.throw(_("Not a Guest Preorder: {0}").format(so_name))
		if so.docstatus != 1:
			frappe.throw(_("Confirm the order before claiming: {0}").format(so_name))

		dn_name = _delivery_note_for_sales_order(so_name)
		if not dn_name:
			dn = make_delivery_note(so_name)
			default_warehouse = frappe.db.get_value("Company", dn.company, "custom_default_warehouse")
			if default_warehouse:
				for row in dn.items:
					row.warehouse = default_warehouse
				dn.set_warehouse = default_warehouse
			dn.insert(ignore_permissions=True)
			dn.flags.ignore_permissions = True
			dn.submit()
			dn_name = dn.name
			frappe.db.commit()
		try:
			frappe.db.set_value("Sales Order", so_name, "status", "En Delivery")
			frappe.db.commit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "claim_orders_to_trip status")
		created_dns.append(dn_name)

	all_dns = list(dict.fromkeys([*delivery_notes, *created_dns]))
	if not all_dns:
		frappe.throw(_("No delivery notes to add to the trip."))

	if trip:
		map_data = add_stops_to_trip(trip, all_dns)
		return {
			"trip": trip,
			"delivery_notes": all_dns,
			"created_delivery_notes": created_dns,
			"map": map_data,
		}

	day = date or str(getdate())
	created = create_trip(
		date=day,
		driver=driver,
		vehicle=vehicle,
		delivery_note_names=all_dns,
		company=company,
		pickup_warehouse=pickup_warehouse,
	)
	return {
		"trip": created.get("trip"),
		"delivery_notes": all_dns,
		"created_delivery_notes": created_dns,
		"status": created.get("status"),
		"stop_count": created.get("stop_count"),
	}


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

	stops = _stops_payload(trip, geo_by_address)

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

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook(
			"planned",
			{
				"trip": trip.name,
				"driver": trip.driver,
				"vehicle": trip.vehicle,
				"stop_count": len(trip.delivery_stops or []),
			},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook planned")

	return {"trip": trip.name, "status": trip.status}


@frappe.whitelist(allow_guest=True)
def list_trips_for_date(date=None, company=None, horizon_days=1):
	"""List trips for ``date`` (as-of day) or a trailing window of ``horizon_days``.

	``horizon_days=1`` → that calendar day only.
	``horizon_days=7`` → the 7 days ending on ``date`` (week view for fleet stats).
	Each trip includes ``stop_count``, ``delivered_count``, ``pending_count``, ``vehicle_plate``.
	"""
	raw_date = date
	if isinstance(raw_date, str):
		raw_date = raw_date.strip()
		if raw_date.lower() in ("", "null", "undefined", "none"):
			raw_date = None
	try:
		end = getdate(raw_date) if raw_date else getdate()
	except Exception:
		end = getdate()
	horizon = cint(horizon_days)
	if horizon < 1:
		horizon = 1
	if horizon > 31:
		horizon = 31
	from frappe.utils import add_days

	start = add_days(end, -(horizon - 1))

	filters = {
		"docstatus": ["!=", 2],
		"departure_time": ["between", [f"{start} 00:00:00", f"{end} 23:59:59"]],
	}
	if company and str(company).strip() and str(company).strip().lower() not in ("null", "undefined", "none"):
		filters["company"] = str(company).strip()

	trips = frappe.get_all(
		"Delivery Trip",
		filters=filters,
		fields=["name", "status", "docstatus", "driver", "driver_name", "vehicle", "departure_time", "total_distance", "uom"],
		order_by="departure_time asc",
		ignore_permissions=True,
	)

	trip_names = [t.name for t in trips]
	stop_stats = {}
	if trip_names:
		for row in frappe.db.sql(
			"""
			select parent,
				count(*) as stop_count,
				sum(
					case
						when ifnull(visited, 0) = 1 then 1
						when ifnull(custom_outcome, '') != '' then 1
						else 0
					end
				) as delivered_count
			from `tabDelivery Stop`
			where parent in %(names)s
			group by parent
			""",
			{"names": trip_names},
			as_dict=True,
		):
			stop_stats[row.parent] = row

	vehicle_names = list({t.vehicle for t in trips if t.vehicle})
	plate_by_vehicle = {}
	if vehicle_names:
		for row in frappe.get_all(
			"Vehicle",
			filters={"name": ["in", vehicle_names]},
			fields=["name", "license_plate"],
			ignore_permissions=True,
		):
			plate_by_vehicle[row.name] = row.license_plate or row.name

	for t in trips:
		stats = stop_stats.get(t.name) or {}
		stop_count = cint(stats.get("stop_count") or 0)
		delivered = cint(stats.get("delivered_count") or 0)
		if delivered > stop_count:
			delivered = stop_count
		t["stop_count"] = stop_count
		t["delivered_count"] = delivered
		t["pending_count"] = max(0, stop_count - delivered)
		t["vehicle_plate"] = plate_by_vehicle.get(t.vehicle) if t.vehicle else None

	return {
		"date": str(end),
		"from_date": str(start),
		"horizon_days": horizon,
		"trips": trips,
	}


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


def _parse_delivery_note_order(delivery_note_names):
	"""Normalize a dirty frontend list of DN names into an ordered unique list."""
	raw = delivery_note_names
	if isinstance(raw, str):
		raw = raw.strip()
		if raw.lower() in ("", "null", "undefined", "none"):
			raw = None
		else:
			try:
				raw = frappe.parse_json(raw)
			except Exception:
				raw = [raw] if raw else None
	if raw is None:
		frappe.throw(_("Provide the stop order as a list of delivery notes."))
	if not isinstance(raw, (list, tuple)):
		frappe.throw(_("Stop order must be a list of delivery notes."))

	names = []
	seen = set()
	for item in raw:
		if item is None:
			continue
		dn = str(item).strip()
		if not dn or dn.lower() in ("null", "undefined", "none"):
			continue
		if dn in seen:
			continue
		seen.add(dn)
		names.append(dn)

	if not names:
		frappe.throw(_("Provide the stop order as a list of delivery notes."))
	return names


def _apply_trip_stop_order(trip, delivery_note_names):
	"""Rewrite Delivery Stop ``idx`` to match ``delivery_note_names`` (1-based).

	Works on Draft (save) and Submitted (direct idx update) trips. Stops without
	a delivery_note stay at the end in their previous relative order.
	"""
	names = _parse_delivery_note_order(delivery_note_names)
	by_dn = {s.delivery_note: s for s in trip.delivery_stops if s.delivery_note}
	missing = [dn for dn in names if dn not in by_dn]
	if missing:
		frappe.throw(_("Stop not on this trip: {0}").format(missing[0]))
	extra = [dn for dn in by_dn if dn not in set(names)]
	if extra:
		frappe.throw(_("Stop order must include every delivery note on the trip."))

	ordered = [by_dn[dn] for dn in names]
	orphans = [s for s in trip.delivery_stops if not s.delivery_note]
	final_rows = ordered + orphans

	if trip.docstatus == 0:
		for i, row in enumerate(final_rows, start=1):
			row.idx = i
		trip.save(ignore_permissions=True)
	elif trip.docstatus == 1:
		# Submitted trips can't use Document.save for child reorder — set idx directly.
		for i, row in enumerate(final_rows, start=1):
			frappe.db.set_value("Delivery Stop", row.name, "idx", i, update_modified=False)
	else:
		frappe.throw(_("Cancelled trips cannot be reordered."))

	frappe.db.commit()
	return names


@frappe.whitelist(allow_guest=True)
def reorder_trip_stops(trip_name, delivery_note_names=None):
	"""Rewrite Delivery Stop ``idx`` to match ``delivery_note_names`` order (1-based).

	Draft trips only (dispatcher). Dirty ``null`` / ``""`` / partial lists return
	controlled errors. Drivers use ``driver_reorder_stops`` instead.
	"""
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	if trip.docstatus != 0:
		frappe.throw(_("Stops can only be reordered on a Draft trip."))

	_apply_trip_stop_order(trip, delivery_note_names)
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


def _dirty_str(val):
	if val is None:
		return None
	s = cstr(val).strip()
	if s.lower() in ("", "null", "undefined", "none"):
		return None
	return s


def _vehicle_plate_for_employee(employee):
	if not employee:
		return None
	row = frappe.get_all(
		"Vehicle",
		filters={"employee": employee},
		fields=["name", "license_plate"],
		limit=1,
		ignore_permissions=True,
	)
	return (row[0].license_plate or row[0].name) if row else None


def _set_vehicle_plate_for_employee(employee, license_plate):
	"""Link (or create) a Vehicle with this plate to the employee's Driver."""
	if not employee:
		return None
	plate = _dirty_str(license_plate)
	# Unlink previous vehicles for this employee when clearing plate
	prev = frappe.get_all(
		"Vehicle",
		filters={"employee": employee},
		pluck="name",
		ignore_permissions=True,
	)
	if not plate:
		for vn in prev:
			frappe.db.set_value("Vehicle", vn, "employee", None, update_modified=False)
		return None

	existing = frappe.db.exists("Vehicle", plate) or frappe.db.get_value(
		"Vehicle", {"license_plate": plate}, "name"
	)
	if existing:
		frappe.db.set_value("Vehicle", existing, "employee", employee, update_modified=False)
		veh_name = existing
	else:
		veh = frappe.get_doc(
			{
				"doctype": "Vehicle",
				"license_plate": plate,
				"make": "N/A",
				"model": "N/A",
				"fuel_type": "Petrol",
				"last_odometer": 0,
				"uom": "Unit",
				"employee": employee,
			}
		)
		veh.insert(ignore_permissions=True)
		veh_name = veh.name

	for vn in prev:
		if vn != veh_name:
			frappe.db.set_value("Vehicle", vn, "employee", None, update_modified=False)
	return veh_name


def _serialize_driver(driver_name):
	frappe.flags.ignore_permissions = True
	d = frappe.get_doc("Driver", driver_name)
	frappe.flags.ignore_permissions = False
	emp = d.employee
	user_id = frappe.db.get_value("Employee", emp, "user_id") if emp else None
	user_enabled = None
	if user_id and frappe.db.exists("User", user_id):
		user_enabled = cint(frappe.db.get_value("User", user_id, "enabled"))
	return {
		"name": d.name,
		"full_name": d.full_name,
		"cell_number": d.cell_number,
		"address": d.address,
		"license_number": d.license_number,
		"license_plate": _vehicle_plate_for_employee(emp),
		"employee": emp,
		"user_id": user_id,
		"has_user": bool(user_id and frappe.db.exists("User", user_id)),
		"user_enabled": user_enabled,
		"status": d.status,
	}


def _ensure_driver_staff_group():
	"""Ensure Employee Group titled ``Driver`` (or legacy ``driver``) exists."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_ensure_starter_staff_groups,
		_find_employee_group_by_title,
	)

	_ensure_starter_staff_groups()
	return _find_employee_group_by_title("Driver") or _find_employee_group_by_title("driver")


def _rand_driver_password(length: int = 16) -> str:
	specials = "!@#$%^*?"
	alphabet = string.ascii_letters + string.digits + specials
	chars = [
		secrets.choice(string.ascii_uppercase),
		secrets.choice(string.ascii_lowercase),
		secrets.choice(string.digits),
		secrets.choice(specials),
	]
	chars += [secrets.choice(alphabet) for _ in range(max(0, length - 4))]
	secrets.SystemRandom().shuffle(chars)
	return "".join(chars)


@frappe.whitelist(allow_guest=True)
def get_driver_detail(driver=None):
	name = _dirty_str(driver)
	if not name:
		frappe.throw(_("Driver is required."))
	if not frappe.db.exists("Driver", name):
		frappe.throw(_("Driver {0} not found").format(name), frappe.DoesNotExistError)
	return _serialize_driver(name)


@frappe.whitelist(allow_guest=True)
def update_driver(
	driver=None,
	full_name=None,
	cell_number=None,
	code=None,
	license_plate=None,
	license_number=None,
):
	"""Update Driver name/phone/plate; optional rename via ``code``."""
	name = _dirty_str(driver)
	if not name:
		frappe.throw(_("Driver is required."))
	if not frappe.db.exists("Driver", name):
		frappe.throw(_("Driver {0} not found").format(name), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Driver", name)
	frappe.flags.ignore_permissions = False

	new_full = _dirty_str(full_name)
	if new_full is not None:
		doc.full_name = new_full
		if doc.employee and frappe.db.exists("Employee", doc.employee):
			parts = new_full.split()
			frappe.db.set_value(
				"Employee",
				doc.employee,
				{
					"employee_name": new_full,
					"first_name": parts[0],
					"last_name": " ".join(parts[1:]) or parts[0],
				},
				update_modified=False,
			)

	# Always allow clearing phone when key present as empty-after-dirty
	if cell_number is not None:
		doc.cell_number = _dirty_str(cell_number) or ""

	if license_number is not None:
		doc.license_number = _dirty_str(license_number) or ""

	doc.save(ignore_permissions=True)

	if license_plate is not None:
		_set_vehicle_plate_for_employee(doc.employee, license_plate)

	new_code = _dirty_str(code)
	final_name = doc.name
	if new_code and new_code != doc.name:
		if frappe.db.exists("Driver", new_code):
			frappe.throw(_("Driver code {0} already exists.").format(new_code))
		frappe.rename_doc("Driver", doc.name, new_code, force=True, merge=False)
		final_name = new_code

	frappe.db.commit()
	return _serialize_driver(final_name)


@frappe.whitelist(allow_guest=True)
def create_driver_user(driver=None, email=None):
	"""Create a User for the driver's Employee, assign group ``driver``, return one-time password."""
	name = _dirty_str(driver)
	if not name:
		frappe.throw(_("Driver is required."))
	if not frappe.db.exists("Driver", name):
		frappe.throw(_("Driver {0} not found").format(name), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	d = frappe.get_doc("Driver", name)
	frappe.flags.ignore_permissions = False
	if not d.employee:
		frappe.throw(_("Driver has no linked Employee — cannot create a login."))

	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_set_employee_groups,
		_set_user_roles,
		_roles_from_permission_ids,
		_permission_ids_for_employee,
		_unique_staff_email,
	)
	from frappe.utils.password import update_password as _update_password

	emp = frappe.get_doc("Employee", d.employee)
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		frappe.throw(_("Driver already has user {0}").format(emp.user_id))

	group_name = _ensure_driver_staff_group()
	requested = _dirty_str(email) or _dirty_str(emp.company_email) or _dirty_str(emp.prefered_email)
	login_email = _unique_staff_email(requested, emp.employee_name or d.full_name or d.name)

	password = _rand_driver_password()
	parts = (d.full_name or emp.employee_name or "Driver").split()
	first = parts[0]
	last = " ".join(parts[1:]) if len(parts) > 1 else first

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": login_email,
			"first_name": first,
			"last_name": last,
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	user.flags.ignore_password_policy = True
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)
	_update_password(user=user.name, pwd=password, logout_all_sessions=False)

	if group_name:
		current_groups = frappe.get_all(
			"Employee Group Table",
			filters={"employee": emp.name},
			pluck="parent",
			ignore_permissions=True,
		)
		_set_employee_groups(emp.name, list({*current_groups, group_name}))
	group_roles = _roles_from_permission_ids(_permission_ids_for_employee(emp.name))
	if not group_roles or group_roles == ["Employee"]:
		group_roles = ["Employee", "Fleet Manager"] if frappe.db.exists("Role", "Fleet Manager") else ["Employee"]
	_set_user_roles(user.name, group_roles)

	emp.user_id = user.name
	emp.company_email = emp.company_email or login_email
	emp.prefered_email = login_email
	emp.create_user_permission = 0
	emp.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"ok": True,
		"driver": _serialize_driver(name),
		"user_id": user.name,
		"email": login_email,
		"password": password,
		"group": group_name,
	}


@frappe.whitelist(allow_guest=True)
def reset_driver_user_password(driver=None):
	"""Generate a new one-time password for the driver's linked User (reveal once)."""
	name = _dirty_str(driver)
	if not name:
		frappe.throw(_("Driver is required."))
	if not frappe.db.exists("Driver", name):
		frappe.throw(_("Driver {0} not found").format(name), frappe.DoesNotExistError)

	emp = frappe.db.get_value("Driver", name, "employee")
	user_id = frappe.db.get_value("Employee", emp, "user_id") if emp else None
	if not user_id or not frappe.db.exists("User", user_id):
		frappe.throw(_("Driver has no user account yet."))

	from frappe.utils.password import update_password as _update_password

	password = _rand_driver_password()
	user = frappe.get_doc("User", user_id)
	user.flags.ignore_password_policy = True
	user.flags.no_welcome_mail = True
	user.enabled = 1
	user.save(ignore_permissions=True)
	_update_password(user=user.name, pwd=password, logout_all_sessions=False)
	frappe.db.commit()
	return {
		"ok": True,
		"user_id": user.name,
		"email": user.email or user.name,
		"password": password,
		"driver": _serialize_driver(name),
	}


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
	"allow_driver_reorder": True,
	"allow_driver_delivery_request": True,
	"require_pin_for_order_actions": True,
	"print_template_delivery": None,
	"print_template_payment": None,
	"tracking_code_length": 8,
	# i039 auto groups — días de venta + whether visit tags include Man/Med/Tar/Noc
	"auto_group_working_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
	"auto_group_use_time_slots": False,
	"auto_group_excluded_slots": [],
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
	require_pin_for_order_actions=None,
	print_template_delivery=None,
	print_template_payment=None,
	tracking_code_length=None,
	auto_group_working_days=None,
	auto_group_use_time_slots=None,
	auto_group_excluded_slots=None,
):
	return _persist_tms_settings(
		{
			"delivery_payment_mode": delivery_payment_mode,
			"require_signature": require_signature,
			"require_photo_on_not_home": require_photo_on_not_home,
			"allow_driver_reorder": allow_driver_reorder,
			"allow_driver_delivery_request": allow_driver_delivery_request,
			"require_pin_for_order_actions": require_pin_for_order_actions,
			"print_template_delivery": print_template_delivery,
			"print_template_payment": print_template_payment,
			"tracking_code_length": tracking_code_length,
			"auto_group_working_days": auto_group_working_days,
			"auto_group_use_time_slots": auto_group_use_time_slots,
			"auto_group_excluded_slots": auto_group_excluded_slots,
		},
		commit=True,
	)


def _persist_tms_settings(raw, commit=True):
	"""Merge non-None keys into TMS settings blob. ``commit=False`` for batched writers."""
	current = _load_tms_settings()
	raw = raw or {}
	bool_keys = {
		"require_photo_on_not_home",
		"allow_driver_reorder",
		"allow_driver_delivery_request",
		"require_pin_for_order_actions",
		"auto_group_use_time_slots",
	}
	list_keys = {"auto_group_working_days", "auto_group_excluded_slots"}
	for key, value in raw.items():
		if value is None:
			continue
		if key in bool_keys:
			current[key] = frappe.parse_json(value) if isinstance(value, str) else bool(value)
		elif key == "tracking_code_length":
			current[key] = cint(value) or TMS_SETTINGS_DEFAULTS["tracking_code_length"]
		elif key in list_keys:
			parsed = frappe.parse_json(value) if isinstance(value, str) else value
			if isinstance(parsed, str):
				parsed = [p.strip() for p in parsed.split(",") if p.strip()]
			if not isinstance(parsed, list):
				parsed = []
			current[key] = [cstr(p).strip() for p in parsed if cstr(p).strip()]
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
	if commit:
		frappe.db.commit()

	return current


# ---------------------------------------------------------------------------
# Delivery Zones (dispatcher planning regions — stored as settings blob)
# ---------------------------------------------------------------------------

TMS_ZONES_SCOPE = "settings.tms_zones"

_ZONE_DEFAULT_COLORS = [
	"#6366f1",
	"#16a34a",
	"#ea580c",
	"#db2777",
	"#0891b2",
	"#ca8a04",
]


def _load_tms_zones():
	zones = []
	if frappe.db.exists("Table Extra Schema", TMS_ZONES_SCOPE):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Table Extra Schema", TMS_ZONES_SCOPE)
		frappe.flags.ignore_permissions = False
		stored = frappe.parse_json(doc.columns_json) if doc.columns_json else {}
		if isinstance(stored, dict) and isinstance(stored.get("zones"), list):
			zones = stored["zones"]
		elif isinstance(stored, list):
			zones = stored
	return {"zones": zones}


def _save_tms_zones(zones, commit=True):
	payload = frappe.as_json({"zones": zones})
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", TMS_ZONES_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", TMS_ZONES_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": TMS_ZONES_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	if commit:
		frappe.db.commit()
	return {"zones": zones}


def _upsert_zone_into_list(zones, zone_raw):
	"""Normalize and upsert ``zone_raw`` into ``zones`` list (no DB write)."""
	incoming = _normalize_zone(zone_raw)
	found = False
	for i, z in enumerate(zones):
		if str(z.get("code") or "").upper() == incoming["code"]:
			zones[i] = _normalize_zone(incoming, z)
			found = True
			break
	if not found:
		raw_color = None
		if isinstance(zone_raw, dict):
			raw_color = zone_raw.get("color")
		if not raw_color:
			incoming["color"] = _ZONE_DEFAULT_COLORS[len(zones) % len(_ZONE_DEFAULT_COLORS)]
		zones.append(incoming)
	return incoming



def _normalize_zone(raw, existing=None):
	existing = existing or {}
	if isinstance(raw, str):
		raw = frappe.parse_json(raw) or {}
	if not isinstance(raw, dict):
		frappe.throw(_("Invalid zone payload"))
	code = str(raw.get("code") or existing.get("code") or "").strip().upper()
	name = str(raw.get("name") or existing.get("name") or "").strip()
	if not code:
		frappe.throw(_("Zone code is required."))
	if not name:
		name = code
	visit_days = raw.get("visit_days")
	if isinstance(visit_days, str):
		visit_days = [d.strip() for d in visit_days.split(",") if d.strip()]
	if visit_days is None:
		visit_days = existing.get("visit_days") or []
	vehicles = raw.get("vehicles")
	if isinstance(vehicles, str):
		vehicles = [v.strip() for v in vehicles.split(",") if v.strip()]
	if vehicles is None:
		vehicles = existing.get("vehicles") or []
	return {
		"code": code,
		"name": name,
		"color": str(raw.get("color") or existing.get("color") or _ZONE_DEFAULT_COLORS[0]),
		"type": str(raw.get("type") or existing.get("type") or "delivery"),
		"visit_days": list(visit_days or []),
		"vehicles": list(vehicles or []),
		"notes": str(raw.get("notes") or existing.get("notes") or ""),
	}


@frappe.whitelist(allow_guest=True)
def list_tms_zones():
	return _load_tms_zones()


@frappe.whitelist(allow_guest=True)
def save_tms_zone(zone=None):
	"""Create or update a zone by code."""
	data = _load_tms_zones()
	zones = list(data.get("zones") or [])
	_upsert_zone_into_list(zones, zone)
	return _save_tms_zones(zones)


@frappe.whitelist(allow_guest=True)
def delete_tms_zone(code=None):
	code = str(code or "").strip().upper()
	if not code:
		frappe.throw(_("Zone code is required."))
	data = _load_tms_zones()
	zones = [z for z in (data.get("zones") or []) if str(z.get("code") or "").upper() != code]
	return _save_tms_zones(zones)


_ZONE_BA_CENTERS = {
	"NORTE": (-34.555, -58.455),
	"SUR": (-34.635, -58.415),
	"CENTRO": (-34.6037, -58.405),
	"OESTE": (-34.625, -58.495),
	"ESTE": (-34.605, -58.365),
}


def _zone_center_lookup(zones):
	"""Map zone code → (lat, lng) from zone fields or BA sector defaults."""
	centers = {}
	for z in zones or []:
		code = str(z.get("code") or "").strip().upper()
		if not code:
			continue
		lat = flt(z.get("lat"))
		lng = flt(z.get("lng"))
		if lat and lng:
			centers[code] = (lat, lng)
			continue
		name_u = str(z.get("name") or "").upper()
		matched = False
		for key, pt in _ZONE_BA_CENTERS.items():
			if key in code or key in name_u:
				centers[code] = pt
				matched = True
				break
		if not matched:
			# Spread unknown zones around BA by index hash
			idx = abs(hash(code)) % len(_ZONE_BA_CENTERS)
			centers[code] = list(_ZONE_BA_CENTERS.values())[idx]
	return centers


def _haversine_km(lat1, lng1, lat2, lng2):
	from math import asin, cos, radians, sin, sqrt

	r = 6371.0
	dlat = radians(lat2 - lat1)
	dlng = radians(lng2 - lng1)
	a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
	return 2 * r * asin(sqrt(a))


def _nearest_zone_code(lat, lng, centers):
	if not centers or lat is None or lng is None:
		return None
	best = None
	best_d = None
	for code, (clat, clng) in centers.items():
		d = _haversine_km(flt(lat), flt(lng), clat, clng)
		if best_d is None or d < best_d:
			best_d = d
			best = code
	return best


@frappe.whitelist(allow_guest=True)
def auto_assign_tms_zones(only_missing=1):
	"""Assign Address.custom_zone from nearest delivery zone (by lat/lng).

	only_missing=1 (default): skip addresses that already have a zone.
	"""
	only_missing = cint(only_missing) != 0
	zones = _load_tms_zones().get("zones") or []
	if not zones:
		return {"assigned": 0, "skipped": 0, "zones": 0}
	centers = _zone_center_lookup(zones)
	# Prefer storing the zone *name* when it looks like a label (demo), else code.
	code_to_label = {}
	for z in zones:
		code = str(z.get("code") or "").strip().upper()
		name = str(z.get("name") or code).strip()
		# Store code for stable matching with Agrupador filters; keep readable name as fallback label.
		code_to_label[code] = code if code else name

	# Addresses linked to pending / draft-ish delivery notes with coords.
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT addr.name AS address_name,
			addr.custom_latitude AS lat,
			addr.custom_longitude AS lng,
			addr.custom_zone AS zone
		FROM `tabAddress` addr
		INNER JOIN `tabDynamic Link` dl
			ON dl.parent = addr.name AND dl.parenttype = 'Address'
			AND dl.link_doctype = 'Customer'
		INNER JOIN `tabDelivery Note` dn
			ON dn.customer = dl.link_name
			AND dn.docstatus < 2
		WHERE IFNULL(addr.custom_latitude, 0) != 0
			AND IFNULL(addr.custom_longitude, 0) != 0
			AND IFNULL(addr.disabled, 0) = 0
		""",
		as_dict=True,
	)
	assigned = 0
	skipped = 0
	for row in rows:
		existing = cstr(row.get("zone") or "").strip()
		if only_missing and existing:
			skipped += 1
			continue
		code = _nearest_zone_code(row.get("lat"), row.get("lng"), centers)
		if not code:
			skipped += 1
			continue
		label = code_to_label.get(code) or code
		if existing.upper() == label.upper():
			skipped += 1
			continue
		frappe.db.set_value("Address", row.address_name, "custom_zone", label, update_modified=False)
		assigned += 1
	if assigned:
		frappe.db.commit()
	return {"assigned": assigned, "skipped": skipped, "zones": len(zones)}


# ---------------------------------------------------------------------------
# i039 — Auto delivery groups (geo cluster preview → commit zones + due dates)
# ---------------------------------------------------------------------------

_WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAY_SHORT = {
	"Mon": "Lu",
	"Tue": "Mar",
	"Wed": "Mi",
	"Thu": "Ju",
	"Fri": "Vi",
	"Sat": "Sa",
	"Sun": "Do",
}
_TIME_SLOT_SHORT = [
	(1, "Man"),
	(2, "Med"),
	(3, "Tar"),
	(4, "Noc"),
]


def _ensure_delivery_frequency_field():
	"""Create Address.custom_delivery_frequency if missing (patch may not have run)."""
	if frappe.db.has_column("Address", "custom_delivery_frequency"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Address": [
				{
					"fieldname": "custom_delivery_frequency",
					"fieldtype": "Int",
					"label": "Delivery Frequency (per week)",
					"default": "1",
					"insert_after": "custom_zone",
				},
			]
		},
		update=True,
	)
	frappe.clear_cache(doctype="Address")


def _parse_json_list(value, default=None):
	if value is None:
		return list(default or [])
	if isinstance(value, str):
		s = value.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			return list(default or [])
		parsed = frappe.parse_json(s)
		if isinstance(parsed, list):
			return [cstr(x).strip() for x in parsed if cstr(x).strip()]
		if isinstance(parsed, str):
			return [p.strip() for p in parsed.split(",") if p.strip()]
		return list(default or [])
	if isinstance(value, (list, tuple)):
		return [cstr(x).strip() for x in value if cstr(x).strip()]
	return list(default or [])


def _normalize_working_days(raw, default=None):
	default = default or list(TMS_SETTINGS_DEFAULTS["auto_group_working_days"])
	days = _parse_json_list(raw, default)
	canon = []
	for d in days:
		key = d[:3].title() if len(d) >= 3 else d.title()
		# Accept full names / ES abbreviations loosely
		aliases = {
			"Lun": "Mon",
			"Mar": "Tue",
			"Mie": "Wed",
			"Mié": "Wed",
			"Jue": "Thu",
			"Vie": "Fri",
			"Sab": "Sat",
			"Sáb": "Sat",
			"Dom": "Sun",
			"Lu": "Mon",
			"Ma": "Tue",
			"Mi": "Wed",
			"Ju": "Thu",
			"Vi": "Fri",
			"Sa": "Sat",
			"Do": "Sun",
		}
		key = aliases.get(d[:3].title() if len(d) >= 3 else d, aliases.get(d, key))
		if key in _WEEKDAY_ORDER and key not in canon:
			canon.append(key)
	return canon or list(default)


def _clamp_frequency(value):
	f = cint(value)
	if f < 1:
		f = 1
	if f > 7:
		f = 7
	return f


def _preferred_slot_for_customer(customer):
	"""Return (priority, short) from Customer.custom_preferred_hours, default Mañana."""
	if not customer:
		return _TIME_SLOT_SHORT[0]
	if not frappe.db.has_column("Customer", "custom_preferred_hours"):
		return _TIME_SLOT_SHORT[0]
	pref = frappe.db.get_value("Customer", customer, "custom_preferred_hours")
	if not pref or not frappe.db.exists("Preferred Delivery Hours", pref):
		return _TIME_SLOT_SHORT[0]
	row = frappe.db.get_value(
		"Preferred Delivery Hours",
		pref,
		["priority", "label"],
		as_dict=True,
	)
	if not row:
		return _TIME_SLOT_SHORT[0]
	prio = cint(row.get("priority")) or 1
	label = cstr(row.get("label") or "").lower()
	short_map = {"mañana": "Man", "manana": "Man", "mediodía": "Med", "mediodia": "Med", "tarde": "Tar", "noche": "Noc"}
	short = short_map.get(label)
	if not short:
		for p, s in _TIME_SLOT_SHORT:
			if p == prio:
				short = s
				break
	short = short or "Man"
	return (prio if 1 <= prio <= 4 else 1, short)


def _day_time_tag(day, priority, short):
	return f"{day}({priority} {short})"


def _spaced_weekdays(working_days, count, offset=0):
	"""Pick ``count`` days from working_days, spaced evenly, rotated by offset."""
	if not working_days:
		working_days = list(TMS_SETTINGS_DEFAULTS["auto_group_working_days"])
	n = len(working_days)
	count = max(1, min(cint(count) or 1, n))
	if count >= n:
		return list(working_days)
	step = n / float(count)
	picked = []
	for i in range(count):
		idx = int(round(offset + i * step)) % n
		day = working_days[idx]
		if day not in picked:
			picked.append(day)
	# Fill if rounding collided
	j = 0
	while len(picked) < count and j < n * 2:
		day = working_days[(offset + j) % n]
		if day not in picked:
			picked.append(day)
		j += 1
	# Keep calendar order
	order = {d: i for i, d in enumerate(_WEEKDAY_ORDER)}
	picked.sort(key=lambda d: order.get(d, 99))
	return picked


def _visit_tags_for_days(days, use_time_slots, slot, excluded_slots=None):
	excluded = set(_parse_json_list(excluded_slots, []))
	out = []
	prio, short = slot
	for day in days:
		tag = _day_time_tag(day, prio, short) if use_time_slots else day
		if tag in excluded:
			continue
		# Also skip if day-only excluded when using slots
		if day in excluded:
			continue
		out.append(tag)
	return out or ([days[0]] if days else [])


def _next_due_on_weekdays(as_of, weekdays):
	"""Next date >= as_of whose weekday is in ``weekdays`` (Mon..Sun)."""
	from frappe.utils import add_days

	as_of = getdate(as_of)
	if not weekdays:
		return as_of
	wanted = set(weekdays)
	for i in range(0, 14):
		d = add_days(as_of, i)
		# Python: Mon=0 … Sun=6 → map to our labels
		label = _WEEKDAY_ORDER[d.weekday()]
		if label in wanted:
			return d
	return as_of


def _kmeans_cluster_indices(coords, k, max_iter=40):
	"""Pure-Python k-means. ``coords`` = [(lat, lng), ...]. Returns list of cluster ids."""
	n = len(coords)
	if n == 0:
		return []
	k = max(1, min(cint(k) or 1, n))
	if k == 1:
		return [0] * n

	# Farthest-point seeding for stable-ish clusters
	centroids = [coords[0]]
	used = {0}
	while len(centroids) < k:
		best_i = None
		best_d = -1
		for i, p in enumerate(coords):
			if i in used:
				continue
			mind = min(_haversine_km(p[0], p[1], c[0], c[1]) for c in centroids)
			if mind > best_d:
				best_d = mind
				best_i = i
		if best_i is None:
			break
		used.add(best_i)
		centroids.append(coords[best_i])

	labels = [0] * n
	for _ in range(max_iter):
		changed = False
		# assign
		for i, p in enumerate(coords):
			best = 0
			best_d = None
			for ci, c in enumerate(centroids):
				d = (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2
				if best_d is None or d < best_d:
					best_d = d
					best = ci
			if labels[i] != best:
				labels[i] = best
				changed = True
		# update
		sums = [[0.0, 0.0, 0] for _ in centroids]
		for i, p in enumerate(coords):
			ci = labels[i]
			sums[ci][0] += p[0]
			sums[ci][1] += p[1]
			sums[ci][2] += 1
		for ci, (sx, sy, cnt) in enumerate(sums):
			if cnt:
				centroids[ci] = (sx / cnt, sy / cnt)
		if not changed:
			break
	return labels


def _address_frequency_map(address_names):
	_ensure_delivery_frequency_field()
	out = {}
	if not address_names:
		return out
	has_col = frappe.db.has_column("Address", "custom_delivery_frequency")
	fields = ["name"]
	if has_col:
		fields.append("custom_delivery_frequency")
	for row in frappe.get_all(
		"Address",
		filters={"name": ["in", list(address_names)]},
		fields=fields,
		ignore_permissions=True,
	):
		freq = _clamp_frequency(row.get("custom_delivery_frequency") if has_col else 1)
		out[row.name] = freq
	return out


def _build_auto_group_preview(
	date=None,
	driver_names=None,
	vehicle_names=None,
	k=None,
	working_days=None,
	use_time_slots=None,
	excluded_slots=None,
	company=None,
):
	settings = _load_tms_settings()
	as_of_raw = date
	if isinstance(as_of_raw, str):
		as_of_raw = as_of_raw.strip()
		if as_of_raw.lower() in ("", "null", "undefined", "none"):
			as_of_raw = None
	try:
		as_of = getdate(as_of_raw) if as_of_raw else getdate()
	except Exception:
		as_of = getdate()

	work_days = _normalize_working_days(
		working_days if working_days is not None else settings.get("auto_group_working_days")
	)
	if use_time_slots is None:
		use_slots = bool(settings.get("auto_group_use_time_slots"))
	elif isinstance(use_time_slots, str):
		use_slots = use_time_slots.strip().lower() in ("1", "true", "yes")
	else:
		use_slots = bool(use_time_slots)
	excluded = _parse_json_list(
		excluded_slots if excluded_slots is not None else settings.get("auto_group_excluded_slots"),
		[],
	)

	drivers = _parse_json_list(driver_names, [])
	vehicles = _parse_json_list(vehicle_names, [])
	warnings = []

	pending = get_pending_deliveries(date=str(as_of), company=company)
	deliveries = list(pending.get("deliveries") or [])

	geocoded = []
	ungeocoded = []
	for d in deliveries:
		try:
			lat = flt(d.get("lat"))
			lng = flt(d.get("lng"))
		except Exception:
			lat = lng = 0
		if d.get("geocoded") and lat and lng:
			geocoded.append(d)
		else:
			ungeocoded.append(
				{
					"delivery_note": d.get("delivery_note"),
					"customer_name": d.get("customer_name"),
					"address": d.get("address"),
					"due_date": d.get("due_date"),
					"overdue": bool(d.get("overdue")),
				}
			)

	if not drivers:
		warnings.append("no_drivers")
	if ungeocoded:
		warnings.append("ungeocoded_skipped")
	if not geocoded:
		return {
			"as_of": str(as_of),
			"strategy": "geo_clusters",
			"k": 0,
			"working_days": work_days,
			"use_time_slots": use_slots,
			"excluded_slots": excluded,
			"groups": [],
			"ungeocoded": ungeocoded,
			"warnings": warnings + (["no_geocoded"] if not geocoded else []),
			"total_pending": len(deliveries),
			"total_geocoded": 0,
		}

	k_eff = cint(k) if k not in (None, "", "null", "undefined") else (len(drivers) or 1)
	k_eff = max(1, min(k_eff, len(geocoded)))

	addr_names = {d.get("address_name") for d in geocoded if d.get("address_name")}
	freq_map = _address_frequency_map(addr_names)

	coords = [(flt(d["lat"]), flt(d["lng"])) for d in geocoded]
	labels = _kmeans_cluster_indices(coords, k_eff)

	# Bucket orders by cluster
	buckets = [[] for _ in range(k_eff)]
	for d, lab in zip(geocoded, labels):
		buckets[lab].append(d)

	# Drop empty clusters and reindex
	nonempty = [(i, b) for i, b in enumerate(buckets) if b]
	groups = []
	for gi, (_old, orders) in enumerate(nonempty):
		code = f"AG{gi + 1}"
		color = _ZONE_DEFAULT_COLORS[gi % len(_ZONE_DEFAULT_COLORS)]
		driver = drivers[gi % len(drivers)] if drivers else None
		vehicle = vehicles[gi % len(vehicles)] if vehicles else None

		freqs = [
			freq_map.get(o.get("address_name"), 1) for o in orders if o.get("address_name")
		] or [1]
		zone_freq = max(freqs)
		zone_days = _spaced_weekdays(work_days, zone_freq, offset=gi)

		# Prefer slot from first customer with a preference (stable by order)
		slot = _TIME_SLOT_SHORT[0]
		for o in orders:
			if o.get("customer"):
				slot = _preferred_slot_for_customer(o.get("customer"))
				break
		visit_days = _visit_tags_for_days(zone_days, use_slots, slot, excluded)

		day_label = "/".join(_WEEKDAY_SHORT.get(d, d) for d in zone_days)
		name = f"Auto Grupo {gi + 1} · {day_label}" if day_label else f"Auto Grupo {gi + 1}"

		driver_label = None
		if driver and frappe.db.exists("Driver", driver):
			driver_label = frappe.db.get_value("Driver", driver, "full_name") or driver

		order_rows = []
		clients_seen = []
		for o in orders:
			addr = o.get("address_name")
			f = freq_map.get(addr, 1) if addr else 1
			client_days = _spaced_weekdays(zone_days, f, offset=0)
			due = _next_due_on_weekdays(as_of, client_days)
			cname = o.get("customer_name") or o.get("customer") or ""
			if cname and cname not in clients_seen:
				clients_seen.append(cname)
			order_rows.append(
				{
					"delivery_note": o.get("delivery_note"),
					"customer": o.get("customer"),
					"customer_name": o.get("customer_name"),
					"address_name": addr,
					"lat": flt(o.get("lat")),
					"lng": flt(o.get("lng")),
					"frequency": f,
					"visit_days": client_days,
					"proposed_due_date": str(due),
					"overdue": bool(o.get("overdue")),
					"current_due_date": o.get("due_date"),
				}
			)

		groups.append(
			{
				"code": code,
				"name": name,
				"color": color,
				"type": "delivery",
				"driver_name": driver,
				"driver_label": driver_label,
				"vehicles": [vehicle] if vehicle else [],
				"visit_days": visit_days,
				"order_count": len(order_rows),
				"sample_clients": clients_seen[:5],
				"orders": order_rows,
			}
		)

	return {
		"as_of": str(as_of),
		"strategy": "geo_clusters",
		"k": len(groups),
		"working_days": work_days,
		"use_time_slots": use_slots,
		"excluded_slots": excluded,
		"groups": groups,
		"ungeocoded": ungeocoded,
		"warnings": warnings,
		"total_pending": len(deliveries),
		"total_geocoded": len(geocoded),
	}


@frappe.whitelist(allow_guest=True)
def preview_auto_delivery_groups(
	date=None,
	strategy=None,
	driver_names=None,
	vehicle_names=None,
	k=None,
	working_days=None,
	use_time_slots=None,
	excluded_slots=None,
	company=None,
	persist_settings=None,
):
	"""Preview geo clusters → proposed zones + due dates.

	No DB writes by default. Pass ``persist_settings=1`` only when intentionally
	saving calendar prefs without committing zones.
	"""
	_ = strategy  # only geo_clusters in v1
	preview = _build_auto_group_preview(
		date=date,
		driver_names=driver_names,
		vehicle_names=vehicle_names,
		k=k,
		working_days=working_days,
		use_time_slots=use_time_slots,
		excluded_slots=excluded_slots,
		company=company,
	)
	persist = persist_settings
	if isinstance(persist, str):
		persist = persist.strip().lower() in ("1", "true", "yes")
	elif persist is None:
		persist = False
	else:
		persist = bool(persist)
	if persist:
		_persist_tms_settings(
			{
				"auto_group_working_days": preview.get("working_days"),
				"auto_group_use_time_slots": preview.get("use_time_slots"),
				"auto_group_excluded_slots": preview.get("excluded_slots"),
			},
			commit=True,
		)
	return preview


@frappe.whitelist(allow_guest=True)
def commit_auto_delivery_groups(preview=None, force_due_dates=1):
	"""Apply a preview atomically: upsert zones, set Address.custom_zone, force due dates."""
	if isinstance(preview, str):
		s = preview.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			preview = None
		else:
			preview = frappe.parse_json(s)
	if not isinstance(preview, dict):
		frappe.throw(_("Preview payload is required."))

	groups = preview.get("groups")
	if not isinstance(groups, list) or not groups:
		frappe.throw(_("Preview has no groups to commit."))

	# null / dirty → default force on (matches UI contract)
	if force_due_dates is None:
		force_due = True
	elif isinstance(force_due_dates, str) and force_due_dates.strip().lower() in (
		"",
		"null",
		"undefined",
		"none",
	):
		force_due = True
	else:
		force_due = cint(force_due_dates) != 0

	_ensure_delivery_frequency_field()

	zones_written = []
	addresses_updated = 0
	dues_updated = 0
	dues_skipped = 0
	errors = []

	# Stage settings + zones in-memory, then one commit at the end.
	if preview.get("working_days") is not None or preview.get("use_time_slots") is not None:
		_persist_tms_settings(
			{
				"auto_group_working_days": preview.get("working_days"),
				"auto_group_use_time_slots": preview.get("use_time_slots"),
				"auto_group_excluded_slots": preview.get("excluded_slots"),
			},
			commit=False,
		)

	zones = list((_load_tms_zones().get("zones") or []))

	for g in groups:
		if not isinstance(g, dict):
			continue
		zone_payload = {
			"code": g.get("code"),
			"name": g.get("name"),
			"color": g.get("color"),
			"type": g.get("type") or "delivery",
			"visit_days": g.get("visit_days") or [],
			"vehicles": g.get("vehicles") or [],
			"notes": g.get("notes") or "auto-groups",
		}
		try:
			incoming = _upsert_zone_into_list(zones, zone_payload)
			zones_written.append(incoming["code"])
		except Exception as exc:
			errors.append({"zone": zone_payload.get("code"), "error": str(exc)})
			continue

		zone_code = str(zone_payload.get("code") or "").strip().upper()
		for o in g.get("orders") or []:
			if not isinstance(o, dict):
				continue
			dn = cstr(o.get("delivery_note") or "").strip()
			addr = cstr(o.get("address_name") or "").strip()
			if not addr and dn and frappe.db.exists("Delivery Note", dn):
				addr = frappe.db.get_value("Delivery Note", dn, "shipping_address_name") or frappe.db.get_value(
					"Delivery Note", dn, "customer_address"
				)
			if addr and frappe.db.exists("Address", addr) and frappe.db.has_column("Address", "custom_zone"):
				frappe.db.set_value("Address", addr, "custom_zone", zone_code, update_modified=True)
				addresses_updated += 1

			if force_due and dn:
				due = cstr(o.get("proposed_due_date") or "").strip()
				if due:
					try:
						due_d = getdate(due)
						so_names = frappe.get_all(
							"Delivery Note Item",
							filters={"parent": dn, "against_sales_order": ["is", "set"]},
							pluck="against_sales_order",
							ignore_permissions=True,
						)
						so_names = list({s for s in so_names if s})
						if not so_names:
							dues_skipped += 1
						else:
							for so in so_names:
								frappe.db.set_value(
									"Sales Order", so, "delivery_date", due_d, update_modified=True
								)
							dues_updated += 1
					except Exception as exc:
						errors.append({"delivery_note": dn, "error": str(exc)})
						dues_skipped += 1

	_save_tms_zones(zones, commit=False)
	frappe.db.commit()
	return {
		"ok": not bool(errors),
		"zones_written": zones_written,
		"addresses_updated": addresses_updated,
		"dues_updated": dues_updated,
		"dues_skipped": dues_skipped,
		"errors": errors,
		"zones": zones,
	}


# ---------------------------------------------------------------------------
# Map pins / temp locations / named plans (Routes map tools)
# ---------------------------------------------------------------------------
TMS_MAP_PINS_SCOPE = "settings.tms_map_pins"


def _load_tms_map_store():
	store = {"pins": [], "plans": []}
	if frappe.db.exists("Table Extra Schema", TMS_MAP_PINS_SCOPE):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Table Extra Schema", TMS_MAP_PINS_SCOPE)
		frappe.flags.ignore_permissions = False
		stored = frappe.parse_json(doc.columns_json) if doc.columns_json else {}
		if isinstance(stored, dict):
			if isinstance(stored.get("pins"), list):
				store["pins"] = stored["pins"]
			if isinstance(stored.get("plans"), list):
				store["plans"] = stored["plans"]
	return store


def _save_tms_map_store(store):
	payload = frappe.as_json(
		{"pins": list(store.get("pins") or []), "plans": list(store.get("plans") or [])}
	)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", TMS_MAP_PINS_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", TMS_MAP_PINS_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": TMS_MAP_PINS_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return store


def _new_map_id(prefix="pin"):
	return f"{prefix}-{frappe.generate_hash(length=10)}"


@frappe.whitelist(allow_guest=True)
def list_map_pins_and_plans():
	"""Temp locations + named map plans for the Routes map."""
	return _load_tms_map_store()


@frappe.whitelist(allow_guest=True)
def save_map_temp_location(label=None, address=None, lat=None, lng=None, pin_id=None, notes=None):
	"""Create/update a temporary map pin (not a Customer / Warehouse)."""
	lat = flt(lat)
	lng = flt(lng)
	if not (lat or lng):
		frappe.throw(_("Latitude and longitude are required."))
	label = cstr(label or "").strip() or _("Temporary stop")
	address = cstr(address or "").strip() or label
	store = _load_tms_map_store()
	pins = list(store.get("pins") or [])
	pin_id = cstr(pin_id or "").strip() or _new_map_id("temp")
	row = {
		"id": pin_id,
		"kind": "temp",
		"label": label,
		"address": address,
		"lat": lat,
		"lng": lng,
		"notes": cstr(notes or "").strip(),
		"updated_at": str(now_datetime()),
	}
	found = False
	for i, p in enumerate(pins):
		if str(p.get("id")) == pin_id:
			pins[i] = {**p, **row}
			found = True
			break
	if not found:
		row["created_at"] = row["updated_at"]
		pins.append(row)
	store["pins"] = pins
	_save_tms_map_store(store)
	return {"pin": row, "pins": pins}


@frappe.whitelist(allow_guest=True)
def delete_map_pin(pin_id=None):
	pin_id = cstr(pin_id or "").strip()
	if not pin_id:
		frappe.throw(_("pin_id is required"))
	store = _load_tms_map_store()
	pins = [p for p in (store.get("pins") or []) if str(p.get("id")) != pin_id]
	plans = []
	for plan in store.get("plans") or []:
		ids = [i for i in (plan.get("pin_ids") or []) if i != pin_id]
		plans.append({**plan, "pin_ids": ids})
	store["pins"] = pins
	store["plans"] = plans
	_save_tms_map_store(store)
	return store


@frappe.whitelist(allow_guest=True)
def save_map_plan(name=None, pin_ids=None, plan_id=None, date=None, notes=None):
	"""Named map plan — a saved set of pin ids for the dispatcher."""
	name = cstr(name or "").strip()
	if not name:
		frappe.throw(_("Plan name is required."))
	if isinstance(pin_ids, str):
		pin_ids = frappe.parse_json(pin_ids) or []
	pin_ids = [cstr(x).strip() for x in (pin_ids or []) if cstr(x).strip()]
	store = _load_tms_map_store()
	plans = list(store.get("plans") or [])
	plan_id = cstr(plan_id or "").strip() or _new_map_id("plan")
	row = {
		"id": plan_id,
		"name": name,
		"pin_ids": pin_ids,
		"date": cstr(date or "").strip() or str(getdate()),
		"notes": cstr(notes or "").strip(),
		"updated_at": str(now_datetime()),
	}
	found = False
	for i, p in enumerate(plans):
		if str(p.get("id")) == plan_id:
			plans[i] = {**p, **row}
			found = True
			break
	if not found:
		row["created_at"] = row["updated_at"]
		plans.append(row)
	store["plans"] = plans
	_save_tms_map_store(store)
	return {"plan": row, "plans": plans, "pins": store.get("pins") or []}


@frappe.whitelist(allow_guest=True)
def delete_map_plan(plan_id=None):
	plan_id = cstr(plan_id or "").strip()
	if not plan_id:
		frappe.throw(_("plan_id is required"))
	store = _load_tms_map_store()
	store["plans"] = [p for p in (store.get("plans") or []) if str(p.get("id")) != plan_id]
	_save_tms_map_store(store)
	return store


def _ensure_geo_address(title, line1, lat, lng, link_doctype=None, link_name=None, address_type="Shipping"):
	"""Insert Address with coordinates; optionally link to Customer/Warehouse."""
	lat = flt(lat)
	lng = flt(lng)
	doc = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": cstr(title or line1 or "Map pin")[:140],
			"address_type": address_type,
			"address_line1": cstr(line1 or title or "").strip() or "Buenos Aires",
			"city": "Buenos Aires",
			"country": "Argentina",
			"is_shipping_address": 1 if address_type == "Shipping" else 0,
			"custom_latitude": lat,
			"custom_longitude": lng,
		}
	)
	if link_doctype and link_name:
		doc.append("links", {"link_doctype": link_doctype, "link_name": link_name})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


@frappe.whitelist(allow_guest=True)
def create_warehouse_at_location(
	warehouse_name=None,
	address=None,
	lat=None,
	lng=None,
	company=None,
	parent_warehouse=None,
):
	"""Create a non-group Warehouse + geocoded Address at the map pin."""
	warehouse_name = cstr(warehouse_name or "").strip()
	if not warehouse_name:
		frappe.throw(_("Warehouse name is required."))
	company = cstr(company or "").strip() or frappe.defaults.get_user_default("Company")
	if not company:
		frappe.throw(_("Company is required to create a warehouse."))
	lat = flt(lat)
	lng = flt(lng)

	parent = cstr(parent_warehouse or "").strip()
	if not parent:
		rows = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 1, "disabled": 0},
			pluck="name",
			order_by="lft asc",
			limit=1,
			ignore_permissions=True,
		)
		parent = rows[0] if rows else ""

	wh = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": warehouse_name,
			"company": company,
			"is_group": 0,
			"disabled": 0,
		}
	)
	if parent:
		wh.parent_warehouse = parent
	wh.insert(ignore_permissions=True)

	addr_line = cstr(address or "").strip() or warehouse_name
	addr = _ensure_geo_address(
		title=warehouse_name,
		line1=addr_line,
		lat=lat,
		lng=lng,
		link_doctype="Warehouse",
		link_name=wh.name,
		address_type="Warehouse",
	)
	if frappe.db.has_column("Warehouse", "address_line_1"):
		frappe.db.set_value("Warehouse", wh.name, "address_line_1", addr_line, update_modified=False)

	store = _load_tms_map_store()
	pin = {
		"id": _new_map_id("wh"),
		"kind": "warehouse",
		"label": warehouse_name,
		"address": addr_line,
		"lat": lat,
		"lng": lng,
		"ref_doctype": "Warehouse",
		"ref_name": wh.name,
		"address_name": addr.name,
		"created_at": str(now_datetime()),
		"updated_at": str(now_datetime()),
	}
	store["pins"] = list(store.get("pins") or []) + [pin]
	_save_tms_map_store(store)
	return {"warehouse": wh.name, "address": addr.name, "pin": pin, "pins": store["pins"]}


@frappe.whitelist(allow_guest=True)
def create_customer_at_location(
	customer_name=None,
	address=None,
	lat=None,
	lng=None,
	phone=None,
	email=None,
):
	"""Create Customer + shipping Address at the map pin."""
	from erpnext.erpnext_integrations.ecommerce_api.api import create_customer

	customer_name = cstr(customer_name or "").strip()
	if not customer_name:
		frappe.throw(_("Customer name is required."))
	lat = flt(lat)
	lng = flt(lng)
	cust = create_customer(customer_name=customer_name, phone=phone, email=email)
	cust_name = cust.get("name") if isinstance(cust, dict) else cust
	addr_line = cstr(address or "").strip() or customer_name
	addr = _ensure_geo_address(
		title=f"{customer_name} — envío",
		line1=addr_line,
		lat=lat,
		lng=lng,
		link_doctype="Customer",
		link_name=cust_name,
		address_type="Shipping",
	)
	store = _load_tms_map_store()
	pin = {
		"id": _new_map_id("cust"),
		"kind": "customer",
		"label": customer_name,
		"address": addr_line,
		"lat": lat,
		"lng": lng,
		"ref_doctype": "Customer",
		"ref_name": cust_name,
		"address_name": addr.name,
		"created_at": str(now_datetime()),
		"updated_at": str(now_datetime()),
	}
	store["pins"] = list(store.get("pins") or []) + [pin]
	_save_tms_map_store(store)
	return {"customer": cust_name, "address": addr.name, "pin": pin, "pins": store["pins"]}


@frappe.whitelist(allow_guest=True)
def link_customer_at_location(customer=None, address=None, lat=None, lng=None, label=None):
	"""Attach a new geocoded shipping Address to an existing Customer."""
	customer = cstr(customer or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer not found."))
	lat = flt(lat)
	lng = flt(lng)
	cust_label = frappe.db.get_value("Customer", customer, "customer_name") or customer
	addr_line = cstr(address or label or "").strip() or cust_label
	addr = _ensure_geo_address(
		title=cstr(label or "").strip() or f"{cust_label} — mapa",
		line1=addr_line,
		lat=lat,
		lng=lng,
		link_doctype="Customer",
		link_name=customer,
		address_type="Shipping",
	)
	store = _load_tms_map_store()
	pin = {
		"id": _new_map_id("cust"),
		"kind": "customer",
		"label": cust_label,
		"address": addr_line,
		"lat": lat,
		"lng": lng,
		"ref_doctype": "Customer",
		"ref_name": customer,
		"address_name": addr.name,
		"created_at": str(now_datetime()),
		"updated_at": str(now_datetime()),
	}
	store["pins"] = list(store.get("pins") or []) + [pin]
	_save_tms_map_store(store)
	return {"customer": customer, "address": addr.name, "pin": pin, "pins": store["pins"]}


# ---------------------------------------------------------------------------
# Rutas orders CSV (Pedido + Remito import)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_rutas_orders_csv_template():
	from erpnext.erpnext_integrations.ecommerce_api.tms_orders_csv import (
		get_rutas_orders_csv_template as _impl,
	)

	return _impl()


@frappe.whitelist(allow_guest=True)
def import_rutas_orders_csv(csv_text=None, pin=None, company=None):
	from erpnext.erpnext_integrations.ecommerce_api.tms_orders_csv import (
		import_rutas_orders_csv as _impl,
	)

	return _impl(csv_text=csv_text, pin=pin, company=company)


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


# Buenos Aires logistics demo seed — real street addresses + lat/lng so map pins work.
# Customer/driver names are ordinary CABA labels (no "TMS Demo …" placeholders).
_TMS_BA_DRIVERS_SEED = [
	{
		"full_name": "Carlos Ramírez",
		"cell": "011-4444-0001",
		"lat": -34.6100,
		"lng": -58.4200,
		"addr_title": "Av. Rivadavia 5000, Flores",
		"addr_line": "Av. Rivadavia 5000, Flores, CABA",
	},
	{
		"full_name": "María González",
		"cell": "011-4444-0002",
		"lat": -34.5900,
		"lng": -58.4500,
		"addr_title": "Av. Triunvirato 3200, Villa del Parque",
		"addr_line": "Av. Triunvirato 3200, Villa del Parque, CABA",
	},
	{
		"full_name": "Nelson Wang",
		"cell": "011-4444-0003",
		"lat": -34.6037,
		"lng": -58.3816,
		"addr_title": "Av. Corrientes 1200, Centro",
		"addr_line": "Av. Corrientes 1200, San Nicolás, CABA",
	},
	{
		"full_name": "Lucía Fernández",
		"cell": "011-4444-0004",
		"lat": -34.5750,
		"lng": -58.4350,
		"addr_title": "Av. Santa Fe 4500, Palermo",
		"addr_line": "Av. Santa Fe 4500, Palermo, CABA",
	},
	{
		"full_name": "Martín Pérez",
		"cell": "011-4444-0005",
		"lat": -34.6300,
		"lng": -58.4600,
		"addr_title": "Av. Directorio 1800, Parque Chacabuco",
		"addr_line": "Av. Directorio 1800, Parque Chacabuco, CABA",
	},
]

_TMS_BA_VEHICLES_SEED = [
	{"plate": "BA-001-AA", "make": "Ford", "model": "Transit", "fuel": "Diesel"},
	{"plate": "BA-002-BB", "make": "Volkswagen", "model": "Caddy", "fuel": "Diesel"},
	{"plate": "BA-003-CC", "make": "Renault", "model": "Kangoo", "fuel": "Petrol"},
	{"plate": "BA-004-DD", "make": "Mercedes", "model": "Sprinter", "fuel": "Diesel"},
	{"plate": "BA-005-EE", "make": "Fiat", "model": "Fiorino", "fuel": "Petrol"},
]

# name = Customer; addr_title = Address title (shown cleanly); addr = street line for map/geocode.
_TMS_BA_CUSTOMERS_SEED = [
	{"name": "Almacén Santa Fe", "area": "Palermo", "addr_title": "Av. Santa Fe 3000, Palermo", "addr": "Av. Santa Fe 3000, Palermo, CABA", "lat": -34.5883, "lng": -58.4314, "zone": "Norte"},
	{"name": "Bodega Defensa", "area": "San Telmo", "addr_title": "Defensa 500, San Telmo", "addr": "Defensa 500, San Telmo, CABA", "lat": -34.6217, "lng": -58.3731, "zone": "Sur"},
	{"name": "Distribuidora Cabildo", "area": "Belgrano", "addr_title": "Av. Cabildo 2000, Belgrano", "addr": "Av. Cabildo 2000, Belgrano, CABA", "lat": -34.5537, "lng": -58.4560, "zone": "Norte"},
	{"name": "Gourmet Alvear", "area": "Recoleta", "addr_title": "Av. Alvear 1800, Recoleta", "addr": "Av. Alvear 1800, Recoleta, CABA", "lat": -34.5875, "lng": -58.3951, "zone": "Centro"},
	{"name": "Mercado Rivadavia", "area": "Caballito", "addr_title": "Av. Rivadavia 4800, Caballito", "addr": "Av. Rivadavia 4800, Caballito, CABA", "lat": -34.6183, "lng": -58.4407, "zone": "Centro"},
	{"name": "Depósito Directorio", "area": "Flores", "addr_title": "Av. Directorio 2200, Flores", "addr": "Av. Directorio 2200, Flores, CABA", "lat": -34.6333, "lng": -58.4600, "zone": "Sur"},
	{"name": "Proveeduría Triunvirato", "area": "Villa Urquiza", "addr_title": "Av. Triunvirato 4500, Villa Urquiza", "addr": "Av. Triunvirato 4500, Villa Urquiza, CABA", "lat": -34.5722, "lng": -58.4880, "zone": "Norte"},
	{"name": "Comercial Boedo", "area": "Boedo", "addr_title": "Av. Boedo 1100, Boedo", "addr": "Av. Boedo 1100, Boedo, CABA", "lat": -34.6280, "lng": -58.4160, "zone": "Sur"},
	{"name": "Autoservicio Corrientes", "area": "Almagro", "addr_title": "Av. Corrientes 4200, Almagro", "addr": "Av. Corrientes 4200, Almagro, CABA", "lat": -34.6060, "lng": -58.4210, "zone": "Centro"},
	{"name": "Minimarket Núñez", "area": "Núñez", "addr_title": "Av. Cabildo 4200, Núñez", "addr": "Av. Cabildo 4200, Núñez, CABA", "lat": -34.5450, "lng": -58.4630, "zone": "Norte"},
	{"name": "Almacén Lacroze", "area": "Colegiales", "addr_title": "Av. Federico Lacroze 2400, Colegiales", "addr": "Av. Federico Lacroze 2400, Colegiales, CABA", "lat": -34.5738, "lng": -58.4492, "zone": "Norte"},
	{"name": "Kiosco Villa Crespo", "area": "Villa Crespo", "addr_title": "Av. Corrientes 5400, Villa Crespo", "addr": "Av. Corrientes 5400, Villa Crespo, CABA", "lat": -34.5985, "lng": -58.4378, "zone": "Centro"},
	{"name": "Distribuidora Montes de Oca", "area": "Barracas", "addr_title": "Av. Montes de Oca 900, Barracas", "addr": "Av. Montes de Oca 900, Barracas, CABA", "lat": -34.6405, "lng": -58.3745, "zone": "Sur"},
	{"name": "Despensa La Boca", "area": "La Boca", "addr_title": "Av. Almirante Brown 700, La Boca", "addr": "Av. Almirante Brown 700, La Boca, CABA", "lat": -34.6345, "lng": -58.3630, "zone": "Sur"},
	{"name": "Office Puerto Madero", "area": "Puerto Madero", "addr_title": "Juana Manso 500, Puerto Madero", "addr": "Juana Manso 500, Puerto Madero, CABA", "lat": -34.6118, "lng": -58.3632, "zone": "Centro"},
	{"name": "Hotel Libertador", "area": "Retiro", "addr_title": "Av. del Libertador 600, Retiro", "addr": "Av. del Libertador 600, Retiro, CABA", "lat": -34.5895, "lng": -58.3738, "zone": "Centro"},
	{"name": "Café Honduras", "area": "Palermo Hollywood", "addr_title": "Honduras 5600, Palermo Hollywood", "addr": "Honduras 5600, Palermo Hollywood, CABA", "lat": -34.5830, "lng": -58.4325, "zone": "Norte"},
	{"name": "Mayorista Devoto", "area": "Villa Devoto", "addr_title": "Av. San Martín 6500, Villa Devoto", "addr": "Av. San Martín 6500, Villa Devoto, CABA", "lat": -34.6030, "lng": -58.5120, "zone": "Oeste"},
	{"name": "Carnicería Mataderos", "area": "Mataderos", "addr_title": "Av. Eva Perón 5500, Mataderos", "addr": "Av. Eva Perón 5500, Mataderos, CABA", "lat": -34.6550, "lng": -58.5020, "zone": "Oeste"},
	{"name": "Supermercado Liniers", "area": "Liniers", "addr_title": "Av. Rivadavia 11400, Liniers", "addr": "Av. Rivadavia 11400, Liniers, CABA", "lat": -34.6395, "lng": -58.5235, "zone": "Oeste"},
	{"name": "Farmacia Constitución", "area": "Constitución", "addr_title": "Av. Brasil 800, Constitución", "addr": "Av. Brasil 800, Constitución, CABA", "lat": -34.6275, "lng": -58.3805, "zone": "Sur"},
	{"name": "Verdulería Independencia", "area": "San Cristóbal", "addr_title": "Av. Independencia 2800, San Cristóbal", "addr": "Av. Independencia 2800, San Cristóbal, CABA", "lat": -34.6228, "lng": -58.4005, "zone": "Sur"},
	{"name": "Librería Corrientes", "area": "Balvanera", "addr_title": "Av. Corrientes 2200, Balvanera", "addr": "Av. Corrientes 2200, Balvanera, CABA", "lat": -34.6040, "lng": -58.3965, "zone": "Centro"},
	{"name": "Textil Once", "area": "Once", "addr_title": "Av. Pueyrredón 500, Once", "addr": "Av. Pueyrredón 500, Balvanera, CABA", "lat": -34.6085, "lng": -58.4055, "zone": "Centro"},
	{"name": "Taller Elcano", "area": "Chacarita", "addr_title": "Av. Elcano 3200, Chacarita", "addr": "Av. Elcano 3200, Chacarita, CABA", "lat": -34.5865, "lng": -58.4545, "zone": "Norte"},
]


@frappe.whitelist(allow_guest=True)
def seed_tms_demo(reset=False):
	"""Create demo data for TMS dispatcher testing.

	Idempotent by default — skips anything that already exists.
	Pass reset=True to delete existing TMS demo docs first.

	Creates:
	  - 5 Drivers (each with Employee + geocoded home Address)
	  - 5 Vehicles
	  - 25 Customers with geocoded, zone-tagged shipping Addresses across BA
	  - Delivery Notes + guest preorders per customer
	  - 4 planning Zones (Norte / Centro / Sur / Oeste)
	  - Company.custom_default_warehouse set (depot demo)
	  - 1 published trip with POD / cliente-debe demos + driver login User
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
		# Delete existing demo DNs and trips that use them, then customers/drivers/vehicles.
		# Include legacy "TMS Demo Cliente%" names from older seeds.
		legacy_customers = frappe.get_all(
			"Customer",
			filters={"customer_name": ["like", "TMS Demo Cliente%"]},
			pluck="name",
			ignore_permissions=True,
		)
		demo_customer_ids = list(
			{
				*(frappe.db.get_value("Customer", {"customer_name": c["name"]}, "name") for c in _TMS_BA_CUSTOMERS_SEED),
				*legacy_customers,
			}
		)
		demo_customer_ids = [c for c in demo_customer_ids if c]
		for dn in frappe.get_all(
			"Delivery Note",
			filters={"customer": ["in", demo_customer_ids]} if demo_customer_ids else {"customer": ["like", "TMS Demo Cliente%"]},
			ignore_permissions=True,
		):
			if frappe.db.get_value("Delivery Note", dn.name, "docstatus") == 1:
				frappe.db.set_value("Delivery Note", dn.name, "docstatus", 2)
			frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)
		demo_driver_names = [d["full_name"] for d in _TMS_BA_DRIVERS_SEED] + [
			"TMS Demo Carlos Ramírez",
			"TMS Demo María González",
			"TMS Demo Nelson Wang",
			"TMS Demo Lucía Fernández",
			"TMS Demo Martín Pérez",
		]
		for dr in frappe.get_all(
			"Driver", filters={"full_name": ["in", demo_driver_names]}, ignore_permissions=True
		):
			frappe.delete_doc("Driver", dr.name, force=True, ignore_permissions=True)
		for v in frappe.get_all(
			"Vehicle",
			filters={"license_plate": ["in", [x["plate"] for x in _TMS_BA_VEHICLES_SEED] + [
				"TMS-001-AA", "TMS-002-BB", "TMS-003-CC", "TMS-004-DD", "TMS-005-EE",
			]]},
			ignore_permissions=True,
		):
			frappe.delete_doc("Vehicle", v.name, force=True, ignore_permissions=True)
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
	drivers_seed = _TMS_BA_DRIVERS_SEED
	for d in drivers_seed:
		exists = frappe.get_all("Driver", filters={"full_name": d["full_name"]}, ignore_permissions=True)
		if exists:
			# Keep coords fresh so map pins stay valid after seed tweaks.
			drv = exists[0].name
			addr_name = frappe.db.get_value("Driver", drv, "address")
			if addr_name and frappe.db.exists("Address", addr_name):
				frappe.db.set_value(
					"Address",
					addr_name,
					{
						"address_line1": d["addr_line"],
						"custom_latitude": d["lat"],
						"custom_longitude": d["lng"],
					},
					update_modified=False,
				)
			skipped.append(f"Driver: {d['full_name']}")
			continue

		home_addr = frappe.get_doc({
			"doctype": "Address",
			"address_title": d["addr_title"],
			"address_type": "Personal",
			"address_line1": d["addr_line"],
			"city": "Buenos Aires",
			"country": "Argentina",
			"custom_latitude": d["lat"],
			"custom_longitude": d["lng"],
		})
		home_addr.insert(ignore_permissions=True)

		parts = d["full_name"].split()
		first = parts[0]
		last = " ".join(parts[1:]) or parts[0]
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
	vehicles_seed = _TMS_BA_VEHICLES_SEED
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
	customers_seed = _TMS_BA_CUSTOMERS_SEED
	for c in customers_seed:
		if not frappe.db.exists("Customer", c["name"]):
			cust = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": c["name"],
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "Argentina",
			})
			# Fallbacks if Commercial / Company variants missing on the site.
			try:
				cust.insert(ignore_permissions=True)
			except Exception:
				cust = frappe.get_doc({
					"doctype": "Customer",
					"customer_name": c["name"],
					"customer_type": "Individual",
					"customer_group": "All Customer Groups",
					"territory": "All Territories",
				})
				cust.flags.ignore_mandatory = True
				cust.insert(ignore_permissions=True)
			created.append(f"Customer: {cust.name}")
		else:
			skipped.append(f"Customer: {c['name']}")

		addr_title = c["addr_title"]
		existing_addr = frappe.db.get_value(
			"Address", {"address_title": addr_title, "address_type": "Shipping"}, "name"
		)
		if existing_addr:
			frappe.db.set_value(
				"Address",
				existing_addr,
				{
					"address_line1": c["addr"],
					"city": "Buenos Aires",
					"country": "Argentina",
					"custom_latitude": c["lat"],
					"custom_longitude": c["lng"],
					"custom_zone": c["zone"],
				},
				update_modified=False,
			)
		else:
			addr = frappe.get_doc({
				"doctype": "Address",
				"address_title": addr_title,
				"address_type": "Shipping",
				"address_line1": c["addr"],
				"city": "Buenos Aires",
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
			{"address_title": c["addr_title"], "address_type": "Shipping"},
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
			"remarks": "BA demo — stock for guest-preorder-to-delivery-note flow",
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
			guest_notes="Pedido demo CABA — referencia logística",
			cashier_id="ba-demo",
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
				"first_name": "Carlos",
				"last_name": "Ramírez",
				"enabled": 1,
				"send_welcome_email": 0,
			})
			user.flags.ignore_password_policy = True
			user.insert(ignore_permissions=True)
			from frappe.utils.password import update_password

			update_password(user=login_email, pwd="TmsDemo123!", logout_all_sessions=False)
			created.append(f"User: {login_email} (password: TmsDemo123!)")
		frappe.db.set_value("Employee", first_driver.employee, "user_id", login_email)

	# ── 9. One published trip (first driver) with a delivered stop + a
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

	# ── 10. Delivery zones (Buenos Aires planning regions) ─────────────────────
	zones_seed = [
		{"code": "NORTE", "name": "Zona Norte", "color": "#6366f1", "type": "delivery", "visit_days": ["Mon", "Wed", "Fri"], "vehicles": ["BA-001-AA", "BA-003-CC"]},
		{"code": "CENTRO", "name": "Zona Centro", "color": "#16a34a", "type": "delivery", "visit_days": ["Tue", "Thu", "Sat"], "vehicles": ["BA-002-BB"]},
		{"code": "SUR", "name": "Zona Sur", "color": "#ea580c", "type": "delivery", "visit_days": ["Mon", "Thu"], "vehicles": ["BA-004-DD"]},
		{"code": "OESTE", "name": "Zona Oeste", "color": "#db2777", "type": "delivery", "visit_days": ["Wed", "Fri"], "vehicles": ["BA-005-EE"]},
	]
	existing_zones = {str(z.get("code") or "").upper() for z in (_load_tms_zones().get("zones") or [])}
	for z in zones_seed:
		if z["code"] in existing_zones:
			skipped.append(f"Zone: {z['code']}")
			continue
		save_tms_zone(z)
		created.append(f"Zone: {z['code']}")

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

	stops = _stops_payload(trip, geo_by_address)

	preferred_hours = []
	try:
		from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
			list_preferred_delivery_hours,
		)

		preferred_hours = (list_preferred_delivery_hours(include_disabled=0) or {}).get("hours") or []
	except Exception:
		preferred_hours = []

	return {
		"trip": {"name": trip.name, "status": trip.status, "departure_time": trip.departure_time},
		"stops": stops,
		"preferred_hours_options": preferred_hours,
	}


@frappe.whitelist()
def driver_update_stop_details(
	trip_name=None,
	stop_idx=None,
	customer_name=None,
	address_line=None,
	comments=None,
	preferred_hours=None,
	lat=None,
	lng=None,
):
	"""Update customer/address fields shown on Completar Entrega (name, location, notes, preferred time)."""
	trip_name = cstr(trip_name or "").strip()
	if not trip_name or trip_name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required."))
	try:
		idx = cint(stop_idx)
	except Exception:
		idx = 0
	if idx < 1:
		frappe.throw(_("Stop index is required."))

	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	stop = next((s for s in trip.delivery_stops if cint(s.idx) == idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip.").format(idx))

	customer = stop.customer
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer for this stop was not found."))

	# --- customer name ---
	if customer_name is not None:
		name = cstr(customer_name).strip()
		if name.lower() in ("null", "undefined", "none"):
			name = ""
		if name:
			frappe.db.set_value("Customer", customer, "customer_name", name, update_modified=True)
			# Keep stop label in sync for the trip card
			frappe.db.set_value("Delivery Stop", stop.name, "customer", customer, update_modified=False)

	# --- comments (Customer.customer_details) ---
	if comments is not None:
		text = cstr(comments)
		if text.lower() in ("null", "undefined", "none"):
			text = ""
		frappe.db.set_value("Customer", customer, "customer_details", text, update_modified=True)

	# --- preferred hours ---
	if preferred_hours is not None and frappe.db.has_column("Customer", "custom_preferred_hours"):
		pref = cstr(preferred_hours).strip()
		if pref.lower() in ("", "null", "undefined", "none"):
			pref = None
		elif not frappe.db.exists("Preferred Delivery Hours", pref):
			frappe.throw(_("Preferred time {0} was not found.").format(pref))
		frappe.db.set_value("Customer", customer, "custom_preferred_hours", pref, update_modified=True)

	# --- address line + optional coords ---
	addr_name = stop.address
	if address_line is not None or lat is not None or lng is not None:
		line = None
		if address_line is not None:
			line = cstr(address_line).strip()
			if line.lower() in ("null", "undefined", "none"):
				line = ""
		if not addr_name:
			# Create a shipping address linked to the customer
			if not line:
				frappe.throw(_("Address text is required to create a location."))
			cust_label = frappe.db.get_value("Customer", customer, "customer_name") or customer
			addr = frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": f"{cust_label} - Entrega",
					"address_type": "Shipping",
					"address_line1": line,
					"city": "Buenos Aires",
					"country": "Argentina",
					"links": [{"link_doctype": "Customer", "link_name": customer}],
				}
			)
			addr.insert(ignore_permissions=True)
			addr_name = addr.name
			frappe.db.set_value("Delivery Stop", stop.name, "address", addr_name, update_modified=False)
		else:
			if line is not None:
				frappe.db.set_value("Address", addr_name, "address_line1", line, update_modified=True)

		# Refresh stop display text
		try:
			from frappe.contacts.doctype.address.address import get_address_display

			display = get_address_display(addr_name) or line or ""
		except Exception:
			display = line or ""
		if display:
			frappe.db.set_value(
				"Delivery Stop",
				stop.name,
				"customer_address",
				_clean_address_display(display),
				update_modified=False,
			)

		# Coords
		lat_f = lng_f = None
		try:
			if lat is not None and cstr(lat).strip().lower() not in ("", "null", "undefined", "none"):
				lat_f = float(lat)
			if lng is not None and cstr(lng).strip().lower() not in ("", "null", "undefined", "none"):
				lng_f = float(lng)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid coordinates."))

		if lat_f is not None and lng_f is not None:
			updates = {}
			if frappe.db.has_column("Address", "custom_latitude"):
				updates["custom_latitude"] = lat_f
			if frappe.db.has_column("Address", "custom_longitude"):
				updates["custom_longitude"] = lng_f
			if updates:
				frappe.db.set_value("Address", addr_name, updates, update_modified=True)
			# Also pin the stop itself when fields exist
			stop_updates = {}
			if hasattr(stop, "lat"):
				stop_updates["lat"] = lat_f
			if hasattr(stop, "lng"):
				stop_updates["lng"] = lng_f
			if stop_updates:
				frappe.db.set_value("Delivery Stop", stop.name, stop_updates, update_modified=False)

	frappe.db.commit()

	# Return refreshed stop list (same shape as driver_get_trip_stops)
	return driver_get_trip_stops(trip_name)


@frappe.whitelist()
def driver_reorder_stops(trip_name, delivery_note_names=None):
	"""Driver-facing stop reorder for Entregas (HTML5 drag on stop cards).

	Requires ``allow_driver_reorder`` in TMS settings. Works on Draft and
	Submitted owned trips. Visited stops may move; the client usually only
	drags open ones.
	"""
	settings = _load_tms_settings()
	if not settings.get("allow_driver_reorder"):
		frappe.throw(_("Reordering stops is disabled in TMS settings."))

	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)
	if trip.docstatus == 2:
		frappe.throw(_("Cancelled trips cannot be reordered."))

	_apply_trip_stop_order(trip, delivery_note_names)

	# Reload so idx order is fresh
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

	stops = _stops_payload(trip, geo_by_address)
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

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		if outcome == "Delivered":
			evt = "delivered"
		elif outcome == "Not Home":
			evt = "failed"
		else:
			evt = "failed"
		emit_ecommerce_webhook(
			evt,
			{
				"trip": trip_name,
				"stop_idx": stop_idx,
				"outcome": outcome,
				"customer": stop.customer,
				"delivery_note": stop.delivery_note,
			},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook stop outcome")

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
