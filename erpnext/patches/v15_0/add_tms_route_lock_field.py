import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Delivery Trip": [
		{
			"fieldname": "custom_locked",
			"fieldtype": "Check",
			"label": "Route Locked",
			"default": "0",
			"insert_after": "custom_pickup_warehouse",
			"description": "When locked, stops cannot be added/removed/reordered; auto-routing skips the trip. Delivery outcomes and order line edits remain allowed.",
		},
	],
}


def execute():
	frappe.reload_doc("stock", "doctype", "delivery_trip", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
