"""AFIP (Argentina) electronic invoicing — WSAA auth + WSFEv1 CAE requests.

Wraps the vendored pyafipws library (erpnext_integrations/vendor/pyafipws).
Settings (CUIT, punto de venta, cert/key, environment) live in the
"AFIP Settings" single DocType.

External callers (POS apps, other services) authenticate the same way as
the other guest-allowed endpoints in this package: an app-link token in
the `erp_<key_id>_<secret>` format, verified via device_link_api.
"""

from __future__ import annotations

import html
import os
import sys

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime
from frappe.utils.file_manager import get_file_path

from erpnext.erpnext_integrations.ecommerce_api.device_link_api import _require_link_token

VENDOR_PKG = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "pyafipws"
)
# Parent must be on sys.path so `import pyafipws.*` works (wsaa/wsfev1 use that style).
VENDOR_PARENT = os.path.dirname(VENDOR_PKG)
for _p in (VENDOR_PARENT, VENDOR_PKG):
	if _p not in sys.path:
		sys.path.insert(0, _p)


def _require_link_token_or_session(link_token: str) -> None:
	"""Guest callers (POS device using the app-link token) must pass a valid
	link_token. Authenticated callers (the Next.js admin, via site API key)
	are trusted the same way every other ecommerce_api endpoint trusts them.
	"""
	if frappe.session.user != "Guest":
		return
	_require_link_token(link_token or "")


def _get_settings():
	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	frappe.flags.ignore_permissions = False
	if not settings.enabled:
		frappe.throw(_("AFIP integration is not enabled. Configure AFIP Settings first."))
	if not (settings.cuit and settings.punto_venta and settings.certificate and settings.private_key):
		frappe.throw(_("AFIP Settings is missing CUIT, Punto de Venta, certificate or private key."))
	return settings


def _wsaa_login(settings):
	from pyafipws.wsaa import WSAA

	cert = get_file_path(settings.certificate)
	key = get_file_path(settings.private_key)
	is_prod = settings.environment == "produccion"

	wsaa_url = (
		"https://wsaa.afip.gov.ar/ws/services/LoginCms"
		if is_prod
		else "https://wsaahomo.afip.gov.ar/ws/services/LoginCms"
	)

	wsaa = WSAA()
	ta = wsaa.Autenticar("wsfe", cert, key, wsdl=wsaa_url)
	if not ta:
		frappe.throw(_("WSAA authentication failed: {0}").format(wsaa.Excepcion or wsaa.ErrMsg))
	frappe.flags.ignore_permissions = True
	frappe.db.set_value("AFIP Settings", "AFIP Settings", "last_wsaa_login", now_datetime())
	frappe.db.set_value("AFIP Settings", "AFIP Settings", "last_error", None)
	frappe.db.commit()
	frappe.flags.ignore_permissions = False
	return wsaa


def _wsfev1_client(settings):
	from pyafipws.wsfev1 import WSFEv1

	wsaa = _wsaa_login(settings)
	is_prod = settings.environment == "produccion"
	wsfev1_url = (
		"https://servicios1.afip.gov.ar/wsfev1/service.asmx"
		if is_prod
		else "https://wswhomo.afip.gov.ar/wsfev1/service.asmx"
	)

	wsfe = WSFEv1()
	wsfe.Cuit = settings.cuit
	wsfe.Token = wsaa.Token
	wsfe.Sign = wsaa.Sign
	wsfe.Conectar(wsdl=wsfev1_url)
	return wsfe


def _next_invoice_number(wsfe, punto_venta: int, invoice_type: int) -> int:
	last = wsfe.CompUltimoAutorizado(invoice_type, punto_venta)
	return cint(last) + 1


def _record_error(message: str) -> None:
	frappe.flags.ignore_permissions = True
	frappe.db.set_value("AFIP Settings", "AFIP Settings", "last_error", message[:1900])
	frappe.db.commit()


@frappe.whitelist(allow_guest=True)
def solicitar_cae(
	link_token=None,
	invoice_type=None,
	doc_type=99,
	doc_number=0,
	importe_total=0,
	importe_neto=0,
	importe_iva=0,
	iva_id=5,
	concepto=1,
	moneda_id="PES",
	moneda_cotizacion=1,
):
	"""Request a CAE from AFIP WSFEv1 for a single invoice/receipt.

	doc_type: AFIP tipo de documento (99 = Consumidor Final, 80 = CUIT, 96 = DNI, ...)
	iva_id: AFIP alicuota id (5 = 21%, 4 = 10.5%, 3 = 0%, ...)
	"""
	_require_link_token_or_session(link_token or "")

	settings = _get_settings()
	punto_venta = cint(settings.punto_venta)
	tipo_cbte = cint(invoice_type or settings.default_invoice_type)

	importe_total = flt(importe_total)
	importe_neto = flt(importe_neto)
	importe_iva = flt(importe_iva)

	try:
		wsfe = _wsfev1_client(settings)
		nro_cbte = _next_invoice_number(wsfe, punto_venta, tipo_cbte)
		hoy = now_datetime().strftime("%Y%m%d")

		wsfe.CrearFactura(
			concepto=cint(concepto),
			tipo_doc=cint(doc_type),
			nro_doc=cint(doc_number or 0),
			tipo_cbte=tipo_cbte,
			punto_vta=punto_venta,
			cbt_desde=nro_cbte,
			cbt_hasta=nro_cbte,
			imp_total=importe_total,
			imp_tot_conc=0,
			imp_neto=importe_neto,
			imp_iva=importe_iva,
			imp_trib=0,
			imp_op_ex=0,
			fecha_cbte=hoy,
			moneda_id=moneda_id,
			moneda_ctz=flt(moneda_cotizacion),
		)
		if importe_iva:
			wsfe.AgregarIva(iva_id=cint(iva_id), base_imp=importe_neto, importe=importe_iva)

		wsfe.CAESolicitar()

		if wsfe.ErrMsg:
			_record_error(wsfe.ErrMsg)
			frappe.throw(_explain_afip_rejection(wsfe.ErrMsg, tipo_cbte, punto_venta))

		return {
			"ok": True,
			"cae": wsfe.CAE,
			"cae_vencimiento": wsfe.Vencimiento,
			"punto_venta": punto_venta,
			"tipo_cbte": tipo_cbte,
			"nro_cbte": nro_cbte,
			"resultado": wsfe.Resultado,
			"observaciones": wsfe.Obs,
			"cuit": settings.cuit,
			"importe_total": importe_total,
		}
	except frappe.exceptions.ValidationError:
		raise
	except Exception as e:
		_record_error(str(e))
		frappe.throw(_("AFIP request failed: {0}").format(e))


def _explain_afip_rejection(err_msg: str, tipo_cbte: int, punto_venta: int) -> str:
	"""Turn common WSFEv1 codes into actionable setup hints."""
	raw = (err_msg or "").strip()
	hints = []
	upper = raw.upper()
	if "10000" in upper or "RESPONSABLE INSCRIPTO" in upper:
		hints.append(
			_(
				"Code 10000: this CUIT is not IVA Responsable Inscripto, so Factura A/B "
				"are not allowed. Use Default Invoice Type 11 (Factura C) for Monotributo / CF."
			)
		)
	if "10005" in upper or "TIPO RECE" in upper or "PUNTO DE VENTA" in upper:
		hints.append(
			_(
				"Code 10005: Punto de Venta {0} is missing or not type RECE (Web Services) "
				"in AFIP for this environment. Register it under Regímenes de Facturación → "
				"Puntos de Venta (tipo “Web Services” / electrónico), then put that number "
				"in AFIP Settings."
			).format(punto_venta)
		)
	if tipo_cbte in (1, 2, 3, 6, 7, 8) and "10000" in upper:
		hints.append(_("Current comprobante type is {0} — switch to 11 and retry.").format(tipo_cbte))
	if hints:
		return _("AFIP rejected the invoice:\n{0}\n\n{1}").format(raw, "\n".join(hints))
	return _("AFIP rejected the invoice: {0}").format(raw)


@frappe.whitelist(allow_guest=True)
def get_invoice_qr(
	link_token=None,
	cae=None,
	nro_cbte=None,
	tipo_cbte=None,
	importe_total=0,
	doc_type=99,
	doc_number=0,
):
	"""Build the AFIP RG 4892 QR payload/URL for a previously authorized invoice."""
	_require_link_token_or_session(link_token or "")

	import base64
	import json

	settings = _get_settings()
	hoy = now_datetime().strftime("%Y-%m-%d")

	payload = {
		"ver": 1,
		"fecha": hoy,
		"cuit": int(settings.cuit),
		"ptoVta": cint(settings.punto_venta),
		"tipoCmp": cint(tipo_cbte or settings.default_invoice_type),
		"nroCmp": cint(nro_cbte or 0),
		"importe": flt(importe_total),
		"moneda": "PES",
		"ctz": 1,
		"tipoDocRec": cint(doc_type),
		"nroDocRec": cint(doc_number or 0),
		"tipoCodAut": "E",
		"codAut": int(cae) if cae else 0,
	}
	encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
	return {"ok": True, "qr_url": f"https://www.afip.gob.ar/fe/qr/?p={encoded}", "payload": payload}


_INVOICE_LETTER_BY_TYPE = {
	1: "A",
	2: "A",
	3: "A",
	6: "B",
	7: "B",
	8: "B",
	11: "C",
	12: "C",
	13: "C",
}


def _qr_data_uri(qr_url: str) -> str:
	import base64
	import io

	import qrcode

	img = qrcode.make(qr_url)
	buf = io.BytesIO()
	img.save(buf, format="PNG")
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def render_ticket_html(settings, cae_result: dict, customer: dict, items: list) -> str:
	"""Render a printable AFIP ticket (Factura A/B/C) matching the standard
	AFIP layout: header, comprobante box, items, totals, CAE + QR footer.
	"""
	letter = _INVOICE_LETTER_BY_TYPE.get(cint(cae_result["tipo_cbte"]), "C")
	cod_str = str(cae_result["tipo_cbte"]).zfill(3)
	pv_str = str(cae_result["punto_venta"]).zfill(5)
	nro_str = str(cae_result["nro_cbte"]).zfill(8)
	show_qr = cint(settings.get("print_qr", 1))
	qr = _qr_data_uri(cae_result.get("qr_url") or "") if show_qr else ""
	vto = cae_result.get("cae_vencimiento") or ""
	if len(vto) == 8:
		vto = f"{vto[6:8]}/{vto[4:6]}/{vto[0:4]}"

	items_rows = ""
	for it in items:
		qty = flt(it.get("qty"))
		rate = flt(it.get("rate"))
		items_rows += f"""
			<tr>
				<td>{qty:.2f} x {rate:.2f}</td>
				<td>{html.escape(it.get('description') or '')}</td>
				<td class="right">{qty * rate:.2f}</td>
			</tr>"""

	customer_block = (
		f"<div>{html.escape(customer.get('name') or 'Consumidor final')}</div>"
		f"<div>CUIT/DNI Nro: {customer.get('doc_number') or 0}</div>"
		if customer.get("name")
		else "<div>Consumidor final</div><div>CUIT Nro: 0</div>"
	)

	return f"""
<div class="afip-ticket" style="width:280px;font-family:monospace;font-size:12px;line-height:1.4">
	<div style="text-align:center;font-weight:bold">{html.escape(settings.get('company_name') or '')}</div>
	<div>CUIT: {settings.cuit}</div>
	<hr>
	<div style="text-align:center;border:1px solid #000;font-weight:bold">FACTURA (cod.{cod_str}) "{letter}"</div>
	<hr>
	<div>fac-{pv_str}-{nro_str}</div>
	<div>Fecha: {now_datetime().strftime('%d/%m/%Y')}</div>
	<hr>
	{customer_block}
	<hr>
	<table style="width:100%">
		<thead><tr><th align="left">Cant.xP.Unit.</th><th align="left">Desc.</th><th align="right">Subtotal</th></tr></thead>
		<tbody>{items_rows}</tbody>
	</table>
	<hr>
	<div style="font-weight:bold">Importe Total: $ {flt(cae_result.get('importe_total')):.2f}</div>
	<hr>
	<div>CAE Nro: {cae_result.get('cae')}</div>
	<div>Fecha Vto CAE: {vto}</div>
	{f'<div style="text-align:center;margin-top:8px"><img src="{qr}" width="120" height="120"></div>' if show_qr else ''}
	<div style="text-align:center;font-size:10px">Comprobante Autorizado</div>
</div>
"""


def _coerce_ticket_items(items) -> list:
	"""Flutter / Next dirty payloads: null, '', 'null', partial rows → list of dicts."""
	import json as _json

	if items is None or items == "" or items == "null" or items == "undefined":
		return []
	if isinstance(items, str):
		try:
			items = _json.loads(items) if items.strip() else []
		except Exception:
			return []
	if not isinstance(items, list):
		return []
	out = []
	for it in items:
		if not isinstance(it, dict):
			continue
		out.append(
			{
				"description": str(it.get("description") or it.get("item_name") or it.get("name") or ""),
				"qty": flt(it.get("qty") or 0),
				"rate": flt(it.get("rate") or 0),
			}
		)
	return out


@frappe.whitelist(allow_guest=True)
def print_pos_invoice(
	link_token=None,
	invoice_type=None,
	doc_type=99,
	doc_number=0,
	customer_name=None,
	items=None,
	importe_total=0,
	importe_neto=0,
	importe_iva=0,
	iva_id=5,
):
	"""Request a CAE and return ready-to-print ticket HTML in one call.

	Company / CUIT / punto de venta / cert come from AFIP Settings (web ERP) —
	callers only send sale lines + amounts. `items` is a JSON list of
	{description, qty, rate}.
	"""
	_require_link_token_or_session(link_token or "")

	items = _coerce_ticket_items(items)
	doc_type = cint(doc_type if doc_type not in (None, "", "null") else 99)
	doc_number = cint(doc_number if doc_number not in (None, "", "null") else 0)
	importe_total = flt(importe_total or 0)
	importe_neto = flt(importe_neto or 0)
	importe_iva = flt(importe_iva or 0)
	iva_id = cint(iva_id if iva_id not in (None, "", "null") else 5)
	customer_name = None if customer_name in (None, "", "null", "undefined") else str(customer_name)

	cae_result = solicitar_cae(
		link_token=link_token,
		invoice_type=invoice_type,
		doc_type=doc_type,
		doc_number=doc_number,
		importe_total=importe_total,
		importe_neto=importe_neto,
		importe_iva=importe_iva,
		iva_id=iva_id,
	)

	qr_result = get_invoice_qr(
		link_token=link_token,
		cae=cae_result["cae"],
		nro_cbte=cae_result["nro_cbte"],
		tipo_cbte=cae_result["tipo_cbte"],
		importe_total=importe_total,
		doc_type=doc_type,
		doc_number=doc_number,
	)
	cae_result["qr_url"] = qr_result["qr_url"]

	settings = _get_settings()
	html = render_ticket_html(
		settings.as_dict(),
		cae_result,
		{"name": customer_name, "doc_number": doc_number},
		items,
	)
	return {
		"ok": True,
		"html": html,
		"cae": cae_result,
		"qr_url": qr_result["qr_url"],
		"company_name": settings.company_name,
		"cuit": settings.cuit,
		"print_qr": bool(cint(settings.print_qr)),
	}


@frappe.whitelist(allow_guest=True)
def get_afip_print_config(link_token=None):
	"""Device / POS: whether ARCA/AFIP fiscal tickets are allowed.

	Does not throw when disabled or incomplete — callers decide whether to
	fall back to a non-fiscal receipt. Company/cert stay on the server.
	"""
	_require_link_token_or_session(link_token or "")
	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	frappe.flags.ignore_permissions = False

	enabled = bool(cint(settings.enabled))
	ready = bool(
		enabled
		and settings.cuit
		and settings.punto_venta
		and settings.certificate
		and settings.private_key
	)
	return {
		"ok": True,
		"enabled": enabled,
		"ready": ready,
		"print_qr": bool(cint(settings.print_qr)),
		"auto_print_after_sale": bool(cint(settings.auto_print_after_sale)),
		"environment": settings.environment or "homologacion",
		"default_invoice_type": cint(settings.default_invoice_type or 11),
		"company_name": (settings.company_name or "") if enabled else "",
		"cuit": settings.cuit if enabled else None,
		"punto_venta": cint(settings.punto_venta) if enabled and settings.punto_venta else None,
	}


@frappe.whitelist()
def get_afip_status():
	"""Admin-only: last login/error, for the Settings UI."""
	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	return {
		"enabled": bool(_as_check(settings.enabled)),
		"environment": settings.environment,
		"punto_venta": settings.punto_venta,
		"last_wsaa_login": settings.last_wsaa_login,
		"last_error": settings.last_error,
	}


_SETTINGS_FIELDS = (
	"enabled",
	"environment",
	"cuit",
	"punto_venta",
	"company_name",
	"default_invoice_type",
	"print_qr",
	"auto_print_after_sale",
	"last_wsaa_login",
	"last_error",
)

_CHECK_FIELDS = frozenset({"enabled", "print_qr", "auto_print_after_sale"})


def _as_check(value) -> int:
	"""Coerce UI / JSON booleans and Frappe Check values to 0|1.

	``cint('true')`` is 0 in Frappe — treat common truthy strings explicitly.
	"""
	if value is True or value == 1:
		return 1
	if value is False or value in (None, "", 0):
		return 0
	if isinstance(value, str):
		s = value.strip().lower()
		if s in ("1", "true", "yes", "on", "y"):
			return 1
		if s in ("0", "false", "no", "off", "n", "null", "undefined"):
			return 0
	return 1 if cint(value) else 0


def _serialize_afip_settings(settings) -> dict:
	data = {field: settings.get(field) for field in _SETTINGS_FIELDS}
	for field in _CHECK_FIELDS:
		data[field] = bool(_as_check(data.get(field)))
	data["certificate_uploaded"] = bool(settings.certificate)
	data["private_key_uploaded"] = bool(settings.private_key)
	return data


@frappe.whitelist()
def get_afip_settings():
	"""Full config for the Integrations settings panel (no secret file contents)."""
	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	return _serialize_afip_settings(settings)


def _decode_upload_bytes(filedata) -> bytes:
	import base64

	if filedata is None or filedata == "" or filedata == "null" or filedata == "undefined":
		frappe.throw(_("Missing file data"), frappe.ValidationError)
	if not isinstance(filedata, str):
		frappe.throw(_("filedata must be a base64 string or data-URL"), frappe.ValidationError)
	raw = filedata.strip()
	if "," in raw and raw.lower().startswith("data:"):
		raw = raw.split(",", 1)[1]
	try:
		return base64.b64decode(raw)
	except Exception:
		frappe.throw(_("Invalid file data (expected base64)"), frappe.ValidationError)


@frappe.whitelist()
def upload_afip_credential(kind=None, filedata=None, filename=None):
	"""Upload certificate (.crt) or private key (.key) into AFIP Settings.

	Used by Tools → Integrations → AFIP in the Next.js app. `kind` is
	``certificate`` or ``private_key``. `filedata` is a data-URL or raw base64.
	"""
	kind = (kind or "").strip().lower()
	if kind not in ("certificate", "private_key"):
		frappe.throw(_("kind must be 'certificate' or 'private_key'"), frappe.ValidationError)

	content = _decode_upload_bytes(filedata)
	if not content:
		frappe.throw(_("Empty file"), frappe.ValidationError)

	text_head = content[:80].decode("utf-8", errors="ignore")
	if kind == "certificate":
		if "BEGIN CERTIFICATE REQUEST" in text_head:
			frappe.throw(
				_("That file is a CSR (.csr). Upload the .crt AFIP/ARCA issued after you submit the CSR."),
				frappe.ValidationError,
			)
		if "BEGIN CERTIFICATE" not in text_head and "BEGIN TRUSTED CERTIFICATE" not in text_head:
			frappe.throw(_("Certificate must be a PEM .crt file"), frappe.ValidationError)
		default_name = "afip-certificate.crt"
	else:
		if "BEGIN" not in text_head or "PRIVATE KEY" not in text_head:
			frappe.throw(_("Private key must be a PEM .key / .pem file"), frappe.ValidationError)
		default_name = "afip-private.key"

	fname = (filename or "").strip() or default_name
	from frappe.utils.file_manager import remove_file, save_file

	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	old_url = settings.get(kind)
	if old_url:
		try:
			old_files = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": "AFIP Settings",
					"attached_to_name": "AFIP Settings",
					"file_url": old_url,
				},
				pluck="name",
				ignore_permissions=True,
			)
			for name in old_files:
				remove_file(fid=name, attached_to_doctype="AFIP Settings", attached_to_name="AFIP Settings")
		except Exception:
			pass

	file_doc = save_file(
		fname,
		content,
		"AFIP Settings",
		"AFIP Settings",
		is_private=1,
		df=kind,
	)
	settings.set(kind, file_doc.file_url)
	settings.last_error = None
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.flags.ignore_permissions = False
	return get_afip_settings()


@frappe.whitelist()
def clear_afip_credential(kind=None):
	"""Remove certificate or private_key attach from AFIP Settings."""
	kind = (kind or "").strip().lower()
	if kind not in ("certificate", "private_key"):
		frappe.throw(_("kind must be 'certificate' or 'private_key'"), frappe.ValidationError)

	from frappe.utils.file_manager import remove_file

	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	old_url = settings.get(kind)
	if old_url:
		try:
			old_files = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": "AFIP Settings",
					"attached_to_name": "AFIP Settings",
					"file_url": old_url,
				},
				pluck="name",
				ignore_permissions=True,
			)
			for name in old_files:
				remove_file(fid=name, attached_to_doctype="AFIP Settings", attached_to_name="AFIP Settings")
		except Exception:
			pass
	settings.set(kind, None)
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.flags.ignore_permissions = False
	return get_afip_settings()


_PROBE_INVOICE_TYPES = (
	(1, "A", "Factura A"),
	(6, "B", "Factura B"),
	(11, "C", "Factura C"),
)


def _hint_for_cbte_probe(err: str, tipo_cbte: int) -> str:
	upper = (err or "").upper()
	if "10000" in upper or "RESPONSABLE INSCRIPTO" in upper:
		if tipo_cbte in (1, 6):
			return _(
				"CUIT is not IVA Responsable Inscripto — Factura A/B are not allowed. Use Factura C (11) for Monotributo."
			)
		return _("CUIT tax condition rejected this comprobante type.")
	if "10005" in upper or "TIPO RECE" in upper:
		return _(
			"Punto de Venta is missing or not type RECE (Web Services). Register it in AFIP and update AFIP Settings."
		)
	return ""


def _probe_supported_invoice_types(wsfe, punto_venta: int) -> list:
	"""Ask WSFEv1 CompUltimoAutorizado for A/B/C — does not burn a CAE number."""
	out = []
	for code, letter, label in _PROBE_INVOICE_TYPES:
		entry = {
			"tipo_cbte": code,
			"letter": letter,
			"label": label,
			"supported": False,
			"last_nro": None,
			"error": None,
			"hint": None,
		}
		try:
			# Clear prior error flags if present.
			for attr in ("ErrMsg", "Excepcion", "Errores", "Obs"):
				if hasattr(wsfe, attr):
					try:
						setattr(wsfe, attr, "" if attr != "Errores" else [])
					except Exception:
						pass
			last = wsfe.CompUltimoAutorizado(code, punto_venta)
			err = (getattr(wsfe, "ErrMsg", None) or getattr(wsfe, "Excepcion", None) or "").strip()
			if err:
				entry["error"] = err[:500]
				entry["hint"] = _hint_for_cbte_probe(err, code) or None
			else:
				entry["supported"] = True
				entry["last_nro"] = cint(last or 0)
		except Exception as e:
			msg = str(e)
			entry["error"] = msg[:500]
			entry["hint"] = _hint_for_cbte_probe(msg, code) or None
		out.append(entry)
	return out


@frappe.whitelist()
def test_afip_wsaa():
	"""WSAA login + WSFEv1 probe of Factura A/B/C support for this CUIT + PV.

	Does not issue a CAE. Returns which comprobante types CompUltimoAutorizado accepts.
	"""
	try:
		import pysimplesoap  # noqa: F401
	except ImportError:
		frappe.throw(
			_(
				"Missing Python package pysimplesoap. On the ERPNext server run: "
				"bench pip install -r apps/erpnext/erpnext/erpnext_integrations/requirements-afip.txt"
			)
		)
	settings = _get_settings()
	try:
		wsfe = _wsfev1_client(settings)
		punto_venta = cint(settings.punto_venta)
		supported = _probe_supported_invoice_types(wsfe, punto_venta)
		ok_types = [r for r in supported if r.get("supported")]
		# Prefer a short last_error summary when nothing is authorized.
		if not ok_types:
			errs = [r.get("error") for r in supported if r.get("error")]
			if errs:
				_record_error(errs[0])
		status = get_afip_status()
		status["wsaa_ok"] = True
		status["wsfev1_ok"] = True
		status["punto_venta"] = punto_venta
		status["supported_invoice_types"] = supported
		status["supported_summary"] = ", ".join(
			f"{r['letter']}({r['tipo_cbte']})" for r in ok_types
		) or _("none")
		return status
	except Exception as e:
		_record_error(str(e))
		raise


def _sample_amounts(invoice_type: int, amount: float) -> dict:
	"""Split a sample total for Factura A/B (with IVA) vs C (no IVA)."""
	importe_total = round(flt(amount), 2)
	is_c = invoice_type in (11, 12, 13)
	if is_c or importe_total <= 0:
		return {
			"importe_total": importe_total,
			"importe_neto": importe_total,
			"importe_iva": 0.0,
			"iva_id": 3,
		}
	neto = round(importe_total / 1.21, 2)
	iva = round(importe_total - neto, 2)
	return {
		"importe_total": importe_total,
		"importe_neto": neto,
		"importe_iva": iva,
		"iva_id": 5,
	}


@frappe.whitelist()
def test_afip_sample_invoice(confirm=None, confirm_production=None, amount=1):
	"""Issue a tiny real AFIP invoice (default $1) and return ticket HTML + QR.

	Burns the next CAE number for the configured punto de venta — works in
	homologación and producción. Production requires ``confirm_production=1``.
	"""
	if not _as_check(confirm):
		frappe.throw(
			_("Pass confirm=1 to issue a sample invoice (consumes one CAE number)."),
			frappe.ValidationError,
		)

	settings = _get_settings()
	if settings.environment == "produccion" and not _as_check(confirm_production):
		frappe.throw(
			_(
				"Environment is Production. Pass confirm_production=1 to issue a "
				"real AFIP invoice for $1 (this cannot be undone)."
			),
			frappe.ValidationError,
		)

	try:
		amount_f = flt(amount if amount not in (None, "", "null", "undefined") else 1)
	except Exception:
		amount_f = 1.0
	if amount_f <= 0 or amount_f > 100:
		frappe.throw(_("Sample amount must be between 0.01 and 100"), frappe.ValidationError)

	tipo = cint(settings.default_invoice_type) or 11
	amts = _sample_amounts(tipo, amount_f)
	# Authenticated admin call — no device link_token.
	return print_pos_invoice(
		link_token=None,
		invoice_type=tipo,
		doc_type=99,
		doc_number=0,
		customer_name=_("AFIP sample test"),
		items=[
			{
				"description": _("Sample AFIP test ({0})").format(
					settings.environment or "homologacion"
				),
				"qty": 1,
				"rate": amts["importe_total"],
			}
		],
		importe_total=amts["importe_total"],
		importe_neto=amts["importe_neto"],
		importe_iva=amts["importe_iva"],
		iva_id=amts["iva_id"],
	)


def _ensure_afip_check_fields():
	"""Fail loud if AFIP Settings meta is stale (print_qr missing after JSON add)."""
	meta = frappe.get_meta("AFIP Settings")
	missing = [f for f in _CHECK_FIELDS if not meta.has_field(f)]
	if missing:
		frappe.throw(
			_(
				"AFIP Settings is missing fields: {0}. Run: bench --site <site> migrate"
			).format(", ".join(missing)),
			frappe.ValidationError,
		)


@frappe.whitelist()
def save_afip_settings(
	enabled=None,
	environment=None,
	cuit=None,
	punto_venta=None,
	company_name=None,
	default_invoice_type=None,
	print_qr=None,
	auto_print_after_sale=None,
):
	"""Patch the non-file AFIP Settings fields. Cert/key: upload_afip_credential."""
	frappe.flags.ignore_permissions = True
	_ensure_afip_check_fields()
	settings = frappe.get_single("AFIP Settings")
	if enabled is not None:
		settings.enabled = _as_check(enabled)
	if environment in ("homologacion", "produccion"):
		settings.environment = environment
	if cuit is not None:
		settings.cuit = str(cuit).strip()
	if punto_venta is not None:
		settings.punto_venta = cint(punto_venta)
	if company_name is not None:
		settings.company_name = str(company_name).strip()
	if default_invoice_type is not None:
		settings.default_invoice_type = cint(default_invoice_type)
	if print_qr is not None:
		settings.print_qr = _as_check(print_qr)
	if auto_print_after_sale is not None:
		settings.auto_print_after_sale = _as_check(auto_print_after_sale)
	settings.save(ignore_permissions=True)
	# Force Check fields into tabSingles (avoids stale meta / silent drops).
	if print_qr is not None:
		frappe.db.set_single_value("AFIP Settings", "print_qr", _as_check(print_qr))
	if auto_print_after_sale is not None:
		frappe.db.set_single_value(
			"AFIP Settings", "auto_print_after_sale", _as_check(auto_print_after_sale)
		)
	if enabled is not None:
		frappe.db.set_single_value("AFIP Settings", "enabled", _as_check(enabled))
	frappe.db.commit()
	frappe.clear_cache(doctype="AFIP Settings")
	return get_afip_settings()
