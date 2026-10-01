"""Requirement probe for offline-queued writes (g013 phase 2).

Every write the ops outbox can replay is called here with the *minimal payload
the UI sends* (blank optional fields, today's date, no vehicle, …). A queued
replay must never fail on a mandatory field / date rule / link that the server
could have filled itself — those are reported so the endpoint can auto-fill.

Nothing persists: ``frappe.db.commit`` is disabled and each call runs inside a
savepoint that is rolled back.

    bench --site dev_site_a execute \
        erpnext.erpnext_integrations.ecommerce_api.test_ops_requirements.run
"""

from __future__ import annotations

import traceback

import frappe
from frappe.utils import add_days, nowdate

M = "erpnext.erpnext_integrations.ecommerce_api."


def _fx():
	"""Pick existing fixtures (names only). Missing ones skip their cases."""
	g = frappe.db.get_value
	so_draft = g("Sales Order", {"docstatus": 0}, "name", order_by="modified desc")
	so_sub = g("Sales Order", {"docstatus": 1, "status": ["not in", ["Closed", "Completed"]]}, "name", order_by="modified desc")
	so_cancelled = g("Sales Order", {"docstatus": 2}, "name", order_by="modified desc")
	dn = g("Delivery Note", {"docstatus": ["<", 2]}, "name", order_by="modified desc")
	trip_draft = g("Delivery Trip", {"docstatus": 0}, "name", order_by="modified desc")
	stop_dn = None
	if trip_draft:
		stop_dn = g("Delivery Stop", {"parent": trip_draft, "parenttype": "Delivery Trip"}, "delivery_note")
	free_dn = frappe.db.sql(
		"""select dn.name from `tabDelivery Note` dn
		where dn.docstatus=1 and not exists (select 1 from `tabDelivery Stop` s where s.delivery_note=dn.name)
		order by dn.modified desc limit 1"""
	)
	return {
		"so_draft": so_draft,
		"so_sub": so_sub,
		"so_cancelled": so_cancelled,
		"dn": dn,
		"free_dn": free_dn[0][0] if free_dn else None,
		"trip_draft": trip_draft,
		"stop_dn": stop_dn,
		"trip_any": g("Delivery Trip", {}, "name", order_by="modified desc"),
		"driver": g("Driver", {}, "name"),
		"vehicle": g("Vehicle", {}, "name"),
		"customer": g("Customer", {"disabled": 0, "name": ["not like", "EDGE%"]}, "name"),
		"supplier": g("Supplier", {"name": ["!=", "Uncategorized"]}, "name"),
		"po_sub": (
			frappe.db.sql(
				"""select name from `tabPurchase Order` where docstatus=1
				and ifnull(advance_paid,0) < grand_total order by modified desc limit 1"""
			)
			or [[None]]
		)[0][0],
		"po_draft": g("Purchase Order", {"docstatus": 0}, "name", order_by="modified desc"),
		"pe_receive": (
			frappe.db.sql(
				"""select pe.name from `tabPayment Entry` pe
				join `tabPayment Entry Reference` r on r.parent = pe.name and r.reference_doctype = 'Sales Order'
				join `tabSales Order` so on so.name = r.reference_name and so.docstatus = 1
				where pe.docstatus = 1 and pe.payment_type = 'Receive' order by pe.modified desc limit 1"""
			)
			or [[None]]
		)[0][0],
		"pe_pay": g("Payment Entry", {"docstatus": 1, "payment_type": "Pay"}, "name", order_by="modified desc"),
		"lead": g("Lead", {"status": ["not in", ["Converted"]]}, "name", order_by="modified desc"),
		"employee": g("Employee", {"status": "Active"}, "name"),
		"pricing_rule": g("Pricing Rule", {}, "name"),
		"bundle": g("Product Bundle", {}, "name"),
		"pos_profile": g("POS Profile", {}, "name"),
		"item": g("Item", {"disabled": 0, "is_stock_item": 1, "item_code": ["not like", "EDGE%"]}, "name"),
		"delivery_request": g("Delivery Request", {"status": ["in", ["Pending", "Open", ""]]}, "name")
		if frappe.db.exists("DocType", "Delivery Request")
		else None,
		"si": g("Sales Invoice", {"docstatus": 1}, "name", order_by="modified desc"),
	}


def _cases(fx):
	"""(label, dotted method, kwargs, required fixture keys)."""
	t = nowdate()
	uniq = frappe.generate_hash(length=6)
	return [
		# ── Pedidos / preorders ──────────────────────────────────────────────
		("preorder status → Orden", "api.set_guest_preorder_status", {"preorder_name": fx["so_draft"], "target_status": "Orden"}, ["so_draft"]),
		("preorder details zone only", "api.update_guest_preorder_details", {"preorder_name": fx["so_draft"], "data": {"zone": "", "territory": "", "delivery_date_forced": False}}, ["so_draft"]),
		("preorder details past delivery date", "api.update_guest_preorder_details", {"preorder_name": fx["so_draft"], "data": {"delivery_date": add_days(t, -30), "delivery_date_forced": True}}, ["so_draft"]),
		("preorder details cashier", "api.update_guest_preorder_details", {"preorder_name": fx["so_draft"], "data": {"cashier_user": None}}, ["so_draft"]),
		("preorder logistics clear", "api.update_guest_preorder_logistics", {"preorder_name": fx["so_draft"], "clear_trip": 1}, ["so_draft"]),
		("preorder factura A", "api.set_guest_preorder_factura_a", {"preorder_name": fx["so_draft"], "requires_factura_a": 1}, ["so_draft"]),
		("preorder mark prepared", "api.mark_prepared_guest_preorder", {"preorder_name": fx["so_sub"]}, ["so_sub"]),
		("preorder confirm", "api.confirm_guest_preorder", {"preorder_name": fx["so_draft"]}, ["so_draft"]),
		("preorder unarchive", "api.unarchive_guest_preorder", {"preorder_name": fx["so_cancelled"]}, ["so_cancelled"]),
		("preorder create DN", "api.create_delivery_note_for_preorder", {"preorder_name": fx["so_sub"]}, ["so_sub"]),
		("preorder record payment", "api.record_preorder_payment", {"preorder_name": fx["so_sub"], "paid_amount": 1, "mode_of_payment": "Cash"}, ["so_sub"]),
		("preorder update payment", "api.update_preorder_payment", {"payment_name": fx["pe_receive"], "paid_amount": 1}, ["pe_receive"]),
		# ── Customers / suppliers / address ──────────────────────────────────
		("create customer (name only)", "api.create_customer", {"customer_name": f"PROBE CUST {uniq}"}, []),
		("create customer (CRM form)", "api.create_customer", {"customer_name": f"PROBE CRM {uniq}", "tax_category": "IVA 21%", "zone": "Z-PROBE", "address_line1": "Calle 1"}, []),
		("create supplier (name only)", "api.create_supplier", {"supplier_name": f"PROBE SUP {uniq}"}, []),
		("create address (relate modal)", "api.create_address", {"customer_name": fx["customer"], "address_line1": "Calle 1", "city": "—", "country": "Argentina", "address_type": "Shipping", "is_shipping": 1}, ["customer"]),
		("update customer territory", "api.update_customer", {"customer_name": fx["customer"], "territory": "Z-PROBE"}, ["customer"]),
		# ── Promotions ───────────────────────────────────────────────────────
		("pricing rule disable", "api.set_pricing_rule_disabled", {"name": fx["pricing_rule"], "disabled": 1}, ["pricing_rule"]),
		("pricing rule minimal new", "api.save_pricing_rule", {"data": {"title": f"PROBE {uniq}", "apply_on": "Item Code", "rate_or_discount": "Discount Percentage", "discount_percentage": 10, "applicable_items": [fx["item"]]}}, ["item"]),
		("bundle minimal new", "api.save_product_bundle", {"data": {"new_item_code": f"PROBE-BND-{uniq}", "bundle_name": f"Probe {uniq}", "items": [{"item_code": fx["item"], "qty": 1}]}}, ["item"]),
		# ── Preventa ─────────────────────────────────────────────────────────
		("lead create name only", "preventa_api.upsert_lead", {"values": {"lead_name": f"Probe Lead {uniq}"}}, []),
		("lead duplicate", "preventa_api.duplicate_lead", {"lead": fx["lead"]}, ["lead"]),
		("lead archive", "preventa_api.archive_leads", {"leads": [fx["lead"]]}, ["lead"]),
		("lead convert", "preventa_api.convert_lead_to_customer", {"lead": fx["lead"], "customer_type": "Individual"}, ["lead"]),
		# ── Buying ───────────────────────────────────────────────────────────
		("PO minimal create", "buying_api.create_purchase_order", {"supplier": fx["supplier"], "items": [{"item_code": fx["item"], "qty": 1, "rate": 1}]}, ["supplier", "item"]),
		("PO past ETA create", "buying_api.create_purchase_order", {"supplier": fx["supplier"], "schedule_date": add_days(t, -10), "items": [{"item_code": fx["item"], "qty": 1, "rate": 1}]}, ["supplier", "item"]),
		("PO update notes", "buying_api.update_purchase_order", {"name": fx["po_draft"], "notes": "probe"}, ["po_draft"]),
		("PO pipeline → to_receive", "buying_api.set_purchase_order_pipeline", {"name": fx["po_draft"], "target_pipeline": "to_receive"}, ["po_draft"]),
		("PO record payment", "buying_api.record_purchase_order_payment", {"name": fx["po_sub"], "paid_amount": 1}, ["po_sub"]),
		("PO update payment", "buying_api.update_purchase_order_payment", {"payment_name": fx["pe_pay"], "paid_amount": 1}, ["pe_pay"]),
		# ── Empleados / cajas / extra fields ─────────────────────────────────
		("employee create name only", "employee_api.save_employee", {"data": {"employee_name": f"Probe Emp {uniq}"}}, []),
		("employee status", "employee_api.save_employee", {"name": fx["employee"], "data": {"status": "Active"}}, ["employee"]),
		("employee group create", "employee_api.save_employee_group", {"employee_group_name": f"Probe Grp {uniq}", "members": []}, []),
		("pos profile save", "cash_register_api.save_pos_profile", {"name": fx["pos_profile"], "data": {"disabled": 0}}, ["pos_profile"]),
		("pos profile create minimal", "cash_register_api.save_pos_profile", {"data": {"name": f"Probe Caja {uniq}"}}, []),
		("extra column add", "extra_fields.add_extra_column", {"scope": "tables.probe", "label": "Probe"}, []),
		("extra row save", "extra_fields.save_extra_row", {"scope": "tables.probe", "row_key": "X", "values": {"a": 1}}, []),
		("doc remarks SI", "crm_party_api.update_party_doc_remarks", {"doctype": "Sales Invoice", "name": fx["si"], "remarks": "probe"}, ["si"]),
		# ── Rutas / TMS ──────────────────────────────────────────────────────
		("trip create no driver/vehicle", "tms_api.create_trip", {"date": t, "delivery_note_names": [fx["free_dn"]]}, ["free_dn"]),
		("trip create driver only", "tms_api.create_trip", {"date": t, "driver": fx["driver"], "delivery_note_names": [fx["free_dn"]]}, ["free_dn", "driver"]),
		("trip add stop", "tms_api.add_stops_to_trip", {"trip_name": fx["trip_draft"], "delivery_note_names": [fx["free_dn"]]}, ["trip_draft", "free_dn"]),
		("trip remove stop", "tms_api.remove_stops_from_trip", {"trip_name": fx["trip_draft"], "delivery_note_names": [fx["stop_dn"]]}, ["trip_draft", "stop_dn"]),
		("trip reorder", "tms_api.reorder_trip_stops", {"trip_name": fx["trip_draft"], "delivery_note_names": [fx["stop_dn"]]}, ["trip_draft", "stop_dn"]),
		("trip assignment clear vehicle", "tms_api.update_trip_assignment", {"trip_name": fx["trip_draft"], "driver": fx["driver"]}, ["trip_draft", "driver"]),
		("trip publish", "tms_api.publish_trip", {"trip_name": fx["trip_draft"]}, ["trip_draft"]),
		("trip start (draft)", "tms_api.start_trip", {"trip_name": fx["trip_draft"]}, ["trip_draft"]),
		("trip lock", "tms_api.set_trip_locked", {"trip_name": fx["trip_draft"], "locked": 1}, ["trip_draft"]),
		("trip cancel draft", "tms_api.cancel_trip", {"trip_name": fx["trip_draft"]}, ["trip_draft"]),
		("zone save minimal", "tms_api.save_tms_zone", {"zone": {"name": f"Probe {uniq}"}}, []),
		("map temp location", "tms_api.save_map_temp_location", {"label": "Probe", "lat": -34.6, "lng": -58.4}, []),
		("map plan", "tms_api.save_map_plan", {"name": f"Probe {uniq}", "pin_ids": [], "date": t}, []),
		("driver quick create", "tms_api.create_driver_quick", {"full_name": f"Probe Driver {uniq}"}, []),
		("vehicle quick create", "tms_api.create_vehicle_quick", {"license_plate": f"PRB{uniq[:4].upper()}"}, []),
		("driver update phone", "tms_api.update_driver", {"driver": fx["driver"], "cell_number": "1155550000"}, ["driver"]),
		("customer at location", "tms_api.create_customer_at_location", {"customer_name": f"Probe Loc {uniq}", "address": "Calle 1", "lat": -34.6, "lng": -58.4}, []),
		("warehouse at location", "tms_api.create_warehouse_at_location", {"warehouse_name": f"Probe WH {uniq}", "address": "Calle 1", "lat": -34.6, "lng": -58.4}, []),
		("link customer at location", "tms_api.link_customer_at_location", {"customer": fx["customer"], "address": "Calle 1", "lat": -34.6, "lng": -58.4}, ["customer"]),
		("approve delivery request", "tms_api.approve_delivery_request", {"name": fx["delivery_request"]}, ["delivery_request"]),
		("reject delivery request", "tms_api.reject_delivery_request", {"name": fx["delivery_request"]}, ["delivery_request"]),
		("pending driver force", "tms_api.update_pending_delivery_driver", {"delivery_note": fx["free_dn"], "driver": fx["driver"]}, ["free_dn", "driver"]),
		# ── Conductor ────────────────────────────────────────────────────────
		("driver reorder stops", "tms_api.driver_reorder_stops", {"trip_name": fx["trip_draft"], "delivery_note_names": [fx["stop_dn"]]}, ["trip_draft", "stop_dn"]),
		("driver request delivery", "tms_api.driver_request_delivery", {"customer": fx["customer"]}, ["customer"]),
		("driver stop details", "tms_api.driver_update_stop_details", {"trip_name": fx["trip_draft"], "stop_idx": 1, "comments": "probe"}, ["trip_draft"]),
		("driver settle payment", "tms_api.driver_settle_payment", {"trip_name": fx["trip_any"], "stop_idx": 1, "amount_collected": 1, "payment_method": "Cash"}, ["trip_any"]),
		("admin stop outcome", "tms_api.admin_record_stop_outcome", {"trip_name": fx["trip_any"], "stop_idx": 1, "outcome": "Delivered"}, ["trip_any"]),
	]


def _classify(exc) -> str:
	name = type(exc).__name__
	msg = str(exc)
	if name == "MandatoryError" or "Mandatory" in msg or "mandatory" in msg:
		return "MANDATORY"
	if name == "LinkValidationError" or "Could not find" in msg:
		return "LINK"
	if "date" in msg.lower() and ("after" in msg.lower() or "before" in msg.lower()):
		return "DATE_RULE"
	if name in ("ValidationError", "DuplicateEntryError", "UniqueValidationError"):
		return "VALIDATION"
	if name in ("PermissionError",):
		return "PERMISSION"
	return f"CRASH:{name}"


def run(verbose=0):
	"""Probe every queued write; print a table; return the list of failures."""
	real_commit = frappe.db.commit
	frappe.db.commit = lambda *a, **k: None
	results = []
	try:
		fx = _fx()
		for label, method, kwargs, needs in _cases(fx):
			missing = [k for k in needs if not fx.get(k)]
			if missing:
				results.append((label, "SKIP", f"no fixture: {', '.join(missing)}"))
				continue
			fn = frappe.get_attr(M + method)
			sp = f"probe_{len(results)}"
			frappe.db.savepoint(sp)
			frappe.local.message_log = []
			try:
				fn(**kwargs)
				results.append((label, "OK", ""))
			except Exception as exc:  # noqa: BLE001 — classify everything
				detail = str(exc).strip().splitlines()[0][:220] if str(exc).strip() else type(exc).__name__
				if cint_verbose(verbose):
					detail += "\n" + traceback.format_exc()[-900:]
				results.append((label, _classify(exc), detail))
			finally:
				try:
					frappe.db.rollback(save_point=sp)
				except Exception:
					frappe.db.rollback()
	finally:
		frappe.db.rollback()
		frappe.db.commit = real_commit
		frappe.local.message_log = []

	results.extend(_idempotency_checks())
	bad = [r for r in results if r[1] not in ("OK", "SKIP")]
	print("\n" + "═" * 70)
	print("  ops requirement probe — offline-replayable writes")
	print("═" * 70)
	for label, status, detail in results:
		mark = "✓" if status == "OK" else ("⊘" if status == "SKIP" else "✗")
		print(f"  {mark} {status:<12} {label}" + (f"  — {detail}" if detail else ""))
	print(f"\n  {len(results) - len(bad)}/{len(results)} ok/skip, {len(bad)} need attention\n")
	return bad


def cint_verbose(v) -> bool:
	try:
		return int(v or 0) == 1
	except Exception:
		return False


def _idempotency_checks():
	"""Replaying a create with the same client_request_id must not duplicate."""
	out = []
	real_commit = frappe.db.commit
	frappe.db.commit = lambda *a, **k: None
	try:
		from erpnext.erpnext_integrations.ecommerce_api import api, tms_api

		req = f"tmp:probe:{frappe.generate_hash(length=10)}"
		label = f"PROBE IDEM {frappe.generate_hash(length=5)}"
		frappe.db.savepoint("probe_idem")
		try:
			a = api.create_supplier(supplier_name=label, client_request_id=req)
			b = api.create_supplier(supplier_name=label + " 2", client_request_id=req)
			ok = (a.get("name") == b.get("name")) and b.get("already_exists") == 1
			out.append(("idempotent create_supplier replay", "OK" if ok else "VALIDATION", "" if ok else f"{a} vs {b}"))
			req2 = f"tmp:probe:{frappe.generate_hash(length=10)}"
			d1 = tms_api.create_driver_quick(full_name=f"Probe Idem {req2[-5:]}", client_request_id=req2)
			d2 = tms_api.create_driver_quick(full_name=f"Probe Idem {req2[-5:]}", client_request_id=req2)
			ok2 = d1.get("name") == d2.get("name")
			out.append(("idempotent create_driver_quick replay", "OK" if ok2 else "VALIDATION", "" if ok2 else f"{d1} vs {d2}"))
		except Exception as exc:  # noqa: BLE001
			out.append(("idempotent replay", _classify(exc), str(exc)[:200]))
		finally:
			frappe.db.rollback(save_point="probe_idem")
	finally:
		frappe.db.rollback()
		frappe.db.commit = real_commit
	return out
