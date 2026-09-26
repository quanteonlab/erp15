"""CRM Customer extras: preferred delivery hours + ensure custom fields / seed slots."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

_DEFAULT_HOURS = [
	("Mañana", 1),
	("Mediodía", 2),
	("Tarde", 3),
	("Noche", 4),
]


def ensure_preferred_delivery_hours():
	"""Create DocType rows (Mañana…Noche) and Customer.custom_preferred_hours Link."""
	create_custom_fields(
		{
			"Customer": [
				{
					"fieldname": "custom_preferred_hours",
					"label": "Horario Preferible",
					"fieldtype": "Link",
					"options": "Preferred Delivery Hours",
					"insert_after": "territory",
					"in_list_view": 0,
					"in_standard_filter": 1,
				},
			]
		},
		ignore_validate=True,
	)

	if not frappe.db.exists("DocType", "Preferred Delivery Hours"):
		return {"ok": False, "reason": "doctype_missing"}

	created = []
	for label, priority in _DEFAULT_HOURS:
		if frappe.db.exists("Preferred Delivery Hours", label):
			# Keep priority in sync for stock defaults (do not overwrite user renames of priority
			# if they changed it — only seed missing).
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Preferred Delivery Hours",
				"label": label,
				"priority": priority,
				"disabled": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(label)

	if created:
		frappe.db.commit()
	return {"ok": True, "created": created}


@frappe.whitelist(allow_guest=True)
def list_preferred_delivery_hours(include_disabled=0):
	"""CRM select options — sorted by priority ascending."""
	ensure_preferred_delivery_hours()
	filters = {}
	try:
		include = int(include_disabled or 0)
	except (TypeError, ValueError):
		include = 0
	if not include:
		filters["disabled"] = 0
	rows = frappe.get_all(
		"Preferred Delivery Hours",
		filters=filters,
		fields=["name", "label", "priority", "disabled"],
		order_by="priority asc, label asc",
		ignore_permissions=True,
	)
	return {
		"hours": [
			{
				"name": r.name,
				"label": r.label or r.name,
				"priority": r.priority,
				"disabled": int(r.disabled or 0),
			}
			for r in rows
		]
	}


@frappe.whitelist(allow_guest=True)
def save_preferred_delivery_hours(hours=None):
	"""Replace/update the editable hours list from CRM settings-style payload."""
	ensure_preferred_delivery_hours()
	if isinstance(hours, str):
		hours = frappe.parse_json(hours)
	if not isinstance(hours, list):
		frappe.throw(_("hours must be a list"))

	seen = set()
	for idx, row in enumerate(hours):
		if not isinstance(row, dict):
			continue
		label = (row.get("label") or row.get("name") or "").strip()
		if not label:
			continue
		priority = int(row.get("priority") or (idx + 1))
		disabled = 1 if row.get("disabled") else 0
		seen.add(label)
		if frappe.db.exists("Preferred Delivery Hours", label):
			frappe.db.set_value(
				"Preferred Delivery Hours",
				label,
				{"priority": priority, "disabled": disabled},
				update_modified=True,
			)
		else:
			frappe.get_doc(
				{
					"doctype": "Preferred Delivery Hours",
					"label": label,
					"priority": priority,
					"disabled": disabled,
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()
	return list_preferred_delivery_hours(include_disabled=1)
