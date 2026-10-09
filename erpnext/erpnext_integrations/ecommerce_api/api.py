"""
E-Commerce Integration API
Provides comprehensive REST API endpoints for e-commerce integration with SilkOS

All endpoints are whitelisted and can be accessed via:
- REST API: /api/method/erpnext.erpnext_integrations.ecommerce_api.api.<method_name>
- JSON-RPC: frappe.call('erpnext.erpnext_integrations.ecommerce_api.api.<method_name>')
"""

import frappe
import csv
import io
import os
import re
import base64
import zipfile
from contextlib import contextmanager
from frappe import _
from frappe.utils import (
	cint,
	cstr,
	flt,
	getdate,
	nowdate,
	get_datetime,
	add_days,
	now_datetime,
)
from erpnext.stock.get_item_details import get_item_details as get_item_details_base
from erpnext.accounts.doctype.pricing_rule.pricing_rule import apply_pricing_rule
from erpnext.erpnext_integrations.ecommerce_api.ops_kv import idempotent_request


@frappe.whitelist(allow_guest=True)
def get_catalog_taxonomy(lang=None):
	"""Item Group / Brand aliases + thumbnails for catalog UI."""
	from erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n import (
		get_catalog_taxonomy as _get_catalog_taxonomy,
	)

	return _get_catalog_taxonomy(lang=lang)


@frappe.whitelist()
def enqueue_generate_taxonomy_aliases(
	doctype=None,
	names=None,
	langs=None,
	only_missing=1,
	limit=None,
	use_online_translate=1,
	now=0,
):
	"""Queue (or run) auto-generation of Item Group / Brand aliases."""
	from erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n import (
		enqueue_generate_taxonomy_aliases as _enqueue,
	)

	return _enqueue(
		doctype=doctype,
		names=names,
		langs=langs,
		only_missing=only_missing,
		limit=limit,
		use_online_translate=use_online_translate,
		now=now,
	)


@frappe.whitelist()
def set_taxonomy_aliases(doctype=None, name=None, aliases=None, overwrite=0):
	"""Merge JSON aliases onto one Item Group or Brand."""
	from erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n import set_taxonomy_aliases as _set

	return _set(doctype=doctype, name=name, aliases=aliases, overwrite=overwrite)


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
	include_disabled=0,
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
		include_disabled (int): When 1, also return inactive (disabled) Items

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
		for custom in (
			"custom_normalized_title",
			"custom_variant_group",
			"custom_pack_qty",
			"custom_pack_size",
			"custom_pack_unit",
			"custom_unit_sku",
			"custom_review_notes",
		):
			if frappe.db.has_column("Item", custom):
				fields.append(custom)
		# Standard Item fields used by catalog mayorista weight estimates (units × kg).
		for std in ("weight_per_unit", "weight_uom", "shelf_life_in_days", "has_batch_no", "has_expiry_date"):
			if std not in fields and frappe.db.has_column("Item", std):
				fields.append(std)
		for custom in ("custom_unit_weight_min", "custom_unit_weight_max"):
			if custom not in fields and frappe.db.has_column("Item", custom):
				fields.append(custom)

	if isinstance(filters, str):
		import json
		filters = json.loads(filters)

	if not filters:
		filters = {}

	# Convert string parameters to int
	start = cint(start)
	page_length = cint(page_length)

	# Default: only enabled items (POS can opt into disabled via include_disabled)
	if not cint(include_disabled):
		filters["disabled"] = 0
	else:
		filters.pop("disabled", None)

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

	# Pricing: default to selling price list so callers always get rates
	if not price_list:
		price_list = (
			frappe.db.get_single_value("Selling Settings", "selling_price_list")
			or "Standard Selling"
		)
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
	_attach_receiving_meta(items, price_list)
	_attach_item_group_meta(items)

	# Alias native shelf_life as sell-by (plazo comercial) + weight band for frontends.
	for item in items:
		if "shelf_life_in_days" in item and "sell_by_days" not in item:
			item["sell_by_days"] = item.get("shelf_life_in_days")
		if "custom_unit_weight_min" in item and "unit_weight_min" not in item:
			item["unit_weight_min"] = item.get("custom_unit_weight_min")
		if "custom_unit_weight_max" in item and "unit_weight_max" not in item:
			item["unit_weight_max"] = item.get("custom_unit_weight_max")

	return {
		"items": items,
		"total_count": total_count,
		"has_more": (start + page_length) < total_count,
	}


def _attach_item_group_meta(items):
	"""Attach parent_item_group + item_group_path (e.g. Queso>Holanda) in-place."""
	names = sorted({cstr(i.get("item_group") or "").strip() for i in items if i.get("item_group")})
	if not names:
		for item in items:
			item["parent_item_group"] = ""
			item["item_group_path"] = ""
			item["item_group_is_group"] = 0
		return

	rows = frappe.get_all(
		"Item Group",
		filters={"name": ["in", names]},
		fields=["name", "parent_item_group", "is_group"],
		ignore_permissions=True,
	)
	by_name = {r.name: r for r in rows}
	for item in items:
		gname = cstr(item.get("item_group") or "").strip()
		row = by_name.get(gname)
		parent = cstr((row.parent_item_group if row else "") or "").strip()
		if parent == "All Item Groups":
			parent = ""
		item["parent_item_group"] = parent
		item["item_group_is_group"] = cint(row.is_group) if row else 0
		item["item_group_path"] = _item_group_path(parent, gname) if gname else ""


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


def _attach_receiving_meta(items, price_list=None):
	"""Attach last_cost + default_supplier for receiving screen (in-place)."""
	item_codes = [i.get("item_code") for i in items if i.get("item_code")]
	if not item_codes:
		return

	# Latest Purchase Receipt rate per item
	pr_rows = frappe.db.sql(
		"""
		SELECT pri.item_code, pri.rate
		FROM `tabPurchase Receipt Item` pri
		INNER JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
		WHERE pri.item_code IN %(codes)s
		  AND pr.docstatus = 1
		ORDER BY pr.posting_date DESC, pr.creation DESC
		""",
		{"codes": item_codes},
		as_dict=True,
	)
	last_cost = {}
	for row in pr_rows:
		code = row.item_code
		if code not in last_cost and flt(row.rate) > 0:
			last_cost[code] = flt(row.rate)

	# Fallback: bin valuation_rate
	missing = [c for c in item_codes if c not in last_cost]
	if missing:
		bin_rows = frappe.db.sql(
			"""
			SELECT item_code, valuation_rate
			FROM `tabBin`
			WHERE item_code IN %(codes)s
			  AND IFNULL(valuation_rate, 0) > 0
			ORDER BY modified DESC
			""",
			{"codes": missing},
			as_dict=True,
		)
		for row in bin_rows:
			if row.item_code not in last_cost:
				last_cost[row.item_code] = flt(row.valuation_rate)

	# Item Default supplier
	sup_rows = frappe.get_all(
		"Item Default",
		filters={"parent": ["in", item_codes], "default_supplier": ["is", "set"]},
		fields=["parent", "default_supplier"],
		ignore_permissions=True,
	)
	sup_map = {r.parent: r.default_supplier for r in sup_rows if r.default_supplier}

	buying_pl = (
		frappe.db.get_single_value("Buying Settings", "buying_price_list")
		or "Standard Buying"
	)
	buy_rows = frappe.db.sql(
		"""
		SELECT item_code, MAX(price_list_rate) AS rate
		FROM `tabItem Price`
		WHERE price_list = %(pl)s
		  AND buying = 1
		  AND item_code IN %(codes)s
		GROUP BY item_code
		""",
		{"pl": buying_pl, "codes": item_codes},
		as_dict=True,
	)
	buy_map = {r.item_code: flt(r.rate) for r in buy_rows if flt(r.rate) > 0}

	for item in items:
		code = item.get("item_code")
		item["last_cost"] = last_cost.get(code) or 0
		item["buying_price"] = buy_map.get(code) or 0
		item["cost_from_buying"] = 1 if item["buying_price"] else 0
		item["default_supplier"] = sup_map.get(code) or None
		# Ensure price_list_rate present for overwrite comparisons
		if item.get("price_list_rate") is None and price_list:
			item["price_list_rate"] = get_item_price(code, price_list)


@frappe.whitelist(allow_guest=True)
def get_receiving_item_meta(item_codes=None, price_list=None):
	"""Batch meta for receiving submit warnings: last_cost, list_price, supplier, stock."""
	import json

	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)
	item_codes = [c for c in (item_codes or []) if c]
	if not item_codes:
		return {"items": {}}

	if not price_list:
		price_list = (
			frappe.db.get_single_value("Selling Settings", "selling_price_list")
			or "Standard Selling"
		)

	stubs = [{"item_code": c} for c in item_codes]
	_attach_receiving_meta(stubs, price_list)
	out = {}
	for stub in stubs:
		code = stub["item_code"]
		out[code] = {
			"last_cost": stub.get("last_cost") or 0,
			"buying_price": stub.get("buying_price") or 0,
			"cost_from_buying": stub.get("cost_from_buying") or 0,
			"default_supplier": stub.get("default_supplier"),
			"price_list_rate": get_item_price(code, price_list) or 0,
			"stock_qty": get_stock_balance(code) or 0,
		}
		if frappe.db.exists("Item", code):
			fields = ["shelf_life_in_days", "has_batch_no", "has_expiry_date", "weight_per_unit"]
			if frappe.db.has_column("Item", "custom_unit_weight_min"):
				fields.append("custom_unit_weight_min")
			if frappe.db.has_column("Item", "custom_unit_weight_max"):
				fields.append("custom_unit_weight_max")
			row = frappe.db.get_value("Item", code, fields, as_dict=True) or {}
			out[code]["sell_by_days"] = cint(row.get("shelf_life_in_days") or 0) or None
			out[code]["has_batch_no"] = cint(row.get("has_batch_no") or 0)
			out[code]["has_expiry_date"] = cint(row.get("has_expiry_date") or 0)
			out[code]["weight_per_unit"] = flt(row.get("weight_per_unit") or 0) or None
			out[code]["unit_weight_min"] = (
				flt(row.get("custom_unit_weight_min"))
				if row.get("custom_unit_weight_min") not in (None, "")
				else None
			)
			out[code]["unit_weight_max"] = (
				flt(row.get("custom_unit_weight_max"))
				if row.get("custom_unit_weight_max") not in (None, "")
				else None
			)
	return {"items": out}


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


def _item_codes_for_barcode(barcode):
	"""Every Item that owns this exact barcode, plus a direct item_code match."""
	code = cstr(barcode or "").strip()
	if not code:
		return []
	rows = frappe.get_all(
		"Item Barcode",
		filters={"barcode": code},
		fields=["parent"],
		order_by="parent asc",
		ignore_permissions=True,
	)
	codes = []
	seen = set()
	for row in rows:
		parent = row.get("parent")
		if parent and parent not in seen and frappe.db.exists("Item", parent):
			seen.add(parent)
			codes.append(parent)
	if code not in seen and frappe.db.exists("Item", code):
		codes.append(code)
	return codes


@frappe.whitelist(allow_guest=True)
def search_products_by_barcode(barcode, price_list=None, allow_disabled=0):
	"""
	All products that share this barcode (or whose item_code is the barcode).

	Returns a list of get_product() dicts. Empty list when nothing matches —
	does not throw, so the POS can show "not found" or a picker.
	"""
	codes = _item_codes_for_barcode(barcode)
	if not codes:
		return []
	products = []
	prev = frappe.flags.ignore_permissions
	frappe.flags.ignore_permissions = True
	try:
		for item_code in codes:
			if not cint(allow_disabled) and cint(frappe.db.get_value("Item", item_code, "disabled")):
				continue
			products.append(get_product(item_code, price_list=price_list))
	finally:
		frappe.flags.ignore_permissions = prev
	products.sort(key=lambda p: (p.get("item_name") or p.get("item_code") or "").lower())
	return products


@frappe.whitelist(allow_guest=True)
def search_by_barcode(barcode, price_list=None, allow_disabled=0):
	"""
	Find a product by barcode value.
	Falls back to matching item_code directly if no Item Barcode record exists.

	Returns the same structure as get_product(). When several items share the
	barcode, returns the first — POS should call search_products_by_barcode
	and let the cashier pick.
	When allow_disabled=0 (default), disabled Items raise DoesNotExistError so
	cashiers do not silently sell inactive SKUs — POS can pass allow_disabled=1
	to surface them and show its own alert.
	"""
	codes = _item_codes_for_barcode(barcode)
	if not cint(allow_disabled):
		codes = [
			code
			for code in codes
			if not cint(frappe.db.get_value("Item", code, "disabled"))
		]
	if not codes:
		frappe.throw(_("No item found for barcode: {0}").format(barcode), frappe.DoesNotExistError)
	prev = frappe.flags.ignore_permissions
	frappe.flags.ignore_permissions = True
	try:
		return get_product(codes[0], price_list=price_list)
	finally:
		frappe.flags.ignore_permissions = prev


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
	"""Return all Item Groups (including is_group parents) with path Parent>Leaf."""
	rows = frappe.get_all(
		"Item Group",
		fields=["name", "item_group_name", "parent_item_group", "is_group", "image"],
		order_by="lft asc",
		ignore_permissions=True,
	)
	out = []
	for row in rows:
		parent = cstr(row.parent_item_group or "").strip()
		path_parent = "" if parent in ("", "All Item Groups") else parent
		out.append(
			{
				"name": row.name,
				"item_group_name": row.item_group_name or row.name,
				"parent_item_group": parent,
				"is_group": cint(row.is_group),
				"image": row.image,
				"path": _item_group_path(path_parent, row.name),
			}
		)
	return out


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
		"for_price_list",
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
		ignore_permissions=True,
	)

	# Filter by date validity
	rules = [
		r for r in rules
		if (not r.get("valid_from") or getdate(r["valid_from"]) <= getdate(today))
		and (not r.get("valid_upto") or getdate(r["valid_upto"]) >= getdate(today))
	]

	# Empty / * for_price_list = all selling lists
	if price_list:
		rules = [r for r in rules if _price_list_applies(r.get("for_price_list"), price_list)]

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
				ignore_permissions=True,
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


def _pricing_rule_field_list():
	"""Shared field list for Pricing Rule list/get (includes disable + optional custom fields)."""
	has_time_fields = frappe.db.has_column("Pricing Rule", "happy_hour_from")
	has_days_field = frappe.db.has_column("Pricing Rule", "applicable_days")
	has_flash_field = frappe.db.has_column("Pricing Rule", "flash_sale")
	base_fields = [
		"name",
		"title",
		"disable",
		"selling",
		"apply_on",
		"price_or_product_discount",
		"min_qty",
		"max_qty",
		"min_amt",
		"max_amt",
		"valid_from",
		"valid_upto",
		"rate_or_discount",
		"discount_percentage",
		"discount_amount",
		"rate",
		"same_item",
		"free_item",
		"free_qty",
		"free_item_rate",
		"is_recursive",
		"recurse_for",
		"threshold_percentage",
		"rule_description",
		"for_price_list",
		"currency",
		"company",
		"priority",
	]
	if has_time_fields:
		base_fields += ["happy_hour_from", "happy_hour_to"]
	if has_days_field:
		base_fields += ["applicable_days"]
	if has_flash_field:
		base_fields += ["flash_sale"]
	return base_fields


def _attach_pricing_rule_children(rules):
	"""Attach applicable_items / groups / brands arrays onto Pricing Rule dicts."""
	if not rules:
		return rules
	rule_names = [r["name"] for r in rules]
	child_tables = [
		("Pricing Rule Item Code", "item_code", "applicable_items"),
		("Pricing Rule Item Group", "item_group", "applicable_groups"),
		("Pricing Rule Brand", "brand", "applicable_brands"),
	]
	for child_dt, field, key in child_tables:
		try:
			rows = frappe.get_all(
				child_dt,
				filters={"parent": ["in", rule_names]},
				fields=["parent", field],
				ignore_permissions=True,
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


def _default_pricing_currency():
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	if company:
		currency = frappe.db.get_value("Company", company, "default_currency")
		if currency:
			return currency, company
	currency = frappe.db.get_single_value("Global Defaults", "default_currency")
	return currency or "ARS", company


def _parse_target_list(raw):
	if raw is None:
		return []
	if isinstance(raw, str):
		parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
		return [p for p in parts if p]
	if isinstance(raw, (list, tuple)):
		return [str(x).strip() for x in raw if str(x).strip()]
	return []


_ALL_MARKERS = {"*", "ALL", "TODOS"}


def _is_all_marker(value):
	return str(value or "").strip().upper() in _ALL_MARKERS


def _is_all_targets(values):
	return any(_is_all_marker(v) for v in (values or []))


def _normalize_price_list(value):
	pl = (value or "").strip() if isinstance(value, str) else (str(value).strip() if value else "")
	if not pl or _is_all_marker(pl):
		return None
	return pl


def _price_list_applies(rule_pl, current_pl):
	"""Empty or * on the rule = every selling list. Empty current filter = no restriction."""
	rule_pl = _normalize_price_list(rule_pl)
	current_pl = _normalize_price_list(current_pl)
	if not rule_pl:
		return True
	if not current_pl:
		return True
	return rule_pl == current_pl


def _selling_price_lists():
	return (
		frappe.get_all(
			"Price List",
			filters={"selling": 1, "enabled": 1},
			pluck="name",
			ignore_permissions=True,
		)
		or []
	)


@frappe.whitelist()
def list_pricing_rules(include_disabled=1, price_list=None):
	"""
	Admin list of selling-side Pricing Rules (no date/happy-hour filtering).
	Includes disabled rules when include_disabled=1.
	"""
	filters = {"selling": 1}
	if not cint(include_disabled):
		filters["disable"] = 0

	rules = frappe.get_all(
		"Pricing Rule",
		filters=filters,
		fields=_pricing_rule_field_list(),
		order_by="modified desc",
		ignore_permissions=True,
	)
	if price_list:
		rules = [r for r in rules if _price_list_applies(r.get("for_price_list"), price_list)]

	_attach_pricing_rule_children(rules)
	return {"rules": rules, "total_count": len(rules)}


@frappe.whitelist()
def get_pricing_rule(name):
	"""Return one Pricing Rule with child targets for the admin editor."""
	if not name or not frappe.db.exists("Pricing Rule", name):
		frappe.throw(_("Pricing Rule not found"), frappe.DoesNotExistError)
	fields = _pricing_rule_field_list()
	row = frappe.db.get_value("Pricing Rule", name, fields, as_dict=True)
	_attach_pricing_rule_children([row])
	return row


@frappe.whitelist()
@idempotent_request
def save_pricing_rule(data):
	"""
	Create or update a selling Pricing Rule.
	data: JSON object with title, apply_on, discount fields, vigencia, targets, disable, etc.
	"""
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = frappe._dict(data or {})

	title = (data.get("title") or "").strip()
	if not title:
		frappe.throw(_("Title is required"))

	apply_on = data.get("apply_on") or "Item Code"
	if apply_on not in ("Item Code", "Item Group", "Brand", "Transaction"):
		frappe.throw(_("Invalid apply_on"))

	price_or_product = data.get("price_or_product_discount") or "Price"
	if price_or_product not in ("Price", "Product"):
		frappe.throw(_("Invalid price_or_product_discount"))

	currency, company = _default_pricing_currency()
	currency = data.get("currency") or currency
	company = data.get("company") or company

	name = (data.get("name") or "").strip() or None
	promo_sku = (data.get("promo_sku") or "").strip() or None
	existing_name = name if name and frappe.db.exists("Pricing Rule", name) else None
	is_new = not existing_name
	set_name = None
	if is_new:
		set_name = promo_sku or (name if name else None)
		if set_name and frappe.db.exists("Pricing Rule", set_name):
			frappe.throw(_("A pricing rule with code {0} already exists").format(set_name))
	else:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Pricing Rule", existing_name)
		frappe.flags.ignore_permissions = False

	doc = frappe.new_doc("Pricing Rule") if is_new else doc

	doc.title = title
	doc.selling = 1
	doc.buying = cint(data.get("buying") or 0)
	doc.disable = cint(data.get("disable") or 0)
	doc.apply_on = apply_on
	doc.price_or_product_discount = price_or_product
	doc.currency = currency
	if company:
		doc.company = company
	doc.for_price_list = _normalize_price_list(data.get("for_price_list"))
	doc.rate_or_discount = data.get("rate_or_discount") or "Discount Percentage"
	doc.discount_percentage = flt(data.get("discount_percentage") or 0)
	doc.discount_amount = flt(data.get("discount_amount") or 0)
	doc.rate = flt(data.get("rate") or 0)
	doc.min_qty = flt(data.get("min_qty") or 0)
	doc.max_qty = flt(data.get("max_qty") or 0)
	doc.min_amt = flt(data.get("min_amt") or 0)
	doc.max_amt = flt(data.get("max_amt") or 0)
	doc.valid_from = data.get("valid_from") or None
	doc.valid_upto = data.get("valid_upto") or None
	doc.rule_description = data.get("rule_description") or None
	doc.threshold_percentage = flt(data.get("threshold_percentage") or 0)
	doc.same_item = cint(data.get("same_item") or 0)
	doc.free_item = data.get("free_item") or None
	doc.free_qty = flt(data.get("free_qty") or 0)
	doc.free_item_rate = flt(data.get("free_item_rate") or 0)
	doc.is_recursive = cint(data.get("is_recursive") or 0)
	doc.recurse_for = flt(data.get("recurse_for") or 0)
	if data.get("priority"):
		doc.has_priority = 1
		doc.priority = str(data.get("priority"))

	# Optional custom scheduling fields
	if frappe.db.has_column("Pricing Rule", "happy_hour_from"):
		doc.happy_hour_from = data.get("happy_hour_from") or None
		doc.happy_hour_to = data.get("happy_hour_to") or None
	if frappe.db.has_column("Pricing Rule", "applicable_days"):
		doc.applicable_days = data.get("applicable_days") or None
	if frappe.db.has_column("Pricing Rule", "flash_sale"):
		doc.flash_sale = cint(data.get("flash_sale") or 0)

	# Rebuild child targets. "*" / ALL / TODOS = every product → apply_on Transaction.
	doc.set("items", [])
	doc.set("item_groups", [])
	doc.set("brands", [])

	items = _parse_target_list(data.get("applicable_items"))
	groups = _parse_target_list(data.get("applicable_groups"))
	brands = _parse_target_list(data.get("applicable_brands"))

	if apply_on == "Item Code" and _is_all_targets(items):
		apply_on = "Transaction"
		doc.apply_on = "Transaction"
	elif apply_on == "Item Group" and _is_all_targets(groups):
		apply_on = "Transaction"
		doc.apply_on = "Transaction"

	# Empty Item Code targets used to hit ERPNext's cryptic
	# "Item Code is not added in the table".
	if apply_on == "Item Code" and not items:
		frappe.throw(_("Add at least one Item Code in Targets, or * for all products"))
	elif apply_on == "Item Group" and not groups:
		frappe.throw(_("Add at least one Item Group in Targets, or * for all products"))
	elif apply_on == "Brand" and not brands:
		frappe.throw(_("Add at least one Brand in Targets"))

	if apply_on == "Item Code":
		for code in items:
			if not _is_all_marker(code):
				doc.append("items", {"item_code": code})
	elif apply_on == "Item Group":
		for g in groups:
			if not _is_all_marker(g):
				doc.append("item_groups", {"item_group": g})
	elif apply_on == "Brand":
		for b in brands:
			if not _is_all_marker(b):
				doc.append("brands", {"brand": b})

	if is_new:
		if set_name:
			doc.insert(ignore_permissions=True, set_name=set_name)
		else:
			doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	frappe.db.commit()

	row = frappe.db.get_value("Pricing Rule", doc.name, _pricing_rule_field_list(), as_dict=True)
	_attach_pricing_rule_children([row])
	return {"ok": True, "name": doc.name, "rule": row}


@frappe.whitelist()
def set_pricing_rule_disabled(name, disabled=1):
	"""Enable/disable a Pricing Rule (disable=1 means inactive)."""
	if not name or not frappe.db.exists("Pricing Rule", name):
		frappe.throw(_("Pricing Rule not found"), frappe.DoesNotExistError)
	frappe.db.set_value("Pricing Rule", name, "disable", cint(disabled))
	frappe.db.commit()
	return {"ok": True, "name": name, "disable": cint(disabled)}


@frappe.whitelist()
def delete_pricing_rule(name):
	"""Permanently delete a Pricing Rule."""
	if not name or not frappe.db.exists("Pricing Rule", name):
		frappe.throw(_("Pricing Rule not found"), frappe.DoesNotExistError)
	frappe.delete_doc("Pricing Rule", name, ignore_permissions=True, force=1)
	frappe.db.commit()
	return {"ok": True, "name": name}


_bundle_fields_ready = False


def ensure_product_bundle_promo_fields():
	"""
	Custom vigencia + price-list fields on Product Bundle.
	Empty custom_for_price_list = applies to every selling list.
	Idempotent — create_custom_fields skips existing fields.
	"""
	global _bundle_fields_ready
	if _bundle_fields_ready and frappe.db.has_column("Product Bundle", "custom_valid_from"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Product Bundle": [
				{
					"fieldname": "custom_valid_from",
					"fieldtype": "Date",
					"label": "Valid From",
					"insert_after": "disabled",
				},
				{
					"fieldname": "custom_valid_upto",
					"fieldtype": "Date",
					"label": "Valid Upto",
					"insert_after": "custom_valid_from",
				},
				{
					"fieldname": "custom_for_price_list",
					"fieldtype": "Link",
					"options": "Price List",
					"label": "For Price List",
					"insert_after": "custom_valid_upto",
				},
			]
		},
		ignore_validate=True,
	)
	frappe.clear_cache(doctype="Product Bundle")
	_bundle_fields_ready = True


def _bundle_in_vigencia(row_or_doc):
	today = getdate(nowdate())
	vf = row_or_doc.get("valid_from") or row_or_doc.get("custom_valid_from")
	vu = row_or_doc.get("valid_upto") or row_or_doc.get("custom_valid_upto")
	if vf and getdate(vf) > today:
		return False
	if vu and getdate(vu) < today:
		return False
	return True


def _bundle_price(item_code, price_list=None):
	pl = price_list or frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
	return (
		frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": pl, "selling": 1},
			"price_list_rate",
		)
		or 0
	)


def _serialize_product_bundle(name, price_list=None):
	ensure_product_bundle_promo_fields()
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Product Bundle", name)
	frappe.flags.ignore_permissions = False
	item = frappe.db.get_value(
		"Item",
		doc.new_item_code,
		["item_name", "disabled", "image"],
		as_dict=True,
	) or {}
	pl = price_list or frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
	components = []
	components_total = 0.0
	for row in doc.items:
		rate = flt(get_item_price(row.item_code, pl))
		qty = flt(row.qty)
		amount = rate * qty
		components_total += amount
		comp_meta = frappe.db.get_value(
			"Item", row.item_code, ["item_name", "disabled"], as_dict=True
		) or {}
		components.append(
			{
				"item_code": row.item_code,
				"qty": qty,
				"item_name": comp_meta.get("item_name") or row.item_code,
				"uom": row.uom,
				"rate": rate,
				"amount": amount,
				"disabled": cint(comp_meta.get("disabled")),
			}
		)
	bundle_price = flt(_bundle_price(doc.new_item_code, pl))
	discount_amount = max(0.0, components_total - bundle_price) if components_total else 0.0
	discount_pct = (discount_amount / components_total * 100.0) if components_total else 0.0
	for_price_list = _normalize_price_list(doc.get("custom_for_price_list"))
	valid_from = doc.get("custom_valid_from")
	valid_upto = doc.get("custom_valid_upto")
	return {
		"name": doc.name,
		"new_item_code": doc.new_item_code,
		"description": doc.description,
		"disabled": cint(doc.disabled),
		"bundle_name": item.get("item_name") or doc.new_item_code,
		"item_disabled": cint(item.get("disabled")),
		"image": item.get("image"),
		"bundle_price": bundle_price,
		"components_total": components_total,
		"estimated_discount_amount": discount_amount,
		"estimated_discount_pct": discount_pct,
		"components": components,
		"valid_from": str(valid_from) if valid_from else None,
		"valid_upto": str(valid_upto) if valid_upto else None,
		"for_price_list": for_price_list,
	}


@frappe.whitelist()
def list_product_bundles(include_disabled=1, price_list=None):
	"""Admin list of Product Bundle docs with components and selling price."""
	filters = {}
	if not cint(include_disabled):
		filters["disabled"] = 0
	names = frappe.get_all(
		"Product Bundle",
		filters=filters,
		pluck="name",
		order_by="modified desc",
		ignore_permissions=True,
	)
	bundles = [_serialize_product_bundle(n, price_list) for n in names]
	if not cint(include_disabled):
		bundles = [
			b
			for b in bundles
			if _bundle_in_vigencia(b) and _price_list_applies(b.get("for_price_list"), price_list)
		]
	elif price_list:
		# Admin view: still show packs that apply to this list (including all-lists).
		bundles = [b for b in bundles if _price_list_applies(b.get("for_price_list"), price_list)]
	return {"bundles": bundles, "total_count": len(bundles)}


@frappe.whitelist()
def get_product_bundle(name, price_list=None):
	"""Return one Product Bundle with components."""
	if not name or not frappe.db.exists("Product Bundle", name):
		frappe.throw(_("Product Bundle not found"), frappe.DoesNotExistError)
	return _serialize_product_bundle(name, price_list)


@frappe.whitelist()
@idempotent_request
def save_product_bundle(data):
	"""
	Create or update a Product Bundle.
	data: {
	  name?, new_item_code, description?, disabled?,
	  bundle_price?, price_list?,
	  items: [{item_code, qty}, ...]
	}
	Ensures parent Item exists as non-stock; upserts Item Price when bundle_price set.
	"""
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = frappe._dict(data or {})

	new_item_code = (data.get("new_item_code") or data.get("name") or "").strip()
	if not new_item_code:
		frappe.throw(_("Bundle SKU (new_item_code) is required"))

	items_raw = data.get("items") or data.get("components") or []
	if isinstance(items_raw, str):
		items_raw = frappe.parse_json(items_raw) or []
	components = []
	for row in items_raw:
		code = (row.get("item_code") or "").strip()
		if not code:
			continue
		qty = flt(row.get("qty") or 1)
		if qty <= 0:
			frappe.throw(_("Component qty must be > 0 for {0}").format(code))
		if not frappe.db.exists("Item", code):
			frappe.throw(_("Item {0} not found").format(code))
		components.append({"item_code": code, "qty": qty})

	if not components:
		frappe.throw(_("Add at least one component item"))

	# Ensure parent Item (non-stock) exists
	if not frappe.db.exists("Item", new_item_code):
		item_group = (
			frappe.db.get_single_value("Stock Settings", "item_group")
			or (frappe.db.exists("Item Group", "Products") and "Products")
			or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			or "All Item Groups"
		)
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": new_item_code,
				"item_name": (data.get("bundle_name") or data.get("description") or new_item_code)[:140],
				"item_group": item_group,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"include_item_in_manufacturing": 0,
				"disabled": 0,
			}
		)
		item.insert(ignore_permissions=True)
	else:
		# Parent must not be stock item for Product Bundle
		if cint(frappe.db.get_value("Item", new_item_code, "is_stock_item")):
			frappe.db.set_value("Item", new_item_code, "is_stock_item", 0)
		if data.get("bundle_name"):
			frappe.db.set_value("Item", new_item_code, "item_name", str(data.get("bundle_name"))[:140])

	ensure_product_bundle_promo_fields()

	stored_pl = _normalize_price_list(data.get("price_list") or data.get("for_price_list"))
	price_lists_to_write = [stored_pl] if stored_pl else _selling_price_lists()
	# Fallback if no selling lists exist
	if not price_lists_to_write:
		price_lists_to_write = [
			frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
		]

	if data.get("bundle_price") is not None and data.get("bundle_price") != "":
		rate = flt(data.get("bundle_price"))
		allowed = set(price_lists_to_write)
		existing_prices = frappe.get_all(
			"Item Price",
			filters={"item_code": new_item_code, "selling": 1},
			fields=["name", "price_list"],
			ignore_permissions=True,
		)
		for row in existing_prices:
			if stored_pl and row.price_list not in allowed:
				frappe.delete_doc("Item Price", row.name, ignore_permissions=True, force=1)
		for pl in price_lists_to_write:
			existing = frappe.db.get_value(
				"Item Price",
				{"item_code": new_item_code, "price_list": pl, "selling": 1},
				"name",
			)
			if existing:
				frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
			else:
				frappe.get_doc(
					{
						"doctype": "Item Price",
						"item_code": new_item_code,
						"price_list": pl,
						"selling": 1,
						"price_list_rate": rate,
					}
				).insert(ignore_permissions=True)

	exists = frappe.db.exists("Product Bundle", new_item_code)
	valid_from = data.get("valid_from") or None
	valid_upto = data.get("valid_upto") or None
	if exists:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Product Bundle", new_item_code)
		frappe.flags.ignore_permissions = False
		doc.description = data.get("description") or doc.description
		doc.disabled = cint(data.get("disabled") or 0)
		doc.custom_valid_from = valid_from
		doc.custom_valid_upto = valid_upto
		doc.custom_for_price_list = stored_pl
		doc.set("items", [])
		for c in components:
			doc.append("items", c)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": new_item_code,
				"description": data.get("description") or "",
				"disabled": cint(data.get("disabled") or 0),
				"custom_valid_from": valid_from,
				"custom_valid_upto": valid_upto,
				"custom_for_price_list": stored_pl,
				"items": components,
			}
		)
		doc.insert(ignore_permissions=True)

	serialize_pl = stored_pl or (price_lists_to_write[0] if price_lists_to_write else None)
	frappe.db.commit()
	return {"ok": True, "name": doc.name, "bundle": _serialize_product_bundle(doc.name, serialize_pl)}


@frappe.whitelist()
def delete_product_bundle(name):
	"""Delete a Product Bundle (parent Item is kept)."""
	if not name or not frappe.db.exists("Product Bundle", name):
		frappe.throw(_("Product Bundle not found"), frappe.DoesNotExistError)
	frappe.delete_doc("Product Bundle", name, ignore_permissions=True, force=1)
	frappe.db.commit()
	return {"ok": True, "name": name}


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


def _bogo_nxm_label(min_qty, free_qty):
	take = cint(min_qty)
	free = cint(free_qty)
	if take <= 0 or free <= 0 or free >= take:
		return None
	return f"{take}x{take - free}"


def _rule_applies_to_item(rule, item_code, item_group="", brand=""):
	apply_on = rule.get("apply_on") or ""
	if apply_on == "Transaction":
		return True
	if apply_on == "Item Code":
		items = rule.get("applicable_items") or []
		if _is_all_targets(items):
			return True
		return item_code in items
	if apply_on == "Item Group":
		groups = rule.get("applicable_groups") or []
		if _is_all_targets(groups):
			return True
		return bool(item_group) and item_group in groups
	if apply_on == "Brand":
		return bool(brand) and brand in (rule.get("applicable_brands") or [])
	return False


def _put_line_discount(line_map, item, discount_amount, rule_name, free_qty=0, label=""):
	qty = flt(item.get("qty") or 0)
	amount = flt(item.get("amount") or 0)
	rate = flt(item.get("rate") or 0)
	item_code = item.get("item_code")
	discount_amount = min(flt(discount_amount), amount)
	if discount_amount <= 0 or not item_code:
		return
	prev = line_map.get(item_code)
	if prev and flt(prev.get("discount_amount") or 0) >= discount_amount:
		return
	discounted_amount = max(0, amount - discount_amount)
	line_map[item_code] = {
		"item_code": item_code,
		"discount_percentage": (discount_amount / amount * 100.0) if amount else 0,
		"discounted_rate": (discounted_amount / qty) if qty else rate,
		"discount_amount": discount_amount,
		"free_item": item_code if free_qty else None,
		"free_qty": flt(free_qty or 0),
		"rule_name": rule_name or "",
		"label": label or "",
	}


def _compute_cart_promotions_local(items, price_list=None):
	"""NxM 3x2 + % price rules + product-bundle packs. Used by apply_cart_promotions."""
	active_rules = get_active_promotions(price_list=price_list)
	line_map = {}
	upsell_hints = []
	applied_bundles = []
	remaining = {i["item_code"]: flt(i.get("qty") or 0) for i in items}
	by_code = {i["item_code"]: i for i in items}

	for item in items:
		item_code = item.get("item_code")
		qty = flt(item.get("qty") or 0)
		rate = flt(item.get("rate") or 0)
		best = None
		for rule in active_rules:
			if (rule.get("price_or_product_discount") or "") != "Product":
				continue
			if not rule.get("same_item"):
				continue
			if not _rule_applies_to_item(rule, item_code):
				continue
			min_qty = flt(rule.get("min_qty") or 0)
			free_qty = flt(rule.get("free_qty") or 0)
			if min_qty <= 0 or free_qty <= 0:
				continue
			label = _bogo_nxm_label(min_qty, free_qty) or "PROMO"
			packs = int(qty // min_qty) if min_qty else 0
			if packs < 1:
				needed = min_qty - qty
				thresh = flt(rule.get("threshold_percentage") or 80)
				progress = (qty / min_qty) * 100 if min_qty else 0
				if needed > 0 and progress >= thresh:
					upsell_hints.append({
						"rule_name": rule["name"],
						"title": rule.get("title") or rule["name"],
						"message": f"Agregá {int(needed)} más para aplicar {label}",
						"items_needed": [item_code],
						"qty_needed": needed,
						"progress_pct": min(progress, 99),
					})
				continue
			free = min(qty, packs * free_qty)
			discount = free * rate
			if best is None or discount > best["discount"]:
				best = {"rule": rule, "free": free, "discount": discount, "label": label, "min_qty": min_qty}
		if best:
			_put_line_discount(
				line_map, item, best["discount"], best["rule"]["name"],
				free_qty=best["free"], label=best["label"],
			)
			consumed = int(qty // best["min_qty"]) * best["min_qty"]
			remaining[item_code] = max(0, qty - consumed)

	for item in items:
		item_code = item.get("item_code")
		if item_code in line_map:
			continue
		qty = flt(item.get("qty") or 0)
		amount = flt(item.get("amount") or 0)
		best_discount = 0
		best_rule = None
		for rule in active_rules:
			if (rule.get("price_or_product_discount") or "") != "Price":
				continue
			if not _rule_applies_to_item(rule, item_code):
				continue
			min_qty = flt(rule.get("min_qty") or 0)
			min_amt = flt(rule.get("min_amt") or 0)
			promo_style = _promo_style_from_rule(rule)
			rod = (rule.get("rate_or_discount") or "").strip().lower()
			is_rate_rule = rod == "rate" or (
				flt(rule.get("rate") or 0) > 0
				and not flt(rule.get("discount_percentage") or 0)
				and not flt(rule.get("discount_amount") or 0)
			)
			# Pack Rate: upsell toward next complete pack (not "all units once N").
			if is_rate_rule and promo_style == "pack" and min_qty > 1:
				packs = int(qty // min_qty) if min_qty else 0
				if packs < 1:
					needed = min_qty - qty
					thresh = flt(rule.get("threshold_percentage") or 80)
					progress = (qty / min_qty) * 100 if min_qty else 0
					offer = flt(rule.get("rate") or 0)
					if needed > 0 and progress >= thresh:
						upsell_hints.append({
							"rule_name": rule["name"],
							"title": rule.get("title") or rule["name"],
							"message": f"Agregá {int(needed)} más para {cint(min_qty)}x @ ${offer:g}",
							"items_needed": [item_code],
							"qty_needed": needed,
							"progress_pct": min(progress, 99),
						})
					continue
			elif min_qty > 0 and qty < min_qty:
				needed = min_qty - qty
				thresh = flt(rule.get("threshold_percentage") or 80)
				progress = (qty / min_qty) * 100 if min_qty else 0
				if needed > 0 and progress >= thresh:
					pct = flt(rule.get("discount_percentage") or 0)
					offer = flt(rule.get("rate") or 0)
					if pct:
						disc_label = f"{int(pct)}% OFF"
					elif offer > 0:
						disc_label = f"oferta ${offer:g}"
					else:
						disc_label = rule.get("title") or "descuento"
					upsell_hints.append({
						"rule_name": rule["name"],
						"title": rule.get("title") or rule["name"],
						"message": f"Agregá {int(needed)} más para {disc_label}",
						"items_needed": [item_code],
						"qty_needed": needed,
						"progress_pct": min(progress, 99),
					})
				continue
			if min_amt > 0 and amount < min_amt:
				continue
			discount = 0
			if flt(rule.get("discount_percentage") or 0) > 0:
				discount = amount * flt(rule.get("discount_percentage")) / 100.0
			elif flt(rule.get("discount_amount") or 0) > 0:
				discount = flt(rule.get("discount_amount"))
			elif is_rate_rule:
				# Fixed unit rate (Airtable Oferta): pack or threshold savings.
				unit_rate = flt(item.get("rate") or 0)
				offer = flt(rule.get("rate") or 0)
				discount = _rate_rule_discount(unit_rate, offer, qty, min_qty, promo_style)
			if discount > best_discount:
				best_discount = discount
				best_rule = rule
		if best_rule and best_discount > 0:
			pct = flt(best_rule.get("discount_percentage") or 0)
			offer = flt(best_rule.get("rate") or 0)
			style = _promo_style_from_rule(best_rule)
			mq = flt(best_rule.get("min_qty") or 0)
			if pct:
				label = f"{int(pct)}%"
			elif offer > 0 and style == "pack" and mq > 1:
				label = f"{cint(mq)}x ${offer:g}"
			elif offer > 0:
				label = f"${offer:g}"
			else:
				label = "PROMO"
			_put_line_discount(
				line_map, item, best_discount, best_rule["name"],
				label=label,
			)

	try:
		bundle_names = frappe.get_all(
			"Product Bundle",
			filters={"disabled": 0},
			pluck="name",
			ignore_permissions=True,
		)
	except Exception:
		bundle_names = []

	parent_skus = {i["item_code"] for i in items}
	candidates = []
	for bname in bundle_names:
		try:
			frappe.flags.ignore_permissions = True
			row = _serialize_product_bundle(bname, price_list)
			frappe.flags.ignore_permissions = False
		except Exception:
			frappe.flags.ignore_permissions = False
			continue
		if cint(row.get("item_disabled")):
			continue
		if not _bundle_in_vigencia(row):
			continue
		if not _price_list_applies(row.get("for_price_list"), price_list):
			continue
		sku = row.get("new_item_code")
		if sku in parent_skus:
			continue
		bundle_price = flt(row.get("bundle_price") or 0)
		components = row.get("components") or []
		if bundle_price <= 0 or not components:
			continue
		if any(flt(c.get("qty") or 0) <= 0 for c in components):
			continue
		# Pack cannot be applied when a component Item is disabled (SI submit fails).
		if any(cint(c.get("disabled")) for c in components):
			continue
		packs = min(int(remaining.get(c["item_code"], 0) // flt(c["qty"])) for c in components)
		if packs < 1:
			continue
		pack_list = 0.0
		for c in components:
			line = by_code.get(c["item_code"])
			unit = flt(line.get("rate") if line else 0) or flt(c.get("rate") or 0)
			pack_list += unit * flt(c["qty"])
		savings = (pack_list - bundle_price) * packs
		if savings <= 0:
			continue
		candidates.append({
			"sku": sku,
			"name": row.get("bundle_name") or sku,
			"price": bundle_price,
			"components": components,
			"packs": packs,
			"pack_list": pack_list,
			"savings": savings,
		})

	candidates.sort(key=lambda c: c["savings"], reverse=True)
	for cand in candidates:
		still = min(
			cand["packs"],
			min(int(remaining.get(c["item_code"], 0) // flt(c["qty"])) for c in cand["components"]),
		)
		if still < 1:
			continue
		savings_per_pack = cand["pack_list"] - cand["price"]
		savings = savings_per_pack * still
		applied_bundles.append({
			"bundle_item_code": cand["sku"],
			"bundle_name": cand["name"],
			"packs": still,
			"savings": savings,
		})
		for c in cand["components"]:
			line = by_code.get(c["item_code"])
			if not line:
				continue
			unit = flt(line.get("rate") or 0) or flt(c.get("rate") or 0)
			share = (unit * flt(c["qty"]) / cand["pack_list"]) if cand["pack_list"] else 0
			item_savings = savings_per_pack * still * share
			prev = flt((line_map.get(c["item_code"]) or {}).get("discount_amount") or 0)
			_put_line_discount(
				line_map, line, prev + item_savings, cand["sku"], label="PACK",
			)
			remaining[c["item_code"]] = max(
				0, remaining.get(c["item_code"], 0) - flt(c["qty"]) * still
			)

	return {
		"line_discounts": list(line_map.values()),
		"upsell_hints": upsell_hints[:3],
		"applied_bundles": applied_bundles,
	}


@frappe.whitelist(allow_guest=True)
def apply_cart_promotions(items, price_list=None):
	"""
	Given cart items [{item_code, qty, rate, amount}], return computed discounts
	and upsell hints using ERPNext Pricing Rules and Product Bundles.

	3x2 (same_item Product discount) is applied as "Llevá N, pagás N-free":
	adding min_qty units grants free_qty free units.

	Returns:
		{
			line_discounts: [{item_code, discount_percentage, discounted_rate,
			                  discount_amount, free_item, free_qty, rule_name, label}],
			upsell_hints:   [{rule_name, title, message, items_needed,
			                  qty_needed, progress_pct}],
			applied_bundles: [{bundle_item_code, bundle_name, packs, savings}]
		}
	"""
	import json

	if isinstance(items, str):
		items = json.loads(items)

	if not items:
		return {"line_discounts": [], "upsell_hints": [], "applied_bundles": []}

	computed = _compute_cart_promotions_local(items, price_list=price_list)
	line_map = {d["item_code"]: d for d in computed["line_discounts"]}

	# Merge ERPNext % Price rules that apply_on Item Group / Brand (local engine
	# only matches Item Code unless group/brand is on the cart line).
	today = nowdate()
	for item in items:
		if item.get("item_code") in line_map:
			continue
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
				line_map[item["item_code"]] = {
					"item_code": item["item_code"],
					"discount_percentage": disc_pct,
					"discounted_rate": disc_rate,
					"discount_amount": (base - disc_rate) * flt(item["qty"]),
					"free_item": rd.get("free_item"),
					"free_qty": flt(rd.get("free_qty") or 0),
					"rule_name": rd.get("pricing_rule") or "",
					"label": f"{int(disc_pct)}%",
				}
		except Exception:
			continue  # Pricing rule errors are non-fatal

	# Amount-based upsell (cart-level min_amt)
	active_rules = get_active_promotions(price_list=price_list)
	total_amount = sum(flt(i.get("amount", 0)) for i in items)
	upsell_hints = list(computed["upsell_hints"])
	for rule in active_rules:
		if flt(rule.get("min_amt") or 0) <= 0:
			continue
		min_a = flt(rule["min_amt"])
		needed_amt = min_a - total_amount
		thresh = flt(rule.get("threshold_percentage") or 80)
		progress = (total_amount / min_a) * 100 if min_a else 0
		if 0 < needed_amt and progress >= thresh:
			disc_label = (
				f"{rule.get('discount_percentage', '')}% off"
				if rule.get("discount_percentage")
				else "un descuento"
			)
			upsell_hints.append({
				"rule_name": rule["name"],
				"title": rule.get("title") or rule["name"],
				"message": f"Agregá ${needed_amt:.2f} más para {disc_label}",
				"items_needed": [],
				"qty_needed": 0,
				"progress_pct": min(progress, 99),
			})

	return {
		"line_discounts": list(line_map.values()),
		"upsell_hints": upsell_hints[:3],
		"applied_bundles": computed.get("applied_bundles") or [],
	}


@frappe.whitelist(allow_guest=True)
def get_promotions_for_item(item_code, price_list=None):
	"""
	Return all promotions relevant to a specific item:
	  - pricing_rules: active Pricing Rules targeting this item (by code, group, or brand)
	  - bundles: Product Bundles that contain this item as a component

	Used by the POS Promotion Panel when a cashier taps the "Promos" chip on a product card.
	"""
	pricing_rules = _get_pricing_rules_for_item(item_code, price_list=price_list)
	bundles = _get_bundles_containing_item(item_code, price_list=price_list)
	return {"pricing_rules": pricing_rules, "bundles": bundles}


def _get_pricing_rules_for_item(item_code, price_list=None):
	"""Return active Pricing Rules that apply to this item (direct, group, brand, or all)."""
	item = frappe.db.get_value("Item", item_code, ["item_group", "brand"], as_dict=True)
	if not item:
		return []

	item_group = item.get("item_group") or ""
	brand = item.get("brand") or ""

	all_rules = get_active_promotions(price_list=price_list)
	matching = []

	for rule in all_rules:
		if _rule_applies_to_item(rule, item_code, item_group, brand):
			matching.append(rule)

	return matching


def _get_bundles_containing_item(item_code, price_list=None):
	"""Return Product Bundles that have this item as a component, with full component list and price."""
	bundle_rows = frappe.get_all(
		"Product Bundle Item",
		filters={"item_code": item_code},
		fields=["parent"],
		ignore_permissions=True,
	)
	if not bundle_rows:
		return []

	bundle_skus = list({r["parent"] for r in bundle_rows})
	result = []

	for bundle_sku in bundle_skus:
		try:
			row = _serialize_product_bundle(bundle_sku, price_list)
		except Exception:
			continue
		if cint(row.get("disabled")) or cint(row.get("item_disabled")):
			continue
		if not _bundle_in_vigencia(row):
			continue
		if not _price_list_applies(row.get("for_price_list"), price_list):
			continue
		components = row.get("components") or []
		if any(cint(c.get("disabled")) for c in components):
			continue
		result.append({
			"bundle_item_code": row["new_item_code"],
			"bundle_name": row.get("bundle_name") or row["new_item_code"],
			"bundle_price": flt(row.get("bundle_price") or 0),
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
	"""Manager PIN for cashier discounts. If no PIN is set, discounts stay open."""
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
		_pin_configured,
		validate_admin_pin,
	)

	if not _pin_configured():
		return {"authorized": True, "pin_configured": False}
	return validate_admin_pin(pin=pin)


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
	from erpnext.erpnext_integrations.ecommerce_api.item_pricing import effective_item_price

	# Same row ERPNext prices with: valid today, latest valid_from; a customer
	# price wins over the general one when both exist.
	row = None
	if customer:
		row = effective_item_price(item_code, price_list, uom=uom, customer=customer)
	row = row or effective_item_price(item_code, price_list, uom=uom)
	return flt(row.price_list_rate) if row else 0.0


@frappe.whitelist(allow_guest=True)
def get_item_prices_bulk(item_codes, price_list=None, price_lists=None):
	"""Batch item-price lookup.

	Pass price_list (single name, e.g. Efectivo) for POS checkout — returns
	{item_code: price_list_rate} for items that have a row in that list.

	Pass price_lists (JSON-encoded list, e.g. ["Efectivo", "Transferencia"])
	for the catalog's optional payment-method price display — returns
	{item_code: {price_list_name: rate}} covering whichever of the requested
	lists each item actually has a price in.
	"""
	import json

	def _as_list(raw):
		# JSON list from the client; a bare / comma-separated string from dirty callers.
		if not isinstance(raw, str):
			return raw
		try:
			parsed = json.loads(raw)
		except ValueError:
			return [part.strip() for part in raw.split(",")]
		return parsed if isinstance(parsed, list) else [parsed]

	item_codes = [c for c in (_as_list(item_codes) or []) if c and isinstance(c, str)]
	if not item_codes:
		return {}

	if price_lists:
		price_lists = _as_list(price_lists)
		price_lists = [pl for pl in (price_lists or []) if pl]
		if not price_lists:
			return {}
		from erpnext.erpnext_integrations.ecommerce_api.product_manager import _selling_prices_map

		full_map = _selling_prices_map(item_codes)
		out = {}
		for code, prices in full_map.items():
			filtered = {pl: prices[pl] for pl in price_lists if pl in prices}
			if filtered:
				out[code] = filtered
		return out

	if not price_list:
		return {}
	out = {}
	for code in item_codes:
		rate = get_item_price(code, price_list)
		if rate:
			out[code] = rate
	return out


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

	from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
		require_valuation_rate,
	)

	if not require_valuation_rate():
		for row in stock_entry.items:
			row.allow_zero_valuation_rate = 1

	stock_entry.insert(ignore_permissions=True)
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


CONSUMIDOR_FINAL_NAME = "Consumidor Final"
UNCATEGORIZED_CUSTOMER_NAME = "Uncategorized"
UNCATEGORIZED_SUPPLIER_NAME = "Uncategorized"


def _normalize_phone_digits(value) -> str:
	return re.sub(r"\D", "", str(value or ""))


def _customer_bucket_names_lower() -> set:
	return {
		CONSUMIDOR_FINAL_NAME.lower(),
		UNCATEGORIZED_CUSTOMER_NAME.lower(),
	}


def _is_bucket_customer(name=None, customer_name=None) -> bool:
	buckets = _customer_bucket_names_lower()
	for raw in (customer_name, name):
		label = (raw or "").strip().lower()
		if label and label in buckets:
			return True
	return False


def _phones_match(a: str, b: str) -> bool:
	"""True when both digit strings are non-empty and one equals / ends with the other."""
	if not a or not b:
		return False
	if a == b:
		return True
	# Require at least 6 overlapping digits to avoid short false positives.
	if len(a) < 6 or len(b) < 6:
		return False
	return a.endswith(b) or b.endswith(a) or a[-8:] == b[-8:]


def _find_customer_by_phone(phone) -> str | None:
	"""Match an existing Customer by non-empty mobile. Empty phones never match."""
	digits = _normalize_phone_digits(phone)
	if len(digits) < 6:
		return None
	tail = digits[-8:]
	rows = frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		or_filters=[["mobile_no", "like", f"%{tail}%"]],
		fields=["name", "customer_name", "mobile_no"],
		order_by="modified desc",
		limit_page_length=40,
		ignore_permissions=True,
	)
	exact = []
	fuzzy = []
	for r in rows:
		mob = _normalize_phone_digits(r.mobile_no)
		if not mob:
			continue
		if _is_bucket_customer(r.name, r.customer_name):
			continue
		if mob == digits:
			exact.append(r.name)
		elif _phones_match(mob, digits):
			fuzzy.append(r.name)
	candidates = exact or fuzzy
	# Ambiguous → leave as Consumidor Final; staff can relate manually.
	uniq = list(dict.fromkeys(candidates))
	return uniq[0] if len(uniq) == 1 else None


def _find_customer_by_local(address) -> str | None:
	"""Match Customer by primary Address line (guest location / local), non-empty only."""
	addr = str(address or "").strip().lower()
	if len(addr) < 5:
		return None
	rows = frappe.db.sql(
		"""
		SELECT dl.link_name AS customer, a.address_line1
		FROM `tabAddress` a
		INNER JOIN `tabDynamic Link` dl
			ON dl.parent = a.name AND dl.parenttype = 'Address'
		WHERE dl.link_doctype = 'Customer'
		  AND a.address_line1 IS NOT NULL
		  AND TRIM(a.address_line1) != ''
		  AND LOWER(TRIM(a.address_line1)) = %s
		LIMIT 5
		""",
		(addr,),
		as_dict=True,
	)
	uniq = []
	for r in rows:
		name = r.get("customer")
		if not name:
			continue
		cname = frappe.db.get_value("Customer", name, "customer_name")
		if _is_bucket_customer(name, cname):
			continue
		if name not in uniq:
			uniq.append(name)
	return uniq[0] if len(uniq) == 1 else None


def _resolve_consulta_customer(explicit=None, guest_phone=None, guest_address=None) -> str:
	"""Prefer explicit Customer, else phone / local match, else Consumidor Final."""
	explicit = ("" if explicit is None else str(explicit)).strip()
	if explicit:
		if not frappe.db.exists("Customer", explicit):
			frappe.throw(_("Customer {0} not found").format(explicit))
		return explicit
	matched = _find_customer_by_phone(guest_phone)
	if matched:
		return matched
	matched = _find_customer_by_local(guest_address)
	if matched:
		return matched
	return _get_or_create_consumidor_final()


def _party_linked_docs(party_type, party, parenttype):
	"""Return Address/Contact names linked to a party via Dynamic Link."""
	if not party:
		return []
	return frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": party_type, "link_name": party, "parenttype": parenttype},
		pluck="parent",
		ignore_permissions=True,
	) or []


def _resync_so_party_links(so):
	"""Drop or replace contact/address that belong to the previous customer.

	Relacionar Consulta → Cliente fails with HTTP 417
	"Contact Person does not belong to …" when the draft still carries
	Consumidor Final's contact after ``so.customer`` is changed.

	Returns True if any party-link field was changed.
	"""
	customer = cstr(getattr(so, "customer", None) or "").strip()
	if not customer:
		return False

	contacts = set(_party_linked_docs("Customer", customer, "Contact"))
	addresses = set(_party_linked_docs("Customer", customer, "Address"))
	changed = False

	contact = cstr(getattr(so, "contact_person", None) or "").strip()
	if contact and contact not in contacts:
		so.contact_person = next(iter(contacts), None)
		for field in ("contact_display", "contact_mobile", "contact_email", "contact_phone"):
			if hasattr(so, field):
				so.set(field, None)
		changed = True

	billing = cstr(getattr(so, "customer_address", None) or "").strip()
	if billing and billing not in addresses:
		so.customer_address = None
		changed = True

	shipping = cstr(getattr(so, "shipping_address_name", None) or "").strip()
	if shipping and shipping not in addresses:
		so.shipping_address_name = None
		changed = True

	return changed


@frappe.whitelist(allow_guest=True)
def match_customers_for_consulta(guest_phone=None, guest_address=None, page_length=10):
	"""Suggest existing Customers for a consulta (phone first, then local/address).

	Empty phone/address never match empty Customer fields. Used by staff UI to
	auto-suggest or manually confirm a link.
	"""
	limit = max(1, min(cint(page_length) or 10, 40))
	out = []
	seen = set()

	def _append(name, reason):
		if not name or name in seen:
			return
		seen.add(name)
		row = frappe.db.get_value(
			"Customer",
			name,
			["name", "customer_name", "mobile_no", "email_id"],
			as_dict=True,
		)
		if not row or _is_bucket_customer(row.name, row.customer_name):
			return
		out.append(
			{
				"name": row.name,
				"customer_name": row.customer_name,
				"phone": row.mobile_no,
				"email": row.email_id,
				"match_reason": reason,
			}
		)

	phone_hit = _find_customer_by_phone(guest_phone)
	if phone_hit:
		_append(phone_hit, "phone")
	else:
		digits = _normalize_phone_digits(guest_phone)
		if len(digits) >= 6:
			tail = digits[-8:]
			rows = frappe.get_all(
				"Customer",
				filters={"disabled": 0},
				or_filters=[["mobile_no", "like", f"%{tail}%"]],
				fields=["name", "customer_name", "mobile_no"],
				order_by="modified desc",
				limit_page_length=limit,
				ignore_permissions=True,
			)
			for r in rows:
				mob = _normalize_phone_digits(r.mobile_no)
				if not mob or _is_bucket_customer(r.name, r.customer_name):
					continue
				if _phones_match(mob, digits):
					_append(r.name, "phone")
				if len(out) >= limit:
					break

	if len(out) < limit:
		local_hit = _find_customer_by_local(guest_address)
		if local_hit:
			_append(local_hit, "local")

	return {"customers": out[:limit]}


def _resolve_pos_sale_customer(pos_session_id=None, warehouse=None):
	"""Prefer the cash register (POS Profile) default customer; else Consumidor Final."""
	profile = None
	if pos_session_id:
		profile = frappe.db.get_value("POS Cash Session", pos_session_id, "pos_profile")
	if not profile and warehouse:
		profile = frappe.db.get_value(
			"POS Profile",
			{"warehouse": warehouse, "disabled": 0},
			"name",
		)
	if profile:
		cust = frappe.db.get_value("POS Profile", profile, "customer")
		if cust and frappe.db.exists("Customer", cust):
			return cust
	return _get_or_create_consumidor_final()


def _get_or_create_consumidor_final():
	"""Return the walk-in Customer used for POS / guest preorders.

	Looks up by customer_name (case-insensitive). Creates the record if missing
	so Sales Invoice set_missing_values never hits a None customer.
	"""
	existing = frappe.db.sql(
		"""
		SELECT name FROM `tabCustomer`
		WHERE LOWER(TRIM(customer_name)) = %s
		LIMIT 1
		""",
		CONSUMIDOR_FINAL_NAME.lower(),
	)
	if existing:
		return existing[0][0]

	customer_group = (
		frappe.db.get_single_value("Selling Settings", "customer_group")
		or (frappe.db.exists("Customer Group", "Individual") and "Individual")
		or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	)
	territory = (
		frappe.db.get_single_value("Selling Settings", "territory")
		or (frappe.db.exists("Territory", "All Territories") and "All Territories")
		or frappe.db.get_value("Territory", {"is_group": 0}, "name")
	)
	if not customer_group or not territory:
		frappe.throw(_("Cannot create Consumidor Final: Customer Group or Territory is missing."))

	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": CONSUMIDOR_FINAL_NAME,
			"customer_type": "Individual",
			"customer_group": customer_group,
			"territory": territory,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist(allow_guest=True)
@idempotent_request
def create_customer(
	customer_name,
	email=None,
	phone=None,
	customer_group="Individual",
	territory="All Territories",
	customer_type="Individual",
	tax_id=None,
	tax_category=None,
	preferred_hours=None,
	custom_preferred_hours=None,
	address_line1=None,
	client_access_pin=None,
	client_phone_e164=None,
	**kwargs,
):
	"""
	Create a new customer or return existing customer.

	Optional CRM fields (all nullable / blank-safe): tax_id (CUIT), tax_category
	(Cond. IVA — defaults to IVA 21%), preferred hours, address, PIN, credential phone.
	"""
	from frappe.utils import cstr

	try:
		from erpnext.erpnext_integrations.ecommerce_api.client_access_api import (
			ensure_client_access_custom_fields,
		)

		ensure_client_access_custom_fields()
	except Exception:
		pass
	try:
		from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
			ensure_argentina_iva_conditions,
			ensure_preferred_delivery_hours,
		)

		ensure_preferred_delivery_hours()
		ensure_argentina_iva_conditions()
	except Exception:
		pass

	# Coerce multi-salesman payload early (Flutter may send null / "null" / CSV).
	import json as _json

	raw_salesmen = kwargs.get("salesmen")
	if raw_salesmen is None:
		raw_salesmen = kwargs.get("user_ids")
	explicit_salesmen = raw_salesmen is not None
	salesmen_list: list = []
	if explicit_salesmen:
		if isinstance(raw_salesmen, str):
			s = raw_salesmen.strip()
			if not s or s.lower() in ("null", "undefined", "none"):
				salesmen_list = []
			else:
				try:
					parsed = _json.loads(s)
					salesmen_list = list(parsed) if isinstance(parsed, (list, tuple)) else [s]
				except Exception:
					salesmen_list = [x.strip() for x in s.split(",") if x.strip()]
		elif isinstance(raw_salesmen, (list, tuple)):
			salesmen_list = list(raw_salesmen)
		elif raw_salesmen:
			salesmen_list = [raw_salesmen]
		else:
			salesmen_list = []

	label = cstr(customer_name or "").strip()
	if not label:
		frappe.throw(_("Customer name is required"))

	# Check if customer already exists - if so, return it
	existing_name = None
	if frappe.db.exists("Customer", label):
		existing_name = label
	else:
		found = frappe.db.sql(
			"""
			SELECT name FROM `tabCustomer`
			WHERE LOWER(TRIM(customer_name)) = %s
			LIMIT 1
			""",
			(label.lower(),),
		)
		if found:
			existing_name = found[0][0]

	if existing_name:
		frappe.flags.ignore_permissions = True
		customer = frappe.get_doc("Customer", existing_name)
		frappe.flags.ignore_permissions = False
		return customer.as_dict()

	# Defaults for optional CRM fields
	phone_val = cstr(phone or "").strip() or None
	email_val = cstr(email or "").strip() or None
	tax_id_val = cstr(tax_id or "").strip() or None
	# Cond. IVA defaults to standard Argentina rate when omitted / blank
	raw_cat = tax_category if tax_category is not None else kwargs.get("tax_category")
	cat_val = cstr(raw_cat or "").strip() or "IVA 21%"
	if cat_val and not frappe.db.exists("Tax Category", cat_val):
		# Soft-fallback: seed may not have run; keep blank rather than LinkError
		try:
			from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
				ensure_or_create_tax_category,
			)

			ensure_or_create_tax_category(cat_val)
		except Exception:
			cat_val = None

	hours_val = cstr(
		preferred_hours
		if preferred_hours is not None
		else (custom_preferred_hours if custom_preferred_hours is not None else "")
	).strip() or None
	addr_val = cstr(address_line1 or "").strip() or None
	pin_val = cstr(
		client_access_pin
		if client_access_pin is not None
		else kwargs.get("custom_client_access_pin")
	).strip() or None
	cred_val = cstr(
		client_phone_e164
		if client_phone_e164 is not None
		else kwargs.get("custom_client_phone_e164")
	).strip() or None

	customer = frappe.new_doc("Customer")
	customer.customer_name = label
	customer.customer_group = customer_group or "Individual"
	customer.territory = territory or "All Territories"
	customer.customer_type = customer_type or "Individual"
	if phone_val:
		customer.mobile_no = phone_val
	if email_val:
		customer.email_id = email_val
	if tax_id_val:
		customer.tax_id = tax_id_val
	if cat_val:
		customer.tax_category = cat_val
	if hours_val and frappe.db.has_column("Customer", "custom_preferred_hours"):
		customer.custom_preferred_hours = hours_val
	if pin_val and frappe.db.has_column("Customer", "custom_client_access_pin"):
		customer.custom_client_access_pin = pin_val
	if cred_val and frappe.db.has_column("Customer", "custom_client_phone_e164"):
		customer.custom_client_phone_e164 = cred_val

	customer.insert(ignore_permissions=True)

	if addr_val:
		_update_customer_primary_address_line(customer, addr_val)
		customer.save(ignore_permissions=True)

	# RM Zona → Address.custom_zone (same codes as Planificación de entregas)
	zone_val = cstr(kwargs.get("zone") or "").strip() or None
	if zone_val:
		_set_customer_zone_and_address(customer.name, zone=zone_val)

	# CRM table create may pass one/many salesmen (user ids or display labels).
	# When omitted → auto-assign acting salesman (floor / Orden temporal clients).
	# Explicit empty list clears assignment (no auto-assign).
	acting = ""
	assigned_salesmen: list[str] = []
	try:
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
			_acting_username,
			_can_app,
			_is_sales_admin,
			_set_customer_salesmen_users,
			customer_salesmen,
		)

		acting = cstr(_acting_username() or "").strip()
		if explicit_salesmen:
			assigned_salesmen = _set_customer_salesmen_users(customer.name, salesmen_list)
		elif acting and acting not in ("Guest", "Administrator"):
			may_own = (
				_is_sales_admin()
				or _can_app("tables.crm")
				or _can_app("ops.catalog")
				or _can_app("ops.preventa")
				or _can_app("ops.pos")
				or _can_app("sales.commit_assigned")
				or _can_app("sales.commit_all")
				or _can_app("sales.see_assigned")
				or _can_app("sales.see_all")
			)
			if may_own:
				assigned_salesmen = _set_customer_salesmen_users(customer.name, [acting])
		if not assigned_salesmen:
			assigned_salesmen = customer_salesmen(customer.name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "create_customer auto-assign salesman")

	try:
		from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import (
			maybe_mark_creation_review_pending,
		)

		# Non-admin salesman → Pending so it appears in Tables → Revisar.
		maybe_mark_creation_review_pending(
			"Customer",
			customer.name,
			actor=acting or None,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "creation_review mark customer")

	frappe.db.commit()

	# Create contact if email or phone provided
	if email_val or phone_val:
		contact = frappe.new_doc("Contact")
		contact.first_name = label
		if email_val:
			contact.append("email_ids", {"email_id": email_val, "is_primary": 1})
		if phone_val:
			contact.append("phone_nos", {"phone": phone_val, "is_primary_phone": 1})

		contact.append("links", {
			"link_doctype": "Customer",
			"link_name": customer.name,
		})

		contact.insert(ignore_permissions=True)
		frappe.db.commit()

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook(
			"customer_created",
			{"customer": customer.name, "customer_name": customer.customer_name},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook customer_created")

	out = customer.as_dict()
	out["zone"] = _customer_delivery_zone_map([customer.name]).get(customer.name)
	out["salesmen"] = assigned_salesmen
	out["account_manager"] = assigned_salesmen[0] if assigned_salesmen else None
	return out


def _ensure_territory(name) -> str:
	"""Auto-create a missing Territory (leaf under the root) and return its name.

	TMS zone codes (``T1-TUE``…) are written to Customer.territory / SO.territory
	by the CRM / Pedidos zone editors; without a Territory master the save fails
	with LinkValidationError — fatal for offline-replayed edits. Empty → "".
	"""
	label = cstr(name or "").strip()
	if not label or frappe.db.exists("Territory", label):
		return label
	parent = (
		frappe.db.get_value("Territory", {"is_group": 1, "parent_territory": ["in", ["", None]]}, "name")
		or (frappe.db.exists("Territory", "All Territories") and "All Territories")
		or frappe.db.get_value("Territory", {"is_group": 1}, "name")
	)
	try:
		frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": label,
				"parent_territory": parent or "",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		pass
	return label


def _default_customer_group_and_territory():
	customer_group = (
		frappe.db.get_single_value("Selling Settings", "customer_group")
		or (frappe.db.exists("Customer Group", "Individual") and "Individual")
		or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	)
	territory = (
		frappe.db.get_single_value("Selling Settings", "territory")
		or (frappe.db.exists("Territory", "All Territories") and "All Territories")
		or frappe.db.get_value("Territory", {"is_group": 0}, "name")
	)
	return customer_group, territory


def _get_or_create_named_customer(display_name: str) -> str:
	"""Return Customer.name for a known display name; create if missing."""
	label = (display_name or "").strip()
	if not label:
		frappe.throw(_("Customer name is required"))
	existing = frappe.db.sql(
		"""
		SELECT name FROM `tabCustomer`
		WHERE LOWER(TRIM(customer_name)) = %s OR LOWER(TRIM(name)) = %s
		LIMIT 1
		""",
		(label.lower(), label.lower()),
	)
	if existing:
		return existing[0][0]

	customer_group, territory = _default_customer_group_and_territory()
	if not customer_group or not territory:
		frappe.throw(_("Cannot create customer: Customer Group or Territory is missing."))

	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": label,
			"customer_type": "Individual",
			"customer_group": customer_group,
			"territory": territory,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _get_or_create_uncategorized_customer():
	return _get_or_create_named_customer(UNCATEGORIZED_CUSTOMER_NAME)


def _default_supplier_group():
	return (
		(frappe.db.exists("Supplier Group", "All Supplier Groups") and "All Supplier Groups")
		or frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		or "All Supplier Groups"
	)


def _default_supplier_type() -> str:
	"""Supplier.supplier_type is often mandatory (Company / Individual / Partnership)."""
	existing = frappe.db.get_value("Supplier", {"supplier_type": ["is", "set"]}, "supplier_type")
	if existing:
		return existing
	return "Company"


def _get_or_create_named_supplier(display_name: str) -> str:
	label = (display_name or "").strip()
	if not label:
		frappe.throw(_("Supplier name is required"))
	existing = frappe.db.sql(
		"""
		SELECT name FROM `tabSupplier`
		WHERE LOWER(TRIM(supplier_name)) = %s OR LOWER(TRIM(name)) = %s
		LIMIT 1
		""",
		(label.lower(), label.lower()),
	)
	if existing:
		return existing[0][0]

	doc = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": label,
			"supplier_group": _default_supplier_group(),
			"supplier_type": _default_supplier_type(),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


@frappe.whitelist(allow_guest=True)
def ensure_crm_party_buckets():
	"""Ensure catch-all Customer/Supplier rows used by CRM and POS."""
	consumidor = _get_or_create_consumidor_final()
	uncat_customer = _get_or_create_uncategorized_customer()
	uncat_supplier = _get_or_create_named_supplier(UNCATEGORIZED_SUPPLIER_NAME)
	return {
		"consumidor_final": consumidor,
		"uncategorized_customer": uncat_customer,
		"uncategorized_supplier": uncat_supplier,
	}


@frappe.whitelist(allow_guest=True)
def search_customers(search_term="", page_length=20, ensure_buckets=0):
	"""Typeahead / CRM list for Customer (name + customer_name + mobile)."""
	if cint(ensure_buckets):
		ensure_crm_party_buckets()
	try:
		from erpnext.erpnext_integrations.ecommerce_api.client_access_api import (
			ensure_client_access_custom_fields,
		)

		ensure_client_access_custom_fields()
	except Exception:
		pass
	try:
		from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
			ensure_argentina_iva_conditions,
			ensure_preferred_delivery_hours,
		)

		ensure_preferred_delivery_hours()
		ensure_argentina_iva_conditions()
	except Exception:
		pass

	term = cstr(search_term or "").strip()
	if term.lower() in ("null", "undefined", "none"):
		term = ""
	limit = max(1, min(cint(page_length) or 20, 200))
	or_filters = None
	if term:
		like = f"%{term}%"
		or_filters = [
			["name", "like", like],
			["customer_name", "like", like],
			["mobile_no", "like", like],
			["tax_id", "like", like],
		]

	base_fields = [
		"name",
		"customer_name",
		"mobile_no",
		"email_id",
		"customer_group",
		"territory",
		"tax_id",
		"tax_category",
		"primary_address",
		"customer_primary_address",
	]
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		base_fields += ["custom_client_access_pin", "custom_client_phone_e164"]
	if frappe.db.has_column("Customer", "custom_preferred_hours"):
		base_fields.append("custom_preferred_hours")

	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_assigned_customer_names,
		_acting_username,
		customer_salesmen,
		sales_visibility_scope,
	)

	scope = sales_visibility_scope()
	uid = _acting_username() or ""
	assigned_names = list(_assigned_customer_names(uid)) if uid else []
	assigned_set = set(assigned_names)

	def _fetch(extra_filters=None, page_len=None):
		flt = {"disabled": 0}
		if extra_filters:
			flt.update(extra_filters)
		return frappe.get_all(
			"Customer",
			filters=flt,
			or_filters=or_filters,
			fields=base_fields,
			order_by="customer_name asc",
			limit_page_length=page_len if page_len is not None else limit,
			ignore_permissions=True,
		)

	# Query inside the assigned set (do not filter after a global page limit —
	# otherwise seller-assigned clients outside the alphabetical first page never appear).
	if scope == "none":
		rows = []
	elif scope == "assigned":
		rows = _fetch({"name": ["in", assigned_names]}) if assigned_names else []
	elif not term and assigned_names:
		# Prefer the acting seller's assigned clients at the top of an empty typeahead.
		assigned_rows = _fetch({"name": ["in", assigned_names]})
		remaining = max(0, limit - len(assigned_rows))
		other_rows = (
			_fetch({"name": ["not in", assigned_names]}, page_len=remaining) if remaining else []
		)
		rows = list(assigned_rows) + list(other_rows)
	else:
		rows = _fetch()

	# RM Zona = Address.custom_zone (committed Planificación de entregas codes), not Territory.
	cust_names = [r.name for r in rows]
	zone_by_customer = _customer_delivery_zone_map(cust_names)
	stage_by_customer = _customer_preventa_stage_map(cust_names)

	out_customers = []
	for r in rows:
		salesmen = customer_salesmen(r.name)
		st = stage_by_customer.get(r.name) or {}
		out_customers.append(
			{
				"name": r.name,
				"customer_name": r.customer_name,
				"phone": r.mobile_no,
				"email": r.email_id,
				"customer_group": r.customer_group,
				"territory": r.territory,
				"zone": zone_by_customer.get(r.name),
				"tax_id": r.tax_id,
				"tax_category": r.tax_category,
				"primary_address": r.primary_address,
				"customer_primary_address": r.customer_primary_address,
				"preferred_hours": getattr(r, "custom_preferred_hours", None),
				"client_access_pin": getattr(r, "custom_client_access_pin", None),
				"client_phone_e164": getattr(r, "custom_client_phone_e164", None),
				"is_bucket": (r.customer_name or r.name or "")
				in (CONSUMIDOR_FINAL_NAME, UNCATEGORIZED_CUSTOMER_NAME),
				"salesmen": salesmen,
				"account_manager": salesmen[0] if salesmen else None,
				"assigned": r.name in assigned_set,
				"stage": st.get("stage"),
				"stage_label": st.get("stage_label"),
			}
		)
	return {"customers": out_customers}


@frappe.whitelist(allow_guest=True)
@idempotent_request
def create_supplier(supplier_name, supplier_group=None):
	"""Create a Supplier or return the existing one (by name / supplier_name)."""
	label = (supplier_name or "").strip()
	if not label:
		frappe.throw(_("Supplier name is required"))

	existing = frappe.db.sql(
		"""
		SELECT name FROM `tabSupplier`
		WHERE LOWER(TRIM(supplier_name)) = %s OR LOWER(TRIM(name)) = %s
		LIMIT 1
		""",
		(label.lower(), label.lower()),
	)
	if existing:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Supplier", existing[0][0])
		frappe.flags.ignore_permissions = False
		return doc.as_dict()

	doc = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": label,
			"supplier_group": supplier_group or _default_supplier_group(),
			"supplier_type": _default_supplier_type(),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist(allow_guest=True)
def update_supplier(supplier_name=None, **kwargs):
	"""Update Supplier contact fields for RM Proveedores detail edit."""
	name = cstr(supplier_name or kwargs.get("name") or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Supplier is required."), frappe.ValidationError)
	if not frappe.db.exists("Supplier", name):
		frappe.throw(_("Supplier {0} not found").format(name), frappe.DoesNotExistError)
	if (frappe.db.get_value("Supplier", name, "supplier_name") or name) == UNCATEGORIZED_SUPPLIER_NAME:
		frappe.throw(_("Cannot edit Uncategorized supplier."), frappe.ValidationError)

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Supplier", name)
	frappe.flags.ignore_permissions = False

	# phone / mobile aliases
	if "phone" in kwargs and "mobile_no" not in kwargs:
		kwargs["mobile_no"] = kwargs.get("phone")
	if "email" in kwargs and "email_id" not in kwargs:
		kwargs["email_id"] = kwargs.get("email")

	allowed = ["supplier_name", "mobile_no", "email_id", "supplier_group"]
	for field in allowed:
		if field in kwargs:
			val = kwargs.get(field)
			if val is None or cstr(val).strip().lower() in ("null", "undefined"):
				val = ""
			doc.set(field, cstr(val).strip() if field != "supplier_group" else (cstr(val).strip() or doc.supplier_group))

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"name": doc.name,
		"supplier_name": doc.supplier_name,
		"phone": doc.mobile_no,
		"email": doc.email_id,
		"supplier_group": doc.supplier_group,
		"is_bucket": (doc.supplier_name or doc.name or "") == UNCATEGORIZED_SUPPLIER_NAME,
	}


@frappe.whitelist(allow_guest=True)
def search_suppliers(search_term="", page_length=20, ensure_buckets=0):
	"""Typeahead / CRM list for Supplier."""
	if cint(ensure_buckets):
		_get_or_create_named_supplier(UNCATEGORIZED_SUPPLIER_NAME)

	term = (search_term or "").strip()
	limit = max(1, min(cint(page_length) or 20, 100))
	filters = {"disabled": 0}
	or_filters = None
	if term:
		like = f"%{term}%"
		or_filters = [
			["name", "like", like],
			["supplier_name", "like", like],
			["mobile_no", "like", like],
		]

	rows = frappe.get_all(
		"Supplier",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "supplier_name", "mobile_no", "email_id", "supplier_group"],
		order_by="supplier_name asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return {
		"suppliers": [
			{
				"name": r.name,
				"supplier_name": r.supplier_name,
				"phone": r.mobile_no,
				"email": r.email_id,
				"supplier_group": r.supplier_group,
				"is_bucket": (r.supplier_name or r.name or "") == UNCATEGORIZED_SUPPLIER_NAME,
			}
			for r in rows
		]
	}


@frappe.whitelist(allow_guest=True)
def list_crm_customer_options():
	"""CRM clients table: TMS zones, tax categories, preferred delivery hours (by priority).

	``zones`` are committed Planificación de entregas codes (``list_tms_zones``),
	the same values stored on Address.custom_zone. ``territories`` remain for
	legacy consumers but RM Zona should use ``zones``.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
		ensure_argentina_iva_conditions,
		list_preferred_delivery_hours,
	)

	try:
		ensure_argentina_iva_conditions()
	except Exception:
		pass

	hours = list_preferred_delivery_hours(include_disabled=0)
	territories = frappe.get_all(
		"Territory",
		fields=["name"],
		order_by="name asc",
		ignore_permissions=True,
	)
	tax_categories = []
	if frappe.db.exists("DocType", "Tax Category"):
		tax_categories = frappe.get_all(
			"Tax Category",
			filters={"disabled": 0},
			fields=["name"],
			order_by="name asc",
			ignore_permissions=True,
		)
	zones = []
	seen = set()
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import _load_tms_zones

		payload = _load_tms_zones() or {}
		raw = payload.get("zones") if isinstance(payload, dict) else payload
		if isinstance(raw, list):
			for z in raw:
				if not isinstance(z, dict):
					continue
				code = cstr(z.get("code") or "").strip()
				if not code:
					continue
				key = code.upper()
				if key in seen:
					continue
				seen.add(key)
				zones.append(code)
	except Exception:
		pass
	return {
		"preferred_hours": hours.get("hours") or [],
		"territories": [r.name for r in territories],
		"zones": zones,
		"tax_categories": [r.name for r in tax_categories],
	}


@frappe.whitelist(allow_guest=True)
def ensure_or_create_tax_category(title=None):
	"""Proxy — CRM Cond. IVA allowCreate."""
	from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
		ensure_or_create_tax_category as _ensure,
	)

	return _ensure(title=title)


@frappe.whitelist(allow_guest=True)
def list_preferred_delivery_hours(include_disabled=0):
	"""Proxy — Preferred Delivery Hours sorted by priority."""
	from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
		list_preferred_delivery_hours as _list,
	)

	return _list(include_disabled=include_disabled)


@frappe.whitelist(allow_guest=True)
def save_preferred_delivery_hours(hours=None):
	"""Proxy — edit Preferred Delivery Hours master list from CRM."""
	from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
		save_preferred_delivery_hours as _save,
	)

	return _save(hours=hours)


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

	frappe.flags.ignore_permissions = True
	customer = frappe.get_doc("Customer", customer_name)
	frappe.flags.ignore_permissions = False

	# Update allowed fields
	allowed_fields = [
		"customer_name", "customer_group", "territory", "customer_type",
		"default_currency", "default_price_list", "default_sales_partner",
		"mobile_no", "email_id", "tax_id", "tax_category",
	]
	if frappe.db.has_column("Customer", "custom_preferred_hours"):
		allowed_fields.append("custom_preferred_hours")
		# Frontend may send preferred_hours alias
		if "preferred_hours" in kwargs and "custom_preferred_hours" not in kwargs:
			kwargs["custom_preferred_hours"] = kwargs.get("preferred_hours")
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		allowed_fields.append("custom_client_access_pin")
		if "client_access_pin" in kwargs and "custom_client_access_pin" not in kwargs:
			kwargs["custom_client_access_pin"] = kwargs.get("client_access_pin")
	if frappe.db.has_column("Customer", "custom_client_phone_e164"):
		allowed_fields.append("custom_client_phone_e164")
		if "client_phone_e164" in kwargs and "custom_client_phone_e164" not in kwargs:
			kwargs["custom_client_phone_e164"] = kwargs.get("client_phone_e164")
	# CRM TEL column aliases
	if "phone" in kwargs and "mobile_no" not in kwargs:
		kwargs["mobile_no"] = kwargs.get("phone")

	for field, value in kwargs.items():
		if field in allowed_fields:
			customer.set(field, value)

	# Optional primary address line update (CRM editable Address column)
	addr_line = kwargs.get("address_line1")
	if addr_line is not None:
		_update_customer_primary_address_line(customer, cstr(addr_line).strip())

	if customer.territory:
		_ensure_territory(customer.territory)
	# Cond. IVA typed in the CRM (possibly offline) may not exist yet — create it
	# instead of failing the Link (same helper create_customer uses).
	if cstr(customer.get("tax_category") or "").strip():
		try:
			from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
				ensure_or_create_tax_category,
			)

			ensure_or_create_tax_category(customer.tax_category)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "update_customer ensure tax_category")
	customer.save(ignore_permissions=True)

	# RM Zona → Address.custom_zone (committed TMS zone codes like T1-THU)
	if "zone" in kwargs:
		raw_zone = kwargs.get("zone")
		if raw_zone is None or cstr(raw_zone).strip().lower() in ("null", "undefined"):
			zone_val = ""
		else:
			zone_val = cstr(raw_zone).strip()
		_set_customer_zone_and_address(customer_name, zone=zone_val)

	frappe.db.commit()

	out = customer.as_dict()
	out["zone"] = _customer_delivery_zone_map([customer_name]).get(customer_name)
	return out


def _update_customer_primary_address_line(customer, address_line1: str):
	"""Create or patch the customer's primary Address.address_line1."""
	addr_name = customer.customer_primary_address
	if addr_name and frappe.db.exists("Address", addr_name):
		frappe.db.set_value("Address", addr_name, "address_line1", address_line1 or "")
		customer.primary_address = frappe.db.get_value("Address", addr_name, "address_line1")
		return
	if not address_line1:
		return
	addr = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": customer.customer_name or customer.name,
			"address_type": "Billing",
			"address_line1": address_line1,
			"city": "-",
			"country": frappe.db.get_default("country") or "Argentina",
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		}
	)
	addr.insert(ignore_permissions=True)
	customer.customer_primary_address = addr.name
	customer.primary_address = address_line1


@frappe.whitelist(allow_guest=True)
@idempotent_request
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


@frappe.whitelist(allow_guest=True)
def list_customer_addresses(customer=None, search=None, page_length=20):
	"""List Address docs linked to a Customer (for Orden location picker).

	Returns street lines + flags so the UI can prefer primary / shipping.
	Empty / missing customer → empty list (never 500).
	"""
	cust = cstr(customer or "").strip()
	if not cust or cust.lower() in ("null", "undefined", "none"):
		return {"addresses": []}

	try:
		limit = max(1, min(cint(page_length) or 20, 50))
	except Exception:
		limit = 20

	needle = cstr(search or "").strip()
	if needle.lower() in ("null", "undefined", "none"):
		needle = ""

	fields = [
		"name",
		"address_line1",
		"address_line2",
		"city",
		"state",
		"pincode",
		"address_type",
		"is_primary_address",
		"is_shipping_address",
	]
	if frappe.db.has_column("Address", "custom_zone"):
		fields.append("custom_zone")
	if frappe.db.has_column("Address", "custom_latitude"):
		fields.append("custom_latitude")
	if frappe.db.has_column("Address", "custom_longitude"):
		fields.append("custom_longitude")

	rows = frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", "Customer"],
			["Dynamic Link", "link_name", "=", cust],
			["disabled", "=", 0],
		],
		fields=fields,
		# Qualify modified — Dynamic Link join makes bare `modified` ambiguous.
		order_by="`tabAddress`.is_primary_address desc, `tabAddress`.is_shipping_address desc, `tabAddress`.modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)

	out = []
	needle_l = needle.lower()
	for r in rows or []:
		bits = [
			cstr(r.get("address_line1") or "").strip(),
			cstr(r.get("address_line2") or "").strip(),
			cstr(r.get("city") or "").strip(),
		]
		line = ", ".join(b for b in bits if b and b != "-")
		if not line:
			continue
		if needle_l and needle_l not in line.lower() and needle_l not in cstr(r.get("name") or "").lower():
			continue
		lat_v = r.get("custom_latitude")
		lng_v = r.get("custom_longitude")
		try:
			lat_f = flt(lat_v) if lat_v not in (None, "") else None
			lng_f = flt(lng_v) if lng_v not in (None, "") else None
		except Exception:
			lat_f, lng_f = None, None
		out.append(
			{
				"name": r.get("name"),
				"address_line": line,
				"address_line1": cstr(r.get("address_line1") or "").strip() or None,
				"address_line2": cstr(r.get("address_line2") or "").strip() or None,
				"city": cstr(r.get("city") or "").strip() or None,
				"state": cstr(r.get("state") or "").strip() or None,
				"pincode": cstr(r.get("pincode") or "").strip() or None,
				"address_type": cstr(r.get("address_type") or "").strip() or None,
				"zone": cstr(r.get("custom_zone") or "").strip() or None,
				"lat": lat_f,
				"lng": lng_f,
				"is_primary": 1 if cint(r.get("is_primary_address")) else 0,
				"is_shipping": 1 if cint(r.get("is_shipping_address")) else 0,
			}
		)
	return {"addresses": out}


@frappe.whitelist(allow_guest=True)
def upsert_customer_address_place(
	customer=None,
	address_line1=None,
	address_line2=None,
	city=None,
	state=None,
	pincode=None,
	country=None,
	lat=None,
	lng=None,
	address_name=None,
	address_type=None,
):
	"""Create or update a Customer Address with structured fields + lat/lng.

	Used by Address Complete modal (Orden / CRM / consulta). Bucket customers
	(Consumidor Final / Uncategorized) are rejected — callers should only
	persist geo text locally for those.
	"""
	cust = cstr(customer or "").strip()
	if not cust or cust.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Customer is required."), frappe.ValidationError)
	if _is_bucket_customer(cust):
		frappe.throw(
			_("Cannot save a map address on Consumidor Final / Uncategorized."),
			frappe.ValidationError,
		)
	if not frappe.db.exists("Customer", cust):
		frappe.throw(_("Customer {0} not found").format(cust), frappe.DoesNotExistError)

	line1 = cstr(address_line1 or "").strip()
	if not line1 or line1.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Street address is required."), frappe.ValidationError)

	try:
		lat_f = flt(lat)
		lng_f = flt(lng)
	except Exception:
		lat_f, lng_f = 0.0, 0.0
	if not lat_f and not lng_f:
		frappe.throw(_("Latitude and longitude are required."), frappe.ValidationError)

	city_val = cstr(city or "").strip()
	if not city_val or city_val.lower() in ("null", "undefined", "none", "-"):
		city_val = "-"
	state_val = cstr(state or "").strip()
	if state_val.lower() in ("null", "undefined", "none"):
		state_val = ""
	pincode_val = cstr(pincode or "").strip()
	if pincode_val.lower() in ("null", "undefined", "none"):
		pincode_val = ""
	line2_val = cstr(address_line2 or "").strip()
	if line2_val.lower() in ("null", "undefined", "none"):
		line2_val = ""
	country_val = cstr(country or "").strip() or (frappe.db.get_default("country") or "Argentina")
	if country_val.lower() in ("null", "undefined", "none"):
		country_val = frappe.db.get_default("country") or "Argentina"
	atype = cstr(address_type or "Shipping").strip() or "Shipping"
	if atype.lower() in ("null", "undefined", "none"):
		atype = "Shipping"

	addr_name = cstr(address_name or "").strip()
	if addr_name.lower() in ("null", "undefined", "none"):
		addr_name = ""

	# Prefer updating an existing linked address when name omitted.
	if not addr_name:
		primary = frappe.db.get_value("Customer", cust, "customer_primary_address")
		if primary and frappe.db.exists("Address", primary):
			addr_name = primary
		else:
			linked = frappe.get_all(
				"Address",
				filters=[
					["Dynamic Link", "link_doctype", "=", "Customer"],
					["Dynamic Link", "link_name", "=", cust],
					["disabled", "=", 0],
				],
				pluck="name",
				order_by="is_primary_address desc, is_shipping_address desc, modified desc",
				limit_page_length=1,
				ignore_permissions=True,
			)
			if linked:
				addr_name = linked[0]

	created = False
	# Prefer db.set_value for updates: Address.custom_zone often holds TMS codes
	# (e.g. T1-TUE) that fail Select validation on Document.save().
	if addr_name and frappe.db.exists("Address", addr_name):
		field_update = {
			"address_line1": line1,
			"address_line2": line2_val or "",
			"city": city_val,
			"state": state_val or "",
			"pincode": pincode_val or "",
			"country": country_val,
			"address_type": atype,
		}
		if frappe.db.has_column("Address", "is_shipping_address"):
			field_update["is_shipping_address"] = 1
		if frappe.db.has_column("Address", "is_primary_address") and not frappe.db.get_value(
			"Customer", cust, "customer_primary_address"
		):
			field_update["is_primary_address"] = 1
		frappe.db.set_value("Address", addr_name, field_update, update_modified=True)
	else:
		frappe.flags.ignore_permissions = True
		cust_label = frappe.db.get_value("Customer", cust, "customer_name") or cust
		doc = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": cust_label,
				"address_type": atype,
				"address_line1": line1,
				"address_line2": line2_val or None,
				"city": city_val,
				"state": state_val or None,
				"pincode": pincode_val or None,
				"country": country_val,
				"links": [{"link_doctype": "Customer", "link_name": cust}],
			}
		)
		if hasattr(doc, "is_shipping_address"):
			doc.is_shipping_address = 1
		if hasattr(doc, "is_primary_address"):
			doc.is_primary_address = 1
		# Avoid Select validation on custom_zone if a default sneaks in.
		if hasattr(doc, "custom_zone"):
			doc.custom_zone = None
		doc.insert(ignore_permissions=True)
		frappe.flags.ignore_permissions = False
		addr_name = doc.name
		created = True

	geo_update = {}
	if frappe.db.has_column("Address", "custom_latitude"):
		geo_update["custom_latitude"] = lat_f
	if frappe.db.has_column("Address", "custom_longitude"):
		geo_update["custom_longitude"] = lng_f
	if frappe.db.has_column("Address", "custom_geocoded_on"):
		from frappe.utils import now_datetime

		geo_update["custom_geocoded_on"] = now_datetime()
	if geo_update:
		frappe.db.set_value("Address", addr_name, geo_update, update_modified=False)

	# Ensure Customer points at this address as primary when unset.
	primary = frappe.db.get_value("Customer", cust, "customer_primary_address")
	if not primary:
		frappe.db.set_value("Customer", cust, "customer_primary_address", addr_name)
	frappe.db.set_value("Customer", cust, "primary_address", line1)

	zone = None
	try:
		from erpnext.erpnext_integrations.ecommerce_api import tms_api

		# Uses db.set_value internally — safe with TMS zone codes.
		assigned = tms_api._auto_assign_zone_if_missing(addr_name)
		if assigned:
			zone = assigned.get("zone")
		elif frappe.db.has_column("Address", "custom_zone"):
			zone = cstr(frappe.db.get_value("Address", addr_name, "custom_zone") or "").strip() or None
	except Exception:
		zone = None

	frappe.db.commit()

	bits = [line1, line2_val, city_val if city_val != "-" else "", state_val]
	address_line = ", ".join(b for b in bits if b)

	return {
		"name": addr_name,
		"created": 1 if created else 0,
		"address_line": address_line or line1,
		"address_line1": line1,
		"address_line2": line2_val or None,
		"city": city_val if city_val != "-" else None,
		"state": state_val or None,
		"pincode": pincode_val or None,
		"country": country_val,
		"lat": lat_f,
		"lng": lng_f,
		"zone": zone,
	}


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
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	if not company:
		company = resolve_company()
		if not company:
			company = frappe.db.get_value("Company", {}, "name")

	# Create Sales Order
	so = frappe.new_doc("Sales Order")
	so.customer = customer
	so.order_type = order_type
	so.transaction_date = nowdate()
	so.delivery_date = delivery_date or _next_business_delivery_date()
	# Clamp a past delivery date to the order date (ERPNext date rule).
	try:
		if getdate(so.delivery_date) < getdate(so.transaction_date):
			so.delivery_date = so.transaction_date
	except Exception:
		so.delivery_date = _next_business_delivery_date()
	so.company = company

	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	so.currency = currency or company_currency
	if so.currency == company_currency:
		so.conversion_rate = 1.0

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

	# Add items — client/catalog rates win; skip Pricing Rule re-apply on insert.
	so.ignore_pricing_rule = 1
	so.flags.ignore_pricing_rule = True
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
				"currency": so.currency,
				"conversion_rate": so.conversion_rate,
				"transaction_date": so.transaction_date,
				"doctype": "Sales Order",
				"ignore_pricing_rule": 1,
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

# SilkOS Sales Order `order_type` only allows a small set (e.g. Sales, Shopping Cart).
# We tag guest catalog consultations in `remarks` or `terms` (some sites have no `remarks` DB column).
GUEST_PREORDER_REMARKS_TAG = "guest_preorder=1"


def _normalize_create_initial_status(initial_status) -> str:
	"""Map create_guest_preorder.initial_status → Consulta | Orden (default Consulta)."""
	raw = cstr(initial_status or "").strip().lower()
	if raw in ("", "null", "none", "undefined", "consulta", "inquiry", "draft"):
		return "Consulta"
	if raw in ("orden", "order", "1", "true", "yes", "y"):
		return "Orden"
	# Allow exact workflow label passthrough for Orden only (other stages still go Consulta).
	if cstr(initial_status or "").strip() == "Orden":
		return "Orden"
	return "Consulta"


def _run_create_guest_preorder_side_effects(
	so_name=None,
	ensure_remito=0,
	guest_name=None,
	guest_phone=None,
	guest_email=None,
	seller_ref_user=None,
	pin_allowed_countries=None,
	send_client_pin=0,
	client_access_pin=None,
	phone_e164=None,
):
	"""Email / Twilio / Preventa / webhook / remito — after create returns to the UI.

	Runs on the short queue (or inline when enqueue fails). Never raises to the
	caller; each step is best-effort with its own error log.
	"""
	so_name = cstr(so_name or "").strip()
	if not so_name or so_name.lower() in ("null", "undefined", "none"):
		return {"ok": False, "reason": "missing_so"}
	if not frappe.db.exists("Sales Order", so_name):
		return {"ok": False, "reason": "missing_so"}

	# Inquiry email (SMTP) — catalog consultas + Orden creates.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.inquiry_email import (
			send_consulta_notification,
		)

		send_consulta_notification(so_name, guest_name=guest_name, guest_phone=guest_phone)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Inquiry email failed for {so_name}")

	# Twilio PIN send when mint already happened in the request (send=0 there).
	client_access = None
	pin = cstr(client_access_pin or "").strip()
	e164 = cstr(phone_e164 or guest_phone or "").strip()
	if cint(send_client_pin) and pin and e164:
		try:
			from erpnext.erpnext_integrations.ecommerce_api.client_access_api import (
				_portal_base_url,
				_send_pin_message,
			)

			portal = f"{_portal_base_url()}/cliente"
			send_result = _send_pin_message(e164, pin, portal)
			client_access = {"pin": pin, "phone_e164": e164, "send": send_result}
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Client access PIN send failed for {so_name}")
	elif cstr(guest_phone or "").strip() and not pin:
		# No PIN minted in-request (e.g. no phone path) — full issue+send.
		try:
			from erpnext.erpnext_integrations.ecommerce_api.client_access_api import (
				issue_client_access_pin,
			)

			customer = frappe.db.get_value("Sales Order", so_name, "customer")
			client_access = issue_client_access_pin(
				guest_phone=guest_phone,
				guest_name=guest_name,
				guest_email=guest_email,
				customer=customer,
				allowed_countries=pin_allowed_countries,
				send=cint(send_client_pin),
			)
			pin = cstr((client_access or {}).get("pin") or "").strip()
			e164 = cstr((client_access or {}).get("phone_e164") or guest_phone or "").strip()
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Client access PIN failed for {so_name}")

	# Preventa Lead bridge + stamp PIN onto Lead when we have one.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.preventa_api import (
			sync_lead_from_guest_preorder,
		)

		sync_lead_from_guest_preorder(
			so_name,
			guest_name=guest_name,
			guest_phone=guest_phone,
			guest_email=guest_email,
			seller_ref_user=seller_ref_user,
		)
		lead_name = frappe.db.get_value(
			"Preventa Lead Consulta", {"sales_order": so_name}, "lead"
		)
		if lead_name and pin:
			from erpnext.erpnext_integrations.ecommerce_api.client_access_api import (
				_stamp_party,
			)

			_stamp_party("Lead", lead_name, pin, e164 or guest_phone)
			frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Preventa lead sync failed for {so_name}")

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		row = (
			frappe.db.get_value(
				"Sales Order",
				so_name,
				["customer", "grand_total", "currency"],
				as_dict=True,
			)
			or {}
		)
		emit_ecommerce_webhook(
			"order_created",
			{
				"preorder_name": so_name,
				"customer": row.get("customer"),
				"grand_total": flt(row.get("grand_total")),
				"currency": row.get("currency"),
			},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook order_created")

	# Orden / planner remito + soft geocode (was the main UI stall).
	if cint(ensure_remito):
		try:
			frappe.flags.ignore_permissions = True
			so = frappe.get_doc("Sales Order", so_name)
			frappe.flags.ignore_permissions = False
			if _is_guest_preorder_sales_order(so) and cint(so.docstatus) == 1:
				_ensure_planner_delivery_note(so)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"deferred planner remito failed for {so_name}")

	return {"ok": True, "so_name": so_name, "client_access": client_access}


def _enqueue_create_guest_preorder_side_effects(**kwargs):
	"""Queue post-create work; fall back to inline if Redis/workers unavailable."""
	so_name = cstr(kwargs.get("so_name") or "").strip()
	try:
		enq = {
			"queue": "short",
			"timeout": 300,
			"enqueue_after_commit": True,
			**kwargs,
		}
		if so_name:
			enq["job_id"] = f"guest_preorder_side:{so_name}"
		frappe.enqueue(
			"erpnext.erpnext_integrations.ecommerce_api.api._run_create_guest_preorder_side_effects",
			**enq,
		)
		return {"queued": True}
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"enqueue create side effects failed for {so_name or '?'}",
		)
		try:
			_run_create_guest_preorder_side_effects(**kwargs)
			return {"queued": False, "ran_inline": True}
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"inline create side effects failed for {so_name or '?'}",
			)
			return {"queued": False, "ran_inline": False}


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


def _guest_preorder_tag_text(so) -> str:
	return str(getattr(so, "remarks", None) or getattr(so, "terms", None) or "")


def _require_guest_preorder_visible(so) -> None:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_order_visibility_scope,
		guest_preorder_matches_scope,
	)

	scope = _order_visibility_scope()
	if guest_preorder_matches_scope(
		getattr(so, "owner", ""),
		_guest_preorder_tag_text(so),
		scope,
		customer=getattr(so, "customer", None),
	):
		return
	frappe.throw(
		_(
			"No permission to open or confirm this order (tables.orders). "
			"Ask an admin for «Pedidos propios», or copy the selection and send it instead."
		)
	)


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


def _sanitize_guest_tag(value) -> str:
	return str(value or "").replace("|", " ").strip()[:240]


def _parse_remarks_tags(raw) -> dict:
	tags = {}
	for part in str(raw or "").split("|"):
		part = part.strip()
		if ":" in part:
			key, val = part.split(":", 1)
			tags[key.strip()] = val.strip()
	return tags


def _guest_preorder_tag_text(so_or_dict):
	if isinstance(so_or_dict, dict):
		if so_or_dict.get("_tag_raw"):
			return str(so_or_dict.get("_tag_raw") or "")
		tag_fn = _guest_preorder_tag_fieldname()
		if tag_fn and so_or_dict.get(tag_fn):
			return so_or_dict.get(tag_fn) or ""
		return so_or_dict.get("remarks") or so_or_dict.get("terms") or ""
	for fn in ("remarks", "terms"):
		if hasattr(so_or_dict, fn):
			val = getattr(so_or_dict, fn, None) or ""
			if val:
				return str(val)
	return ""


def _cashier_from_guest_preorder(so_or_dict):
	return _parse_remarks_tags(_guest_preorder_tag_text(so_or_dict)).get("cashier") or None


def _seller_ref_from_guest_preorder(so_or_dict):
	"""Salesman attributed via /s/{slug} cookie (seller_ref tag on the SO)."""
	return _parse_remarks_tags(_guest_preorder_tag_text(so_or_dict)).get("seller_ref") or None


def _delivery_date_forced_from_tags(so_or_dict) -> bool:
	return (_parse_remarks_tags(_guest_preorder_tag_text(so_or_dict)).get("delivery_forced") or "") in (
		"1",
		"true",
		"yes",
	)


def _weekday_base_label(raw) -> str:
	"""Normalize ``Mon``, ``Mon(1 Man)``, ``Lunes`` → ``Mon``..``Sun``."""
	s = cstr(raw or "").strip()
	if not s:
		return ""
	head = s.split("(", 1)[0].strip()
	aliases = {
		"mon": "Mon",
		"tue": "Tue",
		"wed": "Wed",
		"thu": "Thu",
		"fri": "Fri",
		"sat": "Sat",
		"sun": "Sun",
		"lun": "Mon",
		"mar": "Tue",
		"mie": "Wed",
		"mié": "Wed",
		"jue": "Thu",
		"vie": "Fri",
		"sab": "Sat",
		"sáb": "Sat",
		"dom": "Sun",
		"lu": "Mon",
		"ma": "Tue",
		"mi": "Wed",
		"ju": "Thu",
		"vi": "Fri",
		"sa": "Sat",
		"do": "Sun",
	}
	key = head[:3].lower() if len(head) >= 3 else head.lower()
	mapped = aliases.get(key) or aliases.get(head.lower())
	if mapped:
		return mapped
	title = head[:3].title() if len(head) >= 3 else head.title()
	if title in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
		return title
	return ""


def _customer_address_zone_map(customer_names):
	"""Batch: customer → {address, territory, zone} (zone prefers Address.custom_zone)."""
	out = {}
	names = list({cstr(c).strip() for c in (customer_names or []) if cstr(c).strip()})
	if not names:
		return out
	customers = frappe.get_all(
		"Customer",
		filters={"name": ["in", names]},
		fields=["name", "territory", "customer_primary_address", "primary_address"],
		ignore_permissions=True,
	)
	addr_names = [c.customer_primary_address for c in customers if c.customer_primary_address]
	addr_by_name = {}
	if addr_names:
		addr_fields = ["name", "address_line1", "address_line2", "city"]
		if frappe.db.has_column("Address", "custom_zone"):
			addr_fields.append("custom_zone")
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", addr_names]},
			fields=addr_fields,
			ignore_permissions=True,
		):
			addr_by_name[row.name] = row

	for c in customers:
		addr = addr_by_name.get(c.customer_primary_address) or {}
		bits = [
			cstr(addr.get("address_line1") or "").strip(),
			cstr(addr.get("address_line2") or "").strip(),
			cstr(addr.get("city") or "").strip(),
		]
		street = ", ".join(b for b in bits if b and b != "-")
		primary = cstr(c.primary_address or "").replace("<br>", ", ").replace("<br/>", ", ").strip()
		zone = cstr(addr.get("custom_zone") or "").strip() or cstr(c.territory or "").strip() or None
		out[c.name] = {
			"address": street or primary or None,
			"territory": cstr(c.territory or "").strip() or None,
			"zone": zone,
		}
	return out


def _customer_delivery_zone_map(customer_names):
	"""customer → Address.custom_zone only (committed TMS codes). No Territory fallback."""
	out = {}
	names = list({cstr(c).strip() for c in (customer_names or []) if cstr(c).strip()})
	if not names:
		return out
	if not frappe.db.has_column("Address", "custom_zone"):
		return {n: None for n in names}
	customers = frappe.get_all(
		"Customer",
		filters={"name": ["in", names]},
		fields=["name", "customer_primary_address"],
		ignore_permissions=True,
	)
	addr_names = [c.customer_primary_address for c in customers if c.customer_primary_address]
	addr_zone = {}
	if addr_names:
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", addr_names]},
			fields=["name", "custom_zone"],
			ignore_permissions=True,
		):
			addr_zone[row.name] = cstr(row.custom_zone or "").strip() or None
	for c in customers:
		out[c.name] = addr_zone.get(c.customer_primary_address) if c.customer_primary_address else None
	for n in names:
		out.setdefault(n, None)
	return out


def _preventa_stage_label_map(owner_user=None) -> dict:
	"""Preventa column key → display label (board template or per-seller columns)."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.preventa_api import (
			_load_board_columns,
			_load_preventa_settings,
		)

		cols = []
		if owner_user:
			try:
				cols = _load_board_columns(owner_user) or []
			except Exception:
				cols = []
		if not cols:
			cols = list((_load_preventa_settings() or {}).get("default_columns_template") or [])
		out = {}
		for col in cols:
			if not isinstance(col, dict):
				continue
			key = cstr(col.get("key") or "").strip()
			if not key:
				continue
			out[key] = cstr(col.get("label") or key).strip() or key
		return out
	except Exception:
		return {}


def _customer_preventa_stage_map(customer_names) -> dict:
	"""customer → {stage, stage_label} from linked Lead.custom_preventa_stage."""
	out = {}
	names = list({cstr(c).strip() for c in (customer_names or []) if cstr(c).strip()})
	if not names:
		return out
	if not frappe.db.has_column("Customer", "lead_name"):
		return {n: {"stage": None, "stage_label": None} for n in names}
	customers = frappe.get_all(
		"Customer",
		filters={"name": ["in", names]},
		fields=["name", "lead_name"],
		ignore_permissions=True,
	)
	lead_ids = [c.lead_name for c in customers if c.lead_name]
	stage_by_lead = {}
	if lead_ids and frappe.db.has_column("Lead", "custom_preventa_stage"):
		for row in frappe.get_all(
			"Lead",
			filters={"name": ["in", lead_ids]},
			fields=["name", "custom_preventa_stage"],
			ignore_permissions=True,
		):
			stage_by_lead[row.name] = cstr(row.custom_preventa_stage or "").strip() or None
	labels = _preventa_stage_label_map()
	for c in customers:
		stage = stage_by_lead.get(c.lead_name) if c.lead_name else None
		out[c.name] = {
			"stage": stage,
			"stage_label": (labels.get(stage) if stage else None) or stage,
		}
	for n in names:
		out.setdefault(n, {"stage": None, "stage_label": None})
	return out


def _tms_zone_visit_days(zone_label):
	label = cstr(zone_label or "").strip()
	if not label:
		return []
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import _load_tms_zones

		payload = _load_tms_zones() or {}
		zones = payload.get("zones") if isinstance(payload, dict) else payload
		if not isinstance(zones, list):
			zones = []
	except Exception:
		return []
	lu = label.upper()
	for z in zones:
		if not isinstance(z, dict):
			continue
		code = cstr(z.get("code") or "").strip()
		name = cstr(z.get("name") or "").strip()
		if code.upper() == lu or name.upper() == lu or lu in name.upper() or (code and code.upper() in lu):
			raw_days = z.get("visit_days") or []
			bases = []
			for d in raw_days:
				b = _weekday_base_label(d)
				if b and b not in bases:
					bases.append(b)
			return bases
	return []


def _next_business_delivery_date(as_of=None):
	"""Tomorrow (lead days) snapped to the next company working day.

	Default blue Entrega when creating / confirming an Orden — not +7 and not
	“next zone visit next week.” Zone visit days remain available via packing /
	zone recompute paths; Orden itself promises the soonest working day.
	"""
	base = getdate(as_of) if as_of else getdate()
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import (
			_load_tms_settings,
			_normalize_working_days,
			_next_due_on_weekdays,
		)

		settings = _load_tms_settings()
		lead = max(1, cint(settings.get("delivery_lead_days") or 1))
		work = _normalize_working_days(settings.get("auto_group_working_days"))
		earliest = add_days(base, lead)
		due = _next_due_on_weekdays(earliest, work, strictly_after=False)
		return str(due or earliest)
	except Exception:
		return str(add_days(base, 1))


def _apply_orden_default_delivery_date(so):
	"""Set blue Entrega to next business day when moving to Orden (if not forced)."""
	if not so or _delivery_date_forced_from_tags(so):
		return None
	due = _next_business_delivery_date(
		as_of=so.transaction_date or None
	)
	try:
		due_d = getdate(due)
	except Exception:
		return None
	if so.transaction_date and due_d < getdate(so.transaction_date):
		due_d = getdate(so.transaction_date)
	so.db_set("delivery_date", due_d, update_modified=True)
	try:
		frappe.db.sql(
			"""
			UPDATE `tabSales Order Item`
			SET delivery_date=%s
			WHERE parent=%s
			""",
			(due_d, so.name),
		)
	except Exception:
		pass
	_update_guest_preorder_tag(so, "delivery_forced", None)
	return str(due_d)


def _auto_delivery_date_for_zone(zone_label, as_of=None):
	"""Next promised due from zone visit days, or greedy pack when strategy says so (i043/i045).

	When the zone has no visit days (or lookup fails), fall back to next business
	day — never a blind +7 calendar week.
	"""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import (
			_load_tms_settings,
			_packing_strategy,
			propose_due_for_order,
		)

		settings = _load_tms_settings()
		if _packing_strategy(settings) == "fast_deliver_greedy":
			out = propose_due_for_order(zone=zone_label)
			due = (out or {}).get("proposed_due_date")
			if due:
				return str(due)
	except Exception:
		pass

	zoned = _zone_visit_due_date(zone_label, as_of=as_of)
	if zoned:
		return zoned
	return _next_business_delivery_date(as_of=as_of)


def _zone_visit_due_date(zone_label, as_of=None):
	"""Pure zone visit-day next due (no packing_strategy branch — safe for greedy fallback)."""
	days = _tms_zone_visit_days(zone_label)
	if not days:
		return None
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import (
			_load_tms_settings,
			_next_due_on_weekdays,
		)

		settings = _load_tms_settings()
		lead = cint(settings.get("delivery_lead_days") or 1)
		base = getdate(as_of) if as_of else getdate()
		earliest = add_days(base, max(0, lead))
		due = _next_due_on_weekdays(earliest, days, strictly_after=False)
		return str(due) if due else None
	except Exception:
		return None


def _zone_matching_weekday(weekday_label, prefer=None):
	"""Pick a TMS zone that visits ``weekday_label`` (Mon..Sun), preferring ``prefer``."""
	wanted = _weekday_base_label(weekday_label)
	if not wanted:
		return None
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import _load_tms_zones

		payload = _load_tms_zones() or {}
		zones = payload.get("zones") if isinstance(payload, dict) else payload
		if not isinstance(zones, list):
			zones = []
	except Exception:
		return None
	prefer_u = cstr(prefer or "").strip().upper()
	matches = []
	for z in zones:
		if not isinstance(z, dict):
			continue
		days = [_weekday_base_label(d) for d in (z.get("visit_days") or [])]
		if wanted not in days:
			continue
		code = cstr(z.get("code") or "").strip()
		name = cstr(z.get("name") or "").strip()
		label = code or name
		if prefer_u and (code.upper() == prefer_u or name.upper() == prefer_u):
			return label
		matches.append(label)
	return matches[0] if matches else None


def _set_customer_zone_and_address(customer, *, territory=None, zone=None, address_line1=None):
	"""Update Customer.territory / primary address / Address.custom_zone.

	When ``zone`` is passed (including empty string), Address.custom_zone is set
	or cleared. Empty ``zone`` clears the delivery zone without touching territory.
	"""
	cust_name = cstr(customer or "").strip()
	if not cust_name or not frappe.db.exists("Customer", cust_name):
		frappe.throw(_("Customer {0} not found").format(cust_name or "?"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Customer", cust_name)
	frappe.flags.ignore_permissions = False
	if territory is not None:
		doc.territory = cstr(territory or "").strip() or doc.territory
	if doc.territory:
		_ensure_territory(doc.territory)
	zone_explicit = zone is not None
	zone_val = cstr(zone if zone is not None else territory or "").strip()
	if address_line1 is not None:
		_update_customer_primary_address_line(doc, cstr(address_line1).strip())
	doc.save(ignore_permissions=True)
	if zone_explicit or zone_val:
		addr_name = doc.customer_primary_address
		if zone_val and not addr_name:
			# Zone needs an Address row; create a minimal primary address.
			_update_customer_primary_address_line(doc, "-")
			doc.save(ignore_permissions=True)
			addr_name = doc.customer_primary_address
		if addr_name and frappe.db.exists("Address", addr_name) and frappe.db.has_column(
			"Address", "custom_zone"
		):
			frappe.db.set_value(
				"Address", addr_name, "custom_zone", zone_val or "", update_modified=True
			)
		# Keep territory aligned with delivery zona when CRM territory is empty or zone-like.
		if zone_val and (not doc.territory or doc.territory == "All Territories"):
			if frappe.db.exists("Territory", zone_val):
				frappe.db.set_value("Customer", cust_name, "territory", zone_val, update_modified=True)
			elif territory is not None and cstr(territory).strip():
				pass
			else:
				# Store zone label on territory only when Territory master has it.
				pass
	return _customer_address_zone_map([cust_name]).get(cust_name) or {}


def _item_line_weight_fields(
	item_code,
	line_uom=None,
	line_weight_per_unit=None,
	line_total_weight=None,
	line_qty=None,
):
	stock_uom, weight_per_unit, weight_uom = "", 0.0, ""
	unit_weight_min, unit_weight_max = None, None
	unit = ""
	try:
		fields = ["stock_uom", "weight_per_unit", "weight_uom"]
		if frappe.db.has_column("Item", "custom_unit"):
			fields.append("custom_unit")
		if frappe.db.has_column("Item", "custom_unit_weight_min"):
			fields.append("custom_unit_weight_min")
		if frappe.db.has_column("Item", "custom_unit_weight_max"):
			fields.append("custom_unit_weight_max")
		row = frappe.db.get_value("Item", item_code, fields, as_dict=True) if item_code else None
		if row:
			stock_uom = row.get("stock_uom") or ""
			weight_per_unit = float(row.get("weight_per_unit") or 0)
			weight_uom = row.get("weight_uom") or ""
			unit = row.get("custom_unit") or ""
			if row.get("custom_unit_weight_min") not in (None, ""):
				unit_weight_min = float(row.get("custom_unit_weight_min") or 0)
			if row.get("custom_unit_weight_max") not in (None, ""):
				unit_weight_max = float(row.get("custom_unit_weight_max") or 0)
	except Exception:
		pass
	sell_uom = cstr(line_uom or "").strip() or stock_uom
	uoms = {
		cstr(stock_uom).strip().lower(),
		cstr(weight_uom).strip().lower(),
		cstr(unit).strip().lower(),
		cstr(sell_uom).strip().lower(),
	}
	weight_tokens = {
		"weight",
		"por peso",
		"porpeso",
		"kg",
		"kgs",
		"g",
		"gr",
		"gram",
		"grams",
		"l",
		"lt",
		"ml",
		"milliliter",
	}
	is_weight_based = bool(uoms & weight_tokens)
	# Prefer line-level pack weight / measured total (remeasure) over Item master.
	line_wpu = flt(line_weight_per_unit) if line_weight_per_unit not in (None, "") else 0.0
	if line_wpu > 0:
		weight_per_unit = line_wpu
	qty = flt(line_qty)
	total_weight = flt(line_total_weight) if line_total_weight not in (None, "") else 0.0
	if total_weight <= 0 and weight_per_unit > 0 and qty > 0:
		total_weight = weight_per_unit * qty
	return {
		# Prefer the Sales Order line UOM so Pedidos "Tipo de peso" persists after save.
		"stock_uom": sell_uom or stock_uom,
		"uom": sell_uom or stock_uom,
		"item_stock_uom": stock_uom,
		"weight_uom": weight_uom,
		"weight_per_unit": weight_per_unit,
		"unit_weight_min": unit_weight_min,
		"unit_weight_max": unit_weight_max,
		"total_weight": total_weight or None,
		"unit": unit,
		"is_weight_based": is_weight_based,
	}


def _normalize_preorder_line_uom(raw_uom):
	"""Map UI labels (WEIGHT / NOS (single) / CAJA) to ERPNext UOM master names."""
	from erpnext.erpnext_integrations.ecommerce_api.product_manager import _normalize_stock_uom

	return _normalize_stock_uom(cstr(raw_uom or "").strip() or None)


def _weight_sell_uom_tokens():
	return {
		"weight",
		"por peso",
		"porpeso",
		"kg",
		"kgs",
		"g",
		"gr",
		"gram",
		"grams",
		"l",
		"lt",
		"ml",
		"milliliter",
	}


def _is_weight_sell_uom(uom):
	return cstr(uom or "").strip().lower() in _weight_sell_uom_tokens()


@contextmanager
def _allow_weight_fractional_stock_qty(so):
	"""Re-align stock_uom after set_missing resets it to Item Nos (whole-number check).

	Also rebills WEIGHT lines as $/kg × kg during calculate_taxes_and_totals so
	Pedidos importe / grand_total match the UI (not qty × rate).
	"""
	from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals as _CTT
	from erpnext.utilities import transaction_base as tb

	orig_uom = tb.validate_uom_is_integer
	orig_civ = _CTT.calculate_item_values

	def _patched_uom(doc, uom_field, qty_fields, child_dt=None):
		if doc is so:
			for row in doc.get("items") or []:
				if _is_weight_sell_uom(row.uom):
					row.stock_uom = row.uom
					row.conversion_factor = 1.0
					row.stock_qty = flt(row.qty)
		return orig_uom(doc, uom_field, qty_fields, child_dt)

	def _patched_civ(self):
		orig_civ(self)
		if self.doc is not so and not getattr(getattr(self.doc, "flags", None), "ecommerce_weight_sell", False):
			return
		for item in self.doc.get("items") or []:
			uom = cstr(getattr(item, "uom", None) or getattr(item, "stock_uom", None) or "")
			if not _is_weight_sell_uom(uom):
				continue
			bill = _weight_billable_qty(item)
			if bill <= 0:
				continue
			rate = _weight_sell_effective_rate(item)
			new_amount = flt(rate * bill, item.precision("amount"))
			if abs(flt(item.amount) - new_amount) < 1e-9:
				continue
			item.amount = new_amount
			item.net_amount = new_amount
			self._set_in_company_currency(item, ["amount", "net_amount"])

	tb.validate_uom_is_integer = _patched_uom
	_CTT.calculate_item_values = _patched_civ
	so.flags.ecommerce_weight_sell = True
	try:
		yield
	finally:
		tb.validate_uom_is_integer = orig_uom
		_CTT.calculate_item_values = orig_civ
		so.flags.ecommerce_weight_sell = False


def _weight_billable_qty(row) -> float:
	"""Kg (or qty) used to bill a WEIGHT line — mirrors frontend preorderLineBillableQty."""
	uom = cstr(getattr(row, "uom", None) or getattr(row, "stock_uom", None) or "")
	qty = flt(getattr(row, "qty", None) or 0)
	if not _is_weight_sell_uom(uom):
		return qty
	tw = flt(getattr(row, "total_weight", None) or 0)
	if tw > 0:
		return tw
	wpu = flt(getattr(row, "weight_per_unit", None) or 0)
	if wpu > 0 and qty > 0:
		return flt(qty * wpu)
	return qty


def _weight_sell_effective_rate(row) -> float:
	"""$/kg used for WEIGHT importe.

	ERPNext only applies ``discount_percentage`` when price_list_rate / margin is set.
	Pedidos often has neither — apply disc here so importe matches the UI
	(``rate × kg × (1 - disc%)``). When ERPNext already netted ``rate``,
	``discount_amount`` is set and we must not double-discount.
	"""
	rate = flt(getattr(row, "rate", None) or 0)
	disc = flt(getattr(row, "discount_percentage", None) or 0)
	if disc <= 0 or disc >= 100:
		return 0.0 if disc >= 100 else rate
	already_net = flt(getattr(row, "discount_amount", None) or 0) > 0 or (
		flt(getattr(row, "price_list_rate", None) or 0) > 0
		and abs(flt(getattr(row, "price_list_rate")) - rate) > 1e-9
	)
	if already_net:
		return rate
	return flt(rate * (1.0 - disc / 100.0))


def _guest_preorder_line_amount(row) -> float:
	"""Line importe for Pedidos: WEIGHT → rate×kg; else rate×qty (or stored amount)."""
	uom = cstr(getattr(row, "uom", None) or getattr(row, "stock_uom", None) or "")
	if _is_weight_sell_uom(uom):
		bill = _weight_billable_qty(row)
		rate = _weight_sell_effective_rate(row)
		if rate > 0 and bill > 0:
			return flt(rate * bill)
		return flt(getattr(row, "amount", None) or 0)
	rate = flt(getattr(row, "rate", None) or 0)
	qty = flt(getattr(row, "qty", None) or 0)
	disc = flt(getattr(row, "discount_percentage", None) or 0)
	if rate > 0 and qty > 0:
		already_net = flt(getattr(row, "discount_amount", None) or 0) > 0
		eff = rate if already_net or disc <= 0 else rate * (1.0 - disc / 100.0)
		return flt(eff * qty)
	return flt(getattr(row, "amount", None) or 0)


def _guest_preorder_estimated_total(so) -> float:
	total = sum(_guest_preorder_line_amount(d) for d in (so.items or []))
	return max(flt(total) - flt(getattr(so, "additional_discount_amount", None) or 0), 0.0)


def _calculate_guest_preorder_totals(so):
	"""ERPNext qty×rate, then WEIGHT rebilled as $/kg × kg."""
	with _allow_weight_fractional_stock_qty(so):
		so.run_method("calculate_taxes_and_totals")
	# Submitted edits save with ignore_validate_update_after_submit, which skips
	# the validate steps that refresh these — the print showed the old rate/total.
	for row in so.items or []:
		row.stock_uom_rate = flt(row.rate) / (flt(row.conversion_factor) or 1)
	if hasattr(so, "set_total_in_words"):
		so.set_total_in_words()


def _repair_guest_preorder_weight_totals(so) -> bool:
	"""Persist WEIGHT $/kg×kg totals when SO still has stale qty×rate money."""
	est = _guest_preorder_estimated_total(so)
	if abs(flt(so.grand_total) - est) < 0.01:
		return False
	has_weight = any(
		_is_weight_sell_uom(cstr(getattr(d, "uom", None) or getattr(d, "stock_uom", None) or ""))
		and _weight_billable_qty(d) > flt(getattr(d, "qty", None) or 0) + 1e-9
		for d in (so.items or [])
	)
	if not has_weight:
		return False
	so.flags.ignore_pricing_rule = True
	if cint(so.docstatus) == 1:
		so.flags.ignore_validate_update_after_submit = True
	_calculate_guest_preorder_totals(so)
	_save_guest_preorder_so(so)
	frappe.db.commit()
	so.reload()
	return True


def _apply_so_line_uom(row, raw_uom):
	"""Set Sales Order Item.uom so fractional qty is allowed when selling by WEIGHT.

	ERPNext also validates stock_qty against stock_uom (usually Nos). For Armado /
	WEIGHT sells we align stock_uom to the sell UOM with conversion_factor=1 so
	fractional kg does not trip 'Must be Whole Number' on Nos.
	"""
	if raw_uom is None and not cstr(getattr(row, "uom", "") or "").strip():
		return
	uom = _normalize_preorder_line_uom(raw_uom if raw_uom is not None else row.uom)
	if not uom:
		return
	row.uom = uom
	if _is_weight_sell_uom(uom):
		row.stock_uom = uom
		row.conversion_factor = 1.0
		row.stock_qty = flt(row.qty)
		return
	try:
		from erpnext.stock.get_item_details import get_conversion_factor

		row.conversion_factor = flt(
			get_conversion_factor(row.item_code, uom).get("conversion_factor") or 1.0
		)
	except Exception:
		row.conversion_factor = flt(getattr(row, "conversion_factor", None) or 1.0)
	# Restore item stock UOM when leaving WEIGHT back to Nos/CAJA.
	item_stock = frappe.db.get_value("Item", row.item_code, "stock_uom") or "Nos"
	row.stock_uom = item_stock
	row.stock_qty = flt(row.qty) * flt(row.conversion_factor)


def _update_guest_preorder_tag(so, key: str, value: str | None) -> None:
	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return
	raw = getattr(so, tag_fn, None) or ""
	prefix = f"{key}:"
	parts = [p for p in str(raw).split("|") if p.strip() and not p.strip().startswith(prefix)]
	clean = _sanitize_guest_tag(value)
	if clean:
		parts.append(f"{key}:{clean}")
	setattr(so, tag_fn, " | ".join(p.strip() for p in parts if p.strip()))


def _resolve_order_cashier(cashier_id=None) -> str | None:
	cid = str(cashier_id or "").strip()
	if cid:
		return cid
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import _acting_user

	user = (_acting_user() or "").strip()
	if not user or user in ("Guest", "Administrator"):
		return None
	emp_name = frappe.db.get_value("Employee", {"user_id": user}, "employee_name")
	return (emp_name or user).strip() or None


def _orders_visibility_mode() -> str:
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import _load_pin_settings

	mode = (_load_pin_settings().get("orders_visibility_mode") or "own_only").strip()
	if mode not in ("own_only", "group", "all_tagged"):
		return "own_only"
	return mode


def _allowed_cashiers_for_viewer(viewer: str, mode: str) -> set[str] | None:
	viewer = (viewer or "").strip()
	if not viewer:
		return set()
	if mode == "all_tagged":
		return None
	if mode == "group":
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
			cashier_names_in_shared_groups,
		)

		names = cashier_names_in_shared_groups(viewer)
		return set(names) if names else {viewer}
	return {viewer}


def _guest_preorder_visible_to_viewer(order: dict, viewer: str, mode: str) -> bool:
	cashier = _cashier_from_guest_preorder(order)
	if mode == "all_tagged":
		return bool(cashier)
	if not cashier:
		return False
	allowed = _allowed_cashiers_for_viewer(viewer, mode)
	if allowed is None:
		return bool(cashier)
	return cashier in allowed


def _can_view_all_guest_preorders() -> bool:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	return _can_app("tables.orders")


@frappe.whitelist(allow_guest=True)
def create_guest_preorder(
	items,
	guest_phone=None,
	guest_name=None,
	guest_email=None,
	price_list="Standard Selling",
	delivery_date=None,
	order_type="Sales",
	company=None,
	guest_address=None,
	guest_notes=None,
	guest_cuil=None,
	guest_preferred_hours=None,
	guest_observation=None,
	is_delivery=0,
	paid_amount=None,
	mode_of_payment=None,
	customer=None,
	order_tag=None,
	seller_ref_user=None,
	cashier_id=None,
	pin_allowed_countries=None,
	send_client_pin=1,
	initial_status=None,
):
	"""
	Create a draft Sales Order to represent a guest preorder (no payment).

	This is intentionally implemented as a normal Sales Order left in Draft
	docstatus so the owner can later confirm/prepare it.

	Uses a valid SilkOS ``order_type`` (default ``Sales``). The flow is identified
	via ``remarks`` containing ``guest_preorder=1``.

	``customer``: optional Customer override for known parties (e.g. seed data,
	a repeat order placed from the client tracking portal for a known
	customer) - default behaviour (anonymous guest -> "Consumidor Final") is
	unchanged when omitted.

	``initial_status``: optional pipeline status after create. Default leaves
	the SO as Consulta (draft). Pass ``Orden`` (e.g. Operaciones → Orden page)
	to submit and skip Inquiry.

	Returns: { preorder_name, estimated_total, currency, status }
	"""
	if isinstance(items, str):
		import json
		items = json.loads(items)

	notes_only = (
		bool(cstr(guest_notes or "").strip())
		or bool(cstr(guest_observation or "").strip())
	) and not items

	if not items and not notes_only:
		frappe.throw(_("Cart is empty"))

	# Resolve / validate lines early (before request-bound helpers) so missing
	# SKUs return a controlled ValidationError instead of Link DoesNotExist 404.
	missing = []
	resolved_rows = []
	for item in items or []:
		if not isinstance(item, dict):
			continue
		item_code = str(item.get("item_code") or "").strip()
		qty = flt(item.get("qty", 1))
		rate = flt(item.get("rate", 0))
		wpu = item.get("weight_per_unit")
		wpu_val = flt(wpu) if wpu not in (None, "") else None
		if not item_code:
			continue
		if not frappe.db.exists("Item", item_code):
			alts = _item_codes_for_barcode(item_code)
			if alts:
				item_code = alts[0]
			else:
				missing.append(item_code)
				continue
		resolved_rows.append(
			{
				"item_code": item_code,
				"qty": qty,
				"rate": rate,
				"weight_per_unit": wpu_val,
			}
		)

	if missing:
		frappe.throw(_("Item(s) not found: {0}").format(", ".join(missing)), frappe.ValidationError)

	if not resolved_rows and not notes_only:
		frappe.throw(_("Cart is empty"))

	# Resolve defaults
	if not company:
		from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

		company = resolve_company() or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("No Company configured"))

	# Explicit customer wins; otherwise match by phone / local (non-empty only).
	customer = _resolve_consulta_customer(
		explicit=customer,
		guest_phone=guest_phone,
		guest_address=guest_address,
	)
	company_currency = frappe.db.get_value("Company", company, "default_currency") or "ARS"
	price_list_currency = (
		frappe.db.get_value("Price List", price_list, "currency") if price_list else None
	)
	# A price list with no currency makes ERPNext look up None → ARS. Consultas
	# are local quotes: use company currency and skip Currency Exchange.
	if price_list and not price_list_currency:
		frappe.db.set_value("Price List", price_list, "currency", company_currency)
		price_list_currency = company_currency
	price_list_currency = price_list_currency or company_currency

	# Create Sales Order in Draft (do NOT submit)
	so = frappe.new_doc("Sales Order")
	so.customer = customer
	# Must be one of the site's allowed Sales Order order types (commonly Sales, Shopping Cart, …).
	so.order_type = order_type or "Sales"
	so.transaction_date = nowdate()
	so.delivery_date = delivery_date or _next_business_delivery_date()
	# ERPNext: delivery ≥ order date. A past order/delivery date (CSV backfill,
	# replayed offline order) is clamped to the transaction date, not rejected.
	try:
		if getdate(so.delivery_date) < getdate(so.transaction_date):
			so.delivery_date = so.transaction_date
	except Exception:
		so.delivery_date = _next_business_delivery_date()
	so.company = company
	so.selling_price_list = price_list
	# Consultas are local quotes. Do not look up Currency Exchange (None → ARS).
	so.currency = company_currency
	so.conversion_rate = 1
	so.price_list_currency = price_list_currency
	so.plc_conversion_rate = 1
	# Catalog/consulta already resolved Price-promo conflicts and sent line rates —
	# do not re-apply Pricing Rules on insert (would 417 on equal-priority overlaps).
	so.ignore_pricing_rule = 1
	so.flags.ignore_pricing_rule = True

	remarks_parts = [GUEST_PREORDER_REMARKS_TAG, f"customer:{customer}"]
	if guest_phone:
		remarks_parts.append(f"guest_phone:{_sanitize_guest_tag(guest_phone)}")
	if guest_name:
		remarks_parts.append(f"guest_name:{_sanitize_guest_tag(guest_name)}")
	if guest_email:
		remarks_parts.append(f"guest_email:{_sanitize_guest_tag(guest_email)}")
	if cint(is_delivery):
		remarks_parts.append("delivery:1")
	if guest_address:
		remarks_parts.append(f"guest_address:{_sanitize_guest_tag(guest_address)}")
	if guest_notes:
		remarks_parts.append(f"guest_notes:{_sanitize_guest_tag(guest_notes)}")
	if guest_cuil:
		remarks_parts.append(f"guest_cuil:{_sanitize_guest_tag(guest_cuil)}")
	if guest_preferred_hours:
		remarks_parts.append(
			f"guest_preferred_hours:{_sanitize_guest_tag(guest_preferred_hours)}"
		)
	if guest_observation:
		remarks_parts.append(f"guest_observation:{_sanitize_guest_tag(guest_observation)}")
	if mode_of_payment:
		remarks_parts.append(f"guest_pay_method:{_sanitize_guest_tag(mode_of_payment)}")
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_acting_username,
		_normalize_order_tag,
	)

	acting = _acting_username()
	if acting:
		remarks_parts.append(f"order_owner:{str(acting).replace('|', '')[:140]}")
	tag_slug = _normalize_order_tag(order_tag)
	if tag_slug:
		remarks_parts.append(f"order_tag:{tag_slug}")
	cashier = _resolve_order_cashier(cashier_id)
	if cashier:
		remarks_parts.append(f"cashier:{_sanitize_guest_tag(cashier)}")
	# Salesman / referrer from the public /s/{slug} link cookie (catalog attribution).
	seller_ref = cstr(seller_ref_user or "").strip()
	if seller_ref and seller_ref.lower() not in ("null", "undefined", "none"):
		remarks_parts.append(f"seller_ref:{_sanitize_guest_tag(seller_ref)}")
	# Preferred salida warehouse (TMS depot) — default company/only warehouse.
	default_wh = _default_company_warehouse(company)
	if default_wh:
		remarks_parts.append(f"warehouse:{_sanitize_guest_tag(default_wh)}")
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

	for row in resolved_rows:
		item_code = row["item_code"]
		qty = row["qty"]
		rate = row["rate"]

		# Get item rate if not provided or zero (mirrors create_order logic)
		if not rate:
			item_details = get_item_details_base({
				"item_code": item_code,
				"customer": customer,
				"company": company,
				"selling_price_list": price_list,
				"currency": company_currency,
				"conversion_rate": 1,
				"price_list_currency": price_list_currency,
				"plc_conversion_rate": 1,
				"doctype": "Sales Order",
				"ignore_pricing_rule": 1,
			})
			rate = item_details.get("price_list_rate", 0)

		line_kwargs = {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"delivery_date": so.delivery_date,
		}
		# Soft pack weight for mayorista WEIGHT estimates (importe before armado remide).
		wpu = row.get("weight_per_unit")
		if wpu is None and frappe.db.has_column("Item", "weight_per_unit"):
			wpu = flt(frappe.db.get_value("Item", item_code, "weight_per_unit") or 0) or None
		if wpu and flt(wpu) > 0:
			line_kwargs["weight_per_unit"] = flt(wpu)
			line_kwargs["total_weight"] = flt(wpu) * flt(qty)

		so.append("items", line_kwargs)

	# Calculate totals. Re-stamp so insert/validate cannot treat currency as None.
	so.currency = company_currency
	so.conversion_rate = 1
	so.price_list_currency = price_list_currency
	so.plc_conversion_rate = 1
	if resolved_rows:
		_calculate_guest_preorder_totals(so)

	# Save as Draft (Consulta). Stamp the acting cashier so Pedidos propios can match.
	if acting and frappe.db.exists("User", acting):
		so.owner = acting
	# Notes-only inquiries have no item rows — skip mandatory child-table / item checks.
	if notes_only:
		so.flags.ignore_validate = True
	# Catalog already sent line rates. Stock Settings auto-insert Item Price on
	# validate otherwise msgprints / duplicates → HTTP 417 "Item Price added…".
	prev_skip_price = frappe.flags.get("skip_auto_insert_item_price")
	prev_mute = frappe.flags.get("mute_messages")
	frappe.flags.skip_auto_insert_item_price = True
	frappe.flags.mute_messages = True
	try:
		with _allow_weight_fractional_stock_qty(so):
			so.insert(ignore_permissions=True, ignore_mandatory=notes_only)
	finally:
		frappe.flags.skip_auto_insert_item_price = prev_skip_price
		frappe.flags.mute_messages = prev_mute
	if acting and frappe.db.exists("User", acting) and so.owner != acting:
		so.db_set("owner", acting)

	try:
		from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import (
			maybe_mark_creation_review_pending,
		)

		# Prefer the stamped owner / acting cashier over the API-key session user.
		maybe_mark_creation_review_pending(
			"Sales Order",
			so.name,
			actor=acting or getattr(so, "owner", None),
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "creation_review mark sales order")

	paid = flt(paid_amount)
	if paid > 0:
		cap = flt(so.grand_total)
		so.db_set("advance_paid", min(paid, cap) if cap > 0 else paid)

	# Mint PIN in-request (fast DB) so catalog can stash it locally; Twilio send
	# + email + Preventa + remito run after the HTTP response (see side effects).
	client_access = None
	if cstr(guest_phone or "").strip():
		try:
			from erpnext.erpnext_integrations.ecommerce_api.client_access_api import (
				issue_client_access_pin,
			)

			client_access = issue_client_access_pin(
				guest_phone=guest_phone,
				guest_name=guest_name,
				guest_email=guest_email,
				customer=so.customer,
				allowed_countries=pin_allowed_countries,
				send=0,
			)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Client access PIN failed for {so.name}")

	# Operaciones → Orden: submit now; remito/geocode deferred (was the long stall).
	# Sellers may create Orden here; later edits stay suggestion-only.
	want_orden = _normalize_create_initial_status(initial_status) == "Orden"
	promoted = None
	if want_orden and resolved_rows:
		frappe.flags.creating_guest_preorder = True
		try:
			promoted = set_guest_preorder_status(
				so.name, "Orden", ensure_planner_remito=0
			)
		finally:
			frappe.flags.creating_guest_preorder = False
		so_name = cstr((promoted or {}).get("name") or so.name)
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", so_name)
		frappe.flags.ignore_permissions = False

	# Link the customer to the acting seller so Orden → Pedidos (assigned clients) lists it.
	if (want_orden or tag_slug == "orden") and so.customer:
		assign_uid = cstr(acting or seller_ref or "").strip()
		if assign_uid and assign_uid not in ("Guest", "Administrator"):
			try:
				from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
					_set_customer_salesmen_users,
					customer_salesmen,
				)

				users = list(customer_salesmen(so.customer) or [])
				if assign_uid not in users:
					users.append(assign_uid)
					_set_customer_salesmen_users(so.customer, users)
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					f"orden assign salesman for {so.customer}",
				)

	payload = {
		"preorder_name": so.name,
		"estimated_total": flt(so.grand_total),
		"currency": so.currency,
		"status": so.status,
		"advance_paid": flt(so.advance_paid) if paid > 0 else 0,
		"side_effects_queued": True,
	}
	if want_orden and resolved_rows:
		payload["display_status"] = "Orden"
		# Remito not ready yet — planner will pick it up after the background job.
		payload["planner_ready"] = False
		if isinstance(promoted, dict):
			for key in (
				"not_deliverable",
				"delivery_warning",
				"delivery_note",
				"geocoded",
				"warnings",
				"address",
			):
				if key in promoted and promoted.get(key) is not None:
					payload[key] = promoted.get(key)
	if client_access and client_access.get("ok"):
		payload["client_access"] = {
			"pin": client_access.get("pin"),
			"phone_e164": client_access.get("phone_e164"),
			"is_new_number": client_access.get("is_new_number"),
			"portal_path": client_access.get("portal_path") or "/cliente",
			"sent": False,
			"channel": None,
		}

	_enqueue_create_guest_preorder_side_effects(
		so_name=so.name,
		ensure_remito=1 if (want_orden and resolved_rows) else 0,
		guest_name=guest_name,
		guest_phone=guest_phone,
		guest_email=guest_email,
		seller_ref_user=seller_ref_user,
		pin_allowed_countries=pin_allowed_countries,
		send_client_pin=cint(send_client_pin),
		client_access_pin=(client_access or {}).get("pin") if client_access else None,
		phone_e164=(client_access or {}).get("phone_e164") if client_access else None,
	)
	return payload


def _normalize_seller_scope(raw) -> str:
	"""'' | mine | assigned | orden_here — Pedidos table / Orden FAB filters."""
	val = cstr(raw or "").strip().lower()
	if val in ("", "all", "null", "undefined", "none", "*", "0", "todos"):
		return ""
	if val in ("mine", "my", "seller", "seller_ref", "mi_enlace", "link"):
		return "mine"
	if val in (
		"assigned",
		"clients",
		"clientes",
		"mis_clientes",
		"my_clients",
		"salesman_clients",
	):
		return "assigned"
	if val in ("orden_here", "orden", "here", "ordered_here", "ops_orden", "pedidos"):
		return "orden_here"
	return ""


def _is_assigned_client_seller_scope(seller_scope: str) -> bool:
	"""True for scopes that list SOs for the acting seller's assigned clients."""
	return seller_scope in ("assigned", "orden_here")


def _orden_pedidos_window_days() -> int:
	"""Admin-configurable lookback for Pedidos table + Orden FAB (default 30)."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import get_shop_ui_settings

		bundle = get_shop_ui_settings() or {}
		settings = bundle.get("settings") if isinstance(bundle, dict) else None
		if not isinstance(settings, dict):
			settings = bundle if isinstance(bundle, dict) else {}
		catalog = settings.get("catalogDisplay") if isinstance(settings, dict) else {}
		if not isinstance(catalog, dict):
			catalog = {}
		days = cint(catalog.get("ordenPedidosDays"))
		if days <= 0:
			return 30
		return max(1, min(365, days))
	except Exception:
		return 30


def _attach_creation_review_fields(rows: list) -> None:
	"""Attach creation_review (+ review_reason / review_actor) onto list/detail rows.

	Also attaches ``customer_creation_review`` / ``customer_review_actor`` so the
	Cliente column only badges when the *Customer* itself is Pending in Revisar.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import FIELDNAME

	names = [cstr(r.get("name") or "").strip() for r in (rows or []) if r.get("name")]
	names = [n for n in names if n]
	has_so_col = bool(names and frappe.db.has_column("Sales Order", FIELDNAME))
	status_map = {}
	if has_so_col:
		status_map = {
			r.name: getattr(r, FIELDNAME, None)
			for r in frappe.get_all(
				"Sales Order",
				filters={"name": ["in", names]},
				fields=["name", FIELDNAME],
				ignore_permissions=True,
			)
		}

	cust_names = sorted(
		{
			cstr(r.get("customer") or "").strip()
			for r in (rows or [])
			if cstr(r.get("customer") or "").strip()
		}
	)
	cust_status = {}
	cust_owner = {}
	if cust_names and frappe.db.has_column("Customer", FIELDNAME):
		for row in frappe.get_all(
			"Customer",
			filters={"name": ["in", cust_names]},
			fields=["name", FIELDNAME, "owner"],
			ignore_permissions=True,
		):
			cust_status[row.name] = getattr(row, FIELDNAME, None)
			cust_owner[row.name] = cstr(row.owner or "").strip() or None

	for r in rows or []:
		name = cstr(r.get("name") or "").strip()
		tags = _parse_remarks_tags(_guest_preorder_tag_text(r))
		r["creation_review"] = status_map.get(name) if has_so_col else (r.get("creation_review") or None)
		if r.get("review_reason") is None:
			r["review_reason"] = tags.get("review_reason") or None
		# Who parked the SO (Pedidos pipeline badge ``r:josefi``).
		actor = (
			cstr(tags.get("review_actor") or "").strip()
			or cstr(tags.get("order_owner") or "").strip()
			or cstr(tags.get("seller_ref") or "").strip()
			or cstr(r.get("suggestion_actor") or "").strip()
			or None
		)
		r["review_actor"] = actor or None
		cust = cstr(r.get("customer") or "").strip()
		r["customer_creation_review"] = cust_status.get(cust) if cust else None
		r["customer_review_actor"] = cust_owner.get(cust) if cust else None


def _requeue_seller_amend_review(preorder_name: str, reason: str = "seller_amend") -> bool:
	"""Park SO in admin Revisar after a non-admin seller material amend/edit."""
	if frappe.flags.get("applying_creation_review_suggestion"):
		return False
	try:
		from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import (
			mark_seller_amend_for_review,
		)
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username

		actor = cstr(_acting_username() or "").strip() or cstr(frappe.session.user or "").strip()
		return bool(
			mark_seller_amend_for_review(
				preorder_name,
				actor=actor,
				reason=reason,
			)
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"requeue seller amend review {preorder_name}")
		return False


def _can_live_mutate_guest_preorder() -> bool:
	"""Admins and Pedidos-table staff may mutate SO body live; sellers suggestion-only.

	Create-time promote (Consulta→Orden inside ``create_guest_preorder``) is allowed
	for sellers — they may place new orders; only post-create edits/pipeline moves
	are suggestion-only.
	"""
	if frappe.flags.get("applying_creation_review_suggestion"):
		return True
	if frappe.flags.get("creating_guest_preorder"):
		return True
	from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import (
		is_creation_review_admin,
	)
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	if is_creation_review_admin():
		return True
	return bool(
		_can_app("tables.orders")
		or _can_app("tables.orders.all")
		or _can_app("tables.orders.own")
	)


def _require_guest_preorder_live_mutate(pipeline_source=None):
	"""Block non-admin / non-Pedidos sellers from live SO edits (use suggest API)."""
	if _can_live_mutate_guest_preorder():
		return
	# TMS / kiosk / armado transitions are PIN-gated upstream.
	if pipeline_source:
		return
	frappe.throw(
		_(
			"Sellers may only queue suggestions for Revisar — "
			"direct edits and pipeline changes are not allowed."
		),
		frappe.PermissionError,
	)


def _guest_preorder_within_cutoff(order: dict, cutoff_date) -> bool:
	if cutoff_date is None:
		return True
	try:
		tx = order.get("transaction_date")
		if tx and getdate(tx) < getdate(cutoff_date):
			return False
	except Exception:
		return False
	return True


def _guest_preorder_matches_seller_scope(
	order: dict,
	seller_scope: str,
	uid: str,
	*,
	assigned_customers: set | None = None,
	cutoff_date=None,
) -> bool:
	"""Filter guest preorders by seller_ref / assigned clients for Pedidos + Orden FAB."""
	if not seller_scope:
		return True
	uid = cstr(uid or "").strip()
	if not uid:
		return False
	tags = _parse_remarks_tags(_guest_preorder_tag_text(order))
	seller = cstr(tags.get("seller_ref") or "").strip()
	order_owner = cstr(tags.get("order_owner") or "").strip()
	if seller_scope == "mine":
		return seller == uid
	if seller_scope == "orden_here":
		# Operaciones → Orden FAB: orders this seller placed here, OR for assigned clients.
		cust = cstr(order.get("customer") or "").strip()
		placed_here = seller == uid or order_owner == uid
		assigned_ok = bool(cust and assigned_customers and cust in assigned_customers)
		if not (placed_here or assigned_ok):
			return False
		return _guest_preorder_within_cutoff(order, cutoff_date)
	if seller_scope == "assigned":
		# Pedidos table Mis clientes: guest preorders for assigned clients in window.
		cust = cstr(order.get("customer") or "").strip()
		if not cust or not assigned_customers or cust not in assigned_customers:
			return False
		return _guest_preorder_within_cutoff(order, cutoff_date)
	return True


@frappe.whitelist()
def get_guest_preorders_list(
	status=None,
	start=0,
	page_length=20,
	cashier_id=None,
	scope="pos",
	include_archived=0,
	seller_scope=None,
	customer=None,
):
	"""
	List Guest Preorders created by `create_guest_preorder`.

	Cancelled / Archivado orders are hidden by default (``include_archived=0``).
	Use ERPNext Desk / advanced search to find archived SOs; pass
	``include_archived=1`` only when a UI explicitly needs them.
	Superseded cancelled orders (replaced by an amendment) are always excluded.

	``customer`` — optional Customer name filter (CRM party Pedidos tab).

	``seller_scope``:
	  - ``mine`` — only SOs tagged ``seller_ref:<acting user>`` (seller link)
	  - ``assigned`` — Pedidos for this seller's assigned clients (windowed)
	  - ``orden_here`` — Operaciones → Orden FAB: orders this seller placed
	    (``order_owner`` / ``seller_ref``) **or** for assigned clients (windowed)
	"""
	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return {"preorders": [], "total_count": 0, "window_days": 30}

	customer_filter = cstr(customer or "").strip()
	if customer_filter.lower() in ("null", "undefined", "none", "*"):
		customer_filter = ""

	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_acting_username,
		_assigned_customer_names,
		_can_app,
		_order_visibility_scope,
		guest_preorder_matches_scope,
	)

	seller_scope = _normalize_seller_scope(seller_scope)
	acting_uid = cstr(_acting_username() or frappe.session.user or "").strip()
	if acting_uid in ("Guest", "guest"):
		acting_uid = ""
	window_days = (
		_orden_pedidos_window_days() if _is_assigned_client_seller_scope(seller_scope) else None
	)
	cutoff_date = add_days(getdate(nowdate()), -window_days) if window_days else None
	assigned_customers: set[str] = set()
	if _is_assigned_client_seller_scope(seller_scope) and acting_uid:
		assigned_customers = {
			cstr(n).strip() for n in (_assigned_customer_names(acting_uid) or []) if cstr(n).strip()
		}

	try:
		start = max(0, cint(start))
	except Exception:
		start = 0
	try:
		page_length = cint(page_length)
	except Exception:
		page_length = 20
	if page_length <= 0:
		page_length = 20

	try:
		include_archived = cint(include_archived)
	except Exception:
		include_archived = 0
	if isinstance(include_archived, str) and include_archived.strip().lower() in ("1", "true", "yes"):
		include_archived = 1

	scope = _order_visibility_scope()
	# Sales floor (ops.catalog / preventa) may list Pedidos via Orden FAB without
	# tables.orders.* — seller_scope filters to assigned clients / orders they placed.
	seller_floor_list = bool(
		seller_scope
		and acting_uid
		and (
			_can_app("ops.catalog")
			or _can_app("ops.preventa")
			or _can_app("sales.see_assigned")
		)
	)
	if (
		scope is not None
		and not scope.get("own")
		and not scope.get("tags")
		and not seller_floor_list
	):
		out = {"preorders": [], "total_count": 0}
		if window_days is not None:
			out["window_days"] = window_days
		return out

	# Mis clientes with no assignments → empty (still report the window).
	# orden_here still lists orders this seller placed even without assignments.
	if seller_scope == "assigned" and not assigned_customers:
		return {"preorders": [], "total_count": 0, "window_days": window_days or 30}

	filters = {tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"]}
	if seller_scope == "mine" and acting_uid:
		# Prefer rows that also carry this seller_ref (tag order is not guaranteed).
		filters[tag_fn] = ["like", f"%seller_ref:{acting_uid}%"]
	elif seller_scope == "assigned":
		filters["customer"] = ["in", list(assigned_customers)]
		if cutoff_date is not None:
			filters["transaction_date"] = [">=", str(cutoff_date)]
	elif seller_scope == "orden_here":
		# Do not restrict SQL to assigned customers — also need order_owner / seller_ref rows.
		if cutoff_date is not None:
			filters["transaction_date"] = [">=", str(cutoff_date)]

	# Explicit customer filter (CRM party Pedidos tab / click-from-order).
	if customer_filter:
		if seller_scope == "assigned" and assigned_customers and customer_filter not in assigned_customers:
			return {"preorders": [], "total_count": 0, "window_days": window_days or 30}
		filters["customer"] = customer_filter

	if status:
		st = cstr(status).strip()
		st_l = st.lower()
		if st_l == "draft":
			filters["docstatus"] = 0
		elif st_l in ("submitted", "confirmed"):
			filters["docstatus"] = 1
		elif st_l in ("archived", "cancelled", "archivado"):
			filters["docstatus"] = 2
			include_archived = 1
		elif st in ("Delivery", "En Delivery") or st.startswith("Delivery"):
			# Facets are display-only; ERP status is Delivery (legacy En Delivery).
			filters["status"] = ["in", ["Delivery", "En Delivery"]]
		elif st.startswith("Completado") or st_l == "completed":
			filters["status"] = "Completed"
		else:
			filters["status"] = status
	elif not include_archived:
		# Active Pedidos table: drafts + submitted only (not Archivado).
		filters["docstatus"] = ["<", 2]

	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=[
			"name",
			"owner",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"grand_total",
			"advance_paid",
			"currency",
			"docstatus",
			"status",
			"amended_from",
			tag_fn,
		],
		start=0 if seller_scope else start,
		limit_page_length=(
			max(page_length + 200, 400)
			if seller_scope
			else page_length + (200 if scope else 50)
		),  # fetch extra to account for filtering
		order_by="transaction_date desc, creation desc",
		ignore_permissions=True,
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

	# Filter out superseded cancelled orders and orders outside this user's Pedidos scope.
	# When include_archived is off, also drop any remaining docstatus=2 rows (belt & suspenders).
	filtered = []
	for o in orders:
		if o.get("docstatus") == 2 and o["name"] in superseded:
			continue
		if not include_archived and cint(o.get("docstatus")) == 2:
			continue
		# Seller-scope lists still require the guest_preorder marker.
		tag_raw_check = str(o.get(tag_fn) or "")
		if GUEST_PREORDER_REMARKS_TAG not in tag_raw_check:
			continue
		if not guest_preorder_matches_scope(
			o.get("owner"),
			o.get(tag_fn),
			scope,
			customer=o.get("customer"),
			assigned_customers=assigned_customers or None,
		):
			continue
		if not _guest_preorder_matches_seller_scope(
			o,
			seller_scope,
			acting_uid,
			assigned_customers=assigned_customers,
			cutoff_date=cutoff_date,
		):
			continue
		tag_raw = str(o.get(tag_fn) or "")
		order_tag = ""
		for part in tag_raw.split("|"):
			part = part.strip()
			if part.startswith("order_tag:"):
				order_tag = part.split(":", 1)[1].strip()
				break
		o["order_tag"] = order_tag or None
		# Keep raw tags under a stable key so seller_ref / review_reason survive pop.
		o["_tag_raw"] = tag_raw
		o.pop(tag_fn, None)
		filtered.append(o)
	if seller_scope and start:
		filtered = filtered[start:]
	total_count = len(filtered)
	filtered = filtered[:page_length]

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
		o["cashier_user"] = _cashier_from_guest_preorder(o)
		o["seller_ref_user"] = _seller_ref_from_guest_preorder(o)
		o["display_status"] = _display_status_from_row(
			o.get("docstatus", 0),
			o.get("status", ""),
			o.get("grand_total", 0),
			o.get("advance_paid", 0),
		)
		o["delivery_date_forced"] = _delivery_date_forced_from_tags(o)

	ext_map = _external_status_change_map([o["name"] for o in filtered])
	for o in filtered:
		o["external_status_change"] = ext_map.get(o["name"]) or None

	geo = _customer_address_zone_map([o.get("customer") for o in filtered])
	stage_map = _customer_preventa_stage_map([o.get("customer") for o in filtered])
	for o in filtered:
		info = geo.get(o.get("customer")) or {}
		o["address"] = info.get("address")
		o["territory"] = info.get("territory")
		o["zone"] = info.get("zone")
		st = stage_map.get(o.get("customer")) or {}
		o["customer_stage"] = st.get("stage")
		o["customer_stage_label"] = st.get("stage_label")
		# Fallback: guest_address tag when customer has no street yet.
		if not o.get("address"):
			tags = _parse_remarks_tags(_guest_preorder_tag_text(o))
			o["address"] = tags.get("guest_address") or None

	# TMS logistics: warehouse / MAT / car / PoD (+ Delivery facets).
	logistics = _tms_logistics_map_for_orders([o["name"] for o in filtered])
	for o in filtered:
		lg = logistics.get(o["name"]) or {}
		tag_wh = _warehouse_from_guest_tags(o)
		o["warehouse"] = lg.get("warehouse") or tag_wh
		o["warehouse_defaulted"] = lg.get("warehouse_defaulted") if lg.get("warehouse") else 1
		o["delivery_note"] = lg.get("delivery_note") or o.get("delivery_note")
		o["trip_name"] = lg.get("trip_name")
		o["trip_status"] = lg.get("trip_status")
		o["stop_idx"] = lg.get("stop_idx")
		o["vehicle"] = lg.get("vehicle")
		o["vehicle_plate"] = lg.get("vehicle_plate")
		o["driver"] = lg.get("driver")
		o["driver_name"] = lg.get("driver_name")
		o["delivery_at"] = lg.get("delivery_at")
		o["pod"] = lg.get("pod")
		_apply_tms_display_status(o)

	_attach_factura_a_fields(filtered)
	# Ecommerce system tags (PRINTED, L_*, SRV-*) for pipeline icons.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tags_api import tags_map_for_docs

		tag_map = tags_map_for_docs("Sales Order", [o["name"] for o in filtered])
		for o in filtered:
			o["tags"] = tag_map.get(o["name"]) or []
	except Exception:
		for o in filtered:
			o.setdefault("tags", [])

	# Creation-review (Revisar) status for seller-amend badges.
	_attach_creation_review_fields(filtered)
	# Pending seller suggestions → Pedidos yellow row highlight.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import (
			SUGGESTION_KV_SCOPE,
		)
		from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get_many

		names = [cstr(o.get("name") or "").strip() for o in filtered if o.get("name")]
		sug_map = kv_get_many(SUGGESTION_KV_SCOPE, names) if names else {}
		for o in filtered:
			data = sug_map.get(o["name"]) or {}
			change = data.get("change") if isinstance(data, dict) else None
			status = (
				cstr(data.get("status") or "pending").lower() if isinstance(data, dict) else ""
			)
			active = bool(isinstance(change, dict) and change and status != "rejected")
			o["has_suggestion"] = active
			sug_actor = (data.get("actor") if active and isinstance(data, dict) else None) or None
			o["suggestion_actor"] = sug_actor
			# Prefer suggestion actor when the park was a seller suggestion.
			if not o.get("review_actor") and sug_actor:
				o["review_actor"] = sug_actor
	except Exception:
		frappe.log_error(frappe.get_traceback(), "list_guest_preorders suggestion attach")
		for o in filtered:
			o.setdefault("has_suggestion", False)
			o.setdefault("suggestion_actor", None)
	for o in filtered:
		o.pop("_tag_raw", None)
	out = {"preorders": filtered, "total_count": total_count}
	if window_days is not None:
		out["window_days"] = window_days
	return out


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
	_require_guest_preorder_visible(so)
	_repair_guest_preorder_weight_totals(so)

	tag_raw = getattr(so, "remarks", None) or getattr(so, "terms", None) or ""
	tags = {}
	for part in str(tag_raw).split("|"):
		part = part.strip()
		if ":" in part:
			key, val = part.split(":", 1)
			tags[key.strip()] = val.strip()

	geo = _customer_address_zone_map([so.customer]).get(so.customer) or {}
	client_state = (_customer_preventa_stage_map([so.customer]).get(so.customer) or {})

	# Soft list rates for lines with rate 0 (e.g. older consultas before $/Kg was kept).
	selling_pl = getattr(so, "selling_price_list", None) or "Standard Selling"
	zero_rate_codes = [d.item_code for d in (so.items or []) if flt(d.rate) <= 0 and d.item_code]
	list_rate_by_code = {}
	if zero_rate_codes:
		try:
			bulk = get_item_prices_bulk(zero_rate_codes, price_list=selling_pl) or {}
			for code in zero_rate_codes:
				list_rate_by_code[code] = flt(bulk.get(code) or 0)
		except Exception:
			for code in zero_rate_codes:
				list_rate_by_code[code] = flt(get_item_price(code, selling_pl) or 0)

	payload = {
		"name": so.name,
		"order_type": so.order_type,
		"customer": so.customer,
		"customer_name": frappe.db.get_value("Customer", so.customer, "customer_name") or so.customer,
		"customer_stage": client_state.get("stage"),
		"customer_stage_label": client_state.get("stage_label"),
		"creation_review": (
			frappe.db.get_value("Sales Order", so.name, "custom_creation_review")
			if frappe.db.has_column("Sales Order", "custom_creation_review")
			else None
		),
		"review_reason": tags.get("review_reason") or None,
		"review_actor": (
			tags.get("review_actor")
			or tags.get("order_owner")
			or tags.get("seller_ref")
			or None
		),
		"pending_suggestion": None,
		"guest_name": tags.get("guest_name") or None,
		"guest_phone": tags.get("guest_phone") or None,
		"guest_email": tags.get("guest_email") or None,
		"guest_address": tags.get("guest_address") or None,
		"guest_notes": tags.get("guest_notes") or None,
		"guest_cuil": tags.get("guest_cuil") or None,
		"guest_preferred_hours": tags.get("guest_preferred_hours") or None,
		"guest_observation": tags.get("guest_observation") or None,
		"is_delivery": tags.get("delivery") == "1",
		"transaction_date": so.transaction_date,
		"delivery_date": so.delivery_date,
		"delivery_date_forced": _delivery_date_forced_from_tags(so),
		"address": geo.get("address") or tags.get("guest_address") or None,
		"territory": geo.get("territory"),
		"zone": geo.get("zone"),
		"docstatus": so.docstatus,
		"status": so.status,
		"display_status": _display_status(so),
		"cashier_user": _cashier_from_guest_preorder(so),
		"seller_ref_user": _seller_ref_from_guest_preorder(so),
		"estimated_total": _guest_preorder_estimated_total(so),
		"currency": so.currency,
		"remarks": getattr(so, "remarks", None),
		"terms": getattr(so, "terms", None),
		"additional_discount_amount": flt(getattr(so, "additional_discount_amount", 0)),
		"advance_paid": flt(getattr(so, "advance_paid", 0)),
		"amended_from": so.amended_from or None,
		"delivery_note": _delivery_note_for_sales_order(preorder_name),
		"payments": _payments_for_sales_order(preorder_name, _guest_preorder_estimated_total(so)),
		"external_status_change": _external_status_change_for(preorder_name),
		"items": [
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": flt(d.qty),
				"rate": flt(d.rate),
				"amount": _guest_preorder_line_amount(d),
				"price_list_rate": (
					list_rate_by_code.get(d.item_code)
					if flt(d.rate) <= 0
					else None
				),
				"discount_percentage": flt(getattr(d, "discount_percentage", 0)),
				**_item_line_weight_fields(
					d.item_code,
					line_uom=getattr(d, "uom", None),
					line_weight_per_unit=getattr(d, "weight_per_unit", None),
					line_total_weight=getattr(d, "total_weight", None),
					line_qty=flt(d.qty),
				),
			}
			for d in (so.items or [])
		],
	}
	lg = (_tms_logistics_map_for_orders([preorder_name]).get(preorder_name) or {})
	tag_wh = tags.get("warehouse")
	payload["warehouse"] = lg.get("warehouse") or tag_wh
	payload["warehouse_defaulted"] = lg.get("warehouse_defaulted") if lg.get("warehouse") else (0 if tag_wh else 1)
	payload["trip_name"] = lg.get("trip_name")
	payload["trip_status"] = lg.get("trip_status")
	payload["stop_idx"] = lg.get("stop_idx")
	payload["vehicle"] = lg.get("vehicle")
	payload["vehicle_plate"] = lg.get("vehicle_plate")
	payload["driver"] = lg.get("driver")
	payload["driver_name"] = lg.get("driver_name")
	payload["delivery_at"] = lg.get("delivery_at")
	payload["departure_time"] = lg.get("departure_time")
	payload["pod"] = lg.get("pod")
	if lg.get("delivery_note"):
		payload["delivery_note"] = lg.get("delivery_note")
	_apply_tms_display_status(payload)
	_attach_factura_a_fields([payload])
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tags_api import tags_map_for_docs

		payload["tags"] = tags_map_for_docs("Sales Order", [preorder_name]).get(preorder_name) or []
	except Exception:
		payload["tags"] = []
	try:
		from erpnext.erpnext_integrations.ecommerce_api.creation_review_api import (
			get_pending_suggestion,
		)

		payload["pending_suggestion"] = get_pending_suggestion(preorder_name)
	except Exception:
		payload["pending_suggestion"] = None
	# Prefer suggestion actor when the park was a seller suggestion.
	sug = payload.get("pending_suggestion")
	if (
		not payload.get("review_actor")
		and isinstance(sug, dict)
		and sug.get("actor")
		and cstr(sug.get("status") or "pending").lower() != "rejected"
	):
		payload["review_actor"] = sug.get("actor")
	return payload


@frappe.whitelist()
def get_guest_preorder_history(preorder_name):
	"""
	Return the full version chain for a guest preorder.

	Walks the amended_from chain backward to find the original,
	then walks forward to collect all versions in chronological order.
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	frappe.flags.ignore_permissions = True
	_require_guest_preorder_visible(frappe.get_doc("Sales Order", preorder_name))

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
# Display statuses: Consulta → Orden → Preparado → Delivery → Completado
# Mapping to SilkOS:
#   Consulta   = docstatus 0 (Draft)
#   Orden      = docstatus 1, status "To Deliver and Bill"
#   Preparado  = docstatus 1, status "Preparado"   (custom via db_set)
#   Delivery   = docstatus 1, status "Delivery"    (custom via db_set; legacy "En Delivery")
#   Completado = docstatus 1, status "Completed"
#   Archivado  = docstatus 2 (Cancelled)
#
# Delivery / Completado expose payment & logistics *facets* in display_status only:
#   Delivery (plan|on|partial|done), Completado (pago|no pagado).

WORKFLOW_STATUSES = ["Consulta", "Orden", "Preparado", "Delivery", "Completado"]
COMPLETED_PAID_LABEL = "Completado (pago)"
COMPLETED_UNPAID_LABEL = "Completado (no pagado)"
DELIVERY_PEND_LABEL = "Delivery (plan)"
DELIVERY_ON_LABEL = "Delivery (on)"
DELIVERY_PARTIAL_LABEL = "Delivery (partial)"
DELIVERY_DONE_LABEL = "Delivery (done)"
# Written via db_set — not in stock Sales Order.status Select options.
GUEST_PREORDER_CUSTOM_STATUSES = frozenset({"Consulta", "Preparado", "Delivery", "En Delivery"})
_SO_STATUS_SELECT_BASE = (
	"\nDraft\nOn Hold\nTo Deliver and Bill\nTo Bill\nTo Deliver\nCompleted\nCancelled\nClosed"
)
# Status transitions done outside the Órdenes table UI (armado / remito / TMS).
EXTERNAL_PIPELINE_SOURCES = frozenset({"armado", "remito", "tms_claim", "tms_pod"})
EXTERNAL_STATUS_KV_SCOPE = "pedidos.external_status"


def ensure_guest_preorder_status_options():
	"""Idempotent: extend Sales Order.status Select so Pedidos markers survive Document.save()."""
	extras = ["Consulta", "Preparado", "Delivery", "En Delivery"]
	meta = frappe.get_meta("Sales Order")
	df = meta.get_field("status")
	current = (df.options if df else "") or _SO_STATUS_SELECT_BASE
	lines = current.split("\n")
	changed = False
	for extra in extras:
		if extra not in lines:
			lines.append(extra)
			changed = True
	if not changed:
		return
	options = "\n".join(lines)
	existing = frappe.db.exists(
		"Property Setter",
		{"doc_type": "Sales Order", "field_name": "status", "property": "options"},
	)
	if existing:
		frappe.db.set_value("Property Setter", existing, "value", options, update_modified=False)
	else:
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doctype_or_field": "DocField",
				"doc_type": "Sales Order",
				"field_name": "status",
				"property": "options",
				"property_type": "Text",
				"value": options,
			}
		).insert(ignore_permissions=True)
	frappe.clear_cache(doctype="Sales Order")


def _save_guest_preorder_so(so):
	"""
	Save a guest-preorder Sales Order even when status is a custom pipeline marker.

	Frappe Select validation rejects Consulta / Preparado / Delivery unless the
	Property Setter has run; stash a valid ERP status for save, then restore.
	WEIGHT lines are rebilled as $/kg × kg inside the save validate path.
	"""
	ensure_guest_preorder_status_options()
	custom = cstr(getattr(so, "status", None) or "").strip()
	restore = custom if custom in GUEST_PREORDER_CUSTOM_STATUSES else None
	if restore:
		so.status = "To Deliver and Bill" if cint(so.docstatus) == 1 else "Draft"
	if cint(so.docstatus) == 1:
		so.flags.ignore_validate_update_after_submit = True
	with _allow_weight_fractional_stock_qty(so):
		so.save(ignore_permissions=True)
	if restore:
		so.db_set("status", restore, update_modified=False)


def _normalize_pipeline_source(source):
	src = cstr(source or "").strip().lower()
	if src in ("null", "undefined", "none"):
		return None
	return src or None


def _record_guest_preorder_status_audit(so_name, old_display, new_display, source=None):
	"""Version Historial row + optional external-change KV for Órdenes highlight/banner."""
	old_s = cstr(old_display or "").strip()
	new_s = cstr(new_display or "").strip()
	if not so_name or old_s == new_s:
		return
	try:
		from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

		changes = [("status", old_s, new_s)]
		src = _normalize_pipeline_source(source)
		if src in EXTERNAL_PIPELINE_SOURCES:
			changes.append(("status_via", "", src))
		log_field_changes("Sales Order", so_name, changes)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "guest preorder status audit")

	src = _normalize_pipeline_source(source)
	if src not in EXTERNAL_PIPELINE_SOURCES:
		return
	try:
		from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_set

		at = str(now_datetime())
		kv_set(
			EXTERNAL_STATUS_KV_SCOPE,
			so_name,
			{
				"status": new_s,
				"previous": old_s,
				"source": src,
				"at": at,
				"by": frappe.session.user,
				"fingerprint": f"{src}:{new_s}:{at}",
			},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "guest preorder external status kv")


def _external_status_change_for(so_name):
	so_name = cstr(so_name or "").strip()
	if not so_name:
		return None
	try:
		from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get

		_name, data = kv_get(EXTERNAL_STATUS_KV_SCOPE, so_name)
		return data or None
	except Exception:
		return None


def _external_status_change_map(so_names):
	names = [cstr(n).strip() for n in (so_names or []) if cstr(n).strip()]
	if not names:
		return {}
	try:
		from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get_many

		return kv_get_many(EXTERNAL_STATUS_KV_SCOPE, names)
	except Exception:
		return {}



def _so_is_fully_paid(so) -> bool:
	total = (
		_guest_preorder_estimated_total(so)
		if _is_guest_preorder_sales_order(so)
		else flt(getattr(so, "grand_total", 0) or 0)
	)
	paid = flt(getattr(so, "advance_paid", 0) or 0)
	return total > 0 and paid + 0.005 >= total


def _payments_for_sales_order(so_name: str, grand_total=None) -> list:
	"""Payment Entry rows allocated to this Sales Order (oldest first) with running balance."""
	if not so_name:
		return []
	rows = frappe.db.sql(
		"""
		SELECT
			pe.name AS name,
			pe.posting_date AS posting_date,
			pe.mode_of_payment AS mode_of_payment,
			pe.paid_amount AS paid_amount,
			pe.received_amount AS received_amount,
			pe.docstatus AS docstatus,
			pe.creation AS creation,
			per.allocated_amount AS allocated_amount
		FROM `tabPayment Entry Reference` per
		INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		WHERE per.reference_doctype = 'Sales Order'
			AND per.reference_name = %s
			AND pe.docstatus < 2
		ORDER BY pe.posting_date ASC, pe.creation ASC
		""",
		(so_name,),
		as_dict=True,
	)
	total = flt(grand_total)
	if total <= 0 and so_name and frappe.db.exists("Sales Order", so_name):
		total = flt(frappe.db.get_value("Sales Order", so_name, "grand_total") or 0)

	out = []
	seen = set()
	running = 0.0
	for r in rows or []:
		name = r.get("name")
		if not name or name in seen:
			continue
		seen.add(name)
		amount = flt(r.get("allocated_amount"))
		if amount <= 0:
			amount = flt(r.get("received_amount") or r.get("paid_amount"))
		running += amount
		out.append(
			{
				"name": name,
				"posting_date": str(r.posting_date) if r.get("posting_date") else None,
				"mode_of_payment": r.get("mode_of_payment") or None,
				"amount": amount,
				"docstatus": cint(r.get("docstatus")),
				"outstanding_after": max(0.0, total - running),
			}
		)
	return out


def _pipeline_base_status(status_or_display) -> str:
	"""Collapse facets / legacy aliases to the canonical WORKFLOW_STATUSES id."""
	s = cstr(status_or_display or "").strip()
	if not s:
		return s
	if s.startswith("Completado"):
		return "Completado"
	if s.startswith("Delivery") or s == "En Delivery":
		return "Delivery"
	return s


def _normalize_target_pipeline_status(target_status) -> str:
	"""Accept legacy En Delivery and Completado facets as set_guest_preorder_status targets."""
	raw = cstr(target_status or "").strip()
	base = _pipeline_base_status(raw)
	if base in WORKFLOW_STATUSES:
		return base
	return raw


def _completado_facet(fully_paid: bool) -> str:
	return COMPLETED_PAID_LABEL if fully_paid else COMPLETED_UNPAID_LABEL


def _delivery_facet(trip_status=None, pod_outcome=None) -> str:
	"""Logistics facet for the Delivery pipeline step."""
	outcome = cstr(pod_outcome or "").strip()
	if outcome == "Delivered":
		return DELIVERY_DONE_LABEL
	if outcome == "Partial":
		return DELIVERY_PARTIAL_LABEL
	trip_st = cstr(trip_status or "").strip()
	if trip_st == "In Transit":
		return DELIVERY_ON_LABEL
	return DELIVERY_PEND_LABEL


def _orders_auto_complete_on_full_payment() -> bool:
	"""Shop UI setting — default False (PoD stays on Delivery until ops closes)."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import get_shop_ui_settings

		res = get_shop_ui_settings() or {}
		cat = (res.get("settings") or {}).get("catalogDisplay") or {}
		return bool(cat.get("autoCompleteOnFullPayment"))
	except Exception:
		return False


def _so_has_mat_trip(so_name: str) -> bool:
	so_name = cstr(so_name or "").strip()
	if not so_name:
		return False
	lg = (_tms_logistics_map_for_orders([so_name]) or {}).get(so_name) or {}
	return bool(cstr(lg.get("trip_name") or "").strip())


def _display_status(so):
	"""Return the user-facing workflow status for a Sales Order."""
	if so.docstatus == 0:
		return "Consulta"
	if so.docstatus == 2:
		return "Archivado"
	# docstatus == 1 — soft Consulta (no cancel/amend) uses status marker
	s = so.status
	if s == "Consulta":
		return "Consulta"
	if s == "Preparado":
		return "Preparado"
	if s in ("Delivery", "En Delivery"):
		# Facet filled later by _apply_tms_display_status when logistics are attached.
		return "Delivery"
	if s == "Completed":
		return _completado_facet(_so_is_fully_paid(so))
	# "To Deliver and Bill", "To Deliver", "To Bill", etc.
	return "Orden"


def _display_status_from_row(docstatus, status, grand_total=0, advance_paid=0):
	"""Same as _display_status but from list-row fields."""
	if docstatus == 0:
		return "Consulta"
	if docstatus == 2:
		return "Archivado"
	if status == "Consulta":
		return "Consulta"
	if status == "Preparado":
		return "Preparado"
	if status in ("Delivery", "En Delivery"):
		return "Delivery"
	if status == "Completed":
		total = flt(grand_total or 0)
		paid = flt(advance_paid or 0)
		fully = total > 0 and paid + 0.005 >= total
		return _completado_facet(fully)
	return "Orden"


def _allocate_amend_name(doctype, amended_from):
	"""Next free ``{prefix}-{n}`` amend name (Frappe's stock helper does not skip collisions)."""
	amended_from = cstr(amended_from or "").strip()
	if not amended_from:
		frappe.throw(_("amended_from is required"))
	am_id = 1
	am_prefix = amended_from
	if frappe.db.get_value(doctype, amended_from, "amended_from"):
		tail = amended_from.rsplit("-", 1)[-1]
		if str(tail).isdigit():
			am_id = cint(tail) + 1
			am_prefix = amended_from.rsplit("-", 1)[0]
	while frappe.db.exists(doctype, f"{am_prefix}-{am_id}"):
		am_id += 1
	return f"{am_prefix}-{am_id}"


def _latest_amend_successor(order_name):
	"""Most recent Sales Order that amends ``order_name``, if any."""
	rows = frappe.get_all(
		"Sales Order",
		filters={"amended_from": order_name},
		fields=["name", "docstatus", "creation"],
		order_by="creation desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


def _erp_status_for_display(display_status):
	"""Map display status → SilkOS status string."""
	base = _normalize_target_pipeline_status(display_status)
	return {
		"Orden": "To Deliver and Bill",
		"Preparado": "Preparado",
		"Delivery": "Delivery",
		"Completado": "Completed",
	}.get(base)


@frappe.whitelist(allow_guest=True)
def _attach_factura_a_fields(rows):
	"""Attach Sales Order Factura A / delivery payment differential fields onto list/detail rows."""
	names = [cstr(r.get("name")).strip() for r in (rows or []) if r.get("name")]
	names = [n for n in names if n]
	if not names:
		return
	if not frappe.db.has_column("Sales Order", "custom_requires_factura_a"):
		for r in rows:
			r.setdefault("requires_factura_a", False)
			r.setdefault("factura_a_status", None)
			r.setdefault("delivery_surcharge_amount", None)
			r.setdefault("delivery_payment_summary", None)
		return
	fields = ["name", "custom_requires_factura_a", "custom_factura_a_status"]
	if frappe.db.has_column("Sales Order", "custom_delivery_surcharge_amount"):
		fields.append("custom_delivery_surcharge_amount")
	if frappe.db.has_column("Sales Order", "custom_delivery_payment_summary"):
		fields.append("custom_delivery_payment_summary")
	so_rows = frappe.get_all(
		"Sales Order",
		filters={"name": ["in", names]},
		fields=fields,
		ignore_permissions=True,
	)
	by_name = {r.name: r for r in so_rows}
	for r in rows:
		so = by_name.get(r.get("name")) or {}
		req = bool(cint(so.get("custom_requires_factura_a") or 0))
		# Prefer SO flag; fall back to PoD stop flag if SO not yet synced.
		pod = r.get("pod") or {}
		if not req and pod.get("requires_factura_a"):
			req = True
		status = cstr(so.get("custom_factura_a_status") or "").strip() or None
		if not status and pod.get("factura_a_status"):
			status = cstr(pod.get("factura_a_status")).strip() or None
		if req and not status:
			status = "pending"
		if not req:
			status = status if status in ("issued",) else ("na" if status == "na" else None)
		r["requires_factura_a"] = req
		r["factura_a_status"] = status
		r["delivery_surcharge_amount"] = (
			flt(so.get("custom_delivery_surcharge_amount"))
			if so.get("custom_delivery_surcharge_amount") is not None
			else (flt(pod.get("surcharge_amount")) if pod.get("surcharge_amount") is not None else None)
		)
		r["delivery_payment_summary"] = (
			cstr(so.get("custom_delivery_payment_summary") or "").strip()
			or cstr(pod.get("payment_summary") or pod.get("payment_method") or "").strip()
			or None
		)


@frappe.whitelist(allow_guest=True)
def set_guest_preorder_factura_a(preorder_name=None, requires_factura_a=None, factura_a_status=None):
	"""Mark/unmark Factura A on a guest Pedido, or set checklist status (pending|issued|na)."""
	name = cstr(preorder_name or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Sales Order is required"))
	if not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name))

	from erpnext.erpnext_integrations.ecommerce_api.tms_api import _ensure_delivery_payment_split_fields

	_ensure_delivery_payment_split_fields()

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	updates = {}
	if requires_factura_a is not None:
		req = 1 if str(requires_factura_a).strip().lower() in ("1", "true", "yes", "y", "on") else (
			0 if str(requires_factura_a).strip().lower() in ("0", "false", "no", "n", "off", "", "null", "none", "undefined")
			else (1 if requires_factura_a else 0)
		)
		updates["custom_requires_factura_a"] = req
		if req:
			cur = cstr(getattr(so, "custom_factura_a_status", None) or "").strip()
			if cur != "issued":
				updates["custom_factura_a_status"] = "pending"
		else:
			updates["custom_factura_a_status"] = "na"
	if factura_a_status is not None:
		st = cstr(factura_a_status).strip().lower()
		if st in ("", "null", "undefined", "none"):
			st = "na"
		if st not in ("pending", "issued", "na"):
			frappe.throw(_("Invalid Factura A status: {0}").format(factura_a_status))
		updates["custom_factura_a_status"] = st
		if st == "pending":
			updates["custom_requires_factura_a"] = 1
		elif st == "na":
			updates.setdefault("custom_requires_factura_a", 0)
		elif st == "issued":
			updates["custom_requires_factura_a"] = 1

	if updates:
		frappe.db.set_value("Sales Order", name, updates, update_modified=True)
		frappe.db.commit()

	return get_guest_preorder(name)


@frappe.whitelist(allow_guest=True)
def set_guest_preorder_status(
	preorder_name, target_status, source=None, ensure_planner_remito=0
):
	"""
	Unified status transition for the custom workflow.

	Accepts target_status as one of: Consulta, Orden, Preparado, Delivery, Completado
	(legacy ``En Delivery`` is normalized to Delivery).
	Consulta on a submitted order is a soft marker (db_set status) — it does **not**
	cancel/archive. Explicit archive uses ``cancel_guest_preorder``.
	 Backward moves among submitted custom statuses use db_set so SilkOS
	update_status does not block with an opaque error.

	``source`` (optional): when one of armado / remito / tms_claim / tms_pod, the
	transition is treated as external to the Órdenes table (highlight + banner).

	``ensure_planner_remito``: default 0 — Rutas planning lists Orden/Preparado by
	Sales Order without requiring a remito. Pass 1 only when a caller explicitly
	wants an early remito (legacy / stock paths). Remito is created on MAT claim.
	"""
	target_status = _normalize_target_pipeline_status(target_status)
	if target_status not in WORKFLOW_STATUSES:
		frappe.throw(_("Invalid target status: {0}").format(target_status))

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	pipeline_source = _normalize_pipeline_source(source)
	_require_guest_preorder_live_mutate(pipeline_source=pipeline_source)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	# Kiosk / TMS transitions are PIN-gated upstream — do not apply Pedidos scope.
	if not pipeline_source:
		_require_guest_preorder_visible(so)

	if so.docstatus == 2:
		# Archivado → amend into a new Consulta draft, then optionally advance.
		new_detail = unarchive_guest_preorder(preorder_name)
		if target_status == "Consulta":
			return new_detail
		return set_guest_preorder_status(
			new_detail["name"],
			target_status,
			source=pipeline_source,
			ensure_planner_remito=ensure_planner_remito,
		)

	current = _display_status(so)
	current_base = _pipeline_base_status(current)

	# Soft Consulta — never cancel/archive just to go back.
	if target_status == "Consulta":
		if so.docstatus == 1 and cstr(so.status) != "Consulta":
			so.db_set("status", "Consulta", update_modified=True)
			so.reload()
			_record_guest_preorder_status_audit(
				preorder_name, current_base, "Consulta", source=pipeline_source
			)
		return get_guest_preorder(preorder_name)

	# Submit draft if needed for forward transitions
	if so.docstatus == 0:
		try:
			# Safety: stale Consumidor Final contact must not block Consulta→Orden.
			if _resync_so_party_links(so):
				so.flags.ignore_permissions = True
				so.flags.ignore_mandatory = True
				_save_guest_preorder_so(so)
				so.reload()
			so.flags.ignore_permissions = True
			so.submit()
			so.reload()
		except Exception as e:
			frappe.throw(
				_("Cannot move from Consulta to {0}: submit failed — {1}").format(
					target_status, frappe.utils.cstr(e)
				)
			)

	# Delivery requires a linked MAT (same pattern as Consulta → real client).
	# tms_claim / tms_pod already sit on a trip stop.
	if target_status == "Delivery" and pipeline_source not in ("tms_claim", "tms_pod"):
		if not _so_has_mat_trip(preorder_name):
			frappe.throw(
				_("Assign a MAT before moving to Delivery."),
				title=_("MAT required"),
			)

	erp_status = _erp_status_for_display(target_status)
	if not erp_status:
		frappe.throw(_("Invalid target status"))

	# Completado / Preparado / Delivery: always db_set (custom workflow markers).
	# Orden ("To Deliver and Bill"): try update_status first; on failure fall back to
	# db_set so going back from Preparado/Delivery/Completado works.
	try:
		if erp_status == "To Deliver and Bill":
			try:
				so.update_status(erp_status)
			except Exception:
				so.db_set("status", erp_status, update_modified=True)
		else:
			so.db_set("status", erp_status, update_modified=True)
	except Exception as e:
		frappe.throw(
			_("Cannot change status from {0} to {1}: {2}").format(
				current_base, target_status, frappe.utils.cstr(e)
			)
		)

	so.reload()
	new_display = _display_status(so)
	new_base = _pipeline_base_status(new_display)
	_record_guest_preorder_status_audit(
		preorder_name, current_base, new_base, source=pipeline_source
	)
	# Pipeline moves clear print-output tags (PRINTED).
	try:
		from erpnext.erpnext_integrations.ecommerce_api.tags_api import clear_sales_order_print_tags

		clear_sales_order_print_tags(preorder_name, commit=False)
	except Exception:
		pass
	# Orden → blue Entrega = tomorrow / next working day (not +7 / next zone week).
	if target_status == "Orden":
		_apply_orden_default_delivery_date(so)
		so.reload()
	detail = get_guest_preorder(preorder_name)
	# Optional early remito (default off — planner keys off SO.delivery_date).
	if target_status in ("Orden", "Preparado") and cint(ensure_planner_remito):
		so.reload()
		gate = _ensure_planner_delivery_note(so)
		_attach_planner_gate_fields(detail, gate)
	elif target_status in ("Orden", "Preparado"):
		# Planner-ready without remito when due is set (address optional for listing).
		detail["planner_ready"] = bool(detail.get("delivery_date"))
	return detail


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
	_require_guest_preorder_visible(so)

	if so.docstatus == 0:
		so.submit()

	so.update_status("To Deliver and Bill")
	so.reload()
	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook(
			"order_confirmed",
			{"preorder_name": so.name, "customer": so.customer, "status": so.status},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook order_confirmed")
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def mark_prepared_guest_preorder(preorder_name):
	"""Mark a guest preorder as Preparado (custom workflow step)."""
	result = set_guest_preorder_status(preorder_name, "Preparado")
	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook("order_prepared", {"preorder_name": preorder_name})
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook order_prepared")
	return result


@frappe.whitelist(allow_guest=True)
def unarchive_guest_preorder(preorder_name=None):
	"""Restore an Archivado (cancelled) guest preorder as a new Consulta draft.

	ERPNext cannot reopen a cancelled Sales Order in place — we amend it:
	copy the cancelled doc into a new draft linked via ``amended_from``.

	If an amendment already exists, reuse it (or walk forward when that
	successor is also cancelled). Amend names skip collisions (Frappe's
	default ``prefix-n`` naming does not).
	"""
	_require_guest_preorder_live_mutate()
	name = cstr(preorder_name or "").strip()
	if not name:
		frappe.throw(_("Sales Order name is required"))
	if not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	if so.docstatus != 2:
		frappe.throw(_("Order is not archived"))

	# Idempotent: existing amendment of this cancelled SO.
	succ = _latest_amend_successor(name)
	if succ:
		if cint(succ.docstatus) == 2:
			return unarchive_guest_preorder(succ.name)
		return get_guest_preorder(succ.name)

	new_so = frappe.copy_doc(so)
	new_so.amended_from = so.name
	new_so.docstatus = 0
	# Clear cancel markers so the draft looks like a fresh Consulta.
	if hasattr(new_so, "status"):
		new_so.status = "Draft"
	desired = _allocate_amend_name("Sales Order", so.name)
	new_so.insert(ignore_permissions=True, set_name=desired)
	_requeue_seller_amend_review(new_so.name, reason="seller_unarchive_amend")
	frappe.db.commit()
	return get_guest_preorder(new_so.name)


@frappe.whitelist()
def unmark_prepared_guest_preorder(preorder_name):
	"""Move Preparado / later custom steps back to Orden."""
	return set_guest_preorder_status(preorder_name, "Orden")


@frappe.whitelist()
def cancel_guest_preorder(preorder_name=None):
	"""Cancel (archive) a guest preorder. Works on both draft and submitted orders."""
	_require_guest_preorder_live_mutate()
	name = cstr(preorder_name or "").strip()
	if not name or name.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Sales Order name is required"))
	if not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name))

	so = frappe.get_doc("Sales Order", name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	if so.docstatus == 2:
		frappe.throw(_("Order is already cancelled"))

	old_display = _display_status(so)
	so.flags.ignore_permissions = True
	if so.docstatus == 1:
		# Submitted → proper cancel (stock/GL hooks).
		with _allow_weight_fractional_stock_qty(so):
			so.cancel()
	else:
		# Draft: Frappe forbids Document.save() transition 0 → 2
		# (DocstatusTransitionError → HTTP 417). Archive via db.
		frappe.db.set_value(
			"Sales Order",
			name,
			{"docstatus": 2, "status": "Cancelled"},
			update_modified=True,
		)
		frappe.db.sql(
			"""
			UPDATE `tabSales Order Item`
			SET docstatus=2
			WHERE parent=%s AND parenttype='Sales Order'
			""",
			(name,),
		)
	so = frappe.get_doc("Sales Order", name)
	try:
		_record_guest_preorder_status_audit(name, old_display, "Archivado", source=None)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "guest preorder archive audit")
	frappe.db.commit()
	return {"ok": True, "name": name, "status": "Cancelled", "display_status": "Archivado"}


@frappe.whitelist()
def update_guest_preorder_details(preorder_name, data=None):
	"""
	Update guest-preorder header fields (not line items).

	data: {
	  delivery_date?,
	  delivery_date_forced?,  # bool / 0 / 1 — black date when forced; blue when auto
	  address_line1? / address?,
	  territory? / zone?,     # CRM territory + Address.custom_zone; auto-recomputes delivery when not forced
	  customer?,          # Customer link (must exist)
	  customer_name?,     # Display name on Customer
	  paid_amount?,       # Absolute advance_paid target
	  new_name?,          # Rename Sales Order
	  cashier_user?,      # Assign/reassign POS cashier (tables.orders)
	  guest_name?, guest_phone?, guest_email?, guest_address?,
	  guest_notes?, guest_cuil?, guest_preferred_hours?, guest_observation?,
	}
	"""
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = frappe._dict(data or {})
	_require_guest_preorder_live_mutate()

	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)
	if so.docstatus == 2:
		frappe.throw(_("Cannot edit a cancelled order"))

	current_name = so.name
	geo_touched = False
	force_flag = None
	if "delivery_date_forced" in data:
		raw_f = data.get("delivery_date_forced")
		force_flag = str(raw_f).strip().lower() in ("1", "true", "yes") if raw_f is not None and raw_f != "" else False

	addr_val = data.get("address_line1") if "address_line1" in data else data.get("address")
	zone_val = data.get("zone") if "zone" in data else data.get("territory")
	if addr_val is not None or zone_val is not None or ("territory" in data):
		territory_val = data.get("territory") if "territory" in data else zone_val
		_set_customer_zone_and_address(
			so.customer,
			territory=territory_val if territory_val is not None else None,
			zone=zone_val if zone_val is not None else territory_val,
			address_line1=addr_val if addr_val is not None else None,
		)
		geo_touched = True
		if force_flag is not True:
			# Zone change → auto delivery date (unless explicitly forcing).
			geo = _customer_address_zone_map([so.customer]).get(so.customer) or {}
			auto_due = _auto_delivery_date_for_zone(geo.get("zone") or geo.get("territory"))
			if auto_due:
				so.delivery_date = getdate(auto_due)
				for row in so.items or []:
					row.delivery_date = so.delivery_date
			_update_guest_preorder_tag(so, "delivery_forced", None)
			force_flag = False

	if data.get("delivery_date") is not None and str(data.get("delivery_date") or "").strip() != "":
		so.delivery_date = getdate(data.get("delivery_date"))
		# ERPNext requires delivery ≥ order date; a stale date (e.g. replayed from
		# the offline outbox) is clamped instead of failing the save.
		if so.transaction_date and so.delivery_date < getdate(so.transaction_date):
			so.delivery_date = getdate(so.transaction_date)
		for row in so.items or []:
			row.delivery_date = so.delivery_date
		if force_flag is None:
			# Explicit date edit without flag → treat as forced.
			force_flag = True
		if force_flag:
			_update_guest_preorder_tag(so, "delivery_forced", "1")
			# Assign a delivery zone that visits that weekday.
			wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
			try:
				label = wd[getdate(so.delivery_date).weekday()]
			except Exception:
				label = None
			if label:
				geo = _customer_address_zone_map([so.customer]).get(so.customer) or {}
				pick = _zone_matching_weekday(label, prefer=geo.get("zone") or geo.get("territory"))
				if pick:
					_set_customer_zone_and_address(so.customer, zone=pick, territory=pick)
					geo_touched = True
		else:
			_update_guest_preorder_tag(so, "delivery_forced", None)
	elif force_flag is False:
		_update_guest_preorder_tag(so, "delivery_forced", None)
		geo = _customer_address_zone_map([so.customer]).get(so.customer) or {}
		auto_due = _auto_delivery_date_for_zone(geo.get("zone") or geo.get("territory"))
		if auto_due:
			so.delivery_date = getdate(auto_due)
			for row in so.items or []:
				row.delivery_date = so.delivery_date
	elif force_flag is True:
		_update_guest_preorder_tag(so, "delivery_forced", "1")

	if data.get("customer"):
		customer = str(data.get("customer")).strip()
		if not frappe.db.exists("Customer", customer):
			frappe.throw(_("Customer {0} not found").format(customer))
		so.customer = customer
		_update_guest_preorder_tag(so, "customer", customer)
		_resync_so_party_links(so)

	guest_tag_keys = (
		("guest_name", "guest_name"),
		("guest_phone", "guest_phone"),
		("guest_email", "guest_email"),
		("guest_address", "guest_address"),
		("guest_notes", "guest_notes"),
		("guest_cuil", "guest_cuil"),
		("guest_preferred_hours", "guest_preferred_hours"),
		("guest_observation", "guest_observation"),
	)
	guest_tags_touched = False
	for data_key, tag_key in guest_tag_keys:
		if data_key in data:
			_update_guest_preorder_tag(so, tag_key, str(data.get(data_key) or "").strip())
			guest_tags_touched = True

	# Auto-relate when still on a bucket customer and phone/local now match someone.
	if not data.get("customer"):
		cname = frappe.db.get_value("Customer", so.customer, "customer_name") or so.customer
		if _is_bucket_customer(so.customer, cname):
			phone = (
				str(data.get("guest_phone")).strip()
				if "guest_phone" in data
				else (_parse_remarks_tags(_guest_preorder_tag_text(so)).get("guest_phone") or "")
			)
			address = (
				str(data.get("guest_address")).strip()
				if "guest_address" in data
				else (_parse_remarks_tags(_guest_preorder_tag_text(so)).get("guest_address") or "")
			)
			matched = _resolve_consulta_customer(
				explicit=None, guest_phone=phone, guest_address=address
			)
			if matched and matched != so.customer and not _is_bucket_customer(
				matched, frappe.db.get_value("Customer", matched, "customer_name")
			):
				so.customer = matched
				_update_guest_preorder_tag(so, "customer", matched)
				_resync_so_party_links(so)

	if data.get("cashier_user") is not None:
		if not _can_view_all_guest_preorders():
			frappe.throw(
				_(
					"No permission to reassign cashier (tables.orders). "
					"Ask an admin for full Pedidos access, or copy the selection instead."
				)
			)
		_update_guest_preorder_tag(so, "cashier", str(data.get("cashier_user") or "").strip())

	if data.get("seller_ref_user") is not None:
		if not _can_view_all_guest_preorders():
			frappe.throw(
				_(
					"No permission to reassign seller (tables.orders). "
					"Ask an admin for full Pedidos access, or copy the selection instead."
				)
			)
		_update_guest_preorder_tag(so, "seller_ref", str(data.get("seller_ref_user") or "").strip())

	if so.docstatus == 0:
		# Consulta drafts may have no lines yet — Frappe "Data missing in table: Items"
		# must not block header / tag updates from the Pedidos panel.
		so.flags.ignore_mandatory = True
		_save_guest_preorder_so(so)
	else:
		# Submitted: persist allowed header fields without full amend
		updates = {}
		if data.get("delivery_date") is not None or geo_touched or force_flag is not None:
			if so.delivery_date:
				updates["delivery_date"] = so.delivery_date
		# Persist customer when explicitly set or auto-matched from phone/local.
		if data.get("customer") or so.customer:
			updates["customer"] = so.customer
		tag_fn = _guest_preorder_tag_fieldname()
		if tag_fn and (
			data.get("customer")
			or data.get("cashier_user") is not None
			or data.get("seller_ref_user") is not None
			or guest_tags_touched
			or so.customer
			or force_flag is not None
			or geo_touched
		):
			updates[tag_fn] = getattr(so, tag_fn, None)
		if updates:
			frappe.db.set_value("Sales Order", current_name, updates)
			if updates.get("delivery_date"):
				frappe.db.sql(
					"""
					UPDATE `tabSales Order Item`
					SET delivery_date=%s
					WHERE parent=%s
					""",
					(so.delivery_date, current_name),
				)

	if data.get("customer_name") is not None:
		cname = str(data.get("customer_name") or "").strip()
		cust = so.customer if not data.get("customer") else str(data.get("customer")).strip()
		if cname and cust and frappe.db.exists("Customer", cust):
			frappe.db.set_value("Customer", cust, "customer_name", cname[:140])

	if data.get("paid_amount") is not None and data.get("paid_amount") != "":
		target = flt(data.get("paid_amount"))
		if target < 0:
			frappe.throw(_("Paid amount cannot be negative"))
		current_paid = flt(frappe.db.get_value("Sales Order", current_name, "advance_paid") or 0)
		delta = target - current_paid
		if abs(delta) >= 0.005:
			if delta > 0 and frappe.db.get_value("Sales Order", current_name, "docstatus") == 1:
				# Proper payment entry for increase
				record_preorder_payment(current_name, delta)
			else:
				# Draft or reduction: set absolute advance (guest admin override)
				frappe.db.set_value("Sales Order", current_name, "advance_paid", target)

	new_name = (data.get("new_name") or "").strip()
	if new_name and new_name != current_name:
		if frappe.db.exists("Sales Order", new_name):
			frappe.throw(_("Sales Order {0} already exists").format(new_name))
		frappe.rename_doc("Sales Order", current_name, new_name, force=True, merge=False)
		current_name = new_name

	# Material header edits by non-admins re-enter Revisar (not soft pipeline moves).
	material_keys = (
		"customer",
		"customer_name",
		"paid_amount",
		"guest_name",
		"guest_phone",
		"guest_address",
		"guest_notes",
		"address_line1",
		"address",
		"zone",
		"territory",
		"delivery_date",
	)
	if any(k in data for k in material_keys):
		_requeue_seller_amend_review(current_name, reason="seller_amend_details")

	frappe.db.commit()
	return get_guest_preorder(current_name)


@frappe.whitelist()
def update_guest_preorder_items(preorder_name, items, additional_discount_amount=0):
	"""
	Full item replacement on a guest preorder.

	Draft (docstatus=0) and submitted (docstatus=1) orders both edit in place —
	same name, no cancel/amend. Archivado is only via explicit archive
	(``cancel_guest_preorder`` / CSV Eliminar), not line edits.

	Returns the updated preorder detail (same ``preorder_name``).
	"""
	import json as _json

	_require_guest_preorder_live_mutate()

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	if so.docstatus == 2:
		frappe.throw(_("Cannot edit a cancelled order"))

	if isinstance(items, str):
		items = _json.loads(items)

	if not items:
		frappe.throw(_("Items list cannot be empty"))

	# Draft + submitted: edit in place. Keep operator rate/qty/uom (WEIGHT) —
	# do not re-price from rules. Submitted needs update-after-submit bypass.
	_apply_item_changes(so, items, additional_discount_amount)
	so.flags.ignore_pricing_rule = True
	if so.docstatus == 1:
		so.flags.ignore_validate_update_after_submit = True
	with _allow_weight_fractional_stock_qty(so):
		_save_guest_preorder_so(so)
	so.reload()
	_requeue_seller_amend_review(so.name, reason="seller_amend_items")
	frappe.db.commit()
	return get_guest_preorder(preorder_name)


def _set_line_rate(row, rate, price_list_rate=None) -> None:
	"""Set a Sales Order line to exactly ``rate``.

	ERPNext stores a manual rate above the list price as margin_type=Amount; on
	the next save a stale margin is re-added (rate = price_list_rate + margin),
	so repricing 7651 → 5216.29 came out as 10085.71. Clear it — ERPNext
	recomputes the margin/discount from the new rate.
	"""
	row.rate = flt(rate)
	if price_list_rate is not None:
		row.price_list_rate = flt(price_list_rate)
	row.margin_type = None
	row.margin_rate_or_amount = 0
	row.rate_with_margin = 0
	row.base_rate_with_margin = 0
	row.discount_amount = 0


def _apply_item_changes(so, items, additional_discount_amount):
	"""Apply item list changes to a Sales Order document (not yet saved)."""
	new_item_map = {i["item_code"]: i for i in items}

	# Remove rows not in the new list
	so.items = [row for row in so.items if row.item_code in new_item_map]

	# Update existing rows
	existing_codes = {row.item_code for row in so.items}
	for row in so.items:
		override = new_item_map[row.item_code]
		if override.get("rate") is not None and abs(flt(override.get("rate")) - flt(row.rate)) >= 0.0001:
			_set_line_rate(row, override.get("rate"))
		row.qty = flt(override.get("qty", row.qty))
		row.discount_percentage = flt(override.get("discount_percentage", 0))
		# WEIGHT / CAJA / Nos from Pedidos "Tipo de peso" — must land on row.uom
		# or Frappe still validates against Nos ("Quantity cannot be a fraction").
		if override.get("uom") is not None or override.get("stock_uom") is not None:
			_apply_so_line_uom(row, override.get("uom") or override.get("stock_uom"))
		# Measured pack weight (gondolas → kg after scale). Keep qty as units.
		if "total_weight" in override and override.get("total_weight") is not None:
			tw = flt(override.get("total_weight"))
			row.total_weight = tw
			if flt(row.qty) > 0:
				row.weight_per_unit = tw / flt(row.qty)
		elif "weight_per_unit" in override and override.get("weight_per_unit") is not None:
			row.weight_per_unit = flt(override.get("weight_per_unit"))
			row.total_weight = flt(row.qty) * flt(row.weight_per_unit)
		row.amount = row.rate * row.qty

	# Add new items. Submitted SOs use ignore_validate_update_after_submit, so
	# set_missing_values never fills item_name/uom — set them explicitly or
	# MandatoryError: "Sales Order Item Row #N: Value missing for: Item Name".
	for item in items:
		if item["item_code"] not in existing_codes:
			code = item["item_code"]
			item_meta = (
				frappe.db.get_value(
					"Item", code, ["item_name", "stock_uom"], as_dict=True
				)
				or {}
			)
			item_name = (
				cstr(item.get("item_name") or "").strip()
				or cstr(item_meta.get("item_name") or "").strip()
				or code
			)
			row = so.append(
				"items",
				{
					"item_code": code,
					"item_name": item_name,
					"qty": flt(item.get("qty", 1)),
					"rate": flt(item.get("rate", 0)),
					"discount_percentage": flt(item.get("discount_percentage", 0)),
					"delivery_date": so.delivery_date,
				},
			)
			if item.get("uom") is not None or item.get("stock_uom") is not None:
				_apply_so_line_uom(row, item.get("uom") or item.get("stock_uom"))
			elif not cstr(getattr(row, "uom", "") or "").strip():
				# uom is mandatory on Sales Order Item — default from Item stock_uom.
				_apply_so_line_uom(row, item_meta.get("stock_uom") or "Nos")
			# total_weight / weight_per_unit on brand-new lines (same as update path)
			if "total_weight" in item and item.get("total_weight") is not None:
				tw = flt(item.get("total_weight"))
				row.total_weight = tw
				if flt(row.qty) > 0:
					row.weight_per_unit = tw / flt(row.qty)
			elif "weight_per_unit" in item and item.get("weight_per_unit") is not None:
				row.weight_per_unit = flt(item.get("weight_per_unit"))
				row.total_weight = flt(row.qty) * flt(row.weight_per_unit)

	so.apply_discount_on = "Grand Total"
	so.additional_discount_amount = flt(additional_discount_amount)
	_calculate_guest_preorder_totals(so)


@frappe.whitelist()
def update_guest_preorder_prices(preorder_name, items, additional_discount_amount=0):
	"""Update item rates/qty and global discount on a draft preorder (docstatus=0 only)."""
	import json as _json

	_require_guest_preorder_live_mutate()

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	if so.docstatus != 0:
		frappe.throw(_("Price editing is only allowed on draft orders (before confirming)"))

	if isinstance(items, str):
		items = _json.loads(items)

	item_map = {i["item_code"]: i for i in items}

	for row in so.items:
		if row.item_code in item_map:
			override = item_map[row.item_code]
			if override.get("rate") is not None and abs(flt(override.get("rate")) - flt(row.rate)) >= 0.0001:
				_set_line_rate(row, override.get("rate"))
			row.qty = flt(override.get("qty", row.qty))
			row.discount_percentage = flt(override.get("discount_percentage", 0))
			if override.get("uom") is not None or override.get("stock_uom") is not None:
				_apply_so_line_uom(row, override.get("uom") or override.get("stock_uom"))
			row.amount = row.rate * row.qty

	so.apply_discount_on = "Grand Total"
	so.additional_discount_amount = flt(additional_discount_amount)
	_calculate_guest_preorder_totals(so)
	so.flags.ignore_pricing_rule = True
	with _allow_weight_fractional_stock_qty(so):
		_save_guest_preorder_so(so)
	so.reload()
	_requeue_seller_amend_review(so.name, reason="seller_amend_prices")
	return get_guest_preorder(preorder_name)


@frappe.whitelist()
def reprice_guest_preorder_from_price_list(preorder_name, price_list=None):
	"""
	Refresh line rates from the selling price list without changing qty / weight.

	WEIGHT lines keep list $/Kg (PRECIO POR 1 KG). Nos/CAJA get list $/unit.
	Does not touch measured total_weight — only money columns.
	"""
	_require_guest_preorder_live_mutate()
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)
	if so.docstatus == 2:
		frappe.throw(_("Cannot edit a cancelled order"))

	pl = (
		cstr(price_list or "").strip()
		or cstr(getattr(so, "selling_price_list", None) or "").strip()
		or "Standard Selling"
	)
	codes = [d.item_code for d in (so.items or []) if d.item_code]
	rates = get_item_prices_bulk(codes, price_list=pl) or {}
	changed = 0
	for row in so.items or []:
		new_rate = flt(rates.get(row.item_code) or 0)
		if new_rate <= 0:
			new_rate = flt(get_item_price(row.item_code, pl) or 0)
		if new_rate <= 0:
			continue
		if abs(flt(row.rate) - new_rate) < 0.0001 and not flt(row.margin_rate_or_amount):
			continue
		_set_line_rate(row, new_rate, price_list_rate=new_rate)
		changed += 1

	if changed:
		so.flags.ignore_pricing_rule = True
		_calculate_guest_preorder_totals(so)
		with _allow_weight_fractional_stock_qty(so):
			_save_guest_preorder_so(so)
		frappe.db.commit()
		so.reload()

	out = get_guest_preorder(preorder_name)
	out["repriced_lines"] = changed
	return out


@frappe.whitelist()
def list_preorder_payment_modes():
	"""Enabled Mode of Payment names for Pedidos payment UI (Cash/Efectivo preferred)."""
	rows = frappe.get_all(
		"Mode of Payment",
		filters={"enabled": 1},
		fields=["name"],
		order_by="name asc",
		ignore_permissions=True,
	)
	names = [r.name for r in rows if r.name]
	preferred = ["Cash", "Efectivo", "Transferencia", "Bank Draft", "Card", "Cheque"]
	ordered = []
	seen = set()
	for p in preferred:
		if p in names and p not in seen:
			ordered.append(p)
			seen.add(p)
	for n in names:
		if n not in seen:
			ordered.append(n)
			seen.add(n)
	default = "Cash" if "Cash" in seen else ("Efectivo" if "Efectivo" in seen else (ordered[0] if ordered else "Cash"))
	return {"modes": ordered, "default": default}


@frappe.whitelist()
@idempotent_request
def record_preorder_payment(preorder_name, paid_amount, mode_of_payment="Efectivo", posting_date=None):
	"""Create a Payment Entry for a confirmed preorder."""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	if so.docstatus != 1:
		frappe.throw(_("Payment can only be recorded on submitted (confirmed) orders"))

	paid_amount = flt(paid_amount)
	if paid_amount <= 0:
		frappe.throw(_("Paid amount must be greater than zero"))

	company = so.company
	receivable_account, cash_account, mop = _resolve_preorder_payment_accounts(
		company, mode_of_payment, so.customer
	)

	outstanding = _guest_preorder_estimated_total(so) - flt(getattr(so, "advance_paid", 0))
	allocated = min(paid_amount, outstanding) if outstanding > 0 else paid_amount

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = company
	pe.party_type = "Customer"
	pe.party = so.customer
	pe.mode_of_payment = mop
	pe.paid_from = receivable_account
	pe.paid_to = cash_account
	pe.paid_from_account_currency = so.currency
	pe.paid_to_account_currency = so.currency
	pe.paid_amount = paid_amount
	pe.received_amount = paid_amount
	pe.reference_date = nowdate()
	pe.reference_no = preorder_name
	raw_pd = str(posting_date or "").strip()
	if raw_pd:
		try:
			pe.posting_date = str(getdate(raw_pd))
		except Exception:
			pass
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


@frappe.whitelist()
def update_preorder_payment(
	payment_name=None,
	posting_date=None,
	mode_of_payment=None,
	paid_amount=None,
):
	"""Amend a Pedidos payment: cancel PE + recreate (keeps SO historial via comment)."""
	payment_name = str(payment_name or "").strip()
	if not payment_name:
		frappe.throw(_("payment_name is required"))
	if not frappe.db.exists("Payment Entry", payment_name):
		frappe.throw(_("Payment Entry {0} not found").format(payment_name))

	frappe.flags.ignore_permissions = True
	pe = frappe.get_doc("Payment Entry", payment_name)
	frappe.flags.ignore_permissions = False

	so_name = None
	for ref in pe.references or []:
		if ref.reference_doctype == "Sales Order" and ref.reference_name:
			so_name = ref.reference_name
			break
	if not so_name:
		so_name = str(getattr(pe, "reference_no", "") or "").strip() or None
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		frappe.throw(_("Payment is not linked to a Sales Order"))

	so = frappe.get_doc("Sales Order", so_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	old_amount = flt(pe.received_amount or pe.paid_amount)
	old_mode = pe.mode_of_payment or ""
	old_date = str(pe.posting_date) if pe.posting_date else ""

	new_amount = flt(paid_amount) if paid_amount is not None and paid_amount != "" else old_amount
	if new_amount <= 0:
		frappe.throw(_("Paid amount must be greater than zero"))
	new_mode = str(mode_of_payment or "").strip() or old_mode or "Cash"
	new_date = str(posting_date or "").strip() or old_date or nowdate()

	if cint(pe.docstatus) == 1:
		pe.cancel()
	elif cint(pe.docstatus) == 0:
		pe.delete()

	# After cancel, SO advance_paid drops — recreate with new values.
	detail = record_preorder_payment(
		so_name,
		new_amount,
		mode_of_payment=new_mode,
		posting_date=new_date,
	)

	try:
		so.add_comment(
			"Comment",
			_("Payment amended: {0} → new entry (was {1} {2} on {3})").format(
				payment_name,
				old_amount,
				old_mode,
				old_date,
			),
		)
		frappe.db.commit()
	except Exception:
		pass

	return detail


def _resolve_preorder_mop_name(preferred: str | None = None) -> str | None:
	"""Pick an enabled Mode of Payment (Efectivo/Cash first for local cash)."""
	candidates = []
	if preferred:
		candidates.append(preferred)
	candidates.extend(["Efectivo", "Cash", "Transferencia", "Bank Draft"])
	seen = set()
	for name in candidates:
		key = (name or "").strip()
		if not key or key in seen:
			continue
		seen.add(key)
		if frappe.db.exists("Mode of Payment", key) and frappe.db.get_value(
			"Mode of Payment", key, "enabled"
		):
			return key
	return frappe.db.get_value("Mode of Payment", {"enabled": 1}, "name")


def _resolve_preorder_cash_account(company: str, mop: str | None) -> str | None:
	"""Cash/Bank account to receive payment into."""
	if mop:
		acc = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mop, "company": company},
			"default_account",
		)
		if acc:
			return acc

	for field in ("default_cash_account", "default_bank_account"):
		acc = frappe.db.get_value("Company", company, field)
		if acc and frappe.db.exists("Account", acc):
			return acc

	for account_type in ("Cash", "Bank"):
		acc = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": account_type, "is_group": 0, "disabled": 0},
			"name",
		)
		if acc:
			return acc

	# Argentine chart often leaves Caja untyped.
	for like in ("%Caja - %", "%Cash%", "%Bank Account%", "%Banco%"):
		acc = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"root_type": "Asset",
				"is_group": 0,
				"disabled": 0,
				"name": ("like", like),
			},
			"name",
		)
		if acc:
			return acc
	return None


def _resolve_preorder_receivable_account(company: str, customer: str | None = None) -> str | None:
	"""Customer receivable (debit) account for Payment Entry Receive."""
	if customer:
		acc = frappe.db.get_value(
			"Party Account",
			{"parent": customer, "parenttype": "Customer", "company": company},
			"account",
		)
		if acc:
			return acc

	# Prefer real trade debtors over a mis-set company default (e.g. supplier advances).
	for like in (
		"%Deudores locales%",
		"%Debtors%",
		"%Deudores%",
		"%Clientes%",
		"%Receivable%",
	):
		acc = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_type": "Receivable",
				"is_group": 0,
				"disabled": 0,
				"name": ("like", like),
			},
			"name",
		)
		if acc and "anticipo" not in acc.lower() and "proveedor" not in acc.lower():
			return acc

	acc = frappe.db.get_value("Company", company, "default_receivable_account")
	if acc and frappe.db.exists("Account", acc):
		# Skip supplier-advance style accounts for customer receipts.
		low = acc.lower()
		if "anticipo" not in low and "proveedor" not in low:
			return acc

	acc = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Receivable", "is_group": 0, "disabled": 0},
		"name",
		order_by="name asc",
	)
	if acc and "anticipo" not in acc.lower() and "proveedor" not in acc.lower():
		return acc
	return acc


def _ensure_preorder_mop_account(company: str, mop: str, cash_account: str) -> None:
	"""Attach cash account to Mode of Payment for this company when missing."""
	if not mop or not cash_account:
		return
	if frappe.db.exists("Mode of Payment Account", {"parent": mop, "company": company}):
		return
	if not frappe.db.exists("Mode of Payment", mop):
		return
	doc = frappe.get_doc("Mode of Payment", mop)
	doc.append("accounts", {"company": company, "default_account": cash_account})
	doc.save(ignore_permissions=True)


def _resolve_preorder_payment_accounts(
	company: str, mode_of_payment: str | None = None, customer: str | None = None
):
	"""Return (receivable_account, cash_account, mop_name) or throw a clear error."""
	mop = _resolve_preorder_mop_name(mode_of_payment)
	cash_account = _resolve_preorder_cash_account(company, mop)
	receivable_account = _resolve_preorder_receivable_account(company, customer)

	if cash_account and mop:
		_ensure_preorder_mop_account(company, mop, cash_account)

	if not receivable_account or not cash_account:
		missing = []
		if not receivable_account:
			missing.append(_("receivable (Company → Default Receivable Account)"))
		if not cash_account:
			missing.append(_("cash/bank (Mode of Payment Account or Company cash account)"))
		frappe.throw(
			_("Could not find debit/credit accounts for payment ({0}). Check company defaults.").format(
				", ".join(str(m) for m in missing)
			)
		)
	return receivable_account, cash_account, mop



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
		from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

		company = resolve_company()

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
	if _is_guest_preorder_sales_order(so):
		_save_guest_preorder_so(so)
	else:
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


def _delivery_note_for_sales_order(sales_order):
	"""Any non-cancelled Delivery Note already built against this Sales Order."""
	return frappe.db.get_value(
		"Delivery Note Item", {"against_sales_order": sales_order, "docstatus": ["!=", 2]}, "parent"
	)


def _default_company_warehouse(company=None):
	"""Company default depot, or the only active warehouse when uniquely defined."""
	company = (
		cstr(company or "").strip()
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	if not company:
		return None
	wh = cstr(frappe.db.get_value("Company", company, "custom_default_warehouse") or "").strip()
	if wh:
		return wh
	rows = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="name asc",
		limit_page_length=2,
		ignore_permissions=True,
	)
	if len(rows) == 1:
		return rows[0]
	return rows[0] if rows else None


def _warehouse_from_guest_tags(so_or_dict):
	return _parse_remarks_tags(_guest_preorder_tag_text(so_or_dict)).get("warehouse") or None


def _tms_logistics_map_for_orders(order_names):
	"""Batch-enrich Sales Orders with DN / MAT (Delivery Trip) / vehicle / PoD.

	Returns ``{ so_name: { warehouse, delivery_note, trip_name, trip_status,
	stop_idx, vehicle, vehicle_plate, driver, driver_name, delivery_at, pod } }``.
	"""
	names = [cstr(n).strip() for n in (order_names or []) if cstr(n).strip()]
	out = {n: {} for n in names}
	if not names:
		return out

	# SO → DN (latest non-cancelled)
	dn_rows = frappe.db.sql(
		"""
		SELECT dni.against_sales_order AS so_name, dni.parent AS dn_name, dn.set_warehouse
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.against_sales_order IN %(names)s
		  AND dn.docstatus != 2
		ORDER BY dn.creation DESC
		""",
		{"names": names},
		as_dict=True,
	)
	dn_by_so = {}
	dn_warehouse = {}
	for r in dn_rows or []:
		so = cstr(r.so_name)
		if so and so not in dn_by_so:
			dn_by_so[so] = r.dn_name
			dn_warehouse[r.dn_name] = cstr(r.set_warehouse or "").strip() or None

	dn_names = list({v for v in dn_by_so.values() if v})
	trip_by_dn = {}
	if dn_names:
		# Payment-split / Factura A / surcharge columns are added by a later patch;
		# sites that have not migrated yet must still list preorders without 1054.
		def _ds_sel(fieldname, alias):
			if frappe.db.has_column("Delivery Stop", fieldname):
				return f"ds.`{fieldname}` AS `{alias}`"
			return f"NULL AS `{alias}`"

		optional_ds_cols = ",\n\t\t\t\t".join(
			[
				_ds_sel("custom_payments_json", "payments_json"),
				_ds_sel("custom_payment_summary", "payment_summary"),
				_ds_sel("custom_requires_factura_a", "stop_requires_factura_a"),
				_ds_sel("custom_factura_a_status", "stop_factura_a_status"),
				_ds_sel("custom_surcharge_pct", "surcharge_pct"),
				_ds_sel("custom_surcharge_amount", "surcharge_amount"),
				_ds_sel("custom_surcharge_rule", "surcharge_rule"),
			]
		)
		pickup_wh_sel = (
			"dt.`custom_pickup_warehouse` AS pickup_warehouse"
			if frappe.db.has_column("Delivery Trip", "custom_pickup_warehouse")
			else "NULL AS pickup_warehouse"
		)
		# Prefer non-cancelled trips; Not Home attempts stay as audit but DN may be free.
		stop_rows = frappe.db.sql(
			f"""
			SELECT
				ds.delivery_note AS dn,
				ds.parent AS trip_name,
				ds.idx AS stop_idx,
				ds.visited AS visited,
				ds.estimated_arrival AS estimated_arrival,
				ds.custom_pod_recipient_name AS recipient_name,
				ds.custom_pod_recipient_id_number AS recipient_id,
				ds.custom_pod_signature AS signature,
				ds.custom_pod_notes AS pod_notes,
				ds.custom_pod_captured_at AS captured_at,
				ds.custom_outcome AS outcome,
				ds.custom_attempt_note AS attempt_note,
				ds.custom_photo_urls AS photo_urls,
				ds.custom_amount_due AS amount_due,
				ds.custom_amount_collected AS amount_collected,
				ds.custom_payment_method AS payment_method,
				{optional_ds_cols},
				dt.status AS trip_status,
				dt.docstatus AS trip_docstatus,
				dt.vehicle AS vehicle,
				dt.driver AS driver,
				dt.driver_name AS driver_name,
				{pickup_wh_sel},
				dt.departure_time AS departure_time
			FROM `tabDelivery Stop` ds
			INNER JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
			WHERE ds.delivery_note IN %(dns)s
			  AND dt.docstatus != 2
			  AND ifnull(ds.custom_outcome, '') != 'Not Home'
			ORDER BY dt.creation DESC
			""",
			{"dns": dn_names},
			as_dict=True,
		)
		for r in stop_rows or []:
			dn = cstr(r.dn)
			if not dn or dn in trip_by_dn:
				continue
			pod = None
			if cint(r.visited) or cstr(r.outcome or "").strip():
				photos = []
				if r.photo_urls:
					try:
						photos = frappe.parse_json(r.photo_urls) or []
					except Exception:
						photos = []
					if not isinstance(photos, list):
						photos = []
				payments = []
				if getattr(r, "payments_json", None):
					try:
						payments = frappe.parse_json(r.payments_json) or []
					except Exception:
						payments = []
					if not isinstance(payments, list):
						payments = []
				pod = {
					"recipient_name": r.recipient_name,
					"recipient_id_number": r.recipient_id,
					"signature": r.signature,
					"notes": r.pod_notes,
					"captured_at": str(r.captured_at) if r.captured_at else None,
					"outcome": r.outcome,
					"attempt_note": r.attempt_note,
					"photo_urls": photos,
					"amount_due": r.amount_due,
					"amount_collected": r.amount_collected,
					"payment_method": r.payment_method,
					"payments": payments,
					"payment_summary": getattr(r, "payment_summary", None) or r.payment_method,
					"requires_factura_a": bool(cint(getattr(r, "stop_requires_factura_a", 0) or 0)),
					"factura_a_status": cstr(getattr(r, "stop_factura_a_status", None) or "") or None,
					"surcharge_pct": getattr(r, "surcharge_pct", None),
					"surcharge_amount": getattr(r, "surcharge_amount", None),
					"surcharge_rule": cstr(getattr(r, "surcharge_rule", None) or "") or None,
				}
			plate = None
			veh = cstr(r.vehicle or "").strip() or None
			if veh:
				plate = frappe.db.get_value("Vehicle", veh, "license_plate") or veh
			trip_by_dn[dn] = {
				"trip_name": r.trip_name,
				"trip_status": r.trip_status,
				"stop_idx": cint(r.stop_idx) or None,
				"vehicle": veh,
				"vehicle_plate": plate,
				"driver": cstr(r.driver or "").strip() or None,
				"driver_name": cstr(r.driver_name or "").strip() or None,
				"pickup_warehouse": cstr(r.pickup_warehouse or "").strip() or None,
				"delivery_at": str(r.captured_at or r.estimated_arrival or "") or None,
				"departure_time": str(r.departure_time) if r.departure_time else None,
				"pod": pod,
			}

	# Company defaults for warehouse fallback (batched by SO company)
	companies = frappe.get_all(
		"Sales Order",
		filters={"name": ["in", names]},
		fields=["name", "company"],
		ignore_permissions=True,
	)
	company_by_so = {r.name: r.company for r in companies}
	default_wh_by_company = {}

	for so in names:
		dn = dn_by_so.get(so)
		trip = trip_by_dn.get(dn) if dn else None
		company = company_by_so.get(so)
		if company not in default_wh_by_company:
			default_wh_by_company[company] = _default_company_warehouse(company)
		wh = None
		if trip and trip.get("pickup_warehouse"):
			wh = trip["pickup_warehouse"]
		elif dn and dn_warehouse.get(dn):
			wh = dn_warehouse[dn]
		row = {
			"delivery_note": dn,
			"warehouse": wh or default_wh_by_company.get(company),
			"warehouse_defaulted": 0 if wh else 1,
			"trip_name": (trip or {}).get("trip_name"),
			"trip_status": (trip or {}).get("trip_status"),
			"stop_idx": (trip or {}).get("stop_idx"),
			"vehicle": (trip or {}).get("vehicle"),
			"vehicle_plate": (trip or {}).get("vehicle_plate"),
			"driver": (trip or {}).get("driver"),
			"driver_name": (trip or {}).get("driver_name"),
			"delivery_at": (trip or {}).get("delivery_at"),
			"departure_time": (trip or {}).get("departure_time"),
			"pod": (trip or {}).get("pod"),
		}
		out[so] = row
	return out


def _apply_tms_display_status(row):
	"""Overlay Delivery facets from MAT trip status + PoD outcome."""
	if not row:
		return row
	disp = cstr(row.get("display_status") or row.get("status") or "")
	base = _pipeline_base_status(disp)
	if base == "Completado" or disp in ("Cancelled", "Closed", "Archivado"):
		return row
	trip_st = cstr(row.get("trip_status") or "").strip()
	erp = cstr(row.get("status") or "").strip()
	pod = row.get("pod") if isinstance(row.get("pod"), dict) else {}
	outcome = cstr((pod or {}).get("outcome") or "").strip()
	on_delivery = (
		base == "Delivery"
		or erp in ("Delivery", "En Delivery")
		or trip_st == "In Transit"
	)
	if not on_delivery:
		return row
	row["display_status"] = _delivery_facet(trip_st, outcome)
	return row


def _set_dn_warehouse(dn_name, warehouse):
	wh = cstr(warehouse or "").strip()
	if not wh or not dn_name:
		return
	if not frappe.db.exists("Warehouse", wh):
		frappe.throw(_("Warehouse {0} not found").format(wh))
	frappe.db.set_value("Delivery Note", dn_name, "set_warehouse", wh, update_modified=False)
	frappe.db.sql(
		"""
		UPDATE `tabDelivery Note Item`
		SET warehouse=%s
		WHERE parent=%s AND docstatus != 2
		""",
		(wh, dn_name),
	)


@frappe.whitelist(allow_guest=True)
def _mat_day_centroid(trip_name):
	"""Average lat/lng of stops on a trip (for nearest-MAT scoring)."""
	rows = frappe.get_all(
		"Delivery Stop",
		filters={"parent": trip_name},
		fields=["lat", "lng"],
		ignore_permissions=True,
	)
	pts = [(flt(r.lat), flt(r.lng)) for r in rows if r.lat and r.lng]
	if not pts:
		return None, None
	return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


@frappe.whitelist(allow_guest=True)
def auto_assign_preorders_to_mats(preorder_names=None, company=None):
	"""Greedy MAT assign for Pedidos: same Entrega day → fill nearest open MAT to cap → next driver / new MAT.

	Uses TMS soft caps (``driver_day_max_orders``). Remitos are created as needed.
	Returns per-order assignment rows + created trip names.
	"""
	from erpnext.erpnext_integrations.ecommerce_api import tms_api
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	raw = preorder_names
	if isinstance(raw, str):
		try:
			raw = frappe.parse_json(raw)
		except Exception:
			raw = [raw] if cstr(raw).strip() else []
	if not isinstance(raw, (list, tuple)):
		raw = [raw] if raw else []
	names = []
	for n in raw:
		s = cstr(n or "").strip()
		if s and s not in ("null", "undefined") and s not in names:
			names.append(s)
	if not names:
		frappe.throw(_("Select at least one order."))

	company = cstr(company or "").strip() or resolve_company() or frappe.defaults.get_user_default("Company")
	settings = tms_api._load_tms_settings()
	max_orders = max(1, cint(settings.get("driver_day_max_orders") or 30))
	today = getdate()

	# Active drivers (fill order = existing planner list).
	drivers = frappe.get_all(
		"Driver",
		filters={"status": "Active"},
		fields=["name", "full_name"],
		order_by="full_name asc",
		ignore_permissions=True,
	)
	driver_ids = [d.name for d in drivers]

	# Open MATs with stop counts + day + centroid.
	open_trips = frappe.get_all(
		"Delivery Trip",
		filters={
			"docstatus": ["<", 2],
			"status": ["in", ["Draft", "Scheduled", ""]],
		},
		fields=[
			"name",
			"driver",
			"driver_name",
			"vehicle",
			"departure_time",
			"custom_pickup_warehouse",
			"company",
			"status",
		],
		ignore_permissions=True,
	)
	if company:
		open_trips = [t for t in open_trips if not t.company or t.company == company]

	trip_state = {}
	for t in open_trips:
		day = str(getdate(t.departure_time)) if t.departure_time else None
		if not day:
			continue
		cnt = cint(
			frappe.db.count("Delivery Stop", {"parent": t.name}) or 0
		)
		clat, clng = _mat_day_centroid(t.name)
		trip_state[t.name] = {
			"name": t.name,
			"day": day,
			"driver": cstr(t.driver or "").strip() or None,
			"driver_name": t.driver_name,
			"vehicle": t.vehicle,
			"warehouse": getattr(t, "custom_pickup_warehouse", None),
			"stop_count": cnt,
			"lat": clat,
			"lng": clng,
			"status": t.status,
		}

	# Order payloads (delivery day + geo).
	order_rows = []
	for so_name in names:
		if not frappe.db.exists("Sales Order", so_name):
			frappe.throw(_("Sales Order {0} not found").format(so_name))
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", so_name)
		frappe.flags.ignore_permissions = False
		if not _is_guest_preorder_sales_order(so):
			frappe.throw(_("Not a Guest Preorder: {0}").format(so_name))
		due = so.delivery_date or add_days(today, 1)
		due_d = getdate(due)
		if due_d < today:
			due_d = add_days(today, 1)
		lat = lng = None
		addr = so.shipping_address_name or so.customer_address
		if addr and frappe.db.has_column("Address", "custom_latitude"):
			geo = frappe.db.get_value(
				"Address",
				addr,
				["custom_latitude", "custom_longitude"],
				as_dict=True,
			)
			if geo and geo.custom_latitude:
				lat, lng = flt(geo.custom_latitude), flt(geo.custom_longitude)
		order_rows.append(
			{
				"name": so_name,
				"day": str(due_d),
				"lat": lat,
				"lng": lng,
				"customer": so.customer,
			}
		)
	order_rows.sort(key=lambda r: (r["day"], r["name"]))

	def _dist(olat, olng, tlat, tlng):
		if olat is None or olng is None or tlat is None or tlng is None:
			return 1e12
		return tms_api._haversine_km(olat, olng, tlat, tlng)

	def _pick_trip(day, olat, olng):
		cands = [
			t
			for t in trip_state.values()
			if t["day"] == day and t["stop_count"] < max_orders
		]
		if not cands:
			return None
		cands.sort(
			key=lambda t: (
				_dist(olat, olng, t["lat"], t["lng"]),
				t["stop_count"],
				t["name"],
			)
		)
		return cands[0]

	def _next_driver_for_day(day):
		"""Greedy: drivers without a trip that day first, else least-loaded."""
		used = {
			t["driver"]
			for t in trip_state.values()
			if t["day"] == day and t.get("driver")
		}
		for d in driver_ids:
			if d not in used:
				return d
		if not driver_ids:
			return None
		by_drv = {}
		for t in trip_state.values():
			if t["day"] != day or not t.get("driver"):
				continue
			by_drv[t["driver"]] = by_drv.get(t["driver"], 0) + t["stop_count"]
		return min(driver_ids, key=lambda d: (by_drv.get(d, 0), d))

	def _register_trip(trip_name, day, *, stop_count=0, lat=None, lng=None):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Delivery Trip", trip_name)
		frappe.flags.ignore_permissions = False
		trip_state[trip_name] = {
			"name": trip_name,
			"day": day,
			"driver": cstr(doc.driver or "").strip() or None,
			"driver_name": doc.driver_name,
			"vehicle": doc.vehicle,
			"warehouse": getattr(doc, "custom_pickup_warehouse", None),
			"stop_count": stop_count,
			"lat": lat,
			"lng": lng,
			"status": doc.status,
		}
		return trip_state[trip_name]

	assignments = []
	created_trips = []
	for row in order_rows:
		day = row["day"]
		picked = _pick_trip(day, row["lat"], row["lng"])
		created_new = False
		if picked:
			detail = update_guest_preorder_logistics(
				preorder_name=row["name"],
				trip_name=picked["name"],
				vehicle=picked.get("vehicle"),
				driver=picked.get("driver"),
				warehouse=picked.get("warehouse"),
			)
			trip_name = picked["name"]
		else:
			# No open MAT with capacity → create remito + new trip for next driver (greedy).
			dn = _delivery_note_for_sales_order(row["name"])
			if not dn:
				created_dn = create_delivery_note_for_preorder(row["name"])
				dn = (created_dn or {}).get("delivery_note") or _delivery_note_for_sales_order(
					row["name"]
				)
			if not dn:
				frappe.throw(_("Could not create remito for {0}").format(row["name"]))
			drv = _next_driver_for_day(day)
			created = tms_api.create_trip(
				date=day,
				driver=drv,
				delivery_note_names=[dn],
				company=company,
			)
			trip_name = created.get("trip")
			if not trip_name:
				frappe.throw(_("Could not create MAT for {0}").format(row["name"]))
			created_new = True
			created_trips.append(trip_name)
			picked = _register_trip(
				trip_name,
				day,
				stop_count=1,
				lat=row["lat"],
				lng=row["lng"],
			)
			detail = update_guest_preorder_logistics(
				preorder_name=row["name"],
				trip_name=trip_name,
				vehicle=picked.get("vehicle"),
				driver=picked.get("driver"),
				warehouse=picked.get("warehouse"),
			)
		# Refresh capacity + centroid after assign.
		st = trip_state.get(trip_name)
		if st and not created_new:
			st["stop_count"] = cint(st.get("stop_count") or 0) + 1
			if row["lat"] is not None and row["lng"] is not None:
				if st["lat"] is None:
					st["lat"], st["lng"] = row["lat"], row["lng"]
				else:
					n = st["stop_count"]
					st["lat"] = ((st["lat"] * (n - 1)) + row["lat"]) / n
					st["lng"] = ((st["lng"] * (n - 1)) + row["lng"]) / n
		assignments.append(
			{
				"preorder_name": row["name"],
				"trip_name": trip_name,
				"day": day,
				"created_trip": created_new,
				"driver": (picked or {}).get("driver"),
				"detail": detail,
			}
		)

	return {
		"ok": True,
		"assignments": [
			{
				"preorder_name": a["preorder_name"],
				"trip_name": a["trip_name"],
				"day": a["day"],
				"created_trip": a["created_trip"],
				"driver": a.get("driver"),
				"display_status": (a.get("detail") or {}).get("display_status"),
				"delivery_note": (a.get("detail") or {}).get("delivery_note"),
				"vehicle": (a.get("detail") or {}).get("vehicle"),
				"vehicle_plate": (a.get("detail") or {}).get("vehicle_plate"),
				"driver_name": (a.get("detail") or {}).get("driver_name"),
				"warehouse": (a.get("detail") or {}).get("warehouse"),
				"stop_idx": (a.get("detail") or {}).get("stop_idx"),
				"trip_status": (a.get("detail") or {}).get("trip_status"),
			}
			for a in assignments
		],
		"created_trips": created_trips,
		"max_orders": max_orders,
	}


def list_available_mats(search=None, limit=50, include_completed=0):
	"""Searchable Delivery Trips (MAT-DT-…) for Pedidos assignment.

	Returns open MAT plans (Draft / Scheduled / In Transit) with driver + vehicle.
	"""
	limit = max(1, min(cint(limit) or 50, 200))
	q = cstr(search or "").strip()
	# Available = Draft/Scheduled. Search also surfaces In Transit MATs by name.
	if cint(include_completed):
		statuses = ["Draft", "Scheduled", "In Transit", "", "Completed"]
	elif q:
		statuses = ["Draft", "Scheduled", "In Transit", ""]
	else:
		statuses = ["Draft", "Scheduled", ""]
	filters = {"docstatus": ["!=", 2], "status": ["in", statuses]}
	or_filters = None
	if q:
		or_filters = [
			["name", "like", f"%{q}%"],
			["driver_name", "like", f"%{q}%"],
			["driver", "like", f"%{q}%"],
			["vehicle", "like", f"%{q}%"],
		]
	rows = frappe.get_all(
		"Delivery Trip",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"status",
			"docstatus",
			"driver",
			"driver_name",
			"vehicle",
			"departure_time",
			"custom_pickup_warehouse",
			"company",
		],
		order_by="modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	veh_names = [r.vehicle for r in rows if r.vehicle]
	plate_map = {}
	if veh_names:
		for v in frappe.get_all(
			"Vehicle",
			filters={"name": ["in", veh_names]},
			fields=["name", "license_plate"],
			ignore_permissions=True,
		):
			plate_map[v.name] = v.license_plate or v.name
	out = []
	for r in rows:
		out.append(
			{
				"name": r.name,
				"status": r.status,
				"docstatus": r.docstatus,
				"driver": r.driver,
				"driver_name": r.driver_name,
				"vehicle": r.vehicle,
				"vehicle_plate": plate_map.get(r.vehicle) if r.vehicle else None,
				"warehouse": getattr(r, "custom_pickup_warehouse", None),
				"departure_time": str(r.departure_time) if r.departure_time else None,
				"company": r.company,
			}
		)
	return {"rows": out, "total": len(out)}


@frappe.whitelist(allow_guest=True)
def update_guest_preorder_logistics(
	preorder_name=None,
	warehouse=None,
	trip_name=None,
	vehicle=None,
	driver=None,
	clear_trip=0,
):
	"""Admin Pedidos logistics: warehouse, MAT (Delivery Trip), car, and driver.

	Creates a Delivery Note when assigning a MAT / warehouse if missing.
	Vehicle / driver options for the UI should come from the selected MAT
	(and planner context); changing them updates the trip assignment.
	"""
	_require_guest_preorder_live_mutate()
	name = cstr(preorder_name or "").strip()
	if not name or not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name or "?"))

	so = frappe.get_doc("Sales Order", name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)
	if so.docstatus == 2:
		frappe.throw(_("Cannot edit a cancelled order"))

	wh_in = None if warehouse is None else cstr(warehouse).strip()
	trip_in = None if trip_name is None else cstr(trip_name).strip()
	veh_in = None if vehicle is None else cstr(vehicle).strip()
	drv_in = None if driver is None else cstr(driver).strip()
	do_clear = cint(clear_trip)

	if wh_in:
		if not frappe.db.exists("Warehouse", wh_in):
			frappe.throw(_("Warehouse {0} not found").format(wh_in))
		_update_guest_preorder_tag(so, "warehouse", wh_in)
		tag_fn = _guest_preorder_tag_fieldname()
		if tag_fn:
			frappe.db.set_value("Sales Order", name, tag_fn, getattr(so, tag_fn, None))

	# Ensure remito when assigning trip or warehouse on a confirmed order.
	dn_name = _delivery_note_for_sales_order(name)
	need_dn = bool(trip_in) or bool(wh_in) or bool(veh_in)
	if need_dn and not dn_name and cint(so.docstatus) == 1:
		created = create_delivery_note_for_preorder(name)
		dn_name = (created or {}).get("delivery_note") or _delivery_note_for_sales_order(name)

	if wh_in and dn_name:
		_set_dn_warehouse(dn_name, wh_in)

	# Current trip for this DN
	current_trip = None
	if dn_name:
		current_trip = frappe.db.sql(
			"""
			SELECT ds.parent AS trip_name, dt.docstatus, dt.status
			FROM `tabDelivery Stop` ds
			INNER JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
			WHERE ds.delivery_note=%s AND dt.docstatus != 2
			  AND ifnull(ds.custom_outcome, '') != 'Not Home'
			ORDER BY dt.creation DESC
			LIMIT 1
			""",
			(dn_name,),
			as_dict=True,
		)
		current_trip = current_trip[0] if current_trip else None

	from erpnext.erpnext_integrations.ecommerce_api import tms_api

	if do_clear and current_trip and dn_name:
		if cint(current_trip.docstatus) == 0:
			tms_api.remove_stops_from_trip(current_trip.trip_name, [dn_name])
		else:
			frappe.throw(_("Cannot remove from a published MAT — cancel the trip in Rutas first."))
		current_trip = None

	if trip_in:
		if not frappe.db.exists("Delivery Trip", trip_in):
			frappe.throw(_("MAT / trip {0} not found").format(trip_in))
		if not dn_name:
			frappe.throw(_("Confirm the order and create a remito before assigning a MAT."))
		# Move off previous draft trip if different.
		if current_trip and current_trip.trip_name != trip_in and cint(current_trip.docstatus) == 0:
			try:
				tms_api.remove_stops_from_trip(current_trip.trip_name, [dn_name])
			except Exception:
				frappe.log_error(frappe.get_traceback(), "pedidos clear previous MAT")
		# Add to target MAT (steal=True lets admin pull from other drafts).
		try:
			tms_api.add_stops_to_trip(trip_in, [dn_name], allow_steal=1)
		except TypeError:
			tms_api.add_stops_to_trip(trip_in, [dn_name])
		set_guest_preorder_status(name, "Delivery", source="tms_claim")
		current_trip = frappe._dict(trip_name=trip_in, docstatus=frappe.db.get_value("Delivery Trip", trip_in, "docstatus"), status=frappe.db.get_value("Delivery Trip", trip_in, "status"))

	target_trip = trip_in or (current_trip.trip_name if current_trip else None)

	if wh_in and target_trip:
		# Admin override — allow on Draft via API, force on published.
		docstatus = cint(frappe.db.get_value("Delivery Trip", target_trip, "docstatus") or 0)
		if docstatus == 0:
			try:
				tms_api.update_trip_assignment(target_trip, pickup_warehouse=wh_in)
			except Exception:
				frappe.db.set_value("Delivery Trip", target_trip, "custom_pickup_warehouse", wh_in)
		else:
			frappe.db.set_value("Delivery Trip", target_trip, "custom_pickup_warehouse", wh_in)

	if veh_in is not None and target_trip:
		if veh_in and not frappe.db.exists("Vehicle", veh_in):
			frappe.throw(_("Vehicle {0} not found").format(veh_in))
		docstatus = cint(frappe.db.get_value("Delivery Trip", target_trip, "docstatus") or 0)
		if docstatus == 0 and veh_in:
			try:
				tms_api.update_trip_assignment(target_trip, vehicle=veh_in)
			except Exception:
				frappe.db.set_value("Delivery Trip", target_trip, "vehicle", veh_in or None)
		else:
			frappe.db.set_value("Delivery Trip", target_trip, "vehicle", veh_in or None)

	if drv_in is not None and target_trip:
		if drv_in and not frappe.db.exists("Driver", drv_in):
			frappe.throw(_("Driver {0} not found").format(drv_in))
		docstatus = cint(frappe.db.get_value("Delivery Trip", target_trip, "docstatus") or 0)
		if docstatus == 0 and drv_in:
			try:
				tms_api.update_trip_assignment(target_trip, driver=drv_in)
			except Exception:
				driver_doc = (
					frappe.db.get_value("Driver", drv_in, ["full_name"], as_dict=True) if drv_in else None
				)
				frappe.db.set_value("Delivery Trip", target_trip, "driver", drv_in or None)
				if driver_doc:
					frappe.db.set_value(
						"Delivery Trip", target_trip, "driver_name", driver_doc.full_name
					)
		else:
			driver_doc = (
				frappe.db.get_value("Driver", drv_in, ["full_name"], as_dict=True) if drv_in else None
			)
			frappe.db.set_value("Delivery Trip", target_trip, "driver", drv_in or None)
			if driver_doc:
				frappe.db.set_value(
					"Delivery Trip", target_trip, "driver_name", driver_doc.full_name
				)
			elif not drv_in:
				frappe.db.set_value("Delivery Trip", target_trip, "driver_name", None)

	frappe.db.commit()
	return get_guest_preorder(name)


def _preorder_address_line(so, *, allow_customer_primary=True) -> str:
	"""Best-effort street text for a guest preorder (Address doc or guest tag).

	``allow_customer_primary=False`` is used by the planner deliverability gate so an
	empty guest form does not silently inherit Consumidor Final's last primary.
	"""
	addr_name = cstr(
		getattr(so, "shipping_address_name", None) or getattr(so, "customer_address", None) or ""
	).strip()
	if addr_name and frappe.db.exists("Address", addr_name):
		row = frappe.db.get_value(
			"Address",
			addr_name,
			["address_line1", "address_line2", "city"],
			as_dict=True,
		) or {}
		bits = [
			cstr(row.get("address_line1") or "").strip(),
			cstr(row.get("address_line2") or "").strip(),
			cstr(row.get("city") or "").strip(),
		]
		street = ", ".join(b for b in bits if b and b != "-")
		if street:
			return street

	tags = _parse_remarks_tags(_guest_preorder_tag_text(so))
	guest = cstr(tags.get("guest_address") or "").strip()
	if guest and guest not in ("-", "null", "undefined"):
		return guest

	if not allow_customer_primary:
		return ""

	cust = cstr(getattr(so, "customer", None) or "").strip()
	if cust:
		geo = _customer_address_zone_map([cust]).get(cust) or {}
		fallback = cstr(geo.get("address") or "").strip()
		if fallback and fallback != "-":
			return fallback
	return ""


def _orden_has_explicit_direction(so):
	"""True when this Orden should be treated as deliverable / planner-eligible.

	- ``guest_address`` tag always counts (consulta form / ops location field).
	- Named customers (not Consumidor Final) may use their primary Address.
	- Anonymous Consumidor Final with no guest_address is **not** deliverable even
	  if CF inherited a primary street from a previous guest order.
	"""
	tags = _parse_remarks_tags(_guest_preorder_tag_text(so))
	guest = cstr(tags.get("guest_address") or "").strip()
	if guest and guest not in ("-", "null", "undefined"):
		return True
	cust = cstr(getattr(so, "customer", None) or "").strip()
	if cust and cust != "Consumidor Final":
		return bool(_preorder_address_line(so, allow_customer_primary=True))
	return False


def _materialize_preorder_shipping_address(so, *, require_explicit=False):
	"""Ensure SO has a linked shipping Address from guest_address / customer primary.

	Guest consultas often only store ``guest_address`` in remarks/terms — the
	planner / remito path needs a real Address on ``shipping_address_name``.

	When ``require_explicit=True`` (Orden gate), anonymous guests without
	``guest_address`` are rejected — see ``_orden_has_explicit_direction``.
	"""
	if require_explicit and not _orden_has_explicit_direction(so):
		return {"address_name": None, "address_line": "", "created": False}

	existing = cstr(
		getattr(so, "shipping_address_name", None) or getattr(so, "customer_address", None) or ""
	).strip()
	if existing and frappe.db.exists("Address", existing):
		line = _preorder_address_line(so, allow_customer_primary=True)
		if line:
			return {
				"address_name": existing,
				"address_line": line,
				"created": False,
			}

	line = _preorder_address_line(so, allow_customer_primary=True)
	if not line:
		return {"address_name": None, "address_line": "", "created": False}

	customer = cstr(getattr(so, "customer", None) or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		return {"address_name": None, "address_line": line, "created": False}

	country = frappe.db.get_default("country") or "Argentina"
	# Reuse matching customer address when present.
	existing_rows = frappe.db.sql(
		"""
		SELECT addr.name
		FROM `tabAddress` addr
		INNER JOIN `tabDynamic Link` link ON link.parent = addr.name
		WHERE link.link_doctype = 'Customer'
		  AND link.link_name = %s
		  AND TRIM(IFNULL(addr.address_line1, '')) = %s
		LIMIT 1
		""",
		(customer, line),
		as_dict=True,
	)
	if existing_rows:
		addr_name = existing_rows[0].name
	else:
		cust_title = (
			frappe.db.get_value("Customer", customer, "customer_name") or customer
		)
		addr = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": cust_title,
				"address_type": "Shipping",
				"address_line1": line,
				"city": "-",
				"country": country,
				"links": [{"link_doctype": "Customer", "link_name": customer}],
			}
		)
		addr.insert(ignore_permissions=True)
		addr_name = addr.name
		# Keep customer primary in sync when empty / placeholder.
		cust_doc = frappe.get_doc("Customer", customer)
		primary = cstr(getattr(cust_doc, "customer_primary_address", None) or "").strip()
		primary_line = cstr(getattr(cust_doc, "primary_address", None) or "").strip()
		if not primary or primary_line in ("", "-"):
			cust_doc.customer_primary_address = addr_name
			cust_doc.primary_address = line
			cust_doc.save(ignore_permissions=True)

	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"shipping_address_name": addr_name,
			"customer_address": addr_name,
		},
		update_modified=False,
	)
	so.shipping_address_name = addr_name
	so.customer_address = addr_name
	return {"address_name": addr_name, "address_line": line, "created": True}


def _try_geocode_address_soft(address_name):
	"""Best-effort geocode; never raises — returns {geocoded, warning?}."""
	name = cstr(address_name or "").strip()
	if not name or not frappe.db.exists("Address", name):
		return {"geocoded": False}
	if not frappe.db.has_column("Address", "custom_latitude"):
		return {"geocoded": False}
	lat = flt(frappe.db.get_value("Address", name, "custom_latitude") or 0)
	lng = flt(frappe.db.get_value("Address", name, "custom_longitude") or 0)
	if lat and lng:
		return {"geocoded": True, "lat": lat, "lng": lng, "cached": True}
	try:
		from erpnext.erpnext_integrations.ecommerce_api import tms_api

		# Prefer Google when configured; Nominatim via geocode_query fallback.
		try:
			geo = tms_api.geocode_address(name)
			return {
				"geocoded": True,
				"lat": geo.get("lat"),
				"lng": geo.get("lng"),
				"cached": bool(geo.get("cached")),
			}
		except Exception:
			line = cstr(frappe.db.get_value("Address", name, "address_line1") or "").strip()
			if not line:
				return {
					"geocoded": False,
					"warning": _(
						"No se pudo validar la dirección en el mapa; el pedido igual puede programarse."
					),
				}
			hit = tms_api.geocode_query(query=line, region="ar") or {}
			if not hit.get("lat") or not hit.get("lng"):
				return {
					"geocoded": False,
					"warning": _(
						"No se pudo validar la dirección en el mapa; el pedido igual puede programarse."
					),
				}
			frappe.db.set_value(
				"Address",
				name,
				{
					"custom_latitude": hit.get("lat"),
					"custom_longitude": hit.get("lng"),
				},
				update_modified=False,
			)
			return {
				"geocoded": True,
				"lat": hit.get("lat"),
				"lng": hit.get("lng"),
				"cached": False,
			}
	except Exception:
		return {
			"geocoded": False,
			"warning": _(
				"No se pudo validar la dirección en el mapa; el pedido igual puede programarse."
			),
		}


def _no_address_delivery_warning():
	return _(
		"Este pedido no tiene dirección — no se entregará ni aparecerá en el "
		"planificador hasta asignar una dirección."
	)


def _ensure_planner_delivery_note(so):
	"""Create a submitted remito for stock/claim paths (optional for planning).

	Rutas ``get_pending_deliveries`` lists Orden/Preparado Sales Orders by
	``delivery_date`` without requiring a remito. Remitos are still created when
	claiming onto a MAT / explicit create. When there is no deliverable address we
	skip remito creation and return ``not_deliverable`` + warning.

	Does **not** change pipeline status (Orden stays Orden until claim / manual remito).
	"""
	warnings = []
	if cint(getattr(so, "docstatus", 0)) != 1:
		return {
			"ok": False,
			"planner_ready": False,
			"not_deliverable": False,
			"delivery_note": None,
			"delivery_warning": _("Confirm the order before creating a delivery note."),
			"warnings": warnings,
		}

	addr = _materialize_preorder_shipping_address(so, require_explicit=True)
	addr_name = addr.get("address_name")
	addr_line = cstr(addr.get("address_line") or "").strip()
	if not addr_name or not addr_line:
		warn = _no_address_delivery_warning()
		warnings.append(warn)
		return {
			"ok": False,
			"planner_ready": False,
			"not_deliverable": True,
			"delivery_note": _delivery_note_for_sales_order(so.name),
			"delivery_warning": warn,
			"address_name": None,
			"address_line": addr_line or None,
			"geocoded": False,
			"warnings": warnings,
		}

	dn_name = _delivery_note_for_sales_order(so.name)
	stock_warnings = []
	created = False
	if not dn_name:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		try:
			dn = make_delivery_note(so.name)
			# Ensure remito carries the shipping address we just materialized.
			if addr_name:
				dn.shipping_address_name = addr_name
				dn.customer_address = addr_name
			preferred_wh = (
				_warehouse_from_guest_tags(so)
				or frappe.db.get_value("Company", dn.company, "custom_default_warehouse")
				or _default_company_warehouse(dn.company)
			)
			if preferred_wh:
				for row in dn.items:
					row.warehouse = preferred_wh
				dn.set_warehouse = preferred_wh
			for row in dn.items:
				if hasattr(row, "allow_zero_valuation_rate"):
					row.allow_zero_valuation_rate = 1
			stock_warnings = _delivery_note_stock_shortages(dn)
			dn.insert(ignore_permissions=True)
			dn.flags.ignore_permissions = True
			_submit_delivery_note_allowing_negative(dn)
			dn_name = dn.name
			created = True
			frappe.db.commit()
		except Exception as e:
			# Discard draft remito without rolling back the Orden status change.
			try:
				draft = getattr(dn, "name", None) if "dn" in locals() else None
				if draft and frappe.db.exists("Delivery Note", draft):
					frappe.delete_doc(
						"Delivery Note", draft, ignore_permissions=True, force=True
					)
					frappe.db.commit()
			except Exception:
				pass
			# frappe.throw leaves 417 / message_log even when caught — clear so the
			# Orden response stays HTTP 200 with delivery_warning instead of 417.
			try:
				frappe.clear_messages()
			except Exception:
				pass
			if hasattr(frappe.local, "response") and isinstance(frappe.local.response, dict):
				frappe.local.response.pop("http_status_code", None)
			err = cstr(e)
			warn = _(
				"Pedido en Orden, pero no se pudo crear el remito para el planificador: {0}"
			).format(err)
			warnings.append(warn)
			frappe.log_error(frappe.get_traceback(), f"planner remito failed for {so.name}")
			return {
				"ok": False,
				"planner_ready": False,
				"not_deliverable": False,
				"delivery_note": None,
				"delivery_note_created": False,
				"delivery_warning": warn,
				"address_name": addr_name,
				"address_line": addr_line,
				"geocoded": False,
				"stock_warnings": stock_warnings,
				"warnings": warnings,
			}

	# Geocode only when we just created address/remito (avoid Nominatim spam on week sync).
	geocoded = False
	if created or addr.get("created"):
		geo = _try_geocode_address_soft(addr_name)
		geocoded = bool(geo.get("geocoded"))
		if geo.get("warning"):
			warnings.append(geo["warning"])
	elif frappe.db.has_column("Address", "custom_latitude"):
		geocoded = bool(flt(frappe.db.get_value("Address", addr_name, "custom_latitude") or 0))

	return {
		"ok": True,
		"planner_ready": True,
		"not_deliverable": False,
		"delivery_note": dn_name,
		"delivery_note_created": created,
		"delivery_warning": warnings[0] if warnings else None,
		"address_name": addr_name,
		"address_line": addr_line,
		"geocoded": geocoded,
		"stock_warnings": stock_warnings,
		"warnings": warnings,
	}


def _attach_planner_gate_fields(payload, gate):
	"""Merge planner address/remito gate onto a guest-preorder API payload."""
	if not isinstance(payload, dict) or not isinstance(gate, dict):
		return payload
	payload["planner_ready"] = bool(gate.get("planner_ready"))
	payload["not_deliverable"] = bool(gate.get("not_deliverable"))
	payload["delivery_warning"] = gate.get("delivery_warning")
	payload["geocoded"] = bool(gate.get("geocoded"))
	if gate.get("delivery_note"):
		payload["delivery_note"] = gate.get("delivery_note")
	if gate.get("address_line"):
		payload["address"] = gate.get("address_line")
	if gate.get("stock_warnings"):
		payload["stock_warnings"] = gate["stock_warnings"]
	if gate.get("warnings"):
		payload["warnings"] = gate["warnings"]
	return payload


def sync_orden_planner_remitos(company=None):
	"""Ensure Orden/Preparado guest preorders with an address have a remito.

	Called from the Rutas week bundle so already-confirmed orders (created
	before this gate) become visible without a manual "Crear remito".
	"""
	from erpnext.erpnext_integrations.ecommerce_api.tms_api import _list_claimable_preorders

	created = []
	skipped = []
	for row in _list_claimable_preorders(company=company) or []:
		so_name = cstr(row.get("preorder_name") or "").strip()
		if not so_name or not frappe.db.exists("Sales Order", so_name):
			continue
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", so_name)
		frappe.flags.ignore_permissions = False
		gate = _ensure_planner_delivery_note(so)
		if gate.get("delivery_note_created") and gate.get("delivery_note"):
			created.append(gate["delivery_note"])
		elif gate.get("not_deliverable"):
			skipped.append(so_name)
	return {"created": created, "skipped_no_address": skipped}


@frappe.whitelist()
@idempotent_request
def create_delivery_note_for_preorder(preorder_name):
	"""Create + submit a real Delivery Note from a confirmed guest preorder,
	so it becomes visible in the TMS dispatcher (get_pending_deliveries only
	lists submitted Delivery Notes).

	Does **not** move the pipeline to Delivery — that requires a MAT
	(``update_guest_preorder_logistics`` / PoD claim). Idempotent: reuses an
	existing linked DN instead of creating a duplicate.

	Insufficient warehouse qty does **not** block submit — stock may go
	negative (Pedidos remitos are operational; shortfalls are warned only).
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)
	if so.docstatus != 1:
		frappe.throw(_("Confirm the order before creating a delivery note."))

	gate = _ensure_planner_delivery_note(so)
	if gate.get("not_deliverable"):
		frappe.throw(gate.get("delivery_warning") or _no_address_delivery_warning())
	if not gate.get("delivery_note"):
		frappe.throw(
			gate.get("delivery_warning")
			or _("Could not create Delivery Note for {0}").format(preorder_name)
		)

	# Remito alone stays on Orden/Preparado; MAT assignment advances to Delivery.
	detail = get_guest_preorder(preorder_name)
	return _attach_planner_gate_fields(detail, gate)


def _delivery_note_stock_shortages(dn):
	"""Soft report of lines where required qty exceeds warehouse balance."""
	from erpnext.stock.utils import get_stock_balance

	out = []
	for row in dn.get("items") or []:
		wh = cstr(getattr(row, "warehouse", None) or "").strip()
		code = cstr(getattr(row, "item_code", None) or "").strip()
		need = flt(getattr(row, "stock_qty", None) or getattr(row, "qty", None) or 0)
		if not wh or not code or need <= 0:
			continue
		try:
			bal = flt(get_stock_balance(code, wh, dn.posting_date, dn.posting_time))
		except TypeError:
			bal = flt(get_stock_balance(code, wh))
		except Exception:
			continue
		if bal + 1e-9 < need:
			out.append(
				{
					"item_code": code,
					"item_name": getattr(row, "item_name", None) or code,
					"warehouse": wh,
					"required": need,
					"available": bal,
				}
			)
	return out


def _submit_delivery_note_allowing_negative(dn):
	"""Submit DN even when warehouse qty is short (allow negative stock)."""
	with _temporarily_allow_negative_stock():
		orig = dn.update_stock_ledger

		def _update_stock_ledger(allow_negative_stock=False):
			return orig(allow_negative_stock=True)

		dn.update_stock_ledger = _update_stock_ledger
		dn.submit()


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
		"message": "SilkOS E-Commerce Integration API is running",
		"frappe_version": frappe.__version__,
		"site": frappe.local.site,
	}


@frappe.whitelist(allow_guest=True)
def get_deploy_info():
	"""Last backend deploy time (ERP_DEPLOY_AT env, else this module's mtime)."""
	from datetime import datetime, timezone

	raw = (os.environ.get("ERP_DEPLOY_AT") or os.environ.get("DEPLOY_AT") or "").strip()
	source = "env"
	if not raw:
		source = "module_mtime"
		try:
			mtime = os.path.getmtime(os.path.abspath(__file__))
			raw = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
		except OSError:
			raw = ""
	return {"deployed_at": raw, "source": source}


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


def _normalize_pos_payments(payment_method, payments, sale_mode):
	"""Build [{mode_of_payment, amount, cash_received}] from mixed or single pay."""
	import json

	BLACK_ALLOWED_METHODS = {"Cash", "Mobile Money"}
	rows = []
	if isinstance(payments, str):
		payments = json.loads(payments) if payments else []
	if payments:
		for p in payments:
			mode = (p.get("mode_of_payment") or p.get("method") or "").strip()
			amount = flt(p.get("amount") or 0)
			if amount <= 0:
				continue
			rows.append({
				"mode_of_payment": mode or "Cash",
				"amount": amount,
				"cash_received": flt(p.get("cash_received") or 0),
			})
	if not rows:
		rows = [{
			"mode_of_payment": payment_method or "Cash",
			"amount": 0,  # filled with outstanding later
			"cash_received": 0,
		}]

	if sale_mode == "BLACK":
		for p in rows:
			if p["mode_of_payment"] not in BLACK_ALLOWED_METHODS:
				frappe.throw(
					_(
						"Payment method '{0}' is not allowed when sale_mode=BLACK. "
						"Allowed methods: {1}."
					).format(p["mode_of_payment"], ", ".join(sorted(BLACK_ALLOWED_METHODS))),
					exc=frappe.ValidationError,
				)
	return rows


def _submit_pos_payments(invoice, payments, receipt_number, remarks_tag, cash_received=0):
	"""Create one Payment Entry per split. Last row absorbs rounding remainder."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	invoice.reload()
	remaining = flt(invoice.outstanding_amount)
	ids = []
	for i, p in enumerate(payments):
		if remaining <= 0:
			break
		amount = flt(p.get("amount") or 0)
		if i == len(payments) - 1 or amount <= 0:
			amount = remaining
		amount = min(amount, remaining)
		if amount <= 0:
			continue
		mode = p.get("mode_of_payment") or "Cash"
		if not frappe.db.exists("Mode of Payment", mode):
			mode = "Cash"
		pe = get_payment_entry("Sales Invoice", invoice.name)
		pe.mode_of_payment = mode
		pe.paid_amount = amount
		pe.received_amount = amount
		if pe.references:
			pe.references[0].allocated_amount = amount
		pe.reference_no = receipt_number
		pe.reference_date = nowdate()
		row_remarks = remarks_tag
		received = flt(p.get("cash_received") or 0) or (flt(cash_received) if mode == "Cash" else 0)
		if received:
			row_remarks = f"{remarks_tag} | cash_received:{received}"
		pe.remarks = row_remarks
		pe.insert(ignore_permissions=True)
		pe.submit()
		ids.append(pe.name)
		invoice.reload()
		remaining = flt(invoice.outstanding_amount)
	return ids


def _pos_sale_required_item_codes(items: list) -> list[str]:
	"""Line SKUs plus Product Bundle components (stock moves use components)."""
	codes = []
	seen = set()
	for item in items or []:
		code = (item.get("item_code") if isinstance(item, dict) else None) or ""
		code = _normalize_pos_caja_item_code(str(code).strip())
		if not code or code in seen:
			continue
		seen.add(code)
		codes.append(code)
	if not codes:
		return codes
	# Expand active packs to their components — SI validates those on submit.
	bundle_parents = frappe.get_all(
		"Product Bundle",
		filters={"new_item_code": ("in", codes), "disabled": 0},
		pluck="new_item_code",
		ignore_permissions=True,
	)
	if bundle_parents:
		for row in frappe.get_all(
			"Product Bundle Item",
			filters={"parent": ("in", bundle_parents)},
			fields=["item_code"],
			ignore_permissions=True,
		):
			c = str(row.item_code or "").strip()
			if c and c not in seen:
				seen.add(c)
				codes.append(c)
	return codes


POS_CAJA_ITEM_CODE = "POS-CAJA"


def _normalize_pos_caja_item_code(code: str) -> str:
	"""Map unique cart line codes (POS-CAJA-…) to the shared non-stock Item."""
	c = (code or "").strip()
	if c == POS_CAJA_ITEM_CODE or c.startswith(POS_CAJA_ITEM_CODE + "-"):
		return POS_CAJA_ITEM_CODE
	return c


def _ensure_pos_caja_item() -> None:
	"""Ensure the shared non-stock Item for ad-hoc POS caja lines exists."""
	if frappe.db.exists("Item", POS_CAJA_ITEM_CODE):
		if cint(frappe.db.get_value("Item", POS_CAJA_ITEM_CODE, "disabled")):
			frappe.db.set_value("Item", POS_CAJA_ITEM_CODE, "disabled", 0, update_modified=False)
		if cint(frappe.db.get_value("Item", POS_CAJA_ITEM_CODE, "is_stock_item")):
			frappe.db.set_value("Item", POS_CAJA_ITEM_CODE, "is_stock_item", 0, update_modified=False)
		return

	item_group = (
		frappe.db.get_single_value("Stock Settings", "item_group")
		or (frappe.db.exists("Item Group", "Products") and "Products")
		or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or "All Item Groups"
	)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": POS_CAJA_ITEM_CODE,
			"item_name": "Artículo de caja",
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
			"disabled": 0,
			"description": "Ítem ad-hoc de POS (bolsas, correcciones, no catalogados).",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _resolve_pos_income_account(company: str) -> str | None:
	"""Company default income account, else first non-group Income account."""
	acc = frappe.db.get_value("Company", company, "default_income_account")
	if acc and frappe.db.exists("Account", acc):
		return acc
	# Prefer a typical sales income account if present.
	for name in frappe.get_all(
		"Account",
		filters={
			"company": company,
			"root_type": "Income",
			"is_group": 0,
			"disabled": 0,
		},
		pluck="name",
		order_by="name asc",
		limit=20,
		ignore_permissions=True,
	):
		lower = name.lower()
		if "venta" in lower or "sales" in lower or "income" in lower:
			return name
	return frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Income", "is_group": 0, "disabled": 0},
		"name",
	)


def _ensure_pos_sale_items_enabled(item_codes: list[str]) -> list[str]:
	"""Re-enable Items needed to submit a POS sale that already happened offline.

	ERPNext refuses Sales Invoice lines / packed components when Item.disabled=1.
	Cash was already taken at the register, so we reactivate those SKUs for sync.
	"""
	reactivated = []
	for code in item_codes or []:
		if not code or not frappe.db.exists("Item", code):
			continue
		if not cint(frappe.db.get_value("Item", code, "disabled")):
			continue
		frappe.db.set_value("Item", code, "disabled", 0, update_modified=False)
		reactivated.append(code)
	if reactivated:
		frappe.db.commit()
	return reactivated


def _ensure_pos_sale_item_groups(item_codes: list[str]) -> None:
	"""Create missing Item Groups referenced by sale SKUs.

	Offline POS already took the money; SI submit looks up Item.item_group and
	raises DoesNotExistError (HTTP 404) if the group was deleted.
	"""
	if not item_codes:
		return
	groups = frappe.get_all(
		"Item",
		filters={"name": ("in", item_codes)},
		pluck="item_group",
		ignore_permissions=True,
	)
	needed = sorted({g for g in groups if g})
	if not needed:
		return
	parent = (
		(frappe.db.exists("Item Group", "All Item Groups") and "All Item Groups")
		or frappe.db.get_value("Item Group", {"is_group": 1}, "name")
	)
	created = False
	for name in needed:
		if frappe.db.exists("Item Group", name):
			continue
		if not parent:
			frappe.throw(_("Item Group {0} not found and no parent group exists.").format(name))
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": parent,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		created = True
	if created:
		frappe.db.commit()


def _temporarily_allow_negative_stock():
	"""
	Offline POS must never fail a sale because warehouse qty is short.

	ERPNext checks Stock Settings / Item.allow_negative_stock in both Stock Entry
	validate and SLE posting. Patch the check in-process (request-local) so we do
	not flip the global Stock Settings flag under concurrent traffic.
	"""
	from contextlib import contextmanager

	@contextmanager
	def _ctx():
		from erpnext.stock import stock_ledger
		from erpnext.stock.doctype.stock_entry import stock_entry as stock_entry_mod

		def _always_allow(**_kwargs):
			return True

		orig_sl = stock_ledger.is_negative_stock_allowed
		orig_se = getattr(stock_entry_mod, "is_negative_stock_allowed", None)
		stock_ledger.is_negative_stock_allowed = _always_allow
		if orig_se is not None:
			stock_entry_mod.is_negative_stock_allowed = _always_allow
		try:
			yield
		finally:
			stock_ledger.is_negative_stock_allowed = orig_sl
			if orig_se is not None:
				stock_entry_mod.is_negative_stock_allowed = orig_se

	return _ctx()


def _submit_stock_entry_allowing_negative(stock_entry):
	"""Submit a Stock Entry, forcing allow_negative_stock on SLE write."""
	orig = stock_entry.update_stock_ledger

	def _update_stock_ledger(allow_negative_stock=False):
		return orig(allow_negative_stock=True)

	stock_entry.update_stock_ledger = _update_stock_ledger
	stock_entry.submit()


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
	payments=None,
	cash_received=None,
	pos_session_id=None,
	company=None,
	customer=None,
	is_return=0,
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
		payment_method (str): Mode of payment when `payments` is omitted (Cash / Card / Mobile Money).
		payments (list[dict]): Optional split: [{mode_of_payment|method, amount, cash_received?}].
		cash_received (float): Cash tendered by the customer (for change / audit).
		cashier_id (str): Cashier identifier for audit trail.
		device_id (str): Device/terminal identifier for audit trail.
		branch_id (str): Branch identifier — used to resolve warehouse.
		is_return (int): 1 → credit note (Sales Invoice is_return); skips payment entry.

	Returns:
		dict: {invoice_id, payment_id, payment_ids, offline_order_uuid, status}

	Raises:
		frappe.ValidationError: If items are empty or total does not match.
	"""
	import json

	# Deserialise items if passed as JSON string (frappe.call sends lists as strings)
	if isinstance(items, str):
		items = json.loads(items)

	if not items:
		frappe.throw(_("At least one item is required to create a POS sale."))

	# Dirty clients may send null/"null"/"" — treat as normal sale.
	is_return = 1 if cint(is_return) else 0

	# ── i008: Validate sale_mode and enforce payment method policy ────────────
	sale_mode = (sale_mode or "WHITE").upper()
	if sale_mode not in ("WHITE", "BLACK"):
		frappe.throw(_("Invalid sale_mode: must be WHITE or BLACK."))

	is_borrador = 1 if sale_mode == "BLACK" else 0
	pay_rows = _normalize_pos_payments(payment_method, payments, sale_mode)
	cash_received = flt(cash_received or 0)

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
			"payment_ids": [],
			"offline_order_uuid": offline_order_uuid,
			"status": "already_exists",
		}

	# POS may sell an active pack whose component was later disabled (or a
	# cached inactive SKU). Re-enable so Sales Invoice submit can succeed.
	# Ad-hoc caja lines (POS-CAJA-*) share one non-stock Item.
	_ensure_pos_caja_item()
	for item in items:
		if isinstance(item, dict) and item.get("item_code"):
			item["item_code"] = _normalize_pos_caja_item_code(str(item.get("item_code")))
	required_codes = _pos_sale_required_item_codes(items)
	missing = [c for c in required_codes if not frappe.db.exists("Item", c)]
	if missing:
		frappe.throw(_("Item(s) not found: {0}").format(", ".join(missing)))
	reactivated = _ensure_pos_sale_items_enabled(required_codes)
	_ensure_pos_sale_item_groups(required_codes)

	# ── Resolve defaults ──────────────────────────────────────────────────────
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	session_company = None
	if pos_session_id:
		session_company = frappe.db.get_value("POS Cash Session", pos_session_id, "company")
	warehouse = (
		frappe.db.get_value("Warehouse", branch_id, "name") if branch_id else None
	)
	warehouse_company = (
		frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
	)
	company = (
		(company or "").strip()
		or session_company
		or warehouse_company
		or resolve_company()
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)

	if not warehouse:
		warehouse = frappe.db.get_value(
			"Warehouse", {"is_group": 0, "company": company}, "name"
		)

	pos_customer = None
	cust_override = (customer or "").strip()
	if cust_override:
		if frappe.db.exists("Customer", cust_override):
			pos_customer = cust_override
		else:
			# Allow lookup by customer_name
			pos_customer = frappe.db.get_value(
				"Customer",
				{"customer_name": cust_override, "disabled": 0},
				"name",
			)
			if not pos_customer:
				frappe.throw(_("Customer {0} not found").format(cust_override))
	if not pos_customer:
		pos_customer = _resolve_pos_sale_customer(pos_session_id=pos_session_id, warehouse=warehouse)

	income_account = _resolve_pos_income_account(company)
	if not income_account:
		frappe.throw(
			_(
				"Company {0} has no Default Income Account, and no Income Account "
				"was found for POS sales. Set Company → Default Income Account."
			).format(company)
		)

	pay_summary = " + ".join(p["mode_of_payment"] for p in pay_rows) or (payment_method or "Cash")

	# ── Build Sales Invoice ───────────────────────────────────────────────────
	remarks_tag = (
		f"offline_order_uuid:{offline_order_uuid} | receipt:{receipt_number}"
		f" | cashier:{cashier_id or 'unknown'} | device:{device_id or 'unknown'}"
		f" | branch:{branch_id or 'unknown'}"
		f" | sale_mode:{sale_mode} | is_borrador:{is_borrador}"
		f" | is_return:{is_return}"
		f" | payments:{pay_summary}"
	)
	if cash_received:
		remarks_tag += f" | cash_received:{cash_received}"
	if reactivated:
		remarks_tag += f" | reenabled_items:{','.join(reactivated)}"
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
		attach_session_to_sale_remarks,
	)

	remarks_tag = attach_session_to_sale_remarks(
		remarks_tag, warehouse=warehouse, pos_session_id=pos_session_id
	)

	for item in items:
		_linked_box_pack(item.get("item_code"))

	invoice_items = []
	for item in items:
		qty = flt(item["qty"])
		if is_return:
			qty = -abs(qty) if qty else 0
		invoice_items.append(
			{
				"item_code": item["item_code"],
				"item_name": item.get("item_name", item["item_code"]),
				"qty": qty,
				"rate": flt(item["rate"]),
				"warehouse": warehouse,
				"income_account": income_account,
			}
		)

	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": pos_customer,
			"company": company,
			"is_pos": 0,
			"is_return": is_return,
			"posting_date": nowdate(),
			"due_date": nowdate(),
			"remarks": remarks_tag,
			"items": invoice_items,
		}
	)

	invoice.set_missing_values()
	invoice.calculate_taxes_and_totals()

	# ── Validate grand total matches client expectation (within 1 unit rounding) ──
	# Credit notes have a negative grand_total; client still sends a positive total.
	server_total = abs(flt(invoice.grand_total))
	client_total = abs(flt(total_amount))
	if abs(server_total - client_total) > 1:
		frappe.throw(
			_(
				"Grand total mismatch: server computed {0}, client sent {1}. "
				"Check item rates and taxes."
			).format(invoice.grand_total, total_amount)
		)

	invoice.insert(ignore_permissions=True)
	# Insufficient warehouse qty must not block POS — allow negative stock for
	# invoice submit (if update_stock) and box unit Material Issues below.
	with _temporarily_allow_negative_stock():
		invoice.submit()

		# Box lines are priced as boxes but stock lives on the unit SKU.
		# Skip stock issue on credit notes (return SI already adjusts stock when update_stock).
		if not is_return:
			from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
			from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
				require_valuation_rate,
			)

			allow_zero_valuation = not require_valuation_rate()

			for item in items:
				box = _linked_box_pack(item.get("item_code"))
				if not box or not warehouse:
					continue
				issue_qty = flt(item.get("qty")) * box["pack"]
				if issue_qty <= 0:
					continue
				stock_entry = make_stock_entry(
					item_code=box["unit"],
					qty=issue_qty,
					from_warehouse=warehouse,
					posting_date=nowdate(),
					purpose="Material Issue",
					do_not_save=True,
				)
				if allow_zero_valuation:
					for row in stock_entry.items:
						row.allow_zero_valuation_rate = 1
				stock_entry.insert(ignore_permissions=True)
				_submit_stock_entry_allowing_negative(stock_entry)

		# Soft FIFO: assume oldest Stock Lots are sold first (estimate only).
		if not is_return:
			try:
				from erpnext.erpnext_integrations.ecommerce_api.lots_api import (
					consume_stock_lots_fifo,
				)

				for item in items:
					code = cstr(item.get("item_code") or "").strip()
					qty = flt(item.get("qty") or 0)
					if not code or qty <= 0:
						continue
					box = _linked_box_pack(code)
					if box:
						code = box["unit"]
						qty = qty * box["pack"]
					consume_stock_lots_fifo(code, qty, warehouse=warehouse, commit=False)
			except Exception:
				frappe.log_error(frappe.get_traceback(), "pos soft FIFO lot consume")


	# ── Create Payment Entry (one per split) — skip for credit notes ──────────
	payment_ids = []
	if not is_return:
		payment_ids = _submit_pos_payments(
			invoice, pay_rows, receipt_number, remarks_tag, cash_received=cash_received
		)
	payment_id = payment_ids[0] if payment_ids else ""

	# ── Store payment entry name back on invoice (best-effort) ───────────────
	try:
		frappe.db.set_value("Sales Invoice", invoice.name, "custom_payment_entry", payment_id)
	except Exception:
		pass  # custom_payment_entry field may not exist — non-fatal

	frappe.db.commit()

	return {
		"invoice_id": invoice.name,
		"payment_id": payment_id,
		"payment_ids": payment_ids,
		"offline_order_uuid": offline_order_uuid,
		"sale_mode": sale_mode,
		"is_borrador": is_borrador,
		"is_return": is_return,
		"status": "created",
	}


@frappe.whitelist(allow_guest=True)
def get_pos_sale(offline_order_uuid=None, invoice_id=None):
	"""Look up a POS invoice without creating one. Used to recheck local 'synced' rows."""
	uuid = (offline_order_uuid or "").strip()
	name = (invoice_id or "").strip()
	filters = None
	if uuid:
		filters = {"remarks": ("like", f"%offline_order_uuid:{uuid}%")}
	elif name:
		filters = {"name": name}
	else:
		return {"found": 0, "invoice_id": None, "offline_order_uuid": uuid}

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=["name", "docstatus", "grand_total", "status"],
		ignore_permissions=True,
		limit=1,
	)
	if not rows:
		return {"found": 0, "invoice_id": None, "docstatus": None, "offline_order_uuid": uuid}
	row = rows[0]
	return {
		"found": 1,
		"invoice_id": row.get("name"),
		"docstatus": row.get("docstatus"),
		"grand_total": row.get("grand_total"),
		"status": row.get("status"),
		"offline_order_uuid": uuid,
	}


@frappe.whitelist(allow_guest=True)
def get_stock_entry_status(stock_entry_id=None):
	"""Confirm a receiving Stock Entry still exists on the backend."""
	name = (stock_entry_id or "").strip()
	if not name:
		return {"found": 0, "stock_entry_id": None}
	rows = frappe.get_all(
		"Stock Entry",
		filters={"name": name},
		fields=["name", "docstatus"],
		ignore_permissions=True,
		limit=1,
	)
	if not rows:
		return {"found": 0, "stock_entry_id": name, "docstatus": None}
	row = rows[0]
	return {
		"found": 1,
		"stock_entry_id": row.get("name"),
		"docstatus": row.get("docstatus"),
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


def _linked_box_pack(item_code):
	"""Box SKU linked to another unit with pack > 1. Boxes do not hold stock."""
	code = (item_code or "").strip()
	if not code or not frappe.db.exists("Item", code):
		return None
	unit = ""
	pack = 0
	if frappe.db.has_column("Item", "custom_unit_sku"):
		unit = (frappe.db.get_value("Item", code, "custom_unit_sku") or "").strip()
	if frappe.db.has_column("Item", "custom_pack_qty"):
		pack = flt(frappe.db.get_value("Item", code, "custom_pack_qty"))
	if not unit or unit == code or pack <= 1 or not frappe.db.exists("Item", unit):
		return None
	if cint(frappe.db.get_value("Item", code, "is_stock_item")):
		frappe.db.set_value("Item", code, "is_stock_item", 0)
	return {"unit": unit, "pack": pack}


@frappe.whitelist(allow_guest=True)
def commit_receiving_session(
	session_id,
	reference,
	supplier,
	warehouse,
	lines,
	draft_items,
	purchase_order=None,
):
	"""
	Atomically:
	1. Create new SilkOS Items for draft items (disabled/inactive until Review
	   approves them — unless draft already has approved_at)
	2. If no Purchase Order linked, auto-create a Compra (PO) from the lines
	3. Create a submitted Stock Entry (Material Receipt)
	4. Create Stock Lot rows (logical lots) per line for Tables → Lotes
	Returns { stock_entry_id, new_item_codes, purchase_order, lot_names }
	"""
	import json
	if isinstance(lines, str):
		try:
			lines = json.loads(lines)
		except Exception:
			lines = []
	if lines is None or lines == "" or (isinstance(lines, str) and lines.strip().lower() in ("null", "undefined", "none")):
		lines = []
	if not isinstance(lines, list):
		lines = []
	if isinstance(draft_items, str):
		try:
			draft_items = json.loads(draft_items)
		except Exception:
			draft_items = []
	if draft_items is None or draft_items == "" or (
		isinstance(draft_items, str) and draft_items.strip().lower() in ("null", "undefined", "none")
	):
		draft_items = []
	if not isinstance(draft_items, list):
		draft_items = []

	purchase_order = cstr(purchase_order or "").strip()
	if purchase_order.lower() in ("null", "undefined", "none"):
		purchase_order = ""
	if purchase_order and not frappe.db.exists("Purchase Order", purchase_order):
		purchase_order = ""


	def _upsert_receiving_item_price(item_code: str, rate: float) -> None:
		pl = (
			frappe.db.get_single_value("Selling Settings", "selling_price_list")
			or "Standard Selling"
		)
		existing = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": pl, "selling": 1},
			"name",
		)
		if existing:
			frappe.db.set_value("Item Price", existing, "price_list_rate", flt(rate))
		else:
			frappe.get_doc({
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": pl,
				"price_list_rate": flt(rate),
				"selling": 1,
			}).insert(ignore_permissions=True)

	def _upsert_item_default_supplier(item_code: str, supplier_name: str) -> None:
		from erpnext.erpnext_integrations.ecommerce_api.api import _get_or_create_named_supplier

		resolved = _get_or_create_named_supplier(cstr(supplier_name or "").strip() or "Uncategorized")
		company = frappe.db.get_single_value("Global Defaults", "default_company")
		defaults = frappe.get_all(
			"Item Default",
			filters={"parent": item_code},
			fields=["name", "default_supplier", "company"],
			limit=1,
		)
		if defaults:
			frappe.db.set_value("Item Default", defaults[0].name, "default_supplier", resolved)
		else:
			item_doc = frappe.get_doc("Item", item_code)
			row = {"default_supplier": resolved}
			if company:
				row["company"] = company
			item_doc.append("item_defaults", row)
			item_doc.save(ignore_permissions=True)

	# 1. Create draft items (inactive until Review marks them approved)
	import uuid as _uuid
	new_item_codes = {}
	for d in draft_items:
		# Existing product marked for review — keep active, store note only
		if d.get("needs_review") and d.get("item_code") and frappe.db.exists("Item", d.get("item_code")):
			new_item_codes[d["draft_id"]] = d["item_code"]
			note = d.get("review_note") or d.get("review_notes")
			if note and frappe.db.has_column("Item", "custom_review_notes"):
				frappe.db.set_value(
					"Item",
					d["item_code"],
					"custom_review_notes",
					f"review: {note}" if not str(note).startswith("review:") else note,
				)
			continue
		if frappe.db.exists("Item", d.get("item_code") or ""):
			new_item_codes[d["draft_id"]] = d["item_code"]
			continue
		# item_code is mandatory in SilkOS — generate a unique one if not provided
		item_code_val = (d.get("item_code") or "").strip() or str(_uuid.uuid4())
		# Already approved in IndexedDB before commit → create active; otherwise disabled
		is_approved = bool(d.get("approved_at"))
		brand_name = (d.get("brand") or "").strip()
		if brand_name and not frappe.db.exists("Brand", brand_name):
			frappe.get_doc({"doctype": "Brand", "brand": brand_name}).insert(ignore_permissions=True)
		item_group = (d.get("item_group") or "Products").strip() or "Products"
		if item_group and not frappe.db.exists("Item Group", item_group):
			parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": item_group,
					"parent_item_group": parent,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		stock_uom = (d.get("stock_uom") or "Nos").strip() or "Nos"
		if stock_uom and not frappe.db.exists("UOM", stock_uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": stock_uom}).insert(ignore_permissions=True)
		item_fields = {
			"doctype": "Item",
			"item_code": item_code_val,
			"item_name": d["item_name"],
			"item_group": item_group,
			"stock_uom": stock_uom,
			"is_stock_item": 1,
			"include_item_in_manufacturing": 0,
			"description": d.get("normalized_title") or d["item_name"],
			"disabled": 0 if is_approved else 1,
		}
		if brand_name:
			item_fields["brand"] = brand_name
		if frappe.db.has_column("Item", "custom_pack_qty") and d.get("pack_qty") is not None:
			item_fields["custom_pack_qty"] = flt(d.get("pack_qty"))
		if frappe.db.has_column("Item", "custom_pack_size") and d.get("pack_size") is not None:
			item_fields["custom_pack_size"] = flt(d.get("pack_size"))
		if frappe.db.has_column("Item", "custom_pack_unit") and d.get("unit"):
			item_fields["custom_pack_unit"] = d.get("unit")
		if frappe.db.has_column("Item", "custom_normalized_title") and d.get("normalized_title"):
			item_fields["custom_normalized_title"] = d.get("normalized_title")
		if frappe.db.has_column("Item", "custom_review_notes") and d.get("review_notes"):
			item_fields["custom_review_notes"] = d.get("review_notes")
		if frappe.db.has_column("Item", "custom_unit_sku") and d.get("unit_sku"):
			item_fields["custom_unit_sku"] = d.get("unit_sku")
		# Sell-by / plazo comercial (days from receive) — native shelf_life_in_days.
		sell_by = d.get("sell_by_days")
		if sell_by in (None, "", "null", "undefined") and d.get("shelf_life_in_days") not in (None, ""):
			sell_by = d.get("shelf_life_in_days")
		if sell_by not in (None, "", "null", "undefined"):
			try:
				item_fields["shelf_life_in_days"] = max(0, cint(sell_by))
			except (TypeError, ValueError):
				pass
		for wk, fk in (
			("weight_per_unit", "weight_per_unit"),
			("unit_weight_min", "custom_unit_weight_min"),
			("unit_weight_max", "custom_unit_weight_max"),
		):
			if d.get(wk) in (None, "", "null", "undefined"):
				continue
			if fk.startswith("custom_") and not frappe.db.has_column("Item", fk):
				continue
			try:
				item_fields[fk] = max(0.0, flt(d.get(wk)))
			except (TypeError, ValueError):
				pass
		if d.get("image"):
			item_fields["image"] = d.get("image")
		item_doc = frappe.get_doc(item_fields)
		item_doc.insert(ignore_permissions=True)
		# Add barcode if provided — skip silently if SilkOS rejects the format
		if d.get("barcode"):
			try:
				item_doc.append("barcodes", {"barcode": d["barcode"], "barcode_type": "EAN"})
				item_doc.save(ignore_permissions=True)
			except Exception:
				pass  # barcode is optional; don't abort the commit over a format error
		# Add selling price if estimated / list
		price = flt(d.get("list_price") or d.get("estimated_price") or 0)
		if price > 0:
			_upsert_receiving_item_price(item_doc.item_code, price)
		est_cost = flt(d.get("estimated_cost") or 0)
		if est_cost > 0:
			from erpnext.erpnext_integrations.ecommerce_api.product_manager import (
				_upsert_item_price_buying,
			)
			_upsert_item_price_buying(item_doc.item_code, est_cost)
		tags = d.get("tags") or []
		if isinstance(tags, list) and tags:
			tag_str = ",".join(sorted({str(t).strip() for t in tags if t and str(t).strip()}))
			if tag_str:
				frappe.db.set_value("Item", item_doc.name, "_user_tags", tag_str)
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
		if flt(line.get("qty") or 0) <= 0:
			continue
		basic_rate = flt(line.get("unit_cost") or 0)
		qty = flt(line.get("qty") or 0)
		# A scanned box never receives its own stock — credit the unit SKU by pack size.
		box = _linked_box_pack(item_code)
		if box:
			qty = qty * box["pack"]
			if basic_rate > 0:
				basic_rate = basic_rate / box["pack"]
			item_code = box["unit"]
		# unit_cost 0 / empty → do not force valuation overwrite (allow current valuation)
		resolved_lines.append({
			"item_code": item_code,
			"qty": qty,
			"basic_rate": basic_rate,
			"t_warehouse": warehouse,
			"allow_zero_valuation_rate": 1 if basic_rate <= 0 else 0,
		})

		# Overwrite selling price only when line carries an explicit price > 0
		sell_price = flt(line.get("list_price") or 0)
		if sell_price > 0:
			_upsert_receiving_item_price(item_code, sell_price)
		if cint(line.get("assign_buying_cost") or line.get("cost_edited")) and basic_rate > 0:
			from erpnext.erpnext_integrations.ecommerce_api.product_manager import (
				_upsert_item_price_buying,
			)
			_upsert_item_price_buying(item_code, basic_rate)

		# Persist sell-by days (plazo comercial) from recepción — days since receive, not a date.
		# Only write when the line sends a numeric value (0 clears). Missing/dirty strings skip.
		raw = line.get("sell_by_days")
		if raw in (None, "") and "shelf_life_in_days" in line:
			raw = line.get("shelf_life_in_days")
		if isinstance(raw, str) and raw.strip().lower() in ("", "null", "undefined", "none"):
			raw = None
		if raw is not None:
			try:
				days = max(0, cint(raw))
			except (TypeError, ValueError):
				days = None
			if days is not None:
				frappe.db.set_value("Item", item_code, "shelf_life_in_days", days)

	if not resolved_lines:
		frappe.throw("No valid lines to receive")

	# Auto-create Compra (Purchase Order) when recepción has no linked OC —
	# assume the buyer forgot to register the compra; autofill from session lines.
	po_created = False
	if not purchase_order:
		try:
			from erpnext.erpnext_integrations.ecommerce_api.buying_api import create_purchase_order

			po_items = []
			for line in lines:
				code = line.get("item_code")
				if not code and line.get("draft_item_id"):
					code = new_item_codes.get(line["draft_item_id"])
				if not code:
					continue
				qty = flt(line.get("qty") or 0)
				if qty <= 0:
					continue
				box = _linked_box_pack(code)
				rate = flt(line.get("unit_cost") or 0)
				if box:
					# PO should list the unit SKU at unit qty/cost (same as stock credit).
					qty = qty * box["pack"]
					if rate > 0:
						rate = rate / box["pack"]
					code = box["unit"]
				po_items.append(
					{
						"item_code": code,
						"qty": qty,
						"rate": rate,
						"cost_edited": 1 if cint(line.get("cost_edited")) else 0,
					}
				)
			if po_items:
				po_res = create_purchase_order(
					supplier=supplier or "Uncategorized",
					schedule_date=nowdate(),
					items=po_items,
					submit=1,
					notes=(
						f"Auto from recepción {cstr(session_id or '').strip()}"
						+ (f" — ref: {reference}" if reference else "")
					),
					client_request_id=f"recv-auto-po:{cstr(session_id or '').strip()}"[:140] or None,
				)
				purchase_order = cstr((po_res or {}).get("name") or "").strip()
				po_created = bool(purchase_order)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "receiving auto-create PO")
			purchase_order = purchase_order or ""

	# Optional: overwrite Item Default supplier when session supplier is set
	supplier_name = (supplier or "").strip()
	if supplier_name:
		for line in lines:
			code = line.get("item_code")
			if not code and line.get("draft_item_id"):
				code = new_item_codes.get(line["draft_item_id"])
			if not code:
				continue
			_upsert_item_default_supplier(code, supplier_name)

	# Draft/new items are often created disabled until Review. ERPNext rejects
	# inactive Items on Stock Entry — temporarily enable for Material Receipt,
	# then restore so POS still hides them until approved.
	receipt_item_codes = list({row["item_code"] for row in resolved_lines if row.get("item_code")})
	reactivated = []
	for code in receipt_item_codes:
		if cint(frappe.db.get_value("Item", code, "disabled")):
			frappe.db.set_value("Item", code, "disabled", 0)
			reactivated.append(code)
	if reactivated:
		frappe.db.commit()

	# 3. Create Stock Entry
	se = None
	try:
		se = frappe.get_doc({
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"posting_date": nowdate(),
			"to_warehouse": warehouse,
			"items": resolved_lines,
			"remarks": f"Receiving session {session_id}"
			+ (f" — ref: {reference}" if reference else "")
			+ (f" — PO: {purchase_order}" if purchase_order else ""),
		})
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
	finally:
		for code in reactivated:
			# Only restore if still present and we flipped it for this receipt
			if frappe.db.exists("Item", code):
				frappe.db.set_value("Item", code, "disabled", 1)
		if reactivated:
			frappe.db.commit()

	# 4. Logical Stock Lots (Tables → Lotes) — one per received line.
	lot_names = []
	try:
		from erpnext.erpnext_integrations.ecommerce_api.lots_api import create_stock_lot

		company = frappe.db.get_single_value("Global Defaults", "default_company")
		for line in lines:
			code = line.get("item_code")
			if not code and line.get("draft_item_id"):
				code = new_item_codes.get(line["draft_item_id"])
			if not code:
				continue
			qty = flt(line.get("qty") or 0)
			if qty <= 0:
				continue
			basic_rate = flt(line.get("unit_cost") or 0)
			box = _linked_box_pack(code)
			if box:
				qty = qty * box["pack"]
				if basic_rate > 0:
					basic_rate = basic_rate / box["pack"]
				code = box["unit"]
			sell_by = line.get("sell_by_days")
			if sell_by in (None, "") and "shelf_life_in_days" in line:
				sell_by = line.get("shelf_life_in_days")
			# Prefer line sell_by; else Item master plazo comercial.
			if sell_by in (None, "", "null", "undefined"):
				sell_by = frappe.db.get_value("Item", code, "shelf_life_in_days")
			try:
				lot_name = create_stock_lot(
					item_code=code,
					qty=qty,
					warehouse=warehouse,
					receive_date=nowdate(),
					sell_by_days=sell_by,
					unit_cost=basic_rate,
					stock_entry=se.name if se else None,
					purchase_order=purchase_order or None,
					supplier=supplier_name or None,
					reference=reference,
					session_id=session_id,
					company=company,
					commit=False,
				)
				lot_names.append(lot_name)
			except Exception:
				frappe.log_error(frappe.get_traceback(), "receiving create stock lot")
		if lot_names:
			frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "receiving stock lots")

	return {
		"stock_entry_id": se.name if se else None,
		"new_item_codes": new_item_codes,
		"purchase_order": purchase_order or None,
		"purchase_order_created": 1 if po_created else 0,
		"lot_names": lot_names,
	}


@frappe.whitelist(allow_guest=True)
def simulate_receiving_flow():
	"""Return a deterministic sample payload for testing the receiving screen."""
	# Prefer simple stock items (no box/pack redirection) so Material Receipt is stable.
	filters = {"disabled": 0, "is_stock_item": 1}
	existing = frappe.get_all(
		"Item",
		filters=filters,
		fields=["item_code", "item_name", "stock_uom", "image"],
		limit=20,
	)
	simple = []
	for e in existing:
		if _linked_box_pack(e["item_code"]):
			continue
		simple.append(e)
		if len(simple) >= 3:
			break
	existing = simple
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	warehouse = frappe.db.get_value(
		"Warehouse", {"is_group": 0, "company": company, "disabled": 0}, "name", order_by="name asc"
	)
	# Prefer a Stores warehouse when present
	stores = frappe.get_all(
		"Warehouse",
		filters={"is_group": 0, "company": company, "disabled": 0, "name": ("like", "%Stores%")},
		pluck="name",
		limit=1,
	)
	if stores:
		warehouse = stores[0]
	if not warehouse:
		warehouse = "Stores - L"
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
		"warehouse": warehouse,
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


def _parse_airtable_catalog_csv(csv_text):
	"""Parse an Airtable "Productos" export (real header row, named columns).

	Produces the same row shape as _parse_catalog_csv, plus optional keys:
	- cash_price (Efectivo) — main catalog / POS price
	- price (Transferencia) — payment-method alternate only
	- image_url (Imagen)
	- brand (Marca)
	- disabled from Estado (Agotado → 1, En Stock → 0)
	- parent_item_group (Clase) when Etiquetas is the leaf category
	- offer_rate (Oferta) + offer_qty (Cantidad, e.g. x2 / xCaja) → Pricing Rule

	Tree: Clase → parent Item Group (is_group=1), Etiquetas → leaf (item_group).
	If Etiquetas is empty, Clase is used as a flat leaf (legacy behaviour).
	Title is always Producto (never Clase / Etiquetas).
	"""
	column_map = {
		"item_code": "TAG",
		"item_name": "Producto",
		"item_group": "Etiquetas",
		"parent_item_group": "Clase",
		"brand": "Marca",
		"status": "Estado",
		"price": "Transferencia",
		"cash_price": "Efectivo",
		"offer_rate": "Oferta",
		"offer_qty": "Cantidad",
		"image_url": "Imagen",
		"stock_uom": "UOM",
	}
	parsed, total, _headers = _parse_mapped_catalog_csv(csv_text, column_map)
	for row in parsed:
		# Title must remain Producto — never swap with Clase/Etiquetas.
		producto = cstr(row.get("item_name") or "").strip()
		etiqueta = cstr(row.get("item_group") or "").strip()
		clase = cstr(row.get("parent_item_group") or "").strip()
		if producto:
			row["item_name"] = producto
			row["title_simplified"] = producto

		if etiqueta and clase and etiqueta != clase:
			row["item_group"] = etiqueta
			row["parent_item_group"] = clase
		elif clase:
			row["item_group"] = clase
			row["parent_item_group"] = ""
		elif etiqueta:
			row["item_group"] = etiqueta
			row["parent_item_group"] = ""
		else:
			row["item_group"] = row.get("item_group") or "Products"
			row["parent_item_group"] = ""

		row["disabled"] = _airtable_estado_to_disabled(row.pop("status", None))
		# Airtable catalog is weighed stock: missing/blank UOM → WEIGHT
		# (export always emits UOM=WEIGHT; older CSVs without the column still import correctly).
		if not cstr(row.get("stock_uom") or "").strip():
			row["stock_uom"] = "WEIGHT"
	return parsed, total


_AIRTABLE_DISABLED_STATUSES = {
	"agotado",
	"out of stock",
	"out-of-stock",
	"sin stock",
	"disabled",
	"inactivo",
	"inactive",
	"no",
	"0",
	"false",
}


def _airtable_estado_to_disabled(estado) -> int:
	"""Map Airtable Estado (En Stock / Agotado / …) → Item.disabled."""
	raw = cstr(estado or "").strip().lower()
	if not raw:
		return 0
	if raw in _AIRTABLE_DISABLED_STATUSES:
		return 1
	# Explicit in-stock / active
	if raw in ("en stock", "in stock", "disponible", "active", "activo", "yes", "1", "true"):
		return 0
	# Unknown → keep enabled (permissive)
	return 0


def _airtable_oferta_rule_name(item_code: str) -> str:
	"""Stable Pricing Rule name for Airtable Oferta rows (re-import upserts)."""
	code = cstr(item_code or "").strip()
	return f"AT-{code}"[:140]


# Catalog CSV Oferta styles:
# - threshold (default): once qty >= N, all units at Oferta (x2 + x10 → pick best tier)
# - pack: complete packs of N at Oferta; remainder at list (buy 8, N=3 → 2×3 oferta + 2 list)
_CATALOG_PROMO_STYLES = ("pack", "threshold")
_PROMO_STYLE_MARKER_RE = re.compile(r"\[promo_style=(pack|threshold)\]", re.I)
# Airtable "xCaja" default box size; override via Item.custom_pack_qty or edit Pricing Rule.min_qty.
_AIRTABLE_XCAJA_DEFAULT_QTY = 4.0


def _normalize_catalog_promo_style(promo_style) -> str:
	raw = cstr(promo_style or "").strip().lower()
	if raw in ("pack", "packs", "nx", "nxm"):
		return "pack"
	if raw in ("threshold", "desde", "min_qty", "all_units", "fixed"):
		return "threshold"
	# Default (and anything dirty/empty) → threshold (>=N all units at Oferta)
	return "threshold"


def _promo_style_from_rule(rule) -> str:
	"""Read style marker from Pricing Rule.rule_description; legacy → threshold."""
	desc = cstr((rule or {}).get("rule_description") or "")
	m = _PROMO_STYLE_MARKER_RE.search(desc)
	if m:
		return m.group(1).lower()
	# Pre-marker Airtable Rate rules behaved as threshold (all units once min_qty met).
	return "threshold"


def _parse_airtable_offer_min_qty(cantidad, item_code=None) -> float:
	"""Cantidad labels → Pricing Rule.min_qty (x2→2, xCaja→4 or Item pack, xc/unidad→1)."""
	raw = cstr(cantidad or "").strip().lower().replace(" ", "")
	if not raw:
		return 1.0
	if raw in ("xc/unidad", "xunidad", "xcunidad", "x1", "1"):
		return 1.0
	m = re.match(r"^x?(\d+(?:\.\d+)?)$", raw)
	if m:
		return flt(m.group(1)) or 1.0
	if raw in ("xcaja", "caja", "xbox", "box"):
		pack = 0.0
		if item_code and frappe.db.exists("Item", item_code) and frappe.db.has_column(
			"Item", "custom_pack_qty"
		):
			pack = flt(frappe.db.get_value("Item", item_code, "custom_pack_qty") or 0)
		# Default caja = 4; use Item pack when already set (>1); editable later on the Oferta rule.
		return pack if pack > 1 else _AIRTABLE_XCAJA_DEFAULT_QTY
	return 1.0


def _rate_rule_discount(unit_rate, offer, qty, min_qty, promo_style) -> float:
	"""Savings for a Price/Rate rule under pack or threshold style."""
	unit_rate = flt(unit_rate)
	offer = flt(offer)
	qty = flt(qty)
	min_qty = flt(min_qty)
	style = cstr(promo_style or "").strip().lower()
	if style not in _CATALOG_PROMO_STYLES:
		style = "threshold"
	if offer <= 0 or unit_rate <= offer or qty <= 0:
		return 0.0
	if style == "pack":
		if min_qty <= 0:
			return 0.0
		packs = int(qty // min_qty)
		if packs < 1:
			return 0.0
		return (unit_rate - offer) * packs * min_qty
	# threshold: all units at offer once qty meets min_qty
	if min_qty > 0 and qty < min_qty:
		return 0.0
	return (unit_rate - offer) * qty


def _upsert_airtable_oferta_promotion(item_code, item_name, offer_rate, offer_qty, promo_style="threshold"):
	"""Create/update/disable Pricing Rule from Airtable Oferta + Cantidad.

	promo_style:
	- threshold (default): once qty >= N, every unit at Oferta (x2 / x10 tiers stack via best rule)
	- pack: N units at Oferta per complete pack; remainder list price

	Stored as Price + Rate with [promo_style=…] in rule_description for cart/catalog.
	Returns: created | updated | disabled | skipped
	"""
	code = cstr(item_code or "").strip()
	if not code:
		return "skipped"
	rule_name = _airtable_oferta_rule_name(code)
	rate = flt(offer_rate)
	exists = bool(frappe.db.exists("Pricing Rule", rule_name))
	style = _normalize_catalog_promo_style(promo_style)

	if rate <= 0:
		if exists:
			frappe.db.set_value("Pricing Rule", rule_name, "disable", 1, update_modified=True)
			return "disabled"
		return "skipped"

	min_qty = _parse_airtable_offer_min_qty(offer_qty, code)
	qty_label = cstr(offer_qty or "").strip() or "x1"
	if style == "pack" and min_qty > 1:
		title = f"Oferta {cint(min_qty)}x — {(cstr(item_name) or code)[:90]}"
	elif style == "threshold" and min_qty > 1:
		title = f"Oferta desde {qty_label} — {(cstr(item_name) or code)[:90]}"
	else:
		title = f"Oferta {qty_label} — {(cstr(item_name) or code)[:90]}"
	currency, company = _default_pricing_currency()

	if exists:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Pricing Rule", rule_name)
		frappe.flags.ignore_permissions = False
		action = "updated"
	else:
		doc = frappe.new_doc("Pricing Rule")
		action = "created"

	doc.title = title
	doc.selling = 1
	doc.buying = 0
	doc.disable = 0
	doc.apply_on = "Item Code"
	doc.price_or_product_discount = "Price"
	doc.rate_or_discount = "Rate"
	doc.rate = rate
	doc.discount_percentage = 0
	doc.discount_amount = 0
	doc.min_qty = min_qty
	doc.max_qty = 0
	doc.min_amt = 0
	doc.max_amt = 0
	doc.for_price_list = ""
	doc.currency = currency
	if company:
		doc.company = company
	doc.rule_description = (
		f"Airtable Oferta {qty_label} @ {rate:g} (min_qty={min_qty:g}) [promo_style={style}]"
	)
	doc.threshold_percentage = 80
	doc.set("items", [])
	doc.set("item_groups", [])
	doc.set("brands", [])
	doc.append("items", {"item_code": code})

	if action == "created":
		doc.insert(ignore_permissions=True, set_name=rule_name)
	else:
		doc.save(ignore_permissions=True)
	return action


# Logical fields the custom mapper can bind to a CSV header.
CUSTOM_CATALOG_MAP_FIELDS = (
	"item_code",
	"item_name",
	"item_group",
	"parent_item_group",
	"brand",
	"status",
	"barcode",
	"stock_uom",
	"price",
	"cash_price",
	"offer_rate",
	"offer_qty",
	"image_url",
)

# Case-insensitive header aliases used to auto-guess a custom map.
_CUSTOM_FIELD_ALIASES = {
	"item_code": ("tag", "sku", "item_code", "item code", "codigo", "código", "code", "item"),
	"item_name": ("producto", "item_name", "item name", "name", "title", "nombre", "description"),
	"item_group": (
		"etiquetas",
		"etiqueta",
		"item_group",
		"item group",
		"category",
		"categoria",
		"categoría",
		"subcategory",
		"subcategoria",
		"subcategoría",
		"group",
		"grupo",
	),
	"parent_item_group": (
		"clase",
		"parent_item_group",
		"parent item group",
		"parent_group",
		"parent group",
		"parent category",
		"categoria padre",
		"categoría padre",
	),
	"brand": ("marca", "brand", "brand_name", "brand name"),
	"status": ("estado", "status", "stock status", "availability", "disponibilidad"),
	"barcode": ("barcode", "ean", "upc", "codigo_barras", "código de barras", "barras"),
	"stock_uom": ("uom", "stock_uom", "unidad", "unit", "um", "weight", "peso"),
	# Catalog / POS list only — never alias Transferencia here (payment-method list).
	"price": ("price", "precio", "rate", "standard selling", "precio lista", "selling price"),
	"cash_price": ("efectivo", "cash", "cash_price", "precio efectivo", "precio_efectivo"),
	"offer_rate": ("oferta", "offer", "offer_rate", "promo_rate", "promo price", "precio oferta"),
	"offer_qty": ("cantidad", "offer_qty", "promo_qty", "min_qty", "x", "cantidad oferta"),
	"image_url": ("imagen", "image", "image_url", "foto", "photo", "url imagen"),
}


def _normalize_column_map(column_map):
	"""Accept dict or JSON string; keep only known fields with non-empty headers."""
	if column_map in (None, "", {}):
		return {}
	if isinstance(column_map, str):
		try:
			column_map = frappe.parse_json(column_map) or {}
		except Exception:
			return {}
	if not isinstance(column_map, dict):
		return {}
	out = {}
	for key in CUSTOM_CATALOG_MAP_FIELDS:
		raw = column_map.get(key)
		if raw in (None, ""):
			continue
		header = cstr(raw).strip()
		if header:
			out[key] = header
	return out


def _csv_header_row(csv_text):
	"""Return the first non-empty CSV header row as a list of fieldnames."""
	if not csv_text:
		return []
	reader = csv.reader(io.StringIO(csv_text))
	for row in reader:
		if any((cell or "").strip() for cell in row):
			return [(cell or "").strip() for cell in row]
	return []


def _guess_column_map(headers):
	"""Best-effort map from header names → logical fields (first match wins)."""
	if not headers:
		return {}
	by_lower = {}
	for h in headers:
		key = (h or "").strip().lower()
		if key and key not in by_lower:
			by_lower[key] = h
	guessed = {}
	for field, aliases in _CUSTOM_FIELD_ALIASES.items():
		for alias in aliases:
			hit = by_lower.get(alias)
			if hit:
				guessed[field] = hit
				break
	return guessed


def _dict_row_get(row, header):
	"""Lookup a DictReader cell by header, tolerating BOM / stray spaces."""
	if not header:
		return ""
	if header in row:
		return row.get(header) or ""
	want = header.strip().lower().lstrip("\ufeff")
	for key, val in row.items():
		if (key or "").strip().lower().lstrip("\ufeff") == want:
			return val or ""
	return ""


def _parse_mapped_catalog_csv(csv_text, column_map):
	"""Parse a headered CSV using an explicit column_map of logical→header names."""
	column_map = _normalize_column_map(column_map)
	if not csv_text:
		return [], 0, []

	headers = _csv_header_row(csv_text)
	reader = csv.DictReader(io.StringIO(csv_text))
	data_rows = list(reader)
	parsed = []

	for line_no, row in enumerate(data_rows, start=2):
		if not any((v or "").strip() for v in (row or {}).values()):
			continue

		item_code = cstr(_dict_row_get(row, column_map.get("item_code"))).strip()
		item_name = cstr(_dict_row_get(row, column_map.get("item_name"))).strip()
		item_group = cstr(_dict_row_get(row, column_map.get("item_group"))).strip()
		parent_item_group = (
			cstr(_dict_row_get(row, column_map.get("parent_item_group"))).strip()
			if column_map.get("parent_item_group")
			else ""
		)
		if not item_group:
			item_group = parent_item_group or "Products"
			if parent_item_group and item_group == parent_item_group:
				parent_item_group = ""
		brand = (
			cstr(_dict_row_get(row, column_map.get("brand"))).strip()
			if column_map.get("brand")
			else ""
		)
		status = (
			cstr(_dict_row_get(row, column_map.get("status"))).strip()
			if column_map.get("status")
			else ""
		)
		# Estado / status → Item.disabled (Agotado hide in catalog; En Stock show).
		disabled = None
		if column_map.get("status"):
			disabled = _airtable_estado_to_disabled(status)
		barcode = cstr(_dict_row_get(row, column_map.get("barcode"))).strip()
		stock_uom = cstr(_dict_row_get(row, column_map.get("stock_uom"))).strip()
		price = _safe_float(_dict_row_get(row, column_map.get("price"))) if column_map.get("price") else 0.0
		cash_price = (
			_safe_float(_dict_row_get(row, column_map.get("cash_price")))
			if column_map.get("cash_price")
			else 0.0
		)
		offer_rate = (
			_safe_float(_dict_row_get(row, column_map.get("offer_rate")))
			if column_map.get("offer_rate")
			else 0.0
		)
		offer_qty = (
			cstr(_dict_row_get(row, column_map.get("offer_qty"))).strip()
			if column_map.get("offer_qty")
			else ""
		)
		image_url = cstr(_dict_row_get(row, column_map.get("image_url"))).strip()

		errors = []
		if not column_map.get("item_code"):
			errors.append("Map item_code to a CSV column.")
		elif not item_code:
			errors.append(f"Missing item_code ({column_map.get('item_code')} column).")
		if not column_map.get("item_name"):
			errors.append("Map item_name to a CSV column.")
		elif not item_name:
			errors.append(f"Missing item_name ({column_map.get('item_name')} column).")

		parsed_row = {
			"line_no": line_no,
			"item_code": item_code,
			"item_name": item_name or item_code,
			"title_simplified": item_name or item_code,
			"barcode": barcode,
			"stock_uom": stock_uom,
			"item_group": item_group,
			"parent_item_group": parent_item_group,
			"brand": brand,
			"status": status,
			"price": price,
			"cash_price": cash_price,
			"offer_rate": offer_rate,
			"offer_qty": offer_qty,
			"image_url": image_url,
			"last_price": 0.0,
			"stock_hint": 0.0,
			"errors": errors,
		}
		if disabled is not None:
			parsed_row["disabled"] = disabled
		parsed.append(parsed_row)

	return parsed, len(data_rows), headers


def _parse_custom_catalog_csv(csv_text, column_map=None):
	"""Custom format: real header row + caller-supplied column_map."""
	column_map = _normalize_column_map(column_map)
	headers = _csv_header_row(csv_text)
	if not column_map:
		column_map = _guess_column_map(headers)
	parsed, total, headers = _parse_mapped_catalog_csv(csv_text, column_map)
	return parsed, total, headers, column_map


def _ensure_price_list(price_list_name):
	"""Create a selling Price List if it doesn't already exist (idempotent)."""
	if not price_list_name or frappe.db.exists("Price List", price_list_name):
		return
	frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": price_list_name,
			"enabled": 1,
			"selling": 1,
			"currency": frappe.defaults.get_global_default("currency") or "ARS",
		}
	).insert(ignore_permissions=True)


def _upsert_item_price(item_code, price_list, rate):
	"""Create or update a selling Item Price row. Returns True if written."""
	rate = flt(rate)
	if rate <= 0 or not item_code or not price_list:
		return False
	price_name = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list, "selling": 1},
		"name",
	)
	if price_name:
		frappe.db.set_value("Item Price", price_name, "price_list_rate", rate)
	else:
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": rate,
				"selling": 1,
			}
		).insert(ignore_permissions=True)
	return True


def _is_remote_image_url(url):
	u = cstr(url or "").strip()
	return u.startswith("http://") or u.startswith("https://")


_CATALOG_IMAGE_MODES = ("blank", "all", "none")


def _normalize_catalog_image_mode(image_mode):
	"""CSV import image policy: blank | all | none (aliases accepted)."""
	raw = cstr(image_mode or "").strip().lower()
	if not raw:
		return "blank"
	aliases = {
		"override": "all",
		"overwrite": "all",
		"replace": "all",
		"always": "all",
		"empty": "blank",
		"empty_only": "blank",
		"blank_only": "blank",
		"if_blank": "blank",
		"if_empty": "blank",
		"skip": "none",
		"off": "none",
		"never": "none",
		"0": "none",
		"false": "none",
	}
	raw = aliases.get(raw, raw)
	return raw if raw in _CATALOG_IMAGE_MODES else "blank"


def _should_apply_catalog_image(current_image, incoming_url, image_mode="blank"):
	"""Whether CSV Imagen / image_url should write Item.image.

	Modes:
	- blank: only when the item currently has no image
	- all: always override when CSV provides a URL
	- none: never apply CSV images
	"""
	incoming = cstr(incoming_url or "").strip()
	if not incoming:
		return False
	mode = _normalize_catalog_image_mode(image_mode)
	if mode == "none":
		return False
	if mode == "all":
		return True
	return not cstr(current_image or "").strip()


def _materialize_catalog_import_image(item_code, image_url):
	"""
	Download a remote (or data:) image into a local 256×256 PNG thumb
	(transparent pad, alpha preserved) for catalog CSV migration.

	Returns (local_file_url, None) on success, or (None, error_message) on failure.
	Does not raise — callers treat broken Airtable/CDN links as warnings.
	"""
	url = cstr(image_url or "").strip()
	if not item_code or not url:
		return None, "empty image url"

	try:
		from erpnext.image_search.thumb import (
			download_image_bytes,
			materialize_item_thumb,
			read_local_file_bytes,
			_normalize_file_url,
			unwrap_image_url,
		)

		url = unwrap_image_url(url)
		if "/api/image-proxy" in url and "src=" in url:
			from urllib.parse import parse_qs, unquote, urlparse

			qs = parse_qs(urlparse(url).query)
			src_vals = qs.get("src") or []
			if src_vals:
				url = unwrap_image_url(unquote(src_vals[0]))

		if url.startswith("data:image/"):
			image_bytes = download_image_bytes(url)
		elif url.startswith("http://") or url.startswith("https://"):
			parsed_path = _normalize_file_url(url)
			if parsed_path.startswith("/files/") or parsed_path.startswith("/private/files/"):
				try:
					image_bytes = read_local_file_bytes(parsed_path)
				except Exception:
					from erpnext.erpnext_integrations.ecommerce_api.image_cdn import (
						download_image_prefer_imgproxy,
					)

					image_bytes = download_image_prefer_imgproxy(url)
			else:
				from erpnext.erpnext_integrations.ecommerce_api.image_cdn import (
					download_image_prefer_imgproxy,
				)

				image_bytes = download_image_prefer_imgproxy(url)
		elif url.startswith("/"):
			# Already a site-relative file — just point Item.image at it.
			frappe.db.set_value("Item", item_code, "image", _normalize_file_url(url) or url)
			return _normalize_file_url(url) or url, None
		else:
			return None, "unsupported image url"

		file_url = materialize_item_thumb(
			item_code,
			image_bytes,
			crop=None,
			commit=False,
			variant="final",
			set_item_image=True,
			fmt="png",
		)
		return file_url, None
	except Exception as exc:
		return None, str(exc)


def _ensure_brand_for_import(brand_name, *, create_missing=1):
	"""Return Brand name, creating it when allowed. Empty → None."""
	brand_name = cstr(brand_name or "").strip()
	if not brand_name:
		return None
	if frappe.db.exists("Brand", brand_name):
		return brand_name
	if not cint(create_missing):
		return None
	frappe.get_doc({"doctype": "Brand", "brand": brand_name}).insert(ignore_permissions=True)
	return brand_name


def _resolve_uom_for_import(uom):
	"""Resolve CSV stock_uom via product_manager aliases (WEIGHT / CAJA / Nos).

	Creates missing UOM master rows (e.g. WEIGHT) instead of silently falling
	back to Nos when the CSV asks for a weighed unit.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.product_manager import _normalize_stock_uom

	raw = cstr(uom or "").strip()
	if not raw:
		return _normalize_stock_uom("Nos")
	return _normalize_stock_uom(raw)


def _root_item_group_name():
	if frappe.db.exists("Item Group", "All Item Groups"):
		return "All Item Groups"
	return frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"


def _item_group_path(parent_name, leaf_name):
	parent = cstr(parent_name or "").strip()
	leaf = cstr(leaf_name or "").strip()
	if parent and leaf and parent not in ("All Item Groups",) and parent != leaf:
		return f"{parent}>{leaf}"
	return leaf or parent


def _holding_leaf_name(parent_name):
	"""Temporary leaf for items that lived on a group before it was promoted."""
	base = f"{parent_name} › Otros"
	if not frappe.db.exists("Item Group", base):
		return base
	# Already exists — reuse
	return base


def _ensure_item_group_is_parent(parent_name, *, create_missing=1):
	"""Ensure ``parent_name`` exists as is_group=1 under All Item Groups.

	If it currently exists as a leaf with Items assigned, move those Items onto a
	holding leaf ``{parent} › Otros``, promote the node, then reparent the holder.
	"""
	parent_name = cstr(parent_name or "").strip()
	if not parent_name:
		return None
	root = _root_item_group_name()
	create_missing = cint(create_missing)

	if not frappe.db.exists("Item Group", parent_name):
		if not create_missing:
			return None
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": parent_name,
				"parent_item_group": root,
				"is_group": 1,
			}
		).insert(ignore_permissions=True)
		return parent_name

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Item Group", parent_name)
	try:
		if cint(doc.is_group):
			if not (doc.parent_item_group or "").strip():
				doc.parent_item_group = root
				doc.save(ignore_permissions=True)
			return parent_name

		# Promote leaf → group. Items cannot stay on an is_group=1 node.
		holding = _holding_leaf_name(parent_name)
		if not frappe.db.exists("Item Group", holding):
			if not create_missing:
				# Cannot promote safely without a place for existing items.
				return parent_name
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": holding,
					"parent_item_group": root,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)

		frappe.db.sql(
			"UPDATE `tabItem` SET item_group=%s WHERE item_group=%s",
			(holding, parent_name),
		)

		doc.is_group = 1
		doc.parent_item_group = doc.parent_item_group or root
		doc.save(ignore_permissions=True)

		holding_doc = frappe.get_doc("Item Group", holding)
		if holding_doc.parent_item_group != parent_name:
			holding_doc.parent_item_group = parent_name
			holding_doc.save(ignore_permissions=True)
		return parent_name
	finally:
		frappe.flags.ignore_permissions = False


def _ensure_item_group_leaf(leaf_name, parent_name, *, create_missing=1):
	"""Ensure a leaf Item Group under ``parent_name``; return the leaf name to assign."""
	leaf_name = cstr(leaf_name or "").strip()
	parent_name = cstr(parent_name or "").strip()
	create_missing = cint(create_missing)
	if not leaf_name:
		return None

	def _create_under(name, parent):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": parent,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		return name

	if frappe.db.exists("Item Group", leaf_name):
		row = frappe.db.get_value(
			"Item Group",
			leaf_name,
			["parent_item_group", "is_group"],
			as_dict=True,
		)
		if cint(row.is_group):
			# Name taken by a group node — use a composite leaf name.
			composite = f"{parent_name} › {leaf_name}" if parent_name else f"{leaf_name} › Items"
			if frappe.db.exists("Item Group", composite):
				return composite
			if not create_missing:
				return None
			if parent_name:
				_ensure_item_group_is_parent(parent_name, create_missing=1)
			return _create_under(composite, parent_name or _root_item_group_name())

		current_parent = cstr(row.parent_item_group or "").strip()
		if parent_name and current_parent == parent_name:
			return leaf_name
		if parent_name and current_parent and current_parent != parent_name:
			# Avoid reparenting a shared leaf — create a namespaced sibling.
			composite = f"{parent_name} › {leaf_name}"
			if frappe.db.exists("Item Group", composite):
				return composite
			if not create_missing:
				return leaf_name
			_ensure_item_group_is_parent(parent_name, create_missing=1)
			return _create_under(composite, parent_name)
		if parent_name and not current_parent:
			if create_missing:
				_ensure_item_group_is_parent(parent_name, create_missing=1)
				frappe.flags.ignore_permissions = True
				try:
					doc = frappe.get_doc("Item Group", leaf_name)
					doc.parent_item_group = parent_name
					doc.save(ignore_permissions=True)
				finally:
					frappe.flags.ignore_permissions = False
			return leaf_name
		return leaf_name

	if not create_missing:
		return None
	parent = parent_name
	if parent:
		_ensure_item_group_is_parent(parent, create_missing=1)
	else:
		parent = _root_item_group_name()
	return _create_under(leaf_name, parent)


def _resolve_item_group_for_import(
	item_group,
	default_item_group="Products",
	create_missing_groups=0,
	parent_item_group=None,
):
	"""Resolve (and optionally create) the leaf Item Group for an import row.

	When ``parent_item_group`` is set (Airtable Clase / custom parent map), ensure
	a tree ``parent (is_group=1) → leaf (is_group=0)`` and return the leaf name.
	"""
	leaf = cstr(item_group or "").strip()
	parent = cstr(parent_item_group or "").strip()
	create = cint(create_missing_groups)

	if parent and leaf and parent != leaf:
		ensured_parent = _ensure_item_group_is_parent(parent, create_missing=create)
		if not ensured_parent and not create:
			# Parent missing and not allowed to create — fall through to flat leaf.
			pass
		else:
			resolved = _ensure_item_group_leaf(
				leaf, ensured_parent or parent, create_missing=create
			)
			if resolved:
				return resolved
			if frappe.db.exists("Item Group", leaf):
				return leaf

	if leaf and frappe.db.exists("Item Group", leaf):
		# Existing flat group: if caller asked for a parent, still try to promote.
		if parent and parent != leaf and create:
			_ensure_item_group_is_parent(parent, create_missing=1)
			resolved = _ensure_item_group_leaf(leaf, parent, create_missing=1)
			if resolved:
				return resolved
		return leaf

	if leaf and create:
		if parent and parent != leaf:
			_ensure_item_group_is_parent(parent, create_missing=1)
			resolved = _ensure_item_group_leaf(leaf, parent, create_missing=1)
			if resolved:
				return resolved
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": leaf,
				"parent_item_group": _root_item_group_name(),
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		return leaf

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
def preview_catalog_csv_import(csv_text, source="mingsheng", column_map=None):
	"""Preview parsed CSV rows.

	source="mingsheng": stable column indexes (header names ignored).
	source="airtable": fixed Airtable Productos headers.
	source="custom": real header row + column_map (logical field → CSV header).
	"""
	headers = []
	resolved_map = None
	if source == "airtable":
		parsed_rows, total_rows = _parse_airtable_catalog_csv(csv_text)
	elif source == "custom":
		parsed_rows, total_rows, headers, resolved_map = _parse_custom_catalog_csv(
			csv_text, column_map
		)
	else:
		parsed_rows, total_rows = _parse_catalog_csv(csv_text)
	valid_rows = [r for r in parsed_rows if not r.get("errors")]
	invalid_rows = [r for r in parsed_rows if r.get("errors")]
	out = {
		"total_rows": total_rows,
		"parsed_rows": len(parsed_rows),
		"valid_rows": len(valid_rows),
		"invalid_rows": len(invalid_rows),
		"preview": parsed_rows[:25],
		"guide": CATALOG_CSV_COLUMN_GUIDE if source == "mingsheng" else None,
	}
	if source == "custom":
		out["headers"] = headers
		out["column_map"] = resolved_map or {}
		out["mappable_fields"] = list(CUSTOM_CATALOG_MAP_FIELDS)
	return out


@frappe.whitelist()
def import_catalog_csv_products(
	csv_text,
	price_list="Standard Selling",
	default_item_group="Products",
	update_existing=1,
	create_missing_groups=0,
	start=0,
	batch_size=0,
	import_session=None,
	file_name=None,
	source="mingsheng",
	cash_price_list="Efectivo",
	transfer_price_list="Transferencia",
	column_map=None,
	image_mode="blank",
	import_promotions=1,
	promo_style="threshold",
	force_update_uom=0,
):
	"""
	Create/update Item + Item Price records from catalog CSV (permissive).
	source="mingsheng" (default): CSV header text is ignored, only stable
	column order is used. source="airtable": a real header row (TAG/Producto/
	Clase/Efectivo/Transferencia/...) is read by name. Efectivo writes to
	price_list (catalog / Product Manager / POS default, usually Standard
	Selling) and also to cash_price_list for payment-method pricing;
	Transferencia writes only to transfer_price_list.
	source="custom": mapped price → price_list (+ optional transfer_price_list);
	cash_price → cash_price_list when mapped.
	image_mode: blank (only empty Item.image) | all (always override) | none.
	import_promotions: when 1, Airtable Oferta+Cantidad (or custom offer_rate/
	offer_qty) upsert Pricing Rules (Rate + min_qty).
	promo_style: threshold (default, all units at Oferta once qty >= N; xCaja→4)
	| pack (Nx complete packs at Oferta).
	force_update_uom: when 1, overwrite Item.stock_uom from CSV for existing
	SKUs even when already set (e.g. Nos → WEIGHT). When 0 (default), keep
	existing stock_uom on update; new Items always get the CSV UOM.
	Errors/conflicts are enqueued to Catalog Import Review by default.
	"""
	from erpnext.erpnext_integrations.ecommerce_api import catalog_import as cir

	image_mode = _normalize_catalog_image_mode(image_mode)
	import_promotions = cint(import_promotions)
	promo_style = _normalize_catalog_promo_style(promo_style)
	# Dirty clients: null/"" → off (preserve existing UOM on update).
	force_update_uom = 1 if cint(force_update_uom) else 0
	resolved_map = {}
	if source in ("airtable", "custom"):
		# Dirty clients may send null/"" — keep catalog + payment lists usable.
		price_list = (cstr(price_list) or "").strip() or "Standard Selling"
		cash_price_list = (cstr(cash_price_list) or "").strip() or "Efectivo"
		transfer_price_list = (cstr(transfer_price_list) or "").strip()
		if source == "airtable" and not transfer_price_list:
			transfer_price_list = "Transferencia"
		_ensure_price_list(price_list)
		_ensure_price_list(cash_price_list)
		if transfer_price_list:
			_ensure_price_list(transfer_price_list)
		if source == "airtable":
			parsed_rows, total_rows = _parse_airtable_catalog_csv(csv_text)
		else:
			parsed_rows, total_rows, _headers, resolved_map = _parse_custom_catalog_csv(
				csv_text, column_map
			)
	else:
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

	session_name = cir.ensure_import_session(
		import_session,
		price_list=price_list,
		default_item_group=default_item_group,
		update_existing=update_existing,
		create_missing_groups=create_missing_groups,
		file_name=file_name,
		total_rows=total_rows if start == 0 else 0,
		parsed_rows=len(parsed_rows) if start == 0 else 0,
	)

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
		"image_updates": 0,
		"image_failures": 0,
		"image_mode": image_mode,
		"promo_style": promo_style,
		"force_update_uom": force_update_uom,
		"uom_updates": 0,
		"promo_updates": 0,
		"promo_disabled": 0,
		"items_enabled": 0,
		"items_disabled": 0,
		"review_created": 0,
		"import_session": session_name,
		"errors": [],
		"warnings": [],
	}

	# First occurrence of each SKU in the full file wins; later duplicates → review.
	first_seen_line = {}
	for prow in parsed_rows:
		code = (prow.get("item_code") or "").strip()
		if not code:
			continue
		if code not in first_seen_line:
			first_seen_line[code] = prow.get("line_no")

	for row in selected_rows:
		item_code = (row.get("item_code") or "").strip()
		payload = {
			"line_no": row.get("line_no"),
			"item_code": item_code,
			"item_name": row.get("item_name"),
			"title_simplified": row.get("title_simplified"),
			"barcode": row.get("barcode"),
			"stock_uom": row.get("stock_uom"),
			"item_group": row.get("item_group"),
			"parent_item_group": row.get("parent_item_group"),
			"brand": row.get("brand"),
			"disabled": row.get("disabled"),
			"price": row.get("price"),
			"cash_price": row.get("cash_price"),
			"offer_rate": row.get("offer_rate"),
			"offer_qty": row.get("offer_qty"),
			"last_price": row.get("last_price"),
			"stock_hint": row.get("stock_hint"),
			"errors": row.get("errors") or [],
		}

		if row.get("errors"):
			report["skipped_invalid"] += 1
			report["errors"].append({"line_no": row["line_no"], "errors": row["errors"]})
			reason = cir.REASON_MISSING_ITEM_CODE
			joined = " ".join(row["errors"]).lower()
			if "item_name" in joined or "title" in joined:
				reason = cir.REASON_MISSING_NAME
			if not item_code:
				reason = cir.REASON_MISSING_ITEM_CODE
			cir.enqueue_import_review(
				session_name,
				line_no=row.get("line_no"),
				item_code=item_code,
				reason_code=reason,
				message="; ".join(row["errors"]),
				severity="error",
				payload=payload,
			)
			report["review_created"] += 1
			continue

		# Duplicate SKU later in the same file → review-only (first wins live).
		if item_code and first_seen_line.get(item_code) not in (None, row.get("line_no")):
			msg = (
				f"Duplicate SKU {item_code} in file; first occurrence at line "
				f"{first_seen_line.get(item_code)} already applied/queued."
			)
			report["warnings"].append({"line_no": row["line_no"], "message": msg})
			cir.enqueue_import_review(
				session_name,
				line_no=row.get("line_no"),
				item_code=item_code,
				reason_code=cir.REASON_DUPLICATE_IN_FILE,
				message=msg,
				severity="conflict",
				payload=payload,
			)
			report["review_created"] += 1
			continue

		item_name = row["item_name"]
		description = row["title_simplified"] or item_name
		barcode = row.get("barcode")

		try:
			target_uom = _resolve_uom_for_import(row.get("stock_uom"))
		except Exception as exc:
			report["errors"].append({"line_no": row["line_no"], "errors": [str(exc)]})
			cir.enqueue_import_review(
				session_name,
				line_no=row.get("line_no"),
				item_code=item_code,
				reason_code=cir.REASON_UOM_RESOLVE_FAILED,
				message=str(exc),
				severity="error",
				payload=payload,
			)
			report["review_created"] += 1
			continue

		try:
			target_group = _resolve_item_group_for_import(
				row.get("item_group"),
				default_item_group=default_item_group,
				create_missing_groups=create_missing_groups,
				parent_item_group=row.get("parent_item_group"),
			)
		except Exception as exc:
			report["errors"].append({"line_no": row["line_no"], "errors": [str(exc)]})
			cir.enqueue_import_review(
				session_name,
				line_no=row.get("line_no"),
				item_code=item_code,
				reason_code=cir.REASON_GROUP_RESOLVE_FAILED,
				message=str(exc),
				severity="error",
				payload=payload,
			)
			report["review_created"] += 1
			continue

		live_item = None
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
			# UOM by SKU: new Items always take CSV UOM. Existing Items keep
			# their stock_uom unless empty or force_update_uom=1.
			if not existing:
				item_doc.stock_uom = target_uom
			else:
				prev_uom = cstr(item_doc.stock_uom or "").strip()
				if not prev_uom or force_update_uom:
					if prev_uom and prev_uom != cstr(target_uom):
						report["uom_updates"] += 1
					item_doc.stock_uom = target_uom
			item_doc.is_stock_item = 1
			item_doc.include_item_in_manufacturing = 0
			# Estado (airtable) / status map → disabled (Agotado hides from catalog).
			# When status is not mapped, leave existing Item.disabled unchanged on update.
			if "disabled" in row:
				item_doc.disabled = 1 if cint(row.get("disabled")) else 0
			elif not existing:
				item_doc.disabled = 0

			brand_name = _ensure_brand_for_import(row.get("brand"), create_missing=1)
			if brand_name:
				item_doc.brand = brand_name
			elif source == "airtable":
				# Marca mapped: empty clears brand on re-import.
				item_doc.brand = None
			elif source == "custom" and "brand" in row:
				item_doc.brand = None

			if frappe.db.has_column("Item", "custom_normalized_title"):
				# Keep catalog display title = Producto (not Etiquetas/Clase).
				item_doc.custom_normalized_title = item_name

			# Defer remote images until after insert/save so we can materialize
			# a local 256×256 thumb (same as Product Manager). Broken links
			# fall back to the remote URL + a warning — never abort the row.
			pending_image_url = cstr(row.get("image_url") or "").strip()
			if pending_image_url and _should_apply_catalog_image(
				item_doc.image, pending_image_url, image_mode
			):
				if not _is_remote_image_url(pending_image_url) and not pending_image_url.startswith(
					"data:image/"
				):
					# Local /files/ path or relative — set directly on the doc.
					item_doc.image = pending_image_url
					pending_image_url = ""
			else:
				pending_image_url = ""

			if existing:
				item_doc.save(ignore_permissions=True)
			else:
				item_doc.insert(ignore_permissions=True)
			live_item = item_doc.item_code
			if cint(item_doc.disabled):
				report["items_disabled"] += 1
			else:
				report["items_enabled"] += 1

			if pending_image_url:
				local_url, img_err = _materialize_catalog_import_image(live_item, pending_image_url)
				if local_url:
					report["image_updates"] += 1
				else:
					report["image_failures"] += 1
					# Keep a remote hotlink so the catalog still shows something;
					# staff can re-run Materialize later if the CDN recovers.
					try:
						frappe.db.set_value("Item", live_item, "image", pending_image_url)
					except Exception:
						pass
					report["warnings"].append(
						{
							"line_no": row["line_no"],
							"message": f"Image download failed (kept remote URL): {img_err}",
						}
					)

			if barcode:
				existing_same = frappe.db.exists(
					"Item Barcode", {"parent": item_doc.item_code, "barcode": barcode}
				)
				owner = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
				if owner and owner != item_doc.item_code:
					msg = (
						f"Barcode {barcode} already assigned to {owner}; "
						f"skipped for {item_doc.item_code}."
					)
					report["warnings"].append({"line_no": row["line_no"], "message": msg})
					cir.enqueue_import_review(
						session_name,
						line_no=row.get("line_no"),
						item_code=item_code,
						reason_code=cir.REASON_BARCODE_OWNED,
						message=msg,
						severity="conflict",
						payload={**payload, "barcode_owner": owner},
						live_item=live_item,
					)
					report["review_created"] += 1
				elif not existing_same:
					item_doc.append("barcodes", {"barcode": barcode, "barcode_type": "EAN"})
					item_doc.save(ignore_permissions=True)
					report["barcode_updates"] += 1

			# Primary catalog / POS price:
			# - airtable: Efectivo (cash_price) → price_list (+ cash_price_list)
			# - mingsheng / custom: price column → price_list (+ optional transfer list)
			if source == "airtable":
				# Transferencia only lands on the payment-method list.
				if transfer_price_list and flt(row.get("price")) > 0:
					try:
						if _upsert_item_price(item_doc.item_code, transfer_price_list, row["price"]):
							report["price_updates"] += 1
					except Exception as price_exc:
						report["warnings"].append(
							{
								"line_no": row["line_no"],
								"message": f"Price write failed ({transfer_price_list}): {price_exc}",
							}
						)
						cir.enqueue_import_review(
							session_name,
							line_no=row.get("line_no"),
							item_code=item_code,
							reason_code=cir.REASON_PRICE_WRITE_FAILED,
							message=str(price_exc),
							severity="warning",
							payload=payload,
							live_item=live_item,
						)
						report["review_created"] += 1

				cash_targets = [price_list]
				if cash_price_list and cash_price_list != price_list:
					cash_targets.append(cash_price_list)
				if flt(row.get("cash_price")) > 0:
					for target_list in cash_targets:
						try:
							if _upsert_item_price(item_doc.item_code, target_list, row["cash_price"]):
								report["price_updates"] += 1
						except Exception as cash_price_exc:
							report["warnings"].append(
								{
									"line_no": row["line_no"],
									"message": f"Cash price write failed ({target_list}): {cash_price_exc}",
								}
							)
							cir.enqueue_import_review(
								session_name,
								line_no=row.get("line_no"),
								item_code=item_code,
								reason_code=cir.REASON_PRICE_WRITE_FAILED,
								message=str(cash_price_exc),
								severity="warning",
								payload=payload,
								live_item=live_item,
							)
							report["review_created"] += 1
			else:
				price_targets = [price_list]
				if (
					source == "custom"
					and transfer_price_list
					and transfer_price_list != price_list
				):
					price_targets.append(transfer_price_list)

				if flt(row.get("price")) > 0:
					for target_list in price_targets:
						try:
							if _upsert_item_price(item_doc.item_code, target_list, row["price"]):
								report["price_updates"] += 1
						except Exception as price_exc:
							report["warnings"].append(
								{
									"line_no": row["line_no"],
									"message": f"Price write failed ({target_list}): {price_exc}",
								}
							)
							cir.enqueue_import_review(
								session_name,
								line_no=row.get("line_no"),
								item_code=item_code,
								reason_code=cir.REASON_PRICE_WRITE_FAILED,
								message=str(price_exc),
								severity="warning",
								payload=payload,
								live_item=live_item,
							)
							report["review_created"] += 1

				write_cash = source == "custom" and resolved_map.get("cash_price")
				if write_cash and flt(row.get("cash_price")) > 0:
					try:
						if _upsert_item_price(item_doc.item_code, cash_price_list, row["cash_price"]):
							report["price_updates"] += 1
					except Exception as cash_price_exc:
						report["warnings"].append(
							{"line_no": row["line_no"], "message": f"Cash price write failed: {cash_price_exc}"}
						)
						cir.enqueue_import_review(
							session_name,
							line_no=row.get("line_no"),
							item_code=item_code,
							reason_code=cir.REASON_PRICE_WRITE_FAILED,
							message=str(cash_price_exc),
							severity="warning",
							payload=payload,
							live_item=live_item,
						)
						report["review_created"] += 1

			# Airtable Oferta (+ Cantidad) / custom offer_rate → Pricing Rule (Rate).
			if import_promotions and (
				source == "airtable" or (source == "custom" and resolved_map.get("offer_rate"))
			):
				try:
					promo_action = _upsert_airtable_oferta_promotion(
						item_doc.item_code,
						item_doc.item_name,
						row.get("offer_rate"),
						row.get("offer_qty"),
						promo_style=promo_style,
					)
					if promo_action in ("created", "updated"):
						report["promo_updates"] += 1
					elif promo_action == "disabled":
						report["promo_disabled"] += 1
				except Exception as promo_exc:
					report["warnings"].append(
						{
							"line_no": row["line_no"],
							"message": f"Promotion upsert failed: {promo_exc}",
						}
					)

		except Exception as exc:
			reason = (
				cir.REASON_ITEM_UPDATE_FAILED
				if frappe.db.exists("Item", item_code)
				else cir.REASON_ITEM_INSERT_FAILED
			)
			report["errors"].append({"line_no": row["line_no"], "errors": [str(exc)]})
			cir.enqueue_import_review(
				session_name,
				line_no=row.get("line_no"),
				item_code=item_code,
				reason_code=reason,
				message=str(exc),
				severity="error",
				payload=payload,
				live_item=live_item,
			)
			report["review_created"] += 1

	cir.bump_session_counters(
		session_name,
		{
			"created_items": report["created_items"],
			"updated_items": report["updated_items"],
			"review_created": report["review_created"],
			"skipped_invalid": report["skipped_invalid"],
			"skipped_existing": report["skipped_existing"],
			"price_updates": report["price_updates"],
			"barcode_updates": report["barcode_updates"],
		},
	)

	if batch_size > 0:
		next_start = start + len(selected_rows)
		if next_start < len(parsed_rows):
			report["has_more"] = 1
			report["next_start"] = next_start
		else:
			cir.finish_import_session(session_name, "done")
	else:
		cir.finish_import_session(session_name, "done")

	frappe.db.commit()
	return report


@frappe.whitelist()
def list_catalog_import_reviews(status="open", session=None, limit=100, start=0):
	"""List Catalog Import Review rows for Aprobaciones / Migrate."""
	from erpnext.erpnext_integrations.ecommerce_api import catalog_import as cir

	return cir.list_import_reviews(status=status, session=session, limit=limit, start=start)


@frappe.whitelist()
def resolve_catalog_import_review(name, action="apply", overrides=None, steal_barcode=0):
	"""Apply or dismiss a Catalog Import Review row."""
	from erpnext.erpnext_integrations.ecommerce_api import catalog_import as cir
	import json as _json

	if isinstance(overrides, str):
		try:
			overrides = _json.loads(overrides) if overrides else {}
		except Exception:
			overrides = {}
	return cir.resolve_import_review(
		name,
		action=action,
		overrides=overrides,
		steal_barcode=steal_barcode,
	)


@frappe.whitelist()
def dismiss_catalog_import_review(name, note=None):
	"""Dismiss a Catalog Import Review without applying."""
	from erpnext.erpnext_integrations.ecommerce_api import catalog_import as cir

	return cir.dismiss_import_review(name, note=note)


@frappe.whitelist(allow_guest=True)
def list_creation_reviews(kind="customer", status="Pending", limit=100, start=0):
	"""List non-admin Customer / guest-preorder creations awaiting admin review."""
	from erpnext.erpnext_integrations.ecommerce_api import creation_review_api as cr

	return cr.list_creation_reviews(kind=kind, status=status, limit=limit, start=start)


@frappe.whitelist(allow_guest=True)
def confirm_creation_review(kind=None, name=None):
	"""Confirm a pending creation review (remove from Revisión queue)."""
	from erpnext.erpnext_integrations.ecommerce_api import creation_review_api as cr

	return cr.confirm_creation_review(kind=kind, name=name)


@frappe.whitelist(allow_guest=True)
def delete_creation_review(kind=None, name=None):
	"""Delete/cancel a pending creation from the Revisión queue."""
	from erpnext.erpnext_integrations.ecommerce_api import creation_review_api as cr

	return cr.delete_creation_review(kind=kind, name=name)


@frappe.whitelist(allow_guest=True)
def count_pending_creation_reviews():
	"""Pending customer + order creation-review counts for nav badges."""
	from erpnext.erpnext_integrations.ecommerce_api import creation_review_api as cr

	return cr.count_pending_creation_reviews()


@frappe.whitelist(allow_guest=True)
def suggest_guest_preorder_change(preorder_name=None, change=None):
	"""Seller suggestion-only: queue a patch for Revisar without applying it."""
	from erpnext.erpnext_integrations.ecommerce_api import creation_review_api as cr

	return cr.suggest_guest_preorder_change(preorder_name=preorder_name, change=change)


@frappe.whitelist(allow_guest=True)
def presentation_demo_status():
	"""Settings: whether the presentation demo pack is loaded."""
	from erpnext.erpnext_integrations.ecommerce_api import presentation_demo_api as pd

	return pd.presentation_demo_status()


@frappe.whitelist(allow_guest=True)
def seed_presentation_demo(reset=0):
	"""Load sample orders + fake team tagged for Settings wipe."""
	from erpnext.erpnext_integrations.ecommerce_api import presentation_demo_api as pd

	return pd.seed_presentation_demo(reset=reset)


@frappe.whitelist(allow_guest=True)
def clear_presentation_demo(pin=None):
	"""Wipe tagged presentation demo rows. Requires admin PIN."""
	from erpnext.erpnext_integrations.ecommerce_api import presentation_demo_api as pd

	return pd.clear_presentation_demo(pin=pin)


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


@frappe.whitelist(allow_guest=True)
def upload_item_image_mobile(item_code=None, image_base64=None, filename=None):
	"""
	Mobile-optimized image upload endpoint for single item images.
	
	Accepts:
	- item_code: SKU/item code (required, can also resolve from barcode)
	- image_base64: base64-encoded image data (with or without data URL prefix)
	- filename: original filename (optional, used for file extension detection)
	
	Supports:
	- Direct multipart form file upload (field name: 'file' or 'image')
	- Base64 payload in JSON request body
	- Auto-resolution: barcode → item_code
	- Image types: jpg, jpeg, png, webp, gif
	
	Returns:
	{
		"ok": true/false,
		"image": "<file_url>",     # URL to uploaded image (if ok=true)
		"item_code": "<item_code>", # Resolved item code
		"message": "<msg>",        # Status message
		"error": "<error>"         # Error details (if ok=false)
	}
	"""
	# Ecommerce / device-link callers use API keys without desk Item write role.
	frappe.flags.ignore_permissions = True

	response = {
		"ok": False,
		"image": None,
		"item_code": None,
		"message": None,
		"error": None,
	}
	
	try:
		# Step 1: Resolve item_code
		if not item_code:
			frappe.throw(_("item_code is required"))
		
		resolved_code = _resolve_item_code_from_image_stem(item_code)
		if not resolved_code:
			# Try as-is (might be exact item_code)
			if frappe.db.exists("Item", item_code):
				resolved_code = item_code
			else:
				response["error"] = f"Item not found: {item_code}"
				return response
		
		response["item_code"] = resolved_code
		
		# Step 2: Get image content
		image_bytes = None
		
		# Try multipart file upload first
		req_files = getattr(getattr(frappe.local, "request", None), "files", None)
		if req_files:
			uploaded_file = req_files.get("file") or req_files.get("image")
			if uploaded_file:
				image_bytes = uploaded_file.read()
				if not filename:
					filename = getattr(uploaded_file, "filename", None)
		
		# Fall back to base64 payload
		if not image_bytes:
			if not image_base64:
				frappe.throw(_("No image data provided. Send multipart file or image_base64."))
			
			# Handle data URL format: "data:image/jpeg;base64,..."
			if "," in image_base64:
				image_base64 = image_base64.split(",", 1)[1]
			
			try:
				image_bytes = base64.b64decode(image_base64)
			except Exception as e:
				response["error"] = f"Invalid base64 data: {str(e)}"
				return response
		
		if not image_bytes:
			response["error"] = "No valid image data"
			return response
		
		# Step 3: Validate image format
		if filename:
			_, ext = os.path.splitext(filename)
		else:
			# Try to detect from bytes magic number
			ext = _detect_image_format(image_bytes)
			if not ext:
				ext = ".png"  # Prefer PNG so catalog cutouts keep transparency
			filename = f"{resolved_code}{ext}"
		
		ext = ext.lower()
		supported_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
		if ext not in supported_ext:
			response["error"] = f"Unsupported image format: {ext}. Supported: {', '.join(supported_ext)}"
			return response
		
		if not filename:
			filename = f"{resolved_code}{ext}"
		
		# Step 4: Save image to Frappe
		try:
			_apply_item_image(resolved_code, filename, image_bytes)
			file_url = frappe.db.get_value("Item", resolved_code, "image")
			response["ok"] = True
			response["image"] = file_url
			response["message"] = f"Image uploaded successfully for {resolved_code}"
			frappe.db.commit()
		except Exception as e:
			response["error"] = f"Failed to save image: {str(e)}"
			return response
		
		return response
	
	except frappe.PermissionError:
		response["error"] = "Permission denied"
		return response
	except Exception as e:
		response["error"] = str(e)
		return response


def _detect_image_format(data):
	"""Detect image format from file magic bytes."""
	if not data or len(data) < 4:
		return None
	
	# JPEG: FF D8 FF
	if data[:3] == b'\xff\xd8\xff':
		return '.jpg'
	# PNG: 89 50 4E 47
	elif data[:4] == b'\x89PNG':
		return '.png'
	# GIF: 47 49 46
	elif data[:3] == b'GIF':
		return '.gif'
	# WebP: RIFF ... WEBP
	elif data[:4] == b'RIFF' and len(data) >= 12 and data[8:12] == b'WEBP':
		return '.webp'
	
	return None


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
		"custom_normalized_title", "custom_review_notes",
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
def save_product_row(item_code, changes, warehouse=None):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().save_product_row(
		item_code=item_code, changes=changes, warehouse=warehouse
	)


@frappe.whitelist()
def save_product_rows_bulk(rows, warehouse=None):
	"""Legacy wrapper; canonical implementation lives in ecommerce_api.product_manager."""
	return _pm_module().save_product_rows_bulk(rows=rows, warehouse=warehouse)


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
def get_unit_sku_neighbors(anchor_item_code=None, limit=10):
	"""i025: lexicographic title neighbors for Unit SKU picker."""
	return _pm_module().get_unit_sku_neighbors(
		anchor_item_code=anchor_item_code, limit=limit
	)


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


# ========================================
# EMAIL + IMAGE SEARCH (public / Swagger)
# ========================================


@frappe.whitelist(allow_guest=True)
def send_email(subject, message, receiver, sender=None):
	"""
	Send an email using the site's default configured SMTP / Email Account.

	Required:
	  - subject
	  - message (HTML or plain text)
	  - receiver (one address, or comma-separated list)

	Optional:
	  - sender — if blank, uses the default outgoing account identity
	"""
	from erpnext.erpnext_integrations.ecommerce_api.inquiry_email import send_outbound_email

	return send_outbound_email(
		subject=subject,
		message=message,
		receiver=receiver,
		sender=sender,
	)


@frappe.whitelist(allow_guest=True)
def search_images(query, limit=9, store_in_erp=0, item_code=None, set_item_image=0):
	"""
	Start an asynchronous web image search.

	Returns immediately with ``results_url`` — poll that endpoint until
	``status`` is ``completed`` or ``failed``. When completed, ``images``
	contains link objects (``url``, ``thumbnail_url``, …).

	Optional ERP storage:
	  - store_in_erp=1 → download images into ERP File records
	  - store_in_erp=1 + item_code → also save Product Image Candidate rows on that Item
	  - set_item_image=1 → also set Item.image from the top candidate (requires store_in_erp + item_code)
	"""
	from erpnext.erpnext_integrations.ecommerce_api.image_search_api import start_image_search

	return start_image_search(
		query=query,
		limit=limit,
		store_in_erp=store_in_erp,
		item_code=item_code,
		set_item_image=set_item_image,
	)


@frappe.whitelist(allow_guest=True)
def get_image_search_results(job_id):
	"""
	Poll an image search started by ``search_images``.

	Statuses: pending | running | completed | failed.
	On completed, ``images`` is a list of ``{url, thumbnail_url, source, …}``.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.image_search_api import get_image_search_job

	return get_image_search_job(job_id)
