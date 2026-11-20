"""Client access for catalog guests — not ERP Users.

Two security flows:

1. **Browser temp session** (default walk-in): id+secret live in the browser
   (Chrome session / local). Server stores only a secret *hash* in Table Extra
   Data and tags Sales Orders with ``guest_browser:<id>``. No Customer / PIN.
2. **Phone + PIN** (cross-device claim): Twilio WhatsApp/SMS PIN on Lead or a
   real Customer — never on the shared Consumidor Final bucket.
"""

from __future__ import annotations

import hashlib
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
	supplier_defs = [
		{
			"fieldname": "custom_supplier_access_pin",
			"fieldtype": "Data",
			"label": "Supplier Access PIN",
			"insert_after": "mobile_no",
			"read_only": 1,
		},
		{
			"fieldname": "custom_supplier_phone_e164",
			"fieldtype": "Data",
			"label": "Supplier Phone (E.164)",
			"insert_after": "custom_supplier_access_pin",
			"read_only": 1,
		},
	]
	create_custom_fields(
		{"Customer": defs, "Lead": defs, "Supplier": supplier_defs},
		ignore_validate=True,
	)
	frappe.clear_cache(doctype="Customer")
	frappe.clear_cache(doctype="Lead")
	frappe.clear_cache(doctype="Supplier")
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
	"""True when pin is on Customer, Lead, or Supplier (party access fields)."""
	raw = cstr(pin or "").strip()
	if not raw:
		return False
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		for row in frappe.get_all(
			"Customer",
			filters={"custom_client_access_pin": raw},
			fields=["name", "customer_name"],
			limit_page_length=5,
			ignore_permissions=True,
		):
			if not _customer_is_bucket(row.name, row.customer_name):
				return True
	if frappe.db.has_column("Lead", "custom_client_access_pin"):
		if frappe.db.exists("Lead", {"custom_client_access_pin": raw}):
			return True
	if frappe.db.has_column("Supplier", "custom_supplier_access_pin"):
		if frappe.db.exists("Supplier", {"custom_supplier_access_pin": raw}):
			return True
	return False


def pin_in_use_global(pin: str) -> bool:
	"""Party PINs + staff ops store + admin PIN — shared mint guard."""
	raw = cstr(pin or "").strip()
	if not raw:
		return False
	if _client_pin_in_use(raw):
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


def _new_pin() -> str:
	"""Mint a client/supplier-access PIN: 6 digits, first digit in 0–4 (below 5)."""
	ensure_client_access_custom_fields()
	for _ in range(120):
		first = secrets.choice(CLIENT_ACCESS_PIN_FIRST_DIGITS)
		rest = f"{secrets.randbelow(100_000):05d}"
		candidate = f"{first}{rest}"
		if pin_in_use_global(candidate):
			continue
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


def _customer_is_bucket(name=None, customer_name=None) -> bool:
	"""Walk-in placeholders (Consumidor Final / Uncategorized) must not own PINs or shared order lists."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _is_bucket_customer

	cust = cstr(name or "").strip()
	label = cstr(customer_name or "").strip()
	if cust and not label and frappe.db.exists("Customer", cust):
		label = cstr(frappe.db.get_value("Customer", cust, "customer_name") or "")
	return _is_bucket_customer(cust or None, label or None)


def _scrub_bucket_customer_credentials():
	"""Drop portal PIN/phone stamped on shared walk-in Customers (legacy leak)."""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		CONSUMIDOR_FINAL_NAME,
		UNCATEGORIZED_CUSTOMER_NAME,
	)

	if not frappe.db.has_column("Customer", "custom_client_access_pin"):
		return
	labels = (CONSUMIDOR_FINAL_NAME.lower(), UNCATEGORIZED_CUSTOMER_NAME.lower())
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabCustomer`
		WHERE LOWER(TRIM(customer_name)) IN %s
		""",
		(labels,),
	)
	for (name,) in rows or ():
		updates = {"custom_client_access_pin": None}
		if frappe.db.has_column("Customer", "custom_client_phone_e164"):
			updates["custom_client_phone_e164"] = None
		frappe.db.set_value("Customer", name, updates, update_modified=False)


def _stamp_party(doctype: str, name: str, pin: str, e164: str):
	if not name or not frappe.db.exists(doctype, name):
		return
	# Never put portal credentials on the shared walk-in Customer — every guest
	# would overwrite the same PIN and list_consultas would leak all CF orders.
	if doctype == "Customer" and _customer_is_bucket(name):
		return
	updates = {}
	if doctype == "Supplier":
		if frappe.db.has_column(doctype, "custom_supplier_access_pin"):
			updates["custom_supplier_access_pin"] = pin
		if e164 and frappe.db.has_column(doctype, "custom_supplier_phone_e164"):
			updates["custom_supplier_phone_e164"] = e164
		if e164 and frappe.db.has_column("Supplier", "mobile_no"):
			if not frappe.db.get_value("Supplier", name, "mobile_no"):
				updates["mobile_no"] = e164
	else:
		if frappe.db.has_column(doctype, "custom_client_access_pin"):
			updates["custom_client_access_pin"] = pin
		if e164 and frappe.db.has_column(doctype, "custom_client_phone_e164"):
			updates["custom_client_phone_e164"] = e164
		if doctype == "Customer" and e164 and frappe.db.has_column("Customer", "mobile_no"):
			if not frappe.db.get_value("Customer", name, "mobile_no"):
				updates["mobile_no"] = e164
	if updates:
		frappe.db.set_value(doctype, name, updates, update_modified=False)


@frappe.whitelist(allow_guest=True)
def _existing_pin_for_e164(e164: str) -> str | None:
	"""Return an admin/party PIN already linked to this phone, if any."""
	tail = _digits(e164)[-8:]
	if len(tail) < 8:
		return None
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		fields = ["name", "customer_name", "custom_client_access_pin", "mobile_no"]
		if frappe.db.has_column("Customer", "custom_client_phone_e164"):
			fields.append("custom_client_phone_e164")
		or_filters = [["mobile_no", "like", f"%{tail}%"]]
		if frappe.db.has_column("Customer", "custom_client_phone_e164"):
			or_filters.append(["custom_client_phone_e164", "like", f"%{tail}%"])
		for row in frappe.get_all(
			"Customer",
			or_filters=or_filters,
			fields=fields,
			limit_page_length=20,
			ignore_permissions=True,
		):
			if _customer_is_bucket(row.name, row.customer_name):
				continue
			cred = _digits(getattr(row, "custom_client_phone_e164", None) or row.mobile_no)
			if not cred.endswith(tail):
				continue
			pin = cstr(getattr(row, "custom_client_access_pin", None) or "").strip()
			if pin:
				return pin
	if frappe.db.has_column("Lead", "custom_client_access_pin"):
		fields = ["name", "mobile_no", "custom_client_access_pin"]
		if frappe.db.has_column("Lead", "custom_client_phone_e164"):
			fields.append("custom_client_phone_e164")
		or_filters = [["mobile_no", "like", f"%{tail}%"]]
		if frappe.db.has_column("Lead", "custom_client_phone_e164"):
			or_filters.append(["custom_client_phone_e164", "like", f"%{tail}%"])
		for row in frappe.get_all(
			"Lead",
			or_filters=or_filters,
			fields=fields,
			limit_page_length=20,
			ignore_permissions=True,
		):
			cred = _digits(getattr(row, "custom_client_phone_e164", None) or row.mobile_no)
			if not cred.endswith(tail):
				continue
			pin = cstr(getattr(row, "custom_client_access_pin", None) or "").strip()
			if pin:
				return pin
	return None


def issue_client_access_pin(
	guest_phone=None,
	guest_name=None,
	guest_email=None,
	customer=None,
	lead=None,
	allowed_countries=None,
	send=1,
	force_new=0,
	mint_if_missing=1,
):
	"""Mint (or reuse) a 6-digit PIN for a guest phone.

	Only issues a *new* PIN + outbound message when the number was never seen
	and its country ISO is in ``allowed_countries`` (default ``["AR"]``).
	Always returns the PIN when one already exists for this party.

	``mint_if_missing=0``: Consulta-only path — return an admin/party PIN if one
	already exists, but never mint a new one.
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
	# Walk-in bucket is shared — never attach PIN/phone there.
	if customer and _customer_is_bucket(customer):
		customer = None

	# Reuse existing PIN on linked party (or any party already tied to this phone).
	existing_pin = None
	if customer and frappe.db.exists("Customer", customer):
		existing_pin = frappe.db.get_value("Customer", customer, "custom_client_access_pin")
	if not existing_pin and lead and frappe.db.exists("Lead", lead):
		existing_pin = frappe.db.get_value("Lead", lead, "custom_client_access_pin")
	if not existing_pin:
		existing_pin = _existing_pin_for_e164(e164)

	seen = _phone_seen_before(e164)
	is_new = (not seen) or cint(force_new)

	pin = cstr(existing_pin or "").strip()
	issued_new = False
	if not pin and not cint(mint_if_missing):
		return {
			"ok": False,
			"reason": "no_existing_pin",
			"phone_e164": e164,
			"country": iso,
			"portal_path": "/cliente",
		}
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
		if row and not _customer_is_bucket(row.name, row.customer_name):
			customer = row.name
			customer_name = cstr(row.customer_name or row.name).strip() or row.name
			phone_e164 = _normalize_e164(
				cstr(getattr(row, "custom_client_phone_e164", None) or row.mobile_no or "")
			) or None
			default_price_list = cstr(getattr(row, "default_price_list", None) or "").strip() or None
			catalog_hide_prices = 1 if cint(getattr(row, "custom_catalog_hide_prices", 0)) else 0
			catalog_lang = normalize_catalog_lang(getattr(row, "custom_catalog_lang", None))
		elif row and _customer_is_bucket(row.name, row.customer_name):
			# Legacy PIN on Consumidor Final — scrub so it cannot gate the whole bucket.
			_scrub_bucket_customer_credentials()

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
	_scrub_bucket_customer_credentials()
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
			if _customer_is_bucket(row.name, row.customer_name):
				continue
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
		for row in frappe.get_all(
			"Customer",
			filters={"custom_client_access_pin": raw},
			fields=["name", "customer_name"],
			limit_page_length=5,
			ignore_permissions=True,
		):
			if _customer_is_bucket(row.name, row.customer_name):
				continue
			if row.name != customer:
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


def _so_tag_matches_client(tag_text: str, *, tail: str, e164: str, email: str = "") -> bool:
	"""Match guest SO tags to the portal session (phone tail / e164 / email)."""
	raw = cstr(tag_text or "")
	if not raw:
		return False
	digits = _digits(raw)
	e164_digits = _digits(e164)
	if tail and tail in digits:
		return True
	if e164_digits and len(e164_digits) >= 8 and e164_digits in digits:
		return True
	em = cstr(email or "").strip().lower()
	if em and f"guest_email:{em}" in raw.lower():
		return True
	return False


@frappe.whitelist(allow_guest=True)
def list_consultas_by_client_access(phone=None, pin=None, page_length=50):
	"""Guest portal: list orders for phone+PIN (no ERP User)."""
	auth = _authenticate_client(phone, pin)
	tail = auth["tail"]
	matched_customer = auth["customer"]
	matched_lead = auth["lead"]
	e164 = auth["e164"]

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_guest_preorder_tag_fieldname,
		get_guest_preorders_list,
	)

	# Real Customer → scope by customer. Bucket / Lead-only → guest tags + phone filter
	# (Consumidor Final is shared; never return every walk-in SO).
	tag_fn = _guest_preorder_tag_fieldname()
	filters = {tag_fn: ["like", f"%{GUEST_PREORDER_REMARKS_TAG}%"]} if tag_fn else {}
	customer_is_bucket = bool(matched_customer and _customer_is_bucket(matched_customer))
	if matched_customer and not customer_is_bucket:
		filters["customer"] = matched_customer

	limit = max(1, min(cint(page_length) or 50, 100))
	# Bucket / lead path may need a wider scan then filter by phone tag.
	fetch_limit = limit if (matched_customer and not customer_is_bucket) else max(limit * 4, 100)

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
		limit_page_length=fetch_limit,
		ignore_permissions=True,
	)

	out = []
	for r in rows:
		tag_text = cstr(r.get(tag_fn) if tag_fn else "")
		if matched_customer and not customer_is_bucket and r.customer == matched_customer:
			out.append(r)
			continue
		if _so_tag_matches_client(tag_text, tail=tail, e164=e164):
			out.append(r)
		if len(out) >= limit:
			break

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
		"customer": None if customer_is_bucket else matched_customer,
		"lead": matched_lead,
		"consultas": consultas,
	}


def _require_users_table_perm():
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _require_app_permission

	_require_app_permission("tables.employees")


@frappe.whitelist()
def list_users_clients(search=None, page=1, page_length=100):
	"""Usuarios → Clientes: identity + PIN (not full Relaciones CRM)."""
	_require_users_table_perm()
	ensure_client_access_custom_fields()
	term = cstr(search or "").strip()
	limit = max(1, min(cint(page_length) or 100, 200))
	start = max(0, (cint(page) or 1) - 1) * limit
	fields = [
		"name",
		"customer_name",
		"mobile_no",
		"disabled",
		"modified",
	]
	if frappe.db.has_column("Customer", "custom_client_access_pin"):
		fields += ["custom_client_access_pin", "custom_client_phone_e164"]
	filters = {}
	or_filters = None
	if term:
		like = f"%{term}%"
		or_filters = [
			["name", "like", like],
			["customer_name", "like", like],
			["mobile_no", "like", like],
		]
		if frappe.db.has_column("Customer", "custom_client_access_pin"):
			or_filters.append(["custom_client_access_pin", "like", like])
			or_filters.append(["custom_client_phone_e164", "like", like])
	rows = frappe.get_all(
		"Customer",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit_start=start,
		limit_page_length=limit,
		ignore_permissions=True,
	)
	clients = []
	for r in rows:
		clients.append(
			{
				"name": r.name,
				"customer_name": r.customer_name,
				"mobile_no": r.mobile_no,
				"phone_e164": getattr(r, "custom_client_phone_e164", None) or r.mobile_no,
				"client_access_pin": getattr(r, "custom_client_access_pin", None),
				"disabled": cint(r.disabled),
				"modified": r.modified,
			}
		)
	return {"clients": clients, "page": cint(page) or 1, "page_length": limit}


@frappe.whitelist()
def rotate_client_access_pin(customer=None, lead=None, send=0):
	"""Admin: mint a new client PIN and optionally re-send via Twilio."""
	_require_users_table_perm()
	ensure_client_access_custom_fields()
	customer = cstr(customer or "").strip() or None
	lead = cstr(lead or "").strip() or None
	if not customer and not lead:
		frappe.throw(_("customer or lead is required"))
	e164 = ""
	if customer and frappe.db.exists("Customer", customer):
		e164 = cstr(
			frappe.db.get_value("Customer", customer, "custom_client_phone_e164")
			or frappe.db.get_value("Customer", customer, "mobile_no")
			or ""
		)
	if not e164 and lead and frappe.db.exists("Lead", lead):
		e164 = cstr(
			frappe.db.get_value("Lead", lead, "custom_client_phone_e164")
			or frappe.db.get_value("Lead", lead, "mobile_no")
			or ""
		)
	e164 = _normalize_e164(e164) or e164
	pin = _new_pin()
	if customer:
		_stamp_party("Customer", customer, pin, e164)
	if lead:
		_stamp_party("Lead", lead, pin, e164)
	frappe.db.commit()
	portal = f"{_portal_base_url()}/cliente"
	send_result = {"sent": False, "reason": "skipped"}
	if cint(send) and e164:
		send_result = _send_pin_message(e164, pin, portal)
	return {
		"ok": True,
		"pin": pin,
		"customer": customer,
		"lead": lead,
		"phone_e164": e164 or None,
		"portal_path": "/cliente",
		"portal_url": portal,
		"send": send_result,
	}


@frappe.whitelist()
def list_users_suppliers(search=None, page=1, page_length=100):
	"""Usuarios → Proveedores: contact + estimated PIN + recent PO counts."""
	_require_users_table_perm()
	ensure_client_access_custom_fields()
	term = cstr(search or "").strip()
	limit = max(1, min(cint(page_length) or 100, 200))
	start = max(0, (cint(page) or 1) - 1) * limit
	fields = ["name", "supplier_name", "mobile_no", "email_id", "disabled", "modified"]
	if frappe.db.has_column("Supplier", "custom_supplier_access_pin"):
		fields += ["custom_supplier_access_pin", "custom_supplier_phone_e164"]
	filters = {}
	or_filters = None
	if term:
		like = f"%{term}%"
		or_filters = [
			["name", "like", like],
			["supplier_name", "like", like],
			["mobile_no", "like", like],
			["email_id", "like", like],
		]
	rows = frappe.get_all(
		"Supplier",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="supplier_name asc",
		limit_start=start,
		limit_page_length=limit,
		ignore_permissions=True,
	)
	names = [r.name for r in rows]
	po_stats = {}
	if names:
		# Recent open/submitted POs per supplier (cheap aggregate).
		for row in frappe.db.sql(
			"""
			select supplier, count(*) as cnt, max(transaction_date) as last_date
			from `tabPurchase Order`
			where supplier in %(names)s and docstatus < 2
			group by supplier
			""",
			{"names": names},
			as_dict=True,
		):
			po_stats[row.supplier] = {
				"po_count": cint(row.cnt),
				"last_po_date": row.last_date,
			}
	suppliers = []
	for r in rows:
		st = po_stats.get(r.name) or {}
		suppliers.append(
			{
				"name": r.name,
				"supplier_name": r.supplier_name,
				"mobile_no": r.mobile_no,
				"email": r.email_id,
				"phone_e164": getattr(r, "custom_supplier_phone_e164", None) or r.mobile_no,
				"supplier_access_pin": getattr(r, "custom_supplier_access_pin", None),
				"disabled": cint(r.disabled),
				"po_count": st.get("po_count") or 0,
				"last_po_date": st.get("last_po_date"),
				"modified": r.modified,
			}
		)
	return {"suppliers": suppliers, "page": cint(page) or 1, "page_length": limit}


@frappe.whitelist()
def ensure_supplier_access_pin(supplier=None, rotate=0):
	"""Mint or reuse estimated supplier PIN (no portal/SMS in v1)."""
	_require_users_table_perm()
	ensure_client_access_custom_fields()
	supplier = cstr(supplier or "").strip()
	if not supplier:
		frappe.throw(_("supplier is required"))
	if not frappe.db.exists("Supplier", supplier):
		frappe.throw(_("Supplier not found"))
	existing = None
	if frappe.db.has_column("Supplier", "custom_supplier_access_pin"):
		existing = cstr(frappe.db.get_value("Supplier", supplier, "custom_supplier_access_pin") or "").strip()
	pin = existing
	if cint(rotate) or not pin:
		pin = _new_pin()
	e164 = cstr(
		frappe.db.get_value("Supplier", supplier, "custom_supplier_phone_e164")
		or frappe.db.get_value("Supplier", supplier, "mobile_no")
		or ""
	)
	e164 = _normalize_e164(e164) or e164
	_stamp_party("Supplier", supplier, pin, e164)
	frappe.db.commit()
	return {
		"ok": True,
		"supplier": supplier,
		"pin": pin,
		"phone_e164": e164 or None,
		"rotated": bool(cint(rotate) or not existing),
	}


# ---------------------------------------------------------------------------
# Temporary browser session (walk-in) — not a Customer account
# ---------------------------------------------------------------------------

GUEST_BROWSER_KV_SCOPE = "guest_browser_session"
GUEST_BROWSER_TAG_PREFIX = "guest_browser:"
_GUEST_BROWSER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_GUEST_BROWSER_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")


def _guest_browser_secret_hash(browser_id: str, secret: str) -> str:
	return hashlib.sha256(f"{browser_id}:{secret}".encode("utf-8")).hexdigest()


def _normalize_guest_browser_creds(browser_id=None, secret=None) -> tuple[str, str]:
	bid = cstr(browser_id or "").strip()
	sec = cstr(secret or "").strip()
	if not _GUEST_BROWSER_ID_RE.match(bid) or not _GUEST_BROWSER_SECRET_RE.match(sec):
		frappe.throw(_("Invalid browser session"), frappe.ValidationError)
	return bid, sec


def ensure_guest_browser_session(browser_id=None, secret=None):
	"""Register (or verify) a temp browser session. Stores hash only — not the secret."""
	from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get, kv_set

	bid, sec = _normalize_guest_browser_creds(browser_id, secret)
	digest = _guest_browser_secret_hash(bid, sec)
	_name, row = kv_get(GUEST_BROWSER_KV_SCOPE, bid)
	if row.get("secret_hash"):
		if not secrets.compare_digest(cstr(row.get("secret_hash")), digest):
			frappe.throw(_("Invalid browser session"), frappe.AuthenticationError)
		row["touched_at"] = frappe.utils.now_datetime().isoformat()
		kv_set(GUEST_BROWSER_KV_SCOPE, bid, row)
		frappe.db.commit()
		return {"ok": True, "browser_id": bid, "created": False}
	kv_set(
		GUEST_BROWSER_KV_SCOPE,
		bid,
		{
			"secret_hash": digest,
			"created_at": frappe.utils.now_datetime().isoformat(),
			"touched_at": frappe.utils.now_datetime().isoformat(),
		},
	)
	frappe.db.commit()
	return {"ok": True, "browser_id": bid, "created": True}


@frappe.whitelist(allow_guest=True)
def register_guest_browser_session(browser_id=None, secret=None):
	"""Whitelist wrapper so the storefront can mint a temp session before checkout."""
	return ensure_guest_browser_session(browser_id=browser_id, secret=secret)


@frappe.whitelist(allow_guest=True)
def list_consultas_by_guest_browser(browser_id=None, secret=None, page_length=50):
	"""Guest portal (this device): orders tagged to this browser session only."""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_guest_preorder_tag_fieldname,
		get_guest_preorders_list,
	)

	bid, sec = _normalize_guest_browser_creds(browser_id, secret)
	ensure_guest_browser_session(browser_id=bid, secret=sec)

	tag_fn = _guest_preorder_tag_fieldname()
	needle = f"{GUEST_BROWSER_TAG_PREFIX}{bid}"
	filters = {}
	if tag_fn:
		filters[tag_fn] = ["like", f"%{needle}%"]
	else:
		frappe.throw(_("Guest order tags unavailable"), frappe.ValidationError)

	limit = max(1, min(cint(page_length) or 50, 100))
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
		],
		order_by="modified desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)

	# Defense in depth: require guest_preorder tag + exact browser id token.
	out = []
	for r in rows:
		tag_text = cstr(r.get(tag_fn) or "")
		if GUEST_PREORDER_REMARKS_TAG not in tag_text:
			continue
		if needle not in tag_text:
			continue
		out.append(r)

	try:
		listed = get_guest_preorders_list(page_length=limit, scope="admin")
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
		"browser_id": bid,
		"kind": "browser_session",
		"consultas": consultas,
	}


def _selftest_guest_browser_isolation():
	"""Dev helper: two browser sessions must not share walk-in orders."""
	from erpnext.erpnext_integrations.ecommerce_api import api

	frappe.set_user("Administrator")
	bid = secrets.token_hex(16)
	sec = secrets.token_hex(24)
	ensure_guest_browser_session(browser_id=bid, secret=sec)
	try:
		list_consultas_by_guest_browser(browser_id=bid, secret=secrets.token_hex(24))
		frappe.throw("wrong secret must fail")
	except frappe.AuthenticationError:
		pass
	item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name")
	if not item:
		frappe.throw("need a sales item")
	created = api.create_guest_preorder(
		items=[{"item_code": item, "qty": 1, "rate": 10}],
		guest_name="Browser Session Smoke",
		guest_browser_id=bid,
		guest_browser_secret=sec,
		send_client_pin=0,
	)
	name = cstr(created.get("preorder_name") or created.get("name") or "").strip()
	listed = list_consultas_by_guest_browser(browser_id=bid, secret=sec)
	assert name in [c["name"] for c in listed["consultas"]], listed
	bid2, sec2 = secrets.token_hex(16), secrets.token_hex(24)
	ensure_guest_browser_session(browser_id=bid2, secret=sec2)
	listed2 = list_consultas_by_guest_browser(browser_id=bid2, secret=sec2)
	assert name not in [c["name"] for c in listed2["consultas"]], listed2
	return {"ok": True, "preorder": name, "browser_id": bid}
