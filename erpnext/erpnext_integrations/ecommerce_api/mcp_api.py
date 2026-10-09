"""Narrow MCP gateway — the ONLY Frappe surface the mcp-erp server may call.

Auth model: the mcp-erp server authenticates to this site with the tenant API
key (``Authorization: token key:secret``) and passes the admin's MCP Bearer
token as ``mcp_token``. The token is verified against the ``mcp.link.token``
store, the request's acting-user header is rebound to the key's bound user so
staff permission checks in the domain APIs keep applying, and every method
enforces the DocType view/edit matrix + field allowlists before touching a
document. Writes are two-step: ``confirm=0`` returns a preview/diff with
``requires_confirm: true`` and persists nothing; ``confirm=1`` applies.

Everything is written to the ``mcp.audit`` store (who / which key / which
fields / ok / error). Applies are bound to a prior preview: confirm=1 needs the
single-use ``preview_id`` returned by the preview, and the payload must be
identical to what was previewed (15 min TTL). Per-token rate limits apply (reads 240/min, writes
60/min). Never expose seed/delete/import/snippet methods here.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time

import frappe
from frappe import _
from frappe.utils import cstr, flt

from erpnext.erpnext_integrations.ecommerce_api.item_pricing import effective_item_price, effective_item_rates

from erpnext.erpnext_integrations.ecommerce_api.mcp_keys_api import (
	AUDIT_SCOPE,
	EDIT_LOCKED_DOCTYPES,
	GATEWAY_VERSION,
	KNOWN_DOCTYPES,
	load_mcp_matrix,
	load_mcp_token_store,
	verify_mcp_token,
)

READ_LIMIT_PER_MIN = 240
WRITE_LIMIT_PER_MIN = 60
MAX_PAGE_LENGTH = 100
MAX_SEARCH_LIMIT = 20
AUDIT_PRUNE_KEEP = 2000
AUDIT_PRUNE_AT = 3000

# Fields no agent may patch at the top level of any document.
UPDATE_BLOCKED_GLOBAL = {
	"name",
	"owner",
	"creation",
	"created_by",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"doctype",
	"lft",
	"rgt",
	"amended_from",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
}
BLOCKED_PREFIXES = ("base_",)
SENSITIVE_OUTPUT = {
	"password",
	"new_password",
	"verify_password",
	"reset_password_key",
	"api_key",
	"api_secret",
	"access_token",
	"refresh_token",
	"token",
	"secret",
	"email_password",
}

# Per-DocType extra blocked fields (money / stock / accounting / identity).
DOCTYPE_EXTRA_BLOCKS = {
	"Item": {
		"valuation_method",
		"opening_stock",
		"opening_valuation",
		"is_fixed_asset",
		"asset_category",
		"deferred_expense_account",
		"deferred_revenue_account",
		"last_purchase_rate",
	},
	"Sales Order": {
		"status",
		"advance_paid",
		"per_delivered",
		"per_billed",
		"per_picked",
		"billing_status",
		"delivery_status",
	},
	"Delivery Note": {"status", "per_billed", "per_returned", "billing_status", "lr_no", "lr_date"},
	"Purchase Order": {"status", "per_received", "per_billed", "billing_status", "advance_paid"},
	"Customer": {"credit_limit", "bypass_credit_limit_check"},
	"Supplier": {"credit_limit"},
	"Warehouse": {"account"},
	"Employee": {"user_id", "bank_name", "bank_account_no", "salary_mode"},
	"User": {"password", "new_password", "reset_password_key", "api_key", "api_secret", "email_password"},
	"Company": {"abbr", "default_currency", "chart_of_accounts", "existing_company"},
}

# System keys silently stripped inside child-table rows (rows are echoed back
# by LLMs; stripping beats failing the whole patch).
CHILD_ROW_STRIP = {
	"owner",
	"creation",
	"created_by",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"doctype",
}

FILTER_OPS = {"=", "!=", "like", "not like", "in", "not in", ">", "<", ">=", "<=", "between"}


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _now_iso() -> str:
	from frappe.utils import now_datetime

	return str(now_datetime())


def _request_ip() -> str | None:
	try:
		return frappe.local.request_ip
	except Exception:
		return None


def _hash_args(payload) -> str:
	try:
		raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
	except Exception:
		raw = str(payload)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _audit(*, tool, actor=None, key_id=None, doctype=None, name=None, args_hash=None, ok=True, error=None):
	try:
		payload = {
			"ts": _now_iso(),
			"tool": tool,
			"actor": actor,
			"key_id": key_id,
			"doctype": doctype,
			"name": name,
			"args_hash": args_hash,
			"ok": 1 if ok else 0,
			"ip": _request_ip(),
		}
		if error:
			payload["error"] = str(error)[:300]
		frappe.flags.ignore_permissions = True
		frappe.get_doc(
			{
				"doctype": "Table Extra Data",
				"scope": AUDIT_SCOPE,
				"row_key": f"{payload['ts']}|{secrets.token_hex(4)}",
				"data_json": json.dumps(payload, ensure_ascii=False, default=str),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		_prune_audit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "mcp_audit_write_failed")


def _prune_audit():
	try:
		count = frappe.db.count("Table Extra Data", {"scope": AUDIT_SCOPE})
		if count and count > AUDIT_PRUNE_AT:
			frappe.flags.ignore_permissions = True
			oldest = frappe.get_all(
				"Table Extra Data",
				filters={"scope": AUDIT_SCOPE},
				pluck="name",
				order_by="creation asc",
				limit_page_length=count - AUDIT_PRUNE_KEEP,
				ignore_permissions=True,
			)
			for name in oldest:
				frappe.delete_doc("Table Extra Data", name, ignore_permissions=True)
			frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "mcp_audit_prune_failed")


def _rate_limit(key_id: str, limit: int, kind: str) -> None:
	try:
		# Separate read/write buckets: browsing must not eat the write budget.
		bucket = f"mcp-rl:{kind}:{key_id}:{int(time.time() // 60)}"
		cache = frappe.cache()
		current = cache.incr(bucket)
		if current == 1:
			cache.expire(bucket, 70)
		if current > limit:
			frappe.throw(_("MCP rate limit exceeded. Retry in a minute."), frappe.ValidationError)
	except frappe.ValidationError:
		raise
	except Exception:
		# Cache unreachable → fail open (audit still records the call).
		pass


PREVIEW_TTL_SEC = 15 * 60


def _preview_key(ctx: dict, preview_id: str) -> str:
	return f"mcp-preview:{ctx['key_id']}:{preview_id}"


def _payload_digest(payload) -> str:
	raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _remember_preview(ctx: dict, payload) -> str:
	"""Store the digest of a previewed write; apply must present the returned id."""
	preview_id = secrets.token_hex(8)
	frappe.cache().set_value(_preview_key(ctx, preview_id), _payload_digest(payload), expires_in_sec=PREVIEW_TTL_SEC)
	return preview_id


def _consume_preview(ctx: dict, payload, preview_id) -> None:
	"""Single-use: the apply payload must be byte-identical to what was previewed."""
	preview_id = str(preview_id or "").strip()
	if not preview_id:
		frappe.throw(
			_("Missing preview_id — call the preview first, show the user the diff, then apply with its preview_id"),
			frappe.ValidationError,
		)
	key = _preview_key(ctx, preview_id)
	stored = frappe.cache().get_value(key)
	if not stored:
		frappe.throw(_("preview_id {0} is unknown, expired or already used — preview again").format(preview_id))
	if stored != _payload_digest(payload):
		frappe.throw(_("Payload differs from the previewed one — preview the new values and show them to the user"))
	frappe.cache().delete_value(key)


def _bind_acting_user(user: str) -> None:
	"""Force the staff-permission layer to see the key's bound user.

	Werkzeug request headers are immutable views over the WSGI environ, so the
	environ key is rewritten instead; the cached perm lookup is dropped so
	``_require_app_permission`` re-resolves against the bound user.
	"""
	req = getattr(frappe.local, "request", None)
	environ = getattr(req, "environ", None) if req is not None else None
	if environ is not None:
		environ["HTTP_X_ERP_ACTING_USER"] = user or ""
	if hasattr(frappe.local, "_staff_acting_perm_info"):
		del frappe.local._staff_acting_perm_info


def _ctx(mcp_token, *, write=False):
	"""Verify the MCP token, bind its acting user, rate-limit. Returns ctx dict."""
	store = load_mcp_token_store()
	key_id = store.get("key_id") or ""
	actor = store.get("acting_user") or ""
	if not verify_mcp_token(mcp_token or ""):
		_audit(tool="auth", actor=actor or None, key_id=key_id or None, ok=False, error="invalid token")
		frappe.throw(_("Invalid or revoked MCP token"), frappe.AuthenticationError)
	ctx = {"store": store, "key_id": key_id, "actor": actor, "write": write}
	frappe.local._mcp_ctx = ctx
	_rate_limit(key_id, WRITE_LIMIT_PER_MIN if write else READ_LIMIT_PER_MIN, "w" if write else "r")
	# Without a bound user the staff-permission layer would allow everything,
	# so an unbound / deleted / disabled actor is a hard auth failure.
	enabled = frappe.db.get_value("User", actor, "enabled") if actor else None
	if enabled is None or int(enabled) == 0:
		_audit(tool="auth", actor=actor or None, key_id=key_id, ok=False, error="actor missing or disabled")
		frappe.throw(
			_("MCP acting user {0} is missing or disabled — rebind it in Settings → Apps & devices").format(
				actor or "(none)"
			),
			frappe.AuthenticationError,
		)
	_bind_acting_user(actor)
	return ctx


def _matrix() -> dict:
	return load_mcp_matrix()


def _can_view(matrix: dict, doctype: str) -> None:
	if doctype not in KNOWN_DOCTYPES:
		frappe.throw(_("DocType {0} is not exposed over MCP").format(doctype), frappe.PermissionError)
	if not (matrix.get(doctype) or {}).get("view"):
		frappe.throw(_("MCP key has no view permission for {0}").format(doctype), frappe.PermissionError)


def _can_edit(matrix: dict, doctype: str) -> None:
	_can_view(matrix, doctype)
	if doctype in EDIT_LOCKED_DOCTYPES:
		frappe.throw(_("DocType {0} is view-only over MCP").format(doctype), frappe.PermissionError)
	if not (matrix.get(doctype) or {}).get("edit"):
		frappe.throw(_("MCP key has no edit permission for {0}").format(doctype), frappe.PermissionError)


def _get_meta(doctype: str):
	meta = getattr(frappe.local, "_mcp_meta_cache", None)
	if meta is None:
		meta = {}
		frappe.local._mcp_meta_cache = meta
	if doctype not in meta:
		meta[doctype] = frappe.get_meta(doctype)
	return meta[doctype]


def _blocked_fields(doctype: str) -> set:
	return set(UPDATE_BLOCKED_GLOBAL) | set(DOCTYPE_EXTRA_BLOCKS.get(doctype, set()))


def _known_fieldnames(doctype: str) -> set:
	meta = _get_meta(doctype)
	names = {df.fieldname for df in meta.fields}
	names.add("name")
	return names


def _sanitize_child_rows(doctype: str, fieldname: str, value):
	meta = _get_meta(doctype)
	field = meta.get_field(fieldname)
	if not field or field.fieldtype != "Table":
		frappe.throw(_("{0} is not a child table on {1}").format(fieldname, doctype))
	child_dt = field.options
	if not isinstance(value, list):
		frappe.throw(_("Child table {0} must be a list of rows").format(fieldname))
	rows = []
	for row in value:
		if not isinstance(row, dict):
			frappe.throw(_("Child table {0} rows must be objects").format(fieldname))
		clean = {k: v for k, v in row.items() if k not in CHILD_ROW_STRIP}
		unknown = set(clean) - _known_fieldnames(child_dt)
		if unknown:
			frappe.throw(
				_("Unknown fields on {0}: {1}").format(child_dt, ", ".join(sorted(unknown))),
				frappe.ValidationError,
			)
		rows.append(clean)
	return rows


def _validate_fields(doctype: str, fields, *, for_create: bool) -> dict:
	if not isinstance(fields, dict):
		frappe.throw(_("fields must be an object of fieldname → value"))
	if not fields:
		frappe.throw(_("fields is empty — nothing to change"))
	blocked = _blocked_fields(doctype)
	known = _known_fieldnames(doctype)
	clean: dict = {}
	rejected: dict = {}
	for key, value in fields.items():
		key = str(key)
		if key in blocked or any(key.startswith(prefix) for prefix in BLOCKED_PREFIXES):
			if for_create and key == "name":
				clean[key] = value
				continue
			rejected[key] = "blocked"
			continue
		if key not in known:
			rejected[key] = "unknown"
			continue
		meta = _get_meta(doctype)
		field = meta.get_field(key)
		if field and field.fieldtype == "Table":
			clean[key] = _sanitize_child_rows(doctype, key, value)
		else:
			clean[key] = value
	if rejected:
		frappe.throw(
			_("Fields not allowed on {0}: {1}").format(
				doctype,
				", ".join(f"{k} ({why})" for k, why in sorted(rejected.items())),
			),
			frappe.PermissionError,
		)
	return clean


def _sanitize_output(value):
	if isinstance(value, dict):
		out = {}
		for k, v in value.items():
			if k in SENSITIVE_OUTPUT or k.lower() in SENSITIVE_OUTPUT:
				continue
			out[k] = _sanitize_output(v)
		return out
	if isinstance(value, list):
		return [_sanitize_output(v) for v in value]
	return value


def _json_safe(value):
	return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _diff_entry(doctype: str, fieldname: str, old, new) -> dict:
	meta = _get_meta(doctype)
	field = meta.get_field(fieldname)
	if field and field.fieldtype == "Table" and isinstance(new, list):
		return {
			"field": fieldname,
			"kind": "table",
			"from_rows": len(old) if isinstance(old, list) else None,
			"to_rows": len(new),
			"sample": _sanitize_output(new[:3]),
		}
	return {"field": fieldname, "kind": "scalar", "from": _json_safe(old), "to": _json_safe(new)}


def _guard(tool: str, doctype=None, name=None, payload=None):
	"""Audit wrapper: records ok/error for every gateway call."""

	def decorator(fn):
		def wrapper(*args, **kwargs):
			ctx = None
			try:
				out = fn(*args, **kwargs)
				ctx = getattr(frappe.local, "_mcp_ctx", None)
				_audit(
					tool=tool,
					actor=(ctx or {}).get("actor"),
					key_id=(ctx or {}).get("key_id"),
					doctype=doctype() if callable(doctype) else doctype,
					name=name() if callable(name) else name,
					args_hash=_hash_args(payload() if callable(payload) else payload),
					ok=True,
				)
				return out
			except Exception as e:
				ctx = getattr(frappe.local, "_mcp_ctx", None)
				if not isinstance(e, frappe.AuthenticationError):
					_audit(
						tool=tool,
						actor=(ctx or {}).get("actor"),
						key_id=(ctx or {}).get("key_id"),
						doctype=doctype() if callable(doctype) else doctype,
						name=name() if callable(name) else name,
						args_hash=_hash_args(payload() if callable(payload) else payload),
						ok=False,
						error=e,
					)
				raise

		return wrapper

	return decorator


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@frappe.whitelist()
def mcp_whoami(mcp_token=None):
	"""Return the authenticated MCP key identity and matrix summary."""

	@_guard("whoami", payload={"token": "***"})
	def _impl(mcp_token):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		matrix = _matrix()
		return {
			"site": frappe.local.site,
			"acting_user": ctx["actor"],
			"key_id": ctx["key_id"],
			"key_created_at": ctx["store"].get("created_at"),
			"created_by": ctx["store"].get("created_by"),
			"gateway_version": GATEWAY_VERSION,
			"matrix": {
				"total": len(matrix),
				"view": sum(1 for v in matrix.values() if v.get("view")),
				"edit": sum(1 for v in matrix.values() if v.get("edit")),
			},
		}

	return _impl(mcp_token)


@frappe.whitelist()
def mcp_get_capabilities(mcp_token=None):
	"""Return this key's full matrix + available workflows (drives tool listing)."""

	@_guard("capabilities", payload={"token": "***"})
	def _impl(mcp_token):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		matrix = _matrix()
		rows = [
			{"doctype": row["doctype"], "group": row["group"], **matrix.get(row["doctype"], {"view": 0, "edit": 0})}
			for row in _matrix_rows_meta()
		]
		return {
			"version": GATEWAY_VERSION,
			"site": frappe.local.site,
			"acting_user": ctx["actor"],
			"doctypes": rows,
			"workflows": _workflow_catalog(),
		}

	return _impl(mcp_token)


def _matrix_rows_meta():
	from erpnext.erpnext_integrations.ecommerce_api.mcp_keys_api import MCP_DOCTYPE_MATRIX

	return MCP_DOCTYPE_MATRIX


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@frappe.whitelist()
def mcp_search(mcp_token=None, doctype=None, query=None, limit=10):
	"""Typeahead across one matrix-view DocType: matches name or title field."""

	@_guard("search", doctype=lambda: doctype, payload=lambda: {"doctype": doctype, "query": query})
	def _impl(mcp_token, doctype, query, limit):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		doctype = str(doctype or "").strip()
		query = str(query or "").strip()
		if not doctype or not query:
			frappe.throw(_("doctype and query are required"))
		_can_view(_matrix(), doctype)
		limit = max(1, min(int(limit or 10), MAX_SEARCH_LIMIT))
		meta = _get_meta(doctype)
		title = meta.get_title_field() or "name"
		or_filters = [["name", "like", f"%{query}%"]]
		if title != "name":
			or_filters.append([title, "like", f"%{query}%"])
		fields = ["name"] + ([title] if title != "name" else [])
		rows = frappe.get_all(
			doctype,
			or_filters=or_filters,
			fields=fields,
			limit_page_length=limit,
			ignore_permissions=True,
		)
		if doctype == "Item" and rows:
			pl = _default_selling_price_list()
			rates = effective_item_rates([r.name for r in rows], pl)
			for r in rows:
				r["price"] = rates.get(r.name)
				r["price_list"] = pl
		return {"doctype": doctype, "matches": _sanitize_output(rows), "count": len(rows)}

	return _impl(mcp_token, doctype, query, limit)


@frappe.whitelist()
def mcp_list_records(mcp_token=None, doctype=None, filters=None, fields=None, limit=50, start=0):
	"""List records of a matrix-view DocType with validated filters.

	``filters`` is a list of [field, operator, value] triples; operator must be
	in the whitelist. ``fields`` (optional) limits the selected columns and
	must be real fieldnames.
	"""

	@_guard("list", doctype=lambda: doctype, payload=lambda: {"doctype": doctype, "filters": filters})
	def _impl(mcp_token, doctype, filters, fields, limit, start):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		doctype = str(doctype or "").strip()
		if not doctype:
			frappe.throw(_("doctype is required"))
		_can_view(_matrix(), doctype)
		meta = _get_meta(doctype)
		known = {df.fieldname for df in meta.fields} | {"name"}
		parsed_filters = []
		for triple in _parse_json(filters, []) or []:
			if not (isinstance(triple, (list, tuple)) and len(triple) == 3):
				frappe.throw(_("Each filter must be [field, operator, value]"))
			field, op, value = str(triple[0]), str(triple[1]).strip().lower(), triple[2]
			if field not in known:
				frappe.throw(_("Unknown filter field {0} on {1}").format(field, doctype))
			if op not in FILTER_OPS:
				frappe.throw(_("Filter operator {0} is not allowed").format(op))
			parsed_filters.append([field, op, value])
		selected = None
		if fields:
			if not isinstance(_parse_json(fields, fields), list):
				frappe.throw(_("fields must be a list of fieldnames"))
			selected = [str(f) for f in _parse_json(fields, fields)]
			unknown = [f for f in selected if f not in known]
			if unknown:
				frappe.throw(_("Unknown fields on {0}: {1}").format(doctype, ", ".join(unknown)))
		limit = max(1, min(int(limit or 50), MAX_PAGE_LENGTH))
		start = max(0, int(start or 0))
		rows = frappe.get_all(
			doctype,
			filters=parsed_filters,
			fields=selected or ["name"],
			limit_start=start,
			limit_page_length=limit,
			ignore_permissions=True,
		)
		return {
			"doctype": doctype,
			"rows": _sanitize_output(rows),
			"count": len(rows),
			"start": start,
			"limit": limit,
		}

	return _impl(mcp_token, doctype, filters, fields, limit, start)


@frappe.whitelist()
def mcp_get_record(mcp_token=None, doctype=None, name=None):
	"""Return one sanitized document from a matrix-view DocType."""

	@_guard("get", doctype=lambda: doctype, name=lambda: name)
	def _impl(mcp_token, doctype, name):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		doctype = str(doctype or "").strip()
		name = str(name or "").strip()
		if not doctype or not name:
			frappe.throw(_("doctype and name are required"))
		_can_view(_matrix(), doctype)
		if not frappe.db.exists(doctype, name):
			frappe.throw(_("{0} {1} not found").format(doctype, name), frappe.DoesNotExistError)
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(doctype, name)
		frappe.flags.ignore_permissions = False
		return {"doctype": doctype, "name": name, "doc": _sanitize_output(_json_safe(doc.as_dict()))}

	return _impl(mcp_token, doctype, name)


MAX_RESOLVE_LINES = 200
MAX_PDF_BYTES = 10 * 1024 * 1024


def _default_selling_price_list() -> str:
	return frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"


def _norm_text(value) -> str:
	import unicodedata

	text = unicodedata.normalize("NFKD", cstr(value)).encode("ascii", "ignore").decode().lower()
	return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def _item_candidates_by_name(name: str, limit: int = 5) -> list[dict]:
	"""Items whose name shares the most words with ``name`` (score 0–1)."""
	words = [w for w in _norm_text(name).split() if len(w) > 1]
	if not words:
		return []
	# Pre-filter on the longest word, then score every candidate on word overlap.
	anchor = max(words, key=len)
	rows = frappe.get_all(
		"Item",
		filters={"disabled": 0, "has_variants": 0},
		or_filters=[["item_name", "like", f"%{anchor}%"], ["name", "like", f"%{anchor}%"]],
		fields=["name", "item_name", "stock_uom"],
		limit_page_length=200,
		ignore_permissions=True,
	)
	wanted = set(words)
	scored = []
	for r in rows:
		have = set(_norm_text(r.item_name).split())
		hits = len(wanted & have)
		if not hits:
			continue
		score = round(hits / len(wanted | have), 3)  # Jaccard: penalises extra words both ways
		scored.append({"item_code": r.name, "item_name": r.item_name, "stock_uom": r.stock_uom, "score": score})
	scored.sort(key=lambda x: -x["score"])
	return scored[:limit]


def _resolve_line(line: dict) -> dict:
	"""Best Item for one document line: exact code → barcode → fuzzy name."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _item_codes_for_barcode

	code = cstr(line.get("item_code") or line.get("code") or "").strip()
	barcode = cstr(line.get("barcode") or "").strip()
	name = cstr(line.get("name") or line.get("description") or "").strip()
	for raw, how in ((code, "item_code"), (barcode, "barcode"), (code, "barcode")):
		if not raw:
			continue
		hit = raw if how == "item_code" and frappe.db.exists("Item", raw) else None
		if how == "barcode":
			codes = _item_codes_for_barcode(raw)
			hit = codes[0] if codes else None
		if hit:
			item = frappe.db.get_value("Item", hit, ["name", "item_name", "stock_uom", "disabled"], as_dict=True)
			return {
				"match": {"item_code": item.name, "item_name": item.item_name, "stock_uom": item.stock_uom, "score": 1.0},
				"matched_by": how,
				"alternatives": [],
				"needs_review": bool(item.disabled),
				"note": "item is disabled" if item.disabled else None,
			}
	candidates = _item_candidates_by_name(name) if name else []
	best = candidates[0] if candidates else None
	runner_up = candidates[1]["score"] if len(candidates) > 1 else 0
	# Confident only when the name matches well AND clearly beats the next guess.
	confident = bool(best) and best["score"] >= 0.6 and best["score"] - runner_up >= 0.15
	return {
		"match": best,
		"matched_by": "name" if best else None,
		"alternatives": candidates[1:],
		"needs_review": not confident,
		"note": None if best else "no item found — ask the user or search_records(doctype='Item')",
	}


@frappe.whitelist()
def mcp_resolve_items(mcp_token=None, lines=None, price_list=None):
	"""Match document lines (code / barcode / name) to Items in one call.

	Each result carries the best match, alternatives, ``needs_review`` and the
	effective price on ``price_list`` (default selling list). Lines echo the
	caller's qty / rate so a PDF can be turned into ``create_order`` items.
	"""

	@_guard("resolve_items", doctype="Item", payload=lambda: {"lines": lines, "price_list": price_list})
	def _impl(mcp_token, lines, price_list):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		_can_view(_matrix(), "Item")
		lines = _parse_json(lines, lines)
		if not isinstance(lines, list) or not lines:
			frappe.throw(_("lines must be a non-empty list of {item_code | barcode | name, qty, rate}"))
		if len(lines) > MAX_RESOLVE_LINES:
			frappe.throw(_("At most {0} lines per call").format(MAX_RESOLVE_LINES))
		pl = cstr(price_list or "").strip() or _default_selling_price_list()
		out = []
		for idx, line in enumerate(lines, start=1):
			if not isinstance(line, dict):
				line = {"name": cstr(line)}
			res = _resolve_line(line)
			match = res["match"]
			price = None
			if match:
				row = effective_item_price(match["item_code"], pl)
				price = flt(row.price_list_rate) if row else None
			out.append(
				{
					"line": idx,
					"input": _sanitize_output(_json_safe(line)),
					**res,
					"price": price,
					"qty": line.get("qty"),
					"document_rate": line.get("rate"),
				}
			)
		return {
			"price_list": pl,
			"lines": out,
			"resolved": sum(1 for r in out if r["match"] and not r["needs_review"]),
			"needs_review": sum(1 for r in out if r["needs_review"]),
		}

	return _impl(mcp_token, lines, price_list)


def _inline_local_resources(html: str) -> str:
	"""Inline /assets stylesheets and /assets|/files images as data: URIs.

	get_pdf expands relative URLs to http://<site name>/…, which does not
	resolve inside the server (HostNotFoundError in dev and in the Docker
	image), and it forces disable-local-file-access, so file:// is out too.
	Inlining needs no network and no knowledge of the web server's address.
	"""
	import base64
	import mimetypes
	import os
	import re

	roots = {
		"/assets/": os.path.abspath(os.path.join(frappe.local.sites_path, "assets")),
		"/files/": os.path.abspath(frappe.get_site_path("public", "files")),
	}

	def _disk(url: str) -> str | None:
		path = url.split("?", 1)[0].split("#", 1)[0]
		for web, root in roots.items():
			if path.startswith(web):
				# normpath (not realpath): sites/assets/<app> are symlinks into apps/,
				# but a ../ in the URL still cannot climb out of the public root.
				full = os.path.normpath(os.path.join(root, path[len(web) :]))
				if full.startswith(root + os.sep) and os.path.isfile(full):
					return full
		return None

	def _stylesheet(match):
		full = _disk(match.group(2))
		if not full:
			return ""
		# Stay a <link>: an inline <style> would be rewritten by scrub_urls
		# (url(...) → "… !important"), which broke the print grid.
		with open(full, "rb") as f:
			return f'<link type="text/css" rel="stylesheet" href="data:text/css;base64,{base64.b64encode(f.read()).decode()}">'

	def _data_uri(match):
		attr, quote, url = match.group(1), match.group(2), match.group(3)
		full = _disk(url)
		if not full:
			return f"{attr}{quote}data:,"
		mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
		with open(full, "rb") as f:
			return f"{attr}{quote}data:{mime};base64,{base64.b64encode(f.read()).decode()}"

	html = re.sub(r"""<link\b[^>]*?href\s*=\s*(["'])(/assets/[^"']+?\.css[^"']*)\1[^>]*>""", _stylesheet, html)
	html = re.sub(r"""(\bsrc\s*=\s*)(["'])(/(?:assets|files)/[^"']*)""", _data_uri, html)
	# src="" would be expanded to the (unresolvable) site root and fetched.
	return re.sub(r"""(\bsrc\s*=\s*)(["'])\2""", r"\1\2data:,\2", html)


def _render_pdf(doctype: str, name: str, print_format: str | None) -> bytes:
	from frappe.utils.pdf import get_pdf

	html = _inline_local_resources(frappe.get_print(doctype, name, print_format=print_format))
	return get_pdf(html)


@frappe.whitelist()
def mcp_get_print_pdf(mcp_token=None, doctype=None, name=None, print_format=None):
	"""Render a viewable document with its print format; returns base64 PDF."""

	@_guard("print_pdf", doctype=lambda: doctype, name=lambda: name)
	def _impl(mcp_token, doctype, name, print_format):
		import base64

		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		doctype = str(doctype or "").strip()
		name = str(name or "").strip()
		if not doctype or not name:
			frappe.throw(_("doctype and name are required"))
		_can_view(_matrix(), doctype)
		if not frappe.db.exists(doctype, name):
			frappe.throw(_("{0} {1} not found").format(doctype, name), frappe.DoesNotExistError)
		print_format = cstr(print_format or "").strip() or None
		if print_format and not frappe.db.exists("Print Format", {"name": print_format, "doc_type": doctype}):
			frappe.throw(_("Print Format {0} does not exist for {1}").format(print_format, doctype))
		frappe.flags.ignore_permissions = True
		try:
			pdf = _render_pdf(doctype, name, print_format)
		finally:
			frappe.flags.ignore_permissions = False
		if len(pdf) > MAX_PDF_BYTES:
			frappe.throw(_("PDF is larger than {0} MB").format(MAX_PDF_BYTES // (1024 * 1024)))
		return {
			"doctype": doctype,
			"name": name,
			"print_format": print_format or "default",
			"filename": f"{name}.pdf",
			"mime_type": "application/pdf",
			"size_bytes": len(pdf),
			"pdf_base64": base64.b64encode(pdf).decode(),
		}

	return _impl(mcp_token, doctype, name, print_format)


# ---------------------------------------------------------------------------
# Writes (preview → confirm)
# ---------------------------------------------------------------------------


def _load_doc_for_edit(doctype: str, name: str):
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(doctype, name), frappe.DoesNotExistError)
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(doctype, name)
	frappe.flags.ignore_permissions = False
	return doc


@frappe.whitelist()
def mcp_update_record(mcp_token=None, doctype=None, name=None, fields=None, confirm=0, preview_id=None):
	"""Patch a document. ``confirm=0`` returns the diff + ``preview_id`` (writes
	nothing); ``confirm=1`` applies it and requires that ``preview_id`` with the
	identical fields. Blocked/unknown fields are rejected loudly."""

	@_guard(
		"update",
		doctype=lambda: doctype,
		name=lambda: name,
		payload=lambda: {"doctype": doctype, "name": name, "fields": fields, "confirm": confirm},
	)
	def _impl(mcp_token, doctype, name, fields, confirm, preview_id):
		ctx = _ctx(mcp_token, write=True)
		frappe.local._mcp_ctx = ctx
		doctype = str(doctype or "").strip()
		name = str(name or "").strip()
		if not doctype or not name:
			frappe.throw(_("doctype and name are required"))
		_can_edit(_matrix(), doctype)
		changes = _validate_fields(doctype, _parse_json(fields, fields), for_create=False)
		doc = _load_doc_for_edit(doctype, name)
		diff = [
			_diff_entry(doctype, fieldname, doc.get(fieldname), value) for fieldname, value in changes.items()
		]
		signed = {"op": "update", "doctype": doctype, "name": name, "fields": changes}
		if not cint_confirm(confirm):
			return {
				"doctype": doctype,
				"name": name,
				"preview": diff,
				"requires_confirm": True,
				"applied": False,
				"preview_id": _remember_preview(ctx, signed),
				"expires_in_sec": PREVIEW_TTL_SEC,
			}
		_consume_preview(ctx, signed, preview_id)
		for fieldname, value in changes.items():
			doc.set(fieldname, value)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return {
			"doctype": doctype,
			"name": name,
			"applied": True,
			"changes": diff,
			"doc": _sanitize_output(_json_safe(doc.as_dict())),
		}

	return _impl(mcp_token, doctype, name, fields, confirm, preview_id)


@frappe.whitelist()
def mcp_create_record(mcp_token=None, doctype=None, fields=None, confirm=0, preview_id=None):
	"""Create a document (matrix edit = write+create). ``confirm=0`` echoes the
	validated payload + ``preview_id``; ``confirm=1`` inserts and requires it."""

	@_guard(
		"create",
		doctype=lambda: doctype,
		payload=lambda: {"doctype": doctype, "fields": fields, "confirm": confirm},
	)
	def _impl(mcp_token, doctype, fields, confirm, preview_id):
		ctx = _ctx(mcp_token, write=True)
		frappe.local._mcp_ctx = ctx
		doctype = str(doctype or "").strip()
		if not doctype:
			frappe.throw(_("doctype is required"))
		_can_edit(_matrix(), doctype)
		changes = _validate_fields(doctype, _parse_json(fields, fields), for_create=True)
		signed = {"op": "create", "doctype": doctype, "fields": changes}
		if not cint_confirm(confirm):
			return {
				"doctype": doctype,
				"preview": _sanitize_output(_json_safe(changes)),
				"requires_confirm": True,
				"applied": False,
				"preview_id": _remember_preview(ctx, signed),
				"expires_in_sec": PREVIEW_TTL_SEC,
			}
		_consume_preview(ctx, signed, preview_id)
		doc = frappe.get_doc({"doctype": doctype, **changes})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return {
			"doctype": doctype,
			"name": doc.name,
			"applied": True,
			"doc": _sanitize_output(_json_safe(doc.as_dict())),
		}

	return _impl(mcp_token, doctype, fields, confirm, preview_id)


# ---------------------------------------------------------------------------
# Workflows (domain verbs over existing ecommerce_api rules)
# ---------------------------------------------------------------------------


def cint_confirm(value) -> bool:
	try:
		from frappe.utils import cint

		return bool(cint(value))
	except Exception:
		return False


_PREORDER_STATUSES = ["Consulta", "Orden", "Preparado", "Delivery", "Completado", "En Delivery"]


_ORDER_LINE_SCHEMA = {
	"type": "object",
	"properties": {
		"item_code": {"type": "string", "description": "Item code or barcode (use resolve_items first)"},
		"qty": {"type": "number"},
		"rate": {
			"type": "number",
			"description": "Optional. Omit to use the current price-list price; set it to copy a document exactly.",
		},
		"uom": {"type": "string"},
	},
	"required": ["item_code", "qty"],
}


def _workflow_catalog() -> list[dict]:
	return [
		{
			"name": "create_order",
			"doctype": "Sales Order",
			"description": (
				"Create a Pedido (Sales Order in the Órdenes pipeline) in one step — same path as the "
				"catalog/Operaciones create. Lines without a rate get the current price-list price; "
				"lines with a rate keep it (copying a PDF/quote exactly). initial_status Orden submits it. "
				"The preview prices every line and flags unknown items."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"customer": {"type": "string", "description": "Customer name; omit for Consumidor Final"},
					"items": {"type": "array", "items": _ORDER_LINE_SCHEMA},
					"initial_status": {"type": "string", "enum": ["Consulta", "Orden"]},
					"delivery_date": {"type": "string", "description": "YYYY-MM-DD; default next business day"},
					"price_list": {"type": "string"},
					"notes": {"type": "string"},
				},
				"required": ["items"],
			},
		},
		{
			"name": "reprice_order",
			"doctype": "Sales Order",
			"description": (
				"Reset every line rate of a Pedido (draft or submitted, no cancel) to the current "
				"price-list price. The preview lists each line's current → new rate and the new total."
			),
			"args_schema": {
				"type": "object",
				"properties": {"name": {"type": "string"}, "price_list": {"type": "string"}},
				"required": ["name"],
			},
		},
		{
			"name": "update_order_lines",
			"doctype": "Sales Order",
			"description": (
				"Replace the lines of a Pedido (draft or submitted, no cancel). Send the FULL new list: "
				"lines not included are removed. A line without rate keeps its current rate, or gets the "
				"price-list price if it is new."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"name": {"type": "string"},
					"items": {"type": "array", "items": _ORDER_LINE_SCHEMA},
					"additional_discount_amount": {"type": "number"},
				},
				"required": ["name", "items"],
			},
		},
		{
			"name": "set_preorder_status",
			"doctype": "Sales Order",
			"description": (
				"Move a guest preorder (Sales Order) through the pipeline: "
				+ " → ".join(_PREORDER_STATUSES)
				+ ". Wraps the same status machine the Órdenes table uses."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"name": {"type": "string", "description": "Sales Order name"},
					"target_status": {"type": "string", "enum": _PREORDER_STATUSES},
					"source": {"type": "string", "enum": ["armado", "remito", "tms_claim", "tms_pod"]},
				},
				"required": ["name", "target_status"],
			},
		},
		{
			"name": "convert_lead",
			"doctype": "Lead",
			"description": (
				"Convert a Preventa Lead into a Customer (wraps convert_lead_to_customer). The site's "
				"conversion checklist may require customer_type / tax_id / customer_group — the preview "
				"lists anything missing under 'issues'."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"lead": {"type": "string"},
					"customer_group": {"type": "string"},
					"customer_type": {"type": "string"},
					"tax_id": {"type": "string"},
				},
				"required": ["lead"],
			},
		},
		{
			"name": "assign_customer_seller",
			"doctype": "Customer",
			"description": "Set the assigned seller(s) (ERP users) for a Customer; first seller becomes account_manager.",
			"args_schema": {
				"type": "object",
				"properties": {
					"customer": {"type": "string"},
					"sellers": {"type": "array", "items": {"type": "string"}},
				},
				"required": ["customer", "sellers"],
			},
		},
		{
			"name": "mark_stop_outcome",
			"doctype": "Delivery Trip",
			"description": (
				"Record a delivery stop outcome (Delivered / Partial / Not Home / Refused / Pending) "
				"with recipient, notes and optionally signature — same rules as the MATs admin editor."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"trip_name": {"type": "string"},
					"stop_idx": {"type": "integer"},
					"outcome": {"type": "string"},
					"recipient_name": {"type": "string"},
					"recipient_id_number": {"type": "string"},
					"notes": {"type": "string"},
					"amount_collected": {"type": "number"},
					"payment_method": {"type": "string"},
				},
				"required": ["trip_name", "stop_idx", "outcome"],
			},
		},
	]


def _order_lines_arg(args: dict) -> list[dict]:
	items = _parse_json(args.get("items"), args.get("items"))
	if not isinstance(items, list) or not items:
		frappe.throw(_("items must be a non-empty list of {item_code, qty, rate?}"))
	for row in items:
		if not isinstance(row, dict) or not cstr(row.get("item_code")).strip():
			frappe.throw(_("Every line needs an item_code"))
	return items


def _guest_preorder(name: str):
	from erpnext.erpnext_integrations.ecommerce_api.api import _is_guest_preorder_sales_order

	if not name or not frappe.db.exists("Sales Order", name):
		frappe.throw(_("Sales Order {0} not found").format(name), frappe.DoesNotExistError)
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", name)
	frappe.flags.ignore_permissions = False
	if not _is_guest_preorder_sales_order(so):
		frappe.throw(_("{0} is not a Pedido (not created through the order pipeline)").format(name))
	if so.docstatus == 2:
		frappe.throw(_("{0} is cancelled (Archivado)").format(name))
	return so


def _priced_lines(items: list[dict], price_list: str, current: dict | None = None):
	"""Resolve + price lines the way the apply will. Returns (lines, total, issues)."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _item_codes_for_barcode

	current = current or {}
	lines, issues, total = [], [], 0.0
	for idx, row in enumerate(items, start=1):
		raw = cstr(row.get("item_code")).strip()
		code = raw if frappe.db.exists("Item", raw) else next(iter(_item_codes_for_barcode(raw) or []), None)
		if not code:
			issues.append(f"Line {idx}: item {raw} not found (use resolve_items)")
			continue
		qty = flt(row.get("qty", 1))
		if qty <= 0:
			issues.append(f"Line {idx}: qty must be > 0")
		eff = effective_item_price(code, price_list)
		list_rate = flt(eff.price_list_rate) if eff else None
		if row.get("rate") not in (None, ""):
			rate, source = flt(row.get("rate")), "given"
		elif code in current:
			rate, source = current[code], "kept"
		else:
			rate, source = list_rate, "price_list"
		if not rate:
			issues.append(f"Line {idx}: {code} has no price on {price_list} — pass a rate")
			rate = 0.0
		amount = round(qty * rate, 2)
		total += amount
		lines.append(
			{
				"line": idx,
				"item_code": code,
				"item_name": frappe.db.get_value("Item", code, "item_name"),
				"qty": qty,
				"rate": rate,
				"rate_source": source,
				"price_list_rate": list_rate,
				"amount": amount,
			}
		)
	return lines, round(total, 2), issues


def _workflow_preview(name: str, args: dict) -> tuple[list[str], dict | None]:
	"""(issues, details) shown at preview time so the user sees exactly what applies."""
	if name == "create_order":
		pl = cstr(args.get("price_list") or "").strip() or _default_selling_price_list()
		issues = []
		customer = cstr(args.get("customer") or "").strip()
		if customer and not frappe.db.exists("Customer", customer):
			issues.append(f"Customer {customer} not found (search_records doctype=Customer)")
		lines, total, line_issues = _priced_lines(_order_lines_arg(args), pl)
		return issues + line_issues, {
			"customer": customer or "Consumidor Final",
			"initial_status": args.get("initial_status") or "Consulta",
			"price_list": pl,
			"lines": lines,
			"estimated_total": total,
		}
	if name == "reprice_order":
		so = _guest_preorder(cstr(args.get("name")))
		pl = cstr(args.get("price_list") or "").strip() or so.selling_price_list or _default_selling_price_list()
		lines = []
		for row in so.items:
			eff = effective_item_price(row.item_code, pl)
			new = flt(eff.price_list_rate) if eff else None
			lines.append(
				{
					"item_code": row.item_code,
					"item_name": row.item_name,
					"qty": flt(row.qty),
					"from_rate": flt(row.rate),
					"to_rate": new if new else flt(row.rate),
					"note": None if new else "no price on list — unchanged",
				}
			)
		new_total = round(sum(line["qty"] * line["to_rate"] for line in lines), 2)
		return [], {
			"order": so.name,
			"price_list": pl,
			"lines": lines,
			"from_total": flt(so.grand_total),
			"to_total_before_discount": new_total,
		}
	if name == "update_order_lines":
		so = _guest_preorder(cstr(args.get("name")))
		current = {row.item_code: flt(row.rate) for row in so.items}
		lines, total, issues = _priced_lines(_order_lines_arg(args), so.selling_price_list or _default_selling_price_list(), current)
		new_codes = {line["item_code"] for line in lines}
		return issues, {
			"order": so.name,
			"from_lines": [
				{"item_code": r.item_code, "item_name": r.item_name, "qty": flt(r.qty), "rate": flt(r.rate)} for r in so.items
			],
			"to_lines": lines,
			"removed": [code for code in current if code not in new_codes],
			"from_total": flt(so.grand_total),
			"to_total_before_discount": total,
		}
	return _workflow_preview_issues(name, args), None


def _workflow_preview_issues(name: str, args: dict) -> list[str]:
	"""Problems the apply would hit, surfaced at preview time so the agent can
	ask the user for them before requesting approval."""
	if name != "convert_lead":
		return []
	from erpnext.erpnext_integrations.ecommerce_api.preventa_api import LEAD_FIELD_MAP, _load_preventa_settings

	lead = args.get("lead")
	if not frappe.db.exists("Lead", lead):
		return [f"Lead {lead} not found"]
	provided = {k: args.get(k) for k in ("customer_type", "tax_id", "customer_group")}
	missing = []
	for fid in _load_preventa_settings().get("conversion_checklist") or []:
		if fid in provided:
			if not provided[fid]:
				missing.append(fid)
		elif not frappe.db.get_value("Lead", lead, LEAD_FIELD_MAP.get(fid, fid)):
			missing.append(fid)
	return [f"Missing required field(s) to convert: {', '.join(missing)}"] if missing else []


def _run_workflow(name: str, args: dict):
	if name == "create_order":
		from erpnext.erpnext_integrations.ecommerce_api.api import create_guest_preorder

		pl = cstr(args.get("price_list") or "").strip() or _default_selling_price_list()
		lines, _total, issues = _priced_lines(_order_lines_arg(args), pl)
		if issues:
			frappe.throw("; ".join(issues))
		items = [
			{
				"item_code": line["item_code"],
				"qty": line["qty"],
				"rate": line["rate"],
				**({"uom": src["uom"]} if src.get("uom") else {}),
			}
			for line, src in zip(lines, _order_lines_arg(args))
		]
		return create_guest_preorder(
			items,
			customer=cstr(args.get("customer") or "").strip() or None,
			price_list=pl,
			delivery_date=args.get("delivery_date") or None,
			guest_notes=args.get("notes") or None,
			initial_status=args.get("initial_status") or None,
			send_client_pin=0,
		)
	if name == "reprice_order":
		from erpnext.erpnext_integrations.ecommerce_api.api import reprice_guest_preorder_from_price_list

		so = _guest_preorder(cstr(args.get("name")))
		return reprice_guest_preorder_from_price_list(so.name, price_list=args.get("price_list") or None)
	if name == "update_order_lines":
		from erpnext.erpnext_integrations.ecommerce_api.api import update_guest_preorder_items

		so = _guest_preorder(cstr(args.get("name")))
		current = {row.item_code: flt(row.rate) for row in so.items}
		src_rows = _order_lines_arg(args)
		lines, _total, issues = _priced_lines(src_rows, so.selling_price_list or _default_selling_price_list(), current)
		if issues:
			frappe.throw("; ".join(issues))
		items = [
			{
				"item_code": line["item_code"],
				"qty": line["qty"],
				"rate": line["rate"],
				**({"uom": src["uom"]} if src.get("uom") else {}),
			}
			for line, src in zip(lines, src_rows)
		]
		return update_guest_preorder_items(
			so.name, items, additional_discount_amount=flt(args.get("additional_discount_amount") or 0)
		)
	if name == "set_preorder_status":
		from erpnext.erpnext_integrations.ecommerce_api.api import set_guest_preorder_status

		return set_guest_preorder_status(
			args.get("name"),
			args.get("target_status"),
			source=args.get("source"),
		)
	if name == "convert_lead":
		from erpnext.erpnext_integrations.ecommerce_api.preventa_api import convert_lead_to_customer

		return convert_lead_to_customer(
			args.get("lead"),
			customer_group=args.get("customer_group"),
			customer_type=args.get("customer_type"),
			tax_id=args.get("tax_id"),
		)
	if name == "assign_customer_seller":
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import _set_customer_salesmen_users

		return _set_customer_salesmen_users(args.get("customer"), args.get("sellers"))
	if name == "mark_stop_outcome":
		from erpnext.erpnext_integrations.ecommerce_api.tms_api import admin_record_stop_outcome

		allowed = {
			"trip_name",
			"stop_idx",
			"outcome",
			"recipient_name",
			"recipient_id_number",
			"signature_base64",
			"notes",
			"attempt_note",
			"lat",
			"lng",
			"amount_collected",
			"payment_method",
			"payments",
			"requires_factura_a",
			"credit_items",
			"clear_signature",
		}
		extra = set(args) - allowed
		if extra:
			frappe.throw(_("Unknown workflow args: {0}").format(", ".join(sorted(extra))))
		return admin_record_stop_outcome(**args)
	frappe.throw(_("Unknown workflow: {0}").format(name), frappe.DoesNotExistError)


@frappe.whitelist()
def mcp_list_workflows(mcp_token=None):
	"""List the domain workflows this key may run (gated by matrix edit)."""

	@_guard("list_workflows", payload={"token": "***"})
	def _impl(mcp_token):
		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		matrix = _matrix()
		out = []
		for wf in _workflow_catalog():
			entry = dict(wf)
			entry["allowed"] = bool((matrix.get(wf["doctype"]) or {}).get("edit"))
			out.append(entry)
		return {"workflows": out}

	return _impl(mcp_token)


@frappe.whitelist()
def mcp_run_workflow(mcp_token=None, workflow=None, args=None, confirm=0, preview_id=None):
	"""Run a named domain workflow. ``confirm=1`` is required — the assistant
	should first fetch the target record (mcp_get_record) and show the user
	what will change, then call with confirm=1."""

	@_guard(
		"run_workflow",
		payload=lambda: {"workflow": workflow, "args": args, "confirm": confirm},
	)
	def _impl(mcp_token, workflow, args, confirm, preview_id):
		ctx = _ctx(mcp_token, write=True)
		frappe.local._mcp_ctx = ctx
		workflow = str(workflow or "").strip()
		args = _parse_json(args, args)
		if not isinstance(args, dict):
			frappe.throw(_("args must be an object"))
		catalog = {wf["name"]: wf for wf in _workflow_catalog()}
		entry = catalog.get(workflow)
		if not entry:
			frappe.throw(_("Unknown workflow: {0}").format(workflow), frappe.DoesNotExistError)
		_can_edit(_matrix(), entry["doctype"])
		missing = [k for k in entry["args_schema"].get("required", []) if args.get(k) in (None, "")]
		if missing:
			frappe.throw(_("Missing required args: {0}").format(", ".join(missing)))
		signed = {"op": "workflow", "workflow": workflow, "args": args}
		issues, details = ([], None) if cint_confirm(confirm) else _workflow_preview(workflow, args)
		if not cint_confirm(confirm) and issues:
			return {
				"workflow": workflow,
				"args": _sanitize_output(_json_safe(args)),
				"requires_confirm": False,
				"applied": False,
				"issues": issues,
				**({"details": _sanitize_output(_json_safe(details))} if details else {}),
				"hint": "Fix these first (ask the user for the missing values), then preview again.",
			}
		if not cint_confirm(confirm):
			return {
				"workflow": workflow,
				"args": _sanitize_output(_json_safe(args)),
				**({"details": _sanitize_output(_json_safe(details))} if details else {}),
				"requires_confirm": True,
				"applied": False,
				"preview_id": _remember_preview(ctx, signed),
				"expires_in_sec": PREVIEW_TTL_SEC,
				"hint": (
					"Show the user the details (every line: item / qty / rate / amount, and the total) "
					"or the target record, then re-call with confirm=1 and this preview_id."
				),
			}
		_consume_preview(ctx, signed, preview_id)
		result = _run_workflow(workflow, args)
		return {"workflow": workflow, "applied": True, "result": _sanitize_output(_json_safe(result))}

	return _impl(mcp_token, workflow, args, confirm, preview_id)
