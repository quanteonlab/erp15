"""
E-Commerce Integration API
Provides comprehensive REST API endpoints for e-commerce integration with ERPNext

All endpoints are whitelisted and can be accessed via:
- REST API: /api/method/erpnext.erpnext_integrations.ecommerce_api.api.<method_name>
- JSON-RPC: frappe.call('erpnext.erpnext_integrations.ecommerce_api.api.<method_name>')
"""

import frappe
import csv
import io
import os
import base64
import zipfile
from frappe import _
from frappe.utils import (
	cint,
	flt,
	getdate,
	nowdate,
	get_datetime,
	add_days,
	now_datetime,
)
from erpnext.stock.get_item_details import get_item_details as get_item_details_base
from erpnext.accounts.doctype.pricing_rule.pricing_rule import apply_pricing_rule


# ========================================
# PRODUCT / ITEM APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def get_products(
	filters=None,
	fields=None,
	start=0,
	page_length=20,
	order_by="modified desc",
	search_term=None,
	item_group=None,
	price_list=None,
	in_stock_only=0,
):
	"""
	Get list of products with pagination and filtering

	Args:
		filters (dict): Additional filters for Item doctype
		fields (list): List of fields to return (default: all standard fields)
		start (int): Pagination offset (default: 0)
		page_length (int): Number of records per page (default: 20)
		order_by (str): Sort order (default: "modified desc")
		search_term (str): Search in item_code, item_name, description
		item_group (str): Filter by item group
		price_list (str): Price list to fetch prices from

	Returns:
		dict: {
			"items": List of item dictionaries,
			"total_count": Total number of items,
			"has_more": Boolean indicating if more records exist
		}
	"""
	if not fields:
		fields = [
			"name",
			"item_code",
			"item_name",
			"description",
			"item_group",
			"custom_normalized_title",
			"custom_variant_group",
			"custom_pack_qty",
			"stock_uom",
			"is_stock_item",
			"has_variants",
			"variant_of",
			"image",
			"thumbnail",
			"disabled",
			"standard_rate",
			"opening_stock",
			"brand",
			"modified",
		]

	if isinstance(filters, str):
		import json
		filters = json.loads(filters)

	if not filters:
		filters = {}

	# Convert string parameters to int
	start = cint(start)
	page_length = cint(page_length)

	# Default filter: only enabled items
	filters["disabled"] = 0

	# Item group filter
	if item_group:
		filters["item_group"] = item_group

	# Search term: use or_filters so Frappe ORs across fields while ANDing with base filters
	or_filters = None
	if search_term:
		or_filters = [
			["item_code", "like", f"%{search_term}%"],
			["item_name", "like", f"%{search_term}%"],
			["description", "like", f"%{search_term}%"],
		]

	# Get items
	items = frappe.get_list(
		"Item",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		start=start,
		page_length=page_length,
		order_by=order_by,
	)

	# Get total count (frappe.db.count doesn't support or_filters — use get_all with fields=["name"])
	total_count = len(frappe.get_all("Item", filters=filters, or_filters=or_filters, fields=["name"], limit_page_length=0))

	# Add pricing information if price_list provided
	if price_list:
		for item in items:
			item["price_list_rate"] = get_item_price(item.item_code, price_list)
			item["price_list"] = price_list

	# Add stock information
	for item in items:
		if item.get("is_stock_item"):
			item["stock_qty"] = get_stock_balance(item.item_code)

	# Filter to in-stock items only (stock items with qty > 0; services pass through)
	if cint(in_stock_only):
		items = [
			i for i in items
			if not i.get("is_stock_item") or (i.get("stock_qty") or 0) > 0
		]
		total_count = len(items)

	# Attach barcode data in bulk (single query for all items)
	_attach_barcodes(items)

	return {
		"items": items,
		"total_count": total_count,
		"has_more": (start + page_length) < total_count,
	}


def _attach_barcodes(items):
	"""Attach barcodes list to each item dict (in-place, single DB query)."""
	item_codes = [i.get("item_code") for i in items if i.get("item_code")]
	if not item_codes:
		return
	rows = frappe.get_all(
		"Item Barcode",
		filters={"parent": ["in", item_codes]},
		fields=["parent", "barcode", "barcode_type"],
	)
	barcode_map = {}
	for row in rows:
		barcode_map.setdefault(row.parent, []).append(
			{"barcode": row.barcode, "barcode_type": row.barcode_type or "CODE128"}
		)
	for item in items:
		item["barcodes"] = barcode_map.get(item.get("item_code"), [])


@frappe.whitelist(allow_guest=True)
def get_product(item_code, price_list=None, warehouse=None, customer=None):
	"""
	Get detailed information about a single product

	Args:
		item_code (str): Item code or item name
		price_list (str): Price list to fetch price from
		warehouse (str): Warehouse to check stock from
		customer (str): Customer to apply customer-specific pricing

	Returns:
		dict: Complete item information including pricing, stock, variants, attributes
	"""
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found").format(item_code))

	item = frappe.get_doc("Item", item_code)

	# Build response
	product = item.as_dict()

	# Add pricing
	if price_list:
		product["price_list_rate"] = get_item_price(item_code, price_list)
		product["price_list"] = price_list

		# Apply pricing rules if customer provided
		if customer:
			pricing_args = {
				"item_code": item_code,
				"customer": customer,
				"price_list": price_list,
				"transaction_date": nowdate(),
				"qty": 1,
				"doctype": "Sales Order",
			}
			pricing_rule_result = apply_pricing_rule(pricing_args)
			if pricing_rule_result:
				product["pricing_rules"] = pricing_rule_result

	# Add stock information
	if item.is_stock_item:
		if warehouse:
			product["stock_qty"] = get_stock_balance(item_code, warehouse)
			product["warehouse"] = warehouse
		else:
			product["stock_qty"] = get_stock_balance(item_code)

		product["projected_qty"] = get_projected_qty(item_code, warehouse)

	# Add variants if this is a template
	if item.has_variants:
		product["variants"] = get_item_variants(item_code)

	# Add variant attributes if this is a variant
	if item.variant_of:
		product["attributes"] = get_item_attributes(item_code)

	# Add item prices from all price lists
	product["all_prices"] = get_all_item_prices(item_code)

	# Add item images
	product["images"] = [
		{"image": img.image_path, "is_primary": 1 if idx == 0 else 0}
		for idx, img in enumerate(item.get("website_image", []))
	] if hasattr(item, "website_image") else []

	return product


## ── EAN-13 helpers ──────────────────────────────────────────────────────────


def _ean13_check_digit(digits_12: str) -> int:
	"""Return the EAN-13 check digit for a 12-character numeric string."""
	total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits_12))
	return (10 - (total % 10)) % 10


def _generate_ean13(sequence_num: int) -> str:
	"""
	Build an EAN-13 barcode for internal use.
	Format: 200 (GS1 private-use prefix) + 9-digit zero-padded sequence + check digit.
	Supports up to 999,999,999 unique products.
	"""
	body = str(sequence_num).zfill(9)
	digits_12 = "200" + body
	return digits_12 + str(_ean13_check_digit(digits_12))


def _next_internal_ean13_seq() -> int:
	"""Return the next available sequence number for internal EAN-13 barcodes."""
	row = frappe.db.sql(
		"SELECT MAX(CAST(SUBSTRING(barcode, 4, 9) AS UNSIGNED)) "
		"FROM `tabItem Barcode` WHERE barcode REGEXP '^200[0-9]{10}$'",
	)
	last = row[0][0] if row and row[0][0] else 0
	return int(last) + 1


@frappe.whitelist()
def assign_item_barcode(item_code):
	"""
	Assign an EAN-13 barcode to an item if it does not already have one.
	Stores the barcode in the Item Barcode child table with type EAN.
	Returns the barcode value (existing or newly created).
	"""
	existing = frappe.db.get_value("Item Barcode", {"parent": item_code}, "barcode")
	if existing:
		return {"barcode": existing, "created": False}

	seq = _next_internal_ean13_seq()
	barcode = _generate_ean13(seq)

	item = frappe.get_doc("Item", item_code)
	item.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})
	item.save(ignore_permissions=True)
	frappe.db.commit()

	return {"barcode": barcode, "created": True}


@frappe.whitelist()
def assign_barcodes_to_all_items():
	"""
	Bulk-assign EAN-13 barcodes to every enabled item that has no barcode yet.
	Returns {assigned: N, items: [{item_code, barcode}]}.
	"""
	items_without = frappe.db.sql(
		"""
		SELECT item_code FROM `tabItem`
		WHERE disabled = 0
		  AND item_code NOT IN (SELECT DISTINCT parent FROM `tabItem Barcode`)
		ORDER BY item_code
		""",
		as_dict=True,
	)

	assigned = []
	for row in items_without:
		seq = _next_internal_ean13_seq()
		barcode = _generate_ean13(seq)
		item = frappe.get_doc("Item", row.item_code)
		item.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})
		item.save(ignore_permissions=True)
		assigned.append({"item_code": row.item_code, "barcode": barcode})

	if assigned:
		frappe.db.commit()

	return {"assigned": len(assigned), "items": assigned}


@frappe.whitelist(allow_guest=True)
def search_by_barcode(barcode, price_list=None):
	"""
	Find a product by barcode value.
	Falls back to matching item_code directly if no Item Barcode record exists.

	Returns the same structure as get_product().
	"""
	item_code = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
	if not item_code:
		# Try direct item_code match (useful when item_code IS the barcode)
		if frappe.db.exists("Item", barcode):
			item_code = barcode
	if not item_code:
		frappe.throw(_("No item found for barcode: {0}").format(barcode), frappe.DoesNotExistError)
	return get_product(item_code, price_list=price_list)


@frappe.whitelist(allow_guest=True)
def get_items_for_label_print(price_list=None, item_codes=None):
	"""
	Return all active items with their barcodes and prices for bulk label printing.

	Args:
		price_list (str): Price list to fetch prices from.
		item_codes (str|list): Optional JSON list of specific item codes to fetch.

	Returns:
		list[dict]: Items with {item_code, item_name, price_list_rate, barcodes}
	"""
	import json
	filters = {"disabled": 0}
	if item_codes:
		if isinstance(item_codes, str):
			item_codes = json.loads(item_codes)
		filters["item_code"] = ["in", item_codes]

	items = frappe.get_all(
		"Item",
		filters=filters,
		fields=["item_code", "item_name", "image"],
		order_by="item_name asc",
	)

	if price_list:
		for item in items:
			item["price_list_rate"] = get_item_price(item.item_code, price_list)

	_attach_barcodes(items)
	return items


@frappe.whitelist(allow_guest=True)
def get_item_groups():
	"""Return item groups with parent metadata for category navigation."""
	return frappe.get_all(
		"Item Group",
		filters={"is_group": 0},
		fields=["name", "item_group_name", "parent_item_group", "is_group"],
		order_by="parent_item_group asc, name asc",
	)


# ── Promotions ────────────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
def get_active_promotions(price_list=None):
	"""
	Return all currently active, selling-side Pricing Rules with child-table data
	(applicable_items, applicable_groups, applicable_brands) attached.
	Called once on POS load; the client caches results for 5 minutes (or 1 minute
	if any time-based promotions exist).

	Custom scheduling fields (happy_hour_from, happy_hour_to, applicable_days,
	flash_sale) are included when present on the Pricing Rule doctype.
	"""
	today = nowdate()

	# Detect which custom scheduling fields have been added to Pricing Rule
	has_time_fields = frappe.db.has_column("Pricing Rule", "happy_hour_from")
	has_days_field  = frappe.db.has_column("Pricing Rule", "applicable_days")
	has_flash_field = frappe.db.has_column("Pricing Rule", "flash_sale")

	base_fields = [
		"name", "title", "apply_on", "price_or_product_discount",
		"min_qty", "max_qty", "min_amt", "max_amt",
		"valid_from", "valid_upto",
		"rate_or_discount", "discount_percentage", "discount_amount", "rate",
		"same_item", "free_item", "free_qty", "free_item_rate",
		"is_recursive", "recurse_for",
		"threshold_percentage", "rule_description",
	]
	if has_time_fields:
		base_fields += ["happy_hour_from", "happy_hour_to"]
	if has_days_field:
		base_fields += ["applicable_days"]
	if has_flash_field:
		base_fields += ["flash_sale"]

	rules = frappe.get_all(
		"Pricing Rule",
		filters={"disable": 0, "selling": 1},
		fields=base_fields,
	)

	# Filter by date validity
	rules = [
		r for r in rules
		if (not r.get("valid_from") or getdate(r["valid_from"]) <= getdate(today))
		and (not r.get("valid_upto") or getdate(r["valid_upto"]) >= getdate(today))
	]

	# Filter by time of day (happy hour)
	if has_time_fields:
		rules = [r for r in rules if _is_happy_hour_active(r)]

	# Filter by day of week
	if has_days_field:
		rules = [r for r in rules if _is_day_active(r)]

	if not rules:
		return []

	rule_names = [r["name"] for r in rules]

	# Attach child table data in bulk
	child_tables = [
		("Pricing Rule Item Code",  "item_code",  "applicable_items"),
		("Pricing Rule Item Group", "item_group", "applicable_groups"),
		("Pricing Rule Brand",      "brand",      "applicable_brands"),
	]
	for child_dt, field, key in child_tables:
		try:
			rows = frappe.get_all(
				child_dt,
				filters={"parent": ["in", rule_names]},
				fields=["parent", field],
			)
			mapping = {}
			for row in rows:
				mapping.setdefault(row["parent"], []).append(row[field])
			for r in rules:
				r[key] = mapping.get(r["name"], [])
		except Exception:
			for r in rules:
				r.setdefault(key, [])

	return rules


def _is_happy_hour_active(rule):
	"""Return True if rule has no time window or the current time is within it."""
	hf = rule.get("happy_hour_from")
	ht = rule.get("happy_hour_to")
	if not hf or not ht:
		return True
	now = frappe.utils.nowtime()  # "HH:MM:SS"
	return hf <= now <= ht


def _is_day_active(rule):
	"""Return True if rule has no day restriction or today matches."""
	days = rule.get("applicable_days") or ""
	if not days:
		return True
	day_map = {
		0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves",
		4: "Viernes", 5: "Sábado", 6: "Domingo",
	}
	today_name = day_map[frappe.utils.getdate().weekday()]
	# applicable_days is stored as a comma-separated string by MultiSelectList
	return today_name in [d.strip() for d in days.split(",")]


@frappe.whitelist(allow_guest=True)
def apply_cart_promotions(items, price_list=None):
	"""
	Given cart items [{item_code, qty, rate, amount}], return computed discounts
	and upsell hints using ERPNext Pricing Rules.

	Returns:
		{
			line_discounts: [{item_code, discount_percentage, discounted_rate,
			                  discount_amount, free_item, free_qty, rule_name}],
			upsell_hints:   [{rule_name, title, message, items_needed,
			                  qty_needed, progress_pct}]
		}
	"""
	import json

	if isinstance(items, str):
		items = json.loads(items)

	if not items:
		return {"line_discounts": [], "upsell_hints": []}

	today = nowdate()
	line_discounts = []

	for item in items:
		try:
			args = frappe._dict({
				"item_code": item["item_code"],
				"qty": flt(item["qty"]),
				"price_list": price_list or "Standard Selling",
				"transaction_date": today,
				"doctype": "Sales Invoice",
				"selling": 1,
			})
			result = apply_pricing_rule(args)
			if not result:
				continue
			rd = result[0] if isinstance(result, list) else result
			if not rd:
				continue
			disc_pct = flt(rd.get("discount_percentage") or 0)
			if disc_pct > 0:
				base = flt(item["rate"])
				disc_rate = base * (1 - disc_pct / 100)
				line_discounts.append({
					"item_code": item["item_code"],
					"discount_percentage": disc_pct,
					"discounted_rate": disc_rate,
					"discount_amount": (base - disc_rate) * flt(item["qty"]),
					"free_item": rd.get("free_item"),
					"free_qty": flt(rd.get("free_qty") or 0),
					"rule_name": rd.get("pricing_rule") or "",
				})
		except Exception:
			continue  # Pricing rule errors are non-fatal

	# Upsell hints: active rules near their threshold but not yet triggered
	active_rules = get_active_promotions(price_list=price_list)
	item_qty_map = {i["item_code"]: flt(i["qty"]) for i in items}
	total_amount = sum(flt(i.get("amount", 0)) for i in items)
	upsell_hints = []

	for rule in active_rules:
		thresh = flt(rule.get("threshold_percentage") or 80)

		# Qty-based upsell
		if flt(rule.get("min_qty") or 0) > 0:
			for ic in rule.get("applicable_items", []):
				current = item_qty_map.get(ic, 0)
				min_q = flt(rule["min_qty"])
				needed = min_q - current
				progress = (current / min_q) * 100 if min_q else 0
				if 0 < needed and progress >= thresh:
					disc_label = f"{rule.get('discount_percentage', '')}% off" if rule.get('discount_percentage') else "a discount"
					upsell_hints.append({
						"rule_name": rule["name"],
						"title": rule.get("title") or rule["name"],
						"message": f"Add {int(needed)} more to unlock {disc_label}",
						"items_needed": [ic],
						"qty_needed": needed,
						"progress_pct": min(progress, 99),
					})

		# Amount-based upsell
		if flt(rule.get("min_amt") or 0) > 0:
			min_a = flt(rule["min_amt"])
			needed_amt = min_a - total_amount
			progress = (total_amount / min_a) * 100 if min_a else 0
			if 0 < needed_amt and progress >= thresh:
				disc_label = f"{rule.get('discount_percentage', '')}% off" if rule.get('discount_percentage') else "a discount"
				upsell_hints.append({
					"rule_name": rule["name"],
					"title": rule.get("title") or rule["name"],
					"message": f"Add ${needed_amt:.2f} more to unlock {disc_label}",
					"items_needed": [],
					"qty_needed": 0,
					"progress_pct": min(progress, 99),
				})

	return {"line_discounts": line_discounts, "upsell_hints": upsell_hints[:3]}


@frappe.whitelist(allow_guest=True)
def get_promotions_for_item(item_code, price_list=None):
	"""
	Return all promotions relevant to a specific item:
	  - pricing_rules: active Pricing Rules targeting this item (by code, group, or brand)
	  - bundles: Product Bundles that contain this item as a component

	Used by the POS Promotion Panel when a cashier taps the "Promos" chip on a product card.
	"""
	pricing_rules = _get_pricing_rules_for_item(item_code)
	bundles = _get_bundles_containing_item(item_code, price_list=price_list)
	return {"pricing_rules": pricing_rules, "bundles": bundles}


def _get_pricing_rules_for_item(item_code):
	"""Return active Pricing Rules that apply to this item (direct, group, or brand match)."""
	item = frappe.db.get_value("Item", item_code, ["item_group", "brand"], as_dict=True)
	if not item:
		return []

	item_group = item.get("item_group") or ""
	brand = item.get("brand") or ""

	all_rules = get_active_promotions()
	matching = []

	for rule in all_rules:
		apply_on = rule.get("apply_on", "")
		if apply_on == "Item Code":
			if item_code in rule.get("applicable_items", []):
				matching.append(rule)
		elif apply_on == "Item Group":
			if item_group and item_group in rule.get("applicable_groups", []):
				matching.append(rule)
		elif apply_on == "Brand":
			if brand and brand in rule.get("applicable_brands", []):
				matching.append(rule)

	return matching


def _get_bundles_containing_item(item_code, price_list=None):
	"""Return Product Bundles that have this item as a component, with full component list and price."""
	bundle_rows = frappe.get_all(
		"Product Bundle Item",
		filters={"item_code": item_code},
		fields=["parent"],
	)
	if not bundle_rows:
		return []

	bundle_skus = list({r["parent"] for r in bundle_rows})
	pl = price_list or "Standard Selling"
	result = []

	for bundle_sku in bundle_skus:
		bundle_item = frappe.db.get_value(
			"Item", bundle_sku, ["item_name", "disabled"], as_dict=True
		)
		if not bundle_item or cint(bundle_item.get("disabled")):
			continue

		components = frappe.get_all(
			"Product Bundle Item",
			filters={"parent": bundle_sku},
			fields=["item_code", "qty"],
		)
		for comp in components:
			comp["item_name"] = (
				frappe.db.get_value("Item", comp["item_code"], "item_name") or comp["item_code"]
			)

		bundle_price = frappe.db.get_value(
			"Item Price",
			{"item_code": bundle_sku, "price_list": pl, "selling": 1},
			"price_list_rate",
		) or 0

		result.append({
			"bundle_item_code": bundle_sku,
			"bundle_name": bundle_item["item_name"],
			"bundle_price": flt(bundle_price),
			"components": components,
		})

	return result


@frappe.whitelist(allow_guest=True)
def validate_coupon_code(coupon_code):
	"""
	Check whether a coupon code is valid and return its discount details.
	Does NOT consume the coupon (that happens at invoice submit via apply_pricing_rule).

	Returns:
		{valid, message, discount_percentage, discount_amount, free_item, coupon_name}
	"""
	if not coupon_code:
		return {"valid": False, "message": "Ingresá un código de cupón"}

	doc = frappe.db.get_value(
		"Coupon Code",
		{"coupon_code": coupon_code},
		["name", "pricing_rule", "maximum_use", "used", "customer"],
		as_dict=True,
	)

	if not doc:
		return {"valid": False, "message": "Cupón no encontrado"}

	max_use = cint(doc.get("maximum_use") or 0)
	used = cint(doc.get("used") or 0)
	if max_use and used >= max_use:
		return {"valid": False, "message": "Cupón agotado — ya se usó el máximo de veces"}

	if not doc.get("pricing_rule"):
		return {"valid": False, "message": "Cupón sin regla de descuento configurada"}

	try:
		rule = frappe.get_doc("Pricing Rule", doc["pricing_rule"])
	except frappe.DoesNotExistError:
		return {"valid": False, "message": "Regla de descuento no encontrada"}

	# Check rule date validity
	today = getdate(nowdate())
	if rule.valid_from and getdate(rule.valid_from) > today:
		return {"valid": False, "message": "Cupón todavía no está activo"}
	if rule.valid_upto and getdate(rule.valid_upto) < today:
		return {"valid": False, "message": "Cupón vencido"}

	return {
		"valid": True,
		"coupon_name": doc["name"],
		"discount_percentage": flt(rule.discount_percentage or 0),
		"discount_amount": flt(rule.discount_amount or 0),
		"free_item": rule.free_item or None,
		"message": f"Cupón válido — {rule.title or rule.name}",
	}


@frappe.whitelist(allow_guest=True)
def validate_discount_pin(pin):
	"""
	Validate a manager PIN for authorising above-threshold cashier discounts.
	The PIN is stored in site_config.json under the key 'pos_manager_pin'.
	Returns {authorized: true/false}.
	"""
	if not pin:
		return {"authorized": False}

	expected = frappe.conf.get("pos_manager_pin")
	if not expected:
		# No PIN configured — all discounts allowed (open mode)
		return {"authorized": True}

	return {"authorized": str(pin) == str(expected)}


@frappe.whitelist(allow_guest=True)
def get_item_variants(item_code):
	"""
	Get all variants of a template item

	Args:
		item_code (str): Template item code

	Returns:
		list: List of variant items
	"""
	return frappe.get_all(
		"Item",
		filters={"variant_of": item_code, "disabled": 0},
		fields=["name", "item_code", "item_name", "image", "standard_rate"],
	)


@frappe.whitelist(allow_guest=True)
def get_item_attributes(item_code):
	"""
	Get variant attributes for an item

	Args:
		item_code (str): Item code

	Returns:
		list: List of item variant attributes
	"""
	return frappe.get_all(
		"Item Variant Attribute",
		filters={"parent": item_code},
		fields=["attribute", "attribute_value"],
	)


@frappe.whitelist(allow_guest=True)
def get_item_price(item_code, price_list, customer=None, uom=None):
	"""
	Get item price from a specific price list

	Args:
		item_code (str): Item code
		price_list (str): Price list name
		customer (str): Customer name (optional)
		uom (str): Unit of measure (optional)

	Returns:
		float: Price list rate
	"""
	filters = {
		"item_code": item_code,
		"price_list": price_list,
	}

	if customer:
		filters["customer"] = customer

	if uom:
		filters["uom"] = uom

	price = frappe.db.get_value(
		"Item Price",
		filters,
		"price_list_rate",
		order_by="valid_from desc",
	)

	return flt(price) if price else 0.0


@frappe.whitelist(allow_guest=True)
def get_all_item_prices(item_code):
	"""
	Get all price list rates for an item

	Args:
		item_code (str): Item code

	Returns:
		list: List of all price list rates
	"""
	return frappe.get_all(
		"Item Price",
		filters={"item_code": item_code},
		fields=["price_list", "price_list_rate", "currency", "valid_from", "valid_upto"],
		order_by="price_list",
	)


# ========================================
# INVENTORY / STOCK APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def get_stock_balance(item_code, warehouse=None):
	"""
	Get current stock balance for an item

	Args:
		item_code (str): Item code
		warehouse (str): Warehouse name (optional, returns total if not provided)

	Returns:
		float: Available stock quantity
	"""
	from erpnext.stock.utils import get_stock_balance as get_stock_balance_util

	return get_stock_balance_util(item_code, warehouse or None)


@frappe.whitelist(allow_guest=True)
def get_projected_qty(item_code, warehouse=None):
	"""
	Get projected quantity (available - reserved) for an item

	Args:
		item_code (str): Item code
		warehouse (str): Warehouse name (optional)

	Returns:
		float: Projected quantity
	"""
	from erpnext.stock.stock_balance import get_balance_qty_from_sle

	return get_balance_qty_from_sle(item_code, warehouse or None)


@frappe.whitelist(allow_guest=True)
def check_stock_availability(items, warehouse=None):
	"""
	Batch check stock availability for multiple items

	Args:
		items (list): List of dicts with item_code and qty
		warehouse (str): Warehouse to check (optional)

	Returns:
		list: List of dicts with item_code, requested_qty, available_qty, is_available
	"""
	if isinstance(items, str):
		import json
		items = json.loads(items)

	results = []
	for item in items:
		item_code = item.get("item_code")
		requested_qty = flt(item.get("qty", 1))

		available_qty = get_stock_balance(item_code, warehouse)

		results.append({
			"item_code": item_code,
			"requested_qty": requested_qty,
			"available_qty": available_qty,
			"is_available": available_qty >= requested_qty,
		})

	return results


@frappe.whitelist(allow_guest=True)
def update_stock(item_code, warehouse, qty, posting_date=None):
	"""
	Update stock level for an item (creates Stock Entry)

	Args:
		item_code (str): Item code
		warehouse (str): Target warehouse
		qty (float): Quantity to add (positive) or remove (negative)
		posting_date (str): Date for stock entry (default: today)

	Returns:
		dict: Stock entry details
	"""
	from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

	if not posting_date:
		posting_date = nowdate()

	# Determine purpose based on qty
	purpose = "Material Receipt" if flt(qty) > 0 else "Material Issue"

	stock_entry = make_stock_entry(
		item_code=item_code,
		qty=abs(flt(qty)),
		to_warehouse=warehouse if flt(qty) > 0 else None,
		from_warehouse=warehouse if flt(qty) < 0 else None,
		posting_date=posting_date,
		purpose=purpose,
		do_not_save=True,
	)

	stock_entry.insert()
	stock_entry.submit()

	return {
		"stock_entry": stock_entry.name,
		"item_code": item_code,
		"warehouse": warehouse,
		"new_qty": get_stock_balance(item_code, warehouse),
	}


# ========================================
# CUSTOMER APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def get_customer(customer_name=None, email=None):
	"""
	Get customer details by name or email

	Args:
		customer_name (str): Customer name/ID
		email (str): Customer email address

	Returns:
		dict: Customer details with addresses and contacts
	"""
	if not customer_name and not email:
		frappe.throw(_("Either customer_name or email is required"))

	if email and not customer_name:
		# Try to find customer by email
		customer_name = frappe.db.get_value(
			"Contact",
			{"email_id": email},
			"name",
		)
		if customer_name:
			customer_name = frappe.db.get_value(
				"Dynamic Link",
				{"link_doctype": "Customer", "parent": customer_name},
				"link_name",
			)

	if not customer_name:
		frappe.throw(_("Customer not found"))

	customer = frappe.get_doc("Customer", customer_name)

	# Get addresses
	addresses = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
		fields=["parent"],
	)

	address_list = []
	for addr in addresses:
		address_doc = frappe.get_doc("Address", addr.parent)
		address_list.append(address_doc.as_dict())

	# Get contacts
	contacts = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
		fields=["parent"],
	)

	contact_list = []
	for cont in contacts:
		contact_doc = frappe.get_doc("Contact", cont.parent)
		contact_list.append(contact_doc.as_dict())

	result = customer.as_dict()
	result["addresses"] = address_list
	result["contacts"] = contact_list

	return result


@frappe.whitelist(allow_guest=True)
def create_customer(
	customer_name,
	email=None,
	phone=None,
	customer_group="Individual",
	territory="All Territories",
	customer_type="Individual",
):
	"""
	Create a new customer or return existing customer

	Args:
		customer_name (str): Customer name
		email (str): Email address
		phone (str): Phone number
		customer_group (str): Customer group (default: Individual)
		territory (str): Territory (default: All Territories)
		customer_type (str): Customer type (default: Individual)

	Returns:
		dict: Created or existing customer details
	"""
	# Check if customer already exists - if so, return it
	if frappe.db.exists("Customer", customer_name):
		customer = frappe.get_doc("Customer", customer_name)
		return customer.as_dict()

	customer = frappe.new_doc("Customer")
	customer.customer_name = customer_name
	customer.customer_group = customer_group
	customer.territory = territory
	customer.customer_type = customer_type

	customer.insert(ignore_permissions=True)

	# Create contact if email or phone provided
	if email or phone:
		contact = frappe.new_doc("Contact")
		contact.first_name = customer_name
		if email:
			contact.append("email_ids", {"email_id": email, "is_primary": 1})
		if phone:
			contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})

		contact.append("links", {
			"link_doctype": "Customer",
			"link_name": customer.name,
		})

		contact.insert(ignore_permissions=True)

	return customer.as_dict()


@frappe.whitelist(allow_guest=True)
def update_customer(customer_name, **kwargs):
	"""
	Update customer details

	Args:
		customer_name (str): Customer name/ID
		**kwargs: Fields to update

	Returns:
		dict: Updated customer details
	"""
	if not frappe.db.exists("Customer", customer_name):
		frappe.throw(_("Customer {0} not found").format(customer_name))

	customer = frappe.get_doc("Customer", customer_name)

	# Update allowed fields
	allowed_fields = [
		"customer_name", "customer_group", "territory", "customer_type",
		"default_currency", "default_price_list", "default_sales_partner",
	]

	for field, value in kwargs.items():
		if field in allowed_fields:
			customer.set(field, value)

	customer.save(ignore_permissions=True)

	return customer.as_dict()


@frappe.whitelist(allow_guest=True)
def create_address(
	customer_name,
	address_line1,
	city,
	country="United States",
	address_type="Billing",
	address_line2=None,
	state=None,
	pincode=None,
	email=None,
	phone=None,
	is_primary=0,
	is_shipping=0,
):
	"""
	Create address for a customer or return existing matching address

	Args:
		customer_name (str): Customer name/ID
		address_line1 (str): Address line 1
		city (str): City
		country (str): Country (default: United States)
		address_type (str): Address type (Billing/Shipping/Office/etc.)
		address_line2 (str): Address line 2 (optional)
		state (str): State (optional)
		pincode (str): PIN/ZIP code (optional)
		email (str): Email for this address (optional)
		phone (str): Phone for this address (optional)
		is_primary (int): Mark as primary address (default: 0)
		is_shipping (int): Mark as shipping address (default: 0)

	Returns:
		dict: Created or existing address details
	"""
	if not frappe.db.exists("Customer", customer_name):
		frappe.throw(_("Customer {0} not found").format(customer_name))

	# Check if similar address already exists for this customer
	existing_address = frappe.db.sql("""
		SELECT addr.name
		FROM `tabAddress` addr
		INNER JOIN `tabDynamic Link` link ON link.parent = addr.name
		WHERE link.link_doctype = 'Customer'
		AND link.link_name = %s
		AND addr.address_line1 = %s
		AND addr.city = %s
		AND addr.country = %s
		LIMIT 1
	""", (customer_name, address_line1, city, country), as_dict=True)

	if existing_address:
		return frappe.get_doc("Address", existing_address[0].name).as_dict()

	address = frappe.new_doc("Address")
	address.address_line1 = address_line1
	address.address_line2 = address_line2
	address.city = city
	address.state = state
	address.pincode = pincode
	address.country = country
	address.address_type = address_type
	address.email_id = email
	address.phone = phone

	# Link to customer
	address.append("links", {
		"link_doctype": "Customer",
		"link_name": customer_name,
	})

	address.insert(ignore_permissions=True)

	# Set as primary/shipping if requested
	if is_primary:
		frappe.db.set_value("Customer", customer_name, "customer_primary_address", address.name)

	if is_shipping:
		frappe.db.set_value("Customer", customer_name, "customer_primary_contact", address.name)

	return address.as_dict()


# ========================================
# ORDER APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def create_order(
	customer,
	items,
	order_type="Shopping Cart",
	delivery_date=None,
	company=None,
	currency=None,
	price_list=None,
	shipping_address=None,
	billing_address=None,
	taxes=None,
	payment_terms=None,
	coupon_code=None,
):
	"""
	Create a sales order from e-commerce platform

	Args:
		customer (str): Customer name/ID
		items (list): List of items with item_code, qty, rate
		order_type (str): Order type (default: "Shopping Cart")
		delivery_date (str): Expected delivery date
		company (str): Company name
		currency (str): Transaction currency
		price_list (str): Price list to use
		shipping_address (str): Shipping address name
		billing_address (str): Billing address name
		taxes (list): List of tax charges
		payment_terms (str): Payment terms template
		coupon_code (str): Coupon/promo code

	Returns:
		dict: Created sales order details
	"""
	if isinstance(items, str):
		import json
		items = json.loads(items)

	if isinstance(taxes, str):
		import json
		taxes = json.loads(taxes) if taxes else None

	# Validate customer exists
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found").format(customer))

	# Get default company if not provided
	if not company:
		company = frappe.defaults.get_user_default("Company")
		if not company:
			# Get first available company if no default is set
			company = frappe.db.get_value("Company", {}, "name")

	# Create Sales Order
	so = frappe.new_doc("Sales Order")
	so.customer = customer
	so.order_type = order_type
	so.transaction_date = nowdate()
	so.delivery_date = delivery_date or add_days(nowdate(), 7)
	so.company = company

	if currency:
		so.currency = currency

	if price_list:
		so.selling_price_list = price_list

	if shipping_address:
		so.shipping_address_name = shipping_address

	if billing_address:
		so.customer_address = billing_address

	if coupon_code:
		so.coupon_code = coupon_code

	if payment_terms:
		so.payment_terms_template = payment_terms

	# Add items
	for item in items:
		item_code = item.get("item_code")
		qty = flt(item.get("qty", 1))
		rate = flt(item.get("rate", 0))

		# Get item details if rate not provided
		if not rate:
			item_details = get_item_details_base({
				"item_code": item_code,
				"customer": customer,
				"company": company,
				"selling_price_list": price_list,
				"doctype": "Sales Order",
			})
			rate = item_details.get("price_list_rate", 0)

		so.append("items", {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"delivery_date": so.delivery_date,
		})

	# Add taxes if provided
	if taxes:
		for tax in taxes:
			so.append("taxes", tax)

	# Calculate totals
	so.run_method("calculate_taxes_and_totals")

	# Save and submit
	so.insert(ignore_permissions=True)

	# Auto-submit if configured
	# so.submit()

	return so.as_dict()


@frappe.whitelist(allow_guest=True)
def get_order(order_name):
	"""
	Get sales order details

	Args:
		order_name (str): Sales order name/ID

	Returns:
		dict: Complete sales order details
	"""
	if not frappe.db.exists("Sales Order", order_name):
		frappe.throw(_("Sales Order {0} not found").format(order_name))

	so = frappe.get_doc("Sales Order", order_name)
	return so.as_dict()


@frappe.whitelist(allow_guest=True)
def update_order_status(order_name, status):
	"""
	Update sales order status

	Args:
		order_name (str): Sales order name/ID
		status (str): New status (Draft, To Deliver and Bill, Completed, Cancelled, Closed)

	Returns:
		dict: Updated sales order
	"""
	if not frappe.db.exists("Sales Order", order_name):
		frappe.throw(_("Sales Order {0} not found").format(order_name))

	so = frappe.get_doc("Sales Order", order_name)

	if status == "Cancelled" and so.docstatus == 1:
		so.cancel()
	elif status == "Closed":
		so.update_status("Closed")
	elif status == "Completed":
		so.update_status("Completed")

	so.reload()
	return so.as_dict()


# ========================================
# GUEST PREORDER (S019)
# ========================================

# ERPNext Sales Order `order_type` only allows a small set (e.g. Sales, Shopping Cart).
# We tag guest catalog consultations in `remarks` or `terms` (some sites have no `remarks` DB column).
GUEST_PREORDER_REMARKS_TAG = "guest_preorder=1"


def _sales_order_table_columns():
	return set(frappe.db.get_table_columns("Sales Order") or [])


def _guest_preorder_tag_fieldname():
	"""DB column to store/search the guest-preorder tag (remarks preferred, else terms)."""
	cols = _sales_order_table_columns()
	if "remarks" in cols:
		return "remarks"
	if "terms" in cols:
		return "terms"
	return None


def _is_guest_preorder_sales_order(so):
	"""True if this SO was created by create_guest_preorder (tag in remarks or terms)."""
	tag = GUEST_PREORDER_REMARKS_TAG
	if isinstance(so, dict):
		for key in ("remarks", "terms"):
			val = so.get(key) or ""
			if tag in str(val):
				return True
		return False
	for fn in ("remarks", "terms"):
		if hasattr(so, fn):
			val = getattr(so, fn, None) or ""
			if tag in str(val):
				return True
	return False


@frappe.whitelist(allow_guest=True)
def create_guest_preorder(
	items,
	guest_phone=None,
	guest_name=None,
	price_list="Standard Selling",
	delivery_date=None,
	order_type="Sales",
	company=None,
):
	"""
	Create a draft Sales Order to represent a guest preorder (no payment).

	This is intentionally implemented as a normal Sales Order left in Draft
	docstatus so the owner can later confirm/prepare it.

	Uses a valid ERPNext ``order_type`` (default ``Sales``). The flow is identified
	via ``remarks`` containing ``guest_preorder=1``.

	Returns: { preorder_name, estimated_total, currency, status }
	"""
	if isinstance(items, str):
		import json
		items = json.loads(items)

	if not items:
		frappe.throw(_("Cart is empty"))

	# Resolve defaults
	if not company:
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("No Company configured"))

	# Use a safe default customer for guest orders (Walk-in Customer or first Customer).
	customer = frappe.db.get_value("Customer", {"customer_name": "Walk-in Customer"}, "name") \
		or frappe.db.get_value("Customer", {}, "name") \
		or "_Test Customer"

	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found").format(customer))

	# Create Sales Order in Draft (do NOT submit)
	so = frappe.new_doc("Sales Order")
	so.customer = customer
	# Must be one of the site's allowed Sales Order order types (commonly Sales, Shopping Cart, …).
	so.order_type = order_type or "Sales"
	so.transaction_date = nowdate()
	so.delivery_date = delivery_date or add_days(nowdate(), 7)
	so.company = company
	so.selling_price_list = price_list

	remarks_parts = [GUEST_PREORDER_REMARKS_TAG, f"customer:{customer}"]
	if guest_phone:
		remarks_parts.append(f"guest_phone:{guest_phone}")
	if guest_name:
		remarks_parts.append(f"guest_name:{guest_name}")
	tag_text = " | ".join(remarks_parts)
	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		frappe.throw(
			_("Sales Order has no suitable text field (remarks/terms) to store guest preorder tag")
		)
	if tag_fn == "remarks":
		so.remarks = tag_text
	else:
		so.terms = tag_text

	# Add items
	for item in items:
		item_code = item.get("item_code")
		qty = flt(item.get("qty", 1))
		rate = flt(item.get("rate", 0))

		if not item_code:
			continue

		# Get item rate if not provided or zero (mirrors create_order logic)
		if not rate:
			item_details = get_item_details_base({
				"item_code": item_code,
				"customer": customer,
				"company": company,
				"selling_price_list": price_list,
				"doctype": "Sales Order",
			})
			rate = item_details.get("price_list_rate", 0)

		so.append("items", {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"delivery_date": so.delivery_date,
		})

	# Calculate totals
	so.run_method("calculate_taxes_and_totals")

	# Save as Draft
	so.insert(ignore_permissions=True)

	return {
		"preorder_name": so.name,
		"estimated_total": flt(so.grand_total),
		"currency": so.currency,
		"status": so.status,
	}


@frappe.whitelist()
def get_guest_preorders_list(status=None, start=0, page_length=20):
	"""
	List Guest Preorders created by `create_guest_preorder`.

	Cancelled orders that have been superseded by an amended version are excluded.
	Only user-archived orders (no successor) and active orders are shown.
	"""
	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return {"preorders": [], "total_count": 0}

	filters = {tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"]}

	if status:
		if str(status).lower() == "draft":
			filters["docstatus"] = 0
		elif str(status).lower() in ("submitted", "confirmed"):
			filters["docstatus"] = 1
		else:
			filters["status"] = status

	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=[
			"name",
			"transaction_date",
			"delivery_date",
			"grand_total",
			"currency",
			"docstatus",
			"status",
			"amended_from",
		],
		start=start,
		limit_page_length=int(page_length) + 50,  # fetch extra to account for filtering
		order_by="transaction_date desc, creation desc",
	)

	# Find cancelled orders that have been replaced by an amendment
	superseded = set()
	for o in orders:
		if o.get("amended_from"):
			superseded.add(o["amended_from"])

	# Also check globally for superseded orders not in the current page
	if orders:
		order_names = [o["name"] for o in orders if o.get("docstatus") == 2]
		if order_names:
			amenders = frappe.get_all(
				"Sales Order",
				filters={"amended_from": ["in", order_names]},
				fields=["amended_from"],
			)
			for a in amenders:
				superseded.add(a["amended_from"])

	# Filter out superseded cancelled orders
	filtered = [o for o in orders if not (o.get("docstatus") == 2 and o["name"] in superseded)]
	total_count = len(filtered)
	filtered = filtered[:int(page_length)]

	order_names = [o["name"] for o in filtered]
	items_count_map = {}
	if order_names:
		rows = frappe.db.sql(
			"""
			SELECT parent, COUNT(*) as items_count
			FROM `tabSales Order Item`
			WHERE parent IN ({placeholders})
			GROUP BY parent
			""".format(placeholders=", ".join(["%s"] * len(order_names))),
			tuple(order_names),
			as_dict=True,
		)
		for r in rows or []:
			items_count_map[r.parent] = r.items_count

	for o in filtered:
		o["items_count"] = items_count_map.get(o["name"], 0)
		# Compute display status from docstatus + status
		ds = o.get("docstatus", 0)
		st = o.get("status", "")
		if ds == 0:
			o["display_status"] = "Consulta"
		elif ds == 2:
			o["display_status"] = "Archivado"
		elif st in ("Preparado", "En Delivery"):
			o["display_status"] = st
		elif st == "Completed":
			o["display_status"] = "Completado"
		else:
			o["display_status"] = "Orden"

	return {"preorders": filtered, "total_count": total_count}


@frappe.whitelist()
def get_guest_preorder(preorder_name):
	"""
	Get one guest preorder (Sales Order doc).
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	return {
		"name": so.name,
		"order_type": so.order_type,
		"customer": so.customer,
		"transaction_date": so.transaction_date,
		"delivery_date": so.delivery_date,
		"docstatus": so.docstatus,
		"status": so.status,
		"display_status": _display_status(so),
		"estimated_total": flt(so.grand_total),
		"currency": so.currency,
		"remarks": getattr(so, "remarks", None),
		"terms": getattr(so, "terms", None),
		"additional_discount_amount": flt(getattr(so, "additional_discount_amount", 0)),
		"advance_paid": flt(getattr(so, "advance_paid", 0)),
		"amended_from": so.amended_from or None,
		"items": [
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": flt(d.qty),
				"rate": flt(d.rate),
				"amount": flt(d.amount),
				"discount_percentage": flt(getattr(d, "discount_percentage", 0)),
			}
			for d in (so.items or [])
		],
	}


@frappe.whitelist()
def get_guest_preorder_history(preorder_name):
	"""
	Return the full version chain for a guest preorder.

	Walks the amended_from chain backward to find the original,
	then walks forward to collect all versions in chronological order.
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	# Walk backward to find the root
	root = preorder_name
	visited = {root}
	while True:
		amended_from = frappe.db.get_value("Sales Order", root, "amended_from")
		if not amended_from or amended_from in visited:
			break
		visited.add(amended_from)
		root = amended_from

	# Walk forward from root collecting all versions
	versions = []
	current = root
	forward_visited = {root}
	while current:
		so_data = frappe.db.get_value(
			"Sales Order",
			current,
			["name", "transaction_date", "grand_total", "currency", "docstatus", "status", "creation"],
			as_dict=True,
		)
		if not so_data:
			break
		items_count = frappe.db.count("Sales Order Item", {"parent": current})
		versions.append({
			"name": so_data.name,
			"transaction_date": so_data.transaction_date,
			"estimated_total": flt(so_data.grand_total),
			"currency": so_data.currency,
			"docstatus": so_data.docstatus,
			"status": so_data.status,
			"creation": so_data.creation,
			"items_count": items_count,
		})
		# Find the next version (the one that has amended_from = current)
		next_version = frappe.db.get_value(
			"Sales Order", {"amended_from": current}, "name"
		)
		if next_version and next_version not in forward_visited:
			forward_visited.add(next_version)
			current = next_version
		else:
			break

	return {
		"current": preorder_name,
		"versions": versions,
	}


# ── Custom workflow status helpers ────────────────────────────────────────────
# Display statuses: Consulta → Orden → Preparado → En Delivery → Completado
# Mapping to ERPNext:
#   Consulta   = docstatus 0 (Draft)
#   Orden      = docstatus 1, status "To Deliver and Bill"
#   Preparado  = docstatus 1, status "Preparado"   (custom via db_set)
#   En Delivery= docstatus 1, status "En Delivery"  (custom via db_set)
#   Completado = docstatus 1, status "Completed"
#   Archivado  = docstatus 2 (Cancelled)

WORKFLOW_STATUSES = ["Consulta", "Orden", "Preparado", "En Delivery", "Completado"]

def _display_status(so):
	"""Return the user-facing workflow status for a Sales Order."""
	if so.docstatus == 0:
		return "Consulta"
	if so.docstatus == 2:
		return "Archivado"
	# docstatus == 1
	s = so.status
	if s in ("Preparado", "En Delivery"):
		return s
	if s == "Completed":
		return "Completado"
	# "To Deliver and Bill", "To Deliver", "To Bill", etc.
	return "Orden"


def _erp_status_for_display(display_status):
	"""Map display status → ERPNext status string."""
	return {
		"Orden": "To Deliver and Bill",
		"Preparado": "Preparado",
		"En Delivery": "En Delivery",
		"Completado": "Completed",
	}.get(display_status)


@frappe.whitelist()
def set_guest_preorder_status(preorder_name, target_status):
	"""
	Unified status transition for the custom workflow.

	Accepts target_status as one of: Consulta, Orden, Preparado, En Delivery, Completado.
	Consulta reverts a submitted order back to Draft.
	"""
	if target_status not in WORKFLOW_STATUSES:
		frappe.throw(_("Invalid target status: {0}").format(target_status))

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus == 2:
		frappe.throw(_("Cannot change status of a cancelled order"))

	# Revert to Consulta (Draft)
	if target_status == "Consulta":
		if so.docstatus == 1:
			so.flags.ignore_permissions = True
			so.cancel()
			# Create a new draft copy
			new_so = frappe.copy_doc(so)
			new_so.amended_from = so.name
			new_so.docstatus = 0
			new_so.insert(ignore_permissions=True)
			frappe.db.commit()
			return get_guest_preorder(new_so.name)
		# Already draft
		return get_guest_preorder(preorder_name)

	# Submit draft if needed for forward transitions
	if so.docstatus == 0:
		so.submit()
		so.reload()

	erp_status = _erp_status_for_display(target_status)
	if not erp_status:
		frappe.throw(_("Invalid target status"))

	# For standard ERPNext statuses, use update_status; for custom ones, db_set
	if erp_status in ("To Deliver and Bill", "Completed"):
		so.update_status(erp_status)
	else:
		so.db_set("status", erp_status)

	so.reload()
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def confirm_guest_preorder(preorder_name):
	"""
	Confirm a guest preorder.

	Best-effort workflow:
	- submit if it is still a Draft
	- set status to "To Deliver and Bill"
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus == 0:
		so.submit()

	so.update_status("To Deliver and Bill")
	so.reload()
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def mark_prepared_guest_preorder(preorder_name):
	"""
	Mark a guest preorder as prepared/completed (best-effort).
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus == 0:
		so.submit()

	so.update_status("Completed")
	so.reload()
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def unmark_prepared_guest_preorder(preorder_name):
	"""Toggle a Completed preorder back to To Deliver and Bill."""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus == 1:
		so.db_set("status", "To Deliver and Bill")
	so.reload()
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def cancel_guest_preorder(preorder_name):
	"""Cancel (archive) a guest preorder. Works on both draft and submitted orders."""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus == 2:
		frappe.throw(_("Order is already cancelled"))

	so.flags.ignore_permissions = True
	if so.docstatus == 1:
		so.cancel()
	else:
		so.docstatus = 2
		so.save()
	so.reload()
	return {"ok": True, "name": preorder_name, "status": "Cancelled"}


@frappe.whitelist()
def update_guest_preorder_items(preorder_name, items, additional_discount_amount=0):
	"""
	Full item replacement on a guest preorder.

	For draft orders (docstatus=0): edits in place.
	For submitted orders (docstatus=1): amends (cancel old, create amended copy, submit).

	Returns the updated preorder detail (may have a new name if amended).
	"""
	import json as _json

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus == 2:
		frappe.throw(_("Cannot edit a cancelled order"))

	if isinstance(items, str):
		items = _json.loads(items)

	if not items:
		frappe.throw(_("Items list cannot be empty"))

	if so.docstatus == 1:
		# Amend: cancel original, create amended copy with changes, submit
		amended = frappe.copy_doc(so)
		amended.amended_from = so.name
		amended.docstatus = 0
		so.flags.ignore_permissions = True
		so.cancel()
		_apply_item_changes(amended, items, additional_discount_amount)
		amended.insert(ignore_permissions=True)
		amended.submit()
		amended.reload()
		return get_guest_preorder(amended.name)

	# Draft: edit in place
	_apply_item_changes(so, items, additional_discount_amount)
	so.save(ignore_permissions=True)
	so.reload()
	return get_guest_preorder(preorder_name)


def _apply_item_changes(so, items, additional_discount_amount):
	"""Apply item list changes to a Sales Order document (not yet saved)."""
	new_item_map = {i["item_code"]: i for i in items}

	# Remove rows not in the new list
	so.items = [row for row in so.items if row.item_code in new_item_map]

	# Update existing rows
	existing_codes = {row.item_code for row in so.items}
	for row in so.items:
		override = new_item_map[row.item_code]
		row.rate = flt(override.get("rate", row.rate))
		row.qty = flt(override.get("qty", row.qty))
		row.discount_percentage = flt(override.get("discount_percentage", 0))
		row.amount = row.rate * row.qty

	# Add new items
	for item in items:
		if item["item_code"] not in existing_codes:
			so.append("items", {
				"item_code": item["item_code"],
				"qty": flt(item.get("qty", 1)),
				"rate": flt(item.get("rate", 0)),
				"discount_percentage": flt(item.get("discount_percentage", 0)),
				"delivery_date": so.delivery_date,
			})

	so.apply_discount_on = "Grand Total"
	so.additional_discount_amount = flt(additional_discount_amount)
	so.run_method("calculate_taxes_and_totals")


@frappe.whitelist()
def update_guest_preorder_prices(preorder_name, items, additional_discount_amount=0):
	"""Update item rates/qty and global discount on a draft preorder (docstatus=0 only)."""
	import json as _json

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus != 0:
		frappe.throw(_("Price editing is only allowed on draft orders (before confirming)"))

	if isinstance(items, str):
		items = _json.loads(items)

	item_map = {i["item_code"]: i for i in items}

	for row in so.items:
		if row.item_code in item_map:
			override = item_map[row.item_code]
			row.rate = flt(override.get("rate", row.rate))
			row.qty = flt(override.get("qty", row.qty))
			row.discount_percentage = flt(override.get("discount_percentage", 0))
			row.amount = row.rate * row.qty

	so.apply_discount_on = "Grand Total"
	so.additional_discount_amount = flt(additional_discount_amount)
	so.run_method("calculate_taxes_and_totals")
	so.save(ignore_permissions=True)
	so.reload()
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def record_preorder_payment(preorder_name, paid_amount, mode_of_payment="Efectivo"):
	"""Create a Payment Entry for a confirmed preorder."""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))

	if so.docstatus != 1:
		frappe.throw(_("Payment can only be recorded on submitted (confirmed) orders"))

	paid_amount = flt(paid_amount)
	if paid_amount <= 0:
		frappe.throw(_("Paid amount must be greater than zero"))

	company = so.company

	receivable_account = frappe.get_value("Company", company, "default_receivable_account")
	cash_account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode_of_payment, "company": company},
		"default_account",
	)
	if not cash_account:
		cash_account = frappe.db.get_value(
			"Account",
			{"account_type": "Cash", "company": company, "is_group": 0},
			"name",
		)

	if not receivable_account or not cash_account:
		frappe.throw(_("Could not find debit/credit accounts for payment. Check company defaults."))

	outstanding = flt(so.grand_total) - flt(getattr(so, "advance_paid", 0))
	allocated = min(paid_amount, outstanding) if outstanding > 0 else paid_amount

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = company
	pe.party_type = "Customer"
	pe.party = so.customer
	pe.paid_from = receivable_account
	pe.paid_to = cash_account
	pe.paid_from_account_currency = so.currency
	pe.paid_to_account_currency = so.currency
	pe.paid_amount = paid_amount
	pe.received_amount = paid_amount
	pe.reference_date = nowdate()
	pe.reference_no = preorder_name
	if allocated > 0:
		pe.append("references", {
			"reference_doctype": "Sales Order",
			"reference_name": preorder_name,
			"total_amount": flt(so.grand_total),
			"outstanding_amount": outstanding,
			"allocated_amount": allocated,
		})
	pe.insert(ignore_permissions=True)
	pe.submit()
	return get_guest_preorder(preorder_name)



	"""
	Get all orders for a customer

	Args:
		customer (str): Customer name/ID
		start (int): Pagination offset
		page_length (int): Records per page

	Returns:
		dict: List of orders with pagination info
	"""
	orders = frappe.get_all(
		"Sales Order",
		filters={"customer": customer},
		fields=[
			"name",
			"transaction_date",
			"delivery_date",
			"status",
			"grand_total",
			"currency",
			"order_type",
		],
		start=start,
		limit_page_length=page_length,
		order_by="transaction_date desc",
	)

	total_count = frappe.db.count("Sales Order", {"customer": customer})

	return {
		"orders": orders,
		"total_count": total_count,
		"has_more": (start + page_length) < total_count,
	}


# ========================================
# PAYMENT APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def create_payment(
	payment_type,
	party,
	amount,
	payment_method="Cash",
	reference_no=None,
	reference_date=None,
	reference_doctype=None,
	reference_name=None,
	company=None,
):
	"""
	Create a payment entry

	Args:
		payment_type (str): "Receive" or "Pay"
		party (str): Customer or Supplier name
		amount (float): Payment amount
		payment_method (str): Mode of payment (default: Cash)
		reference_no (str): External payment reference
		reference_date (str): Payment date
		reference_doctype (str): Reference document type (Sales Order, Sales Invoice, etc.)
		reference_name (str): Reference document name
		company (str): Company name

	Returns:
		dict: Created payment entry
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if not company:
		company = frappe.defaults.get_user_default("Company")

	# If reference provided, use get_payment_entry
	if reference_doctype and reference_name:
		pe = get_payment_entry(reference_doctype, reference_name)
		pe.paid_amount = flt(amount)
		pe.received_amount = flt(amount)
	else:
		# Create manual payment entry
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = payment_type
		pe.party_type = "Customer" if payment_type == "Receive" else "Supplier"
		pe.party = party
		pe.company = company
		pe.paid_amount = flt(amount)
		pe.received_amount = flt(amount)

	if payment_method:
		pe.mode_of_payment = payment_method

	if reference_no:
		pe.reference_no = reference_no

	if reference_date:
		pe.reference_date = getdate(reference_date)
	else:
		pe.reference_date = nowdate()

	pe.posting_date = nowdate()

	pe.insert(ignore_permissions=True)
	pe.submit()

	return pe.as_dict()


@frappe.whitelist(allow_guest=True)
def get_payment_methods():
	"""
	Get all available payment methods

	Returns:
		list: List of mode of payment options
	"""
	return frappe.get_all(
		"Mode of Payment",
		filters={"enabled": 1},
		fields=["name", "mode_of_payment", "type"],
	)


# ========================================
# COUPON / PRICING RULE APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def validate_coupon(coupon_code, customer=None, items=None):
	"""
	Validate and get coupon code details

	Args:
		coupon_code (str): Coupon code to validate
		customer (str): Customer name (optional)
		items (list): List of items to apply coupon on (optional)

	Returns:
		dict: Coupon validity and discount details
	"""
	if not frappe.db.exists("Coupon Code", coupon_code):
		return {
			"valid": False,
			"message": _("Invalid coupon code"),
		}

	coupon = frappe.get_doc("Coupon Code", coupon_code)

	# Check if expired
	if coupon.valid_upto and getdate(coupon.valid_upto) < getdate(nowdate()):
		return {
			"valid": False,
			"message": _("Coupon code has expired"),
		}

	# Check if not yet valid
	if coupon.valid_from and getdate(coupon.valid_from) > getdate(nowdate()):
		return {
			"valid": False,
			"message": _("Coupon code is not yet valid"),
		}

	# Check maximum uses
	if coupon.maximum_use and coupon.used >= coupon.maximum_use:
		return {
			"valid": False,
			"message": _("Coupon code has reached maximum usage limit"),
		}

	# Check customer-specific
	if coupon.customer and customer and coupon.customer != customer:
		return {
			"valid": False,
			"message": _("This coupon is not valid for this customer"),
		}

	# Get pricing rule
	pricing_rule = frappe.get_doc("Pricing Rule", coupon.pricing_rule)

	return {
		"valid": True,
		"coupon_code": coupon_code,
		"pricing_rule": pricing_rule.name,
		"discount_percentage": pricing_rule.discount_percentage,
		"discount_amount": pricing_rule.discount_amount,
		"message": _("Coupon code is valid"),
	}


@frappe.whitelist(allow_guest=True)
def apply_coupon_to_order(order_name, coupon_code):
	"""
	Apply coupon code to an existing order

	Args:
		order_name (str): Sales order name
		coupon_code (str): Coupon code

	Returns:
		dict: Updated order with discount applied
	"""
	if not frappe.db.exists("Sales Order", order_name):
		frappe.throw(_("Sales Order {0} not found").format(order_name))

	# Validate coupon
	so = frappe.get_doc("Sales Order", order_name)
	coupon_validation = validate_coupon(coupon_code, so.customer)

	if not coupon_validation.get("valid"):
		frappe.throw(coupon_validation.get("message"))

	# Apply coupon
	so.coupon_code = coupon_code
	so.run_method("calculate_taxes_and_totals")
	so.save(ignore_permissions=True)

	return so.as_dict()


# ========================================
# SHIPPING / DELIVERY APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def create_delivery_note(sales_order):
	"""
	Create delivery note from sales order

	Args:
		sales_order (str): Sales order name

	Returns:
		dict: Created delivery note
	"""
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	if not frappe.db.exists("Sales Order", sales_order):
		frappe.throw(_("Sales Order {0} not found").format(sales_order))

	dn = make_delivery_note(sales_order)
	dn.insert(ignore_permissions=True)

	return dn.as_dict()


@frappe.whitelist(allow_guest=True)
def update_tracking_info(delivery_note, tracking_number, carrier=None):
	"""
	Update tracking information for delivery note

	Args:
		delivery_note (str): Delivery note name
		tracking_number (str): Tracking number
		carrier (str): Carrier name (optional)

	Returns:
		dict: Updated delivery note
	"""
	if not frappe.db.exists("Delivery Note", delivery_note):
		frappe.throw(_("Delivery Note {0} not found").format(delivery_note))

	dn = frappe.get_doc("Delivery Note", delivery_note)
	dn.lr_no = tracking_number  # LR No field is commonly used for tracking

	if carrier:
		dn.transporter_name = carrier

	dn.save(ignore_permissions=True)

	return dn.as_dict()


# ========================================
# INVOICE APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def create_invoice(sales_order=None, delivery_note=None):
	"""
	Create sales invoice from sales order or delivery note

	Args:
		sales_order (str): Sales order name
		delivery_note (str): Delivery note name

	Returns:
		dict: Created sales invoice
	"""
	if sales_order:
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		if not frappe.db.exists("Sales Order", sales_order):
			frappe.throw(_("Sales Order {0} not found").format(sales_order))

		si = make_sales_invoice(sales_order)

	elif delivery_note:
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		if not frappe.db.exists("Delivery Note", delivery_note):
			frappe.throw(_("Delivery Note {0} not found").format(delivery_note))

		si = make_sales_invoice(delivery_note)

	else:
		frappe.throw(_("Either sales_order or delivery_note is required"))

	si.insert(ignore_permissions=True)

	return si.as_dict()


@frappe.whitelist(allow_guest=True)
def get_invoice(invoice_name):
	"""
	Get sales invoice details

	Args:
		invoice_name (str): Sales invoice name

	Returns:
		dict: Complete invoice details
	"""
	if not frappe.db.exists("Sales Invoice", invoice_name):
		frappe.throw(_("Sales Invoice {0} not found").format(invoice_name))

	si = frappe.get_doc("Sales Invoice", invoice_name)
	return si.as_dict()


# ========================================
# UTILITY APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def get_item_groups():
	"""
	Get all item groups in hierarchical structure

	Returns:
		list: Item groups with parent-child relationships
	"""
	return frappe.get_all(
		"Item Group",
		fields=["name", "parent_item_group", "is_group", "image"],
		order_by="name",
	)


@frappe.whitelist(allow_guest=True)
def get_price_lists():
	"""
	Get all price lists

	Returns:
		list: Available price lists
	"""
	return frappe.get_all(
		"Price List",
		filters={"enabled": 1, "selling": 1},
		fields=["name", "currency", "price_not_uom_dependent"],
	)


@frappe.whitelist(allow_guest=True)
def get_warehouses():
	"""
	Get all warehouses

	Returns:
		list: Available warehouses
	"""
	return frappe.get_all(
		"Warehouse",
		filters={"disabled": 0},
		fields=["name", "warehouse_name", "parent_warehouse", "company"],
	)


@frappe.whitelist(allow_guest=True)
def get_companies():
	"""
	Get all companies

	Returns:
		list: Available companies
	"""
	return frappe.get_all(
		"Company",
		fields=["name", "company_name", "default_currency", "country"],
	)


@frappe.whitelist(allow_guest=True)
def search_items(search_term, price_list=None, limit=20):
	"""
	Quick search for items by name, code, or description

	Args:
		search_term (str): Search query
		price_list (str): Price list to include pricing
		limit (int): Maximum results (default: 20)

	Returns:
		list: Matching items
	"""
	filters = [
		["disabled", "=", 0],
		["item_name", "like", f"%{search_term}%"]
	]

	items = frappe.get_all(
		"Item",
		or_filters=[
			["item_code", "like", f"%{search_term}%"],
			["item_name", "like", f"%{search_term}%"],
			["description", "like", f"%{search_term}%"],
		],
		filters={"disabled": 0},
		fields=["name", "item_code", "item_name", "description", "image", "standard_rate"],
		limit=limit,
	)

	# Add pricing if price_list provided
	if price_list:
		for item in items:
			item["price_list_rate"] = get_item_price(item.item_code, price_list)

	return items


@frappe.whitelist(allow_guest=True)
def get_tax_rates(country=None, state=None):
	"""
	Get applicable tax rates

	Args:
		country (str): Country code
		state (str): State/Province

	Returns:
		list: Applicable tax templates
	"""
	filters = {}

	if country:
		filters["country"] = country

	taxes = frappe.get_all(
		"Sales Taxes and Charges Template",
		filters=filters,
		fields=["name", "title", "company"],
	)

	# Get tax details for each template
	for tax in taxes:
		tax["taxes"] = frappe.get_all(
			"Sales Taxes and Charges",
			filters={"parent": tax.name},
			fields=["charge_type", "account_head", "rate", "description"],
		)

	return taxes


# ========================================
# SHOPPING CART APIs
# ========================================

@frappe.whitelist(allow_guest=True)
def get_cart(session_id=None, customer=None):
	"""
	Get shopping cart for a session or customer

	Args:
		session_id (str): Anonymous session ID
		customer (str): Customer name for logged-in users

	Returns:
		dict: Cart items, totals, and metadata
	"""
	if not session_id and not customer:
		frappe.throw(_("Either session_id or customer is required"))

	cart = None

	# Try to get existing cart - prioritize session_id if provided
	if session_id:
		cart = frappe.db.get_value(
			"Shopping Cart",
			{"session_id": session_id, "cart_type": "Session"},
			["name", "session_id", "customer", "price_list", "total_qty", "total_amount"],
			as_dict=True
		)

	# If no session cart found and customer provided, try customer cart
	if not cart and customer:
		cart = frappe.db.get_value(
			"Shopping Cart",
			{"customer": customer, "cart_type": "Customer"},
			["name", "session_id", "customer", "price_list", "total_qty", "total_amount"],
			as_dict=True
		)

	if not cart:
		# Return empty cart
		return {
			"name": None,
			"session_id": session_id,
			"customer": customer,
			"items": [],
			"total_qty": 0,
			"total_amount": 0,
			"price_list": "Standard Selling"
		}

	# Get cart items
	items = frappe.get_all(
		"Shopping Cart Item",
		filters={"parent": cart.name},
		fields=["item_code", "item_name", "qty", "rate", "amount", "image"],
		order_by="idx"
	)

	cart["items"] = items
	return cart


@frappe.whitelist(allow_guest=True)
def add_to_cart(item_code, qty=1, session_id=None, customer=None, price_list="Standard Selling"):
	"""
	Add item to shopping cart

	Args:
		item_code (str): Item code
		qty (float): Quantity to add (default: 1)
		session_id (str): Anonymous session ID
		customer (str): Customer name for logged-in users
		price_list (str): Price list to use (default: Standard Selling)

	Returns:
		dict: Updated cart
	"""
	if not session_id and not customer:
		frappe.throw(_("Either session_id or customer is required"))

	# Validate item exists
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found").format(item_code))

	# Get or create cart
	cart = _get_or_create_cart(session_id, customer, price_list)

	# Get item details
	item = frappe.get_doc("Item", item_code)
	rate = get_item_price(item_code, price_list) or item.standard_rate

	# Check if item already in cart
	existing_item = frappe.db.get_value(
		"Shopping Cart Item",
		{"parent": cart.name, "item_code": item_code},
		["name", "qty"],
		as_dict=True
	)

	if existing_item:
		# Update quantity
		new_qty = flt(existing_item.qty) + flt(qty)
		frappe.db.set_value("Shopping Cart Item", existing_item.name, {
			"qty": new_qty,
			"amount": new_qty * flt(rate)
		})
	else:
		# Add new item
		cart_item = frappe.get_doc({
			"doctype": "Shopping Cart Item",
			"parent": cart.name,
			"parenttype": "Shopping Cart",
			"parentfield": "items",
			"item_code": item_code,
			"item_name": item.item_name,
			"qty": flt(qty),
			"rate": flt(rate),
			"amount": flt(qty) * flt(rate),
			"image": item.image
		})
		cart_item.insert(ignore_permissions=True)

	# Update cart totals
	_update_cart_totals(cart.name)

	return get_cart(session_id, customer)


@frappe.whitelist(allow_guest=True)
def update_cart_item(item_code, qty, session_id=None, customer=None):
	"""
	Update quantity of item in cart

	Args:
		item_code (str): Item code
		qty (float): New quantity
		session_id (str): Anonymous session ID
		customer (str): Customer name

	Returns:
		dict: Updated cart
	"""
	if not session_id and not customer:
		frappe.throw(_("Either session_id or customer is required"))

	# Get cart
	filters = {}
	if customer:
		filters["customer"] = customer
		filters["cart_type"] = "Customer"
	else:
		filters["session_id"] = session_id
		filters["cart_type"] = "Session"

	cart_name = frappe.db.get_value("Shopping Cart", filters, "name")

	if not cart_name:
		frappe.throw(_("Cart not found"))

	# Find cart item
	cart_item = frappe.db.get_value(
		"Shopping Cart Item",
		{"parent": cart_name, "item_code": item_code},
		["name", "rate"],
		as_dict=True
	)

	if not cart_item:
		frappe.throw(_("Item not found in cart"))

	if flt(qty) <= 0:
		# Remove item using direct DB delete to avoid document-level locking on the parent cart
		frappe.db.delete("Shopping Cart Item", {"name": cart_item.name})
	else:
		# Update quantity
		frappe.db.set_value("Shopping Cart Item", cart_item.name, {
			"qty": flt(qty),
			"amount": flt(qty) * flt(cart_item.rate)
		})

	# Update cart totals
	_update_cart_totals(cart_name)

	return get_cart(session_id, customer)


@frappe.whitelist(allow_guest=True)
def remove_from_cart(item_code, session_id=None, customer=None):
	"""
	Remove item from cart

	Args:
		item_code (str): Item code
		session_id (str): Anonymous session ID
		customer (str): Customer name

	Returns:
		dict: Updated cart
	"""
	return update_cart_item(item_code, 0, session_id, customer)


@frappe.whitelist(allow_guest=True)
def clear_cart(session_id=None, customer=None):
	"""
	Clear all items from cart

	Args:
		session_id (str): Anonymous session ID
		customer (str): Customer name

	Returns:
		dict: Empty cart
	"""
	if not session_id and not customer:
		frappe.throw(_("Either session_id or customer is required"))

	cart_name = None

	# Try to find cart - prioritize session_id if provided
	if session_id:
		cart_name = frappe.db.get_value("Shopping Cart",
			{"session_id": session_id, "cart_type": "Session"},
			"name"
		)

	# If no session cart found and customer provided, try customer cart
	if not cart_name and customer:
		cart_name = frappe.db.get_value("Shopping Cart",
			{"customer": customer, "cart_type": "Customer"},
			"name"
		)

	if cart_name:
		# Delete all items
		frappe.db.delete("Shopping Cart Item", {"parent": cart_name})

		# Update totals
		frappe.db.set_value("Shopping Cart", cart_name, {
			"total_qty": 0,
			"total_amount": 0
		})

	return get_cart(session_id, customer)


@frappe.whitelist(allow_guest=True)
def checkout_cart(session_id=None, customer=None, shipping_address=None, billing_address=None):
	"""
	Convert shopping cart to sales order

	Args:
		session_id (str): Anonymous session ID
		customer (str): Customer name
		shipping_address (str): Shipping address name
		billing_address (str): Billing address name

	Returns:
		dict: Created sales order
	"""
	if not customer:
		frappe.throw(_("Customer is required for checkout"))

	# Get cart
	cart = get_cart(session_id, customer)

	if not cart.get("items") or len(cart["items"]) == 0:
		frappe.throw(_("Cart is empty"))

	# Create sales order
	items = [
		{
			"item_code": item["item_code"],
			"qty": item["qty"],
			"rate": item["rate"]
		}
		for item in cart["items"]
	]

	order = create_order(
		customer=customer,
		items=items,
		order_type="Shopping Cart",
		price_list=cart.get("price_list", "Standard Selling"),
		shipping_address=shipping_address,
		billing_address=billing_address
	)

	# Clear cart after successful checkout
	clear_cart(session_id, customer)

	return order


def _get_or_create_cart(session_id, customer, price_list):
	"""
	Internal function to get or create shopping cart

	Args:
		session_id (str): Session ID
		customer (str): Customer name
		price_list (str): Price list

	Returns:
		Document: Shopping Cart document
	"""
	filters = {}
	cart_type = "Customer" if customer else "Session"

	if customer:
		filters["customer"] = customer
		filters["cart_type"] = "Customer"
	else:
		filters["session_id"] = session_id
		filters["cart_type"] = "Session"

	cart_name = frappe.db.get_value("Shopping Cart", filters, "name")

	if cart_name:
		return frappe.get_doc("Shopping Cart", cart_name)

	# Create new cart
	cart = frappe.get_doc({
		"doctype": "Shopping Cart",
		"cart_type": cart_type,
		"session_id": session_id,
		"customer": customer,
		"price_list": price_list,
		"total_qty": 0,
		"total_amount": 0
	})
	cart.insert(ignore_permissions=True)

	return cart


def _update_cart_totals(cart_name):
	"""
	Internal function to update cart totals

	Args:
		cart_name (str): Shopping Cart name
	"""
	totals = frappe.db.sql("""
		SELECT
			SUM(qty) as total_qty,
			SUM(amount) as total_amount
		FROM `tabShopping Cart Item`
		WHERE parent = %s
	""", cart_name, as_dict=True)

	if totals and totals[0]:
		frappe.db.set_value("Shopping Cart", cart_name, {
			"total_qty": flt(totals[0].total_qty),
			"total_amount": flt(totals[0].total_amount)
		})


@frappe.whitelist(allow_guest=True)
def ping():
	"""
	Health check endpoint

	Returns:
		dict: API status and version info
	"""
	return {
		"status": "ok",
		"message": "ERPNext E-Commerce Integration API is running",
		"frappe_version": frappe.__version__,
		"site": frappe.local.site,
	}


@frappe.whitelist()
def get_user_roles_for_auth(username):
	"""
	Return role names for a specific user.
	Used by the React auth bootstrap to derive frontend privileges.
	"""
	if not username:
		frappe.throw(_("username is required"))

	if not frappe.db.exists("User", username):
		frappe.throw(_("User {0} not found").format(username))

	return frappe.get_roles(username)


# ========================================
# POSNET APIs
# ========================================


@frappe.whitelist()
def create_pos_sale(
	offline_order_uuid,
	receipt_number,
	items,
	total_amount,
	payment_method="Cash",
	cashier_id=None,
	device_id=None,
	branch_id=None,
	sale_mode="WHITE",
):
	"""
	Create a POS sale as a submitted Sales Invoice + Payment Entry.

	Idempotent: if a Sales Invoice already exists for offline_order_uuid it is
	returned without creating a duplicate (S005 idempotency guard).

	Args:
		offline_order_uuid (str): Client-generated UUID used as idempotency key.
		receipt_number (str): Human-readable receipt number (e.g. MAIN-A1B2-20260311-0042).
		items (list[dict]): List of {item_code, item_name, qty, rate, amount}.
		total_amount (float): Expected grand total — validated against computed invoice total.
		payment_method (str): Mode of payment (Cash / Card / Mobile Money).
		cashier_id (str): Cashier identifier for audit trail.
		device_id (str): Device/terminal identifier for audit trail.
		branch_id (str): Branch identifier — used to resolve warehouse.

	Returns:
		dict: {invoice_id, payment_id, offline_order_uuid, status}

	Raises:
		frappe.ValidationError: If items are empty or total does not match.
	"""
	import json

	# Deserialise items if passed as JSON string (frappe.call sends lists as strings)
	if isinstance(items, str):
		items = json.loads(items)

	if not items:
		frappe.throw(_("At least one item is required to create a POS sale."))

	# ── i008: Validate sale_mode and enforce payment method policy ────────────
	sale_mode = (sale_mode or "WHITE").upper()
	if sale_mode not in ("WHITE", "BLACK"):
		frappe.throw(_("Invalid sale_mode: must be WHITE or BLACK."))

	is_borrador = 1 if sale_mode == "BLACK" else 0

	BLACK_ALLOWED_METHODS = {"Cash", "Mobile Money"}
	if sale_mode == "BLACK" and payment_method not in BLACK_ALLOWED_METHODS:
		frappe.throw(
			_(
				"Payment method '{0}' is not allowed when sale_mode=BLACK. "
				"Allowed methods: {1}."
			).format(payment_method, ", ".join(sorted(BLACK_ALLOWED_METHODS))),
			exc=frappe.ValidationError,
		)

	# ── Idempotency check ────────────────────────────────────────────────────
	# We store offline_order_uuid in the `remarks` field so we can look it up
	# without requiring a schema migration.
	existing = frappe.db.get_value(
		"Sales Invoice",
		{"remarks": ("like", f"%offline_order_uuid:{offline_order_uuid}%"), "docstatus": 1},
		"name",
	)
	if existing:
		return {
			"invoice_id": existing,
			"payment_id": "",
			"offline_order_uuid": offline_order_uuid,
			"status": "already_exists",
		}

	# ── Resolve defaults ──────────────────────────────────────────────────────
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	pos_customer = (
		frappe.db.get_value("Customer", {"customer_name": "Walk-in Customer"}, "name")
		or frappe.db.get_value("Customer", {}, "name")
		or "_Test Customer"
	)

	# Resolve warehouse: prefer branch_id as warehouse name, fall back to default
	warehouse = (
		frappe.db.get_value("Warehouse", branch_id, "name") if branch_id else None
	) or frappe.db.get_value(
		"Warehouse", {"is_group": 0, "company": company}, "name"
	)

	# ── Build Sales Invoice ───────────────────────────────────────────────────
	remarks_tag = (
		f"offline_order_uuid:{offline_order_uuid} | receipt:{receipt_number}"
		f" | cashier:{cashier_id or 'unknown'} | device:{device_id or 'unknown'}"
		f" | branch:{branch_id or 'unknown'}"
		f" | sale_mode:{sale_mode} | is_borrador:{is_borrador}"
	)

	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": pos_customer,
			"company": company,
			"is_pos": 0,
			"posting_date": nowdate(),
			"due_date": nowdate(),
			"remarks": remarks_tag,
			"items": [
				{
					"item_code": item["item_code"],
					"item_name": item.get("item_name", item["item_code"]),
					"qty": flt(item["qty"]),
					"rate": flt(item["rate"]),
					"warehouse": warehouse,
				}
				for item in items
			],
		}
	)

	invoice.set_missing_values()
	invoice.calculate_taxes_and_totals()

	# ── Validate grand total matches client expectation (within 1 unit rounding) ──
	if abs(flt(invoice.grand_total) - flt(total_amount)) > 1:
		frappe.throw(
			_(
				"Grand total mismatch: server computed {0}, client sent {1}. "
				"Check item rates and taxes."
			).format(invoice.grand_total, total_amount)
		)

	invoice.insert(ignore_permissions=True)
	invoice.submit()

	# ── Create Payment Entry ──────────────────────────────────────────────────
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment = get_payment_entry("Sales Invoice", invoice.name)
	# Validate mode_of_payment exists; fall back to Cash if not found
	if not frappe.db.exists("Mode of Payment", payment_method):
		payment_method = "Cash"
	payment.mode_of_payment = payment_method
	payment.reference_no = receipt_number
	payment.reference_date = nowdate()
	payment.remarks = remarks_tag
	payment.insert(ignore_permissions=True)
	payment.submit()

	# ── Store payment entry name back on invoice (best-effort) ───────────────
	try:
		frappe.db.set_value("Sales Invoice", invoice.name, "custom_payment_entry", payment.name)
	except Exception:
		pass  # custom_payment_entry field may not exist — non-fatal

	frappe.db.commit()

	return {
		"invoice_id": invoice.name,
		"payment_id": payment.name,
		"offline_order_uuid": offline_order_uuid,
		"sale_mode": sale_mode,
		"is_borrador": is_borrador,
		"status": "created",
	}


@frappe.whitelist()
def sync_stock_events(events):
	"""
	Receive local stock movement events from offline POS clients and record them
	for reconciliation against server stock.

	Each event represents one stock delta (negative = sale, positive = return/adjustment).
	The server logs the events and flags conflicts where the server stock went negative
	or diverges significantly from the client's expectation.

	Args:
		events (list[dict]): List of stock events, each with:
			- id (str): Client-generated event UUID
			- item_code (str): Item affected
			- delta (float): Quantity change (negative for sales)
			- offline_order_uuid (str): Source sale UUID
			- created_at (str): ISO datetime of the event on the client

	Returns:
		dict: {
			processed (int): Number of events accepted,
			conflicts (list[dict]): Events with stock conflicts flagged for review
		}
	"""
	import json

	if isinstance(events, str):
		events = json.loads(events)

	if not events:
		return {"processed": 0, "conflicts": []}

	processed = 0
	conflicts = []

	for event in events:
		item_code = event.get("item_code")
		delta = flt(event.get("delta", 0))
		event_id = event.get("id", "")
		uuid = event.get("offline_order_uuid", "")

		if not item_code:
			continue

		# Check current stock across all warehouses
		actual_qty = flt(
			frappe.db.sql(
				"SELECT SUM(actual_qty) FROM `tabBin` WHERE item_code = %s",
				item_code,
			)[0][0]
			or 0
		)

		# Flag a conflict if applying this delta would push stock below zero
		projected = actual_qty + delta  # delta is negative for sales
		if projected < 0:
			conflicts.append(
				{
					"id": event_id,
					"item_code": item_code,
					"delta": delta,
					"offline_order_uuid": uuid,
					"server_actual_qty": actual_qty,
					"projected_qty": projected,
					"conflict_reason": (
						f"Stock would go negative: server has {actual_qty}, "
						f"applying delta {delta} → {projected}"
					),
				}
			)

		# Log the event to the error log in a structured way for later reconciliation
		# (In production you would write to a dedicated StockEventLog doctype)
		frappe.log_error(
			title=f"POS Stock Event: {item_code}",
			message=(
				f"event_id={event_id}\n"
				f"item_code={item_code}\n"
				f"delta={delta}\n"
				f"offline_order_uuid={uuid}\n"
				f"server_actual_qty={actual_qty}\n"
				f"projected_qty={projected}\n"
				f"conflict={'YES' if projected < 0 else 'no'}"
			),
		) if projected < 0 else None  # only log actual conflicts

		processed += 1

	return {
		"processed": processed,
		"conflicts": conflicts,
	}


# ========================================
# MERCADO PAGO QR PAYMENT APIs
# ========================================

_MP_API = "https://api.mercadopago.com"


@frappe.whitelist(allow_guest=True)
def create_mp_qr_preference(items, total_amount, receipt_number=None):
	"""
	Create a Mercado Pago Checkout Pro preference for QR display at the POS.

	Args:
		items (list[dict]): [{item_code, item_name, qty, rate}]
		total_amount (float): Cart total for display/logging.
		receipt_number (str): Used as external_reference to track the sale.

	Returns:
		dict: {preference_id, checkout_url, external_reference, is_test}
	"""
	import json as _json

	if isinstance(items, str):
		items = _json.loads(items)

	total_amount = flt(total_amount)
	external_reference = receipt_number or f"POS-{frappe.generate_hash(length=10)}"

	doc = frappe.get_doc("Mercado Pago Settings")
	if not doc.enabled:
		frappe.throw(_("Mercado Pago integration is not enabled"))

	token = doc._get_access_token()
	is_test = token.startswith("TEST-")
	idempotency_key = frappe.generate_hash(length=32)

	mp_items = [
		{
			"id": str(item.get("item_code", f"ITEM-{i}")),
			"title": str(item.get("item_name", "Product")),
			"quantity": int(item.get("qty", 1)),
			"unit_price": flt(item.get("rate", 0)),
			"currency_id": "ARS",
		}
		for i, item in enumerate(items)
	]

	payload = {
		"items": mp_items,
		"back_urls": {
			"success": "https://httpbin.org/get?back_url=success",
			"failure": "https://httpbin.org/get?back_url=failure",
			"pending": "https://httpbin.org/get?back_url=pending",
		},
		"auto_return": "approved",
		"external_reference": external_reference,
	}

	from frappe.integrations.utils import make_post_request

	headers = {
		"Authorization": f"Bearer {token}",
		"Content-Type": "application/json",
		"X-Idempotency-Key": idempotency_key,
	}

	try:
		response = make_post_request(
			url=f"{_MP_API}/checkout/preferences",
			headers=headers,
			json=payload,
		)
	except Exception as exc:
		raw = getattr(getattr(exc, "response", None), "text", "")
		frappe.log_error(raw, "MP QR - create_preference failed")
		frappe.throw(_("Could not create Mercado Pago preference: {0}").format(raw))

	preference_id = response.get("id")
	checkout_url = response.get("sandbox_init_point") if is_test else response.get("init_point")

	return {
		"preference_id": preference_id,
		"checkout_url": checkout_url,
		"external_reference": external_reference,
		"is_test": is_test,
	}


@frappe.whitelist(allow_guest=True)
def get_mp_payment_status(external_reference):
	"""
	Poll Mercado Pago for payment status using the external_reference (receipt number).

	Args:
		external_reference (str): Receipt number set as external_reference on the preference.

	Returns:
		dict: {status: 'pending'|'approved'|'rejected'|'error', payment_id, status_detail}
	"""
	from frappe.integrations.utils import make_get_request

	doc = frappe.get_doc("Mercado Pago Settings")
	token = doc._get_access_token()
	headers = {"Authorization": f"Bearer {token}"}

	try:
		response = make_get_request(
			url=(
				f"{_MP_API}/v1/payments/search"
				f"?external_reference={external_reference}"
				f"&sort=date_created&criteria=desc"
			),
			headers=headers,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "MP QR - get_payment_status failed")
		return {"status": "error"}

	results = response.get("results", []) if isinstance(response, dict) else []
	if not results:
		return {"status": "pending"}

	latest = results[0]
	return {
		"status": latest.get("status", "pending"),
		"payment_id": latest.get("id"),
		"status_detail": latest.get("status_detail"),
	}


# ── i012: Merchandise Receiving ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def search_items_for_receiving(search_term=None, page_length=8):
	"""Lean item search for the receiving screen — includes last purchase price."""
	page_length = cint(page_length)
	filters = {"disabled": 0, "is_stock_item": 1}
	or_filters = None
	if search_term:
		or_filters = [
			["item_code", "like", f"%{search_term}%"],
			["item_name", "like", f"%{search_term}%"],
			["Item Barcode", "barcode", "like", f"%{search_term}%"],
		]
	items = frappe.get_list(
		"Item",
		filters=filters,
		or_filters=or_filters,
		fields=["item_code", "item_name", "stock_uom", "item_group"],
		page_length=page_length,
	)
	# Attach barcodes
	if items:
		barcodes = frappe.get_all(
			"Item Barcode",
			filters={"parent": ["in", [i.item_code for i in items]]},
			fields=["parent", "barcode"],
		)
		bc_map = {}
		for b in barcodes:
			bc_map.setdefault(b.parent, []).append(b.barcode)
		for item in items:
			item["barcodes"] = bc_map.get(item.item_code, [])
	return items


@frappe.whitelist(allow_guest=True)
def commit_receiving_session(session_id, reference, supplier, warehouse, lines, draft_items):
	"""
	Atomically:
	1. Create new ERPNext Items for draft items
	2. Create a submitted Stock Entry (Material Receipt)
	Returns { stock_entry_id, new_item_codes }
	"""
	import json
	if isinstance(lines, str):
		lines = json.loads(lines)
	if isinstance(draft_items, str):
		draft_items = json.loads(draft_items)

	# 1. Create draft items
	import uuid as _uuid
	new_item_codes = {}
	for d in draft_items:
		if frappe.db.exists("Item", d.get("item_code") or ""):
			new_item_codes[d["draft_id"]] = d["item_code"]
			continue
		# item_code is mandatory in ERPNext — generate a unique one if not provided
		item_code_val = (d.get("item_code") or "").strip() or str(_uuid.uuid4())
		item_doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code_val,
			"item_name": d["item_name"],
			"item_group": d.get("item_group") or "Products",
			"stock_uom": d.get("stock_uom") or "Nos",
			"is_stock_item": 1,
			"include_item_in_manufacturing": 0,
			"description": d["item_name"],
		})
		item_doc.insert(ignore_permissions=True)
		# Add barcode if provided — skip silently if ERPNext rejects the format
		if d.get("barcode"):
			try:
				item_doc.append("barcodes", {"barcode": d["barcode"], "barcode_type": "EAN"})
				item_doc.save(ignore_permissions=True)
			except Exception:
				pass  # barcode is optional; don't abort the commit over a format error
		# Add selling price if estimated
		if flt(d.get("estimated_price") or 0) > 0:
			frappe.get_doc({
				"doctype": "Item Price",
				"item_code": item_doc.item_code,
				"price_list": "Standard Selling",
				"price_list_rate": flt(d["estimated_price"]),
				"selling": 1,
			}).insert(ignore_permissions=True)
		new_item_codes[d["draft_id"]] = item_doc.name  # name == item_code after insert

	# Commit item inserts so the Stock Entry link-field validation can resolve them
	if new_item_codes:
		frappe.db.commit()

	# 2. Resolve draft item_codes in lines
	resolved_lines = []
	for line in lines:
		item_code = line.get("item_code")
		if not item_code and line.get("draft_item_id"):
			item_code = new_item_codes.get(line["draft_item_id"])
		if not item_code:
			continue
		basic_rate = flt(line.get("unit_cost") or 0)
		resolved_lines.append({
			"item_code": item_code,
			"qty": flt(line.get("qty") or 0),
			"basic_rate": basic_rate,
			"t_warehouse": warehouse,
			"allow_zero_valuation_rate": 1 if basic_rate == 0 else 0,
		})

	if not resolved_lines:
		frappe.throw("No valid lines to receive")

	# 3. Create Stock Entry
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"posting_date": nowdate(),
		"to_warehouse": warehouse,
		"items": resolved_lines,
		"remarks": f"Receiving session {session_id}" + (f" — ref: {reference}" if reference else ""),
	})
	se.insert(ignore_permissions=True)
	se.submit()
	frappe.db.commit()

	return {
		"stock_entry_id": se.name,
		"new_item_codes": new_item_codes,
	}


@frappe.whitelist(allow_guest=True)
def simulate_receiving_flow():
	"""Return a deterministic sample payload for testing the receiving screen."""
	# Use 3 existing items + 2 fake new ones
	existing = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_stock_item": 1},
		fields=["item_code", "item_name", "stock_uom", "image"],
		limit=3,
	)
	import uuid
	draft_items = [
		{
			"draft_id": str(uuid.uuid4()),
			"item_name": "Libro de Prueba Nuevo A",
			"item_group": "Products",
			"stock_uom": "Nos",
			"barcode": "",           # intentionally empty to test soft validation
			"estimated_price": 15.0,
			"estimated_cost": 0,     # intentionally empty to test soft validation
			"is_new": True,
		},
		{
			"draft_id": str(uuid.uuid4()),
			"item_name": "Libro de Prueba Nuevo B",
			"item_group": "Products",
			"stock_uom": "Nos",
			"barcode": "9780306406157",  # valid EAN-13 check digit
			"estimated_price": 22.5,
			"estimated_cost": 12.0,
			"is_new": True,
		},
		{
			"draft_id": str(uuid.uuid4()),
			"item_name": "Libro de Prueba Nuevo C",
			"item_group": "Products",
			"stock_uom": "Nos",
			"barcode": "",           # intentionally empty to test soft validation
			"estimated_price": 18.0,
			"estimated_cost": 0,     # intentionally empty to test soft validation
			"is_new": True,
		},
	]
	lines = [
		{"line_id": str(uuid.uuid4()), "item_code": e["item_code"], "item_name": e["item_name"], "qty": i + 2, "unit_cost": 10.0 + i * 2, "image": e.get("image") or None}
		for i, e in enumerate(existing)
	] + [
		{"line_id": str(uuid.uuid4()), "item_code": None, "draft_item_id": draft_items[0]["draft_id"], "item_name": draft_items[0]["item_name"], "qty": 5, "unit_cost": 0},
		{"line_id": str(uuid.uuid4()), "item_code": None, "draft_item_id": draft_items[1]["draft_id"], "item_name": draft_items[1]["item_name"], "qty": 3, "unit_cost": 12.0},
		{"line_id": str(uuid.uuid4()), "item_code": None, "draft_item_id": draft_items[2]["draft_id"], "item_name": draft_items[2]["item_name"], "qty": 4, "unit_cost": 0},
	]
	return {
		"reference": "Container-SIM-001",
		"supplier": "",   # intentionally empty
		"warehouse": "POSNET Stores - L",
		"lines": lines,
		"draft_items": draft_items,
	}


# ── CSV Catalog Import (stable index mapping) ────────────────────────────────

CATALOG_CSV_COLUMN_GUIDE = [
	{"index": 0, "key": "item_code", "english": "SKU / Item Code", "chinese": "商品编码", "required": 1},
	{"index": 1, "key": "barcode", "english": "Barcode", "chinese": "条码", "required": 0},
	{"index": 2, "key": "item_name", "english": "Title", "chinese": "商品名称", "required": 0},
	{"index": 3, "key": "unused_3", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 4, "key": "title_simplified", "english": "Simplified Title", "chinese": "简化名称", "required": 0},
	{"index": 5, "key": "pack_qty", "english": "Pack Quantity", "chinese": "包装数量", "required": 0},
	{"index": 6, "key": "stock_uom", "english": "UOM", "chinese": "单位", "required": 0},
	{"index": 7, "key": "unused_7", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 8, "key": "unused_8", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 9, "key": "item_group", "english": "Category", "chinese": "分类", "required": 0},
	{"index": 10, "key": "unused_10", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 11, "key": "unused_11", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 12, "key": "price", "english": "Selling Price", "chinese": "销售价格", "required": 0},
	{"index": 13, "key": "unused_13", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 14, "key": "unused_14", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 15, "key": "unused_15", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 16, "key": "unused_16", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 17, "key": "last_price", "english": "Last Price", "chinese": "上次价格", "required": 0},
	{"index": 18, "key": "last_price_date", "english": "Last Price Date", "chinese": "上次价格日期", "required": 0},
	{"index": 19, "key": "unused_19", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 20, "key": "unused_20", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 21, "key": "unused_21", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 22, "key": "stock_hint", "english": "Stock Hint", "chinese": "库存提示", "required": 0},
	{"index": 23, "key": "unused_23", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 24, "key": "unused_24", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 25, "key": "unused_25", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 26, "key": "unused_26", "english": "Unused", "chinese": "未使用", "required": 0},
	{"index": 27, "key": "unused_27", "english": "Unused", "chinese": "未使用", "required": 0},
]


def _row_value(row, index):
	if index < 0 or index >= len(row):
		return ""
	return (row[index] or "").strip()


def _safe_float(value):
	if value in (None, ""):
		return 0.0
	try:
		return flt(str(value).replace(",", "."))
	except Exception:
		return 0.0


def _parse_catalog_csv(csv_text):
	if not csv_text:
		return [], 0

	reader = csv.reader(io.StringIO(csv_text))
	rows = list(reader)
	if not rows:
		return [], 0

	data_rows = rows[1:] if len(rows) > 1 else []
	parsed = []

	for line_no, row in enumerate(data_rows, start=2):
		if not any((cell or "").strip() for cell in row):
			continue

		if len(row) < 28:
			row = row + [""] * (28 - len(row))

		item_code = _row_value(row, 0)
		item_name = _row_value(row, 2) or _row_value(row, 4) or item_code
		title_simplified = _row_value(row, 4)
		barcode = _row_value(row, 1)
		stock_uom = _row_value(row, 6) or "Nos"
		item_group = _row_value(row, 9) or "Products"
		price = _safe_float(_row_value(row, 12))
		last_price = _safe_float(_row_value(row, 17))
		stock_hint = _safe_float(_row_value(row, 22))

		errors = []
		if not item_code:
			errors.append("Missing item_code at index 0.")
		if not item_name:
			errors.append("Missing item_name/title at indexes 2/4.")

		parsed.append(
			{
				"line_no": line_no,
				"item_code": item_code,
				"item_name": item_name,
				"title_simplified": title_simplified,
				"barcode": barcode,
				"stock_uom": stock_uom,
				"item_group": item_group,
				"price": price,
				"last_price": last_price,
				"stock_hint": stock_hint,
				"errors": errors,
			}
		)

	return parsed, len(data_rows)


def _resolve_uom_for_import(uom):
	if uom and frappe.db.exists("UOM", uom):
		return uom
	if frappe.db.exists("UOM", "Nos"):
		return "Nos"
	return frappe.db.get_value("UOM", {}, "name") or "Nos"


def _resolve_item_group_for_import(item_group, default_item_group="Products", create_missing_groups=0):
	if item_group and frappe.db.exists("Item Group", item_group):
		return item_group

	if item_group and cint(create_missing_groups):
		parent_group = "All Item Groups"
		if not frappe.db.exists("Item Group", item_group):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": item_group,
					"parent_item_group": parent_group,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return item_group

	if default_item_group and frappe.db.exists("Item Group", default_item_group):
		return default_item_group
	if frappe.db.exists("Item Group", "Products"):
		return "Products"
	return frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"


@frappe.whitelist(allow_guest=True)
def get_catalog_csv_column_guide():
	"""Return stable CSV index mapping used by catalog import."""
	return CATALOG_CSV_COLUMN_GUIDE


@frappe.whitelist()
def preview_catalog_csv_import(csv_text):
	"""Preview parsed CSV rows using stable column indexes (header names ignored)."""
	parsed_rows, total_rows = _parse_catalog_csv(csv_text)
	valid_rows = [r for r in parsed_rows if not r.get("errors")]
	invalid_rows = [r for r in parsed_rows if r.get("errors")]
	return {
		"total_rows": total_rows,
		"parsed_rows": len(parsed_rows),
		"valid_rows": len(valid_rows),
		"invalid_rows": len(invalid_rows),
		"preview": parsed_rows[:25],
		"guide": CATALOG_CSV_COLUMN_GUIDE,
	}


@frappe.whitelist()
def import_catalog_csv_products(
	csv_text,
	price_list="Standard Selling",
	default_item_group="Products",
	update_existing=1,
	create_missing_groups=0,
	start=0,
	batch_size=0,
):
	"""
	Create/update Item + Item Price records from catalog CSV.
	CSV header text is ignored; only stable column order is used.
	"""
	parsed_rows, total_rows = _parse_catalog_csv(csv_text)
	start = cint(start)
	batch_size = cint(batch_size)
	if start < 0:
		start = 0
	if batch_size < 0:
		batch_size = 0

	selected_rows = parsed_rows
	if batch_size > 0:
		selected_rows = parsed_rows[start : start + batch_size]

	report = {
		"total_rows": total_rows,
		"parsed_rows": len(parsed_rows),
		"processed_rows": len(selected_rows),
		"start": start,
		"batch_size": batch_size,
		"has_more": 0,
		"next_start": None,
		"created_items": 0,
		"updated_items": 0,
		"skipped_invalid": 0,
		"skipped_existing": 0,
		"price_updates": 0,
		"barcode_updates": 0,
		"errors": [],
		"warnings": [],
	}

	for row in selected_rows:
		if row.get("errors"):
			report["skipped_invalid"] += 1
			report["errors"].append({"line_no": row["line_no"], "errors": row["errors"]})
			continue

		item_code = row["item_code"]
		item_name = row["item_name"]
		description = row["title_simplified"] or item_name
		barcode = row.get("barcode")
		target_uom = _resolve_uom_for_import(row.get("stock_uom"))
		target_group = _resolve_item_group_for_import(
			row.get("item_group"),
			default_item_group=default_item_group,
			create_missing_groups=create_missing_groups,
		)

		try:
			existing = frappe.db.exists("Item", item_code)
			if existing and not cint(update_existing):
				report["skipped_existing"] += 1
				continue

			if existing:
				item_doc = frappe.get_doc("Item", item_code)
				report["updated_items"] += 1
			else:
				item_doc = frappe.new_doc("Item")
				item_doc.item_code = item_code
				report["created_items"] += 1

			item_doc.item_name = item_name
			item_doc.description = description
			item_doc.item_group = target_group
			item_doc.stock_uom = target_uom
			item_doc.is_stock_item = 1
			item_doc.include_item_in_manufacturing = 0
			item_doc.disabled = 0

			if existing:
				item_doc.save(ignore_permissions=True)
			else:
				item_doc.insert(ignore_permissions=True)

			if barcode:
				existing_same = frappe.db.exists("Item Barcode", {"parent": item_doc.item_code, "barcode": barcode})
				owner = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
				if owner and owner != item_doc.item_code:
					report["warnings"].append(
						{
							"line_no": row["line_no"],
							"message": f"Barcode {barcode} already assigned to {owner}; skipped for {item_doc.item_code}.",
						}
					)
				elif not existing_same:
					item_doc.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})
					item_doc.save(ignore_permissions=True)
					report["barcode_updates"] += 1

			if flt(row.get("price")) > 0:
				price_name = frappe.db.get_value(
					"Item Price",
					{"item_code": item_doc.item_code, "price_list": price_list, "selling": 1},
					"name",
				)
				if price_name:
					frappe.db.set_value("Item Price", price_name, "price_list_rate", flt(row["price"]))
				else:
					frappe.get_doc(
						{
							"doctype": "Item Price",
							"item_code": item_doc.item_code,
							"price_list": price_list,
							"price_list_rate": flt(row["price"]),
							"selling": 1,
						}
					).insert(ignore_permissions=True)
				report["price_updates"] += 1

		except Exception as exc:
			report["errors"].append({"line_no": row["line_no"], "errors": [str(exc)]})

	frappe.db.commit()
	if batch_size > 0:
		next_start = start + len(selected_rows)
		if next_start < len(parsed_rows):
			report["has_more"] = 1
			report["next_start"] = next_start
	return report


@frappe.whitelist()
def import_catalog_image_zip(zip_base64=None):
	"""
	Import product images from a ZIP payload.
	File name (without extension) must match Item.item_code.
	Supported image types: jpg, jpeg, png, webp, gif.
	"""
	zip_bytes = None
	if zip_base64:
		try:
			zip_bytes = base64.b64decode(zip_base64)
		except Exception:
			frappe.throw(_("Invalid zip_base64 payload"))
	else:
		req_files = getattr(getattr(frappe.local, "request", None), "files", None)
		uploaded = None
		if req_files:
			uploaded = req_files.get("file") or req_files.get("zip_file")
		if uploaded:
			zip_bytes = uploaded.read()
		if not zip_bytes:
			frappe.throw(_("Provide zip_base64 or upload a file field named 'file'."))

	report = {
		"total_files": 0,
		"supported_files": 0,
		"updated_items": 0,
		"missing_items": 0,
		"skipped_unsupported": 0,
		"errors": [],
	}

	supported_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

	with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
		members = [m for m in archive.namelist() if not m.endswith("/")]
		report["total_files"] = len(members)

		for member in members:
			base_name = os.path.basename(member)
			if not base_name:
				continue

			stem, ext = os.path.splitext(base_name)
			ext = ext.lower()
			if ext not in supported_ext:
				report["skipped_unsupported"] += 1
				continue

			report["supported_files"] += 1
			item_code = _resolve_item_code_from_image_stem(stem)
			if not item_code:
				report["missing_items"] += 1
				continue

			try:
				content = archive.read(member)
				_apply_item_image(item_code, f"{item_code}{ext}", content)
				report["updated_items"] += 1
			except Exception as exc:
				report["errors"].append({"file": member, "error": str(exc)})

	frappe.db.commit()
	return report


def _apply_item_image(item_code, file_name, content):
	"""Attach image file to an Item and set Item.image."""
	from frappe.utils.file_manager import save_file

	if isinstance(content, str):
		content = content.encode("utf-8")

	file_doc = save_file(
		file_name,
		content,
		"Item",
		item_code,
		is_private=0,
	)
	frappe.db.set_value("Item", item_code, "image", file_doc.file_url)


def _resolve_item_code_from_image_stem(stem):
	"""
	Resolve image filename stem to Item.item_code.
	Order:
	1) exact item_code
	2) item_code after trimming leading zeros
	3) exact barcode -> Item Barcode.parent
	4) trimmed barcode -> Item Barcode.parent
	"""
	key = (stem or "").strip()
	if not key:
		return None

	candidates = [key]
	trimmed = key.lstrip("0")
	if trimmed and trimmed != key:
		candidates.append(trimmed)

	for candidate in candidates:
		if frappe.db.exists("Item", candidate):
			return candidate

	for candidate in candidates:
		parent = frappe.db.get_value("Item Barcode", {"barcode": candidate}, "parent")
		if parent:
			return parent

	return None


@frappe.whitelist()
def import_catalog_image_batch(images):
	"""
	Import a small batch of product images.
	Each image entry must include:
	- file_name: original name (e.g., 24792.jpg)
	- content_base64: file bytes base64
	"""
	if isinstance(images, str):
		images = frappe.parse_json(images)
	if not isinstance(images, list):
		frappe.throw(_("images must be a list"))

	report = {
		"total_files": len(images),
		"supported_files": 0,
		"updated_items": 0,
		"missing_items": 0,
		"skipped_unsupported": 0,
		"errors": [],
	}

	supported_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

	for entry in images:
		file_name = os.path.basename((entry or {}).get("file_name") or "")
		content_base64 = (entry or {}).get("content_base64")
		if not file_name or not content_base64:
			report["errors"].append({"file": file_name or "(unknown)", "error": "Missing file_name or content_base64."})
			continue

		stem, ext = os.path.splitext(file_name)
		ext = ext.lower()
		if ext not in supported_ext:
			report["skipped_unsupported"] += 1
			continue

		report["supported_files"] += 1
		item_code = _resolve_item_code_from_image_stem(stem)
		if not item_code:
			report["missing_items"] += 1
			continue

		try:
			content = base64.b64decode(content_base64)
			_apply_item_image(item_code, file_name, content)
			report["updated_items"] += 1
		except Exception as exc:
			report["errors"].append({"file": file_name, "error": str(exc)})

	frappe.db.commit()
	return report


# ── i013: Dashboard mock-data seeding ────────────────────────────────────────

@frappe.whitelist()
def seed_bazar_dashboard_mock_data(
	anchor_date=None,
	days=60,
	invoices_per_day=6,
	item_count=120,
	regenerate=0,
	seed_value=13013,
):
	"""
	Prepopulate dashboard-oriented mock data for the bazar admin workspace.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.seed_bazar_dashboard_mock_data import run

	return run(
		anchor_date=anchor_date,
		days=days,
		invoices_per_day=invoices_per_day,
		item_count=item_count,
		regenerate=regenerate,
		seed_value=seed_value,
	)


@frappe.whitelist()
def clear_bazar_dashboard_mock_data():
	"""Clear previously seeded mock data for bazar dashboards."""
	from erpnext.erpnext_integrations.ecommerce_api.seed_bazar_dashboard_mock_data import clear_mock_data

	return clear_mock_data()


# ── i016: Product Manager Page ────────────────────────────────────────────────


def _pm_module():
	from erpnext.erpnext_integrations.ecommerce_api import product_manager as pm
	return pm

def _pm_has_col(col):
	return frappe.db.has_column("Item", col)


def _pm_item_select():
	"""Build SELECT field list depending on which custom fields exist."""
	always = [
		"i.item_code", "i.item_name", "i.brand", "i.item_group",
		"i.stock_uom", "i.image", "i.disabled", "i.modified",
	]
	custom = [
		"custom_pack_qty", "custom_pack_size", "custom_pack_unit",
		"custom_normalized_title", "custom_match_confidence", "custom_review_notes",
	]
	parts = always[:]
	for c in custom:
		parts.append(f"i.{c}" if _pm_has_col(c) else f"NULL AS {c}")
	parts += [
		"(SELECT barcode FROM `tabItem Barcode` ib"
		"  WHERE ib.parent = i.item_code ORDER BY ib.idx LIMIT 1) AS barcode",
		"(SELECT price_list_rate FROM `tabItem Price` ip"
		"  WHERE ip.item_code = i.item_code"
		"  AND ip.price_list = 'Standard Selling' AND ip.selling = 1 LIMIT 1) AS list_price",
	]
	return ", ".join(parts)


@frappe.whitelist()
def get_product_rows(filters=None, page=1, page_length=200):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().get_product_rows(filters=filters, page=page, page_length=page_length)


@frappe.whitelist()
def save_product_row(item_code, changes):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().save_product_row(item_code=item_code, changes=changes)


@frappe.whitelist()
def save_product_rows_bulk(rows):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().save_product_rows_bulk(rows=rows)


@frappe.whitelist()
def set_items_active_bulk(item_codes, is_active):
	"""Legacy wrapper for older clients; canonical name is set_active_bulk."""
	return _pm_module().set_active_bulk(item_codes=item_codes, is_active=is_active)


@frappe.whitelist()
def get_brand_suggestions(query=""):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().get_brand_suggestions(query=query)


@frappe.whitelist()
def get_category_list():
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().get_category_list()


@frappe.whitelist()
def export_product_rows(filters=None):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().export_rows(filters=filters)


@frappe.whitelist()
def update_product_info(item_code, item_name=None, price_list_rate=None, price_list=None):
	"""
	Update a product's official item_name and/or price_list_rate.

	Args:
		item_code: The item to update
		item_name: New display name (optional)
		price_list_rate: New price (optional)
		price_list: Which price list to update (defaults to 'Standard Selling')
	"""
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found").format(item_code))

	item = frappe.get_doc("Item", item_code)

	if item_name is not None and item_name != item.item_name:
		item.item_name = item_name
		item.save(ignore_permissions=True)

	if price_list_rate is not None:
		price_list_rate = flt(price_list_rate)
		pl = price_list or "Standard Selling"
		existing = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": pl, "selling": 1},
			"name",
		)
		if existing:
			frappe.db.set_value("Item Price", existing, "price_list_rate", price_list_rate)
		else:
			ip = frappe.new_doc("Item Price")
			ip.item_code = item_code
			ip.price_list = pl
			ip.selling = 1
			ip.price_list_rate = price_list_rate
			ip.insert(ignore_permissions=True)

	frappe.db.commit()
	return {
		"item_code": item_code,
		"item_name": frappe.db.get_value("Item", item_code, "item_name"),
		"price_list_rate": get_item_price(item_code, price_list or "Standard Selling"),
	}
