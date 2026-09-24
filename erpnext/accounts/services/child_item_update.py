"""Compatibility shim for ecommerce_integrations develop imports.

Upstream expects ``erpnext.accounts.services.child_item_update.update_child_qty_rate``.
In this ERPNext tree that function lives on ``accounts_controller``.
"""

from erpnext.controllers.accounts_controller import update_child_qty_rate

__all__ = ["update_child_qty_rate"]
