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

	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)
	item_codes = [c for c in (item_codes or []) if c]
	if not item_codes:
		return {}

	if price_lists:
		if isinstance(price_lists, str):
			price_lists = json.loads(price_lists)
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
	existing_name = None
	if frappe.db.exists("Customer", customer_name):
		existing_name = customer_name
	else:
		found = frappe.db.sql(
			"""
			SELECT name FROM `tabCustomer`
			WHERE LOWER(TRIM(customer_name)) = %s
			LIMIT 1
			""",
			(str(customer_name).strip().lower(),),
		)
		if found:
			existing_name = found[0][0]

	if existing_name:
		frappe.flags.ignore_permissions = True
		customer = frappe.get_doc("Customer", existing_name)
		frappe.flags.ignore_permissions = False
		return customer.as_dict()

	customer = frappe.new_doc("Customer")
	customer.customer_name = customer_name
	customer.customer_group = customer_group
	customer.territory = territory
	customer.customer_type = customer_type
	if phone:
		customer.mobile_no = phone
	if email:
		customer.email_id = email

	customer.insert(ignore_permissions=True)
	frappe.db.commit()

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
		frappe.db.commit()

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook(
			"customer_created",
			{"customer": customer.name, "customer_name": customer.customer_name},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook customer_created")

	return customer.as_dict()


UNCATEGORIZED_CUSTOMER_NAME = "Uncategorized"
UNCATEGORIZED_SUPPLIER_NAME = "Uncategorized"


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

	term = (search_term or "").strip()
	limit = max(1, min(cint(page_length) or 20, 100))
	filters = {"disabled": 0}
	or_filters = None
	if term:
		like = f"%{term}%"
		or_filters = [
			["name", "like", like],
			["customer_name", "like", like],
			["mobile_no", "like", like],
		]

	rows = frappe.get_all(
		"Customer",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "customer_name", "mobile_no", "email_id", "customer_group", "territory"],
		order_by="customer_name asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	return {
		"customers": [
			{
				"name": r.name,
				"customer_name": r.customer_name,
				"phone": r.mobile_no,
				"email": r.email_id,
				"customer_group": r.customer_group,
				"territory": r.territory,
				"is_bucket": (r.customer_name or r.name or "")
				in (CONSUMIDOR_FINAL_NAME, UNCATEGORIZED_CUSTOMER_NAME),
			}
			for r in rows
		]
	}


@frappe.whitelist(allow_guest=True)
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
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


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
		"mobile_no", "email_id",
	]

	for field, value in kwargs.items():
		if field in allowed_fields:
			customer.set(field, value)

	customer.save(ignore_permissions=True)
	frappe.db.commit()

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
	so.delivery_date = delivery_date or add_days(nowdate(), 7)
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
				"currency": so.currency,
				"conversion_rate": so.conversion_rate,
				"transaction_date": so.transaction_date,
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

# SilkOS Sales Order `order_type` only allows a small set (e.g. Sales, Shopping Cart).
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


def _guest_preorder_tag_text(so) -> str:
	return str(getattr(so, "remarks", None) or getattr(so, "terms", None) or "")


def _require_guest_preorder_visible(so) -> None:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_order_visibility_scope,
		guest_preorder_matches_scope,
	)

	scope = _order_visibility_scope()
	if guest_preorder_matches_scope(getattr(so, "owner", ""), _guest_preorder_tag_text(so), scope):
		return
	frappe.throw(_("Not permitted (tables.orders)"))


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
		tag_fn = _guest_preorder_tag_fieldname()
		if tag_fn:
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
	is_delivery=0,
	paid_amount=None,
	mode_of_payment=None,
	customer=None,
	order_tag=None,
	seller_ref_user=None,
	cashier_id=None,
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

	Returns: { preorder_name, estimated_total, currency, status }
	"""
	if isinstance(items, str):
		import json
		items = json.loads(items)

	notes_only = bool(cstr(guest_notes or "").strip()) and not items

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
		if not item_code:
			continue
		if not frappe.db.exists("Item", item_code):
			alts = _item_codes_for_barcode(item_code)
			if alts:
				item_code = alts[0]
			else:
				missing.append(item_code)
				continue
		resolved_rows.append({"item_code": item_code, "qty": qty, "rate": rate})

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

	if customer and not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found").format(customer))
	customer = customer or _get_or_create_consumidor_final()
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
	so.delivery_date = delivery_date or add_days(nowdate(), 7)
	so.company = company
	so.selling_price_list = price_list
	# Consultas are local quotes. Do not look up Currency Exchange (None → ARS).
	so.currency = company_currency
	so.conversion_rate = 1
	so.price_list_currency = price_list_currency
	so.plc_conversion_rate = 1

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
			})
			rate = item_details.get("price_list_rate", 0)

		so.append("items", {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"delivery_date": so.delivery_date,
		})

	# Calculate totals. Re-stamp so insert/validate cannot treat currency as None.
	so.currency = company_currency
	so.conversion_rate = 1
	so.price_list_currency = price_list_currency
	so.plc_conversion_rate = 1
	if resolved_rows:
		so.run_method("calculate_taxes_and_totals")

	# Save as Draft (Consulta). Stamp the acting cashier so Pedidos propios can match.
	if acting and frappe.db.exists("User", acting):
		so.owner = acting
	# Notes-only inquiries have no item rows — skip mandatory child-table / item checks.
	if notes_only:
		so.flags.ignore_validate = True
	so.insert(ignore_permissions=True, ignore_mandatory=notes_only)
	if acting and frappe.db.exists("User", acting) and so.owner != acting:
		so.db_set("owner", acting)

	paid = flt(paid_amount)
	if paid > 0:
		cap = flt(so.grand_total)
		so.db_set("advance_paid", min(paid, cap) if cap > 0 else paid)

	from erpnext.erpnext_integrations.ecommerce_api.inquiry_email import send_consulta_notification

	send_consulta_notification(so.name, guest_name=guest_name, guest_phone=guest_phone)

	# Best-effort Pre-venta bridge: never let a Lead-sync problem affect the
	# guest preorder response (see local_docs/proposals/i033_preventa_sales_kanban.md).
	try:
		from erpnext.erpnext_integrations.ecommerce_api.preventa_api import sync_lead_from_guest_preorder

		sync_lead_from_guest_preorder(
			so.name,
			guest_name=guest_name,
			guest_phone=guest_phone,
			guest_email=guest_email,
			seller_ref_user=seller_ref_user,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Preventa lead sync failed for {so.name}")

	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook(
			"order_created",
			{
				"preorder_name": so.name,
				"customer": so.customer,
				"grand_total": flt(so.grand_total),
				"currency": so.currency,
			},
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "webhook order_created")

	return {
		"preorder_name": so.name,
		"estimated_total": flt(so.grand_total),
		"currency": so.currency,
		"status": so.status,
		"advance_paid": flt(so.advance_paid) if paid > 0 else 0,
	}


@frappe.whitelist()
def get_guest_preorders_list(status=None, start=0, page_length=20, cashier_id=None, scope="pos"):
	"""
	List Guest Preorders created by `create_guest_preorder`.

	Cancelled orders that have been superseded by an amended version are excluded.
	Only user-archived orders (no successor) and active orders are shown.
	"""
	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return {"preorders": [], "total_count": 0}

	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_order_visibility_scope,
		guest_preorder_matches_scope,
	)

	scope = _order_visibility_scope()
	if scope is not None and not scope.get("own") and not scope.get("tags"):
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
		start=start,
		limit_page_length=int(page_length) + (200 if scope else 50),  # fetch extra to account for filtering
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
	filtered = []
	for o in orders:
		if o.get("docstatus") == 2 and o["name"] in superseded:
			continue
		if not guest_preorder_matches_scope(o.get("owner"), o.get(tag_fn), scope):
			continue
		tag_raw = str(o.get(tag_fn) or "")
		order_tag = ""
		for part in tag_raw.split("|"):
			part = part.strip()
			if part.startswith("order_tag:"):
				order_tag = part.split(":", 1)[1].strip()
				break
		o["order_tag"] = order_tag or None
		o.pop(tag_fn, None)
		filtered.append(o)
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
		o["cashier_user"] = _cashier_from_guest_preorder(o)
		o["display_status"] = _display_status_from_row(
			o.get("docstatus", 0),
			o.get("status", ""),
			o.get("grand_total", 0),
			o.get("advance_paid", 0),
		)

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
	_require_guest_preorder_visible(so)

	tag_raw = getattr(so, "remarks", None) or getattr(so, "terms", None) or ""
	tags = {}
	for part in str(tag_raw).split("|"):
		part = part.strip()
		if ":" in part:
			key, val = part.split(":", 1)
			tags[key.strip()] = val.strip()

	return {
		"name": so.name,
		"order_type": so.order_type,
		"customer": so.customer,
		"customer_name": frappe.db.get_value("Customer", so.customer, "customer_name") or so.customer,
		"guest_name": tags.get("guest_name") or None,
		"guest_phone": tags.get("guest_phone") or None,
		"guest_address": tags.get("guest_address") or None,
		"guest_notes": tags.get("guest_notes") or None,
		"is_delivery": tags.get("delivery") == "1",
		"transaction_date": so.transaction_date,
		"delivery_date": so.delivery_date,
		"docstatus": so.docstatus,
		"status": so.status,
		"display_status": _display_status(so),
		"cashier_user": _cashier_from_guest_preorder(so),
		"estimated_total": flt(so.grand_total),
		"currency": so.currency,
		"remarks": getattr(so, "remarks", None),
		"terms": getattr(so, "terms", None),
		"additional_discount_amount": flt(getattr(so, "additional_discount_amount", 0)),
		"advance_paid": flt(getattr(so, "advance_paid", 0)),
		"amended_from": so.amended_from or None,
		"delivery_note": _delivery_note_for_sales_order(preorder_name),
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
# Display statuses: Consulta → Orden → Preparado → En Delivery → Completado
# Mapping to SilkOS:
#   Consulta   = docstatus 0 (Draft)
#   Orden      = docstatus 1, status "To Deliver and Bill"
#   Preparado  = docstatus 1, status "Preparado"   (custom via db_set)
#   En Delivery= docstatus 1, status "En Delivery"  (custom via db_set)
#   Completado = docstatus 1, status "Completed"
#   Archivado  = docstatus 2 (Cancelled)

WORKFLOW_STATUSES = ["Consulta", "Orden", "Preparado", "En Delivery", "Completado"]
COMPLETED_UNPAID_LABEL = "Completado (no pagado)"


def _so_is_fully_paid(so) -> bool:
	total = flt(getattr(so, "grand_total", 0) or 0)
	paid = flt(getattr(so, "advance_paid", 0) or 0)
	return total > 0 and paid + 0.005 >= total


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
		return "Completado" if _so_is_fully_paid(so) else COMPLETED_UNPAID_LABEL
	# "To Deliver and Bill", "To Deliver", "To Bill", etc.
	return "Orden"


def _display_status_from_row(docstatus, status, grand_total=0, advance_paid=0):
	"""Same as _display_status but from list-row fields."""
	if docstatus == 0:
		return "Consulta"
	if docstatus == 2:
		return "Archivado"
	if status in ("Preparado", "En Delivery"):
		return status
	if status == "Completed":
		total = flt(grand_total or 0)
		paid = flt(advance_paid or 0)
		if total > 0 and paid + 0.005 >= total:
			return "Completado"
		return COMPLETED_UNPAID_LABEL
	return "Orden"


def _erp_status_for_display(display_status):
	"""Map display status → SilkOS status string."""
	return {
		"Orden": "To Deliver and Bill",
		"Preparado": "Preparado",
		"En Delivery": "En Delivery",
		"Completado": "Completed",
		COMPLETED_UNPAID_LABEL: "Completed",
	}.get(display_status)


@frappe.whitelist(allow_guest=True)
def set_guest_preorder_status(preorder_name, target_status):
	"""
	Unified status transition for the custom workflow.

	Accepts target_status as one of: Consulta, Orden, Preparado, En Delivery, Completado.
	Consulta reverts a submitted order back to Draft (cancel + amend).
	Backward moves among submitted custom statuses use db_set so SilkOS
	update_status does not block with an opaque error.
	"""
	if target_status not in WORKFLOW_STATUSES:
		frappe.throw(_("Invalid target status: {0}").format(target_status))

	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", preorder_name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

	if so.docstatus == 2:
		frappe.throw(_("Cannot change status of a cancelled order"))

	current = _display_status(so)
	current_base = "Completado" if str(current).startswith("Completado") else current

	# Revert to Consulta (Draft)
	if target_status == "Consulta":
		if so.docstatus == 1:
			try:
				so.flags.ignore_permissions = True
				so.cancel()
			except Exception as e:
				frappe.throw(
					_("Cannot go back to Consulta from {0}: {1}").format(
						current_base, frappe.utils.cstr(e)
					)
				)
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
		try:
			so.flags.ignore_permissions = True
			so.submit()
			so.reload()
		except Exception as e:
			frappe.throw(
				_("Cannot move from Consulta to {0}: submit failed — {1}").format(
					target_status, frappe.utils.cstr(e)
				)
			)

	erp_status = _erp_status_for_display(target_status)
	if not erp_status:
		frappe.throw(_("Invalid target status"))

	# Completado / Preparado / En Delivery: always db_set (custom workflow markers).
	# Orden ("To Deliver and Bill"): try update_status first; on failure fall back to
	# db_set so going back from Preparado/En Delivery/Completado works.
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


@frappe.whitelist()
def unmark_prepared_guest_preorder(preorder_name):
	"""Move Preparado / later custom steps back to Orden."""
	return set_guest_preorder_status(preorder_name, "Orden")


@frappe.whitelist()
def cancel_guest_preorder(preorder_name):
	"""Cancel (archive) a guest preorder. Works on both draft and submitted orders."""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)

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
def update_guest_preorder_details(preorder_name, data=None):
	"""
	Update guest-preorder header fields (not line items).

	data: {
	  delivery_date?,
	  customer?,          # Customer link (must exist)
	  customer_name?,     # Display name on Customer
	  paid_amount?,       # Absolute advance_paid target
	  new_name?,          # Rename Sales Order
	  cashier_user?,      # Assign/reassign POS cashier (tables.orders)
	}
	"""
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = frappe._dict(data or {})

	if not preorder_name or not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)
	if so.docstatus == 2:
		frappe.throw(_("Cannot edit a cancelled order"))

	current_name = so.name

	if data.get("delivery_date"):
		so.delivery_date = getdate(data.get("delivery_date"))
		for row in so.items or []:
			row.delivery_date = so.delivery_date

	if data.get("customer"):
		customer = str(data.get("customer")).strip()
		if not frappe.db.exists("Customer", customer):
			frappe.throw(_("Customer {0} not found").format(customer))
		so.customer = customer
		_update_guest_preorder_tag(so, "customer", customer)

	if data.get("cashier_user") is not None:
		if not _can_view_all_guest_preorders():
			frappe.throw(_("Not permitted ({0})").format("tables.orders"))
		_update_guest_preorder_tag(so, "cashier", str(data.get("cashier_user") or "").strip())

	if so.docstatus == 0:
		so.save(ignore_permissions=True)
	else:
		# Submitted: persist allowed header fields without full amend
		updates = {}
		if data.get("delivery_date"):
			updates["delivery_date"] = so.delivery_date
		if data.get("customer"):
			updates["customer"] = so.customer
		tag_fn = _guest_preorder_tag_fieldname()
		if tag_fn and (data.get("customer") or data.get("cashier_user") is not None):
			updates[tag_fn] = getattr(so, tag_fn, None)
		if updates:
			frappe.db.set_value("Sales Order", current_name, updates)
			if data.get("delivery_date"):
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

	frappe.db.commit()
	return get_guest_preorder(current_name)


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
	_require_guest_preorder_visible(so)

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
	_require_guest_preorder_visible(so)

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

	outstanding = flt(so.grand_total) - flt(getattr(so, "advance_paid", 0))
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


@frappe.whitelist()
def create_delivery_note_for_preorder(preorder_name):
	"""Create + submit a real Delivery Note from a confirmed guest preorder,
	so it becomes visible in the TMS dispatcher (get_pending_deliveries only
	lists submitted Delivery Notes) - and advances the preorder's display
	status to "En Delivery". Idempotent: reuses an existing linked DN
	instead of creating a duplicate.
	"""
	if not frappe.db.exists("Sales Order", preorder_name):
		frappe.throw(_("Sales Order {0} not found").format(preorder_name))

	so = frappe.get_doc("Sales Order", preorder_name)
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"))
	_require_guest_preorder_visible(so)
	if so.docstatus != 1:
		frappe.throw(_("Confirm the order before creating a delivery note."))

	dn_name = _delivery_note_for_sales_order(preorder_name)
	if not dn_name:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		dn = make_delivery_note(preorder_name)

		# Guest preorder items never carry a warehouse (no picker in that
		# flow), so make_delivery_note falls back to the Item's own default
		# warehouse - which may not be the one this business actually stocks
		# from. Force the company's TMS depot warehouse instead, same
		# resolution the dispatcher's own trip creation uses, so the item is
		# actually available and the note lands where Rutas expects it.
		default_warehouse = frappe.db.get_value("Company", dn.company, "custom_default_warehouse")
		if default_warehouse:
			for row in dn.items:
				row.warehouse = default_warehouse
			dn.set_warehouse = default_warehouse

		dn.insert(ignore_permissions=True)
		dn.flags.ignore_permissions = True
		dn.submit()
		dn_name = dn.name
		frappe.db.commit()

	updated = set_guest_preorder_status(preorder_name, "En Delivery")
	updated["delivery_note"] = dn_name
	return updated


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
def commit_receiving_session(session_id, reference, supplier, warehouse, lines, draft_items):
	"""
	Atomically:
	1. Create new SilkOS Items for draft items (disabled/inactive until Review
	   approves them — unless draft already has approved_at)
	2. Create a submitted Stock Entry (Material Receipt)
	Returns { stock_entry_id, new_item_codes }
	"""
	import json
	if isinstance(lines, str):
		lines = json.loads(lines)
	if isinstance(draft_items, str):
		draft_items = json.loads(draft_items)

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
		if not frappe.db.exists("Supplier", supplier_name):
			frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": supplier_name,
				"supplier_group": "All Supplier Groups",
			}).insert(ignore_permissions=True)
		company = frappe.db.get_single_value("Global Defaults", "default_company")
		defaults = frappe.get_all(
			"Item Default",
			filters={"parent": item_code},
			fields=["name", "default_supplier", "company"],
			limit=1,
		)
		if defaults:
			frappe.db.set_value("Item Default", defaults[0].name, "default_supplier", supplier_name)
		else:
			item_doc = frappe.get_doc("Item", item_code)
			row = {"default_supplier": supplier_name}
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

	if not resolved_lines:
		frappe.throw("No valid lines to receive")

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
	try:
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
	finally:
		for code in reactivated:
			# Only restore if still present and we flipped it for this receipt
			if frappe.db.exists("Item", code):
				frappe.db.set_value("Item", code, "disabled", 1)
		if reactivated:
			frappe.db.commit()

	return {
		"stock_entry_id": se.name,
		"new_item_codes": new_item_codes,
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
# - pack (default): complete packs of N at Oferta; remainder at list (buy 8, N=3 → 2×3 oferta + 2 list)
# - threshold: once qty >= N, all units at Oferta (legacy "desde xN")
_CATALOG_PROMO_STYLES = ("pack", "threshold")
_PROMO_STYLE_MARKER_RE = re.compile(r"\[promo_style=(pack|threshold)\]", re.I)


def _normalize_catalog_promo_style(promo_style) -> str:
	raw = cstr(promo_style or "").strip().lower()
	if raw in ("threshold", "desde", "min_qty", "all_units", "fixed"):
		return "threshold"
	# Default (and anything dirty/empty) → pack Nx
	return "pack"


def _promo_style_from_rule(rule) -> str:
	"""Read style marker from Pricing Rule.rule_description; legacy → threshold."""
	desc = cstr((rule or {}).get("rule_description") or "")
	m = _PROMO_STYLE_MARKER_RE.search(desc)
	if m:
		return m.group(1).lower()
	# Pre-marker Airtable Rate rules behaved as threshold (all units once min_qty met).
	return "threshold"


def _parse_airtable_offer_min_qty(cantidad, item_code=None) -> float:
	"""Cantidad labels → Pricing Rule.min_qty (x2→2, xCaja→pack, xc/unidad→1)."""
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
		return pack if pack > 1 else 1.0
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


def _upsert_airtable_oferta_promotion(item_code, item_name, offer_rate, offer_qty, promo_style="pack"):
	"""Create/update/disable Pricing Rule from Airtable Oferta + Cantidad.

	promo_style:
	- pack (default): N units at Oferta per complete pack; remainder list price
	- threshold: once qty >= N, every unit at Oferta

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
	"stock_uom": ("uom", "stock_uom", "unidad", "unit", "um"),
	"price": ("transferencia", "price", "precio", "rate", "standard selling", "precio lista"),
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
	if uom and frappe.db.exists("UOM", uom):
		return uom
	if frappe.db.exists("UOM", "Nos"):
		return "Nos"
	return frappe.db.get_value("UOM", {}, "name") or "Nos"


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
	promo_style="pack",
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
	promo_style: pack (default, Nx packs at Oferta) | threshold (all units once min_qty).
	Errors/conflicts are enqueued to Catalog Import Review by default.
	"""
	from erpnext.erpnext_integrations.ecommerce_api import catalog_import as cir

	image_mode = _normalize_catalog_image_mode(image_mode)
	import_promotions = cint(import_promotions)
	promo_style = _normalize_catalog_promo_style(promo_style)
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
