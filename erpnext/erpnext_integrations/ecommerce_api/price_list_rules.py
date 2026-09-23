"""Price List auto-rules: derive Item Prices from a base list.

Configured on the Price List form in ERPNext desk (custom fields):
  custom_auto_enabled, custom_base_price_list, custom_auto_percent, custom_auto_add_fixed

Default gift: Transferencia = Standard Buying × (1 + 3%) when auto is on and the
Item Price has not been manually overridden (custom_manual_override=0 → blue in UI;
override=1 → black/bold).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

DEFAULT_CURRENCY = "ARS"
TRANSFER_LIST = "Transferencia"
DEFAULT_BASE_LIST = "Standard Buying"
DEFAULT_TRANSFER_PERCENT = 3.0


def ensure_price_list_rule_fields() -> None:
	"""Custom fields on Price List + Item Price. Idempotent; wired to after_migrate."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Price List": [
				{
					"fieldname": "custom_auto_section",
					"fieldtype": "Section Break",
					"label": "Auto price rules",
					"insert_after": "countries",
					"collapsible": 1,
				},
				{
					"fieldname": "custom_auto_enabled",
					"fieldtype": "Check",
					"label": "Auto-configure prices from base list",
					"insert_after": "custom_auto_section",
					"default": "0",
					"description": (
						"When enabled, Item Prices on this list are filled from the base list "
						"using percent + fixed. Manual edits set an override and are not "
						"overwritten (shown in black); auto values stay blue."
					),
				},
				{
					"fieldname": "custom_base_price_list",
					"fieldtype": "Link",
					"label": "Based on price list",
					"options": "Price List",
					"insert_after": "custom_auto_enabled",
					"depends_on": "eval:doc.custom_auto_enabled",
					"mandatory_depends_on": "eval:doc.custom_auto_enabled",
					"description": "Source list for autoconfig (e.g. Standard Buying).",
				},
				{
					"fieldname": "custom_auto_percent",
					"fieldtype": "Float",
					"label": "Markup % over base",
					"insert_after": "custom_base_price_list",
					"depends_on": "eval:doc.custom_auto_enabled",
					"default": "0",
					"description": "e.g. 3 → charge base × 1.03",
				},
				{
					"fieldname": "column_break_auto_rules",
					"fieldtype": "Column Break",
					"insert_after": "custom_auto_percent",
				},
				{
					"fieldname": "custom_auto_add_fixed",
					"fieldtype": "Currency",
					"label": "Add fixed amount",
					"insert_after": "column_break_auto_rules",
					"depends_on": "eval:doc.custom_auto_enabled",
					"default": "0",
					"description": "Added after the percent markup (per unit).",
				},
				{
					"fieldname": "custom_auto_formula_html",
					"fieldtype": "HTML",
					"label": "Formula",
					"insert_after": "custom_auto_add_fixed",
					"depends_on": "eval:doc.custom_auto_enabled",
				},
			],
			"Item Price": [
				{
					"fieldname": "custom_manual_override",
					"fieldtype": "Check",
					"label": "Manual override (do not auto-update)",
					"insert_after": "price_list_rate",
					"default": "0",
					"description": (
						"Set when a user edits this rate. Auto rules skip overridden rows "
						"(black in Product Manager). Clear to resume autoconfig (blue)."
					),
				},
			],
		},
		ignore_validate=True,
	)
	frappe.clear_cache(doctype="Price List")
	frappe.clear_cache(doctype="Item Price")


def ensure_transferencia_auto_defaults() -> None:
	"""Create/enable Transferencia selling list: Standard Buying + 3% (default)."""
	ensure_price_list_rule_fields()
	currency = (
		frappe.db.get_single_value("Global Defaults", "default_currency")
		or DEFAULT_CURRENCY
	)
	if not frappe.db.exists("Price List", DEFAULT_BASE_LIST):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": DEFAULT_BASE_LIST,
				"enabled": 1,
				"buying": 1,
				"selling": 0,
				"currency": currency,
			}
		).insert(ignore_permissions=True)

	if frappe.db.exists("Price List", TRANSFER_LIST):
		doc = frappe.get_doc("Price List", TRANSFER_LIST)
		# Only seed defaults when auto was never configured (base empty).
		if not (doc.get("custom_base_price_list") or "").strip():
			doc.custom_auto_enabled = 1
			doc.custom_base_price_list = DEFAULT_BASE_LIST
			doc.custom_auto_percent = DEFAULT_TRANSFER_PERCENT
			doc.custom_auto_add_fixed = 0
			doc.selling = 1
			doc.enabled = 1
			if not doc.currency:
				doc.currency = currency
			doc.save(ignore_permissions=True)
			frappe.db.commit()
		return

	frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": TRANSFER_LIST,
			"enabled": 1,
			"buying": 0,
			"selling": 1,
			"currency": currency,
			"custom_auto_enabled": 1,
			"custom_base_price_list": DEFAULT_BASE_LIST,
			"custom_auto_percent": DEFAULT_TRANSFER_PERCENT,
			"custom_auto_add_fixed": 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def apply_auto_formula(base_rate: float, percent: float = 0, add_fixed: float = 0) -> float:
	"""base × (1 + percent/100) + add_fixed."""
	base = flt(base_rate)
	if base <= 0:
		return 0.0
	return flt(base * (1.0 + flt(percent) / 100.0) + flt(add_fixed), 6)


def get_auto_rule(price_list: str | None) -> dict | None:
	"""Return auto rule dict for a Price List, or None if disabled/missing."""
	name = (price_list or "").strip()
	if not name or not frappe.db.exists("Price List", name):
		return None
	if not frappe.db.has_column("Price List", "custom_auto_enabled"):
		return None
	row = frappe.db.get_value(
		"Price List",
		name,
		[
			"name",
			"custom_auto_enabled",
			"custom_base_price_list",
			"custom_auto_percent",
			"custom_auto_add_fixed",
			"buying",
			"selling",
		],
		as_dict=True,
	)
	if not row or not cint(row.get("custom_auto_enabled")):
		return None
	base = (row.get("custom_base_price_list") or "").strip()
	if not base:
		return None
	return {
		"price_list": row.name,
		"base_price_list": base,
		"percent": flt(row.get("custom_auto_percent") or 0),
		"add_fixed": flt(row.get("custom_auto_add_fixed") or 0),
		"buying": cint(row.get("buying")),
		"selling": cint(row.get("selling")),
	}


def list_auto_dependent_lists(base_price_list: str | None) -> list[dict]:
	"""Price lists that autoconfig from ``base_price_list``."""
	base = (base_price_list or "").strip()
	if not base or not frappe.db.has_column("Price List", "custom_auto_enabled"):
		return []
	rows = frappe.get_all(
		"Price List",
		filters={
			"enabled": 1,
			"custom_auto_enabled": 1,
			"custom_base_price_list": base,
		},
		fields=[
			"name",
			"custom_base_price_list",
			"custom_auto_percent",
			"custom_auto_add_fixed",
			"buying",
			"selling",
		],
		ignore_permissions=True,
	)
	out = []
	for r in rows:
		out.append(
			{
				"price_list": r.name,
				"base_price_list": r.custom_base_price_list,
				"percent": flt(r.custom_auto_percent or 0),
				"add_fixed": flt(r.custom_auto_add_fixed or 0),
				"buying": cint(r.buying),
				"selling": cint(r.selling),
			}
		)
	return out


def _item_price_filters(price_list: str, item_code: str, buying: int, selling: int) -> dict:
	filt = {"item_code": item_code, "price_list": price_list}
	if buying and not selling:
		filt["buying"] = 1
	elif selling and not buying:
		filt["selling"] = 1
	return filt


def _base_rate_for_item(item_code: str, base_price_list: str) -> float:
	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": base_price_list},
		"price_list_rate",
	)
	return flt(rate or 0)


def upsert_auto_item_price(
	item_code: str,
	rule: dict,
	*,
	force: bool = False,
) -> dict:
	"""Write derived rate unless manual override (unless force)."""
	item_code = (item_code or "").strip()
	if not item_code or not rule:
		return {"ok": False, "skipped": "missing"}

	base_rate = _base_rate_for_item(item_code, rule["base_price_list"])
	if base_rate <= 0:
		return {"ok": False, "skipped": "no_base_rate"}

	next_rate = apply_auto_formula(base_rate, rule["percent"], rule["add_fixed"])
	if next_rate <= 0:
		return {"ok": False, "skipped": "zero_rate"}

	buying = cint(rule.get("buying"))
	selling = cint(rule.get("selling"))
	if not buying and not selling:
		selling = 1
	filt = _item_price_filters(rule["price_list"], item_code, buying, selling)
	existing = frappe.db.get_value("Item Price", filt, ["name", "price_list_rate"], as_dict=True)

	has_override_col = frappe.db.has_column("Item Price", "custom_manual_override")
	if existing and has_override_col and not force:
		if cint(frappe.db.get_value("Item Price", existing.name, "custom_manual_override")):
			return {"ok": True, "skipped": "manual_override", "name": existing.name}

	values = {"price_list_rate": next_rate}
	if has_override_col and not force:
		values["custom_manual_override"] = 0

	if existing:
		frappe.db.set_value("Item Price", existing.name, values, update_modified=True)
		return {"ok": True, "name": existing.name, "rate": next_rate, "updated": True}

	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": rule["price_list"],
			"buying": buying,
			"selling": selling,
			"price_list_rate": next_rate,
			**({"custom_manual_override": 0} if has_override_col else {}),
		}
	)
	doc.insert(ignore_permissions=True)
	return {"ok": True, "name": doc.name, "rate": next_rate, "created": True}


def sync_auto_prices_from_base(
	base_price_list: str | None,
	item_codes=None,
	*,
	force: bool = False,
) -> dict:
	"""Refresh all dependent auto lists for one or more items after a base rate change."""
	base = (base_price_list or "").strip()
	if not base:
		return {"ok": True, "updated": 0}
	rules = list_auto_dependent_lists(base)
	if not rules:
		return {"ok": True, "updated": 0, "rules": 0}

	codes = item_codes
	if isinstance(codes, str):
		codes = frappe.parse_json(codes) if codes.strip().startswith("[") else [codes]
	codes = [c for c in (codes or []) if c]
	if not codes:
		# All items that have a rate on the base list
		codes = frappe.get_all(
			"Item Price",
			filters={"price_list": base},
			pluck="item_code",
			ignore_permissions=True,
		)

	updated = 0
	for code in codes:
		for rule in rules:
			res = upsert_auto_item_price(code, rule, force=force)
			if res.get("updated") or res.get("created"):
				updated += 1
	return {"ok": True, "updated": updated, "items": len(codes), "rules": len(rules)}


def sync_auto_prices_for_list(price_list: str | None, item_codes=None, force: int | bool = 0) -> dict:
	"""Recompute one auto-enabled list from its base (desk button / API)."""
	rule = get_auto_rule(price_list)
	if not rule:
		frappe.throw(_("Price List {0} has no auto rule").format(price_list), frappe.ValidationError)
	codes = item_codes
	if isinstance(codes, str):
		codes = frappe.parse_json(codes) if str(codes).strip().startswith("[") else [codes]
	codes = [c for c in (codes or []) if c]
	if not codes:
		codes = frappe.get_all(
			"Item Price",
			filters={"price_list": rule["base_price_list"]},
			pluck="item_code",
			ignore_permissions=True,
		)
	updated = 0
	for code in codes:
		res = upsert_auto_item_price(code, rule, force=bool(cint(force)))
		if res.get("updated") or res.get("created"):
			updated += 1
	frappe.db.commit()
	return {"ok": True, "updated": updated, "items": len(codes), "rule": rule}


def mark_item_price_manual_override(item_code: str, price_list: str) -> None:
	"""Flag Item Price as user-edited so autoconfig skips it."""
	if not frappe.db.has_column("Item Price", "custom_manual_override"):
		return
	name = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list},
		"name",
	)
	if name:
		frappe.db.set_value("Item Price", name, "custom_manual_override", 1, update_modified=False)


def selling_price_meta_map(item_codes: list) -> dict:
	"""item_code → { price_list → { rate, manual_override, auto } }.

	``auto=1`` means derived by an auto rule and not manually overridden → blue UI.
	"""
	if not item_codes:
		return {}
	ph = ", ".join(["%s"] * len(item_codes))
	has_override = frappe.db.has_column("Item Price", "custom_manual_override")
	override_select = (
		", MAX(IFNULL(custom_manual_override, 0)) AS manual_override"
		if has_override
		else ", 0 AS manual_override"
	)
	rows = frappe.db.sql(
		f"""
		SELECT item_code, price_list, MAX(price_list_rate) AS rate
			{override_select}
		FROM `tabItem Price`
		WHERE item_code IN ({ph})
		  AND selling = 1
		GROUP BY item_code, price_list
		""",
		tuple(item_codes),
		as_dict=True,
	)
	auto_lists: set[str] = set()
	if frappe.db.has_column("Price List", "custom_auto_enabled"):
		auto_lists = {
			r.name
			for r in frappe.get_all(
				"Price List",
				filters={"enabled": 1, "custom_auto_enabled": 1},
				fields=["name"],
				ignore_permissions=True,
			)
		}

	out: dict = {}
	for r in rows:
		manual = cint(r.get("manual_override"))
		is_auto_list = r.price_list in auto_lists
		out.setdefault(r.item_code, {})[r.price_list] = {
			"rate": flt(r.rate),
			"manual_override": manual,
			"auto": 1 if (is_auto_list and not manual) else 0,
		}
	return out


@frappe.whitelist()
def get_price_list_rules(price_list=None):
	"""Return auto rule for one list, or all enabled auto lists."""
	ensure_price_list_rule_fields()
	if price_list:
		rule = get_auto_rule(price_list)
		return {"ok": True, "rule": rule}
	rows = frappe.get_all(
		"Price List",
		filters={"enabled": 1, "custom_auto_enabled": 1},
		fields=[
			"name",
			"custom_base_price_list",
			"custom_auto_percent",
			"custom_auto_add_fixed",
			"buying",
			"selling",
		],
		ignore_permissions=True,
	)
	rules = [
		{
			"price_list": r.name,
			"base_price_list": r.custom_base_price_list,
			"percent": flt(r.custom_auto_percent or 0),
			"add_fixed": flt(r.custom_auto_add_fixed or 0),
			"buying": cint(r.buying),
			"selling": cint(r.selling),
		}
		for r in rows
	]
	return {"ok": True, "rules": rules}


@frappe.whitelist()
def run_sync_auto_prices(price_list=None, item_codes=None, force=0):
	"""Whitelisted: sync one list, or all dependents of Standard Buying when omitted."""
	ensure_price_list_rule_fields()
	if price_list:
		return sync_auto_prices_for_list(price_list, item_codes=item_codes, force=force)
	# Sync every auto list from its own base for given items (or all)
	updated = 0
	for r in (get_price_list_rules().get("rules") or []):
		res = sync_auto_prices_for_list(r["price_list"], item_codes=item_codes, force=force)
		updated += cint(res.get("updated"))
	return {"ok": True, "updated": updated}


def on_price_list_update(doc, method=None):
	"""When an auto rule is saved, optionally offer sync (desk button handles bulk)."""
	# No automatic full-catalog sync on every save — too heavy. Desk button + base
	# Item Price hooks drive updates.
	pass


def on_item_price_update(doc, method=None):
	"""After a base Item Price changes, refresh dependent auto lists (skip overrides)."""
	if frappe.flags.get("in_price_list_auto_sync"):
		return
	pl = (doc.price_list or "").strip()
	code = (doc.item_code or "").strip()
	if not pl or not code:
		return
	dependents = list_auto_dependent_lists(pl)
	if not dependents:
		return
	frappe.flags.in_price_list_auto_sync = True
	try:
		for rule in dependents:
			upsert_auto_item_price(code, rule, force=False)
	finally:
		frappe.flags.in_price_list_auto_sync = False
