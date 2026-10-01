"""Ecommerce SMS gateway settings — Bird.com (primary) + Twilio.

Stored in Single DocType ``Ecommerce SMS Settings``. Secrets are Password
fields (never returned to the client). Outbound calls use HTTPS so we do not
require vendor SDKs.

Bird delivery SMS uses a registered template::

    POST {bird_api_base}/v1/sms/messages
    Authorization: Bearer <bird_api_key>
    { "to": "+54…", "template": { "slug": "bird_delivery_update",
      "language": "en", "parameters": { "ref": "A0000", "date": "2026-09-30" } } }
"""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime, nowdate


_SETTINGS_DOCTYPE = "Ecommerce SMS Settings"
_PROVIDERS = ("Bird", "Twilio")
_DEFAULT_TEST_BODY = "ERP SMS test — connection OK."
_DEFAULT_BIRD_BASE = "https://us1.platform.bird.com"
_DEFAULT_DELIVERY_TEMPLATE = "bird_delivery_update"
_DEFAULT_TEMPLATE_LANG = "en"


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
		doc.provider = "Bird"
		doc.bird_api_base = _DEFAULT_BIRD_BASE
		doc.bird_delivery_template_slug = _DEFAULT_DELIVERY_TEMPLATE
		doc.bird_template_language = _DEFAULT_TEMPLATE_LANG
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return frappe.get_single(_SETTINGS_DOCTYPE)


def _password_set(settings, fieldname: str) -> bool:
	"""Password fields are blank in get_doc; check encrypted store."""
	try:
		token = settings.get_password(fieldname, raise_exception=False)
	except Exception:
		token = None
	return bool(cstr(token or "").strip())


def _get_password(settings, fieldname: str) -> str:
	try:
		token = settings.get_password(fieldname, raise_exception=False) or ""
	except Exception:
		token = ""
	return cstr(token).strip()


def _auth_token_set(settings) -> bool:
	return _password_set(settings, "auth_token")


def _bird_api_key_set(settings) -> bool:
	return _password_set(settings, "bird_api_key")


def _bird_base(settings) -> str:
	base = cstr(settings.get("bird_api_base") or "").strip().rstrip("/")
	return base or _DEFAULT_BIRD_BASE


def _bird_template_slug(settings) -> str:
	return (
		cstr(settings.get("bird_delivery_template_slug") or "").strip()
		or _DEFAULT_DELIVERY_TEMPLATE
	)


def _bird_template_language(settings) -> str:
	return (
		cstr(settings.get("bird_template_language") or "").strip()
		or _DEFAULT_TEMPLATE_LANG
	)


def _twilio_configured(settings) -> bool:
	return bool(
		cstr(settings.get("account_sid") or "").strip()
		and cstr(settings.get("from_number") or "").strip()
		and _auth_token_set(settings)
	)


def _bird_configured(settings) -> bool:
	return _bird_api_key_set(settings)


def _serialize_sms_settings(settings) -> dict:
	provider = cstr(settings.get("provider") or "Bird").strip() or "Bird"
	if provider not in _PROVIDERS:
		provider = "Bird"
	return {
		"enabled": bool(_as_check(settings.get("enabled"))),
		"provider": provider,
		"account_sid": cstr(settings.get("account_sid") or "").strip(),
		"from_number": cstr(settings.get("from_number") or "").strip(),
		"auth_token_set": _auth_token_set(settings),
		"bird_api_key_set": _bird_api_key_set(settings),
		"bird_api_base": _bird_base(settings),
		"bird_delivery_template_slug": _bird_template_slug(settings),
		"bird_template_language": _bird_template_language(settings),
		"last_test_at": settings.get("last_test_at"),
		"last_error": cstr(settings.get("last_error") or "") or None,
		"providers": [
			{"id": "Bird", "label": "Bird.com", "configured": _bird_configured(settings)},
			{"id": "Twilio", "label": "Twilio", "configured": _twilio_configured(settings)},
		],
	}


def _normalize_e164(number: str) -> str:
	raw = cstr(number or "").strip()
	if not raw:
		return ""
	keep = []
	for i, ch in enumerate(raw):
		if ch.isdigit() or (ch == "+" and i == 0):
			keep.append(ch)
	out = "".join(keep)
	if out and not out.startswith("+"):
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


def _set_password_if_provided(settings, fieldname: str, raw) -> None:
	if raw is None or cstr(raw).lower() in ("null", "undefined", ""):
		return
	token = cstr(raw).strip()
	if token and token not in ("••••••••", "********", "***"):
		settings.set(fieldname, token)


@frappe.whitelist(allow_guest=True)
def get_sms_settings():
	"""SMS config for Settings → Integraciones (no secret plaintext)."""
	settings = _ensure_settings()
	return _serialize_sms_settings(settings)


@frappe.whitelist(allow_guest=True)
def save_sms_settings(
	enabled=None,
	provider=None,
	account_sid=None,
	auth_token=None,
	from_number=None,
	bird_api_key=None,
	bird_api_base=None,
	bird_delivery_template_slug=None,
	bird_template_language=None,
):
	"""Persist Bird / Twilio credentials.

	Password fields are optional — omit / blank / null / \"***\" keeps the stored value.
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

	_set_password_if_provided(settings, "auth_token", auth_token)
	_set_password_if_provided(settings, "bird_api_key", bird_api_key)

	if bird_api_base is not None and cstr(bird_api_base).lower() not in ("null", "undefined"):
		base = cstr(bird_api_base).strip().rstrip("/")
		settings.bird_api_base = base or _DEFAULT_BIRD_BASE

	if bird_delivery_template_slug is not None and cstr(bird_delivery_template_slug).lower() not in (
		"null",
		"undefined",
	):
		slug = cstr(bird_delivery_template_slug).strip()
		settings.bird_delivery_template_slug = slug or _DEFAULT_DELIVERY_TEMPLATE

	if bird_template_language is not None and cstr(bird_template_language).lower() not in (
		"null",
		"undefined",
	):
		lang = cstr(bird_template_language).strip()
		settings.bird_template_language = lang or _DEFAULT_TEMPLATE_LANG

	settings.save(ignore_permissions=True)
	frappe.db.commit()
	settings = _ensure_settings()
	return _serialize_sms_settings(settings)


def _http_json(method: str, url: str, *, headers: dict, body: dict | None = None, form=None) -> dict:
	data = None
	hdrs = dict(headers or {})
	if form is not None:
		data = urlencode(form).encode("utf-8")
		hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
	elif body is not None:
		data = json.dumps(body).encode("utf-8")
		hdrs.setdefault("Content-Type", "application/json")
	hdrs.setdefault("Accept", "application/json")
	req = Request(url, data=data, method=method.upper(), headers=hdrs)
	try:
		with urlopen(req, timeout=25) as resp:
			raw = resp.read().decode("utf-8", errors="replace")
			status = getattr(resp, "status", 200)
	except HTTPError as e:
		err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
		detail = err_body
		try:
			parsed = json.loads(err_body) if err_body else {}
			detail = (
				parsed.get("message")
				or parsed.get("error_message")
				or parsed.get("detail")
				or err_body
				or str(e)
			)
			if isinstance(detail, (list, dict)):
				detail = json.dumps(detail)[:800]
		except Exception:
			detail = err_body or str(e)
		frappe.throw(
			_("SMS provider error ({0}): {1}").format(e.code, detail),
			frappe.ValidationError,
		)
	except URLError as e:
		frappe.throw(_("Could not reach SMS provider: {0}").format(e.reason or e), frappe.ValidationError)

	try:
		data_out = json.loads(raw) if raw else {}
	except Exception:
		data_out = {"raw": raw}
	if status >= 400:
		msg = data_out.get("message") if isinstance(data_out, dict) else raw
		frappe.throw(_("SMS provider error ({0}): {1}").format(status, msg or raw), frappe.ValidationError)
	return data_out if isinstance(data_out, dict) else {"raw": data_out}


def _twilio_send_sms(*, account_sid: str, auth_token: str, from_number: str, to_number: str, body: str) -> dict:
	"""POST Messages.json; returns Twilio JSON or raises frappe.ValidationError."""
	url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
	auth = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
	return _http_json(
		"POST",
		url,
		headers={"Authorization": f"Basic {auth}"},
		form={"From": from_number, "To": to_number, "Body": body},
	)


def _bird_send_template_sms(
	*,
	api_key: str,
	api_base: str,
	to_number: str,
	template_slug: str,
	language: str,
	parameters: dict,
) -> dict:
	"""POST Bird /v1/sms/messages with a registered template."""
	base = (api_base or _DEFAULT_BIRD_BASE).rstrip("/")
	url = f"{base}/v1/sms/messages"
	payload = {
		"to": to_number,
		"template": {
			"slug": template_slug,
			"language": language or _DEFAULT_TEMPLATE_LANG,
			"parameters": parameters or {},
		},
	}
	return _http_json(
		"POST",
		url,
		headers={"Authorization": f"Bearer {api_key}"},
		body=payload,
	)


def _require_enabled(settings):
	if not _as_check(settings.get("enabled")):
		frappe.throw(_("SMS is disabled in Settings → Integraciones."), frappe.ValidationError)


def _resolve_customer_phone(customer: str | None = None, delivery_note: str | None = None) -> str:
	"""Best-effort E.164 from DN / Customer / Contact (only columns that exist)."""
	cust = cstr(customer or "").strip()
	dn = cstr(delivery_note or "").strip()
	if not cust and dn and frappe.db.exists("Delivery Note", dn):
		cust = cstr(frappe.db.get_value("Delivery Note", dn, "customer") or "").strip()

	candidates: list[str] = []
	if cust and frappe.db.exists("Customer", cust):
		cust_fields = []
		for col in ("custom_client_phone_e164", "mobile_no", "phone"):
			if frappe.db.has_column("Customer", col):
				cust_fields.append(col)
		if cust_fields:
			row = frappe.db.get_value("Customer", cust, cust_fields, as_dict=True) or {}
			for col in cust_fields:
				candidates.append(row.get(col))
		# Primary contact mobile
		contact = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Customer", "link_name": cust, "parenttype": "Contact"},
			"parent",
		)
		if contact:
			contact_fields = [c for c in ("mobile_no", "phone") if frappe.db.has_column("Contact", c)]
			if contact_fields:
				crow = frappe.db.get_value("Contact", contact, contact_fields, as_dict=True) or {}
				for col in contact_fields:
					candidates.append(crow.get(col))

	for raw in candidates:
		e164 = _normalize_e164(cstr(raw or ""))
		if e164 and len(e164) >= 8:
			return e164
	return ""


def _delivery_ref(delivery_note: str | None = None, ref: str | None = None) -> str:
	explicit = cstr(ref or "").strip()
	if explicit:
		return explicit[:40]
	dn = cstr(delivery_note or "").strip()
	if dn and frappe.db.exists("Delivery Note", dn):
		if frappe.db.has_column("Delivery Note", "custom_tracking_code"):
			code = cstr(frappe.db.get_value("Delivery Note", dn, "custom_tracking_code") or "").strip()
			if code:
				return code[:40]
		return dn[:40]
	return "—"


@frappe.whitelist(allow_guest=True)
def send_test_sms(to_number=None, message=None):
	"""Send a one-off SMS via the configured provider (Bird template or Twilio body)."""
	settings = _ensure_settings()
	_require_enabled(settings)
	provider = cstr(settings.get("provider") or "Bird").strip() or "Bird"

	to = _normalize_e164(cstr(to_number or ""))
	if not to or len(to) < 8:
		frappe.throw(
			_("Enter a valid destination phone number (E.164, e.g. +54911…)."),
			frappe.ValidationError,
		)

	try:
		if provider == "Bird":
			api_key = _get_password(settings, "bird_api_key")
			if not api_key:
				frappe.throw(_("Configure Bird API Key first."), frappe.ValidationError)
			result = _bird_send_template_sms(
				api_key=api_key,
				api_base=_bird_base(settings),
				to_number=to,
				template_slug=_bird_template_slug(settings),
				language=_bird_template_language(settings),
				parameters={
					"ref": "TEST",
					"date": nowdate(),
				},
			)
			sid = (
				result.get("id")
				or result.get("sid")
				or result.get("messageId")
				or result.get("message_id")
			)
			status = result.get("status") or result.get("state")
			from_number = None
		elif provider == "Twilio":
			account_sid = cstr(settings.get("account_sid") or "").strip()
			from_number = _normalize_e164(cstr(settings.get("from_number") or ""))
			auth_token = _get_password(settings, "auth_token")
			if not account_sid or not auth_token or not from_number:
				frappe.throw(
					_("Configure Account SID, Auth Token, and Twilio Phone Number first."),
					frappe.ValidationError,
				)
			body = cstr(message or "").strip() or _DEFAULT_TEST_BODY
			if len(body) > 1600:
				body = body[:1600]
			result = _twilio_send_sms(
				account_sid=account_sid,
				auth_token=auth_token,
				from_number=from_number,
				to_number=to,
				body=body,
			)
			sid = result.get("sid")
			status = result.get("status")
		else:
			frappe.throw(_("Unsupported SMS provider: {0}").format(provider), frappe.ValidationError)
	except Exception as e:
		_set_last_error(cstr(e))
		frappe.db.commit()
		raise

	frappe.db.set_value(
		_SETTINGS_DOCTYPE,
		_SETTINGS_DOCTYPE,
		{"last_test_at": now_datetime(), "last_error": None},
		update_modified=True,
	)
	frappe.db.commit()

	return {
		"ok": True,
		"provider": provider,
		"sid": sid,
		"status": status,
		"to": to,
		"from": from_number,
		"settings": _serialize_sms_settings(_ensure_settings()),
	}


@frappe.whitelist(allow_guest=True)
def send_delivery_sms(
	delivery_note=None,
	to_number=None,
	ref=None,
	date=None,
	template_slug=None,
	language=None,
	customer=None,
):
	"""Manual delivery SMS (Conductor button). Bird template only for now.

	Resolves phone from ``to_number`` or Customer/Contact linked to the DN.
	Template params: ``ref`` (tracking code / DN) + ``date`` (today by default).
	"""
	settings = _ensure_settings()
	_require_enabled(settings)
	provider = cstr(settings.get("provider") or "Bird").strip() or "Bird"
	if provider != "Bird":
		frappe.throw(
			_("Delivery SMS templates require Bird.com as the active SMS provider."),
			frappe.ValidationError,
		)

	dn = cstr(delivery_note or "").strip()
	if dn and dn.lower() in ("null", "undefined"):
		dn = ""
	to = _normalize_e164(cstr(to_number or ""))
	if not to:
		to = _resolve_customer_phone(customer=cstr(customer or ""), delivery_note=dn)
	if not to or len(to) < 8:
		frappe.throw(
			_("No valid phone number for this delivery. Add a mobile on the Customer."),
			frappe.ValidationError,
		)

	api_key = _get_password(settings, "bird_api_key")
	if not api_key:
		frappe.throw(_("Configure Bird API Key in Settings → Integraciones."), frappe.ValidationError)

	slug = cstr(template_slug or "").strip() or _bird_template_slug(settings)
	lang = cstr(language or "").strip() or _bird_template_language(settings)
	ref_val = _delivery_ref(dn, ref)
	date_val = cstr(date or "").strip() or nowdate()

	try:
		result = _bird_send_template_sms(
			api_key=api_key,
			api_base=_bird_base(settings),
			to_number=to,
			template_slug=slug,
			language=lang,
			parameters={"ref": ref_val, "date": date_val},
		)
	except Exception as e:
		_set_last_error(cstr(e))
		frappe.db.commit()
		raise

	frappe.db.set_value(
		_SETTINGS_DOCTYPE,
		_SETTINGS_DOCTYPE,
		{"last_test_at": now_datetime(), "last_error": None},
		update_modified=True,
	)
	frappe.db.commit()

	sid = (
		result.get("id")
		or result.get("sid")
		or result.get("messageId")
		or result.get("message_id")
	)
	return {
		"ok": True,
		"provider": "Bird",
		"sid": sid,
		"status": result.get("status") or result.get("state"),
		"to": to,
		"ref": ref_val,
		"date": date_val,
		"template_slug": slug,
		"delivery_note": dn or None,
	}


@frappe.whitelist(allow_guest=True)
def list_sms_providers():
	"""Provider catalog for the settings UI."""
	settings = _ensure_settings()
	active = cstr(settings.get("provider") or "Bird")
	return {
		"providers": [
			{
				"id": "Bird",
				"label": "Bird.com",
				"configured": _bird_configured(settings),
				"enabled": bool(_as_check(settings.get("enabled"))) and active == "Bird",
			},
			{
				"id": "Twilio",
				"label": "Twilio",
				"configured": _twilio_configured(settings),
				"enabled": bool(_as_check(settings.get("enabled"))) and active == "Twilio",
			},
		],
		"active_provider": active,
	}
