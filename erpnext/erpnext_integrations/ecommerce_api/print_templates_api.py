import json

import frappe
from frappe import _
from frappe.utils import flt

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
	{"fieldname": "eyebrow", "label": "Eyebrow (e.g. CATÁLOGO DE PRODUCTOS)", "fieldtype": "Data"},
	{"fieldname": "export_date", "label": "Export Date Label", "fieldtype": "Data"},
	{"fieldname": "product_count", "label": "Product Count Label", "fieldtype": "Data"},
	{"fieldname": "deadline_label", "label": "Deadline Pill (e.g. Pedidos hasta…)", "fieldtype": "Data"},
	{"fieldname": "logo", "label": "Logo Image URL", "fieldtype": "Attach Image"},
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
	{"fieldname": "name", "label": "Pedido", "fieldtype": "Data"},
	{"fieldname": "customer_name", "label": "Cliente", "fieldtype": "Data"},
	{"fieldname": "customer_address", "label": "Dirección", "fieldtype": "Small Text"},
	{"fieldname": "tax_id", "label": "CUIT", "fieldtype": "Data"},
	{"fieldname": "tax_category", "label": "Cond. IVA", "fieldtype": "Data"},
	{"fieldname": "vendedor", "label": "Vend", "fieldtype": "Data"},
	{"fieldname": "zona", "label": "Zona", "fieldtype": "Data"},
	{"fieldname": "horario", "label": "Horario", "fieldtype": "Data"},
	{"fieldname": "fletero", "label": "Fletero", "fieldtype": "Data"},
	{"fieldname": "cargado", "label": "Cargado", "fieldtype": "Data"},
	{"fieldname": "armado_flag", "label": "Armado", "fieldtype": "Data"},
	{"fieldname": "facturado_flag", "label": "Facturado", "fieldtype": "Data"},
	{"fieldname": "delivery_date", "label": "Fecha de Envio", "fieldtype": "Date"},
	{"fieldname": "transaction_date", "label": "Order Date", "fieldtype": "Date"},
	{"fieldname": "warehouse_name", "label": "Almacen", "fieldtype": "Data"},
	{"fieldname": "total_weight", "label": "Peso total", "fieldtype": "Float"},
	{"fieldname": "company", "label": "Company", "fieldtype": "Data"},
	{"fieldname": "_map", "label": "Warehouse Map", "fieldtype": "JSON"},
]

_DELIVERY_CHECKLIST_CHILD_FIELDS = [
	{"fieldname": "item_code", "label": "Código", "fieldtype": "Data"},
	{"fieldname": "code_display", "label": "Código + barra", "fieldtype": "Data"},
	{"fieldname": "item_name", "label": "Descripción", "fieldtype": "Data"},
	{"fieldname": "qty", "label": "Cantidad", "fieldtype": "Float"},
	{"fieldname": "uom", "label": "Uni", "fieldtype": "Data"},
	{"fieldname": "qty_display", "label": "Cantidad (con UOM)", "fieldtype": "Data"},
	{"fieldname": "weight", "label": "Peso", "fieldtype": "Float"},
	{"fieldname": "actual_weight", "label": "Peso real", "fieldtype": "Float"},
	{"fieldname": "total_weight", "label": "Peso total línea", "fieldtype": "Float"},
	{"fieldname": "weight_uom", "label": "UOM peso", "fieldtype": "Data"},
	{"fieldname": "confirm_uni", "label": "Armado Uni", "fieldtype": "Data"},
	{"fieldname": "confirm_qty", "label": "Armado Cantidad", "fieldtype": "Data"},
	{"fieldname": "warehouse", "label": "Desde", "fieldtype": "Data"},
	{"fieldname": "location", "label": "Ubicación", "fieldtype": "Data"},
	{"fieldname": "barcode", "label": "Codigo de Barras", "fieldtype": "Data"},
	{"fieldname": "is_weight_based", "label": "Por peso", "fieldtype": "Check"},
]

_WEIGHT_UOMS = {
	"kg",
	"kgs",
	"kilogram",
	"kilograms",
	"kilogramo",
	"kilogramos",
	"g",
	"gr",
	"gram",
	"grams",
	"gramo",
	"gramos",
	"lb",
	"lbs",
	"oz",
	"ounce",
	"ounces",
}


def _norm_uom(uom) -> str:
	return str(uom or "").strip().lower()


def _is_weight_uom(uom) -> bool:
	return _norm_uom(uom) in _WEIGHT_UOMS


def _item_weight_meta(item_code):
	"""Return (stock_uom, weight_per_unit, weight_uom) for an Item."""
	if not item_code:
		return "", 0.0, ""
	try:
		row = frappe.db.get_value(
			"Item",
			item_code,
			["stock_uom", "weight_per_unit", "weight_uom"],
			as_dict=True,
		)
	except Exception:
		return "", 0.0, ""
	if not row:
		return "", 0.0, ""
	return (
		row.get("stock_uom") or "",
		float(row.get("weight_per_unit") or 0),
		row.get("weight_uom") or "",
	)


def _order_print_client_knows_weight() -> bool:
	"""Shop UI setting — default False (qty-only orders, reweigh on armado)."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import get_shop_ui_settings

		res = get_shop_ui_settings() or {}
		pos = (res.get("settings") or {}).get("posDisplay") or {}
		return bool(pos.get("orderPrintClientKnowsWeight"))
	except Exception:
		return False


def _is_weight_based_line(row: dict, stock_uom: str = "", weight_uom: str = "") -> bool:
	"""Sold-by-weight when the transaction/stock UOM is a mass unit."""
	for u in (
		row.get("uom"),
		row.get("stock_uom"),
		weight_uom,
		stock_uom,
		row.get("weight_uom"),
	):
		if _is_weight_uom(u):
			return True
	return False


def _enrich_print_line_item(row) -> dict:
	"""Add weight fields; blank amount for weight-based lines when client may not know weight."""
	out = dict(row) if isinstance(row, dict) else {}
	item_code = out.get("item_code") or ""
	stock_uom, wpu, wuom = _item_weight_meta(item_code)
	line_wpu = float(out.get("weight_per_unit") or 0) or wpu
	line_wuom = out.get("weight_uom") or wuom
	qty = float(out.get("qty") or 0)
	weight_based = _is_weight_based_line(out, stock_uom=stock_uom, weight_uom=line_wuom)

	weight_val = line_wpu if line_wpu else (qty if weight_based else None)
	total_w = None
	if line_wpu and qty:
		total_w = line_wpu * qty
	elif weight_based and qty:
		total_w = qty

	out["weight"] = weight_val if weight_val not in (None, 0) else ""
	out["weight_uom"] = line_wuom or (stock_uom if weight_based else "") or ""
	out["total_weight"] = total_w if total_w not in (None, 0) else ""
	out["actual_weight"] = out.get("actual_weight") if out.get("actual_weight") not in (None, "") else ""
	out["is_weight_based"] = 1 if weight_based else 0
	# Default: client does not know weight → leave amount blank for weight-based lines.
	if weight_based and not _order_print_client_knows_weight():
		out["amount"] = ""
	return out


def _sum_line_total_weight(rows) -> float:
	total = 0.0
	any_w = False
	for r in rows or []:
		raw = r.get("total_weight") if isinstance(r, dict) else None
		if raw in (None, ""):
			continue
		try:
			total += float(raw)
			any_w = True
		except (TypeError, ValueError):
			continue
	return total if any_w else 0.0


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


def _normalize_canvas_background(val) -> str:
	"""Empty / transparent / none → '' (see-through). Otherwise keep a color string."""
	raw = str(val or "").strip()
	if not raw or raw.lower() in ("transparent", "none", "null", "undefined"):
		return ""
	return raw[:40]


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
		"canvasBackgroundColor": _normalize_canvas_background(
			getattr(doc, "canvas_background_color", None)
		),
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
	canvas_background_color=None,
	company=None,
	template_id=None,
):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	elements_json = elements_data if isinstance(elements_data, str) else json.dumps(elements_data)
	notes_json = notes_data if isinstance(notes_data, str) else json.dumps(notes_data)
	margin_json = margin_mm if isinstance(margin_mm, str) else json.dumps(margin_mm or [10, 10, 10, 10])
	make_default = bool(frappe.utils.cint(is_default))
	active = resolve_company(company) if _has_company_column() else None
	# Explicit None = leave existing; otherwise normalize (incl. "" → transparent).
	bg_provided = canvas_background_color is not None
	bg_value = _normalize_canvas_background(canvas_background_color) if bg_provided else None

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
		if bg_provided:
			doc.canvas_background_color = bg_value or None
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
	if bg_provided:
		doc.canvas_background_color = bg_value or None
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
	tax_category = ""
	zona = ""
	horario = ""
	customer_address = ""
	if so.customer and frappe.db.exists("Customer", so.customer):
		cust = frappe.db.get_value(
			"Customer",
			so.customer,
			[
				"tax_id",
				"tax_category",
				"territory",
				"primary_address",
				"custom_preferred_hours",
			]
			if frappe.db.has_column("Customer", "custom_preferred_hours")
			else ["tax_id", "tax_category", "territory", "primary_address"],
			as_dict=True,
		) or {}
		tax_id = cust.get("tax_id") or ""
		tax_category = cust.get("tax_category") or ""
		zona = cust.get("territory") or ""
		horario = cust.get("custom_preferred_hours") or ""
		customer_address = (cust.get("primary_address") or "").replace("<br>", ", ").replace("\n", ", ")

	# Facturado / Armado flags from linked docs
	has_si = bool(
		frappe.db.exists(
			"Sales Invoice Item",
			{"sales_order": sales_order_name, "docstatus": ["!=", 2]},
		)
	)
	has_dn = bool(
		frappe.db.exists(
			"Delivery Note Item",
			{"against_sales_order": sales_order_name, "docstatus": ["!=", 2]},
		)
	)

	vendedor = getattr(so, "sales_person", None) or so.owner or ""
	# Delivery trip / driver if linked
	fletero = ""
	try:
		trip = frappe.db.sql(
			"""
			SELECT dt.driver_name, dt.name
			FROM `tabDelivery Trip` dt
			INNER JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
			WHERE ds.customer = %s AND dt.docstatus < 2
			ORDER BY dt.modified DESC LIMIT 1
			""",
			(so.customer,),
		)
		if trip:
			fletero = trip[0][0] or trip[0][1] or ""
	except Exception:
		fletero = ""

	cargado = ""
	if so.modified:
		try:
			cargado = frappe.utils.format_datetime(so.modified, "dd/MM/yy-HH:mm:ss")
		except Exception:
			cargado = str(so.modified)

	rows = []
	highlight_ids = set()
	for it in so.items or []:
		loc_info = sku_to_loc.get(it.item_code) or {}
		loc = loc_info.get("location") or ""
		sid = loc_info.get("section_id")
		if sid:
			highlight_ids.add(sid)
		barcode = _item_barcode(it.item_code)
		code_display = it.item_code or ""
		if barcode:
			code_display = f"{code_display} - {barcode}"
		uom = it.uom or ""
		qty = it.qty
		qty_display = f"{flt(qty):.2f} {uom}".strip() if qty is not None else ""
		row = _enrich_print_line_item(
			{
				"item_code": it.item_code,
				"item_name": it.item_name,
				"qty": qty,
				"uom": uom,
				"qty_display": qty_display,
				"code_display": code_display,
				"rate": it.rate,
				"amount": it.amount,
				"weight_per_unit": getattr(it, "weight_per_unit", None),
				"weight_uom": getattr(it, "weight_uom", None),
				"warehouse": it.warehouse or wh,
				"location": loc,
				"barcode": barcode,
				"confirm_uni": "",
				"confirm_qty": "",
			}
		)
		rows.append(row)

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
		"customer_address": customer_address,
		"tax_id": tax_id,
		"tax_category": tax_category,
		"vendedor": vendedor,
		"zona": zona,
		"horario": horario,
		"fletero": fletero,
		"cargado": cargado,
		"armado_flag": "SI" if has_dn else "NO",
		"facturado_flag": "SI" if has_si else "NO",
		"delivery_date": so.delivery_date,
		"transaction_date": so.transaction_date,
		"warehouse_name": wh,
		"total_weight": _sum_line_total_weight(rows) or getattr(so, "total_weight", None) or "",
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
		rows = [_enrich_print_line_item(r) for r in (data.get(child_table_fieldname) or [])]
		data[child_table_fieldname] = rows
		if not data.get("total_weight"):
			tw = _sum_line_total_weight(rows)
			if tw:
				data["total_weight"] = tw

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
	# One catalog print-grid card cell (retail ABIN-style starter).
	"Catalog Card": (58, 78),
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
				{"fieldPath": "item_code", "label": "Código", "width": 20},
				{"fieldPath": "item_name", "label": "Descripción", "width": 40},
				{"fieldPath": "uom", "label": "Unidades", "width": 16},
				{"fieldPath": "qty", "label": "Cantidad", "width": 16},
				{"fieldPath": "weight", "label": "Peso", "width": 14},
				{"fieldPath": "price_list_rate", "label": "Precio Kg.", "width": 18},
				{"fieldPath": "rate", "label": "Precio U.", "width": 18},
				{"fieldPath": "discount_percentage", "label": "Descuento", "width": 14},
				{"fieldPath": "amount", "label": "Importe", "width": 16},
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


def _armado_meta_header_elements(p: str, *, font: int = 10) -> list:
	"""Compressed header (screenshot-style): meta grid + cliente + pedido — no ENTREGAS title."""
	fs = font
	fs_sm = max(8, font - 1)
	rows = [
		("Vend:", "vendedor", "Zona:", "zona"),
		("Horario:", "horario", "Fletero:", "fletero"),
		("Cargado:", "cargado", "Armado:", "armado_flag"),
		("Facturado:", "facturado_flag", "CUIT:", "tax_id"),
	]
	els = []
	y0 = 10
	# Left: cliente box
	els.extend(
		[
			{
				"id": f"{p}-cli-box",
				"kind": "shape",
				"x": 10,
				"y": y0,
				"width": 105,
				"height": 28,
				"shapeType": "rect",
				"color": "#94a3b8",
				"filled": False,
			},
			{
				"id": f"{p}-cli-label",
				"kind": "text",
				"x": 12,
				"y": y0 + 1,
				"width": 20,
				"height": 5,
				"staticText": "CLIENTE:",
				"fontSize": fs_sm,
				"bold": True,
				"align": "left",
			},
			{
				"id": f"{p}-cli",
				"kind": "field",
				"x": 32,
				"y": y0 + 1,
				"width": 80,
				"height": 5,
				"fieldPath": "customer_name",
				"label": "Cliente",
				"fontSize": fs,
				"bold": True,
				"align": "left",
			},
			{
				"id": f"{p}-addr",
				"kind": "field",
				"x": 12,
				"y": y0 + 8,
				"width": 100,
				"height": 8,
				"fieldPath": "customer_address",
				"label": "Dirección",
				"fontSize": fs_sm,
				"align": "left",
			},
			{
				"id": f"{p}-cond-label",
				"kind": "text",
				"x": 12,
				"y": y0 + 18,
				"width": 22,
				"height": 5,
				"staticText": "Cond. IVA:",
				"fontSize": fs_sm,
				"align": "left",
			},
			{
				"id": f"{p}-cond",
				"kind": "field",
				"x": 34,
				"y": y0 + 18,
				"width": 78,
				"height": 5,
				"fieldPath": "tax_category",
				"label": "Cond. IVA",
				"fontSize": fs_sm,
				"align": "left",
			},
		]
	)
	# Right: pedido badge + meta
	els.extend(
		[
			{
				"id": f"{p}-ped-box",
				"kind": "shape",
				"x": 120,
				"y": y0,
				"width": 80,
				"height": 10,
				"shapeType": "rect",
				"color": "#cbd5e1",
				"filled": True,
				"bgColor": "#cbd5e1",
			},
			{
				"id": f"{p}-ped-label",
				"kind": "text",
				"x": 122,
				"y": y0 + 2,
				"width": 22,
				"height": 6,
				"staticText": "Pedido:",
				"fontSize": fs,
				"bold": True,
				"align": "left",
			},
			{
				"id": f"{p}-ped",
				"kind": "field",
				"x": 144,
				"y": y0 + 2,
				"width": 54,
				"height": 6,
				"fieldPath": "name",
				"label": "Pedido",
				"fontSize": fs,
				"bold": True,
				"align": "left",
			},
		]
	)
	meta_y = y0 + 12
	for i, (l1, f1, l2, f2) in enumerate(rows):
		yy = meta_y + i * 6
		els.extend(
			[
				{
					"id": f"{p}-m{i}a-l",
					"kind": "text",
					"x": 120,
					"y": yy,
					"width": 18,
					"height": 5,
					"staticText": l1,
					"fontSize": fs_sm,
					"align": "left",
				},
				{
					"id": f"{p}-m{i}a",
					"kind": "field",
					"x": 138,
					"y": yy,
					"width": 22,
					"height": 5,
					"fieldPath": f1,
					"label": l1,
					"fontSize": fs_sm,
					"align": "left",
				},
				{
					"id": f"{p}-m{i}b-l",
					"kind": "text",
					"x": 160,
					"y": yy,
					"width": 16,
					"height": 5,
					"staticText": l2,
					"fontSize": fs_sm,
					"align": "left",
				},
				{
					"id": f"{p}-m{i}b",
					"kind": "field",
					"x": 176,
					"y": yy,
					"width": 24,
					"height": 5,
					"fieldPath": f2,
					"label": l2,
					"fontSize": fs_sm,
					"align": "left",
				},
			]
		)
	return els


def _ar_entregas_checklist_a4_elements(
	*,
	id_prefix: str,
	with_location: bool = False,
	with_map: bool = False,
	mode: str = "almacen",
):
	"""A4 armado layouts.

	mode=peso_indefinido — Código (sku-barcode), Descripción, Cantidad, Peso, Peso real + ARMADO side table.
	mode=almacen — warehouse/location focused + ARMADO side confirmation columns.
	Compressed header; larger type; no ENTREGAS title block.
	"""
	p = id_prefix
	font = 12
	header_h = 44
	table_y = header_h + 4

	if mode == "peso_indefinido":
		main_cols = [
			{"fieldPath": "code_display", "label": "Codigo", "width": 36},
			{"fieldPath": "item_name", "label": "Detalle", "width": 58},
			{"fieldPath": "qty_display", "label": "Cantidad", "width": 28},
			{"fieldPath": "weight", "label": "Peso", "width": 16},
			{"fieldPath": "actual_weight", "label": "Peso Real", "width": 18},
		]
		main_w = 156
	else:
		main_cols = [
			{"fieldPath": "code_display", "label": "Codigo", "width": 30},
			{"fieldPath": "item_name", "label": "Detalle", "width": 48},
			{"fieldPath": "qty", "label": "Cant.", "width": 14},
			{"fieldPath": "warehouse", "label": "Desde", "width": 22},
		]
		if with_location:
			main_cols.append({"fieldPath": "location", "label": "Ubic.", "width": 18})
		main_cols.append({"fieldPath": "barcode", "label": "Barras", "width": 20})
		main_w = 152 if with_location else 154

	confirm_cols = [
		{"fieldPath": "confirm_uni", "label": "Uni", "width": 14},
		{"fieldPath": "confirm_qty", "label": "Cantidad", "width": 20},
	]
	confirm_w = 34
	items_h = 95 if with_map else 175

	elements = _armado_meta_header_elements(p, font=font)
	# PEDIDO section label
	elements.append(
		{
			"id": f"{p}-sec-ped",
			"kind": "text",
			"x": 10,
			"y": table_y - 5,
			"width": 40,
			"height": 5,
			"staticText": "PEDIDO",
			"fontSize": 9,
			"bold": True,
			"align": "left",
		}
	)
	elements.append(
		{
			"id": f"{p}-sec-arm",
			"kind": "text",
			"x": 10 + main_w + 2,
			"y": table_y - 5,
			"width": 34,
			"height": 5,
			"staticText": "ARMADO",
			"fontSize": 9,
			"bold": True,
			"align": "center",
		}
	)
	elements.append(
		{
			"id": f"{p}-items-v3",
			"kind": "line-items",
			"x": 10,
			"y": table_y,
			"width": main_w,
			"height": items_h,
			"childTableFieldname": "items",
			"headerBg": "#64748b",
			"headerColor": "#ffffff",
			"columns": main_cols,
		}
	)
	elements.append(
		{
			"id": f"{p}-confirm-v3",
			"kind": "line-items",
			"x": 10 + main_w + 2,
			"y": table_y,
			"width": confirm_w,
			"height": items_h,
			"childTableFieldname": "items",
			"headerBg": "#64748b",
			"headerColor": "#ffffff",
			"columns": confirm_cols,
		}
	)

	footer_y = table_y + items_h + 6
	if with_map:
		elements.append(
			{
				"id": f"{p}-map",
				"kind": "warehouse-map",
				"x": 10,
				"y": footer_y,
				"width": 190,
				"height": 45,
				"fieldPath": "_map",
			}
		)
		footer_y += 50

	elements.extend(
		[
			{
				"id": f"{p}-tw-label",
				"kind": "text",
				"x": 10,
				"y": footer_y,
				"width": 28,
				"height": 6,
				"staticText": "Peso tot:",
				"fontSize": 10,
				"align": "left",
			},
			{
				"id": f"{p}-tw",
				"kind": "field",
				"x": 38,
				"y": footer_y,
				"width": 36,
				"height": 6,
				"fieldPath": "total_weight",
				"label": "Peso total",
				"fontSize": 11,
				"align": "left",
			},
			{
				"id": f"{p}-firma-label",
				"kind": "text",
				"x": 110,
				"y": footer_y,
				"width": 90,
				"height": 6,
				"staticText": "Recibi Conforme (Firma y Aclaración):",
				"fontSize": 9,
				"align": "left",
			},
			{
				"id": f"{p}-firma-line",
				"kind": "shape",
				"x": 110,
				"y": footer_y + 12,
				"width": 90,
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
		"resync": True,
		"resync_if_missing_id": "starter-inv-items-v2",
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
				"id": "starter-inv-items-v2",
				"kind": "line-items",
				"x": 15,
				"y": 60,
				"width": 180,
				"height": 90,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_code", "label": "Item", "width": 26},
					{"fieldPath": "item_name", "label": "Description", "width": 52},
					{"fieldPath": "qty", "label": "Qty", "width": 16},
					{"fieldPath": "weight", "label": "Weight", "width": 18},
					{"fieldPath": "rate", "label": "Rate", "width": 22},
					{"fieldPath": "amount", "label": "Amount", "width": 26},
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
					{"fieldPath": "item_name", "label": "Item", "width": 28},
					{"fieldPath": "qty", "label": "Cant", "width": 10},
					{"fieldPath": "weight", "label": "Peso", "width": 12},
					{"fieldPath": "amount", "label": "Monto", "width": 22},
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
	# ── Catalog Header (retail / ABIN-style banner) ─────────────────────
	{
		"template_name": "Default Catalog Header",
		"source_doctype": "Catalog Header",
		"paper_kind": "A4",
		"is_default": True,
		"resync": True,
		# Full A4 width (210mm) so export scales edge-to-edge without empty side gutters.
		"resync_if_missing_id": "starter-hdr-a4-fullwidth",
		"canvas_width_mm": 210,
		"canvas_height_mm": 46,
		"canvas_background_color": "",
		"margin_mm": [0, 0, 0, 0],
		"elements": [
			{
				"id": "starter-hdr-a4-fullwidth",
				"kind": "shape",
				"x": 0,
				"y": 0,
				"width": 0.1,
				"height": 0.1,
				"shapeType": "rect",
				"color": "transparent",
				"filled": False,
			},
			{
				"id": "starter-hdr-logo-circle",
				"kind": "shape",
				"x": 6,
				"y": 4,
				"width": 24,
				"height": 24,
				"shapeType": "rect",
				"color": "#9f1d1d",
				"filled": True,
				"bgColor": "#9f1d1d",
				"borderRadius": 999,
			},
			{
				"id": "starter-hdr-logo",
				"kind": "image",
				"x": 7,
				"y": 6,
				"width": 22,
				"height": 20,
				"fieldPath": "logo",
				"staticSrc": "/brand/abin-logo.png",
			},
			{"id": "starter-hdr-eyebrow", "kind": "field", "x": 36, "y": 4, "width": 100, "height": 5, "fieldPath": "eyebrow", "label": "Eyebrow", "fontSize": 7, "bold": True, "align": "left", "textColor": "#6b7280"},
			{"id": "starter-hdr-heading-lg", "kind": "field", "x": 36, "y": 10, "width": 108, "height": 18, "fieldPath": "heading", "label": "Heading", "fontSize": 36, "bold": True, "align": "left", "textColor": "#111827"},
			{"id": "starter-hdr-deadline-pill", "kind": "shape", "x": 150, "y": 4, "width": 54, "height": 7, "shapeType": "rect", "color": "#9f1d1d", "filled": True, "bgColor": "#9f1d1d", "borderRadius": 8},
			{"id": "starter-hdr-deadline", "kind": "field", "x": 150, "y": 4.5, "width": 54, "height": 6, "fieldPath": "deadline_label", "label": "Deadline", "fontSize": 7, "bold": True, "align": "center", "textColor": "#ffffff"},
			{"id": "starter-hdr-date", "kind": "field", "x": 148, "y": 13, "width": 56, "height": 5, "fieldPath": "export_date", "label": "Date", "fontSize": 7, "bold": True, "align": "right", "textColor": "#6b7280"},
			{"id": "starter-hdr-count", "kind": "field", "x": 148, "y": 19, "width": 56, "height": 5, "fieldPath": "product_count", "label": "Count", "fontSize": 7, "bold": True, "align": "right", "textColor": "#6b7280"},
			{"id": "starter-hdr-rule", "kind": "shape", "x": 0, "y": 38, "width": 210, "height": 2.2, "shapeType": "rect", "color": "#9f1d1d", "filled": True, "bgColor": "#9f1d1d"},
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
	# ── Catalog Card (retail / ABIN-style product card) ─────────────────
	{
		"template_name": "Default Catalog Card",
		"source_doctype": "Item",
		"paper_kind": "Catalog Card",
		"is_default": True,
		"resync": True,
		# Transparent canvas — no white frame slab over the catalog page background.
		"resync_if_missing_id": "starter-card-transparent-canvas",
		"canvas_width_mm": 58,
		"canvas_height_mm": 78,
		"canvas_background_color": "",
		"margin_mm": [0, 0, 0, 0],
		"elements": [
			{
				"id": "starter-card-transparent-canvas",
				"kind": "shape",
				"x": 0,
				"y": 0,
				"width": 0.1,
				"height": 0.1,
				"shapeType": "rect",
				"color": "transparent",
				"filled": False,
			},
			# Brand top accent only (no white card frame)
			{"id": "starter-card-accent", "kind": "shape", "x": 0, "y": 0, "width": 58, "height": 2.4, "shapeType": "rect", "color": "#9f1d1d", "filled": True, "bgColor": "#9f1d1d"},
			# Product image
			{"id": "starter-card-image", "kind": "image", "x": 4, "y": 5, "width": 50, "height": 36, "fieldPath": "image"},
			# Promo badge (hidden when no promo_label)
			{"id": "starter-card-promo-pill", "kind": "shape", "x": 34, "y": 5, "width": 20, "height": 8, "shapeType": "rect", "color": "#facc15", "filled": True, "bgColor": "#facc15", "borderRadius": 2, "hideWhenEmpty": "promo_label"},
			{"id": "starter-card-promo", "kind": "field", "x": 34, "y": 5.5, "width": 20, "height": 7, "fieldPath": "promo_label", "label": "Promo", "fontSize": 6, "bold": True, "align": "center", "textColor": "#9f1d1d"},
			# SKU pill
			{"id": "starter-card-sku-pill", "kind": "shape", "x": 16, "y": 43, "width": 26, "height": 5.5, "shapeType": "rect", "color": "#c4a484", "filled": True, "bgColor": "#c4a484", "borderRadius": 8},
			{"id": "starter-card-sku", "kind": "field", "x": 16, "y": 43.5, "width": 26, "height": 4.5, "fieldPath": "barcode", "label": "SKU", "fontSize": 7, "bold": True, "align": "center", "textColor": "#ffffff"},
			# Title + brand
			{"id": "starter-card-title", "kind": "field", "x": 3, "y": 50, "width": 52, "height": 9, "fieldPath": "normalized_title", "label": "Normalized Title", "fontSize": 8, "bold": True, "align": "center", "textColor": "#111827"},
			{"id": "starter-card-brand", "kind": "field", "x": 3, "y": 59, "width": 52, "height": 4, "fieldPath": "brand", "label": "Brand", "fontSize": 6, "align": "center", "textColor": "#6b7280"},
			# Price pill
			{"id": "starter-card-price-pill", "kind": "shape", "x": 12, "y": 66, "width": 34, "height": 8, "shapeType": "rect", "color": "#9f1d1d", "filled": True, "bgColor": "#9f1d1d", "borderRadius": 8},
			{"id": "starter-card-price", "kind": "field", "x": 12, "y": 67, "width": 34, "height": 6, "fieldPath": "display_price", "label": "Catalog Price", "fontSize": 11, "bold": True, "align": "center", "textColor": "#ffffff"},
		],
	},
	# ── Delivery Checklist · A4 (armado) ────────────────────────────────
	{
		"template_name": "Armado con Peso Indefinido (A4)",
		"source_doctype": "Delivery Checklist",
		"paper_kind": "A4",
		"is_default": True,
		"gift": True,
		"resync": True,
		"resync_if_missing_id": "starter-peso-items-v3",
		"margin_mm": [8, 8, 8, 8],
		"elements": _ar_entregas_checklist_a4_elements(
			id_prefix="starter-peso",
			with_location=False,
			with_map=False,
			mode="peso_indefinido",
		),
	},
	{
		"template_name": "Armado Almacen (A4)",
		"source_doctype": "Delivery Checklist",
		"paper_kind": "A4",
		"is_default": False,
		"gift": True,
		"resync": True,
		"resync_if_missing_id": "starter-alm-items-v3",
		"margin_mm": [8, 8, 8, 8],
		"elements": _ar_entregas_checklist_a4_elements(
			id_prefix="starter-alm",
			with_location=True,
			with_map=False,
			mode="almacen",
		),
	},
	{
		"template_name": "Armado Almacen + Mapa (A4)",
		"source_doctype": "Delivery Checklist",
		"paper_kind": "A4",
		"is_default": False,
		"gift": True,
		"resync": True,
		"resync_if_missing_id": "starter-almmap-items-v3",
		"margin_mm": [8, 8, 8, 8],
		"elements": _ar_entregas_checklist_a4_elements(
			id_prefix="starter-almmap",
			with_location=True,
			with_map=True,
			mode="almacen",
		),
	},
	# ── TMS delivery tickets (Delivery Note · Thermal 80mm) ─────────────
	{
		"template_name": "Confirmación de Entrega (80mm)",
		"source_doctype": "Delivery Note",
		"paper_kind": "Thermal 80mm",
		"is_default": True,
		"resync": True,
		"resync_if_missing_id": "starter-dnconf-items-v2",
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
				"id": "starter-dnconf-items-v2",
				"kind": "line-items",
				"x": 4,
				"y": 53,
				"width": 72,
				"height": 40,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_name", "label": "Item", "width": 28},
					{"fieldPath": "qty", "label": "Cant", "width": 12},
					{"fieldPath": "weight", "label": "Peso", "width": 12},
					{"fieldPath": "total_weight", "label": "P.tot", "width": 12},
					{"fieldPath": "amount", "label": "Monto", "width": 14},
				],
			},
			{"id": "starter-dnconf-tw-label", "kind": "text", "x": 4, "y": 96, "width": 30, "height": 5, "staticText": "Peso total", "fontSize": 7, "align": "left"},
			{"id": "starter-dnconf-tw", "kind": "field", "x": 34, "y": 96, "width": 42, "height": 6, "fieldPath": "total_weight", "label": "Peso total", "fontSize": 9, "align": "right"},
			{"id": "starter-dnconf-total-label", "kind": "text", "x": 4, "y": 104, "width": 30, "height": 6, "staticText": "Total", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnconf-total", "kind": "field", "x": 34, "y": 104, "width": 42, "height": 8, "fieldPath": "grand_total", "label": "Total", "fontSize": 11, "bold": True, "align": "right"},
			{"id": "starter-dnconf-tracking-label", "kind": "text", "x": 4, "y": 114, "width": 30, "height": 5, "staticText": "Seguimiento", "fontSize": 7, "align": "left"},
			{"id": "starter-dnconf-tracking", "kind": "field", "x": 4, "y": 119, "width": 72, "height": 6, "fieldPath": "custom_tracking_code", "label": "Tracking Code", "fontSize": 9, "align": "left"},
			{"id": "starter-dnconf-qr", "kind": "qrcode", "x": 4, "y": 127, "width": 30, "height": 30, "fieldPath": "custom_tracking_code"},
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
			{"id": "starter-dnpay-tw-label", "kind": "text", "x": 4, "y": 41, "width": 30, "height": 5, "staticText": "Peso total", "fontSize": 7, "align": "left"},
			{"id": "starter-dnpay-tw", "kind": "field", "x": 34, "y": 41, "width": 42, "height": 6, "fieldPath": "total_weight", "label": "Peso total", "fontSize": 9, "align": "right"},
			{"id": "starter-dnpay-total-label", "kind": "text", "x": 4, "y": 49, "width": 30, "height": 6, "staticText": "Total", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnpay-total", "kind": "field", "x": 34, "y": 49, "width": 42, "height": 8, "fieldPath": "grand_total", "label": "Total", "fontSize": 11, "bold": True, "align": "right"},
		],
	},
	{
		"template_name": "Recibo de Devolución (80mm)",
		"source_doctype": "Delivery Note",
		"paper_kind": "Thermal 80mm",
		"resync": True,
		"resync_if_missing_id": "starter-dnret-items-v2",
		"margin_mm": [4, 4, 4, 4],
		"elements": [
			{"id": "starter-dnret-title", "kind": "text", "x": 4, "y": 4, "width": 72, "height": 8, "staticText": "Recibo de Devolución", "fontSize": 12, "bold": True, "align": "center"},
			{"id": "starter-dnret-ref", "kind": "field", "x": 4, "y": 15, "width": 72, "height": 6, "fieldPath": "name", "label": "Delivery Note", "fontSize": 9, "bold": True, "align": "left"},
			{"id": "starter-dnret-customer", "kind": "field", "x": 4, "y": 23, "width": 72, "height": 6, "fieldPath": "customer_name", "label": "Customer", "fontSize": 9, "align": "left"},
			{"id": "starter-dnret-date", "kind": "field", "x": 4, "y": 31, "width": 72, "height": 6, "fieldPath": "posting_date", "label": "Date", "fontSize": 9, "align": "left"},
			{
				"id": "starter-dnret-items-v2",
				"kind": "line-items",
				"x": 4,
				"y": 41,
				"width": 72,
				"height": 40,
				"childTableFieldname": "items",
				"columns": [
					{"fieldPath": "item_name", "label": "Item", "width": 36},
					{"fieldPath": "qty", "label": "Cant", "width": 14},
					{"fieldPath": "weight", "label": "Peso", "width": 14},
					{"fieldPath": "total_weight", "label": "P.tot", "width": 14},
				],
			},
		],
	},
]


def _localize_print_text(text: str, locale: str) -> str:
	"""Best-effort UI string localization for starter template copies."""
	if not text or locale not in ("es", "zh"):
		return text
	# Shared keys → (es, zh)
	table = {
		"INVOICE": ("FACTURA", "发票"),
		"Invoice #": ("Factura #", "发票号"),
		"Date": ("Fecha", "日期"),
		"Bill To": ("Facturar a", "收票方"),
		"Customer": ("Cliente", "客户"),
		"Item": ("Ítem", "商品"),
		"Description": ("Descripción", "描述"),
		"Qty": ("Cant.", "数量"),
		"Weight": ("Peso", "重量"),
		"Rate": ("Precio", "单价"),
		"Amount": ("Importe", "金额"),
		"Total": ("Total", "合计"),
		"TOTAL": ("TOTAL", "合计"),
		"Subtotal": ("Subtotal", "小计"),
		"Código": ("Código", "编码"),
		"Descripción": ("Descripción", "描述"),
		"Cantidad": ("Cantidad", "数量"),
		"Peso": ("Peso", "重量"),
		"Peso real": ("Peso real", "实重"),
		"Peso total": ("Peso total", "总重量"),
		"Peso total:": ("Peso total:", "总重量："),
		"P.tot": ("P.tot", "总重"),
		"Desde": ("Desde", "库位"),
		"Ubicación": ("Ubicación", "位置"),
		"Codigo de Barras": ("Codigo de Barras", "条码"),
		"Cliente": ("Cliente", "客户"),
		"Cliente :": ("Cliente :", "客户："),
		"Almacen": ("Almacen", "仓库"),
		"ENTREGAS": ("ENTREGAS", "发货单"),
		"PEDIDO": ("PEDIDO", "订单"),
		"ARMADO": ("ARMADO", "配货"),
		"CLIENTE:": ("CLIENTE:", "客户："),
		"Pedido:": ("Pedido:", "订单："),
		"Codigo": ("Codigo", "编码"),
		"Detalle": ("Detalle", "明细"),
		"Peso Real": ("Peso Real", "实重"),
		"Peso tot:": ("Peso tot:", "总重："),
		"Ubic.": ("Ubic.", "库位"),
		"Barras": ("Barras", "条码"),
		"Cant.": ("Cant.", "数量"),
		"Uni": ("Uni", "单位"),
		"Vend:": ("Vend:", "销售："),
		"Zona:": ("Zona:", "区域："),
		"Horario:": ("Horario:", "时段："),
		"Fletero:": ("Fletero:", "司机："),
		"Cargado:": ("Cargado:", "装载："),
		"Armado:": ("Armado:", "配货："),
		"Facturado:": ("Facturado:", "已开票："),
		"CUIT:": ("CUIT:", "税号："),
		"Cond. IVA:": ("Cond. IVA:", "IVA条件："),
		"Cond. IVA": ("Cond. IVA", "IVA条件"),
		"Nº": ("Nº", "编号"),
		"Numero de identificador:": ("Numero de identificador:", "税号："),
		"Fecha de Envio:": ("Fecha de Envio:", "发货日期："),
		"Fecha de Envio": ("Fecha de Envio", "发货日期"),
		"Recibi Conforme (Firma y Aclaración):": (
			"Recibi Conforme (Firma y Aclaración):",
			"收货确认（签名）：",
		),
		"Confirmación de Entrega": ("Confirmación de Entrega", "配送确认"),
		"Comprobante": ("Comprobante", "单据"),
		"Fecha": ("Fecha", "日期"),
		"Cant": ("Cant", "数量"),
		"Monto": ("Monto", "金额"),
		"Seguimiento": ("Seguimiento", "追踪"),
		"Recibo de Pago": ("Recibo de Pago", "付款收据"),
		"Recibo de Devolución": ("Recibo de Devolución", "退货收据"),
		"RECIBO DE VENTA": ("RECIBO DE VENTA", "销售小票"),
		"RECIBO": ("RECIBO", "小票"),
		"Price": ("Precio", "价格"),
		"Product": ("Producto", "产品"),
		"Code": ("Código", "编码"),
		"UOM": ("UOM", "单位"),
		"Standard Rate": ("Precio estándar", "标准价"),
		"ITEM SPECIFICATION": ("FICHA DE PRODUCTO", "商品规格"),
		"Name": ("Nombre", "名称"),
		"Unidades": ("Unidades", "单位"),
		"Precio Kg.": ("Precio Kg.", "公斤价"),
		"Precio U.": ("Precio U.", "单价"),
		"Descuento": ("Descuento", "折扣"),
		"Importe": ("Importe", "金额"),
		"Responsable Inscripto :": ("Responsable Inscripto :", "纳税人："),
		"Condición de Venta :": ("Condición de Venta :", "付款条件："),
		"Condición": ("Condición", "条件"),
	}
	pair = table.get(text)
	if not pair:
		return text
	return pair[0] if locale == "es" else pair[1]


def _localize_elements(elements, locale: str):
	import copy

	cloned = copy.deepcopy(elements)
	for el in cloned:
		if not isinstance(el, dict):
			continue
		if el.get("staticText"):
			el["staticText"] = _localize_print_text(el["staticText"], locale)
		if el.get("label"):
			el["label"] = _localize_print_text(el["label"], locale)
		cols = el.get("columns")
		if isinstance(cols, list):
			for c in cols:
				if isinstance(c, dict) and c.get("label"):
					c["label"] = _localize_print_text(c["label"], locale)
	return cloned


def _expand_locale_starter_templates(base_starters):
	"""Duplicate every starter as ES - … and CH - … localized copies."""
	import copy

	out = list(base_starters)
	for starter in base_starters:
		name = starter.get("template_name") or ""
		if name.startswith("ES - ") or name.startswith("CH - "):
			continue
		for locale, prefix in (("es", "ES"), ("zh", "CH")):
			clone = copy.deepcopy(starter)
			clone["template_name"] = f"{prefix} - {name}"
			clone["is_default"] = False
			clone.pop("resync", None)
			clone.pop("resync_if_missing_id", None)
			clone["elements"] = _localize_elements(clone.get("elements") or [], locale)
			out.append(clone)
	return out


_STARTER_TEMPLATES = _expand_locale_starter_templates(_STARTER_TEMPLATES)


@frappe.whitelist(allow_guest=True)
def ensure_starter_print_templates():
	"""Idempotently create built-in starter templates (core gifts for every site).

	New entries in `_STARTER_TEMPLATES` are created on every site the next time
	this runs (after_migrate, provision-tenant, or /logistica/prints open).
	Existing templates are never overwritten — unless the starter sets
	`resync: True` (catalog retail header/card), in which case canvas size +
	elements are refreshed so seed designs stay current.
	"""
	_ensure_source_doctype_select_options()
	created = []
	resynced = []
	for starter in _STARTER_TEMPLATES:
		exists = frappe.db.exists(
			"ECommerce Print Template",
			{
				"template_name": starter["template_name"],
				"source_doctype": starter["source_doctype"],
				"paper_kind": starter["paper_kind"],
			},
		)
		width_mm = starter.get("canvas_width_mm") or _PAPER_SIZE_MM.get(starter["paper_kind"], (210, 297))[0]
		height_mm = starter.get("canvas_height_mm") or _PAPER_SIZE_MM.get(starter["paper_kind"], (210, 297))[1]

		if exists:
			if starter.get("resync"):
				doc = frappe.get_doc("ECommerce Print Template", exists)
				try:
					current_els = json.loads(doc.elements_data or "[]")
				except Exception:
					current_els = []
				current_ids = {
					e.get("id") for e in current_els if isinstance(e, dict) and e.get("id")
				}
				marker = starter.get("resync_if_missing_id")
				# Skip if already on the target design (or customized past the old seed).
				if marker and marker in current_ids:
					continue
				frappe.flags.ignore_permissions = True
				doc.elements_data = json.dumps(starter["elements"])
				doc.margin_mm = json.dumps(starter["margin_mm"])
				doc.canvas_width_mm = width_mm
				doc.canvas_height_mm = height_mm
				if "canvas_background_color" in starter:
					doc.canvas_background_color = _normalize_canvas_background(
						starter.get("canvas_background_color")
					) or None
				doc.save(ignore_permissions=True)
				frappe.flags.ignore_permissions = False
				resynced.append(doc.name)
			continue

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
		if "canvas_background_color" in starter:
			doc.canvas_background_color = _normalize_canvas_background(
				starter.get("canvas_background_color")
			) or None
		doc.insert(ignore_permissions=True)
		if make_default:
			_apply_default(doc.name, doc.source_doctype, doc.paper_kind)
		created.append(doc.name)

	if created or resynced:
		frappe.db.commit()

	# Promote renamed armado default when legacy ENTREGAS is still the only default.
	_promote_armado_default_if_legacy()
	return {"created": created, "gifted": created, "resynced": resynced}


def _promote_armado_default_if_legacy():
	"""If Delivery Checklist A4 default is still the old ENTREGAS seed, switch to Peso Indefinido."""
	cur = frappe.db.get_value(
		"ECommerce Print Template",
		{"source_doctype": "Delivery Checklist", "paper_kind": "A4", "is_default": 1},
		["name", "template_name"],
		as_dict=True,
	)
	if not cur:
		return
	tname = cur.template_name or ""
	if "ENTREGAS" not in tname:
		return
	new = frappe.db.get_value(
		"ECommerce Print Template",
		{
			"template_name": "Armado con Peso Indefinido (A4)",
			"source_doctype": "Delivery Checklist",
			"paper_kind": "A4",
		},
		"name",
	)
	if not new or new == cur.name:
		return
	frappe.db.set_value("ECommerce Print Template", cur.name, "is_default", 0)
	frappe.db.set_value("ECommerce Print Template", new, "is_default", 1)
	frappe.db.commit()


def gift_core_print_templates():
	"""after_migrate / hypervisor hook: ensure every site receives new core templates."""
	return ensure_starter_print_templates()
