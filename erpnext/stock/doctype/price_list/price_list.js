// Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Price List", {
	refresh: function (frm) {
		frm.add_custom_button(
			__("Add / Edit Prices"),
			function () {
				frappe.route_options = {
					price_list: frm.doc.name,
				};
				frappe.set_route("Report", "Item Price");
			},
			"fa fa-money"
		);

		if (cint(frm.doc.custom_auto_enabled) && frm.doc.custom_base_price_list) {
			frm.add_custom_button(__("Sync auto prices"), function () {
				frappe.call({
					method:
						"erpnext.erpnext_integrations.ecommerce_api.price_list_rules.run_sync_auto_prices",
					args: { price_list: frm.doc.name, force: 0 },
					freeze: true,
					freeze_message: __("Syncing from {0}…", [frm.doc.custom_base_price_list]),
					callback: function (r) {
						if (r.message) {
							frappe.show_alert({
								message: __("Updated {0} prices", [r.message.updated || 0]),
								indicator: "green",
							});
						}
					},
				});
			});
			frm.add_custom_button(__("Force re-sync (overwrite manual)"), function () {
				frappe.confirm(
					__(
						"Overwrite manually edited prices on this list from {0}?",
						[frm.doc.custom_base_price_list]
					),
					function () {
						frappe.call({
							method:
								"erpnext.erpnext_integrations.ecommerce_api.price_list_rules.run_sync_auto_prices",
							args: { price_list: frm.doc.name, force: 1 },
							freeze: true,
							callback: function (r) {
								if (r.message) {
									frappe.show_alert({
										message: __("Updated {0} prices", [
											r.message.updated || 0,
										]),
										indicator: "green",
									});
								}
							},
						});
					}
				);
			});
		}

		frm.trigger("render_auto_formula");
	},

	custom_auto_enabled: function (frm) {
		if (cint(frm.doc.custom_auto_enabled)) {
			if (!frm.doc.custom_base_price_list) {
				frm.set_value("custom_base_price_list", "Standard Buying");
			}
			if (frm.doc.name === "Transferencia" && !flt(frm.doc.custom_auto_percent)) {
				frm.set_value("custom_auto_percent", 3);
			}
		}
		frm.trigger("render_auto_formula");
	},

	custom_base_price_list: function (frm) {
		frm.trigger("render_auto_formula");
	},

	custom_auto_percent: function (frm) {
		frm.trigger("render_auto_formula");
	},

	custom_auto_add_fixed: function (frm) {
		frm.trigger("render_auto_formula");
	},

	render_auto_formula: function (frm) {
		if (!frm.fields_dict.custom_auto_formula_html) return;
		if (!cint(frm.doc.custom_auto_enabled)) {
			frm.fields_dict.custom_auto_formula_html.$wrapper.html("");
			return;
		}
		const base = frm.doc.custom_base_price_list || __("(base list)");
		const pct = flt(frm.doc.custom_auto_percent || 0);
		const add = flt(frm.doc.custom_auto_add_fixed || 0);
		const html = `
			<div class="text-muted" style="padding: 4px 0 8px;">
				<strong>${__("Charge")}</strong> =
				(${frappe.utils.escape_html(base)}) × (1 + ${pct}/100) + ${add}
				<br/>
				<span style="font-size: 11px;">
					${__(
						"Auto values stay blue until edited. Manual edits are black and not overwritten."
					)}
				</span>
			</div>`;
		frm.fields_dict.custom_auto_formula_html.$wrapper.html(html);
	},
});
