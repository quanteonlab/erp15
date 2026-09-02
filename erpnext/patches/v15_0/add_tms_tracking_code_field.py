import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Delivery Note": [
		{
			"fieldname": "custom_tracking_code",
			"fieldtype": "Data",
			"label": "Tracking Code",
			"unique": 1,
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "title",
		},
		{
			"fieldname": "custom_requested_delivery_date",
			"fieldtype": "Date",
			"label": "Requested Delivery Date",
			"insert_after": "custom_tracking_code",
		},
	],
}


def execute():
	frappe.reload_doc("stock", "doctype", "delivery_note", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
