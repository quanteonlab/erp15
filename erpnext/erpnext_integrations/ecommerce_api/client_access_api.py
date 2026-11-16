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
from frappe.utils import cstr, cint, flt

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


# Client PIN: 6 digits, first digit < 5 (0–4). Staff ops PINs use first digit > 3 (4–9).
CLIENT_ACCESS_PIN_LEN = 6
CLIENT_ACCESS_PIN_FIRST_DIGITS = "01234"


def _client_pin_in_use(pin: str) -> bool:
	raw = cstr(pin or "").strip()
	if not raw:
		return False
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		if frappe.db.exists("Customer", {"custom_client_access_pin": raw}):
			return True
	if frappe.db.has_column("Lead", "custom_client_access_pin"):
		if frappe.db.exists("Lead", {"custom_client_access_pin": raw}):
			return True
	return False


def _new_pin() -> str:
	"""Mint a client-access PIN: 6 digits, first digit in 0–4 (below 5)."""
	ensure_client_access_custom_fields()
	for _ in range(120):
		first = secrets.choice(CLIENT_ACCESS_PIN_FIRST_DIGITS)
		rest = f"{secrets.randbelow(100_000):05d}"
		candidate = f"{first}{rest}"
		if _client_pin_in_use(candidate):
			continue
		try:
			from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
				employee_ops_pin_taken,
			)
			from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
				_verify_pin_value,
			)

			if employee_ops_pin_taken(candidate) or _verify_pin_value(candidate):
				continue
		except Exception:
			pass
		return candidate
	frappe.throw(_("Could not allocate a unique client PIN"))


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
def resolve_client_access_pin(pin=None):
	"""Resolve a 6-digit CRM client PIN for login / roleplay (no ERP User).

	Employee ops PINs and the admin PIN are rejected — staff must not sign in
	via PIN; only clients may.
	"""
	ensure_client_access_custom_fields()
	raw = cstr(pin or "").strip()
	if not raw.isdigit() or len(raw) != CLIENT_ACCESS_PIN_LEN:
		return {"ok": False, "reason": "invalid", "kind": None}

	# Staff / admin PINs must never authenticate as a client.
	try:
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
			employee_ops_pin_taken,
		)
		from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
			_verify_pin_value,
		)

		if _verify_pin_value(raw) or employee_ops_pin_taken(raw):
			return {"ok": False, "reason": "staff_pin", "kind": "staff"}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "resolve_client_access_pin staff check")

	customer = None
	customer_name = None
	phone_e164 = None
	lead = None
	lead_name = None

	default_price_list = None
	catalog_hide_prices = 0
	catalog_lang = "es"

	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		try:
			from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
				ensure_catalog_price_fields,
				normalize_catalog_lang,
			)

			ensure_catalog_price_fields()
		except Exception:
			normalize_catalog_lang = lambda v: "es"  # noqa: E731
		cust_fields = ["name", "customer_name", "mobile_no", "custom_client_phone_e164", "default_price_list"]
		if frappe.db.has_column("Customer", "custom_catalog_hide_prices"):
			cust_fields.append("custom_catalog_hide_prices")
		if frappe.db.has_column("Customer", "custom_catalog_lang"):
			cust_fields.append("custom_catalog_lang")
		row = frappe.db.get_value(
			"Customer",
			{"custom_client_access_pin": raw, "disabled": 0},
			cust_fields,
			as_dict=True,
		)
		if row:
			customer = row.name
			customer_name = cstr(row.customer_name or row.name).strip() or row.name
			phone_e164 = _normalize_e164(
				cstr(getattr(row, "custom_client_phone_e164", None) or row.mobile_no or "")
			) or None
			default_price_list = cstr(getattr(row, "default_price_list", None) or "").strip() or None
			catalog_hide_prices = 1 if cint(getattr(row, "custom_catalog_hide_prices", 0)) else 0
			catalog_lang = normalize_catalog_lang(getattr(row, "custom_catalog_lang", None))

	if not customer and frappe.db.has_column("Lead", "custom_client_access_pin"):
		fields = ["name", "lead_name", "mobile_no", "whatsapp_no", "phone"]
		if frappe.db.has_column("Lead", "custom_client_phone_e164"):
			fields.append("custom_client_phone_e164")
		row = frappe.db.get_value(
			"Lead",
			{"custom_client_access_pin": raw},
			fields,
			as_dict=True,
		)
		if row:
			lead = row.name
			lead_name = cstr(row.lead_name or row.name).strip() or row.name
			phone_e164 = _normalize_e164(
				cstr(
					getattr(row, "custom_client_phone_e164", None)
					or row.whatsapp_no
					or row.mobile_no
					or row.phone
					or ""
				)
			) or None

	if not customer and not lead:
		return {"ok": False, "reason": "not_found", "kind": None}

	return {
		"ok": True,
		"kind": "client",
		"reason": None,
		"customer": customer,
		"customer_name": customer_name or lead_name,
		"lead": lead,
		"phone_e164": phone_e164,
		"pin": raw,
		"portal_path": "/cliente",
		"default_price_list": default_price_list,
		"catalog_hide_prices": catalog_hide_prices,
		"catalog_lang": catalog_lang,
	}


def _authenticate_client(phone=None, pin=None):
	"""Match a guest portal session. Raises a controlled error on bad input."""
	ensure_client_access_custom_fields()
	e164 = _normalize_e164(cstr(phone or ""))
	pin = cstr(pin or "").strip()
	if not e164 or not pin or len(pin) < 4:
		frappe.throw(_("Phone and PIN are required"), frappe.ValidationError)

	digits = _digits(e164)
	tail = digits[-8:] if len(digits) >= 8 else digits

	matched_customer = None
	matched_lead = None
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
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

	if not matched_customer and frappe.db.has_column("Lead", "custom_client_access_pin"):
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

	return {
		"e164": e164,
		"pin": pin,
		"tail": tail,
		"customer": matched_customer,
		"lead": matched_lead,
	}


def _pin_taken_by_other(pin: str, customer=None, lead=None) -> bool:
	raw = cstr(pin or "").strip()
	if not raw:
		return False
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		owner = frappe.db.get_value(
			"Customer", {"custom_client_access_pin": raw}, "name"
		)
		if owner and owner != customer:
			return True
	if frappe.db.has_column("Lead", "custom_client_access_pin"):
		owner = frappe.db.get_value("Lead", {"custom_client_access_pin": raw}, "name")
		if owner and owner != lead:
			return True
	try:
		from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
			employee_ops_pin_taken,
		)
		from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
			_verify_pin_value,
		)

		if employee_ops_pin_taken(raw) or _verify_pin_value(raw):
			return True
	except Exception:
		pass
	return False


def _optional_coord(value):
	raw = cstr(value if value is not None else "").strip()
	if not raw or raw.lower() in ("null", "undefined", "none"):
		return None
	try:
		return flt(value)
	except Exception:
		return None


def _primary_address_fields(addr_name) -> dict:
	"""Street + locality + map pin from the customer's primary Address."""
	empty = {
		"address_line1": "",
		"address_line2": "",
		"city": "",
		"state": "",
		"pincode": "",
		"country": "",
		"lat": None,
		"lng": None,
	}
	if not addr_name or not frappe.db.exists("Address", addr_name):
		return empty
	cols = ["address_line1", "address_line2", "city", "state", "pincode", "country"]
	for col in ("custom_latitude", "custom_longitude"):
		if frappe.db.has_column("Address", col):
			cols.append(col)
	row = frappe.db.get_value("Address", addr_name, cols, as_dict=True) or {}
	city = cstr(row.get("city") or "").strip()
	if city in ("-", "null", "undefined", "none"):
		city = ""

	def num(key):
		raw = row.get(key)
		if raw in (None, ""):
			return None
		try:
			return flt(raw)
		except Exception:
			return None

	return {
		"address_line1": cstr(row.get("address_line1") or "").strip(),
		"address_line2": cstr(row.get("address_line2") or "").strip(),
		"city": city,
		"state": cstr(row.get("state") or "").strip(),
		"pincode": cstr(row.get("pincode") or "").strip(),
		"country": cstr(row.get("country") or "").strip(),
		"lat": num("custom_latitude"),
		"lng": num("custom_longitude"),
	}


def _account_payload(auth: dict) -> dict:
	"""Contact fields the guest may see and edit."""
	customer = auth.get("customer")
	lead = auth.get("lead")
	out = {
		"ok": True,
		"customer": customer,
		"lead": lead,
		"customer_name": "",
		"email": "",
		"mobile_no": "",
		"client_phone_e164": auth.get("e164") or "",
		"pin": auth.get("pin") or "",
		"tax_id": "",
		"tax_category": "",
		"address_line1": "",
		"address_line2": "",
		"city": "",
		"state": "",
		"pincode": "",
		"country": "",
		"lat": None,
		"lng": None,
		"preferred_hours": "",
		"zone": "",
		"catalog_lang": "es",
	}
	if customer and frappe.db.exists("Customer", customer):
		fields = [
			"name",
			"customer_name",
			"email_id",
			"mobile_no",
			"tax_id",
			"tax_category",
			"customer_primary_address",
		]
		for extra in (
			"custom_client_phone_e164",
			"custom_client_access_pin",
			"custom_preferred_hours",
			"custom_catalog_lang",
		):
			if frappe.db.has_column("Customer", extra):
				fields.append(extra)
		row = frappe.db.get_value("Customer", customer, fields, as_dict=True) or {}
		addr_name = row.get("customer_primary_address")
		if not addr_name or not frappe.db.exists("Address", addr_name):
			linked = frappe.get_all(
				"Address",
				filters=[
					["Dynamic Link", "link_doctype", "=", "Customer"],
					["Dynamic Link", "link_name", "=", customer],
					["disabled", "=", 0],
				],
				pluck="name",
				order_by="`tabAddress`.is_primary_address desc, `tabAddress`.modified desc",
				limit_page_length=1,
				ignore_permissions=True,
			)
			addr_name = linked[0] if linked else None
		addr = _primary_address_fields(addr_name)
		zone = ""
		try:
			from erpnext.erpnext_integrations.ecommerce_api.api import (
				_customer_delivery_zone_map,
			)

			zone = cstr(_customer_delivery_zone_map([customer]).get(customer) or "")
		except Exception:
			zone = ""
		try:
			from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
				normalize_catalog_lang,
			)

			lang = normalize_catalog_lang(row.get("custom_catalog_lang"))
		except Exception:
			lang = "es"
		out.update(
			{
				"customer_name": cstr(row.get("customer_name") or ""),
				"email": cstr(row.get("email_id") or ""),
				"mobile_no": cstr(row.get("mobile_no") or ""),
				"client_phone_e164": cstr(
					row.get("custom_client_phone_e164") or row.get("mobile_no") or auth.get("e164") or ""
				),
				"pin": cstr(row.get("custom_client_access_pin") or auth.get("pin") or ""),
				"tax_id": cstr(row.get("tax_id") or ""),
				"tax_category": cstr(row.get("tax_category") or ""),
				"address_line1": addr["address_line1"],
				"address_line2": addr["address_line2"],
				"city": addr["city"],
				"state": addr["state"],
				"pincode": addr["pincode"],
				"country": addr["country"],
				"lat": addr["lat"],
				"lng": addr["lng"],
				"preferred_hours": cstr(row.get("custom_preferred_hours") or ""),
				"zone": zone,
				"catalog_lang": lang,
			}
		)
	elif lead and frappe.db.exists("Lead", lead):
		fields = ["name", "lead_name", "email_id", "mobile_no"]
		for extra in ("custom_client_phone_e164", "custom_client_access_pin"):
			if frappe.db.has_column("Lead", extra):
				fields.append(extra)
		row = frappe.db.get_value("Lead", lead, fields, as_dict=True) or {}
		out.update(
			{
				"customer_name": cstr(row.get("lead_name") or ""),
				"email": cstr(row.get("email_id") or ""),
				"mobile_no": cstr(row.get("mobile_no") or ""),
				"client_phone_e164": cstr(
					row.get("custom_client_phone_e164") or row.get("mobile_no") or auth.get("e164") or ""
				),
				"pin": cstr(row.get("custom_client_access_pin") or auth.get("pin") or ""),
			}
		)
	return out


@frappe.whitelist(allow_guest=True)
def get_client_account(phone=None, pin=None):
	"""Guest portal: contact card for the phone+PIN account."""
	return _account_payload(_authenticate_client(phone, pin))


@frappe.whitelist(allow_guest=True)
def update_client_account(
	phone=None,
	pin=None,
	customer_name=None,
	email=None,
	mobile_no=None,
	client_phone_e164=None,
	client_access_pin=None,
	tax_id=None,
	tax_category=None,
	address_line1=None,
	address_line2=None,
	city=None,
	state=None,
	pincode=None,
	country=None,
	lat=None,
	lng=None,
	preferred_hours=None,
	zone=None,
	catalog_lang=None,
):
	"""Guest portal: edit own contact fields, including the access PIN and map locality."""
	auth = _authenticate_client(phone, pin)
	customer = auth.get("customer")
	lead = auth.get("lead")
	new_pin = cstr(client_access_pin or "").strip()
	if new_pin and new_pin != auth.get("pin"):
		if not new_pin.isdigit() or len(new_pin) != CLIENT_ACCESS_PIN_LEN or new_pin[0] not in CLIENT_ACCESS_PIN_FIRST_DIGITS:
			frappe.throw(
				_("PIN must be 6 digits and start with 0–4"),
				frappe.ValidationError,
			)
		if _pin_taken_by_other(new_pin, customer=customer, lead=lead):
			frappe.throw(_("That PIN is already in use"), frappe.ValidationError)

	phone_raw = client_phone_e164 if client_phone_e164 is not None else mobile_no
	new_e164 = None
	if phone_raw is not None and cstr(phone_raw).strip():
		new_e164 = _normalize_e164(cstr(phone_raw))
		if not new_e164 or len(_digits(new_e164)) < 8:
			frappe.throw(_("Enter a valid phone number"), frappe.ValidationError)

	lat_f = _optional_coord(lat)
	lng_f = _optional_coord(lng)
	has_geo = lat_f is not None and lng_f is not None and (lat_f or lng_f)
	line1 = cstr(address_line1).strip() if address_line1 is not None else ""
	if line1.lower() in ("null", "undefined", "none"):
		line1 = ""

	if customer:
		patch = {}
		if customer_name is not None and cstr(customer_name).strip():
			patch["customer_name"] = cstr(customer_name).strip()
		if email is not None:
			patch["email_id"] = cstr(email).strip()
		if new_e164:
			patch["mobile_no"] = new_e164
			patch["client_phone_e164"] = new_e164
		elif mobile_no is not None:
			patch["mobile_no"] = cstr(mobile_no).strip()
		if new_pin:
			patch["client_access_pin"] = new_pin
		if tax_id is not None:
			patch["tax_id"] = cstr(tax_id).strip()
		if tax_category is not None:
			patch["tax_category"] = cstr(tax_category).strip()
		if address_line1 is not None and not has_geo:
			patch["address_line1"] = line1
		if preferred_hours is not None:
			patch["preferred_hours"] = cstr(preferred_hours).strip()
		if zone is not None:
			patch["zone"] = cstr(zone).strip()
		if catalog_lang is not None:
			patch["catalog_lang"] = catalog_lang
		if patch:
			from erpnext.erpnext_integrations.ecommerce_api.api import update_customer

			update_customer(customer, **patch)
		if has_geo and line1:
			from erpnext.erpnext_integrations.ecommerce_api.api import upsert_customer_address_place

			upsert_customer_address_place(
				customer=customer,
				address_line1=line1,
				address_line2=cstr(address_line2 or "").strip(),
				city=cstr(city or "").strip(),
				state=cstr(state or "").strip(),
				pincode=cstr(pincode or "").strip(),
				country=cstr(country or "").strip(),
				lat=lat_f,
				lng=lng_f,
			)
	elif lead:
		frappe.flags.ignore_permissions = True
		doc = frappe.get_doc("Lead", lead)
		if customer_name is not None and cstr(customer_name).strip():
			doc.lead_name = cstr(customer_name).strip()
		if email is not None:
			doc.email_id = cstr(email).strip()
		if new_e164:
			doc.mobile_no = new_e164
			if frappe.db.has_column("Lead", "custom_client_phone_e164"):
				doc.custom_client_phone_e164 = new_e164
		if new_pin and frappe.db.has_column("Lead", "custom_client_access_pin"):
			doc.custom_client_access_pin = new_pin
		doc.save(ignore_permissions=True)
		frappe.flags.ignore_permissions = False
		frappe.db.commit()

	next_phone = new_e164 or auth.get("e164")
	next_pin = new_pin or auth.get("pin")
	payload = _account_payload(_authenticate_client(next_phone, next_pin))
	# Leads have no Customer catalog_lang column; keep the choice on the response
	# so the portal can store it locally until the account becomes a customer.
	if not customer and catalog_lang is not None:
		try:
			from erpnext.erpnext_integrations.ecommerce_api.crm_customer_fields import (
				normalize_catalog_lang,
			)

			payload["catalog_lang"] = normalize_catalog_lang(catalog_lang)
		except Exception:
			payload["catalog_lang"] = "zh" if cstr(catalog_lang).strip().lower() in ("zh", "ch") else "es"
	return payload


@frappe.whitelist(allow_guest=True)
def list_consultas_by_client_access(phone=None, pin=None, page_length=50):
	"""Guest portal: list orders for phone+PIN (no ERP User)."""
	auth = _authenticate_client(phone, pin)
	tail = auth["tail"]
	matched_customer = auth["customer"]
	matched_lead = auth["lead"]

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
