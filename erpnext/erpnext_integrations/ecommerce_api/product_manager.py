from __future__ import annotations

import base64
import csv
import io
import importlib
import json
import secrets

import frappe
from frappe import _
from frappe.utils.background_jobs import enqueue
from frappe.utils import cint, flt, get_datetime, nowtime, today


# ---------------------------------------------------------------------------
# Defaults & helpers
# ---------------------------------------------------------------------------


def _default_buying_price_list() -> str:
    """Buying Settings buying price list, else Standard Buying."""
    bs = frappe.db.get_single_value("Buying Settings", "buying_price_list")
    if bs:
        return bs
    return "Standard Buying"


def _ensure_buying_price_list() -> str:
    name = _default_buying_price_list()
    if frappe.db.exists("Price List", name):
        return name
    currency = (
        frappe.db.get_single_value("Global Defaults", "default_currency")
        or "ARS"
    )
    frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": name,
            "enabled": 1,
            "buying": 1,
            "selling": 0,
            "currency": currency,
        }
    ).insert(ignore_permissions=True)
    return name


def _upsert_item_price_buying(item_code: str, rate: float) -> None:
    from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

    rate = flt(rate)
    if rate <= 0:
        return
    pl = _ensure_buying_price_list()
    existing = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": pl, "buying": 1},
        "name",
    )
    if existing:
        old = frappe.db.get_value("Item Price", existing, "price_list_rate")
        frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
        log_field_changes("Item Price", existing, [("price_list_rate", old, rate)])
        return
    doc = frappe.get_doc(
        {
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": pl,
            "buying": 1,
            "selling": 0,
            "price_list_rate": rate,
        }
    )
    doc.insert(ignore_permissions=True)
    log_field_changes("Item Price", doc.name, [("price_list_rate", None, rate)])


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


def _reconcile_item_stock_qty(item_code: str, warehouse: str, target_qty: float) -> None:
    """Set absolute stock in *warehouse* via Stock Reconciliation (POS Product Manager)."""
    target_qty = flt(target_qty)
    if target_qty < 0:
        frappe.throw(_("Quantity cannot be negative"))

    item = frappe.get_doc("Item", item_code)
    if not item.is_stock_item:
        frappe.throw(_("Item {0} is not a stock item").format(item_code))
    if item.has_serial_no or item.has_batch_no:
        frappe.throw(
            _(
                "Quantity cannot be adjusted from Product Manager for serialized or batched item {0}"
            ).format(item_code)
        )

    company = frappe.db.get_value("Warehouse", warehouse, "company")
    if not company:
        frappe.throw(_("Could not determine company for warehouse {0}").format(warehouse))

    current = flt(
        frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
        )
        or 0
    )
    if abs(current - target_qty) < 1e-6:
        return

    expense_account = frappe.get_cached_value("Company", company, "stock_adjustment_account")
    if not expense_account:
        expense_account = frappe.db.get_value(
            "Account",
            {"account_type": "Stock Adjustment", "company": company, "disabled": 0},
            "name",
            order_by="name asc",
        )
    if not expense_account:
        frappe.throw(_("No Stock Adjustment account for company {0}").format(company))

    cost_center = frappe.get_cached_value("Company", company, "cost_center")
    if not cost_center:
        cost_center = frappe.db.get_value(
            "Cost Center",
            {"company": company, "is_group": 0, "disabled": 0},
            "name",
            order_by="name asc",
        )

    sr = frappe.new_doc("Stock Reconciliation")
    sr.purpose = "Stock Reconciliation"
    sr.company = company
    sr.posting_date = today()
    sr.posting_time = nowtime()
    sr.set_posting_time = 1
    sr.expense_account = expense_account
    if cost_center:
        sr.cost_center = cost_center
    sr.append(
        "items",
        {
            "item_code": item_code,
            "warehouse": warehouse,
            "qty": target_qty,
        },
    )
    frappe.flags.ignore_permissions = True
    try:
        sr.insert()
        sr.submit()
    finally:
        frappe.flags.ignore_permissions = False


def _upsert_item_price(item_code: str, rate: float, price_list: str | None = None) -> None:
    from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

    pl = price_list or _default_price_list()
    existing = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": pl, "selling": 1},
        "name",
    )
    if existing:
        old = frappe.db.get_value("Item Price", existing, "price_list_rate")
        frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
        log_field_changes("Item Price", existing, [("price_list_rate", old, rate)])
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": item_code,
                "price_list": pl,
                "selling": 1,
                "price_list_rate": rate,
            }
        )
        doc.insert(ignore_permissions=True)
        log_field_changes("Item Price", doc.name, [("price_list_rate", None, rate)])


def _parse_filters(filters):
    if isinstance(filters, str):
        filters = json.loads(filters)
    return filters or {}


def _item_lex_sort_sql(alias: str = "i") -> str:
    """Lowercase lexicographic key: TRIM(COALESCE(normalized, item_name)) in SQL."""
    if frappe.db.has_column("Item", "custom_normalized_title"):
        return (
            f"LOWER(TRIM(COALESCE(NULLIF(TRIM({alias}.custom_normalized_title), ''), "
            f"{alias}.item_name)))"
        )
    return f"LOWER(TRIM({alias}.item_name))"


def _anchor_lex_sort_from_item_row(item: dict) -> str:
    if not item:
        return ""
    name = (item.get("item_name") or "").strip()
    if frappe.db.has_column("Item", "custom_normalized_title"):
        nt = (item.get("custom_normalized_title") or "").strip()
        key = nt if nt else name
    else:
        key = name
    return key.lower()


# ---------------------------------------------------------------------------
# get_unit_sku_neighbors — i025 lexicographic title neighbors for Unit SKU picker
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_unit_sku_neighbors(anchor_item_code, limit=10):
    """
    Return Items immediately before and after the anchor in lexicographic order
    on LOWER(TRIM(COALESCE(normalized_title, item_name))), tie-break item_code.
    """
    frappe.has_permission("Item", "read", throw=True)

    anchor_item_code = (anchor_item_code or "").strip()
    if not anchor_item_code:
        frappe.throw(_("Item code is required"))
    if not frappe.db.exists("Item", anchor_item_code):
        frappe.throw(_("Item {0} not found").format(anchor_item_code))

    limit_n = cint(limit) or 10
    limit_n = max(4, min(limit_n, 30))
    half = limit_n // 2
    before_n = half
    after_n = limit_n - half

    fields = ["item_name"]
    if frappe.db.has_column("Item", "custom_normalized_title"):
        fields.append("custom_normalized_title")
    item = frappe.db.get_value("Item", anchor_item_code, fields, as_dict=True)
    anchor_sort = _anchor_lex_sort_from_item_row(item or {})

    lex = _item_lex_sort_sql("i")

    before = frappe.db.sql(
        f"""
        SELECT i.item_code, i.item_name, {lex} AS sort_title
        FROM `tabItem` i
        WHERE IFNULL(i.disabled, 0) = 0
          AND i.item_code != %(anchor)s
          AND (
               ({lex}) < %(asort)s
            OR (({lex}) = %(asort)s AND i.item_code < %(anchor)s)
          )
        ORDER BY {lex} DESC, i.item_code DESC
        LIMIT %(bn)s
        """,
        {"anchor": anchor_item_code, "asort": anchor_sort, "bn": before_n},
        as_dict=True,
    )

    after = frappe.db.sql(
        f"""
        SELECT i.item_code, i.item_name, {lex} AS sort_title
        FROM `tabItem` i
        WHERE IFNULL(i.disabled, 0) = 0
          AND i.item_code != %(anchor)s
          AND (
               ({lex}) > %(asort)s
            OR (({lex}) = %(asort)s AND i.item_code > %(anchor)s)
          )
        ORDER BY {lex} ASC, i.item_code ASC
        LIMIT %(an)s
        """,
        {"anchor": anchor_item_code, "asort": anchor_sort, "an": after_n},
        as_dict=True,
    )

    def row_out(r):
        return {
            "item_code": r.get("item_code"),
            "title": r.get("item_name"),
            "sort_title": (r.get("sort_title") or "")[:500],
        }

    b = [row_out(x) for x in (before or [])]
    a = [row_out(x) for x in (after or [])]

    return {
        "anchor_sort_title": anchor_sort,
        "before": b,
        "after": a,
        "total_returned": len(b) + len(a),
    }


def _default_item_group() -> str:
    """Return a leaf Item Group name for new Item creation."""
    row = frappe.db.sql(
        """
        SELECT name
        FROM `tabItem Group`
        WHERE is_group = 0
        ORDER BY lft ASC
        LIMIT 1
        """
    )
    if row and row[0][0]:
        return row[0][0]
    return "All Item Groups"


def _generate_unique_item_code(exclude_codes=None) -> str:
    """Generate a UUID-like item_code that does not collide with Item or exclude_codes.

    Format: 32-char lowercase hex (uuid4 without dashes), e.g. ``a1b2c3d4e5f6...``.
    Retries until the candidate is free in ``tabItem`` and not in the exclude set.
    """
    import uuid as _uuid

    excluded = set()
    if isinstance(exclude_codes, str):
        try:
            exclude_codes = json.loads(exclude_codes)
        except Exception:
            exclude_codes = []
    if isinstance(exclude_codes, list):
        excluded = {str(x).strip() for x in exclude_codes if str(x).strip()}

    for _ in range(64):
        candidate = _uuid.uuid4().hex
        if candidate in excluded:
            continue
        if not frappe.db.exists("Item", candidate):
            return candidate

    # Extremely unlikely fallback: longer unique token
    while True:
        candidate = f"{_uuid.uuid4().hex}{secrets.token_hex(4)}"
        if candidate in excluded:
            continue
        if not frappe.db.exists("Item", candidate):
            return candidate


# ---------------------------------------------------------------------------
# get_pm_context — price lists, warehouses, default POS list
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_pm_context(company=None):
    from erpnext.erpnext_integrations.ecommerce_api.company_context import (
        company_payload,
        company_scope,
        resolve_company,
    )

    default_pl = _default_price_list()
    payload = company_payload(company)
    default_company = payload.get("default_company") or resolve_company(company)
    scoped = company_scope(company)
    wh_filters = {"disabled": 0, "is_group": 0}
    if scoped:
        wh_filters["company"] = scoped
    price_lists = frappe.get_all(
        "Price List",
        filters={"selling": 1},
        pluck="name",
        order_by="name asc",
        ignore_permissions=True,
    )
    warehouses = frappe.get_all(
        "Warehouse",
        filters=wh_filters,
        pluck="name",
        order_by="name asc",
        limit=200,
        ignore_permissions=True,
    )
    buying_price_lists = frappe.get_all(
        "Price List",
        filters={"buying": 1},
        pluck="name",
        order_by="name asc",
        ignore_permissions=True,
    )
    pos_filters = {"disabled": 0}
    if scoped:
        pos_filters["company"] = scoped
    pos_profiles = frappe.get_all(
        "POS Profile",
        filters=pos_filters,
        pluck="name",
        order_by="name asc",
        ignore_permissions=True,
    )
    return {
        "default_price_list": default_pl,
        "default_buying_price_list": _default_buying_price_list(),
        "default_company": default_company,
        "multi_company_enabled": 1 if payload.get("enabled") else 0,
        "companies": payload.get("companies") or [],
        "price_lists": price_lists or [],
        "buying_price_lists": buying_price_lists or [],
        "warehouses": warehouses or [],
        "pos_profiles": pos_profiles or [],
    }


@frappe.whitelist()
def clone_price_list(source_price_list, new_name):
    """
    Create a new selling Price List by copying metadata and all Item Price rows
    from *source_price_list*.
    """
    source_price_list = (source_price_list or "").strip()
    new_name = (new_name or "").strip()
    if not source_price_list:
        frappe.throw(_("Source price list is required"))
    if not new_name:
        frappe.throw(_("New price list name is required"))
    if not frappe.db.exists("Price List", source_price_list):
        frappe.throw(_("Price List {0} not found").format(source_price_list))
    if frappe.db.exists("Price List", new_name):
        frappe.throw(_("Price List {0} already exists").format(new_name))

    src = frappe.get_doc("Price List", source_price_list)
    new_pl = frappe.copy_doc(src)
    new_pl.price_list_name = new_name
    new_pl.insert(ignore_permissions=False)

    # Bulk SQL copy — far faster than per-row Document.insert for large lists
    cols = [
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "idx",
        "item_code",
        "uom",
        "packing_unit",
        "item_name",
        "brand",
        "item_description",
        "price_list",
        "customer",
        "supplier",
        "batch_no",
        "buying",
        "selling",
        "currency",
        "price_list_rate",
        "valid_from",
        "lead_time_days",
        "valid_upto",
        "note",
        "reference",
    ]
    existing = {c[0] for c in frappe.db.sql("DESC `tabItem Price`")}
    cols = [c for c in cols if c in existing]
    col_sql = ", ".join(f"`{c}`" for c in cols)
    select_exprs = []
    params: list = []
    user = frappe.session.user
    for c in cols:
        if c == "name":
            select_exprs.append("CONCAT(%s, '-', REPLACE(UUID(), '-', ''))")
            params.append(new_pl.name)
        elif c == "price_list":
            select_exprs.append("%s")
            params.append(new_pl.name)
        elif c in ("creation", "modified"):
            select_exprs.append("NOW(6)")
        elif c in ("owner", "modified_by"):
            select_exprs.append("%s")
            params.append(user)
        else:
            select_exprs.append(f"`{c}`")
    select_sql = ", ".join(select_exprs)
    params.append(source_price_list)
    frappe.db.sql(
        f"""
        INSERT INTO `tabItem Price` ({col_sql})
        SELECT {select_sql}
        FROM `tabItem Price`
        WHERE price_list = %s
        """,
        tuple(params),
    )
    copied = cint(frappe.db.count("Item Price", {"price_list": new_pl.name}))
    frappe.db.commit()
    return {
        "ok": True,
        "name": new_pl.name,
        "copied_prices": copied,
        "source": source_price_list,
    }


@frappe.whitelist()
def clone_warehouse(source_warehouse, new_name):
    """
    Create a new leaf Warehouse copying company/parent/type from *source_warehouse*.
    Does not copy stock balances (use Stock Entry / Reconciliation separately).
    """
    source_warehouse = (source_warehouse or "").strip()
    new_name = (new_name or "").strip()
    if not source_warehouse:
        frappe.throw(_("Source warehouse is required"))
    if not new_name:
        frappe.throw(_("New warehouse name is required"))
    if not frappe.db.exists("Warehouse", source_warehouse):
        frappe.throw(_("Warehouse {0} not found").format(source_warehouse))

    src = frappe.get_doc("Warehouse", source_warehouse)
    abbr = frappe.get_cached_value("Company", src.company, "abbr") or ""
    suffix = f" - {abbr}" if abbr else ""
    expected = new_name if (suffix and new_name.endswith(suffix)) else f"{new_name}{suffix}"
    if frappe.db.exists("Warehouse", expected) or frappe.db.exists("Warehouse", new_name):
        frappe.throw(_("Warehouse {0} already exists").format(expected or new_name))

    # warehouse_name without company suffix (ERPNext autoname appends abbr)
    warehouse_name = new_name[: -len(suffix)] if suffix and new_name.endswith(suffix) else new_name

    doc = frappe.new_doc("Warehouse")
    doc.warehouse_name = warehouse_name
    doc.company = src.company
    doc.parent_warehouse = src.parent_warehouse
    doc.warehouse_type = src.warehouse_type
    doc.is_group = 0
    doc.disabled = 0
    if src.account:
        doc.account = src.account
    if getattr(src, "default_in_transit_warehouse", None):
        doc.default_in_transit_warehouse = src.default_in_transit_warehouse
    doc.insert(ignore_permissions=False)
    frappe.db.commit()
    return {
        "ok": True,
        "name": doc.name,
        "source": source_warehouse,
    }


# ---------------------------------------------------------------------------
# get_product_rows
# ---------------------------------------------------------------------------


def ensure_product_manager_custom_fields() -> None:
    """
    Ensure the pack/normalization/unit-sku custom fields used throughout Product Manager
    (get/save/create rows, description generation) exist on Item.

    Wired into hooks.after_migrate so every `bench migrate` (fresh site or
    existing) guarantees these fields exist, instead of relying on each
    Product Manager endpoint to check for them at request time.
    Idempotent — create_custom_fields() skips fields that already exist.
    """
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(
        {
            "Item": [
                {
                    "fieldname": "custom_normalized_title",
                    "fieldtype": "Data",
                    "label": "Normalized Title",
                    "insert_after": "item_name",
                },
                {
                    "fieldname": "custom_pack_qty",
                    "fieldtype": "Int",
                    "label": "Pack Qty",
                    "insert_after": "stock_uom",
                },
                {
                    "fieldname": "custom_pack_size",
                    "fieldtype": "Float",
                    "label": "Pack Size",
                    "insert_after": "custom_pack_qty",
                },
                {
                    "fieldname": "custom_pack_unit",
                    "fieldtype": "Data",
                    "label": "Pack Unit",
                    "insert_after": "custom_pack_size",
                },
                {
                    "fieldname": "custom_unit_sku",
                    "fieldtype": "Data",
                    "label": "Unit SKU",
                    "insert_after": "custom_pack_unit",
                    "description": "Item code of the base/unit SKU this pack or variant decomposes into.",
                },
                {
                    "fieldname": "custom_review_notes",
                    "fieldtype": "Small Text",
                    "label": "Review Notes",
                    "insert_after": "custom_unit_sku",
                },
            ]
        },
        ignore_validate=True,
    )
    frappe.clear_cache(doctype="Item")


def _ensure_unit_sku_column() -> bool:
    """Create custom_unit_sku if missing. Returns True when the column is available."""
    if frappe.db.has_column("Item", "custom_unit_sku"):
        return True
    ensure_product_manager_custom_fields()
    return bool(frappe.db.has_column("Item", "custom_unit_sku"))

def _selling_prices_map(item_codes: list) -> dict:
    """Map item_code → { price_list_name → rate } for all selling Item Prices."""
    if not item_codes:
        return {}
    # Use explicit placeholders — Frappe IN %(list)s is unreliable across MariaDB builds.
    ph = ", ".join(["%s"] * len(item_codes))
    rows = frappe.db.sql(
        f"""
        SELECT item_code, price_list, MAX(price_list_rate) AS rate
        FROM `tabItem Price`
        WHERE item_code IN ({ph})
          AND selling = 1
        GROUP BY item_code, price_list
        """,
        tuple(item_codes),
        as_dict=True,
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r.item_code, {})[r.price_list] = flt(r.rate)
    return out


@frappe.whitelist()
def get_product_rows(
    filters=None,
    page=1,
    page_length=100,
    price_list=None,
    warehouse=None,
    include_all_prices=0,
):
    """
    Returns a joined view of Item + Item Price + Item Barcode + tags.
    price_list: selling price list; defaults to POS / Selling Settings.
    warehouse: if set, includes stock_qty from tabBin.
    include_all_prices: when truthy, each row gets `prices` {list → rate} for all selling lists.
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
    buying_price_list = _default_buying_price_list()
    values: dict = {
        "price_list": price_list,
        "buying_price_list": buying_price_list,
    }

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

    if filters.get("disabled_only"):
        conditions.append("IFNULL(i.disabled, 0) = 1")

    if cint(filters.get("no_image")):
        conditions.append("(i.image IS NULL OR i.image = '')")

    if cint(filters.get("no_image_with_candidates")):
        conditions.append("(i.image IS NULL OR i.image = '')")
        conditions.append(
            "EXISTS (SELECT 1 FROM `tabProduct Image Candidate` pic"
            " WHERE pic.product_type = 'Item' AND pic.product_id = i.item_code)"
        )

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

    # Displayed cost is blank when buying rate is missing or <= 0.
    buying_rate_sql = (
        "IFNULL(("
        " SELECT ipx.price_list_rate FROM `tabItem Price` ipx"
        " WHERE ipx.item_code = i.item_code AND ipx.price_list = %(buying_price_list)s AND ipx.buying = 1"
        " LIMIT 1"
        "), 0)"
    )
    missing_cost_sql = f"({buying_rate_sql} <= 0)"

    cost_price_gt = filters.get("cost_price_gt")
    if cost_price_gt is not None and str(cost_price_gt).strip() != "":
        values["cost_price_gt"] = flt(cost_price_gt)
        conditions.append(f"{buying_rate_sql} > %(cost_price_gt)s")

    cost_price_lt = filters.get("cost_price_lt")
    if cost_price_lt is not None and str(cost_price_lt).strip() != "":
        values["cost_price_lt"] = flt(cost_price_lt)
        if flt(cost_price_lt) <= 0:
            # Cost < 0 / < -1 / < 0 includes rows with no buying cost.
            conditions.append(
                f"({missing_cost_sql} OR {buying_rate_sql} < %(cost_price_lt)s)"
            )
        else:
            conditions.append(
                f"({buying_rate_sql} > 0 AND {buying_rate_sql} < %(cost_price_lt)s)"
            )

    modified_after = filters.get("modified_after")
    if modified_after not in (None, "", "null"):
        conditions.append("i.modified >= %(modified_after)s")
        values["modified_after"] = get_datetime(modified_after)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    bin_join = ""
    bin_select = "NULL AS stock_qty, val.valuation_rate AS valuation_rate"
    val_join = """
        LEFT JOIN (
            SELECT item_code, MAX(valuation_rate) AS valuation_rate
            FROM `tabBin`
            WHERE IFNULL(valuation_rate, 0) > 0
            GROUP BY item_code
        ) val ON val.item_code = i.item_code
    """
    if warehouse:
        values["warehouse"] = warehouse
        bin_join = """
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock_qty, MAX(valuation_rate) AS valuation_rate
            FROM `tabBin`
            WHERE warehouse = %(warehouse)s
            GROUP BY item_code
        ) bsum ON bsum.item_code = i.item_code
        """
        bin_select = "COALESCE(bsum.stock_qty, 0) AS stock_qty, bsum.valuation_rate AS valuation_rate"
        val_join = ""

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
            COALESCE(i.custom_review_notes, NULL)      AS review_notes,
            i.image                  AS image,
            i.modified               AS last_synced,
            i._user_tags             AS _user_tags,
            ip.price_list_rate       AS list_price,
            bp.price_list_rate       AS buying_price,
            i.last_purchase_rate     AS last_purchase_rate,
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
            SELECT item_code, MAX(price_list_rate) AS price_list_rate
            FROM `tabItem Price`
            WHERE price_list = %(buying_price_list)s
              AND buying = 1
            GROUP BY item_code
        ) bp ON bp.item_code = i.item_code
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
        {val_join}
        WHERE {where_clause}
        ORDER BY {"i.modified DESC" if values.get("modified_after") else "i.item_name ASC"}
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
        row.pop("last_purchase_rate", None)
        row.pop("valuation_rate", None)
        buying = flt(row.pop("buying_price", None) or 0)
        # Buying/cost: only show when a buying price list rate exists (else blank).
        if buying > 0:
            row["cost_price"] = buying
            row["cost_from_buying"] = 1
        else:
            row["cost_price"] = None
            row["cost_from_buying"] = 0

    # Always attach selling prices by list so the grid can show one column per list.
    if rows:
        by_item = _selling_prices_map([r["client_sku"] for r in rows])
        for row in rows:
            row["prices"] = by_item.get(row["client_sku"], {})

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


def _save_product_row_impl(item_code, changes, price_list=None, commit=True, warehouse=None):
    if isinstance(changes, str):
        changes = json.loads(changes)

    changes = dict(changes)
    stock_qty_target = changes.pop("stock_qty", None)
    changes.pop("cost_from_buying", None)

    # Ecommerce API-key callers may not hold Item write roles; whitelist is the gate.
    frappe.flags.ignore_permissions = True
    try:
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
        if "unit_sku" in changes:
            if not _ensure_unit_sku_column():
                frappe.throw(
                    _("Item field custom_unit_sku is missing; cannot save Unit SKU. Run bench migrate.")
                )
            direct_field_map["unit_sku"] = "custom_unit_sku"
        elif frappe.db.has_column("Item", "custom_unit_sku"):
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

        if "cost_price" in changes and changes["cost_price"] not in (None, ""):
            _upsert_item_price_buying(item_code, flt(changes["cost_price"]))

        extra_prices = changes.get("extra_prices")
        if isinstance(extra_prices, dict):
            for list_name, rate in extra_prices.items():
                name = (list_name or "").strip()
                if not name or name == pl:
                    continue
                if rate in (None, ""):
                    continue
                _upsert_item_price(item_code, flt(rate), name)

        if "barcode" in changes:
            _upsert_barcode(item_code, changes["barcode"])

        if "tags" in changes:
            _sync_tags(item_code, changes["tags"])

        if stock_qty_target is not None:
            if not warehouse:
                frappe.throw(
                    _("Select a warehouse in Product Manager before saving quantity changes.")
                )
            _reconcile_item_stock_qty(item_code, warehouse, flt(stock_qty_target))

        if commit:
            frappe.db.commit()
        modified = frappe.db.get_value("Item", item_code, "modified")
        return {"ok": True, "modified": str(modified)}
    finally:
        frappe.flags.ignore_permissions = False


@frappe.whitelist()
def save_product_row(item_code, changes, price_list=None, warehouse=None):
    return _save_product_row_impl(
        item_code,
        changes,
        price_list=price_list,
        commit=True,
        warehouse=warehouse,
    )


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
def save_product_rows_bulk(rows, price_list=None, warehouse=None):
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
            warehouse=warehouse,
        )
        results.append(result)

    frappe.db.commit()
    return results


@frappe.whitelist()
def generate_item_code(exclude_codes=None):
    """Generate a new unique SKU/item_code for POS product creation flows."""
    frappe.has_permission("Item", "write", throw=True)
    item_code = _generate_unique_item_code(exclude_codes=exclude_codes)
    return {"item_code": item_code}


@frappe.whitelist()
def create_product_row(item_code=None, changes=None, price_list=None, activate=0, warehouse=None):
    """Create a new Item from POS products page and optionally activate it."""
    frappe.has_permission("Item", "write", throw=True)

    if isinstance(changes, str):
        changes = json.loads(changes)
    changes = changes or {}
    if isinstance(warehouse, str) and not warehouse.strip():
        warehouse = None

    title = (changes.get("source_title") or "").strip()
    if not title:
        frappe.throw(_("Product name is required"))

    candidate_code = (item_code or changes.get("client_sku") or "").strip()
    if not candidate_code:
        candidate_code = _generate_unique_item_code()

    if frappe.db.exists("Item", candidate_code):
        frappe.throw(_("SKU already exists: {0}").format(candidate_code))

    is_active = 1 if cint(activate) else cint(changes.get("is_active") or 0)
    item_group = (changes.get("source_category") or "").strip() or _default_item_group()
    stock_uom = (changes.get("stock_uom") or "").strip() or "Nos"

    if not frappe.db.exists("Item Group", item_group):
        item_group = _default_item_group()

    brand_name = (changes.get("brand") or "").strip()
    if brand_name and not frappe.db.exists("Brand", brand_name):
        frappe.get_doc({"doctype": "Brand", "brand": brand_name}).insert(ignore_permissions=True)

    item_doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": candidate_code,
            "item_name": title,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "disabled": 0 if is_active else 1,
            "brand": brand_name or None,
            "custom_normalized_title": (changes.get("normalized_title") or "").strip() or None,
            "custom_pack_qty": changes.get("pack_qty"),
            "custom_pack_size": changes.get("pack_size"),
            "custom_pack_unit": (changes.get("unit") or "").strip() or None,
            "custom_review_notes": (changes.get("review_notes") or "").strip() or None,
            "image": (changes.get("image") or "").strip() or None,
        }
    )

    if "unit_sku" in changes:
        if not _ensure_unit_sku_column():
            frappe.throw(
                _("Item field custom_unit_sku is missing; cannot save Unit SKU. Run bench migrate.")
            )
        unit_sku = (changes.get("unit_sku") or "").strip()
        item_doc.custom_unit_sku = unit_sku or None

    item_doc.insert(ignore_permissions=True)

    if changes.get("list_price") not in (None, ""):
        _upsert_item_price(candidate_code, flt(changes.get("list_price")), price_list)

    if changes.get("cost_price") not in (None, ""):
        _upsert_item_price_buying(candidate_code, flt(changes.get("cost_price")))

    barcode = (changes.get("barcode") or "").strip()
    if barcode:
        _upsert_barcode(candidate_code, barcode)

    if "tags" in changes:
        _sync_tags(candidate_code, changes.get("tags"))

    queued_image_search = False
    if title:
        try:
            from erpnext.image_search.api import enqueue_product_image_search

            enqueue_product_image_search("Item", candidate_code, "Low")
            queued_image_search = True
        except Exception:
            frappe.log_error(
                title="Create Product - Image Search Queue Error",
                message=f"Could not queue image search for {candidate_code}",
            )

    if warehouse and changes.get("stock_qty") is not None:
        _reconcile_item_stock_qty(candidate_code, warehouse, flt(changes["stock_qty"]))

    frappe.db.commit()

    modified = frappe.db.get_value("Item", candidate_code, "modified")
    return {
        "ok": True,
        "item_code": candidate_code,
        "modified": str(modified),
        "queued_image_search": queued_image_search,
    }


# ---------------------------------------------------------------------------
# set_active_bulk
# ---------------------------------------------------------------------------


@frappe.whitelist()
def set_active_bulk(item_codes, is_active):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    disabled_val = 0 if cint(is_active) else 1
    frappe.flags.ignore_permissions = True
    try:
        for code in item_codes:
            frappe.db.set_value("Item", code, "disabled", disabled_val)
        frappe.db.commit()
    finally:
        frappe.flags.ignore_permissions = False
    return {"ok": True, "updated": len(item_codes)}


# ---------------------------------------------------------------------------
# upload_item_image — base64; server re-encodes to 256×256 JPEG
# ---------------------------------------------------------------------------


@frappe.whitelist()
def upload_item_image(item_code, filedata, filename="image.jpg"):
    frappe.has_permission("Item", "write", throw=True)
    if not filedata:
        frappe.throw(_("No file data"))

    raw = filedata.split(",", 1)[1] if "," in filedata else filedata
    content = base64.b64decode(raw)

    from erpnext.image_search.thumb import materialize_item_thumb

    file_url = materialize_item_thumb(
        item_code, content, crop=None, commit=True, variant="temp", set_item_image=True
    )
    return {"ok": True, "image": file_url}


@frappe.whitelist()
def crop_item_image(item_code, crop, candidate_name=None, filedata=None):
    """
    Apply a relative 0–1 crop box and save a local 256×256 Item thumb.

    Prefer candidate_name (re-download remote) or filedata (upload).
    If both empty, recrop from current local Item.image (/files/ only).
    """
    frappe.has_permission("Item", "write", throw=True)
    if isinstance(crop, str):
        crop = json.loads(crop) if crop else None
    if not crop or not isinstance(crop, dict):
        frappe.throw(_("Crop box required"))

    from erpnext.image_search.thumb import (
        download_image_bytes,
        mark_candidate_downloaded,
        materialize_item_thumb,
        promote_item_image_to_final,
        read_local_file_bytes,
    )

    image_bytes = None
    selected_candidate = None

    if candidate_name:
        frappe.flags.ignore_permissions = True
        try:
            candidate = frappe.get_doc("Product Image Candidate", candidate_name)
        finally:
            frappe.flags.ignore_permissions = False
        if candidate.product_type != "Item" or candidate.product_id != item_code:
            frappe.throw(_("Candidate does not belong to this item"))
        image_bytes = download_image_bytes(candidate.image_url)
        selected_candidate = candidate_name
    elif filedata:
        raw = filedata.split(",", 1)[1] if "," in filedata else filedata
        image_bytes = base64.b64decode(raw)
    else:
        current = frappe.db.get_value("Item", item_code, "image")
        if not current:
            frappe.throw(_("No image to recrop; pick a candidate or upload a file"))
        current = str(current)
        if current.startswith("http://") or current.startswith("https://"):
            image_bytes = download_image_bytes(current)
        elif current.startswith("/"):
            image_bytes = read_local_file_bytes(current)
        else:
            frappe.throw(_("Unsupported image URL for recrop"))

    materialize_item_thumb(
        item_code,
        image_bytes,
        crop=crop,
        commit=False,
        variant="temp_crop",
        set_item_image=False,
    )
    file_url = promote_item_image_to_final(item_code, "temp_crop", commit=False)

    if selected_candidate:
        frappe.db.sql(
            """
            UPDATE `tabProduct Image Candidate`
            SET is_selected = 0
            WHERE product_type = 'Item' AND product_id = %s
            """,
            (item_code,),
        )
        frappe.db.set_value("Product Image Candidate", selected_candidate, "is_selected", 1)
        mark_candidate_downloaded(selected_candidate, file_url)

    frappe.db.commit()
    return {"ok": True, "image": file_url}


@frappe.whitelist()
def materialize_remote_item_images(limit=20, dry_run=0):
    """
    Backfill: Items whose image is still a remote http(s) URL get a local 256 thumb.
    """
    frappe.has_permission("Item", "write", throw=True)

    from erpnext.image_search.thumb import download_image_bytes, materialize_item_thumb

    limit_val = cint(limit) if limit not in (None, "", 0, "0") else 20
    if limit_val <= 0:
        limit_val = 20
    dry = cint(dry_run) == 1

    item_rows = frappe.db.sql(
        """
        SELECT name, image
        FROM `tabItem`
        WHERE IFNULL(image, '') LIKE 'http%%'
        ORDER BY modified DESC
        LIMIT %s
        """,
        (limit_val,),
        as_dict=True,
    )

    total = len(item_rows)
    converted = 0
    errors = []

    if dry:
        return {
            "ok": True,
            "dry_run": 1,
            "total": total,
            "converted": 0,
            "errors": [],
        }

    for row in item_rows:
        item_code = row.name
        try:
            image_bytes = download_image_bytes(row.image)
            materialize_item_thumb(item_code, image_bytes, crop=None, commit=True)
            converted += 1
        except Exception as exc:
            errors.append({"item_code": item_code, "error": str(exc)})

    return {
        "ok": True,
        "dry_run": 0,
        "total": total,
        "converted": converted,
        "errors": errors[:50],
    }


# ---------------------------------------------------------------------------
# image search helpers for POS products modal
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_item_image_candidates(item_code):
    """Return image-search candidates and latest job state for one Item."""
    frappe.has_permission("Item", "read", throw=True)
    from erpnext.image_search.api import get_product_images_ui

    return get_product_images_ui("Item", item_code)


@frappe.whitelist()
def enqueue_item_image_search(item_code, priority="High"):
    """Queue an image-search job for one Item."""
    frappe.has_permission("Item", "write", throw=True)
    from erpnext.image_search.api import enqueue_product_image_search

    return enqueue_product_image_search("Item", item_code, priority)


@frappe.whitelist()
def select_item_image_candidate(item_code, candidate_name, stage="temp"):
    """Set one candidate as the working image (temp file until crop Apply)."""
    frappe.has_permission("Item", "write", throw=True)
    from erpnext.image_search.api import select_primary_image

    return select_primary_image("Item", item_code, candidate_name, stage=stage)


@frappe.whitelist()
def apply_item_image_from_url(item_code, image_url, stage="temp"):
    """Download a remote/local/data URL (png/webp/jpg/…) into {sku}_temp.jpg until Apply."""
    frappe.has_permission("Item", "write", throw=True)
    url = (image_url or "").strip()
    if not url:
        frappe.throw(_("Image URL required"))

    from erpnext.image_search.thumb import (
        download_image_bytes,
        materialize_item_thumb,
        read_local_file_bytes,
        _normalize_file_url,
        unwrap_image_url,
    )

    url = unwrap_image_url(url)
    variant = "temp" if str(stage).strip().lower() != "final" else "final"

    if url.startswith("data:image/"):
        image_bytes = download_image_bytes(url)
    elif url.startswith("http://") or url.startswith("https://"):
        parsed_path = _normalize_file_url(url)
        if parsed_path.startswith("/files/") or parsed_path.startswith("/private/files/"):
            try:
                image_bytes = read_local_file_bytes(parsed_path)
            except Exception:
                image_bytes = download_image_bytes(url)
        else:
            image_bytes = download_image_bytes(url)
    elif url.startswith("/"):
        try:
            image_bytes = read_local_file_bytes(url)
        except Exception:
            from frappe.utils import get_url

            image_bytes = download_image_bytes(get_url(_normalize_file_url(url)))
    else:
        frappe.throw(_("Unsupported image URL"))

    file_url = materialize_item_thumb(
        item_code, image_bytes, crop=None, commit=False, variant=variant, set_item_image=True
    )
    frappe.db.sql(
        """
        UPDATE `tabProduct Image Candidate`
        SET is_selected = 0
        WHERE product_type = 'Item' AND product_id = %s
        """,
        (item_code,),
    )
    frappe.db.commit()
    return {"ok": True, "success": True, "image_url": file_url}


@frappe.whitelist()
def get_item_image_jobs(status_group="running", limit=100):
    """Return item image-search jobs for the POS jobs modal."""
    frappe.has_permission("Item", "read", throw=True)
    from erpnext.image_search.api import get_product_jobs_ui

    return get_product_jobs_ui("Item", status_group, limit)


@frappe.whitelist()
def get_item_image_queue_stats():
    """Return image-search queue counts for the POS jobs button."""
    frappe.has_permission("Item", "read", throw=True)
    from erpnext.image_search.api import get_queue_stats

    return get_queue_stats()


@frappe.whitelist()
def enqueue_items_without_images_and_candidates(priority="Low", limit=None):
    """Queue items that still have no image and no candidate options."""
    frappe.has_permission("Item", "write", throw=True)
    from erpnext.image_search.api import enqueue_items_without_images_and_candidates as enqueue_missing

    return enqueue_missing(priority=priority, limit=limit)


@frappe.whitelist()
def auto_apply_first_image_for_missing_items(priority="Low", limit=None, dry_run=0):
    """
    For Item rows without an image:
    - if candidates already exist, apply the first candidate image
    - if no candidates exist, enqueue image search

    Args:
        priority: Job priority for queued searches
        limit: Optional max number of items to process
        dry_run: When truthy, return counts only and do not modify data
    """
    frappe.has_permission("Item", "write", throw=True)

    from erpnext.image_search.api import enqueue_product_image_search, select_primary_image

    # Downloads are slower than URL copies; default batch 20.
    limit_val = cint(limit) if limit not in (None, "", 0, "0") else 20
    if limit_val <= 0:
        limit_val = 20
    dry = cint(dry_run) == 1

    item_rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabItem`
        WHERE IFNULL(image, '') = ''
        ORDER BY modified DESC
        LIMIT %s
        """,
        (limit_val,),
        as_dict=True,
    )

    total_missing = len(item_rows)
    with_candidates_not_selected = 0
    with_candidates_selected = 0
    without_candidates = 0

    applied_count = 0
    queued_count = 0
    already_queued_or_existing_job = 0
    errors = []

    for row in item_rows:
        item_code = row.name

        selected_candidate = frappe.db.get_value(
            "Product Image Candidate",
            {
                "product_type": "Item",
                "product_id": item_code,
                "is_selected": 1,
            },
            ["name", "rank", "image_url"],
            as_dict=True,
        )

        first_candidate = frappe.db.sql(
            """
            SELECT name, rank, image_url
            FROM `tabProduct Image Candidate`
            WHERE product_type = 'Item' AND product_id = %s
            ORDER BY rank ASC, creation ASC
            LIMIT 1
            """,
            (item_code,),
            as_dict=True,
        )

        first_candidate = first_candidate[0] if first_candidate else None

        if first_candidate:
            if selected_candidate:
                with_candidates_selected += 1
            else:
                with_candidates_not_selected += 1

            if dry:
                continue

            try:
                select_primary_image("Item", item_code, first_candidate.name, stage="final")
                applied_count += 1
            except Exception as exc:
                errors.append({"item_code": item_code, "error": str(exc)})
            continue

        without_candidates += 1
        if dry:
            continue

        try:
            queued = enqueue_product_image_search("Item", item_code, priority)
            if queued.get("success"):
                queued_count += 1
            else:
                already_queued_or_existing_job += 1
        except Exception as exc:
            errors.append({"item_code": item_code, "error": str(exc)})

    return {
        "dry_run": dry,
        "total_missing_image": total_missing,
        "with_candidates_not_selected": with_candidates_not_selected,
        "with_candidates_selected": with_candidates_selected,
        "without_candidates": without_candidates,
        "applied_count": applied_count,
        "queued_count": queued_count,
        "already_queued_or_existing_job": already_queued_or_existing_job,
        "errors": errors,
    }


@frappe.whitelist()
def clear_item_completed_image_jobs(include_failed=0, limit=5000):
    """Clear completed Item image-search jobs for the POS jobs modal."""
    frappe.has_permission("Item", "write", throw=True)
    from erpnext.image_search.api import clear_product_jobs_ui

    return clear_product_jobs_ui("Item", include_failed=include_failed, limit=limit)


@frappe.whitelist()
def clear_item_queued_image_jobs(limit=5000):
    """Clear active (Pending/Queued/In Progress/Retrying) Item image-search jobs."""
    frappe.has_permission("Item", "write", throw=True)
    from erpnext.image_search.api import clear_product_queue_ui

    return clear_product_queue_ui("Item", limit=limit)


@frappe.whitelist()
def recover_stuck_item_image_jobs(start_worker_after=1):
    """Reset orphaned Queued / stale In Progress Item jobs to Pending and start worker."""
    frappe.has_permission("Item", "write", throw=True)
    from erpnext.image_search.api import recover_stuck_product_jobs_ui

    return recover_stuck_product_jobs_ui("Item", start_worker_after=start_worker_after)


# ---------------------------------------------------------------------------
# description automation helpers for POS products modal
# ---------------------------------------------------------------------------


_DESC_MARKER = "DESC_AUTOMATION"
_DESC_STATUS_QUEUED = "Desc Queued"
_DESC_STATUS_IN_PROGRESS = "Desc In Progress"
_DESC_STATUS_COMPLETED = "Desc Completed"
_DESC_STATUS_FAILED = "Desc Failed"
_DESC_RUNNING_STATUSES = [_DESC_STATUS_QUEUED, _DESC_STATUS_IN_PROGRESS]
_DESC_DONE_STATUSES = [_DESC_STATUS_COMPLETED, _DESC_STATUS_FAILED]


def _description_source_field() -> str | None:
    if frappe.db.has_column("Item", "description"):
        return "description"
    if frappe.db.has_column("Item", "web_long_description"):
        return "web_long_description"
    return None


def _build_item_description_text(item_code: str) -> str:
    item = frappe.db.get_value(
        "Item",
        item_code,
        [
            "item_name",
            "brand",
            "item_group",
            "stock_uom",
            "custom_pack_qty",
            "custom_pack_size",
            "custom_pack_unit",
            "custom_normalized_title",
        ],
        as_dict=True,
    ) or {}

    title = (item.get("custom_normalized_title") or item.get("item_name") or item_code or "").strip()
    brand = (item.get("brand") or "").strip()
    category = (item.get("item_group") or "").strip()
    uom = (item.get("stock_uom") or "").strip()
    pack_qty = item.get("custom_pack_qty")
    pack_size = item.get("custom_pack_size")
    pack_unit = (item.get("custom_pack_unit") or "").strip()

    chunks = []
    if title:
        chunks.append(title)
    if brand:
        chunks.append(_("Brand") + f": {brand}")
    if category:
        chunks.append(_("Category") + f": {category}")

    pack_parts = []
    if pack_qty not in (None, ""):
        pack_parts.append(_("qty") + f" {pack_qty}")
    if pack_size not in (None, ""):
        unit_suffix = f" {pack_unit}" if pack_unit else ""
        pack_parts.append(_("size") + f" {pack_size}{unit_suffix}")
    if pack_parts:
        chunks.append(_("Pack") + ": " + ", ".join(pack_parts))

    if uom:
        chunks.append(_("Stock UOM") + f": {uom}")

    if not chunks:
        return item_code

    return ". ".join(chunks) + "."


def _create_description_job(item_code: str, priority: str = "Low") -> str:
    item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
    job = frappe.get_doc(
        {
            "doctype": "Product Image Search Job",
            "product_type": "Item",
            "product_id": item_code,
            "product_name": item_name,
            "search_query": f"{_DESC_MARKER}:{item_code}",
            "priority": priority,
            # Insert with a valid status first; then switch to description-only status.
            "status": "Completed",
            "images_found": 0,
            "target_count": 0,
            "created_at": frappe.utils.now(),
            "attempt_count": 0,
        }
    )
    job.insert(ignore_permissions=True)
    frappe.db.set_value(
        "Product Image Search Job",
        job.name,
        {
            "status": _DESC_STATUS_QUEUED,
            "error_message": None,
            "started_at": None,
            "completed_at": None,
            "images_found": 0,
            "attempt_count": 0,
        },
        update_modified=False,
    )
    return job.name


def _enqueue_description_jobs(priority="Low", limit=None, include_existing=0):
    source_field = _description_source_field()
    if not source_field:
        return {
            "queued_count": 0,
            "eligible_count": 0,
            "already_queued": 0,
            "job_names": [],
            "reason": "No Item description field available",
        }

    limit_val = cint(limit) if limit not in (None, "", 0, "0") else None
    limit_clause = f"LIMIT {limit_val}" if limit_val and limit_val > 0 else ""

    if cint(include_existing):
        where_clause = "1=1"
    else:
        where_clause = f"IFNULL(i.{source_field}, '') = ''"

    candidates = frappe.db.sql(
        f"""
        SELECT i.name
        FROM `tabItem` i
        WHERE {where_clause}
        ORDER BY i.modified DESC
        {limit_clause}
        """,
        as_dict=True,
    )

    job_names = []
    already_queued = 0
    for row in candidates:
        item_code = row.name
        existing = frappe.db.exists(
            "Product Image Search Job",
            {
                "product_type": "Item",
                "product_id": item_code,
                "search_query": ["like", f"{_DESC_MARKER}:%"],
                "status": ["in", _DESC_RUNNING_STATUSES],
            },
        )
        if existing:
            already_queued += 1
            continue

        job_name = _create_description_job(item_code=item_code, priority=priority)
        job_names.append(job_name)
        enqueue(
            "erpnext.erpnext_integrations.ecommerce_api.product_manager.process_item_description_job",
            queue="default",
            timeout=300,
            is_async=True,
            job_name=job_name,
        )

    frappe.db.commit()
    return {
        "queued_count": len(job_names),
        "eligible_count": len(candidates),
        "already_queued": already_queued,
        "job_names": job_names,
        "include_existing": cint(include_existing),
    }


def process_item_description_job(job_name):
    """Background worker: generate and save one Item description, then finalize job status."""
    try:
        job = frappe.get_doc("Product Image Search Job", job_name)
    except Exception:
        return

    if not (job.search_query or "").startswith(f"{_DESC_MARKER}:"):
        return

    try:
        frappe.db.set_value(
            "Product Image Search Job",
            job_name,
            {
                "status": _DESC_STATUS_IN_PROGRESS,
                "started_at": frappe.utils.now(),
                "error_message": None,
                "attempt_count": cint(job.attempt_count or 0) + 1,
            },
            update_modified=False,
        )

        item_code = job.product_id
        if not frappe.db.exists("Item", item_code):
            raise frappe.ValidationError(_("Item {0} not found").format(item_code))

        text = _build_item_description_text(item_code)
        updates = {}
        if frappe.db.has_column("Item", "description"):
            updates["description"] = text
        if frappe.db.has_column("Item", "web_long_description"):
            updates["web_long_description"] = text
        if not updates:
            raise frappe.ValidationError(_("No writable Item description field found"))

        frappe.db.set_value("Item", item_code, updates)

        frappe.db.set_value(
            "Product Image Search Job",
            job_name,
            {
                "status": _DESC_STATUS_COMPLETED,
                "completed_at": frappe.utils.now(),
                "images_found": len(text),
                "error_message": None,
            },
            update_modified=False,
        )
        frappe.db.commit()
    except Exception as exc:
        frappe.db.set_value(
            "Product Image Search Job",
            job_name,
            {
                "status": _DESC_STATUS_FAILED,
                "completed_at": frappe.utils.now(),
                "error_message": str(exc),
            },
            update_modified=False,
        )
        frappe.db.commit()


@frappe.whitelist()
def enqueue_items_without_descriptions(priority="Low", limit=None):
    """Queue Items that currently have no description text."""
    frappe.has_permission("Item", "write", throw=True)
    return _enqueue_description_jobs(priority=priority, limit=limit, include_existing=0)


@frappe.whitelist()
def enqueue_items_for_description_refresh(priority="Low", limit=None):
    """Queue Items for description regeneration regardless of current description value."""
    frappe.has_permission("Item", "write", throw=True)
    return _enqueue_description_jobs(priority=priority, limit=limit, include_existing=1)


@frappe.whitelist()
def get_item_description_jobs(status_group="running", limit=100):
    """Return Item description-automation jobs for the POS automation modal."""
    frappe.has_permission("Item", "read", throw=True)
    try:
        row_limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        row_limit = 100

    group = (status_group or "running").strip().lower()
    statuses = _DESC_DONE_STATUSES if group == "done" else _DESC_RUNNING_STATUSES
    if group != "done":
        group = "running"

    jobs = frappe.get_all(
        "Product Image Search Job",
        filters={
            "product_type": "Item",
            "search_query": ["like", f"{_DESC_MARKER}:%"],
            "status": ["in", statuses],
        },
        fields=[
            "name",
            "product_type",
            "product_id",
            "product_name",
            "status",
            "priority",
            "images_found",
            "attempt_count",
            "error_message",
            "created_at",
            "started_at",
            "completed_at",
            "modified",
        ],
        order_by="IFNULL(completed_at, modified) DESC",
        limit_page_length=row_limit,
    )
    return {"status_group": group, "jobs": jobs}


@frappe.whitelist()
def get_item_description_queue_stats():
    """Return counts for description automation statuses."""
    frappe.has_permission("Item", "read", throw=True)

    def _count(st):
        return frappe.db.count(
            "Product Image Search Job",
            {
                "product_type": "Item",
                "search_query": ["like", f"{_DESC_MARKER}:%"],
                "status": st,
            },
        )

    return {
        "pending": 0,
        "queued": _count(_DESC_STATUS_QUEUED),
        "in_progress": _count(_DESC_STATUS_IN_PROGRESS),
        "completed": _count(_DESC_STATUS_COMPLETED),
        "failed": _count(_DESC_STATUS_FAILED),
        "retrying": 0,
    }


@frappe.whitelist()
def clear_item_completed_description_jobs(include_failed=0):
    """Clear completed description-automation jobs from UI history."""
    frappe.has_permission("Item", "write", throw=True)
    statuses = [_DESC_STATUS_COMPLETED]
    if cint(include_failed):
        statuses.append(_DESC_STATUS_FAILED)

    names = frappe.get_all(
        "Product Image Search Job",
        filters={
            "product_type": "Item",
            "search_query": ["like", f"{_DESC_MARKER}:%"],
            "status": ["in", statuses],
        },
        pluck="name",
        limit_page_length=0,
    )

    if not names:
        return {
            "deleted_count": 0,
            "statuses": statuses,
            "product_type": "Item",
        }

    for name in names:
        frappe.delete_doc(
            "Product Image Search Job",
            name,
            ignore_permissions=True,
            delete_permanently=True,
        )

    frappe.db.commit()
    return {
        "deleted_count": len(names),
        "statuses": statuses,
        "product_type": "Item",
    }


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
def apply_interest_adjustment_bulk(item_codes, percent, price_list=None, price_lists=None):
    """
    Multiply selling list prices by (1 + percent/100).
    price_lists: optional list of Price List names; when set, adjusts each.
    Otherwise uses price_list or the default selling list.
    """
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)
    if isinstance(price_lists, str):
        price_lists = json.loads(price_lists)

    frappe.has_permission("Item", "write", throw=True)

    if isinstance(price_lists, list) and price_lists:
        pls = [p for p in price_lists if p]
    else:
        pls = [price_list or _default_price_list()]

    factor = 1 + flt(percent) / 100.0
    updated = 0
    for pl in pls:
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
# Share view / column-aware export
# ---------------------------------------------------------------------------

_SHARE_UI_COL_IDS = frozenset(
    {
        "sku",
        "unit_sku",
        "img",
        "title",
        "brand",
        "cat",
        "price",
        "cost",
        "norm",
        "barcode",
        "uom",
        "qty",
        "pack",
        "size",
        "unit",
        "on",
        "tags",
        "notes",
        "synced",
    }
)

_SHARE_DEFAULT_LABELS = {
    "sku": "SKU",
    "unit_sku": "Unit SKU",
    "img": "Img",
    "title": "Title",
    "brand": "Brand",
    "cat": "Category",
    "price": "Price",
    "cost": "Cost",
    "norm": "Norm. Title",
    "barcode": "Barcode",
    "uom": "UOM",
    "qty": "Qty",
    "pack": "Pack",
    "size": "Size",
    "unit": "Unit",
    "on": "On",
    "tags": "Tags",
    "notes": "Notes",
    "synced": "Synced",
}


def _is_price_list_col(col_id: str) -> bool:
    return isinstance(col_id, str) and col_id.startswith("pl:")


def _normalize_share_columns(columns) -> list[dict]:
    """Return ordered [{id, label}] for allowed UI columns only."""
    if isinstance(columns, str):
        columns = json.loads(columns)
    if not isinstance(columns, list):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for raw in columns:
        if isinstance(raw, str):
            col_id = raw.strip()
            label = ""
        elif isinstance(raw, dict):
            col_id = str(raw.get("id") or "").strip()
            label = str(raw.get("label") or "").strip()
        else:
            continue
        if not col_id or col_id == "sel" or col_id in seen:
            continue
        if col_id not in _SHARE_UI_COL_IDS and not _is_price_list_col(col_id):
            continue
        seen.add(col_id)
        if not label:
            if _is_price_list_col(col_id):
                label = col_id[3:]
            else:
                label = _SHARE_DEFAULT_LABELS.get(col_id, col_id)
        out.append({"id": col_id, "label": label})
    return out


def _cell_for_share_col(row: dict, col_id: str):
    """Project a single display cell. Never returns unrelated fields."""
    if _is_price_list_col(col_id):
        prices = row.get("prices") or {}
        if not isinstance(prices, dict):
            return None
        return prices.get(col_id[3:])

    if col_id == "sku":
        return row.get("client_sku")
    if col_id == "unit_sku":
        return row.get("unit_sku")
    if col_id == "img":
        return row.get("image")
    if col_id == "title":
        return row.get("source_title")
    if col_id == "brand":
        return row.get("brand")
    if col_id == "cat":
        return row.get("source_category")
    if col_id == "price":
        return row.get("list_price")
    if col_id == "cost":
        return row.get("cost_price")
    if col_id == "norm":
        return row.get("normalized_title")
    if col_id == "barcode":
        return row.get("barcode")
    if col_id == "uom":
        return row.get("stock_uom")
    if col_id == "qty":
        if row.get("stock_qty") is not None:
            return row.get("stock_qty")
        pq = row.get("pack_qty")
        try:
            if pq is not None and float(pq) > 0:
                return pq
        except (TypeError, ValueError):
            pass
        return 1
    if col_id == "pack":
        return row.get("pack_qty")
    if col_id == "size":
        return row.get("pack_size")
    if col_id == "unit":
        return row.get("unit")
    if col_id == "on":
        return row.get("is_active")
    if col_id == "tags":
        tags = row.get("tags")
        if isinstance(tags, list):
            return "|".join(str(t) for t in tags if t)
        return tags or ""
    if col_id == "notes":
        return row.get("review_notes")
    if col_id == "synced":
        return row.get("last_synced")
    return None


def _project_share_rows(rows: list, columns: list[dict]) -> list[dict]:
    """Return rows keyed only by allowed column ids (no backend field leakage)."""
    projected = []
    for row in rows or []:
        cell = {}
        for col in columns:
            cid = col["id"]
            cell[cid] = _cell_for_share_col(row, cid)
        projected.append(cell)
    return projected


def _new_view_sku() -> str:
    # URL-safe, unguessable token (≈ 16 bytes entropy).
    return f"PV-{secrets.token_urlsafe(16)}"


@frappe.whitelist()
def create_product_share_view(
    columns=None,
    filters=None,
    price_list=None,
    warehouse=None,
    title=None,
):
    """Persist a locked column + filter snapshot for a live share link."""
    cols = _normalize_share_columns(columns)
    if not cols:
        frappe.throw(_("Select at least one visible column to share"))

    if isinstance(filters, str):
        filters = json.loads(filters)
    if not isinstance(filters, dict):
        filters = {}

    if isinstance(price_list, str) and not price_list.strip():
        price_list = None
    if warehouse in ("", None, "null"):
        warehouse = None

    view_sku = _new_view_sku()
    # Extremely unlikely collision; retry a few times.
    for _ in range(5):
        if not frappe.db.exists("Product Share View", view_sku):
            break
        view_sku = _new_view_sku()

    acting = (frappe.get_request_header("X-ERP-Acting-User") or "").strip() or frappe.session.user
    doc = frappe.get_doc(
        {
            "doctype": "Product Share View",
            "view_sku": view_sku,
            "title": (title or "").strip() or "Shared products",
            "columns_json": json.dumps(cols),
            "filters_json": json.dumps(filters),
            "price_list": price_list or "",
            "warehouse": warehouse or "",
            "created_by_user": acting,
            "disabled": 0,
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {
        "view_sku": view_sku,
        "title": doc.title,
        "columns": cols,
    }


@frappe.whitelist(allow_guest=True)
def get_product_share_view(view_sku, page=1, page_length=5000):
    """
    Serve a live shared product view.

    Response rows are projected to the saved column allowlist only — capturing
    the network payload cannot reveal other product fields.
    """
    view_sku = (view_sku or "").strip()
    if not view_sku:
        frappe.throw(_("Missing view SKU"))

    frappe.flags.ignore_permissions = True
    try:
        if not frappe.db.exists("Product Share View", view_sku):
            frappe.throw(_("Share view not found"), frappe.DoesNotExistError)
        doc = frappe.get_doc("Product Share View", view_sku)
    finally:
        frappe.flags.ignore_permissions = False

    if cint(doc.disabled):
        frappe.throw(_("This share link is disabled"))

    try:
        columns = _normalize_share_columns(json.loads(doc.columns_json or "[]"))
    except Exception:
        columns = []
    if not columns:
        frappe.throw(_("Share view has no columns"))

    try:
        filters = json.loads(doc.filters_json or "{}")
    except Exception:
        filters = {}
    if not isinstance(filters, dict):
        filters = {}

    page = cint(page) or 1
    page_length = min(max(cint(page_length) or 5000, 1), 10000)

    data = get_product_rows(
        filters=filters,
        page=page,
        page_length=page_length,
        price_list=doc.price_list or None,
        warehouse=doc.warehouse or None,
    )
    rows = _project_share_rows(data.get("rows") or [], columns)

    return {
        "view_sku": view_sku,
        "title": doc.title or "Shared products",
        "columns": columns,
        "rows": rows,
        "total": cint(data.get("total") or 0),
        "page": page,
        "page_length": page_length,
        # Metadata only — never echo raw filters that could hint at hidden fields.
        "price_list": doc.price_list or None,
        "warehouse": doc.warehouse or None,
    }


# ---------------------------------------------------------------------------
# export_rows
# ---------------------------------------------------------------------------


@frappe.whitelist()
def export_rows(filters=None, price_list=None, warehouse=None, columns=None):
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

    share_cols = _normalize_share_columns(columns) if columns not in (None, "", "null") else []
    if share_cols:
        projected = _project_share_rows(rows, share_cols)
        headers = [c["id"] for c in share_cols]
        labels = {c["id"]: c["label"] for c in share_cols}
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writerow(labels)
        for row in projected:
            writer.writerow(row)
        return {"csv": output.getvalue()}

    headers = [
        "client_sku",
        "source_title",
        "brand",
        "source_category",
        "barcode",
        "list_price",
        "cost_price",
        "cost_from_buying",
        "stock_uom",
        "pack_qty",
        "pack_size",
        "unit",
        "is_active",
        "normalized_title",
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
        # Drop nested prices map from legacy full export (not in headers).
        if "prices" in row:
            row = dict(row)
            row.pop("prices", None)
        writer.writerow(row)

    return {"csv": output.getvalue()}


# ---------------------------------------------------------------------------
# Floor Map APIs (bench-side canonical service wrappers)
# ---------------------------------------------------------------------------


def _floor_map_api_module():
    # Source of truth is the local ecommerce_api implementation.
    # Never point this at webshop.* -- that app directory is intentionally empty.
    return importlib.import_module("erpnext.erpnext_integrations.ecommerce_api.floor_map_api")


@frappe.whitelist()
def get_floors(company=None):
    return _floor_map_api_module().get_floors(company=company)


@frappe.whitelist()
def get_floor_sections(floor_id, company=None):
    return _floor_map_api_module().get_floor_sections(floor_id)


@frappe.whitelist()
def get_section_details(section_id, company=None):
    return _floor_map_api_module().get_section_details(section_id)


@frappe.whitelist()
def save_floor_map(location_name, floor_name, sections_data=None, notes_data=None, canvas_width=1400, canvas_height=900, company=None):
    if isinstance(sections_data, list):
        sections_data = json.dumps(sections_data)
    if isinstance(notes_data, list):
        notes_data = json.dumps(notes_data)
    return _floor_map_api_module().save_floor_map(
        location_name,
        floor_name,
        sections_data or "[]",
        notes_data or "[]",
        canvas_width,
        canvas_height,
        company=company,
    )


@frappe.whitelist()
def delete_floor_map(floor_id, company=None):
    return _floor_map_api_module().delete_floor_map(floor_id)


def _coerce_floor_sections_for_export(payload):
    sections = []
    for section in payload or []:
        if not isinstance(section, dict):
            continue
        sections.append(
            {
                "id": section.get("id") or "",
                "code": section.get("code") or "",
                "name": section.get("name") or "",
                "x": cint(section.get("x") or 0),
                "y": cint(section.get("y") or 0),
                "width": cint(section.get("width") or 0),
                "height": cint(section.get("height") or 0),
                "color": section.get("color") or "#94a3b8",
                "products": section.get("products") or [],
            }
        )
    return sections


def _floor_map_svg_export(sections, width=1400, height=900):
    width = max(1, cint(width) or 1400)
    height = max(1, cint(height) or 900)

    svg = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        '<defs><pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse">',
        '<path d="M 24 0 L 0 0 0 24" fill="none" stroke="#e0e0e0" stroke-width="0.5"/>',
        '</pattern></defs>',
        f'<rect width="{width}" height="{height}" fill="url(#grid)"/>',
    ]

    sections_map = []
    for section in sections:
        x = cint(section.get("x") or 0)
        y = cint(section.get("y") or 0)
        w = max(0, cint(section.get("width") or 0))
        h = max(0, cint(section.get("height") or 0))
        color = section.get("color") or "#94a3b8"
        sid = frappe.safe_encode(section.get("id") or "").decode("utf-8", errors="ignore")
        code = frappe.safe_encode(section.get("code") or "").decode("utf-8", errors="ignore")
        name = frappe.safe_encode(section.get("name") or "").decode("utf-8", errors="ignore")

        svg.append(f'<g id="section-{sid}">')
        svg.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}20" stroke="{color}" stroke-width="2"/>'
        )
        svg.append(
            f'<text x="{x + 5}" y="{y + 15}" font-size="12" font-weight="bold" font-family="Arial" fill="#000000">{code}</text>'
        )
        svg.append(
            f'<text x="{x + 5}" y="{y + 28}" font-size="10" font-family="Arial" fill="#333333">{name}</text>'
        )
        svg.append("</g>")

        sections_map.append(
            {
                "id": section.get("id"),
                "code": section.get("code"),
                "x": x,
                "y": y,
                "width": w,
                "height": h,
            }
        )

    svg.append("</svg>")
    return "".join(svg), sections_map


@frappe.whitelist()
def export_floor_map_svg(floor_id, width=1400, height=900, company=None):
    floor = get_floor_sections(floor_id, company=company)
    sections = _coerce_floor_sections_for_export((floor or {}).get("sections") or [])
    svg, sections_map = _floor_map_svg_export(sections, width=width, height=height)
    return {
        "svg": svg,
        "sections_map": sections_map,
    }


# ---------------------------------------------------------------------------
# Tables: field history + employees (thin re-exports)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_field_history(doctype, name, limit=50, price_list=None):
    from erpnext.erpnext_integrations.ecommerce_api.table_history import (
        list_field_history as _impl,
    )

    return _impl(doctype, name, limit=limit, price_list=price_list)


@frappe.whitelist()
def revert_field_change(doctype, name, field, value):
    from erpnext.erpnext_integrations.ecommerce_api.table_history import (
        revert_field_change as _impl,
    )

    return _impl(doctype, name, field, value)


@frappe.whitelist()
def get_user_companies():
    from erpnext.erpnext_integrations.ecommerce_api.company_context import (
        get_user_companies as _impl,
    )

    return _impl()


@frappe.whitelist()
def set_user_company(company):
    from erpnext.erpnext_integrations.ecommerce_api.company_context import (
        set_user_company as _impl,
    )

    return _impl(company)


@frappe.whitelist()
def list_employees(search=None, status=None, page=1, page_length=100, company=None):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import list_employees as _impl

    return _impl(search=search, status=status, page=page, page_length=page_length, company=company)


@frappe.whitelist()
def get_employee(name):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import get_employee as _impl

    return _impl(name)


@frappe.whitelist()
def save_employee(name=None, data=None):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import save_employee as _impl

    return _impl(name=name, data=data)


@frappe.whitelist()
def create_employee_user(employee, email=None, roles=None):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        create_employee_user as _impl,
    )

    return _impl(employee, email=email, roles=roles)


@frappe.whitelist()
def reset_employee_user_password(employee):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        reset_employee_user_password as _impl,
    )

    return _impl(employee)


@frappe.whitelist()
def list_employee_groups():
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        list_employee_groups as _impl,
    )

    return _impl()


@frappe.whitelist()
def save_employee_group(name=None, employee_group_name=None, members=None, permissions=None):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        save_employee_group as _impl,
    )

    return _impl(
        name=name,
        employee_group_name=employee_group_name,
        members=members,
        permissions=permissions,
    )


@frappe.whitelist()
def delete_employee_group(name):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        delete_employee_group as _impl,
    )

    return _impl(name)


@frappe.whitelist()
def list_employee_meta():
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        list_employee_meta as _impl,
    )

    return _impl()


@frappe.whitelist()
def list_app_permissions():
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        list_app_permissions as _impl,
    )

    return _impl()


@frappe.whitelist()
def get_user_app_permissions(username=None):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        get_user_app_permissions as _impl,
    )

    return _impl(username)


@frappe.whitelist()
def save_employee_group_permissions(name, permissions=None):
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        save_employee_group_permissions as _impl,
    )

    return _impl(name, permissions=permissions)


@frappe.whitelist()
def ensure_starter_staff_groups():
    from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
        ensure_starter_staff_groups as _impl,
    )

    return _impl()


@frappe.whitelist()
def get_extra_fields_bundle(scope, row_keys=None):
    from erpnext.erpnext_integrations.ecommerce_api.extra_fields import (
        get_extra_fields_bundle as _impl,
    )

    return _impl(scope, row_keys=row_keys)


@frappe.whitelist()
def get_extra_row(scope, row_key):
    from erpnext.erpnext_integrations.ecommerce_api.extra_fields import get_extra_row as _impl

    return _impl(scope, row_key)


@frappe.whitelist()
def save_extra_row(scope, row_key, values=None):
    from erpnext.erpnext_integrations.ecommerce_api.extra_fields import save_extra_row as _impl

    return _impl(scope, row_key, values=values)


@frappe.whitelist()
def list_pos_profiles(search=None, company=None):
    from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
        list_pos_profiles as _impl,
    )

    return _impl(search=search, company=company)


@frappe.whitelist()
def get_pos_profile(name):
    from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
        get_pos_profile as _impl,
    )

    return _impl(name)


@frappe.whitelist()
def list_pos_profile_meta():
    from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
        list_pos_profile_meta as _impl,
    )

    return _impl()


@frappe.whitelist()
def save_pos_profile(name=None, data=None):
    from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
        save_pos_profile as _impl,
    )

    return _impl(name=name, data=data)


@frappe.whitelist()
def list_cash_register_sessions(warehouse=None, search=None, page=1, page_length=200, company=None):
    from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
        list_cash_register_sessions as _impl,
    )

    return _impl(
        warehouse=warehouse, search=search, page=page, page_length=page_length, company=company
    )


@frappe.whitelist()
def get_cash_register_session(session_id):
    from erpnext.erpnext_integrations.ecommerce_api.cash_register_api import (
        get_cash_register_session as _impl,
    )

    return _impl(session_id)


@frappe.whitelist()
def get_pos_admin_settings():
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        get_pos_admin_settings as _impl,
    )

    return _impl()


@frappe.whitelist()
def save_pos_admin_settings(pin=None, amendment_note_required=None, clear_pin=0, session_mode=None, start_requires_pin=None, default_pos_profile=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        save_pos_admin_settings as _impl,
    )

    return _impl(
        pin=pin,
        amendment_note_required=amendment_note_required,
        clear_pin=clear_pin,
        session_mode=session_mode,
        start_requires_pin=start_requires_pin,
        default_pos_profile=default_pos_profile,
    )


@frappe.whitelist()
def validate_admin_pin(pin=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        validate_admin_pin as _impl,
    )

    return _impl(pin=pin)


@frappe.whitelist()
def list_pos_cash_sessions(pos_profile=None, status=None, page=1, page_length=50):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        list_pos_cash_sessions as _impl,
    )

    return _impl(pos_profile=pos_profile, status=status, page=page, page_length=page_length)


@frappe.whitelist()
def get_pos_cash_session(session_id=None, pos_profile=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        get_pos_cash_session as _impl,
    )

    return _impl(session_id=session_id, pos_profile=pos_profile)


@frappe.whitelist()
def start_pos_cash_session(pos_profile, cashier_user=None, opening_cash=0, pin=None, close_existing=0):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        start_pos_cash_session as _impl,
    )

    return _impl(
        pos_profile=pos_profile,
        cashier_user=cashier_user,
        opening_cash=opening_cash,
        pin=pin,
        close_existing=close_existing,
    )


@frappe.whitelist()
def ensure_pos_cash_session(pos_profile=None, cashier_user=None, pin=None, opening_cash=0):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        ensure_pos_cash_session as _impl,
    )

    return _impl(
        pos_profile=pos_profile,
        cashier_user=cashier_user,
        pin=pin,
        opening_cash=opening_cash,
    )


@frappe.whitelist()
def close_pos_cash_session(session_id, pin=None, note=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        close_pos_cash_session as _impl,
    )

    return _impl(session_id=session_id, pin=pin, note=note)


@frappe.whitelist()
def update_pos_cash_session(session_id, pin=None, cashier_user=None, opening_cash=None, note=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        update_pos_cash_session as _impl,
    )

    return _impl(
        session_id=session_id,
        pin=pin,
        cashier_user=cashier_user,
        opening_cash=opening_cash,
        note=note,
    )


@frappe.whitelist()
def preview_pos_sale_amendment(invoice_name, action="cancel", items=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        preview_pos_sale_amendment as _impl,
    )

    return _impl(invoice_name=invoice_name, action=action, items=items)


@frappe.whitelist()
def amend_pos_sale(invoice_name, action="cancel", pin=None, note=None, items=None, session_id=None):
    from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
        amend_pos_sale as _impl,
    )

    return _impl(
        invoice_name=invoice_name,
        action=action,
        pin=pin,
        note=note,
        items=items,
        session_id=session_id,
    )


@frappe.whitelist(allow_guest=True)
def get_accounting_constants(as_of_date=None):
    from erpnext.erpnext_integrations.ecommerce_api.accounting_sheet_api import (
        get_accounting_constants as _impl,
    )

    return _impl(as_of_date=as_of_date)


@frappe.whitelist(allow_guest=True)
def get_accounting_sheet(scope=None):
    from erpnext.erpnext_integrations.ecommerce_api.accounting_sheet_api import (
        get_accounting_sheet as _impl,
    )

    return _impl(scope=scope)


@frappe.whitelist(allow_guest=True)
def save_accounting_sheet(
    scope=None,
    sheets=None,
    active_sheet_id=None,
    variables=None,
    notes=None,
    code=None,
    conditional_formats=None,
    notebooks=None,
    active_notebook_id=None,
    reports=None,
    tables=None,
):
    from erpnext.erpnext_integrations.ecommerce_api.accounting_sheet_api import (
        save_accounting_sheet as _impl,
    )

    return _impl(
        scope=scope,
        sheets=sheets,
        active_sheet_id=active_sheet_id,
        variables=variables,
        notes=notes,
        code=code,
        conditional_formats=conditional_formats,
        notebooks=notebooks,
        active_notebook_id=active_notebook_id,
        reports=reports,
        tables=tables,
    )


@frappe.whitelist(allow_guest=True)
def run_accounting_snippets(scope=None, namespace=None, background=None):
    from erpnext.erpnext_integrations.ecommerce_api.accounting_sheet_api import (
        run_accounting_snippets as _impl,
    )

    return _impl(scope=scope, namespace=namespace, background=background)


@frappe.whitelist(allow_guest=True)
def get_accounting_snippet_outputs(scope=None):
    from erpnext.erpnext_integrations.ecommerce_api.accounting_sheet_api import (
        get_accounting_snippet_outputs as _impl,
    )

    return _impl(scope=scope)


@frappe.whitelist()
def add_extra_column(scope, label, fieldtype="string"):
    from erpnext.erpnext_integrations.ecommerce_api.extra_fields import add_extra_column as _impl

    return _impl(scope, label, fieldtype=fieldtype)


@frappe.whitelist()
def update_extra_column(scope, column_id, label=None, fieldtype=None, hidden=None):
    from erpnext.erpnext_integrations.ecommerce_api.extra_fields import (
        update_extra_column as _impl,
    )

    return _impl(scope, column_id, label=label, fieldtype=fieldtype, hidden=hidden)


@frappe.whitelist()
def remove_extra_column(scope, column_id, scrub_values=1):
    from erpnext.erpnext_integrations.ecommerce_api.extra_fields import (
        remove_extra_column as _impl,
    )

    return _impl(scope, column_id, scrub_values=scrub_values)


@frappe.whitelist()
def get_screen_notes(scope):
    from erpnext.erpnext_integrations.ecommerce_api.screen_notes import get_screen_notes as _impl

    return _impl(scope)


@frappe.whitelist()
def add_note_block(scope, block_type="note"):
    from erpnext.erpnext_integrations.ecommerce_api.screen_notes import add_note_block as _impl

    return _impl(scope, block_type=block_type)


@frappe.whitelist()
def lock_note_block(scope, block_id):
    from erpnext.erpnext_integrations.ecommerce_api.screen_notes import lock_note_block as _impl

    return _impl(scope, block_id)


@frappe.whitelist()
def unlock_note_block(scope, block_id):
    from erpnext.erpnext_integrations.ecommerce_api.screen_notes import unlock_note_block as _impl

    return _impl(scope, block_id)


@frappe.whitelist()
def update_note_block(scope, block_id, patch=None):
    from erpnext.erpnext_integrations.ecommerce_api.screen_notes import update_note_block as _impl

    return _impl(scope, block_id, patch=patch)


@frappe.whitelist()
def remove_note_block(scope, block_id):
    from erpnext.erpnext_integrations.ecommerce_api.screen_notes import remove_note_block as _impl

    return _impl(scope, block_id)


@frappe.whitelist()
def get_shop_ui_settings():
    from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
        get_shop_ui_settings as _impl,
    )

    return _impl()


@frappe.whitelist()
def save_shop_ui_settings(settings=None):
    from erpnext.erpnext_integrations.ecommerce_api.shop_ui_settings import (
        save_shop_ui_settings as _impl,
    )

    return _impl(settings=settings)


@frappe.whitelist()
def export_floor_map_png(floor_id, width=1400, height=900, company=None):
    try:
        from PIL import Image, ImageDraw
    except Exception:
        frappe.throw(_("Pillow is required for floor map PNG export"))

    floor = get_floor_sections(floor_id, company=company)
    sections = _coerce_floor_sections_for_export((floor or {}).get("sections") or [])

    width = max(1, cint(width) or 1400)
    height = max(1, cint(height) or 900)

    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)

    grid_size = 24
    for x in range(0, width + 1, grid_size):
        draw.line([(x, 0), (x, height)], fill="#e0e0e0", width=1)
    for y in range(0, height + 1, grid_size):
        draw.line([(0, y), (width, y)], fill="#e0e0e0", width=1)

    for section in sections:
        x = cint(section.get("x") or 0)
        y = cint(section.get("y") or 0)
        w = max(0, cint(section.get("width") or 0))
        h = max(0, cint(section.get("height") or 0))
        color = section.get("color") or "#94a3b8"
        code = str(section.get("code") or "")
        name = str(section.get("name") or "")

        draw.rectangle([(x, y), (x + w, y + h)], outline=color, width=2)
        draw.text((x + 5, y + 3), code, fill="#000000")
        draw.text((x + 5, y + 18), name, fill="#333333")

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return {
        "png_base64": png_b64,
        "filename": f"floor-map-{floor_id}.png",
        "content_type": "image/png",
    }
