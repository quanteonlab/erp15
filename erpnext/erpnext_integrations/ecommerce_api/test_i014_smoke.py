"""
i014 Smoke Test — Chinese Bazar POS
=====================================
Automated smoke suite covering the critical flows from the i014 Android App Test Plan.

Usage:
  # Daily smoke (all suites + cleanup)
  bench --site dev_site_a execute \
    erpnext.erpnext_integrations.ecommerce_api.test_i014_smoke.run

  # Keep test records for inspection
  bench --site dev_site_a execute \
    erpnext.erpnext_integrations.ecommerce_api.test_i014_smoke.run \
    --kwargs '{"do_cleanup":"0"}'

  # Cleanup only (without running tests)
  bench --site dev_site_a execute \
    erpnext.erpnext_integrations.ecommerce_api.test_i014_smoke.cleanup

Suites implemented:
  5.1  Auth & Configuration
  5.2  Catalog, Search, Barcode
  5.3  POS Sale — WHITE mode (with idempotency check)
  5.4  POS Sale — BLACK mode (Cash allowed, Card rejected)
  5.5  Promotions, Coupons, Discount PIN
  5.7  Receiving Flow (simulate + commit)
  5.8  Stock Event Sync

All test records are tagged with I014_SMOKE for easy cleanup.
"""

import uuid
import traceback

import frappe
from frappe.utils import flt, nowdate

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
    customer = (
        frappe.db.get_value("Customer", {"customer_name": "Walk-in Customer"}, "name")
        or frappe.db.get_value("Customer", {}, "name")
        or "_Test Customer"
    )
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
        If pos_manager_pin is configured: wrong pin → not authorized.
        If no pin configured: open mode → any input authorized.
        """
        if frappe.conf.get("pos_manager_pin"):
            result = api.validate_discount_pin("000000_WRONG_I014")
            assert result.get("authorized") is False, \
                "Wrong PIN should not be authorized when pos_manager_pin is set"
        else:
            result = api.validate_discount_pin("anything")
            assert result.get("authorized") is True, \
                "Open mode (no pos_manager_pin) should authorize any PIN input"

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


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup():
    """
    Cancel and delete all Sales Invoices and Stock Entries tagged with I014_SMOKE.
    Safe to run standalone: bench --site dev_site_a execute
        erpnext.erpnext_integrations.ecommerce_api.test_i014_smoke.cleanup
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
    print("  i014 Smoke Test — Chinese Bazar POS")
    print("═" * 60)

    suite_5_1_auth()
    suite_5_2_catalog()
    suite_5_3_pos_sale_white()
    suite_5_4_pos_sale_black()
    suite_5_5_promotions()
    suite_5_7_receiving()
    suite_5_8_sync()

    passed = _print_summary()

    if str(do_cleanup) != "0":
        cleanup()

    if not passed:
        sys.exit(1)
