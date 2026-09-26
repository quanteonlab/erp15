"""Global shop UI settings (POS look, stock warnings, …).

Stored in Table Extra Schema (no migrate). Readable by any authenticated
API caller; writes require tools.settings (acting-user check when header set).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint

SCOPE = "settings.shop_ui"


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


def _as_int(val, default: int, min_v: int = 0, max_v: int = 9999) -> int:
	try:
		n = int(val)
	except Exception:
		n = default
	return max(min_v, min(max_v, n))


def _normalize_order_tag(raw) -> str:
	s = str(raw or "").strip().lower()
	s = re.sub(r"\s+", "-", s)
	s = re.sub(r"[^a-z0-9_-]", "", s)
	return s[:40]


def _normalize_pos_display(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	layout = src.get("productLayout")
	if layout not in ("grid", "list", "tradicional"):
		layout = "grid"
	shortcut = src.get("headerToggleShortcut")
	if not isinstance(shortcut, str) or not shortcut.strip():
		shortcut = "F11"
	return {
		"showDiscountName": _as_bool(src.get("showDiscountName"), True),
		"productLayout": layout,
		"loadProductImages": _as_bool(src.get("loadProductImages"), False),
		"showSessionsTab": _as_bool(src.get("showSessionsTab"), True),
		"showOrdersTab": _as_bool(src.get("showOrdersTab"), True),
		"showDisabledProducts": _as_bool(src.get("showDisabledProducts"), False),
		"showNegativeStockProducts": _as_bool(src.get("showNegativeStockProducts"), True),
		"alertOnDisabledAdd": _as_bool(src.get("alertOnDisabledAdd"), True),
		# When True, POS stock docs fail if Item has no valuation rate.
		# Default False so offline/POS sales are not blocked.
		"requireValuationRate": _as_bool(src.get("requireValuationRate"), False),
		"orderTag": _normalize_order_tag(src.get("orderTag")) or "caja",
		# Manual line % button; off hides it for all cashiers.
		"showManualDiscount": _as_bool(src.get("showManualDiscount"), True),
		# Discounts strictly above this % need admin PIN. Default 0 = all discounts.
		"discountPinThresholdPct": _as_int(src.get("discountPinThresholdPct"), 0, 0, 100),
		# Cant. / % / Precio / +/− on the tablet numpad. Default off (digits only).
		"showNumpadModeKeys": _as_bool(src.get("showNumpadModeKeys"), False),
		"receiptPaperKind": (
			"A4"
			if src.get("receiptPaperKind") == "A4"
			else "Thermal 58mm"
			if src.get("receiptPaperKind") == "Thermal 58mm"
			else "Thermal 80mm"
		),
		"showCobroPrintPreview": _as_bool(src.get("showCobroPrintPreview"), True),
		"printOnCobro": _as_bool(src.get("printOnCobro"), False),
		# Orders print modal (armado / SI / DN / PR / catalog)
		"orderPrintPaperKind": (
			"Thermal 80mm"
			if src.get("orderPrintPaperKind") == "Thermal 80mm"
			else "Thermal 58mm"
			if src.get("orderPrintPaperKind") == "Thermal 58mm"
			else "A4"
		),
		"orderPrintWarnUnpaid": _as_bool(src.get("orderPrintWarnUnpaid"), True),
		"orderPrintAllowUnpaid": _as_bool(src.get("orderPrintAllowUnpaid"), True),
		"orderPrintWarnMissingDn": _as_bool(src.get("orderPrintWarnMissingDn"), True),
		"orderPrintAllowMissingDn": _as_bool(src.get("orderPrintAllowMissingDn"), True),
		# Default False: client usually orders by qty; warehouse reweighs (armado "Peso real").
		"orderPrintClientKnowsWeight": _as_bool(src.get("orderPrintClientKnowsWeight"), False),
		# Kept for forward-compat if clients send them; personal prefs stay client-local.
		"headerToggleShortcut": shortcut.strip(),
	}


def _normalize_stock_warning(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	overrides = {}
	raw_ov = src.get("cajaOverrides")
	if isinstance(raw_ov, dict):
		for key, val in raw_ov.items():
			if not key or not isinstance(val, dict):
				continue
			row = {}
			if "allowSessionIgnore" in val and isinstance(val.get("allowSessionIgnore"), bool):
				row["allowSessionIgnore"] = val["allowSessionIgnore"]
			if "allow24hIgnore" in val and isinstance(val.get("allow24hIgnore"), bool):
				row["allow24hIgnore"] = val["allow24hIgnore"]
			if row:
				overrides[str(key)] = row
	return {
		"showWarning": _as_bool(src.get("showWarning"), True),
		"warnAtOrBelow": _as_int(src.get("warnAtOrBelow"), 0),
		"hideExactCount": _as_bool(src.get("hideExactCount"), True),
		"allowSessionIgnore": _as_bool(src.get("allowSessionIgnore"), True),
		"allow24hIgnore": _as_bool(src.get("allow24hIgnore"), True),
		"cajaOverrides": overrides,
	}


def _as_hex(val, default: str) -> str:
	s = str(val or "").strip()
	if s in ("", "transparent", "none"):
		return ""
	if s.startswith("#") and len(s) == 7:
		try:
			int(s[1:], 16)
			return s.lower()
		except Exception:
			pass
	return default


def _normalize_header_font(val) -> str:
	if val in ("sans", "serif"):
		return val
	return "system"


def _normalize_header_preset(val) -> str:
	if val in ("marketplace", "dark", "custom"):
		return val
	return "default"


def _normalize_catalog_template(val) -> str:
	if val in ("commerce", "coming_soon", "classic"):
		return val
	return "classic"


def _normalize_catalog_display(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	return {
		"pageBackgroundColor": _as_hex(src.get("pageBackgroundColor"), ""),
		"cardBackgroundColor": _as_hex(src.get("cardBackgroundColor"), "#ffffff"),
		"cardBorderColor": _as_hex(src.get("cardBorderColor"), "#e5e7eb"),
		"showCardBorder": _as_bool(src.get("showCardBorder"), True),
		"cardBorderRadius": _as_int(src.get("cardBorderRadius"), 8, 0, 32),
		"noImageTilesAtEnd": _as_bool(src.get("noImageTilesAtEnd"), True),
		"catalogTemplate": _normalize_catalog_template(src.get("catalogTemplate")),
		"showPaymentMethodPrices": _as_bool(src.get("showPaymentMethodPrices"), False),
		# Commerce catalog: qty-gated Oferta/BOGO badges vs unit red/strike.
		"showQtyPromoBadges": _as_bool(src.get("showQtyPromoBadges"), True),
		"showUnitPromoStrike": _as_bool(src.get("showUnitPromoStrike"), True),
		"headerPreset": _normalize_header_preset(src.get("headerPreset")),
		"headerSearchBackgroundColor": _as_hex(src.get("headerSearchBackgroundColor"), ""),
		"headerSearchTextColor": _as_hex(src.get("headerSearchTextColor"), ""),
		"headerNavBackgroundColor": _as_hex(src.get("headerNavBackgroundColor"), ""),
		"headerNavTextColor": _as_hex(src.get("headerNavTextColor"), ""),
		"headerFont": _normalize_header_font(src.get("headerFont")),
	}


def _normalize_companies(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	return {"enabled": _as_bool(src.get("enabled"), False)}


def _normalize_locale(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	lang = src.get("defaultLanguage")
	if lang not in ("en", "es", "zh"):
		lang = "es"
	return {"defaultLanguage": lang}


def _normalize_printers(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	rows = []
	seen = set()
	for item in src.get("printers") or []:
		if not isinstance(item, dict):
			continue
		pid = str(item.get("id") or "").strip()
		name = str(item.get("name") or "").strip()
		if not pid or not name or pid in seen:
			continue
		seen.add(pid)
		paper = item.get("preferredPaperKind")
		if paper not in ("A4", "Thermal 58mm", "Thermal 80mm"):
			paper = "Thermal 80mm"
		rows.append(
			{
				"id": pid[:80],
				"name": name[:140],
				"systemName": str(item.get("systemName") or "").strip()[:140],
				"preferredPaperKind": paper,
				"notes": str(item.get("notes") or "")[:500],
			}
		)
	default_id = str(src.get("defaultPrinterId") or "").strip() or None
	if default_id and default_id not in seen:
		default_id = rows[0]["id"] if rows else None
	if not default_id and len(rows) == 1:
		default_id = rows[0]["id"]
	return {"printers": rows, "defaultPrinterId": default_id}


def _normalize_company_transfer(raw) -> dict:
	"""Per-company bank transfer / alias shown at cobro (copy + QR)."""
	src = raw if isinstance(raw, dict) else {}
	alias = str(src.get("transferAlias") or "").strip()[:200]
	info = str(src.get("transferInfo") or "").strip()[:500]
	qr = str(src.get("transferQrPayload") or "").strip()[:500]
	return {
		"transferAlias": alias,
		"transferInfo": info,
		# Empty → clients encode transferAlias for the QR.
		"transferQrPayload": qr,
	}


def _normalize_payments(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	by_company = {}
	incoming = src.get("byCompany") if isinstance(src.get("byCompany"), dict) else {}
	for company, row in incoming.items():
		name = str(company or "").strip()
		if not name:
			continue
		by_company[name[:140]] = _normalize_company_transfer(row)
	return {"byCompany": by_company}


def get_company_transfer_info(company: str | None = None) -> dict:
	"""Return transfer alias/info for one company (empty defaults if unset)."""
	name = str(company or "").strip()
	raw = _load_raw()
	payments = _normalize_payments(raw.get("payments") if isinstance(raw, dict) else {})
	row = (payments.get("byCompany") or {}).get(name) if name else None
	return _normalize_company_transfer(row)


def set_company_transfer_info(company: str, patch: dict | None) -> dict:
	"""Merge transfer alias/info for one company into shop_ui payments.byCompany."""
	name = str(company or "").strip()
	if not name:
		frappe.throw(_("No company configured"), frappe.ValidationError)
	incoming = patch if isinstance(patch, dict) else {}
	current = _load_raw()
	payments = _normalize_payments(current.get("payments") if isinstance(current, dict) else {})
	by_company = dict(payments.get("byCompany") or {})
	merged_row = _normalize_company_transfer({**(by_company.get(name) or {}), **incoming})
	by_company[name] = merged_row
	current = current if isinstance(current, dict) else {}
	current["payments"] = {"byCompany": by_company}
	_save_raw(_normalize_bundle(current))
	return merged_row


def rename_company_transfer_info(old_company: str, new_company: str) -> None:
	"""Move payments.byCompany row when Company is renamed."""
	old = str(old_company or "").strip()
	new = str(new_company or "").strip()
	if not old or not new or old == new:
		return
	current = _load_raw()
	payments = _normalize_payments(current.get("payments") if isinstance(current, dict) else {})
	by_company = dict(payments.get("byCompany") or {})
	if old not in by_company:
		return
	if new not in by_company:
		by_company[new] = by_company.pop(old)
	else:
		by_company.pop(old, None)
	current = current if isinstance(current, dict) else {}
	current["payments"] = {"byCompany": by_company}
	_save_raw(_normalize_bundle(current))


def _normalize_bundle(data: dict | None) -> dict:
	src = data if isinstance(data, dict) else {}
	return {
		"posDisplay": _normalize_pos_display(src.get("posDisplay")),
		"stockWarning": _normalize_stock_warning(src.get("stockWarning")),
		"catalogDisplay": _normalize_catalog_display(src.get("catalogDisplay")),
		"companies": _normalize_companies(src.get("companies")),
		"locale": _normalize_locale(src.get("locale")),
		"printers": _normalize_printers(src.get("printers")),
		"payments": _normalize_payments(src.get("payments")),
	}


def _can_manage_settings() -> bool:
	"""Mirror employee_api acting-user gate without importing cycles."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	return _can_app("tools.settings")


@frappe.whitelist()
def get_shop_ui_settings():
	"""Return global shop UI settings. Missing store → empty dict (client uses defaults)."""
	raw = _load_raw()
	if not raw:
		return {"ok": True, "settings": {}, "source": "default"}
	return {"ok": True, "settings": _normalize_bundle(raw), "source": "server"}


@frappe.whitelist()
def save_shop_ui_settings(settings=None):
	"""Merge and persist global shop UI settings (POS look, stock warnings, …)."""
	if not _can_manage_settings():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"))
	if isinstance(settings, str):
		settings = frappe.parse_json(settings)
	incoming = settings if isinstance(settings, dict) else {}
	current = _load_raw()
	merged = {
		"posDisplay": _normalize_pos_display(
			{**(current.get("posDisplay") or {}), **(incoming.get("posDisplay") or {})}
		),
		"stockWarning": _normalize_stock_warning(
			{**(current.get("stockWarning") or {}), **(incoming.get("stockWarning") or {})}
		),
		"catalogDisplay": _normalize_catalog_display(
			{**(current.get("catalogDisplay") or {}), **(incoming.get("catalogDisplay") or {})}
		),
		"companies": _normalize_companies(
			{**(current.get("companies") or {}), **(incoming.get("companies") or {})}
		),
		"locale": _normalize_locale(
			{**(current.get("locale") or {}), **(incoming.get("locale") or {})}
		),
		"printers": _normalize_printers(
			{**(current.get("printers") or {}), **(incoming.get("printers") or {})}
		),
		"payments": _normalize_payments(
			{
				"byCompany": {
					**((current.get("payments") or {}).get("byCompany") or {}),
					**((incoming.get("payments") or {}).get("byCompany") or {}),
				}
			}
		),
	}
	_save_raw(merged)
	return {"ok": True, "settings": merged, "source": "server"}


def require_valuation_rate() -> bool:
	"""POS stock policy: require Item valuation rate. Default False (allow zero)."""
	raw = _load_raw()
	pos = (raw.get("posDisplay") or {}) if isinstance(raw, dict) else {}
	return _as_bool(pos.get("requireValuationRate"), False)
