"""Geometric containment for ECommerce Floor Map sections (zone / fila / rack).

Parents are derived from AABB center-in tests — no stored parent links.
Mirrors erpnext-ecommerce/lib/floor-map-containment.ts.
"""

from __future__ import annotations

import json
from typing import Any

FAR = 1e12


def _as_float(v, default=0.0) -> float:
	try:
		if v is None or v == "":
			return float(default)
		return float(v)
	except (TypeError, ValueError):
		return float(default)


def section_kind(section: dict) -> str:
	"""Resolve kind from products._meta.kind or top-level kind (default rack)."""
	if not isinstance(section, dict):
		return "rack"
	raw = section.get("kind")
	if raw in ("rack", "fila", "zone"):
		return raw
	products = section.get("products")
	if isinstance(products, dict):
		meta = products.get("_meta") or {}
		if isinstance(meta, dict):
			k = meta.get("kind")
			if k in ("rack", "fila", "zone"):
				return k
	return "rack"


def section_skus(section: dict) -> list[str]:
	"""Extract SKU list from legacy list or v2 products.{rows,skus}."""
	if not isinstance(section, dict):
		return []
	out: list[str] = []
	products = section.get("products")
	if isinstance(products, list):
		for sku in products:
			s = str(sku or "").strip()
			if s and s not in out:
				out.append(s)
	elif isinstance(products, dict):
		for sku in products.get("skus") or []:
			s = str(sku or "").strip()
			if s and s not in out:
				out.append(s)
		for row in products.get("rows") or []:
			if isinstance(row, dict):
				s = str(row.get("sku") or "").strip()
				if s and s not in out:
					out.append(s)
	for row in section.get("productRows") or []:
		if isinstance(row, dict):
			s = str(row.get("sku") or "").strip()
			if s and s not in out:
				out.append(s)
	return out


def _label(section: dict) -> str:
	return str(section.get("code") or section.get("name") or section.get("id") or "").strip()


def _area(section: dict) -> float:
	return max(0.0, _as_float(section.get("width"))) * max(0.0, _as_float(section.get("height")))


def _center(section: dict) -> tuple[float, float]:
	x = _as_float(section.get("x"))
	y = _as_float(section.get("y"))
	w = _as_float(section.get("width"))
	h = _as_float(section.get("height"))
	return x + w / 2.0, y + h / 2.0


def center_in(container: dict, child: dict) -> bool:
	cx, cy = _center(child)
	x = _as_float(container.get("x"))
	y = _as_float(container.get("y"))
	w = _as_float(container.get("width"))
	h = _as_float(container.get("height"))
	return x <= cx <= x + w and y <= cy <= y + h


def _pick_smallest(candidates: list[dict]) -> dict | None:
	if not candidates:
		return None
	return min(candidates, key=_area)


def format_location_path(zone=None, fila=None, rack=None) -> str:
	parts = [str(p).strip() for p in (zone, fila, rack) if p and str(p).strip()]
	return " › ".join(parts)


def provisional_path_string(zone=None, fila=None, rack=None) -> str:
	parts = [str(p).strip() for p in (zone, fila, rack) if p and str(p).strip()]
	return "/".join(parts)


def parse_provisional_path(raw: str | None) -> dict[str, str]:
	text = str(raw or "").strip()
	if not text:
		return {}
	if " › " in text:
		tokens = [t.strip() for t in text.split(" › ") if t.strip()]
	elif "/" in text:
		tokens = [t.strip() for t in text.split("/") if t.strip()]
	else:
		tokens = [text]
	out: dict[str, str] = {}
	for tok in tokens:
		u = tok.upper()
		if u.startswith("Z") and "zone" not in out:
			out["zone"] = tok
		elif u.startswith("F") and "fila" not in out:
			out["fila"] = tok
		elif u.startswith("R") and "rack" not in out:
			out["rack"] = tok
		elif "zone" not in out:
			out["zone"] = tok
		elif "fila" not in out:
			out["fila"] = tok
		elif "rack" not in out:
			out["rack"] = tok
	return out


def build_containment_index(sections_raw: list | None) -> dict[str, Any]:
	sections = [s for s in (sections_raw or []) if isinstance(s, dict) and s.get("id")]
	by_id = {s["id"]: s for s in sections}
	kinds = {s["id"]: section_kind(s) for s in sections}
	zones = [s for s in sections if kinds[s["id"]] == "zone"]
	filas = [s for s in sections if kinds[s["id"]] == "fila"]
	racks = [s for s in sections if kinds[s["id"]] == "rack"]

	parents_by_rack: dict[str, dict] = {}
	racks_in_zone: dict[str, list[str]] = {z["id"]: [] for z in zones}
	racks_in_fila: dict[str, list[str]] = {f["id"]: [] for f in filas}
	zone_of_fila: dict[str, str | None] = {}

	for rack in racks:
		zone = _pick_smallest([z for z in zones if center_in(z, rack)])
		fila = _pick_smallest([f for f in filas if center_in(f, rack)])
		parents_by_rack[rack["id"]] = {
			"zone_id": zone["id"] if zone else None,
			"fila_id": fila["id"] if fila else None,
			"zone": zone,
			"fila": fila,
		}
		if zone:
			racks_in_zone[zone["id"]].append(rack["id"])
		if fila:
			racks_in_fila[fila["id"]].append(rack["id"])

	for fila in filas:
		by_center = _pick_smallest([z for z in zones if center_in(z, fila)])
		if by_center:
			zone_of_fila[fila["id"]] = by_center["id"]
			continue
		votes: dict[str, int] = {}
		for rid in racks_in_fila.get(fila["id"]) or []:
			zid = (parents_by_rack.get(rid) or {}).get("zone_id")
			if zid:
				votes[zid] = votes.get(zid, 0) + 1
		best = None
		best_n = 0
		for zid, n in votes.items():
			if n > best_n:
				best = zid
				best_n = n
		zone_of_fila[fila["id"]] = best

	def parents_of(section_id: str) -> dict:
		s = by_id.get(section_id)
		if not s:
			return {"zone_id": None, "fila_id": None, "zone": None, "fila": None}
		kind = kinds.get(section_id, "rack")
		if kind == "rack":
			return parents_by_rack.get(section_id) or {
				"zone_id": None,
				"fila_id": None,
				"zone": None,
				"fila": None,
			}
		if kind == "fila":
			zid = zone_of_fila.get(section_id)
			return {
				"zone_id": zid,
				"fila_id": section_id,
				"zone": by_id.get(zid) if zid else None,
				"fila": s,
			}
		if kind == "zone":
			return {"zone_id": section_id, "fila_id": None, "zone": s, "fila": None}
		return {"zone_id": None, "fila_id": None, "zone": None, "fila": None}

	def children_of(section_id: str) -> list[dict]:
		kind = kinds.get(section_id)
		ids: list[str] = []
		if kind == "zone":
			ids = racks_in_zone.get(section_id) or []
		elif kind == "fila":
			ids = racks_in_fila.get(section_id) or []
		return [by_id[i] for i in ids if i in by_id]

	def path_codes(section_id: str) -> dict[str, str]:
		s = by_id.get(section_id)
		if not s:
			return {}
		p = parents_of(section_id)
		kind = kinds.get(section_id, "rack")
		out: dict[str, str] = {}
		if p.get("zone"):
			out["zone"] = _label(p["zone"])
		elif kind == "zone":
			out["zone"] = _label(s)
		if p.get("fila"):
			out["fila"] = _label(p["fila"])
		elif kind == "fila":
			out["fila"] = _label(s)
		if kind == "rack":
			out["rack"] = _label(s)
		return out

	def path_of(section_id: str) -> str:
		c = path_codes(section_id)
		return format_location_path(c.get("zone"), c.get("fila"), c.get("rack"))

	def sort_key(section_id: str | None) -> tuple:
		if not section_id or section_id not in by_id:
			return (FAR, FAR, FAR, FAR, FAR, FAR)
		s = by_id[section_id]
		p = parents_of(section_id)
		kind = kinds.get(section_id, "rack")
		zone = p.get("zone") or (s if kind == "zone" else None)
		fila = p.get("fila") or (s if kind == "fila" else None)
		rack = s if kind == "rack" else None
		zx = _as_float(zone.get("x")) if zone else FAR
		zy = _as_float(zone.get("y")) if zone else FAR
		fy = _as_float(fila.get("y")) if fila else FAR
		fx = _as_float(fila.get("x")) if fila else FAR
		rx = _as_float(rack.get("x")) if rack else _as_float(s.get("x"))
		ry = _as_float(rack.get("y")) if rack else _as_float(s.get("y"))
		return (zx, zy, fy, fx, rx, ry)

	def find_by_code(code: str | None, kind: str) -> dict | None:
		if not code:
			return None
		u = str(code).strip().upper()
		for s in sections:
			if kinds.get(s["id"]) != kind:
				continue
			if _label(s).upper() == u or str(s.get("id")) == code:
				return s
		return None

	def sort_key_for_provisional(path: str) -> tuple:
		parsed = parse_provisional_path(path)
		rack = find_by_code(parsed.get("rack"), "rack")
		if rack:
			return sort_key(rack["id"])
		fila = find_by_code(parsed.get("fila"), "fila")
		if fila:
			return sort_key(fila["id"])
		zone = find_by_code(parsed.get("zone"), "zone")
		if zone:
			return sort_key(zone["id"])
		return (FAR, FAR, FAR, FAR, FAR, FAR)

	def sku_locations() -> dict[str, dict]:
		"""Map item_code → {section_id, location, location_path, sort_key}."""
		out: dict[str, dict] = {}
		for rack in racks:
			sid = rack["id"]
			path = path_of(sid)
			code = _label(rack)
			sk = sort_key(sid)
			for sku in section_skus(rack):
				if sku not in out:
					out[sku] = {
						"section_id": sid,
						"location": code,
						"location_path": path,
						"sort_key": sk,
					}
		return out

	return {
		"sections": sections,
		"by_id": by_id,
		"kinds": kinds,
		"racks_in_zone": racks_in_zone,
		"racks_in_fila": racks_in_fila,
		"parents_by_rack": parents_by_rack,
		"zone_of_fila": zone_of_fila,
		"parents_of": parents_of,
		"children_of": children_of,
		"path_of": path_of,
		"path_codes": path_codes,
		"sort_key": sort_key,
		"sort_key_for_provisional": sort_key_for_provisional,
		"sku_locations": sku_locations,
		"find_by_code": find_by_code,
	}


def load_sections_json(raw) -> list:
	if isinstance(raw, list):
		return raw
	if isinstance(raw, str):
		try:
			data = json.loads(raw or "[]")
			return data if isinstance(data, list) else []
		except Exception:
			return []
	return []
