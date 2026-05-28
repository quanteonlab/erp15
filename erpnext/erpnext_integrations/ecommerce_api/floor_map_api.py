import json

import frappe
from frappe import _


@frappe.whitelist(allow_guest=True)
def get_floors():
    floors_raw = frappe.get_all(
        "ECommerce Floor Map",
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
def save_floor_map(location_name, floor_name, sections_data="[]", notes_data="[]", canvas_width=1400, canvas_height=900):
    sections = json.loads(sections_data) if isinstance(sections_data, str) else sections_data
    accent = sections[0].get("color", "#fb7185") if sections else "#fb7185"
    sections_json = json.dumps(sections) if not isinstance(sections_data, str) else sections_data
    notes_json = json.dumps(notes_data) if not isinstance(notes_data, str) else notes_data

    existing = frappe.db.get_value(
        "ECommerce Floor Map",
        {"location_name": location_name, "floor_name": floor_name},
        "name",
    )

    if existing:
        doc = frappe.get_doc("ECommerce Floor Map", existing)
        doc.sections_data = sections_json
        doc.notes_data = notes_json
        doc.canvas_width = int(canvas_width)
        doc.canvas_height = int(canvas_height)
        doc.accent = accent
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {"status": "updated", "message": "Floor map updated", "floor_id": doc.name}

    doc = frappe.new_doc("ECommerce Floor Map")
    doc.location_name = location_name
    doc.floor_name = floor_name
    doc.sections_data = sections_json
    doc.notes_data = notes_json
    doc.canvas_width = int(canvas_width)
    doc.canvas_height = int(canvas_height)
    doc.accent = accent
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
