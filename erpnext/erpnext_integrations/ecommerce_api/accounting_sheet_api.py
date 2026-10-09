"""Editable 'Excel-like' workbook for Logistica > Accounting, plus named constants.

Cell formulas (=A1+B1, =this_month_revenue*0.1, ...) resolve client-side in
`lib/sheet-formula.ts`. This module only persists the raw workbook (scope +
JSON blob, same storage pattern as `screen_notes.py` / `Screen Note Board`)
and computes the constants registry that formulas can reference by name.

A "workbook" (one `Accounting Sheet` doc, keyed by `scope`) holds multiple
named sheets (Google-Sheets-style tabs), each with its own cell map and cell
background-color map:

    {"sheets": [{"id": "...", "name": "Sheet1", "cells": {...}, "styles": {...}}],
     "active_sheet_id": "...",
     "variables": [{"id": "_a01", "name": "total_cost", "expr": "0.1", "notes": ""}],
     "notes": "", "code": "", "script": "", "conditional_formats": []}

Constants are NOT auto-refreshed — the frontend only calls
`get_accounting_constants` on an explicit user refresh, optionally pinned to
an `as_of_date` (defaults to today) so historical "what would this have
looked like on date X" checks are possible without editing the sheet.

Naming convention for constants ("genealogy") — read before adding a new one:

- Time-scoped financial metric: `{scope}_{metric}`
  scope  in {today, yesterday, this_week, this_month, this_year}
  metric in {revenue, net_total, tax_total, orders_count, avg_ticket,
             cash_total, card_total, mp_total, transfer_total,
             purchase_total, payout_total, sales_gross, returns_total,
             sales_net, black_total, white_total, po_ordered,
             po_received_value, po_billed_value, caja_opening_total,
             si_white_count, si_black_count}
  e.g. today_revenue, this_month_tax_total, this_year_purchase_total,
       this_month_sales_net, this_month_po_ordered

- Point-in-time state: `{domain}_{noun}_count` / `{domain}_{noun}_total` / `{domain}_outstanding`
  e.g. employees_count, employees_salary_total, receivables_outstanding,
       so_unbilled_total, ar_aging_0_30, caja_sessions_open_count

- Configured rate (from ERP masters, not a date window): `{domain}_{noun}_rate`
  e.g. sales_tax_rate (percent from the default Sales Taxes template)

Always lowercase snake_case, no abbreviations (`count` not `cnt`, `revenue`
not `rev`) so formulas stay readable, e.g. `=this_month_revenue/this_month_orders_count`.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

DEFAULT_SCOPE = "logistica.accounting"
DEFAULT_SHEET_ID = "sheet-1"
DEFAULT_SHEET_NAME = "Sheet1"


def _empty_invoice_totals() -> dict:
	return {
		"revenue": 0.0,
		"net_total": 0.0,
		"tax_total": 0.0,
		"outstanding": 0.0,
		"orders_count": 0,
		"avg_ticket": 0.0,
	}


def _invoice_totals(doctype: str, start_date, end_date) -> dict:
	out = _empty_invoice_totals()
	if not frappe.db.exists("DocType", doctype):
		return out
	try:
		row = frappe.db.sql(
			f"""
			SELECT
				COALESCE(SUM(grand_total), 0) AS revenue,
				COALESCE(SUM(net_total), 0) AS net_total,
				COALESCE(SUM(total_taxes_and_charges), 0) AS tax_total,
				COALESCE(SUM(outstanding_amount), 0) AS outstanding,
				COUNT(*) AS orders_count
			FROM `tab{doctype}`
			WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s
			""",
			(start_date, end_date),
			as_dict=True,
		)[0]
	except Exception:
		return out
	revenue = flt(row.revenue)
	orders_count = int(row.orders_count or 0)
	out.update(
		{
			"revenue": revenue,
			"net_total": flt(row.net_total),
			"tax_total": flt(row.tax_total),
			"outstanding": flt(row.outstanding),
			"orders_count": orders_count,
			"avg_ticket": flt(revenue / orders_count) if orders_count else 0.0,
		}
	)
	return out


def _sales_totals(start_date, end_date) -> dict:
	return _invoice_totals("Sales Invoice", start_date, end_date)


def _purchase_totals(start_date, end_date) -> dict:
	return _invoice_totals("Purchase Invoice", start_date, end_date)


def _payment_totals(start_date, end_date, payment_type: str = "Receive") -> dict:
	empty = {
		"cash_total": 0.0,
		"card_total": 0.0,
		"mp_total": 0.0,
		"transfer_total": 0.0,
		"payout_total": 0.0,
	}
	if not frappe.db.exists("DocType", "Payment Entry"):
		return empty
	try:
		rows = frappe.db.sql(
			"""
			SELECT mode_of_payment, COALESCE(SUM(paid_amount), 0) AS amount
			FROM `tabPayment Entry`
			WHERE docstatus = 1 AND payment_type = %s
			  AND posting_date BETWEEN %s AND %s
			GROUP BY mode_of_payment
			""",
			(payment_type, start_date, end_date),
			as_dict=True,
		)
	except Exception:
		return empty
	cash = 0.0
	card = 0.0
	mp = 0.0
	transfer = 0.0
	total = 0.0
	for r in rows:
		amt = flt(r.amount)
		total += amt
		mop = (r.mode_of_payment or "").lower()
		if "cash" in mop or "efectivo" in mop:
			cash += amt
		elif "card" in mop or "tarjeta" in mop:
			card += amt
		elif "mobile" in mop or "mercado" in mop or mop in ("mp", "qr"):
			mp += amt
		elif "transfer" in mop or "transferencia" in mop or "bank" in mop or "cheque" in mop:
			transfer += amt
	return {
		"cash_total": cash,
		"card_total": card,
		"mp_total": mp,
		"transfer_total": transfer,
		"payout_total": total,
	}


def _sales_mode_totals(start_date, end_date) -> dict:
	"""Gross / returns / WHITE vs BLACK (sale_mode tagged in SI.remarks)."""
	out = {
		"sales_gross": 0.0,
		"returns_total": 0.0,
		"sales_net": 0.0,
		"black_total": 0.0,
		"white_total": 0.0,
		"si_white_count": 0,
		"si_black_count": 0,
	}
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return out
	try:
		rows = frappe.db.sql(
			"""
			SELECT
				IFNULL(is_return, 0) AS is_return,
				CASE
					WHEN IFNULL(remarks, '') LIKE '%%sale_mode:BLACK%%' THEN 'BLACK'
					ELSE 'WHITE'
				END AS sale_mode,
				COALESCE(SUM(grand_total), 0) AS revenue,
				COUNT(*) AS orders_count
			FROM `tabSales Invoice`
			WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s
			GROUP BY IFNULL(is_return, 0),
				CASE
					WHEN IFNULL(remarks, '') LIKE '%%sale_mode:BLACK%%' THEN 'BLACK'
					ELSE 'WHITE'
				END
			""",
			(start_date, end_date),
			as_dict=True,
		)
	except Exception:
		return out
	gross = 0.0
	returns = 0.0
	black = 0.0
	white = 0.0
	white_count = 0
	black_count = 0
	for r in rows:
		amt = flt(r.revenue)
		cnt = int(r.orders_count or 0)
		is_ret = int(r.is_return or 0)
		is_black = str(r.sale_mode or "") == "BLACK"
		if is_ret:
			returns += abs(amt)
			continue
		gross += amt
		if is_black:
			black += amt
			black_count += cnt
		else:
			white += amt
			white_count += cnt
	out.update(
		{
			"sales_gross": gross,
			"returns_total": returns,
			"sales_net": gross - returns,
			"black_total": black,
			"white_total": white,
			"si_white_count": white_count,
			"si_black_count": black_count,
		}
	)
	return out


def _po_totals(start_date, end_date) -> dict:
	empty = {"ordered": 0.0, "received_value": 0.0, "billed_value": 0.0}
	if not frappe.db.exists("DocType", "Purchase Order"):
		return empty
	try:
		row = frappe.db.sql(
			"""
			SELECT
				COALESCE(SUM(grand_total), 0) AS ordered,
				COALESCE(SUM(grand_total * IFNULL(per_received, 0) / 100), 0) AS received_value,
				COALESCE(SUM(grand_total * IFNULL(per_billed, 0) / 100), 0) AS billed_value
			FROM `tabPurchase Order`
			WHERE docstatus = 1 AND transaction_date BETWEEN %s AND %s
			""",
			(start_date, end_date),
			as_dict=True,
		)[0]
	except Exception:
		return empty
	return {
		"ordered": flt(row.ordered),
		"received_value": flt(row.received_value),
		"billed_value": flt(row.billed_value),
	}


def _so_unbilled_total() -> float:
	if not frappe.db.exists("DocType", "Sales Order"):
		return 0.0
	try:
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(grand_total * (100 - IFNULL(per_billed, 0)) / 100), 0) AS gap
			FROM `tabSales Order`
			WHERE docstatus = 1
			  AND IFNULL(status, '') NOT IN ('Completed', 'Closed', 'Cancelled')
			""",
			as_dict=True,
		)[0]
		return flt(row.gap)
	except Exception:
		return 0.0


def _ar_aging(as_of) -> dict:
	empty = {
		"ar_aging_0_30": 0.0,
		"ar_aging_31_60": 0.0,
		"ar_aging_61_90": 0.0,
		"ar_aging_90_plus": 0.0,
	}
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return empty
	try:
		row = frappe.db.sql(
			"""
			SELECT
				COALESCE(SUM(CASE WHEN DATEDIFF(%s, posting_date) <= 30
					THEN outstanding_amount ELSE 0 END), 0) AS b0,
				COALESCE(SUM(CASE WHEN DATEDIFF(%s, posting_date) BETWEEN 31 AND 60
					THEN outstanding_amount ELSE 0 END), 0) AS b1,
				COALESCE(SUM(CASE WHEN DATEDIFF(%s, posting_date) BETWEEN 61 AND 90
					THEN outstanding_amount ELSE 0 END), 0) AS b2,
				COALESCE(SUM(CASE WHEN DATEDIFF(%s, posting_date) > 90
					THEN outstanding_amount ELSE 0 END), 0) AS b3
			FROM `tabSales Invoice`
			WHERE docstatus = 1
			  AND IFNULL(is_return, 0) = 0
			  AND outstanding_amount > 0.0001
			""",
			(as_of, as_of, as_of, as_of),
			as_dict=True,
		)[0]
	except Exception:
		return empty
	return {
		"ar_aging_0_30": flt(row.b0),
		"ar_aging_31_60": flt(row.b1),
		"ar_aging_61_90": flt(row.b2),
		"ar_aging_90_plus": flt(row.b3),
	}


def _caja_session_stats(month_start, as_of) -> dict:
	empty = {
		"caja_sessions_open_count": 0,
		"caja_sessions_closed_count": 0,
		"this_month_caja_opening_total": 0.0,
	}
	if not frappe.db.exists("DocType", "POS Cash Session"):
		return empty
	try:
		open_n = int(
			frappe.db.count("POS Cash Session", {"status": "Open"}) or 0
		)
		closed_n = int(
			frappe.db.count("POS Cash Session", {"status": "Closed"}) or 0
		)
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(opening_cash), 0) AS opening_total
			FROM `tabPOS Cash Session`
			WHERE DATE(started_at) BETWEEN %s AND %s
			""",
			(month_start, as_of),
			as_dict=True,
		)[0]
		return {
			"caja_sessions_open_count": open_n,
			"caja_sessions_closed_count": closed_n,
			"this_month_caja_opening_total": flt(row.opening_total),
		}
	except Exception:
		return empty


def _sales_tax_rate() -> float:
	"""Percent from the default Sales Taxes and Charges Template (sum of % rows)."""
	if not frappe.db.exists("DocType", "Sales Taxes and Charges Template"):
		return 0.0
	name = frappe.db.get_value(
		"Sales Taxes and Charges Template", {"is_default": 1, "disabled": 0}, "name"
	)
	if not name:
		name = frappe.db.get_value("Sales Taxes and Charges Template", {"disabled": 0}, "name")
	if not name:
		return 0.0
	rows = frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": name, "parenttype": "Sales Taxes and Charges Template"},
		fields=["charge_type", "rate"],
		ignore_permissions=True,
	)
	rate = 0.0
	for r in rows:
		charge = (r.get("charge_type") or "").lower()
		if "actual" in charge:
			continue
		rate += flt(r.get("rate"))
	return flt(rate)


def _inventory_value() -> float:
	if not frappe.db.exists("DocType", "Bin"):
		return 0.0
	try:
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(actual_qty * valuation_rate), 0) AS value
			FROM `tabBin`
			""",
			as_dict=True,
		)[0]
		return flt(row.value)
	except Exception:
		return 0.0


def _sum_ctc_active() -> float:
	if not frappe.db.exists("DocType", "Employee"):
		return 0.0
	try:
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(ctc), 0) AS total
			FROM `tabEmployee`
			WHERE status = 'Active'
			""",
			as_dict=True,
		)[0]
		return flt(row.total)
	except Exception:
		return 0.0


USER_VAR_ID_RE = re.compile(r"^_?a\d{2}$")
USER_VAR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FORMAT_KINDS = ("starts", "ends", "gt", "lt", "eq")


@frappe.whitelist(allow_guest=True)
def get_accounting_constants(as_of_date=None):
	today = str(getdate(as_of_date)) if as_of_date else nowdate()
	yesterday = add_days(today, -1)
	week_start = add_days(today, -6)
	month_start = str(get_first_day(today))
	year_start = f"{today[:4]}-01-01"

	today_sales = _sales_totals(today, today)
	today_pay = _payment_totals(today, today)
	month_sales = _sales_totals(month_start, today)
	year_sales = _sales_totals(year_start, today)
	month_pay = _payment_totals(month_start, today)
	month_payout = _payment_totals(month_start, today, "Pay")
	month_purchases = _purchase_totals(month_start, today)
	year_purchases = _purchase_totals(year_start, today)
	month_mode = _sales_mode_totals(month_start, today)
	month_po = _po_totals(month_start, today)
	aging = _ar_aging(today)
	caja = _caja_session_stats(month_start, today)

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
		"this_year_revenue": year_sales["revenue"],
		"active_registers_count": _safe_count("POS Profile", {"disabled": 0}),
		"low_stock_items_count": _safe_count("Bin", {"actual_qty": ["<=", 0]}),
		# Append-only so existing s01… short ids stay stable.
		"today_net_total": today_sales["net_total"],
		"today_tax_total": today_sales["tax_total"],
		"this_month_net_total": month_sales["net_total"],
		"this_month_tax_total": month_sales["tax_total"],
		"this_year_tax_total": year_sales["tax_total"],
		"this_month_cash_total": month_pay["cash_total"],
		"this_month_card_total": month_pay["card_total"],
		"this_month_payout_total": month_payout["payout_total"],
		"this_month_purchase_total": month_purchases["revenue"],
		"this_year_purchase_total": year_purchases["revenue"],
		"sales_tax_rate": _sales_tax_rate(),
		"receivables_outstanding": _invoice_totals("Sales Invoice", "1900-01-01", today)["outstanding"],
		"payables_outstanding": _invoice_totals("Purchase Invoice", "1900-01-01", today)["outstanding"],
		"employees_count": _safe_count("Employee", {"status": "Active"}),
		"employees_salary_total": _sum_ctc_active(),
		"customers_count": _safe_count("Customer", {"disabled": 0}),
		"suppliers_count": _safe_count("Supplier", {"disabled": 0}),
		"active_items_count": _safe_count("Item", {"disabled": 0}),
		"inventory_value": _inventory_value(),
		# Starter-pack constants (Dashboard / POS / Ventas / Compras / Sueldos).
		"this_month_sales_gross": month_mode["sales_gross"],
		"this_month_returns_total": month_mode["returns_total"],
		"this_month_sales_net": month_mode["sales_net"],
		"this_month_black_total": month_mode["black_total"],
		"this_month_white_total": month_mode["white_total"],
		"this_month_mp_total": month_pay["mp_total"],
		"this_month_transfer_total": month_pay["transfer_total"],
		"this_month_po_ordered": month_po["ordered"],
		"this_month_po_received_value": month_po["received_value"],
		"this_month_po_billed_value": month_po["billed_value"],
		"so_unbilled_total": _so_unbilled_total(),
		"ar_aging_0_30": aging["ar_aging_0_30"],
		"ar_aging_31_60": aging["ar_aging_31_60"],
		"ar_aging_61_90": aging["ar_aging_61_90"],
		"ar_aging_90_plus": aging["ar_aging_90_plus"],
		"caja_sessions_open_count": caja["caja_sessions_open_count"],
		"caja_sessions_closed_count": caja["caja_sessions_closed_count"],
		"this_month_caja_opening_total": caja["this_month_caja_opening_total"],
		"this_month_si_white_count": month_mode["si_white_count"],
		"this_month_si_black_count": month_mode["si_black_count"],
		# Archivo (paper + payments hub) — ops vs posted for month close
		**_archivo_constants(month_start, today),
	}


DETAIL_ROW_LIMIT = 45


def _po_pipeline_label(per_received, per_billed) -> str:
	pr = flt(per_received)
	pb = flt(per_billed)
	if pr >= 99.5 and pb >= 99.5:
		return "done"
	if pr >= 99.5:
		return "to_bill"
	if pr > 0.5:
		return "partial"
	return "to_receive"


def _compras_oc_dump(month_start, as_of, limit: int) -> dict:
	headers = [
		"fecha_oc",
		"oc",
		"proveedor",
		"pipeline",
		"total_oc",
		"recibido_pct",
		"pagado",
		"saldo",
	]
	empty = {
		"headers": headers,
		"rows": [],
		"total": 0,
		"truncated": False,
		"month_start": str(month_start),
		"as_of": str(as_of),
	}
	if not frappe.db.exists("DocType", "Purchase Order"):
		return empty
	try:
		total = int(
			frappe.db.sql(
				"""
				SELECT COUNT(*)
				FROM `tabPurchase Order`
				WHERE docstatus = 1 AND transaction_date BETWEEN %s AND %s
				""",
				(month_start, as_of),
			)[0][0]
			or 0
		)
		rows = frappe.db.sql(
			"""
			SELECT
				name,
				transaction_date,
				IFNULL(supplier_name, supplier) AS proveedor,
				IFNULL(grand_total, 0) AS total_oc,
				IFNULL(per_received, 0) AS per_received,
				IFNULL(per_billed, 0) AS per_billed,
				IFNULL(advance_paid, 0) AS pagado
			FROM `tabPurchase Order`
			WHERE docstatus = 1 AND transaction_date BETWEEN %s AND %s
			ORDER BY transaction_date DESC, name DESC
			LIMIT %s
			""",
			(month_start, as_of, int(limit)),
			as_dict=True,
		)
	except Exception:
		return empty
	out_rows = []
	for r in rows:
		total_oc = flt(r.total_oc)
		pagado = flt(r.pagado)
		out_rows.append(
			[
				str(r.transaction_date or ""),
				str(r.name or ""),
				str(r.proveedor or ""),
				_po_pipeline_label(r.per_received, r.per_billed),
				total_oc,
				round(flt(r.per_received), 2),
				pagado,
			]
		)
	return {
		"headers": headers,
		"rows": out_rows,
		"total": total,
		"truncated": total > len(out_rows),
		"month_start": str(month_start),
		"as_of": str(as_of),
	}


def _sueldos_ctc_dump(limit: int) -> dict:
	headers = ["empleado", "sucursal", "ctc_mensual", "estado", "dias", "costo_mes", "notas"]
	empty = {"headers": headers, "rows": [], "total": 0, "truncated": False}
	if not frappe.db.exists("DocType", "Employee"):
		return empty
	try:
		total = int(frappe.db.count("Employee", {"status": "Active"}) or 0)
		rows = frappe.db.sql(
			"""
			SELECT
				IFNULL(employee_name, name) AS empleado,
				IFNULL(branch, '') AS sucursal,
				IFNULL(ctc, 0) AS ctc,
				IFNULL(status, 'Active') AS estado
			FROM `tabEmployee`
			WHERE status = 'Active'
			ORDER BY employee_name ASC, name ASC
			LIMIT %s
			""",
			(int(limit),),
			as_dict=True,
		)
	except Exception:
		return empty
	out_rows = []
	for r in rows:
		ctc = flt(r.ctc)
		out_rows.append(
			[
				str(r.empleado or ""),
				str(r.sucursal or ""),
				ctc,
				str(r.estado or "Active"),
				30,
				"",  # notas; costo_mes is FX filled client-side
			]
		)
	return {
		"headers": headers,
		"rows": out_rows,
		"total": total,
		"truncated": total > len(out_rows),
	}


@frappe.whitelist(allow_guest=True)
def get_accounting_detail_tables(as_of_date=None, limit=None):
	"""ERP row dumps for starter tables (month-scoped where it matters).

	Compras OC: Purchase Orders with transaction_date in [month_start, as_of].
	Sueldos CTC: active employees (point-in-time headcount cost proxy).
	Both are capped by `limit` (default 45) so the sheet stays readable.
	"""
	today = str(getdate(as_of_date)) if as_of_date else nowdate()
	month_start = str(get_first_day(today))
	try:
		lim = int(limit) if limit not in (None, "") else DETAIL_ROW_LIMIT
	except (TypeError, ValueError):
		lim = DETAIL_ROW_LIMIT
	lim = max(1, min(80, lim))
	archivo_dump = {"headers": [], "rows": [], "total": 0, "truncated": False}
	try:
		from erpnext.erpnext_integrations.ecommerce_api.archivo_api import (
			archivo_month_dump,
		)

		archivo_dump = archivo_month_dump(month_start, today, lim)
	except Exception:
		pass
	return {
		"as_of": today,
		"month_start": month_start,
		"limit": lim,
		"compras_oc": _compras_oc_dump(month_start, today, lim),
		"sueldos_ctc": _sueldos_ctc_dump(lim),
		"archivo_gastos": archivo_dump,
	}


def _archivo_constants(month_start, as_of) -> dict:
	try:
		from erpnext.erpnext_integrations.ecommerce_api.archivo_api import (
			archivo_month_constants,
		)

		return archivo_month_constants(month_start, as_of)
	except Exception:
		return {
			"this_month_archivo_ops_total": 0.0,
			"this_month_archivo_posted_total": 0.0,
			"this_month_archivo_to_pay": 0.0,
			"this_month_archivo_paid": 0.0,
			"this_month_archivo_local_only": 0.0,
			"this_month_archivo_fines": 0.0,
			"this_month_archivo_utilities": 0.0,
			"this_month_archivo_petty": 0.0,
			"archivo_docs_active_count": 0,
			"archivo_docs_expired_count": 0,
			"archivo_missing_attachment_count": 0,
		}


def _safe_count(doctype: str, filters: dict) -> int:
	if not frappe.db.exists("DocType", doctype):
		return 0
	try:
		return int(frappe.db.count(doctype, filters) or 0)
	except Exception:
		return 0


def _prefix_short_ids(text: str) -> str:
	"""Rewrite legacy lowercase a01/s01 tokens to _a01/_s01 (not cell refs like A10)."""
	return re.sub(r"(^|[^A-Za-z0-9_])([as])(\d{2})\b", r"\1_\2\3", text or "")


def _normalize_variables(raw) -> list:
	if not isinstance(raw, list):
		return []
	out = []
	for v in raw:
		if not isinstance(v, dict):
			continue
		vid = str(v.get("id") or "").strip().lower()
		if re.match(r"^a\d{2}$", vid):
			vid = f"_{vid}"
		name = str(v.get("name") or "").strip().lower().replace(" ", "_")
		if not USER_VAR_ID_RE.match(vid) or not USER_VAR_NAME_RE.match(name):
			continue
		if not vid.startswith("_"):
			vid = f"_{vid}"
		out.append(
			{
				"id": vid,
				"name": name,
				"expr": _prefix_short_ids(str(v.get("expr") or "")),
				"notes": str(v.get("notes") or ""),
			}
		)
	return out


def _normalize_formats(raw) -> list:
	if not isinstance(raw, list):
		return []
	out = []
	for r in raw:
		if not isinstance(r, dict) or not r.get("id"):
			continue
		kind = str(r.get("kind") or "")
		if kind not in FORMAT_KINDS:
			continue
		out.append(
			{
				"id": str(r["id"]),
				"kind": kind,
				"needle": str(r.get("needle") or ""),
				"color": str(r.get("color") or "#fef08a"),
			}
		)
	return out


def _next_output_id(used: set) -> str:
	n = 1
	while True:
		oid = f"out_{n:02d}"
		if oid not in used:
			return oid
		n += 1


def _normalize_notebooks(raw, legacy_code: str = "") -> tuple[list, str]:
	used = set()
	used_nb = set()
	notebooks = []
	if isinstance(raw, list) and raw:
		for nb in raw[:20]:
			if not isinstance(nb, dict):
				continue
			nb_id = str(nb.get("id") or "").strip() or f"nb-{len(notebooks) + 1}"
			if nb_id in used_nb:
				nb_id = f"{nb_id}-{len(notebooks) + 1}"
			used_nb.add(nb_id)
			cells = []
			for c in (nb.get("cells") or [])[:40]:
				if not isinstance(c, dict):
					continue
				oid = str(c.get("output_id") or "").strip()
				if not oid.startswith("out_") or oid in used:
					oid = _next_output_id(used)
				used.add(oid)
				cells.append(
					{
						"id": str(c.get("id") or f"c-{len(cells) + 1}")[:80],
						"source": str(c.get("source") or "")[:20000],
						"output_id": oid,
					}
				)
			if not cells:
				oid = _next_output_id(used)
				used.add(oid)
				cells = [{"id": "c-1", "source": "", "output_id": oid}]
			name = str(nb.get("name") or "charts1.ipynb").strip() or "charts1.ipynb"
			name = name.replace("/", "_").replace("\\", "_")[:80]
			if not name.endswith(".ipynb"):
				name = f"{name}.ipynb"
			notebooks.append({"id": nb_id, "name": name, "cells": cells})
	if not notebooks:
		oid = "out_01"
		source = str(legacy_code or "")
		notebooks = [
			{
				"id": "nb-1",
				"name": "charts1.ipynb",
				"cells": [{"id": "c-1", "source": source, "output_id": oid}],
			}
		]
	active = ""
	return notebooks, active


def _normalize_reports(raw) -> dict:
	rows = []
	src_rows = []
	if isinstance(raw, dict):
		src_rows = raw.get("rows") or []
	elif isinstance(raw, list):
		src_rows = raw
	for r in src_rows[:40]:
		if not isinstance(r, dict):
			continue
		kind = str(r.get("kind") or "outputs")
		if kind not in {"outputs", "comment"}:
			kind = "outputs"
		try:
			cols = int(r.get("cols") or 1)
		except (TypeError, ValueError):
			cols = 1
		if cols not in (1, 2, 3):
			cols = 1
		oids = [str(x) for x in (r.get("output_ids") or []) if str(x).strip()]
		rows.append(
			{
				"id": str(r.get("id") or f"row-{len(rows) + 1}"),
				"kind": kind,
				"cols": cols,
				"output_ids": oids[:cols] if kind == "outputs" else [],
				"comment": str(r.get("comment") or "") if kind == "comment" else "",
			}
		)
	return {"rows": rows}


def _normalize_tables(raw) -> list:
	if not isinstance(raw, list):
		return []
	out = []
	used_ids = set()
	used_names = set()
	for t in raw[:80]:
		if not isinstance(t, dict):
			continue
		tid = str(t.get("id") or "").strip().lower()
		if not re.match(r"^t\d{2}$", tid) or tid in used_ids:
			n = 1
			while f"t{n:02d}" in used_ids:
				n += 1
			tid = f"t{n:02d}"
		name = str(t.get("name") or "").strip().lower().replace(" ", "_")
		if not re.match(r"^[a-z][a-z0-9_]*$", name):
			name = tid
		if name in used_names:
			name = f"{name}_{tid}"
		rng = str(t.get("range") or "").strip().upper().replace(" ", "")
		if not re.match(r"^[A-Z]{1,2}\d{1,3}(:[A-Z]{1,2}\d{1,3})?$", rng):
			continue
		used_ids.add(tid)
		used_names.add(name)
		out.append(
			{
				"id": tid,
				"name": name,
				"sheet_id": str(t.get("sheet_id") or t.get("sheetId") or ""),
				"range": rng,
			}
		)
	return out


def _col_index(letters: str) -> int:
	n = 0
	for ch in letters.upper():
		n = n * 26 + (ord(ch) - 64)
	return n - 1


def _parse_a1(ref: str):
	m = re.match(r"^([A-Z]{1,2})(\d{1,3})$", (ref or "").upper())
	if not m:
		return None
	return _col_index(m.group(1)), int(m.group(2)) - 1


def _range_refs(rng: str) -> list[str]:
	rng = (rng or "").upper()
	if ":" in rng:
		a, b = rng.split(":", 1)
	else:
		a = b = rng
	pa, pb = _parse_a1(a), _parse_a1(b)
	if not pa or not pb:
		return []
	min_c, max_c = min(pa[0], pb[0]), max(pa[0], pb[0])
	min_r, max_r = min(pa[1], pb[1]), max(pa[1], pb[1])
	out = []
	for r in range(min_r, max_r + 1):
		for c in range(min_c, max_c + 1):
			col = ""
			n = c + 1
			while n:
				n, rem = divmod(n - 1, 26)
				col = chr(65 + rem) + col
			out.append(f"{col}{r + 1}")
	return out


def _coerce_cell(raw):
	if raw is None:
		return None
	s = str(raw).strip()
	if s == "":
		return None
	try:
		n = float(s.replace(",", "."))
		if n.is_integer():
			return int(n)
		return n
	except Exception:
		return s


def _tables_as_frames(workbook: dict) -> dict:
	sheets = {str(s.get("id") or ""): s for s in workbook.get("sheets") or [] if isinstance(s, dict)}
	frames = {}
	for t in workbook.get("tables") or []:
		sheet = sheets.get(str(t.get("sheet_id") or ""))
		if not sheet:
			if sheets:
				sheet = next(iter(sheets.values()))
			else:
				continue
		cells = sheet.get("cells") if isinstance(sheet.get("cells"), dict) else {}
		refs = _range_refs(str(t.get("range") or ""))
		if len(refs) < 2:
			continue
		# Reconstruct rows from A1 order (row-major).
		pa = _parse_a1(refs[0])
		pb = _parse_a1(refs[-1])
		if not pa or not pb:
			continue
		width = pb[0] - pa[0] + 1
		rows = []
		cur = []
		for i, ref in enumerate(refs):
			cur.append(_coerce_cell(cells.get(ref)))
			if (i + 1) % width == 0:
				rows.append(cur)
				cur = []
		if len(rows) < 2:
			continue
		headers = []
		for i, h in enumerate(rows[0]):
			label = str(h or f"col_{i + 1}").strip() or f"col_{i + 1}"
			headers.append(label)
		records = []
		for row in rows[1:]:
			rec = {}
			for i, h in enumerate(headers):
				rec[h] = row[i] if i < len(row) else None
			records.append(rec)
		frames[str(t.get("name"))] = records
		frames[str(t.get("id"))] = records
	return frames


def _normalize_cache(raw) -> dict:
	if not isinstance(raw, dict):
		return {}
	out = {}
	for k, v in raw.items():
		if not isinstance(v, dict):
			continue
		out[str(k)] = {
			"output_id": str(v.get("output_id") or k),
			"type": str(v.get("type") or "empty"),
			"text": str(v.get("text") or "")[:8000],
			"image": (str(v.get("image") or ""))[:450000],
			"error": str(v.get("error") or "")[:4000],
			"ran_at": str(v.get("ran_at") or ""),
		}
	return out


def _starter_variables() -> list:
	return [
		{
			"id": "_a01",
			"name": "contribucion_comercial",
			"expr": "this_month_sales_net-this_month_po_received_value",
			"notes": "Proxy: ventas netas − valor OC recibido (no es COGS contable)",
		},
		{
			"id": "_a02",
			"name": "return_rate",
			"expr": "IF(this_month_sales_gross,this_month_returns_total/this_month_sales_gross,0)",
			"notes": "Tasa de devoluciones (0 si no hay ventas brutas)",
		},
		{
			"id": "_a03",
			"name": "caja_esperado_mes",
			"expr": "this_month_caja_opening_total+this_month_cash_total",
			"notes": "Aperturas del mes + efectivo cobrado (proxy de caja esperada)",
		},
	]


def _starter_sheets() -> list:
	"""Five-tab close pack from accounting_spreadsheet_workbook.md §7."""
	dash_id = "sheet-dash"
	pos_id = "sheet-pos"
	ventas_id = "sheet-ventas"
	compras_id = "sheet-compras"
	sueldos_id = "sheet-sueldos"

	dash_cells = {
		"A1": "Dashboard (mes)",
		"B1": "SYS",
		"C1": "Valor",
		"D1": "Nota",
		"A2": "Ventas brutas",
		"B2": "SYS",
		"C2": "=this_month_sales_gross",
		"D2": "SI excl. returns",
		"A3": "Notas de crédito",
		"B3": "SYS",
		"C3": "=this_month_returns_total",
		"D3": "SI is_return",
		"A4": "Ventas netas",
		"B4": "FX",
		"C4": "=C2-C3",
		"D4": "Brutas − returns",
		"A5": "Cobrado cash",
		"B5": "SYS",
		"C5": "=this_month_cash_total",
		"A6": "Cobrado tarjeta",
		"B6": "SYS",
		"C6": "=this_month_card_total",
		"A7": "Cobrado MP",
		"B7": "SYS",
		"C7": "=this_month_mp_total",
		"A8": "Cobrado transferencia",
		"B8": "SYS",
		"C8": "=this_month_transfer_total",
		"A9": "Compras OC (pedido)",
		"B9": "SYS",
		"C9": "=this_month_po_ordered",
		"A10": "Compras recibidas $",
		"B10": "SYS",
		"C10": "=this_month_po_received_value",
		"A11": "Pagado a proveedores",
		"B11": "SYS",
		"C11": "=this_month_payout_total",
		"A12": "CxC outstanding",
		"B12": "SYS",
		"C12": "=receivables_outstanding",
		"A13": "CxP outstanding",
		"B13": "SYS",
		"C13": "=payables_outstanding",
		"A14": "Inventario estimado",
		"B14": "SYS",
		"C14": "=inventory_value",
		"A15": "Masa salarial CTC",
		"B15": "SYS",
		"C15": "=employees_salary_total",
		"A16": "Contribución comercial",
		"B16": "FX",
		"C16": "=contribucion_comercial",
		"D16": "Proxy, no COGS",
		"A17": "Pedidos sin facturar",
		"B17": "SYS",
		"C17": "=so_unbilled_total",
		"A19": "Cierre del mes (checklist)",
		"A20": "1. Cajas Closed; Diff≈0",
		"B20": "IN",
		"A21": "2. WHITE = SI; BLACK aparte",
		"B21": "IN",
		"A22": "3. MP matched a PE/SI",
		"B22": "IN",
		"A23": "4. OC recibidas vs pagadas",
		"B23": "IN",
		"A24": "5. CxC/CxP aging revisado",
		"B24": "IN",
		"A25": "6. Snapshot inventario",
		"B25": "IN",
		"A26": "7. CTC joiners/leavers",
		"B26": "IN",
		"A27": "8. AFIP vs WHITE documentado",
		"B27": "IN",
		"A28": "9. Pedidos unbilled aceptados",
		"B28": "IN",
		"A29": "10. Dashboard locked archive",
		"B29": "IN",
	}
	dash_styles = {
		"A1": "#dbeafe",
		"B1": "#dbeafe",
		"C1": "#dbeafe",
		"D1": "#dbeafe",
		"B2": "#dbeafe",
		"B3": "#dbeafe",
		"B4": "#dcfce7",
		"B5": "#dbeafe",
		"B6": "#dbeafe",
		"B7": "#dbeafe",
		"B8": "#dbeafe",
		"B9": "#dbeafe",
		"B10": "#dbeafe",
		"B11": "#dbeafe",
		"B12": "#dbeafe",
		"B13": "#dbeafe",
		"B14": "#dbeafe",
		"B15": "#dbeafe",
		"B16": "#dcfce7",
		"B17": "#dbeafe",
		"A19": "#fef08a",
		"B20": "#fef08a",
		"B21": "#fef08a",
		"B22": "#fef08a",
		"B23": "#fef08a",
		"B24": "#fef08a",
		"B25": "#fef08a",
		"B26": "#fef08a",
		"B27": "#fef08a",
		"B28": "#fef08a",
		"B29": "#fef08a",
	}

	pos_cells = {
		"A1": "POS Diario / Cajas",
		"B1": "SYS = auto · IN = tipiado · Diff = contado − esperado",
		"A3": "Resumen mes",
		"A4": "Sesiones abiertas",
		"B4": "SYS",
		"C4": "=caja_sessions_open_count",
		"A5": "Sesiones cerradas",
		"B5": "SYS",
		"C5": "=caja_sessions_closed_count",
		"A6": "Aperturas $ (mes)",
		"B6": "SYS",
		"C6": "=this_month_caja_opening_total",
		"A7": "Efectivo cobrado",
		"B7": "SYS",
		"C7": "=this_month_cash_total",
		"A8": "Caja esperada (proxy)",
		"B8": "FX",
		"C8": "=caja_esperado_mes",
		"A9": "Contado manual (IN)",
		"B9": "IN",
		"C9": "0",
		"A10": "Diferencia",
		"B10": "FX",
		"C10": "=C9-C8",
		"A12": "fecha",
		"B12": "sesion",
		"C12": "caja",
		"D12": "cajero",
		"E12": "apertura",
		"F12": "ventas_cash",
		"G12": "contado",
		"H12": "diff",
		"I12": "estado",
		"A13": "",
		"B13": "",
		"C13": "",
		"D13": "",
		"E13": "",
		"F13": "",
		"G13": "",
		"H13": "=G13-E13-F13",
		"I13": "Open",
		"A14": "",
		"H14": "=G14-E14-F14",
		"A15": "",
		"H15": "=G15-E15-F15",
		"A17": "Cierre: Diff≈0 o nota; abiertas no entran al mes.",
	}
	pos_styles = {
		"A1": "#dbeafe",
		"B4": "#dbeafe",
		"B5": "#dbeafe",
		"B6": "#dbeafe",
		"B7": "#dbeafe",
		"B8": "#dcfce7",
		"B9": "#fef08a",
		"C9": "#fef08a",
		"B10": "#dcfce7",
		"A12": "#e5e7eb",
		"B12": "#e5e7eb",
		"C12": "#e5e7eb",
		"D12": "#e5e7eb",
		"E12": "#e5e7eb",
		"F12": "#e5e7eb",
		"G12": "#e5e7eb",
		"H12": "#e5e7eb",
		"I12": "#e5e7eb",
		"G13": "#fef08a",
		"G14": "#fef08a",
		"G15": "#fef08a",
	}

	ventas_cells = {
		"A1": "Ventas (libro ops)",
		"B1": "WHITE/BLACK split + pivote diario",
		"A3": "Métrica",
		"B3": "Tipo",
		"C3": "Valor",
		"A4": "WHITE $",
		"B4": "SYS",
		"C4": "=this_month_white_total",
		"A5": "BLACK $",
		"B5": "SYS",
		"C5": "=this_month_black_total",
		"A6": "Tickets WHITE",
		"B6": "SYS",
		"C6": "=this_month_si_white_count",
		"A7": "Tickets BLACK",
		"B7": "SYS",
		"C7": "=this_month_si_black_count",
		"A8": "IVA (ops)",
		"B8": "SYS",
		"C8": "=this_month_tax_total",
		"A9": "Neto pre-IVA",
		"B9": "SYS",
		"C9": "=this_month_net_total",
		"A10": "Tasa devolución",
		"B10": "FX",
		"C10": "=return_rate",
		"A12": "month",
		"B12": "revenue",
		"C12": "orders",
		"A13": "Jan",
		"B13": "1200",
		"C13": "18",
		"A14": "Feb",
		"B14": "1540",
		"C14": "21",
		"A15": "Mar",
		"B15": "980",
		"C15": "14",
		"A16": "Apr",
		"B16": "1710",
		"C16": "25",
		"A17": "May",
		"B17": "1890",
		"C17": "27",
		"A18": "Jun",
		"B18": "2100",
		"C18": "31",
		"A20": "Reemplazar filas A13:C18 con totales diarios del mes (IN).",
		"A22": "fecha",
		"B22": "comprobante",
		"C22": "cliente",
		"D22": "canal",
		"E22": "modo",
		"F22": "total",
		"G22": "cobrado",
		"H22": "mop",
		"A23": "",
		"E23": "WHITE",
		"A24": "",
		"E24": "BLACK",
	}
	ventas_styles = {
		"A1": "#dbeafe",
		"A3": "#e5e7eb",
		"B3": "#e5e7eb",
		"C3": "#e5e7eb",
		"B4": "#dbeafe",
		"B5": "#dbeafe",
		"B6": "#dbeafe",
		"B7": "#dbeafe",
		"B8": "#dbeafe",
		"B9": "#dbeafe",
		"B10": "#dcfce7",
		"A12": "#e5e7eb",
		"B12": "#e5e7eb",
		"C12": "#e5e7eb",
		"A22": "#e5e7eb",
		"B22": "#e5e7eb",
		"C22": "#e5e7eb",
		"D22": "#e5e7eb",
		"E22": "#e5e7eb",
		"F22": "#e5e7eb",
		"G22": "#e5e7eb",
		"H22": "#e5e7eb",
		"E23": "#fef08a",
		"E24": "#fef08a",
	}

	compras_cells = {
		"A1": "Compras operativas (OC)",
		"B1": "No es libro IVA compras hasta que PI sea first-class",
		"A3": "Métrica",
		"B3": "Tipo",
		"C3": "Valor",
		"A4": "OC pedidas $",
		"B4": "SYS",
		"C4": "=this_month_po_ordered",
		"A5": "Recibido $",
		"B5": "SYS",
		"C5": "=this_month_po_received_value",
		"A6": "Facturado $ (PI)",
		"B6": "SYS",
		"C6": "=this_month_po_billed_value",
		"A7": "Pagado (PE Pay)",
		"B7": "SYS",
		"C7": "=this_month_payout_total",
		"A8": "Saldo ops",
		"B8": "FX",
		"C8": "=C4-C7",
		"A9": "Recibido no facturado",
		"B9": "FX",
		"C9": "=C5-C6",
		"D9": "Riesgo accrual AP",
		"A10": "PI del mes (legacy)",
		"B10": "SYS",
		"C10": "=this_month_purchase_total",
		"A12": "fecha_oc",
		"B12": "oc",
		"C12": "proveedor",
		"D12": "pipeline",
		"E12": "total_oc",
		"F12": "recibido_pct",
		"G12": "pagado",
		"H12": "saldo",
		"A13": "(Refrescá la fecha → volcado de OCs del mes, tope 45)",
	}
	compras_styles = {
		"A1": "#dbeafe",
		"B1": "#fecaca",
		"A3": "#e5e7eb",
		"B3": "#e5e7eb",
		"C3": "#e5e7eb",
		"B4": "#dbeafe",
		"B5": "#dbeafe",
		"B6": "#dbeafe",
		"B7": "#dbeafe",
		"B8": "#dcfce7",
		"B9": "#dcfce7",
		"B10": "#dbeafe",
		"A12": "#e5e7eb",
		"B12": "#e5e7eb",
		"C12": "#e5e7eb",
		"D12": "#e5e7eb",
		"E12": "#e5e7eb",
		"F12": "#e5e7eb",
		"G12": "#e5e7eb",
		"H12": "#e5e7eb",
		"A13": "#fef9c3",
	}

	sueldos_cells = {
		"A1": "Sueldos (CTC)",
		"B1": "CTC ≠ liquidación — sin Salary Slip / cargas / bancos",
		"A3": "Empleados activos",
		"B3": "SYS",
		"C3": "=employees_count",
		"A4": "Masa salarial CTC",
		"B4": "SYS",
		"C4": "=employees_salary_total",
		"A5": "Prorrateo manual %",
		"B5": "IN",
		"C5": "1",
		"A6": "Costo del mes",
		"B6": "FX",
		"C6": "=C4*C5",
		"A8": "empleado",
		"B8": "sucursal",
		"C8": "ctc_mensual",
		"D8": "estado",
		"E8": "dias",
		"F8": "costo_mes",
		"G8": "notas",
		"A9": "(Refrescá → volcado de empleados Active, tope 45)",
		"A11": "Burn rate / headcount → Dashboard C15. No inventar recibos aquí.",
	}
	sueldos_styles = {
		"A1": "#dbeafe",
		"B1": "#fecaca",
		"B3": "#dbeafe",
		"B4": "#dbeafe",
		"B5": "#fef08a",
		"C5": "#fef08a",
		"B6": "#dcfce7",
		"A8": "#e5e7eb",
		"B8": "#e5e7eb",
		"C8": "#e5e7eb",
		"D8": "#e5e7eb",
		"E8": "#e5e7eb",
		"F8": "#e5e7eb",
		"G8": "#e5e7eb",
		"A9": "#fef9c3",
	}

	return [
		{"id": dash_id, "name": "Dashboard", "cells": dash_cells, "styles": dash_styles},
		{"id": pos_id, "name": "POS Diario", "cells": pos_cells, "styles": pos_styles},
		{"id": ventas_id, "name": "Ventas", "cells": ventas_cells, "styles": ventas_styles},
		{"id": compras_id, "name": "Compras", "cells": compras_cells, "styles": compras_styles},
		{"id": sueldos_id, "name": "Sueldos CTC", "cells": sueldos_cells, "styles": sueldos_styles},
	]


def _starter_tables() -> list:
	return [
		{"id": "t01", "name": "dashboard_kpis", "sheet_id": "sheet-dash", "range": "A2:C17"},
		{"id": "t02", "name": "monthly_sales", "sheet_id": "sheet-ventas", "range": "A12:C18"},
		{"id": "t03", "name": "pos_sessions", "sheet_id": "sheet-pos", "range": "A12:I15"},
		{"id": "t04", "name": "compras_oc", "sheet_id": "sheet-compras", "range": "A12:H12"},
		{"id": "t05", "name": "sueldos_ctc", "sheet_id": "sheet-sueldos", "range": "A8:G8"},
	]


def _workbook_is_blank(workbook: dict) -> bool:
	sheets = workbook.get("sheets") or []
	if not sheets:
		return True
	return all(not (s.get("cells") or {}) for s in sheets if isinstance(s, dict))


def _default_workbook() -> dict:
	notebooks, _ = _normalize_notebooks([], "")
	sheets = _starter_sheets()
	return {
		"sheets": sheets,
		"active_sheet_id": sheets[0]["id"],
		"variables": _starter_variables(),
		"notes": (
			"Pack inicial contable (Dashboard / POS / Ventas / Compras / Sueldos). "
			"SYS=auto refresh · IN=manual · FX=fórmula. CTC ≠ liquidación."
		),
		"code": "",
		"script": "",
		"conditional_formats": [],
		"notebooks": notebooks,
		"active_notebook_id": notebooks[0]["id"],
		"reports": {"rows": []},
		"tables": _starter_tables(),
		"snippet_cache": {},
		"snippet_run": {"status": "idle", "started_at": "", "finished_at": "", "error": ""},
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
			wb = _default_workbook()
			wb["sheets"] = [{"id": DEFAULT_SHEET_ID, "name": DEFAULT_SHEET_NAME, "cells": raw, "styles": {}}]
			return wb
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
	notebooks, _ = _normalize_notebooks(raw.get("notebooks"), str(raw.get("code") or ""))
	active_nb = str(raw.get("active_notebook_id") or "")
	if active_nb not in {n["id"] for n in notebooks}:
		active_nb = notebooks[0]["id"]
	run = raw.get("snippet_run") if isinstance(raw.get("snippet_run"), dict) else {}
	status = str(run.get("status") or "idle")
	started = str(run.get("started_at") or "")
	if status == "running" and started:
		try:
			from frappe.utils import get_datetime, now_datetime

			age = (now_datetime() - get_datetime(started)).total_seconds()
			if age > 120:
				status = "idle"
		except Exception:
			status = "idle"
	return {
		"sheets": sheets,
		"active_sheet_id": active,
		"variables": _normalize_variables(raw.get("variables")),
		"notes": str(raw.get("notes") or ""),
		"code": str(raw.get("code") or ""),
		"script": str(raw.get("script") or raw.get("code") or "")[:20000],
		"conditional_formats": _normalize_formats(raw.get("conditional_formats")),
		"notebooks": notebooks,
		"active_notebook_id": active_nb,
		"reports": _normalize_reports(raw.get("reports")),
		"tables": _normalize_tables(raw.get("tables")),
		"snippet_cache": _normalize_cache(raw.get("snippet_cache")),
		"snippet_run": {
			"status": status,
			"started_at": started,
			"finished_at": str(run.get("finished_at") or ""),
			"error": str(run.get("error") or ""),
		},
	}


def _get_sheet(scope: str | None, create: bool = True):
	scope = (scope or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
	if frappe.db.exists("Accounting Sheet", scope):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Accounting Sheet", scope)
		frappe.flags.ignore_permissions = False
		return doc
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


@frappe.whitelist(allow_guest=True)
def get_accounting_sheet(scope=None):
	doc = _get_sheet(scope)
	try:
		raw = json.loads(doc.cells_json or "{}")
	except Exception:
		raw = {}
	workbook = _normalize_workbook(raw)
	# Upgrade blank workbooks (legacy empty Sheet1) to the starter close pack.
	if _workbook_is_blank(workbook):
		workbook = _default_workbook()
		doc.cells_json = json.dumps(workbook, ensure_ascii=False)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {"scope": doc.scope, **workbook, "modified": str(doc.modified)}


@frappe.whitelist(allow_guest=True)
def reset_accounting_starter_workbook(scope=None):
	"""Replace workbook with the five-tab accounting starter pack (destructive)."""
	doc = _get_sheet(scope)
	workbook = _default_workbook()
	doc.cells_json = json.dumps(workbook, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "scope": doc.scope, "modified": str(doc.modified), **workbook}


@frappe.whitelist(allow_guest=True)
def save_accounting_sheet(
	scope=None,
	sheets=None,
	active_sheet_id=None,
	variables=None,
	notes=None,
	code=None,
	script=None,
	conditional_formats=None,
	notebooks=None,
	active_notebook_id=None,
	reports=None,
	tables=None,
):
	if isinstance(sheets, str):
		if not sheets.strip():
			sheets = None
		else:
			sheets = frappe.parse_json(sheets)
	if isinstance(variables, str):
		variables = frappe.parse_json(variables)
		if variables is None:
			variables = []
	if isinstance(conditional_formats, str):
		conditional_formats = frappe.parse_json(conditional_formats)
		if conditional_formats is None:
			conditional_formats = []
	if isinstance(notebooks, str):
		notebooks = frappe.parse_json(notebooks)
	if isinstance(reports, str):
		reports = frappe.parse_json(reports)
	if isinstance(tables, str):
		tables = frappe.parse_json(tables)
	doc = _get_sheet(scope)
	try:
		existing = _normalize_workbook(json.loads(doc.cells_json or "{}"))
	except Exception:
		existing = _default_workbook()
	# Omitted extras keep the stored values so an older client cannot wipe them.
	if sheets is None:
		sheets = existing["sheets"]
	if not isinstance(sheets, list):
		frappe.throw(_("sheets must be a list of {id, name, cells, styles}"))
	workbook = _normalize_workbook(
		{
			"sheets": sheets,
			"active_sheet_id": active_sheet_id if active_sheet_id is not None else existing["active_sheet_id"],
			"variables": variables if variables is not None else existing["variables"],
			"notes": notes if notes is not None else existing["notes"],
			"code": code if code is not None else existing["code"],
			"script": script if script is not None else existing.get("script", existing.get("code", "")),
			"conditional_formats": (
				conditional_formats if conditional_formats is not None else existing["conditional_formats"]
			),
			"notebooks": notebooks if notebooks is not None else existing.get("notebooks"),
			"active_notebook_id": (
				active_notebook_id if active_notebook_id is not None else existing.get("active_notebook_id")
			),
			"reports": reports if reports is not None else existing.get("reports"),
			"tables": tables if tables is not None else existing.get("tables"),
			"snippet_cache": existing.get("snippet_cache") or {},
			"snippet_run": existing.get("snippet_run") or {},
		}
	)
	doc.cells_json = json.dumps(workbook, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "scope": doc.scope, "modified": str(doc.modified), **workbook}


def _reserved_from_workbook(workbook: dict, extra) -> dict:
	from erpnext.erpnext_integrations.ecommerce_api.accounting_snippet_runtime import parse_namespace

	ns = {}
	try:
		ns.update(get_accounting_constants())
	except Exception:
		pass
	ns.update(parse_namespace(extra))
	for v in workbook.get("variables") or []:
		vid = str(v.get("id") or "")
		name = str(v.get("name") or "")
		expr = str(v.get("expr") or "0").strip()
		val = None
		try:
			val = float(expr.replace(",", "."))
		except Exception:
			if name in ns:
				val = ns.get(name)
			elif vid in ns:
				val = ns.get(vid)
		if val is None:
			continue
		if vid:
			ns[vid] = val
		if name:
			ns[name] = val
	return ns


def _execute_snippets(scope=None, namespace=None):
	from erpnext.erpnext_integrations.ecommerce_api.accounting_snippet_runtime import run_cells
	from frappe.utils import now

	doc = _get_sheet(scope)
	try:
		workbook = _normalize_workbook(json.loads(doc.cells_json or "{}"))
	except Exception:
		workbook = _default_workbook()
	workbook["snippet_run"] = {
		"status": "running",
		"started_at": str(now()),
		"finished_at": "",
		"error": "",
	}
	doc.cells_json = json.dumps(workbook, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	reserved = _reserved_from_workbook(workbook, namespace)
	cache = dict(workbook.get("snippet_cache") or {})
	error = ""
	try:
		cells = []
		for nb in workbook.get("notebooks") or []:
			cells.extend(nb.get("cells") or [])
		fresh = run_cells(cells, reserved, frames=_tables_as_frames(workbook))
		ran_at = str(now())
		for oid, payload in fresh.items():
			payload["ran_at"] = ran_at
			cache[oid] = payload
	except Exception as e:
		error = str(e)
	workbook["snippet_cache"] = _normalize_cache(cache)
	workbook["snippet_run"] = {
		"status": "error" if error else "idle",
		"started_at": workbook["snippet_run"].get("started_at") or "",
		"finished_at": str(now()),
		"error": error,
	}
	doc.cells_json = json.dumps(workbook, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"ok": not bool(error),
		"scope": doc.scope,
		"snippet_cache": workbook["snippet_cache"],
		"snippet_run": workbook["snippet_run"],
		"notebooks": workbook["notebooks"],
	}


@frappe.whitelist(allow_guest=True)
def run_accounting_snippets(scope=None, namespace=None, background=None):
	bg = str(background if background is not None else "1").lower() not in ("0", "false", "no")
	ns_payload = namespace
	if not isinstance(ns_payload, str):
		try:
			ns_payload = json.dumps(ns_payload or {})
		except Exception:
			ns_payload = "{}"
	if bg:
		try:
			frappe.enqueue(
				"erpnext.erpnext_integrations.ecommerce_api.accounting_sheet_api._execute_snippets",
				queue="short",
				timeout=90,
				scope=scope,
				namespace=ns_payload,
			)
			doc = _get_sheet(scope)
			try:
				workbook = _normalize_workbook(json.loads(doc.cells_json or "{}"))
			except Exception:
				workbook = _default_workbook()
			return {
				"ok": True,
				"queued": True,
				"scope": doc.scope,
				"snippet_cache": workbook.get("snippet_cache") or {},
				"snippet_run": workbook.get("snippet_run") or {},
				"notebooks": workbook.get("notebooks") or [],
			}
		except Exception:
			pass
	return _execute_snippets(scope=scope, namespace=ns_payload)


@frappe.whitelist()
def run_accounting_sheet_script(code=None, grids_json=None, constants_json=None):
	from erpnext.erpnext_integrations.ecommerce_api.accounting_script_runtime import run_accounting_script

	grid = None
	sheets = None
	if isinstance(grids_json, str):
		try:
			parsed = json.loads(grids_json)
		except Exception:
			parsed = {}
	else:
		parsed = grids_json if isinstance(grids_json, dict) else {}

	if isinstance(parsed, dict):
		if "grid" in parsed:
			grid = parsed.get("grid")
		if "sheets" in parsed:
			sheets = parsed.get("sheets")
		elif parsed and "grid" not in parsed:
			sheets = parsed

	return run_accounting_script(code=code, constants=constants_json, grid=grid, sheets=sheets)


@frappe.whitelist(allow_guest=True)
def get_accounting_snippet_outputs(scope=None):
	doc = _get_sheet(scope)
	try:
		workbook = _normalize_workbook(json.loads(doc.cells_json or "{}"))
	except Exception:
		workbook = _default_workbook()
	return {
		"scope": doc.scope,
		"snippet_cache": workbook.get("snippet_cache") or {},
		"snippet_run": workbook.get("snippet_run") or {},
		"notebooks": workbook.get("notebooks") or [],
		"active_notebook_id": workbook.get("active_notebook_id") or "",
		"reports": workbook.get("reports") or {"rows": []},
		"modified": str(doc.modified),
	}
