from __future__ import annotations

import base64
import csv
import io
import json
import secrets

import frappe
from frappe import _
from frappe.utils import cint, flt


# ---------------------------------------------------------------------------
# Defaults & helpers
# ---------------------------------------------------------------------------


def _default_price_list() -> str:
    """Prefer first POS Profile selling price list, then Selling Settings, then Standard Selling."""
    row = frappe.db.sql(
        """
        SELECT selling_price_list FROM `tabPOS Profile`
        WHERE selling_price_list IS NOT NULL AND selling_price_list != ''
        ORDER BY modified DESC
        LIMIT 1
        """
    )
    if row and row[0][0]:
        return row[0][0]
    ss = frappe.db.get_single_value("Selling Settings", "selling_price_list")
    if ss:
        return ss
    return "Standard Selling"


def _ean_check_digit(code12: str) -> str:
    s = sum(int(code12[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
    return str((10 - (s % 10)) % 10)


def _new_unique_barcode() -> str:
    """12-digit EAN-13 body (prefix 2 = internal store) + check digit."""
    body = "2" + "".join(str(secrets.randbelow(10)) for _ in range(11))
    return body + _ean_check_digit(body)


def _upsert_item_price(item_code: str, rate: float, price_list: str | None = None) -> None:
    pl = price_list or _default_price_list()
    existing = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": pl, "selling": 1},
        "name",
    )
    if existing:
        frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
    else:
        frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": item_code,
                "price_list": pl,
                "selling": 1,
                "price_list_rate": rate,
            }
        ).insert(ignore_permissions=True)


def _parse_filters(filters):
    if isinstance(filters, str):
        filters = json.loads(filters)
    return filters or {}


# ---------------------------------------------------------------------------
# get_pm_context — price lists, warehouses, default POS list
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_pm_context():
    default_pl = _default_price_list()
    price_lists = frappe.get_all(
        "Price List",
        filters={"selling": 1},
        pluck="name",
        order_by="name asc",
    )
    warehouses = frappe.get_all(
        "Warehouse",
        filters={"disabled": 0, "is_group": 0},
        pluck="name",
        order_by="name asc",
        limit=200,
    )
    return {
        "default_price_list": default_pl,
        "price_lists": price_lists or [],
        "warehouses": warehouses or [],
    }


# ---------------------------------------------------------------------------
# get_product_rows
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_product_rows(filters=None, page=1, page_length=100, price_list=None, warehouse=None):
    """
    Returns a joined view of Item + Item Price + Item Barcode + tags.
    price_list: selling price list; defaults to POS / Selling Settings.
    warehouse: if set, includes stock_qty from tabBin.
    """
    filters = _parse_filters(filters)
    if isinstance(price_list, str) and not price_list.strip():
        price_list = None
    price_list = price_list or _default_price_list()
    if warehouse in ("", None, "null"):
        warehouse = None
    has_unit_sku_col = frappe.db.has_column("Item", "custom_unit_sku")

    page = cint(page) or 1
    page_length = cint(page_length) or 100
    offset = (page - 1) * page_length

    conditions = []
    values: dict = {"price_list": price_list}

    search = (filters.get("search") or "").strip()
    if search:
        conditions.append(
            "("
            "i.item_name LIKE %(search)s"
            " OR i.item_code LIKE %(search)s"
            " OR COALESCE(i.custom_normalized_title,'') LIKE %(search)s"
            " OR EXISTS ("
            "  SELECT 1 FROM `tabItem Barcode` ibs"
            "  WHERE ibs.parent = i.item_code AND ibs.barcode LIKE %(search)s"
            ")"
            ")"
        )
        values["search"] = f"%{search}%"

    title_contains = (filters.get("title_contains") or "").strip()
    if title_contains:
        conditions.append("i.item_name LIKE %(title_contains)s")
        values["title_contains"] = f"%{title_contains}%"

    barcode_contains = (filters.get("barcode_contains") or "").strip()
    if barcode_contains:
        conditions.append(
            "EXISTS ("
            " SELECT 1 FROM `tabItem Barcode` ibf"
            " WHERE ibf.parent = i.item_code AND ibf.barcode LIKE %(barcode_contains)s"
            ")"
        )
        values["barcode_contains"] = f"%{barcode_contains}%"

    category = filters.get("category")
    if category:
        if isinstance(category, list) and category:
            ph = ", ".join(f"%(cat_{j})s" for j in range(len(category)))
            for j, c in enumerate(category):
                values[f"cat_{j}"] = c
            conditions.append(f"i.item_group IN ({ph})")
        elif isinstance(category, str):
            conditions.append("i.item_group = %(category)s")
            values["category"] = category

    brand = filters.get("brand")
    if brand:
        if isinstance(brand, list) and brand:
            ph = ", ".join(f"%(brand_{j})s" for j in range(len(brand)))
            for j, b in enumerate(brand):
                values[f"brand_{j}"] = b
            conditions.append(f"i.brand IN ({ph})")
        elif isinstance(brand, str):
            conditions.append("i.brand = %(brand)s")
            values["brand"] = brand

    item_codes = filters.get("item_codes")
    if item_codes and isinstance(item_codes, list):
        ph = ", ".join(f"%(ic_{j})s" for j in range(len(item_codes)))
        for j, c in enumerate(item_codes):
            values[f"ic_{j}"] = c
        conditions.append(f"i.item_code IN ({ph})")

    if filters.get("active_only"):
        conditions.append("i.disabled = 0")

    if cint(filters.get("no_image")):
        conditions.append("(i.image IS NULL OR i.image = '')")

    if cint(filters.get("has_image")):
        conditions.append("(i.image IS NOT NULL AND i.image != '')")

    if cint(filters.get("barcode_empty")):
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM `tabItem Barcode` ibx WHERE ibx.parent = i.item_code)"
        )

    if cint(filters.get("main_product_only")):
        # Main/base products: keep rows where unit SKU is empty or points to itself.
        # If unit SKU points to another product, this row is considered a derived pack.
        if has_unit_sku_col:
            conditions.append(
                "(COALESCE(NULLIF(TRIM(i.custom_unit_sku), ''), i.item_code) = i.item_code)"
            )
        else:
            # Fallback for legacy sites without custom_unit_sku.
            # Treat pack_qty == 1 (or empty) as "main" to preserve previous behavior.
            conditions.append("IFNULL(i.custom_pack_qty, 1) = 1")

    list_price_gt = filters.get("list_price_gt")
    if list_price_gt is not None and str(list_price_gt).strip() != "":
        conditions.append(
            "IFNULL(("
            " SELECT ipx.price_list_rate FROM `tabItem Price` ipx"
            " WHERE ipx.item_code = i.item_code AND ipx.price_list = %(price_list)s AND ipx.selling = 1"
            " LIMIT 1"
            "), 0) > %(list_price_gt)s"
        )
        values["list_price_gt"] = flt(list_price_gt)

    list_price_lt = filters.get("list_price_lt")
    if list_price_lt is not None and str(list_price_lt).strip() != "":
        conditions.append(
            "IFNULL(("
            " SELECT ipx.price_list_rate FROM `tabItem Price` ipx"
            " WHERE ipx.item_code = i.item_code AND ipx.price_list = %(price_list)s AND ipx.selling = 1"
            " LIMIT 1"
            "), 0) < %(list_price_lt)s"
        )
        values["list_price_lt"] = flt(list_price_lt)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    bin_join = ""
    bin_select = "NULL AS stock_qty"
    if warehouse:
        values["warehouse"] = warehouse
        bin_join = """
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock_qty
            FROM `tabBin`
            WHERE warehouse = %(warehouse)s
            GROUP BY item_code
        ) bsum ON bsum.item_code = i.item_code
        """
        bin_select = "COALESCE(bsum.stock_qty, 0) AS stock_qty"

    unit_sku_select = "COALESCE(i.custom_unit_sku, NULL) AS unit_sku" if has_unit_sku_col else "NULL AS unit_sku"

    sql = f"""
        SELECT
            i.item_code              AS client_sku,
            i.item_name              AS source_title,
            i.brand                  AS brand,
            i.item_group             AS source_category,
            i.stock_uom              AS stock_uom,
            COALESCE(i.custom_pack_qty, NULL)          AS pack_qty,
            COALESCE(i.custom_pack_size, NULL)         AS pack_size,
            COALESCE(i.custom_pack_unit, NULL)         AS unit,
            {unit_sku_select},
            i.disabled               AS _disabled,
            COALESCE(i.custom_normalized_title, NULL)  AS _raw_norm,
            COALESCE(NULLIF(TRIM(i.custom_normalized_title), ''), i.item_name) AS normalized_title,
            COALESCE(i.custom_match_confidence, NULL)  AS match_confidence,
            COALESCE(i.custom_review_notes, NULL)      AS review_notes,
            i.image                  AS image,
            i.modified               AS last_synced,
            i._user_tags             AS _user_tags,
            ip.price_list_rate       AS list_price,
            ib.barcode               AS barcode,
            {bin_select}
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, MAX(price_list_rate) AS price_list_rate
            FROM `tabItem Price`
            WHERE price_list = %(price_list)s
              AND selling = 1
            GROUP BY item_code
        ) ip ON ip.item_code = i.item_code
        LEFT JOIN (
            SELECT parent, barcode
            FROM `tabItem Barcode`
            WHERE idx = (
                SELECT MIN(idx2.idx)
                FROM `tabItem Barcode` idx2
                WHERE idx2.parent = `tabItem Barcode`.parent
            )
        ) ib ON ib.parent = i.item_code
        {bin_join}
        WHERE {where_clause}
        ORDER BY i.item_name ASC
        LIMIT %(page_length)s OFFSET %(offset)s
    """
    values["page_length"] = page_length
    values["offset"] = offset

    rows = frappe.db.sql(sql, values, as_dict=True)

    for row in rows:
        raw = (row.pop("_user_tags", None) or "").strip()
        row["tags"] = [t.strip() for t in raw.split(",") if t.strip()] if raw else []
        row["is_active"] = 0 if row.pop("_disabled", 0) else 1
        row.pop("_raw_norm", None)

    count_vals = {k: v for k, v in values.items() if k not in ("page_length", "offset")}
    total = frappe.db.sql(
        f"SELECT COUNT(*) AS c FROM `tabItem` i WHERE {where_clause}",
        count_vals,
        as_dict=True,
    )[0]["c"]

    return {"rows": rows, "total": total}


# ---------------------------------------------------------------------------
# save_product_row
# ---------------------------------------------------------------------------


def _save_product_row_impl(item_code, changes, price_list=None, commit=True):
    if isinstance(changes, str):
        changes = json.loads(changes)

    frappe.has_permission("Item", "write", throw=True)

    pl = price_list or _default_price_list()

    direct_field_map = {
        "source_title": "item_name",
        "stock_uom": "stock_uom",
        "pack_qty": "custom_pack_qty",
        "pack_size": "custom_pack_size",
        "unit": "custom_pack_unit",
        "normalized_title": "custom_normalized_title",
        "review_notes": "custom_review_notes",
        "image": "image",
        "source_category": "item_group",
    }
    if frappe.db.has_column("Item", "custom_unit_sku"):
        direct_field_map["unit_sku"] = "custom_unit_sku"

    updates: dict = {}

    for grid_field, item_field in direct_field_map.items():
        if grid_field in changes:
            updates[item_field] = changes[grid_field]

    if "is_active" in changes:
        updates["disabled"] = 0 if cint(changes["is_active"]) else 1

    if "brand" in changes:
        brand_name = (changes["brand"] or "").strip()
        if brand_name and not frappe.db.exists("Brand", brand_name):
            frappe.get_doc({"doctype": "Brand", "brand": brand_name}).insert(
                ignore_permissions=True
            )
        updates["brand"] = brand_name or None

    if updates:
        frappe.db.set_value("Item", item_code, updates)

    if "list_price" in changes:
        _upsert_item_price(item_code, flt(changes["list_price"]), pl)

    if "barcode" in changes:
        _upsert_barcode(item_code, changes["barcode"])

    if "tags" in changes:
        _sync_tags(item_code, changes["tags"])

    if commit:
        frappe.db.commit()
    modified = frappe.db.get_value("Item", item_code, "modified")
    return {"ok": True, "modified": str(modified)}


@frappe.whitelist()
def save_product_row(item_code, changes, price_list=None):
    return _save_product_row_impl(item_code, changes, price_list=price_list, commit=True)


def _upsert_barcode(item_code: str, barcode: str) -> None:
    if not barcode:
        return
    item_doc = frappe.get_doc("Item", item_code)
    if item_doc.barcodes:
        item_doc.barcodes[0].barcode = barcode
    else:
        item_doc.append("barcodes", {"barcode": barcode})
    item_doc.save(ignore_permissions=True)


def _sync_tags(item_code: str, new_tags) -> None:
    if not isinstance(new_tags, list):
        new_tags = []
    new_set = sorted(set(t.strip() for t in new_tags if t and t.strip()))
    frappe.db.set_value("Item", item_code, "_user_tags", ",".join(new_set) if new_set else None)


# ---------------------------------------------------------------------------
# save_product_rows_bulk
# ---------------------------------------------------------------------------


@frappe.whitelist()
def save_product_rows_bulk(rows, price_list=None):
    if isinstance(rows, str):
        rows = json.loads(rows)

    frappe.has_permission("Item", "write", throw=True)

    results = []
    for entry in rows:
        result = _save_product_row_impl(
            entry["item_code"],
            entry["changes"],
            price_list=price_list,
            commit=False,
        )
        results.append(result)

    frappe.db.commit()
    return results


# ---------------------------------------------------------------------------
# set_active_bulk
# ---------------------------------------------------------------------------


@frappe.whitelist()
def set_active_bulk(item_codes, is_active):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    frappe.has_permission("Item", "write", throw=True)
    disabled_val = 0 if cint(is_active) else 1

    for code in item_codes:
        frappe.db.set_value("Item", code, "disabled", disabled_val)

    frappe.db.commit()
    return {"ok": True, "updated": len(item_codes)}


# ---------------------------------------------------------------------------
# upload_item_image — base64 from canvas (200x200)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def upload_item_image(item_code, filedata, filename="image.jpg"):
    frappe.has_permission("Item", "write", throw=True)
    if not filedata:
        frappe.throw(_("No file data"))

    raw = filedata.split(",", 1)[1] if "," in filedata else filedata
    content = base64.b64decode(raw)
    fname = filename or "image.jpg"

    from frappe.utils.file_manager import save_file

    ret = save_file(fname, content, "Item", item_code, is_private=0)
    file_url = getattr(ret, "file_url", None)
    if not file_url and isinstance(ret, dict):
        file_url = ret.get("file_url")
    if not file_url and getattr(ret, "name", None):
        file_url = frappe.db.get_value("File", ret.name, "file_url")
    if not file_url:
        frappe.throw(_("Could not store image file"))
    frappe.db.set_value("Item", item_code, "image", file_url)
    frappe.db.commit()
    return {"ok": True, "image": file_url}


# ---------------------------------------------------------------------------
# generate_barcodes_bulk — only rows without existing barcode
# ---------------------------------------------------------------------------


@frappe.whitelist()
def generate_barcodes_bulk(item_codes):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    frappe.has_permission("Item", "write", throw=True)
    results = []
    for code in item_codes:
        item_doc = frappe.get_doc("Item", code)
        if item_doc.barcodes and item_doc.barcodes[0].barcode:
            results.append({"item_code": code, "skipped": True, "barcode": item_doc.barcodes[0].barcode})
            continue
        bc = _new_unique_barcode()
        while frappe.db.sql(
            "SELECT name FROM `tabItem Barcode` WHERE barcode=%s LIMIT 1", (bc,)
        ):
            bc = _new_unique_barcode()
        if item_doc.barcodes:
            item_doc.barcodes[0].barcode = bc
        else:
            item_doc.append("barcodes", {"barcode": bc, "barcode_type": "EAN"})
        item_doc.save(ignore_permissions=True)
        results.append({"item_code": code, "skipped": False, "barcode": bc})

    frappe.db.commit()
    return {"results": results}


# ---------------------------------------------------------------------------
# apply_interest_adjustment_bulk — multiply list price by (1 + percent/100)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def apply_interest_adjustment_bulk(item_codes, percent, price_list=None):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    frappe.has_permission("Item", "write", throw=True)
    pl = price_list or _default_price_list()
    factor = 1 + flt(percent) / 100.0
    updated = 0
    for code in item_codes:
        name = frappe.db.get_value(
            "Item Price",
            {"item_code": code, "price_list": pl, "selling": 1},
            "name",
        )
        if not name:
            continue
        rate = frappe.db.get_value("Item Price", name, "price_list_rate")
        if rate is None:
            continue
        frappe.db.set_value("Item Price", name, "price_list_rate", flt(rate) * factor)
        updated += 1

    frappe.db.commit()
    return {"ok": True, "updated": updated}


# ---------------------------------------------------------------------------
# delete_products_bulk
# ---------------------------------------------------------------------------


@frappe.whitelist()
def delete_products_bulk(item_codes):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    frappe.has_permission("Item", "delete", throw=True)

    deleted = []
    failed = []
    for code in item_codes:
        try:
            frappe.delete_doc("Item", code, force=True, ignore_permissions=False)
            deleted.append(code)
        except Exception as e:
            failed.append({"item_code": code, "error": str(e)})

    frappe.db.commit()
    return {"ok": True, "deleted": deleted, "failed": failed}


# ---------------------------------------------------------------------------
# get_brand_suggestions
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_brand_suggestions(query=""):
    query = (query or "").strip()
    if not query:
        # Initial load for Product Manager brand dropdowns/datalists.
        # Return all brands so users are not limited to the first alphabetic slice.
        brands = frappe.db.get_all("Brand", fields=["brand"], order_by="brand asc")
    else:
        brands = frappe.db.get_all(
            "Brand",
            filters=[["brand", "like", f"%{query}%"]],
            fields=["brand"],
            limit=100,
            order_by="brand asc",
        )
    return [b["brand"] for b in brands]


# ---------------------------------------------------------------------------
# get_category_list
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_category_list():
    groups = frappe.db.get_all(
        "Item Group",
        fields=["name"],
        order_by="name asc",
    )
    return [g["name"] for g in groups]


# ---------------------------------------------------------------------------
# export_rows
# ---------------------------------------------------------------------------


@frappe.whitelist()
def export_rows(filters=None, price_list=None, warehouse=None):
    if isinstance(filters, str):
        filters = json.loads(filters)

    data = get_product_rows(
        filters=filters or {},
        page=1,
        page_length=100000,
        price_list=price_list,
        warehouse=warehouse,
    )
    rows = data.get("rows", [])
    if not rows:
        return {"csv": ""}

    headers = [
        "client_sku",
        "source_title",
        "brand",
        "source_category",
        "barcode",
        "list_price",
        "stock_uom",
        "pack_qty",
        "pack_size",
        "unit",
        "is_active",
        "normalized_title",
        "match_confidence",
        "review_notes",
        "tags",
        "image",
        "last_synced",
        "stock_qty",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        if isinstance(row.get("tags"), list):
            row = dict(row)
            row["tags"] = "|".join(row["tags"])
        writer.writerow(row)

    return {"csv": output.getvalue()}
