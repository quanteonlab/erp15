"""Tagged presentation demo pack: sample orders + fake team, wipeable from Settings.

Only rows with ``custom_demo_pack = presentation`` are created / cleared.
Starter Employee Groups (repositor / caja / ventas / driver / admin) are never wiped.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, today


FIELDNAME = "custom_demo_pack"
PACK_VALUE = "presentation"
_FIELDS_READY = False

# Stable names so seed is idempotent across reloads.
_DEMO_CUSTOMERS = [
	{"name": "Demo · Mercado Palermo", "phone": "+5491100001001", "email": "demo.palermo@example.invalid"},
	{"name": "Demo · Bodega Sur", "phone": "+5491100001002", "email": "demo.bodega@example.invalid"},
	{"name": "Demo · Café Norte", "phone": "+5491100001003", "email": "demo.cafe@example.invalid"},
	{"name": "Demo · Distribuidora Centro", "phone": "+5491100001004", "email": "demo.centro@example.invalid"},
]

_DEMO_TEAM = [
	{
		"employee_name": "Demo · Ana Caja",
		"first_name": "Ana",
		"last_name": "Caja",
		"email": "demo.caja@example.invalid",
		"groups": ["caja"],
		"pin": "111111",
	},
	{
		"employee_name": "Demo · Bruno Ventas",
		"first_name": "Bruno",
		"last_name": "Ventas",
		"email": "demo.ventas@example.invalid",
		"groups": ["ventas"],
		"pin": "222222",
	},
	{
		"employee_name": "Demo · Carla Repositor",
		"first_name": "Carla",
		"last_name": "Repositor",
		"email": "demo.repositor@example.invalid",
		"groups": ["repositor"],
		"pin": "333333",
	},
	{
		"employee_name": "Demo · Diego Conductor",
		"first_name": "Diego",
		"last_name": "Conductor",
		"email": "demo.conductor@example.invalid",
		"groups": ["driver"],
		"pin": "444444",
	},
]

# (customer index, target status, guest_name suffix) — ~10 sample orders
_DEMO_ORDERS = [
	(0, "Consulta", "Consulta A"),
	(1, "Consulta", "Consulta B"),
	(2, "Consulta", "Consulta C"),
	(0, "Orden", "Orden A"),
	(1, "Orden", "Orden B"),
	(3, "Orden", "Orden C"),
	(0, "Preparado", "Preparado A"),
	(1, "Preparado", "Preparado B"),
	(2, "Preparado", "Preparado C"),
	(3, "Preparado", "Preparado D"),
]

_DEMO_ITEM_CODES = [
	"DEMO-PACK-SKU-01",
	"DEMO-PACK-SKU-02",
	"DEMO-PACK-SKU-03",
]


def ensure_demo_pack_custom_fields():
	"""Idempotent Select/Data field on Sales Order, Customer, Employee."""
	global _FIELDS_READY
	targets = ("Sales Order", "Customer", "Employee")
	if all(frappe.db.has_column(dt, FIELDNAME) for dt in targets):
		_FIELDS_READY = True
		return
	if _FIELDS_READY:
		return

	defs = (
		(
			"Customer",
			{
				"fieldname": FIELDNAME,
				"label": "Demo Pack",
				"fieldtype": "Data",
				"insert_after": "disabled",
				"read_only": 1,
				"hidden": 1,
			},
		),
		(
			"Sales Order",
			{
				"fieldname": FIELDNAME,
				"label": "Demo Pack",
				"fieldtype": "Data",
				"insert_after": "status",
				"allow_on_submit": 1,
				"read_only": 1,
				"hidden": 1,
			},
		),
		(
			"Employee",
			{
				"fieldname": FIELDNAME,
				"label": "Demo Pack",
				"fieldtype": "Data",
				"insert_after": "status",
				"read_only": 1,
				"hidden": 1,
			},
		),
	)

	created = False
	for doctype, df in defs:
		if frappe.db.has_column(doctype, FIELDNAME):
			continue
		if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": FIELDNAME}):
			continue
		doc = frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **df})
		doc.insert(ignore_permissions=True)
		created = True

	if created:
		frappe.db.commit()
		for dt in targets:
			frappe.clear_cache(doctype=dt)

	_FIELDS_READY = all(frappe.db.has_column(dt, FIELDNAME) for dt in targets)


def _tag(doctype: str, name: str) -> None:
	if not name or not frappe.db.has_column(doctype, FIELDNAME):
		return
	frappe.db.set_value(doctype, name, FIELDNAME, PACK_VALUE, update_modified=False)


def _pack_names(doctype: str) -> list[str]:
	ensure_demo_pack_custom_fields()
	if not frappe.db.has_column(doctype, FIELDNAME):
		return []
	return frappe.get_all(
		doctype,
		filters={FIELDNAME: PACK_VALUE},
		pluck="name",
		ignore_permissions=True,
	) or []


def _require_admin_pin(pin) -> None:
	pin = cstr(pin or "").strip()
	if not pin:
		frappe.throw(_("Admin PIN required"), frappe.AuthenticationError)
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import validate_admin_pin

	res = validate_admin_pin(pin)
	if not (isinstance(res, dict) and res.get("authorized")):
		frappe.throw(_("Incorrect admin PIN"), frappe.AuthenticationError)


def _resolve_company() -> str:
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	company = resolve_company() or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("No Company configured"))
	return company


def _ensure_demo_items() -> list[str]:
	"""Reuse a few catalog items when present; else create non-stock demo SKUs."""
	existing = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["name"],
		order_by="modified desc",
		limit_page_length=5,
		ignore_permissions=True,
	)
	codes = [r.name for r in existing if r.name and not str(r.name).startswith("DEMO-PACK-")]
	if len(codes) >= 3:
		return codes[:3]

	item_group = (
		frappe.db.get_single_value("Stock Settings", "item_group")
		or (frappe.db.exists("Item Group", "Products") and "Products")
		or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or "All Item Groups"
	)
	out = []
	for i, code in enumerate(_DEMO_ITEM_CODES):
		if not frappe.db.exists("Item", code):
			doc = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": f"Demo pack item {i + 1}",
					"item_group": item_group,
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"include_item_in_manufacturing": 0,
					"disabled": 0,
				}
			)
			doc.insert(ignore_permissions=True)
		out.append(code)
	return out


def _ensure_demo_customers() -> list[str]:
	from erpnext.erpnext_integrations.ecommerce_api.api import create_customer

	names = []
	for spec in _DEMO_CUSTOMERS:
		label = spec["name"]
		existing = frappe.db.get_value("Customer", {"customer_name": label}, "name")
		if existing and frappe.db.get_value("Customer", existing, FIELDNAME) == PACK_VALUE:
			names.append(existing)
			continue
		if existing:
			_tag("Customer", existing)
			names.append(existing)
			continue
		row = create_customer(
			customer_name=label,
			phone=spec["phone"],
			email=spec["email"],
		)
		name = row.get("name") if isinstance(row, dict) else cstr(row)
		if name:
			_tag("Customer", name)
			names.append(name)
	return names


def _set_ops_pin(employee: str, pin: str) -> None:
	"""Assign a fixed 6-digit ops PIN for demo kiosk identity."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_load_ops_pin_store,
		_normalize_ops_pin,
		_save_ops_pin_store,
	)

	raw = _normalize_ops_pin(pin)
	if not raw or not employee:
		return
	store = _load_ops_pin_store()
	by_employee = dict(store.get("by_employee") or {})
	by_pin = dict(store.get("by_pin") or {})
	# Free previous pin for this employee and any occupant of the target pin.
	prev = by_employee.get(employee) if isinstance(by_employee.get(employee), dict) else None
	prev_pin = str((prev or {}).get("pin") or "").strip()
	if prev_pin and prev_pin in by_pin:
		by_pin.pop(prev_pin, None)
	other = by_pin.get(raw)
	if other and other != employee:
		by_employee.pop(other, None)
	by_employee[employee] = {"pin": raw}
	by_pin[raw] = employee
	_save_ops_pin_store({"by_employee": by_employee, "by_pin": by_pin})


def _ensure_demo_team(company: str) -> list[str]:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_set_employee_groups,
		_set_user_roles,
		_set_user_password,
		ensure_starter_staff_groups,
	)

	ensure_starter_staff_groups()
	employee_names = []

	for spec in _DEMO_TEAM:
		email = spec["email"]
		existing_emp = frappe.db.get_value(
			"Employee",
			{FIELDNAME: PACK_VALUE, "employee_name": spec["employee_name"]},
			"name",
		)
		if not existing_emp:
			# Also match by company_email if a prior seed used a different filter.
			existing_emp = frappe.db.get_value(
				"Employee",
				{"company_email": email, FIELDNAME: PACK_VALUE},
				"name",
			)

		if existing_emp:
			_set_employee_groups(existing_emp, spec["groups"])
			_set_ops_pin(existing_emp, spec["pin"])
			employee_names.append(existing_emp)
			continue

		emp = frappe.new_doc("Employee")
		emp.company = company
		emp.first_name = spec["first_name"]
		emp.last_name = spec["last_name"]
		emp.employee_name = spec["employee_name"]
		emp.status = "Active"
		emp.date_of_joining = today()
		emp.gender = "Prefer not to say"
		emp.date_of_birth = "1990-01-01"
		emp.company_email = email
		emp.prefered_email = email
		emp.prefered_contact_email = "Company Email"
		if frappe.db.has_column("Employee", FIELDNAME):
			emp.set(FIELDNAME, PACK_VALUE)
		emp.insert(ignore_permissions=True)
		_tag("Employee", emp.name)

		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": spec["first_name"],
					"last_name": spec["last_name"],
					"enabled": 1,
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			)
			user.flags.ignore_password_policy = True
			user.flags.no_welcome_mail = True
			user.insert(ignore_permissions=True)
			_set_user_password(email, "DemoPack!2026")
		_set_user_roles(email, ["Employee", "Sales User"])

		emp.user_id = email
		emp.create_user_permission = 0
		emp.save(ignore_permissions=True)
		_set_employee_groups(emp.name, spec["groups"])
		_set_ops_pin(emp.name, spec["pin"])
		employee_names.append(emp.name)

	return employee_names


def _demo_order_already_seeded() -> bool:
	"""True when we already have roughly a full pack of tagged orders."""
	return len(_pack_names("Sales Order")) >= len(_DEMO_ORDERS)


def _ensure_demo_orders(customers: list[str], item_codes: list[str]) -> list[str]:
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		create_guest_preorder,
		set_guest_preorder_status,
	)

	if _demo_order_already_seeded():
		return _pack_names("Sales Order")

	created = []
	for cust_idx, status, label in _DEMO_ORDERS:
		customer = customers[cust_idx % len(customers)]
		sku = item_codes[cust_idx % len(item_codes)]
		items = [{"item_code": sku, "qty": 1 + (cust_idx % 3), "rate": 1000 + cust_idx * 50}]
		try:
			out = create_guest_preorder(
				items=items,
				customer=customer,
				guest_name=f"Demo · {label}",
				guest_phone=f"+5491100002{cust_idx:03d}",
				guest_notes=f"presentation_demo={PACK_VALUE}",
				initial_status="Consulta",
			)
		except Exception as e:
			frappe.log_error(f"presentation demo order failed: {e}", "presentation_demo")
			continue
		name = out.get("preorder_name") if isinstance(out, dict) else None
		if not name:
			continue
		_tag("Sales Order", name)
		if status != "Consulta":
			try:
				set_guest_preorder_status(
					name,
					status,
					ensure_planner_remito=0,
				)
				_tag("Sales Order", name)
			except Exception as e:
				frappe.log_error(
					f"presentation demo promote {name}→{status}: {e}",
					"presentation_demo",
				)
		created.append(name)
	return created


@frappe.whitelist(allow_guest=True)
def presentation_demo_status():
	"""Counts for Settings UI: seeded flag + tagged row totals."""
	ensure_demo_pack_custom_fields()
	orders = len(_pack_names("Sales Order"))
	employees = len(_pack_names("Employee"))
	customers = len(_pack_names("Customer"))
	return {
		"ok": True,
		"seeded": bool(orders or employees or customers),
		"orders": orders,
		"employees": employees,
		"customers": customers,
		"pack": PACK_VALUE,
	}


@frappe.whitelist(allow_guest=True)
def clear_presentation_demo(pin=None):
	"""Wipe tagged presentation demo rows only. Requires admin PIN."""
	_require_admin_pin(pin)
	ensure_demo_pack_custom_fields()

	orders_removed = 0
	employees_removed = 0
	customers_removed = 0
	users_removed = 0

	# 1) Sales Orders (cancel submitted, then delete all tagged)
	for so_name in _pack_names("Sales Order"):
		try:
			# Drop contact/address links so Contact docs do not block SO delete.
			updates = {}
			for field in ("contact_person", "customer_address", "shipping_address_name"):
				if frappe.db.has_column("Sales Order", field):
					updates[field] = None
			if updates:
				frappe.db.set_value("Sales Order", so_name, updates, update_modified=False)
			frappe.flags.ignore_permissions = True
			so = frappe.get_doc("Sales Order", so_name)
			frappe.flags.ignore_permissions = False
			ds = cint(so.docstatus)
			if ds == 1:
				so.flags.ignore_permissions = True
				so.cancel()
			if frappe.db.exists("Sales Order", so_name):
				frappe.delete_doc("Sales Order", so_name, ignore_permissions=True, force=True)
			orders_removed += 1
		except Exception as e:
			frappe.log_error(title="presentation_demo clear SO", message=f"{so_name}: {e}")

	# Linked remitos for demo customers (best-effort; pack itself does not create them)
	demo_customers = _pack_names("Customer")
	if demo_customers:
		for dn in frappe.get_all(
			"Delivery Note",
			filters={"customer": ["in", demo_customers]},
			pluck="name",
			ignore_permissions=True,
		):
			try:
				ds = cint(frappe.db.get_value("Delivery Note", dn, "docstatus"))
				if ds == 1:
					frappe.db.set_value("Delivery Note", dn, "docstatus", 2)
				frappe.delete_doc("Delivery Note", dn, ignore_permissions=True, force=True)
			except Exception:
				pass

	# 2) Employees + linked demo users
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_load_ops_pin_store,
		_save_ops_pin_store,
		_set_employee_groups,
	)

	pin_store = _load_ops_pin_store()
	by_employee = dict(pin_store.get("by_employee") or {})
	by_pin = dict(pin_store.get("by_pin") or {})
	pins_changed = False

	for emp_name in _pack_names("Employee"):
		try:
			user_id = frappe.db.get_value("Employee", emp_name, "user_id")
			_set_employee_groups(emp_name, [])
			if user_id:
				frappe.db.set_value("Employee", emp_name, "user_id", None, update_modified=False)
			entry = by_employee.pop(emp_name, None)
			if isinstance(entry, dict):
				p = str(entry.get("pin") or "").strip()
				if p and by_pin.get(p) == emp_name:
					by_pin.pop(p, None)
					pins_changed = True
			elif emp_name in by_employee:
				pins_changed = True
			frappe.delete_doc("Employee", emp_name, ignore_permissions=True, force=True)
			employees_removed += 1
			if user_id and user_id not in ("Administrator", "Guest") and str(user_id).endswith(
				"@example.invalid"
			):
				if frappe.db.exists("User", user_id):
					frappe.delete_doc("User", user_id, ignore_permissions=True, force=True)
					users_removed += 1
		except Exception as e:
			frappe.log_error(title="presentation_demo clear Emp", message=f"{emp_name}: {e}")

	if pins_changed or by_employee != (pin_store.get("by_employee") or {}):
		_save_ops_pin_store({"by_employee": by_employee, "by_pin": by_pin})

	# 3) Customers — only if no remaining untagged Sales Orders
	for cust in _pack_names("Customer"):
		try:
			untagged = frappe.db.sql(
				"""
				SELECT name FROM `tabSales Order`
				WHERE customer = %s
				  AND IFNULL(`custom_demo_pack`, '') != %s
				LIMIT 1
				""",
				(cust, PACK_VALUE),
			)
			if untagged:
				frappe.db.set_value("Customer", cust, FIELDNAME, "", update_modified=False)
				continue
			# Drop leftover cancelled/tagged SOs that still link via Contact
			for leftover in frappe.get_all(
				"Sales Order",
				filters={"customer": cust},
				pluck="name",
				ignore_permissions=True,
			):
				try:
					ds = cint(frappe.db.get_value("Sales Order", leftover, "docstatus"))
					if ds == 1:
						frappe.flags.ignore_permissions = True
						so = frappe.get_doc("Sales Order", leftover)
						frappe.flags.ignore_permissions = False
						so.flags.ignore_permissions = True
						so.cancel()
					frappe.delete_doc("Sales Order", leftover, ignore_permissions=True, force=True)
				except Exception:
					pass
			_unlink_demo_contacts(cust)
			frappe.delete_doc("Customer", cust, ignore_permissions=True, force=True)
			customers_removed += 1
		except Exception as e:
			frappe.log_error(title="presentation_demo clear Cust", message=f"{cust}: {e}")

	frappe.db.commit()
	return {
		"ok": True,
		"orders_removed": orders_removed,
		"employees_removed": employees_removed,
		"customers_removed": customers_removed,
		"users_removed": users_removed,
	}


def _unlink_demo_contacts(customer: str) -> None:
	"""Remove Dynamic Links / Contacts created for a demo customer so delete can proceed."""
	links = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		fields=["name", "parent"],
		ignore_permissions=True,
	)
	for row in links:
		try:
			frappe.delete_doc("Dynamic Link", row.name, ignore_permissions=True, force=True)
		except Exception:
			pass
		parent = row.parent
		if parent and frappe.db.exists("Contact", parent):
			# If contact has no other links, delete it
			remaining = frappe.get_all(
				"Dynamic Link",
				filters={"parent": parent, "parenttype": "Contact"},
				limit_page_length=1,
				ignore_permissions=True,
			)
			if not remaining:
				try:
					frappe.delete_doc("Contact", parent, ignore_permissions=True, force=True)
				except Exception:
					pass

	# Addresses linked only to this customer
	addr_links = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		fields=["name", "parent"],
		ignore_permissions=True,
	)
	for row in addr_links:
		try:
			frappe.delete_doc("Dynamic Link", row.name, ignore_permissions=True, force=True)
		except Exception:
			pass
		if row.parent and frappe.db.exists("Address", row.parent):
			remaining = frappe.get_all(
				"Dynamic Link",
				filters={"parent": row.parent, "parenttype": "Address"},
				limit_page_length=1,
				ignore_permissions=True,
			)
			if not remaining:
				try:
					frappe.delete_doc("Address", row.parent, ignore_permissions=True, force=True)
				except Exception:
					pass


@frappe.whitelist(allow_guest=True)
def seed_presentation_demo(reset=0):
	"""Create sample orders + fake team tagged for presentation wipe.

	``reset=1`` clears the pack first (no PIN — intended for lab provision / Settings
	reload after explicit user confirm; Settings clear path uses ``clear_presentation_demo``).
	"""
	ensure_demo_pack_custom_fields()
	reset = cint(reset)
	if reset:
		# Lab/provision reseed: wipe without PIN (destructive but scoped to tags).
		# Directly call wipe helpers by temporarily skipping PIN via internal path.
		_wipe_pack_untagged_safe()

	company = _resolve_company()
	item_codes = _ensure_demo_items()
	customers = _ensure_demo_customers()
	employees = _ensure_demo_team(company)
	orders = _ensure_demo_orders(customers, item_codes)
	frappe.db.commit()
	status = presentation_demo_status()
	return {
		"ok": True,
		"reset": reset,
		"created": {
			"customers": customers,
			"employees": employees,
			"orders": orders,
			"items": item_codes,
		},
		"status": status,
	}


def _wipe_pack_untagged_safe():
	"""Internal wipe used by seed(reset=1) — same as clear without PIN gate."""
	ensure_demo_pack_custom_fields()
	for so_name in _pack_names("Sales Order"):
		try:
			updates = {}
			for field in ("contact_person", "customer_address", "shipping_address_name"):
				if frappe.db.has_column("Sales Order", field):
					updates[field] = None
			if updates:
				frappe.db.set_value("Sales Order", so_name, updates, update_modified=False)
			ds = cint(frappe.db.get_value("Sales Order", so_name, "docstatus"))
			if ds == 1:
				frappe.flags.ignore_permissions = True
				so = frappe.get_doc("Sales Order", so_name)
				frappe.flags.ignore_permissions = False
				so.flags.ignore_permissions = True
				so.cancel()
			if frappe.db.exists("Sales Order", so_name):
				frappe.delete_doc("Sales Order", so_name, ignore_permissions=True, force=True)
		except Exception:
			pass
	for emp_name in _pack_names("Employee"):
		try:
			user_id = frappe.db.get_value("Employee", emp_name, "user_id")
			from erpnext.erpnext_integrations.ecommerce_api.employee_api import _set_employee_groups

			_set_employee_groups(emp_name, [])
			if user_id:
				frappe.db.set_value("Employee", emp_name, "user_id", None, update_modified=False)
			frappe.delete_doc("Employee", emp_name, ignore_permissions=True, force=True)
			if user_id and user_id not in ("Administrator", "Guest") and str(user_id).endswith(
				"@example.invalid"
			):
				if frappe.db.exists("User", user_id):
					frappe.delete_doc("User", user_id, ignore_permissions=True, force=True)
		except Exception:
			pass
	for cust in _pack_names("Customer"):
		try:
			untagged = frappe.db.sql(
				"""
				SELECT name FROM `tabSales Order`
				WHERE customer = %s
				  AND IFNULL(`custom_demo_pack`, '') != %s
				LIMIT 1
				""",
				(cust, PACK_VALUE),
			)
			if untagged:
				frappe.db.set_value("Customer", cust, FIELDNAME, "", update_modified=False)
				continue
			for leftover in frappe.get_all(
				"Sales Order",
				filters={"customer": cust},
				pluck="name",
				ignore_permissions=True,
			):
				try:
					ds = cint(frappe.db.get_value("Sales Order", leftover, "docstatus"))
					if ds == 1:
						frappe.flags.ignore_permissions = True
						so = frappe.get_doc("Sales Order", leftover)
						frappe.flags.ignore_permissions = False
						so.flags.ignore_permissions = True
						so.cancel()
					frappe.delete_doc("Sales Order", leftover, ignore_permissions=True, force=True)
				except Exception:
					pass
			_unlink_demo_contacts(cust)
			frappe.delete_doc("Customer", cust, ignore_permissions=True, force=True)
		except Exception:
			pass
	frappe.db.commit()
