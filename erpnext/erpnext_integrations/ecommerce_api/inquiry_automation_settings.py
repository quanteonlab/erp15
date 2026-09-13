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

# Outbound SMTP profiles editable in Tools > Settings > Automation.
# Add new keys here when another flow starts sending mail.
EMAIL_OPERATIONS = {
	"inquiry": {
		"label_en": "Inquiry / consulta alerts",
		"label_es": "Alertas de consulta",
		"label_zh": "咨询提醒邮件",
		"default_sender_name": "SilkOS Consultas",
	},
}

DEFAULT_SMTP = {
	"email": "",
	"password": "",
	"server": "smtp.hostinger.com",
	"port": "465",
	"useSsl": True,
	"senderName": "",
}


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


def _normalize_smtp_account(raw, op_id: str, previous: dict | None = None) -> dict:
	src = raw if isinstance(raw, dict) else {}
	prev = previous if isinstance(previous, dict) else {}
	meta = EMAIL_OPERATIONS.get(op_id) or {}
	email = str(src.get("email") or prev.get("email") or "").strip().lower()
	if email and not _EMAIL_RE.match(email):
		email = ""
	password = str(src.get("password") if "password" in src else "").strip()
	# Blank password on save keeps the previously stored secret.
	if not password:
		password = str(prev.get("password") or "")
	server = str(src.get("server") if "server" in src else prev.get("server") or DEFAULT_SMTP["server"]).strip()
	port = str(src.get("port") if "port" in src else prev.get("port") or DEFAULT_SMTP["port"]).strip() or "465"
	use_ssl = _as_bool(
		src.get("useSsl") if "useSsl" in src else (src.get("use_ssl") if "use_ssl" in src else None),
		_as_bool(prev.get("useSsl"), True),
	)
	sender_raw = src.get("senderName") if "senderName" in src else None
	if sender_raw is None and "sender_name" in src:
		sender_raw = src.get("sender_name")
	if sender_raw is None:
		sender_raw = prev.get("senderName") or meta.get("default_sender_name") or ""
	sender_name = str(sender_raw or "").strip()[:120]
	return {
		"email": email,
		"password": password,
		"server": server or DEFAULT_SMTP["server"],
		"port": port,
		"useSsl": use_ssl,
		"senderName": sender_name,
	}


def _normalize_email_accounts(raw, previous: dict | None = None) -> dict:
	src = raw if isinstance(raw, dict) else {}
	prev = previous if isinstance(previous, dict) else {}
	out = {}
	for op_id in EMAIL_OPERATIONS:
		out[op_id] = _normalize_smtp_account(src.get(op_id), op_id, prev.get(op_id))
	# Keep unknown ops that were already saved (forward-compat).
	for key, val in src.items():
		if key in out or not isinstance(val, dict):
			continue
		out[str(key)] = _normalize_smtp_account(val, str(key), prev.get(key))
	return out


def _public_smtp_account(account: dict) -> dict:
	return {
		"email": str(account.get("email") or ""),
		"server": str(account.get("server") or DEFAULT_SMTP["server"]),
		"port": str(account.get("port") or DEFAULT_SMTP["port"]),
		"useSsl": bool(account.get("useSsl", True)),
		"senderName": str(account.get("senderName") or ""),
		"passwordSet": bool(str(account.get("password") or "").strip()),
	}


def _public_settings(settings: dict) -> dict:
	"""API-safe view: never return SMTP passwords."""
	out = dict(settings or {})
	accounts = out.get("emailAccounts") or {}
	out["emailAccounts"] = {
		op_id: _public_smtp_account(cfg if isinstance(cfg, dict) else {})
		for op_id, cfg in accounts.items()
	}
	out["emailOperations"] = [
		{"id": op_id, **meta} for op_id, meta in EMAIL_OPERATIONS.items()
	]
	return out


def _normalize_settings(data: dict | None, previous: dict | None = None) -> dict:
	src = data if isinstance(data, dict) else {}
	prev = previous if isinstance(previous, dict) else {}
	notify_emails = _normalize_emails(src.get("notifyEmails") or src.get("notify_emails"))
	if not notify_emails:
		notify_emails = list(DEFAULT_NOTIFY_EMAILS)
	contact_email = str(src.get("contactEmail") or src.get("contact_email") or "").strip().lower()
	if contact_email and _EMAIL_RE.match(contact_email) and contact_email not in notify_emails:
		notify_emails = [contact_email, *notify_emails]
	email_accounts_raw = src.get("emailAccounts") if "emailAccounts" in src else src.get("email_accounts")
	if email_accounts_raw is None and "emailAccounts" not in src and "email_accounts" not in src:
		# Preserve existing accounts when a partial patch omits the key.
		email_accounts_raw = prev.get("emailAccounts")
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
		"emailAccounts": _normalize_email_accounts(email_accounts_raw, prev.get("emailAccounts")),
	}


def get_inquiry_automation_settings_internal() -> dict:
	raw = _load_raw()
	if not raw:
		return _normalize_settings({})
	return _normalize_settings(raw, previous=raw)


def get_email_account_for_operation(operation: str = "inquiry") -> dict:
	"""Full SMTP credentials for an operation (includes password). Falls back to empty defaults."""
	settings = get_inquiry_automation_settings_internal()
	accounts = settings.get("emailAccounts") or {}
	op = str(operation or "inquiry").strip() or "inquiry"
	account = accounts.get(op) if isinstance(accounts.get(op), dict) else {}
	if not account and op != "inquiry":
		account = accounts.get("inquiry") if isinstance(accounts.get("inquiry"), dict) else {}
	return _normalize_smtp_account(account, op)


def _can_manage_settings() -> bool:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	return _can_app("tools.settings")


@frappe.whitelist()
def get_inquiry_automation_settings():
	"""Return inquiry automation settings for Tools > Automation (passwords redacted)."""
	settings = get_inquiry_automation_settings_internal()
	return {
		"ok": True,
		"settings": _public_settings(settings),
		"source": "server" if _load_raw() else "default",
	}


@frappe.whitelist()
def save_inquiry_automation_settings(settings=None):
	"""Persist inquiry automation settings (admin). Empty password keeps the previous one."""
	if not _can_manage_settings():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"))
	if isinstance(settings, str):
		settings = frappe.parse_json(settings)
	incoming = settings if isinstance(settings, dict) else {}
	current = _load_raw()
	merged = _normalize_settings({**current, **incoming}, previous=current)
	# When client sends public emailAccounts (passwordSet only), merge passwords carefully.
	if isinstance(incoming.get("emailAccounts"), dict):
		merged["emailAccounts"] = _normalize_email_accounts(
			incoming.get("emailAccounts"), current.get("emailAccounts")
		)
	_save_raw(merged)
	return {"ok": True, "settings": _public_settings(merged), "source": "server"}
