import json
import re
import unicodedata
from pathlib import Path

import frappe


LEGACY_RESULTS_PATH = ("issues", "image_search_results.json")


def import_all_legacy_candidates(skip_existing: bool = True) -> dict[str, int]:
    payload = _load_payload()
    item_lookup = _build_item_lookup(payload.keys())
    summary = {
        "items_total": len(payload),
        "items_matched": 0,
        "items_skipped_existing": 0,
        "items_unmatched": 0,
        "candidates_inserted": 0,
        "candidate_errors": 0,
    }

    for legacy_key, raw_candidates in payload.items():
        item_code = item_lookup.get(legacy_key)
        if not item_code:
            summary["items_unmatched"] += 1
            continue

        result = import_candidates_for_item(
            item_code=item_code,
            item_name=legacy_key,
            raw_candidates=raw_candidates,
            skip_existing=skip_existing,
        )
        summary["items_matched"] += 1 if result["matched"] else 0
        summary["items_skipped_existing"] += result["skipped_existing"]
        summary["candidates_inserted"] += result["candidates_inserted"]
        summary["candidate_errors"] += result["candidate_errors"]

    frappe.db.commit()
    return summary


def import_candidates_for_item(
    item_code: str,
    item_name: str | None = None,
    raw_candidates=None,
    skip_existing: bool = True,
) -> dict[str, int | bool]:
    summary = {
        "matched": False,
        "skipped_existing": 0,
        "candidates_inserted": 0,
        "candidate_errors": 0,
    }

    existing_urls = set()
    if skip_existing:
        existing_urls = {
            row.image_url
            for row in frappe.get_all(
                "Product Image Candidate",
                filters={"product_type": "Item", "product_id": item_code},
                fields=["image_url"],
                limit_page_length=0,
            )
            if row.image_url
        }

    payload = None if raw_candidates is not None else _load_payload()
    if raw_candidates is None:
        item_doc = frappe.db.get_value("Item", item_code, ["item_code", "item_name"], as_dict=True)
        if not item_doc:
            return summary
        legacy_key = _resolve_legacy_key(payload, item_doc.item_code, item_name or item_doc.item_name)
        if not legacy_key:
            return summary
        raw_candidates = payload.get(legacy_key)
        item_name = legacy_key

    candidates = _normalize_candidates(raw_candidates)
    if not candidates:
        return summary

    summary["matched"] = True
    for candidate in candidates:
        if skip_existing and candidate["image_url"] in existing_urls:
            continue
        try:
            _insert_candidate(item_code, item_name or item_code, candidate)
            summary["candidates_inserted"] += 1
        except Exception:
            summary["candidate_errors"] += 1
            frappe.log_error(
                title="Legacy Image Candidate Import Failed",
                message=frappe.get_traceback(),
            )

    if skip_existing and existing_urls and summary["candidates_inserted"] == 0:
        summary["skipped_existing"] = 1

    frappe.db.commit()
    return summary


def _load_payload() -> dict[str, list[dict]]:
    legacy_path = _get_legacy_results_path()
    if not legacy_path.exists():
        return {}

    with legacy_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if not isinstance(raw, dict):
        return {}
    return raw


def _get_legacy_results_path() -> Path:
    bench_root = Path(frappe.get_app_path("erpnext")).resolve().parents[2]
    return bench_root.joinpath(*LEGACY_RESULTS_PATH)


def _build_item_lookup(legacy_keys) -> dict[str, str]:
    keys = [str(key).strip() for key in legacy_keys if str(key).strip()]
    if not keys:
        return {}

    lookup = {}
    by_code = frappe.get_all(
        "Item",
        filters={"item_code": ["in", keys]},
        fields=["item_code"],
        limit_page_length=0,
    )
    for row in by_code:
        lookup[row.item_code] = row.item_code

    missing_names = [key for key in keys if key not in lookup]
    if not missing_names:
        return lookup

    by_name = frappe.get_all(
        "Item",
        filters={"item_name": ["in", missing_names]},
        fields=["item_code", "item_name"],
        order_by="modified desc",
        limit_page_length=0,
    )
    for row in by_name:
        lookup.setdefault(row.item_name, row.item_code)

    for key in keys:
        if key in lookup:
            continue
        fuzzy_match = _find_item_code_by_fuzzy_name(key)
        if fuzzy_match:
            lookup[key] = fuzzy_match

    return lookup


def _resolve_legacy_key(payload: dict[str, list[dict]], item_code: str, item_name: str | None) -> str | None:
    if item_code in payload:
        return item_code
    normalized_name = cstr(item_name).strip()
    if normalized_name in payload:
        return normalized_name
    item_name_norm = _normalize_text(normalized_name)
    for legacy_key in payload:
        legacy_norm = _normalize_text(legacy_key)
        if not legacy_norm:
            continue
        if item_name_norm.startswith(legacy_norm) or legacy_norm.startswith(item_name_norm):
            return legacy_key
        if legacy_norm in item_name_norm:
            return legacy_key
    return None


def _find_item_code_by_fuzzy_name(legacy_key: str) -> str | None:
    normalized_legacy = _normalize_text(legacy_key)
    if not normalized_legacy:
        return None

    for pattern in (f"{legacy_key}%", f"%{legacy_key}%"):
        candidates = frappe.get_all(
            "Item",
            filters={"item_name": ["like", pattern]},
            fields=["item_code", "item_name"],
            order_by="LENGTH(item_name) asc, modified desc",
            limit_page_length=20,
        )
        best = _pick_best_fuzzy_match(normalized_legacy, candidates)
        if best:
            return best

    return None


def _pick_best_fuzzy_match(normalized_legacy: str, candidates) -> str | None:
    best_item_code = None
    best_score = None
    for row in candidates:
        normalized_item_name = _normalize_text(row.item_name)
        if normalized_item_name == normalized_legacy:
            return row.item_code
        if not (
            normalized_item_name.startswith(normalized_legacy)
            or normalized_legacy in normalized_item_name
        ):
            continue
        score = len(normalized_item_name) - len(normalized_legacy)
        if best_score is None or score < best_score:
            best_score = score
            best_item_code = row.item_code
    return best_item_code


def _normalize_text(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", cstr(value)).encode("ascii", "ignore").decode("ascii")
    collapsed = re.sub(r"[^A-Z0-9]+", " ", ascii_text.upper())
    return " ".join(collapsed.split())


def _normalize_candidates(raw_candidates) -> list[dict]:
    if not isinstance(raw_candidates, list):
        return []

    normalized = []
    seen_urls = set()
    for index, row in enumerate(raw_candidates, start=1):
        if not isinstance(row, dict):
            continue
        image_url = cstr(row.get("url") or row.get("image_url")).strip()
        if not image_url or image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        normalized.append(
            {
                "rank": cint(row.get("rank")) or index,
                "image_url": image_url,
                "thumbnail_url": cstr(row.get("thumbnail") or row.get("thumbnail_url")).strip() or None,
                "width": cint(row.get("width")) or None,
                "height": cint(row.get("height")) or None,
                "quality_score": flt(row.get("score") or row.get("quality_score") or 0),
                "metadata": {
                    "legacy_quality": row.get("quality"),
                    "legacy_title": row.get("title"),
                    "legacy_source_page": row.get("source"),
                },
            }
        )
    return normalized


def _insert_candidate(item_code: str, legacy_key: str, candidate: dict) -> None:
    doc = frappe.get_doc(
        {
            "doctype": "Product Image Candidate",
            "product_type": "Item",
            "product_id": item_code,
            "rank": candidate["rank"],
            "image_url": candidate["image_url"],
            "thumbnail_url": candidate["thumbnail_url"],
            "width": candidate["width"],
            "height": candidate["height"],
            "quality_score": candidate["quality_score"],
            "source": "Bing Images",
            "metadata": json.dumps(
                {
                    **candidate["metadata"],
                    "legacy_import": True,
                    "legacy_lookup_key": legacy_key,
                }
            ),
        }
    )
    doc.insert(ignore_permissions=True)


def cint(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def flt(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cstr(value) -> str:
    if value is None:
        return ""
    return str(value)