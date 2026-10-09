"""Effective Item Price resolution + cleanup of superseded duplicate rows.

ERPNext prices a line from the Item Price row for (item, price list, uom,
customer) that is valid on the date and has the latest ``valid_from``. Our
upserts and SQL readers (``MAX(price_list_rate)`` / ``LIMIT 1``) assume ONE row
per key, so dated duplicates from imports or the desk made the catalog, the
product table and the MCP show a stale price.

- ``effective_item_price`` — the row ERPNext would use (single source of truth).
- ``collapse_superseded_item_prices`` — delete currently-valid rows that lost to
  a newer ``valid_from`` (kept in Deleted Documents). Future-dated and expired
  rows are left alone. Runs on Item Price save, daily, and once as a patch.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt, getdate, nowdate

# Same tie-break as ERPNext's get_item_price (latest valid_from; undated = oldest).
_EFFECTIVE_ORDER = "(valid_from IS NULL), valid_from DESC, creation DESC"


def effective_item_price(item_code, price_list, *, uom=None, customer=None, on_date=None) -> dict | None:
	"""Return {name, price_list_rate, uom, valid_from} of the row ERPNext uses, or None."""
	if not item_code or not price_list:
		return None
	values = {
		"item_code": item_code,
		"price_list": price_list,
		"on_date": getdate(on_date or nowdate()),
		"customer": cstr(customer).strip(),
	}
	uom_sql = ""
	if uom:
		uom_sql = "AND uom = %(uom)s"
		values["uom"] = uom
	rows = frappe.db.sql(
		f"""
		SELECT name, price_list_rate, uom, valid_from
		FROM `tabItem Price`
		WHERE item_code = %(item_code)s
		  AND price_list = %(price_list)s
		  AND IFNULL(customer, '') = %(customer)s
		  AND (valid_from IS NULL OR valid_from <= %(on_date)s)
		  AND (valid_upto IS NULL OR valid_upto >= %(on_date)s)
		  {uom_sql}
		ORDER BY {_EFFECTIVE_ORDER}
		LIMIT 1
		""",
		values,
		as_dict=True,
	)
	return rows[0] if rows else None


def effective_item_rates(item_codes, price_list, *, on_date=None) -> dict:
	"""Map item_code → effective rate (items without a valid price are omitted)."""
	out = {}
	for code in dict.fromkeys(c for c in (item_codes or []) if c):
		row = effective_item_price(code, price_list, on_date=on_date)
		if row and flt(row.price_list_rate) > 0:
			out[code] = flt(row.price_list_rate)
	return out


def collapse_superseded_item_prices(item_code=None, price_list=None, *, dry_run=False) -> dict:
	"""Delete currently-valid Item Price rows that are shadowed by a newer one.

	Groups by (item, price list, uom, customer, supplier, batch). Within a group
	the effective row (``_EFFECTIVE_ORDER``) is kept; every other row valid
	today is deleted via ``frappe.delete_doc`` so it lands in Deleted Documents.
	"""
	values = {"today": getdate(nowdate())}
	scope_sql = ""
	if item_code:
		scope_sql += " AND item_code = %(item_code)s"
		values["item_code"] = item_code
	if price_list:
		scope_sql += " AND price_list = %(price_list)s"
		values["price_list"] = price_list
	rows = frappe.db.sql(
		f"""
		SELECT name, item_code, price_list, price_list_rate, valid_from,
			IFNULL(uom, '') AS uom, IFNULL(customer, '') AS customer,
			IFNULL(supplier, '') AS supplier, IFNULL(batch_no, '') AS batch_no
		FROM `tabItem Price`
		WHERE (valid_from IS NULL OR valid_from <= %(today)s)
		  AND (valid_upto IS NULL OR valid_upto >= %(today)s)
		  {scope_sql}
		ORDER BY item_code, price_list, {_EFFECTIVE_ORDER}
		""",
		values,
		as_dict=True,
	)
	kept: set = set()
	removed = []
	for row in rows:
		key = (row.item_code, row.price_list, row.uom, row.customer, row.supplier, row.batch_no)
		if key not in kept:
			kept.add(key)  # first row per key in effective order = the one ERPNext uses
			continue
		removed.append(
			{
				"name": row.name,
				"item_code": row.item_code,
				"price_list": row.price_list,
				"rate": flt(row.price_list_rate),
				"valid_from": cstr(row.valid_from) or None,
			}
		)
	if not dry_run:
		for row in removed:
			frappe.delete_doc("Item Price", row["name"], ignore_permissions=True, force=True)
	return {"removed": removed, "count": len(removed), "dry_run": bool(dry_run)}


def on_item_price_update(doc, method=None):
	"""doc_events hook: a newly effective row retires the rows it supersedes."""
	if frappe.flags.get("in_item_price_collapse"):
		return
	if doc.valid_from and getdate(doc.valid_from) > getdate(nowdate()):
		return  # future price — the daily job collapses it once it takes effect
	effective = effective_item_price(
		doc.item_code, doc.price_list, uom=doc.uom or None, customer=doc.customer or None
	)
	if not effective or effective.name != doc.name:
		return  # never delete the row being saved; only a winning row retires others
	frappe.flags.in_item_price_collapse = True
	try:
		collapse_superseded_item_prices(doc.item_code, doc.price_list)
	finally:
		frappe.flags.in_item_price_collapse = False


def daily_collapse_superseded_item_prices():
	"""Scheduler: future-dated prices that took effect today retire the old row."""
	frappe.flags.in_item_price_collapse = True
	try:
		collapse_superseded_item_prices()
		frappe.db.commit()
	finally:
		frappe.flags.in_item_price_collapse = False
