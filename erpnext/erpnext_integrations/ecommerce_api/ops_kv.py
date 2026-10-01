"""Tiny durable KV on top of ``Table Extra Data`` (scope / row_key / data_json).

Used for offline-outbox idempotency keys and small per-document snapshots
(cargo checklist). No DocType / migrate — same pattern as device_link_api.
"""

from __future__ import annotations

import json

import frappe


def kv_get(scope: str, row_key: str) -> tuple[str | None, dict]:
	if not scope or not row_key:
		return None, {}
	name = frappe.db.get_value("Table Extra Data", {"scope": scope, "row_key": row_key}, "name")
	if not name:
		return None, {}
	raw = frappe.db.get_value("Table Extra Data", name, "data_json")
	try:
		data = json.loads(raw) if raw else {}
	except Exception:
		data = {}
	return name, data if isinstance(data, dict) else {}


def kv_set(scope: str, row_key: str, data: dict) -> None:
	name, _current = kv_get(scope, row_key)
	payload = json.dumps(data or {}, ensure_ascii=False, default=str)
	if name:
		frappe.db.set_value("Table Extra Data", name, "data_json", payload)
		return
	frappe.get_doc(
		{
			"doctype": "Table Extra Data",
			"scope": scope,
			"row_key": row_key,
			"data_json": payload,
		}
	).insert(ignore_permissions=True)


def kv_get_many(scope: str, row_keys: list[str]) -> dict[str, dict]:
	"""Batch-read KV rows for one scope. Returns ``{row_key: data_dict}``."""
	keys = [str(k).strip() for k in (row_keys or []) if str(k).strip()]
	if not scope or not keys:
		return {}
	rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": scope, "row_key": ["in", keys]},
		fields=["row_key", "data_json"],
		ignore_permissions=True,
	)
	out: dict[str, dict] = {}
	for row in rows or []:
		rk = str(row.get("row_key") or "").strip()
		if not rk:
			continue
		raw = row.get("data_json")
		try:
			data = json.loads(raw) if raw else {}
		except Exception:
			data = {}
		if isinstance(data, dict):
			out[rk] = data
	return out
