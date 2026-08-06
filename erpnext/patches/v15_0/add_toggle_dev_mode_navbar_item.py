import frappe


def execute():
	navbar_settings = frappe.get_single("Navbar Settings")

	if frappe.db.exists("Navbar Item", {"item_label": "Toggle Developer Mode"}):
		return

	log_out_idx = None
	for item in navbar_settings.settings_dropdown:
		if item.item_label == "Log out":
			log_out_idx = item.idx
			break

	if log_out_idx:
		for item in navbar_settings.settings_dropdown:
			if item.idx >= log_out_idx:
				item.idx += 1

	navbar_settings.append(
		"settings_dropdown",
		{
			"item_label": "Toggle Developer Mode",
			"item_type": "Action",
			"action": "erpnext.toggle_dev_mode()",
			"is_standard": 1,
			"idx": log_out_idx or (len(navbar_settings.settings_dropdown) + 1),
		},
	)

	navbar_settings.save()
