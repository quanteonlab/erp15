# Copyright (c) 2026, Frappe Technologies and contributors
# Robust unit-weight catalog (Median/MAD) + Item Weight Observation store.

from __future__ import unicode_literals

import csv
import io
import math
from collections import defaultdict
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, now_datetime

# Consistency factor: MAD → approx. std-dev for normal data.
_MAD_SCALE = 1.4826
_MAD_Z_OUTLIER = 6.0
_MIN_CLEAN_N = 5
_FIXED_PACK_REL_BAND = 0.05  # ±5% when MAD≈0
_RATIO_ABSURD_HI = 10.0
_RATIO_ABSURD_LO = 0.1


def _median(values):
	vals = sorted(flt(v) for v in (values or []) if v is not None)
	n = len(vals)
	if n == 0:
		return None
	mid = n // 2
	if n % 2:
		return vals[mid]
	return (vals[mid - 1] + vals[mid]) / 2.0


def _mad(values, center=None):
	vals = [flt(v) for v in (values or []) if v is not None]
	if not vals:
		return 0.0
	med = flt(center) if center is not None else _median(vals)
	return _median([abs(v - med) for v in vals]) or 0.0


def _percentile(values, p):
	vals = sorted(flt(v) for v in (values or []) if v is not None)
	n = len(vals)
	if n == 0:
		return None
	if n == 1:
		return vals[0]
	p = max(0.0, min(100.0, flt(p)))
	idx = (n - 1) * (p / 100.0)
	lo = int(math.floor(idx))
	hi = int(math.ceil(idx))
	if lo == hi:
		return vals[lo]
	w = idx - lo
	return vals[lo] * (1.0 - w) + vals[hi] * w


def compute_unit_weight_stats(ratios):
	"""Median/MAD catalog stats from a list of kg-per-unit ratios.

	Returns dict with weight_per_unit, unit_weight_min, unit_weight_max, mad,
	n_raw, n_clean, n_outliers — or None if too few clean points.
	"""
	raw = [flt(r) for r in (ratios or []) if r is not None]
	positive = [r for r in raw if r > 0]
	n_raw = len(raw)
	if len(positive) < 1:
		return {
			"weight_per_unit": None,
			"unit_weight_min": None,
			"unit_weight_max": None,
			"mad": 0.0,
			"n_raw": n_raw,
			"n_clean": 0,
			"n_outliers": n_raw,
			"ok": False,
			"reason": "no_positive",
		}

	prov_med = _median(positive)
	prov_mad = _mad(positive, prov_med)
	clean = []
	n_outliers = 0
	for r in positive:
		absurd = False
		if prov_med and prov_med > 0:
			if r > prov_med * _RATIO_ABSURD_HI or r < prov_med * _RATIO_ABSURD_LO:
				absurd = True
		if not absurd and prov_mad > 1e-12:
			z = abs(r - prov_med) / (_MAD_SCALE * prov_mad)
			if z > _MAD_Z_OUTLIER:
				absurd = True
		if absurd:
			n_outliers += 1
		else:
			clean.append(r)

	# Also count non-positive as outliers for reporting.
	n_outliers += n_raw - len(positive)

	if len(clean) < _MIN_CLEAN_N:
		return {
			"weight_per_unit": None,
			"unit_weight_min": None,
			"unit_weight_max": None,
			"mad": prov_mad,
			"n_raw": n_raw,
			"n_clean": len(clean),
			"n_outliers": n_outliers,
			"ok": False,
			"reason": "insufficient_clean",
		}

	med = _median(clean)
	mad = _mad(clean, med)
	if mad < 1e-12:
		# Fixed pack — small relative band so validation still works.
		span = max(abs(med) * _FIXED_PACK_REL_BAND, 1e-6)
		wmin = max(0.0, med - span)
		wmax = med + span
	else:
		half = 3.0 * _MAD_SCALE * mad
		wmin = max(0.0, med - half)
		wmax = med + half
		# If band collapses oddly, fall back to robust percentiles.
		if wmax <= wmin:
			wmin = max(0.0, _percentile(clean, 10) or 0.0)
			wmax = _percentile(clean, 90) or med

	wmin = min(wmin, med)
	wmax = max(wmax, med)
	return {
		"weight_per_unit": med,
		"unit_weight_min": wmin,
		"unit_weight_max": wmax,
		"mad": mad,
		"n_raw": n_raw,
		"n_clean": len(clean),
		"n_outliers": n_outliers,
		"ok": True,
		"reason": None,
	}


def expected_weight_band(item_code, units=1):
	"""Return expected total kg band for ``units`` pieces from Item catalog."""
	units = flt(units)
	if units <= 0:
		return {
			"item_code": item_code,
			"units": units,
			"expected": None,
			"lo": None,
			"hi": None,
			"ok": False,
			"reason": "units_must_be_positive",
		}
	code = cstr(item_code or "").strip()
	if not code or not frappe.db.exists("Item", code):
		return {
			"item_code": code,
			"units": units,
			"expected": None,
			"lo": None,
			"hi": None,
			"ok": False,
			"reason": "item_not_found",
		}

	fields = ["weight_per_unit"]
	if frappe.db.has_column("Item", "custom_unit_weight_min"):
		fields.append("custom_unit_weight_min")
	if frappe.db.has_column("Item", "custom_unit_weight_max"):
		fields.append("custom_unit_weight_max")
	row = frappe.db.get_value("Item", code, fields, as_dict=True) or {}
	wpu = flt(row.get("weight_per_unit") or 0)
	wmin_raw = row.get("custom_unit_weight_min")
	wmax_raw = row.get("custom_unit_weight_max")
	wmin = flt(wmin_raw) if wmin_raw not in (None, "") else None
	wmax = flt(wmax_raw) if wmax_raw not in (None, "") else None
	# DB Float defaults are often 0 — treat 0/0 as "unset band".
	if wmin is not None and wmax is not None and wmin <= 0 and wmax <= 0:
		wmin, wmax = None, None
	if wpu <= 0 and wmin is None and wmax is None:
		return {
			"item_code": code,
			"units": units,
			"expected": None,
			"lo": None,
			"hi": None,
			"ok": False,
			"reason": "no_catalog_weights",
		}
	expected = wpu * units if wpu > 0 else None
	lo = (wmin * units) if wmin is not None else None
	hi = (wmax * units) if wmax is not None else None
	return {
		"item_code": code,
		"units": units,
		"expected": expected,
		"lo": lo,
		"hi": hi,
		"weight_per_unit": wpu or None,
		"unit_weight_min": wmin,
		"unit_weight_max": wmax,
		"ok": True,
		"reason": None,
	}


def validate_total_weight(item_code, units, total_weight):
	"""Soft check: is total_weight inside units×band?

	Never throws — returns {ok, in_band, warning, band, is_outlier}.
	"""
	units = flt(units)
	total = flt(total_weight)
	band = expected_weight_band(item_code, units)
	if not band.get("ok"):
		return {
			"ok": True,
			"in_band": True,
			"is_outlier": False,
			"warning": None,
			"band": band,
			"total_weight": total,
			"kg_per_unit": (total / units) if units > 0 else None,
		}
	lo, hi = band.get("lo"), band.get("hi")
	in_band = True
	if lo is not None and total + 1e-9 < lo:
		in_band = False
	if hi is not None and total - 1e-9 > hi:
		in_band = False
	warning = None
	if not in_band:
		lo_s = f"{lo:.3f}" if lo is not None else "?"
		hi_s = f"{hi:.3f}" if hi is not None else "?"
		warning = _(
			"Usual weight for {0} × {1:g} units is {2}–{3} kg (got {4:g} kg)."
		).format(cstr(item_code), units, lo_s, hi_s, total)
	return {
		"ok": True,
		"in_band": in_band,
		"is_outlier": not in_band,
		"warning": warning,
		"band": band,
		"total_weight": total,
		"kg_per_unit": (total / units) if units > 0 else None,
	}


def _parse_daily_csv(csv_text):
	"""Parse FECHA,IDART,CODIGO,ARTICULO,CANTIDAD,UNIDADES → list of row dicts."""
	if csv_text is None:
		return []
	if not isinstance(csv_text, str):
		csv_text = str(csv_text)
	csv_text = csv_text.lstrip("\ufeff").strip()
	if not csv_text:
		return []

	sample = csv_text[:2048]
	try:
		dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
	except Exception:
		dialect = csv.excel
	reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
	if not reader.fieldnames:
		return []

	# Normalize headers
	def _key(h):
		return cstr(h or "").strip().upper()

	field_map = {}
	for h in reader.fieldnames:
		k = _key(h)
		if k in ("FECHA", "DATE"):
			field_map["fecha"] = h
		elif k in ("CODIGO", "ITEM_CODE", "SKU", "CODE"):
			field_map["codigo"] = h
		elif k in ("CANTIDAD", "QTY_KG", "KG", "PESO"):
			field_map["cantidad"] = h
		elif k in ("UNIDADES", "UNITS", "PIEZAS"):
			field_map["unidades"] = h
		elif k in ("ARTICULO", "ITEM_NAME", "NAME"):
			field_map["articulo"] = h

	out = []
	for raw in reader:
		if not isinstance(raw, dict):
			continue
		codigo = cstr(raw.get(field_map.get("codigo")) or "").strip().strip('"')
		if not codigo or codigo.lower() in ("null", "undefined", "none"):
			continue
		try:
			cantidad = flt(str(raw.get(field_map.get("cantidad")) or "0").replace(",", "."))
			unidades = flt(str(raw.get(field_map.get("unidades")) or "0").replace(",", "."))
		except Exception:
			continue
		fecha_raw = cstr(raw.get(field_map.get("fecha")) or "").strip()
		articulo = cstr(raw.get(field_map.get("articulo")) or "").strip().strip('"')
		out.append(
			{
				"fecha": fecha_raw,
				"codigo": codigo,
				"articulo": articulo,
				"cantidad": cantidad,
				"unidades": unidades,
			}
		)
	return out


def _ratios_by_codigo(rows):
	by = defaultdict(list)
	for r in rows or []:
		u = flt(r.get("unidades"))
		c = flt(r.get("cantidad"))
		if u <= 0:
			continue
		ratio = c / u
		by[cstr(r.get("codigo"))].append(ratio)
	return by


def _ensure_item_weight_columns():
	"""Best-effort ensure custom min/max columns exist (product_manager installer)."""
	try:
		from erpnext.erpnext_integrations.ecommerce_api.product_manager import (
			ensure_product_manager_custom_fields,
		)

		ensure_product_manager_custom_fields()
	except Exception:
		pass


def _apply_stats_to_item(item_code, stats, dry_run=False):
	if not stats or not stats.get("ok"):
		return False
	if not frappe.db.exists("Item", item_code):
		return False
	if dry_run:
		return True
	updates = {"weight_per_unit": flt(stats["weight_per_unit"])}
	if frappe.db.has_column("Item", "custom_unit_weight_min"):
		updates["custom_unit_weight_min"] = flt(stats["unit_weight_min"])
	if frappe.db.has_column("Item", "custom_unit_weight_max"):
		updates["custom_unit_weight_max"] = flt(stats["unit_weight_max"])
	frappe.db.set_value("Item", item_code, updates, update_modified=True)
	return True


@frappe.whitelist(allow_guest=True)
def import_unit_weight_ranges_from_daily_csv(csv_text=None, dry_run=0, only_weight_like=1):
	"""Seed Item weight_per_unit / min / max from daily CANTIDAD/UNIDADES CSV.

	Uses Median/MAD (not RSS). ``only_weight_like`` (default on) seeds SKUs where
	CANTIDAD often differs from UNIDADES (≥30% of rows).
	"""
	if csv_text is None or cstr(csv_text).strip() in ("", "null", "undefined", "None"):
		return {
			"ok": True,
			"seeded": [],
			"skipped": [],
			"unmatched": [],
			"parsed_rows": 0,
			"dry_run": cint(dry_run),
			"message": "empty csv",
		}

	_ensure_item_weight_columns()
	rows = _parse_daily_csv(csv_text)
	by = _ratios_by_codigo(rows)
	weight_like_only = cint(only_weight_like) != 0

	seeded = []
	skipped = []
	unmatched = []

	for codigo, ratios in by.items():
		# Weight-like: CANTIDAD ≠ UNIDADES often (ratio ≠ 1).
		if weight_like_only:
			diffs = sum(1 for r in ratios if abs(r - 1.0) > 1e-6)
			if len(ratios) and (diffs / len(ratios)) < 0.3:
				skipped.append(
					{"item_code": codigo, "reason": "not_weight_like", "n_raw": len(ratios)}
				)
				continue

		stats = compute_unit_weight_stats(ratios)
		if not stats.get("ok"):
			skipped.append(
				{
					"item_code": codigo,
					"reason": stats.get("reason") or "insufficient",
					"n_raw": stats.get("n_raw"),
					"n_clean": stats.get("n_clean"),
					"n_outliers": stats.get("n_outliers"),
				}
			)
			continue

		if not frappe.db.exists("Item", codigo):
			unmatched.append({"item_code": codigo, "stats": stats})
			continue

		_apply_stats_to_item(codigo, stats, dry_run=cint(dry_run))
		seeded.append(
			{
				"item_code": codigo,
				"weight_per_unit": stats["weight_per_unit"],
				"unit_weight_min": stats["unit_weight_min"],
				"unit_weight_max": stats["unit_weight_max"],
				"n_clean": stats["n_clean"],
				"n_outliers": stats["n_outliers"],
			}
		)

	if not cint(dry_run) and seeded:
		frappe.db.commit()

	return {
		"ok": True,
		"seeded": seeded,
		"skipped": skipped,
		"unmatched": unmatched,
		"parsed_rows": len(rows),
		"sku_count": len(by),
		"dry_run": cint(dry_run),
	}


def ensure_item_weight_observation_doctype():
	"""Reload DocType if present; no-op when JSON not yet migrated."""
	if frappe.db.exists("DocType", "Item Weight Observation"):
		try:
			frappe.reload_doc(
				"erpnext_integrations",
				"doctype",
				"item_weight_observation",
				force=True,
			)
		except Exception:
			pass
		return True
	# Attempt sync from app files (post-deploy before migrate sometimes needed).
	try:
		from frappe.modules.import_file import import_file_by_path
		import os

		path = os.path.join(
			frappe.get_app_path("erpnext"),
			"erpnext_integrations",
			"doctype",
			"item_weight_observation",
			"item_weight_observation.json",
		)
		if os.path.exists(path):
			import_file_by_path(path, force=True, ignore_version=True)
			frappe.clear_cache(doctype="Item Weight Observation")
			return frappe.db.exists("DocType", "Item Weight Observation")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ensure_item_weight_observation_doctype")
	return frappe.db.exists("DocType", "Item Weight Observation")


@frappe.whitelist(allow_guest=True)
def record_item_weight_observation(
	item_code=None,
	units=None,
	total_weight=None,
	source=None,
	reference_doctype=None,
	reference_name=None,
	company=None,
	observed_at=None,
	force_outlier=None,
):
	"""Insert one weigh observation; soft-validates against catalog band."""
	if not ensure_item_weight_observation_doctype():
		frappe.throw(_("Item Weight Observation DocType is not installed (run bench migrate)."))

	code = cstr(item_code or "").strip()
	if not code or code.lower() in ("null", "undefined", "none"):
		frappe.throw(_("Item code is required."))
	if not frappe.db.exists("Item", code):
		frappe.throw(_("Item {0} not found").format(code))

	units_f = flt(units)
	total_f = flt(total_weight)
	if units_f <= 0:
		frappe.throw(_("Units must be positive."))
	if total_f < 0:
		frappe.throw(_("Total weight cannot be negative."))

	src = cstr(source or "manual").strip().lower() or "manual"
	allowed = {"pos", "receiving", "armado", "import", "manual"}
	if src not in allowed:
		src = "manual"

	check = validate_total_weight(code, units_f, total_f)
	is_outlier = bool(cint(force_outlier)) or bool(check.get("is_outlier"))

	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	comp = cstr(company or "").strip() or resolve_company() or None
	obs_at = observed_at
	try:
		obs_at = get_datetime(observed_at) if observed_at else now_datetime()
	except Exception:
		obs_at = now_datetime()

	kg_u = total_f / units_f if units_f else None
	doc = frappe.get_doc(
		{
			"doctype": "Item Weight Observation",
			"item_code": code,
			"observed_at": obs_at,
			"units": units_f,
			"total_weight": total_f,
			"kg_per_unit": kg_u,
			"source": src,
			"reference_doctype": cstr(reference_doctype or "").strip() or None,
			"reference_name": cstr(reference_name or "").strip() or None,
			"is_outlier": 1 if is_outlier else 0,
			"company": comp,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {
		"ok": True,
		"name": doc.name,
		"is_outlier": is_outlier,
		"warning": check.get("warning"),
		"in_band": check.get("in_band"),
		"band": check.get("band"),
		"kg_per_unit": kg_u,
	}


@frappe.whitelist(allow_guest=True)
def import_weight_observations_from_daily_csv(csv_text=None, company=None, dry_run=0):
	"""Import daily aggregates as Item Weight Observation rows (source=import)."""
	if not ensure_item_weight_observation_doctype():
		frappe.throw(_("Item Weight Observation DocType is not installed (run bench migrate)."))

	if csv_text is None or cstr(csv_text).strip() in ("", "null", "undefined", "None"):
		return {"ok": True, "created": 0, "skipped": 0, "parsed_rows": 0, "dry_run": cint(dry_run)}

	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	comp = cstr(company or "").strip() or resolve_company() or None
	rows = _parse_daily_csv(csv_text)
	created = 0
	skipped = 0
	for r in rows:
		codigo = r["codigo"]
		u = flt(r["unidades"])
		c = flt(r["cantidad"])
		if u <= 0 or c < 0:
			skipped += 1
			continue
		if not frappe.db.exists("Item", codigo):
			skipped += 1
			continue
		if cint(dry_run):
			created += 1
			continue
		check = validate_total_weight(codigo, u, c)
		obs_at = now_datetime()
		fecha = cstr(r.get("fecha") or "").strip()
		if fecha:
			try:
				obs_at = datetime.combine(getdate(fecha), datetime.min.time())
			except Exception:
				pass
		doc = frappe.get_doc(
			{
				"doctype": "Item Weight Observation",
				"item_code": codigo,
				"observed_at": obs_at,
				"units": u,
				"total_weight": c,
				"kg_per_unit": c / u,
				"source": "import",
				"is_outlier": 1 if check.get("is_outlier") else 0,
				"company": comp,
			}
		)
		doc.insert(ignore_permissions=True)
		created += 1

	if not cint(dry_run) and created:
		frappe.db.commit()
	return {
		"ok": True,
		"created": created,
		"skipped": skipped,
		"parsed_rows": len(rows),
		"dry_run": cint(dry_run),
	}


@frappe.whitelist(allow_guest=True)
def recompute_item_weight_catalog(item_code=None, lookback_days=180, dry_run=0):
	"""Recompute Item weight fields from Item Weight Observation (Median/MAD)."""
	if not ensure_item_weight_observation_doctype():
		frappe.throw(_("Item Weight Observation DocType is not installed (run bench migrate)."))

	_ensure_item_weight_columns()
	days = cint(lookback_days) if lookback_days not in (None, "", "null") else 180
	if days < 1:
		days = 1
	cutoff = frappe.utils.add_days(now_datetime(), -days)

	filters = {
		"observed_at": [">=", cutoff],
		"is_outlier": 0,
		"units": [">", 0],
	}
	code = cstr(item_code or "").strip()
	if code and code.lower() not in ("null", "undefined", "none"):
		filters["item_code"] = code

	rows = frappe.get_all(
		"Item Weight Observation",
		filters=filters,
		fields=["item_code", "units", "total_weight", "kg_per_unit"],
		limit_page_length=50000,
		ignore_permissions=True,
	)
	by = defaultdict(list)
	for r in rows:
		u = flt(r.units)
		if u <= 0:
			continue
		kg_u = flt(r.kg_per_unit) if r.kg_per_unit not in (None, "") else flt(r.total_weight) / u
		if kg_u > 0:
			by[cstr(r.item_code)].append(kg_u)

	updated = []
	skipped = []
	for codigo, ratios in by.items():
		stats = compute_unit_weight_stats(ratios)
		if not stats.get("ok"):
			skipped.append({"item_code": codigo, "reason": stats.get("reason"), "n_clean": stats.get("n_clean")})
			continue
		if not frappe.db.exists("Item", codigo):
			skipped.append({"item_code": codigo, "reason": "item_not_found"})
			continue
		_apply_stats_to_item(codigo, stats, dry_run=cint(dry_run))
		updated.append(
			{
				"item_code": codigo,
				"weight_per_unit": stats["weight_per_unit"],
				"unit_weight_min": stats["unit_weight_min"],
				"unit_weight_max": stats["unit_weight_max"],
				"n_clean": stats["n_clean"],
				"n_outliers": stats["n_outliers"],
			}
		)

	if not cint(dry_run) and updated:
		frappe.db.commit()

	return {
		"ok": True,
		"updated": updated,
		"skipped": skipped,
		"lookback_days": days,
		"observation_count": len(rows),
		"dry_run": cint(dry_run),
	}


@frappe.whitelist(allow_guest=True)
def get_expected_weight_band(item_code=None, units=None):
	"""Public wrapper for expected_weight_band (dirty-safe)."""
	units_f = 1.0
	if units not in (None, "", "null", "undefined"):
		try:
			units_f = flt(units)
		except Exception:
			units_f = 1.0
	return expected_weight_band(cstr(item_code or "").strip(), units_f)


@frappe.whitelist(allow_guest=True)
def validate_item_total_weight(item_code=None, units=None, total_weight=None):
	"""Public wrapper for validate_total_weight (dirty-safe)."""
	code = cstr(item_code or "").strip()
	try:
		units_f = flt(units) if units not in (None, "", "null", "undefined") else 0
	except Exception:
		units_f = 0
	try:
		total_f = flt(total_weight) if total_weight not in (None, "", "null", "undefined") else 0
	except Exception:
		total_f = 0
	if not code:
		return {
			"ok": True,
			"in_band": True,
			"is_outlier": False,
			"warning": None,
			"band": {"ok": False, "reason": "item_required"},
		}
	return validate_total_weight(code, units_f, total_f)
