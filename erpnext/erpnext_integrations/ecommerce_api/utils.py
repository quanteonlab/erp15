"""
Utility functions for E-Commerce Integration
"""

import frappe
from frappe import _
from frappe.utils import flt, cint, getdate


def validate_ecommerce_request(required_fields=None):
	"""
	Validate incoming e-commerce request

	Args:
		required_fields (list): List of required parameters

	Raises:
		frappe.ValidationError: If validation fails
	"""
	if required_fields:
		for field in required_fields:
			if not frappe.form_dict.get(field):
				frappe.throw(_("Missing required parameter: {0}").format(field))


def format_item_for_ecommerce_client(item_doc, include_stock=True, price_list=None):
	"""
	Format ERPNext item document for e-commerce client

	Args:
		item_doc: Item document object
		include_stock (bool): Include stock information
		price_list (str): Price list to fetch price from

	Returns:
		dict: E-commerce formatted item data
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		get_item_price,
		get_stock_balance,
	)

	item_data = {
		"id": item_doc.name,
		"sku": item_doc.item_code,
		"name": item_doc.item_name,
		"description": item_doc.description or "",
		"short_description": item_doc.item_name,
		"type": "variable" if item_doc.has_variants else "simple",
		"status": "publish" if not item_doc.disabled else "draft",
		"featured": cint(item_doc.is_sales_item),
		"catalog_visibility": "visible",
		"manage_stock": cint(item_doc.is_stock_item),
		"stock_quantity": None,
		"in_stock": True,
		"weight": item_doc.weight_per_unit or "",
		"dimensions": {
			"length": "",
			"width": "",
			"height": "",
		},
		"categories": [
			{"name": item_doc.item_group}
		],
		"images": [],
		"attributes": [],
		"default_attributes": [],
		"variations": [],
		"meta_data": {
			"erp_item_code": item_doc.item_code,
			"erp_item_name": item_doc.name,
		}
	}

	# Add price
	if price_list:
		price = get_item_price(item_doc.item_code, price_list)
		item_data["regular_price"] = str(price)
		item_data["price"] = str(price)
		item_data["sale_price"] = ""

	# Add stock info
	if include_stock and item_doc.is_stock_item:
		stock_qty = get_stock_balance(item_doc.item_code)
		item_data["stock_quantity"] = int(stock_qty)
		item_data["in_stock"] = stock_qty > 0
		item_data["stock_status"] = "instock" if stock_qty > 0 else "outofstock"

	# Add image
	if item_doc.image:
		item_data["images"].append({
			"src": item_doc.image,
			"name": item_doc.item_name,
			"alt": item_doc.item_name,
		})

	# Add variant attributes
	if item_doc.has_variants or item_doc.variant_of:
		attributes = frappe.get_all(
			"Item Variant Attribute",
			filters={"parent": item_doc.name},
			fields=["attribute", "attribute_value"],
		)

		for attr in attributes:
			item_data["attributes"].append({
				"name": attr.attribute,
				"option": attr.attribute_value,
				"visible": True,
				"variation": True,
			})

	return item_data


def format_order_for_erp(ecommerce_order):
	"""
	Convert e-commerce order format to ERPNext Sales Order format

	Args:
		ecommerce_order (dict): E-commerce order data

	Returns:
		dict: ERPNext-compatible order data
	"""
	# Extract customer info
	customer_email = ecommerce_order.get("billing", {}).get("email")
	customer_name = "{} {}".format(
		ecommerce_order.get("billing", {}).get("first_name", ""),
		ecommerce_order.get("billing", {}).get("last_name", "")
	).strip()

	# Map items
	items = []
	for line_item in ecommerce_order.get("line_items", []):
		items.append({
			"item_code": line_item.get("sku") or line_item.get("product_id"),
			"qty": line_item.get("quantity", 1),
			"rate": flt(line_item.get("price", 0)),
		})

	# Map taxes
	taxes = []
	for tax_line in ecommerce_order.get("tax_lines", []):
		taxes.append({
			"charge_type": "Actual",
			"account_head": "Tax - Company",  # This should be configured
			"description": tax_line.get("label", "Tax"),
			"tax_amount": flt(tax_line.get("tax_total", 0)),
		})

	return {
		"customer_name": customer_name,
		"customer_email": customer_email,
		"items": items,
		"taxes": taxes,
		"billing_address": ecommerce_order.get("billing"),
		"shipping_address": ecommerce_order.get("shipping"),
		"order_total": flt(ecommerce_order.get("total", 0)),
		"payment_method": ecommerce_order.get("payment_method_title"),
		"transaction_id": ecommerce_order.get("transaction_id"),
		"ecommerce_order_id": ecommerce_order.get("id"),
		"order_date": ecommerce_order.get("date_created"),
	}


def sync_stock_to_ecommerce(item_code, ecommerce_product_id, api_credentials):
	"""
	Sync stock quantity from ERPNext to e-commerce platform

	Args:
		item_code (str): ERPNext item code
		ecommerce_product_id: E-commerce product ID
		api_credentials (dict): E-commerce API credentials

	Returns:
		dict: Sync result
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import get_stock_balance

	stock_qty = get_stock_balance(item_code)

	# This is a placeholder - actual implementation would use e-commerce platform REST API
	# to update the stock quantity on the e-commerce side

	return {
		"success": True,
		"item_code": item_code,
		"ecommerce_product_id": ecommerce_product_id,
		"stock_qty": stock_qty,
	}


def create_webhook_log(webhook_type, payload, status="Success", error=None):
	"""
	Create a log entry for webhook processing

	Args:
		webhook_type (str): Type of webhook (order_created, order_updated, etc.)
		payload (dict): Webhook payload
		status (str): Success/Failed
		error (str): Error message if failed

	Returns:
		str: Log document name
	"""
	log = frappe.get_doc({
		"doctype": "Integration Request",
		"integration_type": "Remote",
		"integration_request_service": "E-Commerce Integration",
		"is_remote_request": 1,
		"request_description": webhook_type,
		"data": frappe.as_json(payload),
		"status": status,
		"error": error or "",
	})

	log.insert(ignore_permissions=True)
	frappe.db.commit()

	return log.name


def get_or_create_customer_from_ecommerce(ecommerce_customer):
	"""
	Get existing customer or create new one from e-commerce customer data

	Args:
		ecommerce_customer (dict): E-commerce customer data

	Returns:
		str: Customer name
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		get_customer,
		create_customer,
	)

	email = ecommerce_customer.get("email")
	first_name = ecommerce_customer.get("first_name", "")
	last_name = ecommerce_customer.get("last_name", "")
	customer_name = f"{first_name} {last_name}".strip() or email

	# Try to find existing customer by email
	try:
		customer = get_customer(email=email)
		return customer.get("name")
	except:
		# Create new customer
		new_customer = create_customer(
			customer_name=customer_name,
			email=email,
			phone=ecommerce_customer.get("phone"),
		)
		return new_customer.get("name")


def map_ecommerce_payment_status_to_erp(ecommerce_status):
	"""
	Map e-commerce payment status to ERPNext

	Args:
		ecommerce_status (str): E-commerce order status

	Returns:
		str: ERPNext payment status
	"""
	status_map = {
		"pending": "Draft",
		"processing": "Submitted",
		"on-hold": "On Hold",
		"completed": "Completed",
		"cancelled": "Cancelled",
		"refunded": "Refunded",
		"failed": "Failed",
	}

	return status_map.get(ecommerce_status, "Draft")


def calculate_shipping_cost(items, shipping_address, shipping_rule=None):
	"""
	Calculate shipping cost based on items and destination

	Args:
		items (list): List of items with quantities
		shipping_address (dict): Delivery address
		shipping_rule (str): Shipping rule to apply

	Returns:
		float: Shipping cost
	"""
	# This is a placeholder - actual implementation would use ERPNext Shipping Rule
	# or custom shipping calculation logic

	total_weight = 0
	total_qty = 0

	for item in items:
		item_doc = frappe.get_doc("Item", item.get("item_code"))
		qty = flt(item.get("qty", 1))

		total_weight += flt(item_doc.weight_per_unit) * qty
		total_qty += qty

	# Simple flat rate for demonstration
	base_rate = 5.00
	weight_rate = total_weight * 0.50

	return base_rate + weight_rate
