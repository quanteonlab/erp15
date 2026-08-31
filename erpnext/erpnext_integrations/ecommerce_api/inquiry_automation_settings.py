"""Server-side inquiry automation settings (email recipients, toggles).

Stored in Table Extra Schema scope ``settings.inquiry_automation`` (no migrate).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint

SCOPE = "settings.inquiry_automation"
DEFAULT_NOTIFY_EMAILS = ["wangnelson2@gmail.com", "help@l0l.in"]
EMAIL_LANGUAGES = ("es", "en", "zh")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _load_raw() -> dict:
	if not frappe.db.exists("Table Extra Schema", SCOPE):
		return {}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", SCOPE)
	data = _parse_json(doc.columns_json, {})
	return data if isinstance(data, dict) else {}


def _save_raw(data: dict) -> None:
	payload = json.dumps(data or {}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", SCOPE):
		doc = frappe.get_doc("Table Extra Schema", SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _as_bool(val, default: bool) -> bool:
	if isinstance(val, bool):
		return val
	if val is None:
		return default
	return bool(cint(val))


def _normalize_emails(raw) -> list[str]:
	if not isinstance(raw, list):
		return []
	out: list[str] = []
	seen: set[str] = set()
	for item in raw:
		email = str(item or "").strip().lower()
		if not email or email in seen:
			continue
		if not _EMAIL_RE.match(email):
			continue
		seen.add(email)
		out.append(email)
	return out[:40]


def _normalize_email_language(raw) -> str:
	lang = str(raw or "").strip().lower()
	if lang in EMAIL_LANGUAGES:
		return lang
	# Accept legacy/alternate keys
	alt = str(raw or "").strip().lower()
	if alt in ("spanish", "español", "esp"):
		return "es"
	if alt in ("english", "eng"):
		return "en"
	if alt in ("chinese", "mandarin", "cn"):
		return "zh"
	return "es"


def _normalize_settings(data: dict | None) -> dict:
	src = data if isinstance(data, dict) else {}
	notify_emails = _normalize_emails(src.get("notifyEmails") or src.get("notify_emails"))
	if not notify_emails:
		notify_emails = list(DEFAULT_NOTIFY_EMAILS)
	contact_email = str(src.get("contactEmail") or src.get("contact_email") or "").strip().lower()
	if contact_email and _EMAIL_RE.match(contact_email) and contact_email not in notify_emails:
		notify_emails = [contact_email, *notify_emails]
	return {
		"contactName": str(src.get("contactName") or src.get("contact_name") or "").strip()[:120],
		"contactEmail": contact_email if _EMAIL_RE.match(contact_email or "") else "",
		"contactPhone": str(src.get("contactPhone") or src.get("contact_phone") or "").strip()[:80],
		"emailOnInquiry": _as_bool(
			src.get("emailOnInquiry") if "emailOnInquiry" in src else src.get("email_on_inquiry"),
			True,
		),
		"contactOnInquiry": _as_bool(
			src.get("contactOnInquiry") if "contactOnInquiry" in src else src.get("contact_on_inquiry"),
			True,
		),
		"callOnInquiry": _as_bool(
			src.get("callOnInquiry") if "callOnInquiry" in src else src.get("call_on_inquiry"),
			False,
		),
		"emailLanguage": _normalize_email_language(
			src.get("emailLanguage") if "emailLanguage" in src else src.get("email_language")
		),
		"emailIncludeBarcodes": _as_bool(
			src.get("emailIncludeBarcodes")
			if "emailIncludeBarcodes" in src
			else src.get("email_include_barcodes"),
			True,
		),
		"notifyEmails": notify_emails,
		"notifyPhones": [
			str(item or "").strip()
			for item in (src.get("notifyPhones") or src.get("notify_phones") or [])
			if str(item or "").strip()
		][:40],
	}


def get_inquiry_automation_settings_internal() -> dict:
	raw = _load_raw()
	if not raw:
		return _normalize_settings({})
	return _normalize_settings(raw)


def _can_manage_settings() -> bool:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	return _can_app("tools.settings")


@frappe.whitelist()
def get_inquiry_automation_settings():
	"""Return inquiry automation settings for Tools > Automation."""
	settings = get_inquiry_automation_settings_internal()
	return {"ok": True, "settings": settings, "source": "server" if _load_raw() else "default"}


@frappe.whitelist()
def save_inquiry_automation_settings(settings=None):
	"""Persist inquiry automation settings (admin)."""
	if not _can_manage_settings():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"))
	if isinstance(settings, str):
		settings = frappe.parse_json(settings)
	incoming = settings if isinstance(settings, dict) else {}
	current = _load_raw()
	merged = _normalize_settings({**current, **incoming})
	_save_raw(merged)
	return {"ok": True, "settings": merged, "source": "server"}
