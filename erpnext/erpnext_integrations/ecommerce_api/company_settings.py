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
		"default_currency": doc.default_currency or "",
		"country": doc.country or "",
		"tax_id": doc.tax_id or "",
		"phone_no": doc.phone_no or "",
		"email": doc.email or "",
		"website": doc.website or "",
		"domain": doc.domain or "",
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
	currencies = frappe.get_all(
		"Currency",
		filters={"enabled": 1},
		pluck="name",
		order_by="name asc",
		ignore_permissions=True,
	)
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
		"currencies": currencies or [],
		"countries": countries or [],
	}


def _clean_str(val, max_len: int = 140) -> str:
	return str(val or "").strip()[:max_len]


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
	if new_company_name and new_company_name != (doc.company_name or doc.name):
		# Company autoname is field:company_name — renaming the doc updates name.
		frappe.rename_doc("Company", doc.name, new_company_name, force=True, merge=False)
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
		if field == "default_currency" and val and not frappe.db.exists("Currency", val):
			frappe.throw(_("Currency {0} not found").format(val), frappe.ValidationError)
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

	# Refresh Global Defaults default_company if rename moved the active default.
	try:
		gd = frappe.db.get_single_value("Global Defaults", "default_company")
		if gd and not frappe.db.exists("Company", gd) and frappe.db.exists("Company", name):
			frappe.db.set_value("Global Defaults", None, "default_company", name)
			frappe.db.commit()
	except Exception:
		pass

	return get_company_settings(company=name)
