# Compatibility shim: upstream ERPNext moved add_taxes_from_tax_template here from
# erpnext.controllers.accounts_controller. Third-party apps (e.g. ecommerce_integrations)
# import it from this path; re-export so those imports resolve without duplicating logic.
from erpnext.controllers.accounts_controller import add_taxes_from_tax_template

__all__ = ["add_taxes_from_tax_template"]
