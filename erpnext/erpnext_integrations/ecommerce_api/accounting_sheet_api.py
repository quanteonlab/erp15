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
             cash_total, card_total, purchase_total, payout_total}
  e.g. today_revenue, this_month_tax_total, this_year_purchase_total

- Point-in-time state: `{domain}_{noun}_count` / `{domain}_{noun}_total` / `{domain}_outstanding`
  e.g. employees_count, employees_salary_total, receivables_outstanding

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
	empty = {"cash_total": 0.0, "card_total": 0.0, "payout_total": 0.0}
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
	total = 0.0
	for r in rows:
		amt = flt(r.amount)
		total += amt
		mop = (r.mode_of_payment or "").lower()
		if "cash" in mop:
			cash += amt
		elif "card" in mop or "tarjeta" in mop:
			card += amt
	return {"cash_total": cash, "card_total": card, "payout_total": total}


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


def _default_workbook() -> dict:
	notebooks, _ = _normalize_notebooks([], "")
	return {
		"sheets": [{"id": DEFAULT_SHEET_ID, "name": DEFAULT_SHEET_NAME, "cells": {}, "styles": {}}],
		"active_sheet_id": DEFAULT_SHEET_ID,
		"variables": [],
		"notes": "",
		"code": "",
		"script": "",
		"conditional_formats": [],
		"notebooks": notebooks,
		"active_notebook_id": notebooks[0]["id"],
		"reports": {"rows": []},
		"tables": [],
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
	return {"scope": doc.scope, **workbook, "modified": str(doc.modified)}


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
