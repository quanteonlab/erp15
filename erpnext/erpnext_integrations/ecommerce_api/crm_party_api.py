"""CRM party document lists — Sales/Purchase Invoices, payments, product aggregates.

Thin whitelist wrappers over core ERPNext DocTypes so Tables → CRM detail tabs
can show Excel-like DataTables without inventing parallel ledgers.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt


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


def _norm_party_type(party_type) -> str:
	raw = _as_str(party_type).lower()
	if raw in ("customer", "client", "cliente", "clients"):
		return "Customer"
	if raw in ("supplier", "proveedor", "proveedores", "vendor"):
		return "Supplier"
	frappe.throw(_("party_type must be Customer or Supplier"))


def _require_party(party_type: str, party: str) -> str:
	party = _as_str(party)
	if not party:
		frappe.throw(_("party is required"))
	if not frappe.db.exists(party_type, party):
		frappe.throw(_("{0} {1} not found").format(party_type, party))
	return party


def _serialize_invoice(row) -> dict:
	return {
		"name": row.name,
		"posting_date": str(row.posting_date) if row.posting_date else None,
		"due_date": str(row.due_date) if getattr(row, "due_date", None) else None,
		"grand_total": flt(row.grand_total),
		"outstanding_amount": flt(row.outstanding_amount),
		"status": row.status,
		"docstatus": cint(row.docstatus),
		"is_return": cint(getattr(row, "is_return", 0)),
		"return_against": getattr(row, "return_against", None) or None,
		"currency": getattr(row, "currency", None),
		"remarks": (row.remarks or "") if getattr(row, "remarks", None) else "",
		"party": getattr(row, "customer", None) or getattr(row, "supplier", None),
		"party_name": getattr(row, "customer_name", None)
		or getattr(row, "supplier_name", None)
		or getattr(row, "title", None),
	}


@frappe.whitelist(allow_guest=True)
def list_party_invoices(
	party_type=None,
	party=None,
	is_return=0,
	start=0,
	page_length=100,
):
	"""List Sales Invoice (Customer) or Purchase Invoice (Supplier).

	`is_return=1` → credit notes (SI) / debit notes (PI).
	"""
	ptype = _norm_party_type(party_type)
	party = _require_party(ptype, party)
	is_ret = 1 if cint(is_return) else 0
	start = _as_int(start, 0, lo=0, hi=100000)
	page_length = _as_int(page_length, 100, lo=1, hi=300)

	if ptype == "Customer":
		doctype = "Sales Invoice"
		filters = {"customer": party, "docstatus": ["<", 2], "is_return": is_ret}
		fields = [
			"name",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"status",
			"docstatus",
			"is_return",
			"return_against",
			"currency",
			"remarks",
			"customer",
			"customer_name",
		]
	else:
		doctype = "Purchase Invoice"
		filters = {"supplier": party, "docstatus": ["<", 2], "is_return": is_ret}
		fields = [
			"name",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"status",
			"docstatus",
			"is_return",
			"return_against",
			"currency",
			"remarks",
			"supplier",
			"supplier_name",
		]

	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit_start=start,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = frappe.db.count(doctype, filters)
	return {
		"ok": True,
		"doctype": doctype,
		"party_type": ptype,
		"party": party,
		"is_return": is_ret,
		"total": cint(total),
		"rows": [_serialize_invoice(r) for r in rows],
	}


@frappe.whitelist(allow_guest=True)
def list_party_payments(party_type=None, party=None, start=0, page_length=100):
	"""Payment Entry history for a Customer (Receive) or Supplier (Pay)."""
	ptype = _norm_party_type(party_type)
	party = _require_party(ptype, party)
	start = _as_int(start, 0, lo=0, hi=100000)
	page_length = _as_int(page_length, 100, lo=1, hi=300)

	filters = {
		"party_type": ptype,
		"party": party,
		"docstatus": ["<", 2],
	}
	rows = frappe.get_all(
		"Payment Entry",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"payment_type",
			"mode_of_payment",
			"paid_amount",
			"received_amount",
			"status",
			"docstatus",
			"remarks",
			"party",
			"party_name",
			"reference_no",
			"reference_date",
		],
		order_by="posting_date desc, creation desc",
		limit_start=start,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = frappe.db.count("Payment Entry", filters)

	out = []
	for r in rows:
		amount = flt(r.paid_amount) if r.payment_type == "Pay" else flt(r.received_amount or r.paid_amount)
		out.append(
			{
				"name": r.name,
				"posting_date": str(r.posting_date) if r.posting_date else None,
				"payment_type": r.payment_type,
				"mode_of_payment": r.mode_of_payment,
				"amount": amount,
				"status": r.status,
				"docstatus": cint(r.docstatus),
				"remarks": r.remarks or "",
				"party": r.party,
				"party_name": r.party_name,
				"reference_no": r.reference_no,
				"reference_date": str(r.reference_date) if r.reference_date else None,
			}
		)
	return {
		"ok": True,
		"party_type": ptype,
		"party": party,
		"total": cint(total),
		"rows": out,
	}


@frappe.whitelist(allow_guest=True)
def list_party_products(party_type=None, party=None, page_length=200):
	"""Aggregate items bought from (Customer SI) or purchased to (Supplier PI).

	Also returns Supplier Quotation headers as `offers` when party is Supplier.
	"""
	ptype = _norm_party_type(party_type)
	party = _require_party(ptype, party)
	page_length = _as_int(page_length, 200, lo=1, hi=500)

	if ptype == "Customer":
		sql = """
			SELECT
				sii.item_code AS item_code,
				MAX(sii.item_name) AS item_name,
				SUM(sii.qty) AS qty_total,
				SUM(sii.amount) AS amount_total,
				COUNT(DISTINCT si.name) AS invoice_count,
				MAX(si.posting_date) AS last_date,
				SUBSTRING_INDEX(
					GROUP_CONCAT(sii.rate ORDER BY si.posting_date DESC, si.creation DESC),
					',', 1
				) AS last_rate
			FROM `tabSales Invoice Item` sii
			INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE si.docstatus = 1
			  AND IFNULL(si.is_return, 0) = 0
			  AND si.customer = %(party)s
			GROUP BY sii.item_code
			ORDER BY amount_total DESC
			LIMIT %(lim)s
		"""
	else:
		sql = """
			SELECT
				pii.item_code AS item_code,
				MAX(pii.item_name) AS item_name,
				SUM(pii.qty) AS qty_total,
				SUM(pii.amount) AS amount_total,
				COUNT(DISTINCT pi.name) AS invoice_count,
				MAX(pi.posting_date) AS last_date,
				SUBSTRING_INDEX(
					GROUP_CONCAT(pii.rate ORDER BY pi.posting_date DESC, pi.creation DESC),
					',', 1
				) AS last_rate
			FROM `tabPurchase Invoice Item` pii
			INNER JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
			WHERE pi.docstatus = 1
			  AND IFNULL(pi.is_return, 0) = 0
			  AND pi.supplier = %(party)s
			GROUP BY pii.item_code
			ORDER BY amount_total DESC
			LIMIT %(lim)s
		"""

	rows = frappe.db.sql(sql, {"party": party, "lim": page_length}, as_dict=True)
	products = [
		{
			"item_code": r.item_code,
			"item_name": r.item_name,
			"qty_total": flt(r.qty_total),
			"amount_total": flt(r.amount_total),
			"invoice_count": cint(r.invoice_count),
			"last_date": str(r.last_date) if r.last_date else None,
			"last_rate": flt(r.last_rate),
			"id": f"{party}::{r.item_code}",
		}
		for r in rows
		if r.item_code
	]

	offers = []
	if ptype == "Supplier" and frappe.db.exists("DocType", "Supplier Quotation"):
		qrows = frappe.get_all(
			"Supplier Quotation",
			filters={"supplier": party, "docstatus": ["<", 2]},
			fields=[
				"name",
				"transaction_date",
				"valid_till",
				"grand_total",
				"status",
				"docstatus",
				"currency",
			],
			order_by="transaction_date desc, creation desc",
			limit_page_length=page_length,
			ignore_permissions=True,
		)
		offers = [
			{
				"name": q.name,
				"transaction_date": str(q.transaction_date) if q.transaction_date else None,
				"valid_till": str(q.valid_till) if q.valid_till else None,
				"grand_total": flt(q.grand_total),
				"status": q.status,
				"docstatus": cint(q.docstatus),
				"currency": q.currency,
			}
			for q in qrows
		]

	return {
		"ok": True,
		"party_type": ptype,
		"party": party,
		"products": products,
		"offers": offers,
	}


@frappe.whitelist(allow_guest=True)
def update_party_doc_remarks(doctype=None, name=None, remarks=None):
	"""Update remarks on SI / PI / Payment Entry (safe after-submit field)."""
	doctype = _as_str(doctype)
	name = _as_str(name)
	if doctype not in ("Sales Invoice", "Purchase Invoice", "Payment Entry"):
		frappe.throw(_("Unsupported doctype"))
	if not name or not frappe.db.exists(doctype, name):
		frappe.throw(_("Document not found"))
	text = "" if remarks is None else str(remarks)
	# Coerce dirty payloads ("null", lists) to empty string rather than 500.
	if isinstance(remarks, (list, dict)):
		text = ""
	frappe.db.set_value(doctype, name, "remarks", text, update_modified=True)
	frappe.db.commit()
	return {"ok": True, "doctype": doctype, "name": name, "remarks": text}
