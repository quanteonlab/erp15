import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Company": [
		{
			"fieldname": "custom_default_warehouse",
			"fieldtype": "Link",
			"label": "Default Warehouse (TMS Depot)",
			"options": "Warehouse",
			"insert_after": "default_warehouse_for_sales_return",
			"description": "Default pickup/return depot for TMS routes when a trip does not pick its own warehouse and the driver has no home address.",
		},
	],
	"Delivery Trip": [
		{
			"fieldname": "custom_pickup_warehouse",
			"fieldtype": "Link",
			"label": "Pickup Warehouse",
			"options": "Warehouse",
			"insert_after": "driver_address",
			"description": "Warehouse this route's depot address was resolved from, if any (audit only - the route itself still uses driver_address).",
		},
	],
}


def execute():
	frappe.reload_doc("setup", "doctype", "company", force=True)
	frappe.reload_doc("stock", "doctype", "delivery_trip", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
