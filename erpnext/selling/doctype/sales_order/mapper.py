# Compatibility shim: upstream ERPNext moved make_sales_invoice/make_delivery_note
# here from erpnext.selling.doctype.sales_order.sales_order. Third-party apps
# (e.g. ecommerce_integrations) import them from this path; re-export so those
# imports resolve without duplicating logic.
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice

__all__ = ["make_delivery_note", "make_sales_invoice"]
