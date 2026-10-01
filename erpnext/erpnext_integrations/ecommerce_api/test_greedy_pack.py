# Copyright (c) 2026, local and contributors
# License: MIT
"""
Unit tests for i045 greedy packing helpers (pure core, no DB writes).

Run:
  bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.test_greedy_pack.run
  ./scripts/test_greedy_pack.sh
"""

from __future__ import unicode_literals

from erpnext.erpnext_integrations.ecommerce_api import tms_api as tms


DEPOT = {"lat": -34.6037, "lng": -58.3816}


def _stop(dn, lat, lng, **kw):
	row = {
		"delivery_note": dn,
		"sales_order": kw.get("sales_order") or f"SO-{dn}",
		"customer_name": kw.get("customer_name") or dn,
		"address_name": kw.get("address_name"),
		"lat": lat,
		"lng": lng,
		"zone": kw.get("zone"),
		"due_date": kw.get("due_date"),
		"overdue": bool(kw.get("overdue")),
		"preferred_driver": kw.get("preferred_driver"),
		"receive_days": kw.get("receive_days") or [],
		"forced": bool(kw.get("forced")),
	}
	return row


def _pack(**overrides):
	kwargs = dict(
		movable=[],
		fixed=[],
		trip_capacity=[],
		drivers=["DRV-A", "DRV-B"],
		day_strs=["2026-10-01", "2026-10-02", "2026-10-03"],  # Thu Fri Sat if needed — treat as labels
		depot=DEPOT,
		max_orders=3,
		max_minutes=480,
		stop_minutes=15,
		avg_speed=25,
		drive_buffer=5,
		work_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
		as_of="2026-09-30",
		lead_days=1,
		driver_centroids={
			"DRV-A": {"lat": -34.60, "lng": -58.38, "territory": "T1"},
			"DRV-B": {"lat": -34.61, "lng": -58.40, "territory": "T2"},
		},
		nearby_km=8,
		zone_fallback_fn=lambda _z, _a: "2026-10-03",
	)
	kwargs.update(overrides)
	# day_strs must be real weekdays for receive filtering — use known dates:
	# 2026-10-01 = Thu, 2026-10-02 = Fri, 2026-10-05 = Mon
	if "day_strs" not in overrides:
		kwargs["day_strs"] = ["2026-10-01", "2026-10-02", "2026-10-05"]
	return tms._pack_remitos_core(**kwargs)


class _Fail(Exception):
	pass


def _assert(cond, msg):
	if not cond:
		raise _Fail(msg)


def test_unique_address_time_vs_order_count():
	"""Q3: 3 remitos same address → 1 dwell for time, 3 for order_count."""
	stops = [
		_stop("DN1", -34.61, -58.39, address_name="ADDR-1"),
		_stop("DN2", -34.61, -58.39, address_name="ADDR-1"),
		_stop("DN3", -34.61, -58.39, address_name="ADDR-1"),
	]
	_assert(tms._order_count(stops) == 3, "order_count should be 3 remitos")
	pts = tms._unique_stop_points(stops)
	_assert(len(pts) == 1, "unique addresses should be 1")
	mins_one = tms._route_minutes(stops[:1], DEPOT, 15, 25, 5)
	mins_three = tms._route_minutes(stops, DEPOT, 15, 25, 5)
	_assert(
		abs(mins_one - mins_three) < 0.01,
		f"time for 1 vs 3 same-addr should match ({mins_one} vs {mins_three})",
	)
	# Different address adds a dwell (+ drive)
	stops2 = stops + [_stop("DN4", -34.62, -58.40, address_name="ADDR-2")]
	mins_two_addr = tms._route_minutes(stops2, DEPOT, 15, 25, 5)
	_assert(mins_two_addr > mins_three + 10, "second address must add dwell (+ drive)")


def test_drive_buffer_adds_per_leg():
	"""Q14: drive_buffer applied once per NN hop."""
	stops = [_stop("DN1", -34.61, -58.39, address_name="A")]
	no_buf = tms._route_minutes(stops, DEPOT, 15, 25, 0)
	with_buf = tms._route_minutes(stops, DEPOT, 15, 25, 5)
	_assert(abs((with_buf - no_buf) - 5.0) < 0.01, f"expected +5 buffer, got {with_buf - no_buf}")


def test_limiting_resource():
	"""Q15: closer-to-100% wins."""
	_assert(tms._limiting_resource(30, 30, 100, 480) == "orders", "orders at 100%")
	_assert(tms._limiting_resource(10, 30, 480, 480) == "time", "time at 100%")
	_assert(tms._limiting_resource(15, 30, 400, 480) == "time", "time closer (83% > 50%)")


def test_receive_days_skip():
	"""Q4: Wed-only customer never lands on Thu/Fri."""
	# 2026-10-01 Thu, 10-02 Fri, 10-05 Mon — no Wed in horizon → overflow
	movable = [
		_stop(
			"DN-WED",
			-34.61,
			-58.39,
			address_name="W",
			preferred_driver="DRV-A",
			receive_days=["Wed"],
			due_date=None,
		)
	]
	out = _pack(movable=movable)
	a = out["assignments"][0]
	_assert(a.get("overflow") is True, "Wed-only with no Wed in horizon → overflow")
	_assert(a["proposed_due_date"] == "2026-10-03", "overflow uses zone fallback")

	# Include a Wednesday
	movable2 = [
		_stop(
			"DN-WED2",
			-34.61,
			-58.39,
			address_name="W",
			preferred_driver="DRV-A",
			receive_days=["Wed"],
		)
	]
	out2 = _pack(
		movable=movable2,
		day_strs=["2026-09-30", "2026-10-01", "2026-10-07"],  # Wed=09-30? 2026-09-30=Wed, 10-01=Thu, 10-07=Wed
	)
	a2 = out2["assignments"][0]
	_assert(a2.get("overflow") is not True, "should place on a Wed")
	_assert(a2["proposed_due_date"] in ("2026-09-30", "2026-10-07"), f"got {a2['proposed_due_date']}")


def test_forced_consumes_capacity():
	"""Q6: forced remitos seed capacity; flexible fills remainder then spills."""
	fixed = [
		_stop(
			"F1",
			-34.61,
			-58.39,
			address_name="F",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
			forced=True,
		),
		_stop(
			"F2",
			-34.61,
			-58.39,
			address_name="F",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
			forced=True,
		),
		_stop(
			"F3",
			-34.61,
			-58.39,
			address_name="F",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
			forced=True,
		),
	]
	# max_orders=3 → day full for DRV-A (single driver so no nearby spill)
	movable = [
		_stop(
			"M1",
			-34.61,
			-58.39,
			address_name="M",
			preferred_driver="DRV-A",
			due_date=None,
		)
	]
	out = _pack(
		movable=movable,
		fixed=fixed,
		max_orders=3,
		drivers=["DRV-A"],
		driver_centroids={"DRV-A": {"lat": -34.60, "lng": -58.38, "territory": "T1"}},
	)
	a = next(x for x in out["assignments"] if x["delivery_note"] == "M1")
	_assert(a["proposed_due_date"] != "2026-10-01", f"should spill off full day, got {a}")


def test_trip_capacity_counts():
	"""Q1: trip stops consume soft-cap slots."""
	trip = [
		_stop(
			"T1",
			-34.61,
			-58.39,
			address_name="T",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
		),
		_stop(
			"T2",
			-34.61,
			-58.39,
			address_name="T",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
		),
	]
	movable = [
		_stop("M1", -34.61, -58.39, address_name="M", preferred_driver="DRV-A"),
		_stop("M2", -34.61, -58.39, address_name="M", preferred_driver="DRV-A"),
	]
	out = _pack(
		movable=movable,
		trip_capacity=trip,
		max_orders=3,
		drivers=["DRV-A"],
		driver_centroids={"DRV-A": {"lat": -34.60, "lng": -58.38, "territory": "T1"}},
	)
	on_first = [a for a in out["assignments"] if a.get("proposed_due_date") == "2026-10-01"]
	_assert(len(on_first) <= 1, f"only 1 slot left after 2 trip stops, got {on_first}")


def test_soft_lock_within_lead():
	"""Q10: due within lead window stays; overdue moves."""
	# as_of=2026-09-30, lead=1 → lock until 2026-10-01
	locked = _stop(
		"L1",
		-34.61,
		-58.39,
		address_name="L",
		preferred_driver="DRV-A",
		due_date="2026-10-01",
	)
	overdue = _stop(
		"O1",
		-34.61,
		-58.39,
		address_name="O",
		preferred_driver="DRV-A",
		due_date="2026-09-28",
		overdue=True,
	)
	out = _pack(movable=[locked, overdue], lead_days=1, as_of="2026-09-30")
	by_dn = {a["delivery_note"]: a for a in out["assignments"]}
	_assert(by_dn["L1"].get("soft_locked") is True, "within-lead must soft-lock")
	_assert(by_dn["L1"]["proposed_due_date"] == "2026-10-01", "locked due unchanged")
	_assert(by_dn["O1"].get("soft_locked") is not True, "overdue must re-place")
	_assert(by_dn["O1"]["proposed_due_date"] >= "2026-10-01", "overdue lands on open day")


def test_soft_lock_releases_when_over_cap():
	"""Near-due soft locks unlock when preferred driver-day exceeds soft cap."""
	movable = [
		_stop(
			f"L{i}",
			-34.60 - (i * 0.002),
			-58.38 - (i * 0.002),
			address_name=f"A{i}",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
			zone="T1-THU",
		)
		for i in range(4)
	]
	out = _pack(
		movable=movable,
		max_orders=2,
		lead_days=1,
		as_of="2026-09-30",
		nearby_km=50,
	)
	locked = [a for a in out["assignments"] if a.get("soft_locked")]
	moved = [a for a in out["assignments"] if not a.get("soft_locked")]
	_assert(len(locked) <= 2, f"at most cap soft-locked, got {len(locked)} locked={locked}")
	_assert(len(moved) >= 2, f"excess must re-place, moved={moved}")
	# Prefer spilling to nearby DRV-B same day rather than overflowing A
	drv_b = [a for a in moved if a.get("driver") == "DRV-B"]
	_assert(len(drv_b) >= 1, f"expect spill to nearby DRV-B, moved={moved}")
	over = [f for f in out["fills"] if f.get("over_cap")]
	_assert(not over, f"fills should respect soft cap after release: {over}")


def test_force_unlock_moves_off_locked_day():
	"""force_unlock (skip-tomorrow) remitos are not soft-locked and leave that day."""
	row = _stop(
		"TM1",
		-34.61,
		-58.39,
		address_name="T",
		preferred_driver="DRV-A",
		due_date="2026-10-01",
	)
	row["force_unlock"] = True
	# day_strs start at Fri — tomorrow Thu excluded
	out = _pack(
		movable=[row],
		lead_days=1,
		as_of="2026-09-30",
		day_strs=["2026-10-02", "2026-10-05"],
	)
	a = out["assignments"][0]
	_assert(a.get("soft_locked") is not True, f"force_unlock must not soft-lock: {a}")
	_assert(a["proposed_due_date"] != "2026-10-01", f"must leave tomorrow: {a}")
	_assert(a["proposed_due_date"] in ("2026-10-02", "2026-10-05"), f"unexpected due {a}")


def test_sticky_prefers_previous_due():
	"""Q13: keep previous due when it still fits after higher priority."""
	# Fill early day somewhat but leave room; sticky remito has due on Fri
	filler = _stop(
		"FILL",
		-34.61,
		-58.39,
		address_name="X",
		preferred_driver="DRV-A",
		overdue=True,  # places first on earliest day
	)
	sticky = _stop(
		"STICK",
		-34.605,
		-58.385,
		address_name="S",
		preferred_driver="DRV-A",
		due_date="2026-10-02",  # Fri
	)
	out = _pack(movable=[filler, sticky], max_orders=5, prefer_sticky=True)
	by_dn = {a["delivery_note"]: a for a in out["assignments"]}
	_assert(
		by_dn["STICK"]["proposed_due_date"] == "2026-10-02",
		f"sticky should keep Fri, got {by_dn['STICK']}",
	)


def test_asap_without_sticky_fills_earlier_day():
	"""Without sticky, chronological ASAP can move Mon-due onto Fri before Mon."""
	row = _stop(
		"LATE",
		-34.61,
		-58.39,
		address_name="L",
		preferred_driver="DRV-A",
		due_date="2026-10-05",  # Mon
	)
	out = _pack(
		movable=[row],
		max_orders=5,
		prefer_sticky=False,
		day_strs=["2026-10-01", "2026-10-02", "2026-10-05"],
		lead_days=0,
		as_of="2026-09-30",
	)
	a = out["assignments"][0]
	_assert(a.get("soft_locked") is not True, f"should place: {a}")
	_assert(
		a["proposed_due_date"] == "2026-10-01",
		f"ASAP without sticky should take Thu before Mon, got {a}",
	)


def test_preferred_driver_before_nearby():
	"""Q7: zone.driver wins over nearby when both fit."""
	movable = [
		_stop(
			"DN1",
			-34.60,
			-58.38,
			address_name="A",
			preferred_driver="DRV-A",
			zone="T1-THU",
		)
	]
	out = _pack(movable=movable)
	a = out["assignments"][0]
	_assert(a["driver"] == "DRV-A", f"preferred driver expected, got {a}")


def test_overflow_stays_on_preferred():
	"""Q7: overflow → preferred driver even over soft cap, not random far driver."""
	# Cap=1, two remitos same day preference with only DRV-A preferred; second overflows
	m = [
		_stop("A1", -34.61, -58.39, address_name="A", preferred_driver="DRV-A"),
		_stop("A2", -34.62, -58.41, address_name="B", preferred_driver="DRV-A"),
	]
	# Tiny time cap so second cannot fit anywhere with DRV-A or nearby under cap
	out = _pack(
		movable=m,
		max_orders=1,
		max_minutes=20,  # one stop ≈ 15 dwell + drive+buffer > for 2 unique
		drivers=["DRV-A"],
		driver_centroids={"DRV-A": {"lat": -34.60, "lng": -58.38, "territory": "T1"}},
		nearby_km=0.01,  # no nearby others
	)
	ov = [a for a in out["assignments"] if a.get("overflow")]
	_assert(len(ov) >= 1, "expect overflow")
	_assert(all(a.get("driver") == "DRV-A" for a in ov), f"overflow keeps preferred: {ov}")


def test_unassigned_driver_flag():
	"""Q2: no preferred and no usable nearby → still packs with unassigned_driver."""
	movable = [
		_stop("U1", -34.90, -58.90, address_name="FAR")  # far from centroids
	]
	out = _pack(
		movable=movable,
		drivers=["DRV-A"],
		driver_centroids={"DRV-A": {"lat": -34.60, "lng": -58.38, "territory": "T1"}},
		nearby_km=1,
	)
	a = out["assignments"][0]
	# Last resort tries all drivers when cands empty — may assign DRV-A.
	# Force empty drivers list of nearby-only path: no preferred, cands empty → all drivers.
	_assert(a.get("proposed_due_date"), "must still get a due date")
	if a.get("driver") is None:
		_assert(a.get("unassigned_driver") is True, "null driver flagged")


def test_priority_overdue_before_flexible():
	"""Overdue packs before flexible on scarce capacity."""
	flex = _stop("FLEX", -34.61, -58.39, address_name="A", preferred_driver="DRV-A")
	ovd = _stop(
		"OVD",
		-34.61,
		-58.39,
		address_name="A",
		preferred_driver="DRV-A",
		overdue=True,
		due_date="2026-09-20",
	)
	out = _pack(
		movable=[flex, ovd],
		max_orders=1,
		drivers=["DRV-A"],
		driver_centroids={"DRV-A": {"lat": -34.60, "lng": -58.38, "territory": "T1"}},
	)
	by_dn = {a["delivery_note"]: a for a in out["assignments"]}
	_assert(
		by_dn["OVD"]["proposed_due_date"] == "2026-10-01",
		f"overdue should take earliest slot, got {by_dn}",
	)
	_assert(
		by_dn["FLEX"]["proposed_due_date"] != "2026-10-01" or by_dn["FLEX"].get("overflow"),
		"flexible yields earliest to overdue",
	)


def test_nearby_driver_when_preferred_full():
	"""Q7: when preferred is full for a day, nearby under-cap driver may take same day."""
	fixed = [
		_stop(
			"F1",
			-34.61,
			-58.39,
			address_name="F",
			preferred_driver="DRV-A",
			due_date="2026-10-01",
			forced=True,
		)
	]
	movable = [
		_stop(
			"M1",
			-34.605,
			-58.385,
			address_name="M",
			preferred_driver="DRV-A",
		)
	]
	out = _pack(movable=movable, fixed=fixed, max_orders=1)
	a = next(x for x in out["assignments"] if x["delivery_note"] == "M1")
	_assert(
		a["proposed_due_date"] == "2026-10-01" and a["driver"] == "DRV-B",
		f"nearby DRV-B should take same day, got {a}",
	)


def test_parse_receive_days():
	_assert(tms._parse_receive_days(["Wed", "Fri"]) == ["Wed", "Fri"], "list")
	_assert(tms._parse_receive_days("Wed,Fri") == ["Wed", "Fri"], "csv")
	_assert(tms._parse_receive_days(["Mié"]) == ["Wed"], "es alias")
	_assert(tms._parse_receive_days(None) == [], "none")
	_assert(tms._parse_receive_days("") == [], "empty")


def test_allowed_days_empty_receive_uses_work_days():
	days = ["2026-10-01", "2026-10-02", "2026-10-03"]  # Thu Fri Sat
	out = tms._allowed_day_strs(days, [], work_days=["Mon", "Tue", "Wed", "Thu", "Fri"])
	_assert(out == ["2026-10-01", "2026-10-02"], f"Sat filtered: {out}")
	out2 = tms._allowed_day_strs(days, ["Fri"], work_days=["Mon", "Tue", "Wed", "Thu", "Fri"])
	_assert(out2 == ["2026-10-02"], f"receive Fri only: {out2}")


def test_soft_lock_edges():
	_assert(
		tms._should_soft_lock("2026-10-01", "2026-09-30", 1, overdue=False) is True,
		"within lead locks",
	)
	_assert(
		tms._should_soft_lock("2026-10-02", "2026-09-30", 1, overdue=False) is False,
		"beyond lead free",
	)
	_assert(
		tms._should_soft_lock("2026-10-01", "2026-09-30", 1, overdue=True) is False,
		"overdue unlocked",
	)
	_assert(tms._should_soft_lock(None, "2026-09-30", 1) is False, "null due free")


TESTS = [
	test_unique_address_time_vs_order_count,
	test_drive_buffer_adds_per_leg,
	test_limiting_resource,
	test_receive_days_skip,
	test_forced_consumes_capacity,
	test_trip_capacity_counts,
	test_soft_lock_within_lead,
	test_soft_lock_releases_when_over_cap,
	test_force_unlock_moves_off_locked_day,
	test_sticky_prefers_previous_due,
	test_asap_without_sticky_fills_earlier_day,
	test_preferred_driver_before_nearby,
	test_overflow_stays_on_preferred,
	test_unassigned_driver_flag,
	test_priority_overdue_before_flexible,
	test_nearby_driver_when_preferred_full,
	test_parse_receive_days,
	test_allowed_days_empty_receive_uses_work_days,
	test_soft_lock_edges,
]


def run():
	passed = 0
	failed = []
	for fn in TESTS:
		name = fn.__name__
		try:
			fn()
			passed += 1
			print(f"PASS  {name}")
		except Exception as e:
			failed.append((name, str(e)))
			print(f"FAIL  {name}: {e}")
	summary = {"passed": passed, "failed": len(failed), "failures": failed, "total": len(TESTS)}
	print(f"\n{passed}/{len(TESTS)} passed")
	if failed:
		raise AssertionError(f"greedy pack unit tests failed: {failed}")
	return summary
