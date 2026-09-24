"""Outbound webhooks for SilkOS ecommerce / TMS events.

Configs live in Table Extra Schema (`settings.outbound_webhooks`) — no migrate.
Delivery is async via `frappe.enqueue` with HMAC-SHA256 signing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

SCOPE = "settings.outbound_webhooks"

# Canonical event catalog shown in Settings → Automation → Webhooks
WEBHOOK_EVENTS = [
	# TMS / Rutas
	{"id": "planned", "group": "tms", "label": "planned", "description": "Trip published / route planned"},
	{"id": "route_started", "group": "tms", "label": "route_started", "description": "Driver started the trip"},
	{"id": "arriving", "group": "tms", "label": "arriving", "description": "Driver approaching a stop"},
	{"id": "delivered", "group": "tms", "label": "delivered", "description": "Stop delivered successfully"},
	{"id": "failed", "group": "tms", "label": "failed", "description": "Stop failed (not home / refused / partial)"},
	# Products
	{"id": "item_created", "group": "product", "label": "item_created", "description": "Product / Item created"},
	{"id": "item_updated", "group": "product", "label": "item_updated", "description": "Product / Item updated"},
	{"id": "item_price_changed", "group": "product", "label": "item_price_changed", "description": "Selling price changed"},
	# Orders
	{"id": "order_created", "group": "orders", "label": "order_created", "description": "Guest preorder / Sales Order created"},
	{"id": "order_confirmed", "group": "orders", "label": "order_confirmed", "description": "Preorder confirmed (submitted)"},
	{"id": "order_prepared", "group": "orders", "label": "order_prepared", "description": "Order marked Preparado"},
	{"id": "remito_created", "group": "orders", "label": "remito_created", "description": "Delivery Note (remito) created"},
	# Other useful
	{"id": "customer_created", "group": "master", "label": "customer_created", "description": "Customer created"},
	{"id": "payment_received", "group": "finance", "label": "payment_received", "description": "Payment Entry submitted"},
]

_VALID_EVENT_IDS = {e["id"] for e in WEBHOOK_EVENTS}


def _load_blob() -> dict:
	if not frappe.db.exists("Table Extra Schema", SCOPE):
		return {"webhooks": []}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", SCOPE)
	frappe.flags.ignore_permissions = False
	raw = doc.columns_json or "{}"
	try:
		data = frappe.parse_json(raw) if isinstance(raw, str) else (raw or {})
	except Exception:
		data = {}
	if not isinstance(data, dict):
		data = {}
	data.setdefault("webhooks", [])
	if not isinstance(data["webhooks"], list):
		data["webhooks"] = []
	return data


def _save_blob(data: dict) -> dict:
	payload = frappe.as_json(data)
	if frappe.db.exists("Table Extra Schema", SCOPE):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Table Extra Schema", SCOPE)
		frappe.flags.ignore_permissions = False
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return data


def _public_hook(h: dict) -> dict:
	"""Never return the raw secret — only a set flag + last-4 hint."""
	secret = str(h.get("secret") or "")
	return {
		"id": h.get("id"),
		"endpoint_url": h.get("endpoint_url") or "",
		"description": h.get("description") or "",
		"enabled": cint(h.get("enabled", 1)),
		"events": list(h.get("events") or []),
		"secret_set": bool(secret),
		"secret_hint": (("…" + secret[-4:]) if len(secret) >= 4 else ""),
		"created_at": h.get("created_at"),
		"modified_at": h.get("modified_at"),
		"last_delivery_at": h.get("last_delivery_at"),
		"last_status": h.get("last_status"),
		"last_error": h.get("last_error"),
	}


def _gen_secret() -> str:
	return secrets.token_urlsafe(32)


@frappe.whitelist(allow_guest=True)
def list_webhook_events():
	"""Catalog of emit-able events for the Settings UI."""
	groups = {}
	for e in WEBHOOK_EVENTS:
		groups.setdefault(e["group"], []).append(e)
	return {"events": WEBHOOK_EVENTS, "groups": groups}


@frappe.whitelist(allow_guest=True)
def list_webhooks():
	data = _load_blob()
	return {"webhooks": [_public_hook(h) for h in data.get("webhooks") or []]}


@frappe.whitelist(allow_guest=True)
def get_webhook(name=None):
	name = str(name or "").strip()
	if not name:
		frappe.throw(_("Webhook id is required"))
	data = _load_blob()
	for h in data.get("webhooks") or []:
		if h.get("id") == name:
			return {"webhook": _public_hook(h)}
	frappe.throw(_("Webhook {0} not found").format(name), frappe.DoesNotExistError)


@frappe.whitelist(allow_guest=True)
def save_webhook(webhook=None, rotate_secret=0):
	"""Create or update a webhook. Pass `id` to update; omit to create."""
	if isinstance(webhook, str):
		webhook = frappe.parse_json(webhook) or {}
	webhook = webhook or {}
	if not isinstance(webhook, dict):
		frappe.throw(_("Invalid webhook payload"))

	endpoint = str(webhook.get("endpoint_url") or "").strip()
	if not endpoint:
		frappe.throw(_("Endpoint URL is required"))
	if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
		frappe.throw(_("Endpoint URL must start with http:// or https://"))

	events_raw = webhook.get("events") or []
	if isinstance(events_raw, str):
		try:
			events_raw = frappe.parse_json(events_raw)
		except Exception:
			events_raw = [events_raw] if events_raw.strip() else []
	if not isinstance(events_raw, (list, tuple)):
		events_raw = []
	events = []
	for e in events_raw:
		eid = str(e or "").strip()
		if eid and eid in _VALID_EVENT_IDS and eid not in events:
			events.append(eid)
	if not events:
		frappe.throw(_("Select at least one event"))

	data = _load_blob()
	hooks = list(data.get("webhooks") or [])
	now = str(now_datetime())
	hook_id = str(webhook.get("id") or "").strip()
	rotate = cint(rotate_secret)

	existing = None
	idx = None
	if hook_id:
		for i, h in enumerate(hooks):
			if h.get("id") == hook_id:
				existing = h
				idx = i
				break
		if existing is None:
			frappe.throw(_("Webhook {0} not found").format(hook_id), frappe.DoesNotExistError)

	if existing is None:
		hook_id = "wh_" + uuid.uuid4().hex[:12]
		secret = str(webhook.get("secret") or "").strip() or _gen_secret()
		row = {
			"id": hook_id,
			"endpoint_url": endpoint,
			"description": str(webhook.get("description") or "").strip()[:200],
			"secret": secret,
			"enabled": cint(webhook.get("enabled", 1)),
			"events": events,
			"created_at": now,
			"modified_at": now,
			"last_delivery_at": None,
			"last_status": None,
			"last_error": None,
		}
		hooks.append(row)
	else:
		secret = existing.get("secret") or _gen_secret()
		if rotate:
			secret = _gen_secret()
		elif webhook.get("secret"):
			incoming = str(webhook.get("secret") or "").strip()
			if incoming and incoming != secret:
				secret = incoming
		row = dict(existing)
		row.update(
			{
				"endpoint_url": endpoint,
				"description": str(webhook.get("description") or "").strip()[:200],
				"secret": secret,
				"enabled": cint(webhook.get("enabled", existing.get("enabled", 1))),
				"events": events,
				"modified_at": now,
			}
		)
		hooks[idx] = row

	data["webhooks"] = hooks
	_save_blob(data)
	return {"webhook": _public_hook(row), "webhooks": [_public_hook(h) for h in hooks]}


@frappe.whitelist(allow_guest=True)
def delete_webhook(name=None):
	name = str(name or "").strip()
	if not name:
		frappe.throw(_("Webhook id is required"))
	data = _load_blob()
	before = len(data.get("webhooks") or [])
	data["webhooks"] = [h for h in (data.get("webhooks") or []) if h.get("id") != name]
	if len(data["webhooks"]) == before:
		frappe.throw(_("Webhook {0} not found").format(name), frappe.DoesNotExistError)
	_save_blob(data)
	return {"ok": True, "webhooks": [_public_hook(h) for h in data["webhooks"]]}


@frappe.whitelist(allow_guest=True)
def rotate_webhook_secret(name=None):
	name = str(name or "").strip()
	if not name:
		frappe.throw(_("Webhook id is required"))
	data = _load_blob()
	found = None
	for h in data.get("webhooks") or []:
		if h.get("id") == name:
			h["secret"] = _gen_secret()
			h["modified_at"] = str(now_datetime())
			found = h
			break
	if not found:
		frappe.throw(_("Webhook {0} not found").format(name), frappe.DoesNotExistError)
	_save_blob(data)
	# One-time reveal of the new secret for copy-to-clipboard UX
	return {
		"webhook": _public_hook(found),
		"secret": found.get("secret"),
	}


def _sign(secret: str, body: bytes) -> str:
	return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _deliver_one(hook_id: str, event: str, payload: dict):
	"""Worker: POST JSON to the subscriber. Updates last_* on the hook."""
	data = _load_blob()
	hook = None
	for h in data.get("webhooks") or []:
		if h.get("id") == hook_id:
			hook = h
			break
	if not hook or not cint(hook.get("enabled", 1)):
		return

	body_obj = {
		"id": "evt_" + uuid.uuid4().hex[:16],
		"event": event,
		"created_at": str(now_datetime()),
		"data": payload or {},
	}
	body = frappe.as_json(body_obj).encode("utf-8")
	secret = str(hook.get("secret") or "")
	headers = {
		"Content-Type": "application/json",
		"User-Agent": "SilkOS-Webhooks/1.0",
		"X-Ecommerce-Event": event,
		"X-Ecommerce-Delivery": body_obj["id"],
	}
	if secret:
		headers["X-Ecommerce-Signature"] = "sha256=" + _sign(secret, body)

	status = "ok"
	err = None
	try:
		req = urlrequest.Request(
			hook["endpoint_url"],
			data=body,
			headers=headers,
			method="POST",
		)
		with urlrequest.urlopen(req, timeout=15) as resp:
			code = getattr(resp, "status", None) or resp.getcode()
			if int(code) >= 400:
				status = "error"
				err = f"HTTP {code}"
	except HTTPError as e:
		status = "error"
		err = f"HTTP {e.code}: {e.reason}"
	except URLError as e:
		status = "error"
		err = str(e.reason or e)[:200]
	except Exception as e:
		status = "error"
		err = str(e)[:200]

	# Persist delivery meta
	data = _load_blob()
	for h in data.get("webhooks") or []:
		if h.get("id") == hook_id:
			h["last_delivery_at"] = str(now_datetime())
			h["last_status"] = status
			h["last_error"] = err
			break
	_save_blob(data)


def emit_ecommerce_webhook(event: str, payload=None, now=False):
	"""Fan-out an event to all enabled webhooks that subscribed to it.

	Safe to call from any API path — failures are logged, never raised to caller.
	"""
	event = str(event or "").strip()
	if not event or event not in _VALID_EVENT_IDS:
		return {"ok": False, "skipped": "unknown_event"}

	try:
		data = _load_blob()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook_api.emit load")
		return {"ok": False, "skipped": "load_error"}

	targets = [
		h
		for h in (data.get("webhooks") or [])
		if cint(h.get("enabled", 1)) and event in (h.get("events") or [])
	]
	if not targets:
		return {"ok": True, "queued": 0}

	payload = payload if isinstance(payload, dict) else {}
	queued = 0
	for h in targets:
		try:
			if now or frappe.flags.in_test:
				_deliver_one(h["id"], event, payload)
			else:
				frappe.enqueue(
					"erpnext.erpnext_integrations.ecommerce_api.webhook_api._deliver_one",
					queue="short",
					timeout=60,
					hook_id=h["id"],
					event=event,
					payload=payload,
				)
			queued += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"webhook_api.enqueue {event}")
	return {"ok": True, "queued": queued}


@frappe.whitelist(allow_guest=True)
def test_webhook(name=None, event=None):
	"""Fire a sample payload to one webhook (or all subscribed to `event`)."""
	name = str(name or "").strip()
	event = str(event or "planned").strip()
	if event not in _VALID_EVENT_IDS:
		frappe.throw(_("Unknown event: {0}").format(event))

	sample = {
		"test": True,
		"message": "SilkOS webhook test",
		"event": event,
		"site": frappe.local.site if hasattr(frappe.local, "site") else None,
	}

	if name:
		data = _load_blob()
		hook = next((h for h in (data.get("webhooks") or []) if h.get("id") == name), None)
		if not hook:
			frappe.throw(_("Webhook {0} not found").format(name), frappe.DoesNotExistError)
		_deliver_one(name, event, sample)
		# reload meta
		data = _load_blob()
		hook = next((h for h in (data.get("webhooks") or []) if h.get("id") == name), {})
		return {"ok": True, "webhook": _public_hook(hook)}

	return emit_ecommerce_webhook(event, sample, now=True)
