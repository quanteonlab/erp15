# Copyright (c) 2026, SilkOS and contributors
"""Public ops kiosk APIs for /check (showcase) and /armado (pick sheet).

PIN gating is enforced in the Next.js UI (Admin PIN). These endpoints are
allow_guest so kiosk tablets can call through the Next proxy without a
staff login session — same pattern as catalog.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, today

from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get, kv_set


# Internal EAN-13 prefix for armado sheets. Retail product barcodes must not use
# 290… (in-store / internal range). Lookup on /armado prefers this prefix.
ARMADO_EAN13_PREFIX = "290"

# Pedidos → /armado deep-link (theater hash). Opens any SO for ~1h even outside list filters.
ARMADO_ACCESS_TTL_SECONDS = 3600


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


ARMADO_PROGRESS_EVENT_CAP = 100


def _armado_progress_scope(so_name: str) -> str:
	return f"ops.armado.progress::{so_name}"


def _empty_armado_progress() -> dict:
	return {"lines": {}, "boxes": {"count": 0, "by": "", "at": ""}, "events": []}


def _load_armado_progress(so_name: str) -> dict:
	scope = _armado_progress_scope(so_name)
	base = _empty_armado_progress()
	if not so_name or not frappe.db.exists("Table Extra Schema", scope):
		return base
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", scope)
	data = {}
	try:
		data = json.loads(doc.columns_json or "{}")
	except Exception:
		data = {}
	if not isinstance(data, dict):
		return base
	lines = data.get("lines") if isinstance(data.get("lines"), dict) else {}
	boxes = data.get("boxes") if isinstance(data.get("boxes"), dict) else {}
	events = data.get("events") if isinstance(data.get("events"), list) else []
	base["snapshot"] = data.get("snapshot") if isinstance(data.get("snapshot"), list) else []
	base["last_saved_at"] = str(data.get("last_saved_at") or "")
	base["last_saved_by"] = str(data.get("last_saved_by") or "")
	base["amended_after_pack"] = 1 if cint(data.get("amended_after_pack")) else 0
	base["remito_desynced"] = 1 if cint(data.get("remito_desynced")) else 0
	base["lines"] = lines
	base["boxes"] = {
		"count": cint(boxes.get("count")),
		"by": str(boxes.get("by") or ""),
		"at": str(boxes.get("at") or ""),
	}
	base["events"] = events[-ARMADO_PROGRESS_EVENT_CAP:]
	return base


def _save_armado_progress(so_name: str, progress: dict) -> None:
	scope = _armado_progress_scope(so_name)
	events = progress.get("events") if isinstance(progress.get("events"), list) else []
	progress["events"] = events[-ARMADO_PROGRESS_EVENT_CAP:]
	payload = json.dumps(progress, ensure_ascii=False, default=str)
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


def _blank_progress_line(item_code: str) -> dict:
	return {
		"item_code": item_code,
		"packed": False,
		"missing": False,
		"missing_reason": "",
		"packed_by": "",
		"packed_by_employee": "",
		"packed_at": "",
		"missing_by": "",
		"missing_by_employee": "",
		"missing_at": "",
		"qty": None,
		"total_weight": None,
	}


def _coerce_progress_map(raw, label: str):
	"""None when the arg was omitted. Non-maps are a controlled error."""
	if raw is None:
		return None
	if isinstance(raw, str):
		text = raw.strip()
		if text.lower() in ("", "null", "undefined", "none"):
			return None
		try:
			raw = json.loads(text)
		except Exception:
			frappe.throw(_("{0} must be a map").format(label))
	if raw is None:
		return None
	if isinstance(raw, (list, tuple, bool, int, float)):
		frappe.throw(_("{0} must be a map").format(label))
	if not isinstance(raw, dict):
		frappe.throw(_("{0} must be a map").format(label))
	return raw


def _flag_from_value(value) -> bool:
	if isinstance(value, dict):
		if "missing" in value:
			return _flag_from_value(value.get("missing"))
		if "packed" in value:
			return _flag_from_value(value.get("packed"))
		return True
	if value in (None, "", "null", "undefined", "none", "false", "False", "0", 0, 0.0, False):
		return False
	if value in (True, 1, "1", "true", "True", "yes", "on"):
		return True
	return bool(value)


def _missing_reason_from_value(value) -> str:
	if isinstance(value, dict):
		reason = str(value.get("reason") or value.get("missing_reason") or "").strip()
		return (reason or "sin stock")[:140]
	return "sin stock"


def _parse_box_count(raw, *, required: bool = False):
	if isinstance(raw, dict):
		raw = raw.get("count")
	if raw is None:
		if required:
			frappe.throw(_("Box count required"))
		return None
	if isinstance(raw, str):
		text = raw.strip()
		if text.lower() in ("", "null", "undefined", "none"):
			if required:
				frappe.throw(_("Box count required"))
			return None
		raw = text
	if isinstance(raw, (list, tuple, dict, bool)):
		frappe.throw(_("Invalid box count"))
	try:
		count = cint(raw)
	except Exception:
		frappe.throw(_("Invalid box count"))
	if count < 1 or count > 99:
		frappe.throw(_("Box count must be between 1 and 99"))
	return count


def _so_armado_rows(so) -> list[dict]:
	rows = []
	seen = {}
	for i, row in enumerate(so.items or []):
		code = str(getattr(row, "item_code", None) or "").strip()
		row_name = str(getattr(row, "name", None) or "").strip()
		idx = cint(getattr(row, "idx", None) or (i + 1))
		if row_name:
			key = row_name
		else:
			n = seen.get(code, 0) + 1
			seen[code] = n
			key = f"{code or 'line'}#{idx or n}"
		rows.append(
			{
				"key": key,
				"item_code": code,
				"item_name": str(getattr(row, "item_name", None) or code),
				"idx": idx,
				"qty": flt(getattr(row, "qty", None)),
			}
		)
	return rows


def _resolve_progress_key(token: str, rows: list[dict]) -> str | None:
	token = str(token or "").strip()
	if not token or token.lower() in ("null", "undefined", "none"):
		return None
	by_key = {r["key"]: r for r in rows}
	if token in by_key:
		return token
	matches = [r["key"] for r in rows if r["item_code"] == token]
	if len(matches) == 1:
		return matches[0]
	return None


def _progress_event(
	identity: dict,
	*,
	via: str,
	action: str,
	item_codes: list,
	detail: str = "",
	line_keys: list | None = None,
	before: dict | None = None,
	after: dict | None = None,
) -> dict:
	ev = {
		"at": cstr(frappe.utils.now()),
		"via": via,
		"employee": identity.get("employee") or "",
		"employee_name": _operator_label(identity),
		"action": action,
		"item_codes": item_codes,
		"detail": detail or "",
	}
	if line_keys:
		ev["line_keys"] = [str(k) for k in line_keys if k]
	if isinstance(before, dict) and before:
		ev["before"] = before
	if isinstance(after, dict) and after:
		ev["after"] = after
	return ev


def _armado_trip_in_route(so_name: str) -> bool:
	"""True when the SO is on a MAT that already left / finished (pack edits locked).

	``departure_time`` alone is planning metadata (every MAT has a scheduled
	08:00-style slot). Locking on it blocked weight revision as soon as ops
	assigned a MAT — even before the truck left. Only ``In Transit`` /
	``Completed`` mean the route has started / finished.
	"""
	so_name = str(so_name or "").strip()
	if not so_name:
		return False
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _tms_logistics_map_for_orders

		lg = (_tms_logistics_map_for_orders([so_name]) or {}).get(so_name) or {}
	except Exception:
		return False
	status = cstr(lg.get("trip_status") or "").strip().lower()
	# Draft / Scheduled (+ empty) stay editable for última revisión de pesajes.
	return status in ("in transit", "completed")


def _armado_has_delivery_note(so_name: str) -> bool:
	so_name = str(so_name or "").strip()
	if not so_name:
		return False
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _delivery_note_for_sales_order

		return bool(_delivery_note_for_sales_order(so_name))
	except Exception:
		return bool(
			frappe.db.exists(
				"Delivery Note Item",
				{"against_sales_order": so_name, "docstatus": ["!=", 2]},
			)
		)


def _require_armado_not_in_route(so_name: str, identity: dict) -> None:
	"""Block packer edits once the trip is in route; admin/senior PIN may proceed."""
	if not _armado_trip_in_route(so_name):
		return
	if isinstance(identity, dict) and identity.get("kind") == "admin":
		return
	frappe.throw(
		_("Armado locked: trip already in route. Use an admin PIN to amend."),
		frappe.PermissionError,
	)


def _merge_armado_progress(
	so_name: str,
	identity: dict,
	*,
	packed: dict | None = None,
	missing: dict | None = None,
	box_count: int | None = None,
	items: list | None = None,
	via: str = "armado_items",
	amend: bool = False,
) -> dict:
	"""Merge line flags. Unknown keys are ignored. Packed and missing are exclusive."""
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	rows = _so_armado_rows(so)
	progress = _load_armado_progress(so_name)
	lines = progress.setdefault("lines", {})
	events = progress.setdefault("events", [])
	by_code = {}
	if isinstance(items, list):
		for row in items:
			if isinstance(row, dict) and row.get("item_code"):
				by_code[str(row.get("item_code"))] = row

	def _snapshot_qty(entry: dict) -> dict:
		out = {}
		if entry.get("qty") is not None:
			out["qty"] = flt(entry.get("qty"))
		if entry.get("total_weight") is not None:
			out["total_weight"] = flt(entry.get("total_weight"))
		return out

	def _touch_qty(entry: dict) -> dict:
		"""Apply qty/weight from items payload; return after-snapshot."""
		src = by_code.get(entry.get("item_code") or "")
		if not isinstance(src, dict):
			return _snapshot_qty(entry)
		if src.get("qty") is not None:
			entry["qty"] = flt(src.get("qty"))
		if src.get("total_weight") is not None:
			entry["total_weight"] = flt(src.get("total_weight"))
		return _snapshot_qty(entry)

	packed_codes = []
	packed_keys = []
	unpacked_codes = []
	missing_codes = []
	unmissing_codes = []
	restamp_codes = []
	restamp_keys = []
	qty_diffs = []  # (code, key, before, after)
	keys = set()
	if isinstance(packed, dict):
		keys.update(str(k) for k in packed.keys())
	if isinstance(missing, dict):
		keys.update(str(k) for k in missing.keys())

	for token in keys:
		key = _resolve_progress_key(token, rows)
		if not key:
			continue
		row = next(r for r in rows if r["key"] == key)
		entry = lines.get(key) if isinstance(lines.get(key), dict) else _blank_progress_line(row["item_code"])
		entry["item_code"] = row["item_code"]
		before = _snapshot_qty(entry)
		want_missing = missing.get(token) if isinstance(missing, dict) and token in missing else None
		if want_missing is None and isinstance(missing, dict) and key in missing:
			want_missing = missing.get(key)
		want_packed = packed.get(token) if isinstance(packed, dict) and token in packed else None
		if want_packed is None and isinstance(packed, dict) and key in packed:
			want_packed = packed.get(key)

		if want_missing is not None and _flag_from_value(want_missing):
			entry["missing"] = True
			entry["missing_reason"] = _missing_reason_from_value(want_missing)
			entry["missing_by"] = _operator_label(identity)
			entry["missing_by_employee"] = identity.get("employee") or ""
			entry["missing_at"] = cstr(frappe.utils.now())
			entry["packed"] = False
			entry["packed_by"] = ""
			entry["packed_by_employee"] = ""
			entry["packed_at"] = ""
			missing_codes.append(row["item_code"])
		elif want_packed is not None and _flag_from_value(want_packed):
			entry["packed"] = True
			entry["packed_by"] = _operator_label(identity)
			entry["packed_by_employee"] = identity.get("employee") or ""
			entry["packed_at"] = cstr(frappe.utils.now())
			entry["missing"] = False
			entry["missing_reason"] = ""
			entry["missing_by"] = ""
			entry["missing_by_employee"] = ""
			entry["missing_at"] = ""
			packed_codes.append(row["item_code"])
			packed_keys.append(key)
		else:
			if want_packed is not None and not _flag_from_value(want_packed):
				if entry.get("packed"):
					unpacked_codes.append(row["item_code"])
				entry["packed"] = False
				entry["packed_by"] = ""
				entry["packed_by_employee"] = ""
				entry["packed_at"] = ""
			if want_missing is not None and not _flag_from_value(want_missing):
				if entry.get("missing"):
					unmissing_codes.append(row["item_code"])
				entry["missing"] = False
				entry["missing_reason"] = ""
				entry["missing_by"] = ""
				entry["missing_by_employee"] = ""
				entry["missing_at"] = ""
		after = _touch_qty(entry)
		if before != after:
			qty_diffs.append((row["item_code"], key, before, after))
		lines[key] = entry

	# Qty/weight edits on an already packed line restamp the current operator.
	if by_code:
		for row in rows:
			entry = lines.get(row["key"])
			if not isinstance(entry, dict) or not entry.get("packed"):
				continue
			if row["item_code"] not in by_code:
				continue
			if row["item_code"] in packed_codes:
				_touch_qty(entry)
				continue
			before = _snapshot_qty(entry)
			entry["packed_by"] = _operator_label(identity)
			entry["packed_by_employee"] = identity.get("employee") or ""
			entry["packed_at"] = cstr(frappe.utils.now())
			after = _touch_qty(entry)
			restamp_codes.append(row["item_code"])
			restamp_keys.append(row["key"])
			if before != after:
				qty_diffs.append((row["item_code"], row["key"], before, after))

	if box_count is not None:
		progress["boxes"] = {
			"count": int(box_count),
			"by": _operator_label(identity),
			"at": cstr(frappe.utils.now()),
		}
		events.append(
			_progress_event(
				identity,
				via="armado_boxes",
				action="boxes",
				item_codes=[],
				detail=str(int(box_count)),
			)
		)

	for action, codes, detail in (
		("pack", packed_codes, ""),
		("unpack", unpacked_codes, ""),
		("missing", missing_codes, "sin stock"),
		("unmissing", unmissing_codes, ""),
		("save", restamp_codes, ""),
	):
		if codes:
			events.append(
				_progress_event(
					identity,
					via=via,
					action=action,
					item_codes=codes,
					detail=detail,
					line_keys=packed_keys if action == "pack" else (restamp_keys if action == "save" else None),
				)
			)

	# Per-line qty/weight diffs (collaborative history).
	for code, key, before, after in qty_diffs:
		action = "amend" if amend else ("weight" if "total_weight" in after or "total_weight" in before else "qty")
		events.append(
			_progress_event(
				identity,
				via=via,
				action=action,
				item_codes=[code],
				line_keys=[key],
				before=before,
				after=after,
				detail="post-armado" if amend else "",
			)
		)

	if amend:
		progress["amended_after_pack"] = 1
		if _armado_has_delivery_note(so_name):
			progress["remito_desynced"] = 1

	progress["lines"] = lines
	progress["events"] = events
	progress["snapshot"] = [
		{"key": row["key"], "item_code": row["item_code"], "qty": flt(row["qty"]), "idx": row["idx"]}
		for row in rows
	]
	progress["last_saved_at"] = cstr(frappe.utils.now())
	progress["last_saved_by"] = _operator_label(identity)
	_save_armado_progress(so_name, progress)
	return progress


def _armado_row_fingerprint(rows: list) -> list:
	"""Stable line identity + qty. Sorted so order of the child table does not matter."""
	parts = []
	for i, row in enumerate(rows or []):
		if isinstance(row, dict):
			code = str(row.get("item_code") or "")
			key = str(row.get("key") or row.get("name") or "")
			idx = cint(row.get("idx") or (i + 1))
			qty = flt(row.get("qty"))
		else:
			code = str(getattr(row, "item_code", None) or "")
			key = str(getattr(row, "name", None) or "")
			idx = cint(getattr(row, "idx", None) or (i + 1))
			qty = flt(getattr(row, "qty", None))
		if not key:
			key = f"{code or 'line'}#{idx}"
		parts.append(f"{key}|{code}|{qty:.3f}")
	return sorted(parts)


def _armado_has_activity(progress: dict | None) -> bool:
	if not isinstance(progress, dict):
		return False
	lines = progress.get("lines") if isinstance(progress.get("lines"), dict) else {}
	for state in lines.values():
		if isinstance(state, dict) and (state.get("packed") or state.get("missing")):
			return True
	if progress.get("events"):
		return True
	return cint(((progress.get("boxes") or {}) if isinstance(progress.get("boxes"), dict) else {}).get("count")) > 0


def _armado_changed_since_save(item_rows: list, progress: dict | None) -> bool:
	"""True when someone edited the order after a packer had already saved progress."""
	if not _armado_has_activity(progress):
		return False
	current = _armado_row_fingerprint(item_rows)
	snap = (progress or {}).get("snapshot") if isinstance(progress, dict) else None
	if isinstance(snap, list) and snap:
		return current != _armado_row_fingerprint(snap)
	# Saves from before snapshots: compare qty stored on each packed/missing line.
	lines = (progress or {}).get("lines") if isinstance(progress, dict) else {}
	if not isinstance(lines, dict) or not lines:
		return False
	current_keys = set()
	for i, row in enumerate(item_rows or []):
		code = str(row.get("item_code") if isinstance(row, dict) else getattr(row, "item_code", "") or "")
		key = str(row.get("name") if isinstance(row, dict) else getattr(row, "name", "") or "")
		idx = cint(row.get("idx") if isinstance(row, dict) else getattr(row, "idx", i + 1))
		qty = flt(row.get("qty") if isinstance(row, dict) else getattr(row, "qty", 0))
		if not key:
			key = f"{code or 'line'}#{idx or i + 1}"
		current_keys.add(key)
		state = lines.get(key) if isinstance(lines.get(key), dict) else None
		if state is None:
			return True
		if state.get("qty") is not None and abs(flt(state.get("qty")) - qty) > 0.0001:
			return True
	for key, state in lines.items():
		if key not in current_keys and isinstance(state, dict) and (state.get("packed") or state.get("missing")):
			return True
	return False


def _reopen_changed_preparado(so_name: str, item_rows: list | None = None, progress: dict | None = None) -> bool:
	"""If admin edited a Preparado sheet after armado, demote back to Orden so packers can rework it.

	Returns True when a demotion ran. Safe no-op when not changed / not Preparado / non-guest.
	"""
	so_name = str(so_name or "").strip()
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return False
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	rows = item_rows if item_rows is not None else list(so.items or [])
	prog = progress if progress is not None else _load_armado_progress(so_name)
	if not _armado_changed_since_save(rows, prog):
		return False
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_display_status,
		_is_guest_preorder_sales_order,
		set_guest_preorder_status,
	)

	disp = _display_status(so)
	if disp != "Preparado":
		return False
	if not _is_guest_preorder_sales_order(so):
		# Non-guest: soft demote via ERP status marker used for Orden.
		try:
			so.db_set("status", "To Deliver and Bill", update_modified=True)
			frappe.db.commit()
			return True
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"armado reopen non-guest {so_name}")
			return False
	try:
		set_guest_preorder_status(
			so_name, "Orden", source="armado", ensure_planner_remito=0
		)
		frappe.db.commit()
		return True
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"armado reopen {so_name}")
		return False


def _demote_to_orden_for_armado(so) -> bool:
	"""Soft-demote Preparado / Delivery → Orden so Armado can Guardar after a rework.

	Returns True when the SO is Orden afterward.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_display_status,
		_is_guest_preorder_sales_order,
		set_guest_preorder_status,
	)

	disp = _display_status(so)
	base = "Completado" if str(disp).startswith("Completado") else disp
	if base == "Orden":
		return True
	if base not in ("Preparado", "Delivery"):
		return False
	try:
		if _is_guest_preorder_sales_order(so):
			set_guest_preorder_status(
				so.name, "Orden", source="armado", ensure_planner_remito=0
			)
		else:
			so.db_set("status", "To Deliver and Bill", update_modified=True)
		frappe.db.commit()
		so.reload()
		disp = _display_status(so)
		base = "Completado" if str(disp).startswith("Completado") else disp
		return base == "Orden"
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"armado demote {so.name}")
		return False


def _require_armado_writable(so) -> str:
	"""Ensure SO is Orden-stage for writes.

	Preparado / Delivery are demoted to Orden on write (re-pack after pipeline move).
	Completado / Consulta / Archivado stay blocked.

	Returns display status after any reopen. Throws if still not writable.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import _display_status

	disp = _display_status(so)
	base = "Completado" if str(disp).startswith("Completado") else disp
	if base == "Orden":
		return disp
	if base in ("Preparado", "Delivery"):
		# Prefer the existing "changed Preparado" path first, then force demote.
		if base == "Preparado":
			_reopen_changed_preparado(so.name)
			so.reload()
			disp = _display_status(so)
			base = "Completado" if str(disp).startswith("Completado") else disp
			if base == "Orden":
				return disp
		if _demote_to_orden_for_armado(so):
			return _display_status(so)
	frappe.throw(
		_("Only Orden-stage orders can be updated in armado (current: {0})").format(base)
	)


def _summarize_progress(item_rows: list, progress: dict | None) -> dict:
	lines = (progress or {}).get("lines") if isinstance(progress, dict) else {}
	if not isinstance(lines, dict):
		lines = {}
	packed = 0
	missing = 0
	qty_total = 0.0
	for i, row in enumerate(item_rows or []):
		qty_total += flt(row.get("qty") if isinstance(row, dict) else getattr(row, "qty", 0))
		code = str(row.get("item_code") if isinstance(row, dict) else getattr(row, "item_code", "") or "")
		key = str(row.get("name") if isinstance(row, dict) else getattr(row, "name", "") or "")
		idx = cint(row.get("idx") if isinstance(row, dict) else getattr(row, "idx", i + 1))
		if not key:
			key = f"{code or 'line'}#{idx or i + 1}"
		state = lines.get(key) if isinstance(lines.get(key), dict) else None
		if state is None:
			matches = [
				v
				for k, v in lines.items()
				if isinstance(v, dict) and str(v.get("item_code") or "") == code
			]
			state = matches[0] if len(matches) == 1 else None
		if isinstance(state, dict) and state.get("packed"):
			packed += 1
		elif isinstance(state, dict) and state.get("missing"):
			missing += 1
	item_count = len(item_rows or [])
	resolved = packed + missing
	pending = max(0, item_count - resolved)
	if item_count <= 0 or resolved <= 0:
		bucket = "none"
	elif resolved >= item_count:
		bucket = "ready"
	else:
		bucket = "partial"
	box_count = cint(((progress or {}).get("boxes") or {}).get("count")) if isinstance(progress, dict) else 0
	events = (progress or {}).get("events") if isinstance(progress, dict) and isinstance((progress or {}).get("events"), list) else []
	last_event = events[-1] if events else {}
	if not isinstance(last_event, dict):
		last_event = {}
	last_saved_at = ""
	last_saved_by = ""
	amended = 0
	remito_desynced = 0
	if isinstance(progress, dict):
		last_saved_at = str(progress.get("last_saved_at") or last_event.get("at") or "")
		last_saved_by = str(progress.get("last_saved_by") or last_event.get("employee_name") or "")
		amended = 1 if cint(progress.get("amended_after_pack")) else 0
		remito_desynced = 1 if cint(progress.get("remito_desynced")) else 0
	return {
		"packed": packed,
		"missing": missing,
		"pending": pending,
		"item_count": item_count,
		"qty_total": qty_total,
		"box_count": box_count,
		"bucket": bucket,
		"changed": 1 if _armado_changed_since_save(item_rows, progress) else 0,
		"amended_after_pack": amended,
		"remito_desynced": remito_desynced,
		"last_saved_at": last_saved_at,
		"last_saved_by": last_saved_by,
		"last_event": {
			"at": last_event.get("at") or "",
			"by": last_event.get("employee_name") or "",
			"action": last_event.get("action") or "",
			"item_codes": last_event.get("item_codes") or [],
		}
		if last_event
		else None,
	}


def _attach_armado_progress_summaries(orders: list, *, reopen_changed_completed: bool = False) -> None:
	"""Stamp ``progress_summary`` on order rows (Pedidos list / Armado cola).

	When ``reopen_changed_completed`` is True (Armado cola only), admin-edited
	Completados demote back to Orden.
	"""
	names = [o.get("name") for o in orders if o.get("name")]
	if not names:
		return
	item_rows = frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", names]},
		fields=["parent", "name", "item_code", "idx", "qty"],
		limit_page_length=8000,
		ignore_permissions=True,
	)
	scopes = [_armado_progress_scope(n) for n in names]
	schema_rows = frappe.get_all(
		"Table Extra Schema",
		filters={"name": ["in", scopes]},
		fields=["name", "columns_json"],
		limit_page_length=len(scopes) + 10,
		ignore_permissions=True,
	)
	prog_by_scope = {}
	for row in schema_rows:
		try:
			parsed = json.loads(row.columns_json or "{}")
		except Exception:
			parsed = {}
		prog_by_scope[row.name] = parsed if isinstance(parsed, dict) else {}
	by_parent = {}
	for row in item_rows:
		by_parent.setdefault(row.parent, []).append(row)
	for order in orders:
		rows = by_parent.get(order.get("name")) or []
		progress = prog_by_scope.get(_armado_progress_scope(order.get("name"))) or {}
		if reopen_changed_completed and cint(order.get("completed")) and _armado_changed_since_save(
			rows, progress
		):
			if _reopen_changed_preparado(order.get("name"), item_rows=rows, progress=progress):
				order["completed"] = 0
				order["status"] = "To Deliver and Bill"
				order["display_status"] = "Orden"
				progress = _load_armado_progress(order.get("name")) or progress
		summary = _summarize_progress(rows, progress)
		order["items_count"] = summary["item_count"]
		order["qty_total"] = summary["qty_total"]
		order["progress_summary"] = summary


def _attach_armado_list_progress(orders: list) -> None:
	"""Armado cola: summaries + reopen Completados that admin changed."""
	_attach_armado_progress_summaries(orders, reopen_changed_completed=True)


def _stamp_armado_sheet(detail: dict, so) -> dict:
	"""Attach stable line keys, progress, and a read-only flag for non-Orden sheets."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _display_status

	items = detail.get("items") or []
	rows = list(so.items or [])
	used = set()
	for i, it in enumerate(items):
		code = str(it.get("item_code") or "")
		match = None
		if i < len(rows) and str(getattr(rows[i], "item_code", "") or "") == code and rows[i].name not in used:
			match = rows[i]
		if match is None:
			for row in rows:
				if row.name in used:
					continue
				if str(getattr(row, "item_code", "") or "") == code:
					match = row
					break
		if match is not None:
			used.add(match.name)
			it["line_key"] = str(match.name)
		else:
			it["line_key"] = f"{code or 'line'}#{i + 1}"
	progress = _load_armado_progress(so.name)
	item_rows = list(so.items or [])
	changed = _armado_changed_since_save(item_rows, progress)
	# Admin edited a completed pack → reopen so the sheet is writable again.
	if changed and _display_status(so) == "Preparado":
		if _reopen_changed_preparado(so.name, item_rows=item_rows, progress=progress):
			so.reload()
			progress = _load_armado_progress(so.name)
			item_rows = list(so.items or [])
			changed = _armado_changed_since_save(item_rows, progress)
	detail["armado_progress"] = progress
	detail["armado_changed"] = 1 if changed else 0
	detail["armado_last_saved_at"] = progress.get("last_saved_at") or ""
	detail["armado_last_saved_by"] = progress.get("last_saved_by") or ""
	detail["armado_amended_after_pack"] = 1 if cint(progress.get("amended_after_pack")) else 0
	detail["armado_remito_desynced"] = 1 if cint(progress.get("remito_desynced")) else 0
	detail["armado_write_locked"] = 1 if _armado_trip_in_route(so.name) else 0
	disp = _display_status(so)
	detail["display_status"] = disp
	# Orden always writable. Preparado / Delivery stay editable so Guardar works when
	# ops moves Delivery → Armado (write path demotes back to Orden). Completado /
	# Consulta / Archivado stay read-only. Trip in-route locks packers (admin override).
	base = "Completado" if str(disp).startswith("Completado") else disp
	writable = base in ("Orden", "Preparado", "Delivery") or changed
	if detail["armado_write_locked"]:
		# Sheet still viewable; writes gated in update path via admin PIN.
		pass
	detail["armado_readonly"] = 0 if writable else 1
	remarks = cstr(getattr(so, "remarks", None) or "")
	detail["armado_practice"] = 1 if "armado_practice:1" in remarks else 0
	return detail


def _armado_gate_ready(so_name: str) -> dict:
	"""Raise a controlled error unless every current line is packed or missing and boxes are 1–99."""
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	rows = _so_armado_rows(so)
	if not rows:
		frappe.throw(_("Order has no items"))
	progress = _load_armado_progress(so_name)
	lines = progress.get("lines") or {}
	pending = []
	for row in rows:
		state = lines.get(row["key"]) if isinstance(lines.get(row["key"]), dict) else {}
		if not (state.get("packed") or state.get("missing")):
			pending.append(row["item_code"] or row["key"])
	if pending:
		frappe.throw(
			_("Armado incomplete: {0}").format(", ".join(pending[:12]))
		)
	box_count = cint((progress.get("boxes") or {}).get("count"))
	if box_count < 1 or box_count > 99:
		frappe.throw(_("Box count must be between 1 and 99"))
	return progress


def _armado_progress_public(so_name: str) -> dict | None:
	so_name = str(so_name or "").strip()
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return None
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	rows = _so_armado_rows(so)
	progress = _load_armado_progress(so_name)
	lines = progress.get("lines") or {}
	out_lines = []
	for row in rows:
		state = lines.get(row["key"]) if isinstance(lines.get(row["key"]), dict) else {}
		qty_measured = state.get("qty")
		tw = state.get("total_weight")
		out_lines.append(
			{
				"line_key": row["key"],
				"item_code": row["item_code"],
				"item_name": row["item_name"],
				"ordered_qty": flt(row["qty"]),
				"qty": flt(qty_measured) if qty_measured is not None else None,
				"total_weight": flt(tw) if tw is not None else None,
				"packed": 1 if state.get("packed") else 0,
				"missing": 1 if state.get("missing") else 0,
				"missing_reason": state.get("missing_reason") or "",
				"packed_by": state.get("packed_by") or "",
				"packed_at": state.get("packed_at") or "",
				"missing_by": state.get("missing_by") or "",
				"missing_at": state.get("missing_at") or "",
			}
		)
	summary = _summarize_progress(
		[{"name": r["key"], "item_code": r["item_code"], "idx": r["idx"], "qty": r["qty"]} for r in rows],
		progress,
	)
	from erpnext.erpnext_integrations.ecommerce_api.api import _display_status

	return {
		"sales_order": so_name,
		"display_status": _display_status(so),
		"box_count": summary["box_count"],
		"packed": summary["packed"],
		"missing": summary["missing"],
		"pending": summary.get("pending") or 0,
		"item_count": summary["item_count"],
		"bucket": summary["bucket"],
		"complete": 1 if summary["bucket"] == "ready" else 0,
		"changed": summary.get("changed") or 0,
		"amended_after_pack": summary.get("amended_after_pack") or 0,
		"remito_desynced": summary.get("remito_desynced") or 0,
		"write_locked": 1 if _armado_trip_in_route(so_name) else 0,
		"last_saved_at": summary.get("last_saved_at") or "",
		"last_saved_by": summary.get("last_saved_by") or "",
		"last_event": summary.get("last_event"),
		"lines": out_lines,
		"events": (progress.get("events") or [])[-ARMADO_PROGRESS_EVENT_CAP:],
	}


def _truthy_flag(raw) -> bool:
	if raw in (None, "", 0, False, "0", "false", "False", "null", "undefined", "none", "no"):
		return False
	if isinstance(raw, str) and raw.strip().lower() in ("", "0", "false", "null", "undefined", "none", "no"):
		return False
	return True


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
			"status": [
				"not in",
				["Closed", "Completed", "Preparado", "Delivery", "En Delivery", "Cancelled", "Consulta"],
			],
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
def list_armado_orders(delivery_date=None, limit=80, include_completed=None):
	"""Orders for the armado kiosk.

	Only **Orden**-stage open orders (not Consulta drafts, not already Preparado /
	Delivery / Completado). ``delivery_date`` empty / null / ``"all"`` → no date
	filter (default). Otherwise only orders due that day.
	Also returns ``delivery_dates`` (distinct due dates among Orden-stage armado
	orders) so the UI can offer a day picker.

	``include_completed`` also returns Preparado / Delivery sheets touched
	**today** with ``completed: 1`` (still amendable until the MAT is In Transit /
	pipeline Completado — última revisión de pesajes). Each row includes
	``progress_summary``.
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
		"status": [
			"not in",
			["Closed", "Completed", "Preparado", "Delivery", "En Delivery", "Cancelled", "Consulta"],
		],
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
			"status": [
				"not in",
				["Closed", "Completed", "Preparado", "Delivery", "En Delivery", "Cancelled", "Consulta"],
			],
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
				"items_count": 0,
				"qty_total": 0,
				"completed": 0,
				"practice": 1 if "armado_practice:1" in tag else 0,
			}
		)
		if len(out) >= limit:
			break

	if _truthy_flag(include_completed):
		# Completados chip = armado done today (Preparado + Delivery) so ops can
		# reopen for última revisión de pesajes before the MAT leaves.
		day = str(today())
		done_filters = {
			"docstatus": 1,
			"status": ["in", ["Preparado", "Delivery", "En Delivery"]],
			"modified": [">=", f"{day} 00:00:00"],
		}
		if due is not None:
			done_filters["delivery_date"] = due
		done_rows = frappe.get_all(
			"Sales Order",
			filters=done_filters,
			fields=fields,
			order_by="modified desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
		seen = {o["name"] for o in out}
		for r in done_rows:
			if r.name in seen:
				continue
			disp = _display_status_from_row(
				r.docstatus, r.status, r.grand_total, getattr(r, "advance_paid", 0)
			)
			base_disp = "Delivery" if str(disp).startswith("Delivery") else disp
			if base_disp not in ("Preparado", "Delivery"):
				continue
			tag = str(r.get(tag_field) or "") if tag_field else ""
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
					"is_guest": "guest_preorder=1" in tag,
					"ean13": armado_ean13_for_order(r.name),
					"items_count": 0,
					"qty_total": 0,
					"completed": 1,
					"modified": str(getattr(r, "modified", "") or ""),
					"practice": 1 if "armado_practice:1" in tag else 0,
				}
			)
			if len(out) >= limit * 2:
				break

	_attach_armado_list_progress(out)
	# Prefer armado confirm/save stamp when present; drop Completados not from today.
	day = getdate(today())
	filtered = []
	for order in out:
		if not cint(order.get("completed")):
			filtered.append(order)
			continue
		stamp = (
			((order.get("progress_summary") or {}) if isinstance(order.get("progress_summary"), dict) else {}).get(
				"last_saved_at"
			)
			or order.get("modified")
			or ""
		)
		try:
			if stamp and getdate(stamp) == day:
				filtered.append(order)
		except Exception:
			pass
	out = filtered
	return {
		"ok": True,
		"delivery_date": "" if due is None else str(due),
		"delivery_dates": delivery_dates,
		"orders": out,
	}


def _armado_access_secret() -> str:
	"""Site-local theater secret — not high security (sheet has no sensitive PII beyond order lines)."""
	try:
		from frappe.utils.password import get_encryption_key

		key = str(get_encryption_key() or "")
	except Exception:
		key = str((frappe.conf or {}).get("encryption_key") or "")
	site = getattr(frappe.local, "site", None) or "site"
	return f"armado-access|{site}|{key}"


def _armado_access_sig(preorder_name: str, exp: int) -> str:
	msg = f"{preorder_name}|{int(exp)}|armado_access".encode("utf-8")
	digest = hmac.new(
		_armado_access_secret().encode("utf-8"),
		msg,
		hashlib.sha256,
	).hexdigest()
	return digest[:16]


def _build_armado_access_token(preorder_name: str, *, ttl: int | None = None) -> dict:
	ttl_i = int(ttl if ttl is not None else ARMADO_ACCESS_TTL_SECONDS)
	if ttl_i < 60:
		ttl_i = 60
	if ttl_i > 86400:
		ttl_i = 86400
	exp = int(time.time()) + ttl_i
	sig = _armado_access_sig(preorder_name, exp)
	return {
		"token": f"{exp}.{sig}",
		"expires_at": exp,
		"ttl_seconds": ttl_i,
		"preorder_name": preorder_name,
	}


def verify_armado_access_payload(preorder_name=None, token=None) -> dict:
	"""Validate Pedidos→Armado deep-link token. Always returns a controlled dict (no throw)."""
	name = str(preorder_name or "").strip()
	raw = str(token or "").strip()
	if not name or not raw or raw in ("null", "undefined"):
		return {"ok": True, "valid": 0, "reason": "missing"}
	parts = raw.split(".", 1)
	if len(parts) != 2:
		return {"ok": True, "valid": 0, "reason": "malformed"}
	try:
		exp = int(parts[0])
	except Exception:
		return {"ok": True, "valid": 0, "reason": "malformed"}
	sig = str(parts[1] or "").strip().lower()
	if not sig or exp <= 0:
		return {"ok": True, "valid": 0, "reason": "malformed"}
	now = int(time.time())
	if exp < now:
		return {
			"ok": True,
			"valid": 0,
			"reason": "expired",
			"expires_at": exp,
			"preorder_name": name,
		}
	expected = _armado_access_sig(name, exp)
	if not hmac.compare_digest(expected, sig):
		return {
			"ok": True,
			"valid": 0,
			"reason": "bad_sig",
			"expires_at": exp,
			"preorder_name": name,
		}
	if not frappe.db.exists("Sales Order", name):
		return {
			"ok": True,
			"valid": 0,
			"reason": "not_found",
			"expires_at": exp,
			"preorder_name": name,
		}
	return {
		"ok": True,
		"valid": 1,
		"expires_at": exp,
		"preorder_name": name,
		"ttl_remaining": max(0, exp - now),
	}


@frappe.whitelist(allow_guest=True)
def mint_armado_access_link(preorder_name=None, ttl_seconds=None):
	"""Mint a ~1h deep-link so /armado can open this SO outside date/status list filters.

	Theater HMAC only — Pedidos already gates staff; the link just looks signed.
	"""
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or preorder_name in ("null", "undefined"):
		frappe.throw(_("Order name required"))
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Order not found"))
	ttl = None
	if ttl_seconds not in (None, "", "null", "undefined"):
		ttl = cint(ttl_seconds)
	built = _build_armado_access_token(preorder_name, ttl=ttl)
	path = f"/armado?order={quote(preorder_name, safe='')}&t={built['token']}"
	return {"ok": True, **built, "path": path}


@frappe.whitelist(allow_guest=True)
def verify_armado_access_token(preorder_name=None, token=None):
	"""Validate Pedidos→Armado deep-link token (controlled ok/valid dict — never TypeError)."""
	return verify_armado_access_payload(preorder_name, token)


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
	codes = [
		str(it.get("item_code") or "").strip()
		for it in (detail.get("items") or [])
		if it.get("item_code")
	]
	images = {}
	if codes:
		for r in frappe.get_all(
			"Item",
			filters={"name": ["in", codes]},
			fields=["name", "image"],
			ignore_permissions=True,
		):
			if r.image:
				images[r.name] = r.image
	for it in detail.get("items") or []:
		loc = (sku_to_loc.get(it.get("item_code")) or {}).get("location") or ""
		it["location"] = loc
		code = str(it.get("item_code") or "").strip()
		if code and not it.get("image"):
			it["image"] = images.get(code) or ""

	# Product barcodes so the armado sheet can +1 qty on scan.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _attach_barcodes

		_attach_barcodes(detail.get("items") or [])
	except Exception:
		for it in detail.get("items") or []:
			it.setdefault("barcodes", [])

	detail["ean13"] = armado_ean13_for_order(so.name)
	detail["armado_notes"] = _load_armado_notes(so.name)
	_stamp_armado_sheet(detail, so)
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
def save_armado_boxes(preorder_name=None, box_count=None, pin=None):
	"""Store how many boxes were used to pack this order. Does not promote pipeline."""
	identity = _require_ops_operator(pin)
	count = _parse_box_count(box_count, required=True)
	preorder_name = str(preorder_name or "").strip()
	if (
		not preorder_name
		or preorder_name.lower() in ("null", "undefined", "none")
		or not frappe.db.exists("Sales Order", preorder_name)
	):
		frappe.throw(_("Order not found"))
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	_require_armado_writable(so)
	_merge_armado_progress(preorder_name, identity, box_count=count, via="armado_boxes")
	_log_armado_operator(preorder_name, identity, via="armado_boxes")
	return {
		"ok": True,
		"box_count": count,
		"operator": {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		},
	}


@frappe.whitelist(allow_guest=True)
def get_armado_progress(preorder_name=None):
	"""Read-only pack history for Delivery and the armado sheet."""
	preorder_name = str(preorder_name or "").strip()
	if not preorder_name or preorder_name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Order name required"))
	progress = _armado_progress_public(preorder_name)
	if not progress:
		frappe.throw(_("Order not found"))
	return {"ok": True, "progress": progress}


@frappe.whitelist(allow_guest=True)
def sync_armado_remito(preorder_name=None, pin=None):
	"""Re-run planner remito after a post-armado qty/peso amend; clear desync flag."""
	identity = _require_ops_operator(pin)
	preorder_name = str(preorder_name or "").strip()
	if (
		not preorder_name
		or preorder_name.lower() in ("null", "undefined", "none")
		or not frappe.db.exists("Sales Order", preorder_name)
	):
		frappe.throw(_("Order not found"))
	_require_armado_not_in_route(preorder_name, identity)

	gate = {"ok": 0, "delivery_note": None}
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _ensure_planner_delivery_note

		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", preorder_name)
		gate = _ensure_planner_delivery_note(so) or {}
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"sync_armado_remito {preorder_name}")
		frappe.throw(_("Could not sync remito after armado amend"))

	progress = _load_armado_progress(preorder_name)
	progress["remito_desynced"] = 0
	events = progress.setdefault("events", [])
	events.append(
		_progress_event(
			identity,
			via="armado_remito_sync",
			action="remito_sync",
			item_codes=[],
			detail=cstr((gate or {}).get("delivery_note") or ""),
		)
	)
	progress["last_saved_at"] = cstr(frappe.utils.now())
	progress["last_saved_by"] = _operator_label(identity)
	_save_armado_progress(preorder_name, progress)
	return {
		"ok": True,
		"delivery_note": (gate or {}).get("delivery_note"),
		"progress": _armado_progress_public(preorder_name),
		"operator": {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		},
	}


@frappe.whitelist(allow_guest=True)
def update_armado_items(
	preorder_name=None, items=None, pin=None, packed=None, missing=None, box_count=None
):
	"""Update qty/weight and per-line pack/faltante progress. Does not promote pipeline.

	``packed`` / ``missing`` maps merge by line key (or unique item code). Unknown keys
	are ignored. A non-map payload is a controlled error. Unchanged Preparado orders
	reject; admin-changed Preparado sheets reopen to Orden first.
	"""
	identity = _require_ops_operator(pin)
	packed_map = _coerce_progress_map(packed, "packed")
	missing_map = _coerce_progress_map(missing, "missing")
	boxes_n = _parse_box_count(box_count)

	preorder_name = str(preorder_name or "").strip()
	if (
		not preorder_name
		or preorder_name.lower() in ("null", "undefined", "none")
		or not frappe.db.exists("Sales Order", preorder_name)
	):
		frappe.throw(_("Order not found"))

	if isinstance(items, str):
		text = items.strip()
		if text.lower() in ("", "null", "undefined", "none"):
			items = None
		else:
			try:
				items = json.loads(text)
			except Exception:
				frappe.throw(_("Items required"))
	has_items = isinstance(items, list) and bool(items)
	if not has_items and packed_map is None and missing_map is None and boxes_n is None:
		frappe.throw(_("Items required"))
	if not has_items:
		items = None

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_display_status,
		_is_guest_preorder_sales_order,
		update_guest_preorder_items,
	)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	_require_armado_not_in_route(preorder_name, identity)
	disp_before = _display_status(so)
	base_before = "Completado" if str(disp_before).startswith("Completado") else disp_before
	amend = base_before in ("Preparado", "Delivery")
	_require_armado_writable(so)

	if has_items:
		if _is_guest_preorder_sales_order(so):
			update_guest_preorder_items(preorder_name, items)
		else:
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

	if packed_map is not None or missing_map is not None or boxes_n is not None or has_items:
		_merge_armado_progress(
			preorder_name,
			identity,
			packed=packed_map,
			missing=missing_map,
			box_count=boxes_n,
			items=items if has_items else None,
			via="armado_items",
			amend=amend,
		)
	_log_armado_operator(preorder_name, identity, via="armado_items")
	out = get_armado_order(preorder_name)
	if isinstance(out, dict):
		out["operator"] = {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		}
	return out


@frappe.whitelist(allow_guest=True)
def confirm_armado(preorder_name=None, items=None, pin=None, packed=None, missing=None, box_count=None):
	"""Save armado quantities (optional) and move pipeline Orden → Preparado (armado).

	Every current line must be packed or marked missing, and ``box_count`` must be
	1–99 (already saved, or sent on this call). Rejects non-Orden stages.

	Planner remito/geocode is enqueued (not inline) so the kiosk confirm button
	returns quickly — same pattern as create_guest_preorder → Orden.
	"""
	identity = _require_ops_operator(pin)
	# Validate map/box types before the order lookup so dirty payloads stay controlled.
	_coerce_progress_map(packed, "packed")
	_coerce_progress_map(missing, "missing")
	_parse_box_count(box_count)

	preorder_name = str(preorder_name or "").strip()
	if (
		not preorder_name
		or preorder_name.lower() in ("null", "undefined", "none")
		or not frappe.db.exists("Sales Order", preorder_name)
	):
		frappe.throw(_("Order not found"))

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_display_status,
		_is_guest_preorder_sales_order,
		set_guest_preorder_status,
	)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	frappe.flags.ignore_permissions = False

	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Only guest preorders can be confirmed in armado"))

	_require_armado_not_in_route(preorder_name, identity)

	# Admin edited after pack → reopen so confirm can run again.
	_reopen_changed_preparado(so.name)
	so.reload()

	current = _display_status(so)
	base = "Completado" if str(current).startswith("Completado") else current
	if base == "Preparado":
		# Idempotent: already armado and unchanged — return sheet without regressing.
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

	# Persist qty / pack / faltante / boxes before the gate. Already-Preparado returned above.
	has_sheet = items not in (None, "", "null")
	if has_sheet or packed not in (None, "") or missing not in (None, "") or box_count not in (None, ""):
		update_armado_items(
			preorder_name=preorder_name,
			items=items,
			pin=pin,
			packed=packed,
			missing=missing,
			box_count=box_count,
		)

	progress = _armado_gate_ready(preorder_name)
	progress.setdefault("events", []).append(
		_progress_event(
			identity,
			via="armado_confirm",
			action="confirm",
			item_codes=[],
			detail=str(cint((progress.get("boxes") or {}).get("count"))),
		)
	)
	_save_armado_progress(preorder_name, progress)

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

	# i060 Part B: feed weight catalog from measured kg (never fails confirm).
	weight_obs = {"ok": True, "recorded": [], "skipped": [], "errors": []}
	try:
		from erpnext.erpnext_integrations.ecommerce_api.weight_api import (
			soft_record_armado_weights,
		)

		obs_items = items if isinstance(items, list) else None
		if not obs_items:
			# Fall back to SO lines when confirm payload omitted weights.
			obs_items = []
			frappe.flags.ignore_permissions = True
			so_obs = frappe.get_doc("Sales Order", name)
			frappe.flags.ignore_permissions = False
			for row in so_obs.get("items") or []:
				obs_items.append(
					{
						"item_code": row.item_code,
						"qty": row.qty,
						"total_weight": getattr(row, "total_weight", None),
					}
				)
		weight_obs = soft_record_armado_weights(reference_name=name, items=obs_items, source="armado")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "confirm_armado weight obs")

	# Remito + geocode — skip practice / scratch sessions (i060 Part D).
	is_practice = False
	try:
		remarks = cstr(frappe.db.get_value("Sales Order", name, "remarks") or "")
		is_practice = "armado_practice:1" in remarks
	except Exception:
		is_practice = False

	remito_job = {"queued": 0}
	if not is_practice:
		try:
			from erpnext.erpnext_integrations.ecommerce_api.api import _enqueue_planner_remito

			remito_job = _enqueue_planner_remito(name)
		except Exception:
			# Promotion already committed. A missing enqueue helper must not roll the kiosk back.
			frappe.log_error(frappe.get_traceback(), "confirm_armado remito enqueue")
	# Light payload — skip get_armado_order floor-map rebuild; UI drops the sheet.
	order = dict(detail) if isinstance(detail, dict) else {"name": name}
	order["ean13"] = armado_ean13_for_order(name)
	order["display_status"] = order.get("display_status") or "Preparado"
	order["status"] = order.get("status") or "Preparado"
	return {
		"ok": True,
		"already_confirmed": 0,
		"remito_queued": 1 if remito_job.get("queued") else 0,
		"practice": 1 if is_practice else 0,
		"weight_observations": weight_obs,
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

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_guest_preorder_line_amount,
		_item_line_weight_fields,
	)

	frappe.flags.ignore_permissions = True
	dn = frappe.get_doc("Delivery Note", delivery_note)
	frappe.flags.ignore_permissions = False
	items = []
	for d in dn.items or []:
		qty = flt(d.qty)
		rate = flt(getattr(d, "rate", None) or 0)
		# Agreed line total: WEIGHT is rate×kg (total_weight), never rate×units.
		# Falling back to stored amount / rate×qty only when weight math is empty.
		agreed = flt(_guest_preorder_line_amount(d))
		if agreed <= 0:
			agreed = flt(getattr(d, "amount", None) or (rate * qty))
		weight = _item_line_weight_fields(
			d.item_code,
			line_uom=getattr(d, "uom", None),
			line_weight_per_unit=getattr(d, "weight_per_unit", None),
			line_total_weight=getattr(d, "total_weight", None),
			line_qty=qty,
		)
		items.append(
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": qty,
				"uom": d.uom,
				"rate": rate,
				"amount": agreed,
				"total_weight": weight.get("total_weight"),
				"weight_per_unit": weight.get("weight_per_unit"),
				"is_weight_based": bool(weight.get("is_weight_based")),
				"against_sales_order": getattr(d, "against_sales_order", None),
			}
		)
	sales_order = ""
	for row in items:
		sales_order = str(row.get("against_sales_order") or "").strip()
		if sales_order:
			break
	armado_progress = _armado_progress_public(sales_order) if sales_order else None
	return {
		"ok": True,
		"delivery_note": dn.name,
		"customer": dn.customer,
		"customer_name": dn.customer_name or dn.customer,
		"sales_order": sales_order or None,
		"armado_progress": armado_progress,
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


def _sanitize_armado_ref(raw) -> str:
	text = cstr(raw or "").strip()
	if not text or text.lower() in ("null", "undefined", "none"):
		return ""
	# Keep short printable refs for remarks tags.
	return "".join(ch for ch in text[:80] if ch.isalnum() or ch in "-_/#. ")


@frappe.whitelist(allow_guest=True)
def create_armado_scratch_order(
	mode=None,
	customer=None,
	reference=None,
	items=None,
	pin=None,
	guest_name=None,
):
	"""Create Orden-stage sheet for Armado outside an existing pedido (i060 Part D).

	``mode=practice`` → tagged ``armado_practice:1`` (no remito on confirm).
	``mode=temporal`` → real guest preorder Orden for a customer (or Consumidor Final).
	Optional ``reference`` stored as ``armado_ref:…`` in remarks/guest_notes.
	"""
	identity = _require_ops_operator(pin)
	mode_s = cstr(mode or "practice").strip().lower()
	if mode_s not in ("practice", "temporal"):
		mode_s = "practice"

	if isinstance(items, str):
		try:
			items = json.loads(items) if items.strip() else []
		except Exception:
			items = []
	if not isinstance(items, list):
		items = []

	clean_items = []
	for row in items[:200]:
		if not isinstance(row, dict):
			continue
		code = cstr(row.get("item_code") or "").strip()
		if not code or code.lower() in ("null", "undefined", "none"):
			continue
		qty = flt(row.get("qty") or 1)
		if qty <= 0:
			qty = 1
		clean_items.append(
			{
				"item_code": code,
				"qty": qty,
				"rate": flt(row.get("rate") or 0),
				"total_weight": row.get("total_weight"),
				"weight_per_unit": row.get("weight_per_unit"),
			}
		)
	if not clean_items:
		frappe.throw(_("Add at least one product"))

	ref = _sanitize_armado_ref(reference)
	note_parts = []
	if mode_s == "practice":
		note_parts.append("armado_practice:1")
	if ref:
		note_parts.append(f"armado_ref:{ref}")
	guest_notes = " ".join(note_parts)

	cust = cstr(customer or "").strip()
	if cust.lower() in ("null", "undefined", "none"):
		cust = ""
	if cust and not frappe.db.exists("Customer", cust):
		frappe.throw(_("Customer {0} not found").format(cust))

	name_hint = cstr(guest_name or "").strip()
	if name_hint.lower() in ("null", "undefined", "none"):
		name_hint = ""
	if mode_s == "practice" and not name_hint:
		name_hint = "Práctica Armado"
		if ref:
			name_hint = f"{name_hint} ({ref})"

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		create_guest_preorder,
		set_guest_preorder_status,
	)

	# Create as Consulta first so create-side-effects do not enqueue a remito;
	# then promote to Orden with ensure_planner_remito=0 (practice never remitos).
	created = create_guest_preorder(
		items=clean_items,
		customer=cust or None,
		guest_name=name_hint or None,
		guest_notes=guest_notes or None,
		initial_status=None,
		is_delivery=0,
		send_client_pin=0,
	)
	preorder_name = (
		(created or {}).get("preorder_name")
		or (created or {}).get("name")
		or ""
	)
	if not preorder_name:
		frappe.throw(_("Could not create armado scratch order"))

	# Ensure practice / ref tags survive on remarks.
	try:
		remarks = cstr(frappe.db.get_value("Sales Order", preorder_name, "remarks") or "")
		need = []
		if mode_s == "practice" and "armado_practice:1" not in remarks:
			need.append("armado_practice:1")
		if ref and f"armado_ref:{ref}" not in remarks:
			need.append(f"armado_ref:{ref}")
		if need:
			frappe.db.set_value(
				"Sales Order",
				preorder_name,
				"remarks",
				(remarks + " " + " ".join(need)).strip(),
				update_modified=False,
			)
			frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "create_armado_scratch_order tags")

	try:
		set_guest_preorder_status(
			preorder_name,
			"Orden",
			source="armado",
			ensure_planner_remito=0,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "create_armado_scratch_order promote")
		frappe.throw(_("Created draft but could not promote to Orden"))

	_log_armado_operator(preorder_name, identity, via="armado_scratch")
	out = get_armado_order(preorder_name)
	order = out.get("order") if isinstance(out, dict) else None
	return {
		"ok": True,
		"mode": mode_s,
		"practice": 1 if mode_s == "practice" else 0,
		"reference": ref or None,
		"preorder_name": preorder_name,
		"order": order,
		"operator": {
			"kind": identity.get("kind"),
			"employee": identity.get("employee"),
			"employee_name": identity.get("employee_name"),
		},
	}
