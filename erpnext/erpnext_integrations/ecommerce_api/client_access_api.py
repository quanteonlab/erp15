"""Client access PIN (Twilio WhatsApp/SMS) for catalog guests — not ERP Users.

When a consulta arrives with a phone never seen before (and country is in the
allow-list, default AR), we mint a 6-digit PIN, store it on Customer and/or Lead,
and best-effort send it via Twilio. The guest portal uses phone+PIN only to
prefill / track — no Frappe User is created.
"""

from __future__ import annotations

import re
import secrets

import frappe
from frappe import _
from frappe.utils import cstr, cint

from erpnext.erpnext_integrations.ecommerce_api.sms_api import (
	_ensure_settings as _ensure_sms_settings,
	_normalize_e164,
	_twilio_configured,
	_twilio_send_sms,
)

_CLIENT_ACCESS_FIELDS_READY = False

# ISO → dial code (digits). Keep in sync with erpnext-ecommerce/lib/phone-country.ts
_DIAL_BY_ISO = {
	"AR": "54",
	"UY": "598",
	"CL": "56",
	"BR": "55",
	"PY": "595",
	"BO": "591",
	"PE": "51",
	"MX": "52",
	"US": "1",
	"ES": "34",
}


def ensure_client_access_custom_fields():
	"""Pin + E.164 credential phone on Customer and Lead."""
	global _CLIENT_ACCESS_FIELDS_READY
	if _CLIENT_ACCESS_FIELDS_READY:
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	defs = [
		{
			"fieldname": "custom_client_access_pin",
			"fieldtype": "Data",
			"label": "Client Access PIN",
			"insert_after": "mobile_no",
			"read_only": 1,
		},
		{
			"fieldname": "custom_client_phone_e164",
			"fieldtype": "Data",
			"label": "Client Phone (E.164)",
			"insert_after": "custom_client_access_pin",
			"read_only": 1,
		},
	]
	create_custom_fields({"Customer": defs, "Lead": defs}, ignore_validate=True)
	frappe.clear_cache(doctype="Customer")
	frappe.clear_cache(doctype="Lead")
	_CLIENT_ACCESS_FIELDS_READY = True


def _digits(value) -> str:
	return re.sub(r"\D", "", cstr(value or ""))


def _iso_from_e164(e164: str) -> str | None:
	digits = _digits(e164)
	# Longest dial match
	pairs = sorted(_DIAL_BY_ISO.items(), key=lambda kv: -len(kv[1]))
	for iso, dial in pairs:
		if digits.startswith(dial):
			return iso
	return None


def _phone_seen_before(e164: str) -> bool:
	digits = _digits(e164)
	if len(digits) < 8:
		return True  # treat invalid as "seen" so we never mint a PIN
	tail = digits[-8:]
	cust_or = [["mobile_no", "like", f"%{tail}%"]]
	if frappe.db.has_column("Customer", "custom_client_phone_e164"):
		cust_or.append(["custom_client_phone_e164", "like", f"%{tail}%"])
	for row in frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		or_filters=cust_or,
		fields=["name", "mobile_no"]
		+ (["custom_client_phone_e164"] if frappe.db.has_column("Customer", "custom_client_phone_e164") else []),
		limit_page_length=20,
		ignore_permissions=True,
	):
		mob = _digits(row.mobile_no)
		cred = _digits(getattr(row, "custom_client_phone_e164", None))
		if mob and (mob == digits or mob.endswith(digits) or digits.endswith(mob)):
			return True
		if cred and (cred == digits or cred.endswith(digits) or digits.endswith(cred)):
			return True
	lead_or = [
		["mobile_no", "like", f"%{tail}%"],
		["whatsapp_no", "like", f"%{tail}%"],
		["phone", "like", f"%{tail}%"],
	]
	if frappe.db.has_column("Lead", "custom_client_phone_e164"):
		lead_or.append(["custom_client_phone_e164", "like", f"%{tail}%"])
	for row in frappe.get_all(
		"Lead",
		or_filters=lead_or,
		fields=["name", "mobile_no", "whatsapp_no", "phone"]
		+ (["custom_client_phone_e164"] if frappe.db.has_column("Lead", "custom_client_phone_e164") else []),
		limit_page_length=20,
		ignore_permissions=True,
	):
		for field in ("mobile_no", "whatsapp_no", "phone", "custom_client_phone_e164"):
			mob = _digits(getattr(row, field, None))
			if mob and (mob == digits or mob.endswith(digits) or digits.endswith(mob)):
				return True
	return False


def _new_pin() -> str:
	return f"{secrets.randbelow(1_000_000):06d}"


def _portal_base_url() -> str:
	"""Prefer shop URL from site config; fall back to request host."""
	for key in ("shop_url", "ecommerce_url", "hostname"):
		val = frappe.conf.get(key)
		if val:
			url = cstr(val).rstrip("/")
			if not url.startswith("http"):
				url = "https://" + url
			return url
	try:
		return frappe.utils.get_url().rstrip("/")
	except Exception:
		return ""


def _send_pin_message(to_e164: str, pin: str, portal_url: str) -> dict:
	"""Best-effort Twilio WhatsApp (preferred) or SMS."""
	settings = _ensure_sms_settings()
	if not cint(settings.get("enabled")) or not _twilio_configured(settings):
		return {"sent": False, "reason": "sms_disabled"}

	account_sid = cstr(settings.get("account_sid") or "").strip()
	from_raw = _normalize_e164(cstr(settings.get("from_number") or ""))
	try:
		auth_token = settings.get_password("auth_token", raise_exception=False) or ""
	except Exception:
		auth_token = ""
	auth_token = cstr(auth_token).strip()
	if not account_sid or not auth_token or not from_raw:
		return {"sent": False, "reason": "incomplete_twilio"}

	body = (
		f"Tu PIN de acceso es {pin}. "
		f"Guardalo para ver tus consultas: {portal_url or '/cliente'}"
	)[:1500]

	# Prefer WhatsApp channel when From is already a WhatsApp sender,
	# otherwise send plain SMS (still Twilio — "Trillo" in product notes).
	use_wa = "whatsapp" in cstr(settings.get("from_number") or "").lower() or from_raw.startswith(
		"whatsapp:"
	)
	from_number = from_raw if from_raw.startswith("whatsapp:") else (
		f"whatsapp:{from_raw}" if use_wa else from_raw
	)
	to_number = to_e164 if to_e164.startswith("whatsapp:") else (
		f"whatsapp:{to_e164}" if use_wa else to_e164
	)

	try:
		result = _twilio_send_sms(
			account_sid=account_sid,
			auth_token=auth_token,
			from_number=from_number,
			to_number=to_number,
			body=body,
		)
		return {
			"sent": True,
			"channel": "whatsapp" if use_wa else "sms",
			"sid": result.get("sid"),
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Client access PIN Twilio send failed")
		# If WhatsApp failed, try plain SMS once.
		if use_wa:
			try:
				result = _twilio_send_sms(
					account_sid=account_sid,
					auth_token=auth_token,
					from_number=from_raw.replace("whatsapp:", ""),
					to_number=to_e164.replace("whatsapp:", ""),
					body=body,
				)
				return {"sent": True, "channel": "sms_fallback", "sid": result.get("sid")}
			except Exception:
				frappe.log_error(frappe.get_traceback(), "Client access PIN SMS fallback failed")
		return {"sent": False, "reason": "twilio_error"}


def _stamp_party(doctype: str, name: str, pin: str, e164: str):
	if not name or not frappe.db.exists(doctype, name):
		return
	updates = {}
	if frappe.db.has_column(doctype, "custom_client_access_pin"):
		updates["custom_client_access_pin"] = pin
	if frappe.db.has_column(doctype, "custom_client_phone_e164"):
		updates["custom_client_phone_e164"] = e164
	if doctype == "Customer" and frappe.db.has_column("Customer", "mobile_no"):
		if not frappe.db.get_value("Customer", name, "mobile_no"):
			updates["mobile_no"] = e164
	if updates:
		frappe.db.set_value(doctype, name, updates, update_modified=False)


@frappe.whitelist(allow_guest=True)
def issue_client_access_pin(
	guest_phone=None,
	guest_name=None,
	guest_email=None,
	customer=None,
	lead=None,
	allowed_countries=None,
	send=1,
	force_new=0,
):
	"""Mint (or reuse) a 6-digit PIN for a guest phone.

	Only issues a *new* PIN + outbound message when the number was never seen
	and its country ISO is in ``allowed_countries`` (default ``["AR"]``).
	Always returns the PIN when one already exists for this party.
	"""
	ensure_client_access_custom_fields()
	e164 = _normalize_e164(cstr(guest_phone or ""))
	if not e164 or len(_digits(e164)) < 8:
		return {"ok": False, "reason": "invalid_phone"}

	if isinstance(allowed_countries, str):
		try:
			allowed_countries = frappe.parse_json(allowed_countries)
		except Exception:
			allowed_countries = [allowed_countries]
	allowed = [
		cstr(x).strip().upper()
		for x in (allowed_countries or ["AR"])
		if cstr(x).strip()
	] or ["AR"]

	iso = _iso_from_e164(e164) or ""
	if iso and iso not in allowed and not cint(force_new):
		return {"ok": False, "reason": "country_not_allowed", "country": iso}

	customer = cstr(customer or "").strip() or None
	lead = cstr(lead or "").strip() or None

	# Reuse existing PIN on linked party
	existing_pin = None
	if customer and frappe.db.exists("Customer", customer):
		existing_pin = frappe.db.get_value("Customer", customer, "custom_client_access_pin")
	if not existing_pin and lead and frappe.db.exists("Lead", lead):
		existing_pin = frappe.db.get_value("Lead", lead, "custom_client_access_pin")

	seen = _phone_seen_before(e164)
	is_new = (not seen) or cint(force_new)

	pin = cstr(existing_pin or "").strip()
	issued_new = False
	if not pin and is_new:
		pin = _new_pin()
		issued_new = True
	elif not pin and seen:
		# Known phone without PIN — mint silently, do not spam WhatsApp unless send+force
		pin = _new_pin()
		issued_new = True
		send = 0 if not cint(force_new) else send

	if customer:
		_stamp_party("Customer", customer, pin, e164)
	if lead:
		_stamp_party("Lead", lead, pin, e164)
	# If no party yet but brand-new phone, still stamp nothing — caller links later.
	frappe.db.commit()

	portal = f"{_portal_base_url()}/cliente"
	send_result = {"sent": False, "reason": "skipped"}
	if cint(send) and issued_new and pin:
		send_result = _send_pin_message(e164, pin, portal)

	return {
		"ok": True,
		"pin": pin,
		"phone_e164": e164,
		"country": iso,
		"is_new_number": bool(is_new and issued_new),
		"portal_path": "/cliente",
		"portal_url": portal,
		"send": send_result,
	}


@frappe.whitelist(allow_guest=True)
def list_consultas_by_client_access(phone=None, pin=None, page_length=50):
	"""Guest portal: list consultas for phone+PIN (no ERP User)."""
	ensure_client_access_custom_fields()
	e164 = _normalize_e164(cstr(phone or ""))
	pin = cstr(pin or "").strip()
	if not e164 or not pin or len(pin) < 4:
		frappe.throw(_("Phone and PIN are required"), frappe.ValidationError)

	digits = _digits(e164)
	tail = digits[-8:] if len(digits) >= 8 else digits

	# Validate PIN against Customer or Lead
	matched_customer = None
	matched_lead = None
	for row in frappe.get_all(
		"Customer",
		filters={"custom_client_access_pin": pin, "disabled": 0},
		or_filters=[
			["mobile_no", "like", f"%{tail}%"],
			["custom_client_phone_e164", "like", f"%{tail}%"],
		],
		fields=["name", "customer_name", "mobile_no", "custom_client_phone_e164"],
		limit_page_length=5,
		ignore_permissions=True,
	):
		cred = _digits(row.custom_client_phone_e164 or row.mobile_no)
		if cred and (cred == digits or cred.endswith(tail) or digits.endswith(cred)):
			matched_customer = row.name
			break

	if not matched_customer:
		for row in frappe.get_all(
			"Lead",
			filters={"custom_client_access_pin": pin},
			or_filters=[
				["mobile_no", "like", f"%{tail}%"],
				["whatsapp_no", "like", f"%{tail}%"],
				["custom_client_phone_e164", "like", f"%{tail}%"],
			],
			fields=["name", "lead_name", "mobile_no", "custom_client_phone_e164"],
			limit_page_length=5,
			ignore_permissions=True,
		):
			cred = _digits(row.custom_client_phone_e164 or row.mobile_no)
			if cred and (cred == digits or cred.endswith(tail) or digits.endswith(cred)):
				matched_lead = row.name
				break

	if not matched_customer and not matched_lead:
		frappe.throw(_("Invalid phone or PIN"), frappe.AuthenticationError)

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_guest_preorder_tag_fieldname,
		get_guest_preorders_list,
	)

	# Prefer customer-scoped list when available; fall back to recent guest list filter client-side.
	tag_fn = _guest_preorder_tag_fieldname()
	filters = {tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"]} if tag_fn else {}
	if matched_customer:
		filters["customer"] = matched_customer

	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"status",
			"docstatus",
			"grand_total",
			"currency",
			tag_fn,
		]
		if tag_fn
		else [
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"status",
			"docstatus",
			"grand_total",
			"currency",
		],
		order_by="modified desc",
		limit_page_length=max(1, min(cint(page_length) or 50, 100)),
		ignore_permissions=True,
	)

	# When only Lead matched (Consumidor Final), filter by guest_phone tag.
	out = []
	for r in rows:
		tag_text = cstr(r.get(tag_fn) if tag_fn else "")
		if matched_customer and r.customer == matched_customer:
			out.append(r)
			continue
		if tail and tail in _digits(tag_text):
			out.append(r)

	# Enrich display_status lightly via list helper when cheap — else raw status.
	try:
		listed = get_guest_preorders_list(page_length=cint(page_length) or 50, scope="admin")
		by_name = {p.get("name"): p for p in (listed.get("preorders") or [])}
	except Exception:
		by_name = {}

	consultas = []
	for r in out:
		extra = by_name.get(r.name) or {}
		consultas.append(
			{
				"name": r.name,
				"customer": r.customer,
				"customer_name": r.customer_name,
				"transaction_date": r.transaction_date,
				"delivery_date": r.delivery_date,
				"status": r.status,
				"display_status": extra.get("display_status") or r.status,
				"grand_total": r.grand_total,
				"currency": r.currency,
			}
		)

	return {
		"ok": True,
		"customer": matched_customer,
		"lead": matched_lead,
		"consultas": consultas,
	}
