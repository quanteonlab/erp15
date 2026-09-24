"""Simplified Purchase Order API for Logistics → Buying."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company


def _as_str(v) -> str:
	if v is None:
		return ""
	if isinstance(v, (list, dict, tuple)):
		return ""
	s = str(v).strip()
	if s.lower() in ("null", "undefined", "none"):
		return ""
	return s


def _as_int(v, default: int, lo: int = 0, hi: int = 500) -> int:
	try:
		n = cint(v)
	except Exception:
		n = default
	if n < lo:
		n = default
	return min(max(n, lo), hi)


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
):
	"""Create a Purchase Order with one shared expected (schedule) date."""
	supplier = _as_str(supplier)
	if not supplier:
		frappe.throw(_("supplier is required"))
	if not frappe.db.exists("Supplier", supplier):
		frappe.throw(_("Supplier {0} not found").format(supplier))

	sched = _as_str(schedule_date) or nowdate()
	try:
		sched = str(getdate(sched))
	except Exception:
		sched = nowdate()

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
			"schedule_date": row["schedule_date"] or sched,
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
):
	"""Update a draft Purchase Order (lines / ETA). Submitted docs must be amended in desk."""
	name = _as_str(name)
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft purchase orders can be edited here"))

	sched = _as_str(schedule_date)
	if sched:
		try:
			doc.schedule_date = str(getdate(sched))
		except Exception:
			pass

	clean = _parse_items(items)
	if clean:
		doc.set("items", [])
		for row in clean:
			line = {
				"item_code": row["item_code"],
				"qty": row["qty"],
				"rate": row["rate"],
				"schedule_date": row["schedule_date"] or doc.schedule_date,
			}
			if row.get("uom"):
				line["uom"] = row["uom"]
			if row.get("description"):
				line["description"] = row["description"]
			doc.append("items", line)

	if not doc.items:
		frappe.throw(_("Add at least one item"))

	if not doc.title:
		doc.title = (
			frappe.db.get_value("Supplier", doc.supplier, "supplier_name") or doc.supplier or name
		)
	if not doc.status:
		doc.status = "Draft"

	doc.save(ignore_permissions=True)

	note = _as_str(notes)
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


@frappe.whitelist(allow_guest=True)
def set_purchase_order_pipeline(name=None, target_pipeline=None):
	"""
	Attempt a pipeline move for Tables → Compras status bar.

	Selectable targets today:
	  - draft → to_receive (submit)
	  - to_receive / partial / to_bill / done / overdue → draft (cancel; may fail if linked)

	partial / to_bill / done / overdue cannot be forced manually — return a clear reason.
	"""
	name = _as_str(name)
	target = _as_str(target_pipeline).lower()
	if not name:
		frappe.throw(_("name is required"))
	if not target:
		frappe.throw(_("target_pipeline is required"))
	if not frappe.db.exists("Purchase Order", name):
		frappe.throw(_("Purchase Order {0} not found").format(name))

	allowed_manual = {"draft", "to_receive"}
	auto_only = {
		"partial": _(
			"Partial is set automatically when a Purchase Receipt receives some qty. "
			"Create / submit a Purchase Receipt instead."
		),
		"to_bill": _(
			"To Bill means the PO is fully received but not invoiced. "
			"Create a Purchase Invoice against this order."
		),
		"done": _(
			"Done means received and billed (or closed). "
			"Complete receiving and billing documents in ERP — cannot jump here from the status bar."
		),
		"overdue": _(
			"Overdue is computed from the expected delivery date while qty is still pending. "
			"Change the ETA or receive goods — it is not a manual status."
		),
		"cancelled": _("Cancelled documents cannot be selected from the pipeline bar."),
	}

	if target in auto_only:
		frappe.throw(auto_only[target])
	if target not in allowed_manual:
		frappe.throw(_("Unknown pipeline step: {0}").format(target))

	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Purchase Order", name)
	frappe.flags.ignore_permissions = False

	current = _pipeline_label(
		cint(doc.docstatus),
		doc.status or "",
		flt(doc.per_received),
		flt(doc.per_billed),
		None,
	)

	if target == current:
		return get_purchase_order_detail(name)

	if target == "to_receive":
		if cint(doc.docstatus) == 0:
			try:
				doc.flags.ignore_permissions = True
				doc.submit()
			except Exception as e:
				frappe.throw(
					_("Cannot move from Draft to To Receive: submit failed — {0}").format(
						frappe.utils.cstr(e)
					)
				)
			frappe.db.commit()
			return get_purchase_order_detail(name)
		if cint(doc.docstatus) == 1:
			# Already submitted — pipeline may read as partial/to_bill/done/overdue
			frappe.throw(
				_(
					"Cannot force To Receive from {0}. "
					"This PO is already submitted; receiving/billing progress drives the pipeline."
				).format(current)
			)
		frappe.throw(_("Cannot submit a cancelled Purchase Order"))

	# target == draft
	if cint(doc.docstatus) == 0:
		return get_purchase_order_detail(name)
	if cint(doc.docstatus) == 2:
		frappe.throw(_("Purchase Order is already cancelled"))
	try:
		doc.flags.ignore_permissions = True
		doc.cancel()
	except Exception as e:
		frappe.throw(
			_(
				"Cannot go back to Draft from {0}: cancel failed — {1}. "
				"Linked receipts or invoices usually block this; amend or reverse them in ERP first."
			).format(current, frappe.utils.cstr(e))
		)
	frappe.db.commit()
	return get_purchase_order_detail(name)


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


def _pipeline_label(docstatus: int, status: str, per_received: float, per_billed: float, days_to_eta) -> str:
	"""Coarse buying pipeline for filters / badges."""
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

	out = []
	for r in rows:
		lines = by_po.get(r.name, [])
		qty_ordered = sum(flt(x.qty) for x in lines)
		qty_received = sum(flt(x.received_qty) for x in lines)
		line_count = len(lines)
		sku_count = len({x.item_code for x in lines if x.item_code})
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
		pipeline = _pipeline_label(cint(r.docstatus), r.status or "", per_recv, per_bill, days_to_eta)

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
				"items_preview": preview,
				"brands": brands,
				"brands_label": ", ".join(brands[:3]) + ("…" if len(brands) > 3 else ""),
				"age_days": age_days,
				"days_to_eta": days_to_eta,
				"open_receive_value": open_receive_value,
				"open_bill_value": open_bill_value,
				"pipeline": pipeline,
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
					"status": doc.status,
					"docstatus": doc.docstatus,
					"currency": doc.currency,
					"per_received": doc.per_received,
					"per_billed": doc.per_billed,
					"remarks": None,
				}
			)
		]
	)[0]
	base["lines"] = lines
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
