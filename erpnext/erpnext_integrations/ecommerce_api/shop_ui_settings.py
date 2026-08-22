"""Global shop UI settings (POS look, stock warnings, …).

Stored in Table Extra Schema (no migrate). Readable by any authenticated
API caller; writes require tools.settings (acting-user check when header set).
"""

from __future__ import annotations

import json

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


def _normalize_pos_display(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	layout = src.get("productLayout")
	if layout not in ("grid", "list"):
		layout = "grid"
	shortcut = src.get("headerToggleShortcut")
	if not isinstance(shortcut, str) or not shortcut.strip():
		shortcut = "F11"
	return {
		"showDiscountName": _as_bool(src.get("showDiscountName"), True),
		"productLayout": layout,
		"showSessionsTab": _as_bool(src.get("showSessionsTab"), True),
		"showOrdersTab": _as_bool(src.get("showOrdersTab"), True),
		"showDisabledProducts": _as_bool(src.get("showDisabledProducts"), False),
		"showNegativeStockProducts": _as_bool(src.get("showNegativeStockProducts"), True),
		"alertOnDisabledAdd": _as_bool(src.get("alertOnDisabledAdd"), True),
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
		"hideExactCount": _as_bool(src.get("hideExactCount"), False),
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


def _normalize_catalog_display(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	return {
		"pageBackgroundColor": _as_hex(src.get("pageBackgroundColor"), ""),
		"cardBackgroundColor": _as_hex(src.get("cardBackgroundColor"), "#ffffff"),
		"cardBorderColor": _as_hex(src.get("cardBorderColor"), "#e5e7eb"),
		"showCardBorder": _as_bool(src.get("showCardBorder"), True),
		"cardBorderRadius": _as_int(src.get("cardBorderRadius"), 8, 0, 32),
		"noImageTilesAtEnd": _as_bool(src.get("noImageTilesAtEnd"), True),
	}


def _normalize_bundle(data: dict | None) -> dict:
	src = data if isinstance(data, dict) else {}
	return {
		"posDisplay": _normalize_pos_display(src.get("posDisplay")),
		"stockWarning": _normalize_stock_warning(src.get("stockWarning")),
		"catalogDisplay": _normalize_catalog_display(src.get("catalogDisplay")),
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
	}
	_save_raw(merged)
	return {"ok": True, "settings": merged, "source": "server"}
