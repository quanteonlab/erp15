import frappe


def execute():
	"""Copy "Keyboard Shortcuts" from the Help dropdown into the account/user
	dropdown, since the Help dropdown itself is hidden entirely (see
	desk_declutter.scss's .dropdown-help rule)."""
	navbar_settings = frappe.get_single("Navbar Settings")

	if frappe.db.exists("Navbar Item", {"item_label": "Keyboard Shortcuts", "parentfield": "settings_dropdown"}):
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
			"item_label": "Keyboard Shortcuts",
			"item_type": "Action",
			"action": "frappe.ui.toolbar.show_shortcuts(event)",
			"is_standard": 1,
			"idx": log_out_idx or (len(navbar_settings.settings_dropdown) + 1),
		},
	)

	navbar_settings.save()
