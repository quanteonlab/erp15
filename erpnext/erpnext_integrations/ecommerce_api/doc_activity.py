"""Document Activity timeline + comments for Tables detail panels (Frappe Comment / Version)."""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, format_datetime, strip_html

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


def _parse_version_summary(data_raw) -> str:
	try:
		data = json.loads(data_raw) if isinstance(data_raw, str) else (data_raw or {})
	except Exception:
		return _("edited this")
	changed = data.get("changed") or []
	parts = []
	for ch in changed[:8]:
		if not isinstance(ch, (list, tuple)) or len(ch) < 3:
			continue
		field, prev, new = ch[0], ch[1], ch[2]
		prev_s = "null" if prev is None or prev == "" else str(prev)
		new_s = "null" if new is None or new == "" else str(new)
		if len(prev_s) > 40:
			prev_s = prev_s[:37] + "…"
		if len(new_s) > 40:
			new_s = new_s[:37] + "…"
		parts.append(f"{field}: {prev_s} → {new_s}")
	if parts:
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
	for v in versions:
		who = _user_label(v.owner)
		items.append(
			{
				"id": f"version:{v.name}",
				"kind": "version",
				"comment_type": "Version",
				"who": who,
				"who_user": v.owner or "",
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
