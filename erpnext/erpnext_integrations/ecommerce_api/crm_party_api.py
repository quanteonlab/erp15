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


@frappe.whitelist(allow_guest=True)
def get_party_invoice_detail(invoice_name=None):
	"""Sales Invoice header + items for credit-note picker (ignore_permissions)."""
	name = _as_str(invoice_name)
	if not name:
		frappe.throw(_("invoice_name is required"))
	if not frappe.db.exists("Sales Invoice", name):
		frappe.throw(_("Sales Invoice {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	try:
		doc = frappe.get_doc("Sales Invoice", name)
	finally:
		frappe.flags.ignore_permissions = False

	if cint(doc.is_return):
		frappe.throw(_("Cannot credit against another credit note"))
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Invoice must be submitted"))

	items = []
	for row in doc.items or []:
		qty = abs(flt(row.qty))
		if qty <= 0:
			continue
		items.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name or row.item_code,
				"qty": qty,
				"rate": flt(row.rate),
				"amount": flt(row.amount),
				"uom": row.uom,
				"warehouse": row.warehouse,
			}
		)

	return {
		"ok": True,
		"invoice": {
			"name": doc.name,
			"posting_date": str(doc.posting_date) if doc.posting_date else None,
			"grand_total": flt(doc.grand_total),
			"outstanding_amount": flt(doc.outstanding_amount),
			"customer": doc.customer,
			"customer_name": doc.customer_name,
			"status": doc.status,
			"currency": doc.currency,
			"company": doc.company,
		},
		"items": items,
	}


@frappe.whitelist(allow_guest=True)
def search_party_invoices(
	party_type=None,
	party=None,
	search_term=None,
	is_return=0,
	page_length=20,
):
	"""Autocomplete Sales/Purchase invoices for a party (name / remarks / date)."""
	ptype = _norm_party_type(party_type)
	party = _require_party(ptype, party)
	is_ret = 1 if cint(is_return) else 0
	page_length = _as_int(page_length, 20, lo=1, hi=50)
	term = _as_str(search_term)

	doctype = "Sales Invoice" if ptype == "Customer" else "Purchase Invoice"
	party_field = "customer" if ptype == "Customer" else "supplier"
	filters = {party_field: party, "docstatus": 1, "is_return": is_ret}

	or_filters = None
	if term:
		or_filters = [
			["name", "like", f"%{term}%"],
			["remarks", "like", f"%{term}%"],
		]

	fields = [
		"name",
		"posting_date",
		"grand_total",
		"outstanding_amount",
		"status",
		"is_return",
		"return_against",
		"currency",
		party_field,
		f"{party_field}_name",
	]
	rows = frappe.get_all(
		doctype,
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	return {
		"ok": True,
		"doctype": doctype,
		"rows": [_serialize_invoice(r) for r in rows],
	}


@frappe.whitelist(allow_guest=True)
def create_party_credit_note(
	customer=None,
	return_against=None,
	items=None,
	reason=None,
	reason_code=None,
	company=None,
):
	"""Create a Sales Invoice credit note (is_return), optionally linked to an invoice.

	``items``: [{item_code, qty, rate?, item_name?}] with positive qty to credit.
	When ``return_against`` is set, uses ERPNext return mapper then keeps only selected lines.
	"""
	import json

	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	if isinstance(items, str):
		try:
			items = json.loads(items) if items.strip() else []
		except Exception:
			items = []
	if items is None:
		items = []
	if not isinstance(items, list):
		frappe.throw(_("items must be a list"))

	cust = _as_str(customer)
	against = _as_str(return_against)
	reason_text = _as_str(reason)
	reason_key = _as_str(reason_code) or "other"

	clean = []
	for raw in items:
		if not isinstance(raw, dict):
			continue
		code_item = _as_str(raw.get("item_code"))
		qty = abs(flt(raw.get("qty")))
		if not code_item or qty <= 0:
			continue
		if not frappe.db.exists("Item", code_item):
			frappe.throw(_("Item {0} not found").format(code_item))
		clean.append(
			{
				"item_code": code_item,
				"item_name": _as_str(raw.get("item_name")) or code_item,
				"qty": qty,
				"rate": abs(flt(raw.get("rate"))),
			}
		)
	if not clean:
		frappe.throw(_("Select at least one item to credit"))

	company = resolve_company(company)

	from erpnext.erpnext_integrations.ecommerce_api.api import _temporarily_allow_negative_stock

	if against:
		if not frappe.db.exists("Sales Invoice", against):
			frappe.throw(_("Invoice {0} not found").format(against))
		src_customer = frappe.db.get_value("Sales Invoice", against, "customer")
		if cust and src_customer and cust != src_customer:
			frappe.throw(_("Customer does not match invoice {0}").format(against))
		cust = cust or src_customer

		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		wanted = {r["item_code"]: r for r in clean}
		doc = make_return_doc("Sales Invoice", against)
		doc.customer = cust or doc.customer
		kept = []
		for row in list(doc.items or []):
			spec = wanted.get(row.item_code)
			if not spec:
				continue
			max_q = abs(flt(row.qty))
			q = min(spec["qty"], max_q) if max_q else spec["qty"]
			if q <= 0:
				continue
			row.qty = -q
			if spec.get("rate"):
				row.rate = flt(spec["rate"])
			kept.append(row)
		if not kept:
			frappe.throw(_("No matching invoice lines for selected items"))
		doc.set("items", [])
		for row in kept:
			doc.append(
				"items",
				{
					"item_code": row.item_code,
					"item_name": row.item_name,
					"qty": row.qty,
					"rate": row.rate,
					"uom": row.uom,
					"stock_uom": row.stock_uom,
					"conversion_factor": row.conversion_factor,
					"warehouse": row.warehouse,
					"income_account": row.income_account,
					"cost_center": row.cost_center,
					"sales_order": getattr(row, "sales_order", None),
					"so_detail": getattr(row, "so_detail", None),
					"delivery_note": getattr(row, "delivery_note", None),
					"dn_detail": getattr(row, "dn_detail", None),
				},
			)
	else:
		if not cust:
			frappe.throw(_("customer is required when no invoice is linked"))
		if not frappe.db.exists("Customer", cust):
			frappe.throw(_("Customer {0} not found").format(cust))

		from erpnext.erpnext_integrations.ecommerce_api.api import (
			_ensure_pos_caja_item,
			_ensure_pos_sale_item_groups,
			_ensure_pos_sale_items_enabled,
			_normalize_pos_caja_item_code,
		)

		_ensure_pos_caja_item()
		for r in clean:
			r["item_code"] = _normalize_pos_caja_item_code(r["item_code"])
		codes = [r["item_code"] for r in clean]
		_ensure_pos_sale_items_enabled(codes)
		_ensure_pos_sale_item_groups(codes)

		invoice_items = [
			{
				"item_code": r["item_code"],
				"item_name": r["item_name"],
				"qty": -r["qty"],
				"rate": r["rate"],
			}
			for r in clean
		]
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": cust,
				"company": company,
				"is_pos": 0,
				"is_return": 1,
				"posting_date": frappe.utils.nowdate(),
				"due_date": frappe.utils.nowdate(),
				"items": invoice_items,
			}
		)

	tag_parts = [f"credit_reason:{reason_key}"]
	if reason_text:
		tag_parts.append(f"note:{reason_text[:200]}")
	existing_remarks = (doc.remarks or "").strip()
	doc.remarks = (existing_remarks + " | " if existing_remarks else "") + " | ".join(tag_parts)

	doc.set_missing_values()
	doc.calculate_taxes_and_totals()
	doc.insert(ignore_permissions=True)
	with _temporarily_allow_negative_stock():
		doc.submit()
	frappe.db.commit()

	return {
		"ok": True,
		"invoice_id": doc.name,
		"return_against": against or None,
		"grand_total": flt(doc.grand_total),
		"customer": doc.customer,
		"reason_code": reason_key,
	}
