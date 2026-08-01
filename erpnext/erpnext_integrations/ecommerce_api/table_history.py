"""Field-level history for Tables side panels (Frappe Version + manual logs)."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, format_datetime


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


def _parse_version_row(row) -> list[dict]:
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
				"revertible": True,
			}
		)
	return out


def _versions_for(doctype: str, name: str, limit: int = 80) -> list[dict]:
	rows = frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": name},
		fields=["name", "owner", "modified_by", "creation", "data", "ref_doctype", "docname"],
		order_by="creation desc",
		limit=limit,
	)
	flat = []
	for row in rows:
		flat.extend(_parse_version_row(row))
	return flat


@frappe.whitelist()
def list_field_history(doctype, name, limit=50, price_list=None):
	"""
	Flat field history for a document.
	If doctype=Item and price_list is set, also includes Item Price history for that list.
	"""
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	if not doctype or not name:
		frappe.throw(_("doctype and name are required"))
	limit = max(1, min(200, cint(limit) or 50))

	rows = _versions_for(doctype, name, limit=limit)

	if doctype == "Item" and price_list:
		ip_name = frappe.db.get_value(
			"Item Price",
			{"item_code": name, "price_list": price_list, "selling": 1},
			"name",
		)
		if ip_name:
			for r in _versions_for("Item Price", ip_name, limit=limit):
				# Prefer friendly field label in UI
				if r.get("field") == "price_list_rate":
					r = {**r, "field": "list_price"}
				rows.append(r)

	rows.sort(key=lambda r: r.get("datetime_raw") or "", reverse=True)
	return {"rows": rows[:limit], "doctype": doctype, "name": name}


@frappe.whitelist()
def revert_field_change(doctype, name, field, value):
	"""Set a single field back to *value* and save (creates a new Version entry)."""
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	field = (field or "").strip()
	if not doctype or not name or not field:
		frappe.throw(_("doctype, name and field are required"))

	# Alias used by Tables UI
	if doctype == "Item Price" and field == "list_price":
		field = "price_list_rate"
	if doctype == "Item" and field == "list_price":
		# Caller should pass Item Price; keep safe fallback
		frappe.throw(_("Use Item Price document to revert list_price"))

	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(doctype, name))

	meta = frappe.get_meta(doctype)
	if not meta.has_field(field) and field not in ("name",):
		frappe.throw(_("Field {0} not on {1}").format(field, doctype))

	doc = frappe.get_doc(doctype, name)
	old = doc.get(field)
	# Coerce numbers when the target looks numeric
	df = meta.get_field(field)
	new_val = value
	if df and df.fieldtype in ("Currency", "Float", "Percent", "Int"):
		try:
			new_val = float(value) if df.fieldtype != "Int" else int(float(value))
		except (TypeError, ValueError):
			new_val = value

	doc.set(field, new_val)
	doc.save(ignore_permissions=False)
	log_field_changes(doctype, name, [(field, old, doc.get(field))])
	frappe.db.commit()
	return {"ok": True, "doctype": doctype, "name": name, "field": field, "value": doc.get(field)}
