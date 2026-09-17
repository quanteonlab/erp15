# Copyright (c) 2026, Frappe Technologies and contributors
# Catalog CSV import sessions + permissive review queue helpers.

from __future__ import unicode_literals

import json

import frappe
from frappe.utils import cint, flt, now_datetime


REASON_MISSING_ITEM_CODE = "missing_item_code"
REASON_MISSING_NAME = "missing_name"
REASON_DUPLICATE_IN_FILE = "duplicate_in_file"
REASON_BARCODE_OWNED = "barcode_owned_by_other"
REASON_ITEM_INSERT_FAILED = "item_insert_failed"
REASON_ITEM_UPDATE_FAILED = "item_update_failed"
REASON_PRICE_WRITE_FAILED = "price_write_failed"
REASON_GROUP_RESOLVE_FAILED = "group_resolve_failed"
REASON_UOM_RESOLVE_FAILED = "uom_resolve_failed"
REASON_AMBIGUOUS_MATCH = "ambiguous_match"

_SESSION_COUNTER_FIELDS = (
	"created_items",
	"updated_items",
	"review_created",
	"skipped_invalid",
	"skipped_existing",
	"price_updates",
	"barcode_updates",
	"total_rows",
	"parsed_rows",
)


def _json_dumps(value):
	try:
		return json.dumps(value, ensure_ascii=False, default=str)
	except Exception:
		return "{}"


def ensure_import_session(
	session_name=None,
	*,
	price_list="Standard Selling",
	default_item_group="Products",
	update_existing=1,
	create_missing_groups=0,
	file_name=None,
	total_rows=0,
	parsed_rows=0,
):
	"""Return an existing or new Catalog Import Session name."""
	if session_name and frappe.db.exists("Catalog Import Session", session_name):
		return session_name

	doc = frappe.get_doc(
		{
			"doctype": "Catalog Import Session",
			"status": "running",
			"operator": frappe.session.user if frappe.session else None,
			"file_name": file_name or "",
			"price_list": price_list or "Standard Selling",
			"default_item_group": default_item_group or "Products",
			"update_existing": cint(update_existing),
			"create_missing_groups": cint(create_missing_groups),
			"total_rows": cint(total_rows),
			"parsed_rows": cint(parsed_rows),
			"options_json": _json_dumps(
				{
					"price_list": price_list,
					"default_item_group": default_item_group,
					"update_existing": cint(update_existing),
					"create_missing_groups": cint(create_missing_groups),
				}
			),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def bump_session_counters(session_name, increments):
	if not session_name or not frappe.db.exists("Catalog Import Session", session_name):
		return
	for field in _SESSION_COUNTER_FIELDS:
		delta = cint(increments.get(field))
		if not delta:
			continue
		current = cint(frappe.db.get_value("Catalog Import Session", session_name, field))
		frappe.db.set_value(
			"Catalog Import Session",
			session_name,
			field,
			current + delta,
			update_modified=False,
		)


def finish_import_session(session_name, status="done"):
	if not session_name or not frappe.db.exists("Catalog Import Session", session_name):
		return
	frappe.db.set_value("Catalog Import Session", session_name, "status", status)


def enqueue_import_review(
	session_name,
	*,
	line_no=None,
	item_code=None,
	reason_code,
	message,
	severity="conflict",
	payload=None,
	live_item=None,
):
	"""Insert one Catalog Import Review row. Returns name or None."""
	if not session_name:
		return None
	if not frappe.db.exists("Catalog Import Session", session_name):
		return None

	doc = frappe.get_doc(
		{
			"doctype": "Catalog Import Review",
			"session": session_name,
			"status": "open",
			"severity": severity if severity in ("error", "conflict", "warning") else "conflict",
			"reason_code": reason_code or REASON_AMBIGUOUS_MATCH,
			"line_no": cint(line_no) if line_no is not None else None,
			"item_code": item_code or "",
			"live_item": live_item if live_item and frappe.db.exists("Item", live_item) else None,
			"message": (message or "")[:500],
			"payload_json": _json_dumps(payload or {}),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def list_import_reviews(status="open", session=None, limit=100, start=0):
	filters = {}
	if status and status != "all":
		filters["status"] = status
	if session:
		filters["session"] = session

	rows = frappe.get_all(
		"Catalog Import Review",
		filters=filters,
		fields=[
			"name",
			"session",
			"status",
			"severity",
			"reason_code",
			"line_no",
			"item_code",
			"live_item",
			"message",
			"payload_json",
			"resolution_json",
			"resolved_by",
			"resolved_at",
			"creation",
			"modified",
		],
		order_by="creation desc",
		limit_start=cint(start),
		limit_page_length=min(cint(limit) or 100, 500),
		ignore_permissions=True,
	)
	for row in rows:
		try:
			row["payload"] = json.loads(row.get("payload_json") or "{}")
		except Exception:
			row["payload"] = {}
		try:
			row["resolution"] = json.loads(row.get("resolution_json") or "{}")
		except Exception:
			row["resolution"] = {}
	return rows


def dismiss_import_review(name, note=None):
	if not name or not frappe.db.exists("Catalog Import Review", name):
		frappe.throw("Catalog Import Review not found")
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Catalog Import Review", name)
	frappe.flags.ignore_permissions = False
	doc.status = "dismissed"
	doc.resolved_by = frappe.session.user if frappe.session else None
	doc.resolved_at = now_datetime()
	doc.resolution_json = _json_dumps({"action": "dismiss", "note": note or ""})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": 1, "name": doc.name, "status": doc.status}


def resolve_import_review(name, action="apply", overrides=None, steal_barcode=0):
	"""
	Resolve an open review.
	action:
	  - apply: create/update Item from payload (+ overrides); optionally steal barcode
	  - dismiss: mark dismissed without write
	"""
	if not name or not frappe.db.exists("Catalog Import Review", name):
		frappe.throw("Catalog Import Review not found")

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Catalog Import Review", name)
	frappe.flags.ignore_permissions = False

	if doc.status != "open":
		return {"ok": 1, "name": doc.name, "status": doc.status, "skipped": 1}

	action = (action or "apply").strip().lower()
	if action == "dismiss":
		return dismiss_import_review(name)

	try:
		payload = json.loads(doc.payload_json or "{}")
	except Exception:
		payload = {}
	overrides = overrides if isinstance(overrides, dict) else {}
	merged = {**payload, **overrides}

	item_code = (merged.get("item_code") or doc.item_code or "").strip()
	if not item_code:
		frappe.throw("item_code is required to resolve this review")

	item_name = (merged.get("item_name") or item_code).strip()
	description = (merged.get("title_simplified") or item_name).strip()
	barcode = (merged.get("barcode") or "").strip()
	stock_uom = (merged.get("stock_uom") or "Nos").strip() or "Nos"
	item_group = (merged.get("item_group") or "Products").strip() or "Products"
	price = flt(merged.get("price"))

	# Lazy import to avoid circular deps with api.py helpers
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_resolve_item_group_for_import,
		_resolve_uom_for_import,
	)

	session_price_list = "Standard Selling"
	create_missing = 1
	if doc.session and frappe.db.exists("Catalog Import Session", doc.session):
		session_price_list = (
			frappe.db.get_value("Catalog Import Session", doc.session, "price_list")
			or "Standard Selling"
		)
		create_missing = cint(
			frappe.db.get_value("Catalog Import Session", doc.session, "create_missing_groups")
		)

	target_uom = _resolve_uom_for_import(stock_uom)
	target_group = _resolve_item_group_for_import(
		item_group,
		default_item_group="Products",
		create_missing_groups=create_missing,
	)

	existing = frappe.db.exists("Item", item_code)
	if existing:
		item_doc = frappe.get_doc("Item", item_code)
	else:
		item_doc = frappe.new_doc("Item")
		item_doc.item_code = item_code

	item_doc.item_name = item_name
	item_doc.description = description
	item_doc.item_group = target_group
	item_doc.stock_uom = target_uom
	item_doc.is_stock_item = 1
	item_doc.include_item_in_manufacturing = 0
	item_doc.disabled = 0

	if existing:
		item_doc.save(ignore_permissions=True)
	else:
		item_doc.insert(ignore_permissions=True)

	barcode_note = None
	if barcode:
		owner = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
		existing_same = frappe.db.exists(
			"Item Barcode", {"parent": item_doc.item_code, "barcode": barcode}
		)
		if owner and owner != item_doc.item_code:
			if cint(steal_barcode):
				# Move barcode to this item
				frappe.db.delete("Item Barcode", {"barcode": barcode})
				item_doc.reload()
				item_doc.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})
				item_doc.save(ignore_permissions=True)
				barcode_note = f"stole barcode {barcode} from {owner}"
			else:
				barcode_note = f"barcode {barcode} still owned by {owner}; not applied"
		elif not existing_same:
			item_doc.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})
			item_doc.save(ignore_permissions=True)

	if price > 0:
		price_name = frappe.db.get_value(
			"Item Price",
			{"item_code": item_doc.item_code, "price_list": session_price_list, "selling": 1},
			"name",
		)
		if price_name:
			frappe.db.set_value("Item Price", price_name, "price_list_rate", price)
		else:
			frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": item_doc.item_code,
					"price_list": session_price_list,
					"price_list_rate": price,
					"selling": 1,
				}
			).insert(ignore_permissions=True)

	doc.status = "resolved"
	doc.live_item = item_doc.item_code
	doc.item_code = item_doc.item_code
	doc.resolved_by = frappe.session.user if frappe.session else None
	doc.resolved_at = now_datetime()
	doc.resolution_json = _json_dumps(
		{
			"action": "apply",
			"steal_barcode": cint(steal_barcode),
			"barcode_note": barcode_note,
			"overrides": overrides,
		}
	)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"ok": 1,
		"name": doc.name,
		"status": doc.status,
		"item_code": item_doc.item_code,
		"barcode_note": barcode_note,
	}
