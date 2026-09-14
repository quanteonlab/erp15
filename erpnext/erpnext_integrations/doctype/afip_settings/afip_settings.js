// Copyright (c) 2026, and contributors
// For license information, please see license.txt

frappe.ui.form.on("AFIP Settings", {
	refresh: function (frm) {
		frm.toggle_reqd("cuit", frm.doc.enabled);
		frm.toggle_reqd("punto_venta", frm.doc.enabled);
		frm.toggle_reqd("certificate", frm.doc.enabled);
		frm.toggle_reqd("private_key", frm.doc.enabled);

		if (frm.doc.enabled && !frm.is_new()) {
			frm.add_custom_button(__("Test Connection"), () => {
				frappe.call({
					method: "erpnext.erpnext_integrations.ecommerce_api.afip_api.get_afip_status",
					freeze: true,
					callback: (r) => {
						const status = r.message || {};
						frappe.msgprint({
							title: __("AFIP Status"),
							message: `
								<b>${__("Environment")}:</b> ${status.environment || "-"}<br>
								<b>${__("Punto de Venta")}:</b> ${status.punto_venta || "-"}<br>
								<b>${__("Last WSAA Login")}:</b> ${status.last_wsaa_login || __("Never")}<br>
								<b>${__("Last Error")}:</b> ${status.last_error || __("None")}
							`,
							indicator: status.last_error ? "orange" : "green",
						});
					},
				});
			}).addClass("btn-primary");
		}
	},

	enabled: function (frm) {
		frm.toggle_reqd("cuit", frm.doc.enabled);
		frm.toggle_reqd("punto_venta", frm.doc.enabled);
		frm.toggle_reqd("certificate", frm.doc.enabled);
		frm.toggle_reqd("private_key", frm.doc.enabled);
	},
});
