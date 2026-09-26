import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Address": [
		{
			"fieldname": "custom_delivery_frequency",
			"fieldtype": "Int",
			"label": "Delivery Frequency (per week)",
			"default": "1",
			"insert_after": "custom_zone",
			"description": "How many times per week this address should be visited (1–7).",
		},
	],
}


def execute():
	frappe.reload_doc("contacts", "doctype", "address", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
