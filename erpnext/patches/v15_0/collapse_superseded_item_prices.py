import frappe


def execute():
	"""Delete Item Price rows shadowed by a newer valid_from (kept in Deleted Documents)."""
	from erpnext.erpnext_integrations.ecommerce_api.item_pricing import collapse_superseded_item_prices

	frappe.flags.in_item_price_collapse = True
	try:
		out = collapse_superseded_item_prices()
	finally:
		frappe.flags.in_item_price_collapse = False
	if out["count"]:
		print(f"collapse_superseded_item_prices: removed {out['count']} superseded Item Price row(s)")
