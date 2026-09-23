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
	for field in _EDITABLE_FIELDS:
		if field not in incoming:
			continue
		val = _clean_str(incoming.get(field))
		if field == "domain":
			val = _coerce_company_domain(val)
		if field == "default_currency":
			if not val:
				val = DEFAULT_CURRENCY
			if not frappe.db.exists("Currency", val):
				frappe.throw(_("Currency {0} not found").format(val), frappe.ValidationError)
			if not cint(frappe.db.get_value("Currency", val, "enabled")):
				frappe.db.set_value("Currency", val, "enabled", 1, update_modified=False)
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
