import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Item": [
		{
			"fieldname": "custom_unit_weight_min",
			"fieldtype": "Float",
			"label": "Min Unit Weight",
			"insert_after": "weight_uom",
			"description": "Soft min weight per stock unit (same UOM as Weight UOM). Used by Armado advisories.",
		},
		{
			"fieldname": "custom_unit_weight_max",
			"fieldtype": "Float",
			"label": "Max Unit Weight",
			"insert_after": "custom_unit_weight_min",
			"description": "Soft max weight per stock unit (same UOM as Weight UOM). Used by Armado advisories.",
		},
	]
}


def execute():
	create_custom_fields(CUSTOM_FIELDS, update=True)
	frappe.clear_cache(doctype="Item")
