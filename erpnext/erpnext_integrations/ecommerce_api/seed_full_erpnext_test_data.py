"""
Seed a broader ERPNext test dataset from CSV files.

Run with:
bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.seed_full_erpnext_test_data.run
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, add_months, flt, get_bench_path, getdate, now_datetime, nowdate

from .seed_posnet_test_data import run as seed_posnet_base


def run(item_count=60, disabled_count=5, stock_item_ratio=0.75):
	"""Seed baseline POS data + broader ERP test fixtures from CSV."""
	result = {
		"status": "ok",
		"base_seed": {},
		"created": {},
		"warnings": [],
	}

	# 1) Base seed (company defaults, warehouse, items, payment modes, customers)
	base = seed_posnet_base(
		item_count=item_count,
		disabled_count=disabled_count,
		stock_item_ratio=stock_item_ratio,
	)
	result["base_seed"] = base
	result["warnings"].extend(base.get("warnings", []))

	# 2) CSV-driven seed
	base_dir = os.path.join(os.path.dirname(__file__), "seed_data", "erpnext_full")

	company = base.get("company") or _get_company()
	warehouse = base.get("warehouse")
	price_list = base.get("price_list", "Standard Selling")
	currency = base.get("currency", "USD")

	_ensure_active_fiscal_year(company, result["warnings"])

	result["created"]["supplier_groups"] = _seed_supplier_groups(base_dir)
	result["created"]["companies_checked"] = _check_companies(base_dir, result["warnings"])
	result["created"]["warehouses"] = _seed_warehouses(base_dir, company)
	result["created"]["suppliers"] = _seed_suppliers(base_dir)
	result["created"]["customer_groups"] = _seed_customer_groups(base_dir)
	result["created"]["customers"] = _seed_customers(base_dir)
	result["created"]["item_groups"] = _seed_item_groups(base_dir)
	result["created"]["items"] = _seed_items(base_dir)
	result["created"]["item_prices"] = _seed_item_prices(base_dir, price_list, currency)
	result["created"]["support_issues"] = _seed_support_issues(base_dir, result["warnings"])
	result["created"]["opportunities"] = _seed_opportunities(base_dir, result["warnings"])
	result["created"]["sales_invoices"] = _seed_sales_invoices(
		base_dir, company, warehouse, result["warnings"]
	)
	result["created"]["sales_invoice_history"] = _seed_sales_invoice_history(
		base_dir, company, warehouse, result["warnings"], months_back=6
	)
	result["created"]["purchase_invoices"] = _seed_purchase_invoices(
		base_dir, company, warehouse, result["warnings"]
	)
	result["created"]["submitted_seed_sales_invoices"] = _submit_seed_invoices(
		doctype="Sales Invoice",
		seed_pattern="SEED-SINV-%",
		warnings=result["warnings"],
	)
	result["created"]["submitted_seed_purchase_invoices"] = _submit_seed_invoices(
		doctype="Purchase Invoice",
		seed_pattern="SEED-PINV-%",
		warnings=result["warnings"],
	)
	result["created"]["payments"] = _seed_payments_for_seed_invoices(result["warnings"])
	result["created"]["error_logs"] = _seed_error_logs()
	result["created"]["integration_requests"] = _seed_integration_requests()
	result["created"]["scheduled_job_logs"] = _seed_scheduled_job_logs()
	result["created"]["practice_log_files"] = _seed_practice_log_files()

	result["created"]["leads"] = _seed_leads(base_dir, result["warnings"])
	result["created"]["quotations"] = _seed_quotations(base_dir, company, result["warnings"])
	result["created"]["sales_orders"] = _seed_sales_orders(base_dir, company, result["warnings"])
	result["created"]["purchase_orders"] = _seed_purchase_orders(base_dir, company, result["warnings"])
	result["created"]["material_requests"] = _seed_material_requests(base_dir, company, result["warnings"])
	result["created"]["boms"] = _seed_boms(base_dir, company, result["warnings"])
	result["created"]["work_orders"] = _seed_work_orders(base_dir, company, result["warnings"])
	result["created"]["projects"] = _seed_projects(base_dir, result["warnings"])
	result["created"]["tasks"] = _seed_tasks(base_dir, result["warnings"])
	result["created"]["journal_entries"] = _seed_journal_entries(company, result["warnings"])

	return result


def _csv_rows(base_dir, filename):
	path = os.path.join(base_dir, filename)
	if not os.path.exists(path):
		return []
	with open(path, newline="", encoding="utf-8") as f:
		return list(csv.DictReader(f))


def _get_company():
	company = frappe.defaults.get_user_default("Company")
	if company and frappe.db.exists("Company", company):
		return company
	return frappe.db.get_value("Company", {}, "name")


def _ensure_active_fiscal_year(company, warnings):
	today = getdate(nowdate())
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
		if not frappe.db.exists("Fiscal Year Company", {"parent": fy_name, "company": company}):
			fy_doc = frappe.get_doc("Fiscal Year", fy_name)
			fy_doc.append("companies", {"company": company})
			fy_doc.save(ignore_permissions=True)
		return

	year = today.year
	fy = frappe.get_doc(
		{
			"doctype": "Fiscal Year",
			"year": f"SEED FY {year}",
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
			"companies": [{"company": company}],
		}
	)
	try:
		fy.insert(ignore_permissions=True)
	except Exception as exc:
		warnings.append(f"Fiscal Year create skipped: {exc}")


def _seed_supplier_groups(base_dir):
	created = 0
	for row in _csv_rows(base_dir, "supplier_groups.csv"):
		name = row.get("name", "").strip()
		if not name or frappe.db.exists("Supplier Group", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Supplier Group",
				"supplier_group_name": name,
				"parent_supplier_group": row.get("parent_supplier_group") or "All Supplier Groups",
				"is_group": int(row.get("is_group") or 0),
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _check_companies(base_dir, warnings):
	"""Validate companies listed in CSV exist. We do not auto-create companies."""
	checked = 0
	for row in _csv_rows(base_dir, "companies.csv"):
		company_name = (row.get("company_name") or "").strip()
		if not company_name:
			continue
		checked += 1
		if not frappe.db.exists("Company", company_name):
			warnings.append(
				f"Company not found: {company_name}. Create it manually via setup wizard."
			)
	return checked


def _seed_warehouses(base_dir, fallback_company):
	created = 0
	for row in _csv_rows(base_dir, "warehouses.csv"):
		warehouse_name = (row.get("warehouse_name") or "").strip()
		if not warehouse_name:
			continue
		company = (row.get("company") or fallback_company or "").strip()
		if not company or not frappe.db.exists("Company", company):
			continue

		existing = frappe.db.get_value(
			"Warehouse",
			{"warehouse_name": warehouse_name, "company": company},
			"name",
		)
		if existing:
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": warehouse_name,
				"company": company,
				"is_group": int(row.get("is_group") or 0),
				"parent_warehouse": row.get("parent_warehouse") or None,
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_suppliers(base_dir):
	created = 0
	for row in _csv_rows(base_dir, "suppliers.csv"):
		name = row.get("supplier_name", "").strip()
		if not name or frappe.db.exists("Supplier", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": name,
				"supplier_group": row.get("supplier_group") or "Services",
				"supplier_type": row.get("supplier_type") or "Company",
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_customer_groups(base_dir):
	created = 0
	for row in _csv_rows(base_dir, "customer_groups.csv"):
		name = row.get("name", "").strip()
		if not name or frappe.db.exists("Customer Group", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": name,
				"parent_customer_group": row.get("parent_customer_group") or "All Customer Groups",
				"is_group": int(row.get("is_group") or 0),
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_customers(base_dir):
	created = 0
	territory = frappe.db.exists("Territory", "All Territories") or frappe.db.get_value(
		"Territory", {}, "name"
	)
	for row in _csv_rows(base_dir, "customers.csv"):
		name = row.get("customer_name", "").strip()
		if not name or frappe.db.exists("Customer", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_type": row.get("customer_type") or "Individual",
				"customer_group": row.get("customer_group") or "Individual",
				"territory": row.get("territory") or territory,
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_item_groups(base_dir):
	created = 0
	for row in _csv_rows(base_dir, "item_groups.csv"):
		name = row.get("name", "").strip()
		if not name or frappe.db.exists("Item Group", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": row.get("parent_item_group") or "All Item Groups",
				"is_group": int(row.get("is_group") or 0),
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_items(base_dir):
	created = 0
	for row in _csv_rows(base_dir, "items.csv"):
		item_code = row.get("item_code", "").strip()
		if not item_code:
			continue

		if frappe.db.exists("Item", item_code):
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": row.get("item_name") or item_code,
				"item_group": row.get("item_group") or "Products",
				"stock_uom": row.get("stock_uom") or "Nos",
				"is_stock_item": int(row.get("is_stock_item") or 0),
				"disabled": int(row.get("disabled") or 0),
				"description": row.get("description") or f"Seeded item {item_code}",
			}
		)
		barcode = (row.get("barcode") or "").strip()
		if barcode:
			doc.append("barcodes", {"barcode": barcode})
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_item_prices(base_dir, default_price_list, default_currency):
	created = 0
	for row in _csv_rows(base_dir, "item_prices.csv"):
		item_code = row.get("item_code", "").strip()
		if not item_code:
			continue
		if not frappe.db.exists("Item", item_code):
			continue

		price_list = row.get("price_list") or default_price_list
		existing = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list},
			"name",
		)
		if existing:
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"currency": row.get("currency") or default_currency,
				"price_list_rate": flt(row.get("price_list_rate") or 0),
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_support_issues(base_dir, warnings):
	created = 0
	for row in _csv_rows(base_dir, "support_issues.csv"):
		subject = row.get("subject", "").strip()
		if not subject:
			continue
		if frappe.db.exists("Issue", {"subject": subject}):
			continue
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Issue",
					"subject": subject,
					"status": row.get("status") or "Open",
					"priority": row.get("priority") or "Medium",
					"raised_by": row.get("raised_by") or "Administrator",
					"description": row.get("description") or "Seeded support issue",
				}
			)
			customer = (row.get("customer") or "").strip()
			if customer and frappe.db.exists("Customer", customer):
				doc.customer = customer
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception as exc:
			warnings.append(f"Issue seed skipped ({subject}): {exc}")
	return created


def _seed_opportunities(base_dir, warnings):
	created = 0
	for row in _csv_rows(base_dir, "opportunities.csv"):
		title = row.get("title", "").strip()
		customer = row.get("customer", "").strip()
		if not title or not customer:
			continue
		if not frappe.db.exists("Customer", customer):
			continue
		if frappe.db.exists("Opportunity", {"title": title, "party_name": customer}):
			continue
		try:
			source = (row.get("source") or "").strip()
			if source and not frappe.db.exists("Lead Source", source):
				source = None
			doc = frappe.get_doc(
				{
					"doctype": "Opportunity",
					"title": title,
					"opportunity_from": "Customer",
					"party_name": customer,
					"status": row.get("status") or "Open",
					"source": source,
				}
			)
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception as exc:
			warnings.append(f"Opportunity seed skipped ({title}): {exc}")
	return created


def _seed_sales_invoices(base_dir, company, warehouse, warnings):
	created = 0
	for row in _csv_rows(base_dir, "sales_invoices.csv"):
		seed_key = row.get("seed_key", "").strip()
		customer = row.get("customer", "").strip()
		item_code = row.get("item_code", "").strip()
		if not seed_key or not customer or not item_code:
			continue
		if frappe.db.exists("Sales Invoice", {"remarks": seed_key}):
			continue
		if not frappe.db.exists("Customer", customer) or not frappe.db.exists("Item", item_code):
			continue
		try:
			doc = frappe.new_doc("Sales Invoice")
			doc.company = company
			doc.customer = customer
			posting_date = nowdate()
			doc.posting_date = posting_date
			doc.due_date = add_days(posting_date, 7)
			doc.remarks = seed_key
			doc.update_stock = 0
			doc.append(
				"items",
				{
					"item_code": item_code,
					"qty": flt(row.get("qty") or 1),
					"rate": flt(row.get("rate") or 0),
					"warehouse": row.get("warehouse") or warehouse,
				},
			)
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as submit_exc:
				warnings.append(f"Sales Invoice submit skipped ({seed_key}): {submit_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Sales Invoice seed skipped ({seed_key}): {exc}")
	return created


def _seed_purchase_invoices(base_dir, company, warehouse, warnings):
	created = 0
	for row in _csv_rows(base_dir, "purchase_invoices.csv"):
		seed_key = row.get("seed_key", "").strip()
		supplier = row.get("supplier", "").strip()
		item_code = row.get("item_code", "").strip()
		if not seed_key or not supplier or not item_code:
			continue
		if frappe.db.exists("Purchase Invoice", {"remarks": seed_key}):
			continue
		if not frappe.db.exists("Supplier", supplier) or not frappe.db.exists("Item", item_code):
			continue
		try:
			doc = frappe.new_doc("Purchase Invoice")
			doc.company = company
			doc.supplier = supplier
			posting_date = nowdate()
			doc.posting_date = posting_date
			doc.due_date = add_days(posting_date, 7)
			doc.bill_no = row.get("bill_no") or seed_key
			doc.remarks = seed_key
			doc.update_stock = 0
			doc.append(
				"items",
				{
					"item_code": item_code,
					"qty": flt(row.get("qty") or 1),
					"rate": flt(row.get("rate") or 0),
					"warehouse": row.get("warehouse") or warehouse,
				},
			)
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as submit_exc:
				warnings.append(f"Purchase Invoice submit skipped ({seed_key}): {submit_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Purchase Invoice seed skipped ({seed_key}): {exc}")
	return created


def _seed_sales_invoice_history(base_dir, company, warehouse, warnings, months_back=6):
	"""
	Seed submitted historical Sales Invoices across multiple months so MoM queries
	in SQL practice labs always have data points.
	"""
	created = 0
	template_rows = _csv_rows(base_dir, "sales_invoices.csv")
	if not template_rows:
		return created

	for month_offset in range(months_back, 0, -1):
		posting_date = add_months(nowdate(), -month_offset)
		due_date = add_days(posting_date, 7)
		# Older months have lower totals; recent months higher totals.
		month_factor = 0.65 + ((months_back - month_offset + 1) * 0.1)
		month_key = getdate(posting_date).strftime("%Y%m")

		for i, row in enumerate(template_rows, start=1):
			customer = (row.get("customer") or "").strip()
			item_code = (row.get("item_code") or "").strip()
			if not customer or not item_code:
				continue
			if not frappe.db.exists("Customer", customer) or not frappe.db.exists("Item", item_code):
				continue

			seed_key = f"SEED-SINV-MONTH-{month_key}-{i:03d}"
			if frappe.db.exists("Sales Invoice", {"remarks": seed_key}):
				continue

			try:
				base_qty = flt(row.get("qty") or 1)
				base_rate = flt(row.get("rate") or 0)
				scaled_qty = max(1, int(round(base_qty * (0.85 + month_factor / 3))))
				scaled_rate = flt(base_rate * month_factor, 2)

				doc = frappe.new_doc("Sales Invoice")
				doc.company = company
				doc.customer = customer
				doc.set_posting_time = 1
				doc.posting_date = posting_date
				doc.posting_time = "12:00:00"
				doc.due_date = due_date
				doc.ignore_default_payment_terms_template = 1
				doc.payment_terms_template = None
				doc.set("payment_schedule", [])
				doc.remarks = seed_key
				doc.update_stock = 0
				doc.append(
					"items",
					{
						"item_code": item_code,
						"qty": scaled_qty,
						"rate": scaled_rate,
						"warehouse": row.get("warehouse") or warehouse,
					},
				)
				doc.insert(ignore_permissions=True)
				doc.submit()
				created += 1
			except Exception as exc:
				warnings.append(f"Sales Invoice history seed skipped ({seed_key}): {exc}")

	return created


def _submit_seed_invoices(doctype, seed_pattern, warnings):
	submitted = 0
	rows = frappe.get_all(
		doctype,
		filters={"remarks": ["like", seed_pattern], "docstatus": 0},
		fields=["name"],
	)
	for row in rows:
		try:
			doc = frappe.get_doc(doctype, row.name)
			doc.submit()
			submitted += 1
		except Exception as exc:
			warnings.append(f"{doctype} submit skipped ({row.name}): {exc}")
	return submitted


def _seed_payments_for_seed_invoices(warnings):
	created = 0
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={
			"remarks": ["like", "SEED-SINV-%"],
			"docstatus": 1,
			"outstanding_amount": [">", 0],
		},
		fields=["name", "customer", "outstanding_amount", "company", "posting_date"],
	)

	for inv in invoices:
		existing = frappe.db.exists(
			"Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": inv.name},
		)
		if existing:
			continue
		try:
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			pe = get_payment_entry("Sales Invoice", inv.name)
			pe.posting_date = nowdate()
			pe.mode_of_payment = "Cash" if frappe.db.exists("Mode of Payment", "Cash") else None
			pe.reference_no = f"SEED-PAY-{inv.name}"
			pe.reference_date = nowdate()
			pe.paid_amount = flt(inv.outstanding_amount)
			pe.received_amount = flt(inv.outstanding_amount)
			pe.insert(ignore_permissions=True)
			pe.submit()
			created += 1
		except Exception as exc:
			warnings.append(f"Payment seed skipped ({inv.name}): {exc}")
	return created


def _seed_error_logs():
	created = 0
	for i in range(1, 6):
		method = f"LAB-ERROR-{i:03d}"
		exists = frappe.db.exists("Error Log", {"method": method})
		if exists:
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Error Log",
				"method": method,
				"error": f"Traceback (most recent call last):\\nException: Sample lab error {i}",
				"reference_doctype": "Sales Invoice",
				"reference_name": f"SEED-SINV-{i:03d}",
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_integration_requests():
	created = 0
	for i in range(1, 6):
		request_id = f"LAB-INT-{i:03d}"
		exists = frappe.db.exists("Integration Request", {"request_id": request_id})
		if exists:
			continue
		status = "Failed" if i % 2 == 0 else "Completed"
		doc = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"request_id": request_id,
				"integration_request_service": "POSnet Lab Service",
				"status": status,
				"request_description": f"Sample integration request {i}",
				"url": f"https://api.example.com/posnet/{i}",
				"data": '{"sample":"request"}',
				"output": '{"sample":"response"}' if status == "Completed" else "",
				"error": "Sample failed integration payload" if status == "Failed" else "",
				"is_remote_request": 1,
				"reference_doctype": "Sales Invoice",
				"reference_docname": frappe.db.get_value(
					"Sales Invoice", {"remarks": ["like", "SEED-SINV-%"]}, "name"
				),
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_scheduled_job_logs():
	created = 0
	sjt = frappe.db.get_value("Scheduled Job Type", {}, "name")
	if not sjt:
		return created

	for i, status in enumerate(["Complete", "Failed", "Complete", "Failed", "Complete"], start=1):
		details_marker = f"LAB-SJOB-{i:03d}"
		exists = frappe.db.exists("Scheduled Job Log", {"details": ["like", f"%{details_marker}%"]})
		if exists:
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Scheduled Job Log",
				"scheduled_job_type": sjt,
				"status": status,
				"details": f"{details_marker} simulated scheduled task output",
				"debug_log": "Simulated debug stack for lab grep practice",
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1
	return created


def _seed_practice_log_files():
	bench_logs = os.path.join(get_bench_path(), "logs")
	os.makedirs(bench_logs, exist_ok=True)

	now = now_datetime()
	files = {
		"lab_practice_web.log": [
			f"{_ts(now)} INFO /api/method/erpnext.erpnext_integrations.ecommerce_api.api.get_products",
			f"{_ts(now + timedelta(seconds=1))} ERROR Traceback (most recent call last): ValidationError in Sales Invoice submit",
			f"{_ts(now + timedelta(seconds=2))} WARNING Request timeout while calling Payment Entry endpoint",
		],
		"lab_practice_worker.log": [
			f"{_ts(now)} INFO Job queued: sync_pending_orders",
			f"{_ts(now + timedelta(seconds=2))} ERROR Job failed: Duplicate offline_order_uuid detected",
			f"{_ts(now + timedelta(seconds=3))} ERROR Exception: Deadlock found when trying to get lock",
		],
		"lab_practice_scheduler.log": [
			f"{_ts(now)} INFO Scheduler tick",
			f"{_ts(now + timedelta(seconds=5))} ERROR Scheduled job failed: stock_reconcile_nightly",
			f"{_ts(now + timedelta(seconds=6))} INFO Retry scheduled in 300 seconds",
		],
	}

	written = 0
	for filename, lines in files.items():
		path = os.path.join(bench_logs, filename)
		with open(path, "w", encoding="utf-8") as f:
			f.write("\n".join(lines) + "\n")
		written += 1

	# Compatibility file for labs that still reference logs/web.log.
	# Do not overwrite if a real runtime web.log already exists.
	compat_web_log = os.path.join(bench_logs, "web.log")
	if not os.path.exists(compat_web_log):
		with open(compat_web_log, "w", encoding="utf-8") as f:
			f.write("\n".join(files["lab_practice_web.log"]) + "\n")
		written += 1
	return written


def _ensure_lead_sources(sources):
	"""Create missing Lead Source records so leads can reference them."""
	for source in sources:
		if source and not frappe.db.exists("Lead Source", source):
			try:
				frappe.get_doc({"doctype": "Lead Source", "source_name": source}).insert(
					ignore_permissions=True
				)
			except Exception:
				pass  # might already exist under a different name lookup


def _seed_leads(base_dir, warnings):
	# Ensure all referenced sources exist before inserting leads
	rows = _csv_rows(base_dir, "leads.csv")
	_ensure_lead_sources({r.get("source", "").strip() for r in rows if r.get("source")})

	created = 0
	for row in rows:
		name = row.get("lead_name", "").strip()
		if not name:
			continue
		if frappe.db.exists("CRM Lead", {"lead_name": name}) or frappe.db.exists("Lead", {"lead_name": name}):
			continue
		source = row.get("source", "").strip()
		try:
			doc = frappe.get_doc({
				"doctype": "CRM Lead",
				"lead_name": name,
				"company_name": row.get("company_name") or "",
				"email": row.get("email") or "",
				"mobile_no": row.get("mobile_no") or "",
				"status": row.get("status") or "Open",
				"source": source,
			})
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception:
			# Try legacy Lead doctype
			try:
				doc = frappe.get_doc({
					"doctype": "Lead",
					"lead_name": name,
					"company_name": row.get("company_name") or "",
					"email_id": row.get("email") or "",
					"mobile_no": row.get("mobile_no") or "",
					"status": row.get("status") or "Open",
					"source": source,
				})
				doc.insert(ignore_permissions=True)
				created += 1
			except Exception as exc2:
				warnings.append(f"Lead seed skipped ({name}): {exc2}")
	return created


def _seed_quotations(base_dir, company, warnings):
	created = 0
	for row in _csv_rows(base_dir, "quotations.csv"):
		seed_key = row.get("seed_key", "").strip()
		customer = row.get("customer", "").strip()
		item_code = row.get("item_code", "").strip()
		if not seed_key or not customer or not item_code:
			continue
		if frappe.db.exists("Quotation", {"terms": seed_key}):
			continue
		if not frappe.db.exists("Customer", customer) or not frappe.db.exists("Item", item_code):
			continue
		try:
			doc = frappe.get_doc({
				"doctype": "Quotation",
				"quotation_to": "Customer",
				"party_name": customer,
				"company": company,
				"transaction_date": nowdate(),
				"valid_till": add_days(nowdate(), 30),
				"terms": seed_key,
				"items": [{
					"item_code": item_code,
					"qty": flt(row.get("qty") or 1),
					"rate": flt(row.get("rate") or 0),
				}],
			})
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception as exc:
			warnings.append(f"Quotation seed skipped ({seed_key}): {exc}")
	return created


def _seed_sales_orders(base_dir, company, warnings):
	created = 0
	for row in _csv_rows(base_dir, "sales_orders.csv"):
		seed_key = row.get("seed_key", "").strip()
		customer = row.get("customer", "").strip()
		item_code = row.get("item_code", "").strip()
		if not seed_key or not customer or not item_code:
			continue
		if frappe.db.exists("Sales Order", {"terms": seed_key}):
			continue
		if not frappe.db.exists("Customer", customer) or not frappe.db.exists("Item", item_code):
			continue
		try:
			delivery_date = add_days(nowdate(), int(row.get("delivery_days") or 7))
			doc = frappe.get_doc({
				"doctype": "Sales Order",
				"customer": customer,
				"company": company,
				"transaction_date": nowdate(),
				"delivery_date": delivery_date,
				"terms": seed_key,
				"items": [{
					"item_code": item_code,
					"qty": flt(row.get("qty") or 1),
					"rate": flt(row.get("rate") or 0),
					"delivery_date": delivery_date,
				}],
			})
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as sub_exc:
				warnings.append(f"Sales Order submit skipped ({seed_key}): {sub_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Sales Order seed skipped ({seed_key}): {exc}")
	return created


def _seed_purchase_orders(base_dir, company, warnings):
	created = 0
	for row in _csv_rows(base_dir, "purchase_orders.csv"):
		seed_key = row.get("seed_key", "").strip()
		supplier = row.get("supplier", "").strip()
		item_code = row.get("item_code", "").strip()
		if not seed_key or not supplier or not item_code:
			continue
		if frappe.db.exists("Purchase Order", {"terms": seed_key}):
			continue
		if not frappe.db.exists("Supplier", supplier) or not frappe.db.exists("Item", item_code):
			continue
		try:
			schedule_date = add_days(nowdate(), int(row.get("schedule_days") or 14))
			doc = frappe.get_doc({
				"doctype": "Purchase Order",
				"supplier": supplier,
				"company": company,
				"transaction_date": nowdate(),
				"schedule_date": schedule_date,
				"terms": seed_key,
				"items": [{
					"item_code": item_code,
					"qty": flt(row.get("qty") or 1),
					"rate": flt(row.get("rate") or 0),
					"schedule_date": schedule_date,
				}],
			})
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as sub_exc:
				warnings.append(f"Purchase Order submit skipped ({seed_key}): {sub_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Purchase Order seed skipped ({seed_key}): {exc}")
	return created


def _seed_material_requests(base_dir, company, warnings):
	created = 0
	for row in _csv_rows(base_dir, "material_requests.csv"):
		seed_key = row.get("seed_key", "").strip()
		item_code = row.get("item_code", "").strip()
		if not seed_key or not item_code:
			continue
		if frappe.db.exists("Material Request", {"terms": seed_key}):
			continue
		if not frappe.db.exists("Item", item_code):
			continue
		warehouse = row.get("warehouse") or "Stores - L"
		if not frappe.db.exists("Warehouse", warehouse):
			warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")
		try:
			doc = frappe.get_doc({
				"doctype": "Material Request",
				"material_request_type": row.get("purpose") or "Purchase",
				"company": company,
				"transaction_date": nowdate(),
				"schedule_date": add_days(nowdate(), 7),
				"terms": seed_key,
				"items": [{
					"item_code": item_code,
					"qty": flt(row.get("qty") or 1),
					"uom": "Nos",
					"warehouse": warehouse,
					"schedule_date": add_days(nowdate(), 7),
				}],
			})
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as sub_exc:
				warnings.append(f"Material Request submit skipped ({seed_key}): {sub_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Material Request seed skipped ({seed_key}): {exc}")
	return created


def _seed_boms(base_dir, company, warnings):
	"""Group CSV rows by bom_item_code to build one BOM per manufactured item."""
	from collections import defaultdict

	rows = _csv_rows(base_dir, "boms.csv")
	components_by_item = defaultdict(list)
	for row in rows:
		bom_item = row.get("bom_item_code", "").strip()
		comp = row.get("component_item_code", "").strip()
		if bom_item and comp:
			components_by_item[bom_item].append({
				"item_code": comp,
				"qty": flt(row.get("qty") or 1),
				"uom": "Nos",
			})

	created = 0
	for bom_item, components in components_by_item.items():
		if not frappe.db.exists("Item", bom_item):
			warnings.append(f"BOM skipped — item not found: {bom_item}")
			continue
		if frappe.db.exists("BOM", {"item": bom_item, "is_active": 1, "docstatus": 1}):
			continue
		try:
			doc = frappe.get_doc({
				"doctype": "BOM",
				"item": bom_item,
				"company": company,
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"items": [
					{
						"item_code": c["item_code"],
						"qty": c["qty"],
						"uom": c["uom"],
					}
					for c in components
					if frappe.db.exists("Item", c["item_code"])
				],
			})
			doc.insert(ignore_permissions=True)
			doc.submit()
			created += 1
		except Exception as exc:
			warnings.append(f"BOM seed skipped ({bom_item}): {exc}")
	return created


def _seed_work_orders(base_dir, company, warnings):
	created = 0
	warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name") or "Stores - L"

	for row in _csv_rows(base_dir, "work_orders.csv"):
		seed_key = row.get("seed_key", "").strip()
		production_item = row.get("production_item", "").strip()
		if not seed_key or not production_item:
			continue
		if frappe.db.exists("Work Order", {"description": seed_key}):
			continue
		if not frappe.db.exists("Item", production_item):
			continue
		bom = frappe.db.get_value(
			"BOM", {"item": production_item, "is_active": 1, "docstatus": 1}, "name"
		)
		if not bom:
			warnings.append(f"Work Order skipped — no active BOM for {production_item}")
			continue
		try:
			doc = frappe.get_doc({
				"doctype": "Work Order",
				"production_item": production_item,
				"bom_no": bom,
				"qty": flt(row.get("qty") or 1),
				"company": company,
				"planned_start_date": nowdate(),
				"fg_warehouse": warehouse,
				"wip_warehouse": warehouse,
				"description": seed_key,
			})
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as sub_exc:
				warnings.append(f"Work Order submit skipped ({seed_key}): {sub_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Work Order seed skipped ({seed_key}): {exc}")
	return created


def _seed_projects(base_dir, warnings):
	created = 0
	for row in _csv_rows(base_dir, "projects.csv"):
		project_name = row.get("project_name", "").strip()
		if not project_name:
			continue
		# Project uses naming_series (PROJ-0001), so check by project_name field
		if frappe.db.get_value("Project", {"project_name": project_name}, "name"):
			continue
		try:
			start_offset = int(row.get("start_offset") or 0)
			end_offset = int(row.get("end_offset") or 30)
			doc = frappe.get_doc({
				"doctype": "Project",
				"project_name": project_name,
				"status": row.get("status") or "Open",
				"expected_start_date": add_days(nowdate(), start_offset),
				"expected_end_date": add_days(nowdate(), end_offset),
			})
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception as exc:
			warnings.append(f"Project seed skipped ({project_name}): {exc}")
	return created


def _seed_tasks(base_dir, warnings):
	created = 0
	for row in _csv_rows(base_dir, "tasks.csv"):
		project_name = row.get("project", "").strip()
		subject = row.get("subject", "").strip()
		if not project_name or not subject:
			continue
		# Project uses naming_series (PROJ-0001), so look up by project_name field
		project_id = frappe.db.get_value("Project", {"project_name": project_name}, "name")
		if not project_id:
			warnings.append(f"Task skipped — project not found: {project_name}")
			continue
		if frappe.db.exists("Task", {"project": project_id, "subject": subject}):
			continue
		try:
			doc = frappe.get_doc({
				"doctype": "Task",
				"project": project_id,
				"subject": subject,
				"status": row.get("status") or "Open",
				"priority": row.get("priority") or "Medium",
			})
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception as exc:
			warnings.append(f"Task seed skipped ({project_name}/{subject}): {exc}")
	return created


def _seed_journal_entries(company, warnings):
	"""Seed GL journal entries covering income recognition, cash movement, and AR/AP.

	Receivable/Payable accounts in ERPNext require party_type + party on each row.
	We resolve all accounts dynamically from the live CoA so this works regardless
	of which Chart of Accounts the company uses.
	"""
	created = 0

	def _acct(filters):
		return frappe.db.get_value("Account", {**filters, "company": company, "is_group": 0}, "name")

	receivable = _acct({"account_type": "Receivable"})
	payable = _acct({"account_type": "Payable"})
	cash = _acct({"account_type": "Cash"}) or _acct({"root_type": "Asset", "account_name": ["like", "%Cash%"]})
	income = _acct({"root_type": "Income"})
	expense = _acct({"root_type": "Expense"}) or _acct({"account_type": "Expense Account"})

	# Pick a seeded customer and supplier for party-required rows
	customer = frappe.db.get_value("Customer", {"customer_name": "CUST-RETAIL-001"}, "name") or \
		frappe.db.get_value("Customer", {}, "name")
	supplier = frappe.db.get_value("Supplier", {"supplier_name": "SUP-BOOKS-DIST"}, "name") or \
		frappe.db.get_value("Supplier", {}, "name")

	if not cash or not income:
		warnings.append("Journal Entry seed skipped — could not resolve Cash/Income accounts")
		return created

	def _row(account, debit, credit, party_type=None, party=None):
		r = {
			"account": account,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
		}
		if party_type and party:
			r["party_type"] = party_type
			r["party"] = party
		return r

	entries = [
		# JE-001: Income recognition — cash sale, no AR needed
		{
			"seed_key": "SEED-JE-001",
			"accounts": [
				_row(cash, 5000, 0),
				_row(income, 0, 5000),
			],
		},
		# JE-002: Petty cash expense
		{
			"seed_key": "SEED-JE-002",
			"accounts": [
				_row(expense or income, 500, 0),
				_row(cash, 0, 500),
			],
		} if expense else None,
		# JE-003: AR — requires party; skip if no receivable account resolved
		{
			"seed_key": "SEED-JE-003",
			"accounts": [
				_row(receivable, 3000, 0, "Customer", customer),
				_row(income, 0, 3000),
			],
		} if receivable and customer else None,
		# JE-004: AP — requires party
		{
			"seed_key": "SEED-JE-004",
			"accounts": [
				_row(expense or income, 2000, 0),
				_row(payable, 0, 2000, "Supplier", supplier),
			],
		} if payable and supplier and expense else None,
		# JE-005: AR settlement (cash receipt against customer)
		{
			"seed_key": "SEED-JE-005",
			"accounts": [
				_row(cash, 1500, 0),
				_row(receivable, 0, 1500, "Customer", customer),
			],
		} if receivable and customer else None,
	]

	for entry in entries:
		if not entry:
			continue
		seed_key = entry["seed_key"]
		# user_remark is a stable text field ERPNext never auto-overwrites (unlike title)
		if frappe.db.exists("Journal Entry", {"user_remark": seed_key}):
			continue
		try:
			doc = frappe.get_doc({
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"user_remark": seed_key,
				"company": company,
				"posting_date": nowdate(),
				"accounts": entry["accounts"],
			})
			doc.insert(ignore_permissions=True)
			try:
				doc.submit()
			except Exception as sub_exc:
				warnings.append(f"Journal Entry submit skipped ({seed_key}): {sub_exc}")
			created += 1
		except Exception as exc:
			warnings.append(f"Journal Entry seed skipped ({seed_key}): {exc}")
	return created


def _ts(dt):
	if isinstance(dt, datetime):
		return dt.strftime("%Y-%m-%d %H:%M:%S")
	return str(dt)
