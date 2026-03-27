"""
Seed deterministic mock data for Bazar dashboards.

Run with:
bench --site <site> execute erpnext.erpnext_integrations.ecommerce_api.seed_bazar_dashboard_mock_data.run
"""

from __future__ import annotations

import random
from datetime import timedelta

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from .seed_posnet_test_data import run as seed_posnet_base


MOCK_TAG = "MOCK_BAZAR_DASHBOARD"
RULE_PREFIX = "MOCK BAZAR PROMO"
COUPON_PREFIX = "MOCKBAZAR"


def run(
	anchor_date=None,
	days=60,
	invoices_per_day=6,
	item_count=120,
	regenerate=0,
	seed_value=13013,
):
	"""
	Prepopulate mock data used by bazar dashboards.

	Args:
		anchor_date (str|None): End date for generated window (defaults to today).
		days (int): Number of historical days to generate.
		invoices_per_day (int): Number of invoices per day.
		item_count (int): Minimum POSNET seeded item count.
		regenerate (int|bool): If true, clear existing mock-tagged records first.
		seed_value (int): RNG seed for deterministic output.
	"""
	days = int(days)
	invoices_per_day = int(invoices_per_day)
	item_count = int(item_count)
	regenerate = int(regenerate)
	seed_value = int(seed_value)

	if days < 7:
		frappe.throw("days should be >= 7")
	if invoices_per_day < 1:
		frappe.throw("invoices_per_day should be >= 1")

	rng = random.Random(seed_value)
	anchor = getdate(anchor_date) if anchor_date else getdate(nowdate())

	if regenerate:
		clear_mock_data()

	# Ensure baseline master data exists (items/customers/payment modes/warehouse).
	try:
		base = seed_posnet_base(item_count=item_count, disabled_count=6, stock_item_ratio=0.85)
	except Exception:
		# Some sites have pre-existing POSNET items with incompatible stock flags.
		# Keep dashboard seeding resilient by falling back to current site defaults.
		frappe.db.rollback()
		base = _fallback_base_context()
		frappe.log_error(
			title=f"{MOCK_TAG}:base_seed_fallback",
			message="seed_posnet_base failed; using fallback site context.",
		)
	company = base["company"]
	warehouse = base["warehouse"]

	_ensure_additional_payment_modes()
	promo_meta = _ensure_promotions()
	customers = _get_seed_customers()
	items = _get_seed_items(limit=item_count)
	if not items:
		frappe.throw("No active stock items found for mock seeding.")

	created_sales = 0
	failed_sales = 0

	for day_offset in range(days):
		posting_date = add_days(anchor, -day_offset)
		for serial in range(invoices_per_day):
			res = _create_mock_sales_invoice(
				company=company,
				warehouse=warehouse,
				posting_date=posting_date,
				serial=serial,
				customers=customers,
				items=items,
				promo_meta=promo_meta,
				rng=rng,
			)
			if res:
				created_sales += 1
			else:
				failed_sales += 1

	price_events = _seed_price_history_events(items=items[:20], anchor=anchor, rng=rng)
	receiving_events = _seed_receiving_queue_markers(anchor=anchor)
	frappe.db.commit()

	return {
		"status": "ok",
		"tag": MOCK_TAG,
		"window": {"anchor_date": str(anchor), "days": days},
		"created_sales_invoices": created_sales,
		"failed_sales_invoices": failed_sales,
		"price_history_events": price_events,
		"receiving_queue_markers": receiving_events,
		"promotions": promo_meta,
	}


def clear_mock_data():
	"""Remove records created by this seeder."""
	removed = {
		"sales_invoices": 0,
		"pricing_rules": 0,
		"coupon_codes": 0,
		"error_logs": 0,
	}

	# Cancel + delete mock sales invoices.
	sales = frappe.get_all(
		"Sales Invoice",
		filters={"remarks": ["like", f"%{MOCK_TAG}%"]},
		fields=["name", "docstatus"],
		limit_page_length=0,
	)
	for row in sales:
		try:
			doc = frappe.get_doc("Sales Invoice", row.name)
			if doc.docstatus == 1:
				doc.cancel()
			doc.delete(ignore_permissions=True, force=True)
			removed["sales_invoices"] += 1
		except Exception:
			# Keep cleanup resilient even if one doc fails.
			frappe.db.rollback()

	rules = frappe.get_all(
		"Pricing Rule",
		filters={"title": ["like", f"{RULE_PREFIX}%"]},
		fields=["name"],
		limit_page_length=0,
	)
	for row in rules:
		try:
			doc = frappe.get_doc("Pricing Rule", row.name)
			if doc.docstatus == 1:
				doc.cancel()
			doc.delete(ignore_permissions=True, force=True)
			removed["pricing_rules"] += 1
		except Exception:
			frappe.db.rollback()

	coupons = frappe.get_all(
		"Coupon Code",
		filters={"coupon_code": ["like", f"{COUPON_PREFIX}-%"]},
		fields=["name"],
		limit_page_length=0,
	)
	for row in coupons:
		try:
			doc = frappe.get_doc("Coupon Code", row.name)
			doc.delete(ignore_permissions=True, force=True)
			removed["coupon_codes"] += 1
		except Exception:
			frappe.db.rollback()

	logs = frappe.get_all(
		"Error Log",
		filters={"method": ["like", f"{MOCK_TAG}:%"]},
		fields=["name"],
		limit_page_length=0,
	)
	for row in logs:
		frappe.db.delete("Error Log", {"name": row.name})
		removed["error_logs"] += 1

	frappe.db.commit()
	return {"status": "ok", "removed": removed, "tag": MOCK_TAG}


def _ensure_additional_payment_modes():
	for mode_name, mode_type in [("Mobile Money", "Bank"), ("Transfer", "Bank")]:
		if frappe.db.exists("Mode of Payment", mode_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": mode_name,
				"type": mode_type,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def _ensure_promotions():
	today = getdate(nowdate())
	start = add_days(today, -30)
	end = add_days(today, 30)
	rules = []

	rules.append(
		_ensure_pricing_rule(
			title=f"{RULE_PREFIX} - GENERAL 10",
			discount_percentage=10,
			valid_from=start,
			valid_upto=end,
		)
	)
	rules.append(
		_ensure_pricing_rule(
			title=f"{RULE_PREFIX} - WEEKEND 15",
			discount_percentage=15,
			valid_from=start,
			valid_upto=end,
		)
	)

	coupon = _ensure_coupon_code(rules[0], f"{COUPON_PREFIX}-10")
	return {"pricing_rules": rules, "coupon_code": coupon}


def _ensure_pricing_rule(title, discount_percentage, valid_from, valid_upto):
	existing = frappe.db.get_value("Pricing Rule", {"title": title}, "name")
	if existing:
		return existing

	doc = frappe.get_doc(
		{
			"doctype": "Pricing Rule",
			"title": title,
			"disable": 0,
			"selling": 1,
			"apply_on": "Transaction",
			"price_or_product_discount": "Price",
			"rate_or_discount": "Discount Percentage",
			"discount_percentage": flt(discount_percentage),
			"valid_from": valid_from,
			"valid_upto": valid_upto,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_coupon_code(pricing_rule, coupon_code):
	existing = frappe.db.get_value("Coupon Code", {"coupon_code": coupon_code}, "name")
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Coupon Code",
			"coupon_name": f"{COUPON_PREFIX} Campaign",
			"coupon_code": coupon_code,
			"pricing_rule": pricing_rule,
			"maximum_use": 5000,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _get_seed_customers():
	customers = frappe.get_all(
		"Customer",
		filters={"name": ["like", "POSNET-CUST-%"]},
		fields=["name"],
		limit_page_length=0,
	)
	if customers:
		return [c.name for c in customers]
	fallback = frappe.db.get_value("Customer", {}, "name")
	return [fallback] if fallback else []


def _get_seed_items(limit=120):
	return frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_stock_item": 1},
		fields=["item_code", "item_name"],
		order_by="item_code asc",
		limit_page_length=int(limit),
	)


def _create_mock_sales_invoice(
	company, warehouse, posting_date, serial, customers, items, promo_meta, rng
):
	if not customers:
		return None

	account_mode = "white" if rng.random() < 0.65 else "black"
	promo_type = _choose_promotion_type(rng)
	payment = _choose_payment_method(account_mode, rng)
	customer = rng.choice(customers)
	line_count = rng.randint(1, 4)
	selected = rng.sample(items, min(line_count, len(items)))
	campaign = "clearance" if rng.random() < 0.25 else "weekly"
	container_id = f"CONT-{(serial % 5) + 1:03d}"

	invoice = frappe.new_doc("Sales Invoice")
	invoice.company = company
	invoice.customer = customer
	invoice.set_posting_time = 1
	invoice.posting_date = posting_date
	invoice.posting_time = f"{8 + (serial % 12):02d}:{(serial * 7) % 60:02d}:00"
	invoice.due_date = posting_date
	invoice.ignore_default_payment_terms_template = 1
	invoice.payment_terms_template = None
	invoice.set("payment_schedule", [])
	invoice.update_stock = 0

	for row in selected:
		base_rate = _get_rate(row.item_code)
		qty = rng.randint(1, 6)
		item_row = {
			"item_code": row.item_code,
			"qty": qty,
			"rate": base_rate,
			"warehouse": warehouse,
		}
		if promo_type == "manual_discount":
			item_row["discount_percentage"] = 12 if account_mode == "white" else 8
		invoice.append("items", item_row)

	if promo_type == "campaign":
		invoice.additional_discount_percentage = 7
		invoice.apply_discount_on = "Net Total"
	elif promo_type == "coupon":
		invoice.coupon_code = f"{COUPON_PREFIX}-10"

	invoice.remarks = (
		f"{MOCK_TAG}|account_mode:{account_mode}|promotion_type:{promo_type}|"
		f"campaign:{campaign}|container:{container_id}|payment:{payment}"
	)
	_set_if_exists(invoice, ["custom_source_tag", "source_tag"], MOCK_TAG)
	_set_if_exists(
		invoice,
		["custom_account_mode", "custom_transaction_account_mode"],
		account_mode,
	)
	_set_if_exists(invoice, ["custom_promotion_type"], promo_type)
	_set_if_exists(invoice, ["custom_source_container_id"], container_id)

	try:
		invoice.insert(ignore_permissions=True)
		invoice.submit()
		return invoice.name
	except Exception:
		frappe.log_error(
			title=f"{MOCK_TAG}:seed_sales_invoice",
			message=f"Failed for posting_date={posting_date}, promo={promo_type}",
		)
		frappe.db.rollback()
		return None


def _seed_price_history_events(items, anchor, rng):
	created = 0
	for row in items:
		rate_now = _get_rate(row.item_code)
		if rate_now <= 0:
			continue
		old_rate = flt(rate_now * rng.uniform(0.72, 0.93), 2)
		event_date = add_days(anchor, -rng.randint(15, 55))
		existing = frappe.db.exists(
			"Item Price",
			{
				"item_code": row.item_code,
				"price_list": "Standard Selling",
				"valid_from": event_date,
			},
		)
		if existing:
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": row.item_code,
				"price_list": "Standard Selling",
				"currency": _get_company_currency(),
				"price_list_rate": old_rate,
				"valid_from": event_date,
			}
		)
		try:
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception:
			frappe.db.rollback()
	return created


def _seed_receiving_queue_markers(anchor):
	"""
	Create lightweight markers to emulate receiving queue/offline sync states.
	Uses Error Log so it works without requiring custom doctypes in every site.
	"""
	statuses = ["draft", "queued", "failed", "committed"]
	created = 0
	for idx, status in enumerate(statuses, start=1):
		method = f"{MOCK_TAG}:receiving:{status}:{idx:02d}"
		if frappe.db.exists("Error Log", {"method": method}):
			continue
		frappe.get_doc(
			{
				"doctype": "Error Log",
				"method": method,
				"error": (
					f"status={status}\n"
					f"session_id=MOCK-REC-{idx:03d}\n"
					f"attempts={idx}\n"
					f"created_at={anchor - timedelta(days=idx)}"
				),
			}
		).insert(ignore_permissions=True)
		created += 1
	return created


def _choose_promotion_type(rng):
	r = rng.random()
	if r < 0.45:
		return "none"
	if r < 0.63:
		return "manual_discount"
	if r < 0.78:
		return "coupon"
	if r < 0.9:
		return "bundle"
	return "campaign"


def _choose_payment_method(account_mode, rng):
	if account_mode == "black":
		return rng.choice(["Cash", "Mobile Money"])
	return rng.choice(["Cash", "Card", "Transfer"])


def _get_rate(item_code):
	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": "Standard Selling"},
		"price_list_rate",
		order_by="valid_from desc",
	)
	if rate:
		return flt(rate)
	return flt(frappe.db.get_value("Item", item_code, "standard_rate") or 0)


def _get_company_currency():
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	return frappe.db.get_value("Company", company, "default_currency") or "USD"


def _fallback_base_context():
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw("No Company found to seed mock dashboard data.")

	warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")
	if not warehouse:
		warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
	if not warehouse:
		frappe.throw("No leaf Warehouse found to seed mock dashboard data.")

	return {"company": company, "warehouse": warehouse}


def _set_if_exists(doc, field_candidates, value):
	for fieldname in field_candidates:
		if doc.meta.has_field(fieldname):
			doc.set(fieldname, value)
			return

