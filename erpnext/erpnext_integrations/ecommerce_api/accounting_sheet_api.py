"""Editable 'Excel-like' workbook for Logistica > Accounting, plus named constants.

Cell formulas (=A1+B1, =this_month_revenue*0.1, ...) resolve client-side in
`lib/sheet-formula.ts`. This module only persists the raw workbook (scope +
JSON blob, same storage pattern as `screen_notes.py` / `Screen Note Board`)
and computes the constants registry that formulas can reference by name.

A "workbook" (one `Accounting Sheet` doc, keyed by `scope`) holds multiple
named sheets (Google-Sheets-style tabs), each with its own cell map and cell
background-color map:

    {"sheets": [{"id": "...", "name": "Sheet1", "cells": {...}, "styles": {...}}],
     "active_sheet_id": "..."}

Constants are NOT auto-refreshed — the frontend only calls
`get_accounting_constants` on an explicit user refresh, optionally pinned to
an `as_of_date` (defaults to today) so historical "what would this have
looked like on date X" checks are possible without editing the sheet.

Naming convention for constants ("genealogy") — read before adding a new one:

- Time-scoped financial metric: `{scope}_{metric}`
  scope  in {today, yesterday, this_week, this_month, this_year}
  metric in {revenue, orders_count, avg_ticket, cash_total, card_total}
  e.g. today_revenue, this_month_avg_ticket

- Point-in-time state count (no time scope, reflects current state): `{domain}_{noun}_count`
  e.g. active_registers_count, low_stock_items_count

Always lowercase snake_case, no abbreviations (`count` not `cnt`, `revenue`
not `rev`) so formulas stay readable, e.g. `=this_month_revenue/this_month_orders_count`.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

DEFAULT_SCOPE = "logistica.accounting"
DEFAULT_SHEET_ID = "sheet-1"
DEFAULT_SHEET_NAME = "Sheet1"


def _sales_totals(start_date, end_date) -> dict:
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(grand_total), 0) AS revenue, COUNT(*) AS orders_count
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s
		""",
		(start_date, end_date),
		as_dict=True,
	)[0]
	revenue = flt(row.revenue)
	orders_count = int(row.orders_count or 0)
	avg_ticket = flt(revenue / orders_count) if orders_count else 0.0
	return {"revenue": revenue, "orders_count": orders_count, "avg_ticket": avg_ticket}


def _payment_totals(start_date, end_date) -> dict:
	rows = frappe.db.sql(
		"""
		SELECT mode_of_payment, COALESCE(SUM(paid_amount), 0) AS amount
		FROM `tabPayment Entry`
		WHERE docstatus = 1 AND payment_type = 'Receive'
		  AND posting_date BETWEEN %s AND %s
		GROUP BY mode_of_payment
		""",
		(start_date, end_date),
		as_dict=True,
	)
	cash = 0.0
	card = 0.0
	for r in rows:
		mop = (r.mode_of_payment or "").lower()
		if "cash" in mop:
			cash += flt(r.amount)
		elif "card" in mop or "tarjeta" in mop:
			card += flt(r.amount)
	return {"cash_total": cash, "card_total": card}


@frappe.whitelist()
def get_accounting_constants(as_of_date=None):
	today = str(getdate(as_of_date)) if as_of_date else nowdate()
	yesterday = add_days(today, -1)
	week_start = add_days(today, -6)
	month_start = str(get_first_day(today))
	year_start = f"{today[:4]}-01-01"

	today_sales = _sales_totals(today, today)
	today_pay = _payment_totals(today, today)
	month_sales = _sales_totals(month_start, today)

	return {
		"today_revenue": today_sales["revenue"],
		"today_orders_count": today_sales["orders_count"],
		"today_avg_ticket": today_sales["avg_ticket"],
		"today_cash_total": today_pay["cash_total"],
		"today_card_total": today_pay["card_total"],
		"yesterday_revenue": _sales_totals(yesterday, yesterday)["revenue"],
		"this_week_revenue": _sales_totals(week_start, today)["revenue"],
		"this_month_revenue": month_sales["revenue"],
		"this_month_orders_count": month_sales["orders_count"],
		"this_month_avg_ticket": month_sales["avg_ticket"],
		"this_year_revenue": _sales_totals(year_start, today)["revenue"],
		"active_registers_count": (
			frappe.db.count("POS Profile", {"disabled": 0})
			if frappe.db.exists("DocType", "POS Profile")
			else 0
		),
		"low_stock_items_count": frappe.db.count("Bin", {"actual_qty": ["<=", 0]}),
	}


def _default_workbook() -> dict:
	return {
		"sheets": [{"id": DEFAULT_SHEET_ID, "name": DEFAULT_SHEET_NAME, "cells": {}, "styles": {}}],
		"active_sheet_id": DEFAULT_SHEET_ID,
	}


def _normalize_workbook(raw) -> dict:
	"""Coerce arbitrary stored/incoming JSON into a valid {sheets, active_sheet_id} shape.

	Also upgrades the pre-multi-sheet flat `{cell_ref: value}` shape (no
	migration needed — legacy scopes just become a single-sheet workbook).
	"""
	if not isinstance(raw, dict):
		return _default_workbook()

	sheets_in = raw.get("sheets")
	if not isinstance(sheets_in, list) or not sheets_in:
		# Legacy flat-cells shape: {"A1": "10", ...} with no "sheets" key.
		if raw and "sheets" not in raw:
			return {
				"sheets": [{"id": DEFAULT_SHEET_ID, "name": DEFAULT_SHEET_NAME, "cells": raw, "styles": {}}],
				"active_sheet_id": DEFAULT_SHEET_ID,
			}
		return _default_workbook()

	sheets = []
	for s in sheets_in:
		if not isinstance(s, dict) or not s.get("id"):
			continue
		sheets.append(
			{
				"id": str(s["id"]),
				"name": str(s.get("name") or s["id"]),
				"cells": s.get("cells") if isinstance(s.get("cells"), dict) else {},
				"styles": s.get("styles") if isinstance(s.get("styles"), dict) else {},
			}
		)
	if not sheets:
		return _default_workbook()

	active = raw.get("active_sheet_id")
	if active not in {s["id"] for s in sheets}:
		active = sheets[0]["id"]
	return {"sheets": sheets, "active_sheet_id": active}


def _get_sheet(scope: str | None, create: bool = True):
	scope = (scope or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
	if frappe.db.exists("Accounting Sheet", scope):
		return frappe.get_doc("Accounting Sheet", scope)
	if not create:
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Accounting Sheet",
			"scope": scope,
			"cells_json": json.dumps(_default_workbook()),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


@frappe.whitelist()
def get_accounting_sheet(scope=None):
	doc = _get_sheet(scope)
	try:
		raw = json.loads(doc.cells_json or "{}")
	except Exception:
		raw = {}
	workbook = _normalize_workbook(raw)
	return {"scope": doc.scope, **workbook, "modified": str(doc.modified)}


@frappe.whitelist()
def save_accounting_sheet(scope=None, sheets=None, active_sheet_id=None):
	if isinstance(sheets, str):
		sheets = frappe.parse_json(sheets) or []
	if not isinstance(sheets, list):
		frappe.throw(_("sheets must be a list of {id, name, cells, styles}"))
	workbook = _normalize_workbook({"sheets": sheets, "active_sheet_id": active_sheet_id})
	doc = _get_sheet(scope)
	doc.cells_json = json.dumps(workbook, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "scope": doc.scope, "modified": str(doc.modified)}
