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

# DocTypes whose writes must go through a domain workflow (generic create /
# update would skip its rules) → the workflow to use instead.
WORKFLOW_ONLY_DOCTYPES = {"Company Archive Entry": "classify_document"}
# Generic *create* would leave half a record (a bare Driver has no Employee, so it
# never shows in Employees) — create through the workflow; edits stay generic.
CREATE_VIA_WORKFLOW = {"Driver": "create_driver", "Employee": "save_employee"}
# Rendered/validated by the React app (g015): layout JSON is only meaningful to
# the React designer, so generic create/update is refused → the React tools.
REACT_SURFACE_DOCTYPES = {
	"ECommerce Print Template": "preview_template_edit / apply_template_edit",
	"ECommerce Floor Map": "preview_section_edit / apply_section_edit",
}
# Fields whose generic patch is lossy: a child-table patch REPLACES the list, so
# adding members by patching Employee Group would drop everyone not echoed back.
FIELD_VIA_WORKFLOW = {("Employee Group", "employee_list"): "save_employee"}


def _require_generic_write(doctype: str, *, for_create: bool = False) -> None:
	tools = REACT_SURFACE_DOCTYPES.get(doctype)
	if tools:
		frappe.throw(_("{0} is edited only with the {1} tools").format(doctype, tools), frappe.PermissionError)
	workflow = WORKFLOW_ONLY_DOCTYPES.get(doctype)
	if workflow:
		frappe.throw(
			_("{0} is edited only through run_workflow('{1}')").format(doctype, workflow),
			frappe.PermissionError,
		)
	workflow = CREATE_VIA_WORKFLOW.get(doctype) if for_create else None
	if workflow:
		frappe.throw(
			_("Create {0} with run_workflow('{1}') — it applies the same defaults, groups and links as the app").format(
				doctype, workflow
			),
			frappe.PermissionError,
		)


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
	from erpnext.erpnext_integrations.ecommerce_api.mcp_keys_api import PSEUDO_DOCTYPES

	if doctype in PSEUDO_DOCTYPES:
		frappe.throw(
			_("{0} is a capability switch, not a record type — use the {1} tools").format(doctype, PSEUDO_DOCTYPES[doctype]),
			frappe.PermissionError,
		)
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
		workflow = FIELD_VIA_WORKFLOW.get((doctype, key))
		if workflow:
			frappe.throw(
				_("{0}.{1} is changed through run_workflow('{2}') (per employee, groups=[…])").format(
					doctype, key, workflow
				),
				frappe.PermissionError,
			)
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
		out = {"doctype": doctype, "name": name, "doc": _sanitize_output(_json_safe(doc.as_dict()))}
		if doctype == "Sales Order":
			view = _pedido_view(doc)
			if view:
				out["pedido_view"] = view
		return out

	return _impl(mcp_token, doctype, name)


def _pedido_view(so) -> dict | None:
	"""What the Órdenes pipeline shows for a Pedido, next to the raw ERPNext numbers.

	Órdenes soft-fills lines whose stored rate is 0 with the selling price list rate
	(``get_guest_preorder`` → ``lineEffectiveRate`` in the UI) and bills WEIGHT lines
	as $/kg × kg. The raw document keeps rate 0, so without this block an assistant
	reads ARS 0 where the user sees a real total.
	"""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_guest_preorder_estimated_total,
		_guest_preorder_line_amount,
		_is_guest_preorder_sales_order,
		get_item_prices_bulk,
	)

	if not _is_guest_preorder_sales_order(so):
		return None
	price_list = cstr(getattr(so, "selling_price_list", None)) or "Standard Selling"
	zero_codes = [d.item_code for d in (so.items or []) if flt(d.rate) <= 0 and d.item_code]
	list_rates = {}
	if zero_codes:
		try:
			list_rates = get_item_prices_bulk(zero_codes, price_list=price_list) or {}
		except Exception:
			list_rates = {}

	lines, unpriced, ui_total = [], [], 0.0
	for d in so.items or []:
		doc_amount = _guest_preorder_line_amount(d)
		if flt(d.rate) > 0:
			ui_rate, source, ui_amount = flt(d.rate), "document", doc_amount
		else:
			ui_rate = flt(list_rates.get(d.item_code) or 0)
			source = "price_list_fallback" if ui_rate > 0 else "unpriced"
			row = frappe._dict(d.as_dict())
			row.rate, row.amount = ui_rate, 0
			ui_amount = _guest_preorder_line_amount(row)
			unpriced.append(d.item_code)
		ui_total += ui_amount
		lines.append(
			{
				"idx": d.idx,
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": flt(d.qty),
				"uom": d.uom,
				"delivered_qty": flt(getattr(d, "delivered_qty", 0)),
				"document_rate": flt(d.rate),
				"document_amount": doc_amount,
				"shown_rate": ui_rate,
				"shown_amount": ui_amount,
				"rate_source": source,
			}
		)
	shown_total = max(ui_total - flt(getattr(so, "additional_discount_amount", None) or 0), 0.0)
	document_total = _guest_preorder_estimated_total(so)
	notes = []
	if unpriced:
		notes.append(
			f"{len(unpriced)} line(s) ({', '.join(unpriced)}) have rate 0 in the ERPNext document. "
			f"Órdenes displays the current '{price_list}' price for them (rate_source=price_list_fallback); "
			"that price is NOT saved, so prints/invoices from this document show 0 for those lines. "
			"This is expected for older consultas — not data loss. To persist the shown prices, "
			f"run_workflow('reprice_order', {{\"name\": \"{so.name}\"}}) (preview first)."
		)
	short = [l for l in lines if l["delivered_qty"] and l["delivered_qty"] < l["qty"]]
	if short:
		notes.append(
			"Partially delivered: "
			+ ", ".join(f"{l['item_code']} {l['delivered_qty']:g}/{l['qty']:g}" for l in short)
			+ " (delivered_qty from Delivery Notes vs ordered qty)."
		)
	return {
		"price_list": price_list,
		"shown_total": shown_total,
		"document_total": document_total,
		"currency": so.currency,
		"lines": lines,
		"notes": notes,
		"hint": "Report shown_total / shown_rate as what the user sees in Órdenes; "
		"document_* are the stored ERPNext values.",
	}


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


ARCHIVE_DOCTYPE = "Company Archive Entry"
TRIAGE_MAX_FILES = 5
TRIAGE_MAX_INLINE_BYTES = 5 * 1024 * 1024
TRIAGE_MAX_TEXT_CHARS = 20000
TRIAGE_MAX_PDF_PAGES = 30


def _archivo_kinds_for_triage() -> list[dict]:
	from erpnext.erpnext_integrations.ecommerce_api import archivo_api

	return [
		{"code": k["code"], "label_es": k["label_es"], "label_en": k["label_en"], "sync_template": k["sync_template"]}
		for k in archivo_api.list_archivo_kinds().get("kinds") or []
		if k.get("code") != archivo_api.INBOX_KIND
	]


@frappe.whitelist()
def mcp_list_document_inbox(mcp_token=None, status=None, limit=20, start=0):
	"""Documentos review inbox: ``queue`` (uploaded, unsorted), ``drafts``
	(classified, awaiting a person) or ``all``. Oldest first."""

	@_guard("document_inbox", doctype=ARCHIVE_DOCTYPE, payload=lambda: {"status": status})
	def _impl(mcp_token, status, limit, start):
		from erpnext.erpnext_integrations.ecommerce_api import archivo_api

		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		_can_view(_matrix(), ARCHIVE_DOCTYPE)
		out = archivo_api.list_archivo_inbox(
			status=status, limit=max(1, min(int(limit or 20), MAX_PAGE_LENGTH)), start=start
		)
		keep = (
			"name", "title", "kind", "inbox_status", "workflow_status", "posting_date", "party_type",
			"party", "amount", "currency", "attachment_count", "notes", "owner", "creation",
		)
		return {
			"rows": [{k: r.get(k) for k in keep} for r in out["rows"]],
			"total": out["total"],
			"counts": out["counts"],
			"kinds": _archivo_kinds_for_triage(),
			"hint": "get_document_for_triage(name) → read the files → run_workflow('classify_document', …)",
		}

	return _impl(mcp_token, status, limit, start)


def _pdf_text(content: bytes) -> tuple[str, int]:
	import io

	from pypdf import PdfReader

	reader = PdfReader(io.BytesIO(content))
	parts = []
	for page in reader.pages[:TRIAGE_MAX_PDF_PAGES]:
		try:
			parts.append(page.extract_text() or "")
		except Exception:
			parts.append("")
	return "\n\n".join(parts).strip(), len(reader.pages)


def _triage_file(row) -> dict:
	"""File metadata + extracted text, and the raw bytes for images/PDFs the
	assistant can read itself (scans have no text layer)."""
	import base64
	import mimetypes

	mime = mimetypes.guess_type(row.file_name or row.file_url or "")[0] or "application/octet-stream"
	info = {"file_name": row.file_name, "file_url": row.file_url, "mime_type": mime}
	try:
		frappe.flags.ignore_permissions = True
		content = frappe.get_doc("File", row.name).get_content()
	except Exception as e:
		return {**info, "error": f"could not read file: {e}"}
	finally:
		frappe.flags.ignore_permissions = False
	if isinstance(content, str):
		content = content.encode()
	info["size_bytes"] = len(content)
	if mime == "application/pdf":
		try:
			text, pages = _pdf_text(content)
			info["pages"] = pages
			info["text"] = text[:TRIAGE_MAX_TEXT_CHARS]
			info["text_truncated"] = len(text) > TRIAGE_MAX_TEXT_CHARS
		except Exception as e:
			info["text_error"] = str(e)[:200]
	elif mime.startswith("text/"):
		text = content.decode("utf-8", errors="replace")
		info["text"] = text[:TRIAGE_MAX_TEXT_CHARS]
		info["text_truncated"] = len(text) > TRIAGE_MAX_TEXT_CHARS
	if (mime.startswith("image/") or mime == "application/pdf") and len(content) <= TRIAGE_MAX_INLINE_BYTES:
		info["content_base64"] = base64.b64encode(content).decode()
	elif mime.startswith("image/") or mime == "application/pdf":
		info["content_omitted"] = f"larger than {TRIAGE_MAX_INLINE_BYTES // (1024 * 1024)} MB"
	return info


@frappe.whitelist()
def mcp_get_document_for_triage(mcp_token=None, name=None):
	"""One Documentos row + its files (PDF text, image/PDF bytes) + the kinds
	it can be classified as."""

	@_guard("document_triage", doctype=ARCHIVE_DOCTYPE, name=lambda: name)
	def _impl(mcp_token, name):
		from erpnext.erpnext_integrations.ecommerce_api import archivo_api

		ctx = _ctx(mcp_token)
		frappe.local._mcp_ctx = ctx
		_can_view(_matrix(), ARCHIVE_DOCTYPE)
		name = cstr(name).strip()
		if not name or not frappe.db.exists(ARCHIVE_DOCTYPE, name):
			frappe.throw(_("{0} {1} not found").format(ARCHIVE_DOCTYPE, name), frappe.DoesNotExistError)
		entry = archivo_api.get_archivo_entry(name)
		files = frappe.get_all(
			"File",
			filters={"attached_to_doctype": ARCHIVE_DOCTYPE, "attached_to_name": name},
			fields=["name", "file_name", "file_url"],
			order_by="creation asc",
			limit_page_length=TRIAGE_MAX_FILES,
			ignore_permissions=True,
		)
		return {
			"entry": _sanitize_output(_json_safe(entry)),
			"inbox_status": "queue" if entry.get("kind") == archivo_api.INBOX_KIND else entry.get("workflow_status"),
			"files": [_triage_file(f) for f in files],
			"kinds": _archivo_kinds_for_triage(),
			"classify_fields": list(archivo_api.CLASSIFY_FIELDS),
		}

	return _impl(mcp_token, name)


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
		_require_generic_write(doctype)
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
		_require_generic_write(doctype, for_create=True)
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


_CLASSIFY_SCHEMA = {
	"type": "object",
	"properties": {
		"name": {"type": "string", "description": "Documentos row (ARCH-…)"},
		"kind": {"type": "string", "description": "A code from kinds (required for queue rows)"},
		"title": {"type": "string"},
		"posting_date": {"type": "string", "description": "YYYY-MM-DD (document date)"},
		"due_date": {"type": "string"},
		"valid_from": {"type": "string"},
		"valid_to": {"type": "string"},
		"amount": {"type": "number"},
		"currency": {"type": "string"},
		"party_type": {"type": "string", "enum": ["Supplier", "Employee", "Other"]},
		"party": {"type": "string"},
		"payment_reference": {"type": "string", "description": "Invoice / receipt number"},
		"payment_method": {"type": "string", "enum": ["cash", "transfer", "card", "mp", "other"]},
		"notes": {"type": "string"},
		"related_refs": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {"link_doctype": {"type": "string"}, "link_name": {"type": "string"}},
			},
		},
	},
	"required": ["name"],
}


def _workflow_catalog() -> list[dict]:
	return [
		{
			"name": "classify_document",
			"doctype": ARCHIVE_DOCTYPE,
			"description": (
				"Classify a Documentos inbox row (queue or draft): kind, title, dates, amount, party, "
				"reference, notes, related tags. The row always stays a DRAFT — a person confirms it in "
				"Revisar → Documentos; nothing is paid or posted (Contabilizar) from here."
			),
			"args_schema": _CLASSIFY_SCHEMA,
		},
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
			"name": "create_driver",
			"doctype": "Driver",
			"description": (
				"Create a driver the way the dispatcher does: an Employee (listed in Employees, "
				"group driver) plus the linked Driver used for trips. Pass driver=<HR-DRI-…> instead "
				"of creating to attach an Employee to an existing Driver that has none."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"full_name": {"type": "string", "description": "First and last name"},
					"cell_number": {"type": "string"},
					"company": {"type": "string"},
					"driver": {"type": "string", "description": "Existing Driver with no Employee to repair"},
				},
				"required": [],
			},
		},
		{
			"name": "save_employee",
			"doctype": "Employee",
			"description": (
				"Create an employee (no name) or update one (name=HR-EMP-…) through the Employees page "
				"save: defaults for mandatory HR fields, a new ops PIN, and staff groups by title "
				"(ventas / sales, caja, repositor, driver, admin). groups REPLACES that employee's "
				"groups — the preview shows current → new. Does not create a login. For a driver use "
				"create_driver (it also creates the Driver record)."
			),
			"args_schema": {
				"type": "object",
				"properties": {
					"name": {"type": "string", "description": "Existing Employee to update; omit to create"},
					"first_name": {"type": "string"},
					"last_name": {"type": "string"},
					"employee_name": {"type": "string", "description": "Full name (alternative to first/last)"},
					"groups": {"type": "array", "items": {"type": "string"}},
					"cell_number": {"type": "string"},
					"designation": {"type": "string"},
					"company": {"type": "string"},
					"status": {"type": "string", "enum": ["Active", "Inactive", "Left"]},
				},
				"required": [],
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


def _driver_args(args: dict) -> tuple[str | None, str, list[str]]:
	"""(existing driver to repair | None, full_name, issues) for create_driver."""
	issues = []
	driver = cstr(args.get("driver") or "").strip() or None
	full_name = " ".join(cstr(args.get("full_name") or "").split())
	if driver:
		row = frappe.db.get_value("Driver", driver, ["full_name", "employee"], as_dict=True)
		if not row:
			issues.append(f"Driver {driver} not found")
		elif row.employee:
			issues.append(f"Driver {driver} is already linked to Employee {row.employee}")
		else:
			full_name = full_name or cstr(row.full_name).strip()
	elif not full_name:
		issues.append("full_name is required (or driver=<existing Driver> to repair)")
	elif frappe.db.exists("Driver", {"full_name": full_name}):
		issues.append(f"A driver named {full_name} already exists (search_records doctype=Driver)")
	company = cstr(args.get("company") or "").strip()
	if company and not frappe.db.exists("Company", company):
		issues.append(f"Company {company} not found")
	return driver, full_name, issues


_EMPLOYEE_ARGS = {"name", "first_name", "last_name", "employee_name", "groups", "cell_number", "designation", "company", "status"}


def _employee_groups_of(employee: str) -> list[str]:
	return sorted(
		set(
			frappe.get_all(
				"Employee Group Table", filters={"employee": employee}, pluck="parent", ignore_permissions=True
			)
		)
	)


def _resolve_staff_groups(raw) -> tuple[list[str], list[str]]:
	"""Group titles/names → Employee Group names (same aliases as the Employees page)."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _find_employee_group_by_title

	alias = {"sales": "ventas", "vendedor": "ventas", "vendedores": "ventas", "chofer": "driver"}
	resolved, unknown = [], []
	for g in _parse_json(raw, raw) or []:
		title = cstr(g).strip()
		if not title:
			continue
		lookup = alias.get(title.lower(), title)
		hit = (
			(frappe.db.exists("Employee Group", title) and title)
			or _find_employee_group_by_title(lookup)
			or (frappe.db.exists("Employee Group", lookup) and lookup)
		)
		(resolved if hit else unknown).append(hit or title)
	return sorted(set(resolved)), unknown


def _employee_args(args: dict) -> tuple[list[str], dict]:
	"""(issues, details) for save_employee — shared by preview and apply."""
	issues = []
	extra = set(args) - _EMPLOYEE_ARGS
	if extra:
		issues.append(f"Unknown args: {', '.join(sorted(extra))}")
	name = cstr(args.get("name") or "").strip() or None
	full = " ".join(
		(cstr(args.get("employee_name")) or f"{cstr(args.get('first_name'))} {cstr(args.get('last_name'))}").split()
	)
	details = {"action": "update" if name else "create", "employee": name, "employee_name": full or None}
	if name:
		if not frappe.db.exists("Employee", name):
			issues.append(f"Employee {name} not found")
		else:
			details["employee_name"] = full or frappe.db.get_value("Employee", name, "employee_name")
			details["current_groups"] = _employee_groups_of(name)
	elif not full:
		issues.append("first_name / employee_name is required to create an employee")
	elif frappe.db.exists("Employee", {"employee_name": full, "status": "Active"}):
		issues.append(f"An active employee named {full} already exists — pass name=HR-EMP-… to update it")
	if "groups" in args:
		groups, unknown = _resolve_staff_groups(args.get("groups"))
		if unknown:
			issues.append(f"Unknown group(s): {', '.join(unknown)} (list_records doctype=Employee Group)")
		details["groups"] = groups
	company = cstr(args.get("company") or "").strip()
	if company and not frappe.db.exists("Company", company):
		issues.append(f"Company {company} not found")
	for key in ("cell_number", "designation", "company", "status"):
		if args.get(key) not in (None, ""):
			details[key] = args.get(key)
	if not name:
		details["also"] = "new ops PIN; HR defaults (gender, date_of_birth, date_of_joining=today)"
	return issues, details


def _workflow_preview(name: str, args: dict) -> tuple[list[str], dict | None]:
	"""(issues, details) shown at preview time so the user sees exactly what applies."""
	if name == "save_employee":
		return _employee_args(args)
	if name == "create_driver":
		driver, full_name, issues = _driver_args(args)
		company = cstr(args.get("company") or "").strip() or frappe.defaults.get_user_default("Company")
		return issues, {
			"action": "link_employee_to_existing_driver" if driver else "create_employee_and_driver",
			"driver": driver,
			"employee_name": full_name,
			"company": company,
			"employee_group": "driver",
			"cell_number": cstr(args.get("cell_number") or "").strip() or None,
		}
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
	if name == "classify_document":
		from erpnext.erpnext_integrations.ecommerce_api import archivo_api

		changes = {k: v for k, v in args.items() if k != "name"}
		try:
			archivo_api.validate_archivo_classification(args.get("name"), changes)
		except frappe.DoesNotExistError:
			raise
		except Exception as e:
			return [cstr(e)], None
		current = archivo_api.get_archivo_entry(args.get("name"))
		diff = [
			{"field": k, "from": _json_safe(current.get(k)), "to": _json_safe(v)}
			for k, v in changes.items()
			if _json_safe(current.get(k)) != _json_safe(v)
		]
		if current.get("workflow_status") != "draft":
			diff.append({"field": "workflow_status", "from": current.get("workflow_status"), "to": "draft"})
		return [], {
			"document": args.get("name"),
			"title": current.get("title"),
			"changes": diff,
			"after_apply": "Stays a draft (Borrador) until a person confirms it in Revisar → Documentos.",
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


def _run_create_driver(args: dict) -> dict:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _set_employee_groups
	from erpnext.erpnext_integrations.ecommerce_api.tms_api import (
		_ensure_driver_staff_group,
		_insert_driver_with_employee,
		_insert_employee_for_driver,
	)

	driver, full_name, issues = _driver_args(args)
	if issues:
		frappe.throw("; ".join(issues))
	company = cstr(args.get("company") or "").strip() or None
	cell = cstr(args.get("cell_number") or "").strip() or None
	if driver:
		emp = _insert_employee_for_driver(full_name, company=company)
		frappe.db.set_value("Driver", driver, "employee", emp.name)
		if cell:
			frappe.db.set_value("Driver", driver, "cell_number", cell)
		emp_name = emp.name
	else:
		doc = _insert_driver_with_employee(full_name, cell_number=cell, company=company)
		driver, emp_name = doc.name, doc.employee
	group = _ensure_driver_staff_group()
	if group:
		_set_employee_groups(emp_name, [group])
	frappe.db.commit()
	return {"driver": driver, "employee": emp_name, "full_name": full_name, "employee_group": group}


def _run_save_employee(args: dict) -> dict:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import save_employee

	issues, details = _employee_args(args)
	if issues:
		frappe.throw("; ".join(issues))
	data = {k: args[k] for k in ("first_name", "last_name", "employee_name", "cell_number", "designation", "company", "status") if args.get(k) not in (None, "")}
	if "groups" in details:
		data["groups"] = details["groups"]
	res = save_employee(name=details["employee"], data=data)
	emp = (res or {}).get("employee") or {}
	emp_name = emp.get("name") or details["employee"]
	return {
		"employee": emp_name,
		"employee_name": emp.get("employee_name") or details["employee_name"],
		"groups": _employee_groups_of(emp_name) if emp_name else [],
		"created": not details["employee"],
	}


def _run_workflow(name: str, args: dict):
	if name == "create_driver":
		return _run_create_driver(args)
	if name == "save_employee":
		return _run_save_employee(args)
	if name == "classify_document":
		from erpnext.erpnext_integrations.ecommerce_api.archivo_api import classify_archivo_entry

		return classify_archivo_entry(args.get("name"), {k: v for k, v in args.items() if k != "name"})
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


# ---------------------------------------------------------------------------
# Playbook feedback (mcp-erp playbooks.csv improvement loop)
# ---------------------------------------------------------------------------

PLAYBOOK_FEEDBACK_SCOPE = "mcp.playbook_feedback"
PLAYBOOK_FEEDBACK_KINDS = {"correction", "step_failed", "missing_process", "unclear_rule", "worked_well"}
PLAYBOOK_FEEDBACK_KEEP = 500


@frappe.whitelist()
def mcp_playbook_feedback(
	mcp_token=None, process_id=None, what_happened=None, suggestion=None, step=None, kind=None, user_quote=None
):
	"""Store one piece of playbook feedback from the assistant (no ERP data is
	changed, so no preview). Pulled into mcp-erp playbooks/feedback.csv by
	``mcp-erp/scripts/pull_playbook_feedback.py`` for a human to apply."""

	@_guard("playbook_feedback", payload=lambda: {"process_id": process_id, "kind": kind})
	def _impl():
		ctx = _ctx(mcp_token, write=True)
		frappe.local._mcp_ctx = ctx
		what = cstr(what_happened).strip()
		if not what:
			frappe.throw(_("what_happened is required"))
		k = cstr(kind).strip() or "correction"
		if k not in PLAYBOOK_FEEDBACK_KINDS:
			frappe.throw(_("kind must be one of: {0}").format(", ".join(sorted(PLAYBOOK_FEEDBACK_KINDS))))
		row = {
			"id": secrets.token_hex(6),
			"ts": _now_iso(),
			"site": frappe.local.site,
			"actor": ctx.get("actor"),
			"key_id": ctx.get("key_id"),
			"process_id": cstr(process_id).strip()[:20] or "NEW",
			"step": cstr(step).strip()[:10],
			"kind": k,
			"what_happened": what[:2000],
			"suggestion": cstr(suggestion).strip()[:2000],
			"user_quote": cstr(user_quote).strip()[:1000],
		}
		frappe.get_doc(
			{
				"doctype": "Table Extra Data",
				"scope": PLAYBOOK_FEEDBACK_SCOPE,
				"row_key": f"{row['ts']}|{row['id']}",
				"data_json": json.dumps(row, ensure_ascii=False),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return {"recorded": True, "id": row["id"]}

	return _impl()


def export_playbook_feedback(since=None) -> list[dict]:
	"""bench execute target for the pull script (not whitelisted). Prunes to the newest
	PLAYBOOK_FEEDBACK_KEEP rows so the store stays small once pulled."""
	filters = {"scope": PLAYBOOK_FEEDBACK_SCOPE}
	if since:
		filters["creation"] = [">", since]
	rows = frappe.get_all(
		"Table Extra Data",
		filters=filters,
		fields=["name", "data_json"],
		order_by="creation asc",
		limit_page_length=0,
		ignore_permissions=True,
	)
	out = [_parse_json(r.data_json, {}) for r in rows]
	total = frappe.db.count("Table Extra Data", {"scope": PLAYBOOK_FEEDBACK_SCOPE})
	if total > PLAYBOOK_FEEDBACK_KEEP:
		for name in frappe.get_all(
			"Table Extra Data",
			filters={"scope": PLAYBOOK_FEEDBACK_SCOPE},
			pluck="name",
			order_by="creation asc",
			limit_page_length=total - PLAYBOOK_FEEDBACK_KEEP,
			ignore_permissions=True,
		):
			frappe.delete_doc("Table Extra Data", name, ignore_permissions=True, force=True)
		frappe.db.commit()
	return out


# ---------------------------------------------------------------------------
# React-hosted MCP surfaces (g015): the React app verifies the same mcp_ token
# here and keeps using this preview store, so the contract stays server-side.
# ---------------------------------------------------------------------------

REACT_SURFACES = {"print", "templates", "sections", "labels", "migrate"}


def _react_surface(surface) -> str:
	surface = cstr(surface).strip()
	if surface not in REACT_SURFACES:
		frappe.throw(_("Unknown React MCP surface: {0}").format(surface))
	return surface


@frappe.whitelist()
def mcp_react_session(mcp_token=None, surface=None, write=0):
	"""Verify an mcp_ token for a React surface → who is acting and what they may touch."""

	@_guard("react_session", payload=lambda: {"surface": surface, "write": write})
	def _impl():
		sfc = _react_surface(surface)
		ctx = _ctx(mcp_token, write=cint_confirm(write))
		frappe.local._mcp_ctx = ctx
		matrix = _matrix()
		return {
			"site": frappe.local.site,
			"surface": sfc,
			"actor": ctx["actor"],
			"key_id": ctx["key_id"],
			"matrix": {dt: {"view": bool(v.get("view")), "edit": bool(v.get("edit"))} for dt, v in matrix.items()},
		}

	return _impl()


def _react_preview_payload(surface: str, payload) -> dict:
	data = _parse_json(payload, payload)
	if not isinstance(data, dict):
		frappe.throw(_("payload must be an object"))
	# Namespaced so a React preview can never satisfy a gateway apply (or vice versa).
	return {"op": f"react:{surface}", "payload": data}


@frappe.whitelist()
def mcp_preview_remember(mcp_token=None, surface=None, payload=None):
	"""Store a React surface preview; returns the single-use preview_id."""

	@_guard("react_preview", payload=lambda: {"surface": surface})
	def _impl():
		sfc = _react_surface(surface)
		ctx = _ctx(mcp_token, write=True)
		frappe.local._mcp_ctx = ctx
		return {"preview_id": _remember_preview(ctx, _react_preview_payload(sfc, payload)), "expires_in_sec": PREVIEW_TTL_SEC}

	return _impl()


@frappe.whitelist()
def mcp_preview_consume(mcp_token=None, surface=None, payload=None, preview_id=None):
	"""Consume a React surface preview (identical payload, single use) before its apply."""

	@_guard("react_apply", payload=lambda: {"surface": surface})
	def _impl():
		sfc = _react_surface(surface)
		ctx = _ctx(mcp_token, write=True)
		frappe.local._mcp_ctx = ctx
		_consume_preview(ctx, _react_preview_payload(sfc, payload), preview_id)
		return {"ok": True, "actor": ctx["actor"]}

	return _impl()
