"""MCP gateway probe — key store, matrix gates, field allowlists, preview→confirm.

  bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.test_mcp_gateway.run

Returns a list of (label, "FAIL", detail) — empty when everything passes
(``test_smoke`` suite 5.17 asserts it is empty). The site's real MCP key and
matrix are snapshotted first and restored afterwards; records created here are
tagged ``MCP_PROBE`` and deleted.
"""

from __future__ import annotations

import json
import traceback
import uuid

import frappe
from frappe.utils import flt

from erpnext.erpnext_integrations.ecommerce_api import mcp_api, mcp_keys_api as keys

PROBE_TAG = "MCP_PROBE"


def _snapshot(scope: str):
	if not frappe.db.exists("Table Extra Schema", scope):
		return None
	return frappe.db.get_value("Table Extra Schema", scope, "columns_json")


def _restore(scope: str, raw) -> None:
	if raw is None:
		if frappe.db.exists("Table Extra Schema", scope):
			frappe.delete_doc("Table Extra Schema", scope, ignore_permissions=True, force=True)
	else:
		keys._save_schema_store(scope, json.loads(raw) if raw else {})
	frappe.db.commit()


def _expect_raise(exc_type, fn, *args, **kwargs) -> str:
	try:
		fn(*args, **kwargs)
	except exc_type as e:
		return str(e)
	raise AssertionError(f"expected {exc_type.__name__}, call succeeded")


class _FakeRequest:
	"""Just enough of a werkzeug request for the acting-user header path."""

	def __init__(self, acting_user: str = ""):
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		headers = {"X-ERP-Acting-User": acting_user} if acting_user else {}
		self._req = Request(EnvironBuilder(headers=headers).get_environ())

	def __enter__(self):
		self._prev = getattr(frappe.local, "request", None)
		frappe.local.request = self._req
		if hasattr(frappe.local, "_staff_acting_perm_info"):
			del frappe.local._staff_acting_perm_info
		return self._req

	def __exit__(self, *exc):
		frappe.local.request = self._prev
		if hasattr(frappe.local, "_staff_acting_perm_info"):
			del frappe.local._staff_acting_perm_info
		return False


def _set_matrix(overrides: dict) -> None:
	rows = []
	for row in keys.MCP_DOCTYPE_MATRIX:
		perms = overrides.get(row["doctype"], {"view": row["view"], "edit": row["edit"]})
		rows.append({"doctype": row["doctype"], **perms})
	keys.save_mcp_matrix(rows)


def run():
	failures: list[tuple[str, str, str]] = []
	token_raw = _snapshot(keys.TOKEN_SCOPE)
	matrix_raw = _snapshot(keys.MATRIX_SCOPE)
	created: list[tuple[str, str]] = []
	key_ids: set[str] = set()
	state: dict = {}

	def check(label, fn):
		try:
			fn()
			print(f"  ✓ {label}")
		except Exception as e:
			detail = str(e) if isinstance(e, AssertionError) else traceback.format_exc(limit=3)
			print(f"  ✗ {label}: {e}")
			failures.append((label, "FAIL", detail))

	try:
		# Fresh key bound to Administrator, default matrix.
		if frappe.db.exists("Table Extra Schema", keys.TOKEN_SCOPE):
			frappe.delete_doc("Table Extra Schema", keys.TOKEN_SCOPE, ignore_permissions=True, force=True)
		keys._save_schema_store(keys.MATRIX_SCOPE, {})

		def key_minted():
			link = keys.get_mcp_link()
			token = link["token"]
			assert token.startswith("mcp_") and token.count("_") == 2, token
			assert link["acting_user"], "acting_user must default to the creator"
			assert keys.verify_mcp_token(token)
			assert keys.verify_mcp_token(f"Bearer {token}")
			assert not keys.verify_mcp_token(token[:-1] + ("0" if token[-1] != "0" else "1"))
			assert not keys.verify_mcp_token("erp_abc_def")
			keys.set_mcp_acting_user("Administrator")
			state["token"] = token
			key_ids.add(link["key_id"])

		check("key mint + verify (prefix, bearer, tamper)", key_minted)
		token = state.get("token")
		if not token:
			return failures

		check(
			"bad token → AuthenticationError",
			lambda: _expect_raise(frappe.AuthenticationError, mcp_api.mcp_whoami, "mcp_nope_nope"),
		)

		def whoami_ok():
			out = mcp_api.mcp_whoami(token)
			assert out["acting_user"] == "Administrator", out
			assert out["matrix"]["view"] > 0

		check("whoami returns bound actor + matrix counts", whoami_ok)

		def reads_do_not_eat_write_budget():
			for _ in range(mcp_api.WRITE_LIMIT_PER_MIN + 5):
				mcp_api.mcp_search(token, "Brand", "a", 1)
			out = mcp_api.mcp_create_record(token, "Brand", {"brand": "rate-probe"}, 0)
			assert out["requires_confirm"], out

		check("reads beyond the write limit do not block writes", reads_do_not_eat_write_budget)

		def actor_bound_on_request():
			from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username

			with _FakeRequest("someone-else@example.com"):
				mcp_api.mcp_whoami(token)
				assert _acting_username() == "Administrator", _acting_username()

		check("acting user header is rebound to the key's user", actor_bound_on_request)

		def missing_actor_fails():
			store = keys._load_schema_store(keys.TOKEN_SCOPE)
			keys._save_schema_store(keys.TOKEN_SCOPE, {**store, "acting_user": f"ghost-{uuid.uuid4().hex[:6]}@x.y"})
			try:
				_expect_raise(frappe.AuthenticationError, mcp_api.mcp_whoami, token)
			finally:
				keys._save_schema_store(keys.TOKEN_SCOPE, store)

		check("unknown acting user → AuthenticationError", missing_actor_fails)

		def matrix_rules():
			_set_matrix({"Company": {"view": 1, "edit": 1}, "Brand": {"view": 0, "edit": 1}, "Lead": {"view": 0, "edit": 0}})
			m = keys.load_mcp_matrix()
			assert m["Company"]["edit"] == 0, "edit-locked doctype must store edit=0"
			assert m["Brand"]["view"] == 1, "edit implies view"
			assert m["Lead"] == {"view": 0, "edit": 0}
			_expect_raise(frappe.ValidationError, keys.save_mcp_matrix, [{"doctype": "GL Entry", "view": 1}])

		check("matrix: edit-locked, edit⇒view, unknown doctype rejected", matrix_rules)

		def view_gate():
			_expect_raise(frappe.PermissionError, mcp_api.mcp_search, token, "Lead", "a")
			_expect_raise(frappe.PermissionError, mcp_api.mcp_list_records, token, "GL Entry")
			_expect_raise(frappe.PermissionError, mcp_api.mcp_update_record, token, "Company", "x", {"phone_no": "1"}, 1)

		check("view/edit gates (no view, not allowlisted, edit-locked)", view_gate)

		def list_validation():
			_expect_raise(frappe.ValidationError, mcp_api.mcp_list_records, token, "Brand", [["brand", "regexp", "x"]])
			_expect_raise(frappe.ValidationError, mcp_api.mcp_list_records, token, "Brand", [["nope", "=", "x"]])
			_expect_raise(frappe.ValidationError, mcp_api.mcp_list_records, token, "Brand", None, ["nope"])
			out = mcp_api.mcp_list_records(token, "Brand", [["brand", "like", "%"]], ["name", "brand"], 5)
			assert out["limit"] == 5 and isinstance(out["rows"], list)

		check("list_records validates operators + fields", list_validation)

		brand = f"{PROBE_TAG}-{uuid.uuid4().hex[:6]}"

		def create_preview_then_apply():
			preview = mcp_api.mcp_create_record(token, "Brand", {"brand": brand}, 0)
			assert preview["requires_confirm"] and not preview["applied"]
			assert not frappe.db.exists("Brand", brand), "preview must not insert"
			_expect_raise(frappe.ValidationError, mcp_api.mcp_create_record, token, "Brand", {"brand": brand}, 1)
			assert not frappe.db.exists("Brand", brand), "apply without preview_id must not insert"
			out = mcp_api.mcp_create_record(token, "Brand", {"brand": brand}, 1, preview_id=preview["preview_id"])
			assert out["applied"] and frappe.db.exists("Brand", out["name"])
			created.append(("Brand", out["name"]))
			_expect_raise(
				frappe.ValidationError, mcp_api.mcp_create_record, token, "Brand", {"brand": brand + "2"}, 1,
				preview_id=preview["preview_id"],
			)

		check("create: preview writes nothing, apply needs preview_id (single use)", create_preview_then_apply)

		def update_preview_then_apply():
			if not created:
				raise AssertionError("no probe Brand")
			name = created[0][1]
			new_desc = f"{PROBE_TAG} desc"
			preview = mcp_api.mcp_update_record(token, "Brand", name, {"description": new_desc}, 0)
			assert preview["requires_confirm"] and preview["preview"][0]["to"] == new_desc, preview
			assert frappe.db.get_value("Brand", name, "description") != new_desc, "preview must not write"
			pid = preview["preview_id"]
			msg = _expect_raise(
				frappe.ValidationError, mcp_api.mcp_update_record, token, "Brand", name, {"description": "sneaky"}, 1,
				preview_id=pid,
			)
			assert "differs" in msg, msg
			assert frappe.db.get_value("Brand", name, "description") != "sneaky"
			mcp_api.mcp_update_record(token, "Brand", name, json.dumps({"description": new_desc}), 1, preview_id=pid)
			assert frappe.db.get_value("Brand", name, "description") == new_desc

		check("update: diff preview, payload must match preview, then applies", update_preview_then_apply)

		def field_allowlist():
			name = created[0][1] if created else "x"
			msg = _expect_raise(frappe.PermissionError, mcp_api.mcp_update_record, token, "Brand", name, {"docstatus": 1, "zz_bogus": 1}, 0)
			assert "docstatus (blocked)" in msg and "zz_bogus (unknown)" in msg, msg
			cust = frappe.get_all("Customer", pluck="name", limit_page_length=1, ignore_permissions=True)
			if cust:
				_expect_raise(frappe.PermissionError, mcp_api.mcp_update_record, token, "Customer", cust[0], {"credit_limit": 1}, 0)
			_expect_raise(frappe.ValidationError, mcp_api.mcp_update_record, token, "Brand", name, {}, 0)

		check("blocked / unknown / empty field patches rejected", field_allowlist)

		def sensitive_output():
			out = mcp_api.mcp_get_record(token, "User", "Administrator")
			doc = out["doc"]
			for k in ("api_key", "api_secret", "password", "reset_password_key"):
				assert k not in doc, f"{k} leaked"

		check("get_record strips secrets", sensitive_output)

		def workflow_gate():
			out = mcp_api.mcp_run_workflow(token, "set_preorder_status", {"name": "SO-NOPE", "target_status": "Orden"}, 0)
			assert out["requires_confirm"] and not out["applied"] and out["preview_id"]
			_expect_raise(
				frappe.ValidationError, mcp_api.mcp_run_workflow, token, "set_preorder_status",
				{"name": "SO-NOPE", "target_status": "Orden"}, 1,
			)
			_expect_raise(frappe.ValidationError, mcp_api.mcp_run_workflow, token, "set_preorder_status", {"name": "SO-NOPE"}, 1)
			_expect_raise(frappe.DoesNotExistError, mcp_api.mcp_run_workflow, token, "drop_tables", {}, 1)
			_expect_raise(frappe.PermissionError, mcp_api.mcp_run_workflow, token, "convert_lead", {"lead": "x"}, 0)
			wfs = {w["name"]: w["allowed"] for w in mcp_api.mcp_list_workflows(token)["workflows"]}
			assert wfs["convert_lead"] is False and wfs["set_preorder_status"] is True, wfs

		check("workflows: preview_id gate, required args, unknown, matrix", workflow_gate)

		_set_matrix({})  # back to the default matrix for the order/price checks
		item_code = f"{PROBE_TAG}-ITEM-{uuid.uuid4().hex[:6]}"
		pl = mcp_api._default_selling_price_list()

		def _price(rate, valid_from=None):
			doc = frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": item_code,
					"price_list": pl,
					"price_list_rate": rate,
					"valid_from": valid_from,
				}
			)
			doc.insert(ignore_permissions=True)
			return doc.name

		def effective_price_and_collapse():
			from frappe.utils import add_days, nowdate

			from erpnext.erpnext_integrations.ecommerce_api.api import get_item_price
			from erpnext.erpnext_integrations.ecommerce_api.item_pricing import effective_item_price

			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": f"{PROBE_TAG} Ascensor Cuantico Probe",
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"is_sales_item": 1,
				}
			)
			item.insert(ignore_permissions=True)
			created.append(("Item", item_code))
			# Same shape as the El ascensor cuántico bug: undated 7651, then two dated rows.
			frappe.flags.in_item_price_collapse = True  # seed the duplicates the hook would retire
			try:
				frappe.db.set_value("Item Price", _price(7651), "valid_from", None)  # legacy undated row
				_price(6429.22, "2026-01-31")
				_price(5216.29, "2026-02-12")
			finally:
				frappe.flags.in_item_price_collapse = False
			assert flt(get_item_price(item_code, pl)) == 5216.29, get_item_price(item_code, pl)
			assert flt(mcp_api.mcp_search(token, "Item", item_code)["matches"][0]["price"]) == 5216.29
			# A new price taking effect today retires every row it supersedes.
			_price(5300, nowdate())
			rows = frappe.get_all("Item Price", filters={"item_code": item_code, "price_list": pl}, pluck="price_list_rate", ignore_permissions=True)
			assert [flt(r) for r in rows] == [5300.0], rows
			# Future price is kept alongside; the current one still wins today.
			_price(9999, add_days(nowdate(), 30))
			assert flt(effective_item_price(item_code, pl).price_list_rate) == 5300.0
			assert frappe.db.count("Item Price", {"item_code": item_code, "price_list": pl}) == 2

		check("prices: effective row = latest valid_from; save hook retires superseded rows", effective_price_and_collapse)

		def resolve_items():
			out = mcp_api.mcp_resolve_items(
				token,
				[
					{"item_code": item_code, "qty": 2},
					{"name": "ascensor cuantico probe", "qty": 1, "rate": 10},
					{"name": f"zz-nothing-{uuid.uuid4().hex[:8]}"},
				],
			)
			first, by_name, missing = out["lines"]
			assert first["matched_by"] == "item_code" and first["price"] == 5300.0 and not first["needs_review"], first
			assert by_name["match"] and by_name["match"]["item_code"] == item_code, by_name
			assert by_name["document_rate"] == 10 and by_name["qty"] == 1
			assert missing["match"] is None and missing["needs_review"], missing
			_expect_raise(frappe.ValidationError, mcp_api.mcp_resolve_items, token, [])

		check("resolve_items: code / name / unknown + effective price", resolve_items)

		def order_workflows():
			if not frappe.db.exists("Customer", "Consumidor Final"):
				print("  - skip order workflows (no Consumidor Final)")
				return
			args = {"items": [{"item_code": item_code, "qty": 2}, {"item_code": item_code + "-NOPE", "qty": 1}]}
			bad = mcp_api.mcp_run_workflow(token, "create_order", args, 0)
			assert bad["issues"] and not bad.get("preview_id"), bad
			# Exact document rate (above list) → stored as a margin by ERPNext.
			args = {"items": [{"item_code": item_code, "qty": 2, "rate": 7651}], "initial_status": "Orden"}
			pv = mcp_api.mcp_run_workflow(token, "create_order", args, 0)
			line = pv["details"]["lines"][0]
			assert line["rate"] == 7651 and line["rate_source"] == "given" and pv["details"]["estimated_total"] == 15302, pv
			res = mcp_api.mcp_run_workflow(token, "create_order", args, 1, preview_id=pv["preview_id"])["result"]
			so_name = res["preorder_name"]
			created.append(("Sales Order", so_name))
			so = frappe.get_doc("Sales Order", so_name)
			assert so.docstatus == 1 and flt(so.items[0].rate) == 7651, (so.docstatus, so.items[0].rate)
			# Stale margin regression: reprice must land on the list price, not list + old margin.
			pv = mcp_api.mcp_run_workflow(token, "reprice_order", {"name": so_name}, 0)
			assert pv["details"]["lines"][0]["to_rate"] == 5300.0, pv["details"]
			mcp_api.mcp_run_workflow(token, "reprice_order", {"name": so_name}, 1, preview_id=pv["preview_id"])
			so.reload()
			assert flt(so.items[0].rate) == 5300.0 and flt(so.grand_total) == 10600.0, (so.items[0].rate, so.grand_total)
			assert flt(so.items[0].stock_uom_rate) == 5300.0, so.items[0].stock_uom_rate
			# Lines edit on the submitted order: qty change, rate kept.
			upd = {"name": so_name, "items": [{"item_code": item_code, "qty": 3}]}
			pv = mcp_api.mcp_run_workflow(token, "update_order_lines", upd, 0)
			assert pv["details"]["to_lines"][0]["rate_source"] == "kept", pv["details"]
			mcp_api.mcp_run_workflow(token, "update_order_lines", upd, 1, preview_id=pv["preview_id"])
			so.reload()
			assert flt(so.grand_total) == 15900.0 and "Fifteen Thousand" in (so.in_words or ""), (so.grand_total, so.in_words)
			# Generic (non-pipeline) orders are refused with a clear message.
			plain = frappe.get_all("Sales Order", filters={"docstatus": 0}, pluck="name", limit_page_length=50, ignore_permissions=True)
			from erpnext.erpnext_integrations.ecommerce_api.api import _is_guest_preorder_sales_order

			plain = [n for n in plain if not _is_guest_preorder_sales_order(frappe.get_doc("Sales Order", n))]
			if plain:
				msg = _expect_raise(frappe.ValidationError, mcp_api.mcp_run_workflow, token, "reprice_order", {"name": plain[0]}, 0)
				assert "not a Pedido" in msg, msg
			state["so_name"] = so_name

		def in_request(fn):
			def wrapped():
				with _FakeRequest():
					fn()

			return wrapped

		check("order workflows: create (exact rate) → reprice (no stale margin) → edit lines", in_request(order_workflows))

		def print_pdf():
			if not state.get("so_name"):
				raise AssertionError("no probe order to print")
			out = mcp_api.mcp_get_print_pdf(token, "Sales Order", state["so_name"])
			import base64

			assert base64.b64decode(out["pdf_base64"])[:5] == b"%PDF-", out["size_bytes"]
			_expect_raise(frappe.PermissionError, mcp_api.mcp_get_print_pdf, token, "GL Entry", "x")

		check("get_print_pdf renders a real PDF; non-matrix doctype refused", in_request(print_pdf))

		def pedido_view_soft_fill():
			# Regression: old consultas keep rate 0 in ERPNext while Órdenes shows the list price;
			# get_record must expose that (pedido_view) so the assistant doesn't report ARS 0.
			if not state.get("so_name"):
				raise AssertionError("no probe order")
			name = state["so_name"]
			row = frappe.get_doc("Sales Order", name).items[0]
			frappe.db.set_value("Sales Order Item", row.name, {"rate": 0, "amount": 0}, update_modified=False)
			view = mcp_api.mcp_get_record(token, "Sales Order", name)["pedido_view"]
			line = view["lines"][0]
			assert line["rate_source"] == "price_list_fallback" and line["shown_rate"] == 5300.0, line
			assert line["document_rate"] == 0 and view["shown_total"] == 15900.0, view
			assert any("reprice_order" in n for n in view["notes"]), view["notes"]

		check("get_record: Pedido rate-0 lines show the Órdenes list-price fallback", in_request(pedido_view_soft_fill))

		def driver_workflow():
			# Regression: generic create left bare Drivers with no Employee (invisible in Employees).
			full_name = f"{PROBE_TAG} Chofer {uuid.uuid4().hex[:6]}"
			msg = _expect_raise(frappe.PermissionError, mcp_api.mcp_create_record, token, "Driver", {"full_name": full_name}, 0)
			assert "create_driver" in msg, msg
			pv = mcp_api.mcp_run_workflow(token, "create_driver", {"full_name": full_name}, 0)
			assert pv["details"]["action"] == "create_employee_and_driver", pv
			res = mcp_api.mcp_run_workflow(token, "create_driver", {"full_name": full_name}, 1, preview_id=pv["preview_id"])["result"]
			created.extend([("Employee", res["employee"]), ("Driver", res["driver"])])
			assert frappe.db.get_value("Driver", res["driver"], "employee") == res["employee"], res
			dup = mcp_api.mcp_run_workflow(token, "create_driver", {"full_name": full_name}, 0)
			assert any("already exists" in i for i in dup.get("issues") or []), dup

		check("create_driver: Employee + Driver; generic Driver create refused", in_request(driver_workflow))

		def employee_workflow():
			# Regression: generic Employee create skipped groups/PIN/defaults, and patching
			# Employee Group.employee_list replaced (dropped) every existing member.
			msg = _expect_raise(frappe.PermissionError, mcp_api.mcp_create_record, token, "Employee", {"first_name": "X"}, 0)
			assert "save_employee" in msg, msg
			group = frappe.get_all("Employee Group", pluck="name", limit_page_length=1, ignore_permissions=True)
			if group:
				_expect_raise(
					frappe.PermissionError, mcp_api.mcp_update_record, token, "Employee Group", group[0], {"employee_list": []}, 0
				)
			full_name = f"{PROBE_TAG} Vendedor {uuid.uuid4().hex[:6]}"
			args = {"employee_name": full_name, "groups": ["sales"]}
			pv = mcp_api.mcp_run_workflow(token, "save_employee", args, 0)
			assert pv["details"]["groups"] == ["ventas"], pv
			res = mcp_api.mcp_run_workflow(token, "save_employee", args, 1, preview_id=pv["preview_id"])["result"]
			created.append(("Employee", res["employee"]))
			assert res["created"] and res["groups"] == ["ventas"], res
			bad = mcp_api.mcp_run_workflow(token, "save_employee", {"employee_name": "Y Z", "groups": ["nope"]}, 0)
			assert any("Unknown group" in i for i in bad.get("issues") or []), bad

		check("save_employee: create in group ventas; generic Employee create / group patch refused", in_request(employee_workflow))

		def react_surfaces():
			# g015: React verifies the same token here and reuses the preview store.
			sess = mcp_api.mcp_react_session(token, "templates", 0)
			assert sess["actor"] == "Administrator" and "ECommerce Print Template" in sess["matrix"], sess
			_expect_raise(frappe.ValidationError, mcp_api.mcp_react_session, token, "billing", 0)
			_expect_raise(frappe.AuthenticationError, mcp_api.mcp_react_session, "mcp_bad_token", "print", 0)
			payload = {"template_id": "X", "ops": [{"op": "remove_element", "id": "a"}]}
			pid = mcp_api.mcp_preview_remember(token, "templates", payload)["preview_id"]
			_expect_raise(frappe.ValidationError, mcp_api.mcp_preview_consume, token, "sections", payload, pid)  # other surface
			_expect_raise(frappe.ValidationError, mcp_api.mcp_preview_consume, token, "templates", {**payload, "ops": []}, pid)
			pid = mcp_api.mcp_preview_remember(token, "templates", payload)["preview_id"]
			assert mcp_api.mcp_preview_consume(token, "templates", payload, pid)["ok"]
			_expect_raise(frappe.ValidationError, mcp_api.mcp_preview_consume, token, "templates", payload, pid)  # single use
			for dt in ("ECommerce Print Template", "ECommerce Floor Map"):
				msg = _expect_raise(frappe.PermissionError, mcp_api.mcp_create_record, token, dt, {"template_name": "x"}, 0)
				assert "tools" in msg, msg

		check("react surfaces: session + namespaced single-use previews; generic writes refused", in_request(react_surfaces))

		def capability_switches():
			# Staff Credential / Catalog Migration are matrix switches for React surfaces, not DocTypes.
			for sfc in ("labels", "migrate"):
				assert "Staff Credential" in mcp_api.mcp_react_session(token, sfc, 0)["matrix"]
			for dt in ("Staff Credential", "Catalog Migration"):
				msg = _expect_raise(frappe.PermissionError, mcp_api.mcp_list_records, token, dt)
				assert "capability switch" in msg, msg

		check("capability switches: labels/migrate sessions; pseudo rows refused by generic tools", in_request(capability_switches))

		def document_triage():
			import base64

			from frappe.utils.pdf import get_pdf

			from erpnext.erpnext_integrations.ecommerce_api import archivo_api

			pdf = get_pdf("<p>Factura Edenor N 0001-123 Total $ 4321</p>")
			out = archivo_api.enqueue_archivo_bulk(
				files=[{"file_name": f"{PROBE_TAG} factura.pdf", "content_b64": base64.b64encode(pdf).decode()}]
			)
			name = out["created"][0]["name"]
			created.append(("Company Archive Entry", name))

			inbox = mcp_api.mcp_list_document_inbox(token, "queue", 100)
			assert name in {r["name"] for r in inbox["rows"]}, inbox["counts"]
			assert inbox["kinds"] and all(k["code"] != archivo_api.INBOX_KIND for k in inbox["kinds"])

			tri = mcp_api.mcp_get_document_for_triage(token, name)
			f = tri["files"][0]
			assert f["mime_type"] == "application/pdf" and "4321" in f.get("text", ""), f.get("text")
			assert base64.b64decode(f["content_base64"])[:5] == b"%PDF-"
			assert tri["inbox_status"] == "queue"

			# Generic writes cannot bypass the draft rule.
			msg = _expect_raise(frappe.PermissionError, mcp_api.mcp_update_record, token, "Company Archive Entry", name, {"title": "x"}, 0)
			assert "classify_document" in msg, msg

			bad = mcp_api.mcp_run_workflow(token, "classify_document", {"name": name, "title": "x"}, 0)
			assert bad["issues"] and not bad.get("preview_id"), bad
			args = {"name": name, "kind": "utility_bill", "amount": 4321, "party": "Edenor", "payment_reference": "0001-123"}
			pv = mcp_api.mcp_run_workflow(token, "classify_document", args, 0)
			fields = {c["field"]: c["to"] for c in pv["details"]["changes"]}
			assert fields["kind"] == "utility_bill" and fields["workflow_status"] == "draft", pv["details"]
			assert frappe.db.get_value("Company Archive Entry", name, "kind") == archivo_api.INBOX_KIND, "preview wrote"
			mcp_api.mcp_run_workflow(token, "classify_document", args, 1, preview_id=pv["preview_id"])
			row = frappe.db.get_value("Company Archive Entry", name, ["kind", "workflow_status", "amount", "erp_sync_status"], as_dict=True)
			assert row.kind == "utility_bill" and row.workflow_status == "draft", row
			assert flt(row.amount) == 4321 and row.erp_sync_status == "local_only", row

			_set_matrix({"Company Archive Entry": {"view": 0, "edit": 0}})
			try:
				_expect_raise(frappe.PermissionError, mcp_api.mcp_list_document_inbox, token)
			finally:
				_set_matrix({})

		check("documents: inbox → triage (PDF text) → classify stays draft; generic edit refused", document_triage)

		def rotate_revokes():
			fresh = keys.rotate_mcp_link()
			key_ids.add(fresh["key_id"])
			assert fresh["acting_user"] == "Administrator", "rotate keeps the bound user"
			assert not keys.verify_mcp_token(token)
			_expect_raise(frappe.AuthenticationError, mcp_api.mcp_whoami, token)
			assert mcp_api.mcp_whoami(fresh["token"])["key_id"] == fresh["key_id"]

		check("rotate revokes the old token immediately", rotate_revokes)

		def audit_rows():
			items = keys.get_mcp_audit(200)["items"]
			mine = [i for i in items if i.get("key_id") in key_ids]
			assert any(i.get("tool") == "create" and i.get("ok") for i in mine), "missing create audit row"
			assert any(not i.get("ok") for i in mine), "missing failure audit row"

		check("audit trail records ok + failed calls", audit_rows)
	finally:
		for doctype, name in reversed(created):
			try:
				if doctype == "Sales Order" and frappe.db.get_value(doctype, name, "docstatus") == 1:
					frappe.get_doc(doctype, name).cancel()
				if doctype == "Item":
					for price in frappe.get_all("Item Price", filters={"item_code": name}, pluck="name", ignore_permissions=True):
						frappe.delete_doc("Item Price", price, ignore_permissions=True, force=True)
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
			except Exception:
				print(f"  ! cleanup {doctype} {name}: {traceback.format_exc(limit=1)}")
		for row in frappe.get_all(
			"Table Extra Data",
			filters={"scope": keys.AUDIT_SCOPE},
			fields=["name", "data_json"],
			ignore_permissions=True,
		):
			if any(k and k in (row.data_json or "") for k in key_ids):
				frappe.delete_doc("Table Extra Data", row.name, ignore_permissions=True, force=True)
		_restore(keys.TOKEN_SCOPE, token_raw)
		_restore(keys.MATRIX_SCOPE, matrix_raw)
		frappe.db.commit()

	print(f"\nMCP gateway probe: {'OK' if not failures else f'{len(failures)} failure(s)'}")
	return failures
