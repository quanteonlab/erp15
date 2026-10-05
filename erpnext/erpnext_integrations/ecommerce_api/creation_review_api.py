"""Admin soft-review queue for Customers and guest preorders created by non-admins.

Records are created live; ``custom_creation_review=Pending`` parks them in
Tables → Revisión until an admin confirms or deletes.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr


FIELDNAME = "custom_creation_review"
STATUS_PENDING = "Pending"
STATUS_CONFIRMED = "Confirmed"
_ADMIN_ROLES = {"Administrator", "System Manager"}
_FIELDS_READY = False


def ensure_creation_review_custom_fields():
	"""Idempotent Select field on Customer + Sales Order.

	Safe under API-key / non-admin sessions: Custom Field docs are inserted with
	``ignore_permissions=True`` (``frappe.flags.ignore_permissions`` alone does
	not bypass ``Document.insert`` permission checks).
	"""
	global _FIELDS_READY
	if frappe.db.has_column("Customer", FIELDNAME) and frappe.db.has_column(
		"Sales Order", FIELDNAME
	):
		_FIELDS_READY = True
		return
	if _FIELDS_READY:
		return

	defs = (
		(
			"Customer",
			{
				"fieldname": FIELDNAME,
				"label": "Creation Review",
				"fieldtype": "Select",
				"options": f"\n{STATUS_PENDING}\n{STATUS_CONFIRMED}",
				"insert_after": "disabled",
				"in_standard_filter": 1,
			},
		),
		(
			"Sales Order",
			{
				"fieldname": FIELDNAME,
				"label": "Creation Review",
				"fieldtype": "Select",
				"options": f"\n{STATUS_PENDING}\n{STATUS_CONFIRMED}",
				"insert_after": "status",
				"in_standard_filter": 1,
				"allow_on_submit": 1,
			},
		),
	)

	created = False
	for doctype, df in defs:
		if frappe.db.has_column(doctype, FIELDNAME):
			continue
		if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": FIELDNAME}):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Custom Field",
				"dt": doctype,
				**df,
			}
		)
		doc.insert(ignore_permissions=True)
		created = True

	if created:
		# Schema change must survive even if the outer request later rolls back.
		frappe.db.commit()
		frappe.clear_cache(doctype="Customer")
		frappe.clear_cache(doctype="Sales Order")

	_FIELDS_READY = bool(
		frappe.db.has_column("Customer", FIELDNAME)
		and frappe.db.has_column("Sales Order", FIELDNAME)
	)


def _acting_or_session_user(explicit_user=None) -> str:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username

	raw = cstr(explicit_user or "").strip() or _acting_username() or cstr(frappe.session.user or "").strip()
	return raw


def is_creation_review_admin(username=None) -> bool:
	"""True for Administrator / System Manager (desk admin accounts)."""
	user = _acting_or_session_user(username)
	if not user or user == "Guest":
		return False
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user) or [])
	return bool(roles & _ADMIN_ROLES)


def maybe_mark_creation_review_pending(doctype: str, name: str, actor=None) -> bool:
	"""Set Pending when actor is non-admin. Returns True if marked."""
	if not doctype or not name:
		return False
	if is_creation_review_admin(actor):
		return False
	ensure_creation_review_custom_fields()
	if not frappe.db.has_column(doctype, FIELDNAME):
		return False
	if not frappe.db.exists(doctype, name):
		return False
	frappe.db.set_value(doctype, name, FIELDNAME, STATUS_PENDING, update_modified=False)
	return True


def _owner_full_name(owner: str) -> str:
	owner = cstr(owner or "").strip()
	if not owner:
		return ""
	full = frappe.db.get_value("User", owner, "full_name")
	return cstr(full or owner)


def _primary_address_line(customer: str) -> str | None:
	customer = cstr(customer or "").strip()
	if not customer:
		return None
	rows = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		fields=["parent"],
		limit_page_length=5,
		ignore_permissions=True,
	)
	for row in rows or []:
		line = frappe.db.get_value("Address", row.parent, "address_line1")
		if cstr(line or "").strip():
			return cstr(line).strip()
	return None


def _normalize_kind(kind) -> str:
	raw = cstr(kind or "").strip().lower()
	if raw in ("customer", "client", "clients", "cliente", "clientes"):
		return "customer"
	if raw in ("order", "orders", "pedido", "pedidos", "preorder", "sales_order"):
		return "order"
	frappe.throw(_("kind must be customer or order"), frappe.ValidationError)


def _normalize_status(status) -> str | None:
	raw = cstr(status or "").strip()
	if not raw or raw.lower() in ("null", "undefined", "all", "*"):
		return None
	low = raw.lower()
	if low in ("pending", "open", "pendiente"):
		return STATUS_PENDING
	if low in ("confirmed", "done", "approved", "confirmado", "listo"):
		return STATUS_CONFIRMED
	if raw in (STATUS_PENDING, STATUS_CONFIRMED):
		return raw
	frappe.throw(_("Invalid status"), frappe.ValidationError)


def list_creation_reviews(kind="customer", status="Pending", limit=100, start=0):
	"""List pending/confirmed creation-review rows for Revisión tabs."""
	ensure_creation_review_custom_fields()
	kind = _normalize_kind(kind)
	status_filter = _normalize_status(status)
	limit = max(1, min(cint(limit) or 100, 500))
	start = max(0, cint(start) or 0)

	if not frappe.db.has_column(
		"Customer" if kind == "customer" else "Sales Order", FIELDNAME
	):
		return []

	if kind == "customer":
		filters = {"disabled": 0}
		if status_filter:
			filters[FIELDNAME] = status_filter
		else:
			filters[FIELDNAME] = ["in", [STATUS_PENDING, STATUS_CONFIRMED]]
		rows = frappe.get_all(
			"Customer",
			filters=filters,
			fields=[
				"name",
				"customer_name",
				"mobile_no",
				"email_id",
				"territory",
				"tax_id",
				"owner",
				"creation",
				"modified",
				FIELDNAME,
			],
			order_by="creation desc",
			limit_start=start,
			limit_page_length=limit,
			ignore_permissions=True,
		)
		out = []
		for r in rows:
			out.append(
				{
					"kind": "customer",
					"name": r.name,
					"customer_name": r.customer_name or r.name,
					"phone": r.mobile_no,
					"email": r.email_id,
					"territory": r.territory,
					"tax_id": r.tax_id,
					"address_line1": _primary_address_line(r.name),
					"owner": r.owner,
					"owner_full_name": _owner_full_name(r.owner),
					"creation": str(r.creation) if r.creation else None,
					"modified": str(r.modified) if r.modified else None,
					"review_status": getattr(r, FIELDNAME, None) or status_filter,
				}
			)
		return out

	# Orders = guest preorders only
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_display_status_from_row,
		_guest_preorder_tag_fieldname,
	)

	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return []

	filters = {"docstatus": ["<", 2]}
	if status_filter:
		filters[FIELDNAME] = status_filter
	else:
		filters[FIELDNAME] = ["in", [STATUS_PENDING, STATUS_CONFIRMED]]
	filters[tag_fn] = ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"]

	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"grand_total",
			"currency",
			"status",
			"docstatus",
			"transaction_date",
			"delivery_date",
			"owner",
			"creation",
			"modified",
			"advance_paid",
			tag_fn,
			FIELDNAME,
		],
		order_by="creation desc",
		limit_start=start,
		limit_page_length=limit,
		ignore_permissions=True,
	)
	out = []
	for r in rows:
		tags = {}
		for part in str(getattr(r, tag_fn, None) or "").split("|"):
			part = part.strip()
			if ":" in part:
				key, val = part.split(":", 1)
				tags[key.strip()] = val.strip()
		out.append(
			{
				"kind": "order",
				"name": r.name,
				"customer": r.customer,
				"customer_name": r.customer_name
				or frappe.db.get_value("Customer", r.customer, "customer_name")
				or r.customer,
				"guest_name": tags.get("guest_name"),
				"guest_phone": tags.get("guest_phone"),
				"guest_address": tags.get("guest_address"),
				"grand_total": float(r.grand_total or 0),
				"currency": r.currency,
				"status": r.status,
				"display_status": _display_status_from_row(
					r.docstatus, r.status, r.grand_total, getattr(r, "advance_paid", 0)
				),
				"docstatus": r.docstatus,
				"transaction_date": str(r.transaction_date) if r.transaction_date else None,
				"delivery_date": str(r.delivery_date) if r.delivery_date else None,
				"owner": r.owner,
				"owner_full_name": _owner_full_name(r.owner),
				"creation": str(r.creation) if r.creation else None,
				"modified": str(r.modified) if r.modified else None,
				"review_status": getattr(r, FIELDNAME, None) or status_filter,
			}
		)
	return out


def confirm_creation_review(kind=None, name=None):
	"""Mark a pending creation as Confirmed (leave the Revisión queue)."""
	ensure_creation_review_custom_fields()
	kind = _normalize_kind(kind)
	name = cstr(name or "").strip()
	if not name:
		frappe.throw(_("name is required"), frappe.ValidationError)

	doctype = "Customer" if kind == "customer" else "Sales Order"
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(doctype, name), frappe.DoesNotExistError)

	if kind == "order":
		from erpnext.erpnext_integrations.ecommerce_api.api import _is_guest_preorder_sales_order

		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", name)
		frappe.flags.ignore_permissions = False
		if not _is_guest_preorder_sales_order(so):
			frappe.throw(_("Not a Guest Preorder"), frappe.ValidationError)

	frappe.db.set_value(doctype, name, FIELDNAME, STATUS_CONFIRMED)
	frappe.db.commit()
	return {"ok": True, "kind": kind, "name": name, "review_status": STATUS_CONFIRMED}


def delete_creation_review(kind=None, name=None):
	"""Delete (or disable/cancel) a pending creation and clear it from the queue."""
	ensure_creation_review_custom_fields()
	kind = _normalize_kind(kind)
	name = cstr(name or "").strip()
	if not name:
		frappe.throw(_("name is required"), frappe.ValidationError)

	if kind == "customer":
		if not frappe.db.exists("Customer", name):
			frappe.throw(_("Customer {0} not found").format(name), frappe.DoesNotExistError)

		# Linked transactional docs → disable instead of hard-delete.
		linked = False
		for dt in ("Sales Order", "Sales Invoice", "Delivery Note", "Payment Entry"):
			if frappe.db.exists(dt, {"customer": name, "docstatus": ["<", 2]}):
				linked = True
				break

		if linked:
			frappe.db.set_value("Customer", name, "disabled", 1)
			if frappe.db.has_column("Customer", FIELDNAME):
				frappe.db.set_value("Customer", name, FIELDNAME, STATUS_CONFIRMED)
			frappe.db.commit()
			return {
				"ok": True,
				"kind": kind,
				"name": name,
				"action": "disabled",
			}

		frappe.delete_doc("Customer", name, ignore_permissions=True, force=True)
		frappe.db.commit()
		return {"ok": True, "kind": kind, "name": name, "action": "deleted"}

	# Order → delete draft guest preorders; cancel submitted ones
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_is_guest_preorder_sales_order,
		cancel_guest_preorder,
	)

	if not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name), frappe.DoesNotExistError)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"), frappe.ValidationError)

	if cint(so.docstatus) == 0:
		frappe.delete_doc("Sales Order", name, ignore_permissions=True, force=True)
		frappe.db.commit()
		return {"ok": True, "kind": kind, "name": name, "action": "deleted"}

	result = cancel_guest_preorder(name)
	if frappe.db.exists("Sales Order", name) and frappe.db.has_column("Sales Order", FIELDNAME):
		frappe.db.set_value("Sales Order", name, FIELDNAME, STATUS_CONFIRMED)
		frappe.db.commit()
	return {
		"ok": True,
		"kind": kind,
		"name": name,
		"action": "cancelled",
		"detail": result,
	}


def count_pending_creation_reviews():
	"""Lightweight counts for notification badges."""
	ensure_creation_review_custom_fields()
	out = {"customers": 0, "orders": 0}
	if frappe.db.has_column("Customer", FIELDNAME):
		out["customers"] = cint(
			frappe.db.count("Customer", {FIELDNAME: STATUS_PENDING, "disabled": 0})
		)
	if frappe.db.has_column("Sales Order", FIELDNAME):
		from erpnext.erpnext_integrations.ecommerce_api.api import (
			GUEST_PREORDER_REMARKS_TAG,
			_guest_preorder_tag_fieldname,
		)

		tag_fn = _guest_preorder_tag_fieldname()
		if tag_fn:
			out["orders"] = cint(
				frappe.db.count(
					"Sales Order",
					{
						FIELDNAME: STATUS_PENDING,
						"docstatus": ["<", 2],
						tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"],
					},
				)
			)
	out["total"] = out["customers"] + out["orders"]
	return out
