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

VENDOR_PATH = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "pyafipws"
)
if VENDOR_PATH not in sys.path:
	sys.path.insert(0, VENDOR_PATH)


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
	from wsaa import WSAA

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
	return wsaa


def _wsfev1_client(settings):
	from wsfev1 import WSFEv1

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

	frappe.flags.ignore_permissions = True
	frappe.db.set_value("AFIP Settings", "AFIP Settings", "last_wsaa_login", now_datetime())
	frappe.db.commit()
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
	_require_link_token(link_token or "")

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
			frappe.throw(_("AFIP rejected the invoice: {0}").format(wsfe.ErrMsg))

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
	_require_link_token(link_token or "")

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
	qr = _qr_data_uri(cae_result.get("qr_url") or "")
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
	<div style="text-align:center;margin-top:8px">
		<img src="{qr}" width="120" height="120">
	</div>
	<div style="text-align:center;font-size:10px">Comprobante Autorizado</div>
</div>
"""


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

	`items` is a JSON list of {description, qty, rate}.
	"""
	_require_link_token(link_token or "")

	import json as _json

	items = _json.loads(items) if isinstance(items, str) else (items or [])

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
	return {"ok": True, "html": html, "cae": cae_result}


@frappe.whitelist()
def get_afip_status():
	"""Admin-only: last login/error, for the Settings UI."""
	frappe.flags.ignore_permissions = True
	settings = frappe.get_single("AFIP Settings")
	return {
		"enabled": bool(settings.enabled),
		"environment": settings.environment,
		"punto_venta": settings.punto_venta,
		"last_wsaa_login": settings.last_wsaa_login,
		"last_error": settings.last_error,
	}
