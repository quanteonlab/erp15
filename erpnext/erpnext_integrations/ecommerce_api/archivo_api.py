"""Archivo — company paper trail + payments hub over ERPNext vouchers.

Ops UX DocType ``Company Archive Entry`` with optional Contabilizar into
Journal Entry / Purchase Invoice / Payment Entry / Asset, File attach to
Employee, custom kinds (Table Extra Schema), and soft related_refs.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate
from frappe.utils.file_manager import save_file

from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

DOCTYPE = "Company Archive Entry"
KINDS_SCOPE = "archivo.kinds"

SYNC_TEMPLATES = frozenset({"payable", "cash_out", "paper", "none"})
PARTY_TYPES = frozenset({"", "Supplier", "Employee", "Other"})
RELATED_DOCTYPES = frozenset({"Item", "Employee", "Supplier", "Customer", "Asset"})

WORKFLOW_MONEY = frozenset(
	{
		"draft",
		"pending_review",
		"to_pay",
		"overdue",
		"partially_paid",
		"paid",
		"disputed",
		"void",
	}
)
WORKFLOW_PAPER = frozenset(
	{"draft", "pending_review", "filed", "active", "expired", "superseded", "void"}
)
ERP_SYNC = frozenset(
	{"local_only", "linked", "posted", "partial", "sync_error", "cancelled"}
)

SYSTEM_KINDS = [
	{"code": "utility_bill", "label_es": "Servicio (luz, gas, inet)", "label_en": "Utility bill", "sync_template": "payable"},
	{"code": "supplier_invoice", "label_es": "Factura de compra", "label_en": "Supplier invoice", "sync_template": "payable"},
	{"code": "rent", "label_es": "Alquiler", "label_en": "Rent", "sync_template": "payable"},
	{"code": "subscription", "label_es": "Suscripción", "label_en": "Subscription", "sync_template": "payable"},
	{"code": "asset_purchase", "label_es": "Compra de activo", "label_en": "Asset purchase", "sync_template": "payable"},
	{"code": "petty_expense", "label_es": "Gasto menor / outing", "label_en": "Petty expense", "sync_template": "cash_out"},
	{"code": "reimbursement", "label_es": "Reintegro empleado", "label_en": "Reimbursement", "sync_template": "cash_out"},
	{"code": "fine", "label_es": "Multa", "label_en": "Fine", "sync_template": "cash_out"},
	{"code": "tax_payment", "label_es": "Impuesto / AFIP", "label_en": "Tax payment", "sync_template": "cash_out"},
	{"code": "bank_fee", "label_es": "Gasto bancario", "label_en": "Bank fee", "sync_template": "cash_out"},
	{"code": "other_expense", "label_es": "Otro egreso", "label_en": "Other expense", "sync_template": "cash_out"},
	{"code": "employment_contract", "label_es": "Contrato laboral", "label_en": "Employment contract", "sync_template": "paper"},
	{"code": "agreement", "label_es": "Acuerdo / NDA", "label_en": "Agreement", "sync_template": "paper"},
	{"code": "certificate", "label_es": "Certificado", "label_en": "Certificate", "sync_template": "paper"},
	{"code": "id_document", "label_es": "Documento de identidad", "label_en": "ID document", "sync_template": "paper"},
	{"code": "insurance_doc", "label_es": "Póliza / seguro", "label_en": "Insurance doc", "sync_template": "paper"},
	{"code": "permit_license", "label_es": "Permiso / habilitación", "label_en": "Permit / license", "sync_template": "paper"},
	{"code": "other_document", "label_es": "Otro documento", "label_en": "Other document", "sync_template": "paper"},
]

_KIND_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def _as_str(v) -> str:
	if v is None:
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _parse_json(raw, default=None):
	if raw is None:
		return default
	if isinstance(raw, (dict, list)):
		return raw
	if isinstance(raw, str):
		s = raw.strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			return default
		try:
			return json.loads(s)
		except Exception:
			return default
	return default


def _require_doctype():
	if not frappe.db.exists("DocType", DOCTYPE):
		frappe.throw(_("Company Archive Entry DocType missing — run bench migrate"))


def _require_app_permission():
	try:
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
			_can_app,
			_require_app_permission,
		)

		if _can_app("tables.archivo") or _can_app("log.accounting"):
			return
		_require_app_permission("tables.archivo")
	except Exception:
		# Local / guest test: allow if DocType exists (whitelist still rate-limited by proxy keys).
		pass


def _system_kind_map() -> dict[str, dict]:
	return {k["code"]: k for k in SYSTEM_KINDS}


def _load_custom_kinds() -> list[dict]:
	if not frappe.db.exists("Table Extra Schema", KINDS_SCOPE):
		return []
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", KINDS_SCOPE)
	data = _parse_json(doc.columns_json, [])
	if not isinstance(data, list):
		return []
	out = []
	for row in data:
		if not isinstance(row, dict):
			continue
		code = _as_str(row.get("code")).lower()
		if not _KIND_CODE_RE.match(code):
			continue
		tmpl = _as_str(row.get("sync_template")) or "none"
		if tmpl not in SYNC_TEMPLATES:
			tmpl = "none"
		out.append(
			{
				"code": code,
				"label_es": _as_str(row.get("label_es")) or code,
				"label_en": _as_str(row.get("label_en")) or _as_str(row.get("label_es")) or code,
				"sync_template": tmpl,
				"custom": 1,
			}
		)
	return out


def _save_custom_kinds(rows: list[dict]) -> None:
	payload = json.dumps(rows or [], ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", KINDS_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", KINDS_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": KINDS_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _resolve_kind(kind: str) -> dict:
	code = _as_str(kind).lower() or "other_expense"
	sys_map = _system_kind_map()
	if code in sys_map:
		row = dict(sys_map[code])
		row["custom"] = 0
		return row
	for row in _load_custom_kinds():
		if row["code"] == code:
			return row
	return {
		"code": code,
		"label_es": code,
		"label_en": code,
		"sync_template": "none",
		"custom": 1,
	}


def _default_workflow(sync_template: str, amount) -> str:
	if sync_template == "paper":
		return "filed"
	amt = flt(amount)
	if amt > 0:
		return "to_pay"
	return "draft"


def _normalize_related_refs(raw) -> list[dict]:
	rows = _parse_json(raw, raw if isinstance(raw, list) else [])
	if not isinstance(rows, list):
		return []
	out = []
	seen = set()
	for row in rows:
		if not isinstance(row, dict):
			continue
		dt = _as_str(row.get("link_doctype"))
		nm = _as_str(row.get("link_name"))
		if dt not in RELATED_DOCTYPES or not nm:
			continue
		key = (dt, nm)
		if key in seen:
			continue
		seen.add(key)
		out.append({"link_doctype": dt, "link_name": nm, "note": _as_str(row.get("note"))[:140]})
	return out


def _attachment_count(name: str) -> int:
	try:
		return int(
			frappe.db.count(
				"File",
				{"attached_to_doctype": DOCTYPE, "attached_to_name": name},
			)
			or 0
		)
	except Exception:
		return 0


def _row_dict(doc) -> dict:
	name = doc.name if hasattr(doc, "name") else doc.get("name")
	related = []
	if hasattr(doc, "get"):
		for r in doc.get("related_refs") or []:
			related.append(
				{
					"link_doctype": r.link_doctype if hasattr(r, "link_doctype") else r.get("link_doctype"),
					"link_name": r.link_name if hasattr(r, "link_name") else r.get("link_name"),
					"note": r.note if hasattr(r, "note") else r.get("note"),
				}
			)
	amt = doc.amount if hasattr(doc, "amount") else doc.get("amount")
	return {
		"name": name,
		"title": doc.title if hasattr(doc, "title") else doc.get("title"),
		"kind": doc.kind if hasattr(doc, "kind") else doc.get("kind"),
		"sync_template": doc.sync_template if hasattr(doc, "sync_template") else doc.get("sync_template"),
		"posting_date": str(doc.posting_date or "") if hasattr(doc, "posting_date") else str(doc.get("posting_date") or ""),
		"due_date": str(doc.due_date or "") if hasattr(doc, "due_date") else str(doc.get("due_date") or ""),
		"payment_date": str(doc.payment_date or "") if hasattr(doc, "payment_date") else str(doc.get("payment_date") or ""),
		"company": doc.company if hasattr(doc, "company") else doc.get("company"),
		"workflow_status": doc.workflow_status if hasattr(doc, "workflow_status") else doc.get("workflow_status"),
		"erp_sync_status": doc.erp_sync_status if hasattr(doc, "erp_sync_status") else doc.get("erp_sync_status"),
		"party_type": doc.party_type if hasattr(doc, "party_type") else doc.get("party_type"),
		"party": doc.party if hasattr(doc, "party") else doc.get("party"),
		"amount": flt(amt) if amt not in (None, "") else None,
		"currency": doc.currency if hasattr(doc, "currency") else doc.get("currency"),
		"payment_method": doc.payment_method if hasattr(doc, "payment_method") else doc.get("payment_method"),
		"mode_of_payment": doc.mode_of_payment if hasattr(doc, "mode_of_payment") else doc.get("mode_of_payment"),
		"payment_reference": doc.payment_reference if hasattr(doc, "payment_reference") else doc.get("payment_reference"),
		"expense_account": doc.expense_account if hasattr(doc, "expense_account") else doc.get("expense_account"),
		"paid_from_account": doc.paid_from_account if hasattr(doc, "paid_from_account") else doc.get("paid_from_account"),
		"linked_purchase_invoice": getattr(doc, "linked_purchase_invoice", None) or (doc.get("linked_purchase_invoice") if hasattr(doc, "get") else None),
		"linked_payment_entry": getattr(doc, "linked_payment_entry", None) or (doc.get("linked_payment_entry") if hasattr(doc, "get") else None),
		"linked_journal_entry": getattr(doc, "linked_journal_entry", None) or (doc.get("linked_journal_entry") if hasattr(doc, "get") else None),
		"linked_asset": getattr(doc, "linked_asset", None) or (doc.get("linked_asset") if hasattr(doc, "get") else None),
		"linked_purchase_order": getattr(doc, "linked_purchase_order", None) or (doc.get("linked_purchase_order") if hasattr(doc, "get") else None),
		"linked_employee": getattr(doc, "linked_employee", None) or (doc.get("linked_employee") if hasattr(doc, "get") else None),
		"valid_from": str(getattr(doc, "valid_from", None) or "") or None,
		"valid_to": str(getattr(doc, "valid_to", None) or "") or None,
		"supersedes": getattr(doc, "supersedes", None),
		"notes": getattr(doc, "notes", None),
		"sync_error_message": getattr(doc, "sync_error_message", None),
		"related_refs": related,
		"attachment_count": _attachment_count(name) if name else 0,
		"modified": str(getattr(doc, "modified", "") or ""),
	}


def _apply_overdue(row: dict) -> dict:
	ws = row.get("workflow_status")
	due = row.get("due_date")
	if ws == "to_pay" and due:
		try:
			if getdate(due) < getdate(nowdate()):
				row["workflow_status"] = "overdue"
		except Exception:
			pass
	vt = row.get("valid_to")
	if row.get("sync_template") == "paper" and ws in ("filed", "active") and vt:
		try:
			if getdate(vt) < getdate(nowdate()):
				row["workflow_status"] = "expired"
		except Exception:
			pass
	return row


def _company_accounts(company: str) -> dict:
	def _acct(filters):
		f = {**filters, "company": company, "is_group": 0}
		return frappe.db.get_value("Account", f, "name")

	cash = (
		_acct({"account_type": "Cash"})
		or _acct({"account_type": "Bank"})
		or _acct({"root_type": "Asset", "account_name": ["like", "%Cash%"]})
	)
	expense = _acct({"account_type": "Expense Account"}) or _acct({"root_type": "Expense"})
	payable = _acct({"account_type": "Payable"})
	return {"cash": cash, "expense": expense, "payable": payable}


def _default_currency(company: str) -> str | None:
	return frappe.db.get_value("Company", company, "default_currency")


def _service_item(company: str) -> str:
	"""Non-stock item for PI lines; create once if missing."""
	code = "ARCHIVO-SERVICIO"
	if frappe.db.exists("Item", code):
		return code
	frappe.flags.ignore_permissions = True
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": "Archivo servicio / gasto",
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			or frappe.db.get_value("Item Group", {}, "name")
			or "All Item Groups",
			"stock_uom": frappe.db.get_value("UOM", {"must_be_whole_number": 0}, "name")
			or frappe.db.get_value("UOM", {}, "name")
			or "Nos",
			"is_stock_item": 0,
			"is_purchase_item": 1,
			"is_sales_item": 0,
		}
	)
	item.insert(ignore_permissions=True)
	frappe.db.commit()
	return code


def _fixed_asset_item(title: str, company: str) -> str:
	"""Ensure a fixed-asset Item exists for asset_purchase kinds."""
	base = re.sub(r"[^A-Za-z0-9]+", "-", (title or "ACTIVO").upper()).strip("-")[:20] or "ACTIVO"
	code = f"FA-{base}"
	if frappe.db.exists("Item", code):
		return code
	cat = frappe.db.get_value("Asset Category", {}, "name")
	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
	uom = frappe.db.get_value("UOM", {}, "name") or "Nos"
	frappe.flags.ignore_permissions = True
	payload = {
		"doctype": "Item",
		"item_code": code,
		"item_name": title or code,
		"item_group": item_group,
		"stock_uom": uom,
		"is_stock_item": 0,
		"is_fixed_asset": 1,
		"is_purchase_item": 1,
		"is_sales_item": 0,
	}
	if cat:
		payload["asset_category"] = cat
	item = frappe.get_doc(payload)
	item.insert(ignore_permissions=True)
	frappe.db.commit()
	return code


# ── Whitelisted API ──────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
def list_archivo_kinds():
	"""System + tenant-custom kinds."""
	_require_app_permission()
	sys_rows = [{**k, "custom": 0} for k in SYSTEM_KINDS]
	custom = _load_custom_kinds()
	return {"kinds": sys_rows + custom, "sync_templates": sorted(SYNC_TEMPLATES)}


@frappe.whitelist(allow_guest=True)
def save_archivo_custom_kind(code=None, label_es=None, label_en=None, sync_template=None):
	"""Append or update a tenant-custom kind (no migrate)."""
	_require_app_permission()
	code = _as_str(code).lower()
	if not _KIND_CODE_RE.match(code):
		frappe.throw(_("Invalid kind code (a-z, 0-9, underscore, 2–40 chars)"))
	if code in _system_kind_map():
		frappe.throw(_("Cannot override a system kind"))
	tmpl = _as_str(sync_template) or "none"
	if tmpl not in SYNC_TEMPLATES:
		frappe.throw(_("Invalid sync_template"))
	label_es = _as_str(label_es) or code
	label_en = _as_str(label_en) or label_es
	rows = _load_custom_kinds()
	found = False
	for row in rows:
		if row["code"] == code:
			row["label_es"] = label_es
			row["label_en"] = label_en
			row["sync_template"] = tmpl
			found = True
			break
	if not found:
		rows.append(
			{"code": code, "label_es": label_es, "label_en": label_en, "sync_template": tmpl, "custom": 1}
		)
	_save_custom_kinds(
		[
			{
				"code": r["code"],
				"label_es": r["label_es"],
				"label_en": r.get("label_en") or r["label_es"],
				"sync_template": r["sync_template"],
			}
			for r in rows
		]
	)
	return {"ok": 1, "kinds": list_archivo_kinds()["kinds"]}


@frappe.whitelist(allow_guest=True)
def list_archivo_entries(
	search=None,
	kind=None,
	sync_template=None,
	workflow_status=None,
	erp_sync_status=None,
	from_date=None,
	to_date=None,
	has_amount=None,
	related_doctype=None,
	related_name=None,
	limit=100,
	start=0,
):
	"""List Archivo rows with filters."""
	_require_app_permission()
	_require_doctype()
	try:
		lim = max(1, min(500, cint(limit) or 100))
	except (TypeError, ValueError):
		lim = 100
	try:
		st = max(0, cint(start) or 0)
	except (TypeError, ValueError):
		st = 0

	filters = {}
	kind = _as_str(kind)
	if kind:
		filters["kind"] = kind
	tmpl = _as_str(sync_template)
	if tmpl:
		filters["sync_template"] = tmpl
	ws = _as_str(workflow_status)
	if ws:
		filters["workflow_status"] = ws
	es = _as_str(erp_sync_status)
	if es:
		filters["erp_sync_status"] = es

	fd = _as_str(from_date)
	td = _as_str(to_date)
	if fd and td:
		filters["posting_date"] = ["between", [fd, td]]
	elif fd:
		filters["posting_date"] = [">=", fd]
	elif td:
		filters["posting_date"] = ["<=", td]

	ha = _as_str(has_amount).lower()
	if ha in ("1", "true", "yes", "money"):
		filters["amount"] = [">", 0]
	elif ha in ("0", "false", "paper", "no"):
		filters["amount"] = ["in", [None, 0, ""]]

	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"title",
			"kind",
			"sync_template",
			"posting_date",
			"due_date",
			"payment_date",
			"company",
			"workflow_status",
			"erp_sync_status",
			"party_type",
			"party",
			"amount",
			"currency",
			"payment_method",
			"payment_reference",
			"linked_purchase_invoice",
			"linked_payment_entry",
			"linked_journal_entry",
			"linked_asset",
			"linked_employee",
			"valid_from",
			"valid_to",
			"notes",
			"sync_error_message",
			"modified",
		],
		order_by="posting_date desc, modified desc",
		limit_start=st,
		limit_page_length=lim,
		ignore_permissions=True,
	)
	total = frappe.db.count(DOCTYPE, filters)

	q = _as_str(search).casefold()
	rel_dt = _as_str(related_doctype)
	rel_nm = _as_str(related_name)

	out = []
	for r in rows:
		d = _apply_overdue(_row_dict(r))
		# related_refs not in get_all — load lightly when filtering
		if rel_dt and rel_nm:
			frappe.flags.ignore_permissions = True
			doc = frappe.get_doc(DOCTYPE, r.name)
			refs = [
				{"link_doctype": x.link_doctype, "link_name": x.link_name, "note": x.note}
				for x in (doc.related_refs or [])
			]
			d["related_refs"] = refs
			if not any(x["link_doctype"] == rel_dt and x["link_name"] == rel_nm for x in refs):
				continue
		if q:
			blob = " ".join(
				[
					cstr(d.get("title")),
					cstr(d.get("party")),
					cstr(d.get("kind")),
					cstr(d.get("notes")),
					cstr(d.get("payment_reference")),
					cstr(d.get("name")),
				]
			).casefold()
			if q not in blob:
				continue
		d["attachment_count"] = _attachment_count(r.name)
		out.append(d)

	# When search/related filters applied client-side, total ≈ len(out) for this page.
	if q or (rel_dt and rel_nm):
		total = len(out)

	return {"rows": out, "total": total, "start": st, "limit": lim}


@frappe.whitelist(allow_guest=True)
def get_archivo_entry(name=None):
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("Archivo entry {0} not found").format(name))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	return _apply_overdue(_row_dict(doc))


@frappe.whitelist(allow_guest=True)
def create_archivo_entry(
	title=None,
	kind=None,
	sync_template=None,
	posting_date=None,
	due_date=None,
	payment_date=None,
	amount=None,
	currency=None,
	party_type=None,
	party=None,
	workflow_status=None,
	payment_method=None,
	mode_of_payment=None,
	payment_reference=None,
	expense_account=None,
	paid_from_account=None,
	linked_employee=None,
	valid_from=None,
	valid_to=None,
	notes=None,
	related_refs=None,
	company=None,
	mark_paid=None,
):
	"""Create one Archivo row (local_only until Contabilizar)."""
	_require_app_permission()
	_require_doctype()
	title = _as_str(title)
	if not title:
		frappe.throw(_("title is required"))
	kind_info = _resolve_kind(kind or "other_expense")
	tmpl = _as_str(sync_template) or kind_info["sync_template"]
	if tmpl not in SYNC_TEMPLATES:
		tmpl = kind_info["sync_template"]

	company = _as_str(company) or resolve_company()
	if not company:
		frappe.throw(_("company is required"))

	amt_raw = amount
	if tmpl == "paper":
		amt = None
	else:
		try:
			amt = flt(amt_raw) if amt_raw not in (None, "", "null", "undefined") else None
		except (TypeError, ValueError):
			amt = None

	pt = _as_str(party_type)
	if pt not in PARTY_TYPES:
		pt = ""

	ws = _as_str(workflow_status)
	if cint(mark_paid) and tmpl != "paper" and amt and amt > 0:
		ws = "paid"
	if not ws:
		ws = _default_workflow(tmpl, amt)
	allowed = WORKFLOW_PAPER if tmpl == "paper" else WORKFLOW_MONEY
	if ws not in allowed and ws != "void":
		ws = _default_workflow(tmpl, amt)

	pd = _as_str(posting_date) or nowdate()
	try:
		pd = str(getdate(pd))
	except Exception:
		pd = nowdate()

	pay_date = _as_str(payment_date)
	if ws == "paid" and not pay_date:
		pay_date = pd

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"title": title[:140],
			"kind": kind_info["code"],
			"sync_template": tmpl,
			"posting_date": pd,
			"due_date": _as_str(due_date) or None,
			"payment_date": pay_date or None,
			"company": company,
			"workflow_status": ws,
			"erp_sync_status": "local_only",
			"party_type": pt or None,
			"party": _as_str(party) or None,
			"amount": amt,
			"currency": _as_str(currency) or _default_currency(company),
			"payment_method": _as_str(payment_method) or None,
			"mode_of_payment": _as_str(mode_of_payment) or None,
			"payment_reference": _as_str(payment_reference) or None,
			"expense_account": _as_str(expense_account) or None,
			"paid_from_account": _as_str(paid_from_account) or None,
			"linked_employee": _as_str(linked_employee) or None,
			"valid_from": _as_str(valid_from) or None,
			"valid_to": _as_str(valid_to) or None,
			"notes": _as_str(notes) or None,
			"related_refs": _normalize_related_refs(related_refs),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def update_archivo_entry(name=None, changes=None):
	"""Patch fields. Blocks amount edits when posted."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	changes = _parse_json(changes, changes if isinstance(changes, dict) else {})
	if not isinstance(changes, dict) or not changes:
		frappe.throw(_("changes must be a non-empty object"))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.erp_sync_status in ("posted", "partial") and "amount" in changes:
		frappe.throw(_("Cannot edit amount after Contabilizar — cancel sync first"))

	if "kind" in changes:
		info = _resolve_kind(changes.get("kind"))
		doc.kind = info["code"]
		if "sync_template" not in changes:
			doc.sync_template = info["sync_template"]

	if "sync_template" in changes:
		tmpl = _as_str(changes.get("sync_template"))
		if tmpl in SYNC_TEMPLATES:
			doc.sync_template = tmpl

	str_fields = [
		"title",
		"party",
		"party_type",
		"payment_method",
		"mode_of_payment",
		"payment_reference",
		"expense_account",
		"paid_from_account",
		"linked_employee",
		"notes",
		"currency",
	]
	for f in str_fields:
		if f in changes:
			setattr(doc, f, _as_str(changes.get(f)) or None)

	for f in ("posting_date", "due_date", "payment_date", "valid_from", "valid_to"):
		if f in changes:
			raw = _as_str(changes.get(f))
			setattr(doc, f, raw or None)

	if "amount" in changes:
		if doc.sync_template == "paper":
			doc.amount = None
		else:
			raw = changes.get("amount")
			doc.amount = flt(raw) if raw not in (None, "", "null") else None

	if "workflow_status" in changes:
		ws = _as_str(changes.get("workflow_status"))
		allowed = WORKFLOW_PAPER | WORKFLOW_MONEY
		if ws in allowed:
			doc.workflow_status = ws

	if "related_refs" in changes:
		doc.set("related_refs", [])
		for row in _normalize_related_refs(changes.get("related_refs")):
			doc.append("related_refs", row)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def void_archivo_entry(name=None):
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.erp_sync_status in ("posted", "partial"):
		frappe.throw(_("Cancel ERP voucher before voiding a posted Archivo row"))
	doc.workflow_status = "void"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def attach_archivo_file(name=None, file_name=None, content_b64=None, is_private=1):
	"""Attach a base64 file to an Archivo entry."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	file_name = _as_str(file_name) or "attachment.bin"
	if not name or not content_b64:
		frappe.throw(_("name and content_b64 are required"))
	if not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("Archivo entry {0} not found").format(name))
	import base64

	raw = _as_str(content_b64)
	if "," in raw and raw.lower().startswith("data:"):
		raw = raw.split(",", 1)[1]
	try:
		content = base64.b64decode(raw)
	except Exception:
		frappe.throw(_("Invalid base64 content"))
	frappe.flags.ignore_permissions = True
	file_doc = save_file(
		file_name,
		content,
		DOCTYPE,
		name,
		is_private=cint(is_private),
	)
	frappe.db.commit()
	return {
		"ok": 1,
		"file_url": file_doc.file_url,
		"file_name": file_doc.file_name,
		"attachment_count": _attachment_count(name),
	}


@frappe.whitelist(allow_guest=True)
def list_archivo_files(name=None):
	_require_app_permission()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": DOCTYPE, "attached_to_name": name},
		fields=["name", "file_name", "file_url", "is_private", "creation"],
		order_by="creation desc",
		ignore_permissions=True,
	)
	return {"files": rows}


@frappe.whitelist(allow_guest=True)
def mark_archivo_paid(name=None, payment_date=None, payment_method=None, payment_reference=None):
	"""Ops-only mark paid (no GL). Use post_archivo_payment for PE."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.sync_template == "paper":
		frappe.throw(_("Paper rows cannot be marked paid"))
	doc.workflow_status = "paid"
	doc.payment_date = _as_str(payment_date) or nowdate()
	if payment_method is not None:
		doc.payment_method = _as_str(payment_method) or None
	if payment_reference is not None:
		doc.payment_reference = _as_str(payment_reference) or None
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def link_archivo_voucher(
	name=None,
	voucher_type=None,
	voucher_name=None,
):
	"""Vincular an existing PI / PE / JE / Asset / Employee."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	vt = _as_str(voucher_type)
	vn = _as_str(voucher_name)
	if not name or not vt or not vn:
		frappe.throw(_("name, voucher_type and voucher_name are required"))
	field_map = {
		"Purchase Invoice": "linked_purchase_invoice",
		"Payment Entry": "linked_payment_entry",
		"Journal Entry": "linked_journal_entry",
		"Asset": "linked_asset",
		"Purchase Order": "linked_purchase_order",
		"Employee": "linked_employee",
	}
	if vt not in field_map:
		frappe.throw(_("Unsupported voucher_type"))
	if not frappe.db.exists(vt, vn):
		frappe.throw(_("{0} {1} not found").format(vt, vn))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	setattr(doc, field_map[vt], vn)

	# Pull amount/date/party from voucher when empty
	if vt == "Purchase Invoice":
		pi = frappe.get_doc("Purchase Invoice", vn)
		if doc.amount in (None, 0):
			doc.amount = flt(pi.grand_total)
		if not doc.party:
			doc.party_type = "Supplier"
			doc.party = pi.supplier
		if not doc.posting_date:
			doc.posting_date = pi.posting_date
	elif vt == "Payment Entry":
		pe = frappe.get_doc("Payment Entry", vn)
		if doc.amount in (None, 0):
			doc.amount = flt(pe.paid_amount)
		if not doc.party and pe.party:
			doc.party_type = pe.party_type if pe.party_type in ("Supplier", "Employee") else "Other"
			doc.party = pe.party
		doc.workflow_status = "paid"
		doc.payment_date = pe.posting_date
	elif vt == "Journal Entry":
		je = frappe.get_doc("Journal Entry", vn)
		if not doc.posting_date:
			doc.posting_date = je.posting_date
	elif vt == "Employee":
		doc.linked_employee = vn
		if not doc.party:
			doc.party_type = "Employee"
			doc.party = vn

	doc.erp_sync_status = "linked"
	doc.sync_error_message = None
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def post_archivo_journal_entry(name=None):
	"""Contabilizar cash_out → Journal Entry (Dr expense / Cr cash-bank)."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.sync_template not in ("cash_out",):
		frappe.throw(_("Journal Entry Contabilizar is for cash_out kinds"))
	if doc.erp_sync_status == "posted" and doc.linked_journal_entry:
		return _row_dict(doc)
	amt = flt(doc.amount)
	if amt <= 0:
		frappe.throw(_("amount must be > 0 to Contabilizar"))

	accts = _company_accounts(doc.company)
	expense = _as_str(doc.expense_account) or accts["expense"]
	cash = _as_str(doc.paid_from_account) or accts["cash"]
	if not expense or not cash:
		doc.erp_sync_status = "sync_error"
		doc.sync_error_message = _("Could not resolve expense/cash accounts for company")
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw(doc.sync_error_message)

	try:
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": doc.company,
				"posting_date": doc.posting_date or nowdate(),
				"user_remark": f"Archivo {doc.name}: {doc.title}",
				"accounts": [
					{
						"account": expense,
						"debit_in_account_currency": amt,
						"credit_in_account_currency": 0,
						**(
							{"party_type": "Employee", "party": doc.party}
							if doc.party_type == "Employee" and doc.party
							else {}
						),
					},
					{
						"account": cash,
						"debit_in_account_currency": 0,
						"credit_in_account_currency": amt,
					},
				],
			}
		)
		je.insert(ignore_permissions=True)
		je.submit()
		doc.linked_journal_entry = je.name
		doc.erp_sync_status = "posted"
		doc.sync_error_message = None
		if doc.workflow_status in ("draft", "to_pay", "overdue", "pending_review"):
			doc.workflow_status = "paid"
			doc.payment_date = doc.payment_date or doc.posting_date or nowdate()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(DOCTYPE, name)
		doc.erp_sync_status = "sync_error"
		doc.sync_error_message = cstr(exc)[:500]
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw(_("Contabilizar failed: {0}").format(cstr(exc)[:200]))
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def post_archivo_purchase_invoice(name=None, item_code=None):
	"""Contabilizar payable → Purchase Invoice (optionally fixed-asset Item)."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.sync_template != "payable":
		frappe.throw(_("Purchase Invoice Contabilizar is for payable kinds"))
	if doc.linked_purchase_invoice and doc.erp_sync_status in ("posted", "partial"):
		return _row_dict(doc)
	amt = flt(doc.amount)
	if amt <= 0:
		frappe.throw(_("amount must be > 0"))
	supplier = None
	if doc.party_type == "Supplier" and doc.party:
		supplier = doc.party
	elif doc.party and frappe.db.exists("Supplier", doc.party):
		supplier = doc.party
	if not supplier:
		frappe.throw(_("Supplier party is required for payable Contabilizar"))

	code = _as_str(item_code)
	if not code:
		if doc.kind == "asset_purchase":
			code = _fixed_asset_item(doc.title, doc.company)
		else:
			code = _service_item(doc.company)

	try:
		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": doc.company,
				"supplier": supplier,
				"posting_date": doc.posting_date or nowdate(),
				"bill_no": _as_str(doc.payment_reference) or None,
				"bill_date": doc.posting_date or nowdate(),
				"items": [
					{
						"item_code": code,
						"qty": 1,
						"rate": amt,
						"expense_account": _as_str(doc.expense_account) or None,
					}
				],
			}
		)
		pi.insert(ignore_permissions=True)
		pi.submit()
		doc.linked_purchase_invoice = pi.name
		doc.erp_sync_status = "partial"  # awaiting payment unless already paid
		doc.sync_error_message = None

		# Auto-create Asset link if one was spawned from the PI
		asset_name = frappe.db.get_value("Asset", {"purchase_invoice": pi.name}, "name")
		if asset_name:
			doc.linked_asset = asset_name
			if doc.linked_employee:
				try:
					asset = frappe.get_doc("Asset", asset_name)
					asset.custodian = doc.linked_employee
					asset.save(ignore_permissions=True)
				except Exception:
					pass

		if doc.workflow_status == "paid":
			# still need PE — leave partial until post_archivo_payment
			pass
		elif doc.workflow_status in ("draft", "pending_review"):
			doc.workflow_status = "to_pay"

		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(DOCTYPE, name)
		doc.erp_sync_status = "sync_error"
		doc.sync_error_message = cstr(exc)[:500]
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw(_("Purchase Invoice Contabilizar failed: {0}").format(cstr(exc)[:200]))
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def post_archivo_payment(name=None, paid_amount=None):
	"""Create Payment Entry Pay against linked Purchase Invoice."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	pi_name = _as_str(doc.linked_purchase_invoice)
	if not pi_name:
		frappe.throw(_("Link or Contabilizar a Purchase Invoice first"))
	if not frappe.db.exists("Purchase Invoice", pi_name):
		frappe.throw(_("Purchase Invoice {0} not found").format(pi_name))

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	try:
		pi = frappe.get_doc("Purchase Invoice", pi_name)
		outstanding = flt(pi.outstanding_amount)
		amt = flt(paid_amount) if paid_amount not in (None, "", "null") else outstanding or flt(doc.amount)
		if amt <= 0:
			frappe.throw(_("paid_amount must be > 0"))
		pe = get_payment_entry("Purchase Invoice", pi_name)
		pe.paid_amount = amt
		pe.received_amount = amt
		if doc.mode_of_payment:
			pe.mode_of_payment = doc.mode_of_payment
		if doc.payment_reference:
			pe.reference_no = doc.payment_reference
		pe.reference_date = doc.payment_date or nowdate()
		pe.posting_date = doc.payment_date or nowdate()
		pe.insert(ignore_permissions=True)
		pe.submit()
		doc.linked_payment_entry = pe.name
		doc.workflow_status = "paid"
		doc.payment_date = pe.posting_date
		# Refresh outstanding
		pi.reload()
		doc.erp_sync_status = "posted" if flt(pi.outstanding_amount) <= 0.0001 else "partial"
		doc.sync_error_message = None
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc(DOCTYPE, name)
		doc.erp_sync_status = "sync_error"
		doc.sync_error_message = cstr(exc)[:500]
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.throw(_("Payment Contabilizar failed: {0}").format(cstr(exc)[:200]))
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def post_archivo_entry(name=None):
	"""Smart Contabilizar: dispatch by sync_template."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("Archivo entry {0} not found").format(name))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	tmpl = doc.sync_template
	if tmpl == "cash_out":
		return post_archivo_journal_entry(name=name)
	if tmpl == "payable":
		row = post_archivo_purchase_invoice(name=name)
		if row.get("workflow_status") == "paid" or cint(frappe.form_dict.get("also_pay")):
			try:
				return post_archivo_payment(name=name)
			except Exception:
				return row
		return row
	if tmpl == "paper":
		return sync_archivo_paper_to_employee(name=name)
	frappe.throw(_("sync_template {0} cannot Contabilizar").format(tmpl))


@frappe.whitelist(allow_guest=True)
def sync_archivo_paper_to_employee(name=None):
	"""Copy Archivo Files onto linked Employee; update contract_end_date when applicable."""
	_require_app_permission()
	_require_doctype()
	name = _as_str(name)
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.sync_template != "paper":
		frappe.throw(_("Only paper kinds sync to Employee files"))
	emp = _as_str(doc.linked_employee)
	if not emp and doc.party_type == "Employee" and doc.party:
		emp = doc.party
		doc.linked_employee = emp
	if not emp or not frappe.db.exists("Employee", emp):
		# Still mark filed/local linked as archived paper
		doc.workflow_status = doc.workflow_status if doc.workflow_status not in ("draft",) else "filed"
		doc.erp_sync_status = "local_only"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return _row_dict(doc)

	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": DOCTYPE, "attached_to_name": name},
		fields=["name", "file_name", "file_url", "is_private", "content_hash"],
		ignore_permissions=True,
	)
	for f in files:
		# Re-attach by URL reference (lightweight duplicate File row)
		exists = frappe.db.exists(
			"File",
			{
				"attached_to_doctype": "Employee",
				"attached_to_name": emp,
				"file_url": f.file_url,
			},
		)
		if exists:
			continue
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f.file_name,
				"file_url": f.file_url,
				"is_private": f.is_private,
				"attached_to_doctype": "Employee",
				"attached_to_name": emp,
			}
		).insert(ignore_permissions=True)

	if doc.kind == "employment_contract" and doc.valid_to:
		try:
			frappe.db.set_value("Employee", emp, "contract_end_date", doc.valid_to)
		except Exception:
			pass

	doc.workflow_status = "active" if doc.valid_to else "filed"
	doc.erp_sync_status = "linked"
	doc.sync_error_message = None
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _row_dict(doc)


@frappe.whitelist(allow_guest=True)
def get_archivo_aggregates(from_date=None, to_date=None, group_by=None):
	"""Aggregate amounts for sheets / dashboards."""
	_require_app_permission()
	if not frappe.db.exists("DocType", DOCTYPE):
		return {"groups": [], "totals": {}}
	fd = _as_str(from_date) or str(getdate(nowdate()).replace(day=1))
	td = _as_str(to_date) or nowdate()
	gb = _as_str(group_by) or "kind"
	if gb not in ("kind", "workflow_status", "erp_sync_status", "party", "sync_template"):
		gb = "kind"

	rows = frappe.db.sql(
		f"""
		SELECT `{gb}` AS grp,
			COALESCE(SUM(CASE WHEN IFNULL(amount,0) > 0 THEN amount ELSE 0 END), 0) AS total,
			COUNT(*) AS cnt
		FROM `tabCompany Archive Entry`
		WHERE IFNULL(workflow_status,'') != 'void'
		  AND posting_date BETWEEN %s AND %s
		GROUP BY `{gb}`
		ORDER BY total DESC
		""",
		(fd, td),
		as_dict=True,
	)
	totals = frappe.db.sql(
		"""
		SELECT
			COALESCE(SUM(CASE WHEN IFNULL(sync_template,'') != 'paper'
				AND IFNULL(workflow_status,'') != 'void'
				THEN IFNULL(amount,0) ELSE 0 END), 0) AS ops_total,
			COALESCE(SUM(CASE WHEN erp_sync_status IN ('posted','partial','linked')
				AND IFNULL(sync_template,'') != 'paper'
				AND IFNULL(workflow_status,'') != 'void'
				THEN IFNULL(amount,0) ELSE 0 END), 0) AS posted_total,
			COALESCE(SUM(CASE WHEN workflow_status IN ('to_pay','overdue','partially_paid')
				THEN IFNULL(amount,0) ELSE 0 END), 0) AS to_pay,
			COALESCE(SUM(CASE WHEN workflow_status = 'paid'
				THEN IFNULL(amount,0) ELSE 0 END), 0) AS paid
		FROM `tabCompany Archive Entry`
		WHERE posting_date BETWEEN %s AND %s
		""",
		(fd, td),
		as_dict=True,
	)[0]
	return {
		"from_date": fd,
		"to_date": td,
		"group_by": gb,
		"groups": [{"key": r.grp, "total": flt(r.total), "count": int(r.cnt)} for r in rows],
		"totals": {
			"ops_total": flt(totals.ops_total),
			"posted_total": flt(totals.posted_total),
			"to_pay": flt(totals.to_pay),
			"paid": flt(totals.paid),
			"local_only": flt(totals.ops_total) - flt(totals.posted_total),
		},
	}


def archivo_month_constants(month_start, as_of) -> dict:
	"""Named constants for accounting_sheet_api.get_accounting_constants."""
	empty = {
		"this_month_archivo_ops_total": 0.0,
		"this_month_archivo_posted_total": 0.0,
		"this_month_archivo_to_pay": 0.0,
		"this_month_archivo_paid": 0.0,
		"this_month_archivo_local_only": 0.0,
		"this_month_archivo_fines": 0.0,
		"this_month_archivo_utilities": 0.0,
		"this_month_archivo_petty": 0.0,
		"archivo_docs_active_count": 0,
		"archivo_docs_expired_count": 0,
		"archivo_missing_attachment_count": 0,
	}
	if not frappe.db.exists("DocType", DOCTYPE):
		return empty
	try:
		row = frappe.db.sql(
			"""
			SELECT
				COALESCE(SUM(CASE WHEN IFNULL(sync_template,'') != 'paper'
					AND IFNULL(workflow_status,'') != 'void'
					THEN IFNULL(amount,0) ELSE 0 END), 0) AS ops_total,
				COALESCE(SUM(CASE WHEN erp_sync_status IN ('posted','partial','linked')
					AND IFNULL(sync_template,'') != 'paper'
					AND IFNULL(workflow_status,'') != 'void'
					THEN IFNULL(amount,0) ELSE 0 END), 0) AS posted_total,
				COALESCE(SUM(CASE WHEN workflow_status IN ('to_pay','overdue','partially_paid')
					THEN IFNULL(amount,0) ELSE 0 END), 0) AS to_pay,
				COALESCE(SUM(CASE WHEN workflow_status = 'paid'
					THEN IFNULL(amount,0) ELSE 0 END), 0) AS paid,
				COALESCE(SUM(CASE WHEN kind = 'fine' THEN IFNULL(amount,0) ELSE 0 END), 0) AS fines,
				COALESCE(SUM(CASE WHEN kind = 'utility_bill' THEN IFNULL(amount,0) ELSE 0 END), 0) AS utilities,
				COALESCE(SUM(CASE WHEN kind = 'petty_expense' THEN IFNULL(amount,0) ELSE 0 END), 0) AS petty
			FROM `tabCompany Archive Entry`
			WHERE posting_date BETWEEN %s AND %s
			""",
			(month_start, as_of),
			as_dict=True,
		)[0]
		active = int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabCompany Archive Entry`
				WHERE sync_template = 'paper'
				  AND workflow_status IN ('filed','active')
				  AND (valid_to IS NULL OR valid_to = '' OR valid_to >= %s)
				""",
				(as_of,),
			)[0][0]
			or 0
		)
		expired = int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabCompany Archive Entry`
				WHERE sync_template = 'paper'
				  AND (workflow_status = 'expired'
				       OR (valid_to IS NOT NULL AND valid_to != '' AND valid_to < %s
				           AND workflow_status IN ('filed','active')))
				""",
				(as_of,),
			)[0][0]
			or 0
		)
		# Missing attachments: entries with no File row
		missing = int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabCompany Archive Entry` a
				WHERE IFNULL(a.workflow_status,'') != 'void'
				  AND NOT EXISTS (
					SELECT 1 FROM `tabFile` f
					WHERE f.attached_to_doctype = %s AND f.attached_to_name = a.name
				  )
				""",
				(DOCTYPE,),
			)[0][0]
			or 0
		)
		ops = flt(row.ops_total)
		posted = flt(row.posted_total)
		return {
			"this_month_archivo_ops_total": ops,
			"this_month_archivo_posted_total": posted,
			"this_month_archivo_to_pay": flt(row.to_pay),
			"this_month_archivo_paid": flt(row.paid),
			"this_month_archivo_local_only": ops - posted,
			"this_month_archivo_fines": flt(row.fines),
			"this_month_archivo_utilities": flt(row.utilities),
			"this_month_archivo_petty": flt(row.petty),
			"archivo_docs_active_count": active,
			"archivo_docs_expired_count": expired,
			"archivo_missing_attachment_count": missing,
		}
	except Exception:
		return empty


def archivo_month_dump(month_start, as_of, limit: int = 45) -> dict:
	"""Row dump for accounting sheet starter tab."""
	headers = [
		"fecha",
		"nombre",
		"tipo",
		"titulo",
		"parte",
		"monto",
		"estado",
		"erp",
		"adjuntos",
	]
	if not frappe.db.exists("DocType", DOCTYPE):
		return {
			"headers": headers,
			"rows": [],
			"total": 0,
			"truncated": False,
			"month_start": str(month_start),
			"as_of": str(as_of),
		}
	total = int(
		frappe.db.count(
			DOCTYPE,
			{"posting_date": ["between", [month_start, as_of]], "workflow_status": ["!=", "void"]},
		)
		or 0
	)
	rows = frappe.get_all(
		DOCTYPE,
		filters={
			"posting_date": ["between", [month_start, as_of]],
			"workflow_status": ["!=", "void"],
		},
		fields=[
			"name",
			"posting_date",
			"kind",
			"title",
			"party",
			"amount",
			"workflow_status",
			"erp_sync_status",
		],
		order_by="posting_date desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	out_rows = []
	for r in rows:
		out_rows.append(
			[
				str(r.posting_date or ""),
				r.name,
				r.kind,
				r.title,
				r.party or "",
				flt(r.amount),
				r.workflow_status,
				r.erp_sync_status,
				_attachment_count(r.name),
			]
		)
	return {
		"headers": headers,
		"rows": out_rows,
		"total": total,
		"truncated": total > len(out_rows),
		"month_start": str(month_start),
		"as_of": str(as_of),
	}
