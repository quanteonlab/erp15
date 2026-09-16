"""Preventa (flexible sales pipeline) API.

Per-salesperson Kanban of ERPNext `Lead`s, admin-configurable field layout +
per-stage gates, a best-effort bridge from the guest catalog consulta into a
Lead, seller share-link attribution, best-effort metrics, and an explicit
Lead -> Customer conversion step. See
local_docs/proposals/i033_preventa_sales_kanban.md for the product spec.

Settings and per-seller board columns reuse the "Table Extra Schema" doctype
(scope -> JSON blob), the same pattern as tms_api.py / shop_ui_settings.py -
no new doctype or migration is warranted for settings-shaped data. Lead
conversion reuses core `erpnext.crm.doctype.lead.lead._make_customer` /
`Lead.create_contact` / `make_opportunity` rather than reimplementing them.
"""

from __future__ import annotations

import re
import secrets

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from erpnext.erpnext_integrations.ecommerce_api.company_context import acting_user as _acting_user
from erpnext.erpnext_integrations.ecommerce_api.employee_api import _can_app, _require_app_permission
from erpnext.erpnext_integrations.ecommerce_api.tags_api import set_tags_for_doc, tags_map_for_docs

# ---------------------------------------------------------------------------
# Custom fields (Lead) — wired into hooks.py after_migrate
# ---------------------------------------------------------------------------

# Maps a Preventa "field id" (used in field_layout / stage_requirements /
# conversion_checklist) to the actual attribute on the Lead doctype.
LEAD_FIELD_MAP = {
	"lead_name": "lead_name",
	"company_name": "company_name",
	"mobile_no": "mobile_no",
	"whatsapp_no": "whatsapp_no",
	"phone": "phone",
	"email_id": "email_id",
	"address_line1": "custom_address_line1",
	"city": "city",
	"state": "state",
	"pincode": "custom_pincode",
	"country": "country",
	"salutation": "salutation",
	"gender": "gender",
	"industry": "industry",
	"market_segment": "market_segment",
	"territory": "territory",
	"website": "website",
	"job_title": "job_title",
	"annual_revenue": "annual_revenue",
	"no_of_employees": "no_of_employees",
}

_preventa_fields_ready = False


def ensure_preventa_custom_fields():
	"""Custom Lead fields for the preventa stage + address. Idempotent —
	create_custom_fields skips fields that already exist. Called from
	hooks.py's after_migrate, and lazily from the write paths below so a
	fresh deploy self-heals even before `bench migrate` runs."""
	global _preventa_fields_ready
	if _preventa_fields_ready and frappe.db.has_column("Lead", "custom_preventa_stage"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Lead": [
				{
					"fieldname": "custom_preventa_stage",
					"fieldtype": "Data",
					"label": "Preventa Stage",
					"insert_after": "status",
					"default": "prospect",
					"in_standard_filter": 1,
				},
				{
					"fieldname": "custom_preventa_stage_since",
					"fieldtype": "Datetime",
					"label": "Stage Since",
					"insert_after": "custom_preventa_stage",
					"read_only": 1,
				},
				{
					"fieldname": "custom_address_line1",
					"fieldtype": "Data",
					"label": "Address Line 1",
					"insert_after": "country",
				},
				{
					"fieldname": "custom_pincode",
					"fieldtype": "Data",
					"label": "Pincode",
					"insert_after": "custom_address_line1",
				},
				{
					"fieldname": "custom_preventa_lost_reason",
					"fieldtype": "Small Text",
					"label": "Lost Reason",
					"insert_after": "custom_pincode",
				},
			]
		},
		ignore_validate=True,
	)
	frappe.clear_cache(doctype="Lead")
	_preventa_fields_ready = True


def _ensure_lead_source_catalog() -> str:
	if not frappe.db.exists("Lead Source", "Catalog"):
		frappe.get_doc({"doctype": "Lead Source", "source_name": "Catalog"}).insert(ignore_permissions=True)
	return "Catalog"


# ---------------------------------------------------------------------------
# Settings blob (Table Extra Schema reuse — see module docstring)
# ---------------------------------------------------------------------------

PREVENTA_SETTINGS_SCOPE = "settings.preventa"

PREVENTA_SETTINGS_DEFAULTS = {
	"field_layout": {
		"lead_name": "primary",
		"company_name": "primary",
		"mobile_no": "primary",
		"whatsapp_no": "primary",
		"phone": "primary",
		"email_id": "primary",
		"address_line1": "primary",
		"city": "primary",
		"state": "hidden",
		"pincode": "hidden",
		"country": "hidden",
		"notes": "primary",
		"tags": "primary",
		"salutation": "hidden",
		"gender": "hidden",
		"industry": "hidden",
		"market_segment": "hidden",
		"territory": "hidden",
		"website": "hidden",
		"job_title": "hidden",
		"annual_revenue": "hidden",
		"no_of_employees": "hidden",
		"tax_id": "hidden",
	},
	"stage_requirements": {
		"prospect": [["lead_name", "company_name"]],
		"contacted": [["mobile_no", "whatsapp_no", "phone", "email_id"]],
		"qualified": [["company_name"], ["mobile_no", "whatsapp_no", "phone", "email_id"]],
		"proposition": [["address_line1"], ["city"]],
	},
	"conversion_checklist": ["lead_name", "customer_type"],
	"default_columns_template": [
		{"key": "prospect", "label": "Prospecto", "is_default_new": True},
		{"key": "contacted", "label": "Contactado"},
		{"key": "qualified", "label": "Calificado"},
		{"key": "proposition", "label": "Propuesta"},
		{"key": "customer", "label": "Cliente", "is_won": True},
		{"key": "lost", "label": "Perdido", "is_lost": True},
	],
	"auto_convert_on_won": False,
	# Stage key that triggers a best-effort core Opportunity on entry, or None.
	"create_opportunity_on_stage": None,
}


def _load_preventa_settings() -> dict:
	settings = frappe.parse_json(frappe.as_json(PREVENTA_SETTINGS_DEFAULTS))
	if frappe.db.exists("Table Extra Schema", PREVENTA_SETTINGS_SCOPE):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Table Extra Schema", PREVENTA_SETTINGS_SCOPE)
		frappe.flags.ignore_permissions = False
		stored = frappe.parse_json(doc.columns_json) if doc.columns_json else {}
		if isinstance(stored, dict):
			settings.update(stored)
	return settings


@frappe.whitelist(allow_guest=True)
def get_preventa_settings():
	return _load_preventa_settings()


@frappe.whitelist(allow_guest=True)
def save_preventa_settings(
	field_layout=None,
	stage_requirements=None,
	conversion_checklist=None,
	default_columns_template=None,
	auto_convert_on_won=None,
	create_opportunity_on_stage=None,
):
	_require_app_permission("tools.settings")
	current = _load_preventa_settings()
	raw = {
		"field_layout": field_layout,
		"stage_requirements": stage_requirements,
		"conversion_checklist": conversion_checklist,
		"default_columns_template": default_columns_template,
		"auto_convert_on_won": auto_convert_on_won,
		"create_opportunity_on_stage": create_opportunity_on_stage,
	}
	for key, value in raw.items():
		if value is None:
			continue
		if isinstance(value, str):
			try:
				value = frappe.parse_json(value)
			except Exception:
				pass
		if key == "auto_convert_on_won":
			value = frappe.parse_json(value) if isinstance(value, str) else bool(value)
		current[key] = value

	payload = frappe.as_json(current)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", PREVENTA_SETTINGS_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", PREVENTA_SETTINGS_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": PREVENTA_SETTINGS_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return current


def _default_new_stage_key(settings: dict | None = None) -> str:
	settings = settings or _load_preventa_settings()
	cols = settings.get("default_columns_template") or []
	for col in cols:
		if col.get("is_default_new"):
			return col.get("key")
	return cols[0]["key"] if cols else "prospect"


# ---------------------------------------------------------------------------
# Per-seller board columns (Table Extra Schema reuse, one row per owner)
# ---------------------------------------------------------------------------


def _board_scope(owner_user: str) -> str:
	return f"preventa.board.{owner_user}"


def _load_board_columns(owner_user: str) -> list:
	scope = _board_scope(owner_user)
	if frappe.db.exists("Table Extra Schema", scope):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Table Extra Schema", scope)
		frappe.flags.ignore_permissions = False
		stored = frappe.parse_json(doc.columns_json) if doc.columns_json else None
		if isinstance(stored, list) and stored:
			return stored
	return list(_load_preventa_settings().get("default_columns_template") or [])


def _save_board_columns_raw(owner_user: str, columns: list) -> None:
	scope = _board_scope(owner_user)
	payload = frappe.as_json(columns)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", scope):
		doc = frappe.get_doc("Table Extra Schema", scope)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Table Extra Schema", "scope": scope, "columns_json": payload})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Per-seller "last seen" (unread-badge baseline)
# ---------------------------------------------------------------------------


def _lastseen_scope(owner_user: str) -> str:
	return f"preventa.lastseen.{owner_user}"


def _get_lastseen(owner_user: str):
	scope = _lastseen_scope(owner_user)
	if not frappe.db.exists("Table Extra Schema", scope):
		return None
	raw = frappe.db.get_value("Table Extra Schema", scope, "columns_json")
	data = frappe.parse_json(raw) if raw else {}
	return data.get("seen_at") if isinstance(data, dict) else None


def _touch_lastseen(owner_user: str) -> None:
	scope = _lastseen_scope(owner_user)
	payload = frappe.as_json({"seen_at": now_datetime()})
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", scope):
		doc = frappe.get_doc("Table Extra Schema", scope)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({"doctype": "Table Extra Schema", "scope": scope, "columns_json": payload}).insert(
			ignore_permissions=True
		)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Ownership / permission helpers
# ---------------------------------------------------------------------------


def _require_self_or_crm(owner_user: str) -> None:
	if _acting_user() == owner_user:
		_require_app_permission("ops.preventa")
	else:
		_require_app_permission("tables.crm")


def _require_owner_or_crm(lead_owner) -> None:
	if lead_owner and _acting_user() == lead_owner:
		_require_app_permission("ops.preventa")
	else:
		_require_app_permission("tables.crm")


def _require_any_permission(pids: list) -> None:
	if any(_can_app(p) for p in pids):
		return
	frappe.throw(_("Not permitted ({0})").format(" / ".join(pids)))


def _missing_requirement_groups(doc, groups: list) -> list:
	"""AND-of-OR check: each group is satisfied if at least one of its field
	ids is non-empty on `doc`. Returns the groups that are NOT satisfied."""
	missing = []
	for group in groups or []:
		if not isinstance(group, list) or not group:
			continue
		satisfied = any(bool(getattr(doc, LEAD_FIELD_MAP.get(fid, fid), None)) for fid in group)
		if not satisfied:
			missing.append(group)
	return missing


# ---------------------------------------------------------------------------
# Board + leads
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_my_board(owner_user=None):
	owner_user = (owner_user or _acting_user() or "").strip()
	if not owner_user:
		frappe.throw(_("Login required"), frappe.AuthenticationError)
	is_self = owner_user == _acting_user()
	_require_self_or_crm(owner_user)
	ensure_preventa_custom_fields()

	columns = _load_board_columns(owner_user)
	settings = _load_preventa_settings()
	layout = settings.get("field_layout") or {}
	prior_seen = _get_lastseen(owner_user)

	lead_rows = frappe.get_all(
		"Lead",
		filters={"lead_owner": owner_user, "status": ["!=", "Converted"]},
		fields=[
			"name", "lead_name", "company_name", "mobile_no", "whatsapp_no", "phone", "email_id",
			"city", "state", "country", "custom_address_line1", "custom_pincode",
			"custom_preventa_stage", "custom_preventa_stage_since", "modified", "status",
		],
		order_by="modified desc",
		limit_page_length=500,
		ignore_permissions=True,
	)
	names = [r.name for r in lead_rows]
	tags_map = tags_map_for_docs("Lead", names) if names else {}
	consulta_by_lead = _consulta_aggregate(names)

	leads_out = []
	for r in lead_rows:
		fields_out = {}
		for fid, vis in layout.items():
			if vis == "omit":
				continue
			attr = LEAD_FIELD_MAP.get(fid)
			if not attr:
				continue
			fields_out[fid] = r.get(attr)
		agg = consulta_by_lead.get(r.name, {"cnt": 0, "last_linked": None})
		last_activity = r.modified
		if agg["last_linked"] and agg["last_linked"] > last_activity:
			last_activity = agg["last_linked"]
		unread = bool(prior_seen and last_activity and last_activity > prior_seen)
		leads_out.append(
			{
				"name": r.name,
				"lead_name": r.lead_name,
				"company_name": r.company_name,
				"stage": r.custom_preventa_stage or _default_new_stage_key(settings),
				"stage_since": r.custom_preventa_stage_since,
				"fields": fields_out,
				"tags": tags_map.get(r.name, []),
				"consulta_count": agg["cnt"],
				"last_activity": last_activity,
				"unread": unread,
			}
		)

	if is_self:
		_touch_lastseen(owner_user)

	return {"columns": columns, "leads": leads_out, "owner_user": owner_user}


def _consulta_aggregate(lead_names: list) -> dict:
	if not lead_names:
		return {}
	rows = frappe.get_all(
		"Preventa Lead Consulta",
		filters={"lead": ["in", lead_names]},
		fields=["lead", "linked_on"],
		ignore_permissions=True,
	)
	out: dict = {}
	for row in rows:
		agg = out.setdefault(row.lead, {"cnt": 0, "last_linked": None})
		agg["cnt"] += 1
		if not agg["last_linked"] or (row.linked_on and row.linked_on > agg["last_linked"]):
			agg["last_linked"] = row.linked_on
	return out


@frappe.whitelist(allow_guest=True)
def save_board_columns(columns, owner_user=None):
	owner_user = (owner_user or _acting_user() or "").strip()
	_require_self_or_crm(owner_user)
	if isinstance(columns, str):
		columns = frappe.parse_json(columns)
	if not isinstance(columns, list) or not columns:
		frappe.throw(_("At least one column is required"))
	seen = set()
	for col in columns:
		key = (col or {}).get("key")
		if not key:
			frappe.throw(_("Every column needs a key"))
		if key in seen:
			frappe.throw(_("Duplicate column key: {0}").format(key))
		seen.add(key)
	_save_board_columns_raw(owner_user, columns)
	return {"ok": True, "columns": columns}


@frappe.whitelist(allow_guest=True)
def upsert_lead(lead=None, values=None):
	ensure_preventa_custom_fields()
	if isinstance(values, str):
		values = frappe.parse_json(values)
	values = values if isinstance(values, dict) else {}

	if lead and frappe.db.exists("Lead", lead):
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Lead", lead)
		frappe.flags.ignore_permissions = False
		_require_owner_or_crm(doc.lead_owner)
	else:
		_require_app_permission("ops.preventa")
		doc = frappe.new_doc("Lead")
		doc.lead_owner = values.get("lead_owner") or _acting_user()
		doc.custom_preventa_stage = values.get("custom_preventa_stage") or _default_new_stage_key()
		doc.custom_preventa_stage_since = now_datetime()
		if values.get("source"):
			doc.source = values["source"]

	for fid, attr in LEAD_FIELD_MAP.items():
		if fid in values:
			doc.set(attr, values[fid])

	if "lead_owner" in values and not doc.is_new() and values["lead_owner"] != doc.lead_owner:
		_require_app_permission("tables.crm")
		doc.lead_owner = values["lead_owner"]

	doc.flags.ignore_permissions = True
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)

	if "tags" in values:
		set_tags_for_doc("Lead", doc.name, tags=values.get("tags"), commit=False)

	frappe.db.commit()
	return {"ok": True, "name": doc.name}


@frappe.whitelist(allow_guest=True)
def add_lead_note(lead, note):
	note = (note or "").strip()
	if not note:
		frappe.throw(_("Note text is required"))
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Lead", lead)
	frappe.flags.ignore_permissions = False
	_require_owner_or_crm(doc.lead_owner)
	doc.append("notes", {"note": note, "added_by": _acting_user(), "added_on": now_datetime()})
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(allow_guest=True)
def move_lead(lead, to_stage, lost_reason=None):
	ensure_preventa_custom_fields()
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Lead", lead)
	frappe.flags.ignore_permissions = False
	_require_owner_or_crm(doc.lead_owner)

	settings = _load_preventa_settings()
	reqs = (settings.get("stage_requirements") or {}).get(to_stage) or []
	missing = _missing_requirement_groups(doc, reqs)
	if missing:
		labels = "; ".join(" o ".join(g) for g in missing)
		frappe.throw(_("Missing required field(s) to enter '{0}': {1}").format(to_stage, labels))

	columns = _load_board_columns(doc.lead_owner or _acting_user())
	col = next((c for c in columns if c.get("key") == to_stage), {})

	old_stage = doc.custom_preventa_stage
	doc.custom_preventa_stage = to_stage
	doc.custom_preventa_stage_since = now_datetime()
	if col.get("is_lost") and lost_reason:
		doc.custom_preventa_lost_reason = lost_reason
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		_log_preventa_event("stage_change", lead=doc.name, payload={"from": old_stage, "to": to_stage})
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Preventa stage_change event failed")

	result = {"ok": True, "name": doc.name, "stage": to_stage}

	if col.get("is_won") and settings.get("auto_convert_on_won"):
		try:
			result["converted"] = _do_convert_lead(doc.name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Preventa auto-convert failed")

	if settings.get("create_opportunity_on_stage") == to_stage:
		try:
			result["opportunity"] = _create_opportunity_for_lead(doc.name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Preventa auto-opportunity failed")

	return result


def _create_opportunity_for_lead(lead: str):
	from erpnext.crm.doctype.lead.lead import make_opportunity

	if frappe.db.exists("Opportunity", {"party_name": lead, "status": ["!=", "Lost"]}):
		return None
	opp = make_opportunity(lead)
	opp.flags.ignore_permissions = True
	opp.insert(ignore_permissions=True)
	frappe.db.commit()
	return opp.name


# ---------------------------------------------------------------------------
# Conversion to Customer (reuses core Lead helpers)
# ---------------------------------------------------------------------------


def _do_convert_lead(
	lead: str,
	customer_group=None,
	customer_type=None,
	tax_id=None,
	create_contact=True,
	create_address=True,
) -> dict:
	from erpnext.crm.doctype.lead.lead import _make_customer

	frappe.flags.ignore_permissions = True
	lead_doc = frappe.get_doc("Lead", lead)
	frappe.flags.ignore_permissions = False

	settings = _load_preventa_settings()
	provided = {"customer_type": customer_type, "tax_id": tax_id, "customer_group": customer_group}
	missing = []
	for fid in settings.get("conversion_checklist") or []:
		if fid in provided:
			if not provided[fid]:
				missing.append(fid)
			continue
		if not getattr(lead_doc, LEAD_FIELD_MAP.get(fid, fid), None):
			missing.append(fid)
	if missing:
		frappe.throw(_("Missing required field(s) to convert: {0}").format(", ".join(missing)))

	target = _make_customer(lead, ignore_permissions=True)
	if customer_type:
		target.customer_type = customer_type
	if customer_group:
		target.customer_group = customer_group
	target.flags.ignore_permissions = True
	target.insert(ignore_permissions=True)

	if tax_id and frappe.db.has_column("Customer", "tax_id"):
		frappe.db.set_value("Customer", target.name, "tax_id", tax_id, update_modified=False)

	contact_name = None
	if create_contact:
		try:
			contact = lead_doc.create_contact()
			contact_name = contact.name if contact else None
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Preventa contact creation failed for {lead}")

	address_name = None
	if create_address and lead_doc.custom_address_line1 and lead_doc.city and lead_doc.country:
		try:
			address = frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": target.customer_name,
					"address_type": "Billing",
					"address_line1": lead_doc.custom_address_line1,
					"city": lead_doc.city,
					"state": lead_doc.state,
					"country": lead_doc.country,
					"pincode": lead_doc.custom_pincode,
					"links": [{"link_doctype": "Customer", "link_name": target.name}],
				}
			)
			address.insert(ignore_permissions=True)
			address_name = address.name
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Preventa address creation failed for {lead}")

	lead_doc.status = "Converted"
	lead_doc.flags.ignore_permissions = True
	lead_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {"customer": target.name, "contact": contact_name, "address": address_name}


@frappe.whitelist(allow_guest=True)
def convert_lead_to_customer(
	lead, customer_group=None, customer_type=None, tax_id=None, create_contact=1, create_address=1
):
	frappe.flags.ignore_permissions = True
	lead_owner = frappe.db.get_value("Lead", lead, "lead_owner")
	frappe.flags.ignore_permissions = False
	if lead_owner is None:
		frappe.throw(_("Lead {0} not found").format(lead), frappe.DoesNotExistError)
	_require_owner_or_crm(lead_owner)

	result = _do_convert_lead(
		lead,
		customer_group=customer_group,
		customer_type=customer_type,
		tax_id=tax_id,
		create_contact=cint(create_contact),
		create_address=cint(create_address),
	)
	result["ok"] = True
	return result


# ---------------------------------------------------------------------------
# Seller Link (share-link attribution)
# ---------------------------------------------------------------------------


def _new_seller_slug() -> str:
	return f"SL-{secrets.token_urlsafe(10)}"


@frappe.whitelist(allow_guest=True)
def get_or_create_seller_link(owner_user=None, label=None):
	owner_user = (owner_user or _acting_user() or "").strip()
	_require_self_or_crm(owner_user)

	existing = frappe.get_all(
		"Seller Link",
		filters={"owner_user": owner_user, "active": 1},
		fields=["slug", "label", "company"],
		order_by="creation desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	if existing:
		row = existing[0]
		return {"slug": row.slug, "label": row.label, "path": f"/s/{row.slug}"}

	slug = _new_seller_slug()
	for _attempt in range(5):
		if not frappe.db.exists("Seller Link", slug):
			break
		slug = _new_seller_slug()

	doc = frappe.get_doc(
		{
			"doctype": "Seller Link",
			"slug": slug,
			"owner_user": owner_user,
			"label": label or "",
			"created_by_user": _acting_user(),
			"active": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"slug": doc.slug, "label": doc.label, "path": f"/s/{doc.slug}"}


@frappe.whitelist(allow_guest=True)
def list_seller_links(owner_user=None):
	owner_user = (owner_user or "").strip()
	if owner_user:
		_require_self_or_crm(owner_user)
	else:
		_require_app_permission("tables.crm")
	filters = {"owner_user": owner_user} if owner_user else {}
	rows = frappe.get_all(
		"Seller Link",
		filters=filters,
		fields=["name", "slug", "owner_user", "label", "active", "last_used_on", "open_count"],
		order_by="creation desc",
		ignore_permissions=True,
	)
	return {"links": rows}


@frappe.whitelist(allow_guest=True)
def revoke_seller_link(slug):
	owner_user = frappe.db.get_value("Seller Link", slug, "owner_user")
	if not owner_user:
		frappe.throw(_("Seller link not found"), frappe.DoesNotExistError)
	_require_self_or_crm(owner_user)
	frappe.db.set_value("Seller Link", slug, "active", 0)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(allow_guest=True)
def rotate_seller_link(slug):
	owner_user = frappe.db.get_value("Seller Link", slug, "owner_user")
	if not owner_user:
		frappe.throw(_("Seller link not found"), frappe.DoesNotExistError)
	_require_self_or_crm(owner_user)
	label = frappe.db.get_value("Seller Link", slug, "label")
	frappe.db.set_value("Seller Link", slug, "active", 0)
	frappe.db.commit()
	return get_or_create_seller_link(owner_user=owner_user, label=label)


@frappe.whitelist(allow_guest=True)
def resolve_seller_link(slug):
	"""Public resolve for /s/{slug}. Never throws — a bad/expired slug just
	returns {ok: false} so the guest can still be redirected to the catalog."""
	try:
		row = frappe.db.get_value(
			"Seller Link", slug, ["owner_user", "company", "active", "open_count"], as_dict=True
		)
		if not row or not cint(row.active):
			return {"ok": False}
		frappe.db.set_value("Seller Link", slug, "last_used_on", now_datetime(), update_modified=False)
		frappe.db.set_value("Seller Link", slug, "open_count", cint(row.open_count) + 1, update_modified=False)
		frappe.db.commit()
		try:
			_log_preventa_event("link_open", seller_link=slug, company=row.company)
		except Exception:
			pass
		return {"ok": True, "owner_user": row.owner_user, "company": row.company}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "resolve_seller_link failed")
		return {"ok": False}


# ---------------------------------------------------------------------------
# Best-effort metrics
# ---------------------------------------------------------------------------


def _log_preventa_event(event_type, lead=None, seller_link=None, visitor_key=None, payload=None, company=None):
	frappe.get_doc(
		{
			"doctype": "Preventa Event",
			"event_type": event_type,
			"lead": lead,
			"seller_link": seller_link,
			"visitor_key": visitor_key,
			"company": company,
			"created_on": now_datetime(),
			"payload_json": frappe.as_json(payload) if payload is not None else None,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


@frappe.whitelist(allow_guest=True)
def track_preventa_event(event_type, lead=None, seller_link=None, visitor_key=None, payload=None, company=None):
	try:
		if isinstance(payload, str):
			payload = frappe.parse_json(payload)
		_log_preventa_event(
			event_type, lead=lead, seller_link=seller_link, visitor_key=visitor_key, payload=payload, company=company
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "track_preventa_event failed")
	return {"ok": True}


# ---------------------------------------------------------------------------
# CRM admin (Tablas > CRM > Pre-venta tab)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def list_leads_admin(filters=None, start=0, page_length=50):
	_require_app_permission("tables.crm")
	ensure_preventa_custom_fields()
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	filters = filters if isinstance(filters, dict) else {}

	query_filters = {}
	if filters.get("stage"):
		query_filters["custom_preventa_stage"] = filters["stage"]
	if filters.get("owner"):
		query_filters["lead_owner"] = filters["owner"]

	or_filters = None
	search = (filters.get("search") or "").strip()
	if search:
		or_filters = [
			["lead_name", "like", f"%{search}%"],
			["company_name", "like", f"%{search}%"],
			["mobile_no", "like", f"%{search}%"],
			["email_id", "like", f"%{search}%"],
		]

	rows = frappe.get_all(
		"Lead",
		filters=query_filters,
		or_filters=or_filters,
		fields=[
			"name", "lead_name", "company_name", "lead_owner", "status",
			"custom_preventa_stage", "custom_preventa_stage_since",
			"mobile_no", "whatsapp_no", "phone", "email_id", "modified",
		],
		order_by="modified desc",
		start=cint(start),
		page_length=cint(page_length),
		ignore_permissions=True,
	)
	names = [r.name for r in rows]
	tags_map = tags_map_for_docs("Lead", names) if names else {}
	consulta_by_lead = _consulta_aggregate(names)
	for r in rows:
		r["tags"] = tags_map.get(r.name, [])
		r["consulta_count"] = consulta_by_lead.get(r.name, {}).get("cnt", 0)

	return {"leads": rows, "start": cint(start), "page_length": cint(page_length)}


@frappe.whitelist(allow_guest=True)
def get_lead_timeline(lead):
	"""Shared payload for both the Kanban drawer and the CRM leads tab, so
	the two surfaces can't drift."""
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Lead", lead)
	frappe.flags.ignore_permissions = False
	_require_owner_or_crm(doc.lead_owner)

	settings = _load_preventa_settings()
	layout = settings.get("field_layout") or {}
	fields_out = {}
	for fid, vis in layout.items():
		if vis == "omit":
			continue
		attr = LEAD_FIELD_MAP.get(fid)
		if not attr:
			continue
		fields_out[fid] = doc.get(attr)

	notes = [{"note": n.note, "added_by": n.added_by, "added_on": n.added_on} for n in (doc.notes or [])]

	consultas = frappe.get_all(
		"Preventa Lead Consulta",
		filters={"lead": lead},
		fields=["sales_order", "source", "linked_on"],
		order_by="linked_on desc",
		ignore_permissions=True,
	)
	for c in consultas:
		c["items"] = frappe.get_all(
			"Sales Order Item",
			filters={"parent": c.sales_order},
			fields=["item_code", "item_name", "qty", "rate"],
			ignore_permissions=True,
		)

	events = frappe.get_all(
		"Preventa Event",
		filters={"lead": lead},
		fields=["event_type", "created_on", "payload_json"],
		order_by="created_on desc",
		limit_page_length=50,
		ignore_permissions=True,
	)

	missing_by_stage = {}
	for stage_key, groups in (settings.get("stage_requirements") or {}).items():
		m = _missing_requirement_groups(doc, groups)
		if m:
			missing_by_stage[stage_key] = m

	return {
		"lead": {
			"name": doc.name,
			"lead_name": doc.lead_name,
			"company_name": doc.company_name,
			"lead_owner": doc.lead_owner,
			"status": doc.status,
			"stage": doc.custom_preventa_stage,
			"stage_since": doc.custom_preventa_stage_since,
			"fields": fields_out,
		},
		"notes": notes,
		"consultas": consultas,
		"events": events,
		"tags": tags_map_for_docs("Lead", [lead]).get(lead, []),
		"missing_by_stage": missing_by_stage,
	}


@frappe.whitelist(allow_guest=True)
def list_ventas_sellers():
	"""Users belonging to an Employee Group that grants ops.preventa — used
	to optionally source the catalog-consulta salesperson picker from real
	staff instead of the localStorage list (Phase C, additive/opt-in)."""
	_require_any_permission(["ops.preventa", "tables.crm"])
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _load_perm_store

	store = _load_perm_store()
	group_names = [g for g, perms in store.items() if "ops.preventa" in (perms or [])]
	if not group_names:
		return {"sellers": []}

	member_rows = frappe.get_all(
		"Employee Group Table",
		filters={"parent": ["in", group_names]},
		fields=["employee"],
		ignore_permissions=True,
	)
	emp_names = list({r.employee for r in member_rows if r.employee})
	if not emp_names:
		return {"sellers": []}

	emps = frappe.get_all(
		"Employee",
		filters={"name": ["in", emp_names], "status": "Active"},
		fields=["user_id", "employee_name", "cell_number"],
		ignore_permissions=True,
	)
	sellers = [
		{"id": e.user_id, "name": e.employee_name, "phone": e.cell_number} for e in emps if e.user_id
	]
	return {"sellers": sellers}


# ---------------------------------------------------------------------------
# Bridge: guest catalog consulta -> Lead (called from api.create_guest_preorder)
# ---------------------------------------------------------------------------


def _normalize_phone(value) -> str | None:
	digits = re.sub(r"\D", "", str(value or ""))
	return digits or None


def _normalize_email(value) -> str | None:
	value = str(value or "").strip().lower()
	return value or None


def sync_lead_from_guest_preorder(so_name, guest_name=None, guest_phone=None, guest_email=None, seller_ref_user=None):
	"""Best-effort: create or update a Lead from a guest catalog consulta and
	link the consulta's Sales Order to it. Never raises — the caller
	(create_guest_preorder) must succeed regardless of what happens here, and
	this function additionally guards itself since Lead.validate() can throw
	on a duplicate/malformed email (frappe.DuplicateEntryError)."""
	try:
		ensure_preventa_custom_fields()
		phone_digits = _normalize_phone(guest_phone)
		email_n = _normalize_email(guest_email)

		match_or_filters = []
		if phone_digits and len(phone_digits) >= 6:
			tail = phone_digits[-8:]
			for fieldname in ("mobile_no", "whatsapp_no", "phone"):
				match_or_filters.append([fieldname, "like", f"%{tail}"])
		if email_n:
			match_or_filters.append(["email_id", "=", email_n])

		lead_name = None
		if match_or_filters:
			found = frappe.get_all(
				"Lead",
				filters={"status": ["not in", ["Converted", "Do Not Contact"]]},
				or_filters=match_or_filters,
				fields=["name"],
				order_by="modified desc",
				limit_page_length=1,
				ignore_permissions=True,
			)
			if found:
				lead_name = found[0].name

		if lead_name:
			frappe.flags.ignore_permissions = True
			lead_doc = frappe.get_doc("Lead", lead_name)
			frappe.flags.ignore_permissions = False
			changed = False
			if guest_name and not lead_doc.lead_name:
				lead_doc.lead_name = guest_name
				changed = True
			if guest_phone and not lead_doc.mobile_no:
				lead_doc.mobile_no = guest_phone
				changed = True
			if guest_phone and not lead_doc.whatsapp_no:
				lead_doc.whatsapp_no = guest_phone
				changed = True
			if guest_email and not lead_doc.email_id:
				lead_doc.email_id = guest_email
				changed = True
			if seller_ref_user and not lead_doc.lead_owner:
				lead_doc.lead_owner = seller_ref_user
				changed = True
			if changed:
				lead_doc.flags.ignore_permissions = True
				lead_doc.save(ignore_permissions=True)
		else:
			lead_doc = frappe.new_doc("Lead")
			lead_doc.lead_name = guest_name or guest_phone or f"Consulta {so_name}"
			if guest_phone:
				lead_doc.mobile_no = guest_phone
				lead_doc.whatsapp_no = guest_phone
			if guest_email:
				lead_doc.email_id = guest_email
			if seller_ref_user:
				lead_doc.lead_owner = seller_ref_user
			lead_doc.source = _ensure_lead_source_catalog()
			lead_doc.custom_preventa_stage = _default_new_stage_key()
			lead_doc.custom_preventa_stage_since = now_datetime()
			lead_doc.flags.ignore_permissions = True
			lead_doc.insert(ignore_permissions=True)
			lead_name = lead_doc.name

		if not frappe.db.exists("Preventa Lead Consulta", {"sales_order": so_name}):
			frappe.get_doc(
				{
					"doctype": "Preventa Lead Consulta",
					"lead": lead_name,
					"sales_order": so_name,
					"source": "catalog",
					"linked_on": now_datetime(),
					"added_by": frappe.session.user if frappe.session.user != "Guest" else None,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Preventa lead sync failed for {so_name}")
