from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import Any

import frappe
from frappe.utils import cint, flt, now


STANDARD_COLUMNS = {
    "client_id",
    "client_sku",
    "source_title",
    "source_brand",
    "source_category",
    "barcode",
    "list_price",
    "stock_uom",
    "count_in_inventory",
    "pack_qty",
    "pack_size",
    "pack_size_unit",
    "is_active",
    "normalized_title",
    "inferred_brand",
    "inferred_pack_size",
    "inferred_pack_qty",
    "match_confidence",
    "review_notes",
}


@dataclass
class ImportSummary:
    created_items: int = 0
    updated_items: int = 0
    created_brands: int = 0
    created_item_groups: int = 0
    created_uoms: int = 0
    created_prices: int = 0
    updated_prices: int = 0
    barcode_updates: int = 0
    skipped_rows: int = 0
    failed_rows: int = 0
    variant_items_linked: int = 0
    barcode_conflicts: int = 0


@frappe.whitelist(methods=["POST"])
def import_catalog_csv(
    input_option: str = "standard_migration_file",
    file_url: str | None = None,
    file_name: str | None = None,
    csv_text: str | None = None,
    client_id: str | None = None,
    company: str | None = None,
    selling_price_list: str = "Standard Selling",
    update_existing: int = 1,
    one_time_import: int = 0,
    force: int = 0,
) -> dict[str, Any]:
    """
    Import items from a standard migration CSV.

    Input options:
    - standard_migration_file: pass file_url/file_name/csv_text for standard migration CSV.
    - standard_upload: same parser, but supports one-time guard for upload workflows.

    Notes:
    - Missing Brand / Item Group / UOM are auto-created.
    - Count in inventory is optional; blank/missing is ignored.
    """
    frappe.only_for(("System Manager", "Stock Manager"))

    normalized_option = (input_option or "").strip().lower()
    if normalized_option not in {"standard_migration_file", "standard_upload"}:
        frappe.throw("input_option must be 'standard_migration_file' or 'standard_upload'")

    rows, source_name, content_hash = _load_csv_rows(file_url=file_url, file_name=file_name, csv_text=csv_text)

    if not rows:
        frappe.throw("CSV contains no rows")

    _validate_standard_columns(rows[0].keys())

    inferred_client_id = client_id or (rows[0].get("client_id") or "").strip() or "CLIENT_UNKNOWN"

    if cint(one_time_import):
        _enforce_one_time_import(
            client_id=inferred_client_id,
            content_hash=content_hash,
            import_scope=normalized_option,
            force=cint(force),
        )

    company_name = company or frappe.defaults.get_global_default("company")
    if not company_name:
        frappe.throw("Company is required. Pass company or set a global default company")

    _ensure_price_list(selling_price_list)

    summary = ImportSummary()
    errors: list[dict[str, Any]] = []

    _ensure_variant_group_field()

    variant_map = _build_variant_map(rows)

    for idx, row in enumerate(rows, start=2):
        try:
            sku = (row.get("client_sku") or "").strip()
            result = _import_row(
                row=row,
                company=company_name,
                selling_price_list=selling_price_list,
                update_existing=cint(update_existing),
                variant_group=variant_map.get(sku),
            )
            for key, value in result.items():
                setattr(summary, key, getattr(summary, key) + value)
        except Exception as err:
            summary.failed_rows += 1
            errors.append({"row": idx, "item": row.get("client_sku"), "error": str(err)})

    if cint(one_time_import):
        _mark_import_consumed(
            client_id=inferred_client_id,
            content_hash=content_hash,
            import_scope=normalized_option,
            source_name=source_name,
        )

    return {
        "ok": True,
        "input_option": normalized_option,
        "client_id": inferred_client_id,
        "source": source_name,
        "summary": summary.__dict__,
        "errors": errors[:50],
    }


def _load_csv_rows(
    file_url: str | None,
    file_name: str | None,
    csv_text: str | None,
) -> tuple[list[dict[str, str]], str, str]:
    if csv_text:
        text = csv_text
        source_name = "inline_csv_text"
    else:
        file_doc = None
        if file_name:
            file_doc = frappe.get_doc("File", file_name)
        elif file_url:
            file_doc_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
            if not file_doc_name:
                frappe.throw(f"File not found for file_url: {file_url}")
            file_doc = frappe.get_doc("File", file_doc_name)
        else:
            frappe.throw("Provide one of: file_url, file_name, or csv_text")

        file_content = file_doc.get_content()
        if isinstance(file_content, bytes):
            text = _decode_bytes(file_content)
        else:
            text = str(file_content)
        source_name = file_doc.file_name or file_doc.name

    content_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    reader = csv.DictReader(io.StringIO(text))
    rows = [
        {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items()}
        for row in reader
        if any((v or "").strip() for v in row.values() if v is not None)
    ]
    return rows, source_name, content_hash


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _validate_standard_columns(columns: Any) -> None:
    normalized = {str(c).strip() for c in columns}
    required = {
        "client_sku",
        "source_title",
        "source_category",
        "list_price",
        "stock_uom",
    }
    missing_required = sorted(required - normalized)
    if missing_required:
        frappe.throw("Missing required columns: " + ", ".join(missing_required))

    unknown = sorted(normalized - STANDARD_COLUMNS)
    if unknown:
        # Tolerant for extra columns, but clearly reports them in logs.
        frappe.logger().info("Catalog import received extra columns: %s", ", ".join(unknown))


def _ensure_variant_group_field() -> None:
    """
    Ensure the custom_variant_group field exists on Item.
    This custom Data field stores the normalized_title group key so that all
    SKUs sharing a normalized title can be queried together as variant siblings.
    Called once per import run; safe to call repeatedly (idempotent).
    """
    fieldname = "custom_variant_group"
    if frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": fieldname}):
        return

    frappe.get_doc(
        {
            "doctype": "Custom Field",
            "dt": "Item",
            "fieldname": fieldname,
            "label": "Variant Group",
            "fieldtype": "Data",
            "insert_after": "item_code",
            "print_hide": 1,
            "search_index": 1,
            "in_standard_filter": 0,
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    # Reload Item meta so the new field is visible to the current session
    frappe.clear_cache(doctype="Item")


def _ensure_masters_for_row(row: dict) -> dict[str, int]:
    """Idempotently create Brand, Item Group, UOM for a row. Returns creation counters."""
    counts = {"created_brands": 0, "created_item_groups": 0, "created_uoms": 0}
    brand = (row.get("inferred_brand") or row.get("source_brand") or "").strip()
    item_group = (row.get("source_category") or "").strip()
    stock_uom = (row.get("stock_uom") or "Nos").strip()

    if brand and not frappe.db.exists("Brand", brand):
        frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
        counts["created_brands"] += 1

    if item_group and not frappe.db.exists("Item Group", item_group):
        frappe.get_doc(
            {
                "doctype": "Item Group",
                "item_group_name": item_group,
                "parent_item_group": "All Item Groups",
                "is_group": 0,
            }
        ).insert(ignore_permissions=True)
        counts["created_item_groups"] += 1

    if stock_uom and not frappe.db.exists("UOM", stock_uom):
        frappe.get_doc({"doctype": "UOM", "uom_name": stock_uom, "enabled": 1}).insert(ignore_permissions=True)
        counts["created_uoms"] += 1

    return counts


def _build_variant_map(rows: list[dict]) -> dict[str, str]:
    """
    Group rows by normalized_title. For any title that appears on 2+ SKUs,
    return a map of {client_sku: template_item_code}.

    Template item code = first 140 chars of normalized_title (matches Item code limit).
    Single-SKU titles are not variant groups and are NOT in the output map.
    """
    groups: dict[str, list[str]] = {}
    for row in rows:
        key = (row.get("normalized_title") or row.get("source_title") or "").strip()
        sku = (row.get("client_sku") or "").strip()
        if key and sku:
            groups.setdefault(key, []).append(sku)

    sku_to_template: dict[str, str] = {}
    for key, skus in groups.items():
        if len(skus) > 1:
            template_code = key[:140]
            for sku in skus:
                sku_to_template[sku] = template_code
    return sku_to_template


def _import_row(
    row: dict[str, str],
    company: str,
    selling_price_list: str,
    update_existing: int,
    variant_group: str | None = None,
) -> dict[str, int]:
    """Import a single row. variant_group is the normalized_title key for variant siblings."""
    counters = {
        "created_items": 0,
        "updated_items": 0,
        "created_brands": 0,
        "created_item_groups": 0,
        "created_uoms": 0,
        "created_prices": 0,
        "updated_prices": 0,
        "barcode_updates": 0,
        "skipped_rows": 0,
        "variant_items_linked": 0,
        "barcode_conflicts": 0,
    }

    item_code = (row.get("client_sku") or "").strip()
    if not item_code:
        counters["skipped_rows"] += 1
        return counters

    item_name = (row.get("source_title") or "").strip() or item_code
    brand = (row.get("inferred_brand") or row.get("source_brand") or "").strip()
    item_group = (row.get("source_category") or "All Item Groups").strip()
    stock_uom = (row.get("stock_uom") or "Nos").strip()
    is_active = cint(row.get("is_active") or 1)
    barcode = (row.get("barcode") or "").strip()
    list_price = flt(row.get("list_price") or 0)

    master_counts = _ensure_masters_for_row(row)
    counters["created_brands"] += master_counts["created_brands"]
    counters["created_item_groups"] += master_counts["created_item_groups"]
    counters["created_uoms"] += master_counts["created_uoms"]

    item_docname = frappe.db.exists("Item", item_code)
    if item_docname:
        if not update_existing:
            counters["skipped_rows"] += 1
            return counters

        item_doc = frappe.get_doc("Item", item_docname)
        item_doc.item_name = item_name
        item_doc.item_group = item_group
        item_doc.stock_uom = stock_uom
        item_doc.disabled = 0 if is_active else 1
        if brand:
            item_doc.brand = brand
        if variant_group:
            item_doc.custom_variant_group = variant_group
        item_doc.save(ignore_permissions=True)
        counters["updated_items"] += 1
        if variant_group:
            counters["variant_items_linked"] += 1
    else:
        item_doc = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": item_code,
                "item_name": item_name,
                "description": item_name,
                "item_group": item_group,
                "stock_uom": stock_uom,
                "disabled": 0 if is_active else 1,
                "is_stock_item": 1,
                "include_item_in_manufacturing": 0,
                "brand": brand or None,
                "custom_variant_group": variant_group,
            }
        )
        item_doc.insert(ignore_permissions=True)
        counters["created_items"] += 1
        if variant_group:
            counters["variant_items_linked"] += 1

    if barcode:
        try:
            counters["barcode_updates"] += _ensure_item_barcode(item_doc, barcode)
        except frappe.ValidationError as _bce:
            # Barcode already assigned to a different item — skip it, don't fail the row.
            counters["barcode_conflicts"] += 1
            frappe.logger().info(
                "Barcode conflict for SKU %s barcode %s: %s", item_code, barcode, str(_bce)
            )

    if list_price > 0:
        price_key = {
            "item_code": item_doc.name,
            "price_list": selling_price_list,
            "uom": stock_uom,
        }
        existing_price_name = frappe.db.exists("Item Price", price_key)
        if existing_price_name:
            ip = frappe.get_doc("Item Price", existing_price_name)
            ip.price_list_rate = list_price
            ip.save(ignore_permissions=True)
            counters["updated_prices"] += 1
        else:
            frappe.get_doc(
                {
                    "doctype": "Item Price",
                    **price_key,
                    "price_list_rate": list_price,
                }
            ).insert(ignore_permissions=True)
            counters["created_prices"] += 1

    # Optional inventory count field is intentionally tolerant.
    # If blank or missing, nothing is created and no error is raised.
    _ = row.get("count_in_inventory")

    frappe.db.commit()
    return counters


def _ensure_item_barcode(item_doc, barcode: str) -> int:
    if not item_doc.meta.has_field("barcodes"):
        return 0

    for row in item_doc.get("barcodes") or []:
        if (row.barcode or "").strip() == barcode:
            return 0

    item_doc.append("barcodes", {"barcode": barcode})
    item_doc.save(ignore_permissions=True)
    return 1


def _ensure_price_list(price_list: str) -> None:
    if frappe.db.exists("Price List", price_list):
        return

    frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": price_list,
            "enabled": 1,
            "selling": 1,
            "currency": frappe.defaults.get_global_default("currency") or "USD",
        }
    ).insert(ignore_permissions=True)


def _enforce_one_time_import(client_id: str, content_hash: str, import_scope: str, force: int) -> None:
    if force:
        return

    cache_key = _one_time_key(client_id, content_hash, import_scope)
    if frappe.cache().get_value(cache_key):
        frappe.throw(
            "One-time import already consumed for this file content and client. "
            "Use force=1 to run again."
        )


def _mark_import_consumed(client_id: str, content_hash: str, import_scope: str, source_name: str) -> None:
    cache_key = _one_time_key(client_id, content_hash, import_scope)
    frappe.cache().set_value(
        cache_key,
        {
            "client_id": client_id,
            "source": source_name,
            "import_scope": import_scope,
            "consumed_at": now(),
        },
    )


def _one_time_key(client_id: str, content_hash: str, import_scope: str) -> str:
    return f"erpnext:catalog-import:{import_scope}:{client_id}:{content_hash}"
