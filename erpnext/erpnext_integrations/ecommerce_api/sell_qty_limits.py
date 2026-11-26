"""
Product sell qty limits (Item.custom_min_sell_qty / custom_max_sell_qty_day).

- min_sell_qty: per order line (stock UOM)
- max_sell_qty_day: per client per calendar day (SO + SI), excluding walk-in
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

_WALKIN_CUSTOMERS = {
	"consumidor final",
	"guest",
	"walk-in customer",
	"walk in customer",
}


def _is_walkin_customer(customer: str | None) -> bool:
	name = cstr(customer or "").strip().lower()
	return (not name) or name in _WALKIN_CUSTOMERS


def get_item_sell_limits(item_code: str) -> tuple[float, float]:
	"""Return (min_sell_qty, max_sell_qty_day); missing columns → (0, 0)."""
	code = cstr(item_code or "").strip()
	if not code or not frappe.db.exists("Item", code):
		return 0.0, 0.0
	fields = []
	if frappe.db.has_column("Item", "custom_min_sell_qty"):
		fields.append("custom_min_sell_qty")
	if frappe.db.has_column("Item", "custom_max_sell_qty_day"):
		fields.append("custom_max_sell_qty_day")
	if not fields:
		return 0.0, 0.0
	row = frappe.db.get_value("Item", code, fields, as_dict=True) or {}
	return flt(row.get("custom_min_sell_qty") or 0), flt(row.get("custom_max_sell_qty_day") or 0)


def client_item_qty_sold_today(
	item_code: str,
	customer: str | None = None,
	on_date=None,
	exclude_parents: list[str] | None = None,
) -> float:
	"""Sum stock-UOM qty already ordered/invoiced today for this client+item."""
	code = cstr(item_code or "").strip()
	cust = cstr(customer or "").strip()
	if not code or _is_walkin_customer(cust):
		return 0.0
	day = getdate(on_date or nowdate())
	exclude = [cstr(n) for n in (exclude_parents or []) if cstr(n).strip()]

	total = 0.0
	# Sales Orders (preventa / catalog / vendedor) — count Draft + Submitted.
	so_filters = {
		"customer": cust,
		"transaction_date": day,
		"docstatus": ["<", 2],
		"status": ["not in", ["Cancelled", "Closed"]],
	}
	so_names = frappe.get_all(
		"Sales Order",
		filters=so_filters,
		pluck="name",
		ignore_permissions=True,
	)
	so_names = [n for n in so_names if n not in exclude]
	if so_names:
		rows = frappe.get_all(
			"Sales Order Item",
			filters={"parent": ["in", so_names], "item_code": code},
			fields=["qty"],
			ignore_permissions=True,
		)
		total += sum(flt(r.get("qty") or 0) for r in rows)

	# POS Sales Invoices for the same day.
	si_filters = {
		"customer": cust,
		"posting_date": day,
		"docstatus": ["<", 2],
		"is_return": 0,
	}
	si_names = frappe.get_all(
		"Sales Invoice",
		filters=si_filters,
		pluck="name",
		ignore_permissions=True,
	)
	si_names = [n for n in si_names if n not in exclude]
	if si_names:
		rows = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": ["in", si_names], "item_code": code},
			fields=["qty"],
			ignore_permissions=True,
		)
		total += sum(flt(r.get("qty") or 0) for r in rows)

	return total


def validate_items_sell_qty_limits(
	items,
	customer: str | None = None,
	on_date=None,
	exclude_parents: list[str] | None = None,
) -> None:
	"""
	frappe.throw when a line is below min_sell_qty or the client would exceed
	max_sell_qty_day (already sold today + this cart).
	"""
	if items is None or items in ("", "null", "undefined"):
		return
	if isinstance(items, str):
		try:
			items = frappe.parse_json(items)
		except Exception:
			return
	if isinstance(items, dict):
		items = [items]
	if not isinstance(items, (list, tuple)) or not items:
		return

	# Aggregate cart qty per item (dirty clients may split lines).
	cart_qty: dict[str, float] = {}
	for raw in items:
		if not isinstance(raw, dict):
			continue
		code = cstr(raw.get("item_code") or raw.get("itemCode") or "").strip()
		if not code:
			continue
		qty = flt(raw.get("qty") or 0)
		if qty <= 0:
			continue
		cart_qty[code] = cart_qty.get(code, 0.0) + qty

	if not cart_qty:
		return

	cust = cstr(customer or "").strip() or None
	day = getdate(on_date or nowdate())

	for code, qty in cart_qty.items():
		min_qty, max_day = get_item_sell_limits(code)
		title = frappe.db.get_value("Item", code, "item_name") or code
		if min_qty > 0 and qty + 1e-9 < min_qty:
			frappe.throw(
				_("Minimum for {0} is {1}").format(title, cint(min_qty) if float(min_qty).is_integer() else min_qty)
			)
		if max_day > 0 and not _is_walkin_customer(cust):
			already = client_item_qty_sold_today(
				code, customer=cust, on_date=day, exclude_parents=exclude_parents
			)
			if already + qty > max_day + 1e-9:
				remaining = max(0.0, max_day - already)
				frappe.throw(
					_(
						"Daily max for {0} is {1} per client (already {2} today; {3} left)"
					).format(
						title,
						cint(max_day) if float(max_day).is_integer() else max_day,
						cint(already) if float(already).is_integer() else already,
						cint(remaining) if float(remaining).is_integer() else remaining,
					)
				)
