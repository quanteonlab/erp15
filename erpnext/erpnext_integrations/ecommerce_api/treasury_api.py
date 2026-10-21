"""i056 — two-bucket treasury ledger (Cuenta A / Cuenta B).

Auto rows are derived on read (cierres, tarjeta, transferencia MoP, paid
pedidos/compras). A↔B transfers are stored on Treasury Ledger Entry.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate

from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company
from erpnext.erpnext_integrations.ecommerce_api.ops_kv import idempotent_request

CODES = ("A", "B")
DEFAULT_TITLES = {"A": "Cuenta A", "B": "Cuenta B"}
CASH_HINTS = ("cash", "efectivo")
CARD_HINTS = ("card", "tarjeta", "credit", "debit", "crédito", "débito", "visa", "master", "amex")
TRANSFER_HINTS = (
	"transfer",
	"transferencia",
	"bank",
	"banco",
	"draft",
	"mercado pago",
	"mercadopago",
	"mp ",
)
PAID_EPS = 0.05
OVERRIDE_KIND = "override"
POS_SPLIT_KIND = "pos_split"
MOVABLE_KINDS = frozenset(
	{"cierre", "tarjeta", "transferencia", "pedido", "compra", "pos_split"}
)
GANANCIAS_KINDS = frozenset(
	{"cierre", "tarjeta", "transferencia", "pedido", "compra", "pos_split"}
)
AUTO_ORDER_DEFAULT_KINDS = ("tarjeta", "transferencia", "compra")
DEMO_TAG = "CUENTAS_DEMO"
POS_MOPS = ("cash", "card", "transfer")
POS_LABEL = {"cash": "POS CASH", "card": "POS CARD", "transfer": "POS TRANSFER"}
POS_KIND = {"cash": "cierre", "card": "tarjeta", "transfer": "transferencia"}


def _as_str(v) -> str:
	if v is None or isinstance(v, (list, dict, tuple)):
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _code(v) -> str:
	s = _as_str(v).upper()
	return s if s in CODES else ""


def _parse_day(raw, fallback=None):
	s = _as_str(raw)
	if not s:
		return fallback
	try:
		return getdate(s)
	except Exception:
		return fallback


def _require_company(company=None) -> str:
	co = resolve_company(company)
	if not co:
		frappe.throw(_("Company is required"), frappe.ValidationError)
	return co


def _month_bounds():
	today = getdate()
	start = getdate(f"{today.year}-{today.month:02d}-01")
	return start, today


def _mop_kind(name: str | None) -> str:
	s = (name or "").strip().lower()
	if any(h in s for h in CASH_HINTS):
		return "cash"
	if any(h in s for h in CARD_HINTS):
		return "card"
	if any(h in s for h in TRANSFER_HINTS):
		return "transfer"
	return "other"


def _account_for_mop(kind: str) -> str:
	return "A" if kind == "cash" else "B"


def _company(company=None) -> str:
	return _require_company(company)


def _n_from_label(label: str) -> int:
	s = _as_str(label)
	if s.endswith(")") and "(" in s:
		try:
			return int(s.rsplit("(", 1)[-1].rstrip(")"))
		except ValueError:
			return 0
	return 0


def _next_split_n(company: str, mop: str, dest: str) -> int:
	rows = frappe.get_all(
		"Treasury Ledger Entry",
		filters={"company": company, "kind": POS_SPLIT_KIND, "account_code": dest},
		fields=["label"],
		ignore_permissions=True,
		limit_page_length=2000,
	)
	used = set()
	prefix = POS_LABEL.get(mop, "POS")
	for r in rows or []:
		lab = _as_str(r.label)
		if not lab.startswith(prefix):
			continue
		n = _n_from_label(lab)
		if n:
			used.add(n)
	n = 1
	while n in used:
		n += 1
	return n


def _doctype_ready() -> bool:
	return bool(frappe.db.exists("DocType", "Treasury Account")) and bool(
		frappe.db.exists("DocType", "Treasury Ledger Entry")
	)


def _ensure_accounts(company: str) -> list[dict]:
	if not _doctype_ready():
		frappe.throw(_("Treasury DocTypes are not migrated"), frappe.ValidationError)
	out = []
	for code in CODES:
		row = frappe.db.get_value(
			"Treasury Account",
			{"company": company, "code": code},
			["name", "company", "code", "title"],
			as_dict=True,
		)
		if not row:
			doc = frappe.get_doc(
				{
					"doctype": "Treasury Account",
					"company": company,
					"code": code,
					"title": DEFAULT_TITLES[code],
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
			row = frappe._dict(
				{
					"name": doc.name,
					"company": company,
					"code": code,
					"title": doc.title,
				}
			)
		out.append({"name": row.name, "company": company, "code": code, "title": row.title})
	return out


def _row(
	*,
	account: str,
	date,
	kind: str,
	amount: float,
	label: str,
	source_key: str,
	ref_doctype: str | None = None,
	ref_name: str | None = None,
	counterpart: str | None = None,
	note: str | None = None,
) -> dict:
	return {
		"account": account,
		"date": str(date),
		"kind": kind,
		"amount": flt(amount, 2),
		"label": label,
		"source_key": source_key,
		"ref_doctype": ref_doctype,
		"ref_name": ref_name,
		"counterpart": counterpart,
		"note": note,
		"movable": kind in MOVABLE_KINDS,
		"reassigned": False,
	}


def _company_clause(alias: str, company: str | None) -> tuple[str, dict]:
	if not company:
		return "1=1", {}
	return f"{alias}.company = %(company)s", {"company": company}


def _sip_by_day(company: str | None, start, end) -> list[dict]:
	"""POS / standalone SIP amounts grouped by warehouse / date / mop.

	Invoices that belong to a Sales Order are excluded so they appear once as
	named ``pedido`` rows instead of being double-counted in daily totals.
	"""
	if not frappe.db.exists("DocType", "Sales Invoice Payment"):
		return []
	co_sql, values = _company_clause("si", company)
	values.update({"start": start, "end": end})
	return frappe.db.sql(
		f"""
		SELECT
			si.posting_date AS posting_date,
			COALESCE((
				SELECT sii.warehouse
				FROM `tabSales Invoice Item` sii
				WHERE sii.parent = si.name
				ORDER BY sii.idx
				LIMIT 1
			), '') AS warehouse,
			sip.mode_of_payment AS mode_of_payment,
			SUM(sip.amount) AS amount
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Invoice Payment` sip ON sip.parent = si.name
		WHERE si.docstatus = 1
		  AND IFNULL(si.is_return, 0) = 0
		  AND si.posting_date BETWEEN %(start)s AND %(end)s
		  AND {co_sql}
		  AND NOT EXISTS (
		    SELECT 1 FROM `tabSales Invoice Item` sii2
		    WHERE sii2.parent = si.name
		      AND IFNULL(sii2.sales_order, '') != ''
		  )
		GROUP BY si.posting_date, warehouse, sip.mode_of_payment
		""",
		values,
		as_dict=True,
	)


def _register_label(warehouse: str, cache: dict | None = None) -> str:
	wh = (warehouse or "").strip()
	if not wh:
		return "—"
	if cache is not None and wh in cache:
		return cache[wh]
	name = frappe.db.get_value("POS Profile", {"warehouse": wh}, "name")
	label = name or wh
	if cache is not None:
		cache[wh] = label
	return label


def _pos_label(mop: str, day) -> str:
	d = getdate(day)
	return f"{POS_LABEL.get(mop, 'POS')} {d.day:02d}/{d.month:02d}/{d.year}"


def _pos_day_key(mop: str, day) -> str:
	return f"pos:{mop}:{day}"


def _pos_invoices(company: str | None, start, end) -> list[dict]:
	co_sql, values = _company_clause("si", company)
	values.update(
		{
			"start": start,
			"end": end,
			"pos_tag": "%pos_session:%",
			"demo_tag": f"%{DEMO_TAG}:pos%",
			"offline_tag": "%offline_order_uuid:%",
		}
	)
	pos_bit = "IFNULL(si.is_pos, 0) = 1 OR " if frappe.db.has_column("Sales Invoice", "is_pos") else ""
	extra = ["IFNULL(si.remarks, '') LIKE %(offline_tag)s"]
	if frappe.db.has_column("Sales Invoice", "pos_profile"):
		extra.append("IFNULL(si.pos_profile, '') != ''")
	extra_sql = " OR " + " OR ".join(extra)
	return frappe.db.sql(
		f"""
		SELECT
			si.name AS name,
			si.posting_date AS posting_date,
			si.posting_time AS posting_time,
			si.customer AS customer,
			si.customer_name AS customer_name,
			si.grand_total AS grand_total,
			si.remarks AS remarks,
			COALESCE((
				SELECT sii.warehouse
				FROM `tabSales Invoice Item` sii
				WHERE sii.parent = si.name
				ORDER BY sii.idx
				LIMIT 1
			), '') AS warehouse
		FROM `tabSales Invoice` si
		WHERE si.docstatus = 1
		  AND IFNULL(si.is_return, 0) = 0
		  AND si.posting_date BETWEEN %(start)s AND %(end)s
		  AND {co_sql}
		  AND NOT EXISTS (
		    SELECT 1 FROM `tabSales Invoice Item` sii2
		    WHERE sii2.parent = si.name
		      AND IFNULL(sii2.sales_order, '') != ''
		  )
		  AND (
		    {pos_bit}IFNULL(si.remarks, '') LIKE %(pos_tag)s
		    OR IFNULL(si.remarks, '') LIKE %(demo_tag)s
		    {extra_sql}
		  )
		ORDER BY si.posting_date ASC, si.posting_time ASC, si.name ASC
		""",
		values,
		as_dict=True,
	)


def _invoice_mop_slices(names: list[str]) -> dict[str, dict[str, float]]:
	out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
	if not names:
		return out
	if frappe.db.exists("DocType", "Sales Invoice Payment"):
		for p in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ["in", names]},
			fields=["parent", "mode_of_payment", "amount"],
			ignore_permissions=True,
			limit_page_length=20000,
		) or []:
			out[p.parent][_mop_kind(p.mode_of_payment)] += flt(p.amount)
	missing = [n for n in names if not out[n]]
	if missing:
		for r in frappe.db.sql(
			"""
			SELECT per.reference_name AS invoice, pe.mode_of_payment AS mop, per.allocated_amount AS amount
			FROM `tabPayment Entry Reference` per
			INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
			WHERE pe.docstatus = 1
			  AND per.reference_doctype = 'Sales Invoice'
			  AND per.reference_name IN %(names)s
			""",
			{"names": tuple(missing)},
			as_dict=True,
		) or []:
			out[r.invoice][_mop_kind(r.mop)] += flt(r.amount)
	return out


def _pos_split_map(company: str) -> dict[str, dict]:
	if not _doctype_ready():
		return {}
	rows = frappe.get_all(
		"Treasury Ledger Entry",
		filters={"company": company, "kind": POS_SPLIT_KIND},
		fields=["name", "source_key", "account_code", "amount", "label", "note", "posting_date"],
		ignore_permissions=True,
		limit_page_length=5000,
	)
	out = {}
	for r in rows or []:
		inv = _as_str(r.source_key).split(":", 1)[-1] if _as_str(r.source_key).startswith("pos_split:") else ""
		if inv:
			out[inv] = r
	return out


def _pos_sale_slices(company: str | None, start, end) -> list[dict]:
	invoices = _pos_invoices(company, start, end)
	slices = _invoice_mop_slices([r.name for r in invoices])
	out = []
	for inv in invoices:
		parts = slices.get(inv.name) or {}
		if not parts:
			parts = {"cash": flt(inv.grand_total)}
		for mop, amt in parts.items():
			if mop not in POS_MOPS or abs(amt) < PAID_EPS:
				continue
			out.append(
				{
					"invoice": inv.name,
					"date": str(inv.posting_date),
					"time": str(inv.posting_time) if inv.posting_time else None,
					"customer": inv.customer,
					"customer_name": (inv.customer_name or inv.customer or "").strip(),
					"mop": mop,
					"amount": flt(amt, 2),
					"warehouse": inv.warehouse or "",
				}
			)
	return out


def _pos_day_rows(company: str | None, start, end) -> list[dict]:
	"""One POS CASH/CARD/TRANSFER row per date, minus split sales."""
	co = company or ""
	splits = _pos_split_map(co) if co else {}
	by_day: dict[tuple[str, str], float] = defaultdict(float)
	for s in _pos_sale_slices(company, start, end):
		if s["invoice"] in splits:
			continue
		by_day[(s["mop"], s["date"])] += s["amount"]
	out = []
	for (mop, day), amt in by_day.items():
		if abs(amt) < PAID_EPS:
			continue
		out.append(
			_row(
				account=_account_for_mop(mop),
				date=day,
				kind=POS_KIND[mop],
				amount=amt,
				label=_pos_label(mop, day),
				source_key=_pos_day_key(mop, day),
				ref_doctype="Sales Invoice",
				ref_name=None,
				note=mop,
			)
		)
	return out


def _closed_cash_rows(company: str | None, start, end, sip=None) -> list[dict]:
	return [r for r in _pos_day_rows(company, start, end) if r["kind"] == "cierre"]


def _daily_mop_rows(company: str | None, start, end, kind: str, sip=None) -> list[dict]:
	want = "tarjeta" if kind == "card" else "transferencia"
	return [r for r in _pos_day_rows(company, start, end) if r["kind"] == want]


def _so_paid_rows(company: str | None, start, end) -> list[dict]:
	filters: dict = {
		"docstatus": 1,
		"status": ["not in", ["Cancelled", "Closed", "On Hold"]],
		"transaction_date": ["between", [start, end]],
	}
	if company:
		filters["company"] = company
	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=["name", "customer_name", "grand_total", "transaction_date", "per_billed", "advance_paid"],
		ignore_permissions=True,
		limit_page_length=2000,
		order_by="transaction_date desc",
	)
	if not orders:
		return []
	so_names = [so.name for so in orders]
	so_to_si: dict[str, set[str]] = defaultdict(set)
	for r in frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": ["in", so_names], "docstatus": 1},
		fields=["sales_order", "parent"],
		ignore_permissions=True,
		limit_page_length=20000,
	) or []:
		if r.parent:
			so_to_si[r.sales_order].add(r.parent)
	all_si = list({n for names in so_to_si.values() for n in names})
	outstanding: dict[str, float] = {}
	remarks: dict[str, str] = {}
	if all_si:
		for si in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", all_si], "docstatus": 1},
			fields=["name", "outstanding_amount", "remarks"],
			ignore_permissions=True,
			limit_page_length=20000,
		) or []:
			outstanding[si.name] = flt(si.outstanding_amount)
			remarks[si.name] = si.remarks or ""
	sip_by_si: dict[str, list] = defaultdict(list)
	if all_si and frappe.db.exists("DocType", "Sales Invoice Payment"):
		for p in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ["in", all_si]},
			fields=["parent", "mode_of_payment", "amount"],
			ignore_permissions=True,
			limit_page_length=20000,
		) or []:
			sip_by_si[p.parent].append(p)

	out = []
	for so in orders:
		si_names = list(so_to_si.get(so.name) or [])
		paid = flt(so.per_billed) >= 99.99 or flt(so.advance_paid) + PAID_EPS >= flt(so.grand_total)
		if si_names:
			open_si = any(outstanding.get(n, 0) > PAID_EPS for n in si_names)
			paid = (paid or not open_si) and not open_si
		if not paid:
			continue
		# Skip walk-in POS invoices tagged to a cash session (already in cierre/tarjeta).
		if si_names and all("pos_session:" in remarks.get(n, "") for n in si_names):
			continue
		slices: dict[str, float] = defaultdict(float)
		for n in si_names:
			for p in sip_by_si.get(n) or []:
				slices[_mop_kind(p.mode_of_payment)] += flt(p.amount)
		if not slices:
			slices["cash"] = flt(so.grand_total)
		day = so.transaction_date
		label = (so.customer_name or so.name or "").strip() or so.name
		for mop_kind, amt in slices.items():
			if abs(amt) < PAID_EPS:
				continue
			out.append(
				_row(
					account=_account_for_mop(mop_kind),
					date=day,
					kind="pedido",
					amount=amt,
					label=label,
					source_key=f"so:{so.name}:{mop_kind}",
					ref_doctype="Sales Order",
					ref_name=so.name,
				)
			)
	return out


def _pe_slices_for_pos(po_names: list[str]) -> dict[str, tuple[float, dict[str, float]]]:
	out: dict[str, tuple[float, dict[str, float]]] = {}
	if not po_names:
		return out
	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Purchase Order", "reference_name": ["in", po_names]},
		fields=["parent", "reference_name", "allocated_amount"],
		ignore_permissions=True,
		limit_page_length=20000,
	)
	pe_names = list({r.parent for r in (refs or []) if r.parent})
	pe_map = {}
	if pe_names:
		for pe in frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", pe_names]},
			fields=["name", "docstatus", "mode_of_payment"],
			ignore_permissions=True,
			limit_page_length=20000,
		):
			pe_map[pe.name] = pe
	totals: dict[str, float] = defaultdict(float)
	slices: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
	for r in refs or []:
		pe = pe_map.get(r.parent)
		if not pe or cint(pe.docstatus) != 1:
			continue
		amt = flt(r.allocated_amount)
		totals[r.reference_name] += amt
		slices[r.reference_name][_mop_kind(pe.mode_of_payment)] += amt
	for name in po_names:
		out[name] = (totals.get(name, 0.0), dict(slices.get(name) or {}))
	return out


def _po_paid_amount(po_name: str) -> tuple[float, dict[str, float]]:
	return _pe_slices_for_pos([po_name]).get(po_name, (0.0, {}))


def _po_has_factura(po_name: str) -> bool:
	name = _as_str(po_name)
	if not name:
		return False
	if frappe.db.has_column("Purchase Order", "custom_supplier_invoice_ref"):
		if _as_str(frappe.db.get_value("Purchase Order", name, "custom_supplier_invoice_ref")):
			return True
	if frappe.db.count("File", {"attached_to_doctype": "Purchase Order", "attached_to_name": name}):
		return True
	try:
		from erpnext.erpnext_integrations.ecommerce_api.buying_api import _find_po_archivo_twin

		return bool(_find_po_archivo_twin(name))
	except Exception:
		return False


def _po_paid_rows(company: str | None, start, end) -> list[dict]:
	filters: dict = {
		"docstatus": 1,
		"status": ["not in", ["Cancelled"]],
		"transaction_date": ["between", [start, end]],
	}
	if company:
		filters["company"] = company
	orders = frappe.get_all(
		"Purchase Order",
		filters=filters,
		fields=["name", "supplier_name", "grand_total", "transaction_date"],
		ignore_permissions=True,
		limit_page_length=2000,
		order_by="transaction_date desc",
	)
	if not orders:
		return []
	paid_map = _pe_slices_for_pos([po.name for po in orders])
	out = []
	for po in orders:
		paid, slices = paid_map.get(po.name, (0.0, {}))
		if paid + PAID_EPS < flt(po.grand_total):
			continue
		if not slices:
			slices = {"cash": paid or flt(po.grand_total)}
		day = po.transaction_date
		label = (po.supplier_name or po.name or "").strip() or po.name
		for mop_kind, amt in slices.items():
			if abs(amt) < PAID_EPS:
				continue
			out.append(
				_row(
					account="B",
					date=day,
					kind="compra",
					amount=-abs(amt),
					label=label,
					source_key=f"po:{po.name}:{mop_kind}",
					ref_doctype="Purchase Order",
					ref_name=po.name,
					note="sin factura" if not _po_has_factura(po.name) else "factura",
				)
			)
	return out


def _stored_rows(company: str, start, end) -> list[dict]:
	if not _doctype_ready():
		return []
	rows = frappe.get_all(
		"Treasury Ledger Entry",
		filters={
			"company": company,
			"posting_date": ["between", [start, end]],
		},
		fields=[
			"name",
			"account_code",
			"posting_date",
			"kind",
			"amount",
			"counterpart_code",
			"label",
			"note",
			"source_key",
		],
		ignore_permissions=True,
		order_by="posting_date asc, creation asc",
	)
	out = []
	for r in rows or []:
		if (r.kind or "") == OVERRIDE_KIND:
			continue
		ref_dt = "Treasury Ledger Entry"
		ref_nm = r.name
		if (r.kind or "") == POS_SPLIT_KIND and _as_str(r.source_key).startswith("pos_split:"):
			ref_dt = "Sales Invoice"
			ref_nm = r.source_key.split(":", 1)[-1]
		out.append(
			_row(
				account=r.account_code,
				date=r.posting_date,
				kind=r.kind or "movimiento",
				amount=flt(r.amount),
				label=r.label or r.note or "Movimiento",
				source_key=r.source_key or r.name,
				ref_doctype=ref_dt,
				ref_name=ref_nm,
				counterpart=r.counterpart_code,
				note=r.note,
			)
		)
	return out


def _ganancias(rows: list[dict]) -> float:
	return flt(sum(flt(r.get("amount")) for r in rows if r.get("kind") in GANANCIAS_KINDS), 2)


def _auto_rows(company: str | None, start, end) -> list[dict]:
	out: list[dict] = []
	for label, fn in (
		("pos", lambda: _pos_day_rows(company, start, end)),
		("pedido", lambda: _so_paid_rows(company, start, end)),
		("compra", lambda: _po_paid_rows(company, start, end)),
	):
		try:
			out.extend(fn() or [])
		except Exception:
			frappe.log_error(title=f"treasury auto {label}")
	return out


def _overrides(company: str) -> dict[str, str]:
	if not _doctype_ready():
		return {}
	rows = frappe.get_all(
		"Treasury Ledger Entry",
		filters={"company": company, "kind": OVERRIDE_KIND},
		fields=["source_key", "account_code"],
		ignore_permissions=True,
		limit_page_length=5000,
	)
	out: dict[str, str] = {}
	for r in rows or []:
		key = _as_str(r.source_key)
		code = _code(r.account_code)
		if key and code:
			out[key] = code
	return out


def _apply_overrides(rows: list[dict], overrides: dict[str, str]) -> list[dict]:
	if not overrides:
		return rows
	out = []
	for r in rows:
		dest = overrides.get(r.get("source_key") or "")
		if dest in CODES and dest != r.get("account"):
			out.append({**r, "account": dest, "reassigned": True})
		else:
			out.append(r)
	return out


def _sum_by_account(rows: list[dict]) -> dict[str, float]:
	totals = {"A": 0.0, "B": 0.0}
	for r in rows:
		code = r.get("account")
		if code in totals:
			totals[code] += flt(r.get("amount"))
	return totals


def _with_running(rows: list[dict], opening: float) -> list[dict]:
	bal = flt(opening)
	out = []
	ordered = sorted(rows, key=lambda r: (str(r.get("date") or ""), str(r.get("source_key") or "")))
	for r in ordered:
		bal = flt(bal + flt(r.get("amount")), 2)
		out.append({**r, "balance": bal})
	return out


@frappe.whitelist(allow_guest=True)
def list_treasury_accounts(company=None):
	"""Ensure A/B exist and return titles + all-time balances."""
	co = _require_company(company)
	accounts = _ensure_accounts(co)
	start, today = getdate("2000-01-01"), getdate()
	ov = _overrides(co)
	totals = _sum_by_account(
		_apply_overrides(_auto_rows(co, start, today), ov) + _stored_rows(co, start, today)
	)
	for a in accounts:
		a["balance"] = flt(totals.get(a["code"], 0), 2)
	return {"company": co, "accounts": accounts}


@frappe.whitelist(allow_guest=True)
def rename_treasury_account(code=None, title=None, company=None):
	"""Rename Cuenta A or B. Empty/unknown code is a controlled error."""
	co = _require_company(company)
	c = _code(code)
	if not c:
		frappe.throw(_("Account code must be A or B"), frappe.ValidationError)
	name = _as_str(title)
	if not name:
		frappe.throw(_("Account name is required"), frappe.ValidationError)
	if len(name) > 80:
		name = name[:80]
	_ensure_accounts(co)
	docname = frappe.db.get_value("Treasury Account", {"company": co, "code": c}, "name")
	if not docname:
		frappe.throw(_("Account {0} not found").format(c), frappe.DoesNotExistError)
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Treasury Account", docname)
	frappe.flags.ignore_permissions = False
	doc.title = name
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "code": c, "title": name}


@frappe.whitelist(allow_guest=True)
def list_treasury_ledger(from_date=None, to_date=None, company=None):
	"""Merged auto + stored ledger for both accounts. Dirty dates → this month."""
	co = _require_company(company)
	_ensure_accounts(co)
	month_start, today = _month_bounds()
	start = _parse_day(from_date, month_start)
	end = _parse_day(to_date, today)
	if start > end:
		start, end = end, start

	before_end = add_days(start, -1)
	ov = _overrides(co)
	opening_rows = _apply_overrides(
		_auto_rows(co, getdate("2000-01-01"), before_end), ov
	) + _stored_rows(co, getdate("2000-01-01"), before_end)
	opening = _sum_by_account(opening_rows)

	period = _apply_overrides(_auto_rows(co, start, end), ov) + _stored_rows(co, start, end)
	by_acc = {"A": [], "B": []}
	for r in period:
		if r["account"] in by_acc:
			by_acc[r["account"]].append(r)

	accounts = {a["code"]: a for a in _ensure_accounts(co)}
	out = {"company": co, "from_date": str(start), "to_date": str(end), "today": str(today)}
	gain_total = 0.0
	for code in CODES:
		open_bal = flt(opening.get(code, 0), 2)
		rows = _with_running(by_acc[code], open_bal)
		net = flt(sum(flt(r["amount"]) for r in rows), 2)
		gain = _ganancias(rows)
		gain_total += gain
		out[code.lower()] = {
			"code": code,
			"title": accounts[code]["title"],
			"opening": open_bal,
			"net": net,
			"ganancias": gain,
			"closing": flt(open_bal + net, 2),
			"rows": rows,
		}
	out["ganancias"] = flt(gain_total, 2)
	return out


@frappe.whitelist(allow_guest=True)
@idempotent_request
def transfer_treasury(from_code=None, to_code=None, amount=None, date=None, note=None, company=None):
	"""Move money A↔B. Same account / dirty amount → controlled ValidationError."""
	co = _require_company(company)
	src = _code(from_code)
	dst = _code(to_code)
	if not src or not dst:
		frappe.throw(_("Both from_code and to_code must be A or B"), frappe.ValidationError)
	if src == dst:
		frappe.throw(_("Cannot transfer to the same account"), frappe.ValidationError)
	try:
		amt = flt(amount)
	except Exception:
		amt = 0.0
	if amt <= 0:
		frappe.throw(_("Amount must be greater than zero"), frappe.ValidationError)
	day = _parse_day(date, getdate())
	_ensure_accounts(co)
	pair_key = f"xfer:{co}:{src}:{dst}:{day}:{flt(amt, 2)}"
	note_s = _as_str(note)
	src_title = frappe.db.get_value("Treasury Account", {"company": co, "code": src}, "title") or src
	dst_title = frappe.db.get_value("Treasury Account", {"company": co, "code": dst}, "title") or dst

	created = []
	for code, signed, label in (
		(src, -abs(amt), f"→ {dst_title}"),
		(dst, abs(amt), f"← {src_title}"),
	):
		doc = frappe.get_doc(
			{
				"doctype": "Treasury Ledger Entry",
				"company": co,
				"account_code": code,
				"posting_date": day,
				"kind": "movimiento",
				"amount": signed,
				"counterpart_code": dst if code == src else src,
				"label": label,
				"note": note_s,
				"source_key": f"{pair_key}:{code}",
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(doc.name)
	frappe.db.commit()
	return {
		"ok": True,
		"from_code": src,
		"to_code": dst,
		"amount": flt(amt, 2),
		"date": str(day),
		"entries": created,
	}


@frappe.whitelist(allow_guest=True)
@idempotent_request
def reassign_treasury_row(source_key=None, to_code=None, company=None):
	"""Pin an auto row (pedido/compra/cierre/…) to Cuenta A or B."""
	co = _require_company(company)
	key = _as_str(source_key)
	dest = _code(to_code)
	if not key:
		frappe.throw(_("source_key is required"), frappe.ValidationError)
	if key.startswith("xfer:") or key.startswith("opening:"):
		frappe.throw(_("Cannot reassign a transfer or opening row"), frappe.ValidationError)
	if not dest:
		frappe.throw(_("Account code must be A or B"), frappe.ValidationError)
	_ensure_accounts(co)
	if key.startswith(f"{POS_SPLIT_KIND}:"):
		split_name = frappe.db.get_value(
			"Treasury Ledger Entry",
			{"company": co, "kind": POS_SPLIT_KIND, "source_key": key},
			"name",
		)
		if split_name:
			frappe.flags.ignore_permissions = True
			doc = frappe.get_doc("Treasury Ledger Entry", split_name)
			frappe.flags.ignore_permissions = False
			doc.account_code = dest
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return {"ok": True, "source_key": key, "to_code": dest, "name": doc.name}
	existing = frappe.db.get_value(
		"Treasury Ledger Entry",
		{"company": co, "kind": OVERRIDE_KIND, "source_key": key},
		"name",
	)
	if existing:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Treasury Ledger Entry", existing)
		frappe.flags.ignore_permissions = False
		doc.account_code = dest
		doc.save(ignore_permissions=True)
		name = doc.name
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Treasury Ledger Entry",
				"company": co,
				"account_code": dest,
				"posting_date": getdate(),
				"kind": OVERRIDE_KIND,
				"amount": 0,
				"label": "Reassign",
				"source_key": key,
			}
		)
		doc.insert(ignore_permissions=True)
		name = doc.name
	frappe.db.commit()
	return {"ok": True, "source_key": key, "to_code": dest, "name": name}


@frappe.whitelist(allow_guest=True)
def list_treasury_pos_day(company: str | None = None, day: str | None = None, mop: str | None = None) -> dict:
	"""Sales that make up a POS CASH/CARD/TRANSFER day row (for the split modal)."""
	co = _company(company)
	d = _parse_day(day, getdate())
	want = _as_str(mop).lower() or "cash"
	if want not in POS_MOPS:
		frappe.throw(_("mop must be cash, card or transfer"))
	splits = _pos_split_map(co)
	sales = []
	for sl in _pos_sale_slices(co, d, d):
		if sl["mop"] != want:
			continue
		inv = sl["invoice"]
		split = splits.get(inv) or {}
		sales.append(
			{
				"invoice": inv,
				"customer": sl.get("customer") or "",
				"customer_name": sl.get("customer_name") or sl.get("customer") or "",
				"amount": sl["amount"],
				"mop": sl["mop"],
				"time": sl.get("time") or "",
				"split": bool(split),
				"split_account": (split.get("account_code") if isinstance(split, dict) else "")
				or (getattr(split, "account_code", None) if split else "")
				or "",
				"split_label": (split.get("label") if isinstance(split, dict) else "")
				or (getattr(split, "label", None) if split else "")
				or "",
				"source_key": f"{POS_SPLIT_KIND}:{inv}",
				"ref_doctype": "Sales Invoice",
				"ref_name": inv,
			}
		)
	sales.sort(key=lambda s: (s.get("time") or "", s["invoice"]))
	total = flt(sum(flt(s["amount"]) for s in sales), 2)
	split_total = flt(sum(flt(s["amount"]) for s in sales if s["split"]), 2)
	return {
		"company": co,
		"day": str(d),
		"mop": want,
		"label": _pos_label(want, d),
		"source_key": _pos_day_key(want, d),
		"sales": sales,
		"total": total,
		"remaining": flt(total - split_total, 2),
		"split_total": split_total,
		"split_count": sum(1 for s in sales if s["split"]),
	}


@frappe.whitelist(allow_guest=True)
@idempotent_request
def split_treasury_pos_sale(
	invoice: str | None = None,
	to_code: str | None = None,
	company: str | None = None,
) -> dict:
	"""Pull one POS sale out of the daily mop row onto another ledger (POS CASH (1))."""
	co = _company(company)
	inv = _as_str(invoice)
	dest = _as_str(to_code).upper()
	if not inv:
		frappe.throw(_("invoice is required"))
	if dest not in CODES:
		frappe.throw(_("to_code must be A or B"))
	day = frappe.db.get_value("Sales Invoice", inv, "posting_date")
	if not day:
		frappe.throw(_("Sales Invoice {0} not found").format(inv))
	found = None
	for sl in _pos_sale_slices(co, getdate(day), getdate(day)):
		if sl["invoice"] == inv:
			found = sl
			break
	if not found:
		frappe.throw(_("No POS payment found on {0}").format(inv))
	key = f"{POS_SPLIT_KIND}:{inv}"
	existing = frappe.db.get_value(
		"Treasury Ledger Entry", {"company": co, "source_key": key, "kind": POS_SPLIT_KIND}, "name"
	)
	old_label = (
		frappe.db.get_value("Treasury Ledger Entry", existing, "label") if existing else ""
	)
	n = _n_from_label(old_label) or _next_split_n(co, found["mop"], dest)
	label = f"{_pos_label(found['mop'], getdate(day))} ({n})"
	payload = {
		"company": co,
		"account_code": dest,
		"posting_date": getdate(day),
		"kind": POS_SPLIT_KIND,
		"amount": found["amount"],
		"label": label,
		"source_key": key,
		"note": found.get("customer_name") or found.get("customer") or "",
	}
	if existing:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Treasury Ledger Entry", existing)
		frappe.flags.ignore_permissions = False
		for k, v in payload.items():
			doc.set(k, v)
		doc.save(ignore_permissions=True)
		name = doc.name
	else:
		doc = frappe.get_doc({"doctype": "Treasury Ledger Entry", **payload})
		doc.insert(ignore_permissions=True)
		name = doc.name
	frappe.db.commit()
	return {"ok": True, "name": name, "source_key": key, "to_code": dest, "label": label, "n": n}


@frappe.whitelist(allow_guest=True)
def unsplit_treasury_pos_sale(invoice: str | None = None, company: str | None = None) -> dict:
	"""Fold a split POS sale back into its daily mop row."""
	co = _company(company)
	inv = _as_str(invoice)
	if not inv:
		frappe.throw(_("invoice is required"))
	key = f"{POS_SPLIT_KIND}:{inv}"
	name = frappe.db.get_value(
		"Treasury Ledger Entry", {"company": co, "source_key": key, "kind": POS_SPLIT_KIND}, "name"
	)
	if name:
		frappe.delete_doc("Treasury Ledger Entry", name, ignore_permissions=True)
		frappe.db.commit()
	return {"ok": True, "source_key": key, "deleted": bool(name)}


def _as_kind_list(raw) -> list[str]:
	if raw is None or raw == "" or str(raw).strip().lower() in ("null", "undefined", "none"):
		return list(AUTO_ORDER_DEFAULT_KINDS)
	if isinstance(raw, (list, tuple)):
		vals = [_as_str(x).lower() for x in raw]
	else:
		s = _as_str(raw)
		if s.startswith("["):
			try:
				import json

				parsed = json.loads(s)
				vals = [_as_str(x).lower() for x in parsed] if isinstance(parsed, list) else [s.lower()]
			except Exception:
				vals = [p.strip().lower() for p in s.split(",") if p.strip()]
		else:
			vals = [p.strip().lower() for p in s.split(",") if p.strip()]
	out = [v for v in vals if v in GANANCIAS_KINDS]
	return out or list(AUTO_ORDER_DEFAULT_KINDS)


def _profit_stats(amounts: list[float]) -> dict:
	income = flt(sum(a for a in amounts if a > 0), 2)
	costs = flt(sum(a for a in amounts if a < 0), 2)
	profit = flt(income + costs, 2)
	pct = flt((profit / income) * 100, 2) if income else None
	return {"income": income, "compras": costs, "ganancias": profit, "pct": pct}


@frappe.whitelist(allow_guest=True)
def preview_treasury_auto_order(
	to_code=None,
	from_date=None,
	to_date=None,
	kinds=None,
	target_pct=None,
	company=None,
):
	"""Draft: which tarjeta/transfer/compra rows to pin on an account to hit a profit %."""
	co = _require_company(company)
	dest = _code(to_code) or "B"
	want = set(_as_kind_list(kinds))
	try:
		aim = flt(target_pct)
	except Exception:
		aim = 10
	if target_pct is None or _as_str(target_pct) == "":
		aim = 10
	led = list_treasury_ledger(from_date=from_date, to_date=to_date, company=co)
	candidates = []
	for side_key in ("a", "b"):
		side = led.get(side_key) or {}
		for r in side.get("rows") or []:
			if r.get("kind") not in want:
				continue
			if r.get("kind") == "movimiento":
				continue
			po = r.get("ref_name") if r.get("kind") == "compra" else None
			has_factura = _po_has_factura(po) if po else None
			candidates.append(
				{
					"source_key": r.get("source_key"),
					"kind": r.get("kind"),
					"label": r.get("label"),
					"date": r.get("date"),
					"amount": flt(r.get("amount"), 2),
					"account": r.get("account"),
					"ref_doctype": r.get("ref_doctype"),
					"ref_name": r.get("ref_name"),
					"has_factura": has_factura,
					"on_target": r.get("account") == dest,
					"selected": True,
				}
			)
	dest_rows = (led.get(dest.lower()) or {}).get("rows") or []
	current = _profit_stats(
		[flt(r.get("amount")) for r in dest_rows if r.get("kind") in GANANCIAS_KINDS]
	)
	incoming = [c for c in candidates if c["account"] != dest and c["selected"]]
	projected = _profit_stats(
		[flt(r.get("amount")) for r in dest_rows if r.get("kind") in GANANCIAS_KINDS]
		+ [c["amount"] for c in incoming]
	)
	return {
		"company": co,
		"to_code": dest,
		"from_date": led.get("from_date"),
		"to_date": led.get("to_date"),
		"kinds": sorted(want),
		"target_pct": aim,
		"candidates": candidates,
		"current": current,
		"projected": projected,
		"gap_pct": flt((projected["pct"] - aim), 2) if projected["pct"] is not None else None,
	}


@frappe.whitelist(allow_guest=True)
@idempotent_request
def apply_treasury_auto_order(to_code=None, source_keys=None, company=None):
	"""Pin the selected auto-order rows onto the target account."""
	dest = _code(to_code) or "B"
	if source_keys is None or source_keys == "" or str(source_keys).strip().lower() in (
		"null",
		"undefined",
	):
		frappe.throw(_("source_keys is required"), frappe.ValidationError)
	if isinstance(source_keys, str):
		try:
			import json

			parsed = json.loads(source_keys) if source_keys.strip().startswith("[") else None
			keys = (
				parsed
				if isinstance(parsed, list)
				else [p.strip() for p in source_keys.split(",") if p.strip()]
			)
		except Exception:
			keys = [p.strip() for p in source_keys.split(",") if p.strip()]
	elif isinstance(source_keys, (list, tuple)):
		keys = [_as_str(k) for k in source_keys if _as_str(k)]
	else:
		keys = []
	keys = [_as_str(k) for k in (keys or []) if _as_str(k)]
	moved = []
	for key in keys:
		if key.startswith("xfer:") or key.startswith("opening:"):
			continue
		res = reassign_treasury_row(source_key=key, to_code=dest, company=company)
		if res and res.get("ok"):
			moved.append(key)
	return {"ok": True, "to_code": dest, "moved": moved, "count": len(moved)}


def _pick_mop(want: str) -> str:
	preferred = {"cash": ("Cash",), "card": ("Card", "Credit Card"), "transfer": ("Transfer", "Bank Draft")}
	names = frappe.get_all("Mode of Payment", pluck="name", ignore_permissions=True) or []
	ranked = []
	for name in names:
		if _mop_kind(name) != want:
			continue
		has_acc = bool(
			frappe.db.get_value("Mode of Payment Account", {"parent": name}, "default_account")
		)
		pref = 0 if name in preferred.get(want, ()) else 1
		ranked.append((0 if has_acc else 1, pref, name))
	ranked.sort()
	return ranked[0][2] if ranked else "Cash"


def _ensure_mop_account(mop: str, company: str):
	if not mop or not frappe.db.exists("Mode of Payment", mop):
		return
	if frappe.db.get_value(
		"Mode of Payment Account", {"parent": mop, "company": company}, "default_account"
	):
		return
	fallback = (
		frappe.db.get_value(
			"Mode of Payment Account", {"parent": "Transfer", "company": company}, "default_account"
		)
		or frappe.db.get_value(
			"Mode of Payment Account", {"parent": "Cash", "company": company}, "default_account"
		)
	)
	if not fallback:
		return
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Mode of Payment", mop)
	frappe.flags.ignore_permissions = False
	doc.append("accounts", {"company": company, "default_account": fallback})
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _ensure_pos_mops(profile: str | None, mops: list[str]):
	if not profile or not frappe.db.exists("POS Profile", profile):
		return
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("POS Profile", profile)
	frappe.flags.ignore_permissions = False
	have = {p.mode_of_payment for p in (doc.payments or [])}
	changed = False
	for mop in mops:
		if mop and mop not in have:
			doc.append("payments", {"mode_of_payment": mop, "default": 0})
			changed = True
	if changed:
		doc.save(ignore_permissions=True)
		frappe.db.commit()


def _ensure_party(doctype: str, label: str) -> str:
	field = "customer_name" if doctype == "Customer" else "supplier_name"
	existing = frappe.db.get_value(doctype, {field: label}, "name")
	if existing:
		return existing
	if frappe.db.exists(doctype, label):
		return label
	payload = {"doctype": doctype, field: label}
	if doctype == "Customer":
		payload.update(
			{
				"customer_type": "Individual",
				"customer_group": frappe.db.get_single_value("Selling Settings", "customer_group")
				or "All Customer Groups",
				"territory": frappe.db.get_single_value("Selling Settings", "territory")
				or "All Territories",
			}
		)
	else:
		payload["supplier_group"] = (
			frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups"
		)
	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _demo_item() -> str:
	if frappe.db.exists("Item", "POS-CAJA"):
		return "POS-CAJA"
	return (
		frappe.db.get_value("Item", {"disabled": 0, "is_stock_item": 0}, "name")
		or frappe.db.get_value("Item", {"disabled": 0}, "name")
	)


def _cancel_delete(doctype: str, name: str):
	if not name or not frappe.db.exists(doctype, name):
		return
	try:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(doctype, name)
		frappe.flags.ignore_permissions = False
		if cint(doc.docstatus) == 1:
			doc.cancel()
		frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
	except Exception:
		try:
			frappe.db.set_value(doctype, name, "docstatus", 2)
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		except Exception:
			frappe.log_error(title=f"treasury demo cleanup {doctype} {name}")


@frappe.whitelist(allow_guest=True)
def seed_treasury_demo(reset=None, company=None):
	"""Idempotent sample pedidos / compras / daily POS closes for Cuentas."""
	co = _require_company(company)
	_ensure_accounts(co)
	do_reset = cint(reset) == 1
	if do_reset:
		for dt, field in (
			("Payment Entry", "remarks"),
			("Sales Invoice", "remarks"),
			("Sales Order", "title"),
			("Purchase Order", "title"),
		):
			if not frappe.db.has_column(dt, field):
				continue
			for name in frappe.get_all(
				dt,
				filters={field: ["like", f"%{DEMO_TAG}%"]},
				pluck="name",
				ignore_permissions=True,
			) or []:
				_cancel_delete(dt, name)
		if _doctype_ready():
			for r in frappe.get_all(
				"Treasury Ledger Entry",
				filters={"kind": POS_SPLIT_KIND},
				fields=["name", "source_key"],
				ignore_permissions=True,
				limit_page_length=5000,
			) or []:
				inv = _as_str(r.source_key).split(":", 1)[-1]
				if inv and not frappe.db.exists("Sales Invoice", inv):
					frappe.delete_doc("Treasury Ledger Entry", r.name, ignore_permissions=True, force=True)
		frappe.db.commit()

	item = _demo_item()
	if not item:
		frappe.throw(_("No item available to seed treasury demo"), frappe.ValidationError)
	warehouse = (
		frappe.db.get_value("POS Profile", {"company": co}, "warehouse")
		or frappe.db.get_value("Warehouse", {"company": co, "is_group": 0}, "name")
	)
	pos_profile = frappe.db.get_value("POS Profile", {"company": co}, "name")
	today = getdate()
	# Five distinct operating days (skip a couple mid-week so they don't look consecutive).
	days = [add_days(today, o) for o in (-8, -6, -4, -2, 0)]
	cash_mop = _pick_mop("cash")
	card_mop = _pick_mop("card")
	xfer_mop = _pick_mop("transfer")
	for mop in (cash_mop, card_mop, xfer_mop):
		_ensure_mop_account(mop, co)
	_ensure_pos_mops(pos_profile, [cash_mop, card_mop, xfer_mop])
	mop_by_kind = {"cash": cash_mop, "card": card_mop, "transfer": xfer_mop}

	walkin = (
		frappe.db.get_value("Customer", "Consumidor Final", "name")
		or frappe.db.get_value("Customer", {"customer_name": "Consumidor Final"}, "name")
		or frappe.db.get_value("Customer", "POSNET-WALK-IN", "name")
		or _ensure_party("Customer", "Consumidor Final")
	)
	cust_cash = _ensure_party("Customer", "CUENTAS Demo Norte")
	cust_card = _ensure_party("Customer", "CUENTAS Demo Sur")
	cust_xfer = _ensure_party("Customer", "CUENTAS Demo Centro")
	supplier = _ensure_party("Supplier", "CUENTAS Demo Proveedor")
	po_item = (
		frappe.db.get_value("Item", {"disabled": 0, "is_purchase_item": 1}, "name")
		or item
	)

	created = {"pedidos": [], "compras": [], "cierres": []}

	def _already(doctype: str, tag: str) -> str | None:
		for field in ("title", "remarks"):
			if not frappe.db.has_column(doctype, field):
				continue
			name = frappe.db.get_value(doctype, {field: ["like", f"%{tag}%"]}, "name")
			if name:
				return name
		return None

	def _pay_si(si_name: str, tag: str, day):
		outst = flt(frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount"))
		if outst <= PAID_EPS:
			return
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry("Sales Invoice", si_name)
		pe.mode_of_payment = cash_mop
		pe.paid_amount = outst
		pe.received_amount = outst
		pe.reference_no = tag or si_name
		pe.reference_date = day
		if pe.references:
			pe.references[0].allocated_amount = outst
		pe.insert(ignore_permissions=True)
		pe.submit()
		frappe.db.commit()

	def _make_so(customer: str, mop: str, amount: float, day, tag: str):
		existing = _already("Sales Order", tag)
		if existing and flt(frappe.db.get_value("Sales Order", existing, "per_billed")) >= 99:
			for si_name in frappe.get_all(
				"Sales Invoice Item",
				filters={"sales_order": existing, "docstatus": 1},
				pluck="parent",
				ignore_permissions=True,
			) or []:
				_pay_si(si_name, tag, day)
			created["pedidos"].append(existing)
			if _mop_kind(mop) != "cash":
				reassign_treasury_row(source_key=f"so:{existing}:cash", to_code="B", company=co)
			return existing
		if existing:
			frappe.flags.ignore_permissions = True
			so = frappe.get_doc("Sales Order", existing)
			frappe.flags.ignore_permissions = False
		else:
			so = frappe.get_doc(
				{
					"doctype": "Sales Order",
					"customer": customer,
					"company": co,
					"transaction_date": day,
					"delivery_date": day,
					"title": tag,
					"items": [
						{
							"item_code": item,
							"qty": 1,
							"rate": amount,
							"warehouse": warehouse,
							"delivery_date": day,
						}
					],
				}
			)
			so.insert(ignore_permissions=True)
			so.submit()
			frappe.db.set_value("Sales Order", so.name, "title", tag)
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": customer,
				"company": co,
				"is_pos": 0,
				"set_posting_time": 1,
				"posting_date": day,
				"due_date": day,
				"update_stock": 0,
				"remarks": tag,
				"items": [
					{
						"item_code": item,
						"qty": 1,
						"rate": amount,
						"warehouse": warehouse,
						"sales_order": so.name,
						"so_detail": so.items[0].name,
					}
				],
			}
		)
		si.insert(ignore_permissions=True)
		si.submit()
		_pay_si(si.name, tag, day)
		created["pedidos"].append(so.name)
		if _mop_kind(mop) != "cash":
			reassign_treasury_row(source_key=f"so:{so.name}:cash", to_code="B", company=co)
		return so.name

	def _make_po(mop: str, amount: float, day, tag: str):
		from erpnext.erpnext_integrations.ecommerce_api.buying_api import record_purchase_order_payment

		existing = _already("Purchase Order", tag)
		if not existing:
			for r in frappe.get_all(
				"Purchase Order",
				filters={"supplier": supplier, "transaction_date": day, "docstatus": 1},
				fields=["name", "grand_total"],
				ignore_permissions=True,
			) or []:
				if abs(flt(r.grand_total) - amount) < 1:
					existing = r.name
					break
		if existing:
			created["compras"].append(existing)
			return existing
		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": supplier,
				"company": co,
				"transaction_date": day,
				"schedule_date": day,
				"status": "Draft",
				"title": "CUENTAS Demo Proveedor",
				"remarks": tag,
				"items": [
					{
						"item_code": po_item,
						"qty": 1,
						"rate": amount,
						"schedule_date": day,
					}
				],
			}
		)
		po.insert(ignore_permissions=True)
		po.submit()
		if frappe.db.has_column("Purchase Order", "title"):
			frappe.db.set_value("Purchase Order", po.name, "title", tag)
		frappe.db.commit()
		record_purchase_order_payment(
			name=po.name, paid_amount=flt(po.grand_total) or amount, mode_of_payment=mop, posting_date=str(day)
		)
		created["compras"].append(po.name)
		return po.name

	def _make_pos_ticket(mop: str, amount: float, day, tag: str, posting_time: str = "12:00:00"):
		existing = _already("Sales Invoice", tag)
		if existing:
			created["cierres"].append(existing)
			return existing
		qty = 2 if flt(amount) >= 8000 else 1
		rate = flt(amount) / qty
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": walkin,
				"company": co,
				"is_pos": 1,
				"pos_profile": pos_profile,
				"set_posting_time": 1,
				"posting_date": day,
				"posting_time": posting_time,
				"due_date": day,
				"update_stock": 0,
				"remarks": f"{tag} | pos_session:cuentas-demo",
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"rate": rate,
						"warehouse": warehouse,
					}
				],
			}
		)
		si.append("payments", {"mode_of_payment": mop, "amount": amount})
		si.insert(ignore_permissions=True)
		si.reload()
		if si.payments:
			si.payments[0].amount = abs(flt(si.grand_total)) or amount
			si.save(ignore_permissions=True)
		si.submit()
		frappe.db.commit()
		created["cierres"].append(si.name)
		return si.name

	errors = []

	def _try(label: str, fn):
		try:
			fn()
		except Exception as exc:
			frappe.log_error(title=f"treasury demo {label}")
			errors.append(f"{label}: {exc}")

	# Pedidos: cash → A, card/transfer → B
	_try("so-cash", lambda: _make_so(cust_cash, cash_mop, 18500, days[0], f"{DEMO_TAG}:so:cash"))
	_try("so-card", lambda: _make_so(cust_card, card_mop, 24200, days[1], f"{DEMO_TAG}:so:card"))
	_try("so-xfer", lambda: _make_so(cust_xfer, xfer_mop, 9800, days[2], f"{DEMO_TAG}:so:transfer"))
	# Compras
	_try("po-cash", lambda: _make_po(cash_mop, 7200, days[1], f"{DEMO_TAG}:po:cash"))
	_try("po-xfer", lambda: _make_po(xfer_mop, 15400, days[2], f"{DEMO_TAG}:po:transfer"))
	# Walk-in Consumidor Final tickets across 5 dates (usual-day mix).
	pos_scripts = (
		# Quiet weekday — morning coffees, a couple cards, one transfer
		(
			("cash", 650, "09:12:00"),
			("cash", 890, "09:28:00"),
			("cash", 1200, "10:05:00"),
			("cash", 1750, "10:41:00"),
			("card", 3200, "11:18:00"),
			("cash", 980, "12:02:00"),
			("card", 5600, "13:10:00"),
			("cash", 2400, "15:22:00"),
			("transfer", 4100, "16:05:00"),
			("cash", 750, "17:40:00"),
			("card", 2100, "18:15:00"),
			("cash", 1450, "19:02:00"),
		),
		# Normal weekday
		(
			("cash", 720, "08:55:00"),
			("cash", 540, "09:08:00"),
			("cash", 1350, "09:33:00"),
			("card", 2800, "10:12:00"),
			("cash", 960, "10:47:00"),
			("cash", 1880, "11:21:00"),
			("card", 6400, "12:35:00"),
			("cash", 2100, "13:04:00"),
			("transfer", 3500, "13:48:00"),
			("cash", 830, "15:11:00"),
			("card", 4200, "16:20:00"),
			("cash", 2600, "17:05:00"),
			("transfer", 1900, "17:42:00"),
			("cash", 1100, "18:28:00"),
			("card", 7800, "19:15:00"),
		),
		# Midweek rush
		(
			("cash", 480, "08:40:00"),
			("cash", 910, "09:02:00"),
			("cash", 1600, "09:25:00"),
			("card", 2450, "09:58:00"),
			("cash", 700, "10:30:00"),
			("cash", 2150, "11:08:00"),
			("card", 5100, "12:18:00"),
			("cash", 3400, "12:50:00"),
			("transfer", 6200, "13:22:00"),
			("cash", 1250, "14:40:00"),
			("card", 8900, "15:55:00"),
			("cash", 680, "16:33:00"),
			("cash", 1980, "17:10:00"),
			("card", 3600, "18:02:00"),
			("transfer", 2700, "18:41:00"),
			("cash", 1540, "19:20:00"),
		),
		# Friday — busier, more card / MP
		(
			("cash", 590, "08:50:00"),
			("cash", 840, "09:06:00"),
			("cash", 1120, "09:19:00"),
			("card", 1900, "09:44:00"),
			("cash", 2050, "10:15:00"),
			("card", 4300, "10:52:00"),
			("cash", 770, "11:28:00"),
			("cash", 3100, "12:08:00"),
			("card", 7200, "12:41:00"),
			("transfer", 5400, "13:15:00"),
			("cash", 1680, "14:02:00"),
			("card", 2650, "15:18:00"),
			("cash", 920, "16:00:00"),
			("transfer", 8100, "16:37:00"),
			("cash", 2480, "17:21:00"),
			("card", 9500, "18:08:00"),
			("cash", 1340, "18:55:00"),
			("card", 4100, "19:33:00"),
		),
		# Today — peak walk-in traffic
		(
			("cash", 610, "08:35:00"),
			("cash", 450, "08:48:00"),
			("cash", 980, "09:04:00"),
			("cash", 1420, "09:22:00"),
			("card", 2300, "09:51:00"),
			("cash", 870, "10:14:00"),
			("cash", 1950, "10:40:00"),
			("card", 3800, "11:12:00"),
			("cash", 2600, "11:47:00"),
			("card", 6700, "12:25:00"),
			("cash", 1540, "12:58:00"),
			("transfer", 4800, "13:20:00"),
			("cash", 720, "14:05:00"),
			("card", 3150, "14:42:00"),
			("cash", 2100, "15:16:00"),
			("transfer", 9200, "15:50:00"),
			("cash", 1080, "16:28:00"),
			("card", 5400, "17:04:00"),
			("cash", 2860, "17:39:00"),
			("card", 11200, "18:22:00"),
			("cash", 990, "18:58:00"),
			("transfer", 3600, "19:25:00"),
			("cash", 1750, "19:48:00"),
		),
	)
	for day, tickets in zip(days, pos_scripts):
		for seq, (kind, amount, hhmm) in enumerate(tickets, start=1):
			mop = mop_by_kind.get(kind) or cash_mop
			tag = f"{DEMO_TAG}:pos:{kind}:{day}:{seq:02d}"
			_try(
				f"pos-{kind}-{day}-{seq}",
				lambda mop=mop, amount=amount, day=day, tag=tag, hhmm=hhmm: _make_pos_ticket(
					mop, amount, day, tag, hhmm
				),
			)

	frappe.db.commit()
	led = list_treasury_ledger(from_date=str(days[0]), to_date=str(today), company=co)
	pos_keys = {
		r.get("source_key")
		for side in ("a", "b")
		for r in ((led.get(side) or {}).get("rows") or [])
		if _as_str(r.get("source_key")).startswith("pos:")
	}
	return {
		"ok": True,
		"company": co,
		"created": created,
		"from_date": str(days[0]),
		"to_date": str(today),
		"a_rows": len((led.get("a") or {}).get("rows") or []),
		"b_rows": len((led.get("b") or {}).get("rows") or []),
		"ganancias": led.get("ganancias"),
		"pos_days": len(pos_keys),
		"pos_tickets": len(created["cierres"]),
		"errors": errors,
	}
