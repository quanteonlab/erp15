"""Developer wipe tools (Settings → Desarrollador, == unlock).

Hard-deletes transactional docs for the active company. Cascade expands:
- customers → related orders/invoices/DNs/payments, then customers
- orders → related invoices/DNs/payments, then sales orders
- invoices → related payments, then sales invoices
- purchases → purchase invoices/receipts/payments, then purchase orders (compras)
- lots → Stock Lot rows (lotes)
- suppliers → Supplier masters (proveedores) after clearing Dynamic Link contacts/addresses
- employees → HR rows that block delete (attendance, salary slips, …), then employees
- products → Item Prices then Items (site-wide catalog)
- categories → Item Groups except root ``All Item Groups`` (leaves first)

Admin PIN required. Not for production tenants with real data.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr


VALID_TARGETS = (
	"orders",
	"invoices",
	"customers",
	"suppliers",
	"purchases",
	"lots",
	"employees",
	"products",
	"categories",
)
# UI / Spanish aliases accepted by the API.
_TARGET_ALIASES = {
	"compras": "purchases",
	"purchase": "purchases",
	"po": "purchases",
	"lotes": "lots",
	"lote": "lots",
	"stock_lots": "lots",
	"stock_lot": "lots",
	"proveedores": "suppliers",
	"proveedor": "suppliers",
	"supplier": "suppliers",
}
_PROTECTED_ITEM_GROUPS = frozenset({"All Item Groups"})


def _require_admin_pin(pin) -> None:
	pin = cstr(pin or "").strip()
	if not pin or pin in ("null", "undefined"):
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


def _parse_targets(targets) -> set[str]:
	if isinstance(targets, str):
		raw = targets.strip()
		if not raw or raw in ("null", "undefined"):
			return set()
		try:
			targets = json.loads(raw)
		except Exception:
			targets = [p.strip() for p in raw.split(",") if p.strip()]
	if not isinstance(targets, (list, tuple, set)):
		return set()
	out = set()
	for t in targets:
		key = cstr(t or "").strip().lower()
		key = _TARGET_ALIASES.get(key, key)
		if key in VALID_TARGETS:
			out.add(key)
	return out


def _force_cancel_delete(doctype: str, name: str) -> tuple[bool, str | None]:
	"""Cancel (if submitted) then force-delete. Returns (gone, error)."""
	if not name or not frappe.db.exists(doctype, name):
		return True, None
	try:
		# Drop contact/address links that often block transactional delete.
		if doctype in ("Sales Order", "Sales Invoice", "Delivery Note"):
			updates = {}
			for field in ("contact_person", "customer_address", "shipping_address_name", "company_address"):
				if frappe.db.has_column(doctype, field):
					updates[field] = None
			if updates:
				frappe.db.set_value(doctype, name, updates, update_modified=False)
		if doctype in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
			updates = {}
			for field in (
				"contact_person",
				"supplier_address",
				"shipping_address",
				"billing_address",
				"company_address",
			):
				if frappe.db.has_column(doctype, field):
					updates[field] = None
			if updates:
				frappe.db.set_value(doctype, name, updates, update_modified=False)

		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(doctype, name)
		frappe.flags.ignore_permissions = False
		ds = cint(doc.docstatus)
		if ds == 1:
			try:
				doc.flags.ignore_permissions = True
				doc.cancel()
			except Exception:
				# Last-resort: mark cancelled so force delete can proceed (GL may orphan — admin wipe).
				frappe.db.set_value(doctype, name, "docstatus", 2, update_modified=False)
		if frappe.db.exists(doctype, name):
			frappe.flags.ignore_permissions = True
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		if not frappe.db.exists(doctype, name):
			return True, None
		return False, "sigue existiendo"
	except Exception as e:
		brief = _brief_error(e)
		frappe.log_error(title=f"dev_wipe {doctype}", message=f"{name}: {e}")
		try:
			if frappe.db.exists(doctype, name):
				frappe.flags.ignore_permissions = True
				frappe.db.set_value(doctype, name, "docstatus", 2, update_modified=False)
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
			if not frappe.db.exists(doctype, name):
				return True, None
			return False, brief
		except Exception as e2:
			frappe.log_error(title=f"dev_wipe force {doctype}", message=f"{name}: {e2}")
			return False, _brief_error(e2)


def _unlink_party_contacts(party_doctype: str, party_name: str) -> None:
	"""Remove Contact/Address Dynamic Links so Customer/Supplier delete can proceed."""
	for parenttype in ("Contact", "Address"):
		links = frappe.get_all(
			"Dynamic Link",
			filters={
				"link_doctype": party_doctype,
				"link_name": party_name,
				"parenttype": parenttype,
			},
			fields=["name", "parent"],
			ignore_permissions=True,
		)
		for row in links:
			try:
				frappe.delete_doc("Dynamic Link", row.name, ignore_permissions=True, force=True)
			except Exception:
				pass
			parent = row.parent
			if not parent or not frappe.db.exists(parenttype, parent):
				continue
			remaining = frappe.get_all(
				"Dynamic Link",
				filters={"parent": parent, "parenttype": parenttype},
				limit_page_length=1,
				ignore_permissions=True,
			)
			if remaining:
				continue
			try:
				frappe.delete_doc(parenttype, parent, ignore_permissions=True, force=True)
			except Exception:
				pass


SINCE_WINDOWS = ("24h", "1w", "1m", "all")


def _parse_since(since) -> str:
	key = cstr(since or "all").strip().lower()
	aliases = {
		"": "all",
		"null": "all",
		"undefined": "all",
		"all": "all",
		"siempre": "all",
		"forever": "all",
		"24h": "24h",
		"24": "24h",
		"1d": "24h",
		"day": "24h",
		"1w": "1w",
		"7d": "1w",
		"week": "1w",
		"1m": "1m",
		"30d": "1m",
		"month": "1m",
	}
	return aliases.get(key, "all")


def _creation_cutoff(since: str):
	seconds = {"24h": 86400, "1w": 7 * 86400, "1m": 30 * 86400}.get(since)
	if not seconds:
		return None
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-seconds)


def _brief_error(exc: BaseException) -> str:
	kind = type(exc).__name__
	msg = cstr(exc).replace("\n", " ").strip()[:220]
	low = f"{kind} {msg}".lower()
	if "permission" in low or "not permitted" in low or "not allowed" in low:
		return f"permiso: {msg or kind}"
	if "linkexists" in low or "linked with" in low or "cannot delete" in low:
		return f"bloqueado: {msg or kind}"
	return f"{kind}: {msg}" if msg else kind


def _company_names(
	doctype: str, company: str, *, extra_filters: dict | None = None, cutoff=None
) -> list[str]:
	filters: dict = {"company": company}
	if extra_filters:
		filters.update(extra_filters)
	if cutoff is not None:
		filters["creation"] = [">=", cutoff]
	return (
		frappe.get_all(
			doctype,
			filters=filters,
			pluck="name",
			limit_page_length=20000,
			ignore_permissions=True,
		)
		or []
	)


def _payment_entries_for(company: str, *, references: list[tuple[str, str]] | None = None) -> list[str]:
	"""Payment Entries for company, optionally restricted to (ref_doctype, ref_name) pairs."""
	if references is not None and not references:
		return []
	if references is None:
		return _company_names("Payment Entry", company)
	names: set[str] = set()
	by_dt: dict[str, list[str]] = {}
	for dt, nm in references:
		by_dt.setdefault(dt, []).append(nm)
	for dt, nms in by_dt.items():
		for i in range(0, len(nms), 200):
			chunk = nms[i : i + 200]
			rows = frappe.get_all(
				"Payment Entry Reference",
				filters={"reference_doctype": dt, "reference_name": ["in", chunk]},
				pluck="parent",
				ignore_permissions=True,
			)
			for p in rows or []:
				if not p:
					continue
				pe_co = frappe.db.get_value("Payment Entry", p, "company")
				if pe_co and pe_co != company:
					continue
				names.add(p)
	return list(names)


def _names_for_customers(doctype: str, company: str, customers: list[str]) -> list[str]:
	if not customers:
		return []
	out: set[str] = set()
	for i in range(0, len(customers), 400):
		chunk = customers[i : i + 400]
		rows = frappe.get_all(
			doctype,
			filters={"company": company, "customer": ["in", chunk]},
			pluck="name",
			limit_page_length=20000,
			ignore_permissions=True,
		)
		out.update(rows or [])
	return list(out)


def _child_parents(child_dt: str, link_field: str, parents: list[str]) -> list[str]:
	if not parents:
		return []
	out: set[str] = set()
	for i in range(0, len(parents), 200):
		chunk = parents[i : i + 200]
		linked = frappe.db.sql(
			f"""
			SELECT DISTINCT parent FROM `tab{child_dt}`
			WHERE `{link_field}` IN ({",".join(["%s"] * len(chunk))})
			""",
			tuple(chunk),
		)
		out.update(r[0] for r in linked if r and r[0])
	return list(out)


def _plan_wipe(targets: set[str], company: str, since: str = "all") -> dict:
	"""Expand cascade and collect doc names to delete (dependency order).

	``since`` filters the chosen roots by ``creation``. Linked docs of those roots
	are included even if they are older (cascade).
	"""
	cutoff = _creation_cutoff(since)
	want_customers = "customers" in targets
	want_orders = "orders" in targets or want_customers
	want_invoices = "invoices" in targets or want_orders  # orders cascade into invoices

	customers: list[str] = []
	if want_customers:
		filters: dict = {}
		if cutoff is not None:
			filters["creation"] = [">=", cutoff]
		customers = (
			frappe.get_all(
				"Customer",
				filters=filters,
				pluck="name",
				limit_page_length=20000,
				ignore_permissions=True,
			)
			or []
		)

	so_set: set[str] = set()
	if "orders" in targets:
		so_set.update(_company_names("Sales Order", company, cutoff=cutoff))
	if want_customers:
		so_set.update(_names_for_customers("Sales Order", company, customers))
	sales_orders = list(so_set)

	si_set: set[str] = set()
	if "invoices" in targets:
		si_set.update(_company_names("Sales Invoice", company, cutoff=cutoff))
	if want_orders:
		si_set.update(_child_parents("Sales Invoice Item", "sales_order", sales_orders))
	if want_customers:
		si_set.update(_names_for_customers("Sales Invoice", company, customers))
	sales_invoices = list(si_set) if want_invoices else []

	dn_set: set[str] = set()
	if want_orders:
		# Full remito wipe only for "siempre" + orders. A time window keeps remitos
		# that belong to the selected orders / customers.
		if "orders" in targets and cutoff is None:
			dn_set.update(_company_names("Delivery Note", company))
		dn_set.update(_child_parents("Delivery Note Item", "against_sales_order", sales_orders))
		if want_customers:
			dn_set.update(_names_for_customers("Delivery Note", company, customers))
	delivery_notes = list(dn_set)

	refs: list[tuple[str, str]] = [("Sales Order", n) for n in sales_orders] + [
		("Sales Invoice", n) for n in sales_invoices
	]
	payment_entries = _payment_entries_for(company, references=refs) if refs else []

	pos_invoices: list[str] = []
	if frappe.db.exists("DocType", "POS Invoice") and want_invoices:
		pos_set: set[str] = set()
		if "invoices" in targets or "orders" in targets:
			pos_set.update(_company_names("POS Invoice", company, cutoff=cutoff))
		if want_customers:
			pos_set.update(_names_for_customers("POS Invoice", company, customers))
		pos_invoices = list(pos_set)

	employees: list[str] = []
	if "employees" in targets:
		filters = {}
		if cutoff is not None:
			filters["creation"] = [">=", cutoff]
		employees = (
			frappe.get_all(
				"Employee",
				filters=filters,
				pluck="name",
				limit_page_length=20000,
				ignore_permissions=True,
			)
			or []
		)

	products: list[str] = []
	if "products" in targets:
		filters = {}
		if cutoff is not None:
			filters["creation"] = [">=", cutoff]
		products = (
			frappe.get_all(
				"Item",
				filters=filters,
				pluck="name",
				limit_page_length=50000,
				ignore_permissions=True,
			)
			or []
		)

	categories: list[str] = []
	if "categories" in targets:
		filters = {}
		if cutoff is not None:
			filters["creation"] = [">=", cutoff]
		raw_groups = (
			frappe.get_all(
				"Item Group",
				filters=filters,
				fields=["name", "parent_item_group", "lft", "rgt"],
				limit_page_length=20000,
				ignore_permissions=True,
			)
			or []
		)
		# Leaves first (deeper nested / larger lft-rgt span last).
		ranked = []
		for row in raw_groups:
			name = cstr(row.get("name") or "")
			if not name or name in _PROTECTED_ITEM_GROUPS:
				continue
			lft = cint(row.get("lft"))
			rgt = cint(row.get("rgt"))
			depth = max(0, rgt - lft)
			ranked.append((depth, name))
		ranked.sort(key=lambda x: x[0])  # smaller span = leafier first
		categories = [n for _, n in ranked]

	# Compras: Purchase Orders + cascade receipts / purchase invoices / payments.
	purchase_orders: list[str] = []
	purchase_receipts: list[str] = []
	purchase_invoices: list[str] = []
	if "purchases" in targets:
		purchase_orders = _company_names("Purchase Order", company, cutoff=cutoff)
		purchase_receipts = _child_parents(
			"Purchase Receipt Item", "purchase_order", purchase_orders
		)
		# Time-window "all" also sweeps orphan company PRs (no PO link / old window).
		if cutoff is None:
			purchase_receipts = list(
				set(purchase_receipts) | set(_company_names("Purchase Receipt", company))
			)
		else:
			purchase_receipts = list(
				set(purchase_receipts)
				| set(_company_names("Purchase Receipt", company, cutoff=cutoff))
			)
		purchase_invoices = _child_parents(
			"Purchase Invoice Item", "purchase_order", purchase_orders
		)
		if cutoff is None:
			purchase_invoices = list(
				set(purchase_invoices) | set(_company_names("Purchase Invoice", company))
			)
		else:
			purchase_invoices = list(
				set(purchase_invoices)
				| set(_company_names("Purchase Invoice", company, cutoff=cutoff))
			)
		buy_refs: list[tuple[str, str]] = (
			[("Purchase Order", n) for n in purchase_orders]
			+ [("Purchase Invoice", n) for n in purchase_invoices]
			+ [("Purchase Receipt", n) for n in purchase_receipts]
		)
		buy_payments = _payment_entries_for(company, references=buy_refs) if buy_refs else []
		payment_entries = list(set(payment_entries) | set(buy_payments))

	# Lotes: soft Stock Lot ledger (not ERPNext Batch).
	lots: list[str] = []
	if "lots" in targets and frappe.db.exists("DocType", "Stock Lot"):
		conds: list[str] = []
		vals: list = []
		if cutoff is not None:
			conds.append("`creation` >= %s")
			vals.append(cutoff)
		if frappe.db.has_column("Stock Lot", "company"):
			# Active company, or blank company (older receiving rows).
			conds.append("(IFNULL(`company`, '') = '' OR `company` = %s)")
			vals.append(company)
		where = (" WHERE " + " AND ".join(conds)) if conds else ""
		lots = [
			r[0]
			for r in (
				frappe.db.sql(
					f"SELECT `name` FROM `tabStock Lot`{where} ORDER BY `creation` ASC LIMIT 50000",
					tuple(vals),
				)
				or []
			)
			if r and r[0]
		]

	suppliers: list[str] = []
	if "suppliers" in targets:
		filters = {}
		if cutoff is not None:
			filters["creation"] = [">=", cutoff]
		suppliers = (
			frappe.get_all(
				"Supplier",
				filters=filters,
				pluck="name",
				limit_page_length=20000,
				ignore_permissions=True,
			)
			or []
		)

	return {
		"payment_entries": payment_entries,
		"pos_invoices": pos_invoices,
		"sales_invoices": sales_invoices,
		"delivery_notes": delivery_notes,
		"sales_orders": sales_orders if want_orders else [],
		"customers": customers if want_customers else [],
		"suppliers": suppliers,
		"purchase_orders": purchase_orders,
		"purchase_receipts": purchase_receipts,
		"purchase_invoices": purchase_invoices,
		"lots": lots,
		"employees": employees,
		"products": products,
		"categories": categories,
		"targets": sorted(targets),
		"since": since,
		"company": company,
	}


_EMPLOYEE_BLOCKERS = (
	"Attendance",
	"Employee Checkin",
	"Salary Slip",
	"Leave Application",
	"Leave Allocation",
	"Expense Claim",
	"Shift Assignment",
	"Employee Group Table",
)


def _safe_delete_user(user_id: str | None) -> None:
	"""Drop a staff login created for a worker. Never touch Administrator / System Manager."""
	user_id = cstr(user_id or "").strip()
	if not user_id or user_id in ("Administrator", "Guest"):
		return
	if not frappe.db.exists("User", user_id):
		return
	roles = set(
		frappe.get_all(
			"Has Role",
			filters={"parent": user_id, "parenttype": "User"},
			pluck="role",
			ignore_permissions=True,
		)
		or []
	)
	if roles & {"Administrator", "System Manager"}:
		return
	try:
		frappe.delete_doc("User", user_id, ignore_permissions=True, force=True)
	except Exception as e:
		frappe.log_error(title="dev_wipe User", message=f"{user_id}: {e}")


def _wipe_employee(emp: str) -> tuple[bool, str | None]:
	"""Unlink groups/PIN/user and delete HR rows that block Employee delete."""
	if not emp or not frappe.db.exists("Employee", emp):
		return True, None
	user_id = frappe.db.get_value("Employee", emp, "user_id")
	try:
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
			_load_ops_pin_store,
			_save_ops_pin_store,
			_set_employee_groups,
		)

		_set_employee_groups(emp, [])
		pin_store = _load_ops_pin_store()
		by_employee = dict(pin_store.get("by_employee") or {})
		by_pin = dict(pin_store.get("by_pin") or {})
		entry = by_employee.pop(emp, None)
		if isinstance(entry, dict):
			p = str(entry.get("pin") or "").strip()
			if p and by_pin.get(p) == emp:
				by_pin.pop(p, None)
		if entry is not None:
			_save_ops_pin_store({"by_employee": by_employee, "by_pin": by_pin})
	except Exception as e:
		frappe.log_error(title="dev_wipe employee unlink", message=f"{emp}: {e}")

	if user_id:
		frappe.db.set_value("Employee", emp, "user_id", None, update_modified=False)
	if frappe.db.has_column("Sales Person", "employee"):
		for sp in frappe.get_all(
			"Sales Person", filters={"employee": emp}, pluck="name", ignore_permissions=True
		) or []:
			frappe.db.set_value("Sales Person", sp, "employee", None, update_modified=False)

	for dt in _EMPLOYEE_BLOCKERS:
		if not frappe.db.exists("DocType", dt):
			continue
		field = "employee" if frappe.db.has_column(dt, "employee") else None
		if not field:
			continue
		for name in frappe.get_all(dt, filters={field: emp}, pluck="name", ignore_permissions=True) or []:
			_force_cancel_delete(dt, name)

	last_err = None
	if frappe.db.exists("Employee", emp):
		try:
			frappe.flags.ignore_permissions = True
			frappe.delete_doc("Employee", emp, ignore_permissions=True, force=True)
		except Exception as e:
			last_err = _brief_error(e)
			frappe.log_error(title="dev_wipe Employee", message=f"{emp}: {e}")
	gone = not frappe.db.exists("Employee", emp)
	if gone:
		_safe_delete_user(user_id)
		return True, None
	return False, last_err or "sigue existiendo"


def _wipe_item(item_code: str) -> tuple[bool, str | None]:
	"""Delete prices / barcodes noise then force-delete Item."""
	if not item_code or not frappe.db.exists("Item", item_code):
		return True, None
	try:
		for price in frappe.get_all(
			"Item Price",
			filters={"item_code": item_code},
			pluck="name",
			ignore_permissions=True,
		) or []:
			_force_cancel_delete("Item Price", price)
		if frappe.db.exists("DocType", "Item Barcode"):
			# Child table — cleared with parent; no separate delete needed.
			pass
		if frappe.db.exists("DocType", "Bin"):
			for bin_name in frappe.get_all(
				"Bin", filters={"item_code": item_code}, pluck="name", ignore_permissions=True
			) or []:
				try:
					frappe.db.set_value("Bin", bin_name, {"actual_qty": 0, "projected_qty": 0}, update_modified=False)
					frappe.delete_doc("Bin", bin_name, ignore_permissions=True, force=True)
				except Exception:
					pass
		frappe.flags.ignore_permissions = True
		frappe.delete_doc("Item", item_code, ignore_permissions=True, force=True)
		if not frappe.db.exists("Item", item_code):
			return True, None
		return False, "sigue existiendo"
	except Exception as e:
		frappe.log_error(title="dev_wipe Item", message=f"{item_code}: {e}")
		return False, _brief_error(e)


def _wipe_item_group(group: str) -> tuple[bool, str | None]:
	if not group or group in _PROTECTED_ITEM_GROUPS:
		return True, None
	if not frappe.db.exists("Item Group", group):
		return True, None
	try:
		frappe.flags.ignore_permissions = True
		frappe.delete_doc("Item Group", group, ignore_permissions=True, force=True)
		if not frappe.db.exists("Item Group", group):
			return True, None
		return False, "sigue existiendo"
	except Exception as e:
		frappe.log_error(title="dev_wipe Item Group", message=f"{group}: {e}")
		return False, _brief_error(e)


def _empty_counts() -> dict:
	return {
		"payment_entries": 0,
		"pos_invoices": 0,
		"sales_invoices": 0,
		"delivery_notes": 0,
		"sales_orders": 0,
		"customers": 0,
		"suppliers": 0,
		"purchase_orders": 0,
		"purchase_receipts": 0,
		"purchase_invoices": 0,
		"lots": 0,
		"employees": 0,
		"products": 0,
		"categories": 0,
	}


def _note_block(blocked: list, doctype: str, name: str, reason: str | None) -> None:
	if len(blocked) >= 40:
		return
	blocked.append({"doctype": doctype, "name": name, "reason": reason or "error"})


@frappe.whitelist(allow_guest=True)
def dev_wipe_preview(targets=None, since=None):
	"""Count docs that would be wiped for the selected targets (cascade expanded)."""
	parsed = _parse_targets(targets)
	window = _parse_since(since)
	if not parsed:
		return {
			"ok": True,
			"company": _resolve_company(),
			"targets": [],
			"since": window,
			"counts": _empty_counts(),
		}
	company = _resolve_company()
	plan = _plan_wipe(parsed, company, window)
	return {
		"ok": True,
		"company": company,
		"targets": plan["targets"],
		"since": plan["since"],
		"counts": {
			"payment_entries": len(plan["payment_entries"]),
			"pos_invoices": len(plan["pos_invoices"]),
			"sales_invoices": len(plan["sales_invoices"]),
			"delivery_notes": len(plan["delivery_notes"]),
			"sales_orders": len(plan["sales_orders"]),
			"customers": len(plan["customers"]),
			"suppliers": len(plan["suppliers"]),
			"purchase_orders": len(plan["purchase_orders"]),
			"purchase_receipts": len(plan["purchase_receipts"]),
			"purchase_invoices": len(plan["purchase_invoices"]),
			"lots": len(plan["lots"]),
			"employees": len(plan["employees"]),
			"products": len(plan["products"]),
			"categories": len(plan["categories"]),
		},
	}


@frappe.whitelist(allow_guest=True)
def clear_dev_wipe_data(targets=None, pin=None, since=None):
	"""Wipe selected transactional data with cascade. Requires admin PIN."""
	parsed = _parse_targets(targets)
	if not parsed:
		frappe.throw(
			_(
				"Select at least one target: orders, invoices, customers, suppliers, purchases, lots, employees, products, categories"
			)
		)
	_require_admin_pin(pin)
	window = _parse_since(since)

	company = _resolve_company()
	plan = _plan_wipe(parsed, company, window)

	removed = {**_empty_counts(), "errors": 0}
	blocked: list[dict] = []
	prev_ignore = frappe.flags.ignore_permissions
	frappe.flags.ignore_permissions = True

	def _batch(names: list[str], doctype: str, key: str) -> None:
		for i, name in enumerate(names):
			ok, reason = _force_cancel_delete(doctype, name)
			if ok:
				removed[key] += 1
			else:
				removed["errors"] += 1
				_note_block(blocked, doctype, name, reason)
			if (i + 1) % 25 == 0:
				frappe.db.commit()

	try:
		_batch(plan["payment_entries"], "Payment Entry", "payment_entries")
		if plan["pos_invoices"]:
			_batch(plan["pos_invoices"], "POS Invoice", "pos_invoices")
		_batch(plan["sales_invoices"], "Sales Invoice", "sales_invoices")
		_batch(plan["delivery_notes"], "Delivery Note", "delivery_notes")
		_batch(plan["sales_orders"], "Sales Order", "sales_orders")

		# Buying cascade: invoices → receipts → orders. Clear Stock Lot PO links first.
		if plan["purchase_orders"] and frappe.db.exists("DocType", "Stock Lot"):
			if frappe.db.has_column("Stock Lot", "purchase_order"):
				for i in range(0, len(plan["purchase_orders"]), 200):
					chunk = plan["purchase_orders"][i : i + 200]
					frappe.db.sql(
						"""
						UPDATE `tabStock Lot`
						SET purchase_order = NULL
						WHERE purchase_order IN ({})
						""".format(",".join(["%s"] * len(chunk))),
						tuple(chunk),
					)
		_batch(plan["purchase_invoices"], "Purchase Invoice", "purchase_invoices")
		_batch(plan["purchase_receipts"], "Purchase Receipt", "purchase_receipts")
		_batch(plan["purchase_orders"], "Purchase Order", "purchase_orders")

		if plan["lots"] and frappe.db.exists("DocType", "Stock Lot"):
			for i, lot in enumerate(plan["lots"]):
				try:
					if frappe.db.exists("Stock Lot", lot):
						frappe.flags.ignore_permissions = True
						frappe.delete_doc("Stock Lot", lot, ignore_permissions=True, force=True)
					if not frappe.db.exists("Stock Lot", lot):
						removed["lots"] += 1
					else:
						removed["errors"] += 1
						_note_block(blocked, "Stock Lot", lot, "sigue existiendo")
				except Exception as e:
					removed["errors"] += 1
					_note_block(blocked, "Stock Lot", lot, _brief_error(e))
					frappe.log_error(title="dev_wipe Stock Lot", message=f"{lot}: {e}")
				if (i + 1) % 25 == 0:
					frappe.db.commit()

		for i, cust in enumerate(plan["customers"]):
			try:
				_unlink_party_contacts("Customer", cust)
				for leftover_dt in ("Sales Order", "Sales Invoice", "Delivery Note", "POS Invoice"):
					if leftover_dt == "POS Invoice" and not frappe.db.exists("DocType", "POS Invoice"):
						continue
					filters = {"customer": cust}
					if frappe.db.has_column(leftover_dt, "company"):
						filters["company"] = company
					for leftover in frappe.get_all(
						leftover_dt, filters=filters, pluck="name", ignore_permissions=True
					) or []:
						ok, reason = _force_cancel_delete(leftover_dt, leftover)
						if not ok:
							removed["errors"] += 1
							_note_block(blocked, leftover_dt, leftover, reason)
				if frappe.db.exists("Customer", cust):
					frappe.flags.ignore_permissions = True
					frappe.delete_doc("Customer", cust, ignore_permissions=True, force=True)
				if not frappe.db.exists("Customer", cust):
					removed["customers"] += 1
				else:
					removed["errors"] += 1
					_note_block(blocked, "Customer", cust, "sigue existiendo")
			except Exception as e:
				removed["errors"] += 1
				_note_block(blocked, "Customer", cust, _brief_error(e))
				frappe.log_error(title="dev_wipe Customer", message=f"{cust}: {e}")
			if (i + 1) % 25 == 0:
				frappe.db.commit()

		for i, supplier in enumerate(plan["suppliers"]):
			try:
				_unlink_party_contacts("Supplier", supplier)
				for leftover_dt in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
					filters = {"supplier": supplier}
					if frappe.db.has_column(leftover_dt, "company"):
						filters["company"] = company
					for leftover in frappe.get_all(
						leftover_dt, filters=filters, pluck="name", ignore_permissions=True
					) or []:
						ok, reason = _force_cancel_delete(leftover_dt, leftover)
						if not ok:
							removed["errors"] += 1
							_note_block(blocked, leftover_dt, leftover, reason)
				if frappe.db.exists("Supplier", supplier):
					frappe.flags.ignore_permissions = True
					frappe.delete_doc("Supplier", supplier, ignore_permissions=True, force=True)
				if not frappe.db.exists("Supplier", supplier):
					removed["suppliers"] += 1
				else:
					removed["errors"] += 1
					_note_block(blocked, "Supplier", supplier, "sigue existiendo")
			except Exception as e:
				removed["errors"] += 1
				_note_block(blocked, "Supplier", supplier, _brief_error(e))
				frappe.log_error(title="dev_wipe Supplier", message=f"{supplier}: {e}")
			if (i + 1) % 25 == 0:
				frappe.db.commit()

		for i, emp in enumerate(plan["employees"]):
			try:
				ok, reason = _wipe_employee(emp)
				if ok:
					removed["employees"] += 1
				else:
					removed["errors"] += 1
					_note_block(blocked, "Employee", emp, reason)
			except Exception as e:
				removed["errors"] += 1
				_note_block(blocked, "Employee", emp, _brief_error(e))
				frappe.log_error(title="dev_wipe Employee", message=f"{emp}: {e}")
			if (i + 1) % 25 == 0:
				frappe.db.commit()

		# Catalog last: products before categories so groups can empty out.
		for i, item_code in enumerate(plan["products"]):
			try:
				ok, reason = _wipe_item(item_code)
				if ok:
					removed["products"] += 1
				else:
					removed["errors"] += 1
					_note_block(blocked, "Item", item_code, reason)
			except Exception as e:
				removed["errors"] += 1
				_note_block(blocked, "Item", item_code, _brief_error(e))
				frappe.log_error(title="dev_wipe Item", message=f"{item_code}: {e}")
			if (i + 1) % 25 == 0:
				frappe.db.commit()

		for i, group in enumerate(plan["categories"]):
			try:
				ok, reason = _wipe_item_group(group)
				if ok:
					removed["categories"] += 1
				else:
					removed["errors"] += 1
					_note_block(blocked, "Item Group", group, reason)
			except Exception as e:
				removed["errors"] += 1
				_note_block(blocked, "Item Group", group, _brief_error(e))
				frappe.log_error(title="dev_wipe Item Group", message=f"{group}: {e}")
			if (i + 1) % 25 == 0:
				frappe.db.commit()

		frappe.db.commit()
	finally:
		frappe.flags.ignore_permissions = prev_ignore

	return {
		"ok": removed["errors"] == 0,
		"company": company,
		"targets": plan["targets"],
		"since": plan["since"],
		"removed": removed,
		"blocked": blocked,
		"blocked_total": removed["errors"],
	}


# Additive / idempotent ensures — same family as hooks.py after_migrate (+ lean extras).
# Each entry is selectable via reseed_site_defaults(steps=[...]). Order is fixed.
_RESEED_CATALOG: tuple[dict, ...] = (
	{
		"id": "gender_masters",
		"path": "erpnext.erpnext_integrations.ecommerce_api.employee_api.ensure_gender_masters",
		"label": "Gender masters",
		"seeds": "Gender rows: Male, Female, Other, Non-binary, Prefer not to say",
	},
	{
		"id": "crm_party_buckets",
		"path": "erpnext.erpnext_integrations.ecommerce_api.api.ensure_crm_party_buckets",
		"label": "CRM party buckets",
		"seeds": "Consumidor Final, Uncategorized customer, Uncategorized supplier",
	},
	{
		"id": "client_access_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.client_access_api.ensure_client_access_custom_fields",
		"label": "Client access fields",
		"seeds": "Customer/Lead custom_client_access_pin + custom_client_phone_e164",
	},
	{
		"id": "tms_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.tms_api.ensure_tms_lean_site_fields",
		"label": "TMS delivery fields",
		"seeds": (
			"Customer.custom_delivery_warehouse, Address.custom_delivery_frequency, "
			"Delivery Trip.custom_locked, payment-split + late-penalty fields"
		),
	},
	{
		"id": "stock_lot_doctype",
		"path": "erpnext.erpnext_integrations.ecommerce_api.lots_api.ensure_stock_lot_doctype_status",
		"label": "Stock Lot DocType",
		"seeds": "Verify Stock Lot DocType exists (needs migrate if missing — does not invent schema)",
	},
	{
		"id": "field_acl_starters",
		"path": "erpnext.erpnext_integrations.ecommerce_api.field_acl.ensure_starter_field_acls",
		"label": "Field ACL starters",
		"seeds": "Field ACL matrices for starter Employee Groups (only if group has no ACL yet)",
	},
	{
		"id": "product_manager_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.product_manager.ensure_product_manager_custom_fields",
		"label": "Product manager fields",
		"seeds": "Item custom fields (normalized title, pack qty/size/unit, unit SKU, …)",
	},
	{
		"id": "product_bundle_promo_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.api.ensure_product_bundle_promo_fields",
		"label": "Product bundle promo fields",
		"seeds": "Product Bundle custom_valid_from / custom_valid_upto / custom_for_price_list",
	},
	{
		"id": "preventa_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.preventa_api.ensure_preventa_custom_fields",
		"label": "Preventa fields",
		"seeds": "Lead preventa stage + address custom fields",
	},
	{
		"id": "print_templates",
		"path": "erpnext.erpnext_integrations.ecommerce_api.print_templates_api.gift_core_print_templates",
		"label": "Print templates",
		"seeds": "Starter ECommerce Print Template rows (POS, labels, remito, receiving, …)",
	},
	{
		"id": "taxonomy_i18n",
		"path": "erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n.ensure_taxonomy_i18n",
		"label": "Taxonomy i18n",
		"seeds": "Item Group / Brand alias fields + empty alias gifts (es/en/zh)",
	},
	{
		"id": "silkos_branding",
		"path": "erpnext.erpnext_integrations.ecommerce_api.silkos_branding.ensure_silkos_branding",
		"label": "SilkOS branding",
		"seeds": "SilkOS branding defaults (additive)",
	},
	{
		"id": "price_list_rule_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.price_list_rules.ensure_price_list_rule_fields",
		"label": "Price list rule fields",
		"seeds": "Price List / Item Price auto-rule custom fields",
	},
	{
		"id": "transferencia_auto_defaults",
		"path": "erpnext.erpnext_integrations.ecommerce_api.price_list_rules.ensure_transferencia_auto_defaults",
		"label": "Transferencia price list",
		"seeds": "Selling list Transferencia = Standard Buying +3% (only if base empty)",
	},
	{
		"id": "preferred_delivery_hours",
		"path": "erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields.ensure_preferred_delivery_hours",
		"label": "Preferred delivery hours",
		"seeds": "Customer preferred delivery hours custom field",
	},
	{
		"id": "argentina_iva_conditions",
		"path": "erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields.ensure_argentina_iva_conditions",
		"label": "Argentina IVA conditions",
		"seeds": "IVA condition catalog / customer tax options",
	},
	{
		"id": "guest_preorder_status_options",
		"path": "erpnext.erpnext_integrations.ecommerce_api.api.ensure_guest_preorder_status_options",
		"label": "Guest preorder statuses",
		"seeds": "Guest preorder status select options",
	},
	{
		"id": "po_invoice_ref_field",
		"path": "erpnext.erpnext_integrations.ecommerce_api.buying_api.ensure_po_invoice_ref_field",
		"label": "PO invoice ref field",
		"seeds": "Purchase Order supplier invoice reference custom field",
	},
	{
		"id": "coupon_custom_fields",
		"path": "erpnext.erpnext_integrations.ecommerce_api.coupons_api.ensure_coupon_custom_fields",
		"label": "Coupon fields",
		"seeds": "Coupon custom fields",
	},
	{
		"id": "tax_catalog",
		"path": "erpnext.erpnext_integrations.ecommerce_api.tax_api.ensure_tax_catalog_all_companies",
		"label": "Tax catalog",
		"seeds": "Tax templates / IVA catalog for all companies",
	},
	{
		"id": "starter_staff_groups",
		"path": "erpnext.erpnext_integrations.ecommerce_api.employee_api.ensure_starter_staff_groups",
		"label": "Starter staff groups",
		"seeds": "Employee Groups: repositor, caja, ventas, driver, admin (+ empty perm lists)",
	},
)

_RESEED_BY_ID = {row["id"]: row for row in _RESEED_CATALOG}
_RESEED_IDS = frozenset(_RESEED_BY_ID)


def _parse_reseed_steps(steps) -> list[str]:
	"""Return ordered catalog ids to run. None/omitted → all. Explicit [] → error."""
	if steps is None:
		return [row["id"] for row in _RESEED_CATALOG]
	if isinstance(steps, str):
		raw = steps.strip()
		if not raw or raw in ("null", "undefined"):
			return [row["id"] for row in _RESEED_CATALOG]
		if raw.lower() in ("all", "*"):
			return [row["id"] for row in _RESEED_CATALOG]
		try:
			steps = json.loads(raw)
		except Exception:
			steps = [p.strip() for p in raw.split(",") if p.strip()]
	if not isinstance(steps, (list, tuple, set)):
		frappe.throw(_("steps must be a list of reseed ids"), frappe.ValidationError)
	wanted: list[str] = []
	seen: set[str] = set()
	for raw in steps:
		key = cstr(raw or "").strip()
		if not key or key in ("null", "undefined"):
			continue
		if key not in _RESEED_IDS:
			continue
		if key in seen:
			continue
		seen.add(key)
		wanted.append(key)
	# Preserve catalog order, not request order.
	ordered = [row["id"] for row in _RESEED_CATALOG if row["id"] in seen]
	if not ordered:
		frappe.throw(_("Select at least one default to reseed"), frappe.ValidationError)
	return ordered


@frappe.whitelist(allow_guest=True)
def list_reseed_defaults():
	"""Settings → Desarrollador: catalog of selectable additive reseed steps (no PIN)."""
	company = ""
	try:
		company = _resolve_company()
	except Exception:
		pass
	return {
		"ok": True,
		"company": company,
		"steps": [
			{"id": row["id"], "label": row["label"], "seeds": row["seeds"]}
			for row in _RESEED_CATALOG
		],
	}


@frappe.whitelist(allow_guest=True)
def reseed_site_defaults(pin=None, steps=None):
	"""Settings → Desarrollador: re-run selected additive site defaults.

	Requires admin PIN. Never deletes user data — only insert-if-missing ensures.
	``steps``: list of catalog ids (see list_reseed_defaults). Omit / null / \"all\" → all.
	"""
	# Validate selection before PIN so empty checkbox sets fail fast.
	selected = _parse_reseed_steps(steps)
	_require_admin_pin(pin)
	company = ""
	try:
		company = _resolve_company()
	except Exception:
		pass

	ran: list[dict] = []
	ok_n = 0
	err_n = 0
	prev = frappe.flags.ignore_permissions
	frappe.flags.ignore_permissions = True
	try:
		for step_id in selected:
			meta = _RESEED_BY_ID[step_id]
			path = meta["path"]
			entry: dict = {
				"id": step_id,
				"path": path,
				"label": meta["label"],
				"seeds": meta["seeds"],
				"ok": False,
			}
			try:
				fn = frappe.get_attr(path)
				result = fn()
				entry["ok"] = True
				ok_n += 1
				if isinstance(result, dict):
					for key in ("created", "attached", "upgraded", "skipped", "gifted", "fields"):
						if key in result:
							entry[key] = result[key]
					if result.get("ok") is False:
						entry["ok"] = False
						entry["reason"] = result.get("reason") or "reported ok=false"
						ok_n -= 1
						err_n += 1
				elif result is False:
					entry["ok"] = False
					entry["reason"] = "reported false"
					ok_n -= 1
					err_n += 1
			except Exception as e:
				err_n += 1
				entry["error"] = _brief_error(e)
				frappe.log_error(title=f"reseed_site_defaults {step_id}", message=cstr(e))
			ran.append(entry)
		frappe.db.commit()
	finally:
		frappe.flags.ignore_permissions = prev

	checks: dict = {}
	if "gender_masters" in selected:
		checks["gender_prefer_not_to_say"] = bool(frappe.db.exists("DocType", "Gender")) and bool(
			frappe.db.exists("Gender", "Prefer not to say")
		)
	if "crm_party_buckets" in selected:
		from erpnext.erpnext_integrations.ecommerce_api.api import CONSUMIDOR_FINAL_NAME

		checks["consumidor_final"] = bool(
			frappe.db.sql(
				"""
				SELECT name FROM `tabCustomer`
				WHERE LOWER(TRIM(customer_name)) = %s
				LIMIT 1
				""",
				CONSUMIDOR_FINAL_NAME.lower(),
			)
		)
	if "stock_lot_doctype" in selected:
		checks["stock_lot_doctype"] = bool(frappe.db.exists("DocType", "Stock Lot"))

	hard_ok = all(bool(v) for v in checks.values()) if checks else True
	return {
		"ok": err_n == 0 and hard_ok,
		"company": company,
		"selected": selected,
		"steps": ran,
		"ok_count": ok_n,
		"error_count": err_n,
		"checks": checks,
		# Back-compat for older UI:
		"gender_prefer_not_to_say": checks.get("gender_prefer_not_to_say"),
	}
