"""
Sample promotions seed script — run with:
  bench --site dev_site_a execute create_sample_promotions.seed
or
  bench --site dev_site_a execute create_sample_promotions.run
"""

import frappe
from frappe.utils import today, add_days, getdate


# ── Item codes we'll use ──────────────────────────────────────────────────────

COSMOS       = "1e350db6-b302-4b66-846d-cbff4c196379"   # Cosmos, $19.474
SAPIENS      = "5d15f1af-1233-4f3a-ab9a-48de2f3b6712"   # Sapiens, $13.762
HOMO_DEUS    = "81ce6c61-8c72-47be-be11-8f7f30ca6dac"   # Homo Deus, $12.429
BREVES       = "68d4ee8c-d1ce-4ede-a5fc-c48eb84e3d7f"   # Breves respuestas, $7.971
CEREBRO_F    = "99d7f6b8-f9a8-4153-8d1b-4e8e20ecb79e"   # El cerebro femenino, $12.635
CEREBRO_M    = "937ccab6-80b7-4f20-8f02-8d5e01819ff0"   # El cerebro masculino, $12.436
GEN_EGOISTA  = "016f1be3-491a-47d9-b37f-a709a96a4b75"   # El gen egoísta, $12.666
UNIVERSO_EL  = "5583fe33-d2ed-458b-b6c2-6dc9c65c9960"   # El universo elegante, $15.253

PRICE_LIST   = "Standard Selling"


def skip_if_exists(doctype, name):
    if frappe.db.exists(doctype, name):
        print(f"  SKIP (already exists): {doctype} — {name}")
        return True
    return False


# ── 1. Pricing Rules ──────────────────────────────────────────────────────────

def create_pricing_rules():
    print("\n=== Pricing Rules ===")

    # PR-1: 3x2 on Cosmos (buy 3, get 1 free — same item)
    name = "PROMO-3x2-COSMOS"
    if not skip_if_exists("Pricing Rule", name):
        pr = frappe.get_doc({
            "doctype": "Pricing Rule",
            "name": name,
            "title": "3x2 en Cosmos",
            "apply_on": "Item Code",
            "items": [{"item_code": COSMOS}],
            "selling": 1, "buying": 0,
            "applicable_for": "",
            "price_or_product_discount": "Product",
            "same_item": 1,
            "free_qty": 1,
            "min_qty": 3,
            "disable": 0,
            "rule_description": "Llevá 3 Cosmos, pagás 2",
            "threshold_percentage": 60,
        })
        pr.insert(ignore_permissions=True)
        print(f"  CREATED: {name}")

    # PR-2: 15% OFF on Sapiens when buying 2+
    name = "PROMO-15OFF-SAPIENS"
    if not skip_if_exists("Pricing Rule", name):
        pr = frappe.get_doc({
            "doctype": "Pricing Rule",
            "name": name,
            "title": "15% OFF Sapiens (x2+)",
            "apply_on": "Item Code",
            "items": [{"item_code": SAPIENS}],
            "selling": 1, "buying": 0,
            "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage",
            "discount_percentage": 15,
            "min_qty": 2,
            "disable": 0,
            "rule_description": "15% de descuento comprando 2 o más",
            "threshold_percentage": 80,
        })
        pr.insert(ignore_permissions=True)
        print(f"  CREATED: {name}")

    # PR-3: Buy El gen egoísta → get Breves respuestas free
    name = "PROMO-REGALO-GEN"
    if not skip_if_exists("Pricing Rule", name):
        pr = frappe.get_doc({
            "doctype": "Pricing Rule",
            "name": name,
            "title": "Compra El gen egoísta → llevate Breves respuestas",
            "apply_on": "Item Code",
            "items": [{"item_code": GEN_EGOISTA}],
            "selling": 1, "buying": 0,
            "price_or_product_discount": "Product",
            "same_item": 0,
            "free_item": BREVES,
            "free_qty": 1,
            "min_qty": 1,
            "disable": 0,
            "rule_description": "Con la compra de El gen egoísta llevás Breves respuestas gratis",
        })
        pr.insert(ignore_permissions=True)
        print(f"  CREATED: {name}")

    # PR-4: 25% OFF Homo Deus — Flash Sale, expires today
    name = "PROMO-FLASH-HOMODEUS"
    if not skip_if_exists("Pricing Rule", name):
        pr = frappe.get_doc({
            "doctype": "Pricing Rule",
            "name": name,
            "title": "⚡ FLASH: 25% OFF Homo Deus",
            "apply_on": "Item Code",
            "items": [{"item_code": HOMO_DEUS}],
            "selling": 1, "buying": 0,
            "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage",
            "discount_percentage": 25,
            "valid_upto": today(),          # expires at end of today
            "disable": 0,
            "rule_description": "Oferta flash de hoy — 25% OFF",
        })
        pr.insert(ignore_permissions=True)
        print(f"  CREATED: {name}")

    # PR-5: 20% OFF — used by coupon VERANO20 (apply on grand total)
    name = "PROMO-CUPON-VERANO20"
    if not skip_if_exists("Pricing Rule", name):
        pr = frappe.get_doc({
            "doctype": "Pricing Rule",
            "name": name,
            "title": "Cupón VERANO20 — 20% OFF",
            "apply_on": "Transaction",
            "selling": 1, "buying": 0,
            "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage",
            "discount_percentage": 20,
            "disable": 0,
            "rule_description": "20% de descuento en toda la compra",
        })
        pr.insert(ignore_permissions=True)
        print(f"  CREATED: {name}")

    # PR-6: 10% OFF — used by coupon LIBROS10
    name = "PROMO-CUPON-LIBROS10"
    if not skip_if_exists("Pricing Rule", name):
        pr = frappe.get_doc({
            "doctype": "Pricing Rule",
            "name": name,
            "title": "Cupón LIBROS10 — 10% OFF",
            "apply_on": "Transaction",
            "selling": 1, "buying": 0,
            "price_or_product_discount": "Price",
            "rate_or_discount": "Discount Percentage",
            "discount_percentage": 10,
            "disable": 0,
            "rule_description": "10% de descuento con código de fidelidad",
        })
        pr.insert(ignore_permissions=True)
        print(f"  CREATED: {name}")

    frappe.db.commit()


# ── 2. Coupon Codes ───────────────────────────────────────────────────────────

def _get_pr_name(title_fragment):
    """Return the actual DB name for a Pricing Rule whose title contains the fragment."""
    results = frappe.db.get_all(
        "Pricing Rule",
        filters=[["title", "like", f"%{title_fragment}%"]],
        fields=["name"],
        limit=1,
    )
    return results[0]["name"] if results else None


def create_coupon_codes():
    print("\n=== Coupon Codes ===")

    pr_verano  = _get_pr_name("VERANO20")
    pr_libros  = _get_pr_name("LIBROS10")

    if not pr_verano or not pr_libros:
        print(f"  ERROR: Could not find Pricing Rules (verano={pr_verano}, libros={pr_libros})")
        return

    coupons = [
        {
            "coupon_code": "VERANO20",
            "coupon_name": "Descuento verano 20%",
            "coupon_type": "Promotional",
            "pricing_rule": pr_verano,
            "maximum_use": 50,
        },
        {
            "coupon_code": "LIBROS10",
            "coupon_name": "Descuento fidelidad 10%",
            "coupon_type": "Promotional",
            "pricing_rule": pr_libros,
            "maximum_use": 0,   # unlimited
        },
        {
            "coupon_code": "PRIMERAVEZ",
            "coupon_name": "Primera compra — 15% OFF",
            "coupon_type": "Promotional",
            "pricing_rule": pr_libros,  # reuse 10% rule
            "maximum_use": 1,
        },
    ]

    for c in coupons:
        code = c["coupon_code"]
        if frappe.db.exists("Coupon Code", {"coupon_code": code}):
            print(f"  SKIP (already exists): Coupon — {code}")
            continue
        doc = frappe.get_doc({"doctype": "Coupon Code", **c})
        doc.insert(ignore_permissions=True)
        print(f"  CREATED: {code}")

    frappe.db.commit()


# ── 3. Product Bundles ────────────────────────────────────────────────────────

def ensure_bundle_parent_item(item_code, item_name, rate):
    """Create a non-stock parent item for the bundle if it doesn't exist."""
    if frappe.db.exists("Item", item_code):
        return
    item = frappe.get_doc({
        "doctype": "Item",
        "item_code": item_code,
        "item_name": item_name,
        "item_group": "Products",
        "stock_uom": "Unit",
        "is_stock_item": 0,
        "include_item_in_manufacturing": 0,
        "description": item_name,
    })
    item.insert(ignore_permissions=True)
    # Add price
    ip = frappe.get_doc({
        "doctype": "Item Price",
        "item_code": item_code,
        "price_list": PRICE_LIST,
        "price_list_rate": rate,
        "selling": 1,
    })
    ip.insert(ignore_permissions=True)
    print(f"  CREATED item + price: {item_code} = ${rate:,.0f}")


def create_product_bundles():
    print("\n=== Product Bundles ===")

    # Bundle 1: Combo Cerebro (El cerebro femenino + El cerebro masculino)
    #   Individual: $12.635 + $12.436 = $25.071
    #   Bundle price: $22.000 (~12% off)
    bundle_sku = "BUNDLE-COMBO-CEREBRO"
    ensure_bundle_parent_item(bundle_sku, "Combo: Los dos Cerebros", 22000)
    if not skip_if_exists("Product Bundle", bundle_sku):
        pb = frappe.get_doc({
            "doctype": "Product Bundle",
            "new_item_code": bundle_sku,
            "description": "El cerebro femenino + El cerebro masculino — precio especial",
            "items": [
                {"item_code": CEREBRO_F, "qty": 1, "uom": "Unit"},
                {"item_code": CEREBRO_M, "qty": 1, "uom": "Unit"},
            ],
        })
        pb.insert(ignore_permissions=True)
        print(f"  CREATED: {bundle_sku}")

    # Bundle 2: Pack Cosmos x3 (3 copies of Cosmos)
    #   Individual: $19.474 × 3 = $58.422
    #   Bundle price: $50.000 (~14% off)
    bundle_sku = "BUNDLE-COSMOS-X3"
    ensure_bundle_parent_item(bundle_sku, "Pack: Cosmos × 3 unidades", 50000)
    if not skip_if_exists("Product Bundle", bundle_sku):
        pb = frappe.get_doc({
            "doctype": "Product Bundle",
            "new_item_code": bundle_sku,
            "description": "3 ejemplares de Cosmos — precio especial para regalo o aula",
            "items": [
                {"item_code": COSMOS, "qty": 3, "uom": "Unit"},
            ],
        })
        pb.insert(ignore_permissions=True)
        print(f"  CREATED: {bundle_sku}")

    # Bundle 3: Trilogía Universo (Cosmos + El universo elegante + El universo en cáscara)
    #   Individual: $19.474 + $15.253 + $19.233 = $53.960
    #   Bundle price: $46.000 (~15% off)
    UNIVERSO_CASCARA = "ba92bb15-7c83-48d2-b7c8-af5df6599430"
    bundle_sku = "BUNDLE-TRILOGIA-UNIVERSO"
    ensure_bundle_parent_item(bundle_sku, "Trilogía del Universo", 46000)
    if not skip_if_exists("Product Bundle", bundle_sku):
        pb = frappe.get_doc({
            "doctype": "Product Bundle",
            "new_item_code": bundle_sku,
            "description": "Cosmos + El universo elegante + El universo en una cáscara de nuez",
            "items": [
                {"item_code": COSMOS,         "qty": 1, "uom": "Unit"},
                {"item_code": UNIVERSO_EL,    "qty": 1, "uom": "Unit"},
                {"item_code": UNIVERSO_CASCARA, "qty": 1, "uom": "Unit"},
            ],
        })
        pb.insert(ignore_permissions=True)
        print(f"  CREATED: {bundle_sku}")

    frappe.db.commit()


# ── Entry point ───────────────────────────────────────────────────────────────

def run():
    print("Creating sample promotions for i009 + i010 testing…")
    create_pricing_rules()
    create_coupon_codes()
    create_product_bundles()
    print("\nDone. All sample promotions created.")
