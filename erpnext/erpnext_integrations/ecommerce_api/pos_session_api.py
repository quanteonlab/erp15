"""POS cash sessions, admin PIN, sale amend + stock reconciliation audit."""

from __future__ import annotations

import hashlib
import json
import secrets

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, nowdate, nowtime

from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
	_items_for_invoices,
	_parse_invoice_remarks,
	_payments_for_invoices,
)

PIN_SCOPE = "settings.pos_admin"
CASH_MOP_HINTS = ("cash", "efectivo")

# Actions governed by the C1 per-action PIN/comment policy matrix. "start" is
# intentionally excluded — it keeps its own dedicated start_requires_pin flag.
_POLICY_ACTIONS = ("cancel", "edit", "return_items", "opening", "cashier", "close")

# Per-action defaults chosen to match today's *actual* enforced behavior exactly,
# so installs see zero change until an admin edits the matrix:
# - cancel/edit/opening/cashier already unconditionally require PIN and follow the
#   legacy global amendment_note_required toggle for the comment.
# - close already unconditionally requires PIN but has never required a comment.
# - return_items is new; both are required by default (destructive-ish, stock+cash).
_STATIC_POLICY_DEFAULTS = {
	"close": {"requires_comment": False},
	"return_items": {"requires_comment": True},
}


def _load_action_policy(cfg: dict) -> dict:
	stored = cfg.get("action_policy")
	stored = stored if isinstance(stored, dict) else {}
	legacy_note_required = cfg.get("amendment_note_required")
	if legacy_note_required is None:
		legacy_note_required = True
	out = {}
	for action in _POLICY_ACTIONS:
		default_comment = _STATIC_POLICY_DEFAULTS.get(action, {}).get("requires_comment", bool(legacy_note_required))
		entry = stored.get(action) if isinstance(stored.get(action), dict) else {}
		out[action] = {
			"requires_pin": bool(entry.get("requires_pin", True)),
			"requires_comment": bool(entry.get("requires_comment", default_comment)),
		}
	return out


def _acting_user() -> str:
	try:
		header = frappe.get_request_header("X-ERP-Acting-User")
	except RuntimeError:
		header = None
	return (header or "").strip() or frappe.session.user


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _load_pin_settings() -> dict:
	if not frappe.db.exists("Table Extra Schema", PIN_SCOPE):
		return {}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", PIN_SCOPE)
	data = _parse_json(doc.columns_json, {})
	return data if isinstance(data, dict) else {}


def _save_pin_settings(data: dict) -> None:
	payload = json.dumps(data or {}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", PIN_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", PIN_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": PIN_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _hash_pin(pin: str, salt: str) -> str:
	return hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def _pin_configured(cfg: dict | None = None) -> bool:
	cfg = cfg if cfg is not None else _load_pin_settings()
	if cfg.get("pin_hash") and cfg.get("pin_salt"):
		return True
	return bool(frappe.conf.get("pos_manager_pin"))


def _verify_pin_value(pin: str) -> bool:
	if not pin:
		return False
	cfg = _load_pin_settings()
	if cfg.get("pin_hash") and cfg.get("pin_salt"):
		return secrets.compare_digest(_hash_pin(str(pin), cfg["pin_salt"]), str(cfg["pin_hash"]))
	expected = frappe.conf.get("pos_manager_pin")
	if expected:
		return str(pin) == str(expected)
	return False


def _require_pin_or_admin(pin: str, action: str | None = None) -> dict:
	"""PIN required when configured and the action's policy requires it. When the
	policy explicitly disables the PIN for this action, any acting user is
	authorized outright (matches the settings description: "works without PIN").
	If not configured (and no policy override), only desk admins may amend."""
	cfg = _load_pin_settings()
	acting = _acting_user()
	if action is not None and not _load_action_policy(cfg).get(action, {}).get("requires_pin", True):
		return {"authorized": True, "admin_user": acting, "pin_used": False}
	if _pin_configured(cfg):
		if not _verify_pin_value(pin):
			frappe.throw(_("Invalid admin PIN"))
		return {"authorized": True, "admin_user": acting, "pin_used": True}
	roles = frappe.get_roles(acting) if acting else []
	if "Administrator" in roles or "System Manager" in roles:
		return {"authorized": True, "admin_user": acting, "pin_used": False}
	frappe.throw(_("Set an admin PIN in Settings before amending POS sales."))


def _can_manage_settings() -> bool:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app

	return _can_app("tools.settings")


def _append_audit(doc, event: str, extra: dict | None = None) -> None:
	log = _parse_json(doc.audit_log_json, [])
	if not isinstance(log, list):
		log = []
	entry = {
		"at": str(now_datetime()),
		"event": event,
		"acting_user": _acting_user(),
		**(extra or {}),
	}
	log.append(entry)
	doc.audit_log_json = json.dumps(log, ensure_ascii=False)


def _is_cash_mop(name: str | None) -> bool:
	s = (name or "").strip().lower()
	return any(h in s for h in CASH_MOP_HINTS)


def _sale_cash_in_and_change(sale: dict) -> tuple[float, float]:
	cash_in = 0.0
	for p in sale.get("payments") or []:
		if _is_cash_mop(p.get("mode_of_payment")):
			cash_in += flt(p.get("amount"))
	received = flt(sale.get("cash_received") or 0)
	change = max(0.0, received - cash_in) if received else 0.0
	return cash_in, change


def _session_sales(session_name: str) -> list[dict]:
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"remarks": ("like", f"%pos_session:{session_name}%"), "docstatus": 1},
		fields=["name", "posting_date", "posting_time", "grand_total", "outstanding_amount", "remarks", "creation"],
		order_by="creation asc",
		ignore_permissions=True,
	)
	names = [r.name for r in invoices]
	pay_map = _payments_for_invoices(names)
	item_map = _items_for_invoices(names)
	sales = []
	for inv in invoices:
		meta = _parse_invoice_remarks(inv.remarks)
		payments = pay_map.get(inv.name) or []
		if not payments and meta.get("payments_label"):
			payments = [
				{
					"payment_id": None,
					"mode_of_payment": meta["payments_label"],
					"amount": flt(inv.grand_total),
				}
			]
		sales.append(
			{
				"name": inv.name,
				"posting_date": str(inv.posting_date) if inv.posting_date else None,
				"posting_time": str(inv.posting_time) if inv.posting_time else None,
				"grand_total": flt(inv.grand_total),
				"outstanding_amount": flt(inv.outstanding_amount),
				"receipt": meta.get("receipt") or inv.name,
				"cashier": meta.get("cashier"),
				"device": meta.get("device"),
				"sale_mode": meta.get("sale_mode"),
				"cash_received": meta.get("cash_received"),
				"payments": payments,
				"items": item_map.get(inv.name) or [],
			}
		)
	return sales


def _cash_expected(doc, sales: list[dict] | None = None) -> dict:
	sales = sales if sales is not None else _session_sales(doc.name)
	cash_in = 0.0
	change_out = 0.0
	for s in sales:
		cin, chg = _sale_cash_in_and_change(s)
		cash_in += cin
		change_out += chg
	opening = flt(doc.opening_cash)
	return {
		"opening_cash": opening,
		"cash_in": cash_in,
		"change_out": change_out,
		"expected_cash": opening + cash_in - change_out,
	}


def _serialize_session(doc, include_sales=False) -> dict:
	sales = _session_sales(doc.name) if include_sales else []
	# Pass [] (not None) when skipping sales — None would re-trigger a full invoice scan.
	cash = _cash_expected(doc, sales)
	out = {
		"name": doc.name,
		"pos_profile": doc.pos_profile,
		"warehouse": doc.warehouse,
		"company": doc.company,
		"status": doc.status,
		"cashier_user": doc.cashier_user,
		"opening_cash": flt(doc.opening_cash),
		"started_at": str(doc.started_at) if doc.started_at else None,
		"ended_at": str(doc.ended_at) if doc.ended_at else None,
		"is_open": 1 if doc.status == "Open" else 0,
		**cash,
		"sales_count": len(sales) if include_sales else None,
		"total_sale": flt(sum(s["grand_total"] for s in sales)) if include_sales else None,
	}
	if include_sales:
		out["sales"] = sales
		out["audit_log"] = _parse_json(doc.audit_log_json, [])
	return out


def _open_session_for_profile(pos_profile: str):
	name = frappe.db.get_value(
		"POS Cash Session",
		{"pos_profile": pos_profile, "status": "Open"},
		"name",
	)
	if not name:
		return None
	frappe.flags.ignore_permissions = True
	return frappe.get_doc("POS Cash Session", name)


def _get_bin_qty_rate(item_code: str, warehouse: str) -> tuple[float, float]:
	row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "valuation_rate"],
		as_dict=True,
	)
	if not row:
		rate = flt(frappe.db.get_value("Item", item_code, "valuation_rate") or 0)
		return 0.0, rate
	return flt(row.actual_qty), flt(row.valuation_rate or 0)


def _stock_preview_for_deltas(deltas: dict[tuple[str, str], float]) -> list[dict]:
	"""deltas keyed (item_code, warehouse) = qty to ADD to warehouse (negative = remove)."""
	rows = []
	for (item_code, warehouse), delta in deltas.items():
		if not item_code or not warehouse or abs(flt(delta)) < 1e-9:
			continue
		current, rate = _get_bin_qty_rate(item_code, warehouse)
		item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
		new_qty = current + flt(delta)
		rows.append(
			{
				"item_code": item_code,
				"item_name": item_name,
				"warehouse": warehouse,
				"current_qty": current,
				"qty_delta": flt(delta),
				"new_qty": new_qty,
				"valuation_rate": rate,
			}
		)
	return rows


def _invoice_stock_deltas(items: list, multiplier: float = 1.0) -> dict[tuple[str, str], float]:
	out: dict[tuple[str, str], float] = {}
	for it in items or []:
		code = it.get("item_code")
		wh = it.get("warehouse")
		qty = flt(it.get("qty"))
		if not code or not wh:
			continue
		key = (code, wh)
		out[key] = out.get(key, 0.0) + (qty * multiplier)
	return out


def _submit_stock_reconciliation(preview_rows: list, note: str, trace: dict) -> str | None:
	if not preview_rows:
		return None
	company = preview_rows and frappe.db.get_value("Warehouse", preview_rows[0]["warehouse"], "company")
	if not company:
		company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	expense = frappe.db.get_value("Company", company, "stock_adjustment_account")
	cost_center = frappe.db.get_value("Company", company, "cost_center")
	items = []
	for r in preview_rows:
		# Re-read qty after invoice cancel so we set the *intended* new qty from original preview.
		_cur, rate = _get_bin_qty_rate(r["item_code"], r["warehouse"])
		target = flt(r["new_qty"])
		val = flt(r.get("valuation_rate") or rate or 0)
		items.append(
			{
				"item_code": r["item_code"],
				"warehouse": r["warehouse"],
				"qty": target,
				"valuation_rate": val if val > 0 else 0.0001,
				"allow_zero_valuation_rate": 1 if val <= 0 else 0,
			}
		)
	doc = frappe.get_doc(
		{
			"doctype": "Stock Reconciliation",
			"purpose": "Stock Reconciliation",
			"purpose_note": (note or "")[:1000],
			"traceability_json": json.dumps(trace, ensure_ascii=False),
			"company": company,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"set_posting_time": 1,
			"expense_account": expense,
			"cost_center": cost_center,
			"items": items,
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _cancel_invoice_and_payments(invoice_name: str) -> None:
	pay_map = _payments_for_invoices([invoice_name])
	for p in pay_map.get(invoice_name) or []:
		pid = p.get("payment_id")
		if not pid:
			continue
		pe = frappe.get_doc("Payment Entry", pid)
		if cint(pe.docstatus) == 1:
			pe.cancel()
	inv = frappe.get_doc("Sales Invoice", invoice_name)
	if cint(inv.docstatus) == 1:
		inv.cancel()


def _require_note(note: str, action: str | None = None, items=None) -> str:
	note = (note or "").strip()
	cfg = _load_pin_settings()
	if action is not None:
		required = _load_action_policy(cfg).get(action, {}).get("requires_comment", True)
	else:
		required = cfg.get("amendment_note_required")
		if required is None:
			required = True
	# Per-item return reasons can satisfy the comment requirement for Devolver.
	if required and not note and action == "return_items":
		has_item_reason = False
		for it in items or []:
			if isinstance(it, dict) and str(it.get("return_reason") or "").strip():
				has_item_reason = True
				break
		if has_item_reason:
			required = False
	if required and not note:
		frappe.throw(_("A note is required for this amendment."))
	return note


def _ensure_pos_return_qty_field() -> bool:
	from erpnext.erpnext_integrations.ecommerce_api.product_manager import (
		ensure_product_manager_custom_fields,
	)

	if frappe.db.has_column("Item", "custom_pos_return_qty"):
		return True
	ensure_product_manager_custom_fields()
	return bool(frappe.db.has_column("Item", "custom_pos_return_qty"))


def _record_item_pos_return(item_code: str, qty_returned: float, reason: str, invoice_name: str) -> None:
	"""Append return reason to Item notes and bump cumulative return qty."""
	code = (item_code or "").strip()
	qty = flt(qty_returned)
	if not code or qty <= 0 or not frappe.db.exists("Item", code):
		return
	_ensure_pos_return_qty_field()
	stamp = str(now_datetime())[:16]
	reason_txt = (reason or "").strip()
	line = f"[{stamp}] Devolución POS x{qty:g}"
	if reason_txt:
		line += f": {reason_txt}"
	line += f" ({invoice_name})"

	if frappe.db.has_column("Item", "custom_review_notes"):
		existing = frappe.db.get_value("Item", code, "custom_review_notes") or ""
		combined = f"{line}\n{existing}".strip() if str(existing).strip() else line
		# Small Text is short; keep newest notes first.
		if len(combined) > 500:
			combined = combined[:500]
		frappe.db.set_value("Item", code, "custom_review_notes", combined, update_modified=False)

	if frappe.db.has_column("Item", "custom_pos_return_qty"):
		prev = flt(frappe.db.get_value("Item", code, "custom_pos_return_qty") or 0)
		frappe.db.set_value("Item", code, "custom_pos_return_qty", prev + qty, update_modified=False)


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_pos_admin_settings():
	cfg = _load_pin_settings()
	required = cfg.get("amendment_note_required")
	if required is None:
		required = True
	mode = cfg.get("session_mode") or "autostart"
	if mode not in ("autostart", "require_before_sale"):
		mode = "autostart"
	default_profile = str(cfg.get("default_pos_profile") or "").strip()
	opening = cfg.get("default_opening_cash")
	if opening is None or opening == "":
		opening = 5000
	orders_visibility_mode = cfg.get("orders_visibility_mode") or "own_only"
	if orders_visibility_mode not in ("own_only", "group", "all_tagged"):
		orders_visibility_mode = "own_only"
	return {
		"pin_configured": _pin_configured(cfg),
		"amendment_note_required": bool(required),
		"session_mode": mode,
		"start_requires_pin": bool(cint(cfg.get("start_requires_pin") or 0)),
		"default_pos_profile": default_profile or None,
		"default_opening_cash": flt(opening),
		"orders_visibility_mode": orders_visibility_mode,
		"action_policy": _load_action_policy(cfg),
		# Per-line return reason on Devolver (default on).
		"return_reason_per_item": bool(cfg.get("return_reason_per_item", True)),
	}


@frappe.whitelist()
def save_pos_admin_settings(
	pin=None,
	amendment_note_required=None,
	clear_pin=0,
	session_mode=None,
	start_requires_pin=None,
	default_pos_profile=None,
	default_opening_cash=None,
	action_policy=None,
	orders_visibility_mode=None,
	return_reason_per_item=None,
):
	if not _can_manage_settings():
		frappe.throw(_("Not permitted ({0})").format("tools.settings"))
	cfg = _load_pin_settings()
	if cint(clear_pin):
		cfg.pop("pin_hash", None)
		cfg.pop("pin_salt", None)
	elif pin not in (None, ""):
		pin = str(pin).strip()
		if len(pin) < 4:
			frappe.throw(_("Admin PIN must be at least 4 characters"))
		salt = secrets.token_hex(16)
		cfg["pin_salt"] = salt
		cfg["pin_hash"] = _hash_pin(pin, salt)
	if amendment_note_required is not None:
		cfg["amendment_note_required"] = bool(cint(amendment_note_required))
	if session_mode is not None:
		mode = str(session_mode).strip()
		if mode not in ("autostart", "require_before_sale"):
			frappe.throw(_("Invalid session mode"))
		cfg["session_mode"] = mode
	if start_requires_pin is not None:
		cfg["start_requires_pin"] = bool(cint(start_requires_pin))
	if default_pos_profile is not None:
		name = str(default_pos_profile).strip()
		if name and not frappe.db.exists("POS Profile", name):
			frappe.throw(_("Cash register {0} not found").format(name))
		cfg["default_pos_profile"] = name
	if default_opening_cash is not None:
		cfg["default_opening_cash"] = max(0.0, flt(default_opening_cash))
	if orders_visibility_mode is not None:
		mode = str(orders_visibility_mode).strip()
		if mode not in ("own_only", "group", "all_tagged"):
			frappe.throw(_("Invalid orders visibility mode"))
		cfg["orders_visibility_mode"] = mode
	if return_reason_per_item is not None:
		cfg["return_reason_per_item"] = bool(cint(return_reason_per_item))
	if action_policy is not None:
		if isinstance(action_policy, str):
			action_policy = json.loads(action_policy)
		if not isinstance(action_policy, dict):
			frappe.throw(_("Invalid action policy"))
		current = cfg.get("action_policy") if isinstance(cfg.get("action_policy"), dict) else {}
		for action, entry in action_policy.items():
			if action not in _POLICY_ACTIONS or not isinstance(entry, dict):
				continue
			merged = dict(current.get(action) or {})
			if "requires_pin" in entry:
				merged["requires_pin"] = bool(cint(entry["requires_pin"]))
			if "requires_comment" in entry:
				merged["requires_comment"] = bool(cint(entry["requires_comment"]))
			current[action] = merged
		cfg["action_policy"] = current
	_save_pin_settings(cfg)
	return get_pos_admin_settings()


@frappe.whitelist(allow_guest=True)
def validate_admin_pin(pin):
	ok = _verify_pin_value(pin) if pin else False
	if not ok and not _pin_configured():
		# Open mode: treat as authorized only for desk admins (checked later on write).
		ok = False
	return {"authorized": bool(ok and _verify_pin_value(pin)), "pin_configured": _pin_configured()}


@frappe.whitelist()
def list_pos_cash_sessions(pos_profile=None, status=None, page=1, page_length=50):
	filters = {}
	if pos_profile:
		filters["pos_profile"] = pos_profile
	if status:
		filters["status"] = status
	page = max(cint(page) or 1, 1)
	page_length = min(max(cint(page_length) or 50, 1), 200)
	names = frappe.get_all(
		"POS Cash Session",
		filters=filters,
		pluck="name",
		order_by="modified desc",
		limit_start=(page - 1) * page_length,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	rows = []
	for n in names:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("POS Cash Session", n)
		rows.append(_serialize_session(doc, include_sales=False))
	total = frappe.db.count("POS Cash Session", filters)
	return {"rows": rows, "total": cint(total)}


@frappe.whitelist()
def list_recent_cashiers(limit=5):
	"""Most recently used distinct cashier names, for the 'Cambiar cajero' quick-pick tags."""
	limit = min(max(cint(limit) or 5, 1), 20)
	rows = frappe.get_all(
		"POS Cash Session",
		fields=["cashier_user"],
		filters={"cashier_user": ["!=", ""]},
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)
	seen: list[str] = []
	for r in rows:
		name = (r.cashier_user or "").strip()
		if name and name not in seen:
			seen.append(name)
		if len(seen) >= limit:
			break
	return {"cashiers": seen}


@frappe.whitelist()
def get_pos_cash_session(session_id=None, pos_profile=None, include_sales=1):
	include = cint(include_sales)
	if session_id:
		if not frappe.db.exists("POS Cash Session", session_id):
			frappe.throw(_("Session not found"))
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("POS Cash Session", session_id)
		return _serialize_session(doc, include_sales=include)
	if pos_profile:
		doc = _open_session_for_profile(pos_profile)
		if not doc:
			return {"name": None, "is_open": 0, "pos_profile": pos_profile}
		return _serialize_session(doc, include_sales=include)
	frappe.throw(_("session_id or pos_profile is required"))


def _maybe_require_start_pin(pin: str | None) -> None:
	cfg = get_pos_admin_settings()
	if not cfg.get("start_requires_pin"):
		return
	if not cfg.get("pin_configured"):
		return
	_require_pin_or_admin(pin or "")


def _resolve_start_pos_profile(pos_profile) -> str:
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	name = (pos_profile or "").strip()
	company = company_scope()
	if name and frappe.db.exists("POS Profile", name):
		if not company or frappe.db.get_value("POS Profile", name, "company") == company:
			return name
	cfg = get_pos_admin_settings()
	fallback = (cfg.get("default_pos_profile") or "").strip()
	if fallback and frappe.db.exists("POS Profile", fallback):
		if not company or frappe.db.get_value("POS Profile", fallback, "company") == company:
			return fallback
	filters = {"disabled": 0}
	if company:
		filters["company"] = company
	any_open = frappe.db.get_value("POS Profile", filters, "name") or ""
	if any_open:
		return any_open
	from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
		ensure_default_web_pos_profile,
	)

	created = ensure_default_web_pos_profile()
	if created:
		return created
	frappe.throw(_("Cash register (POS Profile) is required"))


@frappe.whitelist()
def start_pos_cash_session(pos_profile, cashier_user=None, opening_cash=0, pin=None, close_existing=0):
	pos_profile = _resolve_start_pos_profile(pos_profile)
	existing = _open_session_for_profile(pos_profile)
	if existing:
		if not cint(close_existing):
			# Skip sales list here — get_pos_cash_session loads it when the Sessions tab opens.
			return _serialize_session(existing, include_sales=False)
		_require_pin_or_admin(pin)
		existing.status = "Closed"
		existing.ended_at = now_datetime()
		_append_audit(existing, "closed_for_new_session")
		existing.save(ignore_permissions=True)
	else:
		_maybe_require_start_pin(pin)

	profile = frappe.get_doc("POS Profile", pos_profile)
	cashier = (cashier_user or _acting_user() or "").strip()
	doc = frappe.get_doc(
		{
			"doctype": "POS Cash Session",
			"pos_profile": pos_profile,
			"warehouse": profile.warehouse,
			"company": profile.company,
			"status": "Open",
			"cashier_user": cashier,
			"opening_cash": flt(opening_cash),
			"started_at": now_datetime(),
		}
	)
	_append_audit(doc, "started", {"opening_cash": flt(opening_cash), "cashier_user": cashier})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return _serialize_session(doc, include_sales=False)


@frappe.whitelist()
def ensure_pos_cash_session(pos_profile=None, cashier_user=None, pin=None, opening_cash=None):
	"""Reuse an open session, or autostart when settings allow (no PIN)."""
	cfg = get_pos_admin_settings()
	profile = (pos_profile or cfg.get("default_pos_profile") or "").strip()
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	company = company_scope()
	if profile and frappe.db.exists("POS Profile", profile):
		if company and frappe.db.get_value("POS Profile", profile, "company") != company:
			profile = ""
	if profile and not frappe.db.exists("POS Profile", profile):
		profile = ""
	if not profile:
		filters = {"disabled": 0}
		if company:
			filters["company"] = company
		profile = frappe.db.get_value("POS Profile", filters, "name") or ""
	if not profile:
		from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
			ensure_default_web_pos_profile,
		)

		profile = ensure_default_web_pos_profile()
	if not profile:
		return {
			"name": None,
			"is_open": 0,
			"needs_start": 1,
			"start_requires_pin": cfg.get("start_requires_pin"),
			"session_mode": cfg.get("session_mode"),
		}
	existing = _open_session_for_profile(profile)
	if existing:
		return _serialize_session(existing, include_sales=False)
	autostart = cfg.get("session_mode") == "autostart" and not cfg.get("start_requires_pin")
	if autostart:
		cash = (
			flt(opening_cash)
			if opening_cash not in (None, "")
			else flt(cfg.get("default_opening_cash") if cfg.get("default_opening_cash") is not None else 5000)
		)
		return start_pos_cash_session(
			pos_profile=profile,
			cashier_user=cashier_user,
			opening_cash=cash,
			pin=None,
			close_existing=0,
		)
	return {
		"name": None,
		"is_open": 0,
		"needs_start": 1,
		"pos_profile": profile,
		"start_requires_pin": cfg.get("start_requires_pin"),
		"session_mode": cfg.get("session_mode"),
	}


@frappe.whitelist()
def close_pos_cash_session(session_id, pin=None, note=None):
	_require_pin_or_admin(pin, "close")
	note = _require_note(note, "close")
	if not frappe.db.exists("POS Cash Session", session_id):
		frappe.throw(_("Session not found"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("POS Cash Session", session_id)
	if doc.status != "Open":
		frappe.throw(_("Session is already closed"))
	doc.status = "Closed"
	doc.ended_at = now_datetime()
	_append_audit(doc, "closed", {"note": note})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _serialize_session(doc, include_sales=True)


@frappe.whitelist()
def update_pos_cash_session(session_id, pin=None, cashier_user=None, opening_cash=None, note=None):
	action = "cashier" if cashier_user is not None else "opening"
	_require_pin_or_admin(pin, action)
	note = _require_note(note, action) if opening_cash is not None or cashier_user else (note or "")
	if not frappe.db.exists("POS Cash Session", session_id):
		frappe.throw(_("Session not found"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("POS Cash Session", session_id)
	if doc.status != "Open":
		frappe.throw(_("Only an open session can be updated"))
	changes = {}
	if cashier_user is not None and str(cashier_user).strip() and str(cashier_user) != str(doc.cashier_user or ""):
		changes["cashier_user"] = {"from": doc.cashier_user, "to": str(cashier_user).strip()}
		doc.cashier_user = str(cashier_user).strip()
	if opening_cash is not None:
		new_open = flt(opening_cash)
		if abs(new_open - flt(doc.opening_cash)) > 0.0001:
			changes["opening_cash"] = {"from": flt(doc.opening_cash), "to": new_open}
			doc.opening_cash = new_open
	if not changes:
		return _serialize_session(doc, include_sales=True)
	_append_audit(doc, "updated", {"changes": changes, "note": note, "admin_user": _acting_user()})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _serialize_session(doc, include_sales=True)


@frappe.whitelist()
def preview_pos_sale_amendment(invoice_name, action="cancel", items=None):
	"""Return stock reconciliation preview for cancel or edit. Does not write."""
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		frappe.throw(_("Sale not found"))
	inv = frappe.get_doc("Sales Invoice", invoice_name)
	if cint(inv.docstatus) != 1:
		frappe.throw(_("Only submitted sales can be amended"))
	item_map = _items_for_invoices([invoice_name]).get(invoice_name) or []
	if action == "cancel":
		deltas = _invoice_stock_deltas(item_map, multiplier=1.0)
	else:
		if isinstance(items, str):
			items = json.loads(items)
		new_by = {}
		for it in items or []:
			new_by[it.get("item_code")] = flt(it.get("qty"))
		deltas = {}
		for old in item_map:
			code, wh = old.get("item_code"), old.get("warehouse")
			old_qty = flt(old.get("qty"))
			new_qty = new_by.get(code, old_qty)
			# qty returning to warehouse = old - new
			deltas[(code, wh)] = deltas.get((code, wh), 0.0) + (old_qty - new_qty)
	rows = _stock_preview_for_deltas(deltas)
	pay_map = _payments_for_invoices([invoice_name]).get(invoice_name) or []
	cash_in = sum(flt(p.get("amount")) for p in pay_map if _is_cash_mop(p.get("mode_of_payment")))
	meta = _parse_invoice_remarks(inv.remarks)
	received = flt(meta.get("cash_received") or 0)
	change = max(0.0, received - cash_in) if received else 0.0
	return {
		"invoice_name": invoice_name,
		"action": action,
		"stock_preview": rows,
		"cashier_delta": -cash_in + change,
		"cash_in": cash_in,
		"change_out": change,
		"amendment_note_required": _load_action_policy(_load_pin_settings()).get(action, {}).get("requires_comment", True),
	}


@frappe.whitelist()
def amend_pos_sale(invoice_name, action="cancel", pin=None, note=None, items=None, session_id=None):
	if isinstance(items, str):
		items = json.loads(items)
	auth = _require_pin_or_admin(pin, action)
	note = _require_note(note, action, items=items)
	preview = preview_pos_sale_amendment(invoice_name, action=action, items=items)
	inv = frappe.get_doc("Sales Invoice", invoice_name)
	meta = _parse_invoice_remarks(inv.remarks)
	session_id = session_id or (None)
	if not session_id and inv.remarks and "pos_session:" in (inv.remarks or ""):
		for part in str(inv.remarks).split(" | "):
			if part.strip().startswith("pos_session:"):
				session_id = part.split(":", 1)[1].strip()
	cashier = meta.get("cashier")
	trace = {
		"action": action,
		"invoice": invoice_name,
		"receipt": meta.get("receipt"),
		"pos_session": session_id,
		"pos_profile": None,
		"warehouse": None,
		"cashier_account": cashier,
		"admin_user": auth.get("admin_user"),
		"pin_used": auth.get("pin_used"),
		"note": note,
		"cash_removed_from_drawer": preview.get("cashier_delta"),
		"cash_in": preview.get("cash_in"),
		"change_out": preview.get("change_out"),
	}
	if session_id and frappe.db.exists("POS Cash Session", session_id):
		ses = frappe.get_doc("POS Cash Session", session_id)
		trace["pos_profile"] = ses.pos_profile
		trace["warehouse"] = ses.warehouse
		trace["opening_cash"] = flt(ses.opening_cash)

	# Capture original qtys before cancel (for return side-effects).
	orig_before = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": invoice_name},
		fields=["item_code", "qty"],
		ignore_permissions=True,
	)
	old_qty_by = {r.item_code: flt(r.qty) for r in orig_before}

	_cancel_invoice_and_payments(invoice_name)

	recreated = None
	if action in ("edit", "return_items"):
		keep = [it for it in (items or []) if flt(it.get("qty")) > 0]
		if keep:
			from erpnext.erpnext_integrations.ecommerce_api.api import create_pos_sale

			orig = frappe.get_all(
				"Sales Invoice Item",
				filters={"parent": invoice_name},
				fields=["item_code", "item_name", "qty", "rate", "amount", "warehouse"],
				ignore_permissions=True,
			)
			by_code = {r.item_code: r for r in orig}
			payload = []
			for it in keep:
				src = by_code.get(it.get("item_code"))
				qty = flt(it.get("qty"))
				rate = flt(it.get("rate") if it.get("rate") is not None else (src.rate if src else 0))
				payload.append(
					{
						"item_code": it.get("item_code"),
						"item_name": (src.item_name if src else it.get("item_name") or it.get("item_code")),
						"qty": qty,
						"rate": rate,
						"amount": qty * rate,
					}
				)
			total = sum(flt(p["amount"]) for p in payload)
			uuid = f"amend-{invoice_name}-{secrets.token_hex(4)}"
			receipt = f"{meta.get('receipt') or invoice_name}-REV"
			recreated = create_pos_sale(
				offline_order_uuid=uuid,
				receipt_number=receipt,
				items=payload,
				total_amount=total,
				payment_method=meta.get("payments_label") or "Cash",
				cashier_id=cashier,
				device_id=meta.get("device"),
				branch_id=meta.get("branch"),
				sale_mode=meta.get("sale_mode") or "WHITE",
				pos_session_id=session_id,
			)

	if action == "return_items":
		new_qty_by = {str(it.get("item_code")): flt(it.get("qty")) for it in (items or [])}
		reason_by = {
			str(it.get("item_code")): str(it.get("return_reason") or "").strip()
			for it in (items or [])
			if isinstance(it, dict)
		}
		for code, old_q in old_qty_by.items():
			returned = old_q - new_qty_by.get(code, 0.0)
			if returned <= 0:
				continue
			_record_item_pos_return(
				code,
				returned,
				reason_by.get(code) or note,
				invoice_name,
			)

	sr_name = _submit_stock_reconciliation(preview.get("stock_preview") or [], note, trace)

	if session_id and frappe.db.exists("POS Cash Session", session_id):
		ses = frappe.get_doc("POS Cash Session", session_id)
		_append_audit(
			ses,
			"sale_amended",
			{
				"invoice": invoice_name,
				"action": action,
				"note": note,
				"stock_reconciliation": sr_name,
				"recreated_invoice": (recreated or {}).get("invoice_id") if isinstance(recreated, dict) else None,
				"cash_removed_from_drawer": preview.get("cashier_delta"),
				"admin_user": auth.get("admin_user"),
			},
		)
		ses.save(ignore_permissions=True)

	frappe.db.commit()
	return {
		"ok": True,
		"cancelled_invoice": invoice_name,
		"stock_reconciliation": sr_name,
		"recreated": recreated,
		"trace": trace,
		"stock_preview": preview.get("stock_preview"),
		"cashier_delta": preview.get("cashier_delta"),
	}


def attach_session_to_sale_remarks(remarks: str, warehouse=None, pos_session_id=None) -> str:
	"""Used by create_pos_sale to stamp the open cash session."""
	sid = (pos_session_id or "").strip()
	if not sid and warehouse:
		name = frappe.db.get_value(
			"POS Cash Session",
			{"warehouse": warehouse, "status": "Open"},
			"name",
		)
		sid = name or ""
	if sid and f"pos_session:{sid}" not in (remarks or ""):
		remarks = f"{remarks} | pos_session:{sid}"
	return remarks
