"""Universal tag system: global tag pool + per-document relationships + audit events."""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

TAG_PRINTED = "PRINTED"
TAG_DOWNLOADED = "DOWNLOADED"
TAG_SRV_FACTURA_A = "SRV-FACTURA_A"
TAG_SRV_LATE = "SRV_LATE"
LAST_EDITOR_PREFIX = "L_"
SYSTEM_SO_TAGS = frozenset({TAG_PRINTED, TAG_DOWNLOADED, TAG_SRV_FACTURA_A, TAG_SRV_LATE})


def _normalize_tag_name(name: str) -> str:
	return " ".join((name or "").strip().split())


def _parse_tags(raw) -> list[str]:
	if raw is None:
		return []
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except Exception:
			raw = [t.strip() for t in raw.split(",") if t.strip()]
	if not isinstance(raw, list):
		return []
	out: list[str] = []
	seen: set[str] = set()
	for item in raw:
		name = _normalize_tag_name(str(item or ""))
		if not name:
			continue
		key = name.casefold()
		if key in seen:
			continue
		seen.add(key)
		out.append(name)
	return sorted(out, key=lambda s: s.casefold())


def user_tag_localpart(user=None) -> str:
	"""Sanitize a Frappe user / email / label into an L_* code suffix."""
	raw = cstr(user if user is not None else (frappe.session.user if frappe.session.user else "Guest")).strip()
	if not raw or raw.lower() in ("null", "none", "undefined"):
		raw = "Guest"
	if "@" in raw:
		raw = raw.split("@", 1)[0]
	code = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
	return code or "Guest"


def last_editor_tag(user=None) -> str:
	return f"{LAST_EDITOR_PREFIX}{user_tag_localpart(user)}"


def _get_or_create_tag(tag_name: str) -> str:
	tag_name = _normalize_tag_name(tag_name)
	if not tag_name:
		frappe.throw(_("Tag name is required"))
	existing = frappe.db.get_value("Ecommerce Tag", {"tag_name": tag_name}, "name")
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": "Ecommerce Tag", "tag_name": tag_name})
	doc.insert(ignore_permissions=True)
	return doc.name


def _log_tag_event(
	tag_id: str,
	tag_name: str,
	reference_doctype: str,
	reference_name: str,
	*,
	event_type: str = "added",
) -> None:
	event_type = "removed" if cstr(event_type).strip().lower() == "removed" else "added"
	payload = {
		"doctype": "Ecommerce Tag Event",
		"tag": tag_id,
		"tag_name": tag_name,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"added_on": now_datetime(),
		"added_by": frappe.session.user if frappe.session.user else None,
	}
	if frappe.db.has_column("Ecommerce Tag Event", "event_type"):
		payload["event_type"] = event_type
	frappe.get_doc(payload).insert(ignore_permissions=True)


def _current_relationships(reference_doctype: str, reference_name: str) -> list[dict]:
	return frappe.get_all(
		"Ecommerce Tag Relationship",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=["name", "tag"],
		ignore_permissions=True,
	)


def _tag_names_by_ids(tag_ids: list[str]) -> dict[str, str]:
	if not tag_ids:
		return {}
	rows = frappe.get_all(
		"Ecommerce Tag",
		filters={"name": ["in", tag_ids]},
		fields=["name", "tag_name"],
		ignore_permissions=True,
	)
	return {r.name: r.tag_name for r in rows}


def get_tags_list(reference_doctype: str, reference_name: str) -> list[str]:
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		return []
	return tags_map_for_docs(reference_doctype, [reference_name]).get(reference_name, [])


def tags_map_for_docs(reference_doctype: str, reference_names: list[str]) -> dict[str, list[str]]:
	"""Batch-load tag names keyed by reference_name."""
	reference_doctype = (reference_doctype or "").strip()
	names = [str(n).strip() for n in (reference_names or []) if str(n).strip()]
	if not reference_doctype or not names:
		return {}

	rows = frappe.get_all(
		"Ecommerce Tag Relationship",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": ["in", names],
		},
		fields=["reference_name", "tag"],
		ignore_permissions=True,
	)
	tag_ids = sorted({r.tag for r in rows if r.tag})
	tag_names = _tag_names_by_ids(tag_ids)

	out: dict[str, list[str]] = {n: [] for n in names}
	for row in rows:
		label = tag_names.get(row.tag)
		if not label:
			continue
		out.setdefault(row.reference_name, []).append(label)
	for key in out:
		out[key] = sorted(set(out[key]), key=lambda s: s.casefold())
	return out


def migrate_item_user_tags(item_code: str) -> list[str]:
	"""Import legacy Item._user_tags into the relationship table once."""
	item_code = (item_code or "").strip()
	if not item_code:
		return []
	raw = (frappe.db.get_value("Item", item_code, "_user_tags") or "").strip()
	if not raw:
		return []
	legacy = [t.strip() for t in raw.split(",") if t.strip()]
	if not legacy:
		return []
	set_tags_for_doc("Item", item_code, tags=legacy, commit=False)
	frappe.db.set_value("Item", item_code, "_user_tags", None)
	return _parse_tags(legacy)


def set_tags_for_doc(
	reference_doctype,
	reference_name,
	tags=None,
	*,
	commit: bool = True,
) -> dict:
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))

	desired = _parse_tags(tags)
	current = _current_relationships(reference_doctype, reference_name)
	current_tag_ids = [r.tag for r in current if r.tag]
	current_names = _tag_names_by_ids(current_tag_ids)
	current_set = {name.casefold(): (tid, name) for tid, name in current_names.items()}

	desired_by_key = {name.casefold(): name for name in desired}
	to_add = [name for key, name in desired_by_key.items() if key not in current_set]
	to_remove = [
		row
		for row in current
		if row.tag and current_names.get(row.tag, "").casefold() not in desired_by_key
	]

	for row in to_remove:
		label = current_names.get(row.tag) or row.tag
		frappe.delete_doc("Ecommerce Tag Relationship", row.name, ignore_permissions=True)
		_log_tag_event(row.tag, label, reference_doctype, reference_name, event_type="removed")

	for tag_name in to_add:
		tag_id = _get_or_create_tag(tag_name)
		existing = frappe.db.get_value(
			"Ecommerce Tag Relationship",
			{
				"tag": tag_id,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
			},
			"name",
		)
		if existing:
			continue
		frappe.get_doc(
			{
				"doctype": "Ecommerce Tag Relationship",
				"tag": tag_id,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
			}
		).insert(ignore_permissions=True)
		_log_tag_event(tag_id, tag_name, reference_doctype, reference_name, event_type="added")

	if commit:
		frappe.db.commit()
	return {"ok": True, "tags": desired}


def add_tags_for_doc(
	reference_doctype,
	reference_name,
	tags=None,
	*,
	commit: bool = True,
) -> dict:
	"""Union-merge tags onto a document without dropping unrelated tags."""
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))
	extra = _parse_tags(tags)
	if not extra:
		return {"ok": True, "tags": get_tags_list(reference_doctype, reference_name)}
	current = get_tags_list(reference_doctype, reference_name)
	merged = sorted(set(current) | set(extra), key=lambda s: s.casefold())
	return set_tags_for_doc(reference_doctype, reference_name, tags=merged, commit=commit)


def remove_tags_for_doc(
	reference_doctype,
	reference_name,
	tags=None,
	*,
	commit: bool = True,
) -> dict:
	"""Remove specific tags; leave all others intact."""
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))
	drop = {t.casefold() for t in _parse_tags(tags)}
	if not drop:
		return {"ok": True, "tags": get_tags_list(reference_doctype, reference_name)}
	current = get_tags_list(reference_doctype, reference_name)
	kept = [t for t in current if t.casefold() not in drop]
	return set_tags_for_doc(reference_doctype, reference_name, tags=kept, commit=commit)


def replace_prefix_tag(
	reference_doctype,
	reference_name,
	*,
	prefix: str,
	new_tag: str | None,
	commit: bool = True,
) -> dict:
	"""Drop every tag starting with ``prefix`` and optionally add ``new_tag``."""
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	prefix = cstr(prefix or "")
	if not reference_doctype or not reference_name or not prefix:
		frappe.throw(_("reference_doctype, reference_name and prefix are required"))
	current = get_tags_list(reference_doctype, reference_name)
	kept = [t for t in current if not t.startswith(prefix)]
	new_tag = _normalize_tag_name(cstr(new_tag or ""))
	if new_tag:
		kept.append(new_tag)
	return set_tags_for_doc(reference_doctype, reference_name, tags=kept, commit=commit)


def touch_sales_order_last_editor(
	so_name,
	user=None,
	*,
	commit: bool = False,
) -> dict | None:
	"""Replace the SO's single L_* tag with the acting user code."""
	so_name = cstr(so_name or "").strip()
	if not so_name or so_name.lower() in ("null", "none", "undefined"):
		return None
	if not frappe.db.exists("Sales Order", so_name):
		return None
	tag = last_editor_tag(user)
	return replace_prefix_tag(
		"Sales Order",
		so_name,
		prefix=LAST_EDITOR_PREFIX,
		new_tag=tag,
		commit=commit,
	)


def safe_touch_sales_order_last_editor(so_name, user=None, *, commit: bool = False) -> None:
	try:
		touch_sales_order_last_editor(so_name, user=user, commit=commit)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "touch_sales_order_last_editor")


def sync_sales_order_service_tags(
	so_name,
	*,
	requires_factura_a=None,
	cliente_debe=None,
	commit: bool = False,
) -> dict | None:
	"""Mirror Factura A / late-payment flags onto SRV-* tags."""
	so_name = cstr(so_name or "").strip()
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return None

	current = get_tags_list("Sales Order", so_name)
	desired = [t for t in current if t not in (TAG_SRV_FACTURA_A, TAG_SRV_LATE)]

	if requires_factura_a is None:
		if frappe.db.has_column("Sales Order", "custom_requires_factura_a"):
			requires_factura_a = cint(
				frappe.db.get_value("Sales Order", so_name, "custom_requires_factura_a") or 0
			)
		else:
			requires_factura_a = 0
	else:
		requires_factura_a = 1 if requires_factura_a else 0

	if cliente_debe is None:
		cliente_debe = _so_has_cliente_debe(so_name)
	else:
		cliente_debe = 1 if cliente_debe else 0

	if requires_factura_a:
		desired.append(TAG_SRV_FACTURA_A)
	if cliente_debe:
		desired.append(TAG_SRV_LATE)

	return set_tags_for_doc("Sales Order", so_name, tags=desired, commit=commit)


def safe_sync_sales_order_service_tags(so_name, **kwargs) -> None:
	try:
		sync_sales_order_service_tags(so_name, **kwargs)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "sync_sales_order_service_tags")


def _so_has_cliente_debe(so_name: str) -> bool:
	"""True when any Delivery Stop linked via DN is flagged cliente_debe."""
	if not frappe.db.has_column("Delivery Stop", "custom_cliente_debe"):
		return False
	dns = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": so_name},
		pluck="parent",
		ignore_permissions=True,
	)
	dns = list({d for d in dns if d})
	if not dns:
		return False
	return bool(
		frappe.db.exists(
			"Delivery Stop",
			{"delivery_note": ["in", dns], "custom_cliente_debe": 1},
		)
	)


def mark_sales_orders_print_action(
	names=None,
	action=None,
	*,
	commit: bool = True,
) -> dict:
	"""Stamp PRINTED or DOWNLOADED (+ last editor) on one or more Sales Orders."""
	action_raw = cstr(action or "").strip().lower()
	if action_raw in ("print", "printed"):
		tag = TAG_PRINTED
	elif action_raw in ("pdf", "download", "downloaded"):
		tag = TAG_DOWNLOADED
	else:
		frappe.throw(_("action must be printed or downloaded"))

	if isinstance(names, str):
		try:
			parsed = json.loads(names)
			names = parsed if isinstance(parsed, list) else [names]
		except Exception:
			names = [n.strip() for n in names.split(",") if n.strip()]

	order_names = [cstr(n).strip() for n in (names or []) if cstr(n).strip()]
	order_names = [n for n in order_names if n.lower() not in ("null", "none", "undefined")]
	if not order_names:
		frappe.throw(_("At least one Sales Order name is required"))

	updated = []
	for name in order_names:
		if not frappe.db.exists("Sales Order", name):
			continue
		add_tags_for_doc("Sales Order", name, tags=[tag], commit=False)
		touch_sales_order_last_editor(name, commit=False)
		updated.append(name)

	if commit:
		frappe.db.commit()
	return {"ok": True, "action": "printed" if tag == TAG_PRINTED else "downloaded", "names": updated}


@frappe.whitelist()
def search_tags(query=""):
	q = _normalize_tag_name(query)
	filters = {}
	if q:
		filters["tag_name"] = ["like", f"%{q}%"]
	rows = frappe.get_all(
		"Ecommerce Tag",
		filters=filters,
		fields=["name", "tag_name"],
		order_by="tag_name asc",
		limit_page_length=30,
		ignore_permissions=True,
	)
	return {"tags": rows}


@frappe.whitelist()
def get_tags_for_doc(reference_doctype, reference_name):
	reference_doctype = (reference_doctype or "").strip()
	reference_name = (reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))

	tags = tags_map_for_docs(reference_doctype, [reference_name]).get(reference_name, [])
	if not tags and reference_doctype == "Item":
		tags = migrate_item_user_tags(reference_name)
		if tags:
			frappe.db.commit()
	return {"reference_doctype": reference_doctype, "reference_name": reference_name, "tags": tags}


@frappe.whitelist()
def set_doc_tags(reference_doctype, reference_name, tags=None):
	return set_tags_for_doc(reference_doctype, reference_name, tags=tags, commit=True)


@frappe.whitelist()
def mark_sales_orders_print_action_api(names=None, action=None):
	"""Whitelisted print/download stamp for OrderPrintModal."""
	return mark_sales_orders_print_action(names=names, action=action, commit=True)


@frappe.whitelist()
def list_tag_events(reference_doctype=None, reference_name=None, limit=50):
	"""Return add/remove historial for a document (newest first)."""
	reference_doctype = cstr(reference_doctype or "").strip()
	reference_name = cstr(reference_name or "").strip()
	if not reference_doctype or not reference_name:
		frappe.throw(_("reference_doctype and reference_name are required"))
	try:
		limit = max(1, min(200, cint(limit) or 50))
	except Exception:
		limit = 50

	fields = ["name", "tag", "tag_name", "reference_doctype", "reference_name", "added_on", "added_by"]
	if frappe.db.has_column("Ecommerce Tag Event", "event_type"):
		fields.append("event_type")

	rows = frappe.get_all(
		"Ecommerce Tag Event",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=fields,
		order_by="added_on desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	for row in rows:
		if "event_type" not in row or not row.get("event_type"):
			row["event_type"] = "added"
	return {"events": rows, "reference_doctype": reference_doctype, "reference_name": reference_name}
