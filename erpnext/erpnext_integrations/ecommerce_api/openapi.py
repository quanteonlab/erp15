from __future__ import annotations

import frappe


def _method_path(dotted_method: str) -> str:
    return f"/api/method/{dotted_method}"


def _post_op(summary: str, description: str, tags: list[str]) -> dict:
    return {
        "summary": summary,
        "description": description,
        "tags": tags,
        "responses": {
            "200": {"description": "Success"},
            "403": {"description": "Permission denied"},
            "417": {"description": "Validation error"},
            "500": {"description": "Server error"},
        },
        "security": [{"tokenAuth": []}],
    }


@frappe.whitelist(allow_guest=True)
def get_openapi_spec():
    """Return OpenAPI 3.0 spec for ERPNext ecommerce integration endpoints."""
    origin = frappe.utils.get_url()

    api_ns = "erpnext.erpnext_integrations.ecommerce_api.api"
    pm_ns = "erpnext.erpnext_integrations.ecommerce_api.product_manager"
    mig_ns = "erpnext.erpnext_integrations.ecommerce_api.standard_migration"

    paths = {
        _method_path(f"{api_ns}.get_products"): {
            "post": _post_op(
                "Get products",
                "List products with optional filters, search, pricing and stock information.",
                ["catalog"],
            )
        },
        _method_path(f"{api_ns}.get_product"): {
            "post": _post_op(
                "Get product detail",
                "Return detailed information for a single item.",
                ["catalog"],
            )
        },
        _method_path(f"{api_ns}.create_guest_preorder"): {
            "post": _post_op(
                "Create guest preorder",
                "Create draft Sales Order from public guest consultation flow.",
                ["preorders"],
            )
        },
        _method_path(f"{api_ns}.get_guest_preorders_list"): {
            "post": _post_op(
                "List guest preorders",
                "List guest-tagged preorders for staff screens.",
                ["preorders"],
            )
        },
        _method_path(f"{api_ns}.confirm_guest_preorder"): {
            "post": _post_op(
                "Confirm guest preorder",
                "Update a guest preorder to confirmed workflow state.",
                ["preorders"],
            )
        },
        _method_path(f"{api_ns}.mark_prepared_guest_preorder"): {
            "post": _post_op(
                "Mark preorder prepared",
                "Mark a guest preorder as prepared by staff.",
                ["preorders"],
            )
        },
        _method_path(f"{pm_ns}.get_pm_context"): {
            "post": _post_op(
                "Product Manager context",
                "Return default price list, available selling price lists and warehouses.",
                ["product-manager"],
            )
        },
        _method_path(f"{pm_ns}.get_product_rows"): {
            "post": _post_op(
                "Product Manager rows",
                "Paginated grid dataset for Product Manager.",
                ["product-manager"],
            )
        },
        _method_path(f"{pm_ns}.save_product_rows_bulk"): {
            "post": _post_op(
                "Save Product Manager rows",
                "Bulk-save Product Manager edited rows.",
                ["product-manager"],
            )
        },
        _method_path(f"{pm_ns}.set_active_bulk"): {
            "post": _post_op(
                "Set active in bulk",
                "Bulk activate or deactivate selected items.",
                ["product-manager"],
            )
        },
        _method_path(f"{pm_ns}.export_rows"): {
            "post": _post_op(
                "Export Product Manager CSV",
                "Export Product Manager rows as CSV text.",
                ["product-manager"],
            )
        },
        _method_path(f"{mig_ns}.import_catalog_csv"): {
            "post": _post_op(
                "Import catalog CSV",
                "Standard migration CSV import endpoint with master auto-creation.",
                ["migration"],
            )
        },
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "ERPNext Ecommerce Integration API",
            "version": "1.0.0",
            "description": "OpenAPI index for custom ERPNext ecommerce integration methods.",
        },
        "servers": [{"url": origin}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "tokenAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Use Frappe token header: Authorization: token <api_key>:<api_secret>",
                }
            }
        },
    }
