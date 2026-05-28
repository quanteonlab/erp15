from __future__ import annotations

import inspect

import frappe


def _method_path(dotted_method: str) -> str:
    return f"/api/method/{dotted_method}"


def _guess_schema(param_name: str) -> dict:
    lower = param_name.lower()
    if lower.startswith(("is_", "has_")):
        return {"type": "boolean"}
    if lower in {"start", "page", "page_length", "limit", "offset", "qty", "width", "height"}:
        return {"type": "integer"}
    if lower in {"rate", "amount", "total", "price", "discount", "delta"}:
        return {"type": "number"}
    if lower.endswith("_json") or lower in {"filters", "items", "events", "rows", "changes", "images"}:
        return {"type": "object", "additionalProperties": True}
    return {"type": "string"}


def _operation_id_for(dotted_method: str) -> str:
    return dotted_method.replace(".", "_")


def _request_body_for(signature: inspect.Signature) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, param in signature.parameters.items():
        if name in {"self", "args", "kwargs"}:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        properties[name] = _guess_schema(name)
        if param.default is inspect._empty:
            required.append(name)

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return {
        "required": False,
        "content": {
            "application/json": {"schema": schema},
            "application/x-www-form-urlencoded": {"schema": schema},
        },
    }


def _query_parameters_for(signature: inspect.Signature) -> list[dict]:
    params: list[dict] = []
    for name, param in signature.parameters.items():
        if name in {"self", "args", "kwargs"}:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        params.append(
            {
                "name": name,
                "in": "query",
                "required": param.default is inspect._empty,
                "schema": _guess_schema(name),
            }
        )
    return params


def _tag_for_namespace(ns_label: str) -> str:
    return {
        "api": "ecommerce-api",
        "product_manager": "product-manager",
        "standard_migration": "migration",
    }.get(ns_label, "ecommerce")


def _summary_from_function_name(name: str) -> str:
    return name.replace("_", " ").strip().title()


def _ops_for_method(dotted_method: str, fn, tag: str) -> dict:
    signature = inspect.signature(fn)
    description = (fn.__doc__ or "").strip() or "ERPNext ecommerce integration endpoint."
    common = {
        "operationId": _operation_id_for(dotted_method),
        "summary": _summary_from_function_name(fn.__name__),
        "description": description,
        "tags": [tag],
        "responses": {
            "200": {"description": "Success"},
            "403": {"description": "Permission denied"},
            "417": {"description": "Validation error"},
            "500": {"description": "Server error"},
        },
        "security": [{"tokenAuth": []}],
    }

    post_op = dict(common)
    post_op["requestBody"] = _request_body_for(signature)

    get_op = dict(common)
    get_op["parameters"] = _query_parameters_for(signature)

    return {"get": get_op, "post": post_op}


def _module_paths(module, ns: str, ns_label: str) -> dict:
    paths: dict[str, dict] = {}
    tag = _tag_for_namespace(ns_label)

    for _, fn in inspect.getmembers(module, inspect.isfunction):
        if fn.__module__ != module.__name__:
            continue
        if fn.__name__.startswith("_"):
            continue

        dotted_method = f"{ns}.{fn.__name__}"
        paths[_method_path(dotted_method)] = _ops_for_method(dotted_method, fn, tag)

    return dict(sorted(paths.items(), key=lambda item: item[0]))


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

    from erpnext.erpnext_integrations.ecommerce_api import api as api_module
    from erpnext.erpnext_integrations.ecommerce_api import product_manager as pm_module
    from erpnext.erpnext_integrations.ecommerce_api import standard_migration as mig_module

    paths = {}
    paths.update(_module_paths(api_module, api_ns, "api"))
    paths.update(_module_paths(pm_module, pm_ns, "product_manager"))
    paths.update(_module_paths(mig_module, mig_ns, "standard_migration"))

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
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "description": "Frappe token auth header format: token <api_key>:<api_secret>",
                }
            }
        },
        "tags": [
            {"name": "ecommerce-api", "description": "Core ecommerce API methods"},
            {"name": "product-manager", "description": "Product Manager and floor-map methods"},
            {"name": "migration", "description": "Catalog and migration methods"},
        ],
    }
