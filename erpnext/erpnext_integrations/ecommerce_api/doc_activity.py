"""Document Activity timeline + comments for Tables detail panels (Frappe Comment / Version)."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, format_datetime, now_datetime, strip_html

from erpnext.erpnext_integrations.ecommerce_api.company_context import acting_user


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(raw) -> str:
	if raw is None:
		return ""
	text = str(raw)
	if "<" in text and ">" in text:
		try:
			text = strip_html(text)
		except Exception:
			text = _HTML_TAG_RE.sub("", text)
	return (text or "").strip()


def _user_label(user: str | None) -> str:
	user = (user or "").strip()
	if not user or user == "Guest":
		return "—"
	full = frappe.db.get_value("User", user, "full_name")
	return (full or user).strip() or user


def _initials(label: str) -> str:
	parts = [p for p in (label or "").split() if p]
	if not parts:
		return "?"
	if len(parts) == 1:
		return parts[0][:2].upper()
	return (parts[0][0] + parts[-1][0]).upper()


def resolve_activity_actor(user: str | None = None) -> str:
	"""Prefer X-ERP-Acting-User over the API-key session (often Administrator)."""
	actor = cstr(user or acting_user() or "").strip()
	if not actor or actor == "Guest":
		actor = cstr(frappe.session.user or "").strip()
	return actor or "Administrator"


@contextmanager
def acting_session(user: str | None = None):
	"""Attribute Frappe Version/Comment inserts to the acting seller, not the API key."""
	actor = resolve_activity_actor(user)
	prev = cstr(frappe.session.user or "").strip()
	switched = False
	if actor and actor != prev and frappe.db.exists("User", actor):
		frappe.session.user = actor
		switched = True
	try:
		yield actor
	finally:
		if switched:
			frappe.session.user = prev


def stamp_comment_actor(comment_name: str, user: str | None = None) -> None:
	"""Rewrite Comment owner / comment_by to the acting user after add_comment."""
	comment_name = cstr(comment_name or "").strip()
	if not comment_name or not frappe.db.exists("Comment", comment_name):
		return
	actor = resolve_activity_actor(user)
	if not actor or actor == "Guest":
		return
	who = _user_label(actor)
	values = {"comment_by": who, "comment_email": actor}
	if frappe.db.exists("User", actor):
		values["owner"] = actor
		values["modified_by"] = actor
	frappe.db.set_value("Comment", comment_name, values, update_modified=False)


def stamp_recent_versions_actor(
	doctype: str,
	name: str,
	user: str | None = None,
	*,
	seconds: int = 15,
) -> int:
	"""Re-attribute Version rows just written under the API-key session."""
	doctype = cstr(doctype or "").strip()
	name = cstr(name or "").strip()
	actor = resolve_activity_actor(user)
	session = cstr(frappe.session.user or "").strip()
	if not doctype or not name or not actor or actor == session:
		return 0
	if not frappe.db.exists("User", actor):
		return 0
	since = add_to_date(now_datetime(), seconds=-max(1, cint(seconds) or 15))
	# Only rewrite rows still owned by the API-key / Administrator session.
	owners = {session, "Administrator"} if session else {"Administrator"}
	owners = [o for o in owners if o]
	if not owners:
		return 0
	frappe.db.sql(
		"""
		UPDATE `tabVersion`
		SET owner=%s, modified_by=%s
		WHERE ref_doctype=%s AND docname=%s
		  AND owner IN %s
		  AND creation >= %s
		""",
		(actor, actor, doctype, name, tuple(owners), since),
	)
	return 1


def log_doc_activity_info(
	doctype: str,
	name: str,
	message: str,
	*,
	user: str | None = None,
	comment_type: str = "Info",
) -> str | None:
	"""Append an Activity Info line attributed to the acting user.

	``message`` is the verb phrase only (UI prefixes ``who``), e.g.
	``requested this order (Consulta → Orden)``.
	"""
	doctype = cstr(doctype or "").strip()
	name = cstr(name or "").strip()
	text = _clean_text(message)
	if not doctype or not name or not text:
		return None
	actor = resolve_activity_actor(user)
	try:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(doctype, name)
		ctype = cstr(comment_type or "Info").strip() or "Info"
		comment = doc.add_comment(ctype, text=text)
		if comment:
			stamp_comment_actor(comment.name, actor)
			return comment.name
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"log_doc_activity_info {doctype} {name}")
		return None
	finally:
		frappe.flags.ignore_permissions = False
	return None


def log_activity_for_delivery_note(delivery_note, message, *, user=None) -> int:
	"""Write the same Activity line on every Sales Order linked to a Delivery Note."""
	dn = cstr(delivery_note or "").strip()
	text = _clean_text(message)
	if not dn or not text or dn.lower() in ("null", "undefined", "none"):
		return 0
	so_names = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": dn, "against_sales_order": ["is", "set"]},
		pluck="against_sales_order",
		ignore_permissions=True,
	)
	seen = []
	for so_name in so_names or []:
		so_name = cstr(so_name or "").strip()
		if not so_name or so_name in seen:
			continue
		seen.append(so_name)
		log_doc_activity_info("Sales Order", so_name, text, user=user)
	return len(seen)


def _version_actor_from_data(data) -> str | None:
	if not isinstance(data, dict):
		return None
	for key in ("actor", "acting_user", "by"):
		val = cstr(data.get(key) or "").strip()
		if val and val != "Guest":
			return val
	return None


def _parse_version_summary(data_raw) -> str:
	try:
		data = json.loads(data_raw) if isinstance(data_raw, str) else (data_raw or {})
	except Exception:
		return _("edited this")
	changed = data.get("changed") or []
	# Prefer human pipeline wording when status is the only meaningful change.
	status_only = [
		ch
		for ch in changed
		if isinstance(ch, (list, tuple)) and len(ch) >= 3 and cstr(ch[0]) == "status"
	]
	noise = {
		cstr(ch[0])
		for ch in changed
		if isinstance(ch, (list, tuple)) and len(ch) >= 1
	} - {"status", "status_via", "docstatus"}
	if status_only and not noise:
		_field, prev, new = status_only[0][0], status_only[0][1], status_only[0][2]
		prev_s = cstr(prev or "").strip() or "—"
		new_s = cstr(new or "").strip() or "—"
		event = cstr(data.get("event") or "").strip().lower()
		if event == "requested" or (
			new_s == "Orden" and prev_s in ("Consulta", "Draft", "—", "")
		):
			return _("requested this order ({0} → {1})").format(prev_s, new_s)
		if event == "approved":
			return _("approved this order ({0} → {1})").format(prev_s, new_s)
		return _("changed status: {0} → {1}").format(prev_s, new_s)
	parts = []
	for ch in changed[:8]:
		if not isinstance(ch, (list, tuple)) or len(ch) < 3:
			continue
		field, prev, new = ch[0], ch[1], ch[2]
		if field in ("status_via", "weighed_by", "weighed_via"):
			continue
		prev_s = "null" if prev is None or prev == "" else str(prev)
		new_s = "null" if new is None or new == "" else str(new)
		if len(prev_s) > 40:
			prev_s = prev_s[:37] + "…"
		if len(new_s) > 40:
			new_s = new_s[:37] + "…"
		if field == "status":
			parts.append(f"status: {prev_s} → {new_s}")
		else:
			parts.append(f"{field}: {prev_s} → {new_s}")
	if parts:
		if len(parts) == 1 and parts[0].startswith("status:"):
			return _("changed {0}").format(parts[0])
		return _("changed {0}").format("; ".join(parts))
	if data.get("added") or data.get("removed") or data.get("row_changed"):
		return _("edited this")
	return _("edited this")


def _resolve_ref(doctype, name) -> tuple[str, str]:
	doctype = (doctype or "").strip() if isinstance(doctype, str) else ""
	name = (name or "").strip() if isinstance(name, str) else ""
	if not doctype or not name:
		frappe.throw(_("doctype and name are required"))
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Unknown DocType: {0}").format(doctype))
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(doctype, name))
	return doctype, name


@frappe.whitelist(allow_guest=True)
def get_doc_activity(doctype=None, name=None, limit=50):
	"""
	Unified Activity feed for a document (comments, versions, info/workflow labels).
	Mirrors the Frappe form Activity timeline at a high level.
	"""
	doctype, name = _resolve_ref(doctype, name)
	limit = max(1, min(200, cint(limit) or 50))

	items: list[dict] = []

	comments = frappe.get_all(
		"Comment",
		filters={"reference_doctype": doctype, "reference_name": name},
		fields=[
			"name",
			"creation",
			"content",
			"owner",
			"comment_type",
			"comment_by",
			"comment_email",
		],
		order_by="creation desc",
		limit=limit,
		ignore_permissions=True,
	)
	for c in comments:
		ctype = (c.comment_type or "Comment").strip()
		owner = c.owner or c.comment_email or ""
		who = (c.comment_by or "").strip() or _user_label(owner)
		content = _clean_text(c.content)
		if ctype == "Comment":
			kind = "comment"
			summary = content
		elif ctype in ("Info", "Edit", "Label", "Workflow"):
			kind = "info"
			summary = content or ctype
		elif ctype in ("Assigned", "Assignment Completed"):
			kind = "assignment"
			summary = content or ctype
		elif ctype in ("Attachment", "Attachment Removed"):
			kind = "attachment"
			summary = content or ctype
		elif ctype in ("Shared", "Unshared"):
			kind = "share"
			summary = content or ctype
		else:
			kind = "info"
			summary = content or ctype
		items.append(
			{
				"id": f"comment:{c.name}",
				"kind": kind,
				"comment_type": ctype,
				"who": who,
				"who_user": owner,
				"initials": _initials(who),
				"summary": summary,
				"content": content if kind == "comment" else "",
				"creation": str(c.creation) if c.creation else "",
				"creation_fmt": format_datetime(c.creation) if c.creation else "",
			}
		)

	versions = frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": name},
		fields=["name", "owner", "creation", "data"],
		order_by="creation desc",
		limit=limit,
		ignore_permissions=True,
	)
	_ARMADO_AUDIT_NOISE = {"weighed_by", "weighed_via"}
	for v in versions:
		try:
			vdata = json.loads(v.data) if isinstance(v.data, str) else (v.data or {})
		except Exception:
			vdata = {}
		# Requested / approved are narrated by Info comments — skip duplicate Version lines.
		event = cstr(vdata.get("event") or "").strip().lower()
		if event in ("requested", "approved"):
			continue
		# Hide armado operator Version spam (weighed_by / weighed_via only).
		changed = vdata.get("changed") or []
		changed_fields = {
			cstr(ch[0])
			for ch in changed
			if isinstance(ch, (list, tuple)) and len(ch) >= 1
		}
		if changed_fields and changed_fields <= _ARMADO_AUDIT_NOISE:
			continue
		actor = _version_actor_from_data(vdata) or (v.owner or "")
		who = _user_label(actor)
		items.append(
			{
				"id": f"version:{v.name}",
				"kind": "version",
				"comment_type": "Version",
				"who": who,
				"who_user": actor,
				"initials": _initials(who),
				"summary": _parse_version_summary(v.data),
				"content": "",
				"creation": str(v.creation) if v.creation else "",
				"creation_fmt": format_datetime(v.creation) if v.creation else "",
			}
		)

	items.sort(key=lambda r: r.get("creation") or "", reverse=True)
	items = items[:limit]

	return {
		"doctype": doctype,
		"name": name,
		"items": items,
		"user": acting_user(),
		"user_label": _user_label(acting_user()),
		"user_initials": _initials(_user_label(acting_user())),
	}


@frappe.whitelist(allow_guest=True)
def add_doc_comment(doctype=None, name=None, content=None):
	"""Insert a Frappe Comment on a document (same as form Activity → comment)."""
	doctype, name = _resolve_ref(doctype, name)
	text = _clean_text(content)
	if not text:
		frappe.throw(_("Comment content is required"))

	user = acting_user()
	who = _user_label(user)

	# Prefer Document.add_comment when the doc loads; fall back to Comment insert.
	try:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(doctype, name)
		comment = doc.add_comment("Comment", text=text)
		# Stamp acting user when API-key session is Administrator / Guest.
		if comment and user and user not in ("Guest",):
			frappe.db.set_value(
				"Comment",
				comment.name,
				{
					"owner": user if frappe.db.exists("User", user) else comment.owner,
					"comment_email": user,
					"comment_by": who,
				},
				update_modified=False,
			)
	finally:
		frappe.flags.ignore_permissions = False

	frappe.db.commit()

	return {
		"ok": True,
		"comment": {
			"id": f"comment:{comment.name}",
			"kind": "comment",
			"comment_type": "Comment",
			"who": who,
			"who_user": user,
			"initials": _initials(who),
			"summary": text,
			"content": text,
			"creation": str(comment.creation) if comment.creation else "",
			"creation_fmt": format_datetime(comment.creation) if comment.creation else "",
		},
	}
