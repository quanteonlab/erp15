import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"System Settings": [
				{
					"fieldname": "erpnext_dual_language_section",
					"fieldtype": "Section Break",
					"label": "Quick Language Toggle",
					"insert_after": "time_zone",
					"collapsible": 1,
				},
				{
					"fieldname": "erpnext_dual_language_1",
					"fieldtype": "Link",
					"options": "Language",
					"label": "Dual Language 1",
					"default": "zh",
					"insert_after": "erpnext_dual_language_section",
					"description": (
						"The two languages users can quickly switch between from the account "
						"menu's \"Toggle Language\" item."
					),
				},
				{
					"fieldname": "erpnext_dual_language_column_break",
					"fieldtype": "Column Break",
					"insert_after": "erpnext_dual_language_1",
				},
				{
					"fieldname": "erpnext_dual_language_2",
					"fieldtype": "Link",
					"options": "Language",
					"label": "Dual Language 2",
					"default": "es",
					"insert_after": "erpnext_dual_language_column_break",
				},
			]
		}
	)

	# create_custom_fields only sets `default` for new documents — System
	# Settings already exists, so backfill it directly if unset.
	settings = frappe.get_single("System Settings")
	changed = False
	if not settings.get("erpnext_dual_language_1"):
		settings.erpnext_dual_language_1 = "zh"
		changed = True
	if not settings.get("erpnext_dual_language_2"):
		settings.erpnext_dual_language_2 = "es"
		changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()
