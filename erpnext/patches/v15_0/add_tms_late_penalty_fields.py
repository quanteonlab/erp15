import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Delivery Stop": [
		{
			"fieldname": "custom_late_penalty_pct",
			"fieldtype": "Percent",
			"label": "Late Payment Penalty %",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_balance_after_stop",
		},
		{
			"fieldname": "custom_late_penalty_base",
			"fieldtype": "Currency",
			"label": "Late Penalty Base",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_late_penalty_pct",
		},
		{
			"fieldname": "custom_late_penalty_amount",
			"fieldtype": "Currency",
			"label": "Late Penalty Amount",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_late_penalty_base",
		},
		{
			"fieldname": "custom_late_penalty_applied",
			"fieldtype": "Check",
			"label": "Late Penalty Applied",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_late_penalty_amount",
		},
		{
			"fieldname": "custom_late_penalty_note",
			"fieldtype": "Small Text",
			"label": "Late Penalty Note",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_late_penalty_applied",
		},
	],
}


def execute():
	frappe.reload_doc("stock", "doctype", "delivery_stop", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
