import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Address": [
		{
			"fieldname": "tms_geocode_section",
			"fieldtype": "Section Break",
			"label": "Geocoding",
			"collapsible": 1,
			"insert_after": "is_shipping_address",
		},
		{
			"fieldname": "custom_latitude",
			"fieldtype": "Float",
			"label": "Latitude",
			"precision": "8",
			"read_only": 1,
			"insert_after": "tms_geocode_section",
		},
		{
			"fieldname": "custom_longitude",
			"fieldtype": "Float",
			"label": "Longitude",
			"precision": "8",
			"read_only": 1,
			"insert_after": "custom_latitude",
		},
		{
			"fieldname": "custom_geocoded_on",
			"fieldtype": "Datetime",
			"label": "Geocoded On",
			"read_only": 1,
			"insert_after": "custom_longitude",
		},
	],
	"Delivery Stop": [
		{
			"fieldname": "custom_pod_recipient_name",
			"fieldtype": "Data",
			"label": "POD Recipient Name",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "details",
		},
		{
			"fieldname": "custom_pod_recipient_id_number",
			"fieldtype": "Data",
			"label": "POD Recipient ID / DNI",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_recipient_name",
		},
		{
			"fieldname": "custom_pod_captured_at",
			"fieldtype": "Datetime",
			"label": "POD Captured At",
			"allow_on_submit": 1,
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_recipient_id_number",
		},
		{
			"fieldname": "custom_pod_captured_lat",
			"fieldtype": "Float",
			"label": "Captured Latitude",
			"precision": "8",
			"allow_on_submit": 1,
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_captured_at",
		},
		{
			"fieldname": "custom_pod_captured_lng",
			"fieldtype": "Float",
			"label": "Captured Longitude",
			"precision": "8",
			"allow_on_submit": 1,
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_captured_lat",
		},
		{
			"fieldname": "custom_pod_signature",
			"fieldtype": "Attach Image",
			"label": "POD Signature",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_captured_lng",
		},
		{
			"fieldname": "custom_pod_notes",
			"fieldtype": "Small Text",
			"label": "POD Notes",
			"allow_on_submit": 1,
			"no_copy": 1,
			"insert_after": "custom_pod_signature",
		},
	],
}


def execute():
	frappe.reload_doc("contacts", "doctype", "address", force=True)
	frappe.reload_doc("stock", "doctype", "delivery_stop", force=True)
	create_custom_fields(CUSTOM_FIELDS, update=True)
