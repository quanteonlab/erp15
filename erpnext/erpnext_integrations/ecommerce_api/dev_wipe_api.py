"""Developer wipe tools (Settings → Desarrollador, == unlock).

Hard-deletes transactional docs for the active company. Cascade expands:
- customers → related orders/invoices/DNs/payments, then customers
- orders → related invoices/DNs/payments, then sales orders
- invoices → related payments, then sales invoices
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


VALID_TARGETS = ("orders", "invoices", "customers", "employees", "products", "categories")
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
		if key in VALID_TARGETS:
			out.add(key)
	return out


def _force_cancel_delete(doctype: str, name: str) -> tuple[bool, str | None]:
	"""Cancel (if submitted) then force-delete. Returns (gone, error)."""
	if not name or not frappe.db.exists(doctype, name):
		return True, None
	try:
		# Drop contact/address links that often block SO/SI delete.
		if doctype in ("Sales Order", "Sales Invoice", "Delivery Note"):
			updates = {}
			for field in ("contact_person", "customer_address", "shipping_address_name", "company_address"):
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


def _unlink_party_contacts(customer: str) -> None:
	"""Remove Contact/Address Dynamic Links so Customer delete can proceed."""
	for parenttype in ("Contact", "Address"):
		links = frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Customer", "link_name": customer, "parenttype": parenttype},
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

	return {
		"payment_entries": payment_entries,
		"pos_invoices": pos_invoices,
		"sales_invoices": sales_invoices,
		"delivery_notes": delivery_notes,
		"sales_orders": sales_orders if want_orders else [],
		"customers": customers if want_customers else [],
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
			_("Select at least one target: orders, invoices, customers, employees, products, categories")
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

		for i, cust in enumerate(plan["customers"]):
			try:
				_unlink_party_contacts(cust)
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
