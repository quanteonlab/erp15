"""Unit tests for geometric floor-map containment (no DB).

Run:
  python -m erpnext.erpnext_integrations.ecommerce_api.test_floor_map_containment
or via bench:
  bench --site <site> execute erpnext.erpnext_integrations.ecommerce_api.test_floor_map_containment.run
"""

from __future__ import annotations

from erpnext.erpnext_integrations.ecommerce_api.floor_map_containment import (
	build_containment_index,
	format_location_path,
	parse_provisional_path,
)


def _box(id_, code, kind, x, y, w, h):
	return {
		"id": id_,
		"code": code,
		"name": code,
		"x": x,
		"y": y,
		"width": w,
		"height": h,
		"products": {"_meta": {"version": 2, "kind": kind, "hidden_row_ids": []}, "rows": [], "skus": []},
	}


def fixture_like_screenshot():
	"""Rough geometry matching Z21/Z22 + F23–F27 + R1–R20 from the sections map.

	Z21: R1–R10 (two columns). Z22: R11–R20 as horizontal pairs per fila
	so F23 contains R1, R6, R11, R12.
	"""
	sections = [
		_box("z21", "Z21", "zone", 4, 2, 6, 16),
		_box("z22", "Z22", "zone", 12, 2, 6, 16),
		_box("f23", "F23", "fila", 4, 3, 14, 2),
		_box("f24", "F24", "fila", 4, 5, 14, 2),
		_box("f25", "F25", "fila", 4, 7, 14, 2),
		_box("f26", "F26", "fila", 4, 9, 14, 2),
		_box("f27", "F27", "fila", 4, 11, 14, 2),
	]
	# Z21: left col R1–R5, right col R6–R10
	for i, row_y in enumerate((3.5, 5.5, 7.5, 9.5, 11.5)):
		sections.append(_box(f"r{i + 1}", f"R{i + 1}", "rack", 5, row_y, 1.2, 1.2))
		sections.append(_box(f"r{i + 6}", f"R{i + 6}", "rack", 8, row_y, 1.2, 1.2))
	# Z22: pairs per fila — F23→R11,R12; F24→R13,R14; …
	pair_codes = [(11, 12), (13, 14), (15, 16), (17, 18), (19, 20)]
	for (left, right), row_y in zip(pair_codes, (3.5, 5.5, 7.5, 9.5, 11.5)):
		sections.append(_box(f"r{left}", f"R{left}", "rack", 13, row_y, 1.2, 1.2))
		sections.append(_box(f"r{right}", f"R{right}", "rack", 16, row_y, 1.2, 1.2))
	return sections


def test_zone_contains_racks():
	index = build_containment_index(fixture_like_screenshot())
	z21 = {c["code"] for c in index["children_of"]("z21")}
	z22 = {c["code"] for c in index["children_of"]("z22")}
	assert z21 == {f"R{i}" for i in range(1, 11)}, z21
	assert z22 == {f"R{i}" for i in range(11, 21)}, z22


def test_fila_contains_racks():
	index = build_containment_index(fixture_like_screenshot())
	f23 = {c["code"] for c in index["children_of"]("f23")}
	assert f23 == {"R1", "R6", "R11", "R12"}, f23
	# R1 parents
	p = index["parents_of"]("r1")
	assert p["zone"]["code"] == "Z21"
	assert p["fila"]["code"] == "F23"
	assert index["path_of"]("r1") == "Z21 › F23 › R1"


def test_walk_sort_zone_then_fila_then_rack():
	index = build_containment_index(fixture_like_screenshot())
	# R11 (Z22) should sort after R1 (Z21)
	assert index["sort_key"]("r1") < index["sort_key"]("r11")
	# F23 racks before F24 racks within same zone
	assert index["sort_key"]("r1") < index["sort_key"]("r2")


def test_provisional_parse_and_sort():
	index = build_containment_index(fixture_like_screenshot())
	parsed = parse_provisional_path("Z21/F23")
	assert parsed == {"zone": "Z21", "fila": "F23"}
	assert format_location_path("Z21", "F23", None) == "Z21 › F23"
	sk = index["sort_key_for_provisional"]("Z21/F23")
	assert sk < index["sort_key_for_provisional"]("Z22")
	assert sk < (1e12, 1e12, 1e12, 1e12, 1e12, 1e12)


def run():
	test_zone_contains_racks()
	test_fila_contains_racks()
	test_walk_sort_zone_then_fila_then_rack()
	test_provisional_parse_and_sort()
	return {"ok": True, "tests": 4}


if __name__ == "__main__":
	print(run())
