import json

import frappe
from frappe import _

# Fields exposed per source doctype, plus the child-table (line items) fieldname
# used by the 'line-items' element kind in the template designer.
_SOURCE_DOCTYPE_CHILD_TABLE = {
	"Sales Invoice": "items",
	"Purchase Receipt": "items",
	"Item": None,
}

_SKIP_FIELDTYPES = {
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Button",
	"Table",
	"Table MultiSelect",
	"Password",
	"Signature",
}


def _has_company_column() -> bool:
	try:
		return bool(frappe.db.has_column("ECommerce Print Template", "company"))
	except Exception:
		return False


def _clear_default(source_doctype, paper_kind, keep_name=None):
	filters = {"source_doctype": source_doctype, "paper_kind": paper_kind, "is_default": 1}
	if keep_name:
		filters["name"] = ["!=", keep_name]
	others = frappe.get_all("ECommerce Print Template", filters=filters, pluck="name", ignore_permissions=True)
	for name in others:
		frappe.db.set_value("ECommerce Print Template", name, "is_default", 0)


def _apply_default(template_id, source_doctype, paper_kind):
	_clear_default(source_doctype, paper_kind, keep_name=template_id)
	frappe.db.set_value("ECommerce Print Template", template_id, "is_default", 1)


def _doc_to_dict(doc):
	return {
		"id": doc.name,
		"templateName": doc.template_name,
		"sourceDoctype": doc.source_doctype,
		"paperKind": doc.paper_kind,
		"status": doc.status or "Draft",
		"isDefault": bool(doc.is_default),
		"company": doc.company,
		"canvasWidthMm": doc.canvas_width_mm,
		"canvasHeightMm": doc.canvas_height_mm,
		"marginMm": json.loads(doc.margin_mm or "[10,10,10,10]"),
		"elements": json.loads(doc.elements_data or "[]"),
		"notes": json.loads(doc.notes_data or "[]"),
		"modified": doc.modified,
	}


@frappe.whitelist(allow_guest=True)
def list_print_templates(source_doctype=None, paper_kind=None, company=None):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	filters = {}
	if source_doctype:
		filters["source_doctype"] = source_doctype
	if paper_kind:
		filters["paper_kind"] = paper_kind
	if _has_company_column():
		active = company_scope(company)
		if active:
			filters["company"] = active

	rows = frappe.get_all(
		"ECommerce Print Template",
		filters=filters,
		fields=[
			"name",
			"template_name",
			"source_doctype",
			"paper_kind",
			"status",
			"is_default",
			"company",
			"modified",
		],
		order_by="modified desc",
		ignore_permissions=True,
	)

	templates = [
		{
			"id": r.name,
			"templateName": r.template_name,
			"sourceDoctype": r.source_doctype,
			"paperKind": r.paper_kind,
			"status": r.status or "Draft",
			"isDefault": bool(r.is_default),
			"company": r.company,
			"modified": r.modified,
		}
		for r in rows
	]
	return {"templates": templates}


@frappe.whitelist(allow_guest=True)
def get_print_template(template_id):
	if not frappe.db.exists("ECommerce Print Template", template_id):
		frappe.throw(_("Print template {0} not found").format(template_id), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("ECommerce Print Template", template_id)
	frappe.flags.ignore_permissions = False

	return {"template": _doc_to_dict(doc)}


@frappe.whitelist(allow_guest=True)
def save_print_template(
	template_name,
	source_doctype,
	paper_kind,
	elements_data="[]",
	notes_data="[]",
	status="Draft",
	is_default=0,
	margin_mm=None,
	canvas_width_mm=None,
	canvas_height_mm=None,
	company=None,
	template_id=None,
):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	elements_json = elements_data if isinstance(elements_data, str) else json.dumps(elements_data)
	notes_json = notes_data if isinstance(notes_data, str) else json.dumps(notes_data)
	margin_json = margin_mm if isinstance(margin_mm, str) else json.dumps(margin_mm or [10, 10, 10, 10])
	make_default = bool(frappe.utils.cint(is_default))
	active = resolve_company(company) if _has_company_column() else None

	# Match strictly by template_id (never by name): several drafts/production copies are
	# allowed to share a name within the same source_doctype+paper_kind scope, so falling
	# back to a name-based lookup here would silently merge a "new" save into an unrelated
	# existing template that happens to share the same name.
	existing = template_id

	if existing:
		doc = frappe.get_doc("ECommerce Print Template", existing)
		doc.template_name = template_name
		doc.source_doctype = source_doctype
		doc.paper_kind = paper_kind
		doc.elements_data = elements_json
		doc.notes_data = notes_json
		doc.status = status or "Draft"
		if canvas_width_mm is not None:
			doc.canvas_width_mm = float(canvas_width_mm)
		if canvas_height_mm is not None:
			doc.canvas_height_mm = float(canvas_height_mm)
		doc.margin_mm = margin_json
		if active:
			doc.company = active
		doc.is_default = 1 if make_default else 0
		doc.save(ignore_permissions=True)
		if make_default:
			_apply_default(doc.name, doc.source_doctype, doc.paper_kind)
		frappe.db.commit()
		return {"status": "updated", "message": "Print template updated", "template_id": doc.name}

	doc = frappe.new_doc("ECommerce Print Template")
	doc.template_name = template_name
	doc.source_doctype = source_doctype
	doc.paper_kind = paper_kind
	doc.elements_data = elements_json
	doc.notes_data = notes_json
	doc.status = status or "Draft"
	if canvas_width_mm is not None:
		doc.canvas_width_mm = float(canvas_width_mm)
	if canvas_height_mm is not None:
		doc.canvas_height_mm = float(canvas_height_mm)
	doc.margin_mm = margin_json
	if active:
		doc.company = active
	doc.is_default = 1 if make_default else 0
	doc.insert(ignore_permissions=True)
	if make_default:
		_apply_default(doc.name, doc.source_doctype, doc.paper_kind)
	frappe.db.commit()
	return {"status": "created", "message": "Print template created", "template_id": doc.name}


@frappe.whitelist(allow_guest=True)
def delete_print_template(template_id):
	if not frappe.db.exists("ECommerce Print Template", template_id):
		frappe.throw(_("Print template {0} not found").format(template_id), frappe.DoesNotExistError)

	frappe.delete_doc("ECommerce Print Template", template_id, ignore_permissions=True)
	frappe.db.commit()
	return {"status": "deleted", "message": "Print template deleted", "deleted_template_id": template_id}


@frappe.whitelist(allow_guest=True)
def set_default_print_template(template_id):
	if not frappe.db.exists("ECommerce Print Template", template_id):
		frappe.throw(_("Print template {0} not found").format(template_id), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("ECommerce Print Template", template_id)
	frappe.flags.ignore_permissions = False

	_apply_default(doc.name, doc.source_doctype, doc.paper_kind)
	frappe.db.set_value("ECommerce Print Template", doc.name, "is_default", 1)
	frappe.db.commit()
	return {"status": "updated", "message": "Default print template set", "template_id": doc.name}


@frappe.whitelist(allow_guest=True)
def get_doctype_fields(source_doctype):
	meta = frappe.get_meta(source_doctype)

	def field_list(doctype_meta):
		out = []
		for f in doctype_meta.fields:
			if f.fieldtype in _SKIP_FIELDTYPES:
				continue
			out.append({"fieldname": f.fieldname, "label": f.label or f.fieldname, "fieldtype": f.fieldtype})
		return out

	fields = field_list(meta)
	child_table_fieldname = _SOURCE_DOCTYPE_CHILD_TABLE.get(source_doctype)
	child_fields = []
	if child_table_fieldname:
		child_field_def = meta.get_field(child_table_fieldname)
		if child_field_def and child_field_def.options:
			child_fields = field_list(frappe.get_meta(child_field_def.options))

	return {
		"fields": fields,
		"childTableFieldname": child_table_fieldname,
		"childTableFields": child_fields,
	}


@frappe.whitelist(allow_guest=True)
def get_print_data(source_doctype, docname):
	if not frappe.db.exists(source_doctype, docname):
		frappe.throw(_("{0} {1} not found").format(source_doctype, docname), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(source_doctype, docname)
	frappe.flags.ignore_permissions = False

	data = doc.as_dict()
	child_table_fieldname = _SOURCE_DOCTYPE_CHILD_TABLE.get(source_doctype)
	rows = []
	if child_table_fieldname:
		# doc.as_dict() already recursively converts child table rows to plain dicts.
		rows = list(data.get(child_table_fieldname) or [])

	return {
		"doc": data,
		"lineItems": rows,
	}


_STARTER_TEMPLATES = [
	{
		"template_name": "Standard Invoice (A4)",
		"source_doctype": "Sales Invoice",
		"paper_kind": "A4",
		"margin_mm": [15, 15, 15, 15],
		"elements": [
			{"id": "starter-inv-title", "kind": "text", "x": 15, "y": 15, "width": 110, "height": 12, "staticText": "INVOICE", "fontSize": 20, "bold": True, "align": "left"},
			{"id": "starter-inv-number-label", "kind": "text", "x": 140, "y": 15, "width": 55, "height": 6, "staticText": "Invoice #", "fontSize": 8, "align": "right"},
			{"id": "starter-inv-number", "kind": "field", "x": 140, "y": 21, "width": 55, "height": 8, "fieldPath": "name", "label": "Invoice #", "fontSize": 11, "bold": True, "align": "right"},
			{"id": "starter-inv-date-label", "kind": "text", "x": 140, "y": 31, "width": 55, "height": 6, "staticText": "Date", "fontSize": 8, "align": "right"},
			{"id": "starter-inv-date", "kind": "field", "x": 140, "y": 37, "width": 55, "height": 8, "fieldPath": "posting_date", "label": "Date", "fontSize": 10, "align": "right"},
			{"id": "starter-inv-bill-to-label", "kind": "text", "x": 15, "y": 32, "width": 90, "height": 6, "staticText": "Bill To", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-inv-customer", "kind": "field", "x": 15, "y": 38, "width": 100, "height": 8, "fieldPath": "customer_name", "label": "Customer", "fontSize": 11, "align": "left"},
			{
				"id": "starter-inv-items",
				"kind": "line-items",
				"x": 15,
				"y": 60,
				"width": 180,
				"height": 90,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_code", "label": "Item", "width": 30},
					{"fieldPath": "item_name", "label": "Description", "width": 70},
					{"fieldPath": "qty", "label": "Qty", "width": 20},
					{"fieldPath": "rate", "label": "Rate", "width": 25},
					{"fieldPath": "amount", "label": "Amount", "width": 30},
				],
			},
			{"id": "starter-inv-total-label", "kind": "text", "x": 130, "y": 155, "width": 30, "height": 8, "staticText": "Total", "fontSize": 10, "bold": True, "align": "right"},
			{"id": "starter-inv-total", "kind": "field", "x": 160, "y": 155, "width": 35, "height": 10, "fieldPath": "grand_total", "label": "Total", "fontSize": 13, "bold": True, "align": "right"},
		],
	},
	{
		"template_name": "Product Catalog (A4)",
		"source_doctype": "Item",
		"paper_kind": "A4",
		"margin_mm": [15, 15, 15, 15],
		"elements": [
			{"id": "starter-cat-image", "kind": "image", "x": 15, "y": 15, "width": 70, "height": 70, "fieldPath": "image"},
			{"id": "starter-cat-name", "kind": "field", "x": 95, "y": 15, "width": 100, "height": 12, "fieldPath": "item_name", "label": "Product", "fontSize": 16, "bold": True, "align": "left"},
			{"id": "starter-cat-code", "kind": "field", "x": 95, "y": 29, "width": 100, "height": 8, "fieldPath": "item_code", "label": "Code", "fontSize": 10, "align": "left"},
			{"id": "starter-cat-desc", "kind": "field", "x": 95, "y": 40, "width": 100, "height": 35, "fieldPath": "description", "label": "Description", "fontSize": 9, "align": "left"},
			{"id": "starter-cat-price-label", "kind": "text", "x": 15, "y": 92, "width": 40, "height": 6, "staticText": "Price", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-cat-price", "kind": "field", "x": 15, "y": 98, "width": 60, "height": 12, "fieldPath": "standard_rate", "label": "Price", "fontSize": 16, "bold": True, "align": "left"},
			{"id": "starter-cat-barcode", "kind": "barcode", "x": 95, "y": 92, "width": 65, "height": 20, "fieldPath": "item_code"},
		],
	},
]


@frappe.whitelist(allow_guest=True)
def ensure_starter_print_templates():
	"""Idempotently create the built-in starter templates so the designer never opens empty."""
	created = []
	for starter in _STARTER_TEMPLATES:
		exists = frappe.db.exists(
			"ECommerce Print Template",
			{
				"template_name": starter["template_name"],
				"source_doctype": starter["source_doctype"],
				"paper_kind": starter["paper_kind"],
			},
		)
		if exists:
			continue

		doc = frappe.new_doc("ECommerce Print Template")
		doc.template_name = starter["template_name"]
		doc.source_doctype = starter["source_doctype"]
		doc.paper_kind = starter["paper_kind"]
		doc.status = "Production"
		doc.is_default = 1
		doc.margin_mm = json.dumps(starter["margin_mm"])
		doc.elements_data = json.dumps(starter["elements"])
		doc.notes_data = "[]"
		doc.canvas_width_mm = 210
		doc.canvas_height_mm = 297
		doc.insert(ignore_permissions=True)
		_apply_default(doc.name, doc.source_doctype, doc.paper_kind)
		created.append(doc.name)

	if created:
		frappe.db.commit()
	return {"created": created}
