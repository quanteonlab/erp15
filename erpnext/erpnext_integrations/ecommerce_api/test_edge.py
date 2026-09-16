"""
API edge / dirty-payload kit.

Run:
  ./scripts/test_edge.sh
  ./scripts/test_edge.sh site.local
  bench --site site.local execute erpnext.erpnext_integrations.ecommerce_api.test_edge.run

Modes (kwargs):
  include_catalog=1  (default) — auto-discover every @frappe.whitelist in ecommerce_api
                 and call with null/empty dirty args (expect no crash)
  include_seed=1     (default) — run hand-written cases in test_edge_cases.json
  module=api         — limit catalog sweep to one module stem
  filter_id=...      — single seed case id

Policy: whenever an API bug is fixed, add a regression case to test_edge_cases.json
with source="regression". See .cursor/rules/api-test-kit.mdc.
"""

from __future__ import annotations

import ast
import json
import os
import traceback
from pathlib import Path

import frappe

_PASS = "\033[92m✓ PASS\033[0m"
_FAIL = "\033[91m✗ FAIL\033[0m"
_SKIP = "\033[93m⊘ SKIP\033[0m"

_results: list[tuple[str, str, str | None]] = []

# Destructive / side-effect heavy — skipped in catalog sweep (still cover via smoke/seed).
_SKIP_CATALOG_PREFIXES = (
	"seed_",
	"clear_",
	"delete_",
	"rotate_",
	"import_",
	"export_",
	"materialize_",
	"auto_apply_",
	"notify_",
	"solicitar_",
	"assign_barcodes_to_all",
	"enqueue_",
	"recover_",
	"create_mp_",
	"print_pos_invoice",
	"send_email",
	"send_test_",
)

_SKIP_CATALOG_EXACT = {
	"assign_barcodes_to_all_items",
	"save_afip_settings",
	"import_catalog_csv",
	"import_catalog_csv_products",
	"import_catalog_image_zip",
	"import_catalog_image_batch",
}


def _cases_path() -> Path:
	return Path(__file__).with_name("test_edge_cases.json")


def _api_dir() -> Path:
	return Path(__file__).resolve().parent


def _load_cases() -> list[dict]:
	raw = json.loads(_cases_path().read_text(encoding="utf-8"))
	return list(raw.get("cases") or [])


def _is_controlled_fail(exc: BaseException) -> bool:
	name = type(exc).__name__
	if name in {
		"ValidationError",
		"MandatoryError",
		"PermissionError",
		"AuthenticationError",
		"DoesNotExistError",
		"DuplicateEntryError",
		"LinkValidationError",
		"InvalidStatusError",
		"CharacterLengthExceededError",
		"InvalidQtyError",
		"TimestampMismatchError",
		"UniqueValidationError",
		"DataError",
	}:
		return True
	if name == "TypeError":
		msg = str(exc).lower()
		return "required" in msg or "missing" in msg or "unexpected keyword" in msg or "takes" in msg
	msg = str(exc).lower()
	if any(x in msg for x in ("not found", "required", "invalid", "missing", "mandatory", "disabled", "inactive")):
		return True
	return False


def _is_crash(exc: BaseException) -> bool:
	if isinstance(exc, (AttributeError, KeyError, ZeroDivisionError, IndexError)):
		return True
	if isinstance(exc, TypeError):
		msg = str(exc).lower()
		if "has no attribute" in msg or "unsupported operand" in msg or "not iterable" in msg:
			return True
		if "object is not" in msg and "subscriptable" in msg:
			return True
		if "required" in msg or "missing" in msg or "unexpected keyword" in msg or "takes" in msg:
			return False
		return True
	if isinstance(exc, ValueError) and not _is_controlled_fail(exc):
		return True
	return False


def _call(method: str, args: dict):
	fn = frappe.get_attr(method)
	return fn(**(args or {}))


def _run_one(case: dict) -> None:
	case_id = case.get("id") or "unnamed"
	method = case.get("method")
	expect = (case.get("expect") or "no_500").lower()
	args = case.get("args") if isinstance(case.get("args"), dict) else {}

	if not method:
		_results.append((case_id, "SKIP", "missing method"))
		print(f"  {_SKIP}  {case_id}: missing method")
		return

	try:
		_call(method, args)
		if expect == "fail":
			_results.append((case_id, "FAIL", "expected controlled failure, got success"))
			print(f"  {_FAIL}  {case_id}: expected fail, got ok")
			return
		_results.append((case_id, "PASS", None))
		quiet = case.get("source") == "catalog" and os.environ.get("EDGE_QUIET", "1") != "0"
		if not quiet:
			print(f"  {_PASS}  {case_id}")
		return
	except Exception as exc:
		controlled = _is_controlled_fail(exc) and not _is_crash(exc)
		detail = f"{type(exc).__name__}: {exc}"

		if expect == "ok":
			_results.append((case_id, "FAIL", detail))
			print(f"  {_FAIL}  {case_id}: {detail}")
			return

		if expect == "fail":
			if controlled:
				_results.append((case_id, "PASS", None))
				print(f"  {_PASS}  {case_id} (controlled fail: {type(exc).__name__})")
			else:
				if os.environ.get("EDGE_VERBOSE") == "1":
					detail = traceback.format_exc()
				_results.append((case_id, "FAIL", detail))
				print(f"  {_FAIL}  {case_id}: expected ValidationError-like, got {detail}")
			return

		if controlled:
			_results.append((case_id, "PASS", None))
			quiet = case.get("source") == "catalog" and os.environ.get("EDGE_QUIET", "1") != "0"
			if not quiet:
				print(f"  {_PASS}  {case_id} (no_500 / controlled: {type(exc).__name__})")
			return

		_results.append((case_id, "FAIL", detail))
		print(f"  {_FAIL}  {case_id}: crash/unexpected — {detail}")


def _discover_whitelist(module_filter: str | None = None) -> list[dict]:
	out: list[dict] = []
	for path in sorted(_api_dir().glob("*.py")):
		stem = path.stem
		if stem.startswith("test_") or stem.startswith("seed_") or stem in {"__init__", "utils"}:
			continue
		if module_filter and stem != module_filter:
			continue
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"))
		except SyntaxError:
			continue
		for node in tree.body:
			if not isinstance(node, ast.FunctionDef):
				continue
			is_wl = False
			for d in node.decorator_list:
				if isinstance(d, ast.Attribute) and d.attr == "whitelist":
					is_wl = True
				if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "whitelist":
					is_wl = True
			if not is_wl:
				continue
			params = [a.arg for a in node.args.args if a.arg != "self"]
			skip_reason = None
			if node.name in _SKIP_CATALOG_EXACT or any(node.name.startswith(p) for p in _SKIP_CATALOG_PREFIXES):
				skip_reason = "destructive/side-effect skip"
			out.append(
				{
					"module": stem,
					"name": node.name,
					"params": params,
					"method": f"erpnext.erpnext_integrations.ecommerce_api.{stem}.{node.name}",
					"skip_reason": skip_reason,
				}
			)
	return out


_JSONISH_PARAMS = {
	"filters",
	"fields",
	"data",
	"items",
	"payments",
	"lines",
	"draft_items",
	"events",
	"rows",
	"changes",
	"columns",
	"settings",
	"values",
	"patch",
	"tags",
	"elements_data",
	"notes_data",
	"sections_data",
	"field_layout",
	"stage_requirements",
	"conversion_checklist",
	"action_policy",
	"payload",
	"grids_json",
	"constants_json",
	"images",
	"crop",
}


def _dirty_arg_variants(params: list[str]) -> list[dict]:
	"""
	Dirty shapes clients actually send:
	  #0 all None
	  #1 scalars as "" (JSON-ish params stay None — "" triggers json.loads crash everywhere)
	  #2 list-ish params as []
	"""
	none_args = {p: None for p in params}
	empty_args = {
		p: ("" if p not in _JSONISH_PARAMS else None)
		for p in params
	}
	variants = [none_args, empty_args]
	listish_keys = [p for p in params if p in {"items", "payments", "lines", "draft_items", "events", "rows", "tags", "columns", "filters"}]
	if listish_keys:
		mixed = dict(none_args)
		for k in listish_keys:
			mixed[k] = []
		variants.append(mixed)
	return variants


def _run_catalog(module_filter: str | None = None) -> None:
	entries = _discover_whitelist(module_filter)
	active = [e for e in entries if not e.get("skip_reason")]
	skipped = len(entries) - len(active)
	print(f"\n[Catalog dirty sweep] methods={len(entries)} active={len(active)} skipped_destructive={skipped}")
	print("  (PASS lines suppressed; set EDGE_QUIET=0 to see all)")
	for entry in entries:
		if entry.get("skip_reason"):
			_results.append((f"catalog:{entry['module']}.{entry['name']}", "SKIP", entry["skip_reason"]))
			continue
		for i, args in enumerate(_dirty_arg_variants(entry["params"])):
			case = {
				"id": f"catalog:{entry['module']}.{entry['name']}#dirty{i}",
				"method": entry["method"],
				"args": args,
				"expect": "no_500",
				"source": "catalog",
			}
			_run_one(case)


def _print_summary(catalog_soft: bool = False) -> bool:
	passed = sum(1 for _, s, _ in _results if s == "PASS")
	failed = sum(1 for _, s, _ in _results if s == "FAIL")
	skipped = sum(1 for _, s, _ in _results if s == "SKIP")
	seed_fails = [(i, d) for i, s, d in _results if s == "FAIL" and not str(i).startswith("catalog:")]
	catalog_fails = [(i, d) for i, s, d in _results if s == "FAIL" and str(i).startswith("catalog:")]
	print("\n" + "─" * 60)
	print(f"  test_edge  PASS={passed}  FAIL={failed}  SKIP={skipped}  TOTAL={len(_results)}")
	print(f"  seed_fail={len(seed_fails)}  catalog_fail={len(catalog_fails)}"
		  f"{'  (catalog soft — set EDGE_STRICT=1 to fail CI)' if catalog_soft and catalog_fails else ''}")
	print("─" * 60)
	fails = seed_fails + catalog_fails
	if fails:
		print("  Failures (fix these APIs + add regression cases):")
		for case_id, detail in fails[:100]:
			print(f"  • {case_id}: {detail}")
		if len(fails) > 100:
			print(f"  … +{len(fails) - 100} more")
	# Seed/regression must always be green. Catalog is discovery unless strict.
	if seed_fails:
		return False
	if catalog_fails and not catalog_soft:
		return False
	return True


def run(
	filter_id: str | None = None,
	source: str | None = None,
	include_seed: str | int = "1",
	include_catalog: str | int = "1",
	module: str | None = None,
	strict: str | int | None = None,
):
	"""
	Run seed cases and/or auto catalog dirty sweep.

	Args:
		filter_id: single seed case id
		source: seed|regression filter for seed cases
		include_seed: "1"/"0"
		include_catalog: "1"/"0" — sweep whitelist methods with dirty args
		module: limit catalog to one module stem (e.g. api, product_manager)
		strict: "1" fail process on catalog crashes; default soft (EDGE_STRICT env)
	"""
	import sys

	_results.clear()
	include_seed = str(include_seed) != "0"
	include_catalog = str(include_catalog) != "0"
	if strict is None:
		strict = os.environ.get("EDGE_STRICT", "0")
	catalog_soft = str(strict) == "0"

	print("\n" + "═" * 60)
	print("  test_edge — dirty / null / flexible client payloads")
	print(f"  seed={include_seed}  catalog={include_catalog}  module={module or '*'}  strict={not catalog_soft}")
	print("═" * 60)

	frappe.set_user("Administrator")

	if include_seed:
		cases = _load_cases()
		if filter_id:
			cases = [c for c in cases if c.get("id") == filter_id]
		if source:
			cases = [c for c in cases if (c.get("source") or "") == source]
		print(f"\n[Seed / regression cases] {len(cases)}")
		for case in cases:
			_run_one(case)

	if include_catalog and not filter_id:
		_run_catalog(module)

	ok = _print_summary(catalog_soft=catalog_soft)
	if not ok:
		sys.exit(1)


def list_coverage():
	"""Print whitelist vs seed coverage."""
	entries = _discover_whitelist()
	seed_methods = {c.get("method") for c in _load_cases()}
	print(f"Whitelisted: {len(entries)}")
	print(f"Seed cases:  {len(_load_cases())}")
	covered = sum(1 for e in entries if e["method"] in seed_methods)
	print(f"Seed covers: {covered} methods")
	skipped = sum(1 for e in entries if e.get("skip_reason"))
	print(f"Catalog dirty-sweep: {len(entries) - skipped} (skip destructive: {skipped})")
