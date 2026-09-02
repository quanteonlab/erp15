import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Delivery Stop": [
		{
			"fieldname": "custom_amount_due",
			"fieldtype": "Currency",
			"label": "Amount Due",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_photo_urls",
		},
		{
			"fieldname": "custom_amount_collected",
			"fieldtype": "Currency",
			"label": "Amount Collected",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_amount_due",
		},
		{
			"fieldname": "custom_payment_method",
			"fieldtype": "Select",
			"label": "Payment Method",
			"options": "\nCash\nTransfer\nOther",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_amount_collected",
		},
		{
			"fieldname": "custom_cliente_debe",
			"fieldtype": "Check",
			"label": "Cliente Debe",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_payment_method",
		},
		{
			"fieldname": "custom_balance_after_stop",
			"fieldtype": "Currency",
			"label": "Balance After Stop",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_cliente_debe",
		},
	],
}


def execute():
	frappe.reload_doc("stock", "doctype", "delivery_stop", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
