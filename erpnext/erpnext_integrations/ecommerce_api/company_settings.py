"""Company profile settings for Tools → Settings → Company.

Reads/writes ERPNext Company fields (name, contact, tax, currency) plus
shop-wide defaults stored in shop UI settings (default language, multi-company).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

_EDITABLE_FIELDS = (
	"abbr",
	"default_currency",
	"country",
	"tax_id",
	"phone_no",
	"email",
	"website",
	"domain",
)

# Product default. Enabled on demand so Settings → Company always offers them.
DEFAULT_CURRENCY = "ARS"

# LatAm + Asia (+ common trade currencies). Order = dropdown priority (ARS first).
_PREFERRED_CURRENCIES = (
	# Latin America
	"ARS",
	"BOB",
	"BRL",
	"CLP",
	"COP",
	"CRC",
	"CUP",
	"DOP",
	"GTQ",
	"HNL",
	"MXN",
	"NIO",
	"PAB",
	"PEN",
	"PYG",
	"UYU",
	"VEF",
	"VES",
	# China & Asia
	"CNY",
	"HKD",
	"TWD",
	"MOP",
	"JPY",
	"KRW",
	"INR",
	"IDR",
	"MYR",
	"SGD",
	"THB",
	"VND",
	"PHP",
	"PKR",
	"BDT",
	"LKR",
	"NPR",
	"MMK",
	"KHR",
	"LAK",
	"MNT",
	# Common extras (often already enabled from setup wizard)
	"USD",
	"EUR",
	"GBP",
	"CHF",
	"AUD",
	"AED",
)


def _ensure_preferred_currencies() -> list[str]:
	"""Enable LatAm + Asia currencies; return enabled list with preferred codes first."""
	changed = False
	for code in _PREFERRED_CURRENCIES:
		if frappe.db.exists("Currency", code):
			if not cint(frappe.db.get_value("Currency", code, "enabled")):
				frappe.db.set_value("Currency", code, "enabled", 1, update_modified=False)
				changed = True
			continue
		try:
			frappe.get_doc(
				{
					"doctype": "Currency",
					"currency_name": code,
					"enabled": 1,
					"fraction_units": 100,
					"symbol": code,
				}
			).insert(ignore_permissions=True)
			changed = True
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"ensure currency {code}")
	if changed:
		frappe.db.commit()

	enabled = frappe.get_all(
		"Currency",
		filters={"enabled": 1},
		pluck="name",
		order_by="name asc",
		ignore_permissions=True,
	) or []
	order = {c: i for i, c in enumerate(_PREFERRED_CURRENCIES)}
	preferred = sorted((c for c in enabled if c in order), key=lambda c: order[c])
	rest = [c for c in enabled if c not in order]
	return preferred + rest


def list_enabled_currencies() -> list[str]:
	"""Public helper: preferred currencies enabled + sorted for Settings dropdowns."""
	return _ensure_preferred_currencies()


def _can_manage() -> bool:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	return _can_app("tools.settings")


def _resolve_target_company(company=None) -> str:
	from erpnext.erpnext_integrations.ecommerce_api.company_context import (
		allowed_company_names,
		resolve_company,
	)

	name = (company or "").strip() or (resolve_company() or "")
	if not name:
		frappe.throw(_("No company configured"), frappe.ValidationError)
	allowed = allowed_company_names()
	if allowed and name not in allowed:
		frappe.throw(_("Not permitted for company {0}").format(name), frappe.PermissionError)
	if not frappe.db.exists("Company", name):
		frappe.throw(_("Company {0} not found").format(name), frappe.DoesNotExistError)
	return name


def _company_row(name: str) -> dict:
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Company", name)
	frappe.flags.ignore_permissions = False
	return {
		"name": doc.name,
		"company_name": doc.company_name or doc.name,
		"abbr": doc.abbr or "",
		"default_currency": doc.default_currency or DEFAULT_CURRENCY,
		"country": doc.country or "",
		"tax_id": doc.tax_id or "",
		"phone_no": doc.phone_no or "",
		"email": doc.email or "",
		"website": doc.website or "",
		"domain": doc.domain or "",
		"company_logo": doc.company_logo or "",
	}


def _locale_defaults() -> dict:
	from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import get_shop_ui_settings

	bundle = get_shop_ui_settings() or {}
	settings = bundle.get("settings") if isinstance(bundle, dict) else {}
	locale = (settings or {}).get("locale") if isinstance(settings, dict) else {}
	if not isinstance(locale, dict):
		locale = {}
	companies = (settings or {}).get("companies") if isinstance(settings, dict) else {}
	if not isinstance(companies, dict):
		companies = {}
	lang = locale.get("defaultLanguage")
	if lang not in ("en", "es", "zh"):
		lang = "es"
	return {
		"default_language": lang,
		"multi_company_enabled": bool(companies.get("enabled")),
	}


@frappe.whitelist()
def get_company_settings(company=None):
	"""Return editable Company profile + shop locale defaults."""
	name = _resolve_target_company(company)
	row = _company_row(name)
	locale = _locale_defaults()
	from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
		get_company_transfer_info,
	)

	transfer = get_company_transfer_info(name)
	currencies = list_enabled_currencies()
	countries = frappe.get_all(
		"Country",
		pluck="name",
		order_by="name asc",
		ignore_permissions=True,
	)
	return {
		"ok": True,
		"company": row,
		"default_language": locale["default_language"],
		"multi_company_enabled": locale["multi_company_enabled"],
		"transfer_alias": transfer.get("transferAlias") or "",
		"transfer_info": transfer.get("transferInfo") or "",
		"transfer_qr_payload": transfer.get("transferQrPayload") or "",
		"currencies": currencies or [],
		"countries": countries or [],
	}


def _clean_str(val, max_len: int = 140) -> str:
	return str(val or "").strip()[:max_len]


def _sync_global_default_currency(currency: str) -> None:
	"""Keep Global Defaults + defaults cache aligned with Company currency."""
	currency = (currency or "").strip() or DEFAULT_CURRENCY
	try:
		current = frappe.db.get_single_value("Global Defaults", "default_currency")
		if current != currency:
			frappe.db.set_value("Global Defaults", None, "default_currency", currency)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "sync Global Defaults currency")
	try:
		# System Settings / tabDefaultValue — used by get_global_default("currency")
		if frappe.defaults.get_global_default("currency") != currency:
			frappe.defaults.set_global_default("currency", currency)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "sync global default currency")


def _sync_price_list_currencies(old_currency: str, new_currency: str) -> None:
	"""Retarget selling/buying lists that still use the previous company currency."""
	old_currency = (old_currency or "").strip()
	new_currency = (new_currency or "").strip() or DEFAULT_CURRENCY
	if not old_currency or old_currency == new_currency:
		return
	for name in frappe.get_all(
		"Price List",
		filters={"currency": old_currency},
		pluck="name",
		ignore_permissions=True,
	) or []:
		frappe.db.set_value("Price List", name, "currency", new_currency, update_modified=False)


def _apply_company_currency(company: str, new_currency: str) -> bool:
	"""Persist Company.default_currency so Settings → Company survives reload.

	ERPNext blocks currency changes when:
	- default accounts still use the old Account.account_currency, or
	- submitted sales/purchase docs exist (validate_currency).

	Tools Settings is the product path to fix wizard USD defaults on live shops,
	so we align CoA account currencies first, then write Company + Global Defaults
	via db.set_value (avoids the transaction lock while keeping books' historical
	invoice currency unchanged).
	"""
	new_currency = _clean_str(new_currency) or DEFAULT_CURRENCY
	if not frappe.db.exists("Currency", new_currency):
		frappe.throw(_("Currency {0} not found").format(new_currency), frappe.ValidationError)
	if not cint(frappe.db.get_value("Currency", new_currency, "enabled")):
		frappe.db.set_value("Currency", new_currency, "enabled", 1, update_modified=False)

	old_currency = frappe.db.get_value("Company", company, "default_currency") or ""
	if old_currency == new_currency:
		_sync_global_default_currency(new_currency)
		return False

	# Align chart accounts that still mirror the previous company currency (or blank).
	if old_currency:
		frappe.db.sql(
			"""
			update `tabAccount`
			set account_currency = %s
			where company = %s
			  and ifnull(account_currency, '') in (%s, '')
			""",
			(new_currency, company, old_currency),
		)
	else:
		frappe.db.sql(
			"""
			update `tabAccount`
			set account_currency = %s
			where company = %s
			  and ifnull(account_currency, '') = ''
			""",
			(new_currency, company),
		)

	frappe.db.set_value("Company", company, "default_currency", new_currency)
	_sync_global_default_currency(new_currency)
	_sync_price_list_currencies(old_currency, new_currency)
	frappe.clear_cache(doctype="Company")
	frappe.clear_cache(doctype="Account")
	return True


def _coerce_company_domain(val: str) -> str:
	"""Company.domain is free text historically, but invalid values (e.g. 'shopify')
	are confusing. Prefer known Domain DocType names; otherwise keep blank."""
	val = _clean_str(val)
	if not val:
		return ""
	if frappe.db.exists("DocType", "Domain") and frappe.db.exists("Domain", val):
		return val
	# Allow common ERPNext domain labels even if Domain row missing.
	if val in ("Manufacturing", "Retail", "Distribution", "Services", "Education", "Healthcare"):
		return val
	return ""


def _update_single_company_link_sql(doctype: str, fieldname: str, old: str, new: str) -> None:
	"""Update a Single DocType Company link without loading its Python controller.

	Orphan DocTypes (Shopify Setting, Webshop Settings, …) can raise
	DoesNotExistError('Module X not found') when their app is not on apps.txt /
	module_app — Frappe's rename_doc only catches ImportError for singles.
	"""
	frappe.db.sql(
		"""
		update `tabSingles`
		set value = %s
		where doctype = %s and field = %s and value = %s
		""",
		(new, doctype, fieldname, old),
	)


def _safe_rename_company(old_name: str, new_name: str) -> None:
	"""Rename Company even when orphan Singles (Shopify Setting, etc.) break get_doc."""
	import frappe.model.rename_doc as rename_mod

	original = rename_mod.update_link_field_values

	def safe_update(link_fields, old, new, doctype):
		singles = [f for f in link_fields if f.get("issingle")]
		non_singles = [f for f in link_fields if not f.get("issingle")]
		if non_singles:
			original(non_singles, old, new, doctype)
		for field in singles:
			parent = field["parent"]
			fieldname = field["fieldname"]
			try:
				single_doc = frappe.get_doc(parent)
				if single_doc.get(fieldname) == old:
					single_doc.set(fieldname, new)
					single_doc.flags.ignore_mandatory = True
					single_doc.flags.ignore_links = True
					single_doc.save(ignore_permissions=True)
			except (ImportError, frappe.DoesNotExistError):
				_update_single_company_link_sql(parent, fieldname, old, new)
			except Exception:
				try:
					_update_single_company_link_sql(parent, fieldname, old, new)
				except Exception:
					frappe.log_error(
						title=f"Company rename: skip single {parent}.{fieldname}",
						message=frappe.get_traceback(),
					)

	rename_mod.update_link_field_values = safe_update
	try:
		frappe.rename_doc("Company", old_name, new_name, force=True, merge=False)
	finally:
		rename_mod.update_link_field_values = original


@frappe.whitelist()
def save_company_settings(company=None, settings=None):
	"""Update Company fields and/or shop default language / multi-company flag."""
	if not _can_manage():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"), frappe.PermissionError)

	if isinstance(settings, str):
		settings = frappe.parse_json(settings)
	incoming = settings if isinstance(settings, dict) else {}

	name = _resolve_target_company(company or incoming.get("name") or incoming.get("company"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Company", name)
	frappe.flags.ignore_permissions = False

	new_company_name = _clean_str(incoming.get("company_name"))
	renamed_from = None
	if new_company_name and new_company_name != (doc.company_name or doc.name):
		# Company autoname is field:company_name — renaming updates linked Singles
		# (e.g. Shopify Setting.company). Orphan modules must not abort Save.
		renamed_from = doc.name
		_safe_rename_company(doc.name, new_company_name)
		frappe.db.commit()
		name = new_company_name
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Company", name)
		frappe.flags.ignore_permissions = False

	changed = False
	currency_to_set = None
	if "default_currency" in incoming:
		currency_to_set = _clean_str(incoming.get("default_currency")) or DEFAULT_CURRENCY

	for field in _EDITABLE_FIELDS:
		if field not in incoming:
			continue
		if field == "default_currency":
			# Applied via _apply_company_currency after save — doc.save() would
			# otherwise fail validate_default_accounts / validate_currency.
			continue
		val = _clean_str(incoming.get(field))
		if field == "domain":
			val = _coerce_company_domain(val)
		if field == "country" and val and not frappe.db.exists("Country", val):
			frappe.throw(_("Country {0} not found").format(val), frappe.ValidationError)
		if field == "email" and val and "@" not in val:
			frappe.throw(_("Invalid email"), frappe.ValidationError)
		if (doc.get(field) or "") != val:
			doc.set(field, val or None)
			changed = True

	if changed:
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	if currency_to_set is not None:
		if _apply_company_currency(name, currency_to_set):
			frappe.db.commit()
		else:
			# Still flush Global Defaults when already matching (e.g. empty → ARS).
			frappe.db.commit()

	# Shop-wide locale / multi-company (Table Extra Schema via shop_ui_settings).
	locale_patch = {}
	if "default_language" in incoming:
		lang = str(incoming.get("default_language") or "").strip()
		if lang not in ("en", "es", "zh"):
			frappe.throw(_("Unsupported language"), frappe.ValidationError)
		locale_patch["locale"] = {"defaultLanguage": lang}
	if "multi_company_enabled" in incoming:
		locale_patch["companies"] = {"enabled": bool(cint(incoming.get("multi_company_enabled")))}

	if locale_patch:
		from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
			save_shop_ui_settings,
		)

		save_shop_ui_settings(locale_patch)

	# Per-company transfer / alias defaults for cobro (copy + QR).
	transfer_keys = ("transfer_alias", "transfer_info", "transfer_qr_payload")
	if any(k in incoming for k in transfer_keys) or renamed_from:
		from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
			rename_company_transfer_info,
			set_company_transfer_info,
		)

		if renamed_from and renamed_from != name:
			rename_company_transfer_info(renamed_from, name)

		if any(k in incoming for k in transfer_keys):
			patch = {}
			if "transfer_alias" in incoming:
				patch["transferAlias"] = _clean_str(incoming.get("transfer_alias"), 200)
			if "transfer_info" in incoming:
				patch["transferInfo"] = _clean_str(incoming.get("transfer_info"), 500)
			if "transfer_qr_payload" in incoming:
				patch["transferQrPayload"] = _clean_str(incoming.get("transfer_qr_payload"), 500)
			set_company_transfer_info(name, patch)

	# Refresh Global Defaults default_company if rename moved the active default.
	try:
		gd = frappe.db.get_single_value("Global Defaults", "default_company")
		if gd and not frappe.db.exists("Company", gd) and frappe.db.exists("Company", name):
			frappe.db.set_value("Global Defaults", None, "default_company", name)
			frappe.db.commit()
	except Exception:
		pass

	return get_company_settings(company=name)


@frappe.whitelist()
def upload_company_logo(company=None, filedata=None, filename="logo.png", source_url=None):
	"""
	Upload or pull a company logo and persist a durable local /files/ copy.

	- filedata: data-URL or raw base64
	- source_url: remote http(s) (fetched via imgproxy when possible, then direct)

	Sets Company.company_logo to the local file URL. Display can still wrap that
	path with imgproxy for resized variants.
	"""
	import base64

	if not _can_manage():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"), frappe.PermissionError)

	name = _resolve_target_company(company)
	from erpnext.erpnext_integrations.ecommerce_api.image_cdn import (
		download_image_prefer_imgproxy,
		materialize_company_logo_bytes,
	)

	image_bytes = None
	filedata = (filedata or "").strip() if isinstance(filedata, str) else filedata
	source_url = (source_url or "").strip() if isinstance(source_url, str) else ""

	if filedata:
		raw = filedata.split(",", 1)[1] if isinstance(filedata, str) and "," in filedata else filedata
		try:
			image_bytes = base64.b64decode(raw)
		except Exception:
			frappe.throw(_("Invalid logo file data"), frappe.ValidationError)
	elif source_url:
		try:
			image_bytes = download_image_prefer_imgproxy(source_url, max_edge=1600)
		except Exception as exc:
			frappe.throw(_("Could not download logo: {0}").format(str(exc)), frappe.ValidationError)
	else:
		frappe.throw(_("Provide filedata or source_url"), frappe.ValidationError)

	if not image_bytes:
		frappe.throw(_("Empty logo image"), frappe.ValidationError)

	file_url = materialize_company_logo_bytes(name, image_bytes, commit=True)
	payload = get_company_settings(company=name)
	payload["company_logo"] = file_url
	payload["ok"] = True
	return payload


@frappe.whitelist()
def clear_company_logo(company=None):
	"""Unset Company.company_logo (local File rows left for history)."""
	if not _can_manage():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"), frappe.PermissionError)
	name = _resolve_target_company(company)
	frappe.db.set_value("Company", name, "company_logo", None)
	frappe.db.commit()
	return get_company_settings(company=name)


# Parent numeric fields: transaction currency → company/base (1:1 after rebase).
_PARENT_BASE_MAP = {
	"Purchase Order": (
		("total", "base_total"),
		("net_total", "base_net_total"),
		("taxes_and_charges_added", "base_taxes_and_charges_added"),
		("taxes_and_charges_deducted", "base_taxes_and_charges_deducted"),
		("total_taxes_and_charges", "base_total_taxes_and_charges"),
		("discount_amount", "base_discount_amount"),
		("grand_total", "base_grand_total"),
		("rounding_adjustment", "base_rounding_adjustment"),
		("rounded_total", "base_rounded_total"),
		("tax_withholding_net_total", "base_tax_withholding_net_total"),
	),
	"Sales Order": (
		("total", "base_total"),
		("net_total", "base_net_total"),
		("total_taxes_and_charges", "base_total_taxes_and_charges"),
		("discount_amount", "base_discount_amount"),
		("grand_total", "base_grand_total"),
		("rounding_adjustment", "base_rounding_adjustment"),
		("rounded_total", "base_rounded_total"),
	),
}

_ITEM_BASE_MAP = {
	"Purchase Order Item": (
		("rate", "base_rate"),
		("amount", "base_amount"),
		("net_rate", "base_net_rate"),
		("net_amount", "base_net_amount"),
		("price_list_rate", "base_price_list_rate"),
		("rate_with_margin", "base_rate_with_margin"),
	),
	"Sales Order Item": (
		("rate", "base_rate"),
		("amount", "base_amount"),
		("net_rate", "base_net_rate"),
		("net_amount", "base_net_amount"),
		("price_list_rate", "base_price_list_rate"),
		("rate_with_margin", "base_rate_with_margin"),
	),
}

_PARENT_ITEM = {
	"Purchase Order": "Purchase Order Item",
	"Sales Order": "Sales Order Item",
}


def _table_columns(doctype: str) -> set[str]:
	"""Column names present on the DocType's SQL table."""
	try:
		rows = frappe.db.sql(f"SHOW COLUMNS FROM `tab{doctype}`", as_dict=True) or []
	except Exception:
		return set()
	return {r.get("Field") for r in rows if r.get("Field")}


def _rebase_doctype_currency(doctype: str, company: str, currency: str) -> int:
	"""Relabel currency on all company docs of `doctype` without FX conversion.

	Keeps transaction amounts; sets conversion_rate / plc_conversion_rate to 1 and
	copies amount → base_amount (and siblings) so company-currency totals match.
	"""
	cols = _table_columns(doctype)
	if "currency" not in cols or "company" not in cols:
		return 0

	to_update = frappe.db.sql(
		f"""
		select name from `tab{doctype}`
		where company = %s and ifnull(currency, '') != %s
		""",
		(company, currency),
	)
	names = [r[0] for r in (to_update or [])]
	if not names:
		return 0

	set_parts = ["currency = %s", "conversion_rate = 1"]
	params: list = [currency]
	if "price_list_currency" in cols:
		set_parts.append("price_list_currency = %s")
		params.append(currency)
	if "plc_conversion_rate" in cols:
		set_parts.append("plc_conversion_rate = 1")

	for src, dst in _PARENT_BASE_MAP.get(doctype, ()):
		if src in cols and dst in cols:
			set_parts.append(f"`{dst}` = `{src}`")

	# Chunk IN lists for large tenants.
	for i in range(0, len(names), 200):
		chunk = names[i : i + 200]
		placeholders = ", ".join(["%s"] * len(chunk))
		frappe.db.sql(
			f"""
			update `tab{doctype}`
			set {", ".join(set_parts)}
			where name in ({placeholders})
			""",
			tuple(params + chunk),
		)

	item_dt = _PARENT_ITEM.get(doctype)
	if item_dt:
		item_cols = _table_columns(item_dt)
		item_sets = []
		for src, dst in _ITEM_BASE_MAP.get(item_dt, ()):
			if src in item_cols and dst in item_cols:
				item_sets.append(f"`{dst}` = `{src}`")
		if item_sets:
			for i in range(0, len(names), 200):
				chunk = names[i : i + 200]
				placeholders = ", ".join(["%s"] * len(chunk))
				frappe.db.sql(
					f"""
					update `tab{item_dt}`
					set {", ".join(item_sets)}
					where parent in ({placeholders})
					""",
					tuple(chunk),
				)

	return len(names)


@frappe.whitelist()
def force_rebase_docs_currency(company=None, currency=None):
	"""Force-convert Sales Orders + Purchase Orders to `currency` without FX.

	Example: USD 30 → ARS 30 (same numbers, new coin). Does not touch prices or
	exchange-rate tables — only document currency labels + base_* mirrors.
	"""
	if not _can_manage():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"), frappe.PermissionError)

	name = _resolve_target_company(company)
	target = _clean_str(currency) or (
		frappe.db.get_value("Company", name, "default_currency") or DEFAULT_CURRENCY
	)
	if not frappe.db.exists("Currency", target):
		frappe.throw(_("Currency {0} not found").format(target), frappe.ValidationError)
	if not cint(frappe.db.get_value("Currency", target, "enabled")):
		frappe.db.set_value("Currency", target, "enabled", 1, update_modified=False)

	# Keep company default in sync so new docs match the rebase target.
	_apply_company_currency(name, target)

	counts = {}
	for dt in ("Purchase Order", "Sales Order"):
		counts[dt] = _rebase_doctype_currency(dt, name, target)

	frappe.db.commit()
	frappe.clear_cache(doctype="Purchase Order")
	frappe.clear_cache(doctype="Sales Order")

	return {
		"ok": True,
		"company": name,
		"currency": target,
		"purchase_orders": counts.get("Purchase Order", 0),
		"sales_orders": counts.get("Sales Order", 0),
		"message": _("Relabeled {0} purchase orders and {1} sales orders to {2}").format(
			counts.get("Purchase Order", 0),
			counts.get("Sales Order", 0),
			target,
		),
	}
