import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Address": [
		{
			"fieldname": "custom_zone",
			"fieldtype": "Select",
			"label": "Delivery Zone",
			"options": "\nNorte\nSur\nEste\nOeste\nCentro",
			"insert_after": "custom_geocoded_on",
		},
	],
}


def execute():
	frappe.reload_doc("contacts", "doctype", "address", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
