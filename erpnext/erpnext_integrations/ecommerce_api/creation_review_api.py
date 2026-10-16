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
	# Stamp who parked it (Pedidos shows ``r:josefi`` from this tag).
	if doctype == "Sales Order":
		_stamp_so_review_actor(name, actor)
	return True


def _stamp_so_review_actor(name: str, actor=None) -> None:
	"""Write ``review_actor:<user>`` on the guest-preorder tag field."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import (
			_guest_preorder_tag_fieldname,
			_sanitize_guest_tag,
			_update_guest_preorder_tag,
		)
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username

		who = (
			cstr(actor or "").strip()
			or cstr(_acting_username() or "").strip()
			or cstr(frappe.session.user or "").strip()
		)
		if not who or who == "Guest":
			return
		tag_fn = _guest_preorder_tag_fieldname()
		if not tag_fn or not frappe.db.exists("Sales Order", name):
			return
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", name)
		frappe.flags.ignore_permissions = False
		_update_guest_preorder_tag(so, "review_actor", _sanitize_guest_tag(who)[:140])
		frappe.db.set_value("Sales Order", name, tag_fn, getattr(so, tag_fn, None), update_modified=False)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"stamp review_actor {name}")


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
		who = cstr(actor or "").strip()
		if who and who != "Guest":
			_update_guest_preorder_tag(so, "review_actor", _sanitize_guest_tag(who)[:140])
		frappe.db.set_value("Sales Order", name, tag_fn, getattr(so, tag_fn, None), update_modified=False)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"mark_seller_amend_for_review tag {name}")
	return True


# ── Seller suggestions (queue without applying until Revisar confirm) ─────────

SUGGESTION_KV_SCOPE = "guest_preorder.suggestion"
CUSTOMER_SUGGESTION_KV_SCOPE = "customer.suggestion"
# Per-actor one-shot “your request was approved” rows for the notification bell.
MY_OUTCOME_KV_SCOPE = "review.my_outcome"
REASON_SELLER_SUGGESTION = "seller_suggestion"

_CUSTOMER_SUGGESTION_FIELDS = (
	"phone",
	"mobile_no",
	"tax_id",
	"tax_category",
	"address_line1",
	"preferred_hours",
	"zone",
	"client_access_pin",
	"client_phone_e164",
	"customer_name",
	"territory",
)


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


def _normalize_customer_suggestion_change(change) -> dict:
	"""Accept JSON string / dict; keep only Customer fields sellers may suggest."""
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
	for key in _CUSTOMER_SUGGESTION_FIELDS:
		if key not in change:
			continue
		val = change.get(key)
		if val is None:
			continue
		s = cstr(val).strip()
		if s.lower() in ("null", "undefined"):
			continue
		out[key] = s
	# Alias phone → mobile_no for update_customer
	if "phone" in out and "mobile_no" not in out:
		out["mobile_no"] = out["phone"]
	return out


def _customer_suggestion_summary(change: dict) -> str:
	parts = []
	for key in (
		"customer_name",
		"phone",
		"mobile_no",
		"tax_id",
		"tax_category",
		"address_line1",
		"preferred_hours",
		"zone",
		"client_access_pin",
		"client_phone_e164",
		"territory",
	):
		if key in (change or {}):
			parts.append(key)
	return ",".join(parts) if parts else "suggestion"


def get_pending_customer_suggestion(customer_name: str) -> dict | None:
	"""Return stored Customer suggestion payload or None."""
	name = cstr(customer_name or "").strip()
	if not name:
		return None
	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get

	_doc, data = kv_get(CUSTOMER_SUGGESTION_KV_SCOPE, name)
	if not isinstance(data, dict):
		return None
	change = data.get("change")
	if not isinstance(change, dict) or not change:
		return None
	if cstr(data.get("status") or "pending").lower() == "rejected":
		clear_pending_customer_suggestion(name)
		frappe.db.commit()
		return None
	return data


def clear_pending_customer_suggestion(customer_name: str) -> bool:
	"""Delete the suggestion KV row for a Customer."""
	name = cstr(customer_name or "").strip()
	if not name:
		return False
	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get

	docname, _ = kv_get(CUSTOMER_SUGGESTION_KV_SCOPE, name)
	if not docname:
		return False
	frappe.delete_doc("Table Extra Data", docname, ignore_permissions=True)
	return True


def suggest_customer_change(customer_name=None, change=None):
	"""Queue a seller Customer patch without mutating the live Customer.

	Stores the patch in Table Extra Data and parks the Customer in Revisar
	(``custom_creation_review=Pending``). Admin confirm applies the patch.
	"""
	name = cstr(customer_name or "").strip()
	if not name:
		frappe.throw(_("customer_name is required"), frappe.ValidationError)
	if not frappe.db.exists("Customer", name):
		frappe.throw(_("Customer {0} not found").format(name), frappe.DoesNotExistError)

	norm = _normalize_customer_suggestion_change(change)
	if not norm:
		frappe.throw(_("Suggestion change is empty"), frappe.ValidationError)

	actor = _acting_or_session_user()
	prev = get_pending_customer_suggestion(name) or {}
	prev_change = prev.get("change") if isinstance(prev.get("change"), dict) else {}
	merged = dict(prev_change)
	merged.update(norm)

	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_set

	summary = _customer_suggestion_summary(merged)
	payload = {
		"change": merged,
		"actor": actor,
		"reason": REASON_SELLER_SUGGESTION,
		"summary": summary,
		"status": "pending",
		"modified": str(frappe.utils.now_datetime()),
	}
	kv_set(CUSTOMER_SUGGESTION_KV_SCOPE, name, payload)

	ensure_creation_review_custom_fields()
	if frappe.db.has_column("Customer", FIELDNAME):
		frappe.db.set_value("Customer", name, FIELDNAME, STATUS_PENDING, update_modified=False)

	frappe.db.commit()
	return {
		"ok": True,
		"name": name,
		"review_status": STATUS_PENDING,
		"suggestion": payload,
	}


def apply_pending_customer_suggestion(customer_name: str) -> dict | None:
	"""Apply a queued Customer suggestion (admin Revisar confirm)."""
	name = cstr(customer_name or "").strip()
	sug = get_pending_customer_suggestion(name)
	if not sug:
		return None
	change = sug.get("change") if isinstance(sug.get("change"), dict) else {}
	if not change:
		clear_pending_customer_suggestion(name)
		return None

	from erpnext.erpnext_integrations.ecommerce_api import api as guest_api

	frappe.flags.applying_creation_review_suggestion = True
	try:
		guest_api.update_customer(name, **change)
	finally:
		frappe.flags.applying_creation_review_suggestion = False

	clear_pending_customer_suggestion(name)
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
					"has_suggestion": False,
					"suggestion_summary": None,
					"suggestion_actor": None,
				}
			)
		# Batch-attach pending seller suggestions for Revisar UI.
		try:
			from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get_many

			names = [r["name"] for r in out if r.get("name")]
			sug_map = kv_get_many(CUSTOMER_SUGGESTION_KV_SCOPE, names) if names else {}
			for row in out:
				data = sug_map.get(row["name"]) or {}
				change = data.get("change") if isinstance(data, dict) else None
				status = cstr(data.get("status") or "pending").lower() if isinstance(data, dict) else ""
				if isinstance(change, dict) and change and status != "rejected":
					row["has_suggestion"] = True
					row["suggestion_summary"] = data.get("summary") or _customer_suggestion_summary(change)
					row["suggestion_actor"] = cstr(data.get("actor") or "") or None
		except Exception:
			frappe.log_error(frappe.get_traceback(), "list_creation_reviews customer suggestion attach")
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

	For orders/customers: applies any queued seller suggestion first, then confirms.
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
	elif kind == "customer":
		_assert_can_decide_seller_suggestion(get_pending_customer_suggestion(name))
		applied = apply_pending_customer_suggestion(name)

	frappe.db.set_value(doctype, name, FIELDNAME, STATUS_CONFIRMED)
	# Notify the suggestion actor once (bell → dismiss after seen).
	try:
		actor = ""
		summary = ""
		if isinstance(applied, dict):
			actor = cstr(applied.get("actor") or "").strip()
			summary = cstr(applied.get("summary") or "").strip()
		if not actor:
			# Creation without suggestion payload — use review_actor / owner when present.
			if kind == "order":
				from erpnext.erpnext_integrations.ecommerce_api.api import (
					_parse_remarks_tags,
					_guest_preorder_tag_text,
				)

				tags = _parse_remarks_tags(_guest_preorder_tag_text(frappe.get_doc("Sales Order", name)))
				actor = cstr(tags.get("review_actor") or tags.get("seller_ref") or "").strip()
			else:
				actor = cstr(frappe.db.get_value("Customer", name, "owner") or "").strip()
		if actor:
			_record_my_outcome(
				kind=kind,
				name=name,
				actor=actor,
				status="approved",
				summary=summary
				or (
					frappe.db.get_value("Customer", name, "customer_name")
					if kind == "customer"
					else name
				),
			)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "record my review outcome")
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

		# Seller suggestion reject: keep Customer body, discard queued patch.
		sug = get_pending_customer_suggestion(name)
		if (
			isinstance(sug, dict)
			and isinstance(sug.get("change"), dict)
			and sug.get("change")
			and cstr(sug.get("reason") or "") == REASON_SELLER_SUGGESTION
			and cstr(sug.get("status") or "pending").lower() != "rejected"
		):
			_assert_can_decide_seller_suggestion(sug)
			clear_pending_customer_suggestion(name)
			if frappe.db.has_column("Customer", FIELDNAME):
				frappe.db.set_value("Customer", name, FIELDNAME, STATUS_CONFIRMED)
			frappe.db.commit()
			return {
				"ok": True,
				"kind": kind,
				"name": name,
				"action": "suggestion_rejected",
				"suggestion": None,
			}

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


def _record_my_outcome(kind: str, name: str, actor: str, status: str, summary: str) -> None:
	"""Persist a one-shot outcome for the requester’s notification bell."""
	actor = cstr(actor or "").strip()
	name = cstr(name or "").strip()
	kind = cstr(kind or "").strip()
	if not actor or not name or not kind:
		return
	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_set

	row_key = f"{kind}:{name}:{actor}"
	title = name
	if kind == "customer":
		title = cstr(frappe.db.get_value("Customer", name, "customer_name") or name)
	elif kind == "order":
		title = cstr(frappe.db.get_value("Sales Order", name, "customer_name") or name)
	kv_set(
		MY_OUTCOME_KV_SCOPE,
		row_key,
		{
			"kind": kind,
			"name": name,
			"actor": actor,
			"status": cstr(status or "approved"),
			"summary": cstr(summary or "")[:240],
			"title": title[:140],
			"at": str(frappe.utils.now_datetime()),
		},
	)
	# Re-amber the Preventa "Órdenes pendientes" tile for the requester.
	if kind == "order" and cstr(status or "").lower() == "approved":
		try:
			from erpnext.erpnext_integrations.ecommerce_api.preventa_api import (
				clear_order_attention_seen,
			)

			clear_order_attention_seen(actor, name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "clear_order_attention_seen failed")


def _iter_suggestion_kv(scope: str, actor: str) -> list[dict]:
	"""Pending suggestion rows in ``scope`` owned by ``actor``."""
	actor = cstr(actor or "").strip()
	if not actor or not scope:
		return []
	rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": scope},
		fields=["row_key", "data_json"],
		limit_page_length=500,
		ignore_permissions=True,
	)
	import json

	out = []
	for row in rows or []:
		raw = row.get("data_json")
		try:
			data = json.loads(raw) if raw else {}
		except Exception:
			data = {}
		if not isinstance(data, dict):
			continue
		if cstr(data.get("actor") or "").strip() != actor:
			continue
		if cstr(data.get("status") or "pending").lower() == "rejected":
			continue
		change = data.get("change")
		if not isinstance(change, dict) or not change:
			continue
		out.append(
			{
				"row_key": cstr(row.get("row_key") or ""),
				"data": data,
			}
		)
	return out


@frappe.whitelist(allow_guest=True)
def list_my_review_requests():
	"""Bell inbox: my pending suggestions + one-shot approved outcomes.

	Reusable later for other operators (drivers, repositor, …) with the same shape.
	"""
	actor = _acting_or_session_user()
	if not actor or actor in ("Guest", "guest"):
		return {"pending": [], "approved": []}

	pending = []
	for item in _iter_suggestion_kv(CUSTOMER_SUGGESTION_KV_SCOPE, actor):
		name = item["row_key"]
		data = item["data"]
		title = cstr(frappe.db.get_value("Customer", name, "customer_name") or name)
		pending.append(
			{
				"id": f"customer:{name}",
				"kind": "customer",
				"name": name,
				"title": title,
				"summary": cstr(data.get("summary") or "")[:240],
				"status": "pending",
				"modified": cstr(data.get("modified") or ""),
			}
		)
	for item in _iter_suggestion_kv(SUGGESTION_KV_SCOPE, actor):
		name = item["row_key"]
		data = item["data"]
		title = cstr(frappe.db.get_value("Sales Order", name, "customer_name") or name)
		pending.append(
			{
				"id": f"order:{name}",
				"kind": "order",
				"name": name,
				"title": title,
				"summary": cstr(data.get("summary") or "")[:240],
				"status": "pending",
				"modified": cstr(data.get("modified") or ""),
			}
		)

	approved = []
	outcome_rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": MY_OUTCOME_KV_SCOPE},
		fields=["name", "row_key", "data_json"],
		limit_page_length=200,
		ignore_permissions=True,
	)
	import json

	for row in outcome_rows or []:
		raw = row.get("data_json")
		try:
			data = json.loads(raw) if raw else {}
		except Exception:
			data = {}
		if not isinstance(data, dict):
			continue
		if cstr(data.get("actor") or "").strip() != actor:
			continue
		if cstr(data.get("status") or "").lower() != "approved":
			continue
		kind = cstr(data.get("kind") or "")
		name = cstr(data.get("name") or "")
		if not kind or not name:
			continue
		approved.append(
			{
				"id": f"{kind}:{name}",
				"kind": kind,
				"name": name,
				"title": cstr(data.get("title") or name),
				"summary": cstr(data.get("summary") or "")[:240],
				"status": "approved",
				"modified": cstr(data.get("at") or ""),
				"outcome_key": cstr(row.get("row_key") or ""),
			}
		)

	pending.sort(key=lambda r: r.get("modified") or "", reverse=True)
	approved.sort(key=lambda r: r.get("modified") or "", reverse=True)
	return {"pending": pending, "approved": approved}


@frappe.whitelist(allow_guest=True)
def cancel_my_review_request(kind=None, name=None):
	"""Requester cancels their own pending suggestion (does not delete the live doc)."""
	kind = _normalize_kind(kind)
	name = cstr(name or "").strip()
	if not name:
		frappe.throw(_("name is required"), frappe.ValidationError)
	actor = _acting_or_session_user()

	if kind == "customer":
		sug = get_pending_customer_suggestion(name)
		if not sug:
			frappe.throw(_("No pending suggestion"), frappe.ValidationError)
		if cstr(sug.get("actor") or "").strip() != actor and not is_creation_review_admin(actor):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		clear_pending_customer_suggestion(name)
		# If parked only for this suggestion, clear Pending flag.
		if frappe.db.has_column("Customer", FIELDNAME):
			if frappe.db.get_value("Customer", name, FIELDNAME) == STATUS_PENDING:
				frappe.db.set_value("Customer", name, FIELDNAME, "", update_modified=False)
	else:
		sug = get_pending_suggestion(name)
		if not sug:
			frappe.throw(_("No pending suggestion"), frappe.ValidationError)
		if cstr(sug.get("actor") or "").strip() != actor and not is_creation_review_admin(actor):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		clear_pending_suggestion(name)
		if frappe.db.has_column("Sales Order", FIELDNAME):
			if frappe.db.get_value("Sales Order", name, FIELDNAME) == STATUS_PENDING:
				# Keep Pending if the SO itself was created as a review draft.
				from erpnext.erpnext_integrations.ecommerce_api.api import (
					_parse_remarks_tags,
					_guest_preorder_tag_text,
				)

				tags = _parse_remarks_tags(
					_guest_preorder_tag_text(frappe.get_doc("Sales Order", name))
				)
				if cstr(tags.get("review_reason") or "") == REASON_SELLER_SUGGESTION:
					frappe.db.set_value("Sales Order", name, FIELDNAME, "", update_modified=False)

	frappe.db.commit()
	return {"ok": True, "kind": kind, "name": name, "action": "cancelled"}


@frappe.whitelist(allow_guest=True)
def dismiss_my_review_outcome(kind=None, name=None):
	"""Dismiss a one-shot approved notice after the requester has seen it."""
	kind = _normalize_kind(kind)
	name = cstr(name or "").strip()
	actor = _acting_or_session_user()
	if not name or not actor:
		frappe.throw(_("name is required"), frappe.ValidationError)
	row_key = f"{kind}:{name}:{actor}"
	docname = frappe.db.get_value(
		"Table Extra Data", {"scope": MY_OUTCOME_KV_SCOPE, "row_key": row_key}, "name"
	)
	if docname:
		frappe.delete_doc("Table Extra Data", docname, ignore_permissions=True)
		frappe.db.commit()
	return {"ok": True, "kind": kind, "name": name, "action": "dismissed"}
