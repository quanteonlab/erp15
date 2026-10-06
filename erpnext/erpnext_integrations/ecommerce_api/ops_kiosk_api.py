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

from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get, kv_set


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


def _require_admin_pin(pin: str | None) -> None:
	"""Mandatory company admin PIN (archive / destructive kiosk actions)."""
	pin = str(pin or "").strip()
	if not pin:
		frappe.throw(_("Admin PIN required"), frappe.AuthenticationError)
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import validate_admin_pin

	res = validate_admin_pin(pin)
	if not (isinstance(res, dict) and res.get("authorized")):
		frappe.throw(_("Incorrect admin PIN"), frappe.AuthenticationError)


def _ops_identity_from_acting_user() -> dict | None:
	"""Build an ops-operator identity from the shop session (no PIN).

	Used when a logged-in staff user opens Armado / ops pages without re-entering PIN.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.company_context import is_desk_admin
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_acting_username,
		get_user_app_permissions,
	)

	user = (_acting_username() or "").strip()
	if not user or user.lower() in ("guest", "null", "none", "undefined"):
		user = (frappe.session.user or "").strip()
	if not user or user.lower() in ("guest", "null", "none", "undefined"):
		return None

	if is_desk_admin(user):
		return {
			"authorized": True,
			"kind": "admin",
			"employee": None,
			"employee_name": None,
			"user_id": user,
			"permissions": ["*"],
			"pin_configured": True,
		}

	info = None
	try:
		info = get_user_app_permissions(user)
	except Exception:
		info = None
	perms = list((info or {}).get("permissions") or [])
	source = (info or {}).get("source") or "roles"
	if source == "admin" or "*" in perms:
		return {
			"authorized": True,
			"kind": "admin",
			"employee": None,
			"employee_name": None,
			"user_id": user,
			"permissions": ["*"],
			"pin_configured": True,
		}

	emp = frappe.db.get_value(
		"Employee",
		{"user_id": user},
		["name", "employee_name", "status"],
		as_dict=True,
	)
	if emp and (emp.status or "") != "Active":
		emp = None
	return {
		"authorized": True,
		"kind": "employee",
		"employee": emp.name if emp else None,
		"employee_name": (emp.employee_name if emp else None) or user,
		"user_id": user,
		"permissions": perms,
		"pin_configured": True,
	}


def _require_ops_operator(pin: str | None) -> dict:
	"""Require admin/employee 6-digit PIN, or a logged-in acting user (empty pin)."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import resolve_ops_pin

	raw = str(pin or "").strip()
	if raw:
		res = resolve_ops_pin(raw)
		if not (isinstance(res, dict) and res.get("authorized")):
			frappe.throw(_("Incorrect PIN"), frappe.AuthenticationError)
		return res

	identity = _ops_identity_from_acting_user()
	if identity and identity.get("authorized"):
		return identity
	frappe.throw(_("PIN required"), frappe.AuthenticationError)


def _operator_label(identity: dict | None) -> str:
	if not isinstance(identity, dict):
		return "—"
	if identity.get("kind") == "admin":
		return "Admin"
	name = str(identity.get("employee_name") or identity.get("employee") or "").strip()
	return name or "—"


def _log_armado_operator(so_name: str, identity: dict, *, via: str = "armado") -> None:
	so_name = str(so_name or "").strip()
	if not so_name:
		return
	try:
		from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

		label = _operator_label(identity)
		log_field_changes(
			"Sales Order",
			so_name,
			[("weighed_by", "", label), ("weighed_via", "", via)],
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "armado operator audit")


@frappe.whitelist(allow_guest=True)
def resolve_armado_ean13(code=None):
	"""Map a scanned armado EAN-13 → Sales Order name (Orden-stage only)."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _display_status_from_row

	code = str(code or "").strip()
	if not is_armado_ean13(code):
		return {"ok": False, "match": None}

	# Search today's + last 14 days guest SOs; compare deterministic codes.
	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"status": ["not in", ["Closed", "Completed", "Preparado", "En Delivery", "Cancelled", "Consulta"]],
			"transaction_date": [">=", frappe.utils.add_days(today(), -14)],
		},
		fields=["name", "docstatus", "status", "grand_total", "advance_paid"],
		order_by="modified desc",
		limit_page_length=500,
		ignore_permissions=True,
	)
	for row in rows:
		disp = _display_status_from_row(
			row.docstatus, row.status, row.grand_total, row.advance_paid
		)
		if disp != "Orden":
			continue
		if armado_ean13_for_order(row.name) == code:
			return {"ok": True, "match": row.name, "ean13": code}
	return {"ok": False, "match": None, "ean13": code}


@frappe.whitelist(allow_guest=True)
def list_armado_orders(delivery_date=None, limit=80):
	"""Orders for the armado kiosk.

	Only **Orden**-stage open orders (not Consulta drafts, not already Preparado /
	En Delivery / Completado). ``delivery_date`` empty / null / ``"all"`` → no date
	filter (default). Otherwise only orders due that day.
	Also returns ``delivery_dates`` (distinct due dates among Orden-stage armado
	orders) so the UI can offer a day picker.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import _display_status_from_row

	raw = delivery_date
	if isinstance(raw, str):
		raw = raw.strip()
	# Default: no filter. Explicit "all"/""/None → all Orden-stage open orders.
	filter_all = raw in (None, "", "all", "null", "undefined")
	due = None if filter_all else getdate(raw)
	limit = max(1, min(cint(limit) or (200 if filter_all else 80), 300))
	tag_field = _guest_tag_field()

	frappe.flags.ignore_permissions = True
	filters = {
		"docstatus": 1,
		"status": ["not in", ["Closed", "Completed", "Preparado", "En Delivery", "Cancelled", "Consulta"]],
	}
	if due is not None:
		filters["delivery_date"] = due

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
		"advance_paid",
		"modified",
	]
	if tag_field:
		fields.append(tag_field)

	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=fields,
		order_by="delivery_date asc, customer_name asc, name asc",
		limit_page_length=max(limit * 3, 120),
		ignore_permissions=True,
	)

	# Distinct due dates for the filter UI (Orden-stage pool only).
	date_rows = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"status": ["not in", ["Closed", "Completed", "Preparado", "En Delivery", "Cancelled", "Consulta"]],
			"delivery_date": ["is", "set"],
		},
		fields=["delivery_date", "docstatus", "status", "grand_total", "advance_paid"],
		order_by="delivery_date asc",
		limit_page_length=800,
		ignore_permissions=True,
	)
	delivery_dates = sorted(
		{
			str(r.delivery_date)
			for r in date_rows
			if r.delivery_date
			and _display_status_from_row(
				r.docstatus, r.status, r.grand_total, r.advance_paid
			)
			== "Orden"
		}
	)

	out = []
	for r in rows:
		disp = _display_status_from_row(
			r.docstatus, r.status, r.grand_total, getattr(r, "advance_paid", 0)
		)
		if disp != "Orden":
			continue
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
				"display_status": disp,
				"is_guest": is_guest,
				"ean13": ean13,
				"items_count": frappe.db.count("Sales Order Item", {"parent": r.name}),
			}
		)
		if len(out) >= limit:
			break
	return {
		"ok": True,
		"delivery_date": "" if due is None else str(due),
		"delivery_dates": delivery_dates,
		"orders": out,
	}


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
	identity = _require_ops_operator(pin)
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Order not found"))
	_save_armado_notes(preorder_name, str(notes or ""))
	_log_armado_operator(preorder_name, identity, via="armado_notes")
	return {
		"ok": True,
		"notes": str(notes or ""),
		"operator": {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		},
	}


@frappe.whitelist(allow_guest=True)
def update_armado_items(preorder_name=None, items=None, pin=None):
	"""Update qty/weight on armado lines (reuses guest preorder item update when possible).

	Does **not** advance pipeline — use ``confirm_armado`` to move Orden → Preparado.
	"""
	identity = _require_ops_operator(pin)
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
		_log_armado_operator(detail.get("name") or preorder_name, identity, via="armado_items")
		return {
			"ok": True,
			"order": {**detail, "ean13": armado_ean13_for_order(detail.get("name") or preorder_name)},
			"operator": {
				"kind": identity.get("kind"),
				"employee": identity.get("employee"),
				"employee_name": identity.get("employee_name"),
			},
		}

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
	_log_armado_operator(so.name, identity, via="armado_items")
	out = get_armado_order(so.name)
	if isinstance(out, dict):
		out["operator"] = {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		}
	return out


@frappe.whitelist(allow_guest=True)
def confirm_armado(preorder_name=None, items=None, pin=None):
	"""Save armado quantities (optional) and move pipeline Orden → Preparado (armado).

	``items`` when provided are persisted first via ``update_armado_items``; then the
	order is promoted with ``source=armado``. Rejects non-Orden stages.

	Planner remito/geocode is enqueued (not inline) so the kiosk confirm button
	returns quickly — same pattern as create_guest_preorder → Orden.
	"""
	identity = _require_ops_operator(pin)
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Order not found"))

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_display_status,
		_is_guest_preorder_sales_order,
		set_guest_preorder_status,
	)

	# Persist qty edits before promoting when the kiosk sends the current sheet.
	if items is not None and items != "" and items != "null":
		if isinstance(items, str):
			try:
				items = json.loads(items)
			except Exception:
				items = None
		if isinstance(items, list) and items:
			update_armado_items(preorder_name=preorder_name, items=items, pin=pin)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	frappe.flags.ignore_permissions = False

	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Only guest preorders can be confirmed in armado"))

	current = _display_status(so)
	base = "Completado" if str(current).startswith("Completado") else current
	if base == "Preparado":
		# Idempotent: already armado — return sheet without regressing.
		out = get_armado_order(preorder_name)
		order = out.get("order") if isinstance(out, dict) else None
		return {
			"ok": True,
			"already_confirmed": 1,
			"order": order,
			"operator": {
				"kind": identity.get("kind"),
				"employee": identity.get("employee"),
				"employee_name": identity.get("employee_name"),
			},
		}
	if base != "Orden":
		frappe.throw(
			_("Only Orden-stage orders can be confirmed in armado (current: {0})").format(base)
		)

	detail = set_guest_preorder_status(
		preorder_name, "Preparado", source="armado", ensure_planner_remito=0
	)
	_log_armado_operator(
		(detail or {}).get("name") or preorder_name, identity, via="armado_confirm"
	)
	# Prefer ops PIN employee user_id / name for L_* over API-key session user.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tags_api import (
			safe_touch_sales_order_last_editor,
		)

		actor = None
		if identity.get("kind") == "admin":
			actor = "Admin"
		else:
			actor = identity.get("user_id") or identity.get("employee_name") or identity.get("employee")
		safe_touch_sales_order_last_editor(
			(detail or {}).get("name") or preorder_name, user=actor, commit=True
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "armado last-editor tag")
	name = (detail or {}).get("name") or preorder_name
	# Remito + geocode used to run inline here and stall the kiosk — same deferral
	# pattern as create_guest_preorder (Orden). Order is already Preparado.
	from erpnext.erpnext_integrations.ecommerce_api.api import _enqueue_planner_remito

	remito_job = _enqueue_planner_remito(name)
	# Light payload — skip get_armado_order floor-map rebuild; UI drops the sheet.
	order = dict(detail) if isinstance(detail, dict) else {"name": name}
	order["ean13"] = armado_ean13_for_order(name)
	order["display_status"] = order.get("display_status") or "Preparado"
	order["status"] = order.get("status") or "Preparado"
	return {
		"ok": True,
		"already_confirmed": 0,
		"remito_queued": 1 if remito_job.get("queued") else 0,
		"order": order,
		"operator": {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		},
	}


@frappe.whitelist(allow_guest=True)
def archive_armado(preorder_name=None, pin=None):
	"""Cancel/archive the current armado Sales Order. Requires company admin PIN.

	Unlike other armado writes (operator PIN), archive is destructive and must
	be unlocked with the admin PIN dialog — employee ops PINs are rejected.
	"""
	_require_admin_pin(pin)
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Order not found"), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	frappe.flags.ignore_permissions = False

	if cint(so.docstatus) == 2:
		return {"ok": True, "already_archived": 1, "name": so.name, "status": "Cancelled"}

	so.flags.ignore_permissions = True
	if cint(so.docstatus) == 1:
		so.cancel()
	else:
		# Armado list is submitted Orden only; draft cancel is still supported.
		frappe.delete_doc("Sales Order", so.name, ignore_permissions=True, force=True)
		frappe.db.commit()
		_log_armado_operator(
			preorder_name,
			{"kind": "admin", "employee": None, "employee_name": "Admin"},
			via="armado_archive",
		)
		return {"ok": True, "already_archived": 0, "name": preorder_name, "status": "Deleted"}

	so.reload()
	_log_armado_operator(
		so.name,
		{"kind": "admin", "employee": None, "employee_name": "Admin"},
		via="armado_archive",
	)
	frappe.db.commit()
	return {
		"ok": True,
		"already_archived": 0,
		"name": so.name,
		"status": "Cancelled",
		"docstatus": cint(so.docstatus),
	}


def _maybe_promote_armado_to_preparado(so_name: str, detail: dict | None = None):
	"""Deprecated path — kept for callers; prefer ``confirm_armado``.

	After armado qty commit used to auto-promote Orden → Preparado. Promotion is
	now explicit via ``confirm_armado`` so "Guardar cantidades" does not advance.
	"""
	return detail


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
	if (
		not delivery_note
		or delivery_note.lower() in ("null", "undefined", "none")
		or not frappe.db.exists("Delivery Note", delivery_note)
	):
		frappe.throw(_("Delivery Note not found"))

	frappe.flags.ignore_permissions = True
	dn = frappe.get_doc("Delivery Note", delivery_note)
	frappe.flags.ignore_permissions = False
	items = []
	for d in dn.items or []:
		qty = flt(d.qty)
		rate = flt(getattr(d, "rate", None) or 0)
		amount = flt(getattr(d, "amount", None) or (rate * qty))
		items.append(
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": qty,
				"uom": d.uom,
				"rate": rate,
				"amount": amount,
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


CARGO_CHECK_SCOPE = "cargo_check"


def _clean_dn_name(delivery_note) -> str:
	raw = str(delivery_note or "").strip() if not isinstance(delivery_note, (list, dict, tuple)) else ""
	if raw.lower() in ("null", "undefined", "none"):
		return ""
	return raw[:140]


def _clean_cargo_qtys(qtys) -> dict:
	if isinstance(qtys, str):
		try:
			qtys = json.loads(qtys) if qtys.strip() else {}
		except Exception:
			qtys = {}
	if not isinstance(qtys, dict):
		return {}
	out = {}
	for k, v in list(qtys.items())[:2000]:
		key = str(k or "").strip()[:280]
		if not key:
			continue
		try:
			n = flt(v)
		except Exception:
			continue
		out[key] = max(0.0, n)
	return out


@frappe.whitelist(allow_guest=True)
def get_cargo_check(delivery_note=None):
	"""Server copy of the truck-load checklist for a DN (``{qtys, updated_at}``)."""
	dn = _clean_dn_name(delivery_note)
	if not dn:
		frappe.throw(_("delivery_note is required"))
	_row, data = kv_get(CARGO_CHECK_SCOPE, dn)
	return {
		"ok": True,
		"delivery_note": dn,
		"qtys": _clean_cargo_qtys(data.get("qtys")),
		"updated_at": cint(data.get("updated_at") or 0),
	}


@frappe.whitelist(allow_guest=True)
def save_cargo_check(delivery_note=None, qtys=None, updated_at=None):
	"""Upsert the truck-load checklist for a DN (offline ops outbox ``cargo_check``).

	Last-write-wins by client ``updated_at`` (ms epoch): an older replay is ignored.
	"""
	dn = _clean_dn_name(delivery_note)
	if not dn:
		frappe.throw(_("delivery_note is required"))
	if not frappe.db.exists("Delivery Note", dn):
		frappe.throw(_("Delivery Note not found"))
	clean = _clean_cargo_qtys(qtys)
	stamp = cint(updated_at or 0) or cint(frappe.utils.now_datetime().timestamp() * 1000)
	_row, current = kv_get(CARGO_CHECK_SCOPE, dn)
	current_stamp = cint(current.get("updated_at") or 0)
	if current_stamp and stamp < current_stamp:
		return {"ok": True, "delivery_note": dn, "stale": 1, "updated_at": current_stamp}
	kv_set(CARGO_CHECK_SCOPE, dn, {"qtys": clean, "updated_at": stamp})
	frappe.db.commit()
	return {"ok": True, "delivery_note": dn, "stale": 0, "updated_at": stamp}
