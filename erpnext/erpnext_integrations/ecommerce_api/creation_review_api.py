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


def mark_seller_amend_for_review(name: str, actor=None, reason: str = "seller_amend") -> bool:
	"""Re-queue a guest preorder in Revisar after a non-admin seller amend/edit.

	Sets ``custom_creation_review=Pending`` and stamps ``review_reason:<reason>``
	on the guest-preorder tag field so admin Revisar can show why it returned.

	``seller_suggestion`` always parks the SO in Revisar (even if the actor is a
	desk admin testing the suggestion-only surface).
	"""
	name = cstr(name or "").strip()
	if not name:
		return False
	reason = cstr(reason or "seller_amend").strip() or "seller_amend"
	# seller_suggestion must always enter Revisar (constant defined below).
	if reason == "seller_suggestion":
		ensure_creation_review_custom_fields()
		if not frappe.db.has_column("Sales Order", FIELDNAME):
			return False
		if not frappe.db.exists("Sales Order", name):
			return False
		frappe.db.set_value("Sales Order", name, FIELDNAME, STATUS_PENDING, update_modified=False)
	elif not maybe_mark_creation_review_pending("Sales Order", name, actor=actor):
		return False
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import (
			_guest_preorder_tag_fieldname,
			_sanitize_guest_tag,
			_update_guest_preorder_tag,
		)

		tag_fn = _guest_preorder_tag_fieldname()
		if not tag_fn or not frappe.db.exists("Sales Order", name):
			return True
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", name)
		frappe.flags.ignore_permissions = False
		_update_guest_preorder_tag(so, "review_reason", _sanitize_guest_tag(reason) or "seller_amend")
		frappe.db.set_value("Sales Order", name, tag_fn, getattr(so, tag_fn, None), update_modified=False)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"mark_seller_amend_for_review tag {name}")
	return True


# ── Seller suggestions (queue without applying until Revisar confirm) ─────────

SUGGESTION_KV_SCOPE = "guest_preorder.suggestion"
REASON_SELLER_SUGGESTION = "seller_suggestion"


def _normalize_suggestion_change(change) -> dict:
	"""Accept JSON string / dict; keep only known suggestion sections."""
	import json as _json

	if change is None or change == "":
		return {}
	if isinstance(change, str):
		raw = change.strip()
		if not raw or raw.lower() in ("null", "undefined", "none"):
			return {}
		try:
			change = _json.loads(raw)
		except Exception:
			frappe.throw(_("Invalid suggestion change JSON"), frappe.ValidationError)
	if not isinstance(change, dict):
		frappe.throw(_("change must be an object"), frappe.ValidationError)

	out = {}
	details = change.get("details")
	if isinstance(details, dict) and details:
		out["details"] = details
	items = change.get("items")
	if isinstance(items, list) and items:
		out["items"] = items
	if "additional_discount_amount" in change and change.get("additional_discount_amount") is not None:
		try:
			out["additional_discount_amount"] = float(change.get("additional_discount_amount") or 0)
		except (TypeError, ValueError):
			out["additional_discount_amount"] = 0.0
	logistics = change.get("logistics")
	if isinstance(logistics, dict) and logistics:
		out["logistics"] = logistics
	# Pipeline / status is intentionally NOT accepted — sellers cannot suggest status moves.
	return out


def get_pending_suggestion(preorder_name: str) -> dict | None:
	"""Return stored suggestion payload or None.

	Legacy soft-rejected rows (status=rejected) are cleared and treated as absent
	so print / both parties only see the live Sales Order body.
	"""
	name = cstr(preorder_name or "").strip()
	if not name:
		return None
	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get

	_doc, data = kv_get(SUGGESTION_KV_SCOPE, name)
	if not isinstance(data, dict):
		return None
	change = data.get("change")
	if not isinstance(change, dict) or not change:
		return None
	if cstr(data.get("status") or "pending").lower() == "rejected":
		clear_pending_suggestion(name)
		frappe.db.commit()
		return None
	return data


def clear_pending_suggestion(preorder_name: str) -> bool:
	"""Delete the suggestion KV row for a Sales Order."""
	name = cstr(preorder_name or "").strip()
	if not name:
		return False
	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get

	docname, _ = kv_get(SUGGESTION_KV_SCOPE, name)
	if not docname:
		return False
	frappe.delete_doc("Table Extra Data", docname, ignore_permissions=True)
	return True


def _suggestion_summary(change: dict) -> str:
	parts = []
	if change.get("details"):
		parts.append("details")
	if change.get("items"):
		parts.append(f"items:{len(change['items'])}")
	if "additional_discount_amount" in change:
		parts.append("discount")
	if change.get("logistics"):
		parts.append("logistics")
	return ",".join(parts) if parts else "suggestion"


def _fmt_sug_num(v) -> str:
	try:
		n = float(v or 0)
	except (TypeError, ValueError):
		return cstr(v)
	if abs(n - round(n)) < 1e-9:
		return str(int(round(n)))
	return cstr(round(n, 3))


def _human_suggestion_summary(preorder_name: str, change: dict) -> str:
	"""Readable diffs vs live SO, e.g. ``ITEM qty 1 → 2``."""
	name = cstr(preorder_name or "").strip()
	if not name or not isinstance(change, dict):
		return _suggestion_summary(change or {})
	parts = []
	live = {}
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import get_guest_preorder

		live = get_guest_preorder(name) or {}
	except Exception:
		live = {}
	items = change.get("items")
	if isinstance(items, list):
		live_by = {
			cstr(r.get("item_code") or "").strip(): r
			for r in (live.get("items") or [])
			if isinstance(r, dict) and cstr(r.get("item_code") or "").strip()
		}
		sug_codes = set()
		for row in items:
			if not isinstance(row, dict):
				continue
			code = cstr(row.get("item_code") or "").strip()
			if not code:
				continue
			sug_codes.add(code)
			cur = live_by.get(code) or {}
			label = cstr(row.get("item_name") or cur.get("item_name") or code)
			if len(label) > 42:
				label = label[:40] + "…"
			if not cur:
				parts.append(f"{label} qty {_fmt_sug_num(row.get('qty'))} (new)")
				continue
			diffs = []
			if float(cur.get("qty") or 0) != float(row.get("qty") or 0):
				diffs.append(f"qty {_fmt_sug_num(cur.get('qty'))} → {_fmt_sug_num(row.get('qty'))}")
			if float(cur.get("rate") or 0) != float(row.get("rate") or 0):
				diffs.append(f"rate {_fmt_sug_num(cur.get('rate'))} → {_fmt_sug_num(row.get('rate'))}")
			if float(cur.get("discount_percentage") or 0) != float(row.get("discount_percentage") or 0):
				diffs.append(
					f"disc {_fmt_sug_num(cur.get('discount_percentage'))}% → {_fmt_sug_num(row.get('discount_percentage'))}%"
				)
			if diffs:
				parts.append(f"{label} {' '.join(diffs)}")
		for code, cur in live_by.items():
			if code in sug_codes:
				continue
			label = cstr(cur.get("item_name") or code)
			if len(label) > 42:
				label = label[:40] + "…"
			parts.append(f"{label} (remove)")
	details = change.get("details")
	if isinstance(details, dict):
		for key, next_v in details.items():
			a = cstr(live.get(key) or "").strip()
			b = cstr(next_v or "").strip()
			if a != b:
				parts.append(f"{key}: {a or '—'} → {b or '—'}")
	if "additional_discount_amount" in change:
		prev_amt = float(live.get("additional_discount_amount") or 0)
		next_amt = float(change.get("additional_discount_amount") or 0)
		if abs(prev_amt - next_amt) > 1e-9:
			parts.append(f"discount {_fmt_sug_num(prev_amt)} → {_fmt_sug_num(next_amt)}")
	logistics = change.get("logistics")
	if isinstance(logistics, dict):
		for key in ("warehouse", "trip_name", "vehicle", "driver"):
			if key not in logistics:
				continue
			prev = cstr(live.get(key) or "").strip()
			nxt = cstr(logistics.get(key) or "").strip()
			if prev != nxt:
				parts.append(f"{key}: {prev or '—'} → {nxt or '—'}")
	return "; ".join(parts) if parts else _suggestion_summary(change)


def _assert_can_decide_seller_suggestion(sug: dict | None) -> None:
	"""Non-admins cannot approve/reject their own seller suggestion."""
	if not isinstance(sug, dict):
		return
	if cstr(sug.get("reason") or "") != REASON_SELLER_SUGGESTION:
		return
	actor = cstr(sug.get("actor") or "").strip()
	if not actor:
		return
	if is_creation_review_admin():
		return
	me = _acting_or_session_user()
	if me and actor == me:
		frappe.throw(
			_("You cannot approve or reject your own suggestion — ask an admin (Revisar)."),
			frappe.PermissionError,
		)


def _log_suggestion_activity(preorder_name: str, text: str) -> None:
	"""Write a Comment on the Sales Order so Actividad shows propose / approve / reject."""
	name = cstr(preorder_name or "").strip()
	msg = cstr(text or "").strip()
	if not name or not msg:
		return
	try:
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", name)
		so.add_comment("Comment", msg)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"suggestion activity {name}")
	finally:
		frappe.flags.ignore_permissions = False


def suggest_guest_preorder_change(preorder_name=None, change=None):
	"""Queue a seller suggestion without mutating Sales Order body fields.

	Stores the patch in Table Extra Data and parks the SO in Revisar
	(``custom_creation_review=Pending``, ``review_reason:seller_suggestion``).
	Admin confirm applies the patch; reject/delete discards it.
	"""
	name = cstr(preorder_name or "").strip()
	if not name:
		frappe.throw(_("preorder_name is required"), frappe.ValidationError)
	if not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name), frappe.DoesNotExistError)

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_is_guest_preorder_sales_order,
		_require_guest_preorder_visible,
		get_guest_preorder,
	)

	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("Not a Guest Preorder"), frappe.ValidationError)
	_require_guest_preorder_visible(so)
	if cint(so.docstatus) == 2:
		frappe.throw(_("Cannot suggest changes on an archived order"), frappe.ValidationError)

	norm = _normalize_suggestion_change(change)
	if not norm:
		frappe.throw(_("Suggestion change is empty"), frappe.ValidationError)

	actor = _acting_or_session_user()
	# Merge with any existing pending suggestion (latest keys win).
	prev = get_pending_suggestion(name) or {}
	prev_change = prev.get("change") if isinstance(prev.get("change"), dict) else {}
	merged = dict(prev_change)
	merged.update(norm)

	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_set

	human = _human_suggestion_summary(name, merged)
	payload = {
		"change": merged,
		"actor": actor,
		"reason": REASON_SELLER_SUGGESTION,
		"summary": human or _suggestion_summary(merged),
		"status": "pending",
		"modified": str(frappe.utils.now_datetime()),
	}
	kv_set(SUGGESTION_KV_SCOPE, name, payload)
	mark_seller_amend_for_review(name, actor=actor, reason=REASON_SELLER_SUGGESTION)
	_log_suggestion_activity(
		name,
		_("Seller suggestion queued ({summary}) by {actor}").format(
			summary=payload.get("summary") or "suggestion",
			actor=actor or frappe.session.user,
		),
	)
	frappe.db.commit()

	detail = get_guest_preorder(name)
	return {
		"ok": True,
		"name": name,
		"review_status": STATUS_PENDING,
		"suggestion": payload,
		"detail": detail,
	}


def apply_pending_suggestion(preorder_name: str) -> dict | None:
	"""Apply a queued seller suggestion (admin Revisar confirm). Returns applied payload or None."""
	name = cstr(preorder_name or "").strip()
	sug = get_pending_suggestion(name)
	if not sug:
		return None
	change = sug.get("change") if isinstance(sug.get("change"), dict) else {}
	if not change:
		clear_pending_suggestion(name)
		return None

	from erpnext.erpnext_integrations.ecommerce_api import api as guest_api

	frappe.flags.applying_creation_review_suggestion = True
	try:
		details = change.get("details")
		if isinstance(details, dict) and details:
			guest_api.update_guest_preorder_details(name, details)

		logistics = change.get("logistics")
		if isinstance(logistics, dict) and logistics:
			guest_api.update_guest_preorder_logistics(
				name,
				warehouse=logistics.get("warehouse"),
				trip_name=logistics.get("trip_name"),
				vehicle=logistics.get("vehicle"),
				driver=logistics.get("driver"),
				clear_trip=1 if logistics.get("clear_trip") else 0,
			)

		items = change.get("items")
		if isinstance(items, list) and items:
			disc = change.get("additional_discount_amount", 0)
			guest_api.update_guest_preorder_items(name, items, disc)
		elif "additional_discount_amount" in change and change.get("items") is None:
			# Discount-only without item list — skip (needs items for update_guest_preorder_items).
			pass
	finally:
		frappe.flags.applying_creation_review_suggestion = False

	clear_pending_suggestion(name)
	_log_suggestion_activity(
		name,
		_("Seller suggestion approved / applied ({summary})").format(
			summary=(sug.get("summary") if isinstance(sug, dict) else None) or "suggestion",
		),
	)
	return sug


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
				"review_reason": tags.get("review_reason") or None,
				"seller_ref_user": tags.get("seller_ref") or None,
				"has_suggestion": False,
				"suggestion_summary": None,
				"suggestion_actor": None,
			}
		)

	# Batch-attach pending seller suggestions for Revisar UI.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get_many

		names = [r["name"] for r in out if r.get("name")]
		sug_map = kv_get_many(SUGGESTION_KV_SCOPE, names) if names else {}
		for row in out:
			data = sug_map.get(row["name"]) or {}
			change = data.get("change") if isinstance(data, dict) else None
			status = cstr(data.get("status") or "pending").lower() if isinstance(data, dict) else ""
			if isinstance(change, dict) and change and status != "rejected":
				row["has_suggestion"] = True
				row["suggestion_summary"] = data.get("summary") or _suggestion_summary(change)
				row["suggestion_actor"] = cstr(data.get("actor") or "") or None
	except Exception:
		frappe.log_error(frappe.get_traceback(), "list_creation_reviews suggestion attach")

	return out


def confirm_creation_review(kind=None, name=None):
	"""Mark a pending creation as Confirmed (leave the Revisión queue).

	For orders: applies any queued seller suggestion first, then confirms.
	"""
	ensure_creation_review_custom_fields()
	kind = _normalize_kind(kind)
	name = cstr(name or "").strip()
	if not name:
		frappe.throw(_("name is required"), frappe.ValidationError)

	doctype = "Customer" if kind == "customer" else "Sales Order"
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(doctype, name), frappe.DoesNotExistError)

	applied = None
	if kind == "order":
		from erpnext.erpnext_integrations.ecommerce_api.api import _is_guest_preorder_sales_order

		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", name)
		frappe.flags.ignore_permissions = False
		if not _is_guest_preorder_sales_order(so):
			frappe.throw(_("Not a Guest Preorder"), frappe.ValidationError)
		_assert_can_decide_seller_suggestion(get_pending_suggestion(name))
		applied = apply_pending_suggestion(name)

	frappe.db.set_value(doctype, name, FIELDNAME, STATUS_CONFIRMED)
	frappe.db.commit()
	return {
		"ok": True,
		"kind": kind,
		"name": name,
		"review_status": STATUS_CONFIRMED,
		"suggestion_applied": bool(applied),
	}


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

	# Seller suggestion reject: keep the SO body as-is, discard the queued patch
	# (no red leftover state — printing / both parties see the live order only).
	sug = get_pending_suggestion(name)
	if (
		isinstance(sug, dict)
		and isinstance(sug.get("change"), dict)
		and sug.get("change")
		and cstr(sug.get("reason") or "") == REASON_SELLER_SUGGESTION
		and cstr(sug.get("status") or "pending").lower() != "rejected"
	):
		_assert_can_decide_seller_suggestion(sug)
		summary = sug.get("summary") or "suggestion"
		actor = sug.get("actor") or ""
		_log_suggestion_activity(
			name,
			_("Seller suggestion rejected / discarded ({summary}) — proposed by {actor}").format(
				summary=summary,
				actor=actor or "—",
			),
		)
		clear_pending_suggestion(name)
		if frappe.db.has_column("Sales Order", FIELDNAME):
			frappe.db.set_value("Sales Order", name, FIELDNAME, STATUS_CONFIRMED)
		frappe.db.commit()
		return {
			"ok": True,
			"kind": kind,
			"name": name,
			"action": "suggestion_rejected",
			"suggestion": None,
		}

	clear_pending_suggestion(name)

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
