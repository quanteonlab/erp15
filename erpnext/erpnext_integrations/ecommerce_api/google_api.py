"""Google Maps + Translation API keys for Settings → Integraciones.

Maps key is stored in Frappe core ``Google Settings`` (``api_key`` / ``enable``) so
TMS / Delivery Trip keep working. Translation key is stored in Single
``Ecommerce Google API Settings`` (Password field).
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr


_GOOGLE_SETTINGS = "Google Settings"
_ECOM_GOOGLE = "Ecommerce Google API Settings"


def _as_check(value) -> int:
	if value is True or value == 1 or value == "1" or cstr(value).lower() == "true":
		return 1
	return 0


def _ensure_google_settings():
	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("DocType", _GOOGLE_SETTINGS):
		frappe.throw(_("Google Settings DocType is missing."), frappe.ValidationError)
	if not frappe.db.exists(_GOOGLE_SETTINGS, _GOOGLE_SETTINGS):
		doc = frappe.new_doc(_GOOGLE_SETTINGS)
		doc.enable = 0
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return frappe.get_single(_GOOGLE_SETTINGS)


def _ensure_ecom_google():
	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("DocType", _ECOM_GOOGLE):
		frappe.throw(
			_("Ecommerce Google API Settings DocType is missing. Run bench migrate."),
			frappe.ValidationError,
		)
	if not frappe.db.exists(_ECOM_GOOGLE, _ECOM_GOOGLE):
		doc = frappe.new_doc(_ECOM_GOOGLE)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return frappe.get_single(_ECOM_GOOGLE)


def _password_set(doc, fieldname: str) -> bool:
	try:
		val = doc.get_password(fieldname, raise_exception=False)
	except Exception:
		val = None
	return bool(cstr(val or "").strip())


def _mask_key(key: str) -> str:
	raw = cstr(key or "").strip()
	if not raw:
		return ""
	if len(raw) <= 8:
		return "••••••••"
	return raw[:4] + "…" + raw[-4:]


@frappe.whitelist(allow_guest=True)
def get_google_api_settings():
	"""Maps (Google Settings) + Translation (Ecommerce Google API Settings)."""
	gs = _ensure_google_settings()
	ecom = _ensure_ecom_google()
	maps_key = cstr(gs.get("api_key") or "").strip()
	return {
		"maps_enabled": bool(_as_check(gs.get("enable"))),
		"maps_api_key": maps_key,
		"maps_api_key_set": bool(maps_key),
		"maps_api_key_masked": _mask_key(maps_key) if maps_key else "",
		"translation_api_key_set": _password_set(ecom, "translation_api_key"),
		"last_translate_error": cstr(ecom.get("last_translate_error") or "") or None,
	}


@frappe.whitelist(allow_guest=True)
def save_google_api_settings(
	maps_enabled=None,
	maps_api_key=None,
	translation_api_key=None,
):
	"""Persist Maps key to Google Settings; translation key to Ecommerce Google API Settings.

	Blank / null / mask for ``translation_api_key`` keeps the stored password.
	Blank ``maps_api_key`` clears the Maps key only when explicitly sent as empty string
	after the user cleared the field — null/undefined leave it unchanged.
	"""
	gs = _ensure_google_settings()
	ecom = _ensure_ecom_google()

	if maps_enabled is not None and cstr(maps_enabled).lower() not in ("", "null", "undefined"):
		gs.enable = _as_check(maps_enabled)

	if maps_api_key is not None and cstr(maps_api_key).lower() not in ("null", "undefined"):
		key = cstr(maps_api_key).strip()
		# Ignore UI mask placeholders
		if key in ("••••••••", "********", "***"):
			pass
		else:
			gs.api_key = key
			if key and not _as_check(gs.enable):
				# Entering a key implies enable for Maps consumers.
				gs.enable = 1

	gs.save(ignore_permissions=True)

	if translation_api_key is not None and cstr(translation_api_key).lower() not in (
		"null",
		"undefined",
		"",
	):
		token = cstr(translation_api_key).strip()
		if token and token not in ("••••••••", "********", "***"):
			ecom.translation_api_key = token
			ecom.save(ignore_permissions=True)

	frappe.db.commit()
	return get_google_api_settings()


def get_google_translation_api_key() -> str:
	"""Server-side helper for taxonomy / other translators."""
	try:
		ecom = _ensure_ecom_google()
		return cstr(ecom.get_password("translation_api_key", raise_exception=False) or "").strip()
	except Exception:
		return ""


def get_google_maps_api_key() -> str:
	"""Server-side helper for TMS / Directions."""
	try:
		return cstr(frappe.db.get_single_value(_GOOGLE_SETTINGS, "api_key") or "").strip()
	except Exception:
		return ""


@frappe.whitelist(allow_guest=True)
def test_google_translation(text=None, source_lang=None, target_lang=None):
	"""Translate a short sample via Google Cloud Translation v2 (requires key)."""
	from erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n import (
		_google_translate,
		_normalize_lang,
	)

	src = _normalize_lang(source_lang) or "es"
	tgt = _normalize_lang(target_lang) or "en"
	sample = cstr(text or "").strip() or "Queso cremoso"
	key = get_google_translation_api_key()
	if not key:
		frappe.throw(
			_("Configure the Google Translation API key first."),
			frappe.ValidationError,
		)
	out = _google_translate(sample, src, tgt, api_key=key)
	if not out:
		ecom = _ensure_ecom_google()
		err = cstr(ecom.get("last_translate_error") or _("Translation failed"))
		frappe.throw(err, frappe.ValidationError)
	return {"ok": True, "source": sample, "translated": out, "source_lang": src, "target_lang": tgt}
