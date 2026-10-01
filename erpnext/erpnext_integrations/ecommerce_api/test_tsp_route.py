"""Unit tests for closed-tour TSP stop ordering (warehouse → stops → warehouse)."""

from __future__ import annotations

from erpnext.erpnext_integrations.ecommerce_api import tms_api as tms

DEPOT = {"lat": -34.60, "lng": -58.38}


def _tour_km(depot, points, order):
	dlat, dlng = depot["lat"], depot["lng"]
	total = 0.0
	prev = (dlat, dlng)
	for i in order:
		p = points[i]
		total += tms._haversine_km(prev[0], prev[1], p["lat"], p["lng"])
		prev = (p["lat"], p["lng"])
	total += tms._haversine_km(prev[0], prev[1], dlat, dlng)
	return total


def test_tsp_empty_and_single():
	assert tms._tsp_closed_tour_order(DEPOT, []) == []
	assert tms._tsp_closed_tour_order(DEPOT, [{"lat": -34.61, "lng": -58.39}]) == [0]


def test_tsp_prefers_shorter_closed_tour():
	# Line of points east of depot — NN then 2-opt should visit left-to-right or right-to-left
	# without zig-zag (e.g. 0,2,1 is worse than sorted by lng).
	points = [
		{"lat": -34.60, "lng": -58.30},  # 0 farthest east
		{"lat": -34.60, "lng": -58.36},  # 1 nearest
		{"lat": -34.60, "lng": -58.33},  # 2 middle
	]
	order = tms._tsp_closed_tour_order(DEPOT, points)
	assert set(order) == {0, 1, 2}
	# Optimal closed tours: 1→2→0 or 0→2→1
	assert order in ([1, 2, 0], [0, 2, 1]), order
	# Zig-zag 1→0→2 must be worse
	zig = _tour_km(DEPOT, points, [1, 0, 2])
	opt = _tour_km(DEPOT, points, order)
	assert opt <= zig + 1e-6


def test_tsp_two_opt_beats_raw_nn_when_crossed():
	# Classic cross: A and C close to depot on opposite sides, B far —
	# ensure result is a valid permutation and finite length.
	points = [
		{"lat": -34.55, "lng": -58.38},
		{"lat": -34.70, "lng": -58.20},
		{"lat": -34.65, "lng": -58.38},
		{"lat": -34.58, "lng": -58.50},
	]
	order = tms._tsp_closed_tour_order(DEPOT, points)
	assert sorted(order) == [0, 1, 2, 3]
	assert _tour_km(DEPOT, points, order) > 0


def run():
	tests = [
		test_tsp_empty_and_single,
		test_tsp_prefers_shorter_closed_tour,
		test_tsp_two_opt_beats_raw_nn_when_crossed,
	]
	failed = []
	for fn in tests:
		try:
			fn()
			print(f"PASS  {fn.__name__}")
		except Exception as e:
			failed.append((fn.__name__, str(e)))
			print(f"FAIL  {fn.__name__}: {e}")
	print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
	return {"passed": len(tests) - len(failed), "failed": len(failed), "failures": failed, "total": len(tests)}
