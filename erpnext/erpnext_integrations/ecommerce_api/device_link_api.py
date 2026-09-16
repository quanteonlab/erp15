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
INBOX_SCOPE = "settings.push_inbox"
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


def _serialize_device(row_key: str, data: dict, *, include_fcm_token: bool = False) -> dict:
	token = data.get("fcm_token") or None
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
		# Admin list never gets the raw token; send path passes include_fcm_token=True.
		"fcm_token": token if include_fcm_token else ("set" if token else None),
		"push_enabled": bool(data.get("push_enabled")) if data.get("push_enabled") is not None else bool(token),
		"push_token_updated_at": data.get("push_token_updated_at"),
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
	fcm_token=None,
	push_enabled=None,
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

	# Push token: empty string clears; None leaves unchanged.
	if fcm_token is not None:
		token = str(fcm_token).strip()
		if token:
			patch["fcm_token"] = token
			patch["push_token_updated_at"] = _now_iso()
			if push_enabled is None:
				patch["push_enabled"] = True
		else:
			patch["fcm_token"] = ""
			patch["push_enabled"] = False
	if push_enabled is not None:
		patch["push_enabled"] = bool(cint(push_enabled))

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


# ---------------------------------------------------------------------------
# Push inbox + dispatch (Table Extra Data — no migrate)
# ---------------------------------------------------------------------------


def _inbox_row_key(notification_id: str) -> str:
	return (notification_id or "").strip()


def _list_device_targets(
	*,
	device_ids: list | None = None,
	branch_id: str | None = None,
	only_with_token: bool = True,
) -> list[dict]:
	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": DEVICE_SCOPE},
		fields=["row_key", "data_json"],
		ignore_permissions=True,
	)
	wanted = {str(x).strip() for x in (device_ids or []) if str(x).strip()} or None
	out = []
	for row in rows:
		data = _parse_json(row.data_json, {})
		if not isinstance(data, dict) or data.get("revoked"):
			continue
		did = data.get("id") or row.row_key
		if wanted is not None and did not in wanted:
			continue
		if branch_id and str(data.get("branch_id") or "") != str(branch_id):
			continue
		if only_with_token and not (data.get("fcm_token") and data.get("push_enabled", True)):
			continue
		out.append(_serialize_device(row.row_key, data, include_fcm_token=True))
	return out


def _write_inbox_row(payload: dict) -> dict:
	nid = payload.get("id") or secrets.token_hex(8)
	payload = dict(payload)
	payload["id"] = nid
	payload.setdefault("created_at", _now_iso())
	payload.setdefault("read_at", None)
	frappe.flags.ignore_permissions = True
	row_key = _inbox_row_key(nid)
	name = frappe.db.get_value(
		"Table Extra Data",
		{"scope": INBOX_SCOPE, "row_key": row_key},
		"name",
	)
	raw = json.dumps(payload, ensure_ascii=False, default=str)
	if name:
		frappe.db.set_value("Table Extra Data", name, "data_json", raw)
	else:
		frappe.get_doc(
			{
				"doctype": "Table Extra Data",
				"scope": INBOX_SCOPE,
				"row_key": row_key,
				"data_json": raw,
			}
		).insert(ignore_permissions=True)
	return payload


@frappe.whitelist(allow_guest=True)
def list_push_inbox(device_id=None, link_token=None, require_token=0, limit=50, unread_only=0):
	"""Return recent push inbox rows for a device (and broadcast rows)."""
	if cint(require_token) or frappe.session.user == "Guest":
		_require_link_token(link_token or "")
	device_id = (device_id or "").strip()
	if not device_id:
		frappe.throw(_("device_id is required"))
	limit = max(1, min(cint(limit) or 50, 200))
	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": INBOX_SCOPE},
		fields=["row_key", "data_json", "modified"],
		order_by="modified desc",
		limit_page_length=500,
		ignore_permissions=True,
	)
	items = []
	for row in rows:
		data = _parse_json(row.data_json, {})
		if not isinstance(data, dict):
			continue
		target = (data.get("target_device_id") or "").strip()
		if target and target != device_id:
			continue
		if cint(unread_only) and data.get("read_at"):
			# read_at may be a map of device_id -> iso
			read_map = data.get("read_at")
			if isinstance(read_map, dict) and read_map.get(device_id):
				continue
			if isinstance(read_map, str) and read_map:
				continue
		items.append(data)
		if len(items) >= limit:
			break
	return {"items": items, "total": len(items)}


@frappe.whitelist(allow_guest=True)
def mark_push_read(device_id=None, notification_id=None, link_token=None, require_token=0):
	if cint(require_token) or frappe.session.user == "Guest":
		_require_link_token(link_token or "")
	device_id = (device_id or "").strip()
	notification_id = (notification_id or "").strip()
	if not device_id or not notification_id:
		frappe.throw(_("device_id and notification_id are required"))
	name = frappe.db.get_value(
		"Table Extra Data",
		{"scope": INBOX_SCOPE, "row_key": _inbox_row_key(notification_id)},
		"name",
	)
	if not name:
		frappe.throw(_("Notification not found"))
	raw = frappe.db.get_value("Table Extra Data", name, "data_json")
	data = _parse_json(raw, {})
	if not isinstance(data, dict):
		data = {}
	read_map = data.get("read_at")
	if not isinstance(read_map, dict):
		read_map = {}
	read_map[device_id] = _now_iso()
	data["read_at"] = read_map
	frappe.db.set_value("Table Extra Data", name, "data_json", json.dumps(data, ensure_ascii=False, default=str))
	frappe.db.commit()
	return {"ok": True, "item": data}


@frappe.whitelist()
def notify_connected_devices(
	title=None,
	body=None,
	notification_type=None,
	deep_link=None,
	entity_id=None,
	device_ids=None,
	branch_id=None,
	data=None,
	send_push=1,
):
	"""Create inbox rows and optionally fan-out via FCM.

	Admin / desk / server jobs call this. ``device_ids`` may be a JSON list or CSV.
	Omit device_ids to target every push-enabled device (optionally filtered by branch_id).
	"""
	title = (title or "").strip() or _("Notification")
	body = (body or "").strip() or ""
	ntype = (notification_type or "generic").strip() or "generic"
	if isinstance(device_ids, str):
		raw = device_ids.strip()
		if raw.startswith("["):
			device_ids = _parse_json(raw, [])
		else:
			device_ids = [x.strip() for x in raw.split(",") if x.strip()]
	if not isinstance(device_ids, list):
		device_ids = None
	extra = data if isinstance(data, dict) else _parse_json(data, {})
	if not isinstance(extra, dict):
		extra = {}

	targets = _list_device_targets(
		device_ids=device_ids,
		branch_id=branch_id,
		only_with_token=False,
	)
	if not targets and device_ids:
		# Still write per-id inbox even if device unknown / no token
		targets = [{"id": d, "fcm_token": None, "push_enabled": False} for d in device_ids]

	created = []
	push_results = []
	for dev in targets:
		did = dev.get("id")
		payload = {
			"id": secrets.token_hex(8),
			"type": ntype,
			"title": title,
			"body": body,
			"deep_link": (deep_link or "").strip() or None,
			"entity_id": (entity_id or "").strip() or None,
			"target_device_id": did,
			"data": extra,
			"created_at": _now_iso(),
			"read_at": {},
		}
		created.append(_write_inbox_row(payload))

		if cint(send_push) and dev.get("fcm_token") and dev.get("push_enabled", True):
			from erpnext.erpnext_integrations.ecommerce_api.push_fcm import send_fcm_message

			try:
				result = send_fcm_message(
					token=dev["fcm_token"],
					title=title,
					body=body,
					data={
						"type": ntype,
						"notification_id": payload["id"],
						"entity_id": payload.get("entity_id") or "",
						"deep_link": payload.get("deep_link") or "",
						**{k: v for k, v in extra.items()},
					},
				)
				push_results.append({"device_id": did, **result})
			except Exception as e:
				push_results.append({"device_id": did, "ok": False, "error": str(e)})

	frappe.db.commit()
	return {
		"ok": True,
		"created": len(created),
		"items": created,
		"push_results": push_results,
		"fcm_attempted": bool(cint(send_push)),
	}


@frappe.whitelist()
def get_push_status():
	"""Desk helper: is FCM configured, how many devices have tokens."""
	from erpnext.erpnext_integrations.ecommerce_api.push_fcm import fcm_configured

	devices = _list_device_targets(only_with_token=False)
	with_token = [d for d in devices if d.get("fcm_token") and d.get("push_enabled", True)]
	return {
		"fcm_configured": fcm_configured(),
		"devices_total": len(devices),
		"devices_with_push": len(with_token),
	}
