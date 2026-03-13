"""
E-Commerce Integration API
Provides comprehensive REST API endpoints for e-commerce integration with ERPNext

All endpoints are whitelisted and can be accessed via:
- REST API: /api/method/erpnext.erpnext_integrations.ecommerce_api.api.<method_name>
- JSON-RPC: frappe.call('erpnext.erpnext_integrations.ecommerce_api.api.<method_name>')
"""

import frappe
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

	# Search term filter
	if search_term:
		search_filters = [
			["item_code", "like", f"%{search_term}%"],
			["item_name", "like", f"%{search_term}%"],
			["description", "like", f"%{search_term}%"],
		]
		filters = [filters, search_filters]

	# Item group filter
	if item_group:
		filters["item_group"] = item_group

	# Get items
	items = frappe.get_list(
		"Item",
		filters=filters,
		fields=fields,
		start=start,
		page_length=page_length,
		order_by=order_by,
	)

	# Get total count
	total_count = frappe.db.count("Item", filters=filters)

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

	return {
		"items": items,
		"total_count": total_count,
		"has_more": (start + page_length) < total_count,
	}


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


@frappe.whitelist(allow_guest=True)
def get_customer_orders(customer, start=0, page_length=20):
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
		# Remove item if quantity is 0 or negative
		frappe.delete_doc("Shopping Cart Item", cart_item.name, ignore_permissions=True)
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
