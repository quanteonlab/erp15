# Copyright (c) 2026, SilkOS and contributors
"""Public ops kiosk APIs for /check (showcase) and /armado (pick sheet).

PIN gating is enforced in the Next.js UI (Admin PIN). These endpoints are
allow_guest so kiosk tablets can call through the Next proxy without a
staff login session — same pattern as catalog.
"""

from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today


# Internal EAN-13 prefix for armado sheets. Retail product barcodes must not use
# 290… (in-store / internal range). Lookup on /armado prefers this prefix.
ARMADO_EAN13_PREFIX = "290"


def _ean13_check_digit(body12: str) -> str:
	odds = sum(int(body12[i]) for i in range(0, 12, 2))
	evens = sum(int(body12[i]) for i in range(1, 12, 2))
	return str((10 - ((odds + evens * 3) % 10)) % 10)


def armado_ean13_for_order(so_name: str) -> str:
	"""Deterministic EAN-13 for a Sales Order — never collides with product barcodes (290…)."""
	digest = hashlib.sha1((so_name or "").encode("utf-8")).hexdigest()
	n = int(digest[:12], 16) % (10**9)
	body = f"{ARMADO_EAN13_PREFIX}{n:09d}"[:12]
	return body + _ean13_check_digit(body)


def is_armado_ean13(code: str) -> bool:
	raw = str(code or "").strip()
	return len(raw) == 13 and raw.isdigit() and raw.startswith(ARMADO_EAN13_PREFIX)


def _guest_tag_field():
	from erpnext.erpnext_integrations.ecommerce_api.api import _guest_preorder_tag_fieldname

	return _guest_preorder_tag_fieldname()


def _require_pin(pin: str | None) -> None:
	"""Optional hardening for writes — validate POS admin PIN when provided."""
	pin = str(pin or "").strip()
	if not pin:
		return
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import validate_admin_pin

	res = validate_admin_pin(pin)
	if not (isinstance(res, dict) and res.get("authorized")):
		frappe.throw(_("Incorrect admin PIN"), frappe.AuthenticationError)


@frappe.whitelist(allow_guest=True)
def resolve_armado_ean13(code=None):
	"""Map a scanned armado EAN-13 → Sales Order name (exact match among recent orders)."""
	code = str(code or "").strip()
	if not is_armado_ean13(code):
		return {"ok": False, "match": None}

	# Search today's + last 14 days guest SOs; compare deterministic codes.
	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": ["<", 2],
			"transaction_date": [">=", frappe.utils.add_days(today(), -14)],
		},
		fields=["name"],
		order_by="modified desc",
		limit_page_length=500,
		ignore_permissions=True,
	)
	for row in rows:
		if armado_ean13_for_order(row.name) == code:
			return {"ok": True, "match": row.name, "ean13": code}
	return {"ok": False, "match": None, "ean13": code}


@frappe.whitelist(allow_guest=True)
def list_armado_orders(delivery_date=None, limit=80):
	"""Orders due on delivery_date (default today) for the armado kiosk."""
	delivery_date = getdate(delivery_date or today())
	limit = max(1, min(cint(limit) or 80, 200))
	tag_field = _guest_tag_field()

	frappe.flags.ignore_permissions = True
	filters = {
		"docstatus": ["<", 2],
		"delivery_date": delivery_date,
		"status": ["not in", ["Closed", "Completed"]],
	}
	fields = [
		"name",
		"customer",
		"customer_name",
		"delivery_date",
		"transaction_date",
		"grand_total",
		"currency",
		"docstatus",
		"status",
		"modified",
	]
	if tag_field:
		fields.append(tag_field)

	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=fields,
		order_by="customer_name asc, name asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)

	# Prefer guest preorders when tag exists; still include non-guest due today.
	out = []
	for r in rows:
		tag = str(r.get(tag_field) or "") if tag_field else ""
		is_guest = "guest_preorder=1" in tag
		ean13 = armado_ean13_for_order(r.name)
		out.append(
			{
				"name": r.name,
				"customer": r.customer,
				"customer_name": r.customer_name or r.customer,
				"delivery_date": str(r.delivery_date or ""),
				"transaction_date": str(r.transaction_date or ""),
				"grand_total": flt(r.grand_total),
				"currency": r.currency,
				"docstatus": cint(r.docstatus),
				"status": r.status,
				"is_guest": is_guest,
				"ean13": ean13,
				"items_count": frappe.db.count("Sales Order Item", {"parent": r.name}),
			}
		)
	return {"ok": True, "delivery_date": str(delivery_date), "orders": out}


@frappe.whitelist(allow_guest=True)
def get_armado_order(preorder_name=None):
	"""Full armado sheet for one Sales Order (items + weights + notes + ean13)."""
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Order not found"))

	from erpnext.erpnext_integrations.ecommerce_api.api import get_guest_preorder
	from erpnext.erpnext_integrations.ecommerce_api.print_templates_api import _floor_sku_locations

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	detail = None
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _is_guest_preorder_sales_order

		if _is_guest_preorder_sales_order(so):
			detail = get_guest_preorder(preorder_name)
	except Exception:
		detail = None

	if not detail:
		# Non-guest SO — serialize similarly
		from erpnext.erpnext_integrations.ecommerce_api.api import _item_line_weight_fields

		detail = {
			"name": so.name,
			"customer": so.customer,
			"customer_name": so.customer_name or so.customer,
			"delivery_date": str(so.delivery_date or ""),
			"transaction_date": str(so.transaction_date or ""),
			"docstatus": cint(so.docstatus),
			"status": so.status,
			"estimated_total": flt(so.grand_total),
			"currency": so.currency,
			"guest_notes": getattr(so, "remarks", None) or getattr(so, "terms", None) or "",
			"items": [
				{
					"item_code": d.item_code,
					"item_name": d.item_name,
					"qty": flt(d.qty),
					"rate": flt(d.rate),
					"amount": flt(d.amount),
					**_item_line_weight_fields(
						d.item_code,
						line_uom=getattr(d, "uom", None),
						line_weight_per_unit=getattr(d, "weight_per_unit", None),
						line_total_weight=getattr(d, "total_weight", None),
						line_qty=flt(d.qty),
					),
				}
				for d in (so.items or [])
			],
		}

	sku_to_loc, _ = _floor_sku_locations(company=so.company)
	for it in detail.get("items") or []:
		loc = (sku_to_loc.get(it.get("item_code")) or {}).get("location") or ""
		it["location"] = loc

	detail["ean13"] = armado_ean13_for_order(so.name)
	detail["armado_notes"] = _load_armado_notes(so.name)
	return {"ok": True, "order": detail}


def _armado_notes_scope(so_name: str) -> str:
	return f"ops.armado.notes::{so_name}"


def _load_armado_notes(so_name: str) -> str:
	scope = _armado_notes_scope(so_name)
	if not frappe.db.exists("Table Extra Schema", scope):
		return ""
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", scope)
	data = {}
	try:
		data = json.loads(doc.columns_json or "{}")
	except Exception:
		data = {}
	return str((data or {}).get("notes") or "")


def _save_armado_notes(so_name: str, notes: str) -> None:
	scope = _armado_notes_scope(so_name)
	payload = json.dumps({"notes": notes or ""}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", scope):
		doc = frappe.get_doc("Table Extra Schema", scope)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": scope, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


@frappe.whitelist(allow_guest=True)
def save_armado_notes(preorder_name=None, notes=None, pin=None):
	_require_pin(pin)
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Order not found"))
	_save_armado_notes(preorder_name, str(notes or ""))
	return {"ok": True, "notes": str(notes or "")}


@frappe.whitelist(allow_guest=True)
def update_armado_items(preorder_name=None, items=None, pin=None):
	"""Update qty/weight on armado lines (reuses guest preorder item update when possible)."""
	_require_pin(pin)
	preorder_name = str(preorder_name or "").strip()
	if isinstance(items, str):
		items = json.loads(items)
	if not isinstance(items, list) or not items:
		frappe.throw(_("Items required"))

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_is_guest_preorder_sales_order,
		update_guest_preorder_items,
	)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	if _is_guest_preorder_sales_order(so):
		detail = update_guest_preorder_items(preorder_name, items)
		return {"ok": True, "order": {**detail, "ean13": armado_ean13_for_order(detail.get("name") or preorder_name)}}

	# Non-guest: edit draft in place only
	if cint(so.docstatus) != 0:
		frappe.throw(_("Only draft orders can be edited here"))
	so.set("items", [])
	for row in items:
		so.append(
			"items",
			{
				"item_code": row.get("item_code"),
				"qty": flt(row.get("qty")),
				"rate": flt(row.get("rate")),
			},
		)
	so.flags.ignore_permissions = True
	so.save(ignore_permissions=True)
	frappe.db.commit()
	return get_armado_order(so.name)


@frappe.whitelist(allow_guest=True)
def get_sku_locations(item_codes=None):
	"""Map item_code → warehouse floor location code (for /check product modal)."""
	if isinstance(item_codes, str):
		try:
			item_codes = json.loads(item_codes)
		except Exception:
			item_codes = [item_codes]
	codes = [str(c).strip() for c in (item_codes or []) if str(c).strip()]
	from erpnext.erpnext_integrations.ecommerce_api.print_templates_api import _floor_sku_locations

	frappe.flags.ignore_permissions = True
	sku_to_loc, _ = _floor_sku_locations()
	if codes:
		return {
			"ok": True,
			"locations": {c: (sku_to_loc.get(c) or {}).get("location") or "" for c in codes},
		}
	return {
		"ok": True,
		"locations": {k: (v or {}).get("location") or "" for k, v in sku_to_loc.items()},
	}


@frappe.whitelist(allow_guest=True)
def get_delivery_check_items(delivery_note=None):
	"""Items for Entregas verification checklist, grouped as one 'caja' per DN (per cliente)."""
	delivery_note = str(delivery_note or "").strip()
	if not delivery_note or not frappe.db.exists("Delivery Note", delivery_note):
		frappe.throw(_("Delivery Note not found"))

	frappe.flags.ignore_permissions = True
	dn = frappe.get_doc("Delivery Note", delivery_note)
	items = []
	for d in dn.items or []:
		items.append(
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": flt(d.qty),
				"uom": d.uom,
				"against_sales_order": getattr(d, "against_sales_order", None),
			}
		)
	return {
		"ok": True,
		"delivery_note": dn.name,
		"customer": dn.customer,
		"customer_name": dn.customer_name or dn.customer,
		"cajas": [
			{
				"id": dn.name,
				"label": dn.name,
				"customer": dn.customer,
				"customer_name": dn.customer_name or dn.customer,
				"items": items,
			}
		],
	}
