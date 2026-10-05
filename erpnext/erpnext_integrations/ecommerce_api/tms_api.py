"""TMS (route planning + delivery) API.

Thin layer over core ERPNext `Driver` / `Vehicle` / `Delivery Trip` /
`Delivery Stop`. Stop autosort uses a local TSP (warehouse → stops → warehouse)
with nearest-neighbor seed + 2-opt; Google Maps `process_route` is optional
for distance fill only. Adds proof-of-delivery capture on top.
"""

import math
import re
import secrets
import string

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_address_display
from frappe.utils import cint, cstr, flt, get_datetime, getdate, now_datetime, nowdate
from frappe.utils.file_manager import save_file

from erpnext.stock.doctype.delivery_trip.delivery_trip import sanitize_address
from erpnext.erpnext_integrations.ecommerce_api.ops_kv import idempotent_request


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


def _ensure_trip_locked_field():
	"""Idempotent — create Delivery Trip.custom_locked if patch has not run."""
	if frappe.db.has_column("Delivery Trip", "custom_locked"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Delivery Trip": [
				{
					"fieldname": "custom_locked",
					"fieldtype": "Check",
					"label": "Route Locked",
					"default": "0",
					"insert_after": "custom_pickup_warehouse",
				},
			]
		},
		update=True,
	)
	frappe.clear_cache(doctype="Delivery Trip")


def _trip_is_locked(trip):
	"""True when the dispatcher locked the route (no stop add/remove/reorder)."""
	if not trip:
		return False
	if isinstance(trip, str):
		if not frappe.db.has_column("Delivery Trip", "custom_locked"):
			return False
		return bool(cint(frappe.db.get_value("Delivery Trip", trip, "custom_locked")))
	if not frappe.db.has_column("Delivery Trip", "custom_locked"):
		return False
	return bool(cint(getattr(trip, "custom_locked", 0) or 0))


def _assert_trip_unlocked(trip, action=None):
	if _trip_is_locked(trip):
		name = trip if isinstance(trip, str) else getattr(trip, "name", "")
		msg = _("Route {0} is locked — unlock it before changing stops.").format(name)
		if action:
			msg = _("Route {0} is locked — unlock it before {1}.").format(name, action)
		frappe.throw(msg)


def _locked_trip_for_delivery_note(delivery_note):
	"""Return locked trip name for a DN, or None."""
	dn = cstr(delivery_note or "").strip()
	if not dn or not frappe.db.has_column("Delivery Trip", "custom_locked"):
		return None
	rows = frappe.db.sql(
		"""
		SELECT dt.name
		FROM `tabDelivery Stop` ds
		INNER JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
		WHERE ds.delivery_note = %s
		  AND IFNULL(dt.docstatus, 0) < 2
		  AND IFNULL(dt.custom_locked, 0) = 1
		ORDER BY dt.modified DESC
		LIMIT 1
		""",
		(dn,),
	)
	return rows[0][0] if rows else None


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
	# ifnull: SQL `NULL != 'Not Home'` is unknown, so unset outcomes must stay locked
	# to their trip (otherwise draft trips can be duplicated with the same DNs).
	rows = frappe.db.sql(
		"""
		select distinct delivery_note
		from `tabDelivery Stop`
		where parent in %(parents)s
		  and ifnull(delivery_note, '') != ''
		  and ifnull(custom_outcome, '') != 'Not Home'
		""",
		{"parents": trip_names or [""]},
	)
	return {r[0] for r in rows if r and r[0]}


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
		if frappe.db.has_column("Customer", "mobile_no"):
			fields.append("mobile_no")
		if frappe.db.has_column("Customer", "phone"):
			fields.append("phone")
		if frappe.db.has_column("Customer", "custom_client_phone_e164"):
			fields.append("custom_client_phone_e164")
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
		addr_fields = ["name", "address_line1", "address_line2", "city", "custom_latitude", "custom_longitude"]
		if frappe.db.has_column("Address", "phone"):
			addr_fields.append("phone")
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", address_names]},
			fields=addr_fields,
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

	tracking_by_dn = {}
	dn_names = [s.delivery_note for s in ordered if s.delivery_note]
	if dn_names and frappe.db.has_column("Delivery Note", "custom_tracking_code"):
		for row in frappe.get_all(
			"Delivery Note",
			filters={"name": ["in", dn_names]},
			fields=["name", "custom_tracking_code"],
			ignore_permissions=True,
		):
			code = cstr(row.custom_tracking_code or "").strip()
			if not code:
				code = cstr(_ensure_tracking_code(row.name) or "").strip()
			if code:
				tracking_by_dn[row.name] = code

	so_by_dn = {}
	if dn_names:
		for row in frappe.get_all(
			"Delivery Note Item",
			filters={"parent": ["in", dn_names], "against_sales_order": ["is", "set"]},
			fields=["parent", "against_sales_order", "idx"],
			order_by="idx asc",
			ignore_permissions=True,
		):
			dn = cstr(row.parent or "").strip()
			so = cstr(row.against_sales_order or "").strip()
			if not dn or not so:
				continue
			bucket = so_by_dn.setdefault(dn, [])
			if so not in bucket:
				bucket.append(so)

	return [
		_stop_out(s, geo_by_address, customer_meta, address_meta, tracking_by_dn, so_by_dn)
		for s in ordered
	]


def _valid_map_coord(value):
	"""Return a usable lat/lng, or None. Treat 0 / empty as missing (common on unoptimized stops)."""
	if value in (None, "", "null", "undefined"):
		return None
	try:
		n = float(value)
	except (TypeError, ValueError):
		return None
	if not n or abs(n) < 1e-6:
		return None
	return n


def _stop_out(stop, address_geo=None, customer_meta=None, address_meta=None, tracking_by_dn=None, so_by_dn=None):
	address_geo = address_geo or {}
	customer_meta = customer_meta or {}
	address_meta = address_meta or {}
	tracking_by_dn = tracking_by_dn or {}
	so_by_dn = so_by_dn or {}
	geo = address_geo.get(stop.address) or {}
	outcome = stop.get("custom_outcome") if hasattr(stop, "get") else getattr(stop, "custom_outcome", None)
	cust = customer_meta.get(stop.customer) or {}
	addr = address_meta.get(stop.address) or {}
	addr_line = addr.get("address_line1") or None
	# Prefer optimized route coords; fall back to address geocode (never keep 0,0 placeholders).
	lat = (
		_valid_map_coord(getattr(stop, "lat", None))
		or _valid_map_coord(geo.get("custom_latitude"))
		or _valid_map_coord(addr.get("custom_latitude"))
	)
	lng = (
		_valid_map_coord(getattr(stop, "lng", None))
		or _valid_map_coord(geo.get("custom_longitude"))
		or _valid_map_coord(addr.get("custom_longitude"))
	)
	dn = cstr(stop.delivery_note or "").strip()
	tracking_code = tracking_by_dn.get(dn) if dn else None
	if dn and not tracking_code and frappe.db.has_column("Delivery Note", "custom_tracking_code"):
		tracking_code = _ensure_tracking_code(dn)
	sales_orders = list(so_by_dn.get(dn) or []) if dn else []
	sales_order = sales_orders[0] if sales_orders else None
	phone = None
	for raw in (
		cust.get("custom_client_phone_e164"),
		cust.get("mobile_no"),
		cust.get("phone"),
		addr.get("phone"),
	):
		s = cstr(raw or "").strip()
		if s:
			phone = s
			break
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
		"sales_order": sales_order,
		"sales_orders": sales_orders,
		"tracking_code": tracking_code or None,
		"phone": phone,
		"grand_total": stop.grand_total,
		"contact": stop.contact,
		"visited": bool(stop.visited),
		"outcome": outcome or None,
		"distance": stop.distance,
		"estimated_arrival": stop.estimated_arrival,
		"lat": lat,
		"lng": lng,
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
			"payments": (
				frappe.parse_json(getattr(stop, "custom_payments_json", None))
				if getattr(stop, "custom_payments_json", None)
				else []
			),
			"payment_summary": getattr(stop, "custom_payment_summary", None) or None,
			"requires_factura_a": bool(getattr(stop, "custom_requires_factura_a", 0)),
			"factura_a_status": getattr(stop, "custom_factura_a_status", None) or None,
			"surcharge_pct": getattr(stop, "custom_surcharge_pct", None),
			"surcharge_amount": getattr(stop, "custom_surcharge_amount", None),
			"surcharge_rule": getattr(stop, "custom_surcharge_rule", None) or None,
			"cliente_debe": bool(getattr(stop, "custom_cliente_debe", 0)),
			"balance_after_stop": getattr(stop, "custom_balance_after_stop", None),
			"late_penalty_pct": getattr(stop, "custom_late_penalty_pct", None),
			"late_penalty_amount": getattr(stop, "custom_late_penalty_amount", None),
			"late_penalty_applied": bool(getattr(stop, "custom_late_penalty_applied", 0)),
			"late_penalty_note": getattr(stop, "custom_late_penalty_note", None) or None,
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
		"depot": _default_depot_latlng(company),
		"google_maps_configured": bool(frappe.db.get_single_value("Google Settings", "api_key")),
		"today": str(getdate()),
	}


def _coerce_opt_date(raw, fallback=None):
	if isinstance(raw, str):
		raw = raw.strip()
		if raw.lower() in ("", "null", "undefined", "none"):
			raw = None
	if raw in (None, ""):
		return fallback
	try:
		return getdate(raw)
	except Exception:
		return fallback


@frappe.whitelist(allow_guest=True)
def get_pending_deliveries(date=None, company=None, from_date=None, to_date=None, horizon_days=None):
	"""Unassigned remitos ready for routing.

	Two modes:

	1. **Ceiling** (default, greedy / legacy): ``date`` = as-of ceiling — include
	   every submitted DN not on an active trip whose target ship date
	   (SO ``delivery_date``, else DN ``custom_requested_delivery_date``, else
	   posting_date) is on or before ``as_of``.

	2. **Window** (Rutas day/week UI): pass ``from_date``+``to_date`` or
	   ``horizon_days`` (1–7) with ``date`` as window start. Only remitos whose
	   due falls inside the inclusive range are returned — no overdue spill from
	   earlier weekdays. Spans wider than 7 days are clamped.
	"""
	from frappe.utils import add_days, date_diff

	assigned_notes = _assigned_delivery_note_names()
	today_d = getdate()

	win_from = _coerce_opt_date(from_date)
	win_to = _coerce_opt_date(to_date)
	hz = None
	if horizon_days not in (None, "", "null", "undefined"):
		try:
			hz = cint(horizon_days)
		except Exception:
			hz = None
	if hz is not None and hz < 1:
		hz = 1

	window_mode = bool(win_from or win_to or hz)
	if window_mode:
		if win_from and win_to:
			if win_to < win_from:
				win_from, win_to = win_to, win_from
		elif hz:
			start = _coerce_opt_date(date, today_d) or today_d
			win_from = start
			win_to = add_days(start, max(0, hz - 1))
		elif win_from:
			win_to = add_days(win_from, 6)
		else:
			win_from = add_days(win_to, -6)
		# Hard cap: Rutas local index is a 7-day window.
		if date_diff(win_to, win_from) > 6:
			win_to = add_days(win_from, 6)
		as_of = win_to
		due_lo, due_hi = win_from, win_to
	else:
		as_of = _coerce_opt_date(date, today_d) or today_d
		due_lo, due_hi = None, as_of

	# Lookback only on posting_date (fetch window). Due filter applied below.
	# Ceiling mode may pass as_of = today+365 (greedy all-open); posting lookback
	# must stay near today/min(as_of, today), or recent remitos vanish.
	posting_anchor = as_of if as_of <= today_d else today_d
	since = add_days(posting_anchor, -120)

	filters = {
		"docstatus": 1,
		"is_return": 0,
		"posting_date": [">=", since],
	}
	if assigned_notes:
		filters["name"] = ["not in", assigned_notes]
	if company and str(company).strip() and str(company).strip().lower() not in ("null", "undefined", "none"):
		filters["company"] = str(company).strip()

	dn_fields = [
		"name",
		"customer",
		"customer_name",
		"shipping_address_name",
		"customer_address",
		"grand_total",
		"posting_date",
		"status",
	]
	has_requested_due = frappe.db.has_column("Delivery Note", "custom_requested_delivery_date")
	if has_requested_due:
		dn_fields.append("custom_requested_delivery_date")

	notes = frappe.get_all(
		"Delivery Note",
		filters=filters,
		fields=dn_fields,
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
		elif has_requested_due and n.get("custom_requested_delivery_date"):
			due = n.get("custom_requested_delivery_date")
		else:
			due = n.posting_date
		due_d = getdate(due) if due else as_of
		if window_mode:
			# Exact window — no overdue spill from earlier weekdays.
			if due_d < due_lo or due_d > due_hi:
				continue
			overdue = due_d < today_d
		else:
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
	payload = {"deliveries": out, "as_of": str(as_of), "mode": "window" if window_mode else "ceiling"}
	if window_mode:
		payload["from_date"] = str(due_lo)
		payload["to_date"] = str(due_hi)
		payload["horizon_days"] = date_diff(due_hi, due_lo) + 1
	return payload


def _monday_of(d):
	"""Monday of the ISO-style week containing ``d`` (Mon=0)."""
	from frappe.utils import add_days

	d = getdate(d)
	return add_days(d, -d.weekday())


@frappe.whitelist(allow_guest=True)
def get_rutas_week_bundle(
	date=None,
	company=None,
	auto_assign_zones=1,
	include_master=1,
):
	"""One Rutas payload for the Mon–Sun week containing ``date``.

	Frontend keeps this locally and filters Fleet / Orders / map by day scope —
	no per-day ``list_trips`` / ``list_fleet`` / ``get_pending`` fan-out.

	``include_master`` (default on): also return context, settings, zones, clients,
	map pins/plans, and delivery requests. Pass 0 on soft refreshes when master
	data is already cached client-side.
	"""
	from frappe.utils import add_days, date_diff

	today_d = getdate()
	as_of = _coerce_opt_date(date, today_d) or today_d
	if abs(date_diff(as_of, today_d)) > 7:
		return {
			"ok": False,
			"too_distant": True,
			"as_of": str(as_of),
			"today": str(today_d),
			"from_date": None,
			"to_date": None,
			"deliveries": [],
			"trips": [],
			"fleet_drivers": [],
		}

	from_d = _monday_of(as_of)
	to_d = add_days(from_d, 6)

	# Optional zone fill — once per week load, not a separate round-trip.
	do_assign = True
	if isinstance(auto_assign_zones, str):
		do_assign = auto_assign_zones.strip().lower() not in (
			"0",
			"false",
			"no",
			"null",
			"undefined",
			"none",
			"",
		)
	else:
		do_assign = cint(auto_assign_zones) != 0
	if do_assign:
		try:
			auto_assign_tms_zones(1)
		except Exception:
			pass

	# Orden/Preparado without remito never show in get_pending_deliveries — sync first.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import sync_orden_planner_remitos

		sync_orden_planner_remitos(company=company)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "sync_orden_planner_remitos")

	pending = get_pending_deliveries(
		from_date=str(from_d), to_date=str(to_d), company=company
	)
	# Week window ending Sunday → Mon..Sun; no open-backlog spill across days.
	trips_payload = list_trips_for_date(
		date=str(to_d),
		company=company,
		horizon_days=7,
		include_open_backlog=0,
	)
	fleet_payload = list_fleet_day_routes(
		date=str(to_d),
		horizon_days=7,
		company=company,
		include_open_backlog=0,
	)

	if include_master is None:
		want_master = True
	elif isinstance(include_master, str):
		want_master = include_master.strip().lower() not in (
			"0",
			"false",
			"no",
			"null",
			"undefined",
			"none",
			"",
		)
	else:
		want_master = cint(include_master) != 0

	out = {
		"ok": True,
		"too_distant": False,
		"as_of": str(as_of),
		"today": str(today_d),
		"from_date": str(from_d),
		"to_date": str(to_d),
		"deliveries": pending.get("deliveries") or [],
		"trips": trips_payload.get("trips") or [],
		"fleet_drivers": fleet_payload.get("drivers") or [],
	}

	if want_master:
		try:
			out["context"] = get_planner_context(company=company)
		except Exception:
			out["context"] = None
		try:
			out["tms_settings"] = _load_tms_settings()
		except Exception:
			out["tms_settings"] = None
		try:
			out["zones"] = (_load_tms_zones() or {}).get("zones") or []
		except Exception:
			out["zones"] = []
		try:
			clients = list_delivery_clients(limit=2000, only_geocoded=1)
			out["clients"] = clients.get("clients") or []
		except Exception:
			out["clients"] = []
		try:
			store = _load_tms_map_store()
			out["map_pins"] = store.get("pins") or []
			out["map_plans"] = store.get("plans") or []
		except Exception:
			out["map_pins"] = []
			out["map_plans"] = []
		try:
			reqs = list_delivery_requests(status="Requested")
			out["delivery_requests"] = reqs.get("requests") or []
		except Exception:
			out["delivery_requests"] = []
		# Claim backlog (guest preorders without DN) — same payload Reclamar needs offline.
		try:
			out["claimable_preorders"] = _list_claimable_preorders(company=company)
		except Exception:
			out["claimable_preorders"] = []

	return out


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


def _sales_orders_for_dn(dn):
	"""Unique against_sales_order values on a Delivery Note's items."""
	so_names = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": dn, "against_sales_order": ["is", "set"]},
		pluck="against_sales_order",
		ignore_permissions=True,
	)
	return list({s for s in so_names if s})


def _weekday_tag_from_date(d):
	tags = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
	try:
		return tags[getdate(d).weekday()]
	except Exception:
		return None


def _zone_visit_bases(visit_days):
	tags = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
	out = set()
	for raw in visit_days or []:
		head = cstr(raw or "").split("(", 1)[0].strip()
		if not head:
			continue
		for t in tags:
			if head.lower() == t.lower() or head[:3].lower() == t[:3].lower():
				out.add(t)
				break
	return out


def _snap_dn_zone_to_due_weekday(dn, due_d):
	"""Align Address.custom_zone to the due weekday within the same territory/driver.

	Territory×day zones (T1-WED → T1-THU) keep geography + owning driver while the
	visit day matches the promised due. No-op when the zone already visits that day
	or no sibling day-zone exists.
	"""
	if not frappe.db.has_column("Address", "custom_zone"):
		return None
	addr = frappe.db.get_value("Delivery Note", dn, "shipping_address_name") or frappe.db.get_value(
		"Delivery Note", dn, "customer_address"
	)
	if not addr or not frappe.db.exists("Address", addr):
		return None
	cur = cstr(frappe.db.get_value("Address", addr, "custom_zone") or "").strip()
	if not cur:
		return None
	tag = _weekday_tag_from_date(due_d)
	if not tag:
		return None
	zones_blob = _load_tms_zones() or {}
	zones = zones_blob.get("zones") if isinstance(zones_blob, dict) else zones_blob
	zones = zones if isinstance(zones, list) else []
	cur_up = cur.upper()
	cur_z = None
	for z in zones:
		if not isinstance(z, dict):
			continue
		if cstr(z.get("code") or "").strip().upper() == cur_up or cstr(z.get("name") or "").strip().upper() == cur_up:
			cur_z = z
			break
	if not cur_z:
		return None
	if tag in _zone_visit_bases(cur_z.get("visit_days")):
		return cstr(cur_z.get("code") or cur)
	drv = cstr(cur_z.get("driver") or "").strip()
	code = cstr(cur_z.get("code") or "").strip()
	prefix = code.rsplit("-", 1)[0] if "-" in code else None
	want = f"{prefix}-{tag}".upper() if prefix else None
	candidates = []
	for z in zones:
		if not isinstance(z, dict):
			continue
		if tag not in _zone_visit_bases(z.get("visit_days")):
			continue
		zcode = cstr(z.get("code") or "").strip()
		zdrv = cstr(z.get("driver") or "").strip()
		same_drv = bool(drv and zdrv == drv)
		same_terr = bool(prefix and zcode.upper().startswith(prefix.upper() + "-"))
		if same_drv or same_terr:
			candidates.append(z)
	if not candidates:
		return None
	chosen = None
	if want:
		for z in candidates:
			if cstr(z.get("code") or "").strip().upper() == want:
				chosen = z
				break
	if not chosen:
		chosen = candidates[0]
	new_code = cstr(chosen.get("code") or "").strip()
	if not new_code or new_code.upper() == cur_up:
		return cstr(cur_z.get("code") or cur)
	frappe.db.set_value("Address", addr, "custom_zone", new_code, update_modified=True)
	return new_code


def _zone_code_for_driver_day(driver, due_d):
	"""TMS zone code owned by ``driver`` that visits ``due_d``'s weekday, if any."""
	drv = cstr(driver or "").strip()
	if not drv:
		return None
	tag = _weekday_tag_from_date(due_d)
	if not tag:
		return None
	zones_blob = _load_tms_zones() or {}
	zones = zones_blob.get("zones") if isinstance(zones_blob, dict) else zones_blob
	zones = zones if isinstance(zones, list) else []
	cands = []
	for z in zones:
		if not isinstance(z, dict):
			continue
		if cstr(z.get("driver") or "").strip() != drv:
			continue
		if tag not in _zone_visit_bases(z.get("visit_days")):
			continue
		cands.append(z)
	if not cands:
		return None
	want_suffix = f"-{tag}".upper()
	for z in cands:
		code = cstr(z.get("code") or "").strip()
		if code.upper().endswith(want_suffix):
			return code
	return cstr(cands[0].get("code") or "").strip() or None


def _assign_dn_zone(dn, zone_code):
	"""Set Address.custom_zone for a Delivery Note's shipping address."""
	code = cstr(zone_code or "").strip()
	if not code or not frappe.db.has_column("Address", "custom_zone"):
		return None
	addr = frappe.db.get_value("Delivery Note", dn, "shipping_address_name") or frappe.db.get_value(
		"Delivery Note", dn, "customer_address"
	)
	if not addr or not frappe.db.exists("Address", addr):
		return None
	cur = cstr(frappe.db.get_value("Address", addr, "custom_zone") or "").strip()
	if cur.upper() == code.upper():
		return code
	frappe.db.set_value("Address", addr, "custom_zone", code, update_modified=True)
	return code


def _apply_pending_delivery_due(dn, due_d, driver=None):
	"""Write planning due date for a remito.

	Prefer linked Sales Order ``delivery_date`` when present. POS / claim remitos
	often have no SO — then use ``Delivery Note.custom_requested_delivery_date``
	so unschedule / greedy / auto-groups still work.

	When ``driver`` is set (greedy pack), assign Address.custom_zone to that
	driver's day-zone for ``due_d``. Otherwise snap to the same-territory
	weekday zone (T1-WED + due Thu → T1-THU).
	"""
	so_names = _sales_orders_for_dn(dn)
	via = None
	if so_names:
		for so in so_names:
			frappe.db.set_value("Sales Order", so, "delivery_date", due_d, update_modified=True)
		via = "sales_order"
	elif frappe.db.has_column("Delivery Note", "custom_requested_delivery_date"):
		frappe.db.set_value(
			"Delivery Note",
			dn,
			"custom_requested_delivery_date",
			due_d,
			update_modified=True,
		)
		via = "custom_requested_delivery_date"
	else:
		frappe.throw(_("No Sales Order linked to this Delivery Note; cannot update due date."))

	snapped = None
	try:
		drv = cstr(driver or "").strip() or None
		if drv:
			code = _zone_code_for_driver_day(drv, due_d)
			if code:
				snapped = _assign_dn_zone(dn, code)
		if not snapped:
			snapped = _snap_dn_zone_to_due_weekday(dn, due_d)
	except Exception:
		snapped = None
	return {
		"sales_orders": so_names if via == "sales_order" else [],
		"via": via,
		"zone": snapped,
		"driver": cstr(driver or "").strip() or None,
	}


@frappe.whitelist(allow_guest=True)
def update_pending_delivery_due(delivery_note=None, due_date=None, driver=None):
	"""Update the planning due date for a pending remito.

	Writes Sales Order.delivery_date when linked; otherwise
	Delivery Note.custom_requested_delivery_date (POS remitos without SO).

	Optional ``driver`` force-assigns Address.custom_zone to that driver's
	day-zone for the due weekday (overrides territory snap).
	"""
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

	meta = _apply_pending_delivery_due(dn, due_d, driver=driver)
	frappe.db.commit()
	return {
		"delivery_note": dn,
		"due_date": str(due_d),
		"sales_orders": meta.get("sales_orders") or [],
		"via": meta.get("via"),
		"zone": meta.get("zone"),
		"driver": meta.get("driver"),
	}


@frappe.whitelist(allow_guest=True)
def update_pending_delivery_driver(delivery_note=None, driver=None, due_date=None):
	"""Force-assign a remito to a driver's territory for its due day.

	Keeps the current due (or ``due_date`` if provided) and snaps
	Address.custom_zone to that driver's visiting zone — so plan-day
	grouping follows the chosen conductor despite prior zone labels.
	"""
	dn = cstr(delivery_note or "").strip()
	drv = cstr(driver or "").strip()
	if not dn:
		frappe.throw(_("Delivery Note is required."))
	if not drv:
		frappe.throw(_("Driver is required."))
	if not frappe.db.exists("Delivery Note", dn):
		frappe.throw(_("Delivery Note not found."))
	if not frappe.db.exists("Driver", drv):
		frappe.throw(_("Driver {0} not found").format(drv), frappe.DoesNotExistError)

	due_raw = cstr(due_date or "").strip()
	if due_raw and due_raw.lower() not in ("null", "undefined", "none"):
		try:
			due_d = getdate(due_raw)
		except Exception:
			frappe.throw(_("Invalid due date."))
	else:
		# Resolve current planning due the same way as get_pending_deliveries.
		so_names = _sales_orders_for_dn(dn)
		due_d = None
		for so in so_names:
			dd = frappe.db.get_value("Sales Order", so, "delivery_date")
			if dd:
				due_d = getdate(dd)
				break
		if due_d is None and frappe.db.has_column("Delivery Note", "custom_requested_delivery_date"):
			rd = frappe.db.get_value("Delivery Note", dn, "custom_requested_delivery_date")
			if rd:
				due_d = getdate(rd)
		if due_d is None:
			due_d = getdate(frappe.db.get_value("Delivery Note", dn, "posting_date") or getdate())

	meta = _apply_pending_delivery_due(dn, due_d, driver=drv)
	if not meta.get("zone"):
		frappe.throw(
			_("No TMS zone for driver {0} on {1}. Assign the driver to a zone that visits that weekday.").format(
				drv, str(due_d)
			)
		)
	frappe.db.commit()
	return {
		"delivery_note": dn,
		"due_date": str(due_d),
		"zone": meta.get("zone"),
		"driver": drv,
	}


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

	so_names = [o.name for o in orders]
	item_stats = {}
	if so_names:
		for row in frappe.db.sql(
			"""
			select parent,
				count(*) as item_count,
				sum(ifnull(qty, 0)) as qty_total
			from `tabSales Order Item`
			where parent in %(names)s
			group by parent
			""",
			{"names": so_names},
			as_dict=True,
		):
			item_stats[row.parent] = row

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
		# Fallback: guest_address tag (consultas often lack shipping_address_name).
		if not address_name:
			from erpnext.erpnext_integrations.ecommerce_api.api import (
				_guest_preorder_tag_text,
				_parse_remarks_tags,
			)

			tags = _parse_remarks_tags(_guest_preorder_tag_text(o))
			guest_line = cstr(tags.get("guest_address") or "").strip()
			if guest_line and guest_line not in ("-", "null", "undefined"):
				address_name = None  # street only; claim UI uses address field
				street_fallback = guest_line
			else:
				street_fallback = None
		else:
			street_fallback = None
		stats = item_stats.get(o.name) or {}
		due = o.delivery_date or o.transaction_date
		out.append(
			{
				"kind": "preorder",
				"preorder_name": o.name,
				"delivery_note": None,
				"customer": o.customer,
				"customer_name": o.customer_name,
				"address": street_fallback or address_name,
				"address_name": address_name,
				"grand_total": o.grand_total,
				"posting_date": o.transaction_date or o.delivery_date,
				"due_date": str(getdate(due)) if due else None,
				"status": display,
				"item_count": cint(stats.get("item_count") or 0),
				"qty_total": flt(stats.get("qty_total") or 0),
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
	# Attach geo + street line for preorders that have an address
	address_names = list({p["address"] for p in preorders if p.get("address")})
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
			],
			ignore_permissions=True,
		):
			geo_by_address[row.name] = row
	for p in preorders:
		geo = geo_by_address.get(p.get("address")) or {}
		p["geocoded"] = bool(geo.get("custom_latitude"))
		p["lat"] = geo.get("custom_latitude")
		p["lng"] = geo.get("custom_longitude")
		street_bits = [
			cstr(geo.get("address_line1") or "").strip(),
			cstr(geo.get("address_line2") or "").strip(),
			cstr(geo.get("city") or "").strip(),
		]
		street = ", ".join(b for b in street_bits if b)
		if street:
			p["address"] = street

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
			from erpnext.erpnext_integrations.ecommerce_api.api import (
				_submit_delivery_note_allowing_negative,
			)

			dn = make_delivery_note(so_name)
			default_warehouse = frappe.db.get_value("Company", dn.company, "custom_default_warehouse")
			if default_warehouse:
				for row in dn.items:
					row.warehouse = default_warehouse
				dn.set_warehouse = default_warehouse
			for row in dn.items:
				if hasattr(row, "allow_zero_valuation_rate"):
					row.allow_zero_valuation_rate = 1
			dn.insert(ignore_permissions=True)
			dn.flags.ignore_permissions = True
			# Same as guest-preorder / CSV claim: don't block planning on bin qty.
			_submit_delivery_note_allowing_negative(dn)
			dn_name = dn.name
			frappe.db.commit()
		try:
			from erpnext.erpnext_integrations.ecommerce_api.api import set_guest_preorder_status

			set_guest_preorder_status(so_name, "En Delivery", source="tms_claim")
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
	# i044 flow B: first geocode of a client address → nearest under-cap zone.
	assigned = _auto_assign_zone_if_missing(address_name)
	frappe.db.commit()

	return {
		"lat": location["lat"],
		"lng": location["lng"],
		"cached": False,
		"zone": (assigned or {}).get("zone"),
	}


def _nominatim_headers():
	return {
		"User-Agent": "NextERP-TMS-DriverGeocode/1.0 (local; contact ops)",
		"Accept": "application/json",
		"Accept-Language": "es,en",
	}


def _parse_nominatim_addressdetails(addr):
	"""Map Nominatim addressdetails → ERPNext Address-shaped fields."""
	if not isinstance(addr, dict):
		return {}
	road = cstr(addr.get("road") or addr.get("pedestrian") or addr.get("residential") or "").strip()
	house = cstr(addr.get("house_number") or "").strip()
	line1 = " ".join(p for p in (road, house) if p).strip() or None
	city = (
		cstr(addr.get("city") or "").strip()
		or cstr(addr.get("town") or "").strip()
		or cstr(addr.get("suburb") or "").strip()
		or cstr(addr.get("neighbourhood") or "").strip()
		or cstr(addr.get("city_district") or "").strip()
		or None
	)
	state = (
		cstr(addr.get("state") or "").strip()
		or cstr(addr.get("province") or "").strip()
		or None
	)
	pincode = cstr(addr.get("postcode") or "").strip() or None
	country = cstr(addr.get("country") or "").strip() or None
	return {
		"address_line1": line1,
		"city": city,
		"state": state,
		"pincode": pincode,
		"country": country,
	}


def _geocode_via_nominatim(address_str, country_code="ar"):
	"""Free fallback geocoder (OpenStreetMap Nominatim) when Google Maps key is unset."""
	import urllib.error
	import urllib.parse
	import urllib.request

	params = urllib.parse.urlencode(
		{
			"q": address_str,
			"format": "json",
			"limit": "1",
			"countrycodes": cstr(country_code or "ar").lower(),
			"addressdetails": "1",
		}
	)
	req = urllib.request.Request(
		f"https://nominatim.openstreetmap.org/search?{params}",
		headers=_nominatim_headers(),
		method="GET",
	)
	try:
		with urllib.request.urlopen(req, timeout=12) as resp:
			payload = resp.read().decode("utf-8")
	except urllib.error.HTTPError as e:
		frappe.throw(_("Geocoder HTTP error: {0}").format(e.code))
	except Exception as e:
		frappe.throw(_("Geocoder unavailable: {0}").format(cstr(e)))

	import json

	try:
		rows = json.loads(payload)
	except Exception:
		frappe.throw(_("Geocoder returned invalid JSON."))

	if not isinstance(rows, list) or not rows:
		return None

	top = rows[0] or {}
	try:
		lat = float(top.get("lat"))
		lng = float(top.get("lon"))
	except (TypeError, ValueError):
		return None
	formatted = cstr(top.get("display_name") or address_str).strip()
	parsed = _parse_nominatim_addressdetails(top.get("address") or {})
	return {
		"lat": lat,
		"lng": lng,
		"address": formatted,
		"provider": "nominatim",
		**parsed,
	}


def _reverse_geocode_via_nominatim(lat, lng):
	"""Reverse geocode lat/lng via Nominatim (browser Google Geocoder often REQUEST_DENIED)."""
	import json
	import urllib.error
	import urllib.parse
	import urllib.request

	params = urllib.parse.urlencode(
		{
			"lat": f"{flt(lat):.7f}",
			"lon": f"{flt(lng):.7f}",
			"format": "json",
			"addressdetails": "1",
			"zoom": "18",
		}
	)
	req = urllib.request.Request(
		f"https://nominatim.openstreetmap.org/reverse?{params}",
		headers=_nominatim_headers(),
		method="GET",
	)
	try:
		with urllib.request.urlopen(req, timeout=12) as resp:
			payload = resp.read().decode("utf-8")
	except Exception:
		return None

	try:
		top = json.loads(payload) or {}
	except Exception:
		return None
	if not isinstance(top, dict) or top.get("error"):
		return None
	try:
		lat_f = float(top.get("lat"))
		lng_f = float(top.get("lon"))
	except (TypeError, ValueError):
		lat_f, lng_f = flt(lat), flt(lng)
	formatted = cstr(top.get("display_name") or "").strip()
	parsed = _parse_nominatim_addressdetails(top.get("address") or {})
	return {
		"lat": lat_f,
		"lng": lng_f,
		"address": formatted,
		"provider": "nominatim",
		**parsed,
	}


def _resolve_maps_geocode_api_key():
	"""Google Settings first, then site_config google_maps_api_key."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.google_api import get_google_maps_api_key

		key = get_google_maps_api_key()
		if key:
			return key
	except Exception:
		pass
	return cstr(frappe.conf.get("google_maps_api_key") or "").strip()


@frappe.whitelist(allow_guest=True)
def geocode_query(query=None, region=None):
	"""Geocode free-text address for driver Completar Entrega map search.

	Prefers Google Settings / site_config Maps key (Geocoding API). Falls back to
	OpenStreetMap Nominatim when no key is configured — so search still works on
	sites that only set NEXT_PUBLIC_GOOGLE_MAPS_API_KEY for the browser map.
	"""
	q = cstr(query or "").strip()
	if not q or q.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Address text is required."))

	region = cstr(region or "ar").strip().lower() or "ar"
	address_str = sanitize_address(q) or q
	api_key = _resolve_maps_geocode_api_key()

	if api_key:
		import googlemaps

		maps_client = googlemaps.Client(key=api_key)
		results = None
		try:
			results = maps_client.geocode(
				address_str,
				region=region,
				components={"country": "AR"},
			)
		except Exception as e:
			# Key present but Geocoding API disabled / billing — try Nominatim.
			frappe.log_error(title="geocode_query Google failed", message=cstr(e))

		if results:
			top = results[0]
			location = top["geometry"]["location"]
			formatted = cstr(top.get("formatted_address") or address_str).strip()
			return {
				"lat": location["lat"],
				"lng": location["lng"],
				"address": formatted,
				"cached": False,
				"provider": "google",
			}

	hit = _geocode_via_nominatim(address_str, country_code=region)
	if not hit:
		if not api_key:
			frappe.throw(
				_(
					"Could not geocode that address. Set a Maps API key in Settings → Integraciones "
					"(Google Settings) for better results, or try a fuller street address."
				)
			)
		frappe.throw(_("Could not geocode that address."))

	return {
		"lat": hit["lat"],
		"lng": hit["lng"],
		"address": hit["address"],
		"cached": False,
		"provider": hit.get("provider") or "nominatim",
		"address_line1": hit.get("address_line1"),
		"city": hit.get("city"),
		"state": hit.get("state"),
		"pincode": hit.get("pincode"),
		"country": hit.get("country"),
	}


@frappe.whitelist(allow_guest=True)
def reverse_geocode_query(lat=None, lng=None):
	"""Reverse geocode coordinates → structured address (Address Complete modal).

	Used when the browser Google Geocoder returns REQUEST_DENIED (restricted
	browser keys). Prefers Google Geocoding API when a server key is set;
	falls back to Nominatim.
	"""
	try:
		lat_f = flt(lat)
		lng_f = flt(lng)
	except Exception:
		lat_f, lng_f = 0.0, 0.0
	if not lat_f and not lng_f:
		frappe.throw(_("Latitude and longitude are required."), frappe.ValidationError)

	api_key = _resolve_maps_geocode_api_key()
	if api_key:
		try:
			import googlemaps

			maps_client = googlemaps.Client(key=api_key)
			results = maps_client.reverse_geocode((lat_f, lng_f), language="es")
			if results:
				top = results[0]
				location = (top.get("geometry") or {}).get("location") or {}
				comps = top.get("address_components") or []

				def _long(type_name):
					for c in comps:
						if type_name in (c.get("types") or []):
							return cstr(c.get("long_name") or "").strip()
					return ""

				route = _long("route")
				number = _long("street_number")
				line1 = " ".join(p for p in (route, number) if p).strip() or None
				city = (
					_long("locality")
					or _long("sublocality")
					or _long("neighborhood")
					or _long("administrative_area_level_2")
					or None
				)
				state = _long("administrative_area_level_1") or None
				pincode = _long("postal_code") or None
				country = _long("country") or None
				return {
					"lat": flt(location.get("lat"), lat_f),
					"lng": flt(location.get("lng"), lng_f),
					"address": cstr(top.get("formatted_address") or "").strip(),
					"address_line1": line1,
					"city": city,
					"state": state,
					"pincode": pincode,
					"country": country,
					"provider": "google",
				}
		except Exception as e:
			frappe.log_error(title="reverse_geocode_query Google failed", message=cstr(e))

	hit = _reverse_geocode_via_nominatim(lat_f, lng_f)
	if not hit:
		frappe.throw(_("Could not reverse-geocode that point."), frappe.ValidationError)
	return {
		"lat": hit["lat"],
		"lng": hit["lng"],
		"address": hit.get("address"),
		"address_line1": hit.get("address_line1"),
		"city": hit.get("city"),
		"state": hit.get("state"),
		"pincode": hit.get("pincode"),
		"country": hit.get("country"),
		"provider": hit.get("provider") or "nominatim",
	}


def _resolve_default_vehicle(driver=None, vehicle=None):
	"""Pick a Vehicle for Delivery Trip (core field is reqd=1).

	Order: explicit → driver's Employee vehicle → sole Vehicle in site → None.
	"""
	veh = cstr(vehicle or "").strip() or None
	if veh and veh.lower() in ("null", "undefined", "none"):
		veh = None
	if veh and frappe.db.exists("Vehicle", veh):
		return veh
	drv = cstr(driver or "").strip() or None
	if drv and drv.lower() in ("null", "undefined", "none"):
		drv = None
	if drv:
		emp = frappe.db.get_value("Driver", drv, "employee")
		if emp:
			rows = frappe.get_all(
				"Vehicle",
				filters={"employee": emp},
				pluck="name",
				limit=1,
				ignore_permissions=True,
			)
			if rows:
				return rows[0]
	# Prefer any vehicle assigned to some employee (fleet), else first Vehicle.
	rows = frappe.get_all(
		"Vehicle",
		filters={"employee": ["is", "set"]},
		pluck="name",
		limit=1,
		ignore_permissions=True,
	)
	if rows:
		return rows[0]
	rows = frappe.get_all("Vehicle", pluck="name", limit=1, ignore_permissions=True)
	return rows[0] if rows else None


def _ensure_trip_vehicle(trip):
	"""Fill missing required Vehicle so draft TMS saves don't fail validation."""
	if cstr(getattr(trip, "vehicle", None) or "").strip():
		return trip.vehicle
	veh = _resolve_default_vehicle(getattr(trip, "driver", None), None)
	if veh:
		trip.vehicle = veh
	return veh


def _save_trip_doc(trip):
	"""Save Delivery Trip for TMS, tolerating missing Vehicle when none exist yet."""
	_ensure_trip_vehicle(trip)
	trip.flags.ignore_permissions = True
	if not cstr(getattr(trip, "vehicle", None) or "").strip():
		# Core Delivery Trip.vehicle is reqd=1 — planning must still work before
		# vehicles are configured.
		trip.flags.ignore_mandatory = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()


def _detach_cancelled_so_links_from_trip_dns(trip):
	"""Clear Delivery Note Item → cancelled Sales Order links.

	Draft trips often carry DNs whose against_sales_order was cancelled after
	the note was created. Core DeliveryTrip.on_submit → update_delivery_notes
	calls DN.save() and dies with CancelledLinkError — never block driving.
	"""
	if trip is None:
		return
	dns = list({s.delivery_note for s in (getattr(trip, "delivery_stops", None) or []) if s.delivery_note})
	if not dns:
		return
	rows = frappe.db.sql(
		"""
		select dni.name
		from `tabDelivery Note Item` dni
		inner join `tabSales Order` so on so.name = dni.against_sales_order
		where dni.parent in %(dns)s
		  and so.docstatus = 2
		""",
		{"dns": dns},
	)
	for (row_name,) in rows or []:
		frappe.db.set_value(
			"Delivery Note Item",
			row_name,
			{"against_sales_order": None, "so_detail": None},
			update_modified=False,
		)
	if rows:
		frappe.db.commit()


def _tms_update_delivery_notes_on_trip(trip, delete=False):
	"""Like DeliveryTrip.update_delivery_notes but never fails on dirty DN links.

	Uses db_set for driver/vehicle/lr fields so CancelledLinkError on DN.save
	cannot abort route publish when the driver starts delivering.
	"""
	delivery_notes = list({s.delivery_note for s in (trip.delivery_stops or []) if s.delivery_note})
	update_fields = {
		"driver": None if delete else trip.driver,
		"driver_name": None if delete else trip.driver_name,
		"vehicle_no": None if delete else trip.vehicle,
		"lr_no": None if delete else trip.name,
		"lr_date": None if delete else trip.departure_time,
	}
	for delivery_note in delivery_notes:
		if not frappe.db.exists("Delivery Note", delivery_note):
			continue
		for field, value in update_fields.items():
			try:
				frappe.db.set_value("Delivery Note", delivery_note, field, value, update_modified=False)
			except Exception:
				frappe.log_error(frappe.get_traceback(), "_tms_update_delivery_notes_on_trip")
	frappe.db.commit()


def _ensure_trip_published_for_driving(trip):
	"""Auto-submit a Draft trip and mark In Transit when the driver starts field work.

	Recording a stop outcome / photo is the publish signal — never block with
	"route has not been published yet" (or CancelledLinkError from stale SO links).
	"""
	name = trip.name
	if cint(trip.docstatus) == 2:
		frappe.throw(_("Cancelled trips cannot be updated."))

	if cint(trip.docstatus) == 0:
		if not trip.driver:
			frappe.throw(_("Assign a driver before starting the route."))
		_ensure_trip_vehicle(trip)
		_detach_cancelled_so_links_from_trip_dns(trip)
		trip.flags.ignore_permissions = True
		if not cstr(getattr(trip, "vehicle", None) or "").strip():
			trip.flags.ignore_mandatory = True
		try:
			trip.save(ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.flags.ignore_permissions = True
			trip = frappe.get_doc("Delivery Trip", name)
			frappe.flags.ignore_permissions = False
			_ensure_trip_vehicle(trip)
			_detach_cancelled_so_links_from_trip_dns(trip)
			trip.flags.ignore_permissions = True
			trip.flags.ignore_mandatory = True
			trip.save(ignore_permissions=True)
			frappe.db.commit()
		frappe.flags.ignore_permissions = True
		trip = frappe.get_doc("Delivery Trip", name)
		frappe.flags.ignore_permissions = False
		trip.flags.ignore_permissions = True
		if not cstr(getattr(trip, "vehicle", None) or "").strip():
			trip.flags.ignore_mandatory = True
		# Core on_submit → DN.save() blows up on cancelled Against Sales Order;
		# replace with db_set-based updater for this submit only.
		trip.update_delivery_notes = lambda delete=False: _tms_update_delivery_notes_on_trip(trip, delete=delete)
		try:
			trip.submit()
			frappe.db.commit()
		except frappe.CancelledLinkError:
			frappe.db.rollback()
			# Last resort: force docstatus + status without DN link validation.
			_detach_cancelled_so_links_from_trip_dns(trip)
			frappe.db.set_value(
				"Delivery Trip",
				name,
				{"docstatus": 1, "status": "In Transit"},
				update_modified=True,
			)
			_tms_update_delivery_notes_on_trip(trip, delete=False)
			frappe.db.commit()
		frappe.flags.ignore_permissions = True
		trip = frappe.get_doc("Delivery Trip", name)
		frappe.flags.ignore_permissions = False

	# Prefer In Transit once the driver is actively delivering.
	try:
		st = cstr(getattr(trip, "status", None) or "")
		if st not in ("Completed", "Cancelled"):
			meta = frappe.get_meta("Delivery Trip")
			status_field = meta.get_field("status")
			options = (status_field.options or "") if status_field else ""
			if "In Transit" in options.split("\n") and st != "In Transit":
				frappe.db.set_value("Delivery Trip", name, "status", "In Transit")
				frappe.db.commit()
				trip.status = "In Transit"
	except Exception:
		frappe.log_error(frappe.get_traceback(), "_ensure_trip_published_for_driving")

	return trip


def _live_sales_order(so_name):
	"""Follow ``amended_from`` forward from a cancelled SO to its live amendment."""
	seen = set()
	cur = so_name
	while cur and cur not in seen:
		seen.add(cur)
		if cint(frappe.db.get_value("Sales Order", cur, "docstatus")) != 2:
			return cur
		cur = frappe.db.get_value("Sales Order", {"amended_from": cur}, "name", order_by="creation desc")
	return None


def _repair_dn_cancelled_so_links(dn_names):
	"""Point DN item ``against_sales_order`` at the live amendment (or clear it).

	Pedidos unarchive / rename amend the Sales Order, leaving existing Delivery
	Notes linked to the cancelled original — any later DN save (trip publish,
	POD) then fails with CancelledLinkError.
	"""
	for dn in set(dn_names or []):
		rows = frappe.get_all(
			"Delivery Note Item",
			filters={"parent": dn, "against_sales_order": ["is", "set"]},
			fields=["name", "item_code", "against_sales_order", "so_detail"],
			ignore_permissions=True,
		)
		for row in rows:
			so = row.against_sales_order
			if cint(frappe.db.get_value("Sales Order", so, "docstatus")) != 2:
				continue
			live = _live_sales_order(so)
			so_detail = None
			if live:
				so_detail = frappe.db.get_value(
					"Sales Order Item", {"parent": live, "item_code": row.item_code}, "name"
				)
			frappe.db.set_value(
				"Delivery Note Item",
				row.name,
				{"against_sales_order": live or None, "so_detail": so_detail},
				update_modified=False,
			)


@frappe.whitelist(allow_guest=True)
@idempotent_request
def create_trip(date, driver=None, vehicle=None, delivery_note_names=None, company=None, pickup_warehouse=None):
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

	day = getdate(date)

	# Reuse today's Draft for this driver instead of spawning a second trip.
	if driver:
		existing_drafts = frappe.get_all(
			"Delivery Trip",
			filters={
				"driver": driver,
				"docstatus": 0,
				"departure_time": ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]],
			},
			pluck="name",
			order_by="creation asc",
			ignore_permissions=True,
		)
		if existing_drafts:
			primary = existing_drafts[0]
			frappe.flags.ignore_permissions = True
			trip = frappe.get_doc("Delivery Trip", primary)
			frappe.flags.ignore_permissions = False
			already = {s.delivery_note for s in trip.delivery_stops if s.delivery_note}
			to_add = [n for n in delivery_note_names if n not in already]
			if to_add:
				conflicts = [n for n in to_add if n in _assigned_delivery_note_names()]
				if conflicts:
					frappe.throw(_("Already assigned to another trip: {0}").format(", ".join(conflicts)))
				notes_by_name, address_display_by_name = _load_delivery_notes_for_stops(to_add)
				_append_delivery_stops(trip, to_add, notes_by_name, address_display_by_name)
				_save_trip_doc(trip)
			# Collapse newer duplicate drafts for the same driver/day (empty or subset).
			for extra in existing_drafts[1:]:
				try:
					frappe.flags.ignore_permissions = True
					extra_doc = frappe.get_doc("Delivery Trip", extra)
					frappe.flags.ignore_permissions = False
					extra_dns = {s.delivery_note for s in extra_doc.delivery_stops if s.delivery_note}
					if not extra_dns or extra_dns.issubset(already | set(delivery_note_names)):
						frappe.delete_doc("Delivery Trip", extra, ignore_permissions=True, force=True)
						frappe.db.commit()
				except Exception:
					frappe.log_error(frappe.get_traceback(), "create_trip collapse draft")
			try:
				optimize_trip(primary)
			except Exception:
				frappe.log_error(frappe.get_traceback(), "create_trip auto optimize_trip")
			frappe.flags.ignore_permissions = True
			trip = frappe.get_doc("Delivery Trip", primary)
			frappe.flags.ignore_permissions = False
			return {"trip": trip.name, "status": trip.status, "stop_count": len(trip.delivery_stops)}

	conflicts = [n for n in delivery_note_names if n in _assigned_delivery_note_names()]
	if conflicts:
		frappe.throw(_("Already assigned to another trip: {0}").format(", ".join(conflicts)))

	driver_doc = None
	if driver:
		driver_doc = frappe.db.get_value("Driver", driver, ["full_name", "address"], as_dict=True)

	driver_address, resolved_warehouse = _resolve_pickup_address(company, driver_doc, pickup_warehouse)

	vehicle = _resolve_default_vehicle(driver, vehicle)

	trip = frappe.get_doc(
		{
			"doctype": "Delivery Trip",
			"company": company,
			"driver": driver,
			"driver_name": driver_doc.full_name if driver_doc else None,
			"driver_address": driver_address,
			"custom_pickup_warehouse": resolved_warehouse,
			"vehicle": vehicle,
			"departure_time": get_datetime(f"{day} 08:00:00"),
			"delivery_stops": [],
		}
	)

	notes_by_name, address_display_by_name = _load_delivery_notes_for_stops(delivery_note_names)
	_append_delivery_stops(trip, delivery_note_names, notes_by_name, address_display_by_name)

	trip.flags.ignore_permissions = True
	if not vehicle:
		trip.flags.ignore_mandatory = True
	trip.insert(ignore_permissions=True)
	frappe.db.commit()

	# Closed TSP (warehouse → stops → warehouse) so the first draft order is already sorted.
	try:
		optimize_trip(trip.name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "create_trip auto optimize_trip")

	return {"trip": trip.name, "status": trip.status, "stop_count": len(trip.delivery_stops)}


@frappe.whitelist(allow_guest=True)
def optimize_trip(trip_name):
	"""Autosort trip stops with a closed TSP (warehouse → stops → warehouse).

	Uses haversine nearest-neighbor + 2-opt. Does not require Google Maps.
	Visited stops stay pinned at the front; ungeocoded stops append after.
	"""
	name = cstr(trip_name or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required"))
	if not frappe.db.exists("Delivery Trip", name):
		frappe.throw(_("Delivery Trip {0} not found").format(name), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", name)
	frappe.flags.ignore_permissions = False

	if not trip.delivery_stops:
		return get_trip_map_data(name)

	if trip.docstatus == 2:
		frappe.throw(_("Cancelled trips cannot be optimized."))

	_assert_trip_unlocked(trip, _("optimizing"))

	ordered = _delivery_note_order_tsp(trip)
	if not ordered:
		return get_trip_map_data(name)

	# Draft: rewrite idx via save. Submitted: allow idx-only reorder (driver mid-route).
	_apply_trip_stop_order(trip, ordered)

	# Optional: fill Google leg distances without changing order (ignore failures).
	# process_route() ends in Document.save() — ensure Vehicle is set / mandatory
	# skipped so Google-maps enrichment never resurfaces "Value missing … Vehicle".
	try:
		frappe.flags.ignore_permissions = True
		trip = frappe.get_doc("Delivery Trip", name)
		trip.flags.ignore_permissions = True
		_ensure_trip_vehicle(trip)
		if not cstr(getattr(trip, "vehicle", None) or "").strip():
			trip.flags.ignore_mandatory = True
		if trip.driver_address:
			trip.process_route(optimize=False)
		frappe.flags.ignore_permissions = False
	except Exception:
		frappe.log_error(frappe.get_traceback(), "optimize_trip process_route")

	return get_trip_map_data(name)


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
			"docstatus": cint(trip.docstatus),
			"driver": trip.driver,
			"driver_name": trip.driver_name,
			"vehicle": trip.vehicle,
			"pickup_warehouse": trip.custom_pickup_warehouse,
			"departure_time": trip.departure_time,
			"total_distance": trip.total_distance,
			"uom": trip.uom,
			"locked": _trip_is_locked(trip),
		},
		"stops": stops,
	}


@frappe.whitelist(allow_guest=True)
def publish_trip(trip_name):
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False

	if not trip.driver:
		frappe.throw(_("Assign a driver before publishing the route."))

	_ensure_trip_vehicle(trip)
	if not cstr(getattr(trip, "vehicle", None) or "").strip():
		frappe.throw(_("Assign a vehicle before publishing the route."))
	# Persist auto-filled Vehicle before submit (mandatory on Delivery Trip).
	_save_trip_doc(trip)
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	# Delivery Trip.on_submit re-saves every DN; repair DN lines still pointing at
	# a cancelled (amended) Sales Order so submit can't fail on CancelledLinkError.
	_repair_dn_cancelled_so_links([st.delivery_note for st in trip.delivery_stops if st.delivery_note])

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
def list_trips_for_date(date=None, company=None, horizon_days=1, include_open_backlog=1):
	"""List trips for ``date`` (as-of day) or a trailing window of ``horizon_days``.

	``horizon_days=1`` → that calendar day only.
	``horizon_days=7`` → the 7 days ending on ``date`` (week view for fleet stats).
	Each trip includes ``stop_count``, ``delivered_count``, ``pending_count``, ``vehicle_plate``.

	When ``include_open_backlog`` is on (default), also include Draft / In Transit /
	non-Completed trips whose departure is on or before ``date`` within the last
	~120 days — so the planner left rail still shows unfinished routes when the
	plan date moves forward (reduced bureaucracy).
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

	company_ok = (
		company
		and str(company).strip()
		and str(company).strip().lower() not in ("null", "undefined", "none")
	)
	company_val = str(company).strip() if company_ok else None

	filters = {
		"docstatus": ["!=", 2],
		"departure_time": ["between", [f"{start} 00:00:00", f"{end} 23:59:59"]],
	}
	if company_val:
		filters["company"] = company_val

	trip_fields = [
		"name",
		"status",
		"docstatus",
		"driver",
		"driver_name",
		"vehicle",
		"departure_time",
		"total_distance",
		"uom",
	]
	if frappe.db.has_column("Delivery Trip", "custom_pickup_warehouse"):
		trip_fields.append("custom_pickup_warehouse")
	if frappe.db.has_column("Delivery Trip", "custom_locked"):
		trip_fields.append("custom_locked")

	trips = frappe.get_all(
		"Delivery Trip",
		filters=filters,
		fields=trip_fields,
		order_by="departure_time asc",
		ignore_permissions=True,
	)

	# Open backlog: unfinished routes still sitting from earlier plan days.
	try:
		include_backlog = cint(include_open_backlog)
	except (TypeError, ValueError):
		include_backlog = 1
	if isinstance(include_open_backlog, str) and include_open_backlog.strip().lower() in (
		"0",
		"false",
		"no",
		"null",
		"undefined",
		"none",
		"",
	):
		include_backlog = 0

	if include_backlog:
		seen = {t.name for t in trips}
		backlog_since = add_days(end, -120)
		# Draft (docstatus 0) or submitted but not Completed
		backlog = frappe.get_all(
			"Delivery Trip",
			filters={
				"docstatus": ["!=", 2],
				"departure_time": ["between", [f"{backlog_since} 00:00:00", f"{end} 23:59:59"]],
				**({"company": company_val} if company_val else {}),
			},
			fields=trip_fields,
			order_by="departure_time asc",
			ignore_permissions=True,
		)
		for t in backlog:
			if t.name in seen:
				continue
			st = str(t.status or "")
			# Keep actionable routes only
			if cint(t.docstatus) == 0 or st in ("Draft", "Scheduled", "In Transit", ""):
				trips.append(t)
				seen.add(t.name)
			elif st != "Completed":
				# e.g. custom statuses still open
				trips.append(t)
				seen.add(t.name)
		trips.sort(key=lambda r: str(r.departure_time or ""))

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
		t["pickup_warehouse"] = (
			cstr(t.get("custom_pickup_warehouse") or "").strip() or None
			if frappe.db.has_column("Delivery Trip", "custom_pickup_warehouse")
			else None
		)
		if frappe.db.has_column("Delivery Trip", "custom_locked"):
			t["locked"] = bool(cint(t.get("custom_locked") or 0))
		else:
			t["locked"] = False

	return {
		"date": str(end),
		"from_date": str(start),
		"horizon_days": horizon,
		"trips": trips,
	}


@frappe.whitelist(allow_guest=True)
def list_fleet_day_routes(date=None, horizon_days=1, company=None, include_open_backlog=1):
	"""Per-driver routes + last activity for the Fleet map (no GPS).

	Last position = latest Delivery Stop activity from the Entregas app when
	viewing calendar today. Otherwise (and when idle) units sit at their
	assigned warehouse depot — never at the driver's home.
	"""
	trip_payload = list_trips_for_date(
		date=date,
		company=company,
		horizon_days=horizon_days,
		include_open_backlog=include_open_backlog,
	)
	trips = list(trip_payload.get("trips") or [])
	trip_names = [t.name for t in trips]
	by_driver = {}

	# Seed every active driver so the UI can show waiting / at warehouse.
	for d in frappe.get_all(
		"Driver",
		filters={"status": "Active"},
		fields=["name", "full_name", "address", "employee"],
		ignore_permissions=True,
	):
		by_driver[d.name] = {
			"driver": d.name,
			"driver_name": d.full_name or d.name,
			"employee": d.employee,
			"home_address": d.address,
			"vehicle": None,
			"vehicle_plate": None,
			"has_vehicle": False,
			"last_at": None,
			"routes": [],
		}

	if not trip_names:
		_fleet_fill_plates_and_depot(by_driver, company=company, as_of=date)
		return {
			"date": trip_payload.get("date"),
			"from_date": trip_payload.get("from_date"),
			"horizon_days": trip_payload.get("horizon_days"),
			"drivers": list(by_driver.values()),
		}

	raw_stops = frappe.get_all(
		"Delivery Stop",
		filters={"parent": ["in", trip_names]},
		fields=[
			"name",
			"parent",
			"idx",
			"customer",
			"address",
			"delivery_note",
			"visited",
			"lat",
			"lng",
			"customer_address",
			"custom_outcome",
			"custom_pod_captured_at",
			"custom_pod_recipient_name",
		],
		order_by="parent asc, idx asc",
		ignore_permissions=True,
	)
	addr_names = list({s.address for s in raw_stops if s.address})
	# Also resolve driver home addresses
	for d in by_driver.values():
		if d.get("home_address"):
			addr_names.append(d["home_address"])
	addr_names = list({a for a in addr_names if a})
	geo_by_address = {}
	if addr_names:
		fields = ["name", "custom_latitude", "custom_longitude", "address_line1", "city"]
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", addr_names]},
			fields=fields,
			ignore_permissions=True,
		):
			geo_by_address[row.name] = row

	cust_names = list({s.customer for s in raw_stops if s.customer})
	cust_label = {}
	if cust_names:
		for row in frappe.get_all(
			"Customer",
			filters={"name": ["in", cust_names]},
			fields=["name", "customer_name"],
			ignore_permissions=True,
		):
			cust_label[row.name] = row.customer_name or row.name

	stops_by_trip = {}
	for s in raw_stops:
		stops_by_trip.setdefault(s.parent, []).append(s)

	for t in trips:
		driver = cstr(t.get("driver") or "").strip()
		if not driver:
			continue
		if driver not in by_driver:
			by_driver[driver] = {
				"driver": driver,
				"driver_name": t.get("driver_name") or driver,
				"employee": None,
				"home_address": None,
				"vehicle": None,
				"vehicle_plate": None,
				"has_vehicle": False,
				"last_at": None,
				"routes": [],
			}
		entry = by_driver[driver]
		plate = t.get("vehicle_plate")
		veh = t.get("vehicle")
		if veh:
			entry["vehicle"] = veh
			entry["vehicle_plate"] = plate or veh
			entry["has_vehicle"] = True

		route_stops = []
		best_activity = None  # (sort_key, payload)
		for s in stops_by_trip.get(t.name) or []:
			geo = geo_by_address.get(s.address) or {}
			lat = _valid_map_coord(s.lat) or _valid_map_coord(geo.get("custom_latitude"))
			lng = _valid_map_coord(s.lng) or _valid_map_coord(geo.get("custom_longitude"))
			outcome = cstr(s.custom_outcome or "").strip() or None
			visited = cint(s.visited) == 1 or bool(outcome)
			cname = cust_label.get(s.customer) or s.customer
			label = cname or _clean_address_display(s.customer_address) or s.delivery_note
			stop_row = {
				"idx": cint(s.idx),
				"customer": s.customer,
				"customer_name": cname,
				"delivery_note": s.delivery_note,
				"address": s.address,
				"label": label,
				"visited": visited,
				"outcome": outcome,
				"captured_at": str(s.custom_pod_captured_at) if s.custom_pod_captured_at else None,
				"lat": lat,
				"lng": lng,
			}
			route_stops.append(stop_row)
			if visited and lat is not None and lng is not None:
				# Prefer wall-clock POD time; else stop order (later idx = later on route).
				ts = s.custom_pod_captured_at
				sort_key = (1, str(ts), cint(s.idx)) if ts else (0, "", cint(s.idx))
				payload = {
					"lat": lat,
					"lng": lng,
					"label": label,
					"stop_idx": cint(s.idx),
					"outcome": outcome or ("Delivered" if visited else None),
					"at": str(ts) if ts else None,
					"trip_name": t.name,
					"delivery_note": s.delivery_note,
					"customer_name": cname,
					"source": "stop",
				}
				if best_activity is None or sort_key >= best_activity[0]:
					best_activity = (sort_key, payload)

		entry["routes"].append(
			{
				"trip_name": t.name,
				"status": t.get("status"),
				"docstatus": cint(t.get("docstatus")),
				"departure_time": str(t.get("departure_time") or "") or None,
				"vehicle": veh,
				"vehicle_plate": plate,
				"stop_count": len(route_stops),
				"stops": route_stops,
			}
		)
		if best_activity:
			prev = entry.get("last_at")
			# Keep the chronologically latest activity across trips for this driver.
			if not prev:
				entry["last_at"] = best_activity[1]
			else:
				prev_at = prev.get("at") or ""
				new_at = best_activity[1].get("at") or ""
				if new_at and (not prev_at or new_at >= prev_at):
					entry["last_at"] = best_activity[1]
				elif not new_at and not prev_at:
					if cint(best_activity[1].get("stop_idx")) >= cint(prev.get("stop_idx") or 0):
						entry["last_at"] = best_activity[1]

	_fleet_fill_plates_and_depot(by_driver, geo_by_address, company=company, as_of=date)
	return {
		"date": trip_payload.get("date"),
		"from_date": trip_payload.get("from_date"),
		"horizon_days": trip_payload.get("horizon_days"),
		"drivers": list(by_driver.values()),
	}


def _warehouse_pins_index():
	"""Map vehicle name/plate → warehouse pin; first warehouse pin as default."""
	store = _load_tms_map_store()
	by_veh = {}
	default_pin = None
	for p in store.get("pins") or []:
		if not isinstance(p, dict) or cstr(p.get("kind") or "") != "warehouse":
			continue
		if default_pin is None:
			default_pin = p
		for v in p.get("vehicles") or []:
			key = cstr(v or "").strip()
			if key:
				by_veh[key] = p
	return by_veh, default_pin


def _fleet_fill_plates_and_depot(by_driver, geo_by_address=None, company=None, as_of=None):
	"""Attach vehicle plates and park idle units at their warehouse depot.

	Never uses driver home. Non-today plan days always show the warehouse;
	on calendar today, real stop/POD activity wins when present.
	"""
	emp_ids = [d["employee"] for d in by_driver.values() if d.get("employee")]
	plate_by_emp = {}
	vehicle_by_emp = {}
	if emp_ids:
		for row in frappe.get_all(
			"Vehicle",
			filters={"employee": ["in", emp_ids]},
			fields=["employee", "license_plate", "name"],
			ignore_permissions=True,
		):
			if row.employee and row.employee not in plate_by_emp:
				plate_by_emp[row.employee] = row.license_plate or row.name
				vehicle_by_emp[row.employee] = row.name

	as_of_d = getdate(as_of) if as_of else getdate()
	is_today = as_of_d == getdate()
	if not is_today:
		for d in by_driver.values():
			d["last_at"] = None

	by_veh, default_pin = _warehouse_pins_index()
	depot_fb = _default_depot_latlng(company)

	for d in by_driver.values():
		emp = d.get("employee")
		if emp and plate_by_emp.get(emp):
			d["has_vehicle"] = True
			if not d.get("vehicle_plate"):
				d["vehicle_plate"] = plate_by_emp[emp]
			if not d.get("vehicle"):
				d["vehicle"] = vehicle_by_emp.get(emp)
		src = cstr((d.get("last_at") or {}).get("source") or "")
		# Keep live POD / stop activity only for calendar today.
		if is_today and d.get("last_at") and src == "stop":
			continue
		veh_key = cstr(d.get("vehicle") or "").strip()
		plate_key = cstr(d.get("vehicle_plate") or "").strip()
		pin = by_veh.get(veh_key) or by_veh.get(plate_key) or default_pin
		if pin and (pin.get("lat") is not None or pin.get("lng") is not None):
			lat = flt(pin.get("lat"))
			lng = flt(pin.get("lng"))
			label = cstr(pin.get("label") or "").strip()
			ref = cstr(pin.get("ref_name") or "").strip()
			wh_name = ref or None
			if ref and frappe.db.exists("Warehouse", ref):
				wn = cstr(frappe.db.get_value("Warehouse", ref, "warehouse_name") or "").strip()
				if wn:
					label = wn
			if not label:
				label = cstr(pin.get("address") or "").strip() or _("Warehouse")
		else:
			lat = flt(depot_fb.get("lat"))
			lng = flt(depot_fb.get("lng"))
			label = _("Warehouse")
			wh_name = None
		d["last_at"] = {
			"lat": lat,
			"lng": lng,
			"label": label,
			"stop_idx": None,
			"outcome": None,
			"at": None,
			"trip_name": None,
			"delivery_note": None,
			"customer_name": None,
			"source": "warehouse",
			"warehouse": wh_name,
		}


def _fleet_fill_home_and_plates(by_driver, geo_by_address=None, company=None, as_of=None):
	"""Back-compat alias — parks at warehouse, never home."""
	return _fleet_fill_plates_and_depot(
		by_driver, geo_by_address=geo_by_address, company=company, as_of=as_of
	)


def _trip_owning_delivery_note(delivery_note, exclude_trip=None):
	"""Active (non-cancelled) trip that currently holds this DN as a stop."""
	dn = cstr(delivery_note or "").strip()
	if not dn:
		return None
	locked_expr = (
		"IFNULL(dt.custom_locked, 0)"
		if frappe.db.has_column("Delivery Trip", "custom_locked")
		else "0"
	)
	rows = frappe.db.sql(
		f"""
		SELECT ds.parent, dt.docstatus, {locked_expr} as locked, dt.status, dt.driver
		FROM `tabDelivery Stop` ds
		INNER JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
		WHERE ds.delivery_note = %s
		  AND IFNULL(dt.docstatus, 0) < 2
		  AND IFNULL(ds.custom_outcome, '') != 'Not Home'
		ORDER BY dt.modified DESC
		LIMIT 5
		""",
		(dn,),
		as_dict=True,
	)
	for r in rows:
		if exclude_trip and r.parent == exclude_trip:
			continue
		return r
	return None


@frappe.whitelist(allow_guest=True)
def add_stops_to_trip(trip_name, delivery_note_names, allow_steal=0):
	delivery_note_names = frappe.parse_json(delivery_note_names) if isinstance(delivery_note_names, str) else (delivery_note_names or [])
	if not delivery_note_names:
		frappe.throw(_("Select at least one order to add."))

	try:
		steal = cint(allow_steal)
	except (TypeError, ValueError):
		steal = 0
	if isinstance(allow_steal, str) and allow_steal.strip().lower() in ("1", "true", "yes"):
		steal = 1

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	if trip.docstatus != 0:
		frappe.throw(_("Stops can only be added to a Draft trip."))
	_assert_trip_unlocked(trip, _("adding stops"))

	already_on_trip = {s.delivery_note for s in trip.delivery_stops if s.delivery_note}
	# Already on this trip (offline outbox replay) → skip instead of failing.
	delivery_note_names = [n for n in delivery_note_names if n not in already_on_trip]
	if not delivery_note_names:
		return get_trip_map_data(trip_name)

	conflicts = [n for n in delivery_note_names if n in _assigned_delivery_note_names()]
	if conflicts and not steal:
		frappe.throw(_("Already assigned to another trip: {0}").format(", ".join(conflicts)))

	stolen_from = []
	if conflicts and steal:
		# Pull stops off other draft unlocked trips before attaching here.
		by_src = {}
		for dn in conflicts:
			owner = _trip_owning_delivery_note(dn, exclude_trip=trip_name)
			if not owner:
				continue
			if cint(owner.docstatus) != 0:
				frappe.throw(
					_("Cannot steal {0} — source trip {1} is not Draft.").format(dn, owner.parent)
				)
			if cint(owner.locked):
				frappe.throw(
					_("Cannot steal {0} — source trip {1} is locked.").format(dn, owner.parent)
				)
			by_src.setdefault(owner.parent, []).append(dn)
		for src_name, dns in by_src.items():
			remove_stops_from_trip(src_name, dns)
			stolen_from.append({"trip": src_name, "delivery_notes": dns})

	notes_by_name, address_display_by_name = _load_delivery_notes_for_stops(delivery_note_names)
	_append_delivery_stops(trip, delivery_note_names, notes_by_name, address_display_by_name)

	_save_trip_doc(trip)

	try:
		map_data = optimize_trip(trip_name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "add_stops_to_trip auto optimize_trip")
		map_data = get_trip_map_data(trip_name)
	if stolen_from:
		map_data = dict(map_data or {})
		map_data["stolen_from"] = stolen_from
	return map_data


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
	_assert_trip_unlocked(trip, _("removing stops"))

	names = set(delivery_note_names)
	rows_to_remove = [s for s in trip.delivery_stops if s.delivery_note in names]
	if not rows_to_remove:
		# Already removed (offline outbox replay / another dispatcher) → no-op.
		return get_trip_map_data(trip_name)

	if len(rows_to_remove) >= len(trip.delivery_stops or []):
		# A Delivery Trip needs at least one stop: removing the last one deletes
		# the (draft) trip instead of failing with MandatoryError.
		frappe.delete_doc("Delivery Trip", trip_name, ignore_permissions=True, force=1)
		frappe.db.commit()
		return {
			"trip": {"name": trip_name, "status": "Deleted", "docstatus": 2, "locked": False},
			"stops": [],
			"deleted": 1,
		}

	# Snapshot list - trip.remove() mutates trip.delivery_stops in place and
	# renumbers idx for the remaining rows, so iterate the snapshot, not the
	# live list.
	for row in rows_to_remove:
		trip.remove(row)

	_save_trip_doc(trip)

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
	# Offline-replay tolerant: the order may have been captured before a stop was
	# added/removed elsewhere. Unknown DNs are dropped; stops not in the list keep
	# their current relative order after the listed ones.
	names = [dn for dn in names if dn in by_dn]
	if not names:
		frappe.throw(_("Stop not on this trip: {0}").format(_parse_delivery_note_order(delivery_note_names)[0]))
	listed = set(names)
	names = names + [
		s.delivery_note
		for s in sorted(trip.delivery_stops, key=lambda r: r.idx or 0)
		if s.delivery_note and s.delivery_note not in listed
	]

	ordered = [by_dn[dn] for dn in names]
	orphans = [s for s in trip.delivery_stops if not s.delivery_note]
	final_rows = ordered + orphans

	if trip.docstatus == 0:
		for i, row in enumerate(final_rows, start=1):
			row.idx = i
		_save_trip_doc(trip)
	elif trip.docstatus == 1:
		# Submitted trips can't use Document.save for child reorder — set idx directly.
		for i, row in enumerate(final_rows, start=1):
			frappe.db.set_value("Delivery Stop", row.name, "idx", i, update_modified=False)
		frappe.db.commit()
	else:
		frappe.throw(_("Cancelled trips cannot be reordered."))

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
	_assert_trip_unlocked(trip, _("reordering stops"))

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
	else:
		_ensure_trip_vehicle(trip)

	_save_trip_doc(trip)

	return get_trip_map_data(trip_name)


@frappe.whitelist(allow_guest=True)
def cancel_trip(trip_name, force=0):
	"""Reset / cancel a route (frees delivery notes back to the pending pool).

	Locked routes require ``force=1`` (force-stop from the sidebar) or unlock first.
	"""
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False

	if trip.docstatus == 2:
		frappe.throw(_("Trip {0} is already cancelled.").format(trip_name))

	try:
		force_ok = cint(force)
	except (TypeError, ValueError):
		force_ok = 0
	if isinstance(force, str) and force.strip().lower() in ("1", "true", "yes"):
		force_ok = 1

	if _trip_is_locked(trip) and not force_ok:
		frappe.throw(
			_("Route {0} is locked — unlock it first, or use force-stop.").format(trip_name)
		)

	# Force-stop clears the lock so cancel can proceed cleanly.
	if force_ok and _trip_is_locked(trip) and frappe.db.has_column("Delivery Trip", "custom_locked"):
		frappe.db.set_value("Delivery Trip", trip_name, "custom_locked", 0, update_modified=False)
		trip.custom_locked = 0

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
def set_trip_locked(trip_name, locked=1):
	"""Toggle route lock. Locked trips keep visits fixed; outcomes / line edits still OK."""
	_ensure_trip_locked_field()
	name = cstr(trip_name or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required"))
	if not frappe.db.exists("Delivery Trip", name):
		frappe.throw(_("Delivery Trip {0} not found").format(name), frappe.DoesNotExistError)

	try:
		want = cint(locked)
	except (TypeError, ValueError):
		want = 0
	if isinstance(locked, str) and locked.strip().lower() in ("1", "true", "yes"):
		want = 1
	elif isinstance(locked, str) and locked.strip().lower() in ("0", "false", "no", "null", "undefined", ""):
		want = 0

	frappe.db.set_value("Delivery Trip", name, "custom_locked", 1 if want else 0)
	frappe.db.commit()
	return get_trip_map_data(name)


@frappe.whitelist(allow_guest=True)
def start_trip(trip_name):
	"""Mark a route as started (submit draft → In Transit, or Submitted → In Transit)."""
	name = cstr(trip_name or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required"))

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", name)
	frappe.flags.ignore_permissions = False

	_ensure_trip_published_for_driving(trip)
	return get_trip_map_data(name)


@frappe.whitelist(allow_guest=True)
def get_delivery_note_route_lock(delivery_note=None):
	"""Lookup helper for UI warnings when editing a pedido on a locked route."""
	dn = cstr(delivery_note or "").strip()
	if not dn or dn.lower() in ("null", "undefined", "none"):
		return {"delivery_note": dn or None, "locked": False, "trip": None}
	trip = _locked_trip_for_delivery_note(dn)
	return {"delivery_note": dn, "locked": bool(trip), "trip": trip}


@frappe.whitelist(allow_guest=True)
@idempotent_request
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
@idempotent_request
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
	"late_payment_penalty_pct": 3.0,  # e.g. 3 = 3%; 0 disables
	"transfer_surcharge_pct": 3.0,  # % on transfer portion when no Factura A
	"factura_a_with_transfer_surcharge_pct": 3.0,  # % on full total when Factura A + transfer (not stacked)
	# so_line | payment_entry | snapshot_only — how surcharge is booked
	"surcharge_accounting_mode": "so_line",
	"sync_delivery_payment_to_so": True,  # PE + Completado on delivery collection
	"require_signature": "always",  # always | never | per_outcome
	"require_photo_on_not_home": True,
	"allow_driver_reorder": True,
	"allow_driver_delivery_request": True,
	"require_pin_for_order_actions": True,
	"print_template_delivery": None,
	"print_template_payment": None,
	"tracking_code_length": 8,
	# Auto-create Sales Invoice credit notes (+ DN returns) from driver returns / partials.
	"auto_credit_note_on_return": True,
	"auto_credit_note_on_partial": True,
	# i039 auto groups — días de venta + whether visit tags include Man/Med/Tar/Noc
	"auto_group_working_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
	"auto_group_use_time_slots": False,
	"auto_group_excluded_slots": [],
	# i045 packing strategies
	"packing_strategy": "client_zone",  # client_zone | fast_deliver_greedy
	"driver_day_max_orders": 30,
	"driver_day_max_minutes": 480,
	"default_stop_minutes": 15,
	"avg_speed_kmh": 25,
	"greedy_horizon_days": 14,
	"delivery_lead_days": 1,
	"drive_buffer_minutes_per_leg": 5,
	"nearby_driver_max_km": 8,
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
def preview_delivery_payment_surcharge(
	goods_total=None,
	payments=None,
	requires_factura_a=None,
	amount_collected=None,
	payment_method=None,
):
	"""Preview surcharge for delivery multi-pay UI / regression tests. No writes."""
	settings = _load_tms_settings()
	goods = max(0.0, flt(goods_total))
	req_fa = _truthy_flag(requires_factura_a, default=False)
	pay_rows = _normalize_delivery_payments(
		payments, amount_collected=amount_collected, payment_method=payment_method
	)
	pct, base, amount, rule = _compute_transfer_surcharge(
		pay_rows,
		requires_factura_a=req_fa,
		goods_total=goods,
		settings=settings,
	)
	collected = sum(flt(p.get("amount") or 0) for p in pay_rows)
	return {
		"goods_total": goods,
		"payments": pay_rows,
		"payment_summary": _payment_summary_label(pay_rows),
		"requires_factura_a": req_fa,
		"surcharge_pct": pct,
		"surcharge_base": base,
		"surcharge_amount": amount,
		"surcharge_rule": rule,
		"amount_due": round(goods + flt(amount), 2),
		"amount_collected": collected,
		"balance": round(goods + flt(amount) - collected, 2),
	}


@frappe.whitelist(allow_guest=True)
def save_tms_settings(
	delivery_payment_mode=None,
	late_payment_penalty_pct=None,
	transfer_surcharge_pct=None,
	factura_a_with_transfer_surcharge_pct=None,
	surcharge_accounting_mode=None,
	sync_delivery_payment_to_so=None,
	require_signature=None,
	require_photo_on_not_home=None,
	allow_driver_reorder=None,
	allow_driver_delivery_request=None,
	require_pin_for_order_actions=None,
	print_template_delivery=None,
	print_template_payment=None,
	tracking_code_length=None,
	auto_credit_note_on_return=None,
	auto_credit_note_on_partial=None,
	auto_group_working_days=None,
	auto_group_use_time_slots=None,
	auto_group_excluded_slots=None,
	packing_strategy=None,
	driver_day_max_orders=None,
	driver_day_max_minutes=None,
	default_stop_minutes=None,
	avg_speed_kmh=None,
	greedy_horizon_days=None,
	delivery_lead_days=None,
	drive_buffer_minutes_per_leg=None,
	nearby_driver_max_km=None,
):
	return _persist_tms_settings(
		{
			"delivery_payment_mode": delivery_payment_mode,
			"late_payment_penalty_pct": late_payment_penalty_pct,
			"transfer_surcharge_pct": transfer_surcharge_pct,
			"factura_a_with_transfer_surcharge_pct": factura_a_with_transfer_surcharge_pct,
			"surcharge_accounting_mode": surcharge_accounting_mode,
			"sync_delivery_payment_to_so": sync_delivery_payment_to_so,
			"require_signature": require_signature,
			"require_photo_on_not_home": require_photo_on_not_home,
			"allow_driver_reorder": allow_driver_reorder,
			"allow_driver_delivery_request": allow_driver_delivery_request,
			"require_pin_for_order_actions": require_pin_for_order_actions,
			"print_template_delivery": print_template_delivery,
			"print_template_payment": print_template_payment,
			"tracking_code_length": tracking_code_length,
			"auto_credit_note_on_return": auto_credit_note_on_return,
			"auto_credit_note_on_partial": auto_credit_note_on_partial,
			"auto_group_working_days": auto_group_working_days,
			"auto_group_use_time_slots": auto_group_use_time_slots,
			"auto_group_excluded_slots": auto_group_excluded_slots,
			"packing_strategy": packing_strategy,
			"driver_day_max_orders": driver_day_max_orders,
			"driver_day_max_minutes": driver_day_max_minutes,
			"default_stop_minutes": default_stop_minutes,
			"avg_speed_kmh": avg_speed_kmh,
			"greedy_horizon_days": greedy_horizon_days,
			"delivery_lead_days": delivery_lead_days,
			"drive_buffer_minutes_per_leg": drive_buffer_minutes_per_leg,
			"nearby_driver_max_km": nearby_driver_max_km,
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
		"auto_credit_note_on_return",
		"auto_credit_note_on_partial",
		"sync_delivery_payment_to_so",
	}
	list_keys = {"auto_group_working_days", "auto_group_excluded_slots"}
	int_keys = {
		"tracking_code_length",
		"driver_day_max_orders",
		"driver_day_max_minutes",
		"default_stop_minutes",
		"avg_speed_kmh",
		"greedy_horizon_days",
		"delivery_lead_days",
		"drive_buffer_minutes_per_leg",
		"nearby_driver_max_km",
	}
	float_keys = {"late_payment_penalty_pct", "transfer_surcharge_pct", "factura_a_with_transfer_surcharge_pct"}
	for key, value in raw.items():
		if value is None:
			continue
		if key in bool_keys:
			current[key] = _truthy_flag(value, default=bool(TMS_SETTINGS_DEFAULTS.get(key, False)))
		elif key in float_keys:
			try:
				if isinstance(value, str) and value.strip().lower() in ("", "null", "undefined", "none"):
					n = flt(TMS_SETTINGS_DEFAULTS.get(key, 0))
				else:
					n = flt(value)
			except (TypeError, ValueError):
				n = flt(TMS_SETTINGS_DEFAULTS.get(key, 0))
			current[key] = max(0.0, min(100.0, n))
		elif key in int_keys:
			try:
				n = cint(value)
			except (TypeError, ValueError):
				n = TMS_SETTINGS_DEFAULTS.get(key, 0)
			if isinstance(value, str) and value.strip().lower() in ("", "null", "undefined", "none"):
				n = TMS_SETTINGS_DEFAULTS.get(key, 0)
			if key == "tracking_code_length":
				n = n or TMS_SETTINGS_DEFAULTS["tracking_code_length"]
			current[key] = max(0, n)
		elif key == "packing_strategy":
			s = cstr(value).strip().lower()
			if s in ("", "null", "undefined", "none"):
				s = TMS_SETTINGS_DEFAULTS["packing_strategy"]
			if s not in ("client_zone", "fast_deliver_greedy"):
				s = TMS_SETTINGS_DEFAULTS["packing_strategy"]
			current[key] = s
		elif key == "surcharge_accounting_mode":
			s = cstr(value).strip().lower()
			if s in ("", "null", "undefined", "none"):
				s = TMS_SETTINGS_DEFAULTS["surcharge_accounting_mode"]
			if s not in ("so_line", "payment_entry", "snapshot_only"):
				s = TMS_SETTINGS_DEFAULTS["surcharge_accounting_mode"]
			current[key] = s
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
	if not code and name:
		# Auto code from the name (offline-created zones only carry a label).
		code = re.sub(r"[^A-Z0-9]+", "-", frappe.scrub(name).upper()).strip("-")[:24]
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
	# Owning driver (i044 territory × day zones: one zone per driver per day).
	driver = raw.get("driver") if "driver" in raw else existing.get("driver")
	driver = cstr(driver or "").strip() or None
	return {
		"code": code,
		"name": name,
		"color": str(raw.get("color") or existing.get("color") or _ZONE_DEFAULT_COLORS[0]),
		"type": str(raw.get("type") or existing.get("type") or "delivery"),
		"visit_days": list(visit_days or []),
		"vehicles": list(vehicles or []),
		"notes": str(raw.get("notes") or existing.get("notes") or ""),
		"driver": driver,
	}


@frappe.whitelist(allow_guest=True)
def list_tms_zones():
	return _load_tms_zones()


@frappe.whitelist(allow_guest=True)
def list_delivery_clients(limit=2000, only_geocoded=1):
	"""Active customers with shipping/primary geocoded Addresses for the Clients map tab.

	Returns one row per address (a customer may appear more than once).
	``zone`` is Address.custom_zone; ``visit_days`` filled from matching TMS zone when possible.
	"""
	raw_lim = limit
	if raw_lim is None or (
		isinstance(raw_lim, str)
		and raw_lim.strip().lower() in ("", "null", "undefined", "none")
	):
		lim = 2000
	else:
		try:
			lim = cint(raw_lim)
		except (TypeError, ValueError):
			lim = 2000
	if lim < 1:
		lim = 2000
	if lim > 5000:
		lim = 5000

	if only_geocoded is None:
		geo_only = True
	elif isinstance(only_geocoded, str) and only_geocoded.strip().lower() in (
		"",
		"null",
		"undefined",
		"none",
	):
		geo_only = True
	else:
		geo_only = cint(only_geocoded) != 0

	has_zone = frappe.db.has_column("Address", "custom_zone")
	has_lat = frappe.db.has_column("Address", "custom_latitude")
	has_lng = frappe.db.has_column("Address", "custom_longitude")
	has_freq = frappe.db.has_column("Address", "custom_delivery_frequency")
	if not has_lat or not has_lng:
		return {"clients": [], "total": 0, "geocoded": 0}

	zone_sel = "addr.custom_zone AS zone" if has_zone else "NULL AS zone"
	freq_sel = (
		"addr.custom_delivery_frequency AS frequency" if has_freq else "NULL AS frequency"
	)
	geo_clause = (
		"AND IFNULL(addr.custom_latitude, 0) != 0 AND IFNULL(addr.custom_longitude, 0) != 0"
		if geo_only
		else ""
	)

	rows = frappe.db.sql(
		f"""
		SELECT
			c.name AS customer,
			c.customer_name AS customer_name,
			addr.name AS address_name,
			addr.address_line1 AS address_line1,
			addr.city AS city,
			addr.custom_latitude AS lat,
			addr.custom_longitude AS lng,
			{zone_sel},
			{freq_sel},
			IFNULL(addr.is_shipping_address, 0) AS is_shipping,
			IFNULL(addr.is_primary_address, 0) AS is_primary
		FROM `tabCustomer` c
		INNER JOIN `tabDynamic Link` dl
			ON dl.link_doctype = 'Customer'
			AND dl.link_name = c.name
			AND dl.parenttype = 'Address'
		INNER JOIN `tabAddress` addr
			ON addr.name = dl.parent
		WHERE IFNULL(c.disabled, 0) = 0
			AND IFNULL(addr.disabled, 0) = 0
			{geo_clause}
		ORDER BY c.customer_name ASC, addr.is_shipping_address DESC, addr.modified DESC
		LIMIT %(lim)s
		""",
		{"lim": lim},
		as_dict=True,
	)

	zones = _load_tms_zones().get("zones") or []
	zone_by_key = {}
	for z in zones:
		if not isinstance(z, dict):
			continue
		code = cstr(z.get("code") or "").strip().upper()
		name = cstr(z.get("name") or "").strip().upper()
		if code:
			zone_by_key[code] = z
		if name:
			zone_by_key[name] = z

	clients = []
	geocoded = 0
	for row in rows:
		try:
			lat = flt(row.get("lat"))
			lng = flt(row.get("lng"))
		except Exception:
			lat = lng = 0
		is_geo = bool(lat and lng)
		if is_geo:
			geocoded += 1
		zone_label = cstr(row.get("zone") or "").strip()
		visit_days = []
		zone_color = None
		if zone_label:
			hit = zone_by_key.get(zone_label.upper())
			if not hit:
				for key, z in zone_by_key.items():
					if zone_label.upper() in key or key in zone_label.upper():
						hit = z
						break
			if hit:
				visit_days = list(hit.get("visit_days") or [])
				zone_color = hit.get("color")
		clients.append(
			{
				"id": cstr(row.get("address_name") or ""),
				"customer": cstr(row.get("customer") or ""),
				"customer_name": cstr(row.get("customer_name") or row.get("customer") or ""),
				"address_name": cstr(row.get("address_name") or ""),
				"address": ", ".join(
					p
					for p in [
						cstr(row.get("address_line1") or "").strip(),
						cstr(row.get("city") or "").strip(),
					]
					if p
				),
				"lat": lat if is_geo else None,
				"lng": lng if is_geo else None,
				"geocoded": is_geo,
				"zone": zone_label or None,
				"zone_color": zone_color,
				"visit_days": visit_days,
				"frequency": cint(row.get("frequency")) if row.get("frequency") not in (None, "") else 1,
				"is_shipping": cint(row.get("is_shipping")) == 1,
			}
		)

	return {"clients": clients, "total": len(clients), "geocoded": geocoded}

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


def _address_latlng(address_name):
	"""Return {lat,lng} for an Address, or None if ungeocoded."""
	name = cstr(address_name or "").strip()
	if not name or not frappe.db.has_column("Address", "custom_latitude"):
		return None
	row = frappe.db.get_value(
		"Address", name, ["custom_latitude", "custom_longitude"], as_dict=True
	)
	if not row:
		return None
	lat = flt(row.custom_latitude)
	lng = flt(row.custom_longitude)
	if not lat and not lng:
		return None
	return {"lat": lat, "lng": lng}


def _trip_depot_latlng(trip):
	"""Warehouse pin / address used as TSP start+end (closed tour)."""
	# 1) Explicit pickup warehouse on trip — prefer map pin, then Address geo
	wh = cstr(getattr(trip, "custom_pickup_warehouse", None) or "").strip()
	if wh:
		pin_geo = _depot_from_warehouse_pin(wh)
		if pin_geo:
			return pin_geo, None, wh
		addr = _default_address("Warehouse", wh)
		geo = _address_latlng(addr) if addr else None
		if geo:
			return geo, addr, wh
	# 2) Trip driver_address (often the resolved warehouse address)
	addr = cstr(getattr(trip, "driver_address", None) or "").strip()
	geo = _address_latlng(addr) if addr else None
	if geo:
		return geo, addr, wh or None
	# 3) Company default warehouse pin / Address / BA fallback
	depot = _default_depot_latlng(getattr(trip, "company", None))
	return depot, None, wh or None


def _tsp_closed_tour_order(depot, points):
	"""Order ``points`` for a closed TSP: depot → … → depot (haversine).

	``points``: list of dicts with ``lat``/``lng``. Returns permutation of indices.
	Uses nearest-neighbor from the depot, then 2-opt until local optimum.
	"""
	n = len(points)
	if n <= 1:
		return list(range(n))

	dlat = flt(depot.get("lat"))
	dlng = flt(depot.get("lng"))
	coords = [(flt(p["lat"]), flt(p["lng"])) for p in points]

	def dist(i, j):
		a, b = coords[i], coords[j]
		return _haversine_km(a[0], a[1], b[0], b[1])

	def depot_to(i):
		a = coords[i]
		return _haversine_km(dlat, dlng, a[0], a[1])

	def tour_km(order):
		if not order:
			return 0.0
		total = depot_to(order[0])
		for a, b in zip(order, order[1:]):
			total += dist(a, b)
		total += depot_to(order[-1])
		return total

	# Nearest-neighbor seed starting from depot
	remaining = set(range(n))
	order = []
	cur = None
	while remaining:
		best = None
		best_d = None
		for i in remaining:
			d = depot_to(i) if cur is None else dist(cur, i)
			if best_d is None or d < best_d:
				best_d = d
				best = i
		order.append(best)
		remaining.remove(best)
		cur = best

	# 2-opt improvement on the open path (tour cost still includes return to depot)
	improved = True
	while improved:
		improved = False
		best_delta = 0.0
		best_i = best_j = None
		for i in range(n - 1):
			for j in range(i + 1, n):
				# Reverse order[i:j+1]
				cand = order[:i] + list(reversed(order[i : j + 1])) + order[j + 1 :]
				delta = tour_km(cand) - tour_km(order)
				if delta < best_delta - 1e-9:
					best_delta = delta
					best_i, best_j = i, j
					improved = True
		if improved and best_i is not None:
			order = order[:best_i] + list(reversed(order[best_i : best_j + 1])) + order[best_j + 1 :]

	return order


def _delivery_note_order_tsp(trip):
	"""Compute DN order for trip stops via warehouse-closed TSP.

	Visited stops keep their relative order at the front. Ungeocoded stops
	append after the optimized geocoded block (stable relative order).
	"""
	rows = sorted(trip.delivery_stops or [], key=lambda r: cint(r.idx) or 0)
	visited = []
	movable = []
	for s in rows:
		dn = cstr(getattr(s, "delivery_note", None) or "").strip()
		if not dn:
			continue
		if cint(getattr(s, "visited", 0)):
			visited.append(dn)
		else:
			movable.append(s)

	depot, _addr, _wh = _trip_depot_latlng(trip)

	# Resolve coords: stop lat/lng first, else address geo
	addr_names = [cstr(s.address or "").strip() for s in movable if s.address]
	geo_by = {}
	names = [a for a in addr_names if a]
	if names and frappe.db.has_column("Address", "custom_latitude"):
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", names]},
			fields=["name", "custom_latitude", "custom_longitude"],
			ignore_permissions=True,
		):
			lat = flt(row.custom_latitude)
			lng = flt(row.custom_longitude)
			if lat or lng:
				geo_by[row.name] = {"lat": lat, "lng": lng}

	geocoded = []
	ungeocoded_dns = []
	for s in movable:
		dn = cstr(s.delivery_note or "").strip()
		lat = _valid_map_coord(getattr(s, "lat", None))
		lng = _valid_map_coord(getattr(s, "lng", None))
		if lat is None or lng is None:
			g = geo_by.get(cstr(s.address or "").strip())
			if g:
				lat, lng = g["lat"], g["lng"]
		if lat is None or lng is None:
			ungeocoded_dns.append(dn)
			continue
		geocoded.append({"dn": dn, "lat": lat, "lng": lng})

	if len(geocoded) <= 1:
		ordered_geo = [g["dn"] for g in geocoded]
	else:
		perm = _tsp_closed_tour_order(depot, geocoded)
		ordered_geo = [geocoded[i]["dn"] for i in perm]

	return visited + ordered_geo + ungeocoded_dns


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


def _next_due_on_weekdays(as_of, weekdays, strictly_after=False):
	"""Next date on a weekday in ``weekdays`` (Mon..Sun).

	By default returns the soonest date >= ``as_of``.
	With ``strictly_after=True`` (auto-group reschedule), skips ``as_of`` itself
	so remitos move to a future delivery day after the plan/as-of date.
	"""
	from frappe.utils import add_days

	as_of = getdate(as_of)
	if not weekdays:
		return add_days(as_of, 1) if strictly_after else as_of
	wanted = set(weekdays)
	start = 1 if strictly_after else 0
	for i in range(start, start + 21):
		d = add_days(as_of, i)
		# Python: Mon=0 … Sun=6 → map to our labels
		label = _WEEKDAY_ORDER[d.weekday()]
		if label in wanted:
			return d
	return add_days(as_of, 1) if strictly_after else as_of


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
	delivery_notes=None,
	reschedule_after_as_of=1,
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

	# Optional allow-list (advance-select which remitos enter the cluster / reschedule set).
	# null / dirty / empty → all pending (no filter).
	note_filter = set()
	raw_notes = delivery_notes
	if isinstance(raw_notes, str):
		s = raw_notes.strip()
		if s and s.lower() not in ("null", "undefined", "none"):
			try:
				raw_notes = frappe.parse_json(s)
			except Exception:
				raw_notes = [x.strip() for x in s.split(",") if x.strip()]
	if isinstance(raw_notes, (list, tuple, set)):
		for n in raw_notes:
			name = cstr(n or "").strip()
			if name and name.lower() not in ("null", "undefined", "none"):
				note_filter.add(name)

	if reschedule_after_as_of is None:
		strict_future = True
	elif isinstance(reschedule_after_as_of, str) and reschedule_after_as_of.strip().lower() in (
		"",
		"null",
		"undefined",
		"none",
	):
		strict_future = True
	else:
		strict_future = cint(reschedule_after_as_of) != 0

	pending = get_pending_deliveries(date=str(as_of), company=company)
	deliveries = list(pending.get("deliveries") or [])
	if note_filter:
		deliveries = [d for d in deliveries if cstr(d.get("delivery_note") or "").strip() in note_filter]

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

	# Day sector from each cluster's centroid (same south-first compass as rebalance)
	# so neighboring geo groups prefer neighboring / same visit days.
	centroids = []
	for b in buckets:
		if not b:
			centroids.append((0.0, 0.0))
			continue
		centroids.append(
			(
				sum(flt(o.get("lat")) for o in b) / len(b),
				sum(flt(o.get("lng")) for o in b) / len(b),
			)
		)
	sector_of_cluster = _day_sector_labels(centroids, max(1, len(work_days))) if centroids else []

	# Drop empty clusters and reindex
	nonempty = [(i, b) for i, b in enumerate(buckets) if b]
	groups = []
	for gi, (old_i, orders) in enumerate(nonempty):
		code = f"AG{gi + 1}"
		color = _ZONE_DEFAULT_COLORS[gi % len(_ZONE_DEFAULT_COLORS)]
		driver = drivers[gi % len(drivers)] if drivers else None
		vehicle = vehicles[gi % len(vehicles)] if vehicles else None

		freqs = [
			freq_map.get(o.get("address_name"), 1) for o in orders if o.get("address_name")
		] or [1]
		zone_freq = max(freqs)
		# Prefer the geographic day sector of this cluster, then space extras.
		sector_idx = sector_of_cluster[old_i] if old_i < len(sector_of_cluster) else gi
		sector_day = work_days[sector_idx % len(work_days)] if work_days else "Mon"
		rotated = list(work_days)
		if sector_day in rotated:
			rotated = rotated[rotated.index(sector_day) :] + rotated[: rotated.index(sector_day)]
		zone_days = _spaced_weekdays(rotated, zone_freq, offset=0)

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
			due = _next_due_on_weekdays(as_of, client_days, strictly_after=strict_future)
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
	delivery_notes=None,
	reschedule_after_as_of=1,
):
	"""Preview geo clusters → proposed zones + due dates.

	No DB writes by default. Pass ``persist_settings=1`` only when intentionally
	saving calendar prefs without committing zones.

	``delivery_notes`` (optional): limit preview to those remitos.
	``reschedule_after_as_of`` (default on): proposed dues are strictly after as-of.
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
		delivery_notes=delivery_notes,
		reschedule_after_as_of=reschedule_after_as_of,
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
						_apply_pending_delivery_due(dn, due_d)
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
# i044 — Zone rebalance from active geocoded clients (not day remitos)
# ---------------------------------------------------------------------------


def _truthy_flag(raw, default=False):
	"""Coerce dirty whitelist flags (null/''/'null') to bool without crashing."""
	if raw is None:
		return default
	if isinstance(raw, str) and raw.strip().lower() in ("", "null", "undefined", "none"):
		return default
	if isinstance(raw, str):
		return raw.strip().lower() in ("1", "true", "yes")
	return bool(cint(raw)) if not isinstance(raw, bool) else raw


def _active_geocoded_client_rows(limit=5000, only_missing_zone=0):
	"""Active Customer + Address rows with lat/lng (shared by Clients tab / rebalance)."""
	payload = list_delivery_clients(limit=limit, only_geocoded=1)
	clients = list(payload.get("clients") or [])
	ungeocoded_total = 0
	# Optional count of active addresses missing geo (informational only).
	try:
		ungeocoded_total = cint(
			frappe.db.sql(
				"""
				SELECT COUNT(*)
				FROM `tabCustomer` c
				INNER JOIN `tabDynamic Link` dl
					ON dl.link_doctype = 'Customer'
					AND dl.link_name = c.name
					AND dl.parenttype = 'Address'
				INNER JOIN `tabAddress` addr ON addr.name = dl.parent
				WHERE IFNULL(c.disabled, 0) = 0
					AND IFNULL(addr.disabled, 0) = 0
					AND (
						IFNULL(addr.custom_latitude, 0) = 0
						OR IFNULL(addr.custom_longitude, 0) = 0
					)
				"""
			)[0][0]
		)
	except Exception:
		ungeocoded_total = 0

	if _truthy_flag(only_missing_zone, False):
		clients = [c for c in clients if not cstr(c.get("zone") or "").strip()]

	# Deduplicate by address_name (primary unit for custom_zone writes).
	seen = set()
	unique = []
	for c in clients:
		addr = cstr(c.get("address_name") or c.get("id") or "").strip()
		if not addr or addr in seen:
			continue
		seen.add(addr)
		unique.append(c)
	return unique, ungeocoded_total


def _parse_zone_cap(raw):
	"""Dirty ``max_clients_per_zone`` → int (0 = auto / tight balance)."""
	if raw is None:
		return 0
	if isinstance(raw, str) and raw.strip().lower() in ("", "null", "undefined", "none"):
		return 0
	try:
		return max(0, cint(raw))
	except (TypeError, ValueError):
		return 0


def _plane_coords(coords):
	"""(lat, lng) → local planar km (equirectangular) so squared distance ≈ real geometry."""
	if not coords:
		return []
	lat0 = sum(p[0] for p in coords) / len(coords)
	kx = 111.32 * math.cos(math.radians(lat0))
	ky = 110.57
	return [(p[1] * kx, p[0] * ky) for p in coords]


def _sq_dist(a, b):
	return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _cluster_centroids(pts, labels, k, fallback):
	sums = [[0.0, 0.0, 0] for _ in range(k)]
	for p, lab in zip(pts, labels):
		sums[lab][0] += p[0]
		sums[lab][1] += p[1]
		sums[lab][2] += 1
	return [
		(sx / cnt, sy / cnt) if cnt else fallback[ci] for ci, (sx, sy, cnt) in enumerate(sums)
	]


def _capacity_assign(pts, cents, cap):
	"""Assign every point to its nearest centroid with room left (highest regret first)."""
	k = len(cents)
	dist = [[_sq_dist(p, c) for c in cents] for p in pts]
	prefs = [sorted(range(k), key=lambda c, row=row: row[c]) for row in dist]
	order = sorted(
		range(len(pts)),
		key=lambda i: -(dist[i][prefs[i][1]] - dist[i][prefs[i][0]]) if k > 1 else 0,
	)
	size = [0] * k
	labels = [0] * len(pts)
	for i in order:
		for ci in prefs[i]:
			if size[ci] < cap:
				labels[i] = ci
				size[ci] += 1
				break
	return labels, dist


def _swap_improve(labels, dist, k, cap, passes=4):
	"""Local search that keeps every cluster ≤ cap: moves into spare room + pairwise swaps."""
	n = len(labels)
	size = [0] * k
	for lab in labels:
		size[lab] += 1
	for _ in range(passes):
		improved = False
		for i in range(n):
			a = labels[i]
			best = a
			for b in range(k):
				if b != a and size[b] < cap and dist[i][b] < dist[i][best]:
					best = b
			if best != a:
				labels[i] = best
				size[a] -= 1
				size[best] += 1
				improved = True
		for a in range(k):
			for b in range(a + 1, k):
				a_to_b = sorted(
					(
						(dist[i][a] - dist[i][b], i)
						for i in range(n)
						if labels[i] == a and dist[i][b] < dist[i][a]
					),
					reverse=True,
				)
				if not a_to_b:
					continue
				b_to_a = sorted(
					(
						(dist[j][b] - dist[j][a], j)
						for j in range(n)
						if labels[j] == b and dist[j][a] < dist[j][b]
					),
					reverse=True,
				)
				for (_ga, i), (_gb, j) in zip(a_to_b, b_to_a):
					labels[i] = b
					labels[j] = a
					improved = True
		if not improved:
			break
	return labels


def _balanced_partition(pts, k, cap=0, iters=20):
	"""Capacity-constrained k-means on planar points.

	Every cluster ends with ≤ ``cap`` members (cap is raised to ceil(n/k) so it is
	always feasible), which keeps headcounts even while clusters stay compact.
	Deterministic: farthest-point seeding, no randomness.
	"""
	n = len(pts)
	if n == 0:
		return []
	k = max(1, min(cint(k) or 1, n))
	if k == 1:
		return [0] * n
	cap = max(cint(cap) or 0, int(math.ceil(n / float(k))))

	mean = (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
	first = max(range(n), key=lambda i: _sq_dist(pts[i], mean))
	cents = [pts[first]]
	mind = [_sq_dist(p, cents[0]) for p in pts]
	while len(cents) < k:
		nxt = max(range(n), key=lambda j: mind[j])
		cents.append(pts[nxt])
		for j in range(n):
			d = _sq_dist(pts[j], pts[nxt])
			if d < mind[j]:
				mind[j] = d

	labels = None
	for _ in range(iters):
		new_labels, dist = _capacity_assign(pts, cents, cap)
		new_labels = _swap_improve(new_labels, dist, k, cap)
		cents = _cluster_centroids(pts, new_labels, k, cents)
		if new_labels == labels:
			break
		labels = new_labels
	return labels


def _match_by_overlap(members_keys, available_keys):
	"""Greedy max-overlap match cluster → previous key (keeps codes / days stable).

	``members_keys``: per cluster, list of previous keys of its members.
	Returns {cluster_index: key} for matched clusters only.
	"""
	avail = set(k for k in available_keys if k)
	pairs = []
	for ci, keys in enumerate(members_keys):
		counts = {}
		for key in keys:
			if key in avail:
				counts[key] = counts.get(key, 0) + 1
		for key, cnt in counts.items():
			pairs.append((cnt, ci, key))
	pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
	out = {}
	used = set()
	for _cnt, ci, key in pairs:
		if ci in out or key in used:
			continue
		out[ci] = key
		used.add(key)
	return out


_TERRITORY_ZONE_RE = None


def _split_territory_zone_code(code):
	"""'T3-WED' → ('T3', 'Wed'); anything else → (None, None)."""
	import re

	global _TERRITORY_ZONE_RE
	if _TERRITORY_ZONE_RE is None:
		_TERRITORY_ZONE_RE = re.compile(r"^(T\d+)-([A-Z]{3})$")
	m = _TERRITORY_ZONE_RE.match(cstr(code or "").strip().upper())
	if not m:
		return None, None
	day = m.group(2).title()
	return m.group(1), (day if day in _WEEKDAY_ORDER else None)


def _territory_color(ti, di=None, days=1):
	"""Territory = hue (golden angle), day = lightness step — 30 zones stay readable."""
	import colorsys

	hue = ((ti * 137.508) + 230.0) % 360.0
	if di is None:
		light = 0.45
	else:
		light = 0.34 + (0.30 * di / float(max(1, days - 1)))
	r, g, b = colorsys.hls_to_rgb(hue / 360.0, light, 0.62)
	return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def _zone_member_stats(clients, zones):
	"""Per delivery zone: members count + centroid (only zones that have members)."""
	by_zone = {}
	for c in clients:
		key = cstr(c.get("zone") or "").strip().upper()
		if key:
			by_zone.setdefault(key, []).append(c)
	stats = []
	for z in zones:
		if not isinstance(z, dict) or (z.get("type") or "delivery") != "delivery":
			continue
		code = cstr(z.get("code") or "").strip().upper()
		if not code:
			continue
		members = by_zone.get(code) or by_zone.get(cstr(z.get("name") or "").strip().upper()) or []
		if not members:
			continue
		stats.append(
			{
				"code": code,
				"zone": z,
				"count": len(members),
				"lat": sum(flt(m["lat"]) for m in members) / len(members),
				"lng": sum(flt(m["lng"]) for m in members) / len(members),
			}
		)
	if not stats:
		# Fresh site: zones exist but nobody tagged yet → BA sector centers.
		for z in zones:
			if not isinstance(z, dict) or (z.get("type") or "delivery") != "delivery":
				continue
			code = cstr(z.get("code") or "").strip().upper()
			pocket = _ZONE_BA_CENTERS.get(code)
			if pocket:
				stats.append({"code": code, "zone": z, "count": 0, "lat": pocket[0], "lng": pocket[1]})
	return stats


def _incremental_cap(total_clients, zone_count, cap=0):
	"""Soft cap for flow B: explicit cap, else ceil(N/zones) + 15% slack."""
	if cap:
		return cap
	ideal = int(math.ceil(total_clients / float(max(1, zone_count))))
	return ideal + max(1, int(math.ceil(ideal * 0.15)))


def _pick_zone_for_point(lat, lng, stats, cap):
	"""Nearest zone under cap; else nearest (overflow). Mutates chosen count."""
	ranked = sorted(stats, key=lambda s: _haversine_km(lat, lng, s["lat"], s["lng"]))
	for s in ranked:
		if s["count"] < cap:
			s["count"] += 1
			return s, False
	ranked[0]["count"] += 1
	return ranked[0], True


def _build_assign_missing_preview(clients, zones, cap_raw, warnings, ungeocoded_count, work_days):
	"""only_missing_zone: incremental assign of unzoned clients into existing zones."""
	missing = [c for c in clients if not cstr(c.get("zone") or "").strip()]
	stats = _zone_member_stats(clients, zones)
	base = {
		"strategy": "territory_days",
		"mode": "assign_missing",
		"working_days": work_days,
		"territories_k": 0,
		"k": 0,
		"clients_total": len(missing),
		"moved_total": 0,
		"territories": [],
		"groups": [],
		"stale_zones": [],
		"ungeocoded_count": ungeocoded_count,
		"only_missing_zone": True,
	}
	if not missing:
		return {**base, "ideal_per_zone": 0, "max_clients_per_zone": 0, "warnings": warnings + ["no_missing"]}
	if not stats:
		return {**base, "ideal_per_zone": 0, "max_clients_per_zone": 0, "warnings": warnings + ["no_zones"]}

	cap = _incremental_cap(len(clients), len(stats), cap_raw)
	added = {}
	overflow_any = False
	for c in missing:
		s, over = _pick_zone_for_point(flt(c["lat"]), flt(c["lng"]), stats, cap)
		overflow_any = overflow_any or over
		added.setdefault(s["code"], []).append(c)

	groups = []
	for s in stats:
		rows = added.get(s["code"])
		if not rows:
			continue
		z = s["zone"]
		groups.append(
			{
				"code": s["code"],
				"name": z.get("name") or s["code"],
				"color": z.get("color"),
				"type": "delivery",
				"vehicles": list(z.get("vehicles") or []),
				"visit_days": list(z.get("visit_days") or []),
				"client_count": s["count"],
				"added": len(rows),
				"moved": len(rows),
				"overflow": max(0, s["count"] - cap),
				"centroid": {"lat": s["lat"], "lng": s["lng"]},
				"sample_clients": [r.get("customer_name") or r.get("customer") for r in rows[:5]],
				"clients": [
					{
						"address_name": r.get("address_name"),
						"customer": r.get("customer"),
						"customer_name": r.get("customer_name"),
						"lat": flt(r.get("lat")),
						"lng": flt(r.get("lng")),
						"previous_zone": None,
					}
					for r in rows
				],
				"keep_zone": True,
			}
		)
	return {
		**base,
		"k": len(groups),
		"moved_total": len(missing),
		"ideal_per_zone": int(math.ceil(len(clients) / float(len(stats)))),
		"max_clients_per_zone": cap,
		"groups": groups,
		"warnings": warnings + (["soft_max_overflow"] if overflow_any else []),
	}


def _day_sector_labels(latlng, n_days):
	"""Assign each (lat, lng) to a weekday sector as a contiguous arc on the city circle.

	Sectors are equal-sized by client count (balanced), ordered clockwise starting from
	**south** so Mon ≈ south, next day ≈ southwest/west, … around the map. Same-day
	zones therefore sit next to each other geographically.
	"""
	n = len(latlng)
	if n == 0:
		return []
	n_days = max(1, min(cint(n_days) or 1, n))
	if n_days == 1:
		return [0] * n
	clat = sum(p[0] for p in latlng) / n
	clng = sum(p[1] for p in latlng) / n
	# atan2(Δlng, -Δlat): 0 = south, increasing toward west/east (clockwise from south).
	indexed = []
	for i, (lat, lng) in enumerate(latlng):
		ang = math.atan2(lng - clng, -(lat - clat)) % (2 * math.pi)
		indexed.append((ang, i))
	indexed.sort(key=lambda x: x[0])
	labels = [0] * n
	base = n // n_days
	rem = n % n_days
	pos = 0
	for d in range(n_days):
		size = base + (1 if d < rem else 0)
		for j in range(size):
			labels[indexed[pos + j][1]] = d
		pos += size
	return labels


def _build_rebalance_zones_preview(
	k=None,
	max_clients_per_zone=None,
	working_days=None,
	only_missing_zone=0,
	driver_names=None,
	vehicle_names=None,
):
	"""Day-sector × driver zones from the active client book.

	1. Slice the city into contiguous weekday arcs (south → Mon, then clockwise)
	   so all Monday zones sit near each other, Tuesday next door, etc.
	2. Within each day arc, balanced-partition into ``k`` territories (drivers).
	3. Emit ``T{i}-{DAY}`` zones with ``visit_days=[day]``.
	"""
	settings = _load_tms_settings()
	work_days = _normalize_working_days(
		working_days if working_days is not None else settings.get("auto_group_working_days")
	)
	work_days = sorted(work_days, key=lambda d: _WEEKDAY_ORDER.index(d) if d in _WEEKDAY_ORDER else 99)
	drivers = _parse_json_list(driver_names, [])
	vehicles_in = _parse_json_list(vehicle_names, [])
	cap_raw = _parse_zone_cap(max_clients_per_zone)
	warnings = []

	clients, ungeocoded_count = _active_geocoded_client_rows(limit=5000, only_missing_zone=0)
	if not drivers:
		drivers = [
			d.name
			for d in frappe.get_all(
				"Driver",
				filters={"status": "Active"},
				fields=["name"],
				order_by="name asc",
				ignore_permissions=True,
			)
		]
	if not drivers:
		warnings.append("no_drivers")
	if ungeocoded_count:
		warnings.append("ungeocoded_skipped")

	existing_zones = _load_tms_zones().get("zones") or []
	if _truthy_flag(only_missing_zone, False):
		return _build_assign_missing_preview(
			clients, existing_zones, cap_raw, warnings, ungeocoded_count, work_days
		)

	if not clients:
		return {
			"strategy": "day_sectors",
			"mode": "rebalance",
			"territories_k": 0,
			"k": 0,
			"working_days": work_days,
			"ideal_per_zone": 0,
			"max_clients_per_zone": cap_raw,
			"clients_total": 0,
			"moved_total": 0,
			"territories": [],
			"groups": [],
			"stale_zones": [],
			"ungeocoded_count": ungeocoded_count,
			"warnings": warnings + ["no_geocoded"],
			"only_missing_zone": False,
		}

	n = len(clients)
	n_days = max(1, len(work_days))
	t_eff = cint(k) if k not in (None, "", "null", "undefined") else (len(drivers) or 1)
	t_eff = max(1, min(t_eff, n))
	zones_total = t_eff * n_days
	ideal = int(math.ceil(n / float(zones_total)))
	if cap_raw and cap_raw < ideal:
		warnings.append("cap_infeasible")

	latlng = [(flt(c["lat"]), flt(c["lng"])) for c in clients]
	pts = _plane_coords(latlng)
	prev = [_split_territory_zone_code(c.get("zone")) for c in clients]

	# Level 1 — city-wide weekday sectors (contiguous arcs, south-first).
	# Sector i → work_days[i] always (no sticky day remap — that would scatter
	# "Monday" around the map when legacy zones were north-heavy).
	day_labels = _day_sector_labels(latlng, n_days)
	day_members = [[] for _ in range(n_days)]
	for i, dlab in enumerate(day_labels):
		day_members[dlab].append(i)
	day_match = {di: work_days[di] for di in range(n_days)}

	existing_by_code = {
		cstr(z.get("code") or "").strip().upper(): z for z in existing_zones if isinstance(z, dict)
	}

	# Collect per-(territory index, day) member lists; assign T-codes globally by sticky.
	# day_territory_members[day_i][ti] = client indices
	day_territory_members = []
	for di in range(n_days):
		members = day_members[di]
		if not members:
			day_territory_members.append([[] for _ in range(t_eff)])
			continue
		sub_pts = [pts[i] for i in members]
		# Cap per day-zone; soft.
		t_labels = _balanced_partition(sub_pts, min(t_eff, len(members)), cap=cap_raw or 0)
		buckets = [[] for _ in range(t_eff)]
		for local, lab in enumerate(t_labels):
			buckets[lab].append(members[local])
		day_territory_members.append(buckets)

	# Territory codes: sticky from previous T* on any day, then T1..Tk.
	flat_prev_t = []
	for ti in range(t_eff):
		keys = []
		for di in range(n_days):
			for i in day_territory_members[di][ti]:
				if prev[i][0]:
					keys.append(prev[i][0])
		flat_prev_t.append(keys)
	t_prev_keys = sorted(
		{p[0] for p in prev if p[0]},
		key=lambda x: cint(x[1:]) if x and x[1:].isdigit() else 99,
	)
	t_match = _match_by_overlap(flat_prev_t, t_prev_keys)
	used_codes = set(t_match.values())
	next_no = 1
	t_codes = []
	for ti in range(t_eff):
		code = t_match.get(ti)
		if not code:
			while f"T{next_no}" in used_codes:
				next_no += 1
			code = f"T{next_no}"
			used_codes.add(code)
		t_codes.append(code)
	t_order = sorted(range(t_eff), key=lambda ti: cint(t_codes[ti][1:]))

	# Vehicles per territory from existing zones / input list.
	t_vehicles_by_code = {}
	for z in existing_zones:
		if not isinstance(z, dict):
			continue
		zt, _zd = _split_territory_zone_code(z.get("code"))
		if zt and z.get("vehicles") and zt not in t_vehicles_by_code:
			t_vehicles_by_code[zt] = list(z.get("vehicles"))

	groups = []
	territories_map = {
		t_codes[ti]: {
			"code": t_codes[ti],
			"name": t_codes[ti],
			"color": _territory_color(cint(t_codes[ti][1:]) - 1),
			"driver": None,
			"vehicles": t_vehicles_by_code.get(t_codes[ti])
			or (
				[vehicles_in[pos]]
				if pos < len(vehicles_in) and vehicles_in[pos]
				else []
			),
			"client_count": 0,
			"centroid_lat_sum": 0.0,
			"centroid_lng_sum": 0.0,
			"zones": [],
		}
		for pos, ti in enumerate(t_order)
	}
	# Bind drivers in T1.. order.
	for pos, ti in enumerate(t_order):
		territories_map[t_codes[ti]]["driver"] = drivers[pos] if pos < len(drivers) else None
		if not territories_map[t_codes[ti]]["vehicles"] and pos < len(vehicles_in) and vehicles_in[pos]:
			territories_map[t_codes[ti]]["vehicles"] = [vehicles_in[pos]]

	moved_total = 0
	overflow_any = False
	# Emit groups day-by-day so same-day zones stay adjacent in the UI list too.
	for di in range(n_days):
		day = day_match[di]
		day_idx = work_days.index(day) if day in work_days else di
		for pos, ti in enumerate(t_order):
			m = day_territory_members[di][ti]
			if not m:
				continue
			t_code = t_codes[ti]
			t_no = cint(t_code[1:])
			code = f"{t_code}-{day.upper()}"
			rows = []
			sample = []
			moved = 0
			for i in m:
				c = clients[i]
				prev_zone = cstr(c.get("zone") or "").strip().upper() or None
				if prev_zone != code:
					moved += 1
				cname = c.get("customer_name") or c.get("customer") or ""
				if cname and cname not in sample and len(sample) < 5:
					sample.append(cname)
				rows.append(
					{
						"address_name": c.get("address_name"),
						"customer": c.get("customer"),
						"customer_name": c.get("customer_name"),
						"lat": latlng[i][0],
						"lng": latlng[i][1],
						"previous_zone": prev_zone,
					}
				)
			moved_total += moved
			count = len(m)
			overflow = max(0, count - cap_raw) if cap_raw else 0
			overflow_any = overflow_any or bool(overflow)
			clat = sum(latlng[i][0] for i in m) / count
			clng = sum(latlng[i][1] for i in m) / count
			t_entry = territories_map[t_code]
			t_entry["client_count"] += count
			t_entry["centroid_lat_sum"] += clat * count
			t_entry["centroid_lng_sum"] += clng * count
			t_entry["zones"].append(code)
			groups.append(
				{
					"code": code,
					"name": f"{t_code} · {_WEEKDAY_SHORT.get(day, day)}",
					"color": _territory_color(t_no - 1, day_idx, n_days),
					"type": "delivery",
					"territory": t_code,
					"day": day,
					"driver": t_entry["driver"],
					"vehicles": list(
						(existing_by_code.get(code) or {}).get("vehicles") or t_entry["vehicles"]
					),
					"visit_days": [day],
					"client_count": count,
					"ideal_per_zone": ideal,
					"max_clients_per_zone": cap_raw or ideal,
					"overflow": overflow,
					"under": max(0, ideal - count),
					"moved": moved,
					"centroid": {"lat": clat, "lng": clng},
					"sample_clients": sample,
					"clients": rows,
					"notes": "rebalance-clients",
				}
			)

	territories = []
	for pos, ti in enumerate(t_order):
		t_code = t_codes[ti]
		e = territories_map[t_code]
		cc = e["client_count"] or 1
		territories.append(
			{
				"code": t_code,
				"name": t_code,
				"color": e["color"],
				"driver": e["driver"],
				"vehicles": e["vehicles"],
				"client_count": e["client_count"],
				"centroid": {
					"lat": e["centroid_lat_sum"] / cc,
					"lng": e["centroid_lng_sum"] / cc,
				},
				"zones": e["zones"],
			}
		)

	if overflow_any:
		warnings.append("soft_max_overflow")
	new_codes = {g["code"] for g in groups}
	stale = [
		{"code": cstr(z.get("code") or "").strip().upper(), "name": z.get("name")}
		for z in existing_zones
		if isinstance(z, dict)
		and (z.get("type") or "delivery") == "delivery"
		and cstr(z.get("code") or "").strip().upper() not in new_codes
	]

	return {
		"strategy": "day_sectors",
		"mode": "rebalance",
		"territories_k": len(territories),
		"k": len(groups),
		"working_days": work_days,
		"ideal_per_zone": ideal,
		"max_clients_per_zone": cap_raw or ideal,
		"clients_total": n,
		"moved_total": moved_total,
		"territories": territories,
		"groups": groups,
		"stale_zones": stale,
		"ungeocoded_count": ungeocoded_count,
		"warnings": warnings,
		"only_missing_zone": False,
	}


@frappe.whitelist(allow_guest=True)
def preview_rebalance_zones(
	k=None,
	max_clients_per_zone=None,
	working_days=None,
	only_missing_zone=0,
	driver_names=None,
	vehicle_names=None,
):
	"""Preview day-sector × driver zones from the active client book.

	Working days are contiguous geographic arcs (south → Mon, then clockwise),
	then each arc is split into ``k`` territories (defaults to active drivers).
	``only_missing_zone=1`` instead assigns unzoned clients into existing zones.
	Does not write DB.
	"""
	return _build_rebalance_zones_preview(
		k=k,
		max_clients_per_zone=max_clients_per_zone,
		working_days=working_days,
		only_missing_zone=only_missing_zone,
		driver_names=driver_names,
		vehicle_names=vehicle_names,
	)


@frappe.whitelist(allow_guest=True)
def commit_rebalance_zones(preview=None, refresh_pending_dues=0, remove_stale_zones=0):
	"""Apply client-book rebalance: upsert zones + Address.custom_zone.

	``remove_stale_zones=1`` drops delivery zones listed in ``preview.stale_zones``
	that no client points to after the commit. Forced delivery dates (i043) are
	never touched; ``refresh_pending_dues`` is reserved / no-op in v1.
	"""
	if isinstance(preview, str):
		s = preview.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			preview = None
		else:
			try:
				preview = frappe.parse_json(s)
			except Exception:
				preview = None
	if not isinstance(preview, dict):
		frappe.throw(_("Preview payload is required."))

	groups = preview.get("groups")
	if not isinstance(groups, list) or not groups:
		frappe.throw(_("Preview has no groups to commit."))

	if not frappe.db.has_column("Address", "custom_zone"):
		frappe.throw(_("Address.custom_zone is not available on this site."))

	zones_written = []
	addresses_updated = 0
	errors = []
	is_rebalance = preview.get("mode") != "assign_missing"

	if is_rebalance and preview.get("working_days"):
		_persist_tms_settings(
			{"auto_group_working_days": preview.get("working_days")},
			commit=False,
		)

	zones = list((_load_tms_zones().get("zones") or []))

	for g in groups:
		if not isinstance(g, dict):
			continue
		zone_code = cstr(g.get("code") or "").strip().upper()
		if not zone_code:
			continue
		if not g.get("keep_zone"):
			zone_payload = {
				"code": zone_code,
				"name": g.get("name"),
				"color": g.get("color"),
				"type": g.get("type") or "delivery",
				"visit_days": g.get("visit_days") or [],
				"vehicles": g.get("vehicles") or [],
				"driver": g.get("driver"),
				"notes": g.get("notes") or "rebalance-clients",
			}
			try:
				_upsert_zone_into_list(zones, zone_payload)
				zones_written.append(zone_code)
			except Exception as exc:
				errors.append({"zone": zone_code, "error": str(exc)})
				continue
		elif zone_code not in {cstr(z.get("code") or "").upper() for z in zones}:
			errors.append({"zone": zone_code, "error": "zone no longer exists"})
			continue

		for c in g.get("clients") or []:
			if not isinstance(c, dict):
				continue
			addr = cstr(c.get("address_name") or "").strip()
			if not addr or not frappe.db.exists("Address", addr):
				continue
			try:
				frappe.db.set_value("Address", addr, "custom_zone", zone_code, update_modified=True)
				addresses_updated += 1
			except Exception as exc:
				errors.append({"address": addr, "error": str(exc)})

	zones_removed = []
	if is_rebalance and _truthy_flag(remove_stale_zones, False):
		stale_codes = {
			cstr(s.get("code") if isinstance(s, dict) else s).strip().upper()
			for s in (preview.get("stale_zones") or [])
		}
		stale_codes.discard("")
		if stale_codes:
			still_used = {
				cstr(r[0] or "").strip().upper()
				for r in frappe.db.sql(
					"SELECT DISTINCT custom_zone FROM `tabAddress` WHERE IFNULL(custom_zone, '') != ''"
				)
			}
			keep = []
			for z in zones:
				code = cstr(z.get("code") or "").strip().upper()
				name = cstr(z.get("name") or "").strip().upper()
				if code in stale_codes and code not in still_used and name not in still_used:
					zones_removed.append(code)
				else:
					keep.append(z)
			zones = keep

	_save_tms_zones(zones, commit=False)
	frappe.db.commit()
	return {
		"ok": not bool(errors),
		"zones_written": zones_written,
		"zones_removed": zones_removed,
		"addresses_updated": addresses_updated,
		"errors": errors,
		"zones": zones,
	}


def _assign_zone_to_address(addr_name, max_clients_per_zone=None):
	"""Flow B core: nearest under-cap zone for one geocoded address. Returns dict or throws."""
	if not frappe.db.has_column("Address", "custom_latitude"):
		frappe.throw(_("Address geocode fields are not available."))
	lat = flt(frappe.db.get_value("Address", addr_name, "custom_latitude"))
	lng = flt(frappe.db.get_value("Address", addr_name, "custom_longitude"))
	if not lat or not lng:
		frappe.throw(_("Address must be geocoded before zone assign."))

	zones = _load_tms_zones().get("zones") or []
	clients, _unused = _active_geocoded_client_rows(limit=5000, only_missing_zone=0)
	# Exclude this address so re-assign doesn't count itself.
	clients = [c for c in clients if c.get("address_name") != addr_name]
	stats = _zone_member_stats(clients, zones)
	if not stats:
		frappe.throw(_("No delivery zones defined — run Rebalance first."))

	cap = _incremental_cap(len(clients) + 1, len(stats), _parse_zone_cap(max_clients_per_zone))
	chosen, overflow = _pick_zone_for_point(lat, lng, stats, cap)
	frappe.db.set_value("Address", addr_name, "custom_zone", chosen["code"], update_modified=True)
	return {
		"address": addr_name,
		"zone": chosen["code"],
		"visit_days": list(chosen["zone"].get("visit_days") or []),
		"overflow": overflow,
		"max_clients_per_zone": cap,
		"zone_client_count": chosen["count"],
	}


def _auto_assign_zone_if_missing(addr_name):
	"""Best-effort flow B hook (geocode / CSV import). Never raises."""
	try:
		if not addr_name or not frappe.db.has_column("Address", "custom_zone"):
			return None
		if cstr(frappe.db.get_value("Address", addr_name, "custom_zone") or "").strip():
			return None
		return _assign_zone_to_address(addr_name)
	except Exception:
		return None


@frappe.whitelist(allow_guest=True)
def assign_zone_for_client(customer=None, address=None, max_clients_per_zone=None):
	"""Incremental assign (flow B): nearest under-cap zone for a new geocoded client."""
	addr_name = cstr(address or "").strip()
	cust = cstr(customer or "").strip()
	if addr_name.lower() in ("null", "undefined", "none"):
		addr_name = ""
	if cust.lower() in ("null", "undefined", "none"):
		cust = ""
	if not addr_name and cust:
		rows = frappe.db.sql(
			"""
			SELECT addr.name
			FROM `tabAddress` addr
			INNER JOIN `tabDynamic Link` dl
				ON dl.parent = addr.name AND dl.parenttype = 'Address'
			WHERE dl.link_doctype = 'Customer' AND dl.link_name = %(c)s
				AND IFNULL(addr.disabled, 0) = 0
			ORDER BY IFNULL(addr.is_shipping_address, 0) DESC, addr.modified DESC
			LIMIT 1
			""",
			{"c": cust},
			as_dict=True,
		)
		addr_name = rows[0].name if rows else ""
	if not addr_name or not frappe.db.exists("Address", addr_name):
		frappe.throw(_("Address is required."))

	out = _assign_zone_to_address(addr_name, max_clients_per_zone)
	frappe.db.commit()
	return out


# ---------------------------------------------------------------------------
# i045 — Fast-deliver greedy packing (due dates under soft caps)
# ---------------------------------------------------------------------------


def _packing_strategy(settings=None):
	settings = settings or _load_tms_settings()
	s = cstr(settings.get("packing_strategy") or "client_zone").strip().lower()
	if s not in ("client_zone", "fast_deliver_greedy"):
		return "client_zone"
	return s


def _drive_minutes(lat1, lng1, lat2, lng2, avg_speed_kmh, drive_buffer=0):
	speed = flt(avg_speed_kmh) or 25.0
	if speed <= 0:
		speed = 25.0
	km = _haversine_km(flt(lat1), flt(lng1), flt(lat2), flt(lng2))
	return (km / speed) * 60.0 + max(0.0, flt(drive_buffer))


def _stop_geo_key(stop):
	"""Collapse same physical address for time (Q3)."""
	addr = cstr(stop.get("address_name") or "").strip()
	if addr:
		return f"a:{addr}"
	try:
		lat = flt(stop.get("lat"))
		lng = flt(stop.get("lng"))
	except Exception:
		return None
	if not lat or not lng:
		return None
	return f"g:{round(lat, 5)}:{round(lng, 5)}"


def _unique_stop_points(stops):
	"""One lat/lng per unique address key (order of first appearance)."""
	seen = set()
	pts = []
	for s in stops or []:
		key = _stop_geo_key(s)
		if not key or key in seen:
			continue
		try:
			lat = flt(s.get("lat"))
			lng = flt(s.get("lng"))
		except Exception:
			continue
		if not lat or not lng:
			continue
		seen.add(key)
		pts.append({"lat": lat, "lng": lng, "key": key})
	return pts


def _route_minutes(stops, depot, stop_minutes, avg_speed_kmh, drive_buffer=0):
	"""NN tour minutes: unique addresses × dwell + drive(+buffer) per leg (Q3, Q14)."""
	dwell = flt(stop_minutes) or 15.0
	pts = _unique_stop_points(stops)
	if not pts:
		return 0.0
	total = dwell * len(pts)
	cur = {"lat": flt(depot.get("lat")), "lng": flt(depot.get("lng"))}
	remaining = list(pts)
	while remaining:
		best_i = 0
		best_d = None
		for i, p in enumerate(remaining):
			d = _drive_minutes(cur["lat"], cur["lng"], p["lat"], p["lng"], avg_speed_kmh, drive_buffer)
			if best_d is None or d < best_d:
				best_d = d
				best_i = i
		total += best_d or 0.0
		cur = remaining.pop(best_i)
	return total


def _order_count(stops):
	"""Remito count (not unique addresses) — Q3."""
	return len(stops or [])


def _fits_soft_cap(stops, depot, max_orders, max_minutes, stop_minutes, avg_speed, drive_buffer=0):
	cnt = _order_count(stops)
	if cnt > max_orders:
		return False, cnt, _route_minutes(stops, depot, stop_minutes, avg_speed, drive_buffer)
	mins = _route_minutes(stops, depot, stop_minutes, avg_speed, drive_buffer)
	ok = cnt <= max_orders and mins <= max_minutes
	return ok, cnt, mins


def _limiting_resource(order_count, max_orders, minutes, max_minutes):
	"""Which soft cap is closer to binding (Q15)."""
	ord_ratio = (flt(order_count) / flt(max_orders)) if max_orders else 0
	min_ratio = (flt(minutes) / flt(max_minutes)) if max_minutes else 0
	if min_ratio >= ord_ratio:
		return "time"
	return "orders"


def _weekday_label_for_date(d):
	d = getdate(d)
	return _WEEKDAY_ORDER[d.weekday()]


def _parse_receive_days(raw):
	"""Normalize receive_days list; empty = any working day (Q4)."""
	if raw is None:
		return []
	if isinstance(raw, str):
		s = raw.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			return []
		try:
			raw = frappe.parse_json(s)
		except Exception:
			raw = [x.strip() for x in s.split(",") if x.strip()]
	if not isinstance(raw, (list, tuple, set)):
		return []
	out = []
	for d in raw:
		label = cstr(d or "").strip()
		if not label:
			continue
		# Accept Mon / Mon(1 Man)
		base = label.split("(", 1)[0].strip().title()[:3]
		aliases = {
			"Mon": "Mon",
			"Tue": "Tue",
			"Wed": "Wed",
			"Thu": "Thu",
			"Fri": "Fri",
			"Sat": "Sat",
			"Sun": "Sun",
			"Lun": "Mon",
			"Mar": "Tue",
			"Mie": "Wed",
			"Mié": "Wed",
			"Jue": "Thu",
			"Vie": "Fri",
			"Sab": "Sat",
			"Sáb": "Sat",
			"Dom": "Sun",
		}
		canon = aliases.get(base) or aliases.get(label[:3].title())
		if canon and canon not in out:
			out.append(canon)
	return out


def _allowed_day_strs(day_strs, receive_days, work_days=None):
	"""Filter calendar days by receive ∩ working (Q4). Empty receive = all work days."""
	recv = set(_parse_receive_days(receive_days))
	work = set(work_days) if work_days else None
	out = []
	for ds in day_strs or []:
		label = _weekday_label_for_date(ds)
		if work is not None and label not in work:
			continue
		if recv and label not in recv:
			continue
		out.append(str(ds))
	return out


def _should_soft_lock(due_date, as_of, lead_days, overdue=False):
	"""True → do not move (Q10). Overdue always unlocked."""
	if overdue:
		return False
	due = cstr(due_date or "").strip()
	if not due:
		return False
	from frappe.utils import add_days

	as_of_d = getdate(as_of)
	try:
		due_d = getdate(due)
	except Exception:
		return False
	lock_until = add_days(as_of_d, max(0, cint(lead_days)))
	return as_of_d <= due_d <= lock_until


def _greedy_priority_tuple(r, as_of=None):
	"""Sort key: overdue → single receive day → older due → name (Q13 priority ladder)."""
	recv = _parse_receive_days(r.get("receive_days"))
	single = 0 if len(recv) == 1 else 1
	due = cstr(r.get("due_date") or "") or "9999-99-99"
	return (
		0 if r.get("overdue") else 1,
		single,
		due,
		cstr(r.get("delivery_note") or ""),
	)


def _territory_id_from_zone(zone_code):
	"""'T3-WED' → 'T3'; else zone code."""
	code = cstr(zone_code or "").strip().upper()
	t, _day = _split_territory_zone_code(code)
	return t or code or None


def _drivers_nearby_for_stop(stop, drivers, driver_centroids, preferred=None, max_km=8):
	"""Preferred first, then drivers within max_km of stop (or same territory), else [] (Q7)."""
	cands = []
	pref = cstr(preferred or "").strip() or None
	if pref and pref in drivers:
		cands.append(pref)
	try:
		lat = flt(stop.get("lat"))
		lng = flt(stop.get("lng"))
	except Exception:
		lat = lng = 0
	stop_terr = _territory_id_from_zone(stop.get("zone"))
	scored = []
	for drv in drivers:
		if drv == pref or drv == "__unassigned__":
			continue
		cent = (driver_centroids or {}).get(drv)
		if not cent:
			continue
		same_terr = False
		if stop_terr and cent.get("territory") and cent["territory"] == stop_terr:
			same_terr = True
		dist = None
		if lat and lng and cent.get("lat") and cent.get("lng"):
			dist = _haversine_km(lat, lng, cent["lat"], cent["lng"])
		if same_terr or (dist is not None and dist <= flt(max_km)):
			scored.append((dist if dist is not None else 999, drv))
	scored.sort(key=lambda x: x[0])
	for _d, drv in scored:
		if drv not in cands:
			cands.append(drv)
	return cands


def _depot_from_warehouse_pin(warehouse=None):
	"""Prefer the map warehouse pin (user-dragged depot) over Address geocode."""
	_by_veh, default_pin = _warehouse_pins_index()
	wh = cstr(warehouse or "").strip()
	if wh:
		for p in (_load_tms_map_store().get("pins") or []):
			if not isinstance(p, dict) or cstr(p.get("kind") or "") != "warehouse":
				continue
			ref = cstr(p.get("ref_name") or "").strip()
			if ref == wh and (p.get("lat") is not None or p.get("lng") is not None):
				return {"lat": flt(p.get("lat")), "lng": flt(p.get("lng"))}
	if default_pin and (default_pin.get("lat") is not None or default_pin.get("lng") is not None):
		return {"lat": flt(default_pin.get("lat")), "lng": flt(default_pin.get("lng"))}
	return None


def _default_depot_latlng(company=None):
	"""Map warehouse pin → warehouse Address → BA center.

	TSP / fleet sketches must start at the same pin the map shows as the depot.
	"""
	wh = None
	try:
		if company and frappe.db.has_column("Company", "custom_default_warehouse"):
			wh = frappe.db.get_value("Company", company, "custom_default_warehouse")
	except Exception:
		wh = None
	if not wh:
		rows = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0},
			fields=["name"],
			limit=1,
			ignore_permissions=True,
		)
		wh = rows[0].name if rows else None

	pin_geo = _depot_from_warehouse_pin(wh)
	if pin_geo:
		return pin_geo

	if wh and frappe.db.exists("Warehouse", wh):
		addr = None
		links = frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Warehouse", "link_name": wh, "parenttype": "Address"},
			fields=["parent"],
			limit=1,
			ignore_permissions=True,
		)
		addr = links[0].parent if links else None
		if addr and frappe.db.has_column("Address", "custom_latitude"):
			lat = flt(frappe.db.get_value("Address", addr, "custom_latitude"))
			lng = flt(frappe.db.get_value("Address", addr, "custom_longitude"))
			if lat and lng:
				return {"lat": lat, "lng": lng}
	return {"lat": -34.6037, "lng": -58.3816}


def _so_delivery_forced(so_name):
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return False
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _delivery_date_forced_from_tags

		return bool(_delivery_date_forced_from_tags(so_name))
	except Exception:
		return False


def _zone_driver_map(zones=None):
	zones = zones if zones is not None else (_load_tms_zones().get("zones") or [])
	out = {}
	for z in zones:
		if not isinstance(z, dict):
			continue
		code = cstr(z.get("code") or "").strip().upper()
		name = cstr(z.get("name") or "").strip().upper()
		driver = cstr(z.get("driver") or "").strip() or None
		if code:
			out[code] = driver
		if name:
			out[name] = driver
	return out


def _zone_centroids_by_driver(zones=None):
	"""Approx driver home territory centroid from zone member codes (for nearby)."""
	zones = zones if zones is not None else (_load_tms_zones().get("zones") or [])
	# Use BA pocket centers keyed by territory / code
	by_drv = {}
	for z in zones:
		if not isinstance(z, dict):
			continue
		drv = cstr(z.get("driver") or "").strip()
		if not drv:
			continue
		code = cstr(z.get("code") or "").strip().upper()
		terr = _territory_id_from_zone(code)
		# Prefer stored lat/lng on zone if any; else BA pocket
		lat = flt(z.get("lat")) if z.get("lat") not in (None, "") else None
		lng = flt(z.get("lng")) if z.get("lng") not in (None, "") else None
		if not lat or not lng:
			pocket = _ZONE_BA_CENTERS.get(code) or _ZONE_BA_CENTERS.get(terr or "")
			if pocket:
				lat, lng = pocket
			else:
				# Hash spread
				idx = abs(hash(code or drv)) % len(_ZONE_BA_CENTERS)
				lat, lng = list(_ZONE_BA_CENTERS.values())[idx]
		prev = by_drv.get(drv)
		if not prev:
			by_drv[drv] = {"lat": lat, "lng": lng, "territory": terr, "n": 1}
		else:
			n = prev["n"] + 1
			by_drv[drv] = {
				"lat": (prev["lat"] * prev["n"] + lat) / n,
				"lng": (prev["lng"] * prev["n"] + lng) / n,
				"territory": prev.get("territory") or terr,
				"n": n,
			}
	return by_drv


def _working_day_dates(as_of, work_days, horizon_days, lead_days):
	from frappe.utils import add_days

	as_of = getdate(as_of)
	earliest = add_days(as_of, max(0, cint(lead_days)))
	wanted = set(work_days or ["Mon", "Tue", "Wed", "Thu", "Fri"])
	out = []
	for i in range(0, max(1, cint(horizon_days)) * 2 + 14):
		d = add_days(earliest, i)
		label = _WEEKDAY_ORDER[d.weekday()]
		if label in wanted:
			out.append(d)
		if len(out) >= max(1, cint(horizon_days)):
			break
	return out


def _load_active_trip_capacity_stops(as_of, horizon_days, drivers):
	"""Stops on Draft/In Transit trips in horizon — capacity only, not movable (Q1/Q12)."""
	from frappe.utils import add_days

	as_of_d = getdate(as_of)
	end = add_days(as_of_d, max(1, cint(horizon_days)))
	start = add_days(as_of_d, -1)
	trips = frappe.get_all(
		"Delivery Trip",
		filters={
			"docstatus": ["<", 2],
			"status": ["in", ["Draft", "Scheduled", "In Transit"]],
			"departure_time": ["between", [f"{start} 00:00:00", f"{end} 23:59:59"]],
		},
		fields=["name", "driver", "departure_time", "status"],
		ignore_permissions=True,
	)
	if drivers:
		allow = set(drivers)
		trips = [t for t in trips if cstr(t.driver or "") in allow]
	if not trips:
		return []
	trip_day = {}
	for t in trips:
		drv = cstr(t.driver or "").strip()
		if not drv:
			continue
		day = str(getdate(t.departure_time)) if t.departure_time else None
		if not day:
			continue
		trip_day[t.name] = (day, drv)
	names = list(trip_day.keys())
	raw = frappe.get_all(
		"Delivery Stop",
		filters={"parent": ["in", names]},
		fields=["parent", "lat", "lng", "address", "delivery_note", "customer"],
		ignore_permissions=True,
	)
	out = []
	for s in raw:
		meta = trip_day.get(s.parent)
		if not meta:
			continue
		day, drv = meta
		out.append(
			{
				"delivery_note": s.delivery_note,
				"address_name": s.address,
				"lat": flt(s.lat) if s.lat else None,
				"lng": flt(s.lng) if s.lng else None,
				"due_date": day,
				"preferred_driver": drv,
				"on_active_trip": True,
				"forced": True,  # capacity seed only
			}
		)
	return out


def _address_receive_days_map(address_names):
	out = {}
	names = [a for a in (address_names or []) if a]
	if not names:
		return out
	if not frappe.db.has_column("Address", "custom_receive_days"):
		return out
	for row in frappe.get_all(
		"Address",
		filters={"name": ["in", names]},
		fields=["name", "custom_receive_days"],
		ignore_permissions=True,
	):
		out[row.name] = _parse_receive_days(row.custom_receive_days)
	return out


def _pack_remitos_core(
	movable,
	fixed,
	trip_capacity,
	drivers,
	day_strs,
	depot,
	*,
	max_orders,
	max_minutes,
	stop_minutes,
	avg_speed,
	drive_buffer,
	work_days,
	as_of,
	lead_days,
	driver_centroids=None,
	nearby_km=8,
	zone_fallback_fn=None,
	prefer_sticky=True,
):
	"""Pure packer (i045 decisions). Returns assignments, overflow, fills, locked.

	``movable`` / ``fixed`` / ``trip_capacity`` are stop dicts.
	``prefer_sticky`` (Q13): when True, try previous due first if it still fits.
	"""
	drivers = list(drivers) or ["__unassigned__"]
	driver_centroids = driver_centroids or {}
	buckets = {}  # (day, driver) -> [stops]
	if isinstance(prefer_sticky, str):
		prefer_sticky = prefer_sticky.strip().lower() in ("1", "true", "yes", "on")
	else:
		prefer_sticky = bool(prefer_sticky)

	def seed(row, force_day=None, force_drv=None):
		due = force_day or row.get("due_date")
		if not due:
			return
		# Q2: no preferred driver → unassigned bucket (do not steal drivers[0] capacity)
		drv = force_drv or row.get("preferred_driver") or "__unassigned__"
		buckets.setdefault((str(due), drv), []).append(row)

	for r in trip_capacity or []:
		seed(r)
	for r in fixed or []:
		seed(r)

	# Soft-lock movable that stay put (Q10)
	locked = []
	to_place = []
	for r in movable or []:
		if r.get("force_unlock"):
			to_place.append(r)
		elif _should_soft_lock(r.get("due_date"), as_of, lead_days, overdue=bool(r.get("overdue"))):
			seed(r)
			locked.append(
				{
					"delivery_note": r.get("delivery_note"),
					"sales_order": r.get("sales_order"),
					"customer_name": r.get("customer_name"),
					"previous_due_date": r.get("due_date"),
					"proposed_due_date": r.get("due_date"),
					"driver": r.get("preferred_driver"),
					"zone": r.get("zone"),
					"overflow": False,
					"forced": False,
					"soft_locked": True,
					"unassigned_driver": not bool(r.get("preferred_driver")),
				}
			)
		else:
			to_place.append(r)

	# Soft-lock protects armado, but not over soft cap: release excess so greedy
	# can rebalance tomorrow/near dues that would otherwise leave one driver at 42+.
	locked_by_dn = {l["delivery_note"]: l for l in locked if l.get("delivery_note")}
	released_rows = []
	for key, stops in list(buckets.items()):
		ok, _cnt, _mins = _fits_soft_cap(
			stops, depot, max_orders, max_minutes, stop_minutes, avg_speed, drive_buffer
		)
		if ok:
			continue
		soft_rows = [s for s in stops if s.get("delivery_note") in locked_by_dn]
		# Release least urgent first (reverse of pack priority)
		soft_rows.sort(key=lambda r: _greedy_priority_tuple(r, as_of), reverse=True)
		for s in soft_rows:
			ok2, _, _ = _fits_soft_cap(
				buckets.get(key) or [],
				depot,
				max_orders,
				max_minutes,
				stop_minutes,
				avg_speed,
				drive_buffer,
			)
			if ok2:
				break
			try:
				buckets[key].remove(s)
			except ValueError:
				continue
			released_rows.append(s)
			locked_by_dn.pop(s.get("delivery_note"), None)

	if released_rows:
		locked = [l for l in locked if l.get("delivery_note") in locked_by_dn]
		to_place.extend(released_rows)

	to_place.sort(key=lambda r: _greedy_priority_tuple(r, as_of))

	assignments = list(locked)
	overflow = []

	def try_fit(row, day_str, drv):
		key = (day_str, drv)
		trial = list(buckets.get(key) or []) + [row]
		ok, cnt, mins = _fits_soft_cap(
			trial, depot, max_orders, max_minutes, stop_minutes, avg_speed, drive_buffer
		)
		return ok, cnt, mins

	for r in to_place:
		allowed = _allowed_day_strs(day_strs, r.get("receive_days"), work_days)
		# Soft sticky (Q13): optional — ASAP rebalance leaves chronological order.
		prev = cstr(r.get("due_date") or "").strip()
		ordered_days = list(allowed)
		if prefer_sticky and prev and prev in ordered_days:
			ordered_days = [prev] + [d for d in ordered_days if d != prev]

		placed = False

		for day_str in ordered_days:
			pref = r.get("preferred_driver")
			cands = _drivers_nearby_for_stop(
				r, drivers, driver_centroids, preferred=pref, max_km=nearby_km
			)
			# Q2: if no preferred and no nearby, still try all drivers only as last resort for that day
			if not cands:
				cands = [d for d in drivers if d != "__unassigned__"] or ["__unassigned__"]

			# Q7: prefer zone.driver exclusively first — only use others if pref cannot fit this day
			day_cands = []
			if pref and pref in drivers:
				ok_p, cnt_p, mins_p = try_fit(r, day_str, pref)
				if ok_p:
					day_cands = [(cnt_p, mins_p, pref)]
				else:
					for drv in cands:
						if drv == pref:
							continue
						ok, cnt, mins = try_fit(r, day_str, drv)
						if ok:
							day_cands.append((cnt, mins, drv))
			else:
				for drv in cands:
					ok, cnt, mins = try_fit(r, day_str, drv)
					if ok:
						day_cands.append((cnt, mins, drv))

			if not day_cands:
				continue
			day_cands.sort(key=lambda x: (x[0], x[1]))
			cnt, mins, drv = day_cands[0]
			# Sticky day wins immediately
			buckets.setdefault((day_str, drv), []).append(r)
			unassigned = drv == "__unassigned__" or not drv
			assignments.append(
				{
					"delivery_note": r.get("delivery_note"),
					"sales_order": r.get("sales_order"),
					"customer_name": r.get("customer_name"),
					"previous_due_date": r.get("due_date"),
					"proposed_due_date": day_str,
					"driver": None if unassigned else drv,
					"zone": r.get("zone"),
					"overflow": False,
					"forced": False,
					"soft_locked": False,
					"unassigned_driver": unassigned,
					"estimated_day_orders": cnt,
					"estimated_day_minutes": round(mins, 1),
					"limiting": _limiting_resource(cnt, max_orders, mins, max_minutes),
				}
			)
			placed = True
			break

		if placed:
			continue

		# Overflow → zone visit day (may exceed soft cap) on preferred driver (Q7)
		fallback = None
		if callable(zone_fallback_fn):
			try:
				fallback = zone_fallback_fn(r.get("zone"), as_of)
			except Exception:
				fallback = None
		if not fallback:
			# Next allowed day or last horizon day
			fallback = (allowed[-1] if allowed else None) or (day_strs[-1] if day_strs else None)
		drv = r.get("preferred_driver")
		unassigned = not drv or drv == "__unassigned__"
		row_out = {
			"delivery_note": r.get("delivery_note"),
			"sales_order": r.get("sales_order"),
			"customer_name": r.get("customer_name"),
			"previous_due_date": r.get("due_date"),
			"proposed_due_date": str(fallback) if fallback else None,
			"driver": None if unassigned else drv,
			"zone": r.get("zone"),
			"overflow": True,
			"forced": False,
			"soft_locked": False,
			"unassigned_driver": unassigned,
		}
		overflow.append(row_out)
		assignments.append(row_out)
		if fallback and drv:
			buckets.setdefault((str(fallback), drv), []).append(r)

	fills = []
	for (day_str, drv), stops in sorted(buckets.items()):
		ok, cnt, mins = _fits_soft_cap(
			stops, depot, max_orders, max_minutes, stop_minutes, avg_speed, drive_buffer
		)
		fills.append(
			{
				"date": day_str,
				"driver": None if drv == "__unassigned__" else drv,
				"order_count": cnt,
				"max_orders": max_orders,
				"estimated_minutes": round(mins, 1),
				"max_minutes": max_minutes,
				"over_cap": not ok,
				"limiting": _limiting_resource(cnt, max_orders, mins, max_minutes),
			}
		)

	return {
		"assignments": assignments,
		"overflow": overflow,
		"fills": fills,
		"locked_count": len(locked),
	}


def _build_greedy_pack_preview(
	as_of=None,
	horizon_days=None,
	driver_names=None,
	company=None,
	skip_tomorrow=None,
	max_orders=None,
	max_minutes=None,
	avg_speed_kmh=None,
	working_days=None,
	prefer_sticky=None,
):
	settings = _load_tms_settings()
	# Optional modal override for which weekdays may receive packed dues.
	raw_wd = working_days
	if isinstance(raw_wd, str):
		s = raw_wd.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			raw_wd = None
		else:
			try:
				raw_wd = frappe.parse_json(s)
			except Exception:
				raw_wd = [p.strip() for p in s.split(",") if p.strip()]
	if isinstance(raw_wd, list) and raw_wd:
		work_days = _normalize_working_days(raw_wd)
	else:
		work_days = _normalize_working_days(settings.get("auto_group_working_days"))
	if not work_days:
		work_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]

	raw_as = as_of
	if isinstance(raw_as, str):
		raw_as = raw_as.strip()
		if raw_as.lower() in ("", "null", "undefined", "none"):
			raw_as = None
	try:
		as_of_d = getdate(raw_as) if raw_as else getdate()
	except Exception:
		as_of_d = getdate()

	if skip_tomorrow is None:
		skip_tm = False
	elif isinstance(skip_tomorrow, str):
		skip_tm = skip_tomorrow.strip().lower() in ("1", "true", "yes", "on")
	else:
		skip_tm = cint(skip_tomorrow) != 0

	# ASAP rebalance default: prefer_sticky off so earlier weekdays fill before sticky Mon.
	if prefer_sticky is None:
		sticky = False
	elif isinstance(prefer_sticky, str):
		sticky = prefer_sticky.strip().lower() in ("1", "true", "yes", "on")
	else:
		sticky = cint(prefer_sticky) != 0

	horizon = cint(horizon_days) if horizon_days not in (None, "", "null", "undefined") else cint(
		settings.get("greedy_horizon_days") or 14
	)
	horizon = max(1, horizon)
	lead = cint(settings.get("delivery_lead_days") or 1)
	# Don't schedule tomorrow → earliest pack day one step past normal lead.
	pack_lead = lead + (1 if skip_tm else 0)

	def _opt_int(raw, fallback, minimum):
		if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "null", "undefined", "none")):
			return max(minimum, cint(fallback))
		try:
			return max(minimum, cint(raw))
		except Exception:
			return max(minimum, cint(fallback))

	max_orders = _opt_int(max_orders, settings.get("driver_day_max_orders") or 30, 1)
	max_minutes = _opt_int(max_minutes, settings.get("driver_day_max_minutes") or 480, 30)
	stop_minutes = max(1, cint(settings.get("default_stop_minutes") or 15))
	avg_speed = _opt_int(avg_speed_kmh, settings.get("avg_speed_kmh") or 25, 5)
	drive_buffer = max(0, cint(settings.get("drive_buffer_minutes_per_leg") or 5))
	nearby_km = max(1, cint(settings.get("nearby_driver_max_km") or 8))

	drivers = _parse_json_list(driver_names, [])
	if not drivers:
		drivers = [
			d.name
			for d in frappe.get_all(
				"Driver", filters={"status": "Active"}, fields=["name"], ignore_permissions=True
			)
		]
	if not drivers:
		drivers = ["__unassigned__"]

	depot = _default_depot_latlng(company)
	zones = _load_tms_zones().get("zones") or []
	zone_driver = _zone_driver_map(zones)
	driver_centroids = _zone_centroids_by_driver(zones)

	from frappe.utils import add_days

	ceiling = add_days(as_of_d, horizon)
	tomorrow_s = str(add_days(as_of_d, 1))
	pending = get_pending_deliveries(date=str(ceiling), company=company)
	deliveries = list(pending.get("deliveries") or [])

	addr_names = [cstr(d.get("address_name") or "") for d in deliveries if d.get("address_name")]
	recv_map = _address_receive_days_map(addr_names)

	movable = []
	fixed = []
	for d in deliveries:
		so = cstr(d.get("sales_order") or "").strip()
		forced = _so_delivery_forced(so) if so else False
		addr = cstr(d.get("address_name") or "").strip()
		due_s = cstr(d.get("due_date") or "").strip() or None
		try:
			overdue = bool(due_s and getdate(due_s) < as_of_d)
		except Exception:
			overdue = bool(d.get("overdue"))
		row = {
			"delivery_note": d.get("delivery_note"),
			"sales_order": so or None,
			"customer": d.get("customer"),
			"customer_name": d.get("customer_name"),
			"address_name": addr or None,
			"lat": flt(d.get("lat")) if d.get("lat") else None,
			"lng": flt(d.get("lng")) if d.get("lng") else None,
			"zone": cstr(d.get("zone") or "").strip() or None,
			"due_date": due_s,
			"overdue": overdue,
			"forced": forced,
			"receive_days": recv_map.get(addr) or [],
			# Unlock remitos already due tomorrow so they can move later.
			"force_unlock": bool(skip_tm and due_s == tomorrow_s),
		}
		zkey = (row["zone"] or "").upper()
		row["preferred_driver"] = zone_driver.get(zkey) if zkey else None
		if forced:
			fixed.append(row)
		else:
			movable.append(row)

	day_dates = _working_day_dates(as_of_d, work_days, horizon, pack_lead)
	day_strs = [str(d) for d in day_dates]
	if skip_tm:
		day_strs = [ds for ds in day_strs if ds != tomorrow_s]
	trip_cap = _load_active_trip_capacity_stops(as_of_d, horizon, [d for d in drivers if d != "__unassigned__"])

	def _fallback(zone, as_of_s):
		from erpnext.erpnext_integrations.ecommerce_api.api import _zone_visit_due_date

		due = _zone_visit_due_date(zone, as_of=as_of_s)
		if skip_tm and due and str(due) == tomorrow_s:
			try:
				return _zone_visit_due_date(zone, as_of=str(add_days(getdate(tomorrow_s), 1))) or due
			except Exception:
				return due
		return due

	core = _pack_remitos_core(
		movable,
		fixed,
		trip_cap,
		drivers,
		day_strs,
		depot,
		max_orders=max_orders,
		max_minutes=max_minutes,
		stop_minutes=stop_minutes,
		avg_speed=avg_speed,
		drive_buffer=drive_buffer,
		work_days=work_days,
		as_of=str(as_of_d),
		lead_days=pack_lead,
		driver_centroids=driver_centroids,
		nearby_km=nearby_km,
		zone_fallback_fn=lambda z, a: _fallback(z, a),
		prefer_sticky=sticky,
	)

	return {
		"strategy": "fast_deliver_greedy",
		"packing_strategy_setting": _packing_strategy(settings),
		"as_of": str(as_of_d),
		"horizon_days": horizon,
		"lead_days": lead,
		"pack_lead_days": pack_lead,
		"skip_tomorrow": skip_tm,
		"prefer_sticky": sticky,
		"working_days": work_days,
		"candidate_days": day_strs,
		"max_orders": max_orders,
		"max_minutes": max_minutes,
		"default_stop_minutes": stop_minutes,
		"avg_speed_kmh": avg_speed,
		"drive_buffer_minutes_per_leg": drive_buffer,
		"nearby_driver_max_km": nearby_km,
		"assignments": core["assignments"],
		"overflow": core["overflow"],
		"fixed_forced": [
			{
				"delivery_note": r["delivery_note"],
				"due_date": r.get("due_date"),
				"driver": r.get("preferred_driver"),
			}
			for r in fixed
		],
		"fills": core["fills"],
		"warnings": (["no_drivers"] if drivers == ["__unassigned__"] else [])
		+ (["overflow"] if core["overflow"] else [])
		+ (["skip_tomorrow"] if skip_tm else []),
		"movable_count": len(movable),
		"forced_count": len(fixed),
		"soft_locked_count": core.get("locked_count") or 0,
		"trip_capacity_stops": len(trip_cap),
	}


@frappe.whitelist(allow_guest=True)
def preview_greedy_pack(
	as_of=None,
	horizon_days=None,
	driver_names=None,
	company=None,
	skip_tomorrow=None,
	max_orders=None,
	max_minutes=None,
	avg_speed_kmh=None,
	working_days=None,
	prefer_sticky=None,
):
	"""Preview ASAP due dates under soft caps (does not write).

	``skip_tomorrow``: when truthy, earliest pack day skips tomorrow and remitos
	already due tomorrow are unlocked so they can move later.

	``max_orders`` / ``max_minutes`` / ``avg_speed_kmh``: optional soft-cap overrides
	for this preview (else TMS Settings defaults).

	``working_days``: optional Mon..Sun list override for packable weekdays.
	``prefer_sticky``: when truthy, keep previous due if it still fits (Q13); default off
	so ASAP fills earlier weekdays (e.g. Friday before a sticky Monday).
	"""
	return _build_greedy_pack_preview(
		as_of=as_of,
		horizon_days=horizon_days,
		driver_names=driver_names,
		company=company,
		skip_tomorrow=skip_tomorrow,
		max_orders=max_orders,
		max_minutes=max_minutes,
		avg_speed_kmh=avg_speed_kmh,
		working_days=working_days,
		prefer_sticky=prefer_sticky,
	)


@frappe.whitelist(allow_guest=True)
def commit_greedy_pack(preview=None):
	"""Apply greedy pack: update SO.delivery_date or DN.custom_requested_delivery_date."""
	if isinstance(preview, str):
		s = preview.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			preview = None
		else:
			try:
				preview = frappe.parse_json(s)
			except Exception:
				preview = None
	if not isinstance(preview, dict):
		frappe.throw(_("Preview payload is required."))

	assignments = preview.get("assignments")
	if not isinstance(assignments, list) or not assignments:
		frappe.throw(_("Preview has no assignments to commit."))

	updated = 0
	skipped = 0
	errors = []
	for a in assignments:
		if not isinstance(a, dict):
			continue
		if a.get("forced"):
			skipped += 1
			continue
		# Soft-locked rows keep their due — skip noop writes (Q10)
		if a.get("soft_locked") and cstr(a.get("proposed_due_date") or "") == cstr(
			a.get("previous_due_date") or ""
		):
			skipped += 1
			continue
		dn = cstr(a.get("delivery_note") or "").strip()
		due = cstr(a.get("proposed_due_date") or "").strip()
		so = cstr(a.get("sales_order") or "").strip()
		if not due or not dn:
			skipped += 1
			continue
		if not frappe.db.exists("Delivery Note", dn):
			skipped += 1
			continue
		so_names = _sales_orders_for_dn(dn) if not so else [so]
		# Skip if any SO is forced
		if so_names and any(_so_delivery_forced(s) for s in so_names):
			skipped += 1
			continue
		try:
			due_d = getdate(due)
			drv = cstr(a.get("driver") or "").strip() or None
			_apply_pending_delivery_due(dn, due_d, driver=drv)
			updated += 1
		except Exception as exc:
			errors.append({"delivery_note": dn, "error": str(exc)})

	frappe.db.commit()
	return {
		"ok": not bool(errors),
		"updated": updated,
		"skipped": skipped,
		"errors": errors,
	}


@frappe.whitelist(allow_guest=True)
def propose_due_for_order(delivery_note=None, sales_order=None, address=None, zone=None, lat=None, lng=None):
	"""Single-order due proposal for confirm-time branching (i045)."""
	settings = _load_tms_settings()
	strategy = _packing_strategy(settings)
	if strategy != "fast_deliver_greedy":
		# Zone path
		z = cstr(zone or "").strip()
		try:
			from erpnext.erpnext_integrations.ecommerce_api.api import _zone_visit_due_date

			due = _zone_visit_due_date(z) if z else None
		except Exception:
			due = None
		return {"strategy": strategy, "proposed_due_date": due, "overflow": False}

	# Build a one-shot pack including this order if present in pending, else synthetic
	preview = _build_greedy_pack_preview()
	dn = cstr(delivery_note or "").strip()
	so = cstr(sales_order or "").strip()
	for a in preview.get("assignments") or []:
		if dn and a.get("delivery_note") == dn:
			return {
				"strategy": strategy,
				"proposed_due_date": a.get("proposed_due_date"),
				"overflow": bool(a.get("overflow")),
				"driver": a.get("driver"),
			}
		if so and a.get("sales_order") == so:
			return {
				"strategy": strategy,
				"proposed_due_date": a.get("proposed_due_date"),
				"overflow": bool(a.get("overflow")),
				"driver": a.get("driver"),
			}
	# Not in open queue yet — run mini fit for synthetic stop
	lat_f = flt(lat) if lat not in (None, "", "null") else None
	lng_f = flt(lng) if lng not in (None, "", "null") else None
	if lat_f and lng_f:
		synth = {
			"delivery_note": dn or "__new__",
			"sales_order": so or None,
			"lat": lat_f,
			"lng": lng_f,
			"zone": cstr(zone or "").strip() or None,
			"due_date": None,
			"overdue": False,
			"forced": False,
			"preferred_driver": _zone_driver_map().get(cstr(zone or "").strip().upper()),
		}
		# Reuse preview fills: try first day that fits
		work_days = _normalize_working_days(settings.get("auto_group_working_days"))
		as_of_d = getdate()
		lead = cint(settings.get("delivery_lead_days") or 1)
		horizon = cint(settings.get("greedy_horizon_days") or 14)
		max_orders = max(1, cint(settings.get("driver_day_max_orders") or 30))
		max_minutes = max(30, cint(settings.get("driver_day_max_minutes") or 480))
		stop_minutes = max(1, cint(settings.get("default_stop_minutes") or 15))
		avg_speed = max(5, cint(settings.get("avg_speed_kmh") or 25))
		drive_buffer = max(0, cint(settings.get("drive_buffer_minutes_per_leg") or 5))
		depot = _default_depot_latlng()
		drivers = [
			d.name
			for d in frappe.get_all(
				"Driver", filters={"status": "Active"}, fields=["name"], ignore_permissions=True
			)
		] or ["__unassigned__"]
		# Seed from preview fills as occupied
		buckets = {}
		for f in preview.get("fills") or []:
			key = (f.get("date"), f.get("driver") or "__unassigned__")
			# Approximate occupied with placeholder stops using count only
			n = cint(f.get("order_count") or 0)
			buckets[key] = [{"lat": depot["lat"], "lng": depot["lng"]}] * n
		for day in _working_day_dates(as_of_d, work_days, horizon, lead):
			day_str = str(day)
			pref = synth.get("preferred_driver")
			cands = ([pref] if pref in drivers else []) + [d for d in drivers if d != pref]
			for drv in cands:
				key = (day_str, drv)
				trial = list(buckets.get(key) or []) + [synth]
				ok, _cnt, _mins = _fits_soft_cap(
					trial, depot, max_orders, max_minutes, stop_minutes, avg_speed, drive_buffer
				)
				if ok:
					return {
						"strategy": strategy,
						"proposed_due_date": day_str,
						"overflow": False,
						"driver": None if drv == "__unassigned__" else drv,
					}
		try:
			from erpnext.erpnext_integrations.ecommerce_api.api import _zone_visit_due_date

			fb = _zone_visit_due_date(zone)
		except Exception:
			fb = None
		return {"strategy": strategy, "proposed_due_date": fb, "overflow": True}
	# No geo — zone fallback
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _zone_visit_due_date

		fb = _zone_visit_due_date(zone)
	except Exception:
		fb = None
	return {"strategy": strategy, "proposed_due_date": fb, "overflow": bool(not fb)}


# ---------------------------------------------------------------------------
# Map pins / temp locations / named plans (Routes map tools)
# ---------------------------------------------------------------------------
TMS_MAP_PINS_SCOPE = "settings.tms_map_pins"

# Default service domain for a new warehouse pin (approx. CABA / BA metro).
BA_COVERAGE_DEFAULT = {
	"kind": "bbox",
	"name": "Buenos Aires",
	"south": -34.705,
	"west": -58.531,
	"north": -34.526,
	"east": -58.335,
}


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
	store = _load_tms_map_store()
	pins = list(store.get("pins") or [])
	changed = False
	for i, p in enumerate(pins):
		if not isinstance(p, dict) or cstr(p.get("kind")) != "warehouse":
			continue
		row = dict(p)
		if not row.get("coverage"):
			row["coverage"] = dict(BA_COVERAGE_DEFAULT)
			changed = True
		if row.get("vehicles") is None:
			row["vehicles"] = []
			changed = True
		pins[i] = row
	# Single-depot convenience: if exactly one warehouse and no vehicles claimed yet,
	# park every vehicle there.
	wh_pins = [p for p in pins if isinstance(p, dict) and cstr(p.get("kind")) == "warehouse"]
	if len(wh_pins) == 1 and not (wh_pins[0].get("vehicles") or []):
		all_veh = frappe.get_all("Vehicle", pluck="name", ignore_permissions=True) or []
		if all_veh:
			for i, p in enumerate(pins):
				if str(p.get("id")) == str(wh_pins[0].get("id")):
					pins[i] = {**p, "vehicles": list(all_veh), "updated_at": str(now_datetime())}
					changed = True
					break
	if changed:
		store["pins"] = pins
		_save_tms_map_store(store)
	return {"pins": pins, "plans": store.get("plans") or []}


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
@idempotent_request
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
		"vehicles": [],
		"coverage": dict(BA_COVERAGE_DEFAULT),
		"created_at": str(now_datetime()),
		"updated_at": str(now_datetime()),
	}
	# First warehouse: assign every active vehicle (single-depot setups).
	existing_wh = [p for p in (store.get("pins") or []) if cstr(p.get("kind")) == "warehouse"]
	if not existing_wh:
		pin["vehicles"] = frappe.get_all(
			"Vehicle", pluck="name", ignore_permissions=True
		) or []
	store["pins"] = list(store.get("pins") or []) + [pin]
	_save_tms_map_store(store)
	return {"warehouse": wh.name, "address": addr.name, "pin": pin, "pins": store["pins"]}


@frappe.whitelist(allow_guest=True)
def update_warehouse_depot(pin_id=None, vehicles=None, coverage=None):
	"""Assign vehicles + service coverage domain on a warehouse map pin.

	``vehicles``: list of Vehicle names (for now typically one warehouse owns all).
	``coverage``: {kind, name, south, west, north, east} bbox — default Buenos Aires.
	"""
	pin_id = cstr(pin_id or "").strip()
	if not pin_id or pin_id.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Warehouse pin is required."))

	store = _load_tms_map_store()
	pins = list(store.get("pins") or [])
	idx = next((i for i, p in enumerate(pins) if str(p.get("id")) == pin_id), None)
	if idx is None:
		frappe.throw(_("Map pin {0} not found").format(pin_id))
	pin = dict(pins[idx])
	if cstr(pin.get("kind") or "") != "warehouse":
		frappe.throw(_("Pin is not a warehouse."))

	if vehicles is not None:
		raw = vehicles
		if isinstance(raw, str):
			s = raw.strip()
			if not s or s.lower() in ("null", "undefined", "none"):
				raw = []
			else:
				try:
					raw = frappe.parse_json(s)
				except Exception:
					raw = [x.strip() for x in s.split(",") if x.strip()]
		if not isinstance(raw, (list, tuple)):
			raw = []
		# Unique vehicle names; also clear them from other warehouse pins (1 depot model).
		wanted = []
		seen = set()
		for v in raw:
			name = cstr(v or "").strip()
			if not name or name in seen:
				continue
			seen.add(name)
			wanted.append(name)
		for i, p in enumerate(pins):
			if i == idx or cstr(p.get("kind")) != "warehouse":
				continue
			others = [x for x in (p.get("vehicles") or []) if cstr(x) not in seen]
			if others != (p.get("vehicles") or []):
				pins[i] = {**p, "vehicles": others, "updated_at": str(now_datetime())}
		pin["vehicles"] = wanted

	if coverage is not None:
		raw_c = coverage
		if isinstance(raw_c, str):
			s = raw_c.strip()
			if not s or s.lower() in ("null", "undefined", "none"):
				raw_c = None
			else:
				try:
					raw_c = frappe.parse_json(s)
				except Exception:
					raw_c = None
		if raw_c is None:
			pin["coverage"] = dict(BA_COVERAGE_DEFAULT)
		elif isinstance(raw_c, dict):
			cov = dict(BA_COVERAGE_DEFAULT)
			cov.update({k: raw_c[k] for k in raw_c if k in ("kind", "name", "south", "west", "north", "east")})
			pin["coverage"] = cov

	# Ensure coverage always present on warehouse pins.
	if not pin.get("coverage"):
		pin["coverage"] = dict(BA_COVERAGE_DEFAULT)

	pin["updated_at"] = str(now_datetime())
	pins[idx] = pin
	store["pins"] = pins
	_save_tms_map_store(store)
	return {"pin": pin, "pins": pins}


@frappe.whitelist(allow_guest=True)
@idempotent_request
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
@idempotent_request
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

# Geo pockets for bulk clients (i044 rebalance showcase). Deterministic jitter — not real doors.
_TMS_BA_BULK_POCKETS = (
	{"zone": "Norte", "area": "Belgrano", "lat": -34.555, "lng": -58.455},
	{"zone": "Centro", "area": "Almagro", "lat": -34.606, "lng": -58.420},
	{"zone": "Sur", "area": "Barracas", "lat": -34.640, "lng": -58.380},
	{"zone": "Oeste", "area": "Floresta", "lat": -34.630, "lng": -58.490},
)

_TMS_BA_BULK_CLIENT_COUNT = 400
_TMS_BA_BULK_NAME_PREFIX = "BA Demo Cliente "


def _tms_ba_bulk_customers(n=None):
	"""Synthetic active geocoded clients for zone-rebalance demos (local BA demo only)."""
	n = cint(n if n is not None else _TMS_BA_BULK_CLIENT_COUNT)
	if n < 1:
		return []
	out = []
	pockets = _TMS_BA_BULK_POCKETS
	for i in range(1, n + 1):
		p = pockets[(i - 1) % len(pockets)]
		# Stable pseudo-random offset in ~±0.018° (~2 km) so clusters stay visible.
		lat = p["lat"] + (((i * 17) % 101) - 50) * 0.00036
		lng = p["lng"] + (((i * 31) % 101) - 50) * 0.00042
		name = f"{_TMS_BA_BULK_NAME_PREFIX}{i:03d}"
		street_n = 1000 + (i * 7) % 8000
		addr_title = f"Calle Demo {street_n}, {p['area']}"
		out.append(
			{
				"name": name,
				"area": p["area"],
				"addr_title": addr_title,
				"addr": f"{addr_title}, CABA",
				"lat": round(lat, 6),
				"lng": round(lng, 6),
				"zone": p["zone"],
				"bulk": True,
			}
		)
	return out


def _tms_ba_all_demo_customers():
	"""Named showcase shops + bulk book for clustering."""
	return list(_TMS_BA_CUSTOMERS_SEED) + _tms_ba_bulk_customers()


@frappe.whitelist(allow_guest=True)
def seed_tms_demo(reset=False):
	"""Create demo data for TMS dispatcher testing.

	Idempotent by default — skips anything that already exists.
	Pass reset=True to delete existing TMS demo docs first.

	Creates:
	  - 5 Drivers (each with Employee + geocoded home Address)
	  - 5 Vehicles
	  - ~25 named Customers + ~400 bulk ``BA Demo Cliente NNN`` (geocoded, zone-tagged)
	  - Delivery Notes + guest preorders for **named** customers only
	  - 4 planning Zones (Norte / Centro / Sur / Oeste)
	  - Company.custom_default_warehouse set (depot demo)
	  - 1 published trip with POD / cliente-debe demos + driver login User

	Bulk clients are for **zone rebalance / map density** (i044), not site-provisioning seeds.
	"""
	reset = frappe.parse_json(reset) if isinstance(reset, str) else bool(reset)
	# Demo company is "library"; fall back so a renamed/other company still seeds.
	company = "library"
	if not frappe.db.exists("Company", company):
		company = (
			frappe.defaults.get_global_default("company")
			or frappe.db.get_value("Company", {}, "name", order_by="creation asc")
			or company
		)
	warehouse = "POSNET Stores - L"
	item_code = "24755"
	item_name = "WHISKY MACALLAN ERATH ESTUCHE 1*700ML"
	today = frappe.utils.today()

	created = []
	skipped = []

	def _ex(doctype, name):
		return bool(frappe.db.exists(doctype, name))

	named_customers = list(_TMS_BA_CUSTOMERS_SEED)
	customers_seed = _tms_ba_all_demo_customers()

	if reset:
		# Delete existing demo DNs and trips that use them, then customers/drivers/vehicles.
		# Include legacy "TMS Demo Cliente%" names from older seeds. Bulk "BA Demo Cliente%"
		# have no transactions — they are kept and only re-tagged to their pocket zone
		# below (deleting + recreating 400 customers made reset take minutes).
		legacy_customers = frappe.get_all(
			"Customer",
			filters={"customer_name": ["like", "TMS Demo Cliente%"]},
			pluck="name",
			ignore_permissions=True,
		)
		demo_customer_ids = list(
			{
				*(frappe.db.get_value("Customer", {"customer_name": c["name"]}, "name") for c in named_customers),
				*legacy_customers,
			}
		)
		demo_customer_ids = [c for c in demo_customer_ids if c]
		# Trips first — their stops link demo Addresses and block Customer delete.
		if demo_customer_ids:
			trip_names = {
				r[0]
				for r in frappe.db.sql(
					"""SELECT DISTINCT parent FROM `tabDelivery Stop`
					WHERE parenttype = 'Delivery Trip' AND customer IN %(c)s""",
					{"c": tuple(demo_customer_ids)},
				)
			}
			for trip_name in trip_names:
				if frappe.db.get_value("Delivery Trip", trip_name, "docstatus") == 1:
					frappe.db.set_value("Delivery Trip", trip_name, "docstatus", 2)
				frappe.delete_doc("Delivery Trip", trip_name, force=True, ignore_permissions=True)
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
			# Customers with invoices / other ledgers can't go — keep them, don't abort reset.
			frappe.db.savepoint("tms_demo_cust")
			try:
				frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)
			except frappe.LinkExistsError:
				frappe.db.rollback(save_point="tms_demo_cust")
				skipped.append(f"Customer kept (linked records): {c}")
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
	# Named shops + bulk BA Demo Cliente NNN (i044 rebalance density).
	has_freq = frappe.db.has_column("Address", "custom_delivery_frequency")
	bulk_created = 0
	for idx, c in enumerate(customers_seed):
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
			if c.get("bulk"):
				bulk_created += 1
			else:
				created.append(f"Customer: {cust.name}")
		else:
			if not c.get("bulk"):
				skipped.append(f"Customer: {c['name']}")

		addr_title = c["addr_title"]
		existing_addr = frappe.db.get_value(
			"Address", {"address_title": addr_title, "address_type": "Shipping"}, "name"
		)
		addr_vals = {
			"address_line1": c["addr"],
			"city": "Buenos Aires",
			"country": "Argentina",
			"custom_latitude": c["lat"],
			"custom_longitude": c["lng"],
			"custom_zone": c["zone"],
		}
		if has_freq:
			addr_vals["custom_delivery_frequency"] = 1
		if existing_addr:
			# Keep a rebalanced zone (i044) unless this is an explicit reset.
			if not reset and cstr(
				frappe.db.get_value("Address", existing_addr, "custom_zone") or ""
			).strip():
				addr_vals.pop("custom_zone", None)
			frappe.db.set_value(
				"Address",
				existing_addr,
				addr_vals,
				update_modified=False,
			)
		else:
			addr_doc = {
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
			}
			if has_freq:
				addr_doc["custom_delivery_frequency"] = 1
			addr = frappe.get_doc(addr_doc)
			addr.insert(ignore_permissions=True)
			if not c.get("bulk"):
				created.append(f"Address: {addr.name}")
		# Keep bulk inserts from holding one giant uncommitted batch.
		if c.get("bulk") and idx % 50 == 0:
			frappe.db.commit()

	if bulk_created:
		created.append(f"Bulk customers: +{bulk_created} ({_TMS_BA_BULK_NAME_PREFIX}*)")
	frappe.db.commit()

	# ── 4. Delivery Notes (force-submitted for demo) — named shops only ────────
	for i, c in enumerate(named_customers, 1):
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
			"custom_requested_delivery_date": frappe.utils.add_days(today, 1) if i > len(named_customers) - 2 else None,
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
	# Named shops only — bulk clients are for zone density, not 400 pedidos.
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_guest_preorder_tag_fieldname,
		confirm_guest_preorder,
		create_guest_preorder,
	)

	tag_fn = _guest_preorder_tag_fieldname()
	for c in named_customers:
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
		for c in named_customers[:3]
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
	current_zones = _load_tms_zones().get("zones") or []
	if reset:
		# Reset returns the book to pocket tags → drop rebalance territories (i044).
		kept = [z for z in current_zones if (z.get("notes") or "") != "rebalance-clients"]
		if len(kept) != len(current_zones):
			_save_tms_zones(kept, commit=False)
			dropped = [
				cstr(z.get("code") or "").strip().upper()
				for z in current_zones
				if (z.get("notes") or "") == "rebalance-clients"
			]
			if dropped and frappe.db.has_column("Address", "custom_zone"):
				# Non-demo clients pointed at those zones → unzoned (flow B re-assigns).
				frappe.db.sql(
					"UPDATE `tabAddress` SET custom_zone = NULL WHERE UPPER(custom_zone) IN %(z)s",
					{"z": tuple(dropped)},
				)
			created.append(f"Zones removed: {len(current_zones) - len(kept)} rebalance zones")
		current_zones = kept
	elif any(
		(z.get("notes") or "") == "rebalance-clients" for z in current_zones if isinstance(z, dict)
	):
		# Book already rebalanced — don't re-add empty pocket zones next to T*-DAY zones.
		zones_seed = []
	existing_zones = {str(z.get("code") or "").upper() for z in current_zones}
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
def _build_trip_delivery_summary(trip):
	"""Shared trip summary payload for driver app + dispatcher (Rutas)."""
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

	dn_names = [cstr(s.get("delivery_note") or "").strip() for s in stops if s.get("delivery_note")]
	dn_names = [n for n in dn_names if n]
	dn_items_by_name = {}
	if dn_names:
		for dn_name in dn_names:
			try:
				frappe.flags.ignore_permissions = True
				dn = frappe.get_doc("Delivery Note", dn_name)
				frappe.flags.ignore_permissions = False
			except Exception:
				frappe.flags.ignore_permissions = False
				continue
			items = []
			for row in dn.items or []:
				qty = flt(row.qty)
				if qty <= 0:
					continue
				items.append(
					{
						"item_code": cstr(row.item_code),
						"item_name": cstr(row.item_name or row.item_code),
						"qty": qty,
						"uom": cstr(getattr(row, "uom", None) or getattr(row, "stock_uom", None) or ""),
					}
				)
			dn_items_by_name[dn_name] = items

	returns_by_stop = {}
	if frappe.db.exists("DocType", "Mobile Return Capture"):
		captures = frappe.get_all(
			"Mobile Return Capture",
			filters={"delivery_trip": trip.name},
			fields=["name", "stop_idx", "customer", "captured_at", "notes", "status"],
			order_by="stop_idx asc, creation asc",
			ignore_permissions=True,
		)
		for cap in captures:
			frappe.flags.ignore_permissions = True
			try:
				doc = frappe.get_doc("Mobile Return Capture", cap.name)
			except Exception:
				continue
			finally:
				frappe.flags.ignore_permissions = False
			lines = []
			for row in doc.lines or []:
				qty = flt(row.qty)
				if qty <= 0:
					continue
				lines.append(
					{
						"item_code": cstr(row.item_code or ""),
						"item_name": cstr(row.description or row.item_code or ""),
						"qty": qty,
						"reason": cstr(row.reason or ""),
					}
				)
			if not lines:
				continue
			idx = cint(cap.stop_idx)
			returns_by_stop.setdefault(idx, []).append(
				{
					"name": cap.name,
					"captured_at": cap.captured_at,
					"notes": cap.notes,
					"lines": lines,
				}
			)

	summary_stops = []
	total_planned = 0.0
	total_delivered = 0.0
	total_returned = 0.0
	total_amount_due = 0.0
	total_amount_collected = 0.0
	total_balance = 0.0
	for s in stops:
		dn = cstr(s.get("delivery_note") or "").strip()
		planned = list(dn_items_by_name.get(dn) or [])
		outcome = cstr(s.get("outcome") or "")
		visited = bool(s.get("visited"))
		delivered = []
		if visited and outcome in ("Delivered", "Partial"):
			delivered = [dict(it) for it in planned]
		returned_groups = returns_by_stop.get(cint(s.get("idx")), [])
		returned_flat = []
		for g in returned_groups:
			returned_flat.extend(g.get("lines") or [])

		planned_qty = sum(flt(i.get("qty")) for i in planned)
		delivered_qty = sum(flt(i.get("qty")) for i in delivered)
		returned_qty = sum(flt(i.get("qty")) for i in returned_flat)
		total_planned += planned_qty
		total_delivered += delivered_qty
		total_returned += returned_qty

		pod = s.get("pod") or {}
		amount_due = flt(pod.get("amount_due") if pod else None)
		if not amount_due:
			amount_due = flt(s.get("grand_total"))
		amount_collected = flt(pod.get("amount_collected") if pod else None)
		balance = pod.get("balance_after_stop") if pod else None
		if balance is None and (amount_due or amount_collected):
			balance = amount_due - amount_collected
		else:
			balance = flt(balance)
		if visited and outcome in ("Delivered", "Partial"):
			total_amount_due += amount_due
			total_amount_collected += amount_collected
			total_balance += balance

		summary_stops.append(
			{
				**s,
				"planned_items": planned,
				"delivered_items": delivered,
				"returned_items": returned_flat,
				"return_captures": returned_groups,
				"planned_qty": planned_qty,
				"delivered_qty": delivered_qty,
				"returned_qty": returned_qty,
				"amount_due": amount_due,
				"amount_collected": amount_collected,
				"balance_after_stop": balance,
				"payment_summary": pod.get("payment_summary") or pod.get("payment_method"),
				"requires_factura_a": bool(pod.get("requires_factura_a")),
				"factura_a_status": pod.get("factura_a_status"),
				"surcharge_amount": pod.get("surcharge_amount"),
				"payments": pod.get("payments") or [],
			}
		)

	return {
		"trip": {
			"name": trip.name,
			"status": trip.status,
			"departure_time": trip.departure_time,
			"driver": trip.driver,
			"driver_name": trip.driver_name,
			"vehicle": trip.vehicle,
		},
		"stops": summary_stops,
		"totals": {
			"planned_qty": total_planned,
			"delivered_qty": total_delivered,
			"returned_qty": total_returned,
			"stops_total": len(summary_stops),
			"stops_visited": sum(1 for s in summary_stops if s.get("visited")),
			"amount_due": total_amount_due,
			"amount_collected": total_amount_collected,
			"balance": total_balance,
		},
	}


@frappe.whitelist(allow_guest=True)
def get_trip_delivery_summary(trip_name=None):
	"""Dispatcher / Rutas: trip delivery summary (same payload as the driver app)."""
	trip_name = cstr(trip_name or "").strip()
	if not trip_name or trip_name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required."))
	if not frappe.db.exists("Delivery Trip", trip_name):
		frappe.throw(_("Delivery Trip {0} not found").format(trip_name), frappe.DoesNotExistError)
	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", trip_name)
	frappe.flags.ignore_permissions = False
	return _build_trip_delivery_summary(trip)


@frappe.whitelist()
def driver_get_trip_delivery_summary(trip_name=None):
	"""End-of-delivery / trip summary: planned, delivered, and returned items (all stops)."""
	trip_name = cstr(trip_name or "").strip()
	if not trip_name or trip_name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required."))

	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)
	return _build_trip_delivery_summary(trip)


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



def _ensure_delivery_payment_split_fields():
	"""Idempotent Delivery Stop / SO fields for multi-pay + Factura A + surcharge."""
	if frappe.db.has_column("Delivery Stop", "custom_payments_json"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
	from erpnext.patches.v15_0.add_tms_delivery_payment_split_fields import CUSTOM_FIELDS

	create_custom_fields(CUSTOM_FIELDS, update=True)
	frappe.clear_cache(doctype="Delivery Stop")
	frappe.clear_cache(doctype="Sales Order")


_TRANSFER_METHODS = {
	"transfer",
	"transferencia",
	"mobile money",
	"mobile_money",
	"bank transfer",
	"bank_transfer",
}


def _is_transfer_method(method) -> bool:
	return cstr(method or "").strip().lower() in _TRANSFER_METHODS


def _normalize_delivery_payments(payments=None, amount_collected=None, payment_method=None):
	"""Return list[{method, amount}] from JSON/list or legacy single mop+amount."""
	rows = []
	raw = payments
	if isinstance(raw, str):
		s = raw.strip()
		if s.lower() in ("", "null", "undefined", "none"):
			raw = None
		else:
			try:
				raw = frappe.parse_json(s)
			except Exception:
				raw = None
	if isinstance(raw, dict):
		raw = [raw]
	if isinstance(raw, (list, tuple)):
		for p in raw:
			if not isinstance(p, dict):
				continue
			amt = flt(p.get("amount") or p.get("paid_amount") or 0)
			method = cstr(p.get("method") or p.get("mode_of_payment") or p.get("payment_method") or "").strip()
			if amt <= 0:
				continue
			if not method:
				method = "Cash"
			rows.append({"method": method, "amount": round(amt, 2)})
	if not rows and amount_collected is not None:
		amt = flt(amount_collected)
		if amt > 0:
			rows.append(
				{
					"method": cstr(payment_method or "").strip() or "Cash",
					"amount": round(amt, 2),
				}
			)
	return rows


def _payment_summary_label(payments) -> str:
	parts = []
	for p in payments or []:
		method = cstr(p.get("method") or "")
		label = {
			"Cash": "Efectivo",
			"Transfer": "Transferencia",
			"Mobile Money": "Transferencia",
			"Other": "Otro",
		}.get(method, method or "Pago")
		parts.append(f"{label} ${flt(p.get('amount') or 0):,.2f}")
	return " + ".join(parts)


def _compute_transfer_surcharge(payments, *, requires_factura_a: bool, goods_total: float, settings: dict):
	"""Return (pct, base, amount, rule). Factura A alone → 0. Never stack both % knobs."""
	transfer_amt = sum(flt(p.get("amount") or 0) for p in (payments or []) if _is_transfer_method(p.get("method")))
	has_transfer = transfer_amt > 0.005
	goods = max(0.0, flt(goods_total))
	if requires_factura_a and has_transfer:
		pct = flt(settings.get("factura_a_with_transfer_surcharge_pct") or 0)
		base = goods
		rule = "factura_a_total"
	elif has_transfer:
		pct = flt(settings.get("transfer_surcharge_pct") or 0)
		base = transfer_amt
		rule = "transfer_portion"
	else:
		return 0.0, 0.0, 0.0, None
	if pct <= 0 or base <= 0:
		return pct, base, 0.0, rule
	amount = round(base * pct / 100.0, 2)
	return pct, base, amount, rule


def _write_stop_payment_snapshot(
	stop,
	*,
	payments,
	requires_factura_a: bool,
	surcharge_pct,
	surcharge_base,
	surcharge_amount,
	surcharge_rule,
):
	_ensure_delivery_payment_split_fields()
	summary = _payment_summary_label(payments)
	if frappe.db.has_column("Delivery Stop", "custom_payments_json"):
		stop.custom_payments_json = frappe.as_json(payments or [])
	if frappe.db.has_column("Delivery Stop", "custom_requires_factura_a"):
		stop.custom_requires_factura_a = 1 if requires_factura_a else 0
	if frappe.db.has_column("Delivery Stop", "custom_factura_a_status"):
		cur = cstr(getattr(stop, "custom_factura_a_status", None) or "").strip()
		if requires_factura_a:
			if cur not in ("issued",):
				stop.custom_factura_a_status = "pending"
		elif not cur:
			stop.custom_factura_a_status = "na"
	if frappe.db.has_column("Delivery Stop", "custom_surcharge_pct"):
		stop.custom_surcharge_pct = flt(surcharge_pct)
		stop.custom_surcharge_base = flt(surcharge_base)
		stop.custom_surcharge_amount = flt(surcharge_amount)
		stop.custom_surcharge_rule = cstr(surcharge_rule or "") or None
	if frappe.db.has_column("Delivery Stop", "custom_payment_summary"):
		stop.custom_payment_summary = summary or None
	# Legacy single mop = joined label for older UIs
	if payments:
		stop.custom_payment_method = " + ".join(
			cstr(p.get("method") or "Cash") for p in payments
		)[:140]
	return summary


def _sync_so_factura_and_surcharge_fields(
	so_names,
	*,
	requires_factura_a: bool,
	surcharge_amount: float,
	payment_summary: str | None,
	settings: dict,
):
	_ensure_delivery_payment_split_fields()
	mode = cstr(settings.get("surcharge_accounting_mode") or "so_line").strip().lower()
	for so_name in so_names or []:
		if not so_name or not frappe.db.exists("Sales Order", so_name):
			continue
		updates = {}
		if frappe.db.has_column("Sales Order", "custom_requires_factura_a"):
			updates["custom_requires_factura_a"] = 1 if requires_factura_a else 0
		if frappe.db.has_column("Sales Order", "custom_factura_a_status"):
			cur = frappe.db.get_value("Sales Order", so_name, "custom_factura_a_status")
			if requires_factura_a:
				if cstr(cur or "") != "issued":
					updates["custom_factura_a_status"] = "pending"
			elif not cur:
				updates["custom_factura_a_status"] = "na"
		if frappe.db.has_column("Sales Order", "custom_delivery_surcharge_amount"):
			updates["custom_delivery_surcharge_amount"] = flt(surcharge_amount)
		if frappe.db.has_column("Sales Order", "custom_delivery_payment_summary") and payment_summary:
			updates["custom_delivery_payment_summary"] = payment_summary
		if updates:
			frappe.db.set_value("Sales Order", so_name, updates, update_modified=True)
		if mode == "snapshot_only":
			continue
		if mode in ("so_line", "payment_entry") and flt(surcharge_amount) > 0.005:
			_append_so_payment_note(
				so_name,
				_("Recargo entrega {0} (modo {1})").format(flt(surcharge_amount), mode),
			)


def _ensure_late_penalty_fields():
	"""Idempotent Delivery Stop late-penalty columns (patch may not have run)."""
	if frappe.db.has_column("Delivery Stop", "custom_late_penalty_pct"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
	from erpnext.patches.v15_0.add_tms_late_penalty_fields import CUSTOM_FIELDS

	create_custom_fields(CUSTOM_FIELDS, update=True)
	frappe.clear_cache(doctype="Delivery Stop")


def _pending_penalty_amount(stop) -> float:
	if not frappe.db.has_column("Delivery Stop", "custom_late_penalty_amount"):
		return 0.0
	if cint(getattr(stop, "custom_late_penalty_applied", 0) or 0):
		return 0.0
	return max(0.0, flt(getattr(stop, "custom_late_penalty_amount", 0) or 0))


def _snapshot_late_penalty(stop, goods_balance: float, settings: dict) -> str | None:
	"""If underpaid and pct>0, snapshot penalty on the stop. Returns note text or None."""
	_ensure_late_penalty_fields()
	pct = flt(settings.get("late_payment_penalty_pct") or 0)
	if goods_balance <= 0.005 or pct <= 0:
		return None
	amount = round(goods_balance * pct / 100.0, 2)
	if amount <= 0:
		return None
	note = _(
		"Recargo {0}% sobre {1} por pago incompleto el {2} "
		"(pendiente próximo cobro: {3})"
	).format(pct, goods_balance, nowdate(), amount)
	stop.custom_late_penalty_pct = pct
	stop.custom_late_penalty_base = goods_balance
	stop.custom_late_penalty_amount = amount
	stop.custom_late_penalty_applied = 0
	stop.custom_late_penalty_note = note
	return note


def _clear_late_penalty(stop):
	if not frappe.db.has_column("Delivery Stop", "custom_late_penalty_pct"):
		return
	stop.custom_late_penalty_pct = 0
	stop.custom_late_penalty_base = 0
	stop.custom_late_penalty_amount = 0
	stop.custom_late_penalty_applied = 0
	stop.custom_late_penalty_note = None


def _append_so_payment_note(so_name: str, text: str):
	so_name = cstr(so_name or "").strip()
	text = cstr(text or "").strip()
	if not so_name or not text or not frappe.db.exists("Sales Order", so_name):
		return
	try:
		frappe.get_doc("Sales Order", so_name).add_comment("Info", text)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "delivery payment SO comment")


def _record_so_receive_payment(so_name: str, paid_amount, mode_of_payment=None) -> str | None:
	"""Create a Receive Payment Entry allocated to the Sales Order. Returns PE name."""
	paid_amount = flt(paid_amount)
	if paid_amount <= 0 or not so_name or not frappe.db.exists("Sales Order", so_name):
		return None
	from erpnext.erpnext_integrations.ecommerce_api.api import _resolve_preorder_payment_accounts

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	frappe.flags.ignore_permissions = False
	mop = cstr(mode_of_payment or "").strip() or "Cash"
	try:
		receivable_account, cash_account, mop = _resolve_preorder_payment_accounts(
			so.company, mop, so.customer
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "delivery payment accounts")
		receivable_account, cash_account = None, None

	if not receivable_account or not cash_account:
		# Still bump advance_paid so Completado (no pagado) can clear when paid in full.
		new_paid = flt(getattr(so, "advance_paid", 0) or 0) + paid_amount
		cap = flt(so.grand_total) or new_paid
		frappe.db.set_value(
			"Sales Order",
			so_name,
			"advance_paid",
			min(new_paid, cap) if cap > 0 else new_paid,
			update_modified=True,
		)
		return None

	outstanding = flt(so.grand_total) - flt(getattr(so, "advance_paid", 0) or 0)
	allocated = min(paid_amount, outstanding) if outstanding > 0 else paid_amount
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = so.company
	pe.party_type = "Customer"
	pe.party = so.customer
	pe.mode_of_payment = mop
	pe.paid_from = receivable_account
	pe.paid_to = cash_account
	pe.paid_from_account_currency = so.currency
	pe.paid_to_account_currency = so.currency
	pe.paid_amount = paid_amount
	pe.received_amount = paid_amount
	pe.reference_date = nowdate()
	pe.reference_no = f"TMS-{so_name}"
	if allocated > 0:
		pe.append(
			"references",
			{
				"reference_doctype": "Sales Order",
				"reference_name": so_name,
				"total_amount": flt(so.grand_total),
				"outstanding_amount": outstanding,
				"allocated_amount": allocated,
			},
		)
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name


def _mark_so_completado(so_name: str):
	"""Move SO to Completado workflow (display Completado / Completado no pagado via advance_paid)."""
	so_name = cstr(so_name or "").strip()
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_is_guest_preorder_sales_order,
		set_guest_preorder_status,
	)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	frappe.flags.ignore_permissions = False
	try:
		if _is_guest_preorder_sales_order(so):
			set_guest_preorder_status(so_name, "Completado", source="tms_pod")
		else:
			if cint(so.docstatus) == 1 and cstr(so.status) not in ("Completed", "Closed"):
				so.db_set("status", "Completed", update_modified=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "delivery mark Completado")


def _sync_stop_collection_to_sales_orders(
	stop,
	*,
	delta_collected: float,
	payment_method=None,
	payments=None,
	complete: bool,
	ledger_note: str | None = None,
	settings: dict | None = None,
	requires_factura_a: bool = False,
	surcharge_amount: float = 0.0,
	payment_summary: str | None = None,
):
	"""Book PE for cash taken + optionally Completado on linked SOs."""
	settings = settings or _load_tms_settings()
	if settings.get("sync_delivery_payment_to_so") is False:
		return
	dn = cstr(getattr(stop, "delivery_note", None) or "").strip()
	so_names = _sales_orders_for_dn(dn) if dn else []
	if not so_names:
		return
	_sync_so_factura_and_surcharge_fields(
		so_names,
		requires_factura_a=requires_factura_a,
		surcharge_amount=surcharge_amount,
		payment_summary=payment_summary,
		settings=settings,
	)
	pay_rows = list(payments or [])
	if not pay_rows and flt(delta_collected) > 0:
		pay_rows = [
			{
				"method": cstr(payment_method or "").strip() or "Cash",
				"amount": round(flt(delta_collected), 2),
			}
		]
	# Scale payment rows to delta when absolute snapshot was larger than this delta
	# (settle path may pass only the new delta as a single synthetic row).
	for i, so_name in enumerate(so_names):
		# Split each tender evenly across linked SOs (rare multi-SO DN).
		n = len(so_names)
		for pi, prow in enumerate(pay_rows):
			amt = flt(prow.get("amount") or 0)
			if amt <= 0:
				continue
			per = round(amt / n, 2)
			remainder = round(amt - per * n, 2)
			chunk = per + (remainder if i == 0 else 0.0)
			if chunk <= 0:
				continue
			try:
				_record_so_receive_payment(so_name, chunk, prow.get("method") or payment_method)
			except Exception:
				frappe.log_error(frappe.get_traceback(), "delivery PE sync")
		if ledger_note:
			_append_so_payment_note(so_name, ledger_note)
		if complete:
			_mark_so_completado(so_name)


def _apply_collection_and_penalty(
	stop,
	*,
	amount_due: float,
	prior_collected: float,
	delta_collected: float,
	payment_method=None,
	payments=None,
	settings: dict,
	complete_so: bool,
	requires_factura_a: bool = False,
	surcharge_amount: float = 0.0,
	payment_summary: str | None = None,
):
	"""Update stop payment fields, snapshot/apply late penalty, sync SO."""
	_ensure_late_penalty_fields()
	delta = max(0.0, flt(delta_collected))
	prior = max(0.0, flt(prior_collected))
	due = max(0.0, flt(amount_due))
	new_collected = prior + delta
	stop.custom_amount_due = due
	stop.custom_amount_collected = new_collected
	if payment_method:
		stop.custom_payment_method = payment_method

	goods_balance_before = max(0.0, due - prior)
	goods_balance_after = max(0.0, due - new_collected)
	penalty_pending_before = _pending_penalty_amount(stop)

	ledger_note = None
	if goods_balance_after > 0.005:
		# Still owing goods — (re)snapshot penalty from current goods balance if none applied yet.
		if not cint(getattr(stop, "custom_late_penalty_applied", 0) or 0):
			# First underpay: snapshot. Later underpays keep existing pending unless none set.
			if penalty_pending_before <= 0.005:
				ledger_note = _snapshot_late_penalty(stop, goods_balance_after, settings)
			else:
				ledger_note = cstr(getattr(stop, "custom_late_penalty_note", "") or "") or None
		stop.custom_balance_after_stop = goods_balance_after + _pending_penalty_amount(stop)
		stop.custom_cliente_debe = 1
	else:
		# Goods covered — allocate excess toward pending penalty.
		excess = max(0.0, new_collected - due)
		penalty = _pending_penalty_amount(stop)
		if penalty > 0.005:
			if excess + 0.005 >= penalty:
				stop.custom_late_penalty_applied = 1
				stop.custom_balance_after_stop = 0
				stop.custom_cliente_debe = 0
				ledger_note = _(
					"[cobro] Recargo {0} aplicado el {1}"
				).format(penalty, nowdate())
			else:
				remaining = round(penalty - excess, 2)
				stop.custom_balance_after_stop = remaining
				stop.custom_cliente_debe = 1
				ledger_note = _(
					"[cobro] Abonado a mercadería; recargo pendiente {0}"
				).format(remaining)
		else:
			_clear_late_penalty(stop)
			stop.custom_balance_after_stop = 0
			stop.custom_cliente_debe = 0

	if delta > 0.005 or ledger_note or complete_so:
		tag = _(
			"[cobro-entrega {0}] cobrado={1} due={2} balance={3}"
		).format(
			nowdate(),
			new_collected,
			due,
			flt(getattr(stop, "custom_balance_after_stop", 0) or 0),
		)
		if ledger_note and ledger_note not in tag:
			tag = f"{tag}\n{ledger_note}"
		_sync_stop_collection_to_sales_orders(
			stop,
			delta_collected=delta,
			payment_method=payment_method,
			payments=payments,
			complete=complete_so,
			ledger_note=tag,
			settings=settings,
			requires_factura_a=requires_factura_a,
			surcharge_amount=surcharge_amount,
			payment_summary=payment_summary,
		)


def _stop_pod_snapshot(stop):
	"""Flat PoD fields for Historial (Delivery Trip Version rows)."""
	return {
		"outcome": cstr(getattr(stop, "custom_outcome", None) or ""),
		"recipient_name": cstr(getattr(stop, "custom_pod_recipient_name", None) or ""),
		"recipient_id_number": cstr(getattr(stop, "custom_pod_recipient_id_number", None) or ""),
		"signature": cstr(getattr(stop, "custom_pod_signature", None) or ""),
		"notes": cstr(getattr(stop, "custom_pod_notes", None) or ""),
		"attempt_note": cstr(getattr(stop, "custom_attempt_note", None) or ""),
		"visited": "1" if cint(getattr(stop, "visited", 0)) else "0",
	}


def _log_stop_pod_audit(trip_name, stop_idx, before, after, source=None):
	"""Commit who/what PoD changes into Version for HistorialPanel."""
	prefix = f"stop_{cint(stop_idx)}"
	changes = []
	for key in (
		"outcome",
		"recipient_name",
		"recipient_id_number",
		"signature",
		"notes",
		"attempt_note",
		"visited",
	):
		old_v = before.get(key) or ""
		new_v = after.get(key) or ""
		if key == "signature":
			# Keep Historial readable — URLs are long private file paths.
			old_v = "signed" if old_v else ""
			new_v = "signed" if new_v else ""
		if old_v == new_v:
			continue
		changes.append((f"{prefix}.{key}", old_v, new_v))
	if changes and source:
		changes.append((f"{prefix}.via", "", cstr(source)))
	if not changes:
		return
	try:
		from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

		log_field_changes("Delivery Trip", trip_name, changes)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "tms stop pod audit")


def _record_stop_outcome_impl(
	trip,
	stop_idx,
	outcome,
	*,
	source="driver",
	require_signature=True,
	recipient_name=None,
	recipient_id_number=None,
	signature_base64=None,
	notes=None,
	attempt_note=None,
	lat=None,
	lng=None,
	amount_collected=None,
	payment_method=None,
	payments=None,
	requires_factura_a=None,
	credit_items=None,
	clear_signature=False,
):
	"""Shared PoD apply for driver + Tables→MATs admin. Always writes Historial."""
	trip_name = trip.name
	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	outcome_raw = cstr(outcome or "").strip()
	reset = outcome_raw.lower() in ("", "pending", "none", "null", "undefined")
	valid_outcomes = {"Delivered", "Not Home", "Partial", "Refused"}
	if not reset and outcome_raw not in valid_outcomes:
		frappe.throw(_("Invalid outcome: {0}").format(outcome_raw))

	before = _stop_pod_snapshot(stop)
	settings = _load_tms_settings()
	signature_required = bool(require_signature) and cstr(settings.get("require_signature") or "always") != "never"

	if reset:
		stop.custom_outcome = None
		stop.custom_pod_recipient_name = None
		stop.custom_pod_recipient_id_number = None
		stop.custom_pod_notes = None
		stop.custom_attempt_note = None
		stop.custom_pod_captured_at = None
		stop.custom_pod_captured_lat = None
		stop.custom_pod_captured_lng = None
		stop.visited = 0
		if clear_signature:
			stop.custom_pod_signature = None
		after = _stop_pod_snapshot(stop)
		_log_stop_pod_audit(trip_name, stop_idx, before, after, source=source)
		trip.flags.ignore_validate_update_after_submit = True
		trip.save(ignore_permissions=True)
		frappe.db.commit()
		payload = get_trip_map_data(trip_name)
		payload["tracking_code"] = None
		payload["credit_note"] = None
		return payload

	outcome = outcome_raw

	if require_signature:
		if outcome == "Delivered":
			if not recipient_name or not recipient_id_number:
				frappe.throw(_("Recipient name and ID/DNI are required to complete a delivery."))
			if signature_required and not signature_base64 and not getattr(stop, "custom_pod_signature", None):
				frappe.throw(_("A signature is required to complete a delivery."))
		elif outcome == "Partial":
			if not recipient_name or not recipient_id_number:
				frappe.throw(_("Recipient name and ID/DNI are required for a partial delivery."))
			if signature_required and not signature_base64 and not getattr(stop, "custom_pod_signature", None):
				frappe.throw(_("A signature is required for a partial delivery."))
		elif outcome == "Not Home":
			if not attempt_note:
				frappe.throw(_("A note is required to record a failed delivery attempt."))
		else:  # Refused
			if not notes:
				frappe.throw(_("A note is required for a {0} outcome.").format(outcome))
			if signature_required and not signature_base64 and not getattr(stop, "custom_pod_signature", None):
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
	elif clear_signature:
		stop.custom_pod_signature = None

	stop.custom_outcome = outcome
	if recipient_name is not None:
		stop.custom_pod_recipient_name = recipient_name
	if recipient_id_number is not None:
		stop.custom_pod_recipient_id_number = recipient_id_number
	if notes is not None:
		stop.custom_pod_notes = notes
	if attempt_note is not None:
		stop.custom_attempt_note = attempt_note
	stop.custom_pod_captured_at = now_datetime()
	stop.custom_pod_captured_lat = flt(lat) if lat else None
	stop.custom_pod_captured_lng = flt(lng) if lng else None
	stop.visited = 1 if outcome in ("Delivered", "Partial", "Refused") else 0

	# Payment: only relevant once something changed hands (Delivered/Partial).
	# "separate_collector" mode means someone else collects later, so the
	# driver isn't asked for these fields at all - ignore anything sent.
	if outcome in ("Delivered", "Partial"):
		goods_total = flt(stop.grand_total)
		prior = flt(getattr(stop, "custom_amount_collected", 0) or 0)
		# Factura A: explicit flag wins; else keep prior stop/SO flag.
		if requires_factura_a is None:
			req_fa = bool(cint(getattr(stop, "custom_requires_factura_a", 0) or 0))
		else:
			req_fa = _truthy_flag(requires_factura_a, default=False)
		pay_rows = _normalize_delivery_payments(
			payments, amount_collected=amount_collected, payment_method=payment_method
		)
		surch_pct, surch_base, surch_amt, surch_rule = _compute_transfer_surcharge(
			pay_rows,
			requires_factura_a=req_fa,
			goods_total=goods_total,
			settings=settings,
		)
		amount_due = goods_total + flt(surch_amt)
		summary = _write_stop_payment_snapshot(
			stop,
			payments=pay_rows,
			requires_factura_a=req_fa,
			surcharge_pct=surch_pct,
			surcharge_base=surch_base,
			surcharge_amount=surch_amt,
			surcharge_rule=surch_rule,
		)
		has_collection_input = (
			amount_collected is not None
			or payments is not None
			or (isinstance(payments, str) and cstr(payments).strip() not in ("", "null", "undefined"))
		)
		if settings.get("delivery_payment_mode") != "separate_collector" and has_collection_input:
			absolute = sum(flt(p.get("amount") or 0) for p in pay_rows)
			if amount_collected is not None and not pay_rows:
				absolute = max(0.0, flt(amount_collected))
			elif amount_collected is not None and pay_rows:
				# Prefer sum of splits; fall back to absolute if client sent both inconsistently.
				absolute = max(absolute, 0.0)
			delta = max(0.0, absolute - prior)
			# Only sync the new delta as payment rows (scale if absolute snapshot).
			delta_rows = pay_rows
			if prior > 0.005 and absolute > prior + 0.005 and pay_rows:
				# Fresh PoD usually prior=0; if settling atop prior, treat pay_rows as the delta.
				delta_rows = pay_rows
			elif absolute <= prior + 0.005:
				delta_rows = []
			_apply_collection_and_penalty(
				stop,
				amount_due=amount_due,
				prior_collected=prior,
				delta_collected=delta,
				payment_method=payment_method or (pay_rows[0]["method"] if pay_rows else None),
				payments=delta_rows if delta > 0.005 else [],
				settings=settings,
				complete_so=True,
				requires_factura_a=req_fa,
				surcharge_amount=surch_amt,
				payment_summary=summary,
			)
			stop.custom_amount_collected = absolute
			goods_bal = max(0.0, amount_due - absolute)
			pen = _pending_penalty_amount(stop)
			stop.custom_balance_after_stop = goods_bal + pen
			stop.custom_cliente_debe = 1 if stop.custom_balance_after_stop > 0.005 else 0
		else:
			stop.custom_amount_due = amount_due
			stop.custom_balance_after_stop = max(0.0, amount_due - prior)
			stop.custom_cliente_debe = 1 if stop.custom_balance_after_stop > 0.005 else 0
			if stop.custom_balance_after_stop > 0.005:
				_snapshot_late_penalty(stop, max(0.0, goods_total - prior), settings)
				pen = _pending_penalty_amount(stop)
				stop.custom_balance_after_stop = max(0.0, amount_due - prior) + pen
			_sync_stop_collection_to_sales_orders(
				stop,
				delta_collected=0,
				payment_method=None,
				payments=[],
				complete=True,
				ledger_note=_(
					"[entrega {0}] sin cobro en parada — pendiente {1}"
				).format(nowdate(), flt(stop.custom_balance_after_stop)),
				settings=settings,
				requires_factura_a=req_fa,
				surcharge_amount=surch_amt,
				payment_summary=summary,
			)

	after = _stop_pod_snapshot(stop)
	_log_stop_pod_audit(trip_name, stop_idx, before, after, source=source)

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)

	tracking_code = None
	if stop.delivery_note and frappe.db.has_column("Delivery Note", "custom_tracking_code"):
		tracking_code = _ensure_tracking_code(stop.delivery_note)

	credit_note = None
	if outcome == "Partial":
		raw_items = frappe.parse_json(credit_items) if isinstance(credit_items, str) else (credit_items or [])
		if isinstance(raw_items, list) and raw_items:
			credit_note = _process_partial_credit_note(
				stop, verify_shortfall_items=raw_items, notes=notes, settings=settings
			)

	frappe.db.commit()

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		evt = "delivered" if outcome == "Delivered" else "failed"
		emit_ecommerce_webhook(
			evt,
			{
				"trip": trip_name,
				"stop_idx": stop_idx,
				"outcome": outcome,
				"customer": stop.customer,
				"delivery_note": stop.delivery_note,
				"tracking_code": tracking_code,
				"credit_note": credit_note,
				"source": source,
			},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook stop outcome")

	payload = get_trip_map_data(trip_name)
	payload["tracking_code"] = tracking_code
	payload["credit_note"] = credit_note
	return payload


@frappe.whitelist()
@idempotent_request
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
	payments=None,
	requires_factura_a=None,
	credit_items=None,
):
	"""Replaces/extends `driver_complete_stop` with an outcome enum.

	- Delivered / Partial: recipient name/ID + signature required (same POD
	  fields). Clarification notes are optional; Partial may still carry an
	  auto-generated qty summary in notes.
	- Not Home: a note is required. Deliberately does NOT mark the stop
	  visited - the linked Delivery Note stays open for replanning (see
	  `_assigned_delivery_note_names`).
	- Refused: a note + signature are required. Marks visited.

	Every change is written to Delivery Trip Historial (who + via=driver).
	"""
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	# Driver starting field work is the publish signal — auto-submit Draft.
	trip = _ensure_trip_published_for_driving(trip)

	return _record_stop_outcome_impl(
		trip,
		stop_idx,
		outcome,
		source="driver",
		require_signature=True,
		recipient_name=recipient_name,
		recipient_id_number=recipient_id_number,
		signature_base64=signature_base64,
		notes=notes,
		attempt_note=attempt_note,
		lat=lat,
		lng=lng,
		amount_collected=amount_collected,
		payment_method=payment_method,
		payments=payments,
		requires_factura_a=requires_factura_a,
		credit_items=credit_items,
	)


@frappe.whitelist(allow_guest=True)
@idempotent_request
def admin_record_stop_outcome(
	trip_name=None,
	stop_idx=None,
	outcome=None,
	recipient_name=None,
	recipient_id_number=None,
	signature_base64=None,
	notes=None,
	attempt_note=None,
	lat=None,
	lng=None,
	amount_collected=None,
	payment_method=None,
	payments=None,
	requires_factura_a=None,
	credit_items=None,
	clear_signature=0,
):
	"""Tables→MATs admin PoD editor — no driver ownership check.

	Allows setting Delivered / Partial / Not Home / Refused / Pending, plus
	recipient / DNI-CUIT / signature / notes. Soft validation (admin may omit
	signature). Historial records ``via=admin`` and the session user.
	"""
	name = cstr(trip_name or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required"))
	if stop_idx is None or cstr(stop_idx).strip().lower() in ("", "null", "undefined", "none"):
		frappe.throw(_("Stop is required"))

	frappe.flags.ignore_permissions = True
	trip = frappe.get_doc("Delivery Trip", name)
	frappe.flags.ignore_permissions = False

	# Admin edits on a draft still publish so SO / remito side-effects stay consistent.
	if cint(trip.docstatus) == 0:
		trip = _ensure_trip_published_for_driving(trip)

	return _record_stop_outcome_impl(
		trip,
		stop_idx,
		outcome,
		source="admin",
		require_signature=False,
		recipient_name=recipient_name,
		recipient_id_number=recipient_id_number,
		signature_base64=signature_base64,
		notes=notes,
		attempt_note=attempt_note,
		lat=lat,
		lng=lng,
		amount_collected=amount_collected,
		payment_method=payment_method,
		payments=payments,
		requires_factura_a=requires_factura_a,
		credit_items=credit_items,
		clear_signature=cint(clear_signature),
	)


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

	fields = [
		"parent as trip_name",
		"idx",
		"customer",
		"customer_address",
		"delivery_note",
		"custom_amount_due",
		"custom_amount_collected",
		"custom_balance_after_stop",
	]
	if frappe.db.has_column("Delivery Stop", "custom_late_penalty_pct"):
		fields.extend(
			[
				"custom_late_penalty_pct",
				"custom_late_penalty_amount",
				"custom_late_penalty_applied",
				"custom_late_penalty_note",
			]
		)

	rows = frappe.get_all(
		"Delivery Stop",
		filters={**filters, "parent": ["in", trip_names]},
		fields=fields,
		ignore_permissions=True,
	)
	return {"stops": rows}


@frappe.whitelist()
@idempotent_request
def driver_settle_payment(
	trip_name,
	stop_idx,
	amount_collected=None,
	payment_method=None,
	payments=None,
	requires_factura_a=None,
):
	"""Record a later payment collection against an existing stop (the
	"separate collector" flow). Applies pending late-payment penalty when due."""
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	settings = _load_tms_settings()
	prior = flt(getattr(stop, "custom_amount_collected", 0) or 0)
	goods_total = flt(stop.grand_total or 0)
	if requires_factura_a is None:
		req_fa = bool(cint(getattr(stop, "custom_requires_factura_a", 0) or 0))
	else:
		req_fa = _truthy_flag(requires_factura_a, default=False)
	pay_rows = _normalize_delivery_payments(
		payments, amount_collected=amount_collected, payment_method=payment_method
	)
	surch_pct, surch_base, surch_amt, surch_rule = _compute_transfer_surcharge(
		pay_rows,
		requires_factura_a=req_fa,
		goods_total=goods_total,
		settings=settings,
	)
	# Keep prior due if already snapshotted higher; else goods + new surcharge.
	prior_due = flt(getattr(stop, "custom_amount_due", 0) or 0)
	due = max(prior_due, goods_total + flt(surch_amt))
	delta = sum(flt(p.get("amount") or 0) for p in pay_rows)
	if amount_collected is not None and not pay_rows:
		delta = max(0.0, flt(amount_collected))
	summary = _write_stop_payment_snapshot(
		stop,
		payments=pay_rows
		or _normalize_delivery_payments(
			getattr(stop, "custom_payments_json", None),
			amount_collected=prior + delta,
			payment_method=payment_method,
		),
		requires_factura_a=req_fa,
		surcharge_pct=surch_pct,
		surcharge_base=surch_base,
		surcharge_amount=surch_amt,
		surcharge_rule=surch_rule,
	)
	_apply_collection_and_penalty(
		stop,
		amount_due=due,
		prior_collected=prior,
		delta_collected=delta,
		payment_method=payment_method or (pay_rows[0]["method"] if pay_rows else None),
		payments=pay_rows,
		settings=settings,
		complete_so=True,
		requires_factura_a=req_fa,
		surcharge_amount=surch_amt,
		payment_summary=summary,
	)

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

	so_by_dn = {}
	if stop.delivery_note:
		for so in _sales_orders_for_dn(stop.delivery_note):
			so_by_dn.setdefault(stop.delivery_note, []).append(so)

	stop_out = _stop_out(stop, so_by_dn=so_by_dn)

	pod = stop_out.get("pod") or {}
	services = []
	if pod.get("requires_factura_a"):
		services.append({"code": "factura_a", "label": "Factura A", "amount": 0})
	services_summary = ", ".join(s["label"] for s in services) if services else None
	return {
		"doc": {
			**dn_data,
			"trip_name": trip.name,
			"driver_name": trip.driver_name,
			"stop_customer": stop.customer,
			"stop_address": stop.customer_address,
			**pod,
			"services": services,
			"services_summary": services_summary,
			"payment_summary": pod.get("payment_summary") or pod.get("payment_method"),
		},
		"lineItems": line_items,
		"payments": pod.get("payments") or [],
		"services": services,
	}


# ---------------------------------------------------------------------------
# In-premise returns
# ---------------------------------------------------------------------------

# Stable English codes — keep in sync with erpnext-ecommerce/lib/return-reasons.ts
# and Mobile Return Capture Item.reason Select options.
RETURN_REASON_OPTIONS = (
	"Damaged",
	"Expired",
	"Wrong item",
	"Quality issue",
	"Customer refusal",
	"Excess",
	"Other",
)


def _delivery_note_line_map(dn_name):
	"""item_code → {rate, qty, against_sales_invoice, warehouse, uom, item_name}."""
	out = {}
	if not dn_name or not frappe.db.exists("Delivery Note", dn_name):
		return out
	frappe.flags.ignore_permissions = True
	try:
		dn = frappe.get_doc("Delivery Note", dn_name)
	finally:
		frappe.flags.ignore_permissions = False
	for row in dn.items or []:
		code = cstr(row.item_code)
		if not code:
			continue
		prev = out.get(code)
		qty = flt(row.qty)
		rate = flt(row.rate)
		if prev:
			prev["qty"] = flt(prev.get("qty")) + qty
			continue
		out[code] = {
			"rate": rate,
			"qty": qty,
			"against_sales_invoice": cstr(getattr(row, "against_sales_invoice", None) or "") or None,
			"warehouse": getattr(row, "warehouse", None),
			"uom": getattr(row, "uom", None),
			"item_name": cstr(row.item_name or code),
		}
	return out


def _find_sales_invoice_for_dn(dn_name, customer=None):
	"""Best Sales Invoice to return-against for a Delivery Note."""
	line_map = _delivery_note_line_map(dn_name)
	for meta in line_map.values():
		si = meta.get("against_sales_invoice")
		if si and frappe.db.exists("Sales Invoice", si):
			return si
	# SI that references this DN on item rows.
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"delivery_note": dn_name, "docstatus": 1, "parenttype": "Sales Invoice"},
		fields=["parent"],
		limit=5,
		ignore_permissions=True,
	)
	for row in rows:
		si = row.parent
		if not si:
			continue
		is_return = cint(frappe.db.get_value("Sales Invoice", si, "is_return") or 0)
		if is_return:
			continue
		return si
	if customer:
		# Last resort: latest submitted non-return SI for customer (no DN link).
		si = frappe.db.get_value(
			"Sales Invoice",
			{"customer": customer, "docstatus": 1, "is_return": 0},
			"name",
			order_by="posting_date desc, creation desc",
		)
		return si
	return None


def _create_credit_note_for_items(customer, items, return_against=None, reason=None, reason_code="return"):
	"""Wrapper around CRM credit-note API (Sales Invoice is_return=1)."""
	from erpnext.erpnext_integrations.ecommerce_api.crm_party_api import create_party_credit_note

	return create_party_credit_note(
		customer=customer,
		return_against=return_against,
		items=items,
		reason=reason,
		reason_code=reason_code,
	)


def _create_dn_return_for_items(dn_name, items):
	"""Create + submit a Delivery Note return for the selected returned qtys."""
	if not dn_name or not items:
		return None
	from erpnext.controllers.sales_and_purchase_return import make_return_doc
	from erpnext.erpnext_integrations.ecommerce_api.api import _temporarily_allow_negative_stock

	wanted = {cstr(i.get("item_code")): abs(flt(i.get("qty"))) for i in items if i.get("item_code")}
	if not wanted:
		return None
	doc = make_return_doc("Delivery Note", dn_name)
	kept = []
	for row in list(doc.items or []):
		code = cstr(row.item_code)
		if code not in wanted:
			continue
		max_q = abs(flt(row.qty))
		q = min(wanted[code], max_q) if max_q else wanted[code]
		if q <= 0:
			continue
		row.qty = -q
		kept.append(row)
		wanted[code] = max(0.0, wanted[code] - q)
	if not kept:
		return None
	doc.set("items", [])
	for row in kept:
		doc.append(
			"items",
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"qty": row.qty,
				"rate": row.rate,
				"uom": row.uom,
				"stock_uom": row.stock_uom,
				"conversion_factor": row.conversion_factor,
				"warehouse": row.warehouse,
				"against_sales_order": getattr(row, "against_sales_order", None),
				"so_detail": getattr(row, "so_detail", None),
				"against_sales_invoice": getattr(row, "against_sales_invoice", None),
				"si_detail": getattr(row, "si_detail", None),
			},
		)
	doc.insert(ignore_permissions=True)
	with _temporarily_allow_negative_stock():
		doc.submit()
	return doc.name


def _capture_set(capture_name, **kwargs):
	"""Set optional Mobile Return Capture fields when columns exist."""
	for field, value in kwargs.items():
		if value is None:
			continue
		if frappe.db.has_column("Mobile Return Capture", field):
			frappe.db.set_value("Mobile Return Capture", capture_name, field, value, update_modified=False)


def _process_return_accounting(capture, stop, settings=None):
	"""Create credit note (+ DN return) from a Mobile Return Capture. Best-effort."""
	settings = settings or _load_tms_settings()
	result = {
		"credit_note": None,
		"debit_note": None,
		"delivery_note_return": None,
		"process_error": None,
	}
	if not settings.get("auto_credit_note_on_return", True):
		return result

	dn_name = cstr(getattr(stop, "delivery_note", None) or "").strip() or None
	customer = cstr(capture.customer or getattr(stop, "customer", None) or "").strip()
	lines = []
	line_map = _delivery_note_line_map(dn_name) if dn_name else {}
	reason_codes = []
	for row in capture.lines or []:
		code = cstr(row.item_code or "").strip()
		qty = flt(row.qty)
		if not code or qty <= 0:
			continue
		meta = line_map.get(code) or {}
		rate = flt(meta.get("rate") or 0)
		lines.append(
			{
				"item_code": code,
				"item_name": cstr(row.description or meta.get("item_name") or code),
				"qty": qty,
				"rate": rate,
			}
		)
		if getattr(row, "reason", None):
			reason_codes.append(cstr(row.reason))
	if not lines:
		return result

	_capture_set(capture.name, delivery_note=dn_name)

	try:
		if dn_name:
			dn_ret = _create_dn_return_for_items(dn_name, lines)
			if dn_ret:
				result["delivery_note_return"] = dn_ret
				_capture_set(capture.name, delivery_note_return=dn_ret)

		return_against = _find_sales_invoice_for_dn(dn_name, customer) if dn_name else None
		# Credit note needs rates when not linked to an invoice mapper.
		if not return_against:
			for line in lines:
				if flt(line.get("rate")) <= 0:
					# Fall back to Item price / valuation 0 is invalid for standalone CN.
					line["rate"] = flt(
						frappe.db.get_value("Item Price", {"item_code": line["item_code"]}, "price_list_rate")
						or 0
					)
		reason_text = cstr(capture.notes or "").strip() or None
		reason_code = reason_codes[0] if reason_codes else "return"
		cn = _create_credit_note_for_items(
			customer=customer,
			items=lines,
			return_against=return_against,
			reason=reason_text,
			reason_code=reason_code,
		)
		cn_name = (cn or {}).get("invoice_id")
		if cn_name:
			result["credit_note"] = cn_name
			_capture_set(capture.name, credit_note=cn_name, status="Processed", process_error="")
		else:
			_capture_set(capture.name, status="Processed")
	except Exception as exc:
		msg = cstr(exc)[:500]
		result["process_error"] = msg
		_capture_set(capture.name, status="Error", process_error=msg)
		frappe.log_error(frappe.get_traceback(), "TMS return credit note")

	return result


def _process_partial_credit_note(stop, verify_shortfall_items=None, notes=None, settings=None):
	"""Credit undelivered qty on Partial when items were already invoiced."""
	settings = settings or _load_tms_settings()
	if not settings.get("auto_credit_note_on_partial", True):
		return None
	dn_name = cstr(getattr(stop, "delivery_note", None) or "").strip()
	customer = cstr(getattr(stop, "customer", None) or "").strip()
	if not dn_name or not customer:
		return None
	items = verify_shortfall_items or []
	if not items:
		return None
	line_map = _delivery_note_line_map(dn_name)
	clean = []
	for raw in items:
		code = cstr(raw.get("item_code") or "").strip()
		qty = abs(flt(raw.get("qty")))
		if not code or qty <= 0:
			continue
		meta = line_map.get(code) or {}
		clean.append(
			{
				"item_code": code,
				"item_name": cstr(raw.get("item_name") or meta.get("item_name") or code),
				"qty": qty,
				"rate": flt(raw.get("rate") or meta.get("rate") or 0),
			}
		)
	if not clean:
		return None
	return_against = _find_sales_invoice_for_dn(dn_name, customer)
	try:
		cn = _create_credit_note_for_items(
			customer=customer,
			items=clean,
			return_against=return_against,
			reason=cstr(notes or "")[:200] or "Partial delivery shortfall",
			reason_code="partial",
		)
		return (cn or {}).get("invoice_id")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "TMS partial credit note")
		return None


def _normalize_return_lines(lines):
	"""Validate/coerce return lines. Qty may be fractional (WEIGHT kg/L)."""
	raw = frappe.parse_json(lines) if isinstance(lines, str) else (lines or [])
	if not isinstance(raw, (list, tuple)):
		frappe.throw(_("Returned items must be a list."))
	normalized = []
	for line in raw:
		if not isinstance(line, dict):
			continue
		item_code = cstr(line.get("item_code") or "").strip()
		if not item_code:
			continue
		if not frappe.db.exists("Item", item_code):
			frappe.throw(_("Item {0} does not exist.").format(item_code), frappe.DoesNotExistError)
		qty = flt(line.get("qty"))
		if qty <= 0:
			frappe.throw(_("Quantity for {0} must be greater than zero.").format(item_code))
		reason = cstr(line.get("reason") or "").strip()
		if reason not in RETURN_REASON_OPTIONS:
			frappe.throw(
				_("Invalid return reason for {0}. Choose one of: {1}.").format(
					item_code, ", ".join(RETURN_REASON_OPTIONS)
				)
			)
		description = cstr(line.get("description") or "").strip()
		if not description:
			description = cstr(frappe.db.get_value("Item", item_code, "item_name") or item_code)
		normalized.append(
			{
				"item_code": item_code,
				"description": description,
				"qty": qty,
				"reason": reason,
			}
		)
	if not normalized:
		frappe.throw(_("Add at least one returned item."))
	return normalized


@frappe.whitelist()
@idempotent_request
def driver_record_return_capture(trip_name, stop_idx, lines, signature_base64, notes=None, photo_base64_list=None):
	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)

	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	normalized_lines = _normalize_return_lines(lines)
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
			"lines": normalized_lines,
			**(
				{"delivery_note": stop.delivery_note}
				if stop.delivery_note and frappe.db.has_column("Mobile Return Capture", "delivery_note")
				else {}
			),
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

	accounting = _process_return_accounting(capture, stop)
	frappe.db.commit()
	return {
		"name": capture.name,
		"status": frappe.db.get_value("Mobile Return Capture", capture.name, "status") or capture.status,
		"credit_note": accounting.get("credit_note"),
		"debit_note": accounting.get("debit_note"),
		"delivery_note_return": accounting.get("delivery_note_return"),
		"process_error": accounting.get("process_error"),
	}


@frappe.whitelist()
def driver_get_stop_returns(trip_name=None, stop_idx=None):
	"""Return captures for one trip stop (for delivery receipt / Completar Entrega)."""
	trip_name = cstr(trip_name or "").strip()
	if not trip_name or trip_name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Trip is required."))
	if stop_idx is None or cstr(stop_idx).strip().lower() in ("", "null", "undefined", "none"):
		frappe.throw(_("Stop is required."))

	driver = _get_current_driver()
	trip = _require_owned_trip(trip_name, driver)
	stop_idx = cint(stop_idx)
	stop = next((s for s in trip.delivery_stops if s.idx == stop_idx), None)
	if not stop:
		frappe.throw(_("Stop {0} not found on this trip").format(stop_idx), frappe.DoesNotExistError)

	tracking_code = None
	dn = cstr(stop.delivery_note or "").strip()
	if dn and frappe.db.has_column("Delivery Note", "custom_tracking_code"):
		tracking_code = _ensure_tracking_code(dn)

	returns = []
	if frappe.db.exists("DocType", "Mobile Return Capture"):
		captures = frappe.get_all(
			"Mobile Return Capture",
			filters={"delivery_trip": trip_name, "stop_idx": stop_idx},
			fields=["name", "captured_at", "notes", "status"],
			order_by="creation asc",
			ignore_permissions=True,
		)
		for cap in captures:
			frappe.flags.ignore_permissions = True
			try:
				doc = frappe.get_doc("Mobile Return Capture", cap.name)
			except Exception:
				continue
			finally:
				frappe.flags.ignore_permissions = False
			lines = []
			for row in doc.lines or []:
				qty = flt(row.qty)
				if qty <= 0:
					continue
				lines.append(
					{
						"item_code": cstr(row.item_code or ""),
						"item_name": cstr(row.description or row.item_code or ""),
						"qty": qty,
						"reason": cstr(row.reason or ""),
					}
				)
			if not lines:
				continue
			returns.append(
				{
					"name": cap.name,
					"captured_at": cap.captured_at,
					"notes": cap.notes,
					"status": cap.status,
					"credit_note": getattr(doc, "credit_note", None),
					"debit_note": getattr(doc, "debit_note", None),
					"delivery_note_return": getattr(doc, "delivery_note_return", None),
					"lines": lines,
				}
			)

	return {
		"trip_name": trip.name,
		"stop_idx": stop_idx,
		"delivery_note": dn or None,
		"tracking_code": tracking_code,
		"returns": returns,
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

	# Photo upload also means the driver is working the route — auto-publish.
	trip = _ensure_trip_published_for_driving(trip)

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

	try:
		from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

		log_field_changes(
			"Delivery Trip",
			trip_name,
			[
				(f"stop_{stop_idx}.photo", "", f"+1 ({len(existing)})"),
				(f"stop_{stop_idx}.via", "", "driver"),
			],
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "tms stop photo audit")

	trip.flags.ignore_validate_update_after_submit = True
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return get_trip_map_data(trip_name)
