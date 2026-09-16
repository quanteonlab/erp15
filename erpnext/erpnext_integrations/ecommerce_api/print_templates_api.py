import json

import frappe
from frappe import _

# Fields exposed per source doctype, plus the child-table (line items) fieldname
# used by the 'line-items' element kind in the template designer.
_SOURCE_DOCTYPE_CHILD_TABLE = {
	"Sales Invoice": "items",
	"Purchase Receipt": "items",
	"Delivery Note": "items",
	"Item": None,
	# Synthetic — not a real Frappe doctype. The catalog PDF binds these fields
	# at export time from the current export settings (heading/phone/description),
	# not from a stored document.
	"Catalog Header": None,
}

# Field list for the synthetic "Catalog Header" source doctype (see above).
_CATALOG_HEADER_FIELDS = [
	{"fieldname": "heading", "label": "Heading", "fieldtype": "Data"},
	{"fieldname": "description", "label": "Description", "fieldtype": "Small Text"},
	{"fieldname": "phone", "label": "WhatsApp Phone", "fieldtype": "Data"},
	{"fieldname": "qr_value", "label": "QR Value (wa.me link)", "fieldtype": "Data"},
	{"fieldname": "company_name", "label": "Company Name", "fieldtype": "Data"},
]

# Extra synthetic fields available only when designing an Item template scoped to
# paper_kind "Catalog Card" — computed by the catalog exporter per product, not
# real Item columns (catalog price is price-list-resolved, not Item.standard_rate).
_CATALOG_CARD_EXTRA_FIELDS = [
	{"fieldname": "display_price", "label": "Catalog Price", "fieldtype": "Data"},
	{"fieldname": "normalized_title", "label": "Normalized Title", "fieldtype": "Data"},
	{"fieldname": "barcode", "label": "Barcode (first)", "fieldtype": "Data"},
	# i030 A3 — set only when the exporter's "show promo variant" toggle is on and this
	# product matches an active promotion; blank otherwise (renders blank on the card).
	{"fieldname": "promo_label", "label": "Promo Label (e.g. 3x2, 20% OFF)", "fieldtype": "Data"},
	{"fieldname": "promo_title", "label": "Promo Title", "fieldtype": "Data"},
]

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
def get_doctype_fields(source_doctype, paper_kind=None):
	if source_doctype == "Catalog Header":
		# Synthetic doctype — no frappe.get_meta lookup, fields are fixed.
		return {"fields": list(_CATALOG_HEADER_FIELDS), "childTableFieldname": None, "childTableFields": []}

	meta = frappe.get_meta(source_doctype)

	def field_list(doctype_meta):
		out = []
		for f in doctype_meta.fields:
			if f.fieldtype in _SKIP_FIELDTYPES:
				continue
			out.append({"fieldname": f.fieldname, "label": f.label or f.fieldname, "fieldtype": f.fieldtype})
		return out

	fields = field_list(meta)
	if source_doctype == "Item" and paper_kind == "Catalog Card":
		fields = fields + list(_CATALOG_CARD_EXTRA_FIELDS)

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


_PAPER_SIZE_MM = {
	"A4": (210, 297),
	"Thermal 58mm": (58, 150),
	"Thermal 80mm": (80, 150),
	# One catalog print-grid card cell (approximates today's hardcoded PrintCard aspect).
	"Catalog Card": (48, 66),
}

_STARTER_TEMPLATES = [
	# ── Sales Invoice ──────────────────────────────────────────────────
	{
		"template_name": "Standard Invoice (A4)",
		"source_doctype": "Sales Invoice",
		"paper_kind": "A4",
		"is_default": True,
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
	# ── Sales Invoice · Thermal (POS cobro tickets) ─────────────────────
	{
		"template_name": "POS Ticket (80mm)",
		"source_doctype": "Sales Invoice",
		"paper_kind": "Thermal 80mm",
		"is_default": True,
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-pos80-title", "kind": "text", "x": 4, "y": 4, "width": 72, "height": 8, "staticText": "RECIBO DE VENTA", "fontSize": 12, "bold": True, "align": "center"},
			{"id": "starter-pos80-nro-label", "kind": "text", "x": 4, "y": 14, "width": 20, "height": 5, "staticText": "N°", "fontSize": 7, "align": "left"},
			{"id": "starter-pos80-nro", "kind": "field", "x": 24, "y": 14, "width": 52, "height": 5, "fieldPath": "name", "label": "Receipt", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-pos80-date", "kind": "field", "x": 4, "y": 21, "width": 72, "height": 5, "fieldPath": "posting_date", "label": "Date", "fontSize": 8, "align": "left"},
			{"id": "starter-pos80-mode", "kind": "field", "x": 4, "y": 28, "width": 72, "height": 5, "fieldPath": "custom_sale_mode", "label": "Mode", "fontSize": 8, "align": "left"},
			{"id": "starter-pos80-pay", "kind": "field", "x": 4, "y": 35, "width": 72, "height": 5, "fieldPath": "custom_payment_method", "label": "Payment", "fontSize": 8, "align": "left"},
			{
				"id": "starter-pos80-items",
				"kind": "line-items",
				"x": 4,
				"y": 43,
				"width": 72,
				"height": 55,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_name", "label": "Item", "width": 36},
					{"fieldPath": "qty", "label": "Cant", "width": 12},
					{"fieldPath": "amount", "label": "Monto", "width": 24},
				],
			},
			{"id": "starter-pos80-total-label", "kind": "text", "x": 4, "y": 102, "width": 28, "height": 7, "staticText": "TOTAL", "fontSize": 10, "bold": True, "align": "left"},
			{"id": "starter-pos80-total", "kind": "field", "x": 32, "y": 102, "width": 44, "height": 8, "fieldPath": "grand_total", "label": "Total", "fontSize": 12, "bold": True, "align": "right"},
		],
	},
	{
		"template_name": "POS Ticket (58mm)",
		"source_doctype": "Sales Invoice",
		"paper_kind": "Thermal 58mm",
		"is_default": True,
		"margin_mm": [3, 3, 3, 3],
		"elements": [
			{"id": "starter-pos58-title", "kind": "text", "x": 3, "y": 3, "width": 52, "height": 7, "staticText": "RECIBO", "fontSize": 11, "bold": True, "align": "center"},
			{"id": "starter-pos58-nro", "kind": "field", "x": 3, "y": 12, "width": 52, "height": 5, "fieldPath": "name", "label": "Receipt", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-pos58-date", "kind": "field", "x": 3, "y": 18, "width": 52, "height": 5, "fieldPath": "posting_date", "label": "Date", "fontSize": 7, "align": "left"},
			{
				"id": "starter-pos58-items",
				"kind": "line-items",
				"x": 3,
				"y": 26,
				"width": 52,
				"height": 50,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_name", "label": "Item", "width": 28},
					{"fieldPath": "qty", "label": "Cant", "width": 10},
					{"fieldPath": "amount", "label": "$", "width": 14},
				],
			},
			{"id": "starter-pos58-total-label", "kind": "text", "x": 3, "y": 80, "width": 20, "height": 6, "staticText": "TOTAL", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-pos58-total", "kind": "field", "x": 23, "y": 80, "width": 32, "height": 7, "fieldPath": "grand_total", "label": "Total", "fontSize": 11, "bold": True, "align": "right"},
		],
	},
	# ── Item · A4 (2) ──────────────────────────────────────────────────
	{
		"template_name": "Product Catalog (A4)",
		"source_doctype": "Item",
		"paper_kind": "A4",
		"is_default": True,
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
	{
		"template_name": "Item Spec Sheet (A4)",
		"source_doctype": "Item",
		"paper_kind": "A4",
		"margin_mm": [15, 15, 15, 15],
		"elements": [
			{"id": "starter-ispec-title", "kind": "text", "x": 15, "y": 15, "width": 120, "height": 10, "staticText": "ITEM SPECIFICATION", "fontSize": 16, "bold": True, "align": "left"},
			{"id": "starter-ispec-code-label", "kind": "text", "x": 140, "y": 15, "width": 55, "height": 6, "staticText": "Item Code", "fontSize": 8, "align": "right"},
			{"id": "starter-ispec-code", "kind": "field", "x": 140, "y": 21, "width": 55, "height": 8, "fieldPath": "item_code", "label": "Code", "fontSize": 12, "bold": True, "align": "right"},
			{"id": "starter-ispec-name", "kind": "field", "x": 15, "y": 40, "width": 180, "height": 12, "fieldPath": "item_name", "label": "Name", "fontSize": 18, "bold": True, "align": "left"},
			{"id": "starter-ispec-desc", "kind": "field", "x": 15, "y": 56, "width": 180, "height": 40, "fieldPath": "description", "label": "Description", "fontSize": 10, "align": "left"},
			{"id": "starter-ispec-uom-label", "kind": "text", "x": 15, "y": 105, "width": 40, "height": 6, "staticText": "UOM", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-ispec-uom", "kind": "field", "x": 15, "y": 111, "width": 50, "height": 8, "fieldPath": "stock_uom", "label": "UOM", "fontSize": 11, "align": "left"},
			{"id": "starter-ispec-price-label", "kind": "text", "x": 80, "y": 105, "width": 40, "height": 6, "staticText": "Standard Rate", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-ispec-price", "kind": "field", "x": 80, "y": 111, "width": 50, "height": 8, "fieldPath": "standard_rate", "label": "Price", "fontSize": 11, "align": "left"},
			{"id": "starter-ispec-qr", "kind": "qrcode", "x": 155, "y": 100, "width": 40, "height": 40, "fieldPath": "item_code"},
			{"id": "starter-ispec-barcode", "kind": "barcode", "x": 15, "y": 130, "width": 120, "height": 25, "fieldPath": "item_code"},
		],
	},
	# ── Item · Thermal 58mm (2) ────────────────────────────────────────
	{
		"template_name": "Item Price Tag (58mm)",
		"source_doctype": "Item",
		"paper_kind": "Thermal 58mm",
		"is_default": True,
		"margin_mm": [3, 3, 3, 3],
		"elements": [
			{"id": "starter-i58a-name", "kind": "field", "x": 3, "y": 3, "width": 52, "height": 10, "fieldPath": "item_name", "label": "Name", "fontSize": 10, "bold": True, "align": "center"},
			{"id": "starter-i58a-code", "kind": "field", "x": 3, "y": 14, "width": 52, "height": 6, "fieldPath": "item_code", "label": "Code", "fontSize": 8, "align": "center"},
			{"id": "starter-i58a-price", "kind": "field", "x": 3, "y": 22, "width": 52, "height": 12, "fieldPath": "standard_rate", "label": "Price", "fontSize": 16, "bold": True, "align": "center"},
			{"id": "starter-i58a-barcode", "kind": "barcode", "x": 5, "y": 36, "width": 48, "height": 18, "fieldPath": "item_code"},
		],
	},
	{
		"template_name": "Item Shelf Label (58mm)",
		"source_doctype": "Item",
		"paper_kind": "Thermal 58mm",
		"margin_mm": [3, 3, 3, 3],
		"elements": [
			{"id": "starter-i58b-code", "kind": "field", "x": 3, "y": 3, "width": 52, "height": 7, "fieldPath": "item_code", "label": "Code", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-i58b-name", "kind": "field", "x": 3, "y": 11, "width": 34, "height": 16, "fieldPath": "item_name", "label": "Name", "fontSize": 9, "align": "left"},
			{"id": "starter-i58b-qr", "kind": "qrcode", "x": 38, "y": 11, "width": 17, "height": 17, "fieldPath": "item_code"},
			{"id": "starter-i58b-uom-label", "kind": "text", "x": 3, "y": 30, "width": 20, "height": 5, "staticText": "UOM", "fontSize": 7, "align": "left"},
			{"id": "starter-i58b-uom", "kind": "field", "x": 22, "y": 30, "width": 30, "height": 5, "fieldPath": "stock_uom", "label": "UOM", "fontSize": 8, "align": "left"},
			{"id": "starter-i58b-barcode", "kind": "barcode", "x": 5, "y": 38, "width": 48, "height": 16, "fieldPath": "item_code"},
		],
	},
	# ── Item · Thermal 80mm (2) ────────────────────────────────────────
	{
		"template_name": "Item Price Tag (80mm)",
		"source_doctype": "Item",
		"paper_kind": "Thermal 80mm",
		"is_default": True,
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-i80a-name", "kind": "field", "x": 4, "y": 4, "width": 72, "height": 10, "fieldPath": "item_name", "label": "Name", "fontSize": 12, "bold": True, "align": "center"},
			{"id": "starter-i80a-code", "kind": "field", "x": 4, "y": 15, "width": 72, "height": 7, "fieldPath": "item_code", "label": "Code", "fontSize": 9, "align": "center"},
			{"id": "starter-i80a-price", "kind": "field", "x": 4, "y": 24, "width": 72, "height": 14, "fieldPath": "standard_rate", "label": "Price", "fontSize": 18, "bold": True, "align": "center"},
			{"id": "starter-i80a-barcode", "kind": "barcode", "x": 8, "y": 40, "width": 64, "height": 20, "fieldPath": "item_code"},
		],
	},
	{
		"template_name": "Item Product Card (80mm)",
		"source_doctype": "Item",
		"paper_kind": "Thermal 80mm",
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-i80b-image", "kind": "image", "x": 4, "y": 4, "width": 28, "height": 28, "fieldPath": "image"},
			{"id": "starter-i80b-name", "kind": "field", "x": 34, "y": 4, "width": 42, "height": 12, "fieldPath": "item_name", "label": "Name", "fontSize": 10, "bold": True, "align": "left"},
			{"id": "starter-i80b-code", "kind": "field", "x": 34, "y": 17, "width": 42, "height": 6, "fieldPath": "item_code", "label": "Code", "fontSize": 8, "align": "left"},
			{"id": "starter-i80b-price", "kind": "field", "x": 34, "y": 24, "width": 42, "height": 8, "fieldPath": "standard_rate", "label": "Price", "fontSize": 12, "bold": True, "align": "left"},
			{"id": "starter-i80b-barcode", "kind": "barcode", "x": 8, "y": 36, "width": 64, "height": 18, "fieldPath": "item_code"},
			{"id": "starter-i80b-qr", "kind": "qrcode", "x": 58, "y": 56, "width": 18, "height": 18, "fieldPath": "item_code"},
		],
	},
	# ── Purchase Receipt · A4 (2) ──────────────────────────────────────
	{
		"template_name": "Goods Receipt (A4)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "A4",
		"is_default": True,
		"margin_mm": [15, 15, 15, 15],
		"elements": [
			{"id": "starter-pra4a-title", "kind": "text", "x": 15, "y": 15, "width": 120, "height": 12, "staticText": "PURCHASE RECEIPT", "fontSize": 18, "bold": True, "align": "left"},
			{"id": "starter-pra4a-num-label", "kind": "text", "x": 140, "y": 15, "width": 55, "height": 6, "staticText": "Receipt #", "fontSize": 8, "align": "right"},
			{"id": "starter-pra4a-num", "kind": "field", "x": 140, "y": 21, "width": 55, "height": 8, "fieldPath": "name", "label": "Receipt #", "fontSize": 11, "bold": True, "align": "right"},
			{"id": "starter-pra4a-date-label", "kind": "text", "x": 140, "y": 31, "width": 55, "height": 6, "staticText": "Date", "fontSize": 8, "align": "right"},
			{"id": "starter-pra4a-date", "kind": "field", "x": 140, "y": 37, "width": 55, "height": 8, "fieldPath": "posting_date", "label": "Date", "fontSize": 10, "align": "right"},
			{"id": "starter-pra4a-sup-label", "kind": "text", "x": 15, "y": 32, "width": 90, "height": 6, "staticText": "Supplier", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-pra4a-sup", "kind": "field", "x": 15, "y": 38, "width": 110, "height": 8, "fieldPath": "supplier_name", "label": "Supplier", "fontSize": 11, "align": "left"},
			{"id": "starter-pra4a-wh-label", "kind": "text", "x": 15, "y": 48, "width": 40, "height": 6, "staticText": "Warehouse", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-pra4a-wh", "kind": "field", "x": 55, "y": 48, "width": 80, "height": 6, "fieldPath": "set_warehouse", "label": "Warehouse", "fontSize": 10, "align": "left"},
			{
				"id": "starter-pra4a-items",
				"kind": "line-items",
				"x": 15,
				"y": 60,
				"width": 180,
				"height": 100,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_code", "label": "Item", "width": 35},
					{"fieldPath": "item_name", "label": "Description", "width": 75},
					{"fieldPath": "qty", "label": "Qty", "width": 20},
					{"fieldPath": "uom", "label": "UOM", "width": 20},
					{"fieldPath": "amount", "label": "Amount", "width": 30},
				],
			},
			{"id": "starter-pra4a-total-label", "kind": "text", "x": 130, "y": 170, "width": 30, "height": 8, "staticText": "Total Qty", "fontSize": 10, "bold": True, "align": "right"},
			{"id": "starter-pra4a-total", "kind": "field", "x": 160, "y": 170, "width": 35, "height": 10, "fieldPath": "total_qty", "label": "Total Qty", "fontSize": 13, "bold": True, "align": "right"},
		],
	},
	{
		"template_name": "Receiving Checklist (A4)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "A4",
		"margin_mm": [12, 12, 12, 12],
		"elements": [
			{"id": "starter-pra4b-title", "kind": "text", "x": 12, "y": 12, "width": 140, "height": 10, "staticText": "RECEIVING CHECKLIST", "fontSize": 16, "bold": True, "align": "left"},
			{"id": "starter-pra4b-num", "kind": "field", "x": 155, "y": 12, "width": 43, "height": 8, "fieldPath": "name", "label": "Receipt #", "fontSize": 10, "bold": True, "align": "right"},
			{"id": "starter-pra4b-date", "kind": "field", "x": 155, "y": 22, "width": 43, "height": 7, "fieldPath": "posting_date", "label": "Date", "fontSize": 9, "align": "right"},
			{"id": "starter-pra4b-sup-label", "kind": "text", "x": 12, "y": 28, "width": 50, "height": 6, "staticText": "From", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-pra4b-sup", "kind": "field", "x": 12, "y": 34, "width": 120, "height": 8, "fieldPath": "supplier_name", "label": "Supplier", "fontSize": 11, "align": "left"},
			{"id": "starter-pra4b-line", "kind": "shape", "x": 12, "y": 46, "width": 186, "height": 1, "shapeType": "line", "color": "#0f172a"},
			{
				"id": "starter-pra4b-items",
				"kind": "line-items",
				"x": 12,
				"y": 52,
				"width": 186,
				"height": 140,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_code", "label": "Code", "width": 40},
					{"fieldPath": "item_name", "label": "Item", "width": 90},
					{"fieldPath": "qty", "label": "Expected", "width": 28},
					{"fieldPath": "uom", "label": "UOM", "width": 28},
				],
			},
			{"id": "starter-pra4b-sign-label", "kind": "text", "x": 12, "y": 210, "width": 80, "height": 6, "staticText": "Received by / Signature", "fontSize": 8, "align": "left"},
			{"id": "starter-pra4b-sign-box", "kind": "shape", "x": 12, "y": 218, "width": 90, "height": 28, "shapeType": "rect", "color": "#94a3b8"},
			{"id": "starter-pra4b-qr", "kind": "qrcode", "x": 160, "y": 210, "width": 38, "height": 38, "fieldPath": "name"},
		],
	},
	# ── Purchase Receipt · Thermal 58mm (2) ────────────────────────────
	{
		"template_name": "PR Compact Ticket (58mm)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "Thermal 58mm",
		"is_default": True,
		"margin_mm": [3, 3, 3, 3],
		"elements": [
			{"id": "starter-pr58a-title", "kind": "text", "x": 3, "y": 3, "width": 52, "height": 7, "staticText": "GOODS IN", "fontSize": 11, "bold": True, "align": "center"},
			{"id": "starter-pr58a-num", "kind": "field", "x": 3, "y": 11, "width": 52, "height": 6, "fieldPath": "name", "label": "Receipt #", "fontSize": 8, "bold": True, "align": "center"},
			{"id": "starter-pr58a-date", "kind": "field", "x": 3, "y": 18, "width": 52, "height": 5, "fieldPath": "posting_date", "label": "Date", "fontSize": 7, "align": "center"},
			{"id": "starter-pr58a-sup", "kind": "field", "x": 3, "y": 25, "width": 52, "height": 8, "fieldPath": "supplier_name", "label": "Supplier", "fontSize": 8, "align": "left"},
			{
				"id": "starter-pr58a-items",
				"kind": "line-items",
				"x": 3,
				"y": 36,
				"width": 52,
				"height": 50,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_code", "label": "Item", "width": 34},
					{"fieldPath": "qty", "label": "Qty", "width": 18},
				],
			},
			{"id": "starter-pr58a-qty-label", "kind": "text", "x": 3, "y": 90, "width": 28, "height": 5, "staticText": "Total Qty", "fontSize": 7, "bold": True, "align": "left"},
			{"id": "starter-pr58a-qty", "kind": "field", "x": 30, "y": 90, "width": 25, "height": 5, "fieldPath": "total_qty", "label": "Qty", "fontSize": 8, "bold": True, "align": "right"},
			{"id": "starter-pr58a-barcode", "kind": "barcode", "x": 5, "y": 98, "width": 48, "height": 14, "fieldPath": "name"},
		],
	},
	{
		"template_name": "PR Receiving Stub (58mm)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "Thermal 58mm",
		"margin_mm": [3, 3, 3, 3],
		"elements": [
			{"id": "starter-pr58b-title", "kind": "text", "x": 3, "y": 3, "width": 52, "height": 6, "staticText": "RECEIVING", "fontSize": 10, "bold": True, "align": "left"},
			{"id": "starter-pr58b-num", "kind": "field", "x": 3, "y": 10, "width": 34, "height": 6, "fieldPath": "name", "label": "Receipt #", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-pr58b-qr", "kind": "qrcode", "x": 38, "y": 10, "width": 17, "height": 17, "fieldPath": "name"},
			{"id": "starter-pr58b-date", "kind": "field", "x": 3, "y": 18, "width": 34, "height": 5, "fieldPath": "posting_date", "label": "Date", "fontSize": 7, "align": "left"},
			{"id": "starter-pr58b-sup", "kind": "field", "x": 3, "y": 30, "width": 52, "height": 8, "fieldPath": "supplier_name", "label": "Supplier", "fontSize": 8, "align": "left"},
			{"id": "starter-pr58b-wh", "kind": "field", "x": 3, "y": 40, "width": 52, "height": 6, "fieldPath": "set_warehouse", "label": "Warehouse", "fontSize": 7, "align": "left"},
			{"id": "starter-pr58b-qty-label", "kind": "text", "x": 3, "y": 50, "width": 28, "height": 5, "staticText": "Total Qty", "fontSize": 7, "align": "left"},
			{"id": "starter-pr58b-qty", "kind": "field", "x": 30, "y": 50, "width": 25, "height": 5, "fieldPath": "total_qty", "label": "Qty", "fontSize": 9, "bold": True, "align": "right"},
		],
	},
	# ── Purchase Receipt · Thermal 80mm (2) ────────────────────────────
	{
		"template_name": "PR Receiving Ticket (80mm)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "Thermal 80mm",
		"is_default": True,
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-pr80a-title", "kind": "text", "x": 4, "y": 4, "width": 72, "height": 8, "staticText": "PURCHASE RECEIPT", "fontSize": 11, "bold": True, "align": "center"},
			{"id": "starter-pr80a-num", "kind": "field", "x": 4, "y": 13, "width": 72, "height": 7, "fieldPath": "name", "label": "Receipt #", "fontSize": 10, "bold": True, "align": "center"},
			{"id": "starter-pr80a-date", "kind": "field", "x": 4, "y": 21, "width": 72, "height": 5, "fieldPath": "posting_date", "label": "Date", "fontSize": 8, "align": "center"},
			{"id": "starter-pr80a-sup-label", "kind": "text", "x": 4, "y": 29, "width": 72, "height": 5, "staticText": "Supplier", "fontSize": 7, "bold": True, "align": "left"},
			{"id": "starter-pr80a-sup", "kind": "field", "x": 4, "y": 34, "width": 72, "height": 7, "fieldPath": "supplier_name", "label": "Supplier", "fontSize": 9, "align": "left"},
			{
				"id": "starter-pr80a-items",
				"kind": "line-items",
				"x": 4,
				"y": 44,
				"width": 72,
				"height": 55,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_code", "label": "Item", "width": 36},
					{"fieldPath": "qty", "label": "Qty", "width": 18},
					{"fieldPath": "uom", "label": "UOM", "width": 18},
				],
			},
			{"id": "starter-pr80a-qty-label", "kind": "text", "x": 4, "y": 104, "width": 36, "height": 6, "staticText": "Total Qty", "fontSize": 8, "bold": True, "align": "left"},
			{"id": "starter-pr80a-qty", "kind": "field", "x": 40, "y": 104, "width": 36, "height": 6, "fieldPath": "total_qty", "label": "Qty", "fontSize": 10, "bold": True, "align": "right"},
			{"id": "starter-pr80a-barcode", "kind": "barcode", "x": 8, "y": 114, "width": 64, "height": 16, "fieldPath": "name"},
		],
	},
	{
		"template_name": "PR Warehouse Stub (80mm)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "Thermal 80mm",
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-pr80b-title", "kind": "text", "x": 4, "y": 4, "width": 50, "height": 7, "staticText": "WAREHOUSE IN", "fontSize": 11, "bold": True, "align": "left"},
			{"id": "starter-pr80b-qr", "kind": "qrcode", "x": 56, "y": 4, "width": 20, "height": 20, "fieldPath": "name"},
			{"id": "starter-pr80b-num", "kind": "field", "x": 4, "y": 13, "width": 50, "height": 6, "fieldPath": "name", "label": "Receipt #", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-pr80b-date", "kind": "field", "x": 4, "y": 20, "width": 50, "height": 5, "fieldPath": "posting_date", "label": "Date", "fontSize": 8, "align": "left"},
			{"id": "starter-pr80b-wh-label", "kind": "text", "x": 4, "y": 28, "width": 72, "height": 5, "staticText": "Put-away warehouse", "fontSize": 7, "bold": True, "align": "left"},
			{"id": "starter-pr80b-wh", "kind": "field", "x": 4, "y": 33, "width": 72, "height": 7, "fieldPath": "set_warehouse", "label": "Warehouse", "fontSize": 9, "align": "left"},
			{"id": "starter-pr80b-sup", "kind": "field", "x": 4, "y": 42, "width": 72, "height": 7, "fieldPath": "supplier_name", "label": "Supplier", "fontSize": 8, "align": "left"},
			{"id": "starter-pr80b-qty-label", "kind": "text", "x": 4, "y": 52, "width": 36, "height": 6, "staticText": "Total Qty", "fontSize": 8, "align": "left"},
			{"id": "starter-pr80b-qty", "kind": "field", "x": 40, "y": 52, "width": 36, "height": 6, "fieldPath": "total_qty", "label": "Qty", "fontSize": 11, "bold": True, "align": "right"},
			{"id": "starter-pr80b-barcode", "kind": "barcode", "x": 8, "y": 62, "width": 64, "height": 16, "fieldPath": "name"},
		],
	},
	# ── Catalog Header (default banner, i030 A1) ───────────────────────
	{
		"template_name": "Default Catalog Header",
		"source_doctype": "Catalog Header",
		"paper_kind": "A4",
		"is_default": True,
		"canvas_width_mm": 190,
		"canvas_height_mm": 80,
		"margin_mm": [0, 0, 0, 0],
		"elements": [
			{"id": "starter-hdr-heading", "kind": "field", "x": 5, "y": 5, "width": 120, "height": 24, "fieldPath": "heading", "label": "Heading", "fontSize": 30, "bold": True, "align": "left"},
			{"id": "starter-hdr-desc", "kind": "field", "x": 5, "y": 30, "width": 120, "height": 10, "fieldPath": "description", "label": "Description", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-hdr-wa-label", "kind": "text", "x": 5, "y": 42, "width": 24, "height": 8, "staticText": "WhatsApp:", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-hdr-wa-phone", "kind": "field", "x": 29, "y": 42, "width": 60, "height": 8, "fieldPath": "phone", "label": "Phone", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-hdr-qr-frame", "kind": "shape", "x": 150, "y": 8, "width": 36, "height": 36, "shapeType": "rect", "color": "#93c5fd"},
			{"id": "starter-hdr-qr", "kind": "qrcode", "x": 152, "y": 10, "width": 32, "height": 32, "fieldPath": "qr_value"},
		],
	},
	# ── Catalog Card (default product card, i030 A2) ───────────────────
	{
		"template_name": "Default Catalog Card",
		"source_doctype": "Item",
		"paper_kind": "Catalog Card",
		"is_default": True,
		"margin_mm": [0, 0, 0, 0],
		"elements": [
			{"id": "starter-card-image", "kind": "image", "x": 2, "y": 2, "width": 44, "height": 32, "fieldPath": "image"},
			{"id": "starter-card-price-pill", "kind": "shape", "x": 9, "y": 30, "width": 30, "height": 9, "shapeType": "rect", "color": "#dbeafe", "filled": True, "bgColor": "#dbeafe"},
			{"id": "starter-card-price", "kind": "field", "x": 9, "y": 31, "width": 30, "height": 7, "fieldPath": "display_price", "label": "Catalog Price", "fontSize": 13, "bold": True, "align": "center"},
			{"id": "starter-card-title", "kind": "field", "x": 2, "y": 42, "width": 44, "height": 8, "fieldPath": "normalized_title", "label": "Normalized Title", "fontSize": 7, "bold": True, "align": "left"},
			{"id": "starter-card-source-title", "kind": "field", "x": 2, "y": 49, "width": 44, "height": 6, "fieldPath": "item_name", "label": "Item Name", "fontSize": 6, "align": "left"},
			{"id": "starter-card-brand", "kind": "field", "x": 2, "y": 54, "width": 44, "height": 5, "fieldPath": "brand", "label": "Brand", "fontSize": 6, "align": "left"},
			{"id": "starter-card-barcode", "kind": "barcode", "x": 2, "y": 59, "width": 44, "height": 6, "fieldPath": "barcode"},
		],
	},
	# ── TMS delivery tickets (Delivery Note · Thermal 80mm) ─────────────
	{
		"template_name": "Confirmación de Entrega (80mm)",
		"source_doctype": "Delivery Note",
		"paper_kind": "Thermal 80mm",
		"is_default": True,
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-dnconf-title", "kind": "text", "x": 4, "y": 4, "width": 72, "height": 8, "staticText": "Confirmación de Entrega", "fontSize": 12, "bold": True, "align": "center"},
			{"id": "starter-dnconf-ref-label", "kind": "text", "x": 4, "y": 14, "width": 30, "height": 5, "staticText": "Comprobante", "fontSize": 7, "align": "left"},
			{"id": "starter-dnconf-ref", "kind": "field", "x": 4, "y": 19, "width": 72, "height": 6, "fieldPath": "name", "label": "Delivery Note", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnconf-customer-label", "kind": "text", "x": 4, "y": 27, "width": 30, "height": 5, "staticText": "Cliente", "fontSize": 7, "align": "left"},
			{"id": "starter-dnconf-customer", "kind": "field", "x": 4, "y": 32, "width": 72, "height": 6, "fieldPath": "customer_name", "label": "Customer", "fontSize": 9, "align": "left"},
			{"id": "starter-dnconf-date-label", "kind": "text", "x": 4, "y": 40, "width": 30, "height": 5, "staticText": "Fecha", "fontSize": 7, "align": "left"},
			{"id": "starter-dnconf-date", "kind": "field", "x": 4, "y": 45, "width": 72, "height": 6, "fieldPath": "posting_date", "label": "Date", "fontSize": 9, "align": "left"},
			{
				"id": "starter-dnconf-items",
				"kind": "line-items",
				"x": 4,
				"y": 53,
				"width": 72,
				"height": 40,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_name", "label": "Item", "width": 44},
					{"fieldPath": "qty", "label": "Cant", "width": 14},
					{"fieldPath": "amount", "label": "Monto", "width": 14},
				],
			},
			{"id": "starter-dnconf-total-label", "kind": "text", "x": 4, "y": 96, "width": 30, "height": 6, "staticText": "Total", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnconf-total", "kind": "field", "x": 34, "y": 96, "width": 42, "height": 8, "fieldPath": "grand_total", "label": "Total", "fontSize": 11, "bold": True, "align": "right"},
			{"id": "starter-dnconf-tracking-label", "kind": "text", "x": 4, "y": 106, "width": 30, "height": 5, "staticText": "Seguimiento", "fontSize": 7, "align": "left"},
			{"id": "starter-dnconf-tracking", "kind": "field", "x": 4, "y": 111, "width": 72, "height": 6, "fieldPath": "custom_tracking_code", "label": "Tracking Code", "fontSize": 9, "align": "left"},
			{"id": "starter-dnconf-qr", "kind": "qrcode", "x": 4, "y": 119, "width": 30, "height": 30, "fieldPath": "custom_tracking_code"},
		],
	},
	{
		"template_name": "Recibo de Pago (80mm)",
		"source_doctype": "Delivery Note",
		"paper_kind": "Thermal 80mm",
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-dnpay-title", "kind": "text", "x": 4, "y": 4, "width": 72, "height": 8, "staticText": "Recibo de Pago", "fontSize": 12, "bold": True, "align": "center"},
			{"id": "starter-dnpay-ref", "kind": "field", "x": 4, "y": 15, "width": 72, "height": 6, "fieldPath": "name", "label": "Delivery Note", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnpay-customer", "kind": "field", "x": 4, "y": 23, "width": 72, "height": 6, "fieldPath": "customer_name", "label": "Customer", "fontSize": 9, "align": "left"},
			{"id": "starter-dnpay-date", "kind": "field", "x": 4, "y": 31, "width": 72, "height": 6, "fieldPath": "posting_date", "label": "Date", "fontSize": 9, "align": "left"},
			{"id": "starter-dnpay-total-label", "kind": "text", "x": 4, "y": 41, "width": 30, "height": 6, "staticText": "Total", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnpay-total", "kind": "field", "x": 34, "y": 41, "width": 42, "height": 8, "fieldPath": "grand_total", "label": "Total", "fontSize": 11, "bold": True, "align": "right"},
		],
	},
	{
		"template_name": "Recibo de Devolución (80mm)",
		"source_doctype": "Delivery Note",
		"paper_kind": "Thermal 80mm",
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-dnret-title", "kind": "text", "x": 4, "y": 4, "width": 72, "height": 8, "staticText": "Recibo de Devolución", "fontSize": 12, "bold": True, "align": "center"},
			{"id": "starter-dnret-ref", "kind": "field", "x": 4, "y": 15, "width": 72, "height": 6, "fieldPath": "name", "label": "Delivery Note", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnret-customer", "kind": "field", "x": 4, "y": 23, "width": 72, "height": 6, "fieldPath": "customer_name", "label": "Customer", "fontSize": 9, "align": "left"},
			{"id": "starter-dnret-date", "kind": "field", "x": 4, "y": 31, "width": 72, "height": 6, "fieldPath": "posting_date", "label": "Date", "fontSize": 9, "align": "left"},
			{
				"id": "starter-dnret-items",
				"kind": "line-items",
				"x": 4,
				"y": 41,
				"width": 72,
				"height": 40,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_name", "label": "Item", "width": 44},
					{"fieldPath": "qty", "label": "Cant", "width": 28},
				],
			},
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

		width_mm = starter.get("canvas_width_mm") or _PAPER_SIZE_MM.get(starter["paper_kind"], (210, 297))[0]
		height_mm = starter.get("canvas_height_mm") or _PAPER_SIZE_MM.get(starter["paper_kind"], (210, 297))[1]
		want_default = bool(starter.get("is_default"))
		already_has_default = bool(
			frappe.db.exists(
				"ECommerce Print Template",
				{
					"source_doctype": starter["source_doctype"],
					"paper_kind": starter["paper_kind"],
					"is_default": 1,
				},
			)
		)
		make_default = want_default and not already_has_default

		doc = frappe.new_doc("ECommerce Print Template")
		doc.template_name = starter["template_name"]
		doc.source_doctype = starter["source_doctype"]
		doc.paper_kind = starter["paper_kind"]
		doc.status = "Production"
		doc.is_default = 1 if make_default else 0
		doc.margin_mm = json.dumps(starter["margin_mm"])
		doc.elements_data = json.dumps(starter["elements"])
		doc.notes_data = "[]"
		doc.canvas_width_mm = width_mm
		doc.canvas_height_mm = height_mm
		doc.insert(ignore_permissions=True)
		if make_default:
			_apply_default(doc.name, doc.source_doctype, doc.paper_kind)
		created.append(doc.name)

	if created:
		frappe.db.commit()
	return {"created": created}
