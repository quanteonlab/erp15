"""Simplified Purchase Order API for Logistics → Buying."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company


def _as_str(v) -> str:
	if v is None:
		return ""
	if isinstance(v, (list, dict, tuple)):
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _as_int(v, default: int, lo: int = 0, hi: int = 500) -> int:
	try:
		n = cint(v)
	except Exception:
		n = default
	if n < lo:
		n = default
	return min(max(n, lo), hi)


def _parse_items(items):
	if isinstance(items, str):
		try:
			items = json.loads(items) if items.strip() else []
		except Exception:
			items = []
	if items is None:
		items = []
	if not isinstance(items, list):
		frappe.throw(_("items must be a list"))
	clean = []
	for raw in items:
		if not isinstance(raw, dict):
			continue
		code = _as_str(raw.get("item_code"))
		qty = abs(flt(raw.get("qty")))
		if not code or qty <= 0:
			continue
		if not frappe.db.exists("Item", code):
			frappe.throw(_("Item {0} not found").format(code))
		clean.append(
			{
				"item_code": code,
				"item_name": _as_str(raw.get("item_name")) or code,
				"qty": qty,
				"rate": abs(flt(raw.get("rate"))),
				"uom": _as_str(raw.get("uom")) or None,
				"schedule_date": _as_str(raw.get("schedule_date")) or None,
				"description": _as_str(raw.get("notes") or raw.get("description")) or None,
			}
		)
	return clean


@frappe.whitelist(allow_guest=True)
def create_purchase_order(
	supplier=None,
	schedule_date=None,
	items=None,
	company=None,
	submit=0,
	notes=None,
):
	"""Create a Purchase Order with one shared expected (schedule) date."""
	supplier = _as_str(supplier)
	if not supplier:
		frappe.throw(_("supplier is required"))
	if not frappe.db.exists("Supplier", supplier):
		frappe.throw(_("Supplier {0} not found").format(supplier))

	sched = _as_str(schedule_date) or nowdate()
	try:
		sched = str(getdate(sched))
	except Exception:
		sched = nowdate()

	clean = _parse_items(items)
	if not clean:
		frappe.throw(_("Add at least one item"))

	company = resolve_company(company) or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("No company configured"))
	do_submit = 1 if cint(submit) else 0
	note = _as_str(notes)

	po_items = []
	for row in clean:
		line = {
			"item_code": row["item_code"],
			"qty": row["qty"],
			"rate": row["rate"],
			"schedule_date": row["schedule_date"] or sched,
		}
		if row.get("uom"):
			line["uom"] = row["uom"]
		if row.get("description"):
			line["description"] = row["description"]
		po_items.append(line)

	doc = frappe.get_doc(
		{
			"doctype": "Purchase Order",
			"supplier": supplier,
			"company": company,
			"transaction_date": nowdate(),
			"schedule_date": sched,
			"items": po_items,
		}
	)
	doc.insert(ignore_permissions=True)
	if note:
		try:
			doc.add_comment("Comment", note)
		except Exception:
			pass
	if do_submit:
		try:
			doc.submit()
		except Exception:
			frappe.db.rollback()
			# Keep as draft if submit fails (missing accounts, etc.)
			doc = frappe.get_doc("Purchase Order", doc.name)
			frappe.log_error(frappe.get_traceback(), "buying_api.create_purchase_order submit")
			frappe.db.commit()
			return {
				"ok": True,
				"name": doc.name,
				"docstatus": cint(doc.docstatus),
				"submitted": 0,
				"grand_total": flt(doc.grand_total),
				"warning": _("Saved as draft — submit failed (check accounts / permissions)"),
			}

	frappe.db.commit()
	return {
		"ok": True,
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"submitted": do_submit,
		"grand_total": flt(doc.grand_total),
		"supplier": doc.supplier,
		"schedule_date": str(doc.schedule_date) if doc.schedule_date else sched,
	}


def _pipeline_label(docstatus: int, status: str, per_received: float, per_billed: float, days_to_eta) -> str:
	"""Coarse buying pipeline for filters / badges."""
	st = (status or "").lower()
	if cint(docstatus) == 0 or "draft" in st:
		return "draft"
	if "cancel" in st:
		return "cancelled"
	if "closed" in st or "complet" in st:
		return "done"
	if days_to_eta is not None and days_to_eta < 0 and flt(per_received) < 99.5:
		return "overdue"
	if flt(per_received) >= 99.5 and flt(per_billed) >= 99.5:
		return "done"
	if flt(per_received) >= 99.5:
		return "to_bill"
	if flt(per_received) > 0.5:
		return "partial"
	return "to_receive"


def _enrich_po_rows(rows: list) -> list:
	"""Attach line aggregates, brand mix, and ETA metrics for Tables → Compras."""
	if not rows:
		return []
	names = [r.name for r in rows]
	today = getdate(nowdate())

	item_rows = frappe.get_all(
		"Purchase Order Item",
		filters={"parent": ["in", names]},
		fields=[
			"parent",
			"item_code",
			"item_name",
			"qty",
			"received_qty",
			"amount",
			"rate",
		],
		ignore_permissions=True,
	)
	by_po: dict[str, list] = {}
	codes: set[str] = set()
	for it in item_rows:
		by_po.setdefault(it.parent, []).append(it)
		if it.item_code:
			codes.add(it.item_code)

	brand_map: dict[str, str] = {}
	if codes:
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", list(codes)]},
			fields=["name", "brand"],
			ignore_permissions=True,
		):
			if row.brand:
				brand_map[row.name] = row.brand

	out = []
	for r in rows:
		lines = by_po.get(r.name, [])
		qty_ordered = sum(flt(x.qty) for x in lines)
		qty_received = sum(flt(x.received_qty) for x in lines)
		line_count = len(lines)
		sku_count = len({x.item_code for x in lines if x.item_code})
		preview = []
		for x in lines[:3]:
			label = _as_str(x.item_name) or _as_str(x.item_code)
			if label:
				preview.append(label)
		brands = sorted(
			{
				brand_map[x.item_code]
				for x in lines
				if x.item_code and brand_map.get(x.item_code)
			}
		)
		tx = getdate(r.transaction_date) if r.transaction_date else None
		sched = getdate(r.schedule_date) if r.schedule_date else None
		age_days = (today - tx).days if tx else None
		days_to_eta = (sched - today).days if sched else None
		per_recv = flt(r.per_received)
		per_bill = flt(r.per_billed)
		grand = flt(r.grand_total)
		open_receive_value = max(0.0, grand * (100.0 - min(per_recv, 100.0)) / 100.0)
		open_bill_value = max(0.0, grand * (100.0 - min(per_bill, 100.0)) / 100.0)
		pipeline = _pipeline_label(cint(r.docstatus), r.status or "", per_recv, per_bill, days_to_eta)

		out.append(
			{
				"name": r.name,
				"supplier": r.supplier,
				"supplier_name": r.supplier_name,
				"company": getattr(r, "company", None),
				"owner": getattr(r, "owner", None),
				"modified": str(r.modified) if getattr(r, "modified", None) else None,
				"transaction_date": str(r.transaction_date) if r.transaction_date else None,
				"schedule_date": str(r.schedule_date) if r.schedule_date else None,
				"grand_total": grand,
				"net_total": flt(getattr(r, "net_total", 0) or 0),
				"total_taxes_and_charges": flt(getattr(r, "total_taxes_and_charges", 0) or 0),
				"status": r.status,
				"docstatus": cint(r.docstatus),
				"currency": r.currency,
				"per_received": per_recv,
				"per_billed": per_bill,
				"line_count": line_count,
				"sku_count": sku_count,
				"qty_ordered": qty_ordered,
				"qty_received": qty_received,
				"qty_pending": max(0.0, qty_ordered - qty_received),
				"items_preview": preview,
				"brands": brands,
				"brands_label": ", ".join(brands[:3]) + ("…" if len(brands) > 3 else ""),
				"age_days": age_days,
				"days_to_eta": days_to_eta,
				"open_receive_value": open_receive_value,
				"open_bill_value": open_bill_value,
				"pipeline": pipeline,
				"remarks": None,
			}
		)
	return out


@frappe.whitelist(allow_guest=True)
def list_purchase_orders(
	supplier=None,
	start=0,
	page_length=30,
	status=None,
	pipeline=None,
	search=None,
	include_cancelled=0,
):
	"""Purchase Orders for Logistics Buying + Tables → Compras."""
	start = _as_int(start, 0, lo=0, hi=100000)
	page_length = _as_int(page_length, 30, lo=1, hi=200)
	filters: dict = {}
	if cint(include_cancelled):
		filters["docstatus"] = ["<", 3]
	else:
		filters["docstatus"] = ["<", 2]

	sup = _as_str(supplier)
	if sup:
		filters["supplier"] = sup
	st = _as_str(status)
	if st and st.lower() not in ("all", "*", "any"):
		filters["status"] = st

	or_filters = None
	q = _as_str(search)
	if q:
		or_filters = [
			["name", "like", f"%{q}%"],
			["supplier", "like", f"%{q}%"],
			["supplier_name", "like", f"%{q}%"],
		]

	rows = frappe.get_all(
		"Purchase Order",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"supplier",
			"supplier_name",
			"company",
			"owner",
			"modified",
			"transaction_date",
			"schedule_date",
			"grand_total",
			"net_total",
			"total_taxes_and_charges",
			"status",
			"docstatus",
			"currency",
			"per_received",
			"per_billed",
		],
		order_by="modified desc",
		limit_start=start,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = (
		len(
			frappe.get_all(
				"Purchase Order",
				filters=filters,
				or_filters=or_filters,
				pluck="name",
				ignore_permissions=True,
			)
		)
		if or_filters
		else frappe.db.count("Purchase Order", filters)
	)
	enriched = _enrich_po_rows(rows)

	pipe = _as_str(pipeline).lower()
	if pipe and pipe not in ("all", "*", "any"):
		enriched = [r for r in enriched if r.get("pipeline") == pipe]

	return {"ok": True, "total": cint(total), "rows": enriched}


@frappe.whitelist(allow_guest=True)
def get_purchase_order_detail(name=None):
	"""Single PO with lines for Tables → Compras detail panel."""
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False

	lines = []
	for row in doc.items or []:
		brand = frappe.db.get_value("Item", row.item_code, "brand") if row.item_code else None
		qty = flt(row.qty)
		recv = flt(row.received_qty)
		lines.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"brand": brand,
				"qty": qty,
				"received_qty": recv,
				"pending_qty": max(0.0, qty - recv),
				"rate": flt(row.rate),
				"amount": flt(row.amount),
				"uom": row.uom,
				"schedule_date": str(row.schedule_date) if row.schedule_date else None,
				"description": _as_str(row.description)[:240] or None,
			}
		)

	base = _enrich_po_rows(
		[
			frappe._dict(
				{
					"name": doc.name,
					"supplier": doc.supplier,
					"supplier_name": doc.supplier_name,
					"company": doc.company,
					"owner": doc.owner,
					"modified": doc.modified,
					"transaction_date": doc.transaction_date,
					"schedule_date": doc.schedule_date,
					"grand_total": doc.grand_total,
					"net_total": doc.net_total,
					"total_taxes_and_charges": doc.total_taxes_and_charges,
					"status": doc.status,
					"docstatus": doc.docstatus,
					"currency": doc.currency,
					"per_received": doc.per_received,
					"per_billed": doc.per_billed,
					"remarks": None,
				}
			)
		]
	)[0]
	base["lines"] = lines
	return {"ok": True, "order": base}
