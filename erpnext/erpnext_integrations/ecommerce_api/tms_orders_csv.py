"""Rutas orders CSV import/export.

Creates/updates Customer + Address, guest preorder (Pedido) + Delivery Note (Remito).
All columns optional; per-row errors do not abort the batch.
"""

from __future__ import annotations

import csv
import io
import re
import uuid

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

RUTAS_ORDERS_CSV_HEADERS = [
	"Código de Cliente",
	"Nombre",
	"Calle y Número",
	"Ciudad",
	"Provincia/Estado",
	"Latitud",
	"Longitud",
	"Teléfono (con código de país)",
	"Email del cliente",
	"Código de Orden",
	"Fecha de Orden",
	"Tipo de Operación (E/R)",
	"Código de Producto",
	"Descripción del Producto",
	"Cantidad de Producto",
	"Peso",
	"Volumen",
	"Dinero",
	"Duración (min)",
	"Ventana horaria 1",
	"Ventana horaria 2",
	"Notas",
	"Agrupador",
	"Email del vendedor o seller",
	"Eliminar Orden (Si - No - Vacío)",
	"Vehículo",
	"Habilidades",
]

# Logical keys used after header normalize
_H = {
	"client_code": "Código de Cliente",
	"name": "Nombre",
	"street": "Calle y Número",
	"city": "Ciudad",
	"state": "Provincia/Estado",
	"lat": "Latitud",
	"lng": "Longitud",
	"phone": "Teléfono (con código de país)",
	"email": "Email del cliente",
	"order_code": "Código de Orden",
	"order_date": "Fecha de Orden",
	"op_type": "Tipo de Operación (E/R)",
	"item_code": "Código de Producto",
	"item_desc": "Descripción del Producto",
	"qty": "Cantidad de Producto",
	"weight": "Peso",
	"volume": "Volumen",
	"money": "Dinero",
	"duration": "Duración (min)",
	"window1": "Ventana horaria 1",
	"window2": "Ventana horaria 2",
	"notes": "Notas",
	"agrupador": "Agrupador",
	"sellers": "Email del vendedor o seller",
	"delete": "Eliminar Orden (Si - No - Vacío)",
	"vehicle": "Vehículo",
	"skills": "Habilidades",
}

_CSV_ORDER_TAG = "csv_order_code"
_ZONE_SELECT = {"norte", "sur", "este", "oeste", "centro"}


def _cell(row: dict, key: str) -> str:
	header = _H[key]
	raw = row.get(header)
	if raw is None:
		return ""
	s = str(raw).strip()
	if s.lower() in ("null", "none", "undefined", "nan"):
		return ""
	return s


def _truthy_delete(val: str) -> bool:
	v = (val or "").strip().lower()
	return v in ("si", "sí", "yes", "y", "1", "true", "eliminar")


def _normalize_header(h: str) -> str:
	return re.sub(r"\s+", " ", (h or "").replace("\ufeff", "").strip())


def get_rutas_orders_csv_template() -> dict:
	"""Return CSV template text (header + 2 example rows)."""
	buf = io.StringIO()
	writer = csv.writer(buf)
	writer.writerow(RUTAS_ORDERS_CSV_HEADERS)
	writer.writerow(
		[
			"",
			"Cliente Demo CSV 1",
			"Av. Corrientes 1200",
			"Buenos Aires",
			"CABA",
			"-34.6037",
			"-58.3816",
			"+5491111110001",
			"demo1@example.com",
			"CSV-ORD-DEMO-001",
			nowdate(),
			"E",
			"24755",
			"Producto demo",
			"1",
			"1",
			"0.1",
			"5000",
			"15",
			"09:00 - 12:00",
			"16:00 - 18:00",
			"El timbre no funciona",
			"Frío",
			"seller1@gmail.com, seller2@gmail.com",
			"",
			"",
			"Grúa, frágil",
		]
	)
	writer.writerow(
		[
			"",
			"Cliente Demo CSV 2",
			"Defensa 500",
			"Buenos Aires",
			"CABA",
			"-34.6217",
			"-58.3731",
			"+5491111110002",
			"",
			"CSV-ORD-DEMO-002",
			nowdate(),
			"E",
			"24755",
			"Producto demo",
			"2",
			"",
			"",
			"10000",
			"20",
			"10:00 - 13:00",
			"",
			"Recibe en portería",
			"Seco",
			"comercial@gmail.com",
			"",
			"",
			"Frío",
		]
	)
	return {"csv_text": buf.getvalue(), "headers": list(RUTAS_ORDERS_CSV_HEADERS)}


def _parse_rutas_orders_csv(csv_text) -> tuple[list[dict], list[dict]]:
	"""Return (rows, parse_errors). Rows are dicts keyed by canonical Spanish headers."""
	if csv_text is None:
		return [], [{"row": 0, "error": _("CSV text is empty")}]
	if not isinstance(csv_text, str):
		csv_text = str(csv_text)
	csv_text = csv_text.lstrip("\ufeff").strip()
	if not csv_text:
		return [], [{"row": 0, "error": _("CSV text is empty")}]

	# Detect delimiter
	sample = csv_text[:2048]
	try:
		dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
	except Exception:
		dialect = csv.excel

	reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
	if not reader.fieldnames:
		return [], [{"row": 0, "error": _("CSV has no header row")}]

	# Map actual headers → canonical
	field_map = {}
	canonical_set = {_normalize_header(h): h for h in RUTAS_ORDERS_CSV_HEADERS}
	for raw in reader.fieldnames:
		norm = _normalize_header(raw)
		if norm in canonical_set:
			field_map[raw] = canonical_set[norm]

	rows = []
	errors = []
	for i, raw_row in enumerate(reader, start=2):
		if not isinstance(raw_row, dict):
			continue
		# Skip fully empty lines
		if not any(str(v or "").strip() for v in raw_row.values()):
			continue
		out = {h: "" for h in RUTAS_ORDERS_CSV_HEADERS}
		for raw_h, val in raw_row.items():
			canon = field_map.get(raw_h)
			if not canon:
				continue
			out[canon] = "" if val is None else str(val).strip()
		out["_row_num"] = i
		rows.append(out)
	return rows, errors


def _find_customer(row: dict) -> str | None:
	code = _cell(row, "client_code")
	if code and frappe.db.exists("Customer", code):
		return code
	phone = _cell(row, "phone")
	if phone:
		name = frappe.db.get_value("Customer", {"mobile_no": phone}, "name")
		if name:
			return name
	email = _cell(row, "email")
	if email:
		name = frappe.db.get_value("Customer", {"email_id": email}, "name")
		if name:
			return name
	cname = _cell(row, "name")
	if cname:
		if frappe.db.exists("Customer", cname):
			return cname
		found = frappe.db.sql(
			"""SELECT name FROM `tabCustomer` WHERE LOWER(TRIM(customer_name))=%s LIMIT 1""",
			(cname.lower(),),
		)
		if found:
			return found[0][0]
	return None


def _upsert_rutas_customer(row: dict) -> tuple[str | None, str | None, list[str]]:
	"""Returns (customer_name, address_name, warnings)."""
	warnings = []
	name = _cell(row, "name") or _cell(row, "client_code") or _cell(row, "phone") or _cell(row, "email")
	if not name:
		return None, None, [_("Need Nombre, Código de Cliente, Teléfono, or Email")]

	code = _cell(row, "client_code")
	phone = _cell(row, "phone")
	email = _cell(row, "email")
	street = _cell(row, "street")
	city = _cell(row, "city") or "Buenos Aires"
	state = _cell(row, "state") or "CABA"
	lat_s = _cell(row, "lat")
	lng_s = _cell(row, "lng")
	agrupador = _cell(row, "agrupador")

	lat = flt(lat_s) if lat_s else None
	lng = flt(lng_s) if lng_s else None
	if lat_s and not (-90 <= (lat or 0) <= 90):
		warnings.append(_("Invalid Latitud: {0}").format(lat_s))
		lat = None
	if lng_s and not (-180 <= (lng or 0) <= 180):
		warnings.append(_("Invalid Longitud: {0}").format(lng_s))
		lng = None

	existing = _find_customer(row)
	territory = (
		frappe.db.exists("Territory", "Argentina")
		and "Argentina"
		or frappe.db.exists("Territory", "All Territories")
		and "All Territories"
		or frappe.db.get_value("Territory", {}, "name")
	)
	customer_group = (
		frappe.db.exists("Customer Group", "Retail")
		and "Retail"
		or frappe.db.exists("Customer Group", "Individual")
		and "Individual"
		or frappe.db.get_value("Customer Group", {}, "name")
	)

	if existing:
		frappe.flags.ignore_permissions = True
		cust = frappe.get_doc("Customer", existing)
		frappe.flags.ignore_permissions = False
		if name and cust.customer_name != name:
			cust.customer_name = name
		if phone:
			cust.mobile_no = phone
		if email:
			cust.email_id = email
		cust.save(ignore_permissions=True)
		customer_name = cust.name
		created = False
	else:
		# Prefer using Código de Cliente as Customer.name when it doesn't collide
		cust = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_type": "Individual",
				"customer_group": customer_group,
				"territory": territory,
				"mobile_no": phone or None,
				"email_id": email or None,
			}
		)
		if code and not frappe.db.exists("Customer", code):
			cust.name = code
		cust.insert(ignore_permissions=True)
		customer_name = cust.name
		created = True

	# Shipping address upsert
	zone = None
	if agrupador and agrupador.strip().lower() in _ZONE_SELECT:
		zone = agrupador.strip().capitalize()
		if zone == "Caba":
			zone = None

	addr_title = f"{customer_name} - CSV"
	addr_name = frappe.db.get_value(
		"Address",
		{"address_title": addr_title, "address_type": "Shipping"},
		"name",
	)
	if not addr_name:
		# any existing shipping for customer
		linked = frappe.get_all(
			"Address",
			filters=[
				["Dynamic Link", "link_doctype", "=", "Customer"],
				["Dynamic Link", "link_name", "=", customer_name],
				["address_type", "=", "Shipping"],
			],
			pluck="name",
			limit=1,
			ignore_permissions=True,
		)
		addr_name = linked[0] if linked else None

	if addr_name:
		frappe.flags.ignore_permissions = True
		addr = frappe.get_doc("Address", addr_name)
		frappe.flags.ignore_permissions = False
		if street:
			addr.address_line1 = street
		addr.city = city
		addr.state = state
		addr.country = "Argentina"
		if lat is not None:
			addr.custom_latitude = lat
		if lng is not None:
			addr.custom_longitude = lng
		if zone and hasattr(addr, "custom_zone"):
			try:
				addr.custom_zone = zone
			except Exception:
				pass
		addr.save(ignore_permissions=True)
	elif street or lat is not None:
		addr = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": addr_title,
				"address_type": "Shipping",
				"address_line1": street or city,
				"city": city,
				"state": state,
				"country": "Argentina",
				"is_shipping_address": 1,
				"custom_latitude": lat,
				"custom_longitude": lng,
				"links": [{"link_doctype": "Customer", "link_name": customer_name}],
			}
		)
		if zone:
			try:
				addr.custom_zone = zone
			except Exception:
				pass
		addr.insert(ignore_permissions=True)
		addr_name = addr.name
	else:
		addr_name = None
		if created:
			warnings.append(_("Customer created without address (no Calle / Lat-Lng)"))

	return customer_name, addr_name, warnings


def _find_so_by_csv_order_code(order_code: str) -> str | None:
	if not order_code:
		return None
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		GUEST_PREORDER_REMARKS_TAG,
		_guest_preorder_tag_fieldname,
	)

	tag_fn = _guest_preorder_tag_fieldname()
	if not tag_fn:
		return None
	needle = f"{_CSV_ORDER_TAG}:{order_code}"
	rows = frappe.db.sql(
		f"""
		SELECT name FROM `tabSales Order`
		WHERE `{tag_fn}` LIKE %(tag)s AND `{tag_fn}` LIKE %(code)s AND docstatus < 2
		ORDER BY modified DESC LIMIT 1
		""",
		{"tag": f"%{GUEST_PREORDER_REMARKS_TAG}%", "code": f"%{needle}%"},
	)
	return rows[0][0] if rows else None


def _submit_remito_for_so(so_name: str) -> str | None:
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_delivery_note_for_sales_order,
		_temporarily_allow_negative_stock,
	)
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	dn_name = _delivery_note_for_sales_order(so_name)
	if dn_name:
		return dn_name
	dn = make_delivery_note(so_name)
	default_warehouse = frappe.db.get_value("Company", dn.company, "custom_default_warehouse")
	if default_warehouse:
		for row in dn.items:
			row.warehouse = default_warehouse
		dn.set_warehouse = default_warehouse
	for row in dn.items:
		# Route CSV import: allow zero valuation so missing Item.valuation_rate
		# does not block remito creation for planning.
		if hasattr(row, "allow_zero_valuation_rate"):
			row.allow_zero_valuation_rate = 1
	dn.insert(ignore_permissions=True)
	dn.flags.ignore_permissions = True
	# CSV route planning should not fail on bin qty; same pattern as POS stock helpers.
	with _temporarily_allow_negative_stock():
		dn.submit()
	try:
		frappe.db.set_value("Sales Order", so_name, "status", "En Delivery")
	except Exception:
		pass
	try:
		from erpnext.erpnext_integrations.ecommerce_api.webhook_api import emit_ecommerce_webhook

		emit_ecommerce_webhook("remito_created", {"delivery_note": dn.name, "sales_order": so_name})
	except Exception:
		pass
	return dn.name


def _confirm_so_for_csv(so_name: str) -> None:
	"""Submit draft guest SO without Pedidos-visibility / full get_guest_preorder reload."""
	frappe.flags.ignore_permissions = True
	so = frappe.get_doc("Sales Order", so_name)
	frappe.flags.ignore_permissions = False
	if so.docstatus == 0:
		so.flags.ignore_permissions = True
		so.submit()
	try:
		so.update_status("To Deliver and Bill")
	except Exception:
		pass


def _append_logistics_tags_to_so(so_name: str, tag_parts: list) -> None:
	"""Write ventana/op_type/csv_order_code… as sibling remark tags (not inside guest_notes)."""
	from erpnext.erpnext_integrations.ecommerce_api.api import _update_guest_preorder_tag

	if not so_name or not tag_parts:
		return
	try:
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", so_name)
		frappe.flags.ignore_permissions = False
		for part in tag_parts:
			part = str(part or "").strip()
			if ":" not in part:
				continue
			key, val = part.split(":", 1)
			_update_guest_preorder_tag(so, key.strip(), val.strip())
		so.flags.ignore_permissions = True
		so.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "tms_orders_csv._append_logistics_tags_to_so")


def _cancel_csv_order(order_code: str, row_num: int) -> dict:
	so_name = _find_so_by_csv_order_code(order_code)
	if not so_name:
		return {
			"ok": False,
			"row": row_num,
			"order_code": order_code,
			"error": _("No order found for Código de Orden {0}").format(order_code),
		}
	from erpnext.erpnext_integrations.ecommerce_api.api import _delivery_note_for_sales_order

	dn_name = _delivery_note_for_sales_order(so_name)
	# Remove from draft trips
	if dn_name:
		stops = frappe.get_all(
			"Delivery Stop",
			filters={"delivery_note": dn_name},
			fields=["name", "parent"],
			ignore_permissions=True,
		)
		for st in stops:
			trip_status = frappe.db.get_value("Delivery Trip", st.parent, ["docstatus", "status"], as_dict=True)
			if trip_status and cint(trip_status.docstatus) == 0:
				try:
					from erpnext.erpnext_integrations.ecommerce_api.tms_api import remove_stops_from_trip

					remove_stops_from_trip(st.parent, [dn_name])
				except Exception:
					frappe.log_error(frappe.get_traceback(), "csv_import remove stop")
		# Cancel DN if submitted
		try:
			frappe.flags.ignore_permissions = True
			dn = frappe.get_doc("Delivery Note", dn_name)
			frappe.flags.ignore_permissions = False
			if dn.docstatus == 1:
				dn.flags.ignore_permissions = True
				dn.cancel()
			frappe.db.commit()
		except Exception as e:
			return {
				"ok": False,
				"row": row_num,
				"order_code": order_code,
				"error": _("Could not cancel remito {0}: {1}").format(dn_name, str(e)[:120]),
			}

	try:
		frappe.flags.ignore_permissions = True
		so = frappe.get_doc("Sales Order", so_name)
		frappe.flags.ignore_permissions = False
		if so.docstatus == 1:
			so.flags.ignore_permissions = True
			so.cancel()
		elif so.docstatus == 0:
			frappe.delete_doc("Sales Order", so_name, ignore_permissions=True, force=True)
		frappe.db.commit()
	except Exception as e:
		return {
			"ok": False,
			"row": row_num,
			"order_code": order_code,
			"error": _("Could not cancel pedido {0}: {1}").format(so_name, str(e)[:120]),
		}

	return {
		"ok": True,
		"row": row_num,
		"order_code": order_code,
		"preorder_name": so_name,
		"delivery_note": dn_name,
		"action": "deleted",
	}


def _build_items(group_rows: list[dict]) -> tuple[list[dict], list[dict]]:
	items = []
	errors = []
	for row in group_rows:
		item_code = _cell(row, "item_code")
		qty = flt(_cell(row, "qty") or 1) or 1
		rate = flt(_cell(row, "money") or 0)
		row_num = row.get("_row_num") or 0
		if not item_code:
			# Allow rate-only line via stub? Plan: require product code
			errors.append(
				{
					"row": row_num,
					"error": _("Código de Producto is required"),
				}
			)
			continue
		if not frappe.db.exists("Item", item_code):
			errors.append(
				{
					"row": row_num,
					"error": _("Item {0} not found").format(item_code),
				}
			)
			continue
		# If money is total for line, convert to rate
		if rate and qty:
			# Prefer interpreting Dinero as line total when qty>0
			line_rate = rate / qty if qty else rate
		else:
			line_rate = rate
		# If Dinero empty, leave 0 and let create_guest_preorder fetch price list
		items.append({"item_code": item_code, "qty": qty, "rate": line_rate})
	return items, errors


def _import_order_group(order_code: str, group_rows: list[dict], company: str | None) -> dict:
	row0 = group_rows[0]
	row_num = row0.get("_row_num") or 0

	if _truthy_delete(_cell(row0, "delete")):
		if not order_code or order_code.startswith("_gen_"):
			return {
				"ok": False,
				"row": row_num,
				"error": _("Eliminar Orden requires Código de Orden"),
			}
		return _cancel_csv_order(order_code, row_num)

	customer, address, warnings = _upsert_rutas_customer(row0)
	if not customer:
		return {
			"ok": False,
			"row": row_num,
			"order_code": order_code,
			"error": "; ".join(warnings) or _("Could not resolve customer"),
		}

	items, item_errors = _build_items(group_rows)
	if not items:
		return {
			"ok": False,
			"row": row_num,
			"order_code": order_code,
			"customer": customer,
			"error": item_errors[0]["error"] if item_errors else _("No valid product lines"),
			"line_errors": item_errors,
		}

	from erpnext.erpnext_integrations.ecommerce_api.api import (
		create_guest_preorder,
		update_guest_preorder_details,
		update_guest_preorder_items,
	)

	# Logistics tags (separate from human guest_notes / consulta comment)
	notes_parts = []
	for key, tag in (
		("window1", "ventana1"),
		("window2", "ventana2"),
		("agrupador", "agrupador"),
		("sellers", "sellers"),
		("vehicle", "vehicle"),
		("skills", "skills"),
		("op_type", "op_type"),
		("duration", "duration_min"),
		("weight", "peso"),
		("volume", "volumen"),
	):
		val = _cell(row0, key)
		if val:
			notes_parts.append(f"{tag}:{val.replace('|', ' ')[:200]}")
	notes_parts.append(f"{_CSV_ORDER_TAG}:{order_code}")
	# Human consultation comment — keep as guest_notes only (not mixed into logistics)
	guest_notes = _cell(row0, "notes")
	logistics_blob = " | ".join(notes_parts)

	order_date = _cell(row0, "order_date") or nowdate()
	try:
		getdate(order_date)
	except Exception:
		order_date = nowdate()

	existing_so = None if order_code.startswith("_gen_") else _find_so_by_csv_order_code(order_code)
	action = "created"
	try:
		if existing_so:
			action = "updated"
			frappe.flags.ignore_permissions = True
			so = frappe.get_doc("Sales Order", existing_so)
			frappe.flags.ignore_permissions = False
			if so.docstatus == 0:
				_confirm_so_for_csv(existing_so)
			try:
				update_guest_preorder_items(existing_so, items)
			except Exception:
				pass
			try:
				# Human consulta comment only in guest_notes; logistics as sibling tags
				# appended after save via remarks key:value parts.
				update_guest_preorder_details(
					existing_so,
					{
						"customer": customer,
						"guest_notes": guest_notes or None,
						"guest_address": _cell(row0, "street") or None,
						"is_delivery": 1,
					},
				)
			except Exception:
				pass
			if logistics_blob:
				_append_logistics_tags_to_so(existing_so, notes_parts)
			so_name = existing_so
		else:
			res = create_guest_preorder(
				items=items,
				customer=customer,
				company=company,
				guest_name=_cell(row0, "name") or customer,
				guest_phone=_cell(row0, "phone") or None,
				guest_email=_cell(row0, "email") or None,
				guest_address=_cell(row0, "street") or None,
				guest_notes=guest_notes or None,
				is_delivery=1,
				delivery_date=order_date,
				cashier_id="Rutas CSV",
			)
			so_name = res["preorder_name"]
			if logistics_blob:
				_append_logistics_tags_to_so(so_name, notes_parts)
			_confirm_so_for_csv(so_name)

		if address:
			try:
				frappe.db.set_value(
					"Sales Order",
					so_name,
					{
						"shipping_address_name": address,
						"customer_address": address,
					},
					update_modified=False,
				)
			except Exception:
				pass

		dn_name = _submit_remito_for_so(so_name)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return {
			"ok": False,
			"row": row_num,
			"order_code": order_code,
			"customer": customer,
			"error": str(e)[:240],
			"warnings": warnings,
			"line_errors": item_errors,
		}

	return {
		"ok": True,
		"row": row_num,
		"order_code": order_code,
		"customer": customer,
		"address": address,
		"preorder_name": so_name,
		"delivery_note": dn_name,
		"action": action,
		"warnings": warnings,
		"line_errors": item_errors,
	}


def import_rutas_orders_csv(csv_text=None, pin=None, company=None) -> dict:
	"""Parse CSV and create/update Pedido+Remito (or delete). Per-row errors."""
	from erpnext.erpnext_integrations.ecommerce_api.tms_api import _require_rutas_order_pin
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	_require_rutas_order_pin(pin)
	company = company or resolve_company() or frappe.defaults.get_user_default("Company")

	# Speed / noise: skip notifications and email during bulk import
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True

	rows, parse_errors = _parse_rutas_orders_csv(csv_text)
	report = {
		"ok": True,
		"total_rows": len(rows),
		"created": [],
		"updated": [],
		"deleted": [],
		"errors": list(parse_errors),
		"warnings": [],
	}
	if not rows:
		if not report["errors"]:
			report["errors"].append({"row": 0, "error": _("No data rows in CSV")})
		report["ok"] = False
		return report

	# Group by order code (generate stable key when blank: use row identity)
	groups: dict[str, list[dict]] = {}
	for row in rows:
		ocode = _cell(row, "order_code")
		if not ocode:
			ocode = f"_gen_{row.get('_row_num')}_{uuid.uuid4().hex[:8]}"
		groups.setdefault(ocode, []).append(row)

	for order_code, group_rows in groups.items():
		# If any row in group is delete, treat as delete
		if any(_truthy_delete(_cell(r, "delete")) for r in group_rows):
			result = _cancel_csv_order(
				order_code if not order_code.startswith("_gen_") else "",
				group_rows[0].get("_row_num") or 0,
			)
		else:
			result = _import_order_group(order_code, group_rows, company)

		if result.get("ok"):
			action = result.get("action")
			entry = {k: result.get(k) for k in ("row", "order_code", "customer", "preorder_name", "delivery_note")}
			if action == "deleted":
				report["deleted"].append(entry)
			elif action == "updated":
				report["updated"].append(entry)
			else:
				report["created"].append(entry)
			for w in result.get("warnings") or []:
				report["warnings"].append({"row": result.get("row"), "warning": w})
			for le in result.get("line_errors") or []:
				report["warnings"].append(le)
		else:
			report["errors"].append(
				{
					"row": result.get("row"),
					"order_code": result.get("order_code"),
					"error": result.get("error"),
				}
			)

	report["ok"] = len(report["errors"]) == 0
	report["summary"] = {
		"created": len(report["created"]),
		"updated": len(report["updated"]),
		"deleted": len(report["deleted"]),
		"errors": len(report["errors"]),
	}
	return report
