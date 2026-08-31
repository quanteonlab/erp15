"""Outbound email for guest catalog consultations (consultas)."""

from __future__ import annotations

import base64
import os
from io import BytesIO

import frappe
from frappe.utils import cint, escape_html, flt

from erpnext.erpnext_integrations.ecommerce_api.inquiry_automation_settings import (
	get_inquiry_automation_settings_internal,
)


def _get_so_tag_text(so) -> str:
	"""Read guest-preorder tag text from remarks or terms (field varies by site)."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _guest_preorder_tag_fieldname

	tag_fn = _guest_preorder_tag_fieldname()
	if tag_fn and hasattr(so, tag_fn):
		return str(getattr(so, tag_fn) or "")
	for fn in ("remarks", "terms"):
		if hasattr(so, fn):
			return str(getattr(so, fn) or "")
	return ""


def _parse_guest_tags(tag_text: str) -> tuple[str, str]:
	guest_name = ""
	guest_phone = ""
	for part in str(tag_text or "").split("|"):
		part = part.strip()
		if part.startswith("guest_name:") and not guest_name:
			guest_name = part.split(":", 1)[1].strip()
		elif part.startswith("guest_phone:") and not guest_phone:
			guest_phone = part.split(":", 1)[1].strip()
	return guest_name, guest_phone


EMAIL_COPY = {
	"es": {
		"subject": "Nueva consulta {order}",
		"heading": "Nueva consulta de catálogo",
		"order": "Pedido",
		"total": "Total estimado",
		"status": "Estado",
		"customer": "Cliente",
		"phone": "WhatsApp / tel.",
		"sku": "SKU",
		"product": "Producto",
		"qty": "Cant.",
		"price": "Precio",
		"subtotal": "Subtotal",
		"barcode": "Código de barras",
		"barcode_image": "Imagen",
		"no_items": "Sin ítems",
		"empty": "—",
		"draft": "Borrador",
		"test_subject": "Prueba — alertas de consulta SilkOS",
		"test_body": (
			"<p>Este es un correo de prueba para confirmar que las alertas de "
			"<strong>consulta</strong> están configuradas correctamente.</p>"
		),
	},
	"en": {
		"subject": "New inquiry {order}",
		"heading": "New catalog inquiry",
		"order": "Order",
		"total": "Estimated total",
		"status": "Status",
		"customer": "Customer",
		"phone": "WhatsApp / phone",
		"sku": "SKU",
		"product": "Product",
		"qty": "Qty",
		"price": "Price",
		"subtotal": "Subtotal",
		"barcode": "Barcode",
		"barcode_image": "Image",
		"no_items": "No items",
		"empty": "—",
		"draft": "Draft",
		"test_subject": "Test — SilkOS inquiry alerts",
		"test_body": (
			"<p>This is a test email to confirm that <strong>inquiry</strong> "
			"alerts are configured correctly.</p>"
		),
	},
	"zh": {
		"subject": "新咨询 {order}",
		"heading": "目录新咨询",
		"order": "订单",
		"total": "预估总额",
		"status": "状态",
		"customer": "客户",
		"phone": "WhatsApp / 电话",
		"sku": "SKU",
		"product": "产品",
		"qty": "数量",
		"price": "单价",
		"subtotal": "小计",
		"barcode": "条码",
		"barcode_image": "条码图",
		"no_items": "无商品",
		"empty": "—",
		"draft": "草稿",
		"test_subject": "测试 — SilkOS 咨询提醒",
		"test_body": (
			"<p>这是一封测试邮件，用于确认<strong>咨询</strong>提醒已正确配置。</p>"
		),
	},
}


def _email_copy(lang: str) -> dict:
	return EMAIL_COPY.get(lang) or EMAIL_COPY["es"]


def _smtp_config() -> dict:
	conf = frappe.conf or {}
	return {
		"email": (
			os.environ.get("ERPNEXT_SMTP_EMAIL")
			or conf.get("smtp_email")
			or conf.get("mail_login")
			or "help@l0l.in"
		),
		"password": (
			os.environ.get("ERPNEXT_SMTP_PASSWORD")
			or conf.get("smtp_password")
			or conf.get("mail_password")
			or ""
		),
		"server": (
			os.environ.get("ERPNEXT_SMTP_SERVER")
			or conf.get("smtp_server")
			or conf.get("mail_server")
			or "smtp.hostinger.com"
		),
		"port": str(
			os.environ.get("ERPNEXT_SMTP_PORT")
			or conf.get("smtp_port")
			or conf.get("mail_port")
			or "465"
		),
		"use_ssl": cint(
			os.environ.get("ERPNEXT_SMTP_USE_SSL")
			or conf.get("smtp_use_ssl")
			or conf.get("use_ssl")
			or 1
		),
		"sender_name": str(conf.get("smtp_sender_name") or "SilkOS Consultas"),
	}


def ensure_outgoing_email_account() -> bool:
	"""Create or update the default outgoing Email Account from env/site config."""
	cfg = _smtp_config()
	if not cfg["password"]:
		frappe.log_error(
			"Set ERPNEXT_SMTP_PASSWORD or smtp_password in site_config to send consulta emails.",
			"Inquiry Email Setup",
		)
		return False

	email_id = cfg["email"].strip()
	if not email_id:
		return False

	account_name = frappe.db.get_value("Email Account", {"email_id": email_id}, "name")
	frappe.flags.ignore_permissions = True
	if account_name:
		doc = frappe.get_doc("Email Account", account_name)
	else:
		doc = frappe.new_doc("Email Account")
		doc.email_id = email_id

	doc.enable_outgoing = 1
	doc.default_outgoing = 1
	doc.smtp_server = cfg["server"]
	doc.smtp_port = cfg["port"]
	doc.use_ssl_for_outgoing = cint(cfg["use_ssl"])
	doc.use_tls = 0 if cint(cfg["use_ssl"]) else 1
	doc.always_use_account_email_id_as_sender = 1
	doc.always_use_account_name_as_sender_name = 1
	doc.send_unsubscribe_message = 0
	doc.track_email_status = 0
	doc.password = cfg["password"]

	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)

	# Clear default flag on other accounts
	frappe.db.sql(
		"""
		UPDATE `tabEmail Account`
		SET default_outgoing = 0
		WHERE name != %s AND default_outgoing = 1
		""",
		doc.name,
	)
	frappe.db.set_value("Email Account", doc.name, "default_outgoing", 1, update_modified=False)
	frappe.db.commit()
	return True


def _load_item_barcodes(item_codes: list[str]) -> dict[str, list[dict]]:
	codes = [c for c in item_codes if c]
	if not codes:
		return {}
	rows = frappe.get_all(
		"Item Barcode",
		filters={"parent": ["in", codes]},
		fields=["parent", "barcode", "barcode_type"],
		order_by="parent asc, idx asc",
	)
	out: dict[str, list[dict]] = {}
	for row in rows:
		value = str(row.barcode or "").strip()
		if not value:
			continue
		out.setdefault(row.parent, []).append(
			{"barcode": value, "barcode_type": row.barcode_type or "CODE128"}
		)
	return out


def _barcode_class(barcode_type: str, value: str):
	try:
		from barcode import Code128, EAN13, EAN8
	except ImportError:
		return None

	t = str(barcode_type or "CODE128").upper().replace("-", "").replace(" ", "")
	digits = "".join(ch for ch in value if ch.isdigit())
	if t in ("EAN", "EAN13") and len(digits) == 13:
		return EAN13
	if t == "EAN8" and len(digits) == 8:
		return EAN8
	if len(digits) in (8, 13) and t in ("EAN", "EAN13", "EAN8"):
		return EAN13 if len(digits) == 13 else EAN8
	return Code128


def _barcode_image_data_uri(barcode_value: str, barcode_type: str = "CODE128") -> str:
	value = str(barcode_value or "").strip()
	if not value:
		return ""
	cls = _barcode_class(barcode_type, value)
	if not cls:
		return ""
	try:
		from barcode.writer import ImageWriter

		buf = BytesIO()
		cls(value, writer=ImageWriter()).write(
			buf,
			options={
				"module_width": 0.22,
				"module_height": 10,
				"font_size": 8,
				"text_distance": 2,
				"quiet_zone": 2,
			},
		)
		encoded = base64.b64encode(buf.getvalue()).decode("ascii")
		return f"data:image/png;base64,{encoded}"
	except Exception:
		return ""


def _barcode_text_cell(barcodes: list[dict], empty_label: str) -> str:
	if not barcodes:
		return escape_html(empty_label)
	parts = [escape_html(row.get("barcode") or "") for row in barcodes if row.get("barcode")]
	return "<br/>".join(parts) if parts else escape_html(empty_label)


def _barcode_image_cell(barcodes: list[dict], empty_label: str) -> str:
	if not barcodes:
		return escape_html(empty_label)
	images = []
	for row in barcodes[:3]:
		value = str(row.get("barcode") or "").strip()
		if not value:
			continue
		data_uri = _barcode_image_data_uri(value, row.get("barcode_type") or "CODE128")
		if data_uri:
			images.append(
				f'<img src="{data_uri}" alt="{escape_html(value)}" '
				f'style="display:block;max-width:150px;height:auto;margin:4px 0" />'
			)
	if not images:
		return escape_html(empty_label)
	return "".join(images)


def _format_item_rows(so, lang: str = "es", include_barcodes: bool = False) -> str:
	copy = _email_copy(lang)
	rows = []
	barcode_map = _load_item_barcodes([row.item_code for row in (so.items or [])]) if include_barcodes else {}
	colspan = 7 if include_barcodes else 5
	for row in so.items or []:
		qty = flt(row.qty)
		rate = flt(row.rate)
		line_total = qty * rate
		cells = [
			f"<td>{escape_html(row.item_code or '')}</td>",
			f"<td>{escape_html(row.item_name or '')}</td>",
		]
		if include_barcodes:
			item_barcodes = barcode_map.get(row.item_code, [])
			cells.extend(
				[
					f"<td>{_barcode_text_cell(item_barcodes, copy['empty'])}</td>",
					f"<td style='text-align:center'>{_barcode_image_cell(item_barcodes, copy['empty'])}</td>",
				]
			)
		cells.extend(
			[
				f"<td style='text-align:right'>{qty:g}</td>",
				f"<td style='text-align:right'>{rate:.2f}</td>",
				f"<td style='text-align:right'>{line_total:.2f}</td>",
			]
		)
		rows.append(f"<tr>{''.join(cells)}</tr>")
	if not rows:
		return f"<tr><td colspan='{colspan}'>{escape_html(copy['no_items'])}</td></tr>"
	return "".join(rows)


def _table_head(copy: dict, include_barcodes: bool) -> str:
	cols = [
		f"<th align='left'>{escape_html(copy['sku'])}</th>",
		f"<th align='left'>{escape_html(copy['product'])}</th>",
	]
	if include_barcodes:
		cols.extend(
			[
				f"<th align='left'>{escape_html(copy['barcode'])}</th>",
				f"<th align='center'>{escape_html(copy['barcode_image'])}</th>",
			]
		)
	cols.extend(
		[
			f"<th align='right'>{escape_html(copy['qty'])}</th>",
			f"<th align='right'>{escape_html(copy['price'])}</th>",
			f"<th align='right'>{escape_html(copy['subtotal'])}</th>",
		]
	)
	return f"<tr style='background:#f3f4f6'>{''.join(cols)}</tr>"


def _build_consulta_email(
	so,
	guest_name: str,
	guest_phone: str,
	lang: str,
	include_barcodes: bool = False,
) -> tuple[str, str]:
	copy = _email_copy(lang)
	status = escape_html(so.status or copy["draft"])
	subject = copy["subject"].format(order=so.name)
	body = f"""
<div style="font-family:Arial,sans-serif;font-size:14px;color:#111">
  <h2 style="margin:0 0 12px">{escape_html(copy['heading'])}</h2>
  <p><strong>{escape_html(copy['order'])}:</strong> {escape_html(so.name)}</p>
  <p><strong>{escape_html(copy['total'])}:</strong> {escape_html(so.currency)} {flt(so.grand_total):.2f}</p>
  <p><strong>{escape_html(copy['status'])}:</strong> {status}</p>
  <p><strong>{escape_html(copy['customer'])}:</strong> {escape_html(guest_name or copy['empty'])}</p>
  <p><strong>{escape_html(copy['phone'])}:</strong> {escape_html(guest_phone or copy['empty'])}</p>
  <table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;margin-top:16px;width:100%;max-width:900px">
    <thead>
      {_table_head(copy, include_barcodes)}
    </thead>
    <tbody>
      {_format_item_rows(so, lang, include_barcodes)}
    </tbody>
  </table>
</div>
"""
	return subject, body


def send_consulta_notification(preorder_name: str, guest_name=None, guest_phone=None) -> bool:
	"""Send email alert when a guest consulta is created. Never raises."""
	try:
		settings = get_inquiry_automation_settings_internal()
		if not settings.get("emailOnInquiry", True):
			return False

		recipients = list(settings.get("notifyEmails") or [])
		if not recipients:
			return False

		if not ensure_outgoing_email_account():
			return False

		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", preorder_name)

		guest_name = (guest_name or "").strip()
		guest_phone = (guest_phone or "").strip()
		if not guest_name or not guest_phone:
			parsed_name, parsed_phone = _parse_guest_tags(_get_so_tag_text(so))
			if not guest_name:
				guest_name = parsed_name
			if not guest_phone:
				guest_phone = parsed_phone

		lang = str(settings.get("emailLanguage") or "es")
		include_barcodes = bool(settings.get("emailIncludeBarcodes", True))
		subject, body = _build_consulta_email(
			so, guest_name, guest_phone, lang, include_barcodes=include_barcodes
		)

		frappe.sendmail(
			recipients=recipients,
			subject=subject,
			message=body,
			delayed=False,
		)
		return True
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Inquiry email failed for {preorder_name}")
		return False


@frappe.whitelist()
def send_test_inquiry_email():
	"""Send a test email to configured recipients (admin)."""
	from erpnext.erpnext_integrations.ecommerce_api.inquiry_automation_settings import (
		_can_manage_settings,
	)

	if not _can_manage_settings():
		frappe.throw("Not permitted (tools.settings)")

	settings = get_inquiry_automation_settings_internal()
	recipients = settings.get("notifyEmails") or []
	if not recipients:
		frappe.throw("No notify emails configured.")

	if not ensure_outgoing_email_account():
		frappe.throw("SMTP is not configured. Set ERPNEXT_SMTP_PASSWORD or smtp_password in site_config.")

	lang = str(settings.get("emailLanguage") or "es")
	copy = _email_copy(lang)
	frappe.sendmail(
		recipients=recipients,
		subject=copy["test_subject"],
		message=copy["test_body"],
		delayed=False,
	)
	return {"ok": True, "recipients": recipients, "emailLanguage": lang}
