"""Simplified Purchase Order API for Logistics → Buying."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company
from erpnext.erpnext_integrations.ecommerce_api.ops_kv import kv_get, kv_set
from erpnext.erpnext_integrations.ecommerce_api.ops_kv import idempotent_request

# Offline outbox replay guard: client_request_id → created PO (Table Extra Data).
PO_CLIENT_REQUEST_SCOPE = "buying_client_request"


def _as_str(v) -> str:
	if v is None:
		return ""
	if isinstance(v, (list, dict, tuple)):
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _resolve_supplier(supplier) -> str:
	"""Return an existing Supplier name, or create one from a free-text label.

	CRM merges receiving-session supplier strings that may never have been
	inserted as `tabSupplier` — Buying / +compra must not fail with
	"Supplier X not found" for those rows.
	"""
	label = _as_str(supplier)
	if not label:
		frappe.throw(_("supplier is required"))
	if frappe.db.exists("Supplier", label):
		return label
	from erpnext.erpnext_integrations.ecommerce_api.api import _get_or_create_named_supplier

	return _get_or_create_named_supplier(label)


def _as_int(v, default: int, lo: int = 0, hi: int = 500) -> int:
	try:
		n = cint(v)
	except Exception:
		n = default
	if n < lo:
		n = default
	return min(max(n, lo), hi)


def _clamp_eta(value, floor) -> str:
	"""ETA (Reqd By) as YYYY-MM-DD, never before ``floor`` (the PO transaction date).

	ERPNext rejects ``schedule_date < transaction_date``; a PO queued offline
	yesterday with ETA "today" replays with today's transaction date, so clamp
	instead of failing the outbox row.
	"""
	try:
		d = getdate(_as_str(value) or floor)
	except Exception:
		d = getdate(floor)
	f = getdate(floor)
	return str(d if d >= f else f)


def _parse_items(items):
	if isinstance(items, str):
		try:
			items = json.loads(items) if items.strip() else []
		except Exception:
			items = []
	if items is None:
		items = []
	if not isinstance(items, list):
		frappe.throw(_("items must be a list"))
	clean = []
	for raw in items:
		if not isinstance(raw, dict):
			continue
		code = _as_str(raw.get("item_code"))
		qty = abs(flt(raw.get("qty")))
		if not code or qty <= 0:
			continue
		if not frappe.db.exists("Item", code):
			frappe.throw(_("Item {0} not found").format(code))
		clean.append(
			{
				"item_code": code,
				"item_name": _as_str(raw.get("item_name")) or code,
				"qty": qty,
				"rate": abs(flt(raw.get("rate"))),
				"uom": _as_str(raw.get("uom")) or None,
				"schedule_date": _as_str(raw.get("schedule_date")) or None,
				"description": _as_str(raw.get("notes") or raw.get("description")) or None,
				"cost_edited": 1 if cint(raw.get("cost_edited")) else 0,
				"amount": abs(flt(raw.get("amount"))) if raw.get("amount") not in (None, "") else None,
				"received_qty": (
					abs(flt(raw.get("received_qty")))
					if raw.get("received_qty") not in (None, "")
					else None
				),
			}
		)
	return clean


def _apply_buying_cost_updates(*, supplier: str, po_name: str, rows: list[dict], transaction_date: str):
	"""When the UI marks cost_edited, write Standard Buying and a readable Item comment."""
	from erpnext.erpnext_integrations.ecommerce_api.product_manager import _upsert_item_price_buying

	supplier_label = (
		frappe.db.get_value("Supplier", supplier, "supplier_name") if supplier else None
	) or supplier
	updated = []
	for row in rows:
		if not row.get("cost_edited"):
			continue
		rate = flt(row.get("rate"))
		if rate <= 0:
			continue
		code = row["item_code"]
		buying_pl = (
			frappe.db.get_single_value("Buying Settings", "buying_price_list") or "Standard Buying"
		)
		old = frappe.db.get_value(
			"Item Price",
			{"item_code": code, "price_list": buying_pl, "buying": 1},
			"price_list_rate",
		)
		_upsert_item_price_buying(code, rate)
		note = _(
			"Compras: costo {0} → {1} · proveedor {2} · OC {3} · fecha {4} · qty {5}"
		).format(
			flt(old) if old is not None else "—",
			rate,
			supplier_label,
			po_name,
			transaction_date,
			flt(row.get("qty")),
		)
		try:
			frappe.flags.ignore_permissions = True
			frappe.get_doc("Item", code).add_comment("Comment", note)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "buying_api.item_cost_comment")
		finally:
			frappe.flags.ignore_permissions = False
		updated.append({"item_code": code, "rate": rate, "previous_rate": flt(old) if old is not None else None})
	return updated


@frappe.whitelist(allow_guest=True)
def create_purchase_order(
	supplier=None,
	schedule_date=None,
	items=None,
	company=None,
	submit=0,
	notes=None,
	client_request_id=None,
):
	"""Create a Purchase Order with one shared expected (schedule) date.

	``client_request_id`` (optional, client UUID) makes the call idempotent so the
	offline ops outbox can replay it: a repeat returns the first PO with
	``already_exists: 1`` instead of creating a duplicate.
	"""
	request_id = _as_str(client_request_id)[:140]
	if request_id:
		_row, prior = kv_get(PO_CLIENT_REQUEST_SCOPE, request_id)
		prior_name = _as_str(prior.get("name"))
		if prior_name and frappe.db.exists("Purchase Order", prior_name):
			docstatus, grand_total = frappe.db.get_value(
				"Purchase Order", prior_name, ["docstatus", "grand_total"]
			)
			return {
				"ok": True,
				"already_exists": 1,
				"name": prior_name,
				"docstatus": cint(docstatus),
				"submitted": 1 if cint(docstatus) == 1 else 0,
				"grand_total": flt(grand_total),
				"cost_updates": [],
			}

	supplier = _resolve_supplier(supplier)

	sched = _clamp_eta(schedule_date, nowdate())

	clean = _parse_items(items)
	if not clean:
		frappe.throw(_("Add at least one item"))

	company = resolve_company(company) or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(_("No company configured"))
	do_submit = 1 if cint(submit) else 0
	note = _as_str(notes)

	po_items = []
	for row in clean:
		line = {
			"item_code": row["item_code"],
			"qty": row["qty"],
			"rate": row["rate"],
			"schedule_date": _clamp_eta(row["schedule_date"] or sched, nowdate()),
		}
		if row.get("uom"):
			line["uom"] = row["uom"]
		if row.get("description"):
			line["description"] = row["description"]
		po_items.append(line)

	supplier_label = (
		frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier
	)
	# title + status are reqd on this site; set before insert so mandatory
	# validation cannot fail if validate/set_title_field is skipped mid-request.
	doc = frappe.get_doc(
		{
			"doctype": "Purchase Order",
			"supplier": supplier,
			"company": company,
			"transaction_date": nowdate(),
			"schedule_date": sched,
			"status": "Draft",
			"title": supplier_label,
			"items": po_items,
		}
	)
	doc.flags.ignore_validate = False
	doc.insert(ignore_permissions=True)
	if request_id:
		kv_set(PO_CLIENT_REQUEST_SCOPE, request_id, {"name": doc.name})
	if note:
		try:
			doc.add_comment("Comment", note)
		except Exception:
			pass

	cost_updates = _apply_buying_cost_updates(
		supplier=supplier,
		po_name=doc.name,
		rows=clean,
		transaction_date=str(doc.transaction_date or nowdate()),
	)

	if do_submit:
		try:
			doc.submit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "buying_api.create_purchase_order submit")
			frappe.db.commit()
			return {
				"ok": True,
				"name": doc.name,
				"docstatus": cint(doc.docstatus),
				"submitted": 0,
				"grand_total": flt(doc.grand_total),
				"cost_updates": cost_updates,
				"warning": _("Saved as draft — submit failed (check accounts / permissions)"),
			}

	frappe.db.commit()
	return {
		"ok": True,
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"submitted": do_submit,
		"grand_total": flt(doc.grand_total),
		"supplier": doc.supplier,
		"schedule_date": str(doc.schedule_date) if doc.schedule_date else sched,
		"cost_updates": cost_updates,
	}


@frappe.whitelist(allow_guest=True)
def update_purchase_order(
	name=None,
	schedule_date=None,
	items=None,
	notes=None,
	submit=0,
	force=0,
	supplier=None,
):
	"""Update Purchase Order lines / ETA / supplier.

	Drafts: full replace of items; supplier can change.
	Submitted: pass force=1 to update qty/rate/amount/received in place (admin inline edit)
	and optionally change supplier.
	"""
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False

	do_force = cint(force)
	if cint(doc.docstatus) == 2:
		frappe.throw(_("Cancelled purchase orders cannot be edited"))
	if cint(doc.docstatus) != 0 and not do_force:
		frappe.throw(_("Only draft purchase orders can be edited here"))

	sched = _as_str(schedule_date)
	if sched:
		try:
			new_sched = _clamp_eta(sched, doc.transaction_date or nowdate())
			doc.schedule_date = new_sched
			# Keep line ETAs in sync when parent ETA is changed (table dbl-click / detail).
			for row in doc.get("items") or []:
				row.schedule_date = new_sched
		except Exception:
			pass

	supplier_changed = False
	sup = _as_str(supplier)
	if sup:
		sup = _resolve_supplier(sup)
		if doc.supplier != sup:
			doc.supplier = sup
			supplier_changed = True
			doc.title = (
				frappe.db.get_value("Supplier", sup, "supplier_name") or sup
			)

	clean = _parse_items(items)
	force_notes: list[str] = []

	if cint(doc.docstatus) == 1 and do_force:
		if clean:
			force_notes = _update_submitted_po_lines(doc, clean)
		elif sched or supplier_changed:
			doc.flags.ignore_validate_update_after_submit = True
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
		if supplier_changed:
			force_notes.append(_("Supplier set to {0}").format(doc.supplier))
		note = _as_str(notes)
		if note and hasattr(doc, "remarks"):
			frappe.db.set_value("Purchase Order", name, "remarks", note, update_modified=False)
		frappe.db.commit()
		return {
			"ok": True,
			"name": doc.name,
			"docstatus": cint(doc.docstatus),
			"submitted": 1,
			"grand_total": flt(frappe.db.get_value("Purchase Order", name, "grand_total")),
			"cost_updates": [],
			"notes": force_notes,
			"forced": 1,
		}

	if clean:
		doc.set("items", [])
		for row in clean:
			line = {
				"item_code": row["item_code"],
				"qty": row["qty"],
				"rate": row["rate"],
				"schedule_date": _clamp_eta(
					row["schedule_date"] or doc.schedule_date, doc.transaction_date or nowdate()
				),
			}
			if row.get("uom"):
				line["uom"] = row["uom"]
			if row.get("description"):
				line["description"] = row["description"]
			if row.get("amount") is not None and flt(row["qty"]) > 0:
				line["rate"] = flt(row["amount"]) / flt(row["qty"])
				line["amount"] = flt(row["amount"])
			doc.append("items", line)

	if not doc.items:
		frappe.throw(_("Add at least one item"))

	if not doc.title:
		doc.title = (
			frappe.db.get_value("Supplier", doc.supplier, "supplier_name") or doc.supplier or name
		)
	if not doc.status:
		doc.status = "Draft"

	note = _as_str(notes)
	if note and hasattr(doc, "remarks"):
		doc.remarks = note

	doc.save(ignore_permissions=True)

	for row in clean:
		if row.get("received_qty") is None:
			continue
		_set_po_line_received(doc.name, row["item_code"], flt(row["received_qty"]))

	if note:
		try:
			doc.add_comment("Comment", note)
		except Exception:
			pass

	cost_updates = _apply_buying_cost_updates(
		supplier=doc.supplier,
		po_name=doc.name,
		rows=clean or [],
		transaction_date=str(doc.transaction_date or nowdate()),
	)

	do_submit = 1 if cint(submit) else 0
	if do_submit:
		try:
			doc.submit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "buying_api.update_purchase_order submit")
			frappe.db.commit()
			return {
				"ok": True,
				"name": doc.name,
				"docstatus": cint(doc.docstatus),
				"submitted": 0,
				"grand_total": flt(doc.grand_total),
				"cost_updates": cost_updates,
				"warning": _("Saved as draft — submit failed"),
			}

	frappe.db.commit()
	return {
		"ok": True,
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"submitted": do_submit,
		"grand_total": flt(doc.grand_total),
		"cost_updates": cost_updates,
	}


def _recalc_po_per_received(po_name: str):
	"""Keep parent per_received in sync after manual received_qty edits."""
	rows = frappe.get_all(
		"Purchase Order Item",
		filters={"parent": po_name},
		fields=["qty", "received_qty"],
		ignore_permissions=True,
	)
	ordered = sum(flt(r.qty) for r in rows) or 0.0
	received = sum(flt(r.received_qty) for r in rows) or 0.0
	pct = (received / ordered * 100.0) if ordered > 0 else 0.0
	frappe.db.set_value(
		"Purchase Order",
		po_name,
		"per_received",
		pct,
		update_modified=False,
	)


def _set_po_line_received(po_name: str, item_code: str, target_received: float):
	"""Best-effort set received qty on a PO line (PR when increasing; override when decreasing)."""
	target = max(0.0, flt(target_received))
	row = frappe.db.get_value(
		"Purchase Order Item",
		{"parent": po_name, "item_code": item_code},
		["name", "qty", "received_qty"],
		as_dict=True,
	)
	if not row:
		return None
	current = flt(row.received_qty)
	ordered = flt(row.qty)
	if target > ordered + 1e-6:
		target = ordered
	delta = target - current
	note = None
	if abs(delta) < 1e-9:
		return None

	if delta > 0 and cint(frappe.db.get_value("Purchase Order", po_name, "docstatus")) == 1:
		try:
			from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

			pr = make_purchase_receipt(po_name)
			kept = []
			for it in pr.items or []:
				if it.item_code == item_code:
					it.qty = min(delta, flt(it.qty) if flt(it.qty) > 0 else delta)
					it.received_qty = it.qty
					it.stock_qty = flt(it.qty) * flt(it.conversion_factor or 1)
					it.amount = flt(it.qty) * flt(it.rate)
					kept.append(it)
			pr.items = kept
			if pr.items:
				pr.flags.ignore_permissions = True
				pr.insert(ignore_permissions=True)
				pr.submit()
				note = _("Purchase Receipt {0}").format(pr.name)
		except Exception:
			frappe.log_error(title="Compras set received via PR failed")
			note = _("PR create failed — received qty overridden on PO line")

	frappe.db.set_value(
		"Purchase Order Item",
		row.name,
		"received_qty",
		target,
		update_modified=False,
	)
	_recalc_po_per_received(po_name)
	return note


def _update_submitted_po_lines(doc, clean: list) -> list:
	"""Force-update qty/rate/amount/received on a submitted PO; append new lines."""
	notes = []
	by_code = {row.item_code: row for row in (doc.items or [])}
	for row in clean:
		poi = by_code.get(row["item_code"])
		if not poi:
			line = {
				"item_code": row["item_code"],
				"qty": flt(row["qty"]),
				"rate": flt(row["rate"]),
				"schedule_date": doc.schedule_date,
			}
			if row.get("uom"):
				line["uom"] = row["uom"]
			if row.get("amount") is not None:
				line["amount"] = flt(row["amount"])
			doc.append("items", line)
			notes.append(_("Added item {0}").format(row["item_code"]))
			continue
		poi.qty = flt(row["qty"])
		poi.rate = flt(row["rate"])
		amt = row.get("amount")
		poi.amount = flt(amt) if amt is not None else flt(poi.qty) * flt(poi.rate)
		if row.get("uom"):
			poi.uom = row["uom"]
	doc.flags.ignore_validate_update_after_submit = True
	doc.flags.ignore_permissions = True
	try:
		doc.run_method("calculate_taxes_and_totals")
	except Exception:
		pass
	doc.save(ignore_permissions=True)

	for row in clean:
		if row.get("received_qty") is None:
			continue
		n = _set_po_line_received(doc.name, row["item_code"], flt(row["received_qty"]))
		if n:
			notes.append(n)
	return notes



_PIPELINE_IDS = frozenset(
	{"draft", "to_receive", "partial", "to_bill", "overdue", "done", "cancelled"}
)
_AUTO_PIPELINE = frozenset({"partial", "to_bill", "overdue", "done"})


def ensure_po_pipeline_override_field():
	"""Idempotent Custom Field so Compras can force a badge when docs conflict."""
	if frappe.db.has_column("Purchase Order", "custom_pipeline_override"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Purchase Order": [
				{
					"fieldname": "custom_pipeline_override",
					"label": "Compras Pipeline Override",
					"fieldtype": "Data",
					"insert_after": "status",
					"hidden": 1,
					"read_only": 1,
					"no_copy": 1,
				}
			]
		},
		ignore_validate=True,
	)


def _po_override_value(name: str) -> str:
	if not name or not frappe.db.has_column("Purchase Order", "custom_pipeline_override"):
		return ""
	return _as_str(frappe.db.get_value("Purchase Order", name, "custom_pipeline_override"))


def _set_po_override(name: str, value: str | None):
	ensure_po_pipeline_override_field()
	frappe.db.set_value(
		"Purchase Order",
		name,
		"custom_pipeline_override",
		_as_str(value) or None,
		update_modified=False,
	)


def _natural_pipeline_for_doc(doc) -> str:
	today = getdate(nowdate())
	sched = getdate(doc.schedule_date) if getattr(doc, "schedule_date", None) else None
	days_to_eta = (sched - today).days if sched else None
	return _pipeline_label(
		cint(doc.docstatus),
		doc.status or "",
		flt(doc.per_received),
		flt(doc.per_billed),
		days_to_eta,
		override=None,
	)


def _linked_pr_names(po_name: str) -> list[str]:
	return frappe.get_all(
		"Purchase Receipt Item",
		filters={"purchase_order": po_name, "docstatus": ["!=", 2]},
		pluck="parent",
		ignore_permissions=True,
	) or []


def _linked_pi_names(po_name: str) -> list[str]:
	return frappe.get_all(
		"Purchase Invoice Item",
		filters={"purchase_order": po_name, "docstatus": ["!=", 2]},
		pluck="parent",
		ignore_permissions=True,
	) or []


def _pipeline_conflict(doc, current: str, target: str) -> dict | None:
	"""Describe why a bar move is not a clean document action (admin can still force)."""
	blockers: list[str] = []
	reasons: list[str] = []
	options = [
		{
			"id": "override",
			"label": _("Override status badge only"),
			"hint": _("Keeps ERP documents as-is; Compras list/detail show the chosen step."),
		},
		{
			"id": "documents",
			"label": _("Apply document changes"),
			"hint": _(
				"Submit / cancel / receive as needed. Falls back to badge override if ERP blocks it."
			),
		},
	]

	if target == "cancelled":
		return {
			"current": current,
			"target": target,
			"reason": _("Cancelled cannot be selected from the pipeline bar."),
			"blockers": blockers,
			"options": [],
		}

	if target in _AUTO_PIPELINE:
		auto_msgs = {
			"partial": _(
				"Partial normally follows a Purchase Receipt with some qty received."
			),
			"to_bill": _("To Pay normally means fully received and not yet invoiced/paid."),
			"done": _("Done normally means received and billed (or closed)."),
			"overdue": _("Overdue is normally computed from ETA while qty is still pending."),
		}
		reasons.append(auto_msgs.get(target, _("This step is normally document-driven.")))

	prs = list({n for n in _linked_pr_names(doc.name) if n})
	pis = list({n for n in _linked_pi_names(doc.name) if n})
	if prs:
		blockers.append(_("Linked Purchase Receipt(s): {0}").format(", ".join(prs[:8])))
	if pis:
		blockers.append(_("Linked Purchase Invoice(s): {0}").format(", ".join(pis[:8])))

	if target == "draft" and cint(doc.docstatus) == 1:
		reasons.append(_("Moving to Draft cancels the Purchase Order."))
		if prs or pis:
			reasons.append(_("Linked receipts/invoices usually block cancel unless reversed first."))
		else:
			# Clean cancel — no modal needed
			return None

	if target == "to_receive" and cint(doc.docstatus) == 1 and current != "to_receive":
		reasons.append(
			_(
				"PO is already submitted; natural pipeline is driven by receiving/billing "
				"({0}). Forcing To Receive will override the badge (or try to unwind docs)."
			).format(current)
		)

	if target == "to_receive" and cint(doc.docstatus) == 0:
		# Clean submit path — no conflict modal.
		return None

	if target == "draft" and cint(doc.docstatus) == 0:
		return None

	if not reasons and not blockers and target in {"draft", "to_receive"}:
		return None

	if not reasons:
		reasons.append(_("Confirm this pipeline change."))

	return {
		"current": current,
		"target": target,
		"reason": " ".join(reasons),
		"blockers": blockers,
		"options": options,
	}


def _try_receive_remaining(po_name: str) -> str | None:
	"""Create+submit Purchase Receipt for remaining qty. Returns PR name or None."""
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	try:
		pr = make_purchase_receipt(po_name)
		if not pr or not getattr(pr, "items", None):
			return None
		# Drop zero-qty lines
		pr.items = [row for row in pr.items if flt(row.qty) > 0]
		if not pr.items:
			return None
		pr.flags.ignore_permissions = True
		pr.insert(ignore_permissions=True)
		pr.submit()
		return pr.name
	except Exception:
		frappe.log_error(title="Compras force receive failed")
		return None


def _apply_pipeline_documents(doc, target: str) -> list[str]:
	"""Best-effort ERP mutations for a forced pipeline target. Returns notes."""
	notes: list[str] = []
	name = doc.name

	if target == "to_receive":
		if cint(doc.docstatus) == 0:
			doc.flags.ignore_permissions = True
			doc.submit()
			notes.append(_("Submitted Purchase Order"))
			_set_po_override(name, None)
			return notes
		# Already submitted — clear override so natural label can show to_receive if applicable
		_set_po_override(name, None)
		natural = _natural_pipeline_for_doc(frappe.get_doc("Purchase Order", name))
		if natural != "to_receive":
			_set_po_override(name, "to_receive")
			notes.append(_("Override set to To Receive (receiving progress remains)"))
		else:
			notes.append(_("Pipeline already To Receive"))
		return notes

	if target == "draft":
		if cint(doc.docstatus) == 0:
			_set_po_override(name, None)
			return notes
		if cint(doc.docstatus) == 2:
			notes.append(_("Already cancelled"))
			return notes
		# Cancel linked drafts first, then PO
		for pr_name in _linked_pr_names(name):
			try:
				pr = frappe.get_doc("Purchase Receipt", pr_name)
				pr.flags.ignore_permissions = True
				if cint(pr.docstatus) == 1:
					pr.cancel()
					notes.append(_("Cancelled {0}").format(pr_name))
				elif cint(pr.docstatus) == 0:
					frappe.delete_doc("Purchase Receipt", pr_name, ignore_permissions=True)
					notes.append(_("Deleted draft {0}").format(pr_name))
			except Exception as e:
				notes.append(_("Could not clear {0}: {1}").format(pr_name, frappe.utils.cstr(e)))
		doc = frappe.get_doc("Purchase Order", name)
		doc.flags.ignore_permissions = True
		try:
			doc.cancel()
			_set_po_override(name, None)
			notes.append(_("Cancelled Purchase Order"))
		except Exception as e:
			_set_po_override(name, "draft")
			notes.append(
				_("Cancel blocked ({0}) — badge overridden to Draft").format(frappe.utils.cstr(e))
			)
		return notes

	if target in {"partial", "to_bill", "done"}:
		if cint(doc.docstatus) == 0:
			doc.flags.ignore_permissions = True
			doc.submit()
			notes.append(_("Submitted Purchase Order"))
			doc = frappe.get_doc("Purchase Order", name)
		if flt(doc.per_received) < 99.5 and target in {"to_bill", "done", "partial"}:
			pr_name = _try_receive_remaining(name)
			if pr_name:
				notes.append(_("Created Purchase Receipt {0}").format(pr_name))
			elif target == "partial" and flt(doc.per_received) <= 0.5:
				# Partial with nothing received yet — override
				_set_po_override(name, "partial")
				notes.append(_("Could not create receipt — badge overridden to Partial"))
				return notes
		doc = frappe.get_doc("Purchase Order", name)
		natural = _natural_pipeline_for_doc(doc)
		if natural != target:
			_set_po_override(name, target)
			notes.append(
				_("Natural pipeline is {0}; badge overridden to {1}").format(natural, target)
			)
		else:
			_set_po_override(name, None)
			notes.append(_("Documents already match {0}").format(target))
		return notes

	if target == "overdue":
		# Push ETA into the past so natural overdue applies when still open
		if cint(doc.docstatus) == 0:
			doc.flags.ignore_permissions = True
			doc.submit()
			notes.append(_("Submitted Purchase Order"))
			doc = frappe.get_doc("Purchase Order", name)
		from frappe.utils import add_days

		past = add_days(nowdate(), -1)
		frappe.db.set_value("Purchase Order", name, "schedule_date", past, update_modified=False)
		for row in doc.items or []:
			frappe.db.set_value(
				"Purchase Order Item", row.name, "schedule_date", past, update_modified=False
			)
		notes.append(_("ETA set to {0}").format(past))
		doc = frappe.get_doc("Purchase Order", name)
		natural = _natural_pipeline_for_doc(doc)
		if natural != "overdue":
			_set_po_override(name, "overdue")
			notes.append(_("Badge overridden to Overdue"))
		else:
			_set_po_override(name, None)
		return notes

	_set_po_override(name, target)
	notes.append(_("Badge overridden to {0}").format(target))
	return notes


@frappe.whitelist(allow_guest=True)
def set_purchase_order_pipeline(name=None, target_pipeline=None, force=0, resolve=None):
	"""
	Attempt a pipeline move for Tables → Compras status bar.

	Clean paths (no force):
	  - draft → to_receive (submit)
	  - submitted → draft (cancel) when nothing blocks

	When the move conflicts with document state, returns
	``{ok: False, conflict: {...}}`` so the UI can open a resolver modal.
	Pass ``force=1`` with ``resolve=override|documents`` to complete the move.
	"""
	name = _as_str(name)
	target = _as_str(target_pipeline).lower()
	resolve = _as_str(resolve).lower() or "override"
	do_force = cint(force)

	if not name:
		frappe.throw(_("name is required"))
	if not target:
		frappe.throw(_("target_pipeline is required"))
	if target not in _PIPELINE_IDS:
		frappe.throw(_("Unknown pipeline step: {0}").format(target))
	if target == "cancelled" and not do_force:
		frappe.throw(_("Cancelled documents cannot be selected from the pipeline bar."))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	ensure_po_pipeline_override_field()

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False

	current = _pipeline_label(
		cint(doc.docstatus),
		doc.status or "",
		flt(doc.per_received),
		flt(doc.per_billed),
		None,
		override=_po_override_value(name),
	)

	if target == current and not do_force:
		return get_purchase_order_detail(name)

	conflict = _pipeline_conflict(doc, current, target)

	# Clean submit draft → to_receive
	if not do_force and target == "to_receive" and cint(doc.docstatus) == 0 and not conflict:
		try:
			doc.flags.ignore_permissions = True
			doc.submit()
		except Exception as e:
			frappe.throw(
				_("Cannot move from Draft to To Receive: submit failed — {0}").format(
					frappe.utils.cstr(e)
				)
			)
		_set_po_override(name, None)
		frappe.db.commit()
		return get_purchase_order_detail(name)

	# Clean cancel → draft when no conflict signal
	if not do_force and target == "draft" and cint(doc.docstatus) == 1 and not conflict:
		try:
			doc.flags.ignore_permissions = True
			doc.cancel()
		except Exception as e:
			# Surface as conflict payload instead of hard fail when linked docs block
			return {
				"ok": False,
				"conflict": {
					"current": current,
					"target": target,
					"reason": _(
						"Cancel failed: {0}. Choose how to resolve."
					).format(frappe.utils.cstr(e)),
					"blockers": [
						b
						for b in [
							_("Linked Purchase Receipt(s): {0}").format(
								", ".join(_linked_pr_names(name)[:8])
							)
							if _linked_pr_names(name)
							else "",
							_("Linked Purchase Invoice(s): {0}").format(
								", ".join(_linked_pi_names(name)[:8])
							)
							if _linked_pi_names(name)
							else "",
						]
						if b
					],
					"options": [
						{
							"id": "override",
							"label": _("Override status badge only"),
							"hint": _("Keeps ERP documents as-is."),
						},
						{
							"id": "documents",
							"label": _("Cancel linked docs then PO"),
							"hint": _("Best-effort cancel of receipts, then the PO."),
						},
					],
				},
				"order": get_purchase_order_detail(name)["order"],
			}
		_set_po_override(name, None)
		frappe.db.commit()
		return get_purchase_order_detail(name)

	if conflict and not do_force:
		return {
			"ok": False,
			"conflict": conflict,
			"order": get_purchase_order_detail(name)["order"],
		}

	# Forced path
	notes: list[str] = []
	if resolve == "documents":
		notes = _apply_pipeline_documents(frappe.get_doc("Purchase Order", name), target)
	else:
		# override (default)
		if target == "to_receive" and cint(doc.docstatus) == 0:
			doc.flags.ignore_permissions = True
			doc.submit()
			_set_po_override(name, None)
			notes.append(_("Submitted Purchase Order"))
		elif target == "draft" and cint(doc.docstatus) == 1 and resolve == "override":
			_set_po_override(name, "draft")
			notes.append(_("Badge overridden to Draft (document still submitted)"))
		else:
			_set_po_override(name, None if target == _natural_pipeline_for_doc(doc) else target)
			notes.append(_("Badge set to {0}").format(target))

	frappe.db.commit()
	detail = get_purchase_order_detail(name)
	detail["ok"] = True
	detail["notes"] = notes
	detail["forced"] = 1
	detail["resolve"] = resolve
	return detail


@frappe.whitelist(allow_guest=True)
def list_item_buying_cost_trail(item_code=None, limit=40):
	"""Audit trail: PO lines (supplier/date/qty/rate) + Standard Buying price versions."""
	code = _as_str(item_code)
	if not code:
		frappe.throw(_("item_code is required"))
	if not frappe.db.exists("Item", code):
		frappe.throw(_("Item {0} not found").format(code))
	limit = _as_int(limit, 40, lo=1, hi=200)

	buying_pl = (
		frappe.db.get_single_value("Buying Settings", "buying_price_list") or "Standard Buying"
	)
	standard = frappe.db.get_value(
		"Item Price",
		{"item_code": code, "price_list": buying_pl, "buying": 1},
		"price_list_rate",
	)

	rows = []
	# Purchase Order Item history (provider + date + prices)
	po_rows = frappe.db.sql(
		"""
		SELECT
			poi.parent AS purchase_order,
			po.supplier AS supplier,
			po.supplier_name AS supplier_name,
			po.transaction_date AS date,
			poi.qty AS qty,
			poi.rate AS rate,
			po.owner AS who
		FROM `tabPurchase Order Item` poi
		INNER JOIN `tabPurchase Order` po ON po.name = poi.parent
		WHERE poi.item_code = %s
			AND po.docstatus < 2
		ORDER BY po.transaction_date DESC, po.creation DESC
		LIMIT %s
		""",
		(code, limit),
		as_dict=True,
	)
	for r in po_rows:
		rows.append(
			{
				"source": "purchase_order",
				"purchase_order": r.purchase_order,
				"supplier": r.supplier,
				"supplier_name": r.supplier_name,
				"date": str(r.date) if r.date else None,
				"qty": flt(r.qty),
				"rate": flt(r.rate),
				"previous_rate": None,
				"who": r.who,
				"note": None,
			}
		)

	# Standard Buying Item Price version history
	ip_name = frappe.db.get_value(
		"Item Price",
		{"item_code": code, "price_list": buying_pl, "buying": 1},
		"name",
	)
	if ip_name:
		versions = frappe.get_all(
			"Version",
			filters={"ref_doctype": "Item Price", "docname": ip_name},
			fields=["name", "owner", "creation", "data"],
			order_by="creation desc",
			limit=limit,
			ignore_permissions=True,
		)
		for v in versions:
			try:
				data = json.loads(v.data) if isinstance(v.data, str) else (v.data or {})
			except Exception:
				continue
			for ch in data.get("changed") or []:
				if not isinstance(ch, (list, tuple)) or len(ch) < 3:
					continue
				if ch[0] != "price_list_rate":
					continue
				rows.append(
					{
						"source": "standard_buying",
						"purchase_order": None,
						"supplier": None,
						"supplier_name": None,
						"date": str(v.creation)[:10] if v.creation else None,
						"qty": None,
						"rate": flt(ch[2]) if ch[2] not in (None, "") else 0,
						"previous_rate": flt(ch[1]) if ch[1] not in (None, "") else None,
						"who": v.owner,
						"note": _("Standard Buying updated"),
					}
				)

	rows.sort(key=lambda r: r.get("date") or "", reverse=True)
	return {
		"ok": True,
		"item_code": code,
		"standard_buying": flt(standard) if standard is not None else None,
		"rows": rows[:limit],
	}


def _pipeline_label(
	docstatus: int,
	status: str,
	per_received: float,
	per_billed: float,
	days_to_eta,
	override=None,
) -> str:
	"""Coarse buying pipeline for filters / badges. Optional override wins for Compras UI."""
	ov = _as_str(override).lower()
	if ov in _PIPELINE_IDS and ov != "cancelled":
		return ov
	st = (status or "").lower()
	if cint(docstatus) == 0 or "draft" in st:
		return "draft"
	if "cancel" in st:
		return "cancelled"
	if "closed" in st or "complet" in st:
		return "done"
	if days_to_eta is not None and days_to_eta < 0 and flt(per_received) < 99.5:
		return "overdue"
	if flt(per_received) >= 99.5 and flt(per_billed) >= 99.5:
		return "done"
	if flt(per_received) >= 99.5:
		return "to_bill"
	if flt(per_received) > 0.5:
		return "partial"
	return "to_receive"


def _enrich_po_rows(rows: list) -> list:
	"""Attach line aggregates, brand mix, and ETA metrics for Tables → Compras."""
	if not rows:
		return []
	names = [r.name for r in rows]
	today = getdate(nowdate())

	item_rows = frappe.get_all(
		"Purchase Order Item",
		filters={"parent": ["in", names]},
		fields=[
			"parent",
			"item_code",
			"item_name",
			"qty",
			"received_qty",
			"amount",
			"rate",
			"billed_amt",
		],
		ignore_permissions=True,
	)
	by_po: dict[str, list] = {}
	codes: set[str] = set()
	for it in item_rows:
		by_po.setdefault(it.parent, []).append(it)
		if it.item_code:
			codes.add(it.item_code)

	brand_map: dict[str, str] = {}
	if codes:
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", list(codes)]},
			fields=["name", "brand"],
			ignore_permissions=True,
		):
			if row.brand:
				brand_map[row.name] = row.brand

	overrides: dict[str, str] = {}
	if names and frappe.db.has_column("Purchase Order", "custom_pipeline_override"):
		for row in frappe.get_all(
			"Purchase Order",
			filters={"name": ["in", names]},
			fields=["name", "custom_pipeline_override"],
			ignore_permissions=True,
		):
			ov = _as_str(row.custom_pipeline_override)
			if ov:
				overrides[row.name] = ov

	out = []
	for r in rows:
		lines = by_po.get(r.name, [])
		qty_ordered = sum(flt(x.qty) for x in lines)
		qty_received = sum(flt(x.received_qty) for x in lines)
		line_count = len(lines)
		sku_count = len({x.item_code for x in lines if x.item_code})
		amount_billed = sum(flt(getattr(x, "billed_amt", 0) or 0) for x in lines)
		qty_billed = 0.0
		for x in lines:
			rate = flt(x.rate)
			billed = flt(getattr(x, "billed_amt", 0) or 0)
			if rate > 0 and billed > 0:
				qty_billed += min(flt(x.qty), billed / rate)
			elif flt(x.qty) > 0 and flt(x.amount) > 0 and billed >= flt(x.amount) - 0.01:
				qty_billed += flt(x.qty)
		preview = []
		for x in lines[:3]:
			label = _as_str(x.item_name) or _as_str(x.item_code)
			if label:
				preview.append(label)
		brands = sorted(
			{
				brand_map[x.item_code]
				for x in lines
				if x.item_code and brand_map.get(x.item_code)
			}
		)
		tx = getdate(r.transaction_date) if r.transaction_date else None
		sched = getdate(r.schedule_date) if r.schedule_date else None
		age_days = (today - tx).days if tx else None
		days_to_eta = (sched - today).days if sched else None
		per_recv = flt(r.per_received)
		per_bill = flt(r.per_billed)
		grand = flt(r.grand_total)
		open_receive_value = max(0.0, grand * (100.0 - min(per_recv, 100.0)) / 100.0)
		open_bill_value = max(0.0, grand * (100.0 - min(per_bill, 100.0)) / 100.0)
		ov = overrides.get(r.name)
		pipeline_natural = _pipeline_label(
			cint(r.docstatus), r.status or "", per_recv, per_bill, days_to_eta, override=None
		)
		pipeline = _pipeline_label(
			cint(r.docstatus), r.status or "", per_recv, per_bill, days_to_eta, override=ov
		)

		out.append(
			{
				"name": r.name,
				"supplier": r.supplier,
				"supplier_name": r.supplier_name,
				"company": getattr(r, "company", None),
				"owner": getattr(r, "owner", None),
				"modified": str(r.modified) if getattr(r, "modified", None) else None,
				"transaction_date": str(r.transaction_date) if r.transaction_date else None,
				"schedule_date": str(r.schedule_date) if r.schedule_date else None,
				"grand_total": grand,
				"net_total": flt(getattr(r, "net_total", 0) or 0),
				"total_taxes_and_charges": flt(getattr(r, "total_taxes_and_charges", 0) or 0),
				"advance_paid": flt(getattr(r, "advance_paid", 0) or 0),
				"status": r.status,
				"docstatus": cint(r.docstatus),
				"currency": r.currency,
				"per_received": per_recv,
				"per_billed": per_bill,
				"line_count": line_count,
				"sku_count": sku_count,
				"qty_ordered": qty_ordered,
				"qty_received": qty_received,
				"qty_pending": max(0.0, qty_ordered - qty_received),
				"qty_billed": qty_billed,
				"amount_billed": amount_billed,
				"items_preview": preview,
				"brands": brands,
				"brands_label": ", ".join(brands[:3]) + ("…" if len(brands) > 3 else ""),
				"age_days": age_days,
				"days_to_eta": days_to_eta,
				"open_receive_value": open_receive_value,
				"open_bill_value": open_bill_value,
				"pipeline": pipeline,
				"pipeline_natural": pipeline_natural,
				"pipeline_override": ov or None,
				"remarks": None,
			}
		)
	return out


@frappe.whitelist(allow_guest=True)
def list_purchase_orders(
	supplier=None,
	start=0,
	page_length=30,
	status=None,
	pipeline=None,
	search=None,
	include_cancelled=0,
):
	"""Purchase Orders for Logistics Buying + Tables → Compras."""
	start = _as_int(start, 0, lo=0, hi=100000)
	page_length = _as_int(page_length, 30, lo=1, hi=200)
	filters: dict = {}
	if cint(include_cancelled):
		filters["docstatus"] = ["<", 3]
	else:
		filters["docstatus"] = ["<", 2]

	sup = _as_str(supplier)
	if sup:
		filters["supplier"] = sup
	st = _as_str(status)
	if st and st.lower() not in ("all", "*", "any"):
		filters["status"] = st

	or_filters = None
	q = _as_str(search)
	if q:
		or_filters = [
			["name", "like", f"%{q}%"],
			["supplier", "like", f"%{q}%"],
			["supplier_name", "like", f"%{q}%"],
		]

	rows = frappe.get_all(
		"Purchase Order",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"supplier",
			"supplier_name",
			"company",
			"owner",
			"modified",
			"transaction_date",
			"schedule_date",
			"grand_total",
			"net_total",
			"total_taxes_and_charges",
			"advance_paid",
			"status",
			"docstatus",
			"currency",
			"per_received",
			"per_billed",
		],
		order_by="modified desc",
		limit_start=start,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = (
		len(
			frappe.get_all(
				"Purchase Order",
				filters=filters,
				or_filters=or_filters,
				pluck="name",
				ignore_permissions=True,
			)
		)
		if or_filters
		else frappe.db.count("Purchase Order", filters)
	)
	enriched = _enrich_po_rows(rows)

	pipe = _as_str(pipeline).lower()
	if pipe and pipe not in ("all", "*", "any"):
		enriched = [r for r in enriched if r.get("pipeline") == pipe]

	return {"ok": True, "total": cint(total), "rows": enriched}


@frappe.whitelist(allow_guest=True)
def get_purchase_order_detail(name=None):
	"""Single PO with lines for Tables → Compras detail panel."""
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False

	lines = []
	for row in doc.items or []:
		brand = frappe.db.get_value("Item", row.item_code, "brand") if row.item_code else None
		qty = flt(row.qty)
		recv = flt(row.received_qty)
		lines.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"brand": brand,
				"qty": qty,
				"received_qty": recv,
				"pending_qty": max(0.0, qty - recv),
				"rate": flt(row.rate),
				"amount": flt(row.amount),
				"uom": row.uom,
				"schedule_date": str(row.schedule_date) if row.schedule_date else None,
				"description": _as_str(row.description)[:240] or None,
			}
		)

	base = _enrich_po_rows(
		[
			frappe._dict(
				{
					"name": doc.name,
					"supplier": doc.supplier,
					"supplier_name": doc.supplier_name,
					"company": doc.company,
					"owner": doc.owner,
					"modified": doc.modified,
					"transaction_date": doc.transaction_date,
					"schedule_date": doc.schedule_date,
					"grand_total": doc.grand_total,
					"net_total": doc.net_total,
					"total_taxes_and_charges": doc.total_taxes_and_charges,
					"advance_paid": getattr(doc, "advance_paid", 0) or 0,
					"status": doc.status,
					"docstatus": doc.docstatus,
					"currency": doc.currency,
					"per_received": doc.per_received,
					"per_billed": doc.per_billed,
					"remarks": _as_str(getattr(doc, "remarks", None)) or None,
				}
			)
		]
	)[0]
	base["lines"] = lines
	base["payments"] = _payments_for_purchase_order(doc.name, flt(doc.grand_total))
	return {"ok": True, "order": base}


# Parent fields: transaction amount → matching base_* (1:1 after retag).
_PO_BASE_MIRROR = (
	("total", "base_total"),
	("net_total", "base_net_total"),
	("total_taxes_and_charges", "base_total_taxes_and_charges"),
	("grand_total", "base_grand_total"),
	("rounding_adjustment", "base_rounding_adjustment"),
	("rounded_total", "base_rounded_total"),
	("taxes_and_charges_added", "base_taxes_and_charges_added"),
	("taxes_and_charges_deducted", "base_taxes_and_charges_deducted"),
	("discount_amount", "base_discount_amount"),
)

_PO_ITEM_BASE_MIRROR = (
	("rate", "base_rate"),
	("amount", "base_amount"),
	("net_rate", "base_net_rate"),
	("net_amount", "base_net_amount"),
)


def _retag_one_po(name: str, currency: str) -> None:
	"""Retag a PO's currency without FX — amounts stay the same number."""
	fields = [
		"currency",
		"conversion_rate",
		"price_list_currency",
		"plc_conversion_rate",
		"party_account_currency",
	]
	for txn, base in _PO_BASE_MIRROR:
		fields.extend([txn, base])
	# Deduplicate while preserving order
	seen = set()
	fields = [f for f in fields if not (f in seen or seen.add(f))]
	fields = [f for f in fields if frappe.db.has_column("Purchase Order", f)]

	row = frappe.db.get_value("Purchase Order", name, fields, as_dict=True)
	if not row:
		return

	updates = {
		"currency": currency,
		"conversion_rate": 1.0,
	}
	if "plc_conversion_rate" in row:
		updates["plc_conversion_rate"] = 1.0
	if "price_list_currency" in row:
		updates["price_list_currency"] = currency
	if "party_account_currency" in row:
		updates["party_account_currency"] = currency

	for txn, base in _PO_BASE_MIRROR:
		if base in row and txn in row:
			updates[base] = flt(row.get(txn))

	frappe.db.set_value("Purchase Order", name, updates, update_modified=True)

	item_fields = ["name"]
	for txn, base in _PO_ITEM_BASE_MIRROR:
		if frappe.db.has_column("Purchase Order Item", txn):
			item_fields.append(txn)
		if frappe.db.has_column("Purchase Order Item", base):
			item_fields.append(base)
	item_fields = list(dict.fromkeys(item_fields))

	items = frappe.get_all(
		"Purchase Order Item",
		filters={"parent": name},
		fields=item_fields,
		ignore_permissions=True,
	)
	for it in items:
		item_upd = {}
		for txn, base in _PO_ITEM_BASE_MIRROR:
			if base in it and txn in it:
				item_upd[base] = flt(it.get(txn))
		if item_upd:
			frappe.db.set_value("Purchase Order Item", it.name, item_upd, update_modified=False)


@frappe.whitelist(allow_guest=True)
def force_retag_po_currency(currency=None, company=None):
	"""Force all Purchase Orders to ``currency`` without converting amounts.

	Example: USD 30 → ARS 30 (same number). Sets conversion_rate=1 and mirrors
	base_* totals from transaction amounts. Skips cancelled POs and those
	already in the target currency.
	"""
	company = resolve_company(company) or frappe.db.get_value("Company", {}, "name")
	currency = _as_str(currency)
	if not currency and company:
		currency = _as_str(frappe.db.get_value("Company", company, "default_currency"))
	if not currency:
		currency = "ARS"
	if not frappe.db.exists("Currency", currency):
		frappe.throw(_("Currency {0} not found").format(currency), frappe.ValidationError)
	if not cint(frappe.db.get_value("Currency", currency, "enabled")):
		frappe.db.set_value("Currency", currency, "enabled", 1, update_modified=False)

	filters = {"docstatus": ["<", 2], "currency": ["!=", currency]}
	if company:
		filters["company"] = company

	names = frappe.get_all(
		"Purchase Order",
		filters=filters,
		pluck="name",
		ignore_permissions=True,
	)
	updated = 0
	for name in names:
		_retag_one_po(name, currency)
		updated += 1

	already_filters = {"docstatus": ["<", 2], "currency": currency}
	if company:
		already_filters["company"] = company
	already = frappe.db.count("Purchase Order", filters=already_filters)
	frappe.db.commit()
	return {
		"ok": True,
		"currency": currency,
		"company": company,
		"updated": updated,
		"already": already,
		"total_active": updated + already,
	}


# ── Compras payments (Payment Entry Pay → Purchase Order) ─────────────────────


def _payments_for_purchase_order(po_name: str, grand_total=None) -> list:
	"""Payment Entry rows allocated to this Purchase Order (oldest first)."""
	if not po_name:
		return []
	rows = frappe.db.sql(
		"""
		SELECT
			pe.name AS name,
			pe.posting_date AS posting_date,
			pe.mode_of_payment AS mode_of_payment,
			pe.paid_amount AS paid_amount,
			pe.received_amount AS received_amount,
			pe.docstatus AS docstatus,
			pe.creation AS creation,
			per.allocated_amount AS allocated_amount
		FROM `tabPayment Entry Reference` per
		INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		WHERE per.reference_doctype = 'Purchase Order'
			AND per.reference_name = %s
			AND pe.docstatus < 2
		ORDER BY pe.posting_date ASC, pe.creation ASC
		""",
		(po_name,),
		as_dict=True,
	)
	total = flt(grand_total)
	if total <= 0 and po_name and frappe.db.exists("Purchase Order", po_name):
		total = flt(frappe.db.get_value("Purchase Order", po_name, "grand_total") or 0)

	out = []
	seen = set()
	running = 0.0
	for r in rows or []:
		name = r.get("name")
		if not name or name in seen:
			continue
		seen.add(name)
		amount = flt(r.get("allocated_amount"))
		if amount <= 0:
			amount = flt(r.get("paid_amount") or r.get("received_amount"))
		running += amount
		out.append(
			{
				"name": name,
				"posting_date": str(r.posting_date) if r.get("posting_date") else None,
				"mode_of_payment": r.get("mode_of_payment") or None,
				"amount": amount,
				"docstatus": cint(r.get("docstatus")),
				"outstanding_after": max(0.0, total - running),
			}
		)
	return out


def _resolve_payable_account(company: str, supplier: str | None = None) -> str | None:
	"""Supplier payable account for Payment Entry Pay."""
	if supplier:
		acc = frappe.db.get_value(
			"Party Account",
			{"parent": supplier, "parenttype": "Supplier", "company": company},
			"account",
		)
		if acc:
			return acc

	acc = frappe.db.get_value("Company", company, "default_payable_account")
	if acc and frappe.db.exists("Account", acc):
		return acc

	for like in (
		"%Acreedores locales%",
		"%Creditors%",
		"%Acreedores%",
		"%Proveedores%",
		"%Payable%",
	):
		acc = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_type": "Payable",
				"is_group": 0,
				"disabled": 0,
				"name": ("like", like),
			},
			"name",
		)
		if acc:
			return acc

	return frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Payable", "is_group": 0, "disabled": 0},
		"name",
	)


def _resolve_purchase_payment_accounts(company: str, mode_of_payment=None, supplier=None):
	"""Return (cash_account, payable_account, mop_name) for supplier Pay entries."""
	from erpnext.erpnext_integrations.ecommerce_api.api import (
		_ensure_preorder_mop_account,
		_resolve_preorder_cash_account,
		_resolve_preorder_mop_name,
	)

	mop = _resolve_preorder_mop_name(mode_of_payment)
	cash_account = _resolve_preorder_cash_account(company, mop)
	payable_account = _resolve_payable_account(company, supplier)

	if cash_account and mop:
		_ensure_preorder_mop_account(company, mop, cash_account)

	if not payable_account or not cash_account:
		missing = []
		if not payable_account:
			missing.append(_("payable (Company → Default Payable Account)"))
		if not cash_account:
			missing.append(_("cash/bank (Mode of Payment Account or Company cash account)"))
		frappe.throw(
			_("Could not find debit/credit accounts for payment ({0}). Check company defaults.").format(
				", ".join(str(m) for m in missing)
			)
		)
	return cash_account, payable_account, mop


@frappe.whitelist(allow_guest=True)
def list_purchase_payment_modes():
	"""Enabled Mode of Payment names for Compras payment UI."""
	from erpnext.erpnext_integrations.ecommerce_api.api import list_preorder_payment_modes

	return list_preorder_payment_modes()


@frappe.whitelist(allow_guest=True)
@idempotent_request
def record_purchase_order_payment(
	name=None,
	paid_amount=None,
	mode_of_payment="Cash",
	posting_date=None,
):
	"""Create a Payment Entry (Pay) allocated to a submitted Purchase Order."""
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	po = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False

	if cint(po.docstatus) != 1:
		frappe.throw(_("Payment can only be recorded on submitted purchase orders"))
	if not po.supplier:
		frappe.throw(_("Purchase Order has no supplier"))

	paid_amount = flt(paid_amount)
	if paid_amount <= 0:
		frappe.throw(_("Paid amount must be greater than zero"))

	company = po.company
	cash_account, payable_account, mop = _resolve_purchase_payment_accounts(
		company, mode_of_payment, po.supplier
	)

	outstanding = flt(po.grand_total) - flt(getattr(po, "advance_paid", 0) or 0)
	allocated = min(paid_amount, outstanding) if outstanding > 0 else paid_amount

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Pay"
	pe.company = company
	pe.party_type = "Supplier"
	pe.party = po.supplier
	pe.mode_of_payment = mop
	pe.paid_from = cash_account
	pe.paid_to = payable_account
	pe.paid_from_account_currency = po.currency
	pe.paid_to_account_currency = po.currency
	pe.paid_amount = paid_amount
	pe.received_amount = paid_amount
	pe.reference_date = nowdate()
	pe.reference_no = name
	raw_pd = _as_str(posting_date)
	if raw_pd:
		try:
			pe.posting_date = str(getdate(raw_pd))
		except Exception:
			pass
	if allocated > 0:
		pe.append(
			"references",
			{
				"reference_doctype": "Purchase Order",
				"reference_name": name,
				"total_amount": flt(po.grand_total),
				"outstanding_amount": outstanding,
				"allocated_amount": allocated,
			},
		)
	pe.insert(ignore_permissions=True)
	pe.submit()
	frappe.db.commit()
	return get_purchase_order_detail(name)


@frappe.whitelist(allow_guest=True)
def update_purchase_order_payment(
	payment_name=None,
	posting_date=None,
	mode_of_payment=None,
	paid_amount=None,
):
	"""Amend a Compras payment: cancel PE + recreate."""
	payment_name = _as_str(payment_name)
	if not payment_name:
		frappe.throw(_("payment_name is required"))
	if not frappe.db.exists("Payment Entry", payment_name):
		frappe.throw(_("Payment Entry {0} not found").format(payment_name))

	frappe.flags.ignore_permissions = True
	pe = frappe.get_doc("Payment Entry", payment_name)
	frappe.flags.ignore_permissions = False

	po_name = None
	for ref in pe.references or []:
		if ref.reference_doctype == "Purchase Order" and ref.reference_name:
			po_name = ref.reference_name
			break
	if not po_name:
		po_name = _as_str(getattr(pe, "reference_no", None)) or None
	if not po_name or not frappe.db.exists("Purchase Order", po_name):
		frappe.throw(_("Payment is not linked to a Purchase Order"))

	old_amount = flt(pe.paid_amount or pe.received_amount)
	old_mode = pe.mode_of_payment or ""
	old_date = str(pe.posting_date) if pe.posting_date else ""

	new_amount = flt(paid_amount) if paid_amount not in (None, "") else old_amount
	if new_amount <= 0:
		frappe.throw(_("Paid amount must be greater than zero"))
	new_mode = _as_str(mode_of_payment) or old_mode or "Cash"
	new_date = _as_str(posting_date) or old_date or nowdate()

	if cint(pe.docstatus) == 1:
		pe.cancel()
	elif cint(pe.docstatus) == 0:
		pe.delete()

	detail = record_purchase_order_payment(
		name=po_name,
		paid_amount=new_amount,
		mode_of_payment=new_mode,
		posting_date=new_date,
	)

	try:
		frappe.flags.ignore_permissions = True
		po = frappe.get_doc("Purchase Order", po_name)
		frappe.flags.ignore_permissions = False
		po.add_comment(
			"Comment",
			_("Payment amended: {0} → new entry (was {1} {2} on {3})").format(
				payment_name,
				old_amount,
				old_mode,
				old_date,
			),
		)
		frappe.db.commit()
	except Exception:
		pass

	return detail

