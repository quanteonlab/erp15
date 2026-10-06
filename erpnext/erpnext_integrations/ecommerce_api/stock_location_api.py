"""Soft multi-location stock estimates — never write Bin / SLE.

Coverage: total = Bin.actual_qty; located = sum(qty_estimate); idk = max(0, total - located).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


def _as_str(v) -> str:
	if v is None:
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _ensure_doctype() -> bool:
	return bool(frappe.db.exists("DocType", "Stock Location Estimate"))


def _bin_qty(item_code: str, warehouse: str) -> float:
	if not item_code or not warehouse:
		return 0.0
	return flt(
		frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			"actual_qty",
		)
		or 0
	)


def _row_dict(doc) -> dict:
	return {
		"name": doc.name,
		"item_code": doc.item_code,
		"item_name": doc.item_name,
		"warehouse": doc.warehouse,
		"location_label": doc.location_label,
		"floor_section_id": doc.floor_section_id,
		"qty_estimate": flt(doc.qty_estimate),
		"lote_code": doc.lote_code,
		"notes": doc.notes,
		"company": doc.company,
		"modified": str(doc.modified) if doc.modified else None,
	}


@frappe.whitelist(allow_guest=True)
def list_stock_location_estimates(item_code=None, warehouse=None):
	"""List soft placement rows for an item (+ optional warehouse filter)."""
	if not _ensure_doctype():
		return {"rows": [], "doctype_missing": 1}
	code = _as_str(item_code)
	wh = _as_str(warehouse)
	if not code:
		frappe.throw(_("item_code is required"))
	filters = {"item_code": code}
	if wh:
		filters["warehouse"] = wh
	rows = frappe.get_all(
		"Stock Location Estimate",
		filters=filters,
		fields=[
			"name",
			"item_code",
			"item_name",
			"warehouse",
			"location_label",
			"floor_section_id",
			"qty_estimate",
			"lote_code",
			"notes",
			"company",
			"modified",
		],
		order_by="modified desc",
		ignore_permissions=True,
	)
	for r in rows:
		r["qty_estimate"] = flt(r.get("qty_estimate"))
		if r.get("modified"):
			r["modified"] = str(r["modified"])
	return {"rows": rows, "doctype_missing": 0}


@frappe.whitelist(allow_guest=True)
def upsert_stock_location_estimate(
	name=None,
	item_code=None,
	warehouse=None,
	location_label=None,
	qty_estimate=None,
	floor_section_id=None,
	lote_code=None,
	notes=None,
	company=None,
):
	"""Create or update a soft location estimate. Does not touch Bin."""
	if not _ensure_doctype():
		frappe.throw(_("Stock Location Estimate DocType missing — run bench migrate"))

	name = _as_str(name)
	code = _as_str(item_code)
	wh = _as_str(warehouse)
	label = _as_str(location_label)

	if name:
		if not frappe.db.exists("Stock Location Estimate", name):
			frappe.throw(_("Estimate {0} not found").format(name))
		frappe.flags.ignore_permissions = True
		try:
			doc = frappe.get_doc("Stock Location Estimate", name)
			if code:
				doc.item_code = code
			if wh:
				doc.warehouse = wh
			if label:
				doc.location_label = label
			if qty_estimate is not None and str(qty_estimate).strip().lower() not in (
				"",
				"null",
				"undefined",
				"none",
			):
				doc.qty_estimate = max(0.0, flt(qty_estimate))
			if floor_section_id is not None:
				doc.floor_section_id = _as_str(floor_section_id) or None
			if lote_code is not None:
				doc.lote_code = _as_str(lote_code) or None
			if notes is not None:
				doc.notes = _as_str(notes) or None
			if company is not None:
				doc.company = _as_str(company) or None
			if not doc.item_name and doc.item_code:
				doc.item_name = frappe.db.get_value("Item", doc.item_code, "item_name") or doc.item_code
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return {"ok": True, "row": _row_dict(doc)}
		finally:
			frappe.flags.ignore_permissions = False

	if not code:
		frappe.throw(_("item_code is required"))
	if not wh:
		frappe.throw(_("warehouse is required"))
	if not label:
		frappe.throw(_("location_label is required"))
	if not frappe.db.exists("Item", code):
		frappe.throw(_("Item {0} not found").format(code))
	if not frappe.db.exists("Warehouse", wh):
		frappe.throw(_("Warehouse {0} not found").format(wh))

	qty = max(0.0, flt(qty_estimate or 0))
	item_name = frappe.db.get_value("Item", code, "item_name") or code
	comp = _as_str(company)
	if not comp:
		comp = frappe.db.get_value("Warehouse", wh, "company")

	frappe.flags.ignore_permissions = True
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Stock Location Estimate",
				"item_code": code,
				"item_name": item_name,
				"warehouse": wh,
				"location_label": label,
				"floor_section_id": _as_str(floor_section_id) or None,
				"qty_estimate": qty,
				"lote_code": _as_str(lote_code) or None,
				"notes": _as_str(notes) or None,
				"company": comp or None,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return {"ok": True, "row": _row_dict(doc)}
	finally:
		frappe.flags.ignore_permissions = False


@frappe.whitelist(allow_guest=True)
def delete_stock_location_estimate(name=None):
	"""Delete a soft estimate. Does not touch Bin."""
	if not _ensure_doctype():
		frappe.throw(_("Stock Location Estimate DocType missing — run bench migrate"))
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists("Stock Location Estimate", name):
		frappe.throw(_("Estimate {0} not found").format(name))
	frappe.delete_doc("Stock Location Estimate", name, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "name": name}


@frappe.whitelist(allow_guest=True)
def get_stock_location_coverage(item_code=None, warehouse=None):
	"""Bin total vs soft located sum vs IDK for one item/warehouse (or list)."""
	if not _ensure_doctype():
		return {
			"rows": [],
			"doctype_missing": 1,
			"total": 0,
			"located": 0,
			"idk": 0,
		}

	code = _as_str(item_code)
	wh = _as_str(warehouse)

	# Single-item coverage (product edit).
	if code and wh:
		listed = list_stock_location_estimates(item_code=code, warehouse=wh)
		rows = listed.get("rows") or []
		located = sum(flt(r.get("qty_estimate")) for r in rows)
		total = _bin_qty(code, wh)
		idk = max(0.0, total - located)
		return {
			"item_code": code,
			"warehouse": wh,
			"total": total,
			"located": located,
			"idk": idk,
			"over_allocated": max(0.0, located - total),
			"rows": rows,
			"doctype_missing": 0,
		}

	# Bulk coverage: group by item+warehouse (light report).
	filters = {}
	if code:
		filters["item_code"] = code
	if wh:
		filters["warehouse"] = wh
	est_rows = frappe.get_all(
		"Stock Location Estimate",
		filters=filters or None,
		fields=["item_code", "warehouse", "qty_estimate", "location_label", "lote_code", "name"],
		ignore_permissions=True,
		limit_page_length=2000,
	)
	from collections import defaultdict

	grouped = defaultdict(lambda: {"located": 0.0, "rows": []})
	for r in est_rows:
		ic = _as_str(r.get("item_code"))
		wname = _as_str(r.get("warehouse"))
		if not ic or not wname:
			continue
		g = grouped[(ic, wname)]
		g["located"] += flt(r.get("qty_estimate"))
		g["rows"].append(r)

	out_rows = []
	for (ic, wname), g in grouped.items():
		total = _bin_qty(ic, wname)
		located = flt(g["located"])
		out_rows.append(
			{
				"item_code": ic,
				"warehouse": wname,
				"total": total,
				"located": located,
				"idk": max(0.0, total - located),
				"over_allocated": max(0.0, located - total),
				"estimate_count": len(g["rows"]),
			}
		)
	out_rows.sort(key=lambda x: (x["item_code"], x["warehouse"]))
	return {"rows": out_rows, "doctype_missing": 0}
