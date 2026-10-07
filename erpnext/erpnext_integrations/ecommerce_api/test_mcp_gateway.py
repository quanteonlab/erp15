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
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
			except Exception:
				pass
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
