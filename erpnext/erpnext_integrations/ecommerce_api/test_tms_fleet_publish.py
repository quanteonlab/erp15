"""publish_fleet_mat: orders parked on a stale Draft MAT must move, not block.

Bug: Flota "publish" for driver A → 417 "Already assigned to another trip:
MAT-DN-…" because the order sat on another driver's never-published Draft
trip from an earlier day; driver A's page stayed empty.

Run: bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.test_tms_fleet_publish.run
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, now_datetime

from erpnext.erpnext_integrations.ecommerce_api import tms_api as tms

_FAKE_DNS = ["TEST-FLEET-DN-1", "TEST-FLEET-DN-2", "TEST-FLEET-DN-3"]


def _draft_trip(stops, locked=0):
	trip = frappe.get_doc(
		{
			"doctype": "Delivery Trip",
			"company": frappe.db.get_value("Company", {}, "name"),
			"departure_time": add_days(now_datetime(), -3),
			"delivery_stops": [
				{"address": "TEST-FLEET-ADDR", "delivery_note": dn, "visited": visited}
				for dn, visited in stops
			],
		}
	)
	trip.flags.ignore_mandatory = True
	trip.flags.ignore_links = True
	trip.flags.ignore_validate = True
	trip.insert(ignore_permissions=True)
	if locked and frappe.db.has_column("Delivery Trip", "custom_locked"):
		frappe.db.set_value("Delivery Trip", trip.name, "custom_locked", 1, update_modified=False)
	frappe.db.commit()
	return trip.name


def _cleanup(*trip_names):
	for name in trip_names:
		if frappe.db.exists("Delivery Trip", name):
			frappe.delete_doc("Delivery Trip", name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _stop_dns(trip_name):
	return set(
		frappe.get_all(
			"Delivery Stop", filters={"parent": trip_name}, pluck="delivery_note", ignore_permissions=True
		)
	)


def test_release_moves_untouched_stops_off_stale_draft():
	dn1, dn2, _dn3 = _FAKE_DNS
	stale = _draft_trip([(dn1, 0), (dn2, 0)])
	try:
		dry = tms._release_dns_from_other_draft_trips([dn1], dry_run=True)
		assert dry == {stale: [dn1]}, dry
		assert _stop_dns(stale) == {dn1, dn2}, "dry_run must not change anything"

		# Fixture stops use fake DN / Address links; skip link checks on the re-save.
		real_save = tms._save_trip_doc

		def _save_no_links(trip):
			trip.flags.ignore_links = True
			trip.flags.ignore_validate = True
			return real_save(trip)

		tms._save_trip_doc = _save_no_links
		try:
			moved = tms._release_dns_from_other_draft_trips([dn1])
		finally:
			tms._save_trip_doc = real_save
		assert moved == {stale: [dn1]}, moved
		assert _stop_dns(stale) == {dn2}
		assert dn1 not in tms._assigned_delivery_note_names()
	finally:
		_cleanup(stale)


def test_release_deletes_emptied_draft():
	dn1 = _FAKE_DNS[0]
	stale = _draft_trip([(dn1, 0)])
	try:
		tms._release_dns_from_other_draft_trips([dn1])
		assert not frappe.db.exists("Delivery Trip", stale), "empty stale draft should be deleted"
	finally:
		_cleanup(stale)


def test_release_keeps_visited_locked_and_own_trips():
	dn1, dn2, dn3 = _FAKE_DNS
	visited = _draft_trip([(dn1, 1)])
	locked = _draft_trip([(dn2, 0)], locked=1)
	own = _draft_trip([(dn3, 0)])
	try:
		moved = tms._release_dns_from_other_draft_trips([dn1, dn2, dn3], keep_trips=[own])
		assert moved == {}, moved
		assert _stop_dns(visited) == {dn1} and _stop_dns(locked) == {dn2} and _stop_dns(own) == {dn3}
		msg = tms._assignment_conflict_message([dn1])
		assert visited in msg and dn1 in msg, msg
	finally:
		_cleanup(visited, locked, own)


def run():
	tests = [
		test_release_moves_untouched_stops_off_stale_draft,
		test_release_deletes_emptied_draft,
		test_release_keeps_visited_locked_and_own_trips,
	]
	failed = []
	for fn in tests:
		try:
			fn()
			print(f"PASS  {fn.__name__}")
		except Exception as e:
			frappe.db.rollback()
			failed.append((fn.__name__, str(e)))
			print(f"FAIL  {fn.__name__}: {e!r}")
	print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
	return {"passed": len(tests) - len(failed), "failed": len(failed), "failures": failed, "total": len(tests)}
