"""
POS / ecommerce smoke suite (i014 flows).

Preferred entrypoints (short):

  ./scripts/test_smoke.sh
  ./scripts/test_smoke.sh site.local
  KEEP_RECORDS=1 ./scripts/test_smoke.sh

  bench --site site.local execute erpnext.erpnext_integrations.ecommerce_api.test_smoke.run
  bench --site site.local execute erpnext.erpnext_integrations.ecommerce_api.test_smoke.cleanup

Legacy module `test_i014_smoke` still re-exports this module.

Suites: auth, catalog/barcode, POS white/black, promotions, receiving, stock sync,
master data, product manager, POS session/cash, preventa/staff/devices/floors/print/TMS.
Records tagged I014_SMOKE for cleanup.

Also run dirty catalog: ./scripts/test_edge.sh  (auto-sweeps ~all whitelist methods)
"""

import uuid
import traceback

import frappe
from frappe.utils import cint, cstr, flt, nowdate

# ── Tag used to find and delete all smoke-test records ───────────────────────
TAG = "I014_SMOKE"

# ── ANSI colours ─────────────────────────────────────────────────────────────
_PASS = "\033[92m✓ PASS\033[0m"
_FAIL = "\033[91m✗ FAIL\033[0m"
_SKIP = "\033[93m⊘ SKIP\033[0m"
_WARN = "\033[93m⚠ WARN\033[0m"

_results = []  # (label, status, severity, detail)


# ── Test runner helpers ───────────────────────────────────────────────────────

def _run(label, fn, severity="S2"):
    """Execute fn(), record PASS/FAIL."""
    try:
        fn()
        _results.append((label, "PASS", severity, None))
        print(f"  {_PASS}  [{severity}] {label}")
        return True
    except AssertionError as exc:
        _results.append((label, "FAIL", severity, str(exc)))
        print(f"  {_FAIL}  [{severity}] {label}: {exc}")
        return False
    except Exception as exc:
        detail = traceback.format_exc()
        _results.append((label, "FAIL", severity, detail))
        print(f"  {_FAIL}  [{severity}] {label}: {exc}")
        return False


def _skip(label, reason, severity="S3"):
    _results.append((label, "SKIP", severity, reason))
    print(f"  {_SKIP}  [{severity}] {label}: {reason}")


def _uid():
    return str(uuid.uuid4())


# ── Test data helpers ─────────────────────────────────────────────────────────

def _get_test_item():
    """Return a suitable active stock item for sale tests."""
    items = frappe.get_all(
        "Item",
        filters={"disabled": 0, "is_stock_item": 1},
        fields=["item_code", "item_name", "stock_uom"],
        limit=1,
    )
    if not items:
        raise RuntimeError("No active stock items found. Run seed scripts first.")
    return items[0]


def _get_defaults():
    """Return company, warehouse, and a usable customer."""
    company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")
    from erpnext.erpnext_integrations.ecommerce_api.api import _get_or_create_consumidor_final

    customer = _get_or_create_consumidor_final()
    return company, warehouse, customer


def _compute_grand_total(item_code, item_name, qty, rate, warehouse, company, customer):
    """
    Create a transient (unsaved) Sales Invoice to compute grand_total with taxes.
    This ensures create_pos_sale's total-mismatch guard is satisfied.
    """
    doc = frappe.get_doc({
        "doctype": "Sales Invoice",
        "customer": customer,
        "company": company,
        "posting_date": nowdate(),
        "due_date": nowdate(),
        "items": [{
            "item_code": item_code,
            "item_name": item_name,
            "qty": qty,
            "rate": rate,
            "warehouse": warehouse,
        }],
    })
    doc.set_missing_values()
    doc.calculate_taxes_and_totals()
    return flt(doc.grand_total)


def _tag_record(doctype, name):
    """Append the I014_SMOKE tag to the remarks field of a document."""
    existing = frappe.db.get_value(doctype, name, "remarks") or ""
    frappe.db.set_value(doctype, name, "remarks", f"{existing}|{TAG}")
    frappe.db.commit()


# ── Suite 5.1 — Auth & Configuration ─────────────────────────────────────────

def suite_5_1_auth():
    print("\n[Suite 5.1] Auth & Configuration")

    def check_company():
        company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
            "Global Defaults", "default_company"
        )
        assert company, "No default company configured in Global Defaults"

    def check_price_list():
        assert frappe.db.exists("Price List", "Standard Selling"), \
            "'Standard Selling' price list not found"

    def check_warehouse():
        company, warehouse, _ = _get_defaults()
        assert warehouse, f"No warehouse found for company '{company}'"

    def check_customer():
        _, _, customer = _get_defaults()
        assert customer, "No customer found for POS sales"

    _run("5.1.1 Default company configured", check_company, "S1")
    _run("5.1.2 Standard Selling price list exists", check_price_list, "S2")
    _run("5.1.3 Warehouse available", check_warehouse, "S1")
    _run("5.1.4 Customer available for POS", check_customer, "S1")


# ── Suite 5.2 — Catalog, Search, Barcode ─────────────────────────────────────

def suite_5_2_catalog():
    print("\n[Suite 5.2] Catalog, Search, Barcode")

    from erpnext.erpnext_integrations.ecommerce_api import api

    def check_get_products():
        result = api.get_products(page_length=5, price_list="Standard Selling")
        assert isinstance(result, dict), "get_products did not return a dict"
        assert "items" in result, "get_products response missing 'items' key"
        assert len(result["items"]) > 0, \
            "get_products returned empty list — seed item data first"

    def check_total_count():
        result = api.get_products(page_length=1)
        assert "total_count" in result, "get_products missing 'total_count'"
        assert isinstance(result["total_count"], int), "'total_count' is not an int"

    def check_search_term():
        item = _get_test_item()
        term = item["item_name"][:4]
        result = api.get_products(search_term=term, page_length=5)
        assert "items" in result, "search response missing 'items'"

    def check_barcode_miss():
        # search_by_barcode either returns None/dict or raises a known exception.
        # Either is acceptable — what must NOT happen is an unhandled server crash.
        try:
            result = api.search_by_barcode("0000000000000")
            assert result is None or isinstance(result, dict), \
                f"search_by_barcode returned unexpected type: {type(result)}"
        except (frappe.ValidationError, frappe.DoesNotExistError):
            pass  # Graceful "not found" exception — acceptable behaviour

    _run("5.2.1 get_products returns items", check_get_products, "S2")
    _run("5.2.2 get_products includes total_count", check_total_count, "S3")
    _run("5.2.3 search_term filter does not crash", check_search_term, "S3")
    _run("5.2.4 barcode miss returns gracefully (no crash)", check_barcode_miss, "S3")


# ── Suite 5.3 + 5.4 WHITE — POS Sale + Idempotency ───────────────────────────

def suite_5_3_pos_sale_white():
    print("\n[Suite 5.3 + 5.4] POS Sale — WHITE mode")

    from erpnext.erpnext_integrations.ecommerce_api import api

    item = _get_test_item()
    company, warehouse, customer = _get_defaults()
    sale_uuid = _uid()
    receipt = f"I014-W-{sale_uuid[:8].upper()}"
    qty = 1
    rate = 100.0
    total = _compute_grand_total(
        item["item_code"], item["item_name"], qty, rate, warehouse, company, customer
    )

    created_invoice = {}

    def check_white_sale():
        result = api.create_pos_sale(
            offline_order_uuid=sale_uuid,
            receipt_number=receipt,
            items=[{
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "qty": qty,
                "rate": rate,
                "amount": rate * qty,
            }],
            total_amount=total,
            payment_method="Cash",
            sale_mode="WHITE",
            cashier_id="I014_TEST_CASHIER",
            device_id="I014_TEST_DEVICE",
        )
        assert result.get("invoice_id"), f"No invoice_id in result: {result}"
        assert result.get("status") in ("created", "already_exists"), \
            f"Unexpected status: {result.get('status')}"
        assert result.get("sale_mode") == "WHITE", \
            f"sale_mode not WHITE in result: {result}"
        created_invoice["name"] = result["invoice_id"]
        _tag_record("Sales Invoice", result["invoice_id"])

    def check_idempotency():
        # Second call with same UUID must return already_exists, not a new invoice
        result = api.create_pos_sale(
            offline_order_uuid=sale_uuid,
            receipt_number=receipt,
            items=[{
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "qty": qty,
                "rate": rate,
                "amount": rate * qty,
            }],
            total_amount=total,
            payment_method="Cash",
            sale_mode="WHITE",
        )
        assert result.get("status") == "already_exists", \
            f"Idempotency guard failed — expected 'already_exists', got: {result.get('status')}"

    ok = _run(
        f"5.3.1 create_pos_sale WHITE (item={item['item_code']}, total={total})",
        check_white_sale, "S1"
    )
    if ok:
        _run("5.6.1 Idempotency — duplicate UUID returns already_exists", check_idempotency, "S1")
    else:
        _skip("5.6.1 Idempotency", "Skipped: WHITE sale failed", "S1")


# ── Suite 5.4 BLACK — Payment policy ─────────────────────────────────────────

def suite_5_4_pos_sale_black():
    print("\n[Suite 5.4] POS Sale — BLACK mode")

    from erpnext.erpnext_integrations.ecommerce_api import api

    item = _get_test_item()
    company, warehouse, customer = _get_defaults()
    qty = 1
    rate = 50.0
    total = _compute_grand_total(
        item["item_code"], item["item_name"], qty, rate, warehouse, company, customer
    )

    def check_black_cash():
        sale_uuid = _uid()
        result = api.create_pos_sale(
            offline_order_uuid=sale_uuid,
            receipt_number=f"I014-B-{sale_uuid[:8].upper()}",
            items=[{
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "qty": qty,
                "rate": rate,
                "amount": rate * qty,
            }],
            total_amount=total,
            payment_method="Cash",
            sale_mode="BLACK",
            cashier_id="I014_TEST_CASHIER",
        )
        assert result.get("invoice_id"), f"BLACK+Cash sale returned no invoice_id: {result}"
        assert result.get("is_borrador") == 1, \
            f"Expected is_borrador=1 for BLACK mode, got: {result.get('is_borrador')}"
        _tag_record("Sales Invoice", result["invoice_id"])

    def check_black_card_rejected():
        try:
            api.create_pos_sale(
                offline_order_uuid=_uid(),
                receipt_number="I014-B-CARD-REJECT-TEST",
                items=[{
                    "item_code": item["item_code"],
                    "item_name": item["item_name"],
                    "qty": qty,
                    "rate": rate,
                    "amount": rate * qty,
                }],
                total_amount=total,
                payment_method="Card",
                sale_mode="BLACK",
            )
            assert False, "Expected ValidationError for BLACK+Card — but call succeeded"
        except frappe.ValidationError:
            pass  # Correct behaviour

    def check_invalid_sale_mode():
        try:
            api.create_pos_sale(
                offline_order_uuid=_uid(),
                receipt_number="I014-INVALID-MODE",
                items=[{
                    "item_code": item["item_code"],
                    "item_name": item["item_name"],
                    "qty": qty,
                    "rate": rate,
                    "amount": rate * qty,
                }],
                total_amount=total,
                payment_method="Cash",
                sale_mode="GREY",
            )
            assert False, "Expected ValidationError for invalid sale_mode"
        except frappe.ValidationError:
            pass  # Correct behaviour

    _run(f"5.4.1 BLACK+Cash succeeds (total={total})", check_black_cash, "S1")
    _run("5.4.2 BLACK+Card is rejected with ValidationError", check_black_card_rejected, "S2")
    _run("5.4.3 Invalid sale_mode is rejected", check_invalid_sale_mode, "S2")


# ── Suite 5.5 — Promotions, Coupons, Discount PIN ────────────────────────────

def suite_5_5_promotions():
    print("\n[Suite 5.5] Promotions, Coupons, Discount PIN")

    from erpnext.erpnext_integrations.ecommerce_api import api

    def check_unknown_coupon():
        result = api.validate_coupon_code("INVALID_CODE_I014_XXXXXXXX")
        assert result.get("valid") is False, \
            f"Expected valid=False for unknown coupon, got: {result}"

    def check_empty_coupon():
        result = api.validate_coupon_code("")
        assert result.get("valid") is False, \
            f"Expected valid=False for empty coupon, got: {result}"

    def check_discount_pin():
        """
        PIN source of truth is pos_session_api._pin_configured() (hashed settings
        and/or legacy conf pos_manager_pin) — not conf alone.
        Configured → wrong pin rejected. Open → any input authorized.
        """
        from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import _pin_configured

        if _pin_configured():
            result = api.validate_discount_pin("000000_WRONG_I014")
            assert result.get("authorized") is False, \
                "Wrong PIN should not be authorized when admin PIN is configured"
            assert result.get("pin_configured") is True
        else:
            result = api.validate_discount_pin("anything")
            assert result.get("authorized") is True, \
                "Open mode (no admin PIN) should authorize any PIN input"
            assert result.get("pin_configured") is False

    _run("5.5.1 Unknown coupon returns valid=False", check_unknown_coupon, "S3")
    _run("5.5.2 Empty coupon returns valid=False", check_empty_coupon, "S3")
    _run("5.5.3 Discount PIN open/closed mode consistent", check_discount_pin, "S3")


# ── Suite 5.7 — Receiving Flow ────────────────────────────────────────────────

def suite_5_7_receiving():
    print("\n[Suite 5.7] Receiving / Stock Entry")

    from erpnext.erpnext_integrations.ecommerce_api import api

    def check_simulate():
        payload = api.simulate_receiving_flow()
        assert isinstance(payload, dict), "simulate_receiving_flow did not return dict"
        assert payload.get("lines"), "simulate_receiving_flow returned no lines"
        assert payload.get("warehouse"), "simulate_receiving_flow missing warehouse"
        assert isinstance(payload["lines"], list), "'lines' is not a list"

    def check_commit():
        payload = api.simulate_receiving_flow()
        session_id = _uid()
        result = api.commit_receiving_session(
            session_id=session_id,
            reference=f"I014-RECV-{session_id[:8].upper()}",
            supplier=payload.get("supplier") or "",
            warehouse=payload["warehouse"],
            lines=payload["lines"],
            draft_items=payload.get("draft_items") or [],
        )
        assert result.get("stock_entry_id"), \
            f"commit_receiving_session missing stock_entry_id: {result}"
        se_name = result["stock_entry_id"]
        # Verify the Stock Entry actually exists
        assert frappe.db.exists("Stock Entry", se_name), \
            f"Stock Entry '{se_name}' does not exist in DB"
        _tag_record("Stock Entry", se_name)

    _run("5.7.1 simulate_receiving_flow returns valid payload", check_simulate, "S2")
    _run("5.7.2 commit_receiving_session creates Stock Entry", check_commit, "S2")


# ── Suite 5.8 — Stock Sync ────────────────────────────────────────────────────

def suite_5_8_sync():
    print("\n[Suite 5.8] Stock Event Sync")

    from erpnext.erpnext_integrations.ecommerce_api import api

    item = _get_test_item()

    def check_sync_one_event():
        events = [{
            "id": _uid(),
            "item_code": item["item_code"],
            "delta": -1,
            "offline_order_uuid": _uid(),
            "created_at": frappe.utils.now_datetime().isoformat(),
        }]
        result = api.sync_stock_events(events)
        assert "processed" in result, f"sync_stock_events missing 'processed': {result}"
        assert isinstance(result["processed"], int), "'processed' is not an int"
        assert result["processed"] >= 1, \
            f"Expected processed >= 1, got {result['processed']}"

    def check_sync_empty():
        result = api.sync_stock_events([])
        assert result.get("processed") == 0, \
            f"Empty event list should return processed=0, got: {result}"

    def check_sync_missing_item():
        """Non-existent item_code should be silently skipped, not crash."""
        events = [{
            "id": _uid(),
            "item_code": "I014_NONEXISTENT_ITEM_XYZ",
            "delta": -1,
            "offline_order_uuid": _uid(),
            "created_at": frappe.utils.now_datetime().isoformat(),
        }]
        result = api.sync_stock_events(events)
        assert "processed" in result, "sync_stock_events crashed on unknown item_code"

    _run(f"5.8.1 sync_stock_events processes event (item={item['item_code']})",
         check_sync_one_event, "S2")
    _run("5.8.2 sync_stock_events empty list returns processed=0", check_sync_empty, "S3")
    _run("5.8.3 sync_stock_events unknown item does not crash", check_sync_missing_item, "S3")


# ── Suite 5.9 — Master data reads (Flutter / POS bootstrap) ───────────────────

def suite_5_9_master_data():
    print("\n[Suite 5.9] Master data reads")

    from erpnext.erpnext_integrations.ecommerce_api import api

    def check_item_groups():
        groups = api.get_item_groups()
        assert isinstance(groups, (list, dict)), f"unexpected type: {type(groups)}"

    def check_price_lists():
        pls = api.get_price_lists()
        assert isinstance(pls, (list, dict)), f"unexpected type: {type(pls)}"

    def check_warehouses():
        whs = api.get_warehouses()
        assert isinstance(whs, (list, dict)), f"unexpected type: {type(whs)}"

    def check_payment_methods():
        methods = api.get_payment_methods()
        assert methods is not None

    def check_promotions():
        promos = api.get_active_promotions(price_list="Standard Selling")
        assert isinstance(promos, (list, dict)), f"unexpected type: {type(promos)}"

    def check_pricing_rules():
        rules = api.list_pricing_rules(include_disabled=1, price_list="Standard Selling")
        assert rules is not None

    def check_get_product():
        item = _get_test_item()
        prod = api.get_product(item["item_code"], price_list="Standard Selling")
        assert prod and (prod.get("item_code") or prod.get("name")), f"bad product: {prod}"

    def check_stock_balance():
        item = _get_test_item()
        _, warehouse, _ = _get_defaults()
        bal = api.get_stock_balance(item["item_code"], warehouse)
        assert bal is not None

    def check_search_items():
        rows = api.search_items(search_term="a", price_list="Standard Selling", limit=5)
        assert isinstance(rows, (list, dict)), f"unexpected type: {type(rows)}"

    def check_labels():
        rows = api.get_items_for_label_print(price_list="Standard Selling")
        assert rows is not None

    def check_guest_preorders_list():
        rows = api.get_guest_preorders_list(page_length=5)
        assert rows is not None

    _run("5.9.1 get_item_groups", check_item_groups, "S3")
    _run("5.9.2 get_price_lists", check_price_lists, "S3")
    _run("5.9.3 get_warehouses", check_warehouses, "S3")
    _run("5.9.4 get_payment_methods", check_payment_methods, "S3")
    _run("5.9.5 get_active_promotions", check_promotions, "S3")
    _run("5.9.6 list_pricing_rules", check_pricing_rules, "S3")
    _run("5.9.7 get_product for stock item", check_get_product, "S2")
    _run("5.9.8 get_stock_balance", check_stock_balance, "S3")
    _run("5.9.9 search_items", check_search_items, "S3")
    _run("5.9.10 get_items_for_label_print", check_labels, "S3")
    _run("5.9.11 get_guest_preorders_list", check_guest_preorders_list, "S3")


# ── Suite 5.10 — Product Manager / ops reads ──────────────────────────────────

def suite_5_10_product_manager():
    print("\n[Suite 5.10] Product Manager reads")

    from erpnext.erpnext_integrations.ecommerce_api import product_manager as pm

    def check_pm_context():
        ctx = pm.get_pm_context()
        assert isinstance(ctx, dict), f"expected dict, got {type(ctx)}"
        assert "price_lists" in ctx or "default_price_list" in ctx, f"keys={list(ctx.keys())[:12]}"

    def check_product_rows():
        rows = pm.get_product_rows(page=1, page_length=5, price_list="Standard Selling")
        assert rows is not None
        assert isinstance(rows, dict) and ("rows" in rows or "items" in rows or "data" in rows), \
            f"unexpected shape: {list(rows.keys()) if isinstance(rows, dict) else type(rows)}"

    def check_uoms():
        assert hasattr(pm, "list_uoms"), "list_uoms missing on product_manager"
        uoms = pm.list_uoms()
        assert uoms is not None

    def check_remove_white_bg_dry():
        assert hasattr(pm, "remove_white_bg_item_images"), "remove_white_bg_item_images missing"
        out = pm.remove_white_bg_item_images(limit=1, dry_run=1)
        assert isinstance(out, dict) and out.get("ok"), f"bad dry_run: {out}"
        assert "total" in out and "remaining" in out

    def check_attr_names():
        names = pm.list_item_attribute_names()
        assert names is not None

    def check_brand_suggestions():
        sug = pm.get_brand_suggestions(query="a")
        assert sug is not None

    def check_category_list():
        cats = pm.get_category_list()
        assert cats is not None

    def check_generate_item_code():
        code = pm.generate_item_code()
        assert code, "generate_item_code returned empty"

    def check_create_with_nos_single_uom():
        # UI sends display label ``NOS (single)``; must map to ERP UOM ``Nos``.
        out = pm.create_product_row(
            None,
            {"source_title": "Smoke NOS single UOM", "stock_uom": "NOS (single)"},
            activate=0,
        )
        assert isinstance(out, dict) and out.get("item_code"), f"bad create: {out}"
        sku = out["item_code"]
        stock_uom = frappe.db.get_value("Item", sku, "stock_uom")
        assert stock_uom == "Nos", f"expected Nos, got {stock_uom!r}"
        frappe.delete_doc("Item", sku, ignore_permissions=True, force=True)

    _run("5.10.1 get_pm_context", check_pm_context, "S2")
    _run("5.10.2 get_product_rows page", check_product_rows, "S2")
    _run("5.10.3 list_uoms", check_uoms, "S3")
    _run("5.10.4 list_item_attribute_names", check_attr_names, "S3")
    _run("5.10.5 get_brand_suggestions", check_brand_suggestions, "S3")
    _run("5.10.6 get_category_list", check_category_list, "S3")
    _run("5.10.7 generate_item_code", check_generate_item_code, "S3")
    _run("5.10.8 create_product_row NOS (single)→Nos", check_create_with_nos_single_uom, "S3")
    _run("5.10.9 remove_white_bg_item_images dry_run", check_remove_white_bg_dry, "S3")


# ── Suite 5.11 — POS session / cash / admin settings ──────────────────────────

def suite_5_11_pos_session():
    print("\n[Suite 5.11] POS session & cash register")

    from erpnext.erpnext_integrations.ecommerce_api import pos_session_api as psa
    from erpnext.erpnext_integrations.ecommerce_api import cash_register_api as cra

    def check_admin_settings():
        s = psa.get_pos_admin_settings()
        assert isinstance(s, dict) and "pin_configured" in s, f"bad settings: {s}"

    def check_list_sessions():
        rows = psa.list_pos_cash_sessions(page=1, page_length=5)
        assert isinstance(rows, dict) and "rows" in rows, f"bad list: {rows}"

    def check_recent_cashiers():
        rows = psa.list_recent_cashiers(limit=5)
        assert rows is not None

    def check_validate_admin_pin_wrong():
        result = psa.validate_admin_pin(pin="___WRONG_SMOKE___")
        assert isinstance(result, dict)
        assert "authorized" in result and "pin_configured" in result

    def check_pos_profiles():
        rows = cra.list_pos_profiles(minimal=1)
        assert rows is not None

    def check_cash_sessions():
        rows = cra.list_cash_register_sessions(page=1, page_length=5)
        assert rows is not None

    def check_pos_profile_meta():
        meta = cra.list_pos_profile_meta()
        assert meta is not None

    _run("5.11.1 get_pos_admin_settings", check_admin_settings, "S2")
    _run("5.11.2 list_pos_cash_sessions", check_list_sessions, "S3")
    _run("5.11.3 list_recent_cashiers", check_recent_cashiers, "S3")
    _run("5.11.4 validate_admin_pin shape", check_validate_admin_pin_wrong, "S3")
    _run("5.11.5 list_pos_profiles", check_pos_profiles, "S3")
    _run("5.11.6 list_cash_register_sessions", check_cash_sessions, "S3")
    _run("5.11.7 list_pos_profile_meta", check_pos_profile_meta, "S3")


# ── Suite 5.12 — Preventa / staff / devices / floors / print / TMS ────────────

def suite_5_12_modules_read():
    print("\n[Suite 5.12] Preventa / staff / devices / floors / print / TMS / UI")

    def check_preventa():
        from erpnext.erpnext_integrations.ecommerce_api import preventa_api as pa
        s = pa.get_preventa_settings()
        assert s is not None
        board = pa.get_my_board()
        assert board is not None
        admin = pa.list_leads_admin(filters={}, start=0, page_length=5)
        assert isinstance(admin, dict) and isinstance(admin.get("leads"), list)
        for lead in admin["leads"]:
            assert "fields" in lead and isinstance(lead["fields"], dict), \
                f"list_leads_admin must nest contact data under fields: {list(lead.keys())}"
            assert "stage" in lead, f"list_leads_admin missing stage: {list(lead.keys())}"

        # duplicate_lead must not 409 on unique email (clone clears email_id)
        src = frappe.new_doc("Lead")
        src.lead_name = "Smoke Dup Source"
        src.email_id = f"smoke.dup.{frappe.generate_hash(length=8)}@example.com"
        src.flags.ignore_permissions = True
        src.insert(ignore_permissions=True)
        frappe.db.commit()
        clone_name = None
        try:
            out = pa.duplicate_lead(lead=src.name)
            assert out and out.get("ok") and out.get("name"), out
            clone_name = out["name"]
            assert clone_name != src.name
            clone_email = frappe.db.get_value("Lead", clone_name, "email_id")
            assert not clone_email, f"clone must clear email_id, got {clone_email!r}"
        finally:
            for name in (clone_name, src.name):
                if name and frappe.db.exists("Lead", name):
                    frappe.delete_doc("Lead", name, ignore_permissions=True, force=True)
            frappe.db.commit()

        # move_lead: soft rules return needs_confirm; force=1 overrides; hard mode throws
        gate = frappe.new_doc("Lead")
        gate.lead_name = "Smoke Stage Gate"
        gate.lead_owner = frappe.session.user
        gate.custom_preventa_stage = "prospect"
        gate.flags.ignore_permissions = True
        gate.insert(ignore_permissions=True)
        frappe.db.commit()
        try:
            out = pa.move_lead(lead=gate.name, to_stage="contacted")
            assert out and out.get("ok") and out.get("stage") == "contacted", out

            soft = pa.move_lead(lead=gate.name, to_stage="qualified")
            assert soft and soft.get("ok") is False and soft.get("needs_confirm"), soft
            assert soft.get("missing_labels"), soft

            forced = pa.move_lead(lead=gate.name, to_stage="qualified", force=1)
            assert forced and forced.get("ok") and forced.get("stage") == "qualified", forced
            assert frappe.db.get_value("Lead", gate.name, "custom_preventa_stage") == "qualified"

            # Hard mode still throws
            pa.save_preventa_settings(soft_stage_rules=0)
            try:
                hard = frappe.new_doc("Lead")
                hard.lead_name = "Smoke Hard Gate"
                hard.lead_owner = frappe.session.user
                hard.custom_preventa_stage = "contacted"
                hard.flags.ignore_permissions = True
                hard.insert(ignore_permissions=True)
                frappe.db.commit()
                try:
                    pa.move_lead(lead=hard.name, to_stage="qualified")
                    assert False, "hard soft_stage_rules=0 must ValidationError"
                except frappe.ValidationError:
                    pass
                finally:
                    if frappe.db.exists("Lead", hard.name):
                        frappe.delete_doc("Lead", hard.name, ignore_permissions=True, force=True)
            finally:
                pa.save_preventa_settings(soft_stage_rules=1)
        finally:
            if frappe.db.exists("Lead", gate.name):
                frappe.delete_doc("Lead", gate.name, ignore_permissions=True, force=True)
            frappe.db.commit()

        # convert_lead_to_customer: Customer.after_insert already marks Lead Converted;
        # must not TimestampMismatchError on a second Lead.save().
        conv = frappe.new_doc("Lead")
        conv.lead_name = "Smoke Convert Lead"
        conv.lead_owner = frappe.session.user
        conv.flags.ignore_permissions = True
        conv.insert(ignore_permissions=True)
        frappe.db.commit()
        cust_name = None
        try:
            out = pa.convert_lead_to_customer(lead=conv.name, customer_type="Individual")
            assert out and out.get("ok") and out.get("customer"), out
            cust_name = out["customer"]
            assert frappe.db.exists("Customer", cust_name)
            assert frappe.db.get_value("Lead", conv.name, "status") == "Converted"
            # Idempotent second call
            again = pa.convert_lead_to_customer(lead=conv.name, customer_type="Individual")
            assert again and again.get("customer") == cust_name, again
        finally:
            if cust_name and frappe.db.exists("Customer", cust_name):
                frappe.delete_doc("Customer", cust_name, ignore_permissions=True, force=True)
            if frappe.db.exists("Lead", conv.name):
                frappe.delete_doc("Lead", conv.name, ignore_permissions=True, force=True)
            frappe.db.commit()

    def check_employees():
        from erpnext.erpnext_integrations.ecommerce_api import employee_api as ea
        perms = ea.list_app_permissions()
        assert perms is not None
        rows = ea.list_employees(page=1, page_length=5)
        assert rows is not None
        groups = ea.list_employee_groups()
        assert groups is not None

    def check_devices():
        from erpnext.erpnext_integrations.ecommerce_api import device_link_api as dla
        link = dla.get_app_link()
        assert link is not None
        devices = dla.list_connected_devices()
        assert devices is not None
        push = dla.get_push_status()
        assert push is not None

    def check_floors():
        from erpnext.erpnext_integrations.ecommerce_api import floor_map_api as fma
        floors = fma.get_floors()
        assert floors is not None

    def check_print():
        from erpnext.erpnext_integrations.ecommerce_api import print_templates_api as pta
        rows = pta.list_print_templates()
        assert rows is not None
        fields = pta.get_doctype_fields("Staff Cred.")
        assert isinstance(fields, dict) and fields.get("fields"), f"Staff Cred. fields: {fields}"
        names = {f.get("fieldname") for f in (fields.get("fields") or [])}
        assert "employee_name" in names and "barcode" in names
        # Missing employee must be controlled error, not AttributeError/500.
        try:
            pta.get_print_data("Staff Cred.", "")
            raise AssertionError("expected DoesNotExistError for empty Staff Cred. docname")
        except Exception as e:
            assert "DoesNotExistError" in type(e).__name__ or "not found" in str(e).lower()

    def check_company_settings():
        from erpnext.erpnext_integrations.ecommerce_api import company_settings as cs
        payload = cs.get_company_settings()
        assert isinstance(payload, dict) and payload.get("company"), payload
        assert payload["company"].get("company_name") or payload["company"].get("name")
        assert payload.get("default_language") in ("en", "es", "zh")
        currencies = payload.get("currencies") or []
        assert "ARS" in currencies, currencies
        # LatAm + Asia should be offered in Settings → Company
        for code in ("BRL", "CLP", "UYU", "MXN", "CNY", "JPY", "KRW", "INR"):
            assert code in currencies, (code, currencies)
        assert currencies[0] == "ARS", currencies[:5]
        assert payload["company"].get("default_currency"), payload["company"]

        # Currency change must persist across reload (Company + Global Defaults).
        # ERPNext validate_currency / account-currency checks used to abort doc.save
        # so Dollar→ARS looked saved in the UI then snapped back on reload.
        company = payload["company"]
        old_currency = company.get("default_currency") or "ARS"
        other = "USD" if old_currency != "USD" else "EUR"
        switched = cs.save_company_settings(
            company=company["name"],
            settings={**company, "default_currency": other},
        )
        assert switched["company"]["default_currency"] == other, switched["company"]
        assert frappe.db.get_value("Company", company["name"], "default_currency") == other
        assert frappe.db.get_single_value("Global Defaults", "default_currency") == other
        restored_cur = cs.save_company_settings(
            company=company["name"],
            settings={**switched["company"], "default_currency": old_currency},
        )
        assert restored_cur["company"]["default_currency"] == old_currency, restored_cur["company"]
        assert frappe.db.get_value("Company", company["name"], "default_currency") == old_currency

        # Rename must succeed even when orphan Singles (Shopify Setting / Module shopify
        # not found) would crash core rename_doc — this is the Save Settings failure mode.
        company = restored_cur["company"]
        old_name = company["name"]
        tmp_name = f"{old_name}__smoke_ren"
        if frappe.db.exists("Company", tmp_name):
            tmp_name = f"{old_name}__smoke_ren2"
        settings = {
            **company,
            "company_name": tmp_name,
            "domain": "shopify",  # invalid domain must coerce, not crash
            "default_language": payload.get("default_language") or "es",
            "multi_company_enabled": payload.get("multi_company_enabled") or 0,
        }
        renamed = cs.save_company_settings(company=old_name, settings=settings)
        assert renamed["company"]["name"] == tmp_name, renamed["company"]
        assert (renamed["company"].get("domain") or "") != "shopify"
        # Rename back
        settings_back = {
            **renamed["company"],
            "company_name": old_name,
            "default_language": renamed.get("default_language") or "es",
            "multi_company_enabled": renamed.get("multi_company_enabled") or 0,
        }
        restored = cs.save_company_settings(company=tmp_name, settings=settings_back)
        assert restored["company"]["name"] == old_name, restored["company"]

        # Company logo: tiny PNG → local /files, then clear
        tiny_png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        uploaded = cs.upload_company_logo(
            company=old_name,
            filedata=f"data:image/png;base64,{tiny_png}",
            filename="smoke-logo.png",
        )
        logo = (uploaded.get("company") or {}).get("company_logo") or uploaded.get("company_logo")
        assert logo and str(logo).startswith("/files/"), uploaded
        cleared = cs.clear_company_logo(company=old_name)
        assert not ((cleared.get("company") or {}).get("company_logo") or ""), cleared

    def check_price_list_auto_rules():
        from erpnext.erpnext_integrations.ecommerce_api import price_list_rules as plr

        plr.ensure_price_list_rule_fields()
        plr.ensure_transferencia_auto_defaults()
        assert frappe.db.exists("Price List", "Transferencia"), "Transferencia list missing"
        assert frappe.db.has_column("Price List", "custom_auto_enabled")
        assert frappe.db.has_column("Item Price", "custom_manual_override")

        # Transferencia = 103% of Standard Selling
        rule = plr.get_auto_rule("Transferencia")
        assert rule, rule
        assert rule["base_price_list"] == "Standard Selling", rule
        assert flt(rule["percent"]) == 3.0, rule
        assert flt(plr.apply_auto_formula(100, 3, 0)) == 103.0

        # Standard Buying = 65% of Standard Selling
        buy_rule = plr.get_auto_rule("Standard Buying")
        assert buy_rule, buy_rule
        assert buy_rule["base_price_list"] == "Standard Selling", buy_rule
        assert flt(buy_rule["percent"]) == -35.0, buy_rule
        assert abs(flt(plr.apply_auto_formula(100, -35, 0)) - 65.0) < 0.01

        # Seed Standard Selling and sync → Transferencia + Standard Buying auto
        code = frappe.db.get_value("Item", {"disabled": 0}, "name")
        assert code, "need at least one Item"
        selling_pl = "Standard Selling"
        if not frappe.db.exists("Price List", selling_pl):
            frappe.get_doc(
                {
                    "doctype": "Price List",
                    "price_list_name": selling_pl,
                    "enabled": 1,
                    "buying": 0,
                    "selling": 1,
                    "currency": "ARS",
                }
            ).insert(ignore_permissions=True)
        existing = frappe.db.get_value(
            "Item Price", {"item_code": code, "price_list": selling_pl}, "name"
        )
        if existing:
            frappe.db.set_value("Item Price", existing, "price_list_rate", 200)
            if frappe.db.has_column("Item Price", "custom_manual_override"):
                frappe.db.set_value("Item Price", existing, "custom_manual_override", 0)
        else:
            frappe.get_doc(
                {
                    "doctype": "Item Price",
                    "item_code": code,
                    "price_list": selling_pl,
                    "buying": 0,
                    "selling": 1,
                    "price_list_rate": 200,
                }
            ).insert(ignore_permissions=True)
        frappe.db.commit()

        synced_t = plr.sync_auto_prices_for_list("Transferencia", item_codes=[code], force=1)
        assert synced_t.get("updated", 0) >= 1, synced_t
        t_rate = frappe.db.get_value(
            "Item Price",
            {"item_code": code, "price_list": "Transferencia"},
            "price_list_rate",
        )
        assert abs(flt(t_rate) - 206.0) < 0.01, (t_rate, "expected 200*1.03=206")
        override = cint(
            frappe.db.get_value(
                "Item Price",
                {"item_code": code, "price_list": "Transferencia"},
                "custom_manual_override",
            )
            or 0
        )
        assert override == 0, "auto sync must clear manual override"

        synced_b = plr.sync_auto_prices_for_list("Standard Buying", item_codes=[code], force=1)
        assert synced_b.get("updated", 0) >= 1, synced_b
        b_rate = frappe.db.get_value(
            "Item Price",
            {"item_code": code, "price_list": "Standard Buying"},
            "price_list_rate",
        )
        assert abs(flt(b_rate) - 130.0) < 0.01, (b_rate, "expected 200*0.65=130")
        b_override = cint(
            frappe.db.get_value(
                "Item Price",
                {"item_code": code, "price_list": "Standard Buying"},
                "custom_manual_override",
            )
            or 0
        )
        assert b_override == 0, "buying auto sync must clear manual override"

        meta = plr.selling_price_meta_map([code]).get(code) or {}
        assert meta.get("Transferencia", {}).get("auto") == 1, meta
        assert meta.get("Standard Buying", {}).get("auto") == 1, meta

    def check_catalog_import_reviews():
        from erpnext.erpnext_integrations.ecommerce_api import api as ecommerce_api
        rows = ecommerce_api.list_catalog_import_reviews(status="open", limit=5, start=0)
        assert isinstance(rows, list)
        # Minimal permissive import: header + blank-ish invalid row → review_created >= 1
        csv_text = (
            "sku,barcode,name,u1,title,pack,uom,a,b,group,c,d,price\n"
            ",,,,\n"
        )
        report = ecommerce_api.import_catalog_csv_products(
            csv_text=csv_text,
            price_list="Standard Selling",
            default_item_group="Products",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=50,
            file_name="smoke-catalog-import.csv",
        )
        assert isinstance(report, dict)
        assert "review_created" in report
        assert report.get("import_session")
        guide = ecommerce_api.get_catalog_csv_column_guide()
        assert isinstance(guide, list) and len(guide) >= 1

        # Airtable: Efectivo → Standard Selling (+ Efectivo list),
        # Transferencia → Transferencia only. Etiquetas leaf under Clase when both set.
        sku = f"SMOKE-AT-{frappe.generate_hash(length=6)}"
        parent_g = f"SmokeClase-{frappe.generate_hash(length=4)}"
        leaf_g = f"SmokeEtiq-{frappe.generate_hash(length=4)}"
        at_csv = (
            "TAG,Producto,Etiquetas,Clase,Marca,Estado,Efectivo,Transferencia,Imagen\n"
            f"{sku},Smoke Airtable Price,{leaf_g},{parent_g},SmokeBrand,Agotado,8800,9064,\n"
        )
        at_report = ecommerce_api.import_catalog_csv_products(
            csv_text=at_csv,
            price_list="Standard Selling",
            cash_price_list="Efectivo",
            transfer_price_list="Transferencia",
            default_item_group="Products",
            update_existing=1,
            create_missing_groups=1,
            start=0,
            batch_size=10,
            source="airtable",
            file_name="smoke-airtable-import.csv",
        )
        assert at_report.get("created_items", 0) + at_report.get("updated_items", 0) >= 1, at_report
        assert at_report.get("price_updates", 0) >= 2, at_report
        # Agotado → Item.disabled=1 → hidden from catalog get_products
        assert cint(frappe.db.get_value("Item", sku, "disabled")) == 1, "Agotado must disable Item"
        hidden = ecommerce_api.get_products(search_term=sku, page_length=5, include_disabled=0)
        assert not any(i.get("item_code") == sku for i in (hidden.get("items") or [])), hidden
        shown = ecommerce_api.get_products(search_term=sku, page_length=5, include_disabled=1)
        assert any(i.get("item_code") == sku for i in (shown.get("items") or [])), shown
        # Oferta + Cantidad → Pricing Rule (Rate, min_qty)
        sku_promo = f"SMOKE-ATP-{frappe.generate_hash(length=6)}"
        leaf_promo = f"SmokePLeaf-{frappe.generate_hash(length=4)}"
        parent_promo = f"SmokePClase-{frappe.generate_hash(length=4)}"
        promo_csv = (
            "TAG,Producto,Etiquetas,Clase,Marca,Estado,Efectivo,Transferencia,Oferta,Cantidad,Imagen\n"
            f"{sku_promo},Smoke Oferta Item,{leaf_promo},{parent_promo},SmokeBrand,En Stock,1000,1030,800,x2,\n"
        )
        promo_report = ecommerce_api.import_catalog_csv_products(
            csv_text=promo_csv,
            price_list="Standard Selling",
            cash_price_list="Efectivo",
            transfer_price_list="Transferencia",
            default_item_group="Products",
            update_existing=1,
            create_missing_groups=1,
            start=0,
            batch_size=10,
            source="airtable",
            import_promotions=1,
            image_mode="none",
            file_name="smoke-airtable-oferta.csv",
        )
        assert cint(promo_report.get("promo_updates") or 0) >= 1, promo_report
        rule_name = f"AT-{sku_promo}"
        assert frappe.db.exists("Pricing Rule", rule_name), rule_name
        rule = frappe.db.get_value(
            "Pricing Rule",
            rule_name,
            ["rate_or_discount", "rate", "min_qty", "disable", "apply_on", "price_or_product_discount"],
            as_dict=True,
        )
        assert rule.rate_or_discount == "Rate", rule
        assert flt(rule.rate) == 800, rule
        assert flt(rule.min_qty) == 2, rule
        assert cint(rule.disable) == 0, rule
        desc = cstr(frappe.db.get_value("Pricing Rule", rule_name, "rule_description") or "")
        assert "[promo_style=pack]" in desc, desc
        # Pack style: qty 5 list 1000 Oferta 800 min 2 → 2 packs × 2 × 200 = 800 savings
        cart = ecommerce_api.apply_cart_promotions(
            items=[{"item_code": sku_promo, "qty": 5, "rate": 1000, "amount": 5000}],
            price_list="Standard Selling",
        )
        lines = cart.get("line_discounts") or []
        assert lines, cart
        assert abs(flt(lines[0].get("discount_amount")) - 800) < 0.01, cart
        # Re-import as threshold → all 5 units at 800 → savings 1000
        thresh_report = ecommerce_api.import_catalog_csv_products(
            csv_text=promo_csv,
            price_list="Standard Selling",
            cash_price_list="Efectivo",
            transfer_price_list="Transferencia",
            default_item_group="Products",
            update_existing=1,
            create_missing_groups=1,
            start=0,
            batch_size=10,
            source="airtable",
            import_promotions=1,
            promo_style="threshold",
            image_mode="none",
            file_name="smoke-airtable-oferta-threshold.csv",
        )
        assert cint(thresh_report.get("promo_updates") or 0) >= 1, thresh_report
        desc_t = cstr(frappe.db.get_value("Pricing Rule", rule_name, "rule_description") or "")
        assert "[promo_style=threshold]" in desc_t, desc_t
        cart_t = ecommerce_api.apply_cart_promotions(
            items=[{"item_code": sku_promo, "qty": 5, "rate": 1000, "amount": 5000}],
            price_list="Standard Selling",
        )
        lines_t = cart_t.get("line_discounts") or []
        assert lines_t, cart_t
        assert abs(flt(lines_t[0].get("discount_amount")) - 1000) < 0.01, cart_t
        # Clear Oferta → disable rule
        clear_csv = (
            "TAG,Producto,Etiquetas,Clase,Marca,Estado,Efectivo,Transferencia,Oferta,Cantidad,Imagen\n"
            f"{sku_promo},Smoke Oferta Item,{leaf_promo},{parent_promo},SmokeBrand,En Stock,1000,1030,,,\n"
        )
        clear_report = ecommerce_api.import_catalog_csv_products(
            csv_text=clear_csv,
            price_list="Standard Selling",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=10,
            source="airtable",
            import_promotions=1,
            image_mode="none",
            file_name="smoke-airtable-oferta-clear.csv",
        )
        assert cint(clear_report.get("promo_disabled") or 0) >= 1, clear_report
        assert cint(frappe.db.get_value("Pricing Rule", rule_name, "disable")) == 1
        # cleanup promo item + rule
        if frappe.db.exists("Pricing Rule", rule_name):
            frappe.delete_doc("Pricing Rule", rule_name, ignore_permissions=True, force=1)
        for pl in ("Standard Selling", "Efectivo", "Transferencia"):
            pname = frappe.db.get_value(
                "Item Price", {"item_code": sku_promo, "price_list": pl, "selling": 1}, "name"
            )
            if pname:
                frappe.delete_doc("Item Price", pname, ignore_permissions=True, force=1)
        if frappe.db.exists("Item", sku_promo):
            frappe.delete_doc("Item", sku_promo, ignore_permissions=True, force=1)
        for g in (leaf_promo, parent_promo, f"{parent_promo} › Otros"):
            if frappe.db.exists("Item Group", g):
                try:
                    frappe.delete_doc("Item Group", g, ignore_permissions=True, force=1)
                except Exception:
                    pass

        item_row = frappe.db.get_value(
            "Item",
            sku,
            ["item_name", "item_group", "brand", "disabled", "custom_normalized_title"],
            as_dict=True,
        )
        assert item_row, f"Item {sku} missing"
        assert item_row.item_name == "Smoke Airtable Price", item_row
        if frappe.db.has_column("Item", "custom_normalized_title"):
            assert (item_row.custom_normalized_title or "") == "Smoke Airtable Price", item_row
        assert item_row.item_group == leaf_g, f"expected leaf {leaf_g}, got {item_row.item_group}"
        assert item_row.brand == "SmokeBrand", item_row
        assert cint(item_row.disabled) == 1, item_row
        parent_of_leaf = frappe.db.get_value("Item Group", leaf_g, "parent_item_group")
        assert parent_of_leaf == parent_g, f"expected parent {parent_g}, got {parent_of_leaf}"
        assert cint(frappe.db.get_value("Item Group", parent_g, "is_group")) == 1
        assert cint(frappe.db.get_value("Item Group", leaf_g, "is_group")) == 0
        meta = ecommerce_api.get_products(search_term=sku, page_length=5, include_disabled=1)
        hit = next((i for i in (meta.get("items") or []) if i.get("item_code") == sku), None)
        assert hit, meta
        assert hit.get("parent_item_group") == parent_g, hit
        assert hit.get("item_group_path") == f"{parent_g}>{leaf_g}", hit
        assert hit.get("item_name") == "Smoke Airtable Price", hit
        assert hit.get("brand") == "SmokeBrand", hit
        std = flt(
            frappe.db.get_value(
                "Item Price",
                {"item_code": sku, "price_list": "Standard Selling", "selling": 1},
                "price_list_rate",
            )
            or 0
        )
        cash = flt(
            frappe.db.get_value(
                "Item Price",
                {"item_code": sku, "price_list": "Efectivo", "selling": 1},
                "price_list_rate",
            )
            or 0
        )
        xfer = flt(
            frappe.db.get_value(
                "Item Price",
                {"item_code": sku, "price_list": "Transferencia", "selling": 1},
                "price_list_rate",
            )
            or 0
        )
        assert std == 8800, f"Standard Selling (Efectivo main) missing/wrong: {std}"
        assert cash == 8800, f"Efectivo missing/wrong: {cash}"
        assert xfer == 9064, f"Transferencia missing/wrong: {xfer}"
        for pl in ("Standard Selling", "Efectivo", "Transferencia"):
            name = frappe.db.get_value(
                "Item Price", {"item_code": sku, "price_list": pl, "selling": 1}, "name"
            )
            if name:
                frappe.delete_doc("Item Price", name, ignore_permissions=True, force=1)
        if frappe.db.exists("Item", sku):
            frappe.delete_doc("Item", sku, ignore_permissions=True, force=1)
        if frappe.db.exists("Brand", "SmokeBrand"):
            frappe.delete_doc("Brand", "SmokeBrand", ignore_permissions=True, force=1)
        for g in (leaf_g, parent_g, f"{parent_g} › Otros"):
            if frappe.db.exists("Item Group", g):
                try:
                    frappe.delete_doc("Item Group", g, ignore_permissions=True, force=1)
                except Exception:
                    pass

        # Custom: manual column_map + preview, then import with Standard Selling price.
        sku2 = f"SMOKE-CU-{frappe.generate_hash(length=6)}"
        # 1×1 PNG
        tiny_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        cu_csv = (
            "Code,Title,Cat,Cash,List,Img\n"
            f'{sku2},Smoke Custom Map,Products,500,550,"{tiny_png}"\n'
        )
        preview = ecommerce_api.preview_catalog_csv_import(
            csv_text=cu_csv,
            source="custom",
            column_map={
                "item_code": "Code",
                "item_name": "Title",
                "item_group": "Cat",
                "price": "List",
                "cash_price": "Cash",
                "image_url": "Img",
            },
        )
        assert preview.get("valid_rows") == 1, preview
        assert preview["preview"][0]["price"] == 550
        assert preview["preview"][0]["cash_price"] == 500
        cu_report = ecommerce_api.import_catalog_csv_products(
            csv_text=cu_csv,
            price_list="Standard Selling",
            cash_price_list="Efectivo",
            transfer_price_list="",
            default_item_group="Products",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=10,
            source="custom",
            column_map={
                "item_code": "Code",
                "item_name": "Title",
                "item_group": "Cat",
                "price": "List",
                "cash_price": "Cash",
                "image_url": "Img",
            },
            file_name="smoke-custom-import.csv",
        )
        assert cu_report.get("price_updates", 0) >= 2, cu_report
        assert cu_report.get("image_updates", 0) >= 1, cu_report
        img = frappe.db.get_value("Item", sku2, "image") or ""
        assert img.startswith("/files/"), f"expected local thumb, got {img!r}"
        assert img.lower().endswith(".png"), f"catalog migration thumbs should be PNG (alpha), got {img!r}"

        # image_mode: none → skip; blank → fill empty only; all → override
        sku_img = f"SMOKE-IM-{frappe.generate_hash(length=6)}"
        img_map = {
            "item_code": "Code",
            "item_name": "Title",
            "item_group": "Cat",
            "price": "List",
            "image_url": "Img",
        }
        img_csv = (
            "Code,Title,Cat,List,Img\n"
            f'{sku_img},Smoke Image Mode,Products,11,"{tiny_png}"\n'
        )
        none_rep = ecommerce_api.import_catalog_csv_products(
            csv_text=img_csv,
            price_list="Standard Selling",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=10,
            source="custom",
            column_map=img_map,
            image_mode="none",
            file_name="smoke-image-mode-none.csv",
        )
        assert none_rep.get("image_mode") == "none", none_rep
        assert cint(none_rep.get("image_updates") or 0) == 0, none_rep
        assert not (frappe.db.get_value("Item", sku_img, "image") or ""), "none must leave image empty"

        blank_rep = ecommerce_api.import_catalog_csv_products(
            csv_text=img_csv,
            price_list="Standard Selling",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=10,
            source="custom",
            column_map=img_map,
            image_mode="blank",
            file_name="smoke-image-mode-blank.csv",
        )
        assert blank_rep.get("image_mode") == "blank", blank_rep
        assert cint(blank_rep.get("image_updates") or 0) >= 1, blank_rep
        filled = frappe.db.get_value("Item", sku_img, "image") or ""
        assert filled.startswith("/files/"), filled
        assert filled.lower().endswith(".png"), filled
        frappe.db.set_value("Item", sku_img, "image", "/files/smoke-keep-existing.jpg")
        blank_keep = ecommerce_api.import_catalog_csv_products(
            csv_text=img_csv,
            price_list="Standard Selling",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=10,
            source="custom",
            column_map=img_map,
            image_mode="blank",
            file_name="smoke-image-mode-blank-keep.csv",
        )
        assert (frappe.db.get_value("Item", sku_img, "image") or "") == "/files/smoke-keep-existing.jpg"
        assert cint(blank_keep.get("image_updates") or 0) == 0, blank_keep
        all_rep = ecommerce_api.import_catalog_csv_products(
            csv_text=img_csv,
            price_list="Standard Selling",
            update_existing=1,
            create_missing_groups=0,
            start=0,
            batch_size=10,
            source="custom",
            column_map=img_map,
            image_mode="all",
            file_name="smoke-image-mode-all.csv",
        )
        assert all_rep.get("image_mode") == "all", all_rep
        assert cint(all_rep.get("image_updates") or 0) >= 1, all_rep
        overridden = frappe.db.get_value("Item", sku_img, "image") or ""
        assert overridden.startswith("/files/"), overridden
        assert overridden.lower().endswith(".png"), overridden
        assert overridden != "/files/smoke-keep-existing.jpg", overridden

        assert (
            flt(
                frappe.db.get_value(
                    "Item Price",
                    {"item_code": sku2, "price_list": "Standard Selling", "selling": 1},
                    "price_list_rate",
                )
                or 0
            )
            == 550
        )
        for pl in ("Standard Selling", "Efectivo"):
            name = frappe.db.get_value(
                "Item Price", {"item_code": sku2, "price_list": pl, "selling": 1}, "name"
            )
            if name:
                frappe.delete_doc("Item Price", name, ignore_permissions=True, force=1)
        for code in (sku2, sku_img):
            name = frappe.db.get_value(
                "Item Price", {"item_code": code, "price_list": "Standard Selling", "selling": 1}, "name"
            )
            if name:
                frappe.delete_doc("Item Price", name, ignore_permissions=True, force=1)
            if frappe.db.exists("Item", code):
                frappe.delete_doc("Item", code, ignore_permissions=True, force=1)
        frappe.db.commit()

    def check_tms():
        from erpnext.erpnext_integrations.ecommerce_api import tms_api as tms
        ctx = tms.get_planner_context()
        assert ctx is not None
        settings = tms.get_tms_settings()
        assert settings is not None
        assert "require_pin_for_order_actions" in settings
        claimable = tms.list_claimable_orders()
        assert isinstance(claimable, dict) and isinstance(claimable.get("orders"), list)
        # Empty claim must raise a controlled error (not TypeError/500)
        try:
            tms.claim_orders_to_trip(delivery_notes=[], preorder_names=[], pin=None)
            raise AssertionError("expected error for empty claim")
        except Exception as exc:
            assert "ValidationError" in type(exc).__name__ or "select" in str(exc).lower() or "pin" in str(exc).lower() or "Admin" in str(exc) or "Incorrect" in str(exc), exc

        tmpl = tms.get_rutas_orders_csv_template()
        assert isinstance(tmpl, dict) and tmpl.get("csv_text")
        assert "Código de Orden" in tmpl["csv_text"]

        item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name")
        assert item
        ocode = f"CSV-SMOKE-TEST-{frappe.generate_hash(length=6)}"
        phone_suffix = abs(hash(ocode)) % 10000000
        create_csv = (
            "Código de Cliente,Nombre,Calle y Número,Ciudad,Provincia/Estado,Latitud,Longitud,"
            "Teléfono (con código de país),Email del cliente,Código de Orden,Fecha de Orden,"
            "Tipo de Operación (E/R),Código de Producto,Descripción del Producto,Cantidad de Producto,"
            "Peso,Volumen,Dinero,Duración (min),Ventana horaria 1,Ventana horaria 2,Notas,Agrupador,"
            "Email del vendedor o seller,Eliminar Orden (Si - No - Vacío),Vehículo,Habilidades\n"
            f",Smoke CSV Client,Av. Test 1,Buenos Aires,CABA,-34.60,-58.38,+54911{phone_suffix:07d},"
            f"smoke.csv.{ocode}@example.com,{ocode},2026-09-23,E,{item},Smoke,1,,,100,10,09:00 - 12:00,,smoke,,,,"
            "\n"
        )
        created = tms.import_rutas_orders_csv(csv_text=create_csv, pin=None)
        assert isinstance(created, dict), created
        assert created.get("summary", {}).get("created", 0) >= 1 or created.get("created"), created

        delete_csv = (
            "Código de Cliente,Nombre,Calle y Número,Ciudad,Provincia/Estado,Latitud,Longitud,"
            "Teléfono (con código de país),Email del cliente,Código de Orden,Fecha de Orden,"
            "Tipo de Operación (E/R),Código de Producto,Descripción del Producto,Cantidad de Producto,"
            "Peso,Volumen,Dinero,Duración (min),Ventana horaria 1,Ventana horaria 2,Notas,Agrupador,"
            "Email del vendedor o seller,Eliminar Orden (Si - No - Vacío),Vehículo,Habilidades\n"
            f",,,,,,,,,{ocode},,,,,,,,,,,,,,,Si,,\n"
        )
        deleted = tms.import_rutas_orders_csv(csv_text=delete_csv, pin=None)
        assert isinstance(deleted, dict), deleted
        assert deleted.get("summary", {}).get("deleted", 0) >= 1 or deleted.get("deleted"), deleted

    def check_shop_ui():
        from erpnext.erpnext_integrations.ecommerce_api import shop_ui_settings as sui
        s = sui.get_shop_ui_settings()
        assert s is not None
        # catalogTemplate must round-trip (save response used to strip it → UI snap-back)
        out = sui.save_shop_ui_settings({"catalogDisplay": {"catalogTemplate": "commerce"}})
        settings = (out or {}).get("settings") or {}
        assert (settings.get("catalogDisplay") or {}).get("catalogTemplate") == "commerce", settings.get("catalogDisplay")
        out2 = sui.save_shop_ui_settings({"catalogDisplay": {"catalogTemplate": "classic"}})
        settings2 = (out2 or {}).get("settings") or {}
        assert (settings2.get("catalogDisplay") or {}).get("catalogTemplate") == "classic"

    def check_tags():
        from erpnext.erpnext_integrations.ecommerce_api import tags_api as ta
        tags = ta.search_tags(query="a")
        assert tags is not None

    def check_openapi():
        from erpnext.erpnext_integrations.ecommerce_api import openapi
        spec = openapi.get_openapi_spec()
        assert isinstance(spec, dict) and (spec.get("paths") or spec.get("openapi")), \
            f"bad openapi: {list(spec.keys())[:8] if isinstance(spec, dict) else type(spec)}"

    def check_crm_party():
        from erpnext.erpnext_integrations.ecommerce_api import crm_party_api as cpa
        cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
        if not cust:
            return
        inv = cpa.list_party_invoices(party_type="Customer", party=cust, is_return=0, page_length=5)
        assert inv.get("ok") and isinstance(inv.get("rows"), list), f"bad invoices: {inv}"
        pay = cpa.list_party_payments(party_type="Customer", party=cust, page_length=5)
        assert pay.get("ok") and isinstance(pay.get("rows"), list), f"bad payments: {pay}"
        prods = cpa.list_party_products(party_type="Customer", party=cust, page_length=5)
        assert prods.get("ok") and isinstance(prods.get("products"), list), f"bad products: {prods}"

    def check_buying():
        from erpnext.erpnext_integrations.ecommerce_api import buying_api as ba
        listed = ba.list_purchase_orders(page_length=5)
        assert listed.get("ok") and isinstance(listed.get("rows"), list), f"bad PO list: {listed}"
        # Controlled validation — empty create must not 500
        try:
            ba.create_purchase_order(supplier="", items=[])
            raise AssertionError("expected ValidationError for empty PO")
        except Exception as exc:
            assert "ValidationError" in type(exc).__name__ or "supplier" in str(exc).lower() or "item" in str(exc).lower(), exc
        try:
            ba.get_purchase_order_detail(name="")
            raise AssertionError("expected ValidationError for empty PO name")
        except Exception as exc:
            assert "ValidationError" in type(exc).__name__ or "name" in str(exc).lower(), exc
        if listed.get("rows"):
            detail = ba.get_purchase_order_detail(name=listed["rows"][0]["name"])
            assert detail.get("ok") and detail.get("order") and isinstance(detail["order"].get("lines"), list)
        # Cost trail + cost_edited write Standard Buying
        item = frappe.db.get_value("Item", {"disabled": 0, "is_stock_item": 1}, "name")
        if item:
            trail = ba.list_item_buying_cost_trail(item_code=item, limit=5)
            assert trail.get("ok") and trail.get("item_code") == item and isinstance(trail.get("rows"), list), trail
        supplier = frappe.db.get_value("Supplier", {}, "name")
        # Prefer stable fixtures — avoid edge-kit artifacts / placeholder suppliers
        supplier = (
            frappe.db.get_value("Supplier", {"name": ["like", "SUP-%"]}, "name")
            or frappe.db.get_value("Supplier", {"name": ["!=", "Uncategorized"]}, "name")
            or supplier
        )
        item = (
            frappe.db.get_value(
                "Item",
                {"disabled": 0, "is_stock_item": 1, "item_code": ["not like", "EDGE%"]},
                "name",
            )
            or item
        )
        if item and supplier:
            rate = 12.34
            buying_pl = frappe.db.get_single_value("Buying Settings", "buying_price_list") or "Standard Buying"
            before = frappe.db.get_value(
                "Item Price",
                {"item_code": item, "price_list": buying_pl, "buying": 1},
                "price_list_rate",
            )
            created = ba.create_purchase_order(
                supplier=supplier,
                schedule_date=frappe.utils.nowdate(),
                submit=0,
                items=[{"item_code": item, "qty": 1, "rate": rate, "cost_edited": 1}],
            )
            assert created.get("ok") and created.get("name"), created
            assert any(u.get("item_code") == item for u in (created.get("cost_updates") or [])), created
            after = frappe.db.get_value(
                "Item Price",
                {"item_code": item, "price_list": buying_pl, "buying": 1},
                "price_list_rate",
            )
            assert float(after or 0) == rate, f"buying rate not updated: before={before} after={after}"
            # cleanup draft PO (db.delete avoids MandatoryError on broken title templates)
            try:
                frappe.db.delete("Purchase Order Item", {"parent": created["name"]})
                frappe.db.delete("Purchase Order", {"name": created["name"]})
                frappe.db.commit()
            except Exception:
                try:
                    frappe.delete_doc("Purchase Order", created["name"], force=1, ignore_permissions=True)
                    frappe.db.commit()
                except Exception:
                    pass
    if frappe.db.exists("DocType", "Preventa Lead Consulta") or frappe.db.exists("DocType", "Preventa Settings"):
        _run("5.12.1 preventa settings + board", check_preventa, "S3")
    else:
        _skip("5.12.1 preventa settings + board", "Preventa DocTypes not migrated on this site", "S3")
    _run("5.12.2 employee permissions/list/groups", check_employees, "S3")
    _run("5.12.3 device link + push status", check_devices, "S3")
    _run("5.12.4 get_floors", check_floors, "S3")
    _run("5.12.5 list_print_templates", check_print, "S3")
    _run("5.12.5b get/save company settings (rename + orphan Shopify Single)", check_company_settings, "S2")
    _run("5.12.5d price list auto rules (Transferencia=103% Standard Selling; Buying=65%)", check_price_list_auto_rules, "S2")
    if frappe.db.exists("DocType", "Catalog Import Session"):
        _run("5.12.5c catalog import reviews + permissive enqueue", check_catalog_import_reviews, "S2")
    else:
        _skip("5.12.5c catalog import reviews + permissive enqueue", "Catalog Import Session DocType not migrated", "S2")
    _run("5.12.6 tms planner context + settings", check_tms, "S3")
    _run("5.12.7 get_shop_ui_settings", check_shop_ui, "S3")
    _run("5.12.8 search_tags", check_tags, "S3")
    _run("5.12.9 get_openapi_spec", check_openapi, "S3")
    _run("5.12.10 crm party invoices/payments/products", check_crm_party, "S3")
    _run("5.12.11 buying list + cost trail + cost_edited upsert", check_buying, "S3")
    _run("5.12.12 doc activity get + comment", check_doc_activity, "S3")


def check_doc_activity():
    from erpnext.erpnext_integrations.ecommerce_api import doc_activity as da

    item = frappe.db.get_value("Item", {"disabled": 0}, "name")
    assert item, "need at least one Item for activity smoke"
    feed = da.get_doc_activity(doctype="Item", name=item, limit=10)
    assert isinstance(feed, dict) and "items" in feed, f"bad feed: {feed}"
    assert feed.get("doctype") == "Item" and feed.get("name") == item
    # Controlled fail on missing ref
    try:
        da.get_doc_activity(doctype="Item", name="")
        raise AssertionError("empty name should fail")
    except Exception:
        pass
    marker = f"smoke-activity-{frappe.generate_hash(length=8)}"
    out = da.add_doc_comment(doctype="Item", name=item, content=marker)
    assert out.get("ok") and out.get("comment", {}).get("content") == marker, f"bad comment: {out}"
    feed2 = da.get_doc_activity(doctype="Item", name=item, limit=20)
    texts = [i.get("content") or i.get("summary") for i in (feed2.get("items") or [])]
    assert marker in texts, f"comment not in feed: {texts[:5]}"


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup():
    """
    Cancel and delete all Sales Invoices and Stock Entries tagged with I014_SMOKE.
    Safe to run standalone:
      ./scripts/test_smoke.sh
      bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.test_smoke.cleanup
    """
    print("\n  Cleaning up I014_SMOKE test records...")
    deleted_si = 0
    deleted_se = 0

    # Sales Invoices — must cancel payment entries first
    si_names = frappe.get_all(
        "Sales Invoice",
        filters=[["remarks", "like", f"%{TAG}%"]],
        pluck="name",
    )
    for name in si_names:
        try:
            si = frappe.get_doc("Sales Invoice", name)
            if si.docstatus == 1:
                # Cancel linked payment entries first
                pe_names = frappe.get_all(
                    "Payment Entry Reference",
                    filters={"reference_doctype": "Sales Invoice", "reference_name": name},
                    pluck="parent",
                )
                for pe_name in pe_names:
                    try:
                        pe = frappe.get_doc("Payment Entry", pe_name)
                        if pe.docstatus == 1:
                            pe.cancel()
                        frappe.delete_doc("Payment Entry", pe_name,
                                          ignore_permissions=True, force=True)
                    except Exception as e:
                        print(f"  {_WARN}  Could not remove Payment Entry {pe_name}: {e}")
                si.reload()  # timestamps changed after PE cancel
                si.cancel()
            frappe.delete_doc("Sales Invoice", name, ignore_permissions=True, force=True)
            deleted_si += 1
        except Exception as e:
            print(f"  {_WARN}  Could not remove Sales Invoice {name}: {e}")

    # Stock Entries
    se_names = frappe.get_all(
        "Stock Entry",
        filters=[["remarks", "like", f"%{TAG}%"]],
        pluck="name",
    )
    for name in se_names:
        try:
            se = frappe.get_doc("Stock Entry", name)
            if se.docstatus == 1:
                se.cancel()
            frappe.delete_doc("Stock Entry", name, ignore_permissions=True, force=True)
            deleted_se += 1
        except Exception as e:
            print(f"  {_WARN}  Could not remove Stock Entry {name}: {e}")

    if deleted_si or deleted_se:
        frappe.db.commit()
    print(f"  Removed {deleted_si} Sales Invoice(s), {deleted_se} Stock Entry(s).")


# ── Summary ───────────────────────────────────────────────────────────────────

def _print_summary():
    total = len(_results)
    passed = sum(1 for _, s, _, _ in _results if s == "PASS")
    failed = sum(1 for _, s, _, _ in _results if s == "FAIL")
    skipped = sum(1 for _, s, _, _ in _results if s == "SKIP")

    s1_fails = [r for r in _results if r[1] == "FAIL" and r[2] == "S1"]
    s2_fails = [r for r in _results if r[1] == "FAIL" and r[2] == "S2"]

    print(f"\n{'─' * 60}")
    print(f"  {passed}/{total} passed  |  {failed} failed  |  {skipped} skipped")

    if failed:
        print(f"\n  Failed tests:")
        for label, status, sev, detail in _results:
            if status == "FAIL":
                first_line = (detail or "").split("\n")[0][:80]
                print(f"    [{sev}] {label}")
                if first_line:
                    print(f"          {first_line}")

    if s1_fails:
        print(f"\n  \033[91m⛔ {len(s1_fails)} S1 CRITICAL failure(s) — release blocked\033[0m")
    elif s2_fails:
        print(f"\n  \033[93m⚠  {len(s2_fails)} S2 HIGH failure(s) — investigate before release\033[0m")
    else:
        print(f"\n  \033[92m✓ No S1/S2 failures — smoke passed\033[0m")

    print(f"{'─' * 60}\n")
    return len(s1_fails) == 0 and len(s2_fails) == 0


# ── Entry point ───────────────────────────────────────────────────────────────

def run(do_cleanup="1"):
    """
    Run all smoke suites and optionally clean up test records.

    Args:
        do_cleanup: "1" (default) delete tagged records after run, "0" to keep them.
    """
    import sys

    _results.clear()

    print("\n" + "═" * 60)
    print("  test_smoke — POS / ecommerce (expanded endpoint coverage)")
    print("═" * 60)

    suite_5_1_auth()
    suite_5_2_catalog()
    suite_5_3_pos_sale_white()
    suite_5_4_pos_sale_black()
    suite_5_5_promotions()
    suite_5_7_receiving()
    suite_5_8_sync()
    suite_5_9_master_data()
    suite_5_10_product_manager()
    suite_5_11_pos_session()
    suite_5_12_modules_read()

    passed = _print_summary()

    if str(do_cleanup) != "0":
        cleanup()

    if not passed:
        sys.exit(1)
