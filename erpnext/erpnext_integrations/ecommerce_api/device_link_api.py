"""Permanent app-link token + connected device registry (mobile / desktop).

Token is stored encrypted in Table Extra Schema (no new DocType / migrate).
Devices are rows in Table Extra Data, one per device_id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.password import decrypt, encrypt

TOKEN_SCOPE = "settings.app_link_token"
DEVICE_SCOPE = "settings.connected_devices"
TOKEN_PREFIX = "erp"

ALLOWED_TYPES = ("mobile", "desktop", "other")
ALLOWED_PLATFORMS = ("ios", "android", "windows", "macos", "linux", "web", "other")


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _now_iso() -> str:
	return str(now_datetime())


def _request_ip() -> str | None:
	try:
		return frappe.local.request_ip
	except Exception:
		return None


def _hash_secret(secret: str) -> str:
	return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _load_token_doc():
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", TOKEN_SCOPE):
		return frappe.get_doc("Table Extra Schema", TOKEN_SCOPE)
	return None


def _load_token_store() -> dict:
	doc = _load_token_doc()
	if not doc:
		return {}
	data = _parse_json(doc.columns_json, {})
	return data if isinstance(data, dict) else {}


def _save_token_store(data: dict) -> None:
	payload = json.dumps(data or {}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", TOKEN_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", TOKEN_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{
				"doctype": "Table Extra Schema",
				"scope": TOKEN_SCOPE,
				"columns_json": payload,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def _format_token(key_id: str, secret: str) -> str:
	return f"{TOKEN_PREFIX}_{key_id}_{secret}"


def _parse_token(raw: str) -> tuple[str, str] | None:
	token = (raw or "").strip()
	if token.lower().startswith("bearer "):
		token = token[7:].strip()
	parts = token.split("_")
	if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
		return None
	key_id, secret = parts[1], parts[2]
	if not key_id or not secret:
		return None
	return key_id, secret


def _new_token_store() -> dict:
	key_id = secrets.token_hex(8)
	secret = secrets.token_hex(16)
	return {
		"key_id": key_id,
		"secret_hash": _hash_secret(secret),
		"secret_enc": encrypt(secret),
		"created_at": _now_iso(),
		"created_by": frappe.session.user if frappe.session.user != "Guest" else "Administrator",
		"_plain_secret": secret,
	}


def _ensure_token_store() -> dict:
	store = _load_token_store()
	if store.get("key_id") and store.get("secret_hash") and store.get("secret_enc"):
		return store
	fresh = _new_token_store()
	plain = fresh.pop("_plain_secret")
	_save_token_store(fresh)
	fresh["_plain_secret"] = plain
	return fresh


def _reveal_secret(store: dict) -> str:
	if store.get("_plain_secret"):
		return str(store["_plain_secret"])
	enc = store.get("secret_enc")
	if not enc:
		frappe.throw(_("Link token is missing. Rotate it to create a new one."))
	return decrypt(enc)


def verify_link_token(token: str) -> bool:
	parsed = _parse_token(token)
	if not parsed:
		return False
	key_id, secret = parsed
	store = _load_token_store()
	if not store or store.get("key_id") != key_id:
		return False
	expected = store.get("secret_hash") or ""
	return hmac.compare_digest(expected, _hash_secret(secret))


def _require_link_token(token: str) -> None:
	if not verify_link_token(token):
		frappe.throw(_("Invalid or expired app link token"), frappe.AuthenticationError)


def _serialize_device(row_key: str, data: dict) -> dict:
	return {
		"id": data.get("id") or row_key,
		"label": data.get("label") or row_key,
		"device_type": data.get("device_type") or "other",
		"platform": data.get("platform") or "other",
		"app_name": data.get("app_name") or None,
		"app_version": data.get("app_version") or None,
		"user": data.get("user") or None,
		"first_seen": data.get("first_seen"),
		"last_sync": data.get("last_sync"),
		"last_seen": data.get("last_seen"),
		"ip": data.get("ip"),
		"revoked": bool(data.get("revoked")),
	}


def _get_device_row(device_id: str) -> tuple[str | None, dict]:
	name = frappe.db.get_value(
		"Table Extra Data",
		{"scope": DEVICE_SCOPE, "row_key": device_id},
		"name",
	)
	if not name:
		return None, {}
	raw = frappe.db.get_value("Table Extra Data", name, "data_json")
	data = _parse_json(raw, {})
	return name, data if isinstance(data, dict) else {}


def _upsert_device(device_id: str, patch: dict) -> dict:
	device_id = (device_id or "").strip()
	if not device_id:
		frappe.throw(_("device_id is required"))
	name, current = _get_device_row(device_id)
	now = _now_iso()
	if not current:
		current = {
			"id": device_id,
			"first_seen": now,
			"revoked": False,
		}
	current.update({k: v for k, v in patch.items() if v is not None})
	current["id"] = device_id
	current["last_seen"] = now
	if not current.get("ip"):
		current["ip"] = _request_ip()
	payload = json.dumps(current, ensure_ascii=False, default=str)
	frappe.flags.ignore_permissions = True
	if name:
		frappe.db.set_value("Table Extra Data", name, "data_json", payload)
	else:
		frappe.get_doc(
			{
				"doctype": "Table Extra Data",
				"scope": DEVICE_SCOPE,
				"row_key": device_id,
				"data_json": payload,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	return _serialize_device(device_id, current)


@frappe.whitelist()
def get_app_link():
	"""Return the permanent app-link token (Settings, admin UI). Creates one if missing."""
	store = _ensure_token_store()
	secret = _reveal_secret(store)
	token = _format_token(store["key_id"], secret)
	return {
		"token": token,
		"key_id": store.get("key_id"),
		"created_at": store.get("created_at"),
		"created_by": store.get("created_by"),
		"api_path": "/api/app/pm",
	}


@frappe.whitelist()
def rotate_app_link():
	"""Replace the link token. Already-paired apps must paste the new key."""
	fresh = _new_token_store()
	plain = fresh.pop("_plain_secret")
	_save_token_store(fresh)
	return {
		"token": _format_token(fresh["key_id"], plain),
		"key_id": fresh["key_id"],
		"created_at": fresh["created_at"],
		"created_by": fresh["created_by"],
		"api_path": "/api/app/pm",
		"rotated": True,
	}


@frappe.whitelist(allow_guest=True)
def verify_app_link_token(token=None):
	ok = verify_link_token(token or "")
	if not ok:
		frappe.throw(_("Invalid or expired app link token"), frappe.AuthenticationError)
	return {"ok": True}


@frappe.whitelist()
def list_connected_devices():
	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": DEVICE_SCOPE},
		fields=["row_key", "data_json"],
		ignore_permissions=True,
	)
	devices = []
	for row in rows:
		data = _parse_json(row.data_json, {})
		if not isinstance(data, dict):
			continue
		devices.append(_serialize_device(row.row_key, data))
	devices.sort(key=lambda d: d.get("last_seen") or d.get("last_sync") or "", reverse=True)
	return {"devices": devices, "total": len(devices)}


@frappe.whitelist(allow_guest=True)
def register_connected_device(
	device_id=None,
	label=None,
	device_type=None,
	platform=None,
	app_name=None,
	app_version=None,
	user=None,
	link_token=None,
	require_token=0,
):
	"""Upsert a device. Mobile/desktop apps must pass a valid link_token."""
	if cint(require_token) or frappe.session.user == "Guest":
		_require_link_token(link_token or "")
	dtype = (device_type or "other").strip().lower()
	if dtype not in ALLOWED_TYPES:
		dtype = "other"
	plat = (platform or "other").strip().lower()
	if plat not in ALLOWED_PLATFORMS:
		plat = "other"
	name, current = _get_device_row((device_id or "").strip())
	if current.get("revoked"):
		frappe.throw(_("This device was revoked. Generate a new device id or ask an admin to restore it."))
	patch = {
		"label": (label or current.get("label") or device_id or "").strip() or device_id,
		"device_type": dtype,
		"platform": plat,
		"app_name": (app_name or current.get("app_name") or "").strip() or None,
		"app_version": (app_version or current.get("app_version") or "").strip() or None,
		"user": (user or current.get("user") or (None if frappe.session.user in (None, "Guest") else frappe.session.user)),
		"ip": _request_ip(),
		"revoked": False,
	}
	# first registration counts as a sync so the row is not empty
	if not current.get("last_sync"):
		patch["last_sync"] = _now_iso()
	device = _upsert_device(device_id, patch)
	return {"ok": True, "device": device}


@frappe.whitelist(allow_guest=True)
def ping_connected_device(
	device_id=None,
	link_token=None,
	require_token=0,
	app_version=None,
	synced=1,
):
	"""Heartbeat / last-sync ping from a paired app or this web POS."""
	if cint(require_token) or frappe.session.user == "Guest":
		_require_link_token(link_token or "")
	device_id = (device_id or "").strip()
	if not device_id:
		frappe.throw(_("device_id is required"))
	_name, current = _get_device_row(device_id)
	if not current:
		frappe.throw(_("Unknown device. Register it first."))
	if current.get("revoked"):
		frappe.throw(_("This device was revoked"), frappe.AuthenticationError)
	patch = {"ip": _request_ip()}
	if app_version:
		patch["app_version"] = str(app_version).strip()
	if cint(synced):
		patch["last_sync"] = _now_iso()
	device = _upsert_device(device_id, patch)
	return {"ok": True, "device": device}


@frappe.whitelist()
def revoke_connected_device(device_id=None):
	device_id = (device_id or "").strip()
	if not device_id:
		frappe.throw(_("device_id is required"))
	name, current = _get_device_row(device_id)
	if not current:
		frappe.throw(_("Device {0} not found").format(device_id))
	device = _upsert_device(device_id, {"revoked": True})
	return {"ok": True, "device": device}


@frappe.whitelist()
def restore_connected_device(device_id=None):
	device_id = (device_id or "").strip()
	if not device_id:
		frappe.throw(_("device_id is required"))
	name, current = _get_device_row(device_id)
	if not current:
		frappe.throw(_("Device {0} not found").format(device_id))
	device = _upsert_device(device_id, {"revoked": False})
	return {"ok": True, "device": device}


@frappe.whitelist()
def delete_connected_device(device_id=None):
	device_id = (device_id or "").strip()
	name, _current = _get_device_row(device_id)
	if not name:
		frappe.throw(_("Device {0} not found").format(device_id))
	frappe.delete_doc("Table Extra Data", name, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}
