"""Cash register (POS Profile) APIs for Logistica > Cajas.

A "cash register" maps to a standard ERPNext POS Profile. Revenue attribution
uses the profile's configured warehouse against submitted Sales Invoice Item
rows (the same warehouse already stamped on every POS sale in
`ecommerce_api.api.create_pos_sale`), so figures are accurate without any
checkout-side schema change. If two registers share a warehouse, their
revenue will overlap — expected for now since this app assigns one warehouse
per branch/register.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, nowdate

from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

TRACKED_FIELDS = [
	"company",
	"warehouse",
	"currency",
	"disabled",
	"write_off_limit",
	"selling_price_list",
]


def _company_defaults(company: str) -> dict:
	if not company:
		return {}
	return (
		frappe.db.get_value(
			"Company",
			company,
			["default_currency", "write_off_account", "cost_center"],
			as_dict=True,
		)
		or {}
	)


def _default_mode_of_payment() -> str | None:
	if frappe.db.exists("Mode of Payment", "Cash"):
		return "Cash"
	return frappe.db.get_value("Mode of Payment", {"enabled": 1}, "name")


def _revenue_for_warehouse(warehouse: str, start_date, end_date) -> float:
	"""Sum submitted Sales Invoice Item amounts for a warehouse in a date range."""
	if not warehouse:
		return 0.0
	total = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(sii.amount), 0)
		FROM `tabSales Invoice Item` sii
		INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
		WHERE sii.warehouse = %s
		  AND si.docstatus = 1
		  AND si.posting_date BETWEEN %s AND %s
		""",
		(warehouse, start_date, end_date),
	)
	return flt(total[0][0]) if total else 0.0


def _set_payments(doc, payments: list) -> None:
	doc.set("payments", [])
	for p in payments or []:
		mop = p.get("mode_of_payment") if isinstance(p, dict) else p
		if not mop or not frappe.db.exists("Mode of Payment", mop):
			continue
		doc.append(
			"payments",
			{
				"mode_of_payment": mop,
				"default": cint(p.get("default")) if isinstance(p, dict) else 0,
			},
		)
	if not doc.payments:
		default_mop = _default_mode_of_payment()
		if not default_mop:
			frappe.throw(_("At least one enabled Mode of Payment is required (e.g. Cash)."))
		doc.append("payments", {"mode_of_payment": default_mop, "default": 1})
	if not any(cint(p.default) for p in doc.payments):
		doc.payments[0].default = 1


def _set_applicable_users(doc, users: list) -> None:
	doc.set("applicable_for_users", [])
	for user in users or []:
		if user and frappe.db.exists("User", user):
			doc.append("applicable_for_users", {"user": user})


def _serialize_pos_profile(name: str) -> dict:
	"""Full row shape — list and single-get return the same thing, no separate
	'summary vs detail' split, since a Tables-style screen renders the detail
	panel straight from the row the user clicked."""
	doc = frappe.get_doc("POS Profile", name)
	today = nowdate()
	days = []
	for i in range(6, -1, -1):
		d = add_days(today, -i)
		days.append({"date": str(d), "amount": _revenue_for_warehouse(doc.warehouse, d, d)})
	made_today = days[-1]["amount"]
	made_this_week = flt(sum(d["amount"] for d in days))
	return {
		"name": doc.name,
		"company": doc.company,
		"warehouse": doc.warehouse,
		"currency": doc.currency,
		"disabled": cint(doc.disabled),
		"write_off_limit": flt(doc.write_off_limit),
		"selling_price_list": doc.selling_price_list,
		"payments": [
			{"mode_of_payment": p.mode_of_payment, "default": cint(p.default)}
			for p in (doc.payments or [])
		],
		"applicable_for_users": [u.user for u in (doc.applicable_for_users or []) if u.user],
		"made_today": made_today,
		"made_this_week": made_this_week,
		"revenue_by_day": days,
		"modified": str(doc.modified) if doc.modified else None,
	}


@frappe.whitelist()
def list_pos_profiles(search=None):
	filters = {}
	or_filters = None
	if search and str(search).strip():
		q = f"%{str(search).strip()}%"
		or_filters = [
			["name", "like", q],
			["warehouse", "like", q],
			["company", "like", q],
		]
	names = frappe.get_all(
		"POS Profile",
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="name asc",
	)
	rows = [_serialize_pos_profile(n) for n in names]
	return {"rows": rows, "total": len(rows)}


@frappe.whitelist()
def get_pos_profile(name):
	if not frappe.db.exists("POS Profile", name):
		frappe.throw(_("Cash register {0} not found").format(name))
	return _serialize_pos_profile(name)


@frappe.whitelist()
def list_pos_profile_meta():
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	companies = frappe.get_all("Company", pluck="name", order_by="name asc")
	warehouses = frappe.get_all(
		"Warehouse", filters={"is_group": 0}, pluck="name", order_by="name asc"
	)
	currencies = frappe.get_all(
		"Currency", filters={"enabled": 1}, pluck="name", order_by="name asc"
	)
	modes_of_payment = frappe.get_all(
		"Mode of Payment", filters={"enabled": 1}, pluck="name", order_by="name asc"
	)
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User"},
		pluck="name",
		order_by="name asc",
		limit_page_length=200,
	)
	return {
		"company": company,
		"companies": companies,
		"warehouses": warehouses,
		"currencies": currencies,
		"modes_of_payment": modes_of_payment,
		"users": users,
	}


@frappe.whitelist()
def save_pos_profile(name=None, data=None):
	"""Create or update a cash register (POS Profile). data: JSON/dict of fields."""
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = data or {}
	is_new = not name

	if is_new:
		title = (data.get("name") or "").strip()
		if not title:
			frappe.throw(_("Cash register name is required"))
		if frappe.db.exists("POS Profile", title):
			frappe.throw(_("Cash register {0} already exists").format(title))

		company = (
			data.get("company")
			or frappe.defaults.get_user_default("Company")
			or frappe.db.get_value("Company", {}, "name")
		)
		if not company:
			frappe.throw(_("Company is required"))
		warehouse = data.get("warehouse") or frappe.db.get_value(
			"Warehouse", {"is_group": 0, "company": company}, "name"
		)
		if not warehouse:
			frappe.throw(_("Warehouse is required"))

		defaults = _company_defaults(company)
		currency = data.get("currency") or defaults.get("default_currency")
		write_off_account = defaults.get("write_off_account") or frappe.db.get_value(
			"Account", {"company": company, "account_type": "Write Off"}, "name"
		)
		cost_center = defaults.get("cost_center") or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0}, "name"
		)
		if not (currency and write_off_account and cost_center):
			frappe.throw(
				_(
					"Company {0} is missing a default currency, write-off account, or cost "
					"center — configure those on the Company before creating a cash register."
				).format(company)
			)

		doc = frappe.new_doc("POS Profile")
		doc.name = title
		doc.company = company
		doc.warehouse = warehouse
		doc.currency = currency
		doc.write_off_account = write_off_account
		doc.write_off_cost_center = cost_center
		doc.write_off_limit = flt(data.get("write_off_limit") or 0)
		doc.disabled = cint(data.get("disabled") or 0)
		if data.get("selling_price_list"):
			doc.selling_price_list = data.get("selling_price_list")

		_set_payments(doc, data.get("payments") or [{"mode_of_payment": "Cash", "default": 1}])
		_set_applicable_users(doc, data.get("applicable_for_users") or [])

		doc.insert(ignore_permissions=True)
		log_field_changes(
			"POS Profile",
			doc.name,
			[("company", None, doc.company), ("warehouse", None, doc.warehouse)],
		)
	else:
		if not frappe.db.exists("POS Profile", name):
			frappe.throw(_("Cash register {0} not found").format(name))
		doc = frappe.get_doc("POS Profile", name)
		changes = []
		for key in TRACKED_FIELDS:
			if key not in data:
				continue
			new_val = data[key]
			if key == "disabled":
				new_val = cint(new_val)
			if key == "write_off_limit":
				new_val = flt(new_val)
			old_val = doc.get(key)
			if str(old_val or "") != str(new_val or ""):
				changes.append((key, old_val, new_val))
				doc.set(key, new_val)

		if "payments" in data:
			_set_payments(doc, data.get("payments") or [])
		if "applicable_for_users" in data:
			_set_applicable_users(doc, data.get("applicable_for_users") or [])

		doc.save(ignore_permissions=True)
		if changes:
			log_field_changes("POS Profile", doc.name, changes)

	frappe.db.commit()
	return {"ok": True, "cash_register": _serialize_pos_profile(doc.name)}
