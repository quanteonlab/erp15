import json
import uuid

import frappe
from frappe import _
from frappe.utils import cstr

from erpnext.erpnext_integrations.ecommerce_api.floor_map_containment import (
	build_containment_index,
	format_location_path,
	load_sections_json,
	parse_provisional_path,
	provisional_path_string,
	section_kind,
	section_skus,
)


def _has_company_column() -> bool:
	try:
		return bool(frappe.db.has_column("ECommerce Floor Map", "company"))
	except Exception:
		return False


def _ensure_physical_section_field() -> bool:
	"""Idempotent Item.custom_physical_section (provisional Zone/Fila path)."""
	if frappe.db.has_column("Item", "custom_physical_section"):
		return True
	try:
		from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

		create_custom_fields(
			{
				"Item": [
					{
						"fieldname": "custom_physical_section",
						"fieldtype": "Data",
						"label": "Physical Section",
						"insert_after": "item_name",
						"description": "Provisional warehouse path Z21/F23 until assigned to a rack.",
						"reqd": 0,
					}
				]
			},
			ignore_validate=True,
			update=True,
		)
		frappe.clear_cache(doctype="Item")
		# Force meta reload so has_column sees the new column in this request.
		frappe.get_meta("Item", cached=False)
	except Exception:
		frappe.log_error(title="ensure custom_physical_section")
		return bool(frappe.db.has_column("Item", "custom_physical_section"))
	return bool(frappe.db.has_column("Item", "custom_physical_section"))


def _blankish(v) -> bool:
	if v is None:
		return True
	s = cstr(v).strip().lower()
	return s in ("", "null", "undefined", "none")


def _products_payload(section: dict) -> dict:
	"""Normalize section.products to v2 {_meta, rows, skus}."""
	kind = section_kind(section)
	products = section.get("products")
	rows = []
	hidden = []
	if isinstance(products, dict):
		meta = products.get("_meta") if isinstance(products.get("_meta"), dict) else {}
		kind = meta.get("kind") if meta.get("kind") in ("rack", "fila", "zone") else kind
		if isinstance(meta.get("hidden_row_ids"), list):
			hidden = [cstr(x) for x in meta["hidden_row_ids"] if cstr(x).strip()]
		for row in products.get("rows") or []:
			if not isinstance(row, dict):
				continue
			rows.append(
				{
					"id": cstr(row.get("id") or "").strip() or f"row-{uuid.uuid4().hex[:8]}",
					"sku": cstr(row.get("sku") or "").strip(),
					"name": cstr(row.get("name") or "").strip(),
					"barcode": cstr(row.get("barcode") or "").strip(),
					"info1": cstr(row.get("info1") or "").strip(),
					"info2": cstr(row.get("info2") or "").strip(),
					"info3": cstr(row.get("info3") or "").strip(),
				}
			)
		if not rows:
			for sku in products.get("skus") or []:
				s = cstr(sku).strip()
				if s:
					rows.append(
						{
							"id": f"row-{uuid.uuid4().hex[:8]}",
							"sku": s,
							"name": "",
							"barcode": "",
							"info1": "",
							"info2": "",
							"info3": "",
						}
					)
	elif isinstance(products, list):
		for sku in products:
			s = cstr(sku).strip()
			if s:
				rows.append(
					{
						"id": f"row-{uuid.uuid4().hex[:8]}",
						"sku": s,
						"name": "",
						"barcode": "",
						"info1": "",
						"info2": "",
						"info3": "",
					}
				)
	for row in section.get("productRows") or []:
		if not isinstance(row, dict):
			continue
		sku = cstr(row.get("sku") or "").strip()
		if not sku:
			continue
		if any(r.get("sku") == sku for r in rows):
			continue
		rows.append(
			{
				"id": cstr(row.get("id") or "").strip() or f"row-{uuid.uuid4().hex[:8]}",
				"sku": sku,
				"name": cstr(row.get("name") or "").strip(),
				"barcode": cstr(row.get("barcode") or "").strip(),
				"info1": cstr(row.get("info1") or "").strip(),
				"info2": cstr(row.get("info2") or "").strip(),
				"info3": cstr(row.get("info3") or "").strip(),
			}
		)
	skus = [r["sku"] for r in rows if r.get("sku")]
	return {
		"_meta": {"version": 2, "kind": kind, "hidden_row_ids": hidden},
		"rows": rows,
		"skus": skus,
	}


def _remove_sku_from_sections(sections: list, item_code: str) -> list:
	sku = cstr(item_code).strip()
	out = []
	for section in sections:
		if not isinstance(section, dict):
			continue
		s = dict(section)
		payload = _products_payload(s)
		rows = [r for r in payload["rows"] if cstr(r.get("sku")).strip() != sku]
		payload["rows"] = rows
		payload["skus"] = [r["sku"] for r in rows if r.get("sku")]
		s["products"] = payload
		s.pop("productRows", None)
		out.append(s)
	return out


def _add_sku_to_section(sections: list, section_id: str, item_code: str, item_name: str = "") -> list:
	sku = cstr(item_code).strip()
	sid = cstr(section_id).strip()
	out = []
	for section in sections:
		if not isinstance(section, dict):
			continue
		s = dict(section)
		payload = _products_payload(s)
		if cstr(s.get("id")) == sid:
			if section_kind(s) != "rack":
				frappe.throw(_("Can only assign products to racks"), frappe.ValidationError)
			if not any(cstr(r.get("sku")).strip() == sku for r in payload["rows"]):
				payload["rows"].append(
					{
						"id": f"row-{uuid.uuid4().hex[:8]}",
						"sku": sku,
						"name": cstr(item_name or "").strip(),
						"barcode": "",
						"info1": "",
						"info2": "",
						"info3": "",
					}
				)
			payload["skus"] = [r["sku"] for r in payload["rows"] if r.get("sku")]
		s["products"] = payload
		s.pop("productRows", None)
		out.append(s)
	return out


def _set_item_provisional(item_code: str, path: str | None):
	if not _ensure_physical_section_field():
		return
	val = cstr(path or "").strip() or None
	frappe.db.set_value("Item", item_code, "custom_physical_section", val)


@frappe.whitelist(allow_guest=True)
def get_floors(company=None):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	filters = {}
	if _has_company_column():
		active = company_scope(company)
		if active:
			filters["company"] = active
	floors_raw = frappe.get_all(
		"ECommerce Floor Map",
		filters=filters,
		fields=["name", "location_name", "floor_name", "canvas_width", "canvas_height", "sections_data", "accent"],
		order_by="modified desc",
		ignore_permissions=True,
	)

	floors = []
	for f in floors_raw:
		sections = json.loads(f.sections_data or "[]")
		preview = [
			{"x": s["x"], "y": s["y"], "width": s["width"], "height": s["height"], "color": s["color"]}
			for s in sections
			if isinstance(s, dict)
		]
		floors.append(
			{
				"id": f.name,
				"title": f"{f.location_name} / {f.floor_name}",
				"locationName": f.location_name,
				"floorName": f.floor_name,
				"accent": f.accent or "#fb7185",
				"previewSections": preview,
				"previewCanvas": {"width": f.canvas_width or 1400, "height": f.canvas_height or 900},
			}
		)

	return {"floors": floors}


@frappe.whitelist(allow_guest=True)
def get_floor_sections(floor_id):
	if not frappe.db.exists("ECommerce Floor Map", floor_id):
		frappe.throw(_("Floor {0} not found").format(floor_id), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("ECommerce Floor Map", floor_id)
	frappe.flags.ignore_permissions = False
	sections = json.loads(doc.sections_data or "[]")
	notes = json.loads(doc.notes_data or "[]")

	return {
		"floor": {
			"id": doc.name,
			"title": f"{doc.location_name} / {doc.floor_name}",
			"locationName": doc.location_name,
			"floorName": doc.floor_name,
			"accent": doc.accent or "#fb7185",
		},
		"sections": sections,
		"notes": notes,
		"canvas": {"width": doc.canvas_width or 1400, "height": doc.canvas_height or 900},
	}


@frappe.whitelist(allow_guest=True)
def save_floor_map(
	location_name,
	floor_name,
	sections_data="[]",
	notes_data="[]",
	canvas_width=1400,
	canvas_height=900,
	company=None,
):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	# None / "null" / "" → omit (keep existing on update). Explicit "[]" still clears.
	sections_omitted = sections_data is None or (
		isinstance(sections_data, str) and _blankish(sections_data)
	)
	notes_omitted = notes_data is None or (isinstance(notes_data, str) and _blankish(notes_data))

	if sections_omitted:
		sections = []
		sections_json = None
	elif isinstance(sections_data, str):
		try:
			sections = json.loads(sections_data or "[]")
		except Exception:
			sections = []
		sections_json = sections_data
	else:
		sections = sections_data if isinstance(sections_data, list) else []
		sections_json = json.dumps(sections)

	if notes_omitted:
		notes_json = None
	elif isinstance(notes_data, str):
		notes_json = notes_data
	else:
		notes_json = json.dumps(notes_data if notes_data is not None else [])

	accent = (
		sections[0].get("color", "#fb7185")
		if isinstance(sections, list) and sections and isinstance(sections[0], dict)
		else "#fb7185"
	)

	active = None
	if _has_company_column():
		# resolve_company already rejects ghosts; blankish / missing → leave company unset
		try:
			active = resolve_company(None if _blankish(company) else company)
		except frappe.DoesNotExistError:
			# Stale client company after rename — fall back to any real company
			active = resolve_company(None)
		if active and not frappe.db.exists("Company", active):
			active = None

	filters = {"location_name": location_name, "floor_name": floor_name}
	if active:
		filters["company"] = active
	existing = frappe.db.get_value("ECommerce Floor Map", filters, "name")

	# Also match maps orphaned under a renamed/ghost company (same location+floor).
	if not existing:
		existing = frappe.db.get_value(
			"ECommerce Floor Map",
			{"location_name": location_name, "floor_name": floor_name},
			"name",
		)

	if existing:
		doc = frappe.get_doc("ECommerce Floor Map", existing)
		if sections_json is not None:
			doc.sections_data = sections_json
			doc.accent = accent
		if notes_json is not None:
			doc.notes_data = notes_json
		if canvas_width not in (None, "", "null"):
			doc.canvas_width = int(canvas_width)
		if canvas_height not in (None, "", "null"):
			doc.canvas_height = int(canvas_height)
		if active:
			doc.company = active
		doc.flags.ignore_links = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return {"status": "updated", "message": "Floor map updated", "floor_id": doc.name}

	doc = frappe.new_doc("ECommerce Floor Map")
	doc.location_name = location_name
	doc.floor_name = floor_name
	if active:
		doc.company = active
	doc.sections_data = sections_json if sections_json is not None else "[]"
	doc.notes_data = notes_json if notes_json is not None else "[]"
	doc.canvas_width = int(canvas_width or 1400)
	doc.canvas_height = int(canvas_height or 900)
	doc.accent = accent
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"status": "created", "message": "Floor map created", "floor_id": doc.name}


@frappe.whitelist(allow_guest=True)
def delete_floor_map(floor_id):
	if not frappe.db.exists("ECommerce Floor Map", floor_id):
		frappe.throw(_("Floor {0} not found").format(floor_id), frappe.DoesNotExistError)

	frappe.delete_doc("ECommerce Floor Map", floor_id, ignore_permissions=True)
	frappe.db.commit()
	return {"status": "deleted", "message": "Floor map deleted", "deleted_floor_id": floor_id}


@frappe.whitelist(allow_guest=True)
def get_section_details(section_id):
	floors = frappe.get_all("ECommerce Floor Map", fields=["name", "sections_data"], ignore_permissions=True)
	for f in floors:
		for section in json.loads(f.sections_data or "[]"):
			if isinstance(section, dict) and section.get("id") == section_id:
				return {"section": section}
	frappe.throw(_("Section {0} not found").format(section_id), frappe.DoesNotExistError)


@frappe.whitelist(allow_guest=True)
def get_floor_containment(floor_id=None, company=None):
	"""Return containment index summary for a floor (or first company floor)."""
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	filters = {}
	if floor_id and not _blankish(floor_id):
		filters["name"] = cstr(floor_id).strip()
	elif _has_company_column():
		active = company_scope(company)
		if active:
			filters["company"] = active

	floors = frappe.get_all(
		"ECommerce Floor Map",
		filters=filters or None,
		fields=["name", "location_name", "floor_name", "sections_data", "canvas_width", "canvas_height"],
		order_by="modified desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	if not floors:
		return {"floor_id": None, "sections": [], "containment": {}}

	f = floors[0]
	sections = load_sections_json(f.sections_data)
	index = build_containment_index(sections)

	members = []
	for s in sections:
		sid = s.get("id")
		kind = section_kind(s)
		codes = index["path_codes"](sid)
		children = [
			{"id": c.get("id"), "code": c.get("code") or "", "name": c.get("name") or ""}
			for c in index["children_of"](sid)
		]
		members.append(
			{
				"id": sid,
				"code": s.get("code") or "",
				"name": s.get("name") or "",
				"kind": kind,
				"x": s.get("x") or 0,
				"y": s.get("y") or 0,
				"width": s.get("width") or 0,
				"height": s.get("height") or 0,
				"color": s.get("color") or "#94a3b8",
				"path": index["path_of"](sid),
				"zone": codes.get("zone"),
				"fila": codes.get("fila"),
				"rack": codes.get("rack"),
				"children": children,
			}
		)

	return {
		"floor_id": f.name,
		"title": f"{f.location_name} / {f.floor_name}",
		"canvas": {"width": f.canvas_width or 1400, "height": f.canvas_height or 900},
		"sections": members,
	}


@frappe.whitelist(allow_guest=True)
def get_item_floor_location(item_code, floor_id=None, company=None):
	"""Resolve current rack path + provisional path for an item."""
	sku = cstr(item_code or "").strip()
	if not sku:
		return {
			"item_code": "",
			"location_path": "",
			"provisional_path": "",
			"section_id": None,
			"floor_id": None,
		}

	if not frappe.db.exists("Item", sku):
		return {
			"item_code": sku,
			"floor_id": None,
			"title": None,
			"section_id": None,
			"location": "",
			"location_path": "",
			"provisional_path": "",
		}

	provisional = ""
	if frappe.db.has_column("Item", "custom_physical_section"):
		provisional = cstr(frappe.db.get_value("Item", sku, "custom_physical_section") or "").strip()

	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	filters = {}
	if floor_id and not _blankish(floor_id):
		filters["name"] = cstr(floor_id).strip()
	elif _has_company_column():
		active = company_scope(company)
		if active:
			filters["company"] = active

	floors = frappe.get_all(
		"ECommerce Floor Map",
		filters=filters or None,
		fields=["name", "location_name", "floor_name", "sections_data"],
		order_by="modified desc",
		ignore_permissions=True,
	)

	for f in floors:
		sections = load_sections_json(f.sections_data)
		index = build_containment_index(sections)
		locs = index["sku_locations"]()
		info = locs.get(sku)
		if info:
			return {
				"item_code": sku,
				"floor_id": f.name,
				"title": f"{f.location_name} / {f.floor_name}",
				"section_id": info.get("section_id"),
				"location": info.get("location") or "",
				"location_path": info.get("location_path") or "",
				"provisional_path": provisional,
			}

	return {
		"item_code": sku,
		"floor_id": floors[0].name if floors else None,
		"title": f"{floors[0].location_name} / {floors[0].floor_name}" if floors else None,
		"section_id": None,
		"location": "",
		"location_path": provisional.replace("/", " › ") if provisional else "",
		"provisional_path": provisional,
	}


@frappe.whitelist(allow_guest=True)
def assign_item_floor_location(
	item_code=None,
	floor_id=None,
	section_id=None,
	provisional_path=None,
	company=None,
):
	"""
	Assign item to a rack section, or set a provisional Zone/Fila path.
	Passing section_id of a rack moves the SKU onto that rack and clears provisional.
	Passing only provisional_path removes rack membership and stores the path on Item.
	"""
	sku = cstr(item_code or "").strip()
	if not sku:
		return {"ok": False, "error": "item_code required"}

	if not frappe.db.exists("Item", sku):
		return {"ok": False, "error": f"Item {sku} not found"}

	sid = None if _blankish(section_id) else cstr(section_id).strip()
	prov_raw = None if _blankish(provisional_path) else cstr(provisional_path).strip()

	# Normalize provisional from path parts if caller sent Z21/F23 style
	if prov_raw:
		parsed = parse_provisional_path(prov_raw)
		prov_raw = provisional_path_string(parsed.get("zone"), parsed.get("fila"), parsed.get("rack")) or None

	item_name = frappe.db.get_value("Item", sku, "item_name") or ""

	# Clear assignment only
	if not sid and not prov_raw:
		_clear_item_from_all_floors(sku, company=company)
		_set_item_provisional(sku, None)
		frappe.db.commit()
		return {"ok": True, "item_code": sku, "cleared": True, "location_path": ""}

	floor_doc = _resolve_floor_doc(floor_id=floor_id, company=company, section_id=sid)
	if not floor_doc:
		# provisional-only without a floor still allowed
		if prov_raw and not sid:
			_set_item_provisional(sku, prov_raw)
			frappe.db.commit()
			parsed = parse_provisional_path(prov_raw)
			return {
				"ok": True,
				"item_code": sku,
				"floor_id": None,
				"section_id": None,
				"location_path": format_location_path(
					parsed.get("zone"), parsed.get("fila"), parsed.get("rack")
				),
				"provisional_path": prov_raw,
			}
		return {"ok": False, "error": "floor_id required"}

	sections = load_sections_json(floor_doc.sections_data)
	index = build_containment_index(sections)

	if sid:
		target = next((s for s in sections if isinstance(s, dict) and s.get("id") == sid), None)
		if not target:
			return {"ok": False, "error": f"section {sid} not found on floor"}
		kind = section_kind(target)
		if kind == "rack":
			# Remove from all floors in company scope, then add to this rack
			_clear_item_from_all_floors(sku, company=company, except_floor=floor_doc.name)
			sections = load_sections_json(floor_doc.sections_data)
			sections = _remove_sku_from_sections(sections, sku)
			sections = _add_sku_to_section(sections, sid, sku, item_name)
			floor_doc.sections_data = json.dumps(sections)
			floor_doc.save(ignore_permissions=True)
			_set_item_provisional(sku, None)
			frappe.db.commit()
			index = build_containment_index(sections)
			return {
				"ok": True,
				"item_code": sku,
				"floor_id": floor_doc.name,
				"section_id": sid,
				"location_path": index["path_of"](sid),
				"provisional_path": "",
			}

		# Zone / fila click → provisional only, still remove from racks
		codes = index["path_codes"](sid)
		prov = provisional_path_string(codes.get("zone"), codes.get("fila"), codes.get("rack"))
		_clear_item_from_all_floors(sku, company=company)
		_set_item_provisional(sku, prov)
		frappe.db.commit()
		return {
			"ok": True,
			"item_code": sku,
			"floor_id": floor_doc.name,
			"section_id": sid,
			"location_path": format_location_path(codes.get("zone"), codes.get("fila"), codes.get("rack")),
			"provisional_path": prov,
		}

	# provisional path only
	_clear_item_from_all_floors(sku, company=company)
	_set_item_provisional(sku, prov_raw)
	frappe.db.commit()
	parsed = parse_provisional_path(prov_raw)
	return {
		"ok": True,
		"item_code": sku,
		"floor_id": floor_doc.name,
		"section_id": None,
		"location_path": format_location_path(parsed.get("zone"), parsed.get("fila"), parsed.get("rack")),
		"provisional_path": prov_raw,
	}


def _resolve_floor_doc(floor_id=None, company=None, section_id=None):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	if floor_id and not _blankish(floor_id):
		fid = cstr(floor_id).strip()
		if frappe.db.exists("ECommerce Floor Map", fid):
			frappe.flags.ignore_permissions = True
			doc = frappe.get_doc("ECommerce Floor Map", fid)
			frappe.flags.ignore_permissions = False
			return doc
		return None

	filters = {}
	if _has_company_column():
		active = company_scope(company)
		if active:
			filters["company"] = active

	floors = frappe.get_all(
		"ECommerce Floor Map",
		filters=filters or None,
		fields=["name", "sections_data"],
		order_by="modified desc",
		ignore_permissions=True,
	)
	if section_id and not _blankish(section_id):
		sid = cstr(section_id).strip()
		for f in floors:
			for s in load_sections_json(f.sections_data):
				if isinstance(s, dict) and s.get("id") == sid:
					frappe.flags.ignore_permissions = True
					doc = frappe.get_doc("ECommerce Floor Map", f.name)
					frappe.flags.ignore_permissions = False
					return doc

	if floors:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("ECommerce Floor Map", floors[0].name)
		frappe.flags.ignore_permissions = False
		return doc
	return None


def _clear_item_from_all_floors(item_code, company=None, except_floor=None):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	filters = {}
	if _has_company_column():
		active = company_scope(company)
		if active:
			filters["company"] = active
	floors = frappe.get_all(
		"ECommerce Floor Map",
		filters=filters or None,
		fields=["name"],
		ignore_permissions=True,
	)
	sku = cstr(item_code).strip()
	for f in floors:
		if except_floor and f.name == except_floor:
			continue
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("ECommerce Floor Map", f.name)
		frappe.flags.ignore_permissions = False
		sections = load_sections_json(doc.sections_data)
		before = json.dumps(sections, sort_keys=True)
		sections = _remove_sku_from_sections(sections, sku)
		after = json.dumps(sections, sort_keys=True)
		if before != after:
			doc.sections_data = json.dumps(sections)
			doc.save(ignore_permissions=True)
