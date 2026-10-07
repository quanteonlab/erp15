"""MCP agent key + DocType view/edit matrix (Settings → Apps & devices).

The MCP key authorizes AI assistants (through the mcp-erp server) to call the
narrow gateway in ``mcp_api.py``. Key material lives in Table Extra Schema
(``mcp.link.token``) and the DocType × view/edit matrix in
``mcp.key.matrix`` — the same no-migrate KV pattern as the app-link token.

Admin surface: get/rotate the key, bind the acting user, read/save the
matrix, read the audit trail. Everything requires the
``settings.manage_mcp`` app permission.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.password import decrypt, encrypt

TOKEN_SCOPE = "mcp.link.token"
MATRIX_SCOPE = "mcp.key.matrix"
AUDIT_SCOPE = "mcp.audit"
TOKEN_PREFIX = "mcp"
GATEWAY_VERSION = 1

# Every DocType the SilkOS tables / ops / logistics pages touch
# (plan: tenant_admin_mcp). Rows in EDIT_LOCKED_DOCTYPES are view-only by
# design (money / stock / system docs): the UI disables Edit and the gateway
# refuses generic writes for them even if a saved row says otherwise.
MCP_DOCTYPE_MATRIX = [
	# catalog
	{"doctype": "Item", "group": "catalog", "label_en": "Products", "label_es": "Productos", "view": 1, "edit": 1},
	{"doctype": "Item Group", "group": "catalog", "label_en": "Categories", "label_es": "Categorías", "view": 1, "edit": 1},
	{"doctype": "Brand", "group": "catalog", "label_en": "Brands", "label_es": "Marcas", "view": 1, "edit": 1},
	{"doctype": "UOM", "group": "catalog", "label_en": "Units of measure", "label_es": "Unidades de medida", "view": 1, "edit": 1},
	{"doctype": "Item Attribute", "group": "catalog", "label_en": "Variant attributes", "label_es": "Atributos de variante", "view": 1, "edit": 1},
	{"doctype": "Price List", "group": "catalog", "label_en": "Price lists", "label_es": "Listas de precio", "view": 1, "edit": 1},
	{"doctype": "Item Price", "group": "catalog", "label_en": "Item prices", "label_es": "Precios de artículo", "view": 1, "edit": 1},
	{"doctype": "Stock Lot", "group": "catalog", "label_en": "Stock lots", "label_es": "Lotes", "view": 1, "edit": 1},
	{"doctype": "Stock Location Estimate", "group": "catalog", "label_en": "Stock locations", "label_es": "Ubicaciones de stock", "view": 1, "edit": 1},
	# sales
	{"doctype": "Sales Order", "group": "sales", "label_en": "Sales orders", "label_es": "Pedidos", "view": 1, "edit": 1},
	{"doctype": "Delivery Note", "group": "sales", "label_en": "Delivery notes", "label_es": "Remitos", "view": 1, "edit": 1},
	{"doctype": "Customer", "group": "sales", "label_en": "Customers", "label_es": "Clientes", "view": 1, "edit": 1},
	{"doctype": "Lead", "group": "sales", "label_en": "Leads", "label_es": "Leads", "view": 1, "edit": 1},
	{"doctype": "Contact", "group": "sales", "label_en": "Contacts", "label_es": "Contactos", "view": 1, "edit": 1},
	{"doctype": "Address", "group": "sales", "label_en": "Addresses", "label_es": "Direcciones", "view": 1, "edit": 1},
	{"doctype": "Territory", "group": "sales", "label_en": "Territories", "label_es": "Territorios", "view": 1, "edit": 1},
	{"doctype": "Pricing Rule", "group": "sales", "label_en": "Pricing rules", "label_es": "Reglas de precio", "view": 1, "edit": 1},
	{"doctype": "Product Bundle", "group": "sales", "label_en": "Product bundles", "label_es": "Combos", "view": 1, "edit": 1},
	{"doctype": "Coupon Code", "group": "sales", "label_en": "Coupon codes", "label_es": "Cupones", "view": 1, "edit": 1},
	# purchases
	{"doctype": "Purchase Order", "group": "purchases", "label_en": "Purchase orders", "label_es": "Órdenes de compra", "view": 1, "edit": 1},
	{"doctype": "Supplier", "group": "purchases", "label_en": "Suppliers", "label_es": "Proveedores", "view": 1, "edit": 1},
	{"doctype": "Purchase Invoice", "group": "purchases", "label_en": "Purchase invoices", "label_es": "Facturas de compra", "view": 1, "edit": 0},
	# cash
	{"doctype": "POS Profile", "group": "cash", "label_en": "Cash registers", "label_es": "Cajas", "view": 1, "edit": 1},
	{"doctype": "POS Cash Session", "group": "cash", "label_en": "Cash sessions", "label_es": "Sesiones de caja", "view": 1, "edit": 1},
	{"doctype": "Mode of Payment", "group": "cash", "label_en": "Payment methods", "label_es": "Medios de pago", "view": 1, "edit": 1},
	{"doctype": "Sales Invoice", "group": "cash", "label_en": "Sales invoices", "label_es": "Facturas de venta", "view": 1, "edit": 0},
	{"doctype": "Payment Entry", "group": "cash", "label_en": "Payment entries", "label_es": "Pagos", "view": 1, "edit": 0},
	# logistics
	{"doctype": "Delivery Trip", "group": "logistics", "label_en": "Delivery trips", "label_es": "Viajes", "view": 1, "edit": 1},
	{"doctype": "Delivery Request", "group": "logistics", "label_en": "Delivery requests", "label_es": "Solicitudes de entrega", "view": 1, "edit": 1},
	{"doctype": "Driver", "group": "logistics", "label_en": "Drivers", "label_es": "Choferes", "view": 1, "edit": 1},
	{"doctype": "Vehicle", "group": "logistics", "label_en": "Vehicles", "label_es": "Vehículos", "view": 1, "edit": 1},
	{"doctype": "Warehouse", "group": "logistics", "label_en": "Warehouses", "label_es": "Depósitos", "view": 1, "edit": 1},
	{"doctype": "Stock Entry", "group": "logistics", "label_en": "Stock entries", "label_es": "Movimientos de stock", "view": 1, "edit": 0},
	{"doctype": "Bin", "group": "logistics", "label_en": "Stock levels", "label_es": "Niveles de stock", "view": 1, "edit": 0},
	# staff
	{"doctype": "Employee", "group": "staff", "label_en": "Employees", "label_es": "Empleados", "view": 1, "edit": 1},
	{"doctype": "Employee Group", "group": "staff", "label_en": "Employee groups", "label_es": "Grupos de empleados", "view": 1, "edit": 1},
	{"doctype": "Branch", "group": "staff", "label_en": "Branches", "label_es": "Sucursales", "view": 1, "edit": 0},
	{"doctype": "User", "group": "staff", "label_en": "Users", "label_es": "Usuarios", "view": 1, "edit": 0},
	# org
	{"doctype": "Company", "group": "org", "label_en": "Company", "label_es": "Empresa", "view": 1, "edit": 0},
]

EDIT_LOCKED_DOCTYPES = {
	"Purchase Invoice",
	"Sales Invoice",
	"Payment Entry",
	"Stock Entry",
	"Bin",
	"User",
	"Company",
}
KNOWN_DOCTYPES = {row["doctype"] for row in MCP_DOCTYPE_MATRIX}
MATRIX_GROUPS = list(dict.fromkeys(row["group"] for row in MCP_DOCTYPE_MATRIX))


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _now_iso() -> str:
	return str(now_datetime())


def _hash_secret(secret: str) -> str:
	return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _load_schema_store(scope: str) -> dict:
	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("Table Extra Schema", scope):
		return {}
	doc = frappe.get_doc("Table Extra Schema", scope)
	data = _parse_json(doc.columns_json, {})
	return data if isinstance(data, dict) else {}


def _save_schema_store(scope: str, data: dict) -> None:
	payload = json.dumps(data or {}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", scope):
		doc = frappe.get_doc("Table Extra Schema", scope)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": scope, "columns_json": payload}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def _format_token(key_id: str, secret: str) -> str:
	return f"{TOKEN_PREFIX}_{key_id}_{secret}"


def _parse_token(raw) -> tuple[str, str] | None:
	token = str(raw or "").strip()
	if token.lower().startswith("bearer "):
		token = token[7:].strip()
	parts = token.split("_")
	if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
		return None
	key_id, secret = parts[1], parts[2]
	if not key_id or not secret:
		return None
	return key_id, secret


def _requesting_user() -> str:
	"""The staff user behind this request: Next forwards it as X-ERP-Acting-User
	(the session user is the tenant API-key user in that case)."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _acting_username

	user = _acting_username()
	if user and frappe.db.exists("User", user):
		return user
	return frappe.session.user if frappe.session.user != "Guest" else "Administrator"


def _new_token_store(acting_user: str | None = None) -> dict:
	key_id = secrets.token_hex(8)
	secret = secrets.token_hex(16)
	creator = _requesting_user()
	return {
		"key_id": key_id,
		"secret_hash": _hash_secret(secret),
		"secret_enc": encrypt(secret),
		"created_at": _now_iso(),
		"created_by": creator,
		"acting_user": acting_user or creator,
		"_plain_secret": secret,
	}


def _ensure_token_store() -> dict:
	store = _load_schema_store(TOKEN_SCOPE)
	if store.get("key_id") and store.get("secret_hash") and store.get("secret_enc"):
		return store
	fresh = _new_token_store()
	plain = fresh.pop("_plain_secret")
	_save_schema_store(TOKEN_SCOPE, fresh)
	fresh["_plain_secret"] = plain
	return fresh


def _reveal_secret(store: dict) -> str:
	if store.get("_plain_secret"):
		return str(store["_plain_secret"])
	enc = store.get("secret_enc")
	if not enc:
		frappe.throw(_("MCP key is missing. Rotate it to create a new one."))
	return decrypt(enc)


def verify_mcp_token(token) -> bool:
	parsed = _parse_token(token)
	if not parsed:
		return False
	key_id, secret = parsed
	store = _load_schema_store(TOKEN_SCOPE)
	if not store or store.get("key_id") != key_id:
		return False
	expected = store.get("secret_hash") or ""
	return hmac.compare_digest(expected, _hash_secret(secret))


def load_mcp_token_store() -> dict:
	return _load_schema_store(TOKEN_SCOPE)


def load_mcp_matrix() -> dict:
	"""Return {doctype: {"view": 0/1, "edit": 0/1}} merged over the defaults."""
	saved = _load_schema_store(MATRIX_SCOPE).get("doctypes") or {}
	merged: dict[str, dict] = {}
	for row in MCP_DOCTYPE_MATRIX:
		dt = row["doctype"]
		override = saved.get(dt) if isinstance(saved.get(dt), dict) else {}
		view = 1 if cint(override.get("view", row["view"])) else 0
		edit = 1 if cint(override.get("edit", row["edit"])) else 0
		if dt in EDIT_LOCKED_DOCTYPES:
			edit = 0
		if edit:
			view = 1
		merged[dt] = {"view": view, "edit": edit}
	return merged


def _require_mcp_admin() -> None:
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _require_app_permission

	_require_app_permission("settings.manage_mcp")


def _serialize_key(store: dict) -> dict:
	secret = _reveal_secret(store)
	return {
		"token": _format_token(store["key_id"], secret),
		"key_id": store.get("key_id"),
		"created_at": store.get("created_at"),
		"created_by": store.get("created_by"),
		"acting_user": store.get("acting_user"),
		"api_path": "/mcp",
		"gateway_version": GATEWAY_VERSION,
	}


def _matrix_counts(matrix: dict) -> dict:
	return {
		"total": len(matrix),
		"view": sum(1 for v in matrix.values() if v.get("view")),
		"edit": sum(1 for v in matrix.values() if v.get("edit")),
	}


@frappe.whitelist()
def get_mcp_link():
	"""Return the MCP key (Settings, admin UI). Creates one if missing."""
	_require_mcp_admin()
	store = _ensure_token_store()
	matrix = load_mcp_matrix()
	return {**_serialize_key(store), "enabled": True, "matrix": _matrix_counts(matrix)}


@frappe.whitelist()
def rotate_mcp_link():
	"""Replace the MCP key. Connected AI assistants must paste the new key."""
	_require_mcp_admin()
	old = _load_schema_store(TOKEN_SCOPE)
	fresh = _new_token_store(acting_user=old.get("acting_user"))
	plain = fresh.pop("_plain_secret")
	_save_schema_store(TOKEN_SCOPE, fresh)
	return {**_serialize_key({**fresh, "_plain_secret": plain}), "rotated": True}


@frappe.whitelist()
def set_mcp_acting_user(user=None):
	"""Bind the MCP key to an acting ERP user (defaults to the creator).

	The gateway calls domain APIs with this user so staff permission checks
	(``_require_app_permission``) keep applying to agent traffic.
	"""
	_require_mcp_admin()
	user = str(user or "").strip()
	if not user or user == "Guest":
		frappe.throw(_("A valid ERP user is required"))
	if not frappe.db.exists("User", user):
		frappe.throw(_("User {0} not found").format(user), frappe.DoesNotExistError)
	if cint(frappe.db.get_value("User", user, "enabled")) == 0:
		frappe.throw(_("User {0} is disabled").format(user))
	store = _ensure_token_store()
	store.pop("_plain_secret", None)
	store["acting_user"] = user
	_save_schema_store(TOKEN_SCOPE, store)
	return {"ok": True, "acting_user": user}


@frappe.whitelist()
def get_mcp_matrix():
	"""Return the full DocType × view/edit matrix (defaults + saved overrides)."""
	_require_mcp_admin()
	saved_matrix = load_mcp_matrix()
	rows = []
	for row in MCP_DOCTYPE_MATRIX:
		dt = row["doctype"]
		perms = saved_matrix.get(dt) or {"view": 0, "edit": 0}
		locked = dt in EDIT_LOCKED_DOCTYPES
		rows.append(
			{
				"doctype": dt,
				"group": row["group"],
				"label_en": row["label_en"],
				"label_es": row["label_es"],
				"view": perms["view"],
				"edit": 0 if locked else perms["edit"],
				"edit_locked": locked,
			}
		)
	return {"rows": rows, "groups": MATRIX_GROUPS, "version": GATEWAY_VERSION}


@frappe.whitelist()
def save_mcp_matrix(rows=None):
	"""Full-replace the matrix. UI sends the whole grid back.

	``rows`` is a list of {"doctype", "view", "edit"}. Edit implies view;
	edit-locked DocTypes always store edit=0.
	"""
	_require_mcp_admin()
	if isinstance(rows, str):
		rows = _parse_json(rows, None)
	# An empty grid would silently reset every override — the UI always sends
	# the full matrix, so treat [] as a dirty client payload.
	if not isinstance(rows, list) or not rows:
		frappe.throw(_("rows must be a non-empty list of {doctype, view, edit}"))
	clean: dict[str, dict] = {}
	for item in rows:
		if not isinstance(item, dict):
			frappe.throw(_("Each matrix row must be an object"))
		dt = str(item.get("doctype") or "").strip()
		if dt not in KNOWN_DOCTYPES:
			frappe.throw(_("DocType {0} is not allowlisted for MCP").format(dt or "(blank)"))
		view = 1 if cint(item.get("view")) else 0
		edit = 1 if cint(item.get("edit")) else 0
		if dt in EDIT_LOCKED_DOCTYPES:
			edit = 0
		if edit:
			view = 1
		clean[dt] = {"view": view, "edit": edit}
	_save_schema_store(MATRIX_SCOPE, {"doctypes": clean})
	return get_mcp_matrix()


@frappe.whitelist()
def get_mcp_audit(limit=50):
	"""Recent MCP gateway audit rows (newest first)."""
	_require_mcp_admin()
	limit = max(1, min(cint(limit) or 50, 200))
	frappe.flags.ignore_permissions = True
	rows = frappe.get_all(
		"Table Extra Data",
		filters={"scope": AUDIT_SCOPE},
		fields=["row_key", "data_json", "creation"],
		order_by="creation desc",
		limit_page_length=limit,
		ignore_permissions=True,
	)
	items = []
	for row in rows:
		data = _parse_json(row.data_json, {})
		if isinstance(data, dict):
			data["_created"] = str(row.creation)
			items.append(data)
	return {"items": items, "total": len(items)}
