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
	"customer",
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


WEB_POS_PROFILE_NAME = "Caja Web"


def _resolve_cash_bank_account(company: str) -> str | None:
	for filters in (
		{"company": company, "account_type": "Cash", "is_group": 0, "disabled": 0},
		{"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0},
	):
		acc = frappe.db.get_value("Account", filters, "name")
		if acc:
			return acc
	# Argentine chart: Caja is often untyped Asset.
	for like in ("%Caja - %", "%Cash%", "%Bank Account%"):
		acc = frappe.db.get_value(
			"Account",
			{"company": company, "root_type": "Asset", "is_group": 0, "disabled": 0, "name": ("like", like)},
			"name",
		)
		if acc:
			return acc
	return None


def _ensure_mode_of_payment_account(company: str, mop: str = "Cash") -> None:
	"""POS Profile validate requires Cash/Bank default account on the Mode of Payment."""
	if not mop or not frappe.db.exists("Mode of Payment", mop):
		return
	already = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mop, "company": company},
		"default_account",
	)
	if already:
		return
	acc = _resolve_cash_bank_account(company)
	if not acc:
		frappe.throw(
			_(
				"No Cash/Bank account found for company {0}. Cannot auto-create a cash register."
			).format(company)
		)
	doc = frappe.get_doc("Mode of Payment", mop)
	doc.append("accounts", {"company": company, "default_account": acc})
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _resolve_write_off_account(company: str) -> str | None:
	defaults = _company_defaults(company)
	acc = defaults.get("write_off_account")
	if acc and frappe.db.exists("Account", acc):
		return acc
	acc = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Write Off", "is_group": 0, "disabled": 0},
		"name",
	)
	if acc:
		return acc
	return frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Expense", "is_group": 0, "disabled": 0},
		"name",
	)


def ensure_default_web_pos_profile() -> str:
	"""Create a web-client POS Profile when the site has none.

	Used by cash-session start so the first web POS visit is not blocked.
	"""
	existing = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
	if existing:
		return existing
	if frappe.db.exists("POS Profile", WEB_POS_PROFILE_NAME):
		frappe.db.set_value("POS Profile", WEB_POS_PROFILE_NAME, "disabled", 0)
		frappe.db.commit()
		return WEB_POS_PROFILE_NAME

	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	company = resolve_company() or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("Company is required to auto-create a cash register."))
	warehouse = frappe.db.get_value(
		"Warehouse", {"is_group": 0, "company": company}, "name"
	)
	if not warehouse:
		frappe.throw(_("Warehouse is required to auto-create a cash register."))
	_ensure_mode_of_payment_account(company, "Cash")
	defaults = _company_defaults(company)
	currency = defaults.get("default_currency") or frappe.db.get_value(
		"Company", company, "default_currency"
	)
	write_off_account = _resolve_write_off_account(company)
	cost_center = defaults.get("cost_center") or frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	)
	if not (currency and write_off_account and cost_center):
		frappe.throw(
			_(
				"Company {0} is missing currency, an expense/write-off account, or a cost "
				"center — cannot auto-create a cash register."
			).format(company)
		)

	price_list = (
		frappe.db.get_single_value("Selling Settings", "selling_price_list")
		or (frappe.db.exists("Price List", "Standard Selling") and "Standard Selling")
		or frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
	)

	doc = frappe.new_doc("POS Profile")
	doc.name = WEB_POS_PROFILE_NAME
	doc.company = company
	doc.warehouse = warehouse
	doc.currency = currency
	doc.write_off_account = write_off_account
	doc.write_off_cost_center = cost_center
	doc.write_off_limit = 0
	doc.disabled = 0
	if price_list:
		doc.selling_price_list = price_list
	doc.customer = _default_pos_customer()
	_set_payments(doc, [{"mode_of_payment": "Cash", "default": 1}])
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


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


def _default_pos_customer() -> str:
	from erpnext.erpnext_integrations.ecommerce_api.api import _get_or_create_consumidor_final

	return _get_or_create_consumidor_final()


def _resolve_customer_for_profile(data_customer) -> str:
	raw = (data_customer or "").strip() if isinstance(data_customer, str) else ""
	if raw and frappe.db.exists("Customer", raw):
		return raw
	return _default_pos_customer()


def _serialize_pos_profile(name: str) -> dict:
	"""Full row shape — list and single-get return the same thing, no separate
	'summary vs detail' split, since a Tables-style screen renders the detail
	panel straight from the row the user clicked."""
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("POS Profile", name)
	frappe.flags.ignore_permissions = False
	today = nowdate()
	days = []
	for i in range(6, -1, -1):
		d = add_days(today, -i)
		days.append({"date": str(d), "amount": _revenue_for_warehouse(doc.warehouse, d, d)})
	made_today = days[-1]["amount"]
	made_this_week = flt(sum(d["amount"] for d in days))
	customer = doc.customer or None
	customer_name = None
	if customer:
		customer_name = frappe.db.get_value("Customer", customer, "customer_name") or customer
	return {
		"name": doc.name,
		"company": doc.company,
		"warehouse": doc.warehouse,
		"currency": doc.currency,
		"disabled": cint(doc.disabled),
		"write_off_limit": flt(doc.write_off_limit),
		"selling_price_list": doc.selling_price_list,
		"customer": customer,
		"customer_name": customer_name,
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
def list_pos_profiles(search=None, company=None):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	filters = {}
	active = company_scope(company)
	if active:
		filters["company"] = active
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
		ignore_permissions=True,
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
	from erpnext.erpnext_integrations.ecommerce_api.company_context import (
		allowed_company_names,
		company_scope,
		resolve_company,
	)

	company = resolve_company() or frappe.db.get_value("Company", {}, "name")
	companies = allowed_company_names()
	wh_filters = {"is_group": 0}
	scoped = company_scope()
	if scoped:
		wh_filters["company"] = scoped
	warehouses = frappe.get_all(
		"Warehouse",
		filters=wh_filters,
		pluck="name",
		order_by="name asc",
		ignore_permissions=True,
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

		from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

		company = data.get("company") or resolve_company() or frappe.db.get_value("Company", {}, "name")
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
		doc.customer = _resolve_customer_for_profile(data.get("customer"))

		_set_payments(doc, data.get("payments") or [{"mode_of_payment": "Cash", "default": 1}])
		_set_applicable_users(doc, data.get("applicable_for_users") or [])

		doc.insert(ignore_permissions=True)
		log_field_changes(
			"POS Profile",
			doc.name,
			[
				("company", None, doc.company),
				("warehouse", None, doc.warehouse),
				("customer", None, doc.customer),
			],
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
			if key == "customer":
				new_val = _resolve_customer_for_profile(new_val)
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


# ---------------------------------------------------------------------------
# Sessions = submitted POS Sales Invoices grouped by warehouse + posting date.
# There is no POS Opening/Closing Entry in this deploy; a "session" is one
# register-day. Start/end are the first/last invoice posting times that day.
# ---------------------------------------------------------------------------


def _parse_invoice_remarks(remarks: str | None) -> dict:
	out = {
		"offline_order_uuid": None,
		"receipt": None,
		"cashier": None,
		"device": None,
		"branch": None,
		"sale_mode": None,
		"payments_label": None,
		"cash_received": None,
	}
	if not remarks:
		return out
	for part in str(remarks).split(" | "):
		if ":" not in part:
			continue
		key, val = part.split(":", 1)
		key = key.strip()
		val = val.strip()
		if key == "offline_order_uuid":
			out["offline_order_uuid"] = val
		elif key == "receipt":
			out["receipt"] = val
		elif key == "cashier":
			out["cashier"] = val
		elif key == "device":
			out["device"] = val
		elif key == "branch":
			out["branch"] = val
		elif key == "sale_mode":
			out["sale_mode"] = val
		elif key == "payments":
			out["payments_label"] = val
		elif key == "cash_received":
			out["cash_received"] = flt(val)
	return out


def _warehouse_register_map() -> dict:
	rows = frappe.get_all(
		"POS Profile",
		fields=["name", "warehouse"],
		ignore_permissions=True,
	)
	mapping = {}
	for r in rows:
		wh = r.get("warehouse")
		if wh and wh not in mapping:
			mapping[wh] = r.get("name")
	return mapping


def _session_id(warehouse: str, posting_date) -> str:
	return f"{warehouse or ''}::{posting_date}"


def _split_session_id(session_id: str) -> tuple[str, str]:
	if not session_id or "::" not in session_id:
		frappe.throw(_("Invalid session id"))
	warehouse, posting_date = session_id.rsplit("::", 1)
	return warehouse, posting_date


def _pos_invoice_where(values: dict, warehouse=None, posting_date=None, company=None):
	"""WHERE clause for submitted POS sales (uuid tag or item warehouse in a POS Profile)."""
	wh_map = _warehouse_register_map()
	warehouses = list(wh_map.keys())
	clauses = ["si.docstatus = 1"]
	if company:
		clauses.append("si.company = %(company)s")
		values["company"] = company
	if posting_date:
		clauses.append("si.posting_date = %(posting_date)s")
		values["posting_date"] = posting_date
	if warehouse:
		clauses.append(
			"""
			EXISTS (
				SELECT 1 FROM `tabSales Invoice Item` siiw
				WHERE siiw.parent = si.name AND siiw.warehouse = %(warehouse)s
			)
			"""
		)
		values["warehouse"] = warehouse
	else:
		pos_clause = "si.remarks LIKE %(uuid_tag)s"
		values["uuid_tag"] = "%offline_order_uuid:%"
		if warehouses:
			values["warehouses"] = tuple(warehouses)
			pos_clause = (
				"("
				+ pos_clause
				+ """
				OR EXISTS (
					SELECT 1 FROM `tabSales Invoice Item` siiw
					WHERE siiw.parent = si.name
					  AND siiw.warehouse IN %(warehouses)s
				)
				)"""
			)
		clauses.append(pos_clause)
	return " AND ".join(clauses), wh_map


def _payments_for_invoices(invoice_names: list[str]) -> dict[str, list]:
	if not invoice_names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT
			per.reference_name AS invoice,
			pe.name AS payment_id,
			pe.mode_of_payment AS mode_of_payment,
			per.allocated_amount AS amount,
			pe.posting_date AS posting_date,
			pe.creation AS creation
		FROM `tabPayment Entry Reference` per
		INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		WHERE pe.docstatus = 1
		  AND per.reference_doctype = 'Sales Invoice'
		  AND per.reference_name IN %(names)s
		ORDER BY pe.creation ASC
		""",
		{"names": tuple(invoice_names)},
		as_dict=True,
	)
	out: dict[str, list] = {}
	for r in rows:
		out.setdefault(r.invoice, []).append(
			{
				"payment_id": r.payment_id,
				"mode_of_payment": r.mode_of_payment or "Cash",
				"amount": flt(r.amount),
			}
		)
	return out


def _items_for_invoices(invoice_names: list[str]) -> dict[str, list]:
	if not invoice_names:
		return {}
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", invoice_names]},
		fields=["parent", "item_code", "item_name", "qty", "rate", "amount", "warehouse", "idx"],
		order_by="idx asc",
		ignore_permissions=True,
	)
	out: dict[str, list] = {}
	for r in rows:
		out.setdefault(r.parent, []).append(
			{
				"item_code": r.item_code,
				"item_name": r.item_name,
				"qty": flt(r.qty),
				"rate": flt(r.rate),
				"amount": flt(r.amount),
				"warehouse": r.warehouse,
			}
		)
	return out


@frappe.whitelist()
def list_cash_register_sessions(warehouse=None, search=None, page=1, page_length=200, company=None):
	"""List POS sale sessions (one row per cash register + day)."""
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	values: dict = {}
	active = company_scope(company)
	where_sql, wh_map = _pos_invoice_where(
		values, warehouse=warehouse or None, company=active
	)
	page = max(cint(page) or 1, 1)
	page_length = min(max(cint(page_length) or 200, 1), 500)
	offset = (page - 1) * page_length

	rows = frappe.db.sql(
		f"""
		SELECT
			t.warehouse AS warehouse,
			t.posting_date AS posting_date,
			MIN(t.posting_time) AS started_at,
			MAX(t.posting_time) AS ended_at,
			COUNT(*) AS sales_count,
			COALESCE(SUM(t.grand_total), 0) AS total_sale
		FROM (
			SELECT
				si.name,
				si.posting_date,
				si.posting_time,
				si.grand_total,
				(
					SELECT sii.warehouse
					FROM `tabSales Invoice Item` sii
					WHERE sii.parent = si.name
					ORDER BY sii.idx
					LIMIT 1
				) AS warehouse
			FROM `tabSales Invoice` si
			WHERE {where_sql}
		) t
		GROUP BY t.warehouse, t.posting_date
		ORDER BY t.posting_date DESC, MIN(t.posting_time) DESC
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		{**values, "limit": page_length, "offset": offset},
		as_dict=True,
	)

	total_row = frappe.db.sql(
		f"""
		SELECT COUNT(*) AS c FROM (
			SELECT
				(
					SELECT sii.warehouse
					FROM `tabSales Invoice Item` sii
					WHERE sii.parent = si.name
					ORDER BY sii.idx
					LIMIT 1
				) AS warehouse,
				si.posting_date
			FROM `tabSales Invoice` si
			WHERE {where_sql}
			GROUP BY warehouse, si.posting_date
		) x
		""",
		values,
		as_dict=True,
	)
	total = cint(total_row[0]["c"]) if total_row else 0

	q = (search or "").strip().lower()
	sessions = []
	today = str(nowdate())
	for r in rows:
		wh = r.warehouse or ""
		register = wh_map.get(wh) or wh or "—"
		date_s = str(r.posting_date)
		if q and q not in register.lower() and q not in date_s and q not in wh.lower():
			continue
		sessions.append(
			{
				"session_id": _session_id(wh, date_s),
				"warehouse": wh or None,
				"register": register,
				"posting_date": date_s,
				"started_at": str(r.started_at) if r.started_at else None,
				"ended_at": str(r.ended_at) if r.ended_at else None,
				"sales_count": cint(r.sales_count),
				"total_sale": flt(r.total_sale),
				"is_open": 1 if date_s == today else 0,
			}
		)

	return {"rows": sessions, "total": total}


@frappe.whitelist()
def get_cash_register_session(session_id):
	"""One register-day: header totals + each sale with items and payments."""
	warehouse, posting_date = _split_session_id(session_id)
	values: dict = {}
	where_sql, wh_map = _pos_invoice_where(
		values, warehouse=warehouse or None, posting_date=posting_date
	)

	invoices = frappe.db.sql(
		f"""
		SELECT
			si.name,
			si.posting_date,
			si.posting_time,
			si.grand_total,
			si.outstanding_amount,
			si.remarks,
			si.owner,
			si.creation
		FROM `tabSales Invoice` si
		WHERE {where_sql}
		ORDER BY si.posting_time ASC, si.creation ASC
		""",
		values,
		as_dict=True,
	)

	names = [r.name for r in invoices]
	pay_map = _payments_for_invoices(names)
	item_map = _items_for_invoices(names)
	register = wh_map.get(warehouse) or warehouse or "—"

	sales = []
	pay_totals: dict[str, float] = {}
	started = None
	ended = None
	for inv in invoices:
		meta = _parse_invoice_remarks(inv.remarks)
		payments = pay_map.get(inv.name) or []
		if not payments and meta.get("payments_label"):
			payments = [
				{
					"payment_id": None,
					"mode_of_payment": meta["payments_label"],
					"amount": flt(inv.grand_total),
				}
			]
		for p in payments:
			mop = p.get("mode_of_payment") or "Cash"
			pay_totals[mop] = pay_totals.get(mop, 0) + flt(p.get("amount"))
		ptime = str(inv.posting_time) if inv.posting_time else None
		if ptime:
			started = ptime if started is None else min(started, ptime)
			ended = ptime if ended is None else max(ended, ptime)
		sales.append(
			{
				"name": inv.name,
				"posting_date": str(inv.posting_date) if inv.posting_date else posting_date,
				"posting_time": ptime,
				"grand_total": flt(inv.grand_total),
				"outstanding_amount": flt(inv.outstanding_amount),
				"receipt": meta.get("receipt") or inv.name,
				"cashier": meta.get("cashier"),
				"device": meta.get("device"),
				"sale_mode": meta.get("sale_mode"),
				"cash_received": meta.get("cash_received"),
				"payments": payments,
				"items": item_map.get(inv.name) or [],
			}
		)

	today = str(nowdate())
	return {
		"session_id": session_id,
		"warehouse": warehouse or None,
		"register": register,
		"posting_date": posting_date,
		"started_at": started,
		"ended_at": ended,
		"sales_count": len(sales),
		"total_sale": flt(sum(s["grand_total"] for s in sales)),
		"is_open": 1 if posting_date == today else 0,
		"payment_totals": [
			{"mode_of_payment": k, "amount": flt(v)} for k, v in sorted(pay_totals.items())
		],
		"sales": sales,
	}
