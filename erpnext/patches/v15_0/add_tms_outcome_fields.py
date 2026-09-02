import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Delivery Stop": [
		{
			"fieldname": "custom_outcome",
			"fieldtype": "Select",
			"label": "Outcome",
			"options": "\nDelivered\nNot Home\nPartial\nRefused",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_notes",
		},
		{
			"fieldname": "custom_attempt_note",
			"fieldtype": "Small Text",
			"label": "Attempt Note (Not Home / Refused)",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_outcome",
		},
		{
			"fieldname": "custom_photo_urls",
			"fieldtype": "Long Text",
			"label": "Photo URLs (JSON list)",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_attempt_note",
		},
	],
}


def execute():
	frappe.reload_doc("stock", "doctype", "delivery_stop", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
