"""
Seed POSnet test data for quick manual testing.

Run with:
bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.seed_posnet_test_data.run
"""

import random

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

from .api import get_stock_balance


def run(item_count=30, disabled_count=3, stock_item_ratio=0.8):
	"""Create baseline ERP data for POSnet testing."""
	item_count = int(item_count)
	disabled_count = int(disabled_count)
	stock_item_ratio = float(stock_item_ratio)

	if item_count < 5:
		frappe.throw(_("item_count should be at least 5"))

	company = _get_company()
	currency = _get_company_currency(company)
	_ensure_active_fiscal_year(company)
	warehouse = _ensure_warehouse(company)
	inventory_account = _ensure_inventory_account(company, warehouse)
	price_list = _ensure_price_list(currency)
	payment_modes = _ensure_payment_modes()
	customers = _ensure_customers()
	items = _ensure_items(
		item_count=item_count,
		disabled_count=min(disabled_count, item_count),
		stock_item_ratio=stock_item_ratio,
		price_list=price_list,
		currency=currency,
		warehouse=warehouse,
		can_seed_stock=bool(inventory_account),
	)
	tax_template = _ensure_tax_template(company)

	return {
		"status": "ok",
		"company": company,
		"currency": currency,
		"warehouse": warehouse,
		"price_list": price_list,
		"payment_modes": payment_modes,
		"customers": customers,
		"items_seeded": len(items),
		"sample_item_codes": [it["item_code"] for it in items[:5]],
		"tax_template": tax_template,
		"inventory_account": inventory_account,
		"warnings": []
		if inventory_account
		else [
			"No stock account found. Stock quantities were not seeded. "
			"Set Company.default_inventory_account or Warehouse.account and rerun."
		],
	}


def _get_company():
	company = frappe.defaults.get_user_default("Company")
	if company and frappe.db.exists("Company", company):
		return company

	company = frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("No Company found. Create one before seeding test data."))
	return company


def _get_company_currency(company):
	return frappe.db.get_value("Company", company, "default_currency") or "USD"


def _ensure_warehouse(company):
	existing = frappe.db.get_value(
		"Warehouse",
		{"warehouse_name": "POSNET Stores", "company": company},
		"name",
	)
	if existing:
		return existing

	doc = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": "POSNET Stores",
			"company": company,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_inventory_account(company, warehouse):
	"""Ensure company/warehouse has a stock account for stock entry posting."""
	account = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "account_type": "Stock"},
		"name",
	)
	if not account:
		account = _create_stock_account(company)
	if not account:
		return None

	if not frappe.db.get_value("Company", company, "default_inventory_account"):
		frappe.db.set_value("Company", company, "default_inventory_account", account)

	if not frappe.db.get_value("Warehouse", warehouse, "account"):
		frappe.db.set_value("Warehouse", warehouse, "account", account)

	return account


def _create_stock_account(company):
	"""Create a basic stock account if missing."""
	parent_account = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": "Asset"},
		"name",
	)
	if not parent_account:
		return None

	account_name = "Stock In Hand - Seed"
	account_number = frappe.db.get_value(
		"Account",
		{"company": company, "account_name": account_name},
		"name",
	)
	if account_number:
		return account_number

	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"company": company,
			"parent_account": parent_account,
			"is_group": 0,
			"root_type": "Asset",
			"account_type": "Stock",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_active_fiscal_year(company):
	"""Ensure current date is inside an active fiscal year for this company."""
	today = getdate(nowdate())

	# Reuse an existing fiscal year covering today, then link company if missing.
	fy_name = frappe.db.get_value(
		"Fiscal Year",
		{
			"disabled": 0,
			"year_start_date": ["<=", today],
			"year_end_date": [">=", today],
		},
		"name",
	)

	if fy_name:
		exists_for_company = frappe.db.exists(
			"Fiscal Year Company",
			{"parent": fy_name, "company": company},
		)
		if not exists_for_company:
			fy_doc = frappe.get_doc("Fiscal Year", fy_name)
			fy_doc.append("companies", {"company": company})
			fy_doc.save(ignore_permissions=True)
		return fy_name

	# If none exists for current date, create one and link company.
	year = today.year
	new_fy_name = f"{year}"
	if frappe.db.exists("Fiscal Year", new_fy_name):
		# Keep unique name if plain year already exists.
		new_fy_name = f"POSNET FY {year}"

	fy_doc = frappe.get_doc(
		{
			"doctype": "Fiscal Year",
			"year": new_fy_name,
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
			"companies": [{"company": company}],
		}
	)
	fy_doc.insert(ignore_permissions=True)
	return fy_doc.name


def _ensure_price_list(currency):
	if frappe.db.exists("Price List", "Standard Selling"):
		return "Standard Selling"

	doc = frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": "Standard Selling",
			"enabled": 1,
			"selling": 1,
			"buying": 0,
			"currency": currency,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_payment_modes():
	modes = []
	modes.append(_ensure_payment_mode("Cash", "Cash"))
	modes.append(_ensure_payment_mode("Card", "Bank"))
	return modes


def _ensure_payment_mode(name, mode_type):
	if frappe.db.exists("Mode of Payment", name):
		return name

	doc = frappe.get_doc(
		{
			"doctype": "Mode of Payment",
			"mode_of_payment": name,
			"type": mode_type,
			"enabled": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_customers():
	customer_group = frappe.db.exists("Customer Group", "Individual") or frappe.db.get_value(
		"Customer Group", {}, "name"
	)
	territory = frappe.db.exists("Territory", "All Territories") or frappe.db.get_value(
		"Territory", {}, "name"
	)

	if not customer_group or not territory:
		frappe.throw(_("Customer Group and Territory must exist before seeding customers."))

	customers = []
	customers.append(_ensure_customer("POSNET-WALK-IN", customer_group, territory))
	for idx in range(1, 6):
		customers.append(_ensure_customer(f"POSNET-CUST-{idx:03d}", customer_group, territory))
	return customers


def _ensure_customer(customer_name, customer_group, territory):
	if frappe.db.exists("Customer", customer_name):
		return customer_name

	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name,
			"customer_type": "Individual",
			"customer_group": customer_group,
			"territory": territory,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_items(
	item_count, disabled_count, stock_item_ratio, price_list, currency, warehouse, can_seed_stock=True
):
	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	if not item_group:
		frappe.throw(_("No leaf Item Group found. Create at least one Item Group."))

	seeded = []
	for idx in range(1, item_count + 1):
		item_code = f"POSNET-ITEM-{idx:03d}"
		item_name = f"POSNET Test Item {idx:03d}"
		is_stock_item = 1 if idx <= int(item_count * stock_item_ratio) else 0
		disabled = 1 if idx <= disabled_count else 0
		barcode = f"75012345{idx:04d}" if idx <= 2 else None
		price = round(random.uniform(1.5, 120.0), 2)
		target_qty = random.randint(10, 150) if is_stock_item else 0

		_ensure_item(
			item_code=item_code,
			item_name=item_name,
			item_group=item_group,
			is_stock_item=is_stock_item,
			disabled=disabled,
			barcode=barcode,
		)
		_ensure_item_price(item_code=item_code, price_list=price_list, currency=currency, rate=price)

		if is_stock_item and not disabled and can_seed_stock:
			_ensure_stock(item_code=item_code, warehouse=warehouse, target_qty=target_qty)

		seeded.append({"item_code": item_code, "is_stock_item": is_stock_item, "disabled": disabled})

	return seeded


def _ensure_item(item_code, item_name, item_group, is_stock_item, disabled, barcode=None):
	if frappe.db.exists("Item", item_code):
		doc = frappe.get_doc("Item", item_code)
		changed = False
		if cint(doc.disabled) != cint(disabled):
			doc.disabled = disabled
			changed = True

		if barcode:
			existing_codes = [b.barcode for b in (doc.barcodes or [])]
			if barcode not in existing_codes:
				doc.append("barcodes", {"barcode": barcode})
				changed = True

		if changed:
			try:
				doc.save(ignore_permissions=True)
			except Exception:
				# On reruns, existing submitted transactions can block certain item updates.
				# We intentionally keep seeding idempotent and non-destructive.
				frappe.db.rollback()
		return

	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_name,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": is_stock_item,
			"disabled": disabled,
			"description": f"Seeded test item for POSnet manual testing: {item_code}",
		}
	)
	if barcode:
		doc.append("barcodes", {"barcode": barcode})
	doc.insert(ignore_permissions=True)


def _ensure_item_price(item_code, price_list, currency, rate):
	existing_name = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list},
		"name",
	)
	if existing_name:
		frappe.db.set_value("Item Price", existing_name, "price_list_rate", flt(rate))
		return

	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": price_list,
			"currency": currency,
			"price_list_rate": flt(rate),
		}
	)
	doc.insert(ignore_permissions=True)


def _ensure_stock(item_code, warehouse, target_qty):
	try:
		current_qty = flt(get_stock_balance(item_code, warehouse))
	except Exception:
		current_qty = 0.0

	diff = flt(target_qty) - current_qty
	if diff > 0:
		stock_entry = make_stock_entry(
			item_code=item_code,
			qty=diff,
			to_warehouse=warehouse,
			rate=1,
			do_not_save=True,
		)
		stock_entry.insert(ignore_permissions=True)
		stock_entry.submit()


def _ensure_tax_template(company):
	existing = frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company},
		"name",
	)
	if existing:
		return existing

	account = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "account_type": "Tax"},
		"name",
	)
	if not account:
		account = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0, "root_type": "Liability"},
			"name",
		)
	if not account:
		return None

	try:
		doc = frappe.get_doc(
			{
				"doctype": "Sales Taxes and Charges Template",
				"title": "POSNET Standard Tax",
				"company": company,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account,
						"rate": 10.0,
						"description": "POSNET Seed Tax 10%",
					}
				],
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		return None


def cint(value):
	try:
		return int(value)
	except Exception:
		return 0
