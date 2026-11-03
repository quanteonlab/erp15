"""Coupons for the public storefront + the Promotions table (Tablas → Promociones).

A coupon is the native ERPNext pair:
  * a **Pricing Rule** (``coupon_code_based=1``, ``apply_on=Transaction``, ``min_amt``, % or amount off), and
  * a **Coupon Code** (code, validity window, ``maximum_use`` / ``used``).
The Coupon Code also carries three small custom fields for the shop: ``custom_public`` (listed in the storefront),
``custom_label_es`` and ``custom_label_zh`` (optional free text such as "Primera compra").

Guests never send a discount amount: ``compute_coupon_discount`` is the single source of truth and is re-run by
``create_guest_preorder``. ERPNext counts a use when staff *submit* the Sales Order (``update_coupon_code_count``),
which only accepts ``used < maximum_use`` — so "unlimited" is stored as ``UNLIMITED`` (never 0).

Reads use ``ignore_permissions=True`` (API-key user) like the rest of ecommerce_api; staff writes additionally
require an acting staff user + ``tables.promotions``.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from erpnext.erpnext_integrations.ecommerce_api.ops_kv import idempotent_request

# Stored as maximum_use when the admin picks "unlimited" (0 would make ERPNext refuse every use).
UNLIMITED = 999_999
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,23}$")  # 2-24 chars, upper case
LABEL_MAX = 60
# Per-IP attempts per minute at the guest validator (code guessing).
THROTTLE_PER_MINUTE = 30

_fields_ready = False


def ensure_coupon_custom_fields() -> None:
	"""Custom fields on Coupon Code. Idempotent; wired into hooks.after_migrate and called lazily below."""
	global _fields_ready
	if _fields_ready and frappe.db.has_column("Coupon Code", "custom_public"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Coupon Code": [
				{
					"fieldname": "custom_public",
					"fieldtype": "Check",
					"label": "Show in shop",
					"insert_after": "maximum_use",
					"default": "0",
					"description": "List this coupon in the public storefront ('Canjear').",
				},
				{
					"fieldname": "custom_label_es",
					"fieldtype": "Data",
					"label": "Shop label (ES)",
					"insert_after": "custom_public",
				},
				{
					"fieldname": "custom_label_zh",
					"fieldtype": "Data",
					"label": "Shop label (ZH)",
					"insert_after": "custom_label_es",
				},
			]
		}
	)
	_fields_ready = True


def _require_staff(permission: str = "tables.promotions") -> None:
	"""Writes need a real acting staff user. Scripts/jobs (no HTTP request) are trusted."""
	if getattr(frappe.local, "request", None) is None:
		return
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username, _require_app_permission

	if not _acting_username():
		frappe.throw(_("Sign in as staff to do this"), frappe.PermissionError)
	_require_app_permission(permission)


def _norm_code(raw) -> str:
	return cstr(raw).strip().upper()


def _clean_label(raw) -> str:
	return cstr(raw).strip()[:LABEL_MAX]


def _clean_date(raw, label: str):
	s = cstr(raw).strip()
	if not s:
		return None
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
		frappe.throw(_("{0}: use YYYY-MM-DD").format(label), frappe.ValidationError)
	try:
		return getdate(s)
	except Exception:
		frappe.throw(_("{0} is not a valid date").format(label), frappe.ValidationError)


_CC_FIELDS = ["name", "coupon_code", "coupon_type", "pricing_rule", "maximum_use", "used", "valid_from", "valid_upto", "customer"]
_RULE_FIELDS = [
	"name",
	"title",
	"disable",
	"min_amt",
	"rate_or_discount",
	"discount_percentage",
	"discount_amount",
	"free_item",
	"valid_from",
	"valid_upto",
]


def _cc_fields() -> list[str]:
	extra = [f for f in ("custom_public", "custom_label_es", "custom_label_zh") if frappe.db.has_column("Coupon Code", f)]
	return _CC_FIELDS + extra


def _find_coupon(code_raw):
	code = cstr(code_raw).strip()
	if not code:
		return None
	for candidate in dict.fromkeys((code, code.upper())):
		row = frappe.db.get_value("Coupon Code", {"coupon_code": candidate}, _cc_fields(), as_dict=True)
		if row:
			return row
	return None


def compute_coupon_discount(code, subtotal=None) -> dict:
	"""Validate a coupon and, when ``subtotal`` is given, the discount it grants on it.

	Returns ``{valid, reason, ...}``; ``reason`` is one of unknown | disabled | not_started | expired |
	exhausted | min_amount (empty when valid). Never raises for an unknown code.
	"""
	cc = _find_coupon(code)
	# Gift cards are tied to a customer and are not redeemable by an anonymous guest.
	if not cc or cc.get("customer") or not cc.get("pricing_rule"):
		return {"valid": False, "reason": "unknown"}
	rule = frappe.db.get_value("Pricing Rule", cc["pricing_rule"], _RULE_FIELDS, as_dict=True)
	if not rule:
		return {"valid": False, "reason": "unknown"}

	base = {
		"code": cc["coupon_code"],
		"coupon_name": cc["name"],
		"title": rule.get("title") or cc["name"],
		"kind": "percent" if rule.get("rate_or_discount") == "Discount Percentage" else "amount",
		"value": flt(rule.get("discount_percentage") if rule.get("rate_or_discount") == "Discount Percentage" else rule.get("discount_amount")),
		"min_amount": flt(rule.get("min_amt")),
		"valid_upto": cstr(cc.get("valid_upto") or rule.get("valid_upto") or ""),
		# legacy shape used by the POS coupon box
		"discount_percentage": flt(rule.get("discount_percentage")),
		"discount_amount": flt(rule.get("discount_amount")),
		"free_item": rule.get("free_item") or None,
	}

	if cint(rule.get("disable")):
		return {**base, "valid": False, "reason": "disabled"}

	today = getdate(nowdate())
	for start in (cc.get("valid_from"), rule.get("valid_from")):
		if start and getdate(start) > today:
			return {**base, "valid": False, "reason": "not_started"}
	for end in (cc.get("valid_upto"), rule.get("valid_upto")):
		if end and getdate(end) < today:
			return {**base, "valid": False, "reason": "expired"}

	max_use = cint(cc.get("maximum_use"))
	if max_use and cint(cc.get("used")) >= max_use:
		return {**base, "valid": False, "reason": "exhausted"}

	if subtotal is not None:
		sub = max(flt(subtotal), 0.0)
		if sub < base["min_amount"]:
			return {**base, "valid": False, "reason": "min_amount", "discount": 0.0}
		if base["kind"] == "percent":
			discount = sub * min(base["value"], 100.0) / 100.0
		else:
			discount = base["value"]
		base["discount"] = round(min(discount, sub), 2)

	return {**base, "valid": True, "reason": ""}


def throttle_coupon_attempts() -> None:
	"""Cheap per-IP limiter for the guest validator. Never blocks if the cache is unavailable."""
	try:
		ip = cstr(getattr(frappe.local, "request_ip", "") or "anon")
		key = f"coupon_try:{ip}"
		cache = frappe.cache()
		n = cache.incrby(key, 1)
		if cint(n) == 1:
			cache.expire(key, 60)
		if cint(n) > THROTTLE_PER_MINUTE:
			frappe.throw(_("Too many attempts. Try again in a minute."), frappe.exceptions.RateLimitExceededError)
	except frappe.exceptions.RateLimitExceededError:
		raise
	except Exception:
		return


# ── payloads ────────────────────────────────────────────────────────────────


def _max_uses_out(raw) -> int:
	"""API value: 0 = unlimited."""
	n = cint(raw)
	return 0 if n >= UNLIMITED else n


def _staff_payload(cc) -> dict:
	rule = frappe.db.get_value("Pricing Rule", cc.get("pricing_rule"), _RULE_FIELDS, as_dict=True) or {}
	percent = rule.get("rate_or_discount") == "Discount Percentage"
	return {
		"name": cc["name"],
		"code": cc["coupon_code"],
		"pricing_rule": cc.get("pricing_rule"),
		"disabled": cint(rule.get("disable")),
		"kind": "percent" if percent else "amount",
		"value": flt(rule.get("discount_percentage") if percent else rule.get("discount_amount")),
		"min_amount": flt(rule.get("min_amt")),
		"valid_from": cstr(cc.get("valid_from") or rule.get("valid_from") or ""),
		"valid_upto": cstr(cc.get("valid_upto") or rule.get("valid_upto") or ""),
		"max_uses": _max_uses_out(cc.get("maximum_use")),
		"used": cint(cc.get("used")),
		"public": cint(cc.get("custom_public")),
		"label_es": cstr(cc.get("custom_label_es")),
		"label_zh": cstr(cc.get("custom_label_zh")),
		"modified": cstr(cc.get("modified") or ""),
	}


# ── staff (Promotions table) ────────────────────────────────────────────────


@frappe.whitelist()
def list_coupons():
	"""All promotional coupons with their rule data and usage (Promotions table, kind = Coupon).

	Staff only: it includes private codes, which must never be readable by a guest."""
	_require_staff()
	ensure_coupon_custom_fields()
	rows = frappe.get_all(
		"Coupon Code",
		filters={"coupon_type": "Promotional"},
		fields=_cc_fields() + ["modified"],
		order_by="modified desc",
		ignore_permissions=True,
	)
	return {"coupons": [_staff_payload(r) for r in rows], "total_count": len(rows)}


@frappe.whitelist()
@idempotent_request
def save_coupon(data):
	"""Create or update a coupon (Pricing Rule + Coupon Code in one call).

	data: {code, kind: percent|amount, value, min_amount, valid_from, valid_upto,
	       max_uses (0 = unlimited), public, label_es, label_zh, disabled}
	The code is the document name, so it cannot be changed once created.
	"""
	_require_staff()
	ensure_coupon_custom_fields()
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = frappe._dict(data or {})

	code = _norm_code(data.get("code") or data.get("name"))
	if not CODE_RE.match(code):
		frappe.throw(_("Code: 2-24 characters, A-Z, 0-9, - or _"), frappe.ValidationError)

	kind = data.get("kind")
	if kind not in ("percent", "amount"):
		frappe.throw(_("Choose a percentage or an amount"), frappe.ValidationError)
	value = flt(data.get("value"))
	if value <= 0:
		frappe.throw(_("Discount must be greater than 0"), frappe.ValidationError)
	if kind == "percent" and value > 100:
		frappe.throw(_("A percentage cannot exceed 100"), frappe.ValidationError)
	min_amount = flt(data.get("min_amount"))
	if min_amount < 0:
		frappe.throw(_("Minimum order cannot be negative"), frappe.ValidationError)
	valid_from = _clean_date(data.get("valid_from"), _("Valid from"))
	valid_upto = _clean_date(data.get("valid_upto"), _("Valid until"))
	if valid_from and valid_upto and valid_from > valid_upto:
		frappe.throw(_("'Valid from' is after 'Valid until'"), frappe.ValidationError)
	max_uses = cint(data.get("max_uses"))
	if max_uses < 0:
		frappe.throw(_("Maximum uses cannot be negative"), frappe.ValidationError)
	disabled = cint(data.get("disabled"))

	existing = frappe.db.exists("Coupon Code", code)
	if existing:
		frappe.flags.ignore_permissions = True
		cc = frappe.get_doc("Coupon Code", code)
		frappe.flags.ignore_permissions = False
		if cc.coupon_type != "Promotional":
			frappe.throw(_("{0} is not a promotional coupon").format(code), frappe.ValidationError)
		rule = frappe.get_doc("Pricing Rule", cc.pricing_rule) if cc.pricing_rule and frappe.db.exists("Pricing Rule", cc.pricing_rule) else None
	else:
		cc = frappe.new_doc("Coupon Code")
		rule = None

	from erpnext.erpnext_integrations.ecommerce_api.api import _default_pricing_currency

	currency, company = _default_pricing_currency()
	if rule is None:
		rule = frappe.new_doc("Pricing Rule")
	rule.title = f"Cupón {code}"
	rule.selling = 1
	rule.buying = 0
	rule.disable = disabled
	rule.apply_on = "Transaction"
	rule.price_or_product_discount = "Price"
	rule.currency = currency
	if company:
		rule.company = company
	rule.coupon_code_based = 1
	rule.rate_or_discount = "Discount Percentage" if kind == "percent" else "Discount Amount"
	rule.discount_percentage = value if kind == "percent" else 0
	rule.discount_amount = value if kind == "amount" else 0
	rule.min_amt = min_amount
	rule.valid_from = valid_from
	rule.valid_upto = valid_upto
	if rule.is_new():
		rule.insert(ignore_permissions=True)
	else:
		rule.save(ignore_permissions=True)

	cc.coupon_name = code
	cc.coupon_type = "Promotional"
	cc.coupon_code = code
	cc.pricing_rule = rule.name
	cc.valid_from = valid_from
	cc.valid_upto = valid_upto
	cc.maximum_use = max_uses or UNLIMITED
	cc.custom_public = 1 if cint(data.get("public")) else 0
	cc.custom_label_es = _clean_label(data.get("label_es"))
	cc.custom_label_zh = _clean_label(data.get("label_zh"))
	if cc.is_new():
		cc.insert(ignore_permissions=True)
	else:
		cc.save(ignore_permissions=True)
	frappe.db.commit()

	row = frappe.db.get_value("Coupon Code", code, _cc_fields() + ["modified"], as_dict=True)
	return {"ok": True, "name": code, "coupon": _staff_payload(row)}


@frappe.whitelist()
def set_coupon_disabled(name, disabled=1):
	"""'Cancel' a coupon: stop it being redeemed but keep its history and usage count."""
	_require_staff()
	ensure_coupon_custom_fields()
	cc = frappe.db.get_value("Coupon Code", name, _cc_fields() + ["modified"], as_dict=True) if name else None
	if not cc or not cc.get("pricing_rule"):
		frappe.throw(_("Coupon not found"), frappe.DoesNotExistError)
	frappe.db.set_value("Pricing Rule", cc["pricing_rule"], "disable", cint(disabled))
	frappe.db.commit()
	return {"ok": True, "name": name, "disabled": cint(disabled), "coupon": _staff_payload(cc)}


@frappe.whitelist()
def delete_coupon(name):
	"""Delete a coupon that was never used. A used coupon must be cancelled (disabled) instead."""
	_require_staff()
	cc = frappe.db.get_value("Coupon Code", name, ["name", "pricing_rule", "used"], as_dict=True) if name else None
	if not cc:
		frappe.throw(_("Coupon not found"), frappe.DoesNotExistError)
	if cint(cc.get("used")) > 0:
		frappe.throw(_("This coupon has been used {0} time(s). Cancel it instead of deleting it.").format(cc["used"]), frappe.ValidationError)
	frappe.delete_doc("Coupon Code", name, ignore_permissions=True, force=1)
	rule_name = cc.get("pricing_rule")
	if rule_name and frappe.db.exists("Pricing Rule", rule_name):
		still_used = frappe.db.exists("Coupon Code", {"pricing_rule": rule_name})
		if not still_used and cint(frappe.db.get_value("Pricing Rule", rule_name, "coupon_code_based")):
			frappe.delete_doc("Pricing Rule", rule_name, ignore_permissions=True, force=1)
	frappe.db.commit()
	return {"ok": True, "name": name}


# ── guests (storefront) ─────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
def list_public_coupons():
	"""Coupons marked 'show in shop' that can be redeemed right now. Private codes are never listed."""
	ensure_coupon_custom_fields()
	rows = frappe.get_all(
		"Coupon Code",
		filters={"coupon_type": "Promotional", "custom_public": 1},
		fields=["coupon_code"] + [f for f in ("custom_label_es", "custom_label_zh") if frappe.db.has_column("Coupon Code", f)],
		order_by="modified desc",
		limit_page_length=20,
		ignore_permissions=True,
	)
	out = []
	for r in rows:
		res = compute_coupon_discount(r["coupon_code"])
		if not res.get("valid"):
			continue
		out.append(
			{
				"code": res["code"],
				"kind": res["kind"],
				"value": res["value"],
				"min_amount": res["min_amount"],
				"valid_upto": res["valid_upto"],
				"label_es": cstr(r.get("custom_label_es")),
				"label_zh": cstr(r.get("custom_label_zh")),
			}
		)
	return out
