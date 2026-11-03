"""Delivery fee + delivery promotions for the public storefront (Tablas → Promociones, kind = Delivery).

  * ``catalogDisplay.deliveryEnabled`` / ``deliveryBaseFee`` (shop_ui settings) turn delivery pricing on and set
    the base fee. While it is off the shop keeps its old behaviour: delivery is just a flag on the inquiry.
  * **Delivery promos** are a small admin-managed list (Table Extra Schema scope ``settings.delivery_promos``, no
    DocType / migration): free delivery over an amount, a % off the fee, or an amount off the fee, each with an
    optional date window.

The quote is always computed here. ``create_guest_preorder`` recomputes it from the order's own subtotal (after any
coupon) and books it as a non-stock service Item line so it shows on the Sales Order / print without needing any
tax-account configuration; clients never send a fee.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from erpnext.erpnext_integrations.ecommerce_api.ops_kv import idempotent_request

SCOPE = "settings.delivery_promos"
KINDS = ("free_over", "percent_off", "amount_off")
MAX_PROMOS = 30
NAME_MAX = 60
LABEL_MAX = 60
MONEY_MAX = 10_000_000
# Shared non-stock service Item that carries the fee on the Sales Order (never listed in catalogs).
DELIVERY_ITEM_CODE = "ENVIO-DELIVERY"


def _require_staff(permission: str = "tables.promotions") -> None:
	from erpnext.erpnext_integrations.ecommerce_api.coupons_api import _require_staff as _gate

	_gate(permission)


def _money(val, default: float = 0.0) -> float:
	try:
		n = float(val)
	except (TypeError, ValueError):
		return default
	if n != n:  # NaN
		return default
	return round(max(0.0, min(MONEY_MAX, n)), 2)


def _iso(val) -> str:
	s = val.strip() if isinstance(val, str) else ""
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
		return ""
	try:
		getdate(s)
	except Exception:
		return ""
	return s


def normalize_promo(raw) -> dict | None:
	"""One stored promo, or None when it is unusable."""
	if not isinstance(raw, dict):
		return None
	pid = re.sub(r"[^A-Za-z0-9_-]", "", cstr(raw.get("id")))[:24]
	if not pid:
		return None
	kind = raw.get("kind")
	if kind not in KINDS:
		return None
	threshold = _money(raw.get("threshold"))
	value = _money(raw.get("value"))
	if kind == "free_over":
		if threshold <= 0:
			return None
		value = 0.0
	elif kind == "percent_off":
		if not (0 < value <= 100):
			return None
	elif value <= 0:  # amount_off
		return None
	vf, vu = _iso(raw.get("valid_from")), _iso(raw.get("valid_upto"))
	if vf and vu and vf > vu:
		vf, vu = vu, vf
	return {
		"id": pid,
		"name": cstr(raw.get("name")).strip()[:NAME_MAX],
		"kind": kind,
		"threshold": threshold,
		"value": value,
		"valid_from": vf,
		"valid_upto": vu,
		"disabled": 1 if cint(raw.get("disabled")) else 0,
		"label_es": cstr(raw.get("label_es")).strip()[:LABEL_MAX],
		"label_zh": cstr(raw.get("label_zh")).strip()[:LABEL_MAX],
	}


def _load() -> list[dict]:
	if not frappe.db.exists("Table Extra Schema", SCOPE):
		return []
	raw = frappe.db.get_value("Table Extra Schema", SCOPE, "columns_json")
	try:
		data = json.loads(raw) if raw else []
	except Exception:
		return []
	rows = [normalize_promo(r) for r in (data if isinstance(data, list) else [])]
	return [r for r in rows if r][:MAX_PROMOS]


def _save(rows: list[dict]) -> None:
	payload = json.dumps(rows, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", SCOPE):
		doc = frappe.get_doc("Table Extra Schema", SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({"doctype": "Table Extra Schema", "scope": SCOPE, "columns_json": payload}).insert(
			ignore_permissions=True
		)
	frappe.flags.ignore_permissions = False
	frappe.db.commit()


def _settings() -> tuple[bool, float]:
	"""(delivery pricing enabled, base fee) from the site-wide shop settings."""
	from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import _load_raw, _normalize_catalog_display

	cd = _normalize_catalog_display((_load_raw() or {}).get("catalogDisplay"))
	return bool(cd.get("deliveryEnabled")), flt(cd.get("deliveryBaseFee"))


def is_active(p: dict, today=None) -> bool:
	if cint(p.get("disabled")):
		return False
	today = today or getdate(nowdate())
	if p.get("valid_from") and getdate(p["valid_from"]) > today:
		return False
	if p.get("valid_upto") and getdate(p["valid_upto"]) < today:
		return False
	return True


def _fee_under(p: dict, base: float) -> float:
	if p["kind"] == "free_over":
		return 0.0
	if p["kind"] == "percent_off":
		return round(base * (1 - p["value"] / 100.0), 2)
	return round(max(base - p["value"], 0.0), 2)


def compute_delivery_quote(subtotal) -> dict:
	"""Fee for delivering an order worth ``subtotal`` (what the customer pays for goods, after any coupon).

	Picks the cheapest of the base fee and every active promo whose threshold the subtotal reaches.
	``free_from`` is the lowest active free-delivery threshold, ``remaining_to_free`` how far the subtotal is from it.
	"""
	enabled, base = _settings()
	out = {
		"enabled": enabled,
		"base_fee": base if enabled else 0.0,
		"fee": base if enabled else 0.0,
		"promo_id": "",
		"promo_label_es": "",
		"promo_label_zh": "",
		"free_from": None,
		"remaining_to_free": None,
	}
	if not enabled:
		return out

	sub = max(flt(subtotal), 0.0)
	promos = [p for p in _load() if is_active(p)]
	free_thresholds = [p["threshold"] for p in promos if p["kind"] == "free_over"]
	best_fee, best = base, None
	for p in promos:
		if sub < p["threshold"]:
			continue
		fee = _fee_under(p, base)
		if fee < best_fee:
			best_fee, best = fee, p
	out["fee"] = best_fee
	if best:
		out["promo_id"] = best["id"]
		out["promo_label_es"] = best["label_es"] or best["name"]
		out["promo_label_zh"] = best["label_zh"] or best["name"]
	if free_thresholds:
		out["free_from"] = min(free_thresholds)
		out["remaining_to_free"] = 0.0 if best_fee == 0 else round(max(out["free_from"] - sub, 0.0), 2)
	return out


def ensure_delivery_item() -> None:
	"""Shared non-stock Item that carries the delivery fee on a Sales Order (mirrors the POS-CAJA item)."""
	if frappe.db.exists("Item", DELIVERY_ITEM_CODE):
		if cint(frappe.db.get_value("Item", DELIVERY_ITEM_CODE, "disabled")):
			frappe.db.set_value("Item", DELIVERY_ITEM_CODE, "disabled", 0, update_modified=False)
		if cint(frappe.db.get_value("Item", DELIVERY_ITEM_CODE, "is_stock_item")):
			frappe.db.set_value("Item", DELIVERY_ITEM_CODE, "is_stock_item", 0, update_modified=False)
		return
	item_group = (
		(frappe.db.exists("Item Group", "Services") and "Services")
		or frappe.db.get_single_value("Stock Settings", "item_group")
		or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or "All Item Groups"
	)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": DELIVERY_ITEM_CODE,
			"item_name": "Envío / Delivery",
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
			"disabled": 0,
			"description": "Cargo de envío de pedidos de la tienda (lo agrega el sistema).",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


# ── guests ──────────────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
def get_delivery_quote(subtotal=0):
	"""What delivery costs for an order of this subtotal (server-authoritative, guest-readable)."""
	return compute_delivery_quote(subtotal)


@frappe.whitelist(allow_guest=True)
def list_public_delivery_promos():
	"""Delivery pricing + the promos running today, for the storefront strip. Disabled/out-of-window promos are hidden."""
	enabled, base = _settings()
	promos = [
		{
			"id": p["id"],
			"kind": p["kind"],
			"threshold": p["threshold"],
			"value": p["value"],
			"valid_upto": p["valid_upto"],
			"label_es": p["label_es"] or p["name"],
			"label_zh": p["label_zh"] or p["name"],
		}
		for p in _load()
		if is_active(p)
	]
	return {"enabled": enabled, "base_fee": base if enabled else 0.0, "promos": promos if enabled else []}


# ── staff (Promotions table) ────────────────────────────────────────────────


@frappe.whitelist()
def list_delivery_promos():
	_require_staff()
	enabled, base = _settings()
	return {"promos": _load(), "enabled": enabled, "base_fee": base}


@frappe.whitelist()
@idempotent_request
def save_delivery_promo(data):
	"""Create or update a delivery promo. ``data.id`` empty → new."""
	_require_staff()
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = data if isinstance(data, dict) else {}

	kind = data.get("kind")
	if kind not in KINDS:
		frappe.throw(_("Choose a promotion type"), frappe.ValidationError)
	threshold = _money(data.get("threshold"))
	value = _money(data.get("value"))
	if kind == "free_over" and threshold <= 0:
		frappe.throw(_("Enter the order amount from which delivery is free"), frappe.ValidationError)
	if kind == "percent_off" and not (0 < value <= 100):
		frappe.throw(_("Percentage must be between 0 and 100"), frappe.ValidationError)
	if kind == "amount_off" and value <= 0:
		frappe.throw(_("Enter the amount off the delivery fee"), frappe.ValidationError)
	vf, vu = _iso(data.get("valid_from")), _iso(data.get("valid_upto"))
	if vf and vu and vf > vu:
		frappe.throw(_("'Valid from' is after 'Valid until'"), frappe.ValidationError)

	rows = _load()
	pid = re.sub(r"[^A-Za-z0-9_-]", "", cstr(data.get("id")))[:24]
	existing = next((r for r in rows if r["id"] == pid), None) if pid else None
	if not existing and len(rows) >= MAX_PROMOS:
		frappe.throw(_("Maximum of {0} delivery promotions reached").format(MAX_PROMOS), frappe.ValidationError)

	promo = normalize_promo({**data, "id": pid or f"DP{frappe.generate_hash(length=8)}"})
	if not promo:
		frappe.throw(_("Invalid delivery promotion"), frappe.ValidationError)
	rows = [promo if r["id"] == promo["id"] else r for r in rows] if existing else [promo, *rows]
	_save(rows)
	return {"ok": True, "id": promo["id"], "promo": promo}


@frappe.whitelist()
def set_delivery_promo_disabled(id, disabled=1):  # noqa: A002 - mirrors the client field name
	"""'Cancel' a delivery promo (keeps it in the list, stops it applying)."""
	_require_staff()
	rows = _load()
	row = next((r for r in rows if r["id"] == cstr(id)), None)
	if not row:
		frappe.throw(_("Delivery promotion not found"), frappe.DoesNotExistError)
	row["disabled"] = 1 if cint(disabled) else 0
	_save(rows)
	return {"ok": True, "id": row["id"], "disabled": row["disabled"], "promo": row}


@frappe.whitelist()
def delete_delivery_promo(id):  # noqa: A002
	_require_staff()
	rows = _load()
	if not any(r["id"] == cstr(id) for r in rows):
		frappe.throw(_("Delivery promotion not found"), frappe.DoesNotExistError)
	_save([r for r in rows if r["id"] != cstr(id)])
	return {"ok": True, "id": cstr(id)}
