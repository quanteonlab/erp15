"""Stock Lot (logical receive lots) — soft sell-by estimates + FIFO consume.

Lots are created on recepción without requiring Item.has_batch_no. Sell-by is
days-from-receive (plazo comercial). On sell, callers should consume oldest
open lots first (estimated FEFO/FIFO).
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate


def _as_str(v) -> str:
	if v is None:
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _parse_json(raw, default=None):
	if raw is None:
		return default
	if isinstance(raw, (dict, list)):
		return raw
	if isinstance(raw, str):
		s = raw.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			return default
		try:
			return json.loads(s)
		except Exception:
			return default
	return default


def ensure_stock_lot_doctype():
	"""No-op if migrated; useful for local benches mid-deploy."""
	return bool(frappe.db.exists("DocType", "Stock Lot"))


def create_stock_lot(
	*,
	item_code,
	qty,
	warehouse=None,
	receive_date=None,
	sell_by_days=None,
	unit_cost=None,
	stock_entry=None,
	purchase_order=None,
	supplier=None,
	reference=None,
	session_id=None,
	company=None,
	commit=False,
):
	"""Insert one Stock Lot row. Returns name."""
	if not ensure_stock_lot_doctype():
		frappe.throw(_("Stock Lot DocType missing — run bench migrate"))
	code = _as_str(item_code)
	qty = flt(qty)
	if not code or qty <= 0:
		frappe.throw(_("item_code and qty are required"))
	recv = _as_str(receive_date) or nowdate()
	try:
		recv = str(getdate(recv))
	except Exception:
		recv = nowdate()
	days = None
	if sell_by_days not in (None, "", "null", "undefined"):
		try:
			days = max(0, cint(sell_by_days))
		except (TypeError, ValueError):
			days = 0
	sell_by_date = add_days(getdate(recv), days) if days else None
	item_name = frappe.db.get_value("Item", code, "item_name") or code
	doc = frappe.get_doc(
		{
			"doctype": "Stock Lot",
			"item_code": code,
			"item_name": item_name,
			"warehouse": _as_str(warehouse) or None,
			"receive_date": recv,
			"sell_by_days": days or 0,
			"sell_by_date": sell_by_date,
			"qty_received": qty,
			"qty_remaining": qty,
			"unit_cost": flt(unit_cost or 0),
			"stock_entry": _as_str(stock_entry) or None,
			"purchase_order": _as_str(purchase_order) or None,
			"supplier": _as_str(supplier) or None,
			"reference": _as_str(reference) or None,
			"session_id": _as_str(session_id) or None,
			"company": _as_str(company) or None,
			"status": "Open",
		}
	)
	doc.insert(ignore_permissions=True)
	if commit:
		frappe.db.commit()
	return doc.name


def consume_stock_lots_fifo(item_code, qty, warehouse=None, commit=False):
	"""Decrement oldest open lots first (soft FIFO). Returns allocations.

	Does not throw if stock lots are missing / short — depletes what exists.
	"""
	if not ensure_stock_lot_doctype():
		return {"ok": True, "allocations": [], "remaining_unallocated": flt(qty)}
	code = _as_str(item_code)
	need = flt(qty)
	if not code or need <= 0:
		return {"ok": True, "allocations": [], "remaining_unallocated": 0.0}

	filters = {
		"item_code": code,
		"qty_remaining": [">", 0],
		"status": ["in", ["Open", "Expired Est."]],
	}
	wh = _as_str(warehouse)
	if wh:
		filters["warehouse"] = wh

	rows = frappe.get_all(
		"Stock Lot",
		filters=filters,
		fields=["name", "qty_remaining", "receive_date", "sell_by_date"],
		order_by="receive_date asc, creation asc",
		ignore_permissions=True,
	)
	allocations = []
	left = need
	for row in rows:
		if left <= 0.0001:
			break
		take = min(flt(row.qty_remaining), left)
		if take <= 0:
			continue
		new_rem = flt(row.qty_remaining) - take
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Stock Lot", row.name)
		doc.qty_remaining = max(0.0, new_rem)
		doc.save(ignore_permissions=True)
		frappe.flags.ignore_permissions = False
		allocations.append({"lot": row.name, "qty": take})
		left -= take

	if commit:
		frappe.db.commit()
	return {
		"ok": True,
		"allocations": allocations,
		"remaining_unallocated": max(0.0, round(left, 6)),
	}


@frappe.whitelist(allow_guest=True)
def list_stock_lots(
	status=None,
	item_code=None,
	supplier=None,
	purchase_order=None,
	warehouse=None,
	search=None,
	days_within=None,
	only_with_remaining=1,
	limit=200,
	offset=0,
):
	"""List logical lots for Tables → Lotes."""
	if not ensure_stock_lot_doctype():
		return {"rows": [], "total": 0, "doctype_missing": 1}

	limit = max(1, min(cint(limit) or 200, 500))
	offset = max(0, cint(offset) or 0)
	filters = {}
	status = _as_str(status)
	if status and status.lower() not in ("all", ""):
		# Soft buckets for UI
		if status == "por_vencer":
			# open lots with sell_by in next N days (default 14)
			pass  # applied below with date range
		elif status == "vencido":
			filters["status"] = "Expired Est."
		elif status == "depleted":
			filters["status"] = "Depleted"
		elif status == "ok":
			filters["status"] = "Open"
		else:
			filters["status"] = status

	if _as_str(item_code):
		filters["item_code"] = _as_str(item_code)
	if _as_str(supplier):
		filters["supplier"] = _as_str(supplier)
	if _as_str(purchase_order):
		filters["purchase_order"] = _as_str(purchase_order)
	if _as_str(warehouse):
		filters["warehouse"] = _as_str(warehouse)
	if cint(only_with_remaining):
		filters["qty_remaining"] = [">", 0]

	or_filters = None
	q = _as_str(search)
	if q:
		or_filters = [
			["item_code", "like", f"%{q}%"],
			["item_name", "like", f"%{q}%"],
			["reference", "like", f"%{q}%"],
			["purchase_order", "like", f"%{q}%"],
			["name", "like", f"%{q}%"],
			["supplier", "like", f"%{q}%"],
		]

	# Date window for por_vencer
	extra = []
	if status == "por_vencer":
		horizon = max(1, cint(days_within) or 14)
		today = getdate(nowdate())
		until = add_days(today, horizon)
		filters["status"] = ["in", ["Open", "Expired Est."]]
		filters["sell_by_date"] = ["between", [str(today), str(until)]]

	rows = frappe.get_all(
		"Stock Lot",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"item_code",
			"item_name",
			"warehouse",
			"receive_date",
			"sell_by_days",
			"sell_by_date",
			"status",
			"qty_received",
			"qty_remaining",
			"unit_cost",
			"stock_entry",
			"purchase_order",
			"supplier",
			"reference",
			"session_id",
			"company",
			"modified",
		],
		order_by="sell_by_date asc, receive_date asc",
		limit_start=offset,
		limit_page_length=limit,
		ignore_permissions=True,
	)
	total = frappe.db.count("Stock Lot", filters=filters) if not or_filters else len(
		frappe.get_all(
			"Stock Lot",
			filters=filters,
			or_filters=or_filters,
			pluck="name",
			ignore_permissions=True,
		)
	)

	today = getdate(nowdate())
	out = []
	for r in rows:
		days_left = None
		if r.get("sell_by_date"):
			try:
				days_left = (getdate(r["sell_by_date"]) - today).days
			except Exception:
				days_left = None
		row = dict(r)
		row["days_left"] = days_left
		# Soft label for UI without requiring a cron rewrite.
		if (
			row.get("status") == "Open"
			and days_left is not None
			and days_left < 0
			and flt(row.get("qty_remaining") or 0) > 0
		):
			row["status"] = "Expired Est."
		out.append(row)

	return {"rows": out, "total": total}


@frappe.whitelist(allow_guest=True)
def get_stock_lot(name=None):
	name = _as_str(name)
	if not name or not frappe.db.exists("Stock Lot", name):
		frappe.throw(_("Lot not found"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Stock Lot", name)
	frappe.flags.ignore_permissions = False
	d = doc.as_dict()
	today = getdate(nowdate())
	d["days_left"] = (getdate(d.sell_by_date) - today).days if d.get("sell_by_date") else None
	return d


@frappe.whitelist(allow_guest=True)
def consume_lots(item_code=None, qty=None, warehouse=None):
	"""Whitelist wrapper for soft FIFO consume (POS / SO submit)."""
	return consume_stock_lots_fifo(item_code, qty, warehouse=warehouse, commit=True)
