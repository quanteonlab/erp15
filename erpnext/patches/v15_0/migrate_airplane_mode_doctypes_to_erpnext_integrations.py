import frappe


DOCTYPES = [
    "Airline",
    "Airplane",
    "Airport",
    "Flight Passenger",
    "Airplane Ticket Add-on Type",
    "Airplane Ticket Add-on Item",
    "Airplane Ticket",
]


def execute():
    for dt in DOCTYPES:
        if frappe.db.exists("DocType", dt):
            frappe.reload_doc("erpnext_integrations", "doctype", frappe.scrub(dt), force=True)

    for dt in DOCTYPES:
        if frappe.db.exists("DocType", dt):
            frappe.db.set_value("DocType", dt, "module", "ERPNext Integrations", update_modified=False)
