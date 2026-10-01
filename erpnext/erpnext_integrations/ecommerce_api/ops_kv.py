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


# ── Offline outbox idempotency ────────────────────────────────────────────────

OPS_REQUEST_SCOPE = "ops_request"


def _clean_request_id(v) -> str:
	if v is None or isinstance(v, (list, dict, tuple)):
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s[:140]


def idempotent_request(fn):
	"""Make a create/record endpoint safe to replay from the offline ops outbox.

	The client sends ``client_request_id`` (a uuid / ``tmp:<kind>:<uuid>``). The
	first call runs ``fn`` and stores its JSON-able result; a repeat with the same
	id returns that stored result (plus ``already_exists: 1`` for dicts) instead of
	creating a duplicate. Without the id the endpoint behaves exactly as before.

	Put it *under* ``@frappe.whitelist`` — the wrapper takes ``**kwargs`` so
	Frappe passes ``client_request_id`` through.
	"""
	import functools
	import inspect

	params = inspect.signature(fn).parameters
	accepts_any = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		request_id = _clean_request_id(kwargs.pop("client_request_id", None))
		if not accepts_any:
			# Same filtering Frappe would have applied to the original function.
			kwargs = {k: v for k, v in kwargs.items() if k in params}
		if not request_id:
			return fn(*args, **kwargs)
		key = f"{fn.__module__.rsplit('.', 1)[-1]}.{fn.__name__}:{request_id}"[:140]
		_row, prior = kv_get(OPS_REQUEST_SCOPE, key)
		if prior.get("done"):
			result = prior.get("result")
			if isinstance(result, dict):
				return {**result, "already_exists": 1}
			return result
		result = fn(*args, **kwargs)
		try:
			kv_set(
				OPS_REQUEST_SCOPE,
				key,
				{"done": 1, "result": json.loads(json.dumps(result, default=str))},
			)
			frappe.db.commit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "ops_kv.idempotent_request store")
		return result

	# Frappe filters kwargs by the *wrapper's* argspec (getfullargspec ignores
	# __wrapped__), so **kwargs keeps client_request_id; drop __wrapped__ so
	# signature-based arg validation doesn't strip it either.
	try:
		del wrapper.__wrapped__
	except AttributeError:
		pass
	return wrapper
