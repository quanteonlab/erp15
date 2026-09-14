"""Field-level history for Tables side panels (Frappe Version + manual logs)."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, format_datetime


# UI field names used by Product Manager Historial ↔ Item DB fields
_ITEM_UI_TO_DB = {
	"source_title": "item_name",
	"source_category": "item_group",
	"stock_uom": "stock_uom",
	"brand": "brand",
	"image": "image",
	"pack_qty": "custom_pack_qty",
	"pack_size": "custom_pack_size",
	"unit": "custom_pack_unit",
	"unit_sku": "custom_unit_sku",
	"normalized_title": "custom_normalized_title",
	"review_notes": "custom_review_notes",
	"is_active": "disabled",
}
_ITEM_DB_TO_UI = {v: k for k, v in _ITEM_UI_TO_DB.items()}

def _serialize_val(val):
	if val is None:
		return None
	if isinstance(val, (dict, list)):
		return json.dumps(val, ensure_ascii=False, default=str)
	return str(val)


def log_field_changes(doctype: str, docname: str, changes: list[tuple[str, object, object]]) -> None:
	"""Insert a Version row for API-driven edits (works even if track_changes is off)."""
	changed = []
	for field, old, new in changes:
		if old == new:
			continue
		changed.append([field, _serialize_val(old), _serialize_val(new)])
	if not changed:
		return
	doc = frappe.get_doc(
		{
			"doctype": "Version",
			"ref_doctype": doctype,
			"docname": docname,
			"data": frappe.as_json(
				{
					"changed": changed,
					"added": [],
					"removed": [],
					"row_changed": [],
				}
			),
		}
	)
	doc.insert(ignore_permissions=True)


def _friendly_item_field(field: str) -> str:
	return _ITEM_DB_TO_UI.get(field, field)


def _parse_version_row(row, *, friendly_item: bool = False) -> list[dict]:
	out = []
	try:
		data = json.loads(row.data) if isinstance(row.data, str) else (row.data or {})
	except Exception:
		return out
	who = row.owner or row.modified_by or "—"
	dt = format_datetime(row.creation) if row.creation else ""
	iso = str(row.creation) if row.creation else ""
	for ch in data.get("changed") or []:
		if not isinstance(ch, (list, tuple)) or len(ch) < 3:
			continue
		field, prev, new = ch[0], ch[1], ch[2]
		if friendly_item:
			field = _friendly_item_field(field)
		# stock_qty needs warehouse context — show but don't offer one-click revert
		revertible = field not in ("stock_qty",)
		out.append(
			{
				"id": f"{row.name}:{field}:{iso}",
				"version": row.name,
				"who": who,
				"field": field,
				"previous": "" if prev is None else str(prev),
				"new": "" if new is None else str(new),
				"datetime": dt,
				"datetime_raw": iso,
				"doctype": row.ref_doctype,
				"docname": row.docname,
				"revertible": revertible,
			}
		)
	return out


def _versions_for(doctype: str, name: str, limit: int = 80, *, friendly_item: bool = False) -> list[dict]:
	rows = frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": name},
		fields=["name", "owner", "modified_by", "creation", "data", "ref_doctype", "docname"],
		order_by="creation desc",
		limit=limit,
		ignore_permissions=True,
	)
	flat = []
	for row in rows:
		flat.extend(_parse_version_row(row, friendly_item=friendly_item and doctype == "Item"))
	return flat


def _append_item_price_history(
	rows: list[dict],
	*,
	item_code: str,
	price_list: str,
	selling: int,
	ui_field: str,
	limit: int,
) -> None:
	filters = {"item_code": item_code, "price_list": price_list}
	if selling:
		filters["selling"] = 1
	else:
		filters["buying"] = 1
	ip_name = frappe.db.get_value("Item Price", filters, "name")
	if not ip_name:
		return
	for r in _versions_for("Item Price", ip_name, limit=limit):
		if r.get("field") == "price_list_rate":
			r = {**r, "field": ui_field}
		rows.append(r)


@frappe.whitelist()
def list_field_history(doctype, name, limit=50, price_list=None):
	"""
	Flat field history for a document.
	If doctype=Item and price_list is set, also includes Item Price history for that list
	(list_price) and the default buying list (cost_price).
	"""
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	if not doctype or not name:
		frappe.throw(_("doctype and name are required"))
	limit = max(1, min(200, cint(limit) or 50))

	rows = _versions_for(doctype, name, limit=limit, friendly_item=doctype == "Item")

	if doctype == "Item":
		if price_list:
			_append_item_price_history(
				rows,
				item_code=name,
				price_list=price_list,
				selling=1,
				ui_field="list_price",
				limit=limit,
			)
		buying_pl = (
			frappe.db.get_single_value("Buying Settings", "buying_price_list") or "Standard Buying"
		)
		if buying_pl:
			_append_item_price_history(
				rows,
				item_code=name,
				price_list=buying_pl,
				selling=0,
				ui_field="cost_price",
				limit=limit,
			)

	rows.sort(key=lambda r: r.get("datetime_raw") or "", reverse=True)
	return {"rows": rows[:limit], "doctype": doctype, "name": name}


def _revert_item_barcode(item_code: str, value: str) -> None:
	from erpnext.erpnext_integrations.ecommerce_api.product_manager import _upsert_barcode

	barcode = (value or "").strip()
	old = _item_barcode_snapshot(item_code)
	if not barcode:
		item_doc = frappe.get_doc("Item", item_code)
		item_doc.set("barcodes", [])
		item_doc.save(ignore_permissions=True)
		log_field_changes("Item", item_code, [("barcode", old, None)])
		return
	_upsert_barcode(item_code, barcode)
	log_field_changes("Item", item_code, [("barcode", old, barcode)])


def _item_barcode_snapshot(item_code: str) -> str | None:
	rows = frappe.get_all(
		"Item Barcode",
		filters={"parent": item_code},
		fields=["barcode"],
		order_by="idx asc",
		limit=1,
		ignore_permissions=True,
	)
	if not rows:
		return None
	val = (rows[0].get("barcode") or "").strip()
	return val or None


def _revert_item_tags(item_code: str, value: str) -> None:
	from erpnext.erpnext_integrations.ecommerce_api.tags_api import get_tags_for_doc, set_tags_for_doc

	old = ", ".join(sorted(str(t).strip() for t in (get_tags_for_doc("Item", item_code) or []) if str(t).strip()))
	tags = [t.strip() for t in (value or "").split(",") if t.strip()]
	set_tags_for_doc("Item", item_code, tags=tags, commit=False)
	new = ", ".join(sorted(tags))
	log_field_changes("Item", item_code, [("tags", old or None, new or None)])


@frappe.whitelist()
def revert_field_change(doctype, name, field, value):
	"""Set a single field back to *value* and save (creates a new Version entry)."""
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	field = (field or "").strip()
	if not doctype or not name or not field:
		frappe.throw(_("doctype, name and field are required"))

	# Aliases used by Tables UI
	if doctype == "Item Price" and field in ("list_price", "cost_price"):
		field = "price_list_rate"
	if doctype == "Item" and field == "list_price":
		frappe.throw(_("Use Item Price document to revert list_price"))
	if doctype == "Item" and field == "cost_price":
		frappe.throw(_("Use Item Price document to revert cost_price"))
	if doctype == "Item" and field == "stock_qty":
		frappe.throw(_("Stock quantity cannot be reverted from Historial"))

	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(doctype, name))

	frappe.flags.ignore_permissions = True
	try:
		if doctype == "Item" and field == "barcode":
			_revert_item_barcode(name, value)
			frappe.db.commit()
			return {"ok": True, "doctype": doctype, "name": name, "field": "barcode", "value": value}

		if doctype == "Item" and field == "tags":
			_revert_item_tags(name, value)
			frappe.db.commit()
			return {"ok": True, "doctype": doctype, "name": name, "field": "tags", "value": value}

		db_field = field
		if doctype == "Item":
			db_field = _ITEM_UI_TO_DB.get(field, field)

		meta = frappe.get_meta(doctype)
		if not meta.has_field(db_field) and db_field not in ("name",):
			frappe.throw(_("Field {0} not on {1}").format(field, doctype))

		doc = frappe.get_doc(doctype, name)
		old = doc.get(db_field)
		df = meta.get_field(db_field)
		new_val = value

		if doctype == "Item" and field == "is_active":
			# Historial stores is_active (1/0); Item column is disabled
			new_val = 0 if cint(value) else 1
			old_ui = 0 if cint(old) else 1
			doc.set("disabled", new_val)
			doc.save(ignore_permissions=True)
			log_field_changes(doctype, name, [("is_active", old_ui, cint(value))])
			frappe.db.commit()
			return {"ok": True, "doctype": doctype, "name": name, "field": field, "value": cint(value)}

		if df and df.fieldtype in ("Currency", "Float", "Percent", "Int"):
			try:
				new_val = float(value) if df.fieldtype != "Int" else int(float(value))
			except (TypeError, ValueError):
				new_val = value

		doc.set(db_field, new_val)
		doc.save(ignore_permissions=True)
		ui_field = _ITEM_DB_TO_UI.get(db_field, field) if doctype == "Item" else field
		log_field_changes(doctype, name, [(ui_field if doctype == "Item" else db_field, old, doc.get(db_field))])
		frappe.db.commit()
		return {
			"ok": True,
			"doctype": doctype,
			"name": name,
			"field": ui_field if doctype == "Item" else db_field,
			"value": doc.get(db_field),
		}
	finally:
		frappe.flags.ignore_permissions = False
