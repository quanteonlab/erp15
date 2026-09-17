import json

import frappe
from frappe import _

# Fields exposed per source doctype, plus the child-table (line items) fieldname
# used by the 'line-items' element kind in the template designer.
_SOURCE_DOCTYPE_CHILD_TABLE = {
	"Sales Invoice": "items",
	"Purchase Receipt": "items",
	"Delivery Note": "items",
	"Sales Order": "items",
	"Item": None,
	# Synthetic — not a real Frappe doctype. The catalog PDF binds these fields
	# at export time from the current export settings (heading/phone/description),
	# not from a stored document.
	"Catalog Header": None,
	# Synthetic — preview/print binds from Employee (+ staff login barcode store).
	"Staff Cred.": None,
	# Synthetic — armado de pedido / picking list bound from a Sales Order (+ floor map).
	"Delivery Checklist": "items",
}

# Field list for the synthetic "Catalog Header" source doctype (see above).
_CATALOG_HEADER_FIELDS = [
	{"fieldname": "heading", "label": "Heading", "fieldtype": "Data"},
	{"fieldname": "description", "label": "Description", "fieldtype": "Small Text"},
	{"fieldname": "phone", "label": "WhatsApp Phone", "fieldtype": "Data"},
	{"fieldname": "qr_value", "label": "QR Value (wa.me link)", "fieldtype": "Data"},
	{"fieldname": "company_name", "label": "Company Name", "fieldtype": "Data"},
]

# Field list for the synthetic "Staff Cred." source (Labels / ID card designer).
_STAFF_CRED_FIELDS = [
	{"fieldname": "org_name", "label": "Organization", "fieldtype": "Data"},
	{"fieldname": "company_name", "label": "Company Name", "fieldtype": "Data"},
	{"fieldname": "employee_name", "label": "Employee Name", "fieldtype": "Data"},
	{"fieldname": "designation", "label": "Designation", "fieldtype": "Data"},
	{"fieldname": "department", "label": "Department", "fieldtype": "Data"},
	{"fieldname": "employee_id", "label": "Employee ID", "fieldtype": "Data"},
	{"fieldname": "date_of_joining", "label": "Date of Joining", "fieldtype": "Date"},
	{"fieldname": "expiration", "label": "Expiration", "fieldtype": "Date"},
	{"fieldname": "barcode", "label": "Login Barcode", "fieldtype": "Data"},
	{"fieldname": "image", "label": "Photo", "fieldtype": "Attach Image"},
]

# Synthetic "Delivery Checklist" (armado de pedido) — bound from Sales Order + warehouse/floor.
_DELIVERY_CHECKLIST_FIELDS = [
	{"fieldname": "name", "label": "Order Nº", "fieldtype": "Data"},
	{"fieldname": "customer_name", "label": "Cliente", "fieldtype": "Data"},
	{"fieldname": "tax_id", "label": "Numero de identificador", "fieldtype": "Data"},
	{"fieldname": "delivery_date", "label": "Fecha de Envio", "fieldtype": "Date"},
	{"fieldname": "transaction_date", "label": "Order Date", "fieldtype": "Date"},
	{"fieldname": "warehouse_name", "label": "Almacen", "fieldtype": "Data"},
	{"fieldname": "net_total", "label": "Subtotal", "fieldtype": "Currency"},
	{"fieldname": "grand_total", "label": "TOTAL", "fieldtype": "Currency"},
	{"fieldname": "_map", "label": "Warehouse Map", "fieldtype": "JSON"},
]

_DELIVERY_CHECKLIST_CHILD_FIELDS = [
	{"fieldname": "item_code", "label": "Código", "fieldtype": "Data"},
	{"fieldname": "item_name", "label": "Descripción", "fieldtype": "Data"},
	{"fieldname": "qty", "label": "Cantidad", "fieldtype": "Float"},
	{"fieldname": "warehouse", "label": "Desde", "fieldtype": "Data"},
	{"fieldname": "location", "label": "Ubicación", "fieldtype": "Data"},
	{"fieldname": "barcode", "label": "Codigo de Barras", "fieldtype": "Data"},
	{"fieldname": "amount", "label": "Importe", "fieldtype": "Currency"},
	{"fieldname": "rate", "label": "Precio", "fieldtype": "Currency"},
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

# Keep Select options in sync with code even if `bench migrate` has not run yet
# (starter gift from /logistica/prints would otherwise ValidationError on new sources).
_SOURCE_DOCTYPE_SELECT_OPTIONS = "\n".join(
	[
		"Sales Invoice",
		"Purchase Receipt",
		"Delivery Note",
		"Sales Order",
		"Delivery Checklist",
		"Item",
		"Catalog Header",
		"Staff Cred.",
	]
)


def _ensure_source_doctype_select_options():
	"""Patch DocField options when JSON/code is ahead of the site's DocType cache."""
	try:
		row = frappe.db.get_value(
			"DocField",
			{"parent": "ECommerce Print Template", "fieldname": "source_doctype"},
			["name", "options"],
			as_dict=True,
		)
		if not row:
			return
		if (row.options or "").strip() == _SOURCE_DOCTYPE_SELECT_OPTIONS:
			return
		frappe.db.set_value(
			"DocField",
			row.name,
			"options",
			_SOURCE_DOCTYPE_SELECT_OPTIONS,
			update_modified=False,
		)
		frappe.clear_cache(doctype="ECommerce Print Template")
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="print_templates_api: sync source_doctype options")


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


def _staff_cred_print_data(docname):
	"""Map an Employee (+ login barcode) into the synthetic Staff Cred. print doc."""
	docname = (docname or "").strip()
	if not docname or not frappe.db.exists("Employee", docname):
		frappe.throw(_("Employee {0} not found").format(docname or "—"), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	emp = frappe.get_doc("Employee", docname)
	frappe.flags.ignore_permissions = False

	login_barcode = ""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import _load_staff_login_store

		store = _load_staff_login_store()
		entry = (store.get("by_employee") or {}).get(emp.name)
		if isinstance(entry, dict):
			login_barcode = str(entry.get("code") or "").strip()
	except Exception:
		login_barcode = ""

	company_name = (emp.company or "").strip() or (frappe.defaults.get_global_default("company") or "")
	data = {
		"name": emp.name,
		"org_name": company_name,
		"company_name": company_name,
		"employee_name": emp.employee_name or emp.name,
		"designation": emp.designation or "",
		"department": emp.department or "",
		"employee_id": emp.name,
		"date_of_joining": str(emp.date_of_joining) if emp.date_of_joining else "",
		"expiration": "",
		"barcode": login_barcode,
		"image": emp.image or "",
	}
	return {"doc": data, "lineItems": []}


@frappe.whitelist(allow_guest=True)
def get_doctype_fields(source_doctype, paper_kind=None):
	if source_doctype == "Catalog Header":
		# Synthetic doctype — no frappe.get_meta lookup, fields are fixed.
		return {"fields": list(_CATALOG_HEADER_FIELDS), "childTableFieldname": None, "childTableFields": []}
	if source_doctype == "Staff Cred.":
		return {"fields": list(_STAFF_CRED_FIELDS), "childTableFieldname": None, "childTableFields": []}
	if source_doctype == "Delivery Checklist":
		return {
			"fields": list(_DELIVERY_CHECKLIST_FIELDS),
			"childTableFieldname": "items",
			"childTableFields": list(_DELIVERY_CHECKLIST_CHILD_FIELDS),
		}

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


def _item_barcode(item_code: str) -> str:
	if not item_code:
		return ""
	try:
		rows = frappe.get_all(
			"Item Barcode",
			filters={"parent": item_code},
			fields=["barcode"],
			limit=1,
			ignore_permissions=True,
		)
		if rows:
			return rows[0].barcode or ""
	except Exception:
		pass
	return frappe.db.get_value("Item", item_code, "item_code") or item_code


def _default_warehouse_name(company=None) -> str:
	wh = None
	if company and frappe.db.has_column("Company", "custom_default_warehouse"):
		wh = frappe.db.get_value("Company", company, "custom_default_warehouse")
	if not wh:
		wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if not wh:
		wh = frappe.db.get_value("Warehouse", {"is_group": 0}, "name", order_by="modified desc")
	return wh or ""


def _floor_sku_locations(floor_id=None, company=None):
	"""Map item_code → {location, section_id} from ECommerce Floor Map racks."""
	filters = {}
	try:
		if company and frappe.db.has_column("ECommerce Floor Map", "company"):
			filters["company"] = company
	except Exception:
		pass
	if floor_id:
		filters["name"] = floor_id

	floors = frappe.get_all(
		"ECommerce Floor Map",
		filters=filters or None,
		fields=["name", "sections_data", "location_name", "floor_name", "canvas_width", "canvas_height"],
		order_by="modified desc",
		ignore_permissions=True,
	)
	sku_to_loc = {}
	map_payload = None
	for f in floors:
		sections = []
		try:
			sections = json.loads(f.sections_data or "[]")
		except Exception:
			sections = []
		highlighted = set()
		for s in sections:
			if not isinstance(s, dict):
				continue
			code = (s.get("code") or s.get("name") or "").strip()
			skus = list(s.get("products") or [])
			for row in s.get("productRows") or []:
				if isinstance(row, dict) and row.get("sku"):
					skus.append(row["sku"])
			for sku in skus:
				sku = (sku or "").strip()
				if not sku:
					continue
				if sku not in sku_to_loc:
					sku_to_loc[sku] = {"location": code, "section_id": s.get("id")}
					if s.get("id"):
						highlighted.add(s.get("id"))
		if map_payload is None:
			map_payload = {
				"floor_id": f.name,
				"title": f"{f.location_name} / {f.floor_name}",
				"canvas": {"width": f.canvas_width or 1400, "height": f.canvas_height or 900},
				"sections": [
					{
						"id": s.get("id"),
						"code": s.get("code") or "",
						"name": s.get("name") or "",
						"x": s.get("x") or 0,
						"y": s.get("y") or 0,
						"width": s.get("width") or 0,
						"height": s.get("height") or 0,
						"color": s.get("color") or "#94a3b8",
						"highlight": False,
					}
					for s in sections
					if isinstance(s, dict)
				],
			}
			# highlight filled after we know which SKUs matter — caller updates
			map_payload["_sku_to_loc"] = sku_to_loc
		# Prefer first matching floor when floor_id set; otherwise first floor with sections
		if floor_id or sections:
			break
	return sku_to_loc, map_payload


def _delivery_checklist_print_data(sales_order_name, warehouse=None, floor_id=None):
	if not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(_("Sales Order {0} not found").format(sales_order_name), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", sales_order_name)
	frappe.flags.ignore_permissions = False

	company = so.company
	wh = warehouse or _default_warehouse_name(company)
	sku_to_loc, map_payload = _floor_sku_locations(floor_id=floor_id, company=company)

	tax_id = ""
	if so.customer:
		tax_id = frappe.db.get_value("Customer", so.customer, "tax_id") or ""

	rows = []
	highlight_ids = set()
	for it in so.items or []:
		loc_info = sku_to_loc.get(it.item_code) or {}
		loc = loc_info.get("location") or ""
		sid = loc_info.get("section_id")
		if sid:
			highlight_ids.add(sid)
		rows.append(
			{
				"item_code": it.item_code,
				"item_name": it.item_name,
				"qty": it.qty,
				"rate": it.rate,
				"amount": it.amount,
				"warehouse": it.warehouse or wh,
				"location": loc,
				"barcode": _item_barcode(it.item_code),
			}
		)

	map_out = None
	if map_payload:
		map_out = {
			"floor_id": map_payload["floor_id"],
			"title": map_payload["title"],
			"canvas": map_payload["canvas"],
			"sections": [
				{**s, "highlight": s.get("id") in highlight_ids}
				for s in map_payload["sections"]
			],
		}

	doc = {
		"name": so.name,
		"customer": so.customer,
		"customer_name": so.customer_name or so.customer,
		"tax_id": tax_id,
		"delivery_date": so.delivery_date,
		"transaction_date": so.transaction_date,
		"warehouse_name": wh,
		"net_total": so.net_total,
		"grand_total": so.grand_total,
		"company": company,
		"_map": map_out,
	}
	return {"doc": doc, "lineItems": rows, "map": map_out}


@frappe.whitelist(allow_guest=True)
def get_print_data(source_doctype, docname, warehouse=None, floor_id=None):
	if source_doctype == "Staff Cred.":
		return _staff_cred_print_data(docname)

	if source_doctype == "Delivery Checklist":
		return _delivery_checklist_print_data(docname, warehouse=warehouse, floor_id=floor_id)

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


@frappe.whitelist(allow_guest=True)
def get_order_print_bundle(sales_order, warehouse=None, floor_id=None):
	"""Resolve linked commercial docs + Delivery Checklist data for the orders print modal."""
	if not sales_order:
		frappe.throw(_("sales_order is required"))
	if not frappe.db.exists("Sales Order", sales_order):
		frappe.throw(_("Sales Order {0} not found").format(sales_order), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", sales_order)
	frappe.flags.ignore_permissions = False

	# Linked Delivery Note (via DN Item against_sales_order)
	dn = frappe.db.get_value(
		"Delivery Note Item",
		{"against_sales_order": sales_order, "docstatus": ["!=", 2]},
		"parent",
	)
	# Linked Sales Invoice
	si = frappe.db.get_value(
		"Sales Invoice Item",
		{"sales_order": sales_order, "docstatus": ["!=", 2]},
		"parent",
	)
	# Purchase Receipt is unusual for SO; still surface if a return/receipt was linked somehow
	pr = None

	advance = float(so.advance_paid or 0)
	grand = float(so.grand_total or 0)
	paid = advance >= grand - 0.01 if grand else advance > 0

	wh = warehouse or _default_warehouse_name(so.company)
	warehouses = frappe.get_all(
		"Warehouse",
		filters={"is_group": 0, "company": so.company} if so.company else {"is_group": 0},
		fields=["name"],
		order_by="name asc",
		limit_page_length=200,
		ignore_permissions=True,
	)

	floors_raw = frappe.get_all(
		"ECommerce Floor Map",
		fields=["name", "location_name", "floor_name"],
		order_by="modified desc",
		ignore_permissions=True,
		limit_page_length=50,
	)

	checklist = _delivery_checklist_print_data(sales_order, warehouse=wh, floor_id=floor_id)

	return {
		"sales_order": so.name,
		"customer_name": so.customer_name or so.customer,
		"grand_total": grand,
		"advance_paid": advance,
		"paid": paid,
		"currency": so.currency,
		"default_warehouse": wh,
		"warehouses": [w.name for w in warehouses],
		"floors": [
			{"id": f.name, "title": f"{f.location_name} / {f.floor_name}"} for f in floors_raw
		],
		"links": {
			"sales_invoice": si,
			"delivery_note": dn,
			"purchase_receipt": pr,
		},
		"checklist": checklist,
		"item_codes": [it.item_code for it in (so.items or []) if it.item_code],
	}


_PAPER_SIZE_MM = {
	"A4": (210, 297),
	"Thermal 58mm": (58, 150),
	"Thermal 80mm": (80, 150),
	# One catalog print-grid card cell (approximates today's hardcoded PrintCard aspect).
	"Catalog Card": (48, 66),
}


def _ar_documento_no_valido_a4_elements(*, party_label: str, party_field: str, id_prefix: str):
	"""A4 commercial layout matching 'DOCUMENTO NO VALIDO COMO FACTURA'.

	Used for both Sales Invoice (Cliente) and Purchase Receipt (Proveedor).
	Columns include Precio Kg. / Precio U. for bazar weight+unit mixes.
	"""
	p = id_prefix
	return [
		# Header (right)
		{
			"id": f"{p}-title",
			"kind": "text",
			"x": 95,
			"y": 12,
			"width": 100,
			"height": 10,
			"staticText": "DOCUMENTO NO VALIDO COMO FACTURA",
			"fontSize": 11,
			"bold": True,
			"align": "right",
		},
		{
			"id": f"{p}-nro-label",
			"kind": "text",
			"x": 130,
			"y": 24,
			"width": 18,
			"height": 6,
			"staticText": "N°",
			"fontSize": 9,
			"bold": True,
			"align": "right",
		},
		{
			"id": f"{p}-nro",
			"kind": "field",
			"x": 148,
			"y": 24,
			"width": 47,
			"height": 6,
			"fieldPath": "name",
			"label": "N°",
			"fontSize": 9,
			"bold": True,
			"align": "right",
		},
		{
			"id": f"{p}-fecha-label",
			"kind": "text",
			"x": 130,
			"y": 32,
			"width": 18,
			"height": 6,
			"staticText": "Fecha:",
			"fontSize": 9,
			"align": "right",
		},
		{
			"id": f"{p}-fecha",
			"kind": "field",
			"x": 148,
			"y": 32,
			"width": 47,
			"height": 6,
			"fieldPath": "posting_date",
			"label": "Fecha",
			"fontSize": 9,
			"align": "right",
		},
		# Party block (left)
		{
			"id": f"{p}-party-label",
			"kind": "text",
			"x": 15,
			"y": 48,
			"width": 28,
			"height": 6,
			"staticText": f"{party_label} :",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-party",
			"kind": "field",
			"x": 43,
			"y": 48,
			"width": 85,
			"height": 6,
			"fieldPath": party_field,
			"label": party_label,
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-dir-label",
			"kind": "text",
			"x": 15,
			"y": 56,
			"width": 28,
			"height": 6,
			"staticText": "Dirección :",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-dir",
			"kind": "field",
			"x": 43,
			"y": 56,
			"width": 85,
			"height": 6,
			"fieldPath": "address_display",
			"label": "Dirección",
			"fontSize": 8,
			"align": "left",
		},
		{
			"id": f"{p}-iva-label",
			"kind": "text",
			"x": 15,
			"y": 64,
			"width": 28,
			"height": 6,
			"staticText": "IVA:",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-iva",
			"kind": "field",
			"x": 43,
			"y": 64,
			"width": 50,
			"height": 6,
			"fieldPath": "tax_category",
			"label": "IVA",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-cuit-label",
			"kind": "text",
			"x": 100,
			"y": 64,
			"width": 18,
			"height": 6,
			"staticText": "CUIT:",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-cuit",
			"kind": "field",
			"x": 118,
			"y": 64,
			"width": 50,
			"height": 6,
			"fieldPath": "tax_id",
			"label": "CUIT",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-ri-label",
			"kind": "text",
			"x": 15,
			"y": 72,
			"width": 42,
			"height": 6,
			"staticText": "Responsable Inscripto :",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-ri",
			"kind": "text",
			"x": 57,
			"y": 72,
			"width": 40,
			"height": 6,
			"staticText": "",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-cond-label",
			"kind": "text",
			"x": 100,
			"y": 72,
			"width": 40,
			"height": 6,
			"staticText": "Condición de Venta :",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-cond",
			"kind": "field",
			"x": 140,
			"y": 72,
			"width": 55,
			"height": 6,
			"fieldPath": "payment_terms_template",
			"label": "Condición",
			"fontSize": 9,
			"align": "left",
		},
		# Line items — navy header bar like the paper form
		{
			"id": f"{p}-items",
			"kind": "line-items",
			"x": 12,
			"y": 86,
			"width": 186,
			"height": 120,
			"childTableFieldname": "items",
			"headerBg": "#1e3a5f",
			"headerColor": "#ffffff",
			"columns": [
				{"fieldPath": "item_code", "label": "Código", "width": 22},
				{"fieldPath": "item_name", "label": "Descripción", "width": 48},
				{"fieldPath": "uom", "label": "Unidades", "width": 22},
				{"fieldPath": "qty", "label": "Cantidad", "width": 18},
				{"fieldPath": "price_list_rate", "label": "Precio Kg.", "width": 22},
				{"fieldPath": "rate", "label": "Precio U.", "width": 20},
				{"fieldPath": "discount_percentage", "label": "Descuento", "width": 16},
				{"fieldPath": "amount", "label": "Importe", "width": 18},
			],
		},
		# Totals (right)
		{
			"id": f"{p}-sub-label",
			"kind": "text",
			"x": 130,
			"y": 220,
			"width": 30,
			"height": 7,
			"staticText": "Subtotal",
			"fontSize": 10,
			"align": "right",
		},
		{
			"id": f"{p}-sub",
			"kind": "field",
			"x": 160,
			"y": 220,
			"width": 35,
			"height": 7,
			"fieldPath": "net_total",
			"label": "Subtotal",
			"fontSize": 10,
			"align": "right",
		},
		{
			"id": f"{p}-total-label",
			"kind": "text",
			"x": 130,
			"y": 230,
			"width": 30,
			"height": 8,
			"staticText": "TOTAL",
			"fontSize": 12,
			"bold": True,
			"align": "right",
		},
		{
			"id": f"{p}-total",
			"kind": "field",
			"x": 160,
			"y": 230,
			"width": 35,
			"height": 8,
			"fieldPath": "grand_total",
			"label": "TOTAL",
			"fontSize": 12,
			"bold": True,
			"align": "right",
		},
		# Signature
		{
			"id": f"{p}-firma-label",
			"kind": "text",
			"x": 110,
			"y": 255,
			"width": 85,
			"height": 6,
			"staticText": "Recibi Conforme (Firma y Aclaración):",
			"fontSize": 8,
			"align": "left",
		},
		{
			"id": f"{p}-firma-line",
			"kind": "shape",
			"x": 110,
			"y": 268,
			"width": 85,
			"height": 1,
			"shapeType": "line",
			"color": "#0f172a",
		},
	]


def _ar_entregas_checklist_a4_elements(*, id_prefix: str, with_location: bool = False, with_map: bool = False):
	"""A4 picking / armado de pedido layout matching ENTREGAS commercial form."""
	p = id_prefix
	columns = [
		{"fieldPath": "item_code", "label": "Código", "width": 28},
		{"fieldPath": "item_name", "label": "Descripción", "width": 62 if with_location else 72},
		{"fieldPath": "qty", "label": "Cantidad", "width": 20},
		{"fieldPath": "warehouse", "label": "Desde", "width": 28},
	]
	if with_location:
		columns.append({"fieldPath": "location", "label": "Ubicación", "width": 22})
	columns.append({"fieldPath": "barcode", "label": "Codigo de Barras", "width": 26 if with_location else 32})

	items_height = 90 if with_map else 120
	elements = [
		{
			"id": f"{p}-title",
			"kind": "text",
			"x": 15,
			"y": 12,
			"width": 180,
			"height": 14,
			"staticText": "ENTREGAS",
			"fontSize": 22,
			"bold": True,
			"align": "center",
		},
		{
			"id": f"{p}-nro-label",
			"kind": "text",
			"x": 15,
			"y": 28,
			"width": 180,
			"height": 6,
			"staticText": "Nº",
			"fontSize": 10,
			"align": "center",
		},
		{
			"id": f"{p}-nro",
			"kind": "field",
			"x": 15,
			"y": 34,
			"width": 180,
			"height": 7,
			"fieldPath": "name",
			"label": "Nº",
			"fontSize": 11,
			"bold": True,
			"align": "center",
		},
		{
			"id": f"{p}-cli-label",
			"kind": "text",
			"x": 15,
			"y": 48,
			"width": 28,
			"height": 6,
			"staticText": "Cliente :",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-cli",
			"kind": "field",
			"x": 43,
			"y": 48,
			"width": 90,
			"height": 6,
			"fieldPath": "customer_name",
			"label": "Cliente",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-wh-label",
			"kind": "text",
			"x": 145,
			"y": 46,
			"width": 50,
			"height": 5,
			"staticText": "Almacen",
			"fontSize": 8,
			"align": "right",
		},
		{
			"id": f"{p}-wh",
			"kind": "field",
			"x": 130,
			"y": 52,
			"width": 65,
			"height": 10,
			"fieldPath": "warehouse_name",
			"label": "Almacen",
			"fontSize": 14,
			"bold": True,
			"align": "right",
		},
		{
			"id": f"{p}-id-label",
			"kind": "text",
			"x": 15,
			"y": 56,
			"width": 50,
			"height": 6,
			"staticText": "Numero de identificador:",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-id",
			"kind": "field",
			"x": 65,
			"y": 56,
			"width": 60,
			"height": 6,
			"fieldPath": "tax_id",
			"label": "ID",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-ship-label",
			"kind": "text",
			"x": 15,
			"y": 64,
			"width": 40,
			"height": 6,
			"staticText": "Fecha de Envio:",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-ship",
			"kind": "field",
			"x": 55,
			"y": 64,
			"width": 50,
			"height": 6,
			"fieldPath": "delivery_date",
			"label": "Fecha de Envio",
			"fontSize": 9,
			"align": "left",
		},
		{
			"id": f"{p}-items",
			"kind": "line-items",
			"x": 12,
			"y": 78,
			"width": 186,
			"height": items_height,
			"childTableFieldname": "items",
			"headerBg": "#1e3a5f",
			"headerColor": "#ffffff",
			"columns": columns,
		},
	]

	footer_y = 200 if with_map else 210
	if with_map:
		elements.append(
			{
				"id": f"{p}-map",
				"kind": "warehouse-map",
				"x": 12,
				"y": 172,
				"width": 186,
				"height": 55,
				"fieldPath": "_map",
			}
		)
		footer_y = 232

	elements.extend(
		[
			{
				"id": f"{p}-sub-label",
				"kind": "text",
				"x": 130,
				"y": footer_y,
				"width": 30,
				"height": 7,
				"staticText": "Subtotal",
				"fontSize": 10,
				"align": "right",
			},
			{
				"id": f"{p}-sub",
				"kind": "field",
				"x": 160,
				"y": footer_y,
				"width": 35,
				"height": 7,
				"fieldPath": "net_total",
				"label": "Subtotal",
				"fontSize": 10,
				"align": "right",
			},
			{
				"id": f"{p}-total-label",
				"kind": "text",
				"x": 130,
				"y": footer_y + 10,
				"width": 30,
				"height": 8,
				"staticText": "TOTAL",
				"fontSize": 12,
				"bold": True,
				"align": "right",
			},
			{
				"id": f"{p}-total",
				"kind": "field",
				"x": 160,
				"y": footer_y + 10,
				"width": 35,
				"height": 8,
				"fieldPath": "grand_total",
				"label": "TOTAL",
				"fontSize": 12,
				"bold": True,
				"align": "right",
			},
			{
				"id": f"{p}-firma-label",
				"kind": "text",
				"x": 110,
				"y": footer_y + 28,
				"width": 85,
				"height": 6,
				"staticText": "Recibi Conforme (Firma y Aclaración):",
				"fontSize": 8,
				"align": "left",
			},
			{
				"id": f"{p}-firma-line",
				"kind": "shape",
				"x": 110,
				"y": footer_y + 40,
				"width": 85,
				"height": 1,
				"shapeType": "line",
				"color": "#0f172a",
			},
		]
	)
	return elements


_STARTER_TEMPLATES = [
	# ── Sales Invoice · AR commercial A4 (gifted core) ─────────────────
	{
		"template_name": "Documento no válido como factura (A4)",
		"source_doctype": "Sales Invoice",
		"paper_kind": "A4",
		"is_default": False,
		"gift": True,
		"margin_mm": [12, 12, 12, 12],
		"elements": _ar_documento_no_valido_a4_elements(
			party_label="Cliente",
			party_field="customer_name",
			id_prefix="starter-siad",
		),
	},
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
		"template_name": "Documento no válido como factura — Remito (A4)",
		"source_doctype": "Purchase Receipt",
		"paper_kind": "A4",
		"is_default": False,
		"gift": True,
		"margin_mm": [12, 12, 12, 12],
		"elements": _ar_documento_no_valido_a4_elements(
			party_label="Proveedor",
			party_field="supplier_name",
			id_prefix="starter-prad",
		),
	},
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
	# ── Staff Cred. (ID card, Labels-style) ────────────────────────────
	{
		"template_name": "Default Staff Cred.",
		"source_doctype": "Staff Cred.",
		"paper_kind": "A4",
		"is_default": True,
		"canvas_width_mm": 86,
		"canvas_height_mm": 54,
		"margin_mm": [0, 0, 0, 0],
		"elements": [
			{"id": "starter-sc-header", "kind": "shape", "x": 0, "y": 0, "width": 86, "height": 12, "shapeType": "rect", "color": "#1d4ed8", "filled": True, "bgColor": "#1d4ed8"},
			{"id": "starter-sc-org", "kind": "field", "x": 3, "y": 2, "width": 80, "height": 8, "fieldPath": "org_name", "label": "Organization", "fontSize": 10, "bold": True, "align": "center"},
			{"id": "starter-sc-photo", "kind": "image", "x": 3, "y": 14, "width": 22, "height": 26, "fieldPath": "image"},
			{"id": "starter-sc-name", "kind": "field", "x": 28, "y": 14, "width": 55, "height": 8, "fieldPath": "employee_name", "label": "Employee Name", "fontSize": 11, "bold": True, "align": "left"},
			{"id": "starter-sc-role", "kind": "field", "x": 28, "y": 22, "width": 55, "height": 6, "fieldPath": "designation", "label": "Designation", "fontSize": 8, "align": "left"},
			{"id": "starter-sc-id-label", "kind": "text", "x": 28, "y": 29, "width": 12, "height": 5, "staticText": "ID:", "fontSize": 7, "bold": True, "align": "left"},
			{"id": "starter-sc-id", "kind": "field", "x": 40, "y": 29, "width": 43, "height": 5, "fieldPath": "employee_id", "label": "Employee ID", "fontSize": 7, "align": "left"},
			{"id": "starter-sc-join-label", "kind": "text", "x": 28, "y": 34, "width": 14, "height": 5, "staticText": "Joined:", "fontSize": 7, "bold": True, "align": "left"},
			{"id": "starter-sc-join", "kind": "field", "x": 42, "y": 34, "width": 41, "height": 5, "fieldPath": "date_of_joining", "label": "Date of Joining", "fontSize": 7, "align": "left"},
			{"id": "starter-sc-barcode", "kind": "barcode", "x": 3, "y": 42, "width": 80, "height": 10, "fieldPath": "barcode"},
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
	# ── Delivery Checklist · A4 (armado de pedido / ENTREGAS) ───────────
	{
		"template_name": "Armado de Pedido — ENTREGAS (A4)",
		"source_doctype": "Delivery Checklist",
		"paper_kind": "A4",
		"is_default": True,
		"gift": True,
		"margin_mm": [12, 12, 12, 12],
		"elements": _ar_entregas_checklist_a4_elements(id_prefix="starter-ent", with_location=False, with_map=False),
	},
	{
		"template_name": "Armado de Pedido — ENTREGAS + Mapa (A4)",
		"source_doctype": "Delivery Checklist",
		"paper_kind": "A4",
		"is_default": False,
		"gift": True,
		"margin_mm": [12, 12, 12, 12],
		"elements": _ar_entregas_checklist_a4_elements(id_prefix="starter-entmap", with_location=True, with_map=True),
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
	"""Idempotently create built-in starter templates (core gifts for every site).

	New entries in `_STARTER_TEMPLATES` are created on every site the next time
	this runs (after_migrate, provision-tenant, or /logistica/prints open).
	Existing templates are never overwritten — cashiers can customize freely.
	"""
	_ensure_source_doctype_select_options()
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
	return {"created": created, "gifted": created}


def gift_core_print_templates():
	"""after_migrate / hypervisor hook: ensure every site receives new core templates."""
	return ensure_starter_print_templates()
