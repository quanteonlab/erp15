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


def ensure_stock_lot_movement_doctype():
	return bool(frappe.db.exists("DocType", "Stock Lot Movement"))


def ensure_stock_lot_doctype_status():
	"""Reseed-friendly report: Stock Lot cannot be created here — needs migrate."""
	ok = ensure_stock_lot_doctype()
	return {
		"ok": ok,
		"reason": None if ok else "Stock Lot DocType missing — run bench migrate",
		"doctype": "Stock Lot",
	}


def _record_stock_lot_movement(
	*,
	stock_lot,
	item_code,
	qty,
	direction,
	warehouse=None,
	posting_date=None,
	voucher_type=None,
	voucher_name=None,
	company=None,
):
	"""Best-effort movement row for i062 timeline. Never throws to callers."""
	if not ensure_stock_lot_movement_doctype():
		return None
	lot = _as_str(stock_lot)
	code = _as_str(item_code)
	q = flt(qty)
	direction = "In" if _as_str(direction).lower() in ("in", "+") else "Out"
	if not lot or not code or q <= 0:
		return None
	try:
		recv = _as_str(posting_date) or nowdate()
		try:
			recv = str(getdate(recv))
		except Exception:
			recv = nowdate()
		doc = frappe.get_doc(
			{
				"doctype": "Stock Lot Movement",
				"stock_lot": lot,
				"item_code": code,
				"warehouse": _as_str(warehouse) or None,
				"posting_date": recv,
				"direction": direction,
				"qty": q,
				"voucher_type": _as_str(voucher_type) or None,
				"voucher_name": _as_str(voucher_name) or None,
				"company": _as_str(company) or None,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(frappe.get_traceback(), "stock lot movement insert")
		return None


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
	_record_stock_lot_movement(
		stock_lot=doc.name,
		item_code=code,
		qty=qty,
		direction="In",
		warehouse=_as_str(warehouse) or None,
		posting_date=recv,
		voucher_type="Stock Entry" if _as_str(stock_entry) else "Stock Lot",
		voucher_name=_as_str(stock_entry) or doc.name,
		company=_as_str(company) or None,
	)
	if commit:
		frappe.db.commit()
	return doc.name


def consume_stock_lots_fifo(
	item_code,
	qty,
	warehouse=None,
	commit=False,
	voucher_type=None,
	voucher_name=None,
	posting_date=None,
):
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
		fields=["name", "qty_remaining", "receive_date", "sell_by_date", "company"],
		order_by="receive_date asc, creation asc",
		ignore_permissions=True,
	)
	allocations = []
	left = need
	post_d = _as_str(posting_date) or nowdate()
	try:
		post_d = str(getdate(post_d))
	except Exception:
		post_d = nowdate()
	v_type = _as_str(voucher_type) or "Stock Exit"
	v_name = _as_str(voucher_name) or None
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
		_record_stock_lot_movement(
			stock_lot=row.name,
			item_code=code,
			qty=take,
			direction="Out",
			warehouse=wh or None,
			posting_date=post_d,
			voucher_type=v_type,
			voucher_name=v_name,
			company=_as_str(getattr(row, "company", None)) or None,
		)
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


def _bucket_key_and_end(d, grain: str):
	"""Return (bucket_key, bucket_end_date) for a date."""
	from calendar import monthrange
	from datetime import timedelta

	d = getdate(d)
	g = (grain or "day").lower()
	if g in ("month", "months", "m"):
		end = getdate(f"{d.year}-{d.month:02d}-{monthrange(d.year, d.month)[1]}")
		return f"{d.year}-{d.month:02d}", end
	if g in ("week", "weeks", "w"):
		# ISO week: Monday start, Sunday end
		start = d - timedelta(days=d.weekday())
		end = start + timedelta(days=6)
		iso = d.isocalendar()
		return f"{iso[0]}-W{iso[1]:02d}", end
	return str(d), d


def _iter_bucket_ends(from_d, to_d, grain: str):
	from calendar import monthrange
	from datetime import timedelta

	from_d = getdate(from_d)
	to_d = getdate(to_d)
	g = (grain or "day").lower()
	out = []
	seen = set()
	cur = from_d
	guard = 0
	while cur <= to_d and guard < 400:
		guard += 1
		key, end = _bucket_key_and_end(cur, g)
		if key not in seen:
			seen.add(key)
			out.append((key, end))
		if g in ("month", "months", "m"):
			# jump to first of next month
			y, m = cur.year, cur.month + 1
			if m > 12:
				y, m = y + 1, 1
			cur = getdate(f"{y}-{m:02d}-01")
		elif g in ("week", "weeks", "w"):
			cur = cur + timedelta(days=7 - cur.weekday())
		else:
			cur = cur + timedelta(days=1)
	return out


def _load_posted_movements(item_code, warehouse=None):
	"""Posted movements + synthetic In from lots when movement log empty."""
	code = _as_str(item_code)
	wh = _as_str(warehouse)
	events = []
	if ensure_stock_lot_movement_doctype():
		filters = {"item_code": code}
		if wh:
			filters["warehouse"] = wh
		rows = frappe.get_all(
			"Stock Lot Movement",
			filters=filters,
			fields=[
				"name",
				"stock_lot",
				"item_code",
				"warehouse",
				"posting_date",
				"direction",
				"qty",
				"voucher_type",
				"voucher_name",
			],
			order_by="posting_date asc, creation asc",
			ignore_permissions=True,
		)
		for r in rows:
			events.append(
				{
					"lot": r.stock_lot,
					"qty": flt(r.qty),
					"direction": "in" if r.direction == "In" else "out",
					"expected": False,
					"posting_date": str(getdate(r.posting_date)),
					"voucher_type": r.voucher_type,
					"voucher_name": r.voucher_name,
					"movement": r.name,
				}
			)
	if events:
		return events

	# Fallback: synthesize In from lot receipts (pre-movement-log lots).
	filters = {"item_code": code}
	if wh:
		filters["warehouse"] = wh
	lots = frappe.get_all(
		"Stock Lot",
		filters=filters,
		fields=["name", "qty_received", "receive_date", "warehouse"],
		order_by="receive_date asc, creation asc",
		ignore_permissions=True,
	)
	for lot in lots:
		q = flt(lot.qty_received)
		if q <= 0:
			continue
		events.append(
			{
				"lot": lot.name,
				"qty": q,
				"direction": "in",
				"expected": False,
				"posting_date": str(getdate(lot.receive_date or nowdate())),
				"voucher_type": "Stock Lot",
				"voucher_name": lot.name,
				"movement": None,
				"synthetic": True,
			}
		)
	return events


def _forecast_out_events(item_code, from_d, to_d, warehouse=None):
	"""Expected outs from open SO lines by delivery_date (soft FIFO colors)."""
	code = _as_str(item_code)
	if not code:
		return []
	try:
		from_d = getdate(from_d)
		to_d = getdate(to_d)
		# Submitted SOs still open for delivery
		rows = frappe.db.sql(
			"""
			SELECT so.name as so_name, so.delivery_date, soi.qty, soi.warehouse, soi.stock_qty
			FROM `tabSales Order Item` soi
			INNER JOIN `tabSales Order` so ON so.name = soi.parent
			WHERE soi.item_code = %s
			  AND so.docstatus = 1
			  AND IFNULL(so.status, '') NOT IN ('Completed', 'Closed', 'Cancelled')
			  AND so.delivery_date IS NOT NULL
			  AND so.delivery_date BETWEEN %s AND %s
			""",
			(code, str(from_d), str(to_d)),
			as_dict=True,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "lot timeline forecast outs")
		return []
	# Open lots for soft FIFO coloring (ok if none — unallocated expected outs).
	lot_filters = {
		"item_code": code,
		"qty_remaining": [">", 0],
		"status": ["in", ["Open", "Expired Est."]],
	}
	wh = _as_str(warehouse)
	if wh:
		lot_filters["warehouse"] = wh
	try:
		lots = frappe.get_all(
			"Stock Lot",
			filters=lot_filters,
			fields=["name", "qty_remaining"],
			order_by="receive_date asc, creation asc",
			ignore_permissions=True,
		) or []
	except Exception:
		lots = []
	remaining = {r.name: flt(r.qty_remaining) for r in lots}
	lot_order = [r.name for r in lots]
	events = []
	for row in rows or []:
		need = flt(row.stock_qty or row.qty or 0)
		if need <= 0:
			continue
		due = str(getdate(row.delivery_date))
		left = need
		for lot_name in lot_order:
			if left <= 0.0001:
				break
			avail = remaining.get(lot_name) or 0
			if avail <= 0:
				continue
			take = min(avail, left)
			remaining[lot_name] = avail - take
			left -= take
			events.append(
				{
					"lot": lot_name,
					"qty": take,
					"direction": "out",
					"expected": True,
					"posting_date": due,
					"voucher_type": "Sales Order",
					"voucher_name": row.so_name,
					"movement": None,
				}
			)
		if left > 0.0001:
			# Unallocated expected out (shortage) — no lot color
			events.append(
				{
					"lot": None,
					"qty": left,
					"direction": "out",
					"expected": True,
					"posting_date": due,
					"voucher_type": "Sales Order",
					"voucher_name": row.so_name,
					"movement": None,
					"unallocated": True,
				}
			)
	return events


def _forecast_in_events(item_code, from_d, to_d):
	"""Expected ins from open Purchase Orders by schedule_date."""
	code = _as_str(item_code)
	if not code:
		return []
	try:
		from_d = getdate(from_d)
		to_d = getdate(to_d)
		rows = frappe.db.sql(
			"""
			SELECT po.name as po_name,
			       COALESCE(poi.schedule_date, po.schedule_date) as due_date,
			       poi.qty, poi.stock_qty
			FROM `tabPurchase Order Item` poi
			INNER JOIN `tabPurchase Order` po ON po.name = poi.parent
			WHERE poi.item_code = %s
			  AND po.docstatus = 1
			  AND IFNULL(po.status, '') NOT IN ('Completed', 'Closed', 'Cancelled')
			  AND COALESCE(poi.schedule_date, po.schedule_date) IS NOT NULL
			  AND COALESCE(poi.schedule_date, po.schedule_date) BETWEEN %s AND %s
			""",
			(code, str(from_d), str(to_d)),
			as_dict=True,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "lot timeline forecast inns")
		return []
	events = []
	for row in rows or []:
		q = flt(row.stock_qty or row.qty or 0)
		if q <= 0:
			continue
		events.append(
			{
				"lot": None,
				"qty": q,
				"direction": "in",
				"expected": True,
				"posting_date": str(getdate(row.due_date)),
				"voucher_type": "Purchase Order",
				"voucher_name": row.po_name,
				"movement": None,
				"petition": True,
			}
		)
	return events


def _empty_lot_timeline(item_code="", grain="day", **extra):
	"""Safe empty payload — never break Product Activity when lots are absent."""
	out = {
		"item_code": _as_str(item_code),
		"grain": _as_str(grain) or "day",
		"today": nowdate(),
		"lots": [],
		"buckets": [],
		"doctype_missing": 0 if ensure_stock_lot_doctype() else 1,
		"movement_doctype_missing": 0 if ensure_stock_lot_movement_doctype() else 1,
		"soft": 1,
	}
	out.update(extra or {})
	return out


@frappe.whitelist(allow_guest=True)
def get_item_lot_timeline(
	item_code=None,
	warehouse=None,
	grain="day",
	from_date=None,
	to_date=None,
	include_forecast=1,
):
	"""Stacked lot timeline for Product Activity (i062).

	Soft by design: missing lots, missing DocTypes, negative Bin, or forecast
	SQL failures return an empty chart — never HTTP 500 / TypeError.
	"""
	code = _as_str(item_code)
	g_raw = _as_str(grain) or "day"
	try:
		return _build_item_lot_timeline(
			code,
			warehouse=warehouse,
			grain=g_raw,
			from_date=from_date,
			to_date=to_date,
			include_forecast=include_forecast,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "get_item_lot_timeline soft fail")
		return _empty_lot_timeline(code, g_raw, error_soft=1)


def _build_item_lot_timeline(
	code,
	warehouse=None,
	grain="day",
	from_date=None,
	to_date=None,
	include_forecast=1,
):
	if not code:
		return _empty_lot_timeline("", grain)
	if not ensure_stock_lot_doctype():
		return _empty_lot_timeline(code, grain, doctype_missing=1)

	g = (_as_str(grain) or "day").lower()
	if g in ("months", "m"):
		g = "month"
	elif g in ("weeks", "w"):
		g = "week"
	elif g not in ("day", "week", "month"):
		g = "day"

	today = getdate(nowdate())
	# Default windows
	if g == "month":
		default_from, default_to = add_days(today, -180), add_days(today, 90)
	elif g == "week":
		default_from, default_to = add_days(today, -56), add_days(today, 28)
	else:
		default_from, default_to = add_days(today, -14), add_days(today, 7)

	try:
		from_d = getdate(_as_str(from_date)) if _as_str(from_date) else default_from
	except Exception:
		from_d = default_from
	try:
		to_d = getdate(_as_str(to_date)) if _as_str(to_date) else default_to
	except Exception:
		to_d = default_to
	if to_d < from_d:
		from_d, to_d = to_d, from_d

	wh = _as_str(warehouse) or None
	lot_filters = {"item_code": code}
	if wh:
		lot_filters["warehouse"] = wh
	lot_rows = frappe.get_all(
		"Stock Lot",
		filters=lot_filters,
		fields=["name", "receive_date", "sell_by_date", "qty_received", "qty_remaining", "status"],
		order_by="receive_date asc, creation asc",
		ignore_permissions=True,
	) or []
	# Gradual adoption: no lots yet → empty chart, Activity tab still works.
	if not lot_rows and not cint(include_forecast):
		return _empty_lot_timeline(
			code,
			g,
			from_date=str(from_d),
			to_date=str(to_d),
			no_lots=1,
		)
	lots_meta = [
		{
			"name": r.name,
			"color_key": r.name,
			"receive_date": str(getdate(r.receive_date)) if r.receive_date else None,
			"sell_by_date": str(getdate(r.sell_by_date)) if r.sell_by_date else None,
			"qty_received": flt(r.qty_received),
			"qty_remaining": flt(r.qty_remaining),
			"status": r.status,
		}
		for r in lot_rows
	]
	lot_sell_by = {
		m["name"]: getdate(m["sell_by_date"]) if m.get("sell_by_date") else None for m in lots_meta
	}

	try:
		posted = _load_posted_movements(code, warehouse=wh) or []
	except Exception:
		frappe.log_error(frappe.get_traceback(), "lot timeline posted movements")
		posted = []
	forecast = []
	if cint(include_forecast):
		try:
			forecast.extend(_forecast_out_events(code, from_d, to_d, warehouse=wh) or [])
		except Exception:
			frappe.log_error(frappe.get_traceback(), "lot timeline forecast outs wrap")
		try:
			forecast.extend(_forecast_in_events(code, from_d, to_d) or [])
		except Exception:
			frappe.log_error(frappe.get_traceback(), "lot timeline forecast inns wrap")

	all_events = sorted(
		posted + forecast,
		key=lambda e: (e.get("posting_date") or "", 0 if not e.get("expected") else 1),
	)

	# Running balances for posted only; forecast applied only on future buckets.
	balances = {m["name"]: 0.0 for m in lots_meta}
	posted_i = 0
	# Apply posted events before from_d so opening balances are correct
	while posted_i < len(posted):
		ev = posted[posted_i]
		if getdate(ev["posting_date"]) >= from_d:
			break
		lot = ev.get("lot")
		if lot and lot in balances:
			if ev["direction"] == "in":
				balances[lot] += flt(ev["qty"])
			else:
				balances[lot] = max(0.0, balances[lot] - flt(ev["qty"]))
		posted_i += 1

	buckets = []
	for key, end in _iter_bucket_ends(from_d, to_d, g):
		is_future = end > today
		# Apply posted movements with date <= end (and > previous — use cumulative)
		while posted_i < len(posted):
			ev = posted[posted_i]
			if getdate(ev["posting_date"]) > end:
				break
			lot = ev.get("lot")
			if lot and lot in balances:
				if ev["direction"] == "in":
					balances[lot] += flt(ev["qty"])
				else:
					balances[lot] = max(0.0, balances[lot] - flt(ev["qty"]))
			posted_i += 1

		# Working copy for forecast overlay on this bucket.
		# Future stacks are cumulative: all expected events with
		# today < posting_date <= bucket_end (so +35 then +50 → green 85).
		# Chips below the axis still show only this period's movements.
		stack_bal = dict(balances)
		period_movements = []
		petition_qty = 0.0
		for ev in all_events:
			ed = getdate(ev["posting_date"])
			ek, _ = _bucket_key_and_end(ed, g)
			if ek == key:
				period_movements.append(
					{
						"lot": ev.get("lot"),
						"qty": flt(ev["qty"]) * (1 if ev["direction"] == "in" else -1),
						"direction": ev["direction"],
						"expected": bool(ev.get("expected")),
						"voucher_type": ev.get("voucher_type"),
						"voucher_name": ev.get("voucher_name"),
						"petition": bool(ev.get("petition")),
						"unallocated": bool(ev.get("unallocated")),
					}
				)
			if not (is_future and ev.get("expected")):
				continue
			# Cumulative forecast through this bucket end (not only same-day)
			if ed <= today or ed > end:
				continue
			lot = ev.get("lot")
			q = flt(ev["qty"])
			if ev.get("petition") and ev["direction"] == "in":
				petition_qty += q
				continue
			# Unallocated expected outs stay chips-only (no lot to stack)
			if not lot or lot not in stack_bal:
				continue
			if ev["direction"] == "in":
				stack_bal[lot] += q
			else:
				stack_bal[lot] = max(0.0, stack_bal[lot] - q)

		stack = []
		# Oldest first (FIFO visual)
		for m in lots_meta:
			q = flt(stack_bal.get(m["name"]) or 0)
			if q <= 0.0001 and not any(
				pm.get("lot") == m["name"] for pm in period_movements
			):
				# skip empty lots with no activity in period
				if q <= 0.0001:
					continue
			if q <= 0.0001:
				continue
			sb = lot_sell_by.get(m["name"])
			days_left = (sb - today).days if sb else None
			stack.append(
				{
					"lot": m["name"],
					"qty": round(q, 6),
					"days_left": days_left,
					"expected": bool(is_future),
				}
			)

		# Expected petition ins (no lot yet) — cumulative dashed green block
		if petition_qty > 0.0001:
			stack.append(
				{
					"lot": None,
					"qty": round(petition_qty, 6),
					"days_left": None,
					"expected": True,
					"petition": True,
				}
			)

		buckets.append(
			{
				"key": key,
				"end": str(end),
				"is_future": bool(is_future),
				"stack": stack,
				"movements": period_movements,
			}
		)

	return {
		"item_code": code,
		"grain": g,
		"today": str(today),
		"from_date": str(from_d),
		"to_date": str(to_d),
		"lots": lots_meta,
		"buckets": buckets,
		"movement_doctype_missing": 0 if ensure_stock_lot_movement_doctype() else 1,
	}


@frappe.whitelist(allow_guest=True)
def list_stock_lot_movements(stock_lot=None, item_code=None, limit=100):
	"""Drill-down list of movements for a lot or item. Soft — never throws."""
	try:
		if not ensure_stock_lot_movement_doctype():
			return {"rows": [], "doctype_missing": 1}
		limit = max(1, min(cint(limit) or 100, 500))
		filters = {}
		if _as_str(stock_lot):
			filters["stock_lot"] = _as_str(stock_lot)
		if _as_str(item_code):
			filters["item_code"] = _as_str(item_code)
		if not filters:
			return {"rows": [], "doctype_missing": 0}
		rows = frappe.get_all(
			"Stock Lot Movement",
			filters=filters,
			fields=[
				"name",
				"stock_lot",
				"item_code",
				"warehouse",
				"posting_date",
				"direction",
				"qty",
				"voucher_type",
				"voucher_name",
				"creation",
			],
			order_by="posting_date desc, creation desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
		return {"rows": rows or [], "doctype_missing": 0}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "list_stock_lot_movements soft fail")
		return {"rows": [], "doctype_missing": 0, "soft": 1}


# ── i062 demo seed (Activity chart) ─────────────────────────────────────────

_LOT_DEMO_ITEMS = (
	{
		"item_code": "LOT-DEMO-SHORT",
		"item_name": "Lot demo — short shelf (3d)",
		"shelf_life_in_days": 3,
		"weight_per_unit": 0.25,
		"lots": (
			# (receive_offset_days, qty, sell_by_days override or None)
			(-10, 40, 3),
			(-6, 30, 3),
			(-2, 50, 3),
		),
		"outs": ((-8, 15), (-4, 20), (-1, 10)),
		# Future forecast (day window is +7d): compras = PO ins, deliveries = SO outs
		"future_ins": ((2, 35), (5, 50)),
		"future_outs": ((1, 12), (3, 18), (6, 10)),
	},
	{
		"item_code": "LOT-DEMO-MID",
		"item_name": "Lot demo — mid shelf (14d)",
		"shelf_life_in_days": 14,
		"weight_per_unit": 1.0,
		"lots": (
			(-21, 80, 14),
			(-14, 60, 14),
			(-7, 90, 14),
			(-1, 40, 14),
		),
		"outs": ((-18, 25), (-10, 35), (-5, 20), (-2, 15)),
	},
	{
		"item_code": "LOT-DEMO-LONG",
		"item_name": "Lot demo — long shelf (45d)",
		"shelf_life_in_days": 45,
		"weight_per_unit": 5.0,
		"lots": (
			(-40, 120, 45),
			(-20, 100, 45),
			(-5, 80, 45),
		),
		"outs": ((-30, 40), (-12, 30), (-3, 25)),
	},
	{
		"item_code": "LOT-DEMO-WEIGHT",
		"item_name": "Lot demo — heavy unit (12 kg)",
		"shelf_life_in_days": 7,
		"weight_per_unit": 12.0,
		"lots": (
			(-9, 20, 7),
			(-5, 15, 7),
			(-1, 25, 7),
		),
		"outs": ((-7, 8), (-3, 6)),
	},
)


def _lot_demo_item_group():
	return (
		frappe.db.get_single_value("Stock Settings", "item_group")
		or (frappe.db.exists("Item Group", "Products") and "Products")
		or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or "All Item Groups"
	)


_LOT_DEMO_TAG = "lot-timeline-demo"
_LOT_DEMO_SUPPLIER = "LOT Demo Supplier"
_LOT_DEMO_CUSTOMER = "LOT Demo Customer"


def _ensure_lot_demo_item(spec):
	code = spec["item_code"]
	fields = {
		"item_name": spec["item_name"],
		"shelf_life_in_days": int(spec["shelf_life_in_days"]),
		"weight_per_unit": flt(spec["weight_per_unit"]),
		"weight_uom": "Kg",
		"is_stock_item": 1,
		"is_purchase_item": 1,
		"is_sales_item": 1,
		"disabled": 0,
	}
	if frappe.db.exists("Item", code):
		doc = frappe.get_doc("Item", code)
		for k, v in fields.items():
			doc.set(k, v)
		doc.save(ignore_permissions=True)
		return code
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_group": _lot_demo_item_group(),
			"stock_uom": "Nos",
			"include_item_in_manufacturing": 0,
			**fields,
		}
	)
	doc.insert(ignore_permissions=True)
	return code


def _ensure_lot_demo_supplier():
	from erpnext.erpnext_integrations.ecommerce_api.api import _get_or_create_named_supplier

	return _get_or_create_named_supplier(_LOT_DEMO_SUPPLIER)


def _ensure_lot_demo_customer():
	name = frappe.db.get_value("Customer", {"customer_name": _LOT_DEMO_CUSTOMER}, "name")
	if name:
		return name
	if frappe.db.exists("Customer", _LOT_DEMO_CUSTOMER):
		return _LOT_DEMO_CUSTOMER
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _LOT_DEMO_CUSTOMER,
			"customer_type": "Company",
			"customer_group": (
				frappe.db.get_single_value("Selling Settings", "customer_group")
				or "All Customer Groups"
			),
			"territory": (
				frappe.db.get_single_value("Selling Settings", "territory") or "All Territories"
			),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _cancel_delete_doc(doctype, name):
	try:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(doctype, name)
		if cint(doc.docstatus) == 1:
			doc.cancel()
		frappe.delete_doc(doctype, name, ignore_permissions=True, force=1)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"lot demo cleanup {doctype} {name}")
	finally:
		frappe.flags.ignore_permissions = False


def _reset_lot_demo_forecast_docs(item_codes):
	"""Cancel/delete prior demo POs/SOs for the dedicated demo supplier/customer."""
	codes = [c for c in (item_codes or []) if c]
	if not codes:
		return
	supplier = frappe.db.get_value("Supplier", {"supplier_name": _LOT_DEMO_SUPPLIER}, "name") or (
		_LOT_DEMO_SUPPLIER if frappe.db.exists("Supplier", _LOT_DEMO_SUPPLIER) else None
	)
	if supplier:
		po_names = frappe.db.sql(
			"""
			SELECT DISTINCT po.name
			FROM `tabPurchase Order` po
			INNER JOIN `tabPurchase Order Item` poi ON poi.parent = po.name
			WHERE po.supplier = %(supplier)s
			  AND poi.item_code IN %(codes)s
			  AND po.docstatus < 2
			""",
			{"supplier": supplier, "codes": codes},
			pluck=True,
		) or []
		for n in po_names:
			_cancel_delete_doc("Purchase Order", n)

	customer = frappe.db.get_value("Customer", {"customer_name": _LOT_DEMO_CUSTOMER}, "name") or (
		_LOT_DEMO_CUSTOMER if frappe.db.exists("Customer", _LOT_DEMO_CUSTOMER) else None
	)
	if customer:
		so_names = frappe.db.sql(
			"""
			SELECT DISTINCT so.name
			FROM `tabSales Order` so
			INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
			WHERE so.customer = %(customer)s
			  AND soi.item_code IN %(codes)s
			  AND so.docstatus < 2
			""",
			{"customer": customer, "codes": codes},
			pluck=True,
		) or []
		for n in so_names:
			_cancel_delete_doc("Sales Order", n)


def _seed_lot_demo_future_ins(item_code, rows, *, company, warehouse, today):
	"""Submitted POs → green dashed expected compra blocks on the chart."""
	from erpnext.erpnext_integrations.ecommerce_api.buying_api import create_purchase_order

	supplier = _ensure_lot_demo_supplier()
	created = []
	for off, qty in rows or ():
		due = str(add_days(today, int(off)))
		res = create_purchase_order(
			supplier=supplier,
			schedule_date=due,
			company=company,
			submit=1,
			notes=_LOT_DEMO_TAG,
			items=[
				{
					"item_code": item_code,
					"qty": flt(qty),
					"rate": 80,
					"schedule_date": due,
				}
			],
		)
		name = (res or {}).get("name")
		if name:
			created.append(
				{
					"name": name,
					"date": due,
					"qty": qty,
					"submitted": (res or {}).get("submitted"),
				}
			)
	return created


def _seed_lot_demo_future_outs(item_code, rows, *, company, warehouse, today):
	"""Submitted SOs → expected delivery outs (soft-FIFO colored chips)."""
	customer = _ensure_lot_demo_customer()
	created = []
	for off, qty in rows or ():
		due = str(add_days(today, int(off)))
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": company,
				"transaction_date": str(today),
				"delivery_date": due,
				"order_type": "Sales",
				"selling_price_list": (
					frappe.db.get_single_value("Selling Settings", "selling_price_list")
					or "Standard Selling"
				),
				"ignore_pricing_rule": 1,
				"items": [
					{
						"item_code": item_code,
						"qty": flt(qty),
						"rate": 120,
						"delivery_date": due,
						"warehouse": warehouse,
					}
				],
			}
		)
		so.flags.ignore_permissions = True
		so.insert(ignore_permissions=True)
		try:
			so.submit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"lot demo SO submit {item_code}")
		created.append(
			{
				"name": so.name,
				"date": due,
				"qty": qty,
				"docstatus": cint(so.docstatus),
			}
		)
	return created


@frappe.whitelist(allow_guest=True)
def seed_lot_timeline_demo(reset=0, company=None, warehouse=None):
	"""Create LOT-DEMO-* items + staggered Stock Lots/movements for i062 chart QA.

	reset=1 deletes prior demo lots/movements (and tagged forecast PO/SO) before reseeding.
	LOT-DEMO-SHORT also gets future COMPRAS (PO) + deliveries (SO) for the dashed forecast.
	"""
	if not ensure_stock_lot_doctype():
		frappe.throw(_("Stock Lot DocType missing — run bench migrate"))
	ensure_stock_lot_movement_doctype()

	from erpnext.erpnext_integrations.ecommerce_api.api import _default_company_warehouse

	comp = _as_str(company) or (
		frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	wh = _as_str(warehouse) or _default_company_warehouse(comp)
	if not wh:
		frappe.throw(_("No warehouse available for lot demo seed"))

	today = getdate(nowdate())
	do_reset = str(reset).lower() in ("1", "true", "yes")
	created_items = []
	created_lots = []
	created_outs = []
	created_pos = []
	created_sos = []

	all_codes = [s["item_code"] for s in _LOT_DEMO_ITEMS]
	if do_reset:
		_reset_lot_demo_forecast_docs(all_codes)

	for spec in _LOT_DEMO_ITEMS:
		code = _ensure_lot_demo_item(spec)
		created_items.append(code)

		if do_reset and ensure_stock_lot_doctype():
			old_lots = frappe.get_all(
				"Stock Lot",
				filters={"item_code": code},
				pluck="name",
				ignore_permissions=True,
			)
			if old_lots and ensure_stock_lot_movement_doctype():
				for mv in frappe.get_all(
					"Stock Lot Movement",
					filters={"stock_lot": ["in", old_lots]},
					pluck="name",
					ignore_permissions=True,
				):
					frappe.delete_doc("Stock Lot Movement", mv, ignore_permissions=True, force=1)
			for lot in old_lots:
				frappe.delete_doc("Stock Lot", lot, ignore_permissions=True, force=1)

		lot_names_by_recv = []
		for recv_off, qty, sell_by in spec["lots"]:
			recv = add_days(today, int(recv_off))
			name = create_stock_lot(
				item_code=code,
				qty=qty,
				warehouse=wh,
				receive_date=str(recv),
				sell_by_days=sell_by,
				unit_cost=100,
				company=comp,
				reference=_LOT_DEMO_TAG,
				commit=False,
			)
			created_lots.append(name)
			lot_names_by_recv.append((recv, name, qty))

		# Staggered FIFO outs so the chart shows declining stacks over time
		for out_off, out_qty in spec.get("outs") or ():
			out_date = add_days(today, int(out_off))
			consumed = consume_stock_lots_fifo(
				item_code=code,
				qty=out_qty,
				warehouse=wh,
				posting_date=str(out_date),
				voucher_type="Demo",
				voucher_name=f"LOT-DEMO-OUT-{code}-{out_off}",
				commit=False,
			)
			created_outs.append({"item_code": code, "date": str(out_date), "consumed": consumed})

		# Future forecast docs (compras + deliveries) — currently SHORT only
		if spec.get("future_ins"):
			created_pos.extend(
				_seed_lot_demo_future_ins(
					code,
					spec["future_ins"],
					company=comp,
					warehouse=wh,
					today=today,
				)
			)
		if spec.get("future_outs"):
			created_sos.extend(
				_seed_lot_demo_future_outs(
					code,
					spec["future_outs"],
					company=comp,
					warehouse=wh,
					today=today,
				)
			)

	frappe.db.commit()
	return {
		"ok": 1,
		"company": comp,
		"warehouse": wh,
		"items": created_items,
		"lots": created_lots,
		"outs": created_outs,
		"purchase_orders": created_pos,
		"sales_orders": created_sos,
		"hint": "Open Product Manager → LOT-DEMO-SHORT → Activity (future dashed compras + deliveries)",
	}
