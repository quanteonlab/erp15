"""Acting-user company list, default, and switch for the shop admin UI.

Multi-company is off by default. Enable it in shop UI settings
(`companies.enabled`). While off, lists are not scoped by company and the
admin switcher stays hidden — one Company record is enough.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint

SHOP_UI_SCOPE = "settings.shop_ui"


def is_multi_company_enabled() -> bool:
	if not frappe.db.exists("Table Extra Schema", SHOP_UI_SCOPE):
		return False
	try:
		raw = frappe.db.get_value("Table Extra Schema", SHOP_UI_SCOPE, "columns_json")
		data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
		if not isinstance(data, dict):
			return False
		block = data.get("companies") or {}
		if not isinstance(block, dict):
			return False
		return bool(cint(block.get("enabled")))
	except Exception:
		return False


def acting_user() -> str:
	header = ""
	try:
		header = (frappe.get_request_header("X-ERP-Acting-User") or "").strip()
	except Exception:
		header = ""
	user = header or frappe.session.user
	if not user or user == "Guest":
		return frappe.session.user
	return user


def _request_company() -> str:
	try:
		raw = (frappe.get_request_header("X-ERP-Company") or "").strip()
	except Exception:
		raw = ""
	return raw


def _unrestricted(user: str) -> bool:
	if user in ("Administrator",):
		return True
	roles = set(frappe.get_roles(user) or [])
	return "Administrator" in roles or "System Manager" in roles


def is_desk_admin(user: str | None = None) -> bool:
	return _unrestricted(user or acting_user())


def allowed_company_names(user: str | None = None) -> list[str]:
	user = user or acting_user()
	perm_rows = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Company"},
		fields=["for_value", "is_default"],
		ignore_permissions=True,
	)
	if perm_rows and not _unrestricted(user):
		names = [r.for_value for r in perm_rows if r.for_value]
	else:
		names = frappe.get_all(
			"Company",
			pluck="name",
			order_by="name asc",
			ignore_permissions=True,
		)
	seen = set()
	out = []
	for n in names:
		if n and n not in seen:
			seen.add(n)
			out.append(n)
	return out


def resolve_company(company=None, user: str | None = None) -> str | None:
	"""Company to stamp on writes. Ignores switcher/header when multi-company is off."""
	user = user or acting_user()
	multi = is_multi_company_enabled()
	if not multi:
		default = frappe.defaults.get_user_default("Company", user)
		if default:
			return default
		global_default = frappe.db.get_single_value("Global Defaults", "default_company")
		if global_default:
			return global_default
		return frappe.db.get_value("Company", {}, "name")

	allowed = allowed_company_names(user)
	requested = (company or _request_company() or "").strip()
	if requested:
		if requested not in allowed:
			frappe.throw(_("Company {0} is not allowed for this user").format(requested))
		return requested
	default = frappe.defaults.get_user_default("Company", user)
	if default and default in allowed:
		return default
	global_default = frappe.db.get_single_value("Global Defaults", "default_company")
	if global_default and global_default in allowed:
		return global_default
	return allowed[0] if allowed else None


def company_scope(company=None, user: str | None = None) -> str | None:
	"""Company to filter lists by, or None when multi-company is off (do not filter)."""
	if not is_multi_company_enabled():
		return None
	return resolve_company(company, user=user)


def company_payload(company=None, user: str | None = None) -> dict:
	user = user or acting_user()
	enabled = is_multi_company_enabled()
	active = resolve_company(company, user=user)
	if not enabled:
		return {
			"enabled": 0,
			"companies": [],
			"default_company": active,
		}
	allowed = allowed_company_names(user)
	return {
		"enabled": 1,
		"companies": [
			{"name": n, "is_default": 1 if n == active else 0} for n in allowed
		],
		"default_company": active,
	}


def assert_company_allowed(company: str, user: str | None = None) -> str:
	company = (company or "").strip()
	if not company:
		frappe.throw(_("Company is required"))
	allowed = allowed_company_names(user)
	if company not in allowed:
		frappe.throw(_("Company {0} is not allowed for this user").format(company))
	return company


@frappe.whitelist()
def get_user_companies():
	return company_payload()


@frappe.whitelist()
def set_user_company(company):
	if not is_multi_company_enabled():
		frappe.throw(_("Multi-company is not enabled. Turn it on in Settings."))
	user = acting_user()
	company = assert_company_allowed(company, user=user)
	frappe.defaults.set_user_default("Company", company, user=user)
	frappe.db.commit()
	return company_payload(company, user=user)
