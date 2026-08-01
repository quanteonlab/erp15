"""Per-table extra columns: schema + JSON values per row."""

from __future__ import annotations

import json
import re
import secrets

import frappe
from frappe import _
from frappe.utils import cint

ALLOWED_TYPES = ("string", "number", "boolean", "date")


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _slug_id(label: str) -> str:
	base = re.sub(r"[^a-z0-9]+", "_", (label or "field").strip().lower()).strip("_") or "field"
	return f"{base}_{secrets.token_hex(3)}"


def _normalize_column(col: dict) -> dict | None:
	if not isinstance(col, dict):
		return None
	cid = str(col.get("id") or "").strip()
	label = str(col.get("label") or "").strip()
	if not cid or not label:
		return None
	ftype = str(col.get("type") or "string").strip().lower()
	if ftype not in ALLOWED_TYPES:
		ftype = "string"
	return {
		"id": cid,
		"label": label,
		"type": ftype,
		"hidden": bool(cint(col.get("hidden"))),
	}


def _get_schema_doc(scope: str, create: bool = False):
	scope = (scope or "").strip()
	if not scope:
		frappe.throw(_("scope is required"))
	if frappe.db.exists("Table Extra Schema", scope):
		return frappe.get_doc("Table Extra Schema", scope)
	if not create:
		return None
	doc = frappe.get_doc(
		{"doctype": "Table Extra Schema", "scope": scope, "columns_json": "[]"}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _get_columns(scope: str) -> list[dict]:
	doc = _get_schema_doc(scope, create=False)
	if not doc:
		return []
	raw = _parse_json(doc.columns_json, [])
	out = []
	for col in raw if isinstance(raw, list) else []:
		n = _normalize_column(col)
		if n:
			out.append(n)
	return out


def _save_columns(scope: str, columns: list[dict]) -> list[dict]:
	norm = []
	for col in columns:
		n = _normalize_column(col)
		if n:
			norm.append(n)
	doc = _get_schema_doc(scope, create=True)
	doc.columns_json = json.dumps(norm, ensure_ascii=False)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return norm


def _row_doc_name(scope: str, row_key: str) -> str | None:
	return frappe.db.get_value(
		"Table Extra Data",
		{"scope": scope, "row_key": row_key},
		"name",
	)


def _get_row_data(scope: str, row_key: str) -> dict:
	name = _row_doc_name(scope, row_key)
	if not name:
		return {}
	raw = frappe.db.get_value("Table Extra Data", name, "data_json")
	data = _parse_json(raw, {})
	return data if isinstance(data, dict) else {}


def _save_row_data(scope: str, row_key: str, data: dict) -> dict:
	scope = (scope or "").strip()
	row_key = (row_key or "").strip()
	if not scope or not row_key:
		frappe.throw(_("scope and row_key are required"))
	clean = {}
	for k, v in (data or {}).items():
		if k is None or str(k).strip() == "":
			continue
		clean[str(k)] = v
	existing = _row_doc_name(scope, row_key)
	payload = json.dumps(clean, ensure_ascii=False, default=str)
	if existing:
		frappe.db.set_value("Table Extra Data", existing, "data_json", payload)
	else:
		frappe.get_doc(
			{
				"doctype": "Table Extra Data",
				"scope": scope,
				"row_key": row_key,
				"data_json": payload,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	return clean


@frappe.whitelist()
def get_extra_fields_bundle(scope, row_keys=None):
	"""
	Return schema columns + values for many rows.
	row_keys: JSON list of row keys (optional — if omitted, only schema).
	"""
	scope = (scope or "").strip()
	if isinstance(row_keys, str):
		row_keys = frappe.parse_json(row_keys)
	columns = _get_columns(scope)
	values = {}
	if row_keys:
		for rk in row_keys:
			rk = str(rk).strip()
			if not rk:
				continue
			values[rk] = _get_row_data(scope, rk)
	return {"scope": scope, "columns": columns, "values": values}


@frappe.whitelist()
def get_extra_row(scope, row_key):
	scope = (scope or "").strip()
	row_key = (row_key or "").strip()
	return {
		"scope": scope,
		"row_key": row_key,
		"columns": _get_columns(scope),
		"values": _get_row_data(scope, row_key),
	}


@frappe.whitelist()
def save_extra_row(scope, row_key, values=None):
	"""Upsert JSON values for one row. Merges with existing keys unless replace=1 via values-only full object."""
	if isinstance(values, str):
		values = frappe.parse_json(values)
	values = values if isinstance(values, dict) else {}
	# Merge into existing so partial saves work
	current = _get_row_data(scope, row_key)
	current.update(values)
	# Drop keys that are explicitly null? Keep empty string.
	saved = _save_row_data(scope, row_key, current)
	return {"ok": True, "scope": scope, "row_key": row_key, "values": saved}


@frappe.whitelist()
def add_extra_column(scope, label, fieldtype="string"):
	label = (label or "").strip()
	if not label:
		frappe.throw(_("Column label is required"))
	ftype = (fieldtype or "string").strip().lower()
	if ftype not in ALLOWED_TYPES:
		ftype = "string"
	columns = _get_columns(scope)
	col = {
		"id": _slug_id(label),
		"label": label,
		"type": ftype,
		"hidden": False,
	}
	columns.append(col)
	saved = _save_columns(scope, columns)
	return {"ok": True, "column": col, "columns": saved}


@frappe.whitelist()
def update_extra_column(scope, column_id, label=None, fieldtype=None, hidden=None):
	column_id = (column_id or "").strip()
	columns = _get_columns(scope)
	found = False
	for col in columns:
		if col["id"] != column_id:
			continue
		found = True
		if label is not None and str(label).strip():
			col["label"] = str(label).strip()
		if fieldtype is not None:
			ft = str(fieldtype).strip().lower()
			col["type"] = ft if ft in ALLOWED_TYPES else col["type"]
		if hidden is not None:
			col["hidden"] = bool(cint(hidden))
		break
	if not found:
		frappe.throw(_("Column {0} not found").format(column_id))
	saved = _save_columns(scope, columns)
	return {"ok": True, "columns": saved}


@frappe.whitelist()
def remove_extra_column(scope, column_id, scrub_values=1):
	"""Remove column from schema; optionally delete that key from all row JSON in scope."""
	column_id = (column_id or "").strip()
	columns = [c for c in _get_columns(scope) if c["id"] != column_id]
	saved = _save_columns(scope, columns)
	scrubbed = 0
	if cint(scrub_values):
		names = frappe.get_all(
			"Table Extra Data",
			filters={"scope": scope},
			fields=["name", "data_json"],
		)
		for row in names:
			data = _parse_json(row.data_json, {})
			if isinstance(data, dict) and column_id in data:
				del data[column_id]
				frappe.db.set_value(
					"Table Extra Data",
					row.name,
					"data_json",
					json.dumps(data, ensure_ascii=False, default=str),
				)
				scrubbed += 1
		frappe.db.commit()
	return {"ok": True, "columns": saved, "scrubbed_rows": scrubbed}
