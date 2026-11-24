"""TMS must work with no Google key and no geocoder at all.

Covers: schema self-heal (MySQL 1054 on skipped patches), soft geocode, manual
pins, local distance/ETA estimate, desk ``process_route`` fallback, and the
``before_request`` hook gating.

Run: bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.test_tms_no_google.run
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from erpnext.erpnext_integrations.ecommerce_api import tms_api as tms

_PROBE_FIELD = "custom_tms_selfheal_probe"


class _no_google:
	"""Context: no Maps key, no network geocoder (worst case)."""

	def __enter__(self):
		frappe.flags.tms_no_google = True
		frappe.flags.tms_no_network_geocode = True

	def __exit__(self, *exc):
		frappe.flags.tms_no_google = False
		frappe.flags.tms_no_network_geocode = False


def _temp_address(lat=None, lng=None):
	addr = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": "TMS no-google test",
			"address_type": "Shipping",
			"address_line1": "Calle Falsa 123",
			"city": "Buenos Aires",
			"country": frappe.db.get_value("Country", {"code": "ar"}) or "Argentina",
		}
	)
	addr.flags.ignore_mandatory = True
	addr.flags.ignore_links = True
	addr.insert(ignore_permissions=True)
	if lat is not None:
		frappe.db.set_value(
			"Address", addr.name, {"custom_latitude": lat, "custom_longitude": lng}, update_modified=False
		)
	return addr.name


def _temp_trip(address_name, coords):
	trip = frappe.get_doc(
		{
			"doctype": "Delivery Trip",
			"company": frappe.db.get_value("Company", {}, "name"),
			"departure_time": now_datetime(),
			"delivery_stops": [
				{"address": address_name, "lat": lat, "lng": lng} for lat, lng in coords
			],
		}
	)
	trip.flags.ignore_mandatory = True
	trip.flags.ignore_links = True
	trip.flags.ignore_validate = True
	trip.insert(ignore_permissions=True)
	return trip


def _cleanup(trip_name=None, address_name=None):
	if trip_name and frappe.db.exists("Delivery Trip", trip_name):
		frappe.delete_doc("Delivery Trip", trip_name, ignore_permissions=True, force=True)
	if address_name and frappe.db.exists("Address", address_name):
		frappe.delete_doc("Address", address_name, ignore_permissions=True, force=True)
	frappe.db.commit()


def test_parse_coords_text():
	p = tms._parse_coords_text
	assert p("-34.6037, -58.3816") == (-34.6037, -58.3816)
	assert p("https://www.google.com/maps/place/X/@-34.60,-58.38,17z/data=!3d-34.6011!4d-58.3822") == (
		-34.6011,
		-58.3822,
	)
	assert p("https://www.openstreetmap.org/?mlat=-34.5&mlon=-58.4#map=17/-34.5/-58.4") == (-34.5, -58.4)
	assert p("https://www.google.com/maps/search/?api=1&query=-34.71%2C-58.21") == (-34.71, -58.21)
	assert p("not coords") == (None, None)
	assert p(None) == (None, None)


def test_tms_schema_complete_after_ensure():
	tms._ensure_tms_schema(force=True)
	for patch_name in tms._TMS_SCHEMA_PATCHES:
		missing = tms._missing_tms_columns(tms._tms_schema_patch_fields(patch_name))
		assert not missing, f"{patch_name}: {missing}"
	assert frappe.db.has_column("Delivery Stop", "custom_outcome")


def test_schema_heal_creates_missing_column():
	"""A patch the site never ran (probe field) gets its column on next ensure."""
	probe = {
		"Delivery Stop": [
			{"fieldname": _PROBE_FIELD, "fieldtype": "Data", "label": "TMS selfheal probe", "insert_after": "details"}
		]
	}
	real_patches, real_loader = tms._TMS_SCHEMA_PATCHES, tms._tms_schema_patch_fields
	tms._TMS_SCHEMA_PATCHES = ("__probe__",)
	tms._tms_schema_patch_fields = lambda _name: probe
	frappe.cache.hdel("table_columns", "tabDelivery Stop")
	try:
		assert not frappe.db.has_column("Delivery Stop", _PROBE_FIELD)
		created = tms._ensure_tms_schema(force=True)
		assert created == [f"Delivery Stop.{_PROBE_FIELD}"], created
		assert frappe.db.has_column("Delivery Stop", _PROBE_FIELD)
		# Memoized: a second non-forced call does no work.
		assert tms._ensure_tms_schema() == []
	finally:
		tms._TMS_SCHEMA_PATCHES, tms._tms_schema_patch_fields = real_patches, real_loader
		cf = f"Delivery Stop-{_PROBE_FIELD}"
		if frappe.db.exists("Custom Field", cf):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
		# Live check — has_column is cached and Custom Field delete may already drop it.
		if frappe.db.sql("SHOW COLUMNS FROM `tabDelivery Stop` LIKE %s", _PROBE_FIELD):
			frappe.db.sql_ddl(f"ALTER TABLE `tabDelivery Stop` DROP COLUMN `{_PROBE_FIELD}`")
		frappe.cache.hdel("table_columns", "tabDelivery Stop")
		frappe.clear_cache(doctype="Delivery Stop")
		frappe.db.commit()


def test_before_request_hook_gating():
	calls = []
	real = tms._ensure_tms_schema
	real_request = getattr(frappe.local, "request", None)
	tms._ensure_tms_schema = lambda *a, **k: calls.append(1) or []
	site = frappe.local.site
	tms._tms_schema_ok_sites.discard(site)
	try:
		frappe.local.request = frappe._dict(path="/api/method/frappe.auth.get_logged_user")
		tms.ensure_tms_schema_before_request()
		assert calls == [], "non-ecommerce path must not trigger ensure"
		frappe.local.request = frappe._dict(path=tms._ECOMMERCE_API_PREFIX + "tms_api.list_trips")
		tms.ensure_tms_schema_before_request()
		assert calls == [1], calls
	finally:
		tms._ensure_tms_schema = real
		frappe.local.request = real_request
		tms._ensure_tms_schema(force=True)


def test_capabilities_without_google():
	with _no_google():
		caps = tms._tms_capabilities()
	assert caps == {"server_geocode": "none", "road_distances": False, "distance_source": "estimate"}, caps


def test_geocode_address_soft_without_google():
	addr = _temp_address()
	try:
		with _no_google():
			res = tms.geocode_address(addr, force=1)
		assert res["geocoded"] is False, res
		assert res["lat"] is None and res["reason"], res
	finally:
		_cleanup(address_name=addr)


def test_set_address_coordinates_manual_pin_syncs_stops():
	addr = _temp_address()
	trip = _temp_trip(addr, [(None, None)])
	try:
		res = tms.set_address_coordinates(
			addr, coords_text="https://maps.google.com/?q=-34.61,-58.40"
		)
		assert (res["lat"], res["lng"]) == (-34.61, -58.40), res
		row = frappe.db.get_value("Address", addr, ["custom_latitude", "custom_longitude"], as_dict=True)
		assert (flt(row.custom_latitude), flt(row.custom_longitude)) == (-34.61, -58.40)
		stop_lat = frappe.db.get_value("Delivery Stop", {"parent": trip.name}, "lat")
		assert flt(stop_lat) == -34.61, stop_lat
		try:
			tms.set_address_coordinates(addr, coords_text="nowhere")
			raise AssertionError("garbage coords must raise ValidationError")
		except frappe.ValidationError:
			frappe.clear_messages()
	finally:
		_cleanup(trip.name, addr)


def test_local_estimate_fills_distance_and_eta():
	addr = _temp_address()  # no coords: the (None, None) stop must stay unlocated
	coords = [(-34.61, -58.39), (-34.62, -58.41), (None, None), (-34.64, -58.42)]
	trip = _temp_trip(addr, coords)
	try:
		with _no_google():
			res = tms.process_route_without_google(trip, optimize=0)
		assert res["distance_source"] == "estimate", res
		stops = frappe.get_all(
			"Delivery Stop",
			filters={"parent": trip.name},
			fields=["idx", "distance", "estimated_arrival", "lat"],
			order_by="idx asc",
			ignore_permissions=True,
		)
		etas = [get_datetime(s.estimated_arrival) for s in stops if s.estimated_arrival]
		assert len(etas) == 3, stops
		assert etas == sorted(etas), "ETAs must increase along the route"
		# Every located stop gets an ETA; legs after the first always have distance.
		assert all(flt(s.distance) > 0 for s in stops[1:] if s.estimated_arrival)
		assert all(s.distance is not None for s in stops)
		assert flt(frappe.db.get_value("Delivery Trip", trip.name, "total_distance")) > 0
	finally:
		_cleanup(trip.name, addr)


def test_desk_process_route_without_google():
	"""Delivery Trip "Optimize Route" button used to 500: Enter API key in Google Settings."""
	addr = _temp_address(-34.60, -58.38)
	trip = _temp_trip(addr, [(-34.61, -58.39), (-34.62, -58.41)])
	try:
		with _no_google():
			frappe.flags.ignore_permissions = True
			doc = frappe.get_doc("Delivery Trip", trip.name)
			frappe.flags.ignore_permissions = False
			doc.process_route(optimize=True)
		eta = frappe.db.get_value("Delivery Stop", {"parent": trip.name, "idx": 1}, "estimated_arrival")
		assert eta, "desk process_route must fill ETAs without Google"
		assert get_datetime(eta) <= add_to_date(now_datetime(), hours=2)
	finally:
		_cleanup(trip.name, addr)


def run():
	tests = [
		test_parse_coords_text,
		test_tms_schema_complete_after_ensure,
		test_schema_heal_creates_missing_column,
		test_before_request_hook_gating,
		test_capabilities_without_google,
		test_geocode_address_soft_without_google,
		test_set_address_coordinates_manual_pin_syncs_stops,
		test_local_estimate_fills_distance_and_eta,
		test_desk_process_route_without_google,
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
		finally:
			frappe.flags.tms_no_google = False
			frappe.flags.tms_no_network_geocode = False
	print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
	return {"passed": len(tests) - len(failed), "failed": len(failed), "failures": failed, "total": len(tests)}
