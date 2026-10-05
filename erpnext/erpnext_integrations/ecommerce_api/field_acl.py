"""Per-group field ACL matrix for Operaciones (lectura / recomendacion / escritura).

Settings UI + persistence only for now. Runtime enforcement on ops screens is deferred
(see local_docs/proposals/i034_field_acl_matrix.md).
"""

from __future__ import annotations

import json

import frappe
from frappe import _

# Table Extra Schema scope — no migrate required.
FIELD_ACL_STORE_SCOPE = "settings.staff_group_field_acl"

# Permission modes on field rows.
MODES = ("read", "recommend", "write")
# Create / Delete actions: no lectura.
ACTION_MODES = ("recommend", "write")


def _L(en: str, es: str, zh: str) -> dict:
	return {"label_en": en, "label_es": es, "label_zh": zh}


def _action(row_id: str, **labels) -> dict:
	return {"id": row_id, "kind": "action", "modes": list(ACTION_MODES), **labels}


def _scope(row_id: str, **labels) -> dict:
	"""Solo tuyos: per-mode ownership scope (lectura / recomendación / escritura)."""
	return {"id": row_id, "kind": "scope", "modes": list(MODES), **labels}


def _field(row_id: str, **labels) -> dict:
	return {"id": row_id, "kind": "field", "modes": list(MODES), **labels}


# Curated business fields (exclude rare metadata: owner, creation, modified, idx, …).
FIELD_ACL_DOMAINS = [
	{
		"id": "productos",
		**_L("Products", "Productos", "商品"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("titulo", **_L("Title", "Título", "标题")),
			_field("marca", **_L("Brand", "Marca", "品牌")),
			_field("imagen", **_L("Image", "Imagen", "图片")),
			_field("categoria", **_L("Category", "Categoría", "分类")),
			_field("precio", **_L("Price", "Precio", "售价")),
			_field("costo", **_L("Cost", "Costo", "成本")),
			_field("barcode", **_L("Barcode", "Cód. de barras", "条码")),
			_field("tamanio", **_L("Size", "Tamaño", "规格")),
			_field("unidad_peso", **_L("Weight unit", "Unidad peso", "重量单位")),
			_field("peso_min", **_L("Min weight", "Peso mín", "最小重量")),
			_field("peso_max", **_L("Max weight", "Peso máx", "最大重量")),
		],
	},
	{
		"id": "compras",
		**_L("Purchases", "Compras", "采购"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("proveedor", **_L("Supplier", "Proveedor", "供应商")),
			_field("fecha", **_L("Date", "Fecha", "日期")),
			_field("line_qty", **_L("Line qty", "Cant. línea", "行数量")),
			_field("line_costo", **_L("Line cost", "Costo línea", "行成本")),
			_field("total", **_L("Total", "Total", "合计")),
			_field("estado", **_L("Status", "Estado", "状态")),
		],
	},
	{
		"id": "pedidos",
		**_L("Orders", "Pedidos", "订单"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("cliente", **_L("Customer", "Cliente", "客户")),
			_field("lineas", **_L("Lines", "Líneas", "明细")),
			_field("totales", **_L("Totals", "Totales", "合计")),
			_field("estado", **_L("Status", "Estado", "状态")),
			_field("direccion_entrega", **_L("Delivery address", "Dirección entrega", "收货地址")),
		],
	},
	{
		"id": "mats",
		**_L("Materials", "Mats", "物料"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("titulo", **_L("Title", "Título", "标题")),
			_field("cantidad", **_L("Qty", "Cantidad", "数量")),
			_field("almacen", **_L("Warehouse", "Almacén", "仓库")),
			_field("costo", **_L("Cost", "Costo", "成本")),
		],
	},
	{
		"id": "lotes",
		**_L("Batches", "Lotes", "批次"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("codigo", **_L("Code", "Código", "编号")),
			_field("item", **_L("Item", "Ítem", "商品")),
			_field("expira", **_L("Expiry", "Expira", "到期")),
			_field("qty", **_L("Qty", "Cantidad", "数量")),
		],
	},
	{
		"id": "clientes",
		**_L("Customers", "Clientes", "客户"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("nombre", **_L("Name", "Nombre", "名称")),
			_field("telefono", **_L("Phone", "Teléfono", "电话")),
			_field("email", **_L("Email", "Email", "邮箱")),
			_field("direccion", **_L("Address", "Dirección", "地址")),
			_field("documento", **_L("Document", "Documento", "证件")),
			_field("notas", **_L("Notes", "Notas", "备注")),
		],
	},
	{
		"id": "proveedores",
		**_L("Suppliers", "Proveedores", "供应商"),
		"rows": [
			_action("create", **_L("Create New", "Crear nuevo", "新建")),
			_action("delete", **_L("Delete", "Eliminar", "删除")),
			_scope("own_only", **_L("Only yours", "Solo tuyos", "仅自己的")),
			_field("nombre", **_L("Name", "Nombre", "名称")),
			_field("telefono", **_L("Phone", "Teléfono", "电话")),
			_field("email", **_L("Email", "Email", "邮箱")),
			_field("direccion", **_L("Address", "Dirección", "地址")),
			_field("condiciones_pago", **_L("Payment terms", "Condiciones pago", "付款条件")),
		],
	},
]

DOMAIN_IDS = {d["id"] for d in FIELD_ACL_DOMAINS}
DOMAIN_BY_ID = {d["id"]: d for d in FIELD_ACL_DOMAINS}


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _empty_mode() -> dict:
	return {"read": False, "recommend": False, "write": False}


def _bool(v) -> bool:
	if v is True or v is False:
		return v
	if v in (1, "1", "true", "True", "yes", "YES"):
		return True
	return False


def _normalize_row_modes(raw, allowed: tuple[str, ...]) -> dict:
	src = raw if isinstance(raw, dict) else {}
	out = _empty_mode()
	for m in allowed:
		out[m] = _bool(src.get(m))
	# Implication: write or recommend ⇒ read (when read is allowed).
	if (out["write"] or out["recommend"]) and "read" in allowed:
		out["read"] = True
	# No lectura ⇒ clear recommend + write.
	if "read" in allowed and not out["read"]:
		out["recommend"] = False
		out["write"] = False
	return out


def empty_domain_acl(domain_id: str) -> dict:
	domain = DOMAIN_BY_ID.get(domain_id)
	if not domain:
		return {"rows": {}}
	rows = {}
	for row in domain["rows"]:
		rows[row["id"]] = _normalize_row_modes({}, tuple(row["modes"]))
	return {"rows": rows}


def empty_group_acl() -> dict:
	return {d["id"]: empty_domain_acl(d["id"]) for d in FIELD_ACL_DOMAINS}


def _rw(**kwargs) -> dict:
	"""Build a mode dict; missing keys default False. Shortcuts: r/s/w."""
	base = _empty_mode()
	if "r" in kwargs:
		base["read"] = _bool(kwargs.pop("r"))
	if "s" in kwargs:
		base["recommend"] = _bool(kwargs.pop("s"))
	if "w" in kwargs:
		base["write"] = _bool(kwargs.pop("w"))
	for k, v in kwargs.items():
		if k in MODES:
			base[k] = _bool(v)
	if base["write"] or base["recommend"]:
		base["read"] = True
	return base


def _domain_acl(rows: dict, *, own_only=None) -> dict:
	"""Build domain block. ``own_only`` may be bool (legacy) or mode dict."""
	merged = dict(rows or {})
	if own_only is True:
		merged["own_only"] = _rw(r=True, s=True, w=True)
	elif own_only is False:
		merged["own_only"] = _empty_mode()
	elif isinstance(own_only, dict):
		merged["own_only"] = _normalize_row_modes(own_only, MODES)
	return {"rows": merged}


def _seed_admin() -> dict:
	acl = empty_group_acl()
	for domain_id, domain in DOMAIN_BY_ID.items():
		rows = {}
		for row in domain["rows"]:
			if row["kind"] == "scope":
				# Admin is company-wide — Solo tuyos off.
				rows[row["id"]] = _empty_mode()
			elif row["kind"] == "action":
				rows[row["id"]] = _rw(w=True)
			else:
				rows[row["id"]] = _rw(r=True, w=True)
		acl[domain_id] = _domain_acl(rows)
	return acl


def _seed_ventas() -> dict:
	acl = empty_group_acl()
	# Clientes — own (read/recommend/write only on own records)
	cli_rows = {
		"own_only": _rw(r=True, s=True, w=True),
		"create": _rw(w=True),
		"delete": _empty_mode(),
		"nombre": _rw(r=True, w=True),
		"telefono": _rw(r=True, w=True),
		"email": _rw(r=True, w=True),
		"direccion": _rw(r=True, w=True),
		"documento": _rw(r=True, w=True),
		"notas": _rw(r=True, w=True),
	}
	acl["clientes"] = _domain_acl({**empty_domain_acl("clientes")["rows"], **cli_rows})
	# Pedidos — own
	ped_rows = {
		"own_only": _rw(r=True, s=True, w=True),
		"create": _rw(w=True),
		"delete": _empty_mode(),
		"cliente": _rw(r=True, w=True),
		"lineas": _rw(r=True, w=True),
		"totales": _rw(r=True, w=True),
		"estado": _rw(r=True, w=True),
		"direccion_entrega": _rw(r=True, w=True),
	}
	acl["pedidos"] = _domain_acl({**empty_domain_acl("pedidos")["rows"], **ped_rows})
	# Productos — suggest catalog edits; never cost
	prod_rows = {
		"titulo": _rw(r=True, s=True),
		"marca": _rw(r=True, s=True),
		"imagen": _rw(r=True, s=True),
		"categoria": _rw(r=True, s=True),
		"precio": _rw(r=True, s=True),
		"costo": _empty_mode(),
		"barcode": _rw(r=True, s=True),
		"tamanio": _rw(r=True, s=True),
		"unidad_peso": _rw(r=True, s=True),
		"peso_min": _rw(r=True, s=True),
		"peso_max": _rw(r=True, s=True),
	}
	acl["productos"] = _domain_acl({**empty_domain_acl("productos")["rows"], **prod_rows})
	# Proveedores — read + suggest
	prov_rows = {
		"nombre": _rw(r=True, s=True),
		"telefono": _rw(r=True, s=True),
		"email": _rw(r=True, s=True),
		"direccion": _rw(r=True, s=True),
		"condiciones_pago": _rw(r=True, s=True),
	}
	acl["proveedores"] = _domain_acl({**empty_domain_acl("proveedores")["rows"], **prov_rows})
	# Compras / mats / lotes — title-like read only where useful
	acl["compras"]["rows"]["proveedor"] = _rw(r=True)
	acl["mats"]["rows"]["titulo"] = _rw(r=True)
	acl["lotes"]["rows"]["codigo"] = _rw(r=True)
	acl["lotes"]["rows"]["item"] = _rw(r=True)
	return acl


def _seed_caja() -> dict:
	acl = empty_group_acl()
	acl["productos"]["rows"].update(
		{
			"titulo": _rw(r=True),
			"precio": _rw(r=True),
			"barcode": _rw(r=True),
			"costo": _empty_mode(),
		}
	)
	acl["clientes"]["rows"].update(
		{
			"nombre": _rw(r=True),
			"telefono": _rw(r=True),
		}
	)
	acl["pedidos"]["rows"].update(
		{
			"totales": _rw(r=True),
			"estado": _rw(r=True),
			"lineas": _rw(r=True),
		}
	)
	return acl


def _seed_repositor() -> dict:
	acl = empty_group_acl()
	acl["productos"]["rows"].update(
		{
			"create": _rw(s=True),
			"titulo": _rw(r=True, s=True),
			"marca": _rw(r=True, s=True),
			"imagen": _rw(r=True, s=True),
			"categoria": _rw(r=True, s=True),
			"precio": _rw(r=True),
			"costo": _empty_mode(),
			"barcode": _rw(r=True, s=True),
			"tamanio": _rw(r=True, s=True),
			"unidad_peso": _rw(r=True, s=True),
			"peso_min": _rw(r=True, s=True),
			"peso_max": _rw(r=True, s=True),
		}
	)
	acl["compras"]["rows"].update(
		{
			"create": _rw(w=True),
			"proveedor": _rw(r=True, w=True),
			"fecha": _rw(r=True, w=True),
			"line_qty": _rw(r=True, w=True),
			"line_costo": _rw(r=True, w=True),
			"total": _rw(r=True),
			"estado": _rw(r=True, w=True),
		}
	)
	acl["lotes"]["rows"].update(
		{
			"create": _rw(w=True),
			"codigo": _rw(r=True, w=True),
			"item": _rw(r=True, w=True),
			"expira": _rw(r=True, w=True),
			"qty": _rw(r=True, w=True),
		}
	)
	acl["mats"]["rows"].update(
		{
			"create": _rw(s=True),
			"titulo": _rw(r=True, w=True),
			"cantidad": _rw(r=True, w=True),
			"almacen": _rw(r=True, w=True),
			"costo": _empty_mode(),
		}
	)
	acl["proveedores"]["rows"]["nombre"] = _rw(r=True)
	return acl


def _seed_driver() -> dict:
	acl = empty_group_acl()
	# Assigned trips only: Solo tuyos lectura (recommend/write off)
	acl["pedidos"] = _domain_acl(
		{
			**empty_domain_acl("pedidos")["rows"],
			"own_only": _rw(r=True),
			"cliente": _rw(r=True),
			"direccion_entrega": _rw(r=True),
			"lineas": _rw(r=True),
			"totales": _rw(r=True),
			"estado": _rw(r=True),
		},
	)
	acl["clientes"] = _domain_acl(
		{
			**empty_domain_acl("clientes")["rows"],
			"own_only": _rw(r=True),
			"nombre": _rw(r=True),
			"direccion": _rw(r=True),
			# phone closed by default
			"telefono": _empty_mode(),
		},
	)
	return acl


# Match Employee Group titles (case-insensitive) → seed factory.
STARTER_FIELD_ACL_BY_TITLE = {
	"admin": _seed_admin,
	"ventas": _seed_ventas,
	"sales": _seed_ventas,
	"caja": _seed_caja,
	"repositor": _seed_repositor,
	"driver": _seed_driver,
}


def _load_field_acl_store() -> dict:
	if not frappe.db.exists("Table Extra Schema", FIELD_ACL_STORE_SCOPE):
		return {}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", FIELD_ACL_STORE_SCOPE)
	data = _parse_json(doc.columns_json, {})
	return data if isinstance(data, dict) else {}


def _save_field_acl_store(data: dict) -> None:
	payload = json.dumps(data or {}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", FIELD_ACL_STORE_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", FIELD_ACL_STORE_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Table Extra Schema",
				"scope": FIELD_ACL_STORE_SCOPE,
				"columns_json": payload,
			}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def normalize_group_acl(raw) -> dict:
	"""Coerce arbitrary JSON into a full domain→rows matrix.

	Migrates legacy ``own_only: bool`` into ``rows.own_only`` mode checkboxes.
	"""
	src = raw if isinstance(raw, dict) else {}
	out = empty_group_acl()
	for domain_id, domain in DOMAIN_BY_ID.items():
		block = src.get(domain_id)
		if not isinstance(block, dict):
			continue
		row_src = block.get("rows") if isinstance(block.get("rows"), dict) else {}
		rows = dict(out[domain_id]["rows"])
		for row in domain["rows"]:
			if row["id"] in row_src:
				rows[row["id"]] = _normalize_row_modes(row_src[row["id"]], tuple(row["modes"]))
		# Legacy top-level bool → scope row
		legacy = block.get("own_only")
		if "own_only" not in row_src:
			if legacy is True:
				rows["own_only"] = _rw(r=True, s=True, w=True)
			elif isinstance(legacy, dict):
				rows["own_only"] = _normalize_row_modes(legacy, MODES)
		out[domain_id] = {"rows": rows}
	return out


def seed_acl_for_group_title(title: str) -> dict:
	key = (title or "").strip().lower()
	factory = STARTER_FIELD_ACL_BY_TITLE.get(key)
	if factory:
		return normalize_group_acl(factory())
	return empty_group_acl()


def get_group_acl(group_name: str) -> dict:
	store = _load_field_acl_store()
	raw = store.get(group_name)
	if raw is None:
		# Fall back to seed by Employee Group title if known starter.
		title = frappe.db.get_value("Employee Group", group_name, "employee_group_name") or group_name
		return seed_acl_for_group_title(title)
	return normalize_group_acl(raw)


def ensure_starter_field_acls(group_specs: list[dict] | None = None) -> dict:
	"""Seed field ACL for starter groups that have no store entry yet.

	``group_specs`` items: ``{name, employee_group_name}``. Does not overwrite
	existing custom matrices.
	"""
	store = _load_field_acl_store()
	created = []
	skipped = []
	dirty = False
	specs = group_specs or []
	if not specs:
		# Discover known starter titles already present as Employee Groups.
		for title in STARTER_FIELD_ACL_BY_TITLE:
			for row in frappe.get_all(
				"Employee Group",
				fields=["name", "employee_group_name"],
				ignore_permissions=True,
			):
				if str(row.employee_group_name or "").strip().lower() == title:
					specs.append({"name": row.name, "employee_group_name": row.employee_group_name})
	seen = set()
	for spec in specs:
		name = spec.get("name")
		if not name or name in seen:
			continue
		seen.add(name)
		if name in store and isinstance(store.get(name), dict) and store[name]:
			skipped.append(name)
			continue
		title = spec.get("employee_group_name") or name
		store[name] = seed_acl_for_group_title(title)
		created.append(name)
		dirty = True
	if dirty:
		_save_field_acl_store(store)
	return {"created": created, "skipped": skipped}


def drop_group_field_acl(group_name: str) -> None:
	store = _load_field_acl_store()
	if group_name in store:
		store.pop(group_name, None)
		_save_field_acl_store(store)


def _union_bool(a: bool, b: bool) -> bool:
	return bool(a or b)


def resolve_user_field_acl(group_names: list[str]) -> dict:
	"""Union of group matrices.

	Field grants OR across groups. Solo tuyos (own_only) modes use AND —
	a mode stays scoped to own records only if every attached group scopes it.
	"""
	if not group_names:
		return empty_group_acl()
	merged = empty_group_acl()
	own_votes: dict[str, list[dict]] = {d: [] for d in DOMAIN_IDS}
	for gname in group_names:
		acl = get_group_acl(gname)
		for domain_id in DOMAIN_IDS:
			block = acl.get(domain_id) or empty_domain_acl(domain_id)
			rows = block.get("rows") or {}
			own_votes[domain_id].append(
				_normalize_row_modes(rows.get("own_only"), MODES)
			)
			for row_id, modes in rows.items():
				if row_id == "own_only":
					continue
				cur = merged[domain_id]["rows"].setdefault(row_id, _empty_mode())
				for m in MODES:
					cur[m] = _union_bool(cur.get(m), (modes or {}).get(m))
				if cur["write"] or cur["recommend"]:
					cur["read"] = True
	for domain_id, votes in own_votes.items():
		if not votes:
			merged[domain_id]["rows"]["own_only"] = _empty_mode()
			continue
		# Strictest ownership per mode
		own = _empty_mode()
		for m in MODES:
			own[m] = all(_bool(v.get(m)) for v in votes)
		if own["write"] or own["recommend"]:
			own["read"] = True
		merged[domain_id]["rows"]["own_only"] = own
	return merged


@frappe.whitelist(allow_guest=True)
def get_field_acl_catalog():
	"""Domains + row catalog for the Settings matrix editor."""
	return {"ok": True, "domains": FIELD_ACL_DOMAINS, "modes": list(MODES)}


@frappe.whitelist(allow_guest=True)
def get_group_field_acl(name=None):
	"""Load field ACL matrix for one Employee Group."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _require_app_permission

	_require_app_permission("settings.manage_groups")
	name = str(name or "").strip()
	if not name or not frappe.db.exists("Employee Group", name):
		frappe.throw(_("Group {0} not found").format(name))
	return {"ok": True, "name": name, "acl": get_group_acl(name)}


@frappe.whitelist(allow_guest=True)
def save_group_field_acl(name=None, acl=None):
	"""Persist field ACL matrix for one Employee Group."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import _require_app_permission

	_require_app_permission("settings.manage_groups")
	name = str(name or "").strip()
	if not name or not frappe.db.exists("Employee Group", name):
		frappe.throw(_("Group {0} not found").format(name))
	parsed = _parse_json(acl, {})
	normalized = normalize_group_acl(parsed)
	store = _load_field_acl_store()
	store[name] = normalized
	_save_field_acl_store(store)
	return {"ok": True, "name": name, "acl": normalized}


@frappe.whitelist(allow_guest=True)
def get_user_field_acl(username=None):
	"""Resolved union of field ACL for acting user / username (for later ops gates)."""
	from erpnext.erpnext_integrations.ecommerce_api.employee_api import (
		_acting_username,
		get_user_app_permissions,
	)

	user = str(username or "").strip() or _acting_username()
	if not user:
		return {"ok": True, "acl": empty_group_acl(), "source": "none", "groups": []}
	info = get_user_app_permissions(user)
	if info.get("source") == "admin" or "*" in (info.get("permissions") or []):
		return {"ok": True, "acl": _seed_admin(), "source": "admin", "groups": []}
	group_names = list(info.get("groups") or [])
	acl = resolve_user_field_acl(group_names)
	return {
		"ok": True,
		"acl": acl,
		"source": "groups" if group_names else "empty",
		"groups": group_names,
	}
