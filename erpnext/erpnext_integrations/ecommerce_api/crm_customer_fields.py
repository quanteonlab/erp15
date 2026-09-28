"""CRM Customer extras: preferred delivery hours + Argentina IVA Cond. + seed slots."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt

_DEFAULT_HOURS = [
	("Mañana", 1),
	("Mediodía", 2),
	("Tarde", 3),
	("Noche", 4),
]

# Argentina Empresa Cond. IVA (alícuotas) — Tax Category + Sales Taxes template per rate.
_ARGENTINA_IVA_RATES = [
	("IVA 21%", 21.0, "Standard — bienes y servicios generales"),
	("IVA 10.5%", 10.5, "Reduced — alimentos básicos / transporte público"),
	("IVA 27%", 27.0, "Higher — electricidad, gas, agua (comercial)"),
]


def ensure_preferred_delivery_hours():
	"""Create DocType rows (Mañana…Noche) and Customer.custom_preferred_hours Link."""
	create_custom_fields(
		{
			"Customer": [
				{
					"fieldname": "custom_preferred_hours",
					"label": "Horario Preferible",
					"fieldtype": "Link",
					"options": "Preferred Delivery Hours",
					"insert_after": "territory",
					"in_list_view": 0,
					"in_standard_filter": 1,
				},
			]
		},
		ignore_validate=True,
	)

	if not frappe.db.exists("DocType", "Preferred Delivery Hours"):
		return {"ok": False, "reason": "doctype_missing"}

	created = []
	for label, priority in _DEFAULT_HOURS:
		if frappe.db.exists("Preferred Delivery Hours", label):
			# Keep priority in sync for stock defaults (do not overwrite user renames of priority
			# if they changed it — only seed missing).
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Preferred Delivery Hours",
				"label": label,
				"priority": priority,
				"disabled": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(label)

	if created:
		frappe.db.commit()
	return {"ok": True, "created": created}


def ensure_argentina_iva_conditions():
	"""
	Seed Argentina Cond. IVA Tax Categories (21% / 10.5% / 27%) and matching
	Sales Taxes and Charges Templates for every Company (Empresa settings).
	Idempotent — safe on after_migrate and CRM option loads.
	"""
	if not frappe.db.exists("DocType", "Tax Category"):
		return {"ok": False, "reason": "doctype_missing"}

	created_cats = []
	for title, _rate, _desc in _ARGENTINA_IVA_RATES:
		if frappe.db.exists("Tax Category", title):
			continue
		frappe.get_doc({"doctype": "Tax Category", "title": title, "disabled": 0}).insert(
			ignore_permissions=True
		)
		created_cats.append(title)

	created_templates = []
	if frappe.db.exists("DocType", "Sales Taxes and Charges Template"):
		companies = frappe.get_all("Company", fields=["name"], ignore_permissions=True)
		for company_row in companies:
			company = company_row.name
			account = _tax_account_for_company(company)
			if not account:
				continue
			for title, rate, desc in _ARGENTINA_IVA_RATES:
				# One template per (company, tax_category)
				existing = frappe.db.exists(
					"Sales Taxes and Charges Template",
					{"company": company, "tax_category": title},
				)
				if existing:
					continue
				# Also skip if title already used for this company (autoname = title - abbr)
				abbr = frappe.get_cached_value("Company", company, "abbr") or ""
				named = f"{title} - {abbr}".strip(" -")
				if named and frappe.db.exists("Sales Taxes and Charges Template", named):
					# Link tax_category if missing
					frappe.db.set_value(
						"Sales Taxes and Charges Template",
						named,
						"tax_category",
						title,
						update_modified=False,
					)
					continue
				try:
					doc = frappe.get_doc(
						{
							"doctype": "Sales Taxes and Charges Template",
							"title": title,
							"company": company,
							"tax_category": title,
							"is_default": 1 if rate == 21.0 else 0,
							"taxes": [
								{
									"charge_type": "On Net Total",
									"account_head": account,
									"rate": flt(rate),
									"description": desc or title,
								}
							],
						}
					)
					doc.insert(ignore_permissions=True)
					created_templates.append(doc.name)
				except Exception:
					frappe.log_error(
						title=f"ensure_argentina_iva_conditions:{company}:{title}"
					)

	if created_cats or created_templates:
		frappe.db.commit()
	return {
		"ok": True,
		"tax_categories": created_cats,
		"templates": created_templates,
	}


def _tax_account_for_company(company: str) -> str | None:
	"""Best-effort Tax / Liability leaf account for Sales Taxes templates."""
	account = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "account_type": "Tax"},
		"name",
	)
	if account:
		return account
	# Prefer an IVA-named liability if present
	for like in ("%IVA%", "%VAT%", "%Tax%"):
		rows = frappe.get_all(
			"Account",
			filters={
				"company": company,
				"is_group": 0,
				"root_type": "Liability",
				"account_name": ("like", like),
			},
			fields=["name"],
			limit_page_length=1,
			ignore_permissions=True,
		)
		if rows:
			return rows[0].name
	return frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "root_type": "Liability"},
		"name",
	)


@frappe.whitelist(allow_guest=True)
def ensure_or_create_tax_category(title=None):
	"""Create a Tax Category by title (CRM Cond. IVA allowCreate). Returns {name}."""
	ensure_argentina_iva_conditions()
	label = (title or "").strip()
	if not label:
		frappe.throw(_("Tax Category title is required"))
	if not frappe.db.exists("DocType", "Tax Category"):
		frappe.throw(_("Tax Category DocType is missing"))
	if frappe.db.exists("Tax Category", label):
		return {"name": label, "created": False}
	frappe.get_doc({"doctype": "Tax Category", "title": label, "disabled": 0}).insert(
		ignore_permissions=True
	)
	frappe.db.commit()
	return {"name": label, "created": True}


@frappe.whitelist(allow_guest=True)
def list_preferred_delivery_hours(include_disabled=0):
	"""CRM select options — sorted by priority ascending."""
	ensure_preferred_delivery_hours()
	filters = {}
	try:
		include = int(include_disabled or 0)
	except (TypeError, ValueError):
		include = 0
	if not include:
		filters["disabled"] = 0
	rows = frappe.get_all(
		"Preferred Delivery Hours",
		filters=filters,
		fields=["name", "label", "priority", "disabled"],
		order_by="priority asc, label asc",
		ignore_permissions=True,
	)
	return {
		"hours": [
			{
				"name": r.name,
				"label": r.label or r.name,
				"priority": r.priority,
				"disabled": int(r.disabled or 0),
			}
			for r in rows
		]
	}


@frappe.whitelist(allow_guest=True)
def save_preferred_delivery_hours(hours=None):
	"""Replace/update the editable hours list from CRM settings-style payload."""
	ensure_preferred_delivery_hours()
	if isinstance(hours, str):
		hours = frappe.parse_json(hours)
	if not isinstance(hours, list):
		frappe.throw(_("hours must be a list"))

	seen = set()
	for idx, row in enumerate(hours):
		if not isinstance(row, dict):
			continue
		label = (row.get("label") or row.get("name") or "").strip()
		if not label:
			continue
		priority = int(row.get("priority") or (idx + 1))
		disabled = 1 if row.get("disabled") else 0
		seen.add(label)
		if frappe.db.exists("Preferred Delivery Hours", label):
			frappe.db.set_value(
				"Preferred Delivery Hours",
				label,
				{"priority": priority, "disabled": disabled},
				update_modified=True,
			)
		else:
			frappe.get_doc(
				{
					"doctype": "Preferred Delivery Hours",
					"label": label,
					"priority": priority,
					"disabled": disabled,
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()
	return list_preferred_delivery_hours(include_disabled=1)
