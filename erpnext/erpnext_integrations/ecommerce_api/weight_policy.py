"""Single sell-by-weight policy for Pedidos / Armado / remito / kiosk.

Rules (locked by smoke + edge):
- Sell UOM is WEIGHT / Kg / L / … → sell_by=weight. Item.weight_uom is ignored
  (that field is the UOM of peso promedio, often Kg on Nos/CAJA).
- Only measured ``total_weight`` is billable kg. Peso promedio × qty is estimate-only.
- Weight + no measured kg → amount 0 (Armado / scale required).
- Nos/CAJA → amount = rate × qty.
"""

from __future__ import annotations

from frappe.utils import cstr, flt

# Catalog token + mass/volume measures. Not Item.weight_uom metadata.
WEIGHT_SELL_UOM_TOKENS = frozenset(
	{
		"weight",
		"por peso",
		"porpeso",
		"kg",
		"kgs",
		"kilogram",
		"kilograms",
		"kilogramo",
		"kilogramos",
		"g",
		"gr",
		"gram",
		"grams",
		"gramo",
		"gramos",
		"l",
		"lt",
		"liter",
		"litre",
		"litros",
		"ml",
		"milliliter",
		"millilitre",
		"lb",
		"lbs",
		"oz",
		"ounce",
		"ounces",
	}
)

CATALOG_WEIGHT_TOKENS = frozenset({"weight", "por peso", "porpeso"})


def _norm_uom(uom) -> str:
	return cstr(uom or "").strip().lower()


def is_weight_sell_uom(uom) -> bool:
	"""True when line sells by weight/measure (WEIGHT token, Kg/L, or UOM kind=medida)."""
	if _norm_uom(uom) in WEIGHT_SELL_UOM_TOKENS:
		return True
	# p20 custom UOMs tagged medida (DB); never fail closed if module/DB unavailable.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.uom_kind_policy import is_medida_uom

		return bool(cstr(uom or "").strip()) and is_medida_uom(uom)
	except Exception:
		return False


def is_catalog_weight_uom(uom) -> bool:
	"""Catalog WEIGHT-style token (pack count + measure on Item.unit), not raw Kg/g.

	Custom UOMs with kind=medida that are *not* mass tokens (kg/g/l/…) count as catalog weight.
	"""
	key = _norm_uom(uom)
	if key in CATALOG_WEIGHT_TOKENS:
		return True
	if key in WEIGHT_SELL_UOM_TOKENS:
		# Known mass/volume measure name → not the catalog pack token.
		return False
	try:
		from erpnext.erpnext_integrations.ecommerce_api.uom_kind_policy import is_medida_uom

		return bool(cstr(uom or "").strip()) and is_medida_uom(uom)
	except Exception:
		return False


def is_mass_uom(uom) -> bool:
	"""True kg/g/lb — not the catalog WEIGHT pack-count token."""
	return is_weight_sell_uom(uom) and not is_catalog_weight_uom(uom)


def _g(row, key, default=None):
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def row_is_weight_sell(row) -> bool:
	"""True when the line sells by kg (line/stock UOM or explicit flag)."""
	flag = _g(row, "is_weight_based")
	if flag in (True, 1, "1"):
		return True
	uom = cstr(_g(row, "uom") or _g(row, "stock_uom") or "")
	return is_weight_sell_uom(uom)


def measured_kg(row):
	tw = flt(_g(row, "total_weight") or 0)
	return tw if tw > 0 else None


def effective_rate(row) -> float:
	"""List/manual rate, applying line disc% once (Pedidos often has no price_list_rate)."""
	rate = flt(_g(row, "rate") or 0)
	if rate <= 0:
		rate = flt(_g(row, "price_list_rate") or 0)
	disc = flt(_g(row, "discount_percentage") or 0)
	if disc <= 0 or disc >= 100:
		return 0.0 if disc >= 100 else rate
	already_net = flt(_g(row, "discount_amount") or 0) > 0 or (
		flt(_g(row, "price_list_rate") or 0) > 0
		and abs(flt(_g(row, "price_list_rate")) - flt(_g(row, "rate") or 0)) > 1e-9
	)
	if already_net:
		return rate
	return flt(rate * (1.0 - disc / 100.0))


def line_weight_policy(row) -> dict:
	"""Canonical money/PESO view of a sales line. Callers must not re-derive."""
	sell_by = "weight" if row_is_weight_sell(row) else "unit"
	kg = measured_kg(row)
	qty = flt(_g(row, "qty") or 0)
	rate = effective_rate(row)
	if sell_by == "weight":
		bill_qty = flt(kg or 0)
		amount = flt(rate * bill_qty) if rate > 0 and bill_qty > 0 else 0.0
		return {
			"sell_by": "weight",
			"is_weight_based": True,
			"measured_kg": kg,
			"bill_qty": bill_qty,
			"amount": amount,
			"unit_suffix": "/kg",
			"basis": "measured_kg" if kg else "none",
		}
	bill_qty = qty
	if rate > 0 and qty > 0:
		amount = flt(rate * qty)
	else:
		amount = flt(_g(row, "amount") or 0)
	return {
		"sell_by": "unit",
		"is_weight_based": False,
		"measured_kg": None,
		"bill_qty": bill_qty,
		"amount": amount,
		"unit_suffix": "",
		"basis": "units" if qty > 0 else "none",
	}


def line_amount(row) -> float:
	return flt(line_weight_policy(row)["amount"])


def billable_qty(row) -> float:
	return flt(line_weight_policy(row)["bill_qty"])
