"""Universal tag system: global tag pool + per-document relationships + audit events."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import now_datetime


def _normalize_tag_name(name: str) -> str:
	return " ".join((name or "").strip().split())


def _parse_tags(raw) -> list[str]:
	if raw is None:
		return []
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except Exception:
			raw = [t.strip() for t in raw.split(",") if t.strip()]
	if not isinstance(raw, list):
		return []
	out: list[str] = []
	seen: set[str] = set()
	for item in raw:
		name = _normalize_tag_name(str(item or ""))
		if not name:
			continue
		key = name.casefold()
		if key in seen:
			continue
		seen.add(key)
		out.append(name)
	return sorted(out, key=lambda s: s.casefold())


def _get_or_create_tag(tag_name: str) -> str:
	tag_name = _normalize_tag_name(tag_name)
	if not tag_name:
		frappe.throw(_("Tag name is required"))
	existing = frappe.db.get_value("Ecommerce Tag", {"tag_name": tag_name}, "name")
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": "Ecommerce Tag", "tag_name": tag_name})
	doc.insert(ignore_permissions=True)
	return doc.name


def _log_tag_event(tag_id: str, tag_name: str, reference_doctype: str, reference_name: str) -> None:
	frappe.get_doc(
		{
			"doctype": "Ecommerce Tag Event",
			"tag": tag_id,
			"tag_name": tag_name,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"added_on": now_datetime(),
			"added_by": frappe.session.user if frappe.session.user else None,
		}
	).insert(ignore_permissions=True)


def _current_relationships(reference_doctype: str, reference_name: str) -> list[dict]:
	return frappe.get_all(
		"Ecommerce Tag Relationship",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=["name", "tag"],
		ignore_permissions=True,
	)


def _tag_names_by_ids(tag_ids: list[str]) -> dict[str, str]:
	if not tag_ids:
		return {}
	rows = frappe.get_all(
		"Ecommerce Tag",
		filters={"name": ["in", tag_ids]},
		fields=["name", "tag_name"],
		ignore_permissions=True,
	)
	return {r.name: r.tag_name for r in rows}


def tags_map_for_docs(reference_doctype: str, reference_names: list[str]) -> dict[str, list[str]]:
	"""Batch-load tag names keyed by reference_name."""
	reference_doctype = (reference_doctype or "").strip()
	names = [str(n).strip() for n in (reference_names or []) if str(n).strip()]
	if not reference_doctype or not names:
		return {}

	rows = frappe.get_all(
		"Ecommerce Tag Relationship",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": ["in", names],
		},
		fields=["reference_name", "tag"],
		ignore_permissions=True,
	)
	tag_ids = sorted({r.tag for r in rows if r.tag})
	tag_names = _tag_names_by_ids(tag_ids)

	out: dict[str, list[str]] = {n: [] for n in names}
	for row in rows:
		label = tag_names.get(row.tag)
		if not label:
			continue
		out.setdefault(row.reference_name, []).append(label)
	for key in out:
		out[key] = sorted(set(out[key]), key=lambda s: s.casefold())
	return out


def migrate_item_user_tags(item_code: str) -> list[str]:
	"""Import legacy Item._user_tags into the relationship table once."""
	item_code = (item_code or "").strip()
	if not item_code:
		return []
	raw = (frappe.db.get_value("Item", item_code, "_user_tags") or "").strip()
	if not raw:
		return []
	legacy = [t.strip() for t in raw.split(",") if t.strip()]
	if not legacy:
		return []
	set_tags_for_doc("Item", item_code, tags=legacy, commit=False)
	frappe.db.set_value("Item", item_code, "_user_tags", None)
	return _parse_tags(legacy)


def set_tags_for_doc(
	reference_doctype,
	reference_name,
	tags=None,
	*,
	commit: bool = True,
) -> dict:
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))

	desired = _parse_tags(tags)
	current = _current_relationships(reference_doctype, reference_name)
	current_tag_ids = [r.tag for r in current if r.tag]
	current_names = _tag_names_by_ids(current_tag_ids)
	current_set = {name.casefold(): (tid, name) for tid, name in current_names.items()}

	desired_by_key = {name.casefold(): name for name in desired}
	to_add = [name for key, name in desired_by_key.items() if key not in current_set]
	to_remove = [
		row
		for row in current
		if row.tag
		and current_names.get(row.tag, "").casefold() not in desired_by_key
	]

	for row in to_remove:
		frappe.delete_doc("Ecommerce Tag Relationship", row.name, ignore_permissions=True)

	for tag_name in to_add:
		tag_id = _get_or_create_tag(tag_name)
		existing = frappe.db.get_value(
			"Ecommerce Tag Relationship",
			{
				"tag": tag_id,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
			},
			"name",
		)
		if existing:
			continue
		frappe.get_doc(
			{
				"doctype": "Ecommerce Tag Relationship",
				"tag": tag_id,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
			}
		).insert(ignore_permissions=True)
		_log_tag_event(tag_id, tag_name, reference_doctype, reference_name)

	if commit:
		frappe.db.commit()
	return {"ok": True, "tags": desired}


@frappe.whitelist()
def search_tags(query=""):
	q = _normalize_tag_name(query)
	filters = {}
	if q:
		filters["tag_name"] = ["like", f"%{q}%"]
	rows = frappe.get_all(
		"Ecommerce Tag",
		filters=filters,
		fields=["name", "tag_name"],
		order_by="tag_name asc",
		limit_page_length=30,
		ignore_permissions=True,
	)
	return {"tags": rows}


@frappe.whitelist()
def get_tags_for_doc(reference_doctype, reference_name):
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))

	tags = tags_map_for_docs(reference_doctype, [reference_name]).get(reference_name, [])
	if not tags and reference_doctype == "Item":
		tags = migrate_item_user_tags(reference_name)
		if tags:
			frappe.db.commit()
	return {"reference_doctype": reference_doctype, "reference_name": reference_name, "tags": tags}


@frappe.whitelist()
def set_doc_tags(reference_doctype, reference_name, tags=None):
	return set_tags_for_doc(reference_doctype, reference_name, tags=tags, commit=True)
