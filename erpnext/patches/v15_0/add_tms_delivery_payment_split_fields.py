import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Delivery Stop": [
		{
			"fieldname": "custom_payments_json",
			"fieldtype": "Long Text",
			"label": "Payments JSON",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_payment_method",
		},
		{
			"fieldname": "custom_requires_factura_a",
			"fieldtype": "Check",
			"label": "Requires Factura A",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_payments_json",
		},
		{
			"fieldname": "custom_factura_a_status",
			"fieldtype": "Select",
			"label": "Factura A Status",
			"options": "\npending\nissued\nna",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_requires_factura_a",
		},
		{
			"fieldname": "custom_surcharge_pct",
			"fieldtype": "Percent",
			"label": "Payment Surcharge %",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_factura_a_status",
		},
		{
			"fieldname": "custom_surcharge_base",
			"fieldtype": "Currency",
			"label": "Payment Surcharge Base",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_surcharge_pct",
		},
		{
			"fieldname": "custom_surcharge_amount",
			"fieldtype": "Currency",
			"label": "Payment Surcharge Amount",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_surcharge_base",
		},
		{
			"fieldname": "custom_surcharge_rule",
			"fieldtype": "Data",
			"label": "Payment Surcharge Rule",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_surcharge_amount",
		},
		{
			"fieldname": "custom_payment_summary",
			"fieldtype": "Small Text",
			"label": "Payment Summary",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_surcharge_rule",
		},
	],
	"Sales Order": [
		{
			"fieldname": "custom_requires_factura_a",
			"fieldtype": "Check",
			"label": "Requires Factura A",
			"allow_on_submit": 1,
			"insert_after": "customer",
		},
		{
			"fieldname": "custom_factura_a_status",
			"fieldtype": "Select",
			"label": "Factura A Status",
			"options": "\npending\nissued\nna",
			"allow_on_submit": 1,
			"insert_after": "custom_requires_factura_a",
		},
		{
			"fieldname": "custom_delivery_surcharge_amount",
			"fieldtype": "Currency",
			"label": "Delivery Surcharge Amount",
			"allow_on_submit": 1,
			"insert_after": "custom_factura_a_status",
		},
		{
			"fieldname": "custom_delivery_payment_summary",
			"fieldtype": "Small Text",
			"label": "Delivery Payment Summary",
			"allow_on_submit": 1,
			"insert_after": "custom_delivery_surcharge_amount",
		},
	],
}


def execute():
	frappe.reload_doc("stock", "doctype", "delivery_stop", force=True)
	frappe.reload_doc("selling", "doctype", "sales_order", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
