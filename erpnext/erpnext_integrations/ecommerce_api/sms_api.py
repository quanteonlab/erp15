"""Ecommerce SMS gateway settings — Twilio-first configuration + test send.

Stored in Single DocType ``Ecommerce SMS Settings``. Auth token is a Password
field (never returned to the client). Outbound SMS uses Twilio's REST API over
HTTPS so we do not require the ``twilio`` Python package.
"""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime


_SETTINGS_DOCTYPE = "Ecommerce SMS Settings"
_PROVIDERS = ("Twilio",)
_DEFAULT_TEST_BODY = "ERP SMS test — connection OK."


def _as_check(value) -> int:
	if value is True or value == 1 or value == "1" or cstr(value).lower() == "true":
		return 1
	return 0


def _ensure_settings():
	"""Return the Single doc, creating a blank row if migrate has not run yet."""
	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("DocType", _SETTINGS_DOCTYPE):
		frappe.throw(
			_("SMS Settings DocType is missing. Run bench migrate on this site."),
			frappe.ValidationError,
		)
	if not frappe.db.exists(_SETTINGS_DOCTYPE, _SETTINGS_DOCTYPE):
		doc = frappe.new_doc(_SETTINGS_DOCTYPE)
		doc.enabled = 0
		doc.provider = "Twilio"
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return frappe.get_single(_SETTINGS_DOCTYPE)


def _auth_token_set(settings) -> bool:
	"""Password fields are blank in get_doc; check encrypted store."""
	try:
		token = settings.get_password("auth_token", raise_exception=False)
	except Exception:
		token = None
	return bool(cstr(token or "").strip())


def _serialize_sms_settings(settings) -> dict:
	provider = cstr(settings.get("provider") or "Twilio").strip() or "Twilio"
	if provider not in _PROVIDERS:
		provider = "Twilio"
	return {
		"enabled": bool(_as_check(settings.get("enabled"))),
		"provider": provider,
		"account_sid": cstr(settings.get("account_sid") or "").strip(),
		"from_number": cstr(settings.get("from_number") or "").strip(),
		"auth_token_set": _auth_token_set(settings),
		"last_test_at": settings.get("last_test_at"),
		"last_error": cstr(settings.get("last_error") or "") or None,
		"providers": [{"id": "Twilio", "label": "Twilio", "configured": _twilio_configured(settings)}],
	}


def _twilio_configured(settings) -> bool:
	return bool(
		cstr(settings.get("account_sid") or "").strip()
		and cstr(settings.get("from_number") or "").strip()
		and _auth_token_set(settings)
	)


def _normalize_e164(number: str) -> str:
	raw = cstr(number or "").strip()
	if not raw:
		return ""
	# Keep leading +; strip common separators.
	keep = []
	for i, ch in enumerate(raw):
		if ch.isdigit() or (ch == "+" and i == 0):
			keep.append(ch)
	out = "".join(keep)
	if out and not out.startswith("+"):
		# Assume already-country-coded digits without + — still require + for Twilio.
		out = "+" + out
	return out


def _set_last_error(message: str | None):
	frappe.db.set_value(
		_SETTINGS_DOCTYPE,
		_SETTINGS_DOCTYPE,
		"last_error",
		(message or "")[:1900] or None,
		update_modified=True,
	)


@frappe.whitelist(allow_guest=True)
def get_sms_settings():
	"""SMS config for Settings → SMS (no auth token plaintext)."""
	settings = _ensure_settings()
	return _serialize_sms_settings(settings)


@frappe.whitelist(allow_guest=True)
def save_sms_settings(
	enabled=None,
	provider=None,
	account_sid=None,
	auth_token=None,
	from_number=None,
):
	"""Persist Twilio (or future provider) credentials.

	``auth_token`` is optional — omit / blank / null / \"***\" keeps the stored token.
	"""
	settings = _ensure_settings()

	if enabled is not None and enabled != "" and cstr(enabled).lower() not in ("null", "undefined"):
		settings.enabled = _as_check(enabled)

	if provider is not None and provider != "" and cstr(provider).lower() not in ("null", "undefined"):
		prov = cstr(provider).strip()
		if prov not in _PROVIDERS:
			frappe.throw(
				_("Unsupported SMS provider: {0}").format(prov),
				frappe.ValidationError,
			)
		settings.provider = prov

	if account_sid is not None and cstr(account_sid).lower() not in ("null", "undefined"):
		settings.account_sid = cstr(account_sid).strip()

	if from_number is not None and cstr(from_number).lower() not in ("null", "undefined"):
		settings.from_number = _normalize_e164(cstr(from_number))

	token_raw = auth_token
	if token_raw is not None and cstr(token_raw).lower() not in ("null", "undefined", ""):
		token = cstr(token_raw).strip()
		# Frontend may send a mask when the user did not change the token.
		if token and token not in ("••••••••", "********", "***"):
			settings.auth_token = token

	settings.save(ignore_permissions=True)
	frappe.db.commit()
	settings = _ensure_settings()
	return _serialize_sms_settings(settings)


def _twilio_send_sms(*, account_sid: str, auth_token: str, from_number: str, to_number: str, body: str) -> dict:
	"""POST Messages.json; returns Twilio JSON or raises frappe.ValidationError."""
	url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
	payload = urlencode(
		{
			"From": from_number,
			"To": to_number,
			"Body": body,
		}
	).encode("utf-8")
	auth = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
	req = Request(
		url,
		data=payload,
		method="POST",
		headers={
			"Authorization": f"Basic {auth}",
			"Content-Type": "application/x-www-form-urlencoded",
			"Accept": "application/json",
		},
	)
	try:
		with urlopen(req, timeout=25) as resp:
			raw = resp.read().decode("utf-8", errors="replace")
			status = getattr(resp, "status", 200)
	except HTTPError as e:
		err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
		detail = err_body
		try:
			parsed = json.loads(err_body) if err_body else {}
			detail = parsed.get("message") or parsed.get("error_message") or err_body or str(e)
		except Exception:
			detail = err_body or str(e)
		frappe.throw(
			_("Twilio error ({0}): {1}").format(e.code, detail),
			frappe.ValidationError,
		)
	except URLError as e:
		frappe.throw(_("Could not reach Twilio: {0}").format(e.reason or e), frappe.ValidationError)

	try:
		data = json.loads(raw) if raw else {}
	except Exception:
		data = {"raw": raw}
	if status >= 400:
		frappe.throw(
			_("Twilio error ({0}): {1}").format(status, data.get("message") or raw),
			frappe.ValidationError,
		)
	return data if isinstance(data, dict) else {"raw": data}


@frappe.whitelist(allow_guest=True)
def send_test_sms(to_number=None, message=None):
	"""Send a one-off SMS via the configured provider (Twilio)."""
	settings = _ensure_settings()
	provider = cstr(settings.get("provider") or "Twilio").strip() or "Twilio"
	if provider != "Twilio":
		frappe.throw(_("Only Twilio is supported for now."), frappe.ValidationError)

	to = _normalize_e164(cstr(to_number or ""))
	if not to or len(to) < 8:
		frappe.throw(_("Enter a valid destination phone number (E.164, e.g. +54911…)."), frappe.ValidationError)

	account_sid = cstr(settings.get("account_sid") or "").strip()
	from_number = _normalize_e164(cstr(settings.get("from_number") or ""))
	try:
		auth_token = settings.get_password("auth_token", raise_exception=False) or ""
	except Exception:
		auth_token = ""
	auth_token = cstr(auth_token).strip()

	if not account_sid or not auth_token or not from_number:
		frappe.throw(
			_("Configure Account SID, Auth Token, and Twilio Phone Number first."),
			frappe.ValidationError,
		)

	body = cstr(message or "").strip() or _DEFAULT_TEST_BODY
	if len(body) > 1600:
		body = body[:1600]

	try:
		result = _twilio_send_sms(
			account_sid=account_sid,
			auth_token=auth_token,
			from_number=from_number,
			to_number=to,
			body=body,
		)
	except Exception as e:
		_set_last_error(cstr(e))
		frappe.db.commit()
		raise

	frappe.db.set_value(
		_SETTINGS_DOCTYPE,
		_SETTINGS_DOCTYPE,
		{
			"last_test_at": now_datetime(),
			"last_error": None,
		},
		update_modified=True,
	)
	frappe.db.commit()

	sid = result.get("sid") if isinstance(result, dict) else None
	status = result.get("status") if isinstance(result, dict) else None
	return {
		"ok": True,
		"provider": "Twilio",
		"sid": sid,
		"status": status,
		"to": to,
		"from": from_number,
		"settings": _serialize_sms_settings(_ensure_settings()),
	}


@frappe.whitelist(allow_guest=True)
def list_sms_providers():
	"""Provider catalog for the settings UI (Twilio only for now)."""
	settings = _ensure_settings()
	return {
		"providers": [
			{
				"id": "Twilio",
				"label": "Twilio",
				"configured": _twilio_configured(settings),
				"enabled": bool(_as_check(settings.get("enabled"))) and settings.get("provider") == "Twilio",
			}
		],
		"active_provider": cstr(settings.get("provider") or "Twilio"),
	}
