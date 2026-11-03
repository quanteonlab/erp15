"""Site-wide shop UI settings (POS look, catalog template, stock warnings, …).

Stored in Table Extra Schema (no migrate). One blob for the whole site — every
user and guest sees the same catalogTemplate / display prefs.
Readable with allow_guest; writes require tools.settings (acting-user check).
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


def _as_money(val, default: float = 0.0, max_v: float = 10_000_000.0) -> float:
	"""Non-negative amount with 2 decimals; anything unparseable (or NaN) becomes the default."""
	try:
		n = float(val)
	except (TypeError, ValueError):
		return default
	if n != n:
		return default
	return round(max(0.0, min(max_v, n)), 2)


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
	# coming_soon kept for legacy saved settings; UI no longer offers it.
	# Default for new / unset sites: Nova Mobile (not classic).
	if val in ("commerce", "nova_mobile", "storefront", "coming_soon", "classic"):
		return val
	return "nova_mobile"


def _normalize_storefront_radius(val) -> str:
	if val in ("sharp", "round"):
		return val
	return "soft"


def _normalize_storefront_font(val) -> str:
	if val in ("system", "serif"):
		return val
	return "brand"


STOREFRONT_SECTIONS = ("banners", "delivery", "categories", "coupons", "deals", "hot")
# Whole features an admin can switch off: coupons (home row + code box in the bag), the barcode-scan icon,
# the language switch, and the Home page itself (open straight on the product list).
STOREFRONT_FEATURES = ("coupons", "scan", "language", "home")


def _normalize_storefront_header_style(val) -> str:
	if val in ("brand", "gradient"):
		return val
	return "light"


def _normalize_storefront_disabled_features(val) -> list:
	"""Features switched off. Unknown ids are dropped; anything new stays enabled by default."""
	return [x for x in _as_string_list(val, max_items=len(STOREFRONT_FEATURES), max_len=20) if x in STOREFRONT_FEATURES]


def _normalize_storefront_hidden_sections(val) -> list:
	"""Home sections switched off. Unknown ids are dropped; new sections stay visible by default."""
	return [x for x in _as_string_list(val, max_items=len(STOREFRONT_SECTIONS), max_len=20) if x in STOREFRONT_SECTIONS]


STOREFRONT_BANNER_STYLES = ("brand", "dark", "light")
STOREFRONT_BANNER_MAX = 8


def _normalize_banner_link(val) -> str:
	"""Where a banner goes. Only known shapes survive; anything else is dropped (no javascript:, no //host)."""
	s = str(val or "").strip()[:200] if isinstance(val, str) else ""
	if s in ("hot", "offers", "coupons"):
		return s
	if s.startswith(("cat:", "product:")) and s.split(":", 1)[1].strip():
		return s
	if s.startswith("/") and not s.startswith("//"):
		return s
	if s.startswith(("https://", "http://")):
		return s
	return ""


def _normalize_iso_date(val) -> str:
	"""YYYY-MM-DD or empty."""
	s = val.strip() if isinstance(val, str) else ""
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
		return ""
	try:
		import datetime

		datetime.date.fromisoformat(s)
	except ValueError:
		return ""
	return s


def _i18n_text(val, max_len: int) -> dict:
	src = val if isinstance(val, dict) else {}
	return {
		lang: (src.get(lang).strip()[:max_len] if isinstance(src.get(lang), str) else "")
		for lang in ("zh", "es")
	}


def _normalize_storefront_banners(raw) -> list:
	"""Promotion banners for the storefront home (max 8). A banner needs an image or a title to be shown."""
	if not isinstance(raw, (list, tuple)):
		return []
	out = []
	seen = set()
	for row in raw:
		if not isinstance(row, dict):
			continue
		bid = re.sub(r"[^A-Za-z0-9_-]", "", str(row.get("id") or ""))[:24] or frappe.generate_hash(length=8)
		if bid in seen:
			continue
		image = _normalize_logo_url(row.get("image"))
		title = _i18n_text(row.get("title"), 80)
		if not image and not any(title.values()):
			continue
		seen.add(bid)
		style = row.get("style")
		out.append(
			{
				"id": bid,
				"image": image,
				"title": title,
				"subtitle": _i18n_text(row.get("subtitle"), 140),
				"cta": _i18n_text(row.get("cta"), 30),
				"style": style if style in STOREFRONT_BANNER_STYLES else "brand",
				"link": _normalize_banner_link(row.get("link")),
				"validFrom": _normalize_iso_date(row.get("validFrom")),
				"validUpto": _normalize_iso_date(row.get("validUpto")),
			}
		)
		if len(out) >= STOREFRONT_BANNER_MAX:
			break
	return out


def _normalize_logo_url(val) -> str:
	"""Storefront logo: a local /files/ path or an http(s) URL (nothing else is rendered)."""
	s = str(val or "").strip()[:500]
	if s.startswith("/files/") or s.startswith("https://") or s.startswith("http://"):
		return s
	return ""


def _normalize_pedidos_default_seller_scope(val) -> str:
	"""Default Pedidos table toggle: assigned clients | seller link | all.

	Unset / unknown → ``assigned`` (salesman's clients). Explicit ``all`` / ``todos`` → ``""``.
	"""
	s = str(val or "").strip().lower()
	if s in ("all", "todos"):
		return ""
	if s in ("mine", "my", "seller_ref", "mi_enlace", "link"):
		return "mine"
	# assigned (default) — also treat empty/null as assigned so new tenants show clients.
	return "assigned"


def _normalize_qty_stepper_labels(val) -> str:
	if val in ("with_step", "symbol"):
		return val
	return "symbol"


def _normalize_card_click_mode(val) -> str:
	if val in ("controls_only", "toggle", "add_only", "increment"):
		return val
	return "controls_only"


def _normalize_cart_fab_side(val) -> str:
	if val in ("right", "left"):
		return val
	return "left"


def _normalize_catalog_promo_conflict(val) -> str:
	if val in ("best_price", "priority", "highest_threshold"):
		return val
	return "best_price"


def _as_string_list(val, max_items: int = 500, max_len: int = 140) -> list:
	"""Deduped trimmed strings (e.g. Item Group names to hide in catalog)."""
	if not isinstance(val, (list, tuple)):
		return []
	out = []
	seen = set()
	for raw in val:
		if not isinstance(raw, str):
			continue
		s = raw.strip()[:max_len]
		if not s or s in seen:
			continue
		seen.add(s)
		out.append(s)
		if len(out) >= max_items:
			break
	return out


def _normalize_catalog_display(raw) -> dict:
	src = raw if isinstance(raw, dict) else {}
	return {
		"pageBackgroundColor": _as_hex(src.get("pageBackgroundColor"), ""),
		"cardBackgroundColor": _as_hex(src.get("cardBackgroundColor"), "#ffffff"),
		"cardBorderColor": _as_hex(src.get("cardBorderColor"), "#e5e7eb"),
		"showCardBorder": _as_bool(src.get("showCardBorder"), True),
		"cardBorderRadius": _as_int(src.get("cardBorderRadius"), 8, 0, 32),
		"noImageTilesAtEnd": _as_bool(src.get("noImageTilesAtEnd"), True),
		# Site-wide for every user/guest (/catalog + /ops/orders).
		"catalogTemplate": _normalize_catalog_template(src.get("catalogTemplate")),
		"showPaymentMethodPrices": _as_bool(src.get("showPaymentMethodPrices"), False),
		# Commerce catalog: qty-gated Oferta/BOGO badges vs unit red/strike.
		"showQtyPromoBadges": _as_bool(src.get("showQtyPromoBadges"), True),
		"showUnitPromoStrike": _as_bool(src.get("showUnitPromoStrike"), True),
		"showCardBrandCode": _as_bool(src.get("showCardBrandCode"), False),
		"showPriceCents": _as_bool(src.get("showPriceCents"), False),
		"qtyStepperLabels": _normalize_qty_stepper_labels(src.get("qtyStepperLabels")),
		"cardClickMode": _normalize_card_click_mode(src.get("cardClickMode")),
		"cartFabSide": _normalize_cart_fab_side(src.get("cartFabSide")),
		"catalogPromoConflictMode": _normalize_catalog_promo_conflict(
			src.get("catalogPromoConflictMode")
		),
		# Pedidos table + Orden FAB: lookback days for assigned clients (default 30).
		"ordenPedidosDays": _as_int(src.get("ordenPedidosDays"), 30, 1, 365),
		# Tablas → Pedidos default toggle for salesmen: assigned | mine | all ("").
		"pedidosDefaultSellerScope": _normalize_pedidos_default_seller_scope(
			src.get("pedidosDefaultSellerScope")
		),
		# When True, PoD/payment that fully settles a Delivery order auto-moves to Completado.
		# Default False — ops closes Completado manually (or jumps with UI warning).
		"autoCompleteOnFullPayment": _as_bool(src.get("autoCompleteOnFullPayment"), False),
		# Site-wide default depósito for catalog stock counts + Products table Stock column.
		# Empty = no default (catalog sums all bins; Products Stock stays read-only until picked).
		"defaultWarehouse": str(src.get("defaultWarehouse") or "").strip()[:140],
		# Item Group names hidden from catalog category rails. Empty = show all.
		"hiddenCatalogCategories": _as_string_list(src.get("hiddenCatalogCategories")),
		"headerPreset": _normalize_header_preset(src.get("headerPreset")),
		"headerSearchBackgroundColor": _as_hex(src.get("headerSearchBackgroundColor"), ""),
		"headerSearchTextColor": _as_hex(src.get("headerSearchTextColor"), ""),
		"headerNavBackgroundColor": _as_hex(src.get("headerNavBackgroundColor"), ""),
		"headerNavTextColor": _as_hex(src.get("headerNavTextColor"), ""),
		"headerFont": _normalize_header_font(src.get("headerFont")),
		# Storefront template branding/theme. Empty title/logo/colors = follow the Company / built-in look.
		"storefrontTitle": str(src.get("storefrontTitle") or "").strip()[:60],
		"storefrontLogoUrl": _normalize_logo_url(src.get("storefrontLogoUrl")),
		"storefrontAccentColor": _as_hex(src.get("storefrontAccentColor"), ""),
		"storefrontBackgroundColor": _as_hex(src.get("storefrontBackgroundColor"), ""),
		"storefrontSurfaceColor": _as_hex(src.get("storefrontSurfaceColor"), ""),
		"storefrontTextColor": _as_hex(src.get("storefrontTextColor"), ""),
		"storefrontAccent2Color": _as_hex(src.get("storefrontAccent2Color"), ""),
		"storefrontPriceColor": _as_hex(src.get("storefrontPriceColor"), ""),
		"storefrontHeaderStyle": _normalize_storefront_header_style(src.get("storefrontHeaderStyle")),
		# Trending-search chips under the search box (home page); empty = row hidden.
		"storefrontSearchHints": _as_string_list(src.get("storefrontSearchHints"), max_items=8, max_len=30),
		"storefrontHiddenSections": _normalize_storefront_hidden_sections(src.get("storefrontHiddenSections")),
		"storefrontDisabledFeatures": _normalize_storefront_disabled_features(src.get("storefrontDisabledFeatures")),
		"storefrontBanners": _normalize_storefront_banners(src.get("storefrontBanners")),
		# Delivery pricing for the storefront (promos live in delivery_promos.py). Off = delivery stays a plain flag.
		"deliveryEnabled": _as_bool(src.get("deliveryEnabled"), False),
		"deliveryBaseFee": _as_money(src.get("deliveryBaseFee")),
		"storefrontRadius": _normalize_storefront_radius(src.get("storefrontRadius")),
		"storefrontFont": _normalize_storefront_font(src.get("storefrontFont")),
	}


def _company_custom_default_warehouse(company=None) -> str:
	"""Company.custom_default_warehouse when the custom field exists."""
	company = (
		str(company or "").strip()
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or ""
	)
	if not company or not frappe.db.has_column("Company", "custom_default_warehouse"):
		return ""
	return str(frappe.db.get_value("Company", company, "custom_default_warehouse") or "").strip()


def _sync_company_default_warehouse(warehouse: str) -> None:
	"""Keep Company.custom_default_warehouse aligned with catalog display default."""
	company = (
		frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	if not company or not frappe.db.has_column("Company", "custom_default_warehouse"):
		return
	wh = str(warehouse or "").strip() or None
	current = str(frappe.db.get_value("Company", company, "custom_default_warehouse") or "").strip() or None
	if current == wh:
		return
	frappe.db.set_value(
		"Company",
		company,
		"custom_default_warehouse",
		wh,
		update_modified=False,
	)


def resolve_default_warehouse(explicit=None) -> str | None:
	"""Warehouse for catalog/stock counts: explicit → shop_ui → company default."""
	wh = str(explicit or "").strip()
	if wh and frappe.db.exists("Warehouse", wh):
		return wh
	raw = _load_raw()
	cd = raw.get("catalogDisplay") if isinstance(raw, dict) else {}
	wh = str((cd or {}).get("defaultWarehouse") or "").strip()
	if wh and frappe.db.exists("Warehouse", wh):
		return wh
	wh = _company_custom_default_warehouse()
	if wh and frappe.db.exists("Warehouse", wh):
		return wh
	return None


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


def _storefront_brand() -> dict:
	"""Public shop identity from the default Company (name + logo). Read-only, never persisted."""
	company = (
		frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or ""
	)
	if not company or not frappe.db.exists("Company", company):
		return {"companyName": "", "companyLogo": ""}
	name, logo = frappe.db.get_value("Company", company, ["company_name", "company_logo"]) or ("", "")
	return {
		"companyName": str(name or company).strip(),
		"companyLogo": _normalize_logo_url(logo),
	}


@frappe.whitelist(allow_guest=True)
def get_shop_ui_settings():
	"""Return site-wide shop UI settings (same for every user/guest).

	Missing store → empty dict (client uses defaults).
	Seeds catalogDisplay.defaultWarehouse from Company when unset (not persisted).
	Also returns a read-only `branding` {companyName, companyLogo} so the guest storefront
	can title itself after the Company without a second request.
	"""
	raw = _load_raw()
	if not raw:
		seeded = _company_custom_default_warehouse()
		settings = (
			{"catalogDisplay": {"defaultWarehouse": seeded}} if seeded else {}
		)
		return {"ok": True, "settings": settings, "source": "default", "branding": _storefront_brand()}
	settings = _normalize_bundle(raw)
	cd = dict(settings.get("catalogDisplay") or {})
	if not cd.get("defaultWarehouse"):
		seeded = _company_custom_default_warehouse()
		if seeded:
			cd["defaultWarehouse"] = seeded
			settings["catalogDisplay"] = cd
	return {"ok": True, "settings": settings, "source": "server", "branding": _storefront_brand()}


@frappe.whitelist(allow_guest=True)
def save_shop_ui_settings(settings=None):
	"""Merge and persist site-wide shop UI settings (POS look, catalog template, …).

	Writes require tools.settings (acting-user check when header set).
	"""
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
		"opsKiosk": {
			**(current.get("opsKiosk") or {}),
			**(incoming.get("opsKiosk") or {}),
		},
	}
	_save_raw(merged)
	# Keep Company depot in sync so TMS / remito defaults match catalog stock.
	try:
		_sync_company_default_warehouse(
			(merged.get("catalogDisplay") or {}).get("defaultWarehouse") or ""
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "shop_ui sync default warehouse")
	return {"ok": True, "settings": merged, "source": "server"}


def require_valuation_rate() -> bool:
	"""POS stock policy: require Item valuation rate. Default False (allow zero)."""
	raw = _load_raw()
	pos = (raw.get("posDisplay") or {}) if isinstance(raw, dict) else {}
	return _as_bool(pos.get("requireValuationRate"), False)


def _require_settings_staff() -> None:
	"""Settings writes that take a file need a real acting staff user (not just the shared service key)."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username

	if not _acting_username():
		frappe.throw(_("Sign in as staff to do this"), frappe.PermissionError)
	if not _can_manage_settings():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"), frappe.PermissionError)


@frappe.whitelist()
def upload_storefront_banner(filedata=None):
	"""Store an uploaded promotion banner image as a public /files/ JPEG and return its URL.

	filedata: data-URL or raw base64 (max ~12 MB). The URL is then saved by the client inside
	catalogDisplay.storefrontBanners via save_shop_ui_settings.
	"""
	import base64

	_require_settings_staff()
	data = filedata.strip() if isinstance(filedata, str) else ""
	if not data:
		frappe.throw(_("Provide filedata"), frappe.ValidationError)
	raw = data.split(",", 1)[1] if "," in data else data
	if len(raw) > 16_000_000:  # ~12 MB decoded
		frappe.throw(_("Image is too large (max 12 MB)"), frappe.ValidationError)
	try:
		image_bytes = base64.b64decode(raw, validate=False)
	except Exception:
		frappe.throw(_("Invalid image data"), frappe.ValidationError)

	from erpnext.erpnext_integrations.ecommerce_api.image_cdn import materialize_storefront_banner_bytes

	return {"ok": True, "url": materialize_storefront_banner_bytes(image_bytes, commit=True)}
