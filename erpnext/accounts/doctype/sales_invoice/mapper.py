"""Compatibility shim for older/newer ecommerce_integrations imports.

Upstream ecommerce_integrations (unicommerce) historically imported
``erpnext.accounts.doctype.sales_invoice.mapper.make_sales_return``. In this
ERPNext tree that function lives on ``sales_invoice.py``. Re-export it so
Sales Order cancel hooks (status_updater → cancellation_and_returns) do not
raise ModuleNotFoundError.
"""

from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

__all__ = ["make_sales_return"]
