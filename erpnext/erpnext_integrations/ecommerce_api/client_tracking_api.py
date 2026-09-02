"""Guest-facing order tracking portal (i031 Part G).

Public, code-only (no login) API - access via a Delivery Note's
`custom_tracking_code`. Every endpoint here returns a deliberately narrow,
safe subset of fields: no internal costs/margins, no signature images, no
leaking whether a code exists (a bad code gets the same generic error as a
missing one).
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_months, getdate, nowdate

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dn_by_code(code):
	code = (code or "").strip()
	if not code:
		frappe.throw(_("Invalid tracking code"))

	name = frappe.db.get_value("Delivery Note", {"custom_tracking_code": code}, "name")
	if not name:
		# Same generic error for "doesn't exist" as any other failure - no leak.
		frappe.throw(_("Tracking code not found"))

	frappe.flags.ignore_permissions = True
	dn = frappe.get_doc("Delivery Note", name)
	frappe.flags.ignore_permissions = False
	return dn


def _trip_stop_for_dn(dn_name):
	"""Finds the (trip, stop) pair a Delivery Note is currently attached to,
	preferring an active (non-cancelled) trip - same "assigned" definition
	tms_api.py uses, so tracking reflects the live plan, not a stale one."""
	rows = frappe.get_all(
		"Delivery Stop",
		filters={"delivery_note": dn_name},
		fields=["parent", "idx", "visited", "custom_outcome", "custom_cliente_debe", "custom_balance_after_stop"],
		ignore_permissions=True,
	)
	if not rows:
		return None, None

	trip_names = [r.parent for r in rows]
	trips = {
		t.name: t
		for t in frappe.get_all(
			"Delivery Trip",
			filters={"name": ["in", trip_names]},
			fields=["name", "docstatus", "status", "departure_time"],
			ignore_permissions=True,
		)
	}
	active_rows = [r for r in rows if trips.get(r.parent) and trips[r.parent].docstatus != 2]
	if not active_rows:
		return None, None

	# Most recent departure time wins if attached to more than one active trip.
	active_rows.sort(key=lambda r: trips[r.parent].departure_time or "", reverse=True)
	best = active_rows[0]
	return trips[best.parent], best


def _status_for(dn, trip, stop):
	if not trip:
		return "prepared" if dn.docstatus == 1 else "confirmed"

	if stop.custom_outcome == "Not Home":
		return "attempt_failed"
	if stop.visited:
		return "delivered"
	if trip.status in ("In Transit",):
		return "on_route"
	if trip.status == "Scheduled" and getdate(trip.departure_time) == getdate():
		return "out_today"
	return "prepared"


_STATUS_LABELS = {
	"confirmed": "Pedido recibido",
	"prepared": "En preparación",
	"out_today": "Entrega hoy",
	"on_route": "En camino",
	"delivered": "Entregado",
	"attempt_failed": "Visita sin éxito — te contactamos",
}


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=20, seconds=60)
def get_tracking_by_code(code):
	dn = _dn_by_code(code)
	trip, stop = _trip_stop_for_dn(dn.name)
	status = _status_for(dn, trip, stop)

	balance_due = None
	if stop is not None and stop.get("custom_cliente_debe"):
		balance_due = stop.get("custom_balance_after_stop")

	return {
		"tracking_code": dn.custom_tracking_code,
		"status": status,
		"status_label": _STATUS_LABELS.get(status, status),
		"delivery_note": dn.name,
		"posting_date": str(dn.posting_date),
		"customer_name": dn.customer_name,
		"stop_number": stop.idx if stop else None,
		"total_stops": (
			frappe.db.count("Delivery Stop", {"parent": trip.name}) if trip else None
		),
		"balance_due": balance_due,
	}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=20, seconds=60)
def get_customer_receipts(code, months=6):
	dn = _dn_by_code(code)
	since = add_months(nowdate(), -int(months or 6))

	rows = frappe.get_all(
		"Delivery Note",
		filters={"customer": dn.customer, "docstatus": 1, "posting_date": [">=", since]},
		fields=["name", "posting_date", "grand_total", "custom_tracking_code"],
		order_by="posting_date desc",
		ignore_permissions=True,
	)
	return {"receipts": rows}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=20, seconds=60)
def get_receipt_pdf(code, delivery_note):
	dn = _dn_by_code(code)
	if delivery_note != dn.name:
		# Codes are per-DN in v1 - a code only unlocks its own receipt, plus
		# the customer-scoped list in get_customer_receipts.
		target = frappe.db.get_value("Delivery Note", delivery_note, "customer")
		if target != dn.customer:
			frappe.throw(_("Not found"), frappe.DoesNotExistError)

	target_dn = frappe.get_doc("Delivery Note", delivery_note)
	rows = "".join(
		f"<tr><td>{row.item_name or row.item_code}</td><td style='text-align:right'>{row.qty}</td>"
		f"<td style='text-align:right'>{row.amount}</td></tr>"
		for row in target_dn.items
	)
	html = f"""
	<html><body style="font-family: sans-serif; font-size: 12px; width: 280px;">
		<h3 style="text-align:center">{frappe.utils.escape_html(target_dn.company or '')}</h3>
		<p>Remito: <b>{target_dn.name}</b><br/>Fecha: {target_dn.posting_date}<br/>
		Cliente: {frappe.utils.escape_html(target_dn.customer_name or '')}</p>
		<table style="width:100%; border-collapse: collapse;">{rows}</table>
		<p style="text-align:right; font-weight:bold;">Total: {target_dn.grand_total}</p>
	</body></html>
	"""

	from frappe.utils.pdf import get_pdf

	pdf_content = get_pdf(html)
	frappe.local.response.filename = f"{target_dn.name}.pdf"
	frappe.local.response.filecontent = pdf_content
	frappe.local.response.type = "download"


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=10, seconds=60)
def create_repeat_order_request(code, items):
	"""Cart scoped to the tracked customer -> reuses the existing guest
	preorder pipeline (same one the ecommerce "consulta" flow uses) instead
	of a new Sales Order path."""
	dn = _dn_by_code(code)

	from erpnext.erpnext_integrations.ecommerce_api.api import create_guest_preorder

	customer_name = frappe.db.get_value("Customer", dn.customer, "customer_name") or dn.customer_name
	result = create_guest_preorder(
		items=items,
		guest_name=customer_name,
		company=dn.company,
		guest_notes=f"Repeat order from tracking code {dn.custom_tracking_code}",
		is_delivery=1,
	)
	return result
