"""Global screen notes with block-level locking for concurrent edits."""

from __future__ import annotations

import json
import secrets
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

LOCK_SECONDS = 90
ALLOWED_TYPES = ("note", "bullets", "checklist")


def _parse_blocks(raw) -> list[dict]:
	if raw is None or raw == "":
		return []
	if isinstance(raw, list):
		data = raw
	else:
		try:
			data = json.loads(raw)
		except Exception:
			return []
	if not isinstance(data, list):
		return []
	out = []
	for b in data:
		if isinstance(b, dict) and b.get("id"):
			out.append(b)
	return out


def _dump_blocks(blocks: list[dict]) -> str:
	return json.dumps(blocks, ensure_ascii=False, default=str)


def _get_board(scope: str, create: bool = True):
	scope = (scope or "").strip()
	if not scope:
		frappe.throw(_("scope is required"))
	if frappe.db.exists("Screen Note Board", scope):
		return frappe.get_doc("Screen Note Board", scope)
	if not create:
		return None
	doc = frappe.get_doc(
		{"doctype": "Screen Note Board", "scope": scope, "blocks_json": "[]"}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _lock_expired(block: dict) -> bool:
	until = block.get("locked_until")
	if not until:
		return True
	try:
		return get_datetime(until) <= now_datetime()
	except Exception:
		return True


def _can_edit(block: dict, user: str) -> bool:
	if not block.get("locked_by") or _lock_expired(block):
		return True
	return block.get("locked_by") == user


def _public_block(block: dict, user: str) -> dict:
	"""Strip nothing; add helper flags for UI."""
	locked_by = block.get("locked_by")
	expired = _lock_expired(block)
	return {
		**block,
		"locked_by": None if expired else locked_by,
		"locked_until": None if expired else block.get("locked_until"),
		"locked_by_other": bool(locked_by and not expired and locked_by != user),
		"editable_by_me": _can_edit(block, user),
	}


@frappe.whitelist()
def get_screen_notes(scope):
	user = frappe.session.user
	doc = _get_board(scope, create=True)
	blocks = [_public_block(b, user) for b in _parse_blocks(doc.blocks_json)]
	return {
		"scope": scope,
		"blocks": blocks,
		"modified": str(doc.modified) if doc.modified else None,
		"user": user,
	}


@frappe.whitelist()
def add_note_block(scope, block_type="note"):
	user = frappe.session.user
	btype = (block_type or "note").strip().lower()
	if btype not in ALLOWED_TYPES:
		btype = "note"
	doc = _get_board(scope, create=True)
	blocks = _parse_blocks(doc.blocks_json)
	now = now_datetime()
	block = {
		"id": secrets.token_hex(8),
		"type": btype,
		"content": "" if btype == "note" else None,
		"items": [] if btype != "note" else None,
		"locked_by": user,
		"locked_until": str(now + timedelta(seconds=LOCK_SECONDS)),
		"modified": str(now),
		"modified_by": user,
	}
	if btype == "checklist":
		block["items"] = [{"text": "", "checked": False}]
	elif btype == "bullets":
		block["items"] = [""]
	blocks.append(block)
	doc.blocks_json = _dump_blocks(blocks)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "block": _public_block(block, user), "blocks": [_public_block(b, user) for b in blocks]}


@frappe.whitelist()
def lock_note_block(scope, block_id):
	user = frappe.session.user
	doc = _get_board(scope, create=True)
	blocks = _parse_blocks(doc.blocks_json)
	found = None
	for b in blocks:
		if b.get("id") != block_id:
			continue
		if not _can_edit(b, user):
			frappe.throw(_("Block is being edited by {0}").format(b.get("locked_by")))
		now = now_datetime()
		b["locked_by"] = user
		b["locked_until"] = str(now + timedelta(seconds=LOCK_SECONDS))
		found = b
		break
	if not found:
		frappe.throw(_("Block not found"))
	doc.blocks_json = _dump_blocks(blocks)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "block": _public_block(found, user)}


@frappe.whitelist()
def unlock_note_block(scope, block_id):
	user = frappe.session.user
	doc = _get_board(scope, create=True)
	blocks = _parse_blocks(doc.blocks_json)
	for b in blocks:
		if b.get("id") != block_id:
			continue
		if b.get("locked_by") == user or _lock_expired(b):
			b["locked_by"] = None
			b["locked_until"] = None
		break
	doc.blocks_json = _dump_blocks(blocks)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "blocks": [_public_block(b, user) for b in blocks]}


@frappe.whitelist()
def update_note_block(scope, block_id, patch=None):
	"""Update block content; renews lock for current user."""
	user = frappe.session.user
	if isinstance(patch, str):
		patch = frappe.parse_json(patch) or {}
	patch = patch or {}
	doc = _get_board(scope, create=True)
	blocks = _parse_blocks(doc.blocks_json)
	found = None
	for b in blocks:
		if b.get("id") != block_id:
			continue
		if not _can_edit(b, user):
			frappe.throw(_("Block is being edited by {0}").format(b.get("locked_by")))
		now = now_datetime()
		if "type" in patch and patch["type"] in ALLOWED_TYPES:
			b["type"] = patch["type"]
		if "content" in patch:
			b["content"] = patch["content"]
		if "items" in patch:
			b["items"] = patch["items"]
		b["locked_by"] = user
		b["locked_until"] = str(now + timedelta(seconds=LOCK_SECONDS))
		b["modified"] = str(now)
		b["modified_by"] = user
		found = b
		break
	if not found:
		frappe.throw(_("Block not found"))
	doc.blocks_json = _dump_blocks(blocks)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "block": _public_block(found, user), "blocks": [_public_block(b, user) for b in blocks]}


@frappe.whitelist()
def remove_note_block(scope, block_id):
	user = frappe.session.user
	doc = _get_board(scope, create=True)
	blocks = _parse_blocks(doc.blocks_json)
	kept = []
	removed = False
	for b in blocks:
		if b.get("id") == block_id:
			if not _can_edit(b, user):
				frappe.throw(_("Block is being edited by {0}").format(b.get("locked_by")))
			removed = True
			continue
		kept.append(b)
	if not removed:
		frappe.throw(_("Block not found"))
	doc.blocks_json = _dump_blocks(kept)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "blocks": [_public_block(b, user) for b in kept]}
