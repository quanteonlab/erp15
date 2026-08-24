"""Employee + Employee Group APIs for Tables > Empleados."""

from __future__ import annotations

import json
import secrets
import string

import frappe
from frappe import _
from frappe.utils import cint, flt, today
from frappe.utils.password import update_password as _update_password

from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

# JSON map of Employee Group name -> permission ids (Table Extra Schema reuse; no migrate).
PERM_STORE_SCOPE = "settings.staff_group_permissions"

# App permissions that match the current Next.js UI (not ERPNext desk roles).
APP_PERMISSIONS = [
	{
		"id": "ops.pos",
		"group": "operaciones",
		"label_en": "Point of Sale",
		"label_es": "Punto de venta",
		"label_zh": "收银台",
		"desc_en": "Open the POS, add to cart, and checkout.",
		"desc_es": "Abrir el POS, armar el carrito y cobrar.",
		"desc_zh": "打开收银台、加购并结账。",
	},
	{
		"id": "ops.catalog",
		"group": "operaciones",
		"label_en": "Catalog",
		"label_es": "Catálogo",
		"label_zh": "目录",
		"desc_en": "Browse the public/internal catalog.",
		"desc_es": "Ver el catálogo interno/público.",
		"desc_zh": "浏览商品目录。",
	},
	{
		"id": "ops.receiving",
		"group": "operaciones",
		"label_en": "Receiving",
		"label_es": "Recepción",
		"label_zh": "收货",
		"desc_en": "Record inbound stock and create draft products.",
		"desc_es": "Registrar mercadería entrante y productos borrador.",
		"desc_zh": "录入进货并创建草稿商品。",
	},
	{
		"id": "log.reports",
		"group": "logistica",
		"label_en": "Reports",
		"label_es": "Reportes",
		"label_zh": "报表",
		"desc_en": "Logistics reports placeholder.",
		"desc_es": "Reportes de logística.",
		"desc_zh": "物流报表。",
	},
	{
		"id": "log.accounting",
		"group": "logistica",
		"label_en": "Accounting sheet",
		"label_es": "Hoja contable",
		"label_zh": "会计表",
		"desc_en": "Spreadsheet of daily figures and cash totals.",
		"desc_es": "Planilla de cifras diarias y totales de caja.",
		"desc_zh": "每日数字与收银合计表。",
	},
	{
		"id": "log.sections",
		"group": "logistica",
		"label_en": "Store sections map",
		"label_es": "Mapa de secciones",
		"label_zh": "店内分区图",
		"desc_en": "Draw and edit floor sections and SKU placement.",
		"desc_es": "Dibujar y editar secciones del piso y ubicación de SKUs.",
		"desc_zh": "绘制并编辑楼层分区与 SKU 摆放。",
	},
	{
		"id": "tables.products",
		"group": "tablas",
		"label_en": "Products",
		"label_es": "Productos",
		"label_zh": "商品",
		"desc_en": "Product master: prices, barcodes, images, bulk edits.",
		"desc_es": "Maestro de productos: precios, códigos, imágenes, edición masiva.",
		"desc_zh": "商品主数据：价格、条码、图片、批量编辑。",
	},
	{
		"id": "tables.rentability",
		"group": "tablas",
		"label_en": "Rentability",
		"label_es": "Rentabilidad",
		"label_zh": "盈利",
		"desc_en": "All price lists, cost, margin, and inflation simulation.",
		"desc_es": "Todas las listas de precio, costo, margen y simulación de inflación.",
		"desc_zh": "全部价目表、成本、毛利与通胀模拟。",
	},
	{
		"id": "tables.promotions",
		"group": "tablas",
		"label_en": "Promotions",
		"label_es": "Promociones",
		"label_zh": "促销",
		"desc_en": "Pricing rules, bundles, and combos.",
		"desc_es": "Pricing rules, packs y combos.",
		"desc_zh": "定价规则、套装与组合。",
	},
	{
		"id": "tables.review",
		"group": "tablas",
		"label_en": "Review / approvals",
		"label_es": "Review / aprobaciones",
		"label_zh": "审核",
		"desc_en": "Approve or reject new products from receiving.",
		"desc_es": "Aprobar o rechazar productos nuevos de recepción.",
		"desc_zh": "批准或拒绝收货产生的新商品。",
	},
	{
		"id": "tables.variants",
		"group": "tablas",
		"label_en": "Variants",
		"label_es": "Variaciones",
		"label_zh": "变体",
		"desc_en": "Product families grouped by unit/pack.",
		"desc_es": "Familias de producto (unidad/pack).",
		"desc_zh": "按单品/包装分组的商品家族。",
	},
	{
		"id": "tables.crm",
		"group": "tablas",
		"label_en": "CRM",
		"label_es": "CRM",
		"label_zh": "客户",
		"desc_en": "Customers from orders and suppliers from receiving.",
		"desc_es": "Clientes (pedidos) y proveedores (recepción).",
		"desc_zh": "订单客户与收货供应商。",
	},
	{
		"id": "tables.employees",
		"group": "tablas",
		"label_en": "Employees",
		"label_es": "Empleados",
		"label_zh": "员工",
		"desc_en": "Staff roster, branches, and group assignment.",
		"desc_es": "Plantilla, sucursales y asignación de grupos.",
		"desc_zh": "员工名册、门店与组分配。",
	},
	{
		"id": "tables.cajas",
		"group": "tablas",
		"label_en": "Cash registers",
		"label_es": "Cajas",
		"label_zh": "收银机",
		"desc_en": "POS profiles, sessions, and register totals.",
		"desc_es": "Perfiles POS, sesiones y totales de caja.",
		"desc_zh": "POS 配置、班次与收银合计。",
	},
	{
		"id": "tables.orders",
		"group": "tablas",
		"label_en": "Orders",
		"label_es": "Pedidos",
		"label_zh": "订单",
		"desc_en": "Guest preorders: confirm, prepare, collect.",
		"desc_es": "Pedidos de invitados: confirmar, preparar, cobrar.",
		"desc_zh": "访客预订单：确认、备货、收款。",
	},
	{
		"id": "tools.sync",
		"group": "herramientas",
		"label_en": "Sync",
		"label_es": "Sincronizar",
		"label_zh": "同步",
		"desc_en": "Push local receiving/POS queues to ERPNext.",
		"desc_es": "Enviar colas locales de recepción/POS a ERPNext.",
		"desc_zh": "将本地收货/POS 队列推送到 ERPNext。",
	},
	{
		"id": "tools.labels",
		"group": "herramientas",
		"label_en": "Labels / barcodes",
		"label_es": "Etiquetas / códigos",
		"label_zh": "标签 / 条码",
		"desc_en": "Print barcode and price labels.",
		"desc_es": "Imprimir etiquetas de código de barras y precio.",
		"desc_zh": "打印条码与价格标签。",
	},
	{
		"id": "tools.migrate",
		"group": "herramientas",
		"label_en": "Migrate / CSV",
		"label_es": "Migrar / CSV",
		"label_zh": "迁移 / CSV",
		"desc_en": "Import catalog CSV and images.",
		"desc_es": "Importar catálogo CSV e imágenes.",
		"desc_zh": "导入商品 CSV 与图片。",
	},
	{
		"id": "tools.settings",
		"group": "herramientas",
		"label_en": "Settings",
		"label_es": "Ajustes",
		"label_zh": "设置",
		"desc_en": "Open Tools > Settings (POS, catalog, automation).",
		"desc_es": "Abrir Herramientas > Ajustes (POS, catálogo, automatización).",
		"desc_zh": "打开工具 > 设置（POS、目录、自动化）。",
	},
	{
		"id": "employees.create_user",
		"group": "empleados",
		"label_en": "Create staff login",
		"label_es": "Crear usuario de staff",
		"label_zh": "创建员工登录",
		"desc_en": "Create or link a User and generate a one-time password.",
		"desc_es": "Crear o vincular un User y generar una contraseña de un solo uso.",
		"desc_zh": "创建或关联用户并生成一次性密码。",
	},
	{
		"id": "employees.reset_password",
		"group": "empleados",
		"label_en": "Reset staff password",
		"label_es": "Resetear contraseña",
		"label_zh": "重置员工密码",
		"desc_en": "Generate a new random password for a linked user.",
		"desc_es": "Generar una nueva contraseña aleatoria para el usuario vinculado.",
		"desc_zh": "为已关联用户生成新随机密码。",
	},
	{
		"id": "employees.view_salary",
		"group": "empleados",
		"label_en": "View salary (CTC)",
		"label_es": "Ver sueldo (CTC)",
		"label_zh": "查看薪资",
		"desc_en": "See monthly CTC on the employees table.",
		"desc_es": "Ver el CTC mensual en la tabla de empleados.",
		"desc_zh": "在员工表中查看月薪 CTC。",
	},
	{
		"id": "employees.edit",
		"group": "empleados",
		"label_en": "Edit employees",
		"label_es": "Editar empleados",
		"label_zh": "编辑员工",
		"desc_en": "Change name, branch, contact, status, and group membership.",
		"desc_es": "Cambiar nombre, sucursal, contacto, estado y grupos.",
		"desc_zh": "修改姓名、门店、联系方式、状态与分组。",
	},
	{
		"id": "settings.manage_groups",
		"group": "empleados",
		"label_en": "Manage groups & permissions",
		"label_es": "Administrar grupos y permisos",
		"label_zh": "管理组与权限",
		"desc_en": "Create groups and attach/detach permissions (Settings).",
		"desc_es": "Crear grupos y agregar/quitar permisos (Ajustes).",
		"desc_zh": "在设置中创建组并附加/移除权限。",
	},
	{
		"id": "settings.view_link_key",
		"group": "empleados",
		"label_en": "View app link key & devices",
		"label_es": "Ver clave de app y dispositivos",
		"label_zh": "查看应用密钥与设备",
		"desc_en": "See the permanent API link token and every connected mobile/desktop device.",
		"desc_es": "Ver la clave permanente de la API y todos los dispositivos móviles/escritorio conectados.",
		"desc_zh": "查看永久 API 连接密钥以及已连接的手机/电脑设备。",
	},
]

KNOWN_PERMISSION_IDS = {p["id"] for p in APP_PERMISSIONS}

PERMISSION_TO_ROLES = {
	"ops.pos": ["Sales User"],
	"ops.catalog": ["Sales User"],
	"ops.receiving": ["Stock User", "Purchase User"],
	"log.reports": ["Accounts User"],
	"log.accounting": ["Accounts User"],
	"log.sections": ["Stock User"],
	"tables.products": ["Stock User", "Item Manager"],
	"tables.rentability": ["Stock User"],
	"tables.promotions": ["Sales Manager"],
	"tables.review": ["Stock User", "Purchase User"],
	"tables.variants": ["Item Manager"],
	"tables.crm": ["Sales User"],
	"tables.employees": ["HR User"],
	"tables.cajas": ["Accounts User"],
	"tables.orders": ["Sales User"],
	"tools.sync": ["Stock Manager"],
	"tools.labels": ["Stock User"],
	"tools.migrate": ["Stock Manager"],
	"tools.settings": ["HR User"],
	"employees.create_user": ["HR Manager"],
	"employees.reset_password": ["HR Manager"],
	"employees.view_salary": ["HR Manager"],
	"employees.edit": ["HR User"],
	"settings.manage_groups": ["HR Manager"],
	"settings.view_link_key": ["System Manager"],
}

PROTECTED_ROLES = {"All", "Guest", "Administrator"}

# Default groups: created automatically if missing. Existing non-empty permission
# lists are never overwritten.
_STARTER_REPOSITOR = [
	"ops.receiving",
	"ops.catalog",
	"tables.products",
	"tables.review",
	"tables.variants",
	"log.sections",
	"tools.labels",
	"tools.sync",
]
_STARTER_CAJA = [
	"ops.pos",
	"ops.catalog",
	"tables.orders",
	"tables.cajas",
	"log.accounting",
	"tools.labels",
]


STARTER_STAFF_GROUPS = [
	{"employee_group_name": "repositor", "permissions": list(_STARTER_REPOSITOR)},
	{"employee_group_name": "caja", "permissions": list(_STARTER_CAJA)},
	{
		"employee_group_name": "admin",
		"permissions": sorted(KNOWN_PERMISSION_IDS),
	},
]


def _parse_json(raw, default):
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		return default


def _rand_password(length: int = 16) -> str:
	"""Password that satisfies typical Frappe zxcvbn minimum_password_score=2/3."""
	specials = "!@#$%^*?"
	alphabet = string.ascii_letters + string.digits + specials
	chars = [
		secrets.choice(string.ascii_uppercase),
		secrets.choice(string.ascii_lowercase),
		secrets.choice(string.digits),
		secrets.choice(specials),
	]
	chars += [secrets.choice(alphabet) for _ in range(max(0, length - 4))]
	secrets.SystemRandom().shuffle(chars)
	return "".join(chars)


def _set_user_password(user_name: str, password: str) -> None:
	_update_password(user=user_name, pwd=password, logout_all_sessions=False)


def _load_perm_store() -> dict:
	if not frappe.db.exists("Table Extra Schema", PERM_STORE_SCOPE):
		return {}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", PERM_STORE_SCOPE)
	data = _parse_json(doc.columns_json, {})
	return data if isinstance(data, dict) else {}


def _save_perm_store(data: dict) -> None:
	payload = json.dumps(data or {}, ensure_ascii=False)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", PERM_STORE_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", PERM_STORE_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": PERM_STORE_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _normalize_permission_ids(raw) -> list[str]:
	if isinstance(raw, str):
		raw = _parse_json(raw, [])
	if not isinstance(raw, list):
		return []
	out = []
	seen = set()
	for item in raw:
		key = str(item or "").strip()
		if key in KNOWN_PERMISSION_IDS and key not in seen:
			seen.add(key)
			out.append(key)
	return out


def _permissions_for_group(group_name: str) -> list[str]:
	store = _load_perm_store()
	return _normalize_permission_ids(store.get(group_name) or [])


def _acting_username() -> str:
	req = getattr(frappe.local, "request", None)
	if req is None:
		return ""
	try:
		return str(req.headers.get("X-ERP-Acting-User") or "").strip()
	except Exception:
		return ""


def _acting_perm_info() -> dict | None:
	cached = getattr(frappe.local, "_staff_acting_perm_info", "missing")
	if cached != "missing":
		return cached
	user = _acting_username()
	info = None
	if user and frappe.db.exists("User", user):
		info = get_user_app_permissions(user)
	frappe.local._staff_acting_perm_info = info
	return info


def _can_app(pid: str) -> bool:
	info = _acting_perm_info()
	if not info:
		return True
	if info.get("source") == "admin" or "*" in (info.get("permissions") or []):
		return True
	if info.get("source") == "groups":
		return pid in (info.get("permissions") or [])
	user = _acting_username()
	return _coarse_allows(user, pid) if user else True


def _require_app_permission(pid: str) -> None:
	if not _can_app(pid):
		frappe.throw(_("Not permitted ({0})").format(pid))


_POS_ROLES = {
	"Administrator",
	"System Manager",
	"Sales User",
	"Sales Manager",
	"Accounts User",
	"Accounts Manager",
	"Stock User",
	"Stock Manager",
}
_PAYMENT_ROLES = {
	"Administrator",
	"System Manager",
	"Accounts User",
	"Accounts Manager",
	"Sales Manager",
}
_RECEIVING_ROLES = {
	"Administrator",
	"System Manager",
	"Purchase User",
	"Purchase Manager",
	"Stock User",
	"Stock Manager",
}
_SYNC_ROLES = {"Administrator", "System Manager"}

# Same mapping as erpnext-ecommerce/lib/staff-permissions.ts COARSE_KEYS.
_COARSE_FLAG = {
	"ops.pos": "pos",
	"ops.catalog": "pos",
	"tables.orders": "pos",
	"tools.labels": "pos",
	"log.accounting": "payments",
	"tables.cajas": "payments",
	"log.reports": "payments",
	"ops.receiving": "receiving",
	"tables.products": "receiving",
	"tables.rentability": "receiving",
	"tables.promotions": "receiving",
	"tables.review": "receiving",
	"tables.variants": "receiving",
	"tables.crm": "receiving",
	"tables.employees": "receiving",
	"log.sections": "receiving",
	"tools.migrate": "receiving",
	"tools.settings": "receiving",
	"employees.create_user": "receiving",
	"employees.reset_password": "receiving",
	"employees.view_salary": "receiving",
	"employees.edit": "receiving",
	"settings.manage_groups": "receiving",
	"tools.sync": "sync",
}


def _coarse_allows(username: str, pid: str) -> bool:
	flag = _COARSE_FLAG.get(pid)
	if not flag:
		return False
	roles = set(frappe.get_roles(username))
	if flag == "pos":
		return bool(roles & _POS_ROLES)
	if flag == "payments":
		return bool(roles & _PAYMENT_ROLES)
	if flag == "receiving":
		return bool(roles & _RECEIVING_ROLES)
	if flag == "sync":
		return bool(roles & _SYNC_ROLES)
	return False


def _roles_from_permission_ids(permission_ids: list[str]) -> list[str]:
	roles = {"Employee"}
	for pid in permission_ids:
		for role in PERMISSION_TO_ROLES.get(pid, []):
			roles.add(role)
	return sorted(roles)


def _permission_ids_for_employee(employee: str) -> list[str]:
	groups = frappe.get_all(
		"Employee Group Table",
		filters={"employee": employee},
		pluck="parent",
		ignore_permissions=True,
	)
	store = _load_perm_store()
	seen = set()
	out = []
	for g in groups:
		for pid in _normalize_permission_ids(store.get(g) or []):
			if pid not in seen:
				seen.add(pid)
				out.append(pid)
	return out


def _apply_group_roles_to_user(user_id: str, employee: str) -> None:
	if not user_id:
		return
	perms = _permission_ids_for_employee(employee)
	if not perms:
		return
	_set_user_roles(user_id, _roles_from_permission_ids(perms))


def _serialize_employee(name: str) -> dict:
	frappe.flags.ignore_permissions = True
	emp = frappe.get_doc("Employee", name)
	groups = frappe.get_all(
		"Employee Group Table",
		filters={"employee": name},
		pluck="parent",
		order_by="parent",
		ignore_permissions=True,
	)
	roles: list[str] = []
	user_enabled = None
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		roles = [r for r in frappe.get_roles(emp.user_id) if r not in ("All", "Guest", "Desk User")]
		user_enabled = cint(frappe.db.get_value("User", emp.user_id, "enabled"))
	row = {
		"name": emp.name,
		"employee_name": emp.employee_name,
		"first_name": emp.first_name,
		"last_name": emp.last_name,
		"status": emp.status,
		"company": emp.company,
		"branch": emp.branch,
		"department": emp.department,
		"designation": emp.designation,
		"cell_number": emp.cell_number,
		"company_email": emp.company_email,
		"personal_email": emp.personal_email,
		"prefered_email": emp.prefered_email,
		"ctc": emp.ctc,
		"salary_currency": emp.salary_currency,
		"bio": emp.bio,
		"user_id": emp.user_id,
		"has_user": bool(emp.user_id),
		"user_enabled": user_enabled,
		"roles": roles,
		"permissions": _permission_ids_for_employee(emp.name),
		"groups": groups,
		"date_of_joining": str(emp.date_of_joining) if emp.date_of_joining else None,
		"modified": str(emp.modified) if emp.modified else None,
	}
	if not _can_app("employees.view_salary"):
		row["ctc"] = None
		row["salary_currency"] = None
		row["salary_hidden"] = True
	return row


@frappe.whitelist()
def list_app_permissions():
	"""Catalog of app permissions for Settings / employee group editors."""
	return {
		"permissions": APP_PERMISSIONS,
		"groups": [
			{"id": "operaciones", "label_en": "Operations", "label_es": "Operaciones", "label_zh": "运营"},
			{"id": "logistica", "label_en": "Logistics", "label_es": "Logística", "label_zh": "物流"},
			{"id": "tablas", "label_en": "Tables", "label_es": "Tablas", "label_zh": "表格"},
			{"id": "herramientas", "label_en": "Tools", "label_es": "Herramientas", "label_zh": "工具"},
			{"id": "empleados", "label_en": "Staff", "label_es": "Personal", "label_zh": "员工管理"},
		],
	}


@frappe.whitelist()
def get_user_app_permissions(username=None):
	"""Union of group permissions for a User (used at login).

	If the employee is in one or more groups, the union of those groups is the
	entire grant — even when the lists are empty (IAM restriction). Ungrouped
	users keep the previous ERPNext-role mapping (source=roles).
	"""
	username = (username or frappe.session.user or "").strip()
	if not username:
		frappe.throw(_("username is required"))
	if not frappe.db.exists("User", username):
		frappe.throw(_("User {0} not found").format(username))
	roles = frappe.get_roles(username)
	if "Administrator" in roles or "System Manager" in roles:
		return {"permissions": ["*"], "groups": [], "source": "admin"}
	emp = frappe.db.get_value("Employee", {"user_id": username}, "name")
	if not emp:
		return {"permissions": [], "groups": [], "source": "roles"}
	groups = frappe.get_all(
		"Employee Group Table",
		filters={"employee": emp},
		pluck="parent",
		ignore_permissions=True,
	)
	perms = _permission_ids_for_employee(emp)
	if groups:
		return {"permissions": perms, "groups": groups, "source": "groups"}
	return {"permissions": [], "groups": [], "source": "roles"}


@frappe.whitelist()
def list_employees(search=None, status=None, page=1, page_length=100, company=None):
	from erpnext.erpnext_integrations.ecommerce_api.company_context import company_scope

	_require_app_permission("tables.employees")
	page = max(1, cint(page) or 1)
	page_length = max(1, min(500, cint(page_length) or 100))
	filters = {}
	active = company_scope(company)
	if active:
		filters["company"] = active
	if status:
		filters["status"] = status
	or_filters = None
	if search and str(search).strip():
		q = f"%{str(search).strip()}%"
		or_filters = [
			["employee_name", "like", q],
			["name", "like", q],
			["user_id", "like", q],
			["branch", "like", q],
			["cell_number", "like", q],
		]
	names = frappe.get_all(
		"Employee",
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="employee_name asc",
		limit_start=(page - 1) * page_length,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = frappe.db.count("Employee", filters=filters)
	return {"rows": [_serialize_employee(n) for n in names], "total": total}


@frappe.whitelist()
def get_employee(name):
	_require_app_permission("tables.employees")
	if not frappe.db.exists("Employee", name):
		frappe.throw(_("Employee {0} not found").format(name))
	return _serialize_employee(name)


@frappe.whitelist()
def save_employee(name=None, data=None):
	"""Create or update Employee. data: JSON/dict of fields."""
	_require_app_permission("employees.edit")
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = data or {}
	if not _can_app("employees.view_salary"):
		data.pop("ctc", None)
	is_new = not name
	tracked = [
		"employee_name",
		"first_name",
		"last_name",
		"status",
		"branch",
		"department",
		"designation",
		"cell_number",
		"company_email",
		"personal_email",
		"prefered_email",
		"ctc",
		"bio",
		"date_of_joining",
	]

	frappe.flags.ignore_permissions = True
	if is_new:
		from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

		company = data.get("company") or resolve_company()
		if not company:
			company = frappe.db.get_value("Company", {}, "name")
		first = (data.get("first_name") or "").strip()
		last = (data.get("last_name") or "").strip()
		full = (data.get("employee_name") or "").strip() or f"{first} {last}".strip()
		if not first and full:
			parts = full.split()
			first = parts[0]
			last = " ".join(parts[1:]) if len(parts) > 1 else ""
		if not first:
			frappe.throw(_("First name / employee name is required"))
		doc = frappe.new_doc("Employee")
		doc.company = company
		doc.first_name = first
		doc.last_name = last or None
		doc.employee_name = full or first
		doc.status = data.get("status") or "Active"
		doc.date_of_joining = data.get("date_of_joining") or today()
		doc.gender = data.get("gender") or "Prefer not to say"
		doc.date_of_birth = data.get("date_of_birth") or "1990-01-01"
		for key in tracked:
			if key in data and data[key] is not None and key not in (
				"first_name",
				"last_name",
				"employee_name",
				"status",
				"date_of_joining",
			):
				doc.set(key, data[key])
		if "ctc" in data:
			doc.ctc = flt(data.get("ctc"))
		if data.get("company_email") or data.get("prefered_email"):
			doc.prefered_contact_email = "Company Email"
		doc.insert(ignore_permissions=True)
		log_field_changes(
			"Employee",
			doc.name,
			[("employee_name", None, doc.employee_name), ("status", None, doc.status)],
		)
	else:
		if not frappe.db.exists("Employee", name):
			frappe.throw(_("Employee {0} not found").format(name))
		doc = frappe.get_doc("Employee", name)
		changes = []
		for key in tracked:
			if key not in data:
				continue
			new_val = data[key]
			if key == "ctc":
				new_val = flt(new_val) if new_val not in (None, "") else None
			old_val = doc.get(key)
			if str(old_val or "") != str(new_val or ""):
				changes.append((key, old_val, new_val))
				doc.set(key, new_val)
		if data.get("first_name") or data.get("last_name") or data.get("employee_name"):
			first = (data.get("first_name") or doc.first_name or "").strip()
			last = (data.get("last_name") or doc.last_name or "").strip()
			full = (data.get("employee_name") or "").strip() or f"{first} {last}".strip()
			if full and full != doc.employee_name:
				changes.append(("employee_name", doc.employee_name, full))
				doc.employee_name = full
			if first:
				doc.first_name = first
			if last is not None:
				doc.last_name = last or None
		if data.get("company_email") or data.get("prefered_email"):
			doc.prefered_contact_email = "Company Email"
		doc.save(ignore_permissions=True)
		if changes:
			log_field_changes("Employee", doc.name, changes)

	if "groups" in data:
		_set_employee_groups(doc.name, data.get("groups") or [])

	if doc.user_id:
		_apply_group_roles_to_user(doc.user_id, doc.name)
	elif "roles" in data and doc.user_id:
		_set_user_roles(doc.user_id, data.get("roles") or [])

	frappe.db.commit()
	return {"ok": True, "employee": _serialize_employee(doc.name)}


def _set_employee_groups(employee: str, group_names: list[str]) -> None:
	wanted = set(g for g in group_names if g)
	current = set(
		frappe.get_all(
			"Employee Group Table",
			filters={"employee": employee},
			pluck="parent",
			ignore_permissions=True,
		)
	)
	for g in current - wanted:
		rows = frappe.get_all(
			"Employee Group Table",
			filters={"parent": g, "employee": employee},
			pluck="name",
			ignore_permissions=True,
		)
		for row_name in rows:
			frappe.delete_doc("Employee Group Table", row_name, ignore_permissions=True)
	emp_name = frappe.db.get_value("Employee", employee, "employee_name")
	user_id = frappe.db.get_value("Employee", employee, "user_id")
	frappe.flags.ignore_permissions = True
	for g in wanted - current:
		if not frappe.db.exists("Employee Group", g):
			continue
		parent = frappe.get_doc("Employee Group", g)
		parent.append(
			"employee_list",
			{"employee": employee, "employee_name": emp_name, "user_id": user_id},
		)
		parent.save(ignore_permissions=True)


def _set_user_roles(user: str, roles: list[str]) -> None:
	frappe.flags.ignore_permissions = True
	user_doc = frappe.get_doc("User", user)
	current = {d.role for d in user_doc.roles}
	desired = set(roles) | (current & PROTECTED_ROLES)
	if desired - {"All", "Guest"}:
		desired.add("Desk User")
		desired.add("Employee")
	user_doc.set("roles", [])
	for r in sorted(desired):
		if frappe.db.exists("Role", r):
			user_doc.append("roles", {"role": r})
	user_doc.flags.ignore_permissions = True
	user_doc.save(ignore_permissions=True)


def _unique_staff_email(base_email: str, employee_name: str) -> str:
	email = (base_email or "").strip().lower()
	if email and not frappe.db.exists("User", email):
		return email
	slug = "".join(ch for ch in (employee_name or "staff").lower() if ch.isalnum())[:18] or "staff"
	for i in range(0, 40):
		candidate = f"{slug}{i or ''}@employees.local"
		if not frappe.db.exists("User", candidate):
			return candidate
	return f"{slug}{secrets.token_hex(3)}@employees.local"


@frappe.whitelist()
def create_employee_user(employee, email=None, roles=None):
	"""Create or link a User for the employee and return a one-time random password."""
	_require_app_permission("employees.create_user")
	if isinstance(roles, str):
		roles = frappe.parse_json(roles)

	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("Employee", employee):
		frappe.throw(_("Employee {0} not found").format(employee))
	emp = frappe.get_doc("Employee", employee)
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		frappe.throw(_("Employee already has user {0}").format(emp.user_id))

	requested_email = (email or emp.prefered_email or emp.company_email or emp.personal_email or "").strip()
	linked_existing = False
	user_name = None

	if requested_email and frappe.db.exists("User", requested_email):
		existing_roles = set(frappe.get_roles(requested_email))
		other = frappe.db.get_value(
			"Employee", {"user_id": requested_email, "name": ["!=", emp.name]}, "name"
		)
		if requested_email in ("Administrator", "Guest") or "Administrator" in existing_roles:
			requested_email = ""
		elif other:
			frappe.throw(_("User {0} is already linked to employee {1}").format(requested_email, other))
		else:
			user_name = requested_email
			linked_existing = True

	login_email = requested_email if linked_existing else _unique_staff_email(
		requested_email, emp.employee_name or emp.name
	)
	emp.prefered_contact_email = "Company Email"
	emp.company_email = emp.company_email or login_email
	emp.prefered_email = login_email
	emp.save(ignore_permissions=True)

	password = _rand_password()
	parts = (emp.employee_name or emp.first_name or "User").split()
	first = parts[0]
	last = " ".join(parts[1:]) if len(parts) > 1 else first

	if linked_existing:
		user_name = login_email
		user = frappe.get_doc("User", user_name)
		user.flags.ignore_password_policy = True
		user.flags.no_welcome_mail = True
		user.enabled = 1
		user.save(ignore_permissions=True)
		_set_user_password(user_name, password)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": login_email,
				"first_name": first,
				"last_name": last,
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		user.flags.ignore_password_policy = True
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		user_name = user.name
		_set_user_password(user_name, password)

	group_roles = _roles_from_permission_ids(_permission_ids_for_employee(emp.name))
	if not group_roles or group_roles == ["Employee"]:
		fallback = list(roles) if roles else ["Employee", "Sales User"]
		if "Employee" not in fallback:
			fallback.append("Employee")
		group_roles = fallback
	_set_user_roles(user_name, group_roles)

	emp.user_id = user_name
	emp.create_user_permission = 0
	emp.save(ignore_permissions=True)
	log_field_changes("Employee", emp.name, [("user_id", None, user_name)])
	frappe.db.commit()
	return {
		"ok": True,
		"user_id": user_name,
		"email": login_email,
		"password": password,
		"linked_existing": linked_existing,
		"employee": _serialize_employee(emp.name),
	}


@frappe.whitelist()
def reset_employee_user_password(employee):
	"""Generate a new random password for the linked user (returned once)."""
	_require_app_permission("employees.reset_password")
	frappe.flags.ignore_permissions = True
	emp = frappe.get_doc("Employee", employee)
	if not emp.user_id or not frappe.db.exists("User", emp.user_id):
		frappe.throw(_("Employee has no user"))
	password = _rand_password()
	user = frappe.get_doc("User", emp.user_id)
	user.flags.ignore_password_policy = True
	user.flags.no_welcome_mail = True
	user.save(ignore_permissions=True)
	_set_user_password(user.name, password)
	frappe.db.commit()
	return {"ok": True, "user_id": user.name, "email": user.email, "password": password}


@frappe.whitelist()
def list_employee_groups():
	if not (
		_can_app("tables.employees")
		or _can_app("settings.manage_groups")
		or _can_app("tools.settings")
	):
		frappe.throw(_("Not permitted ({0})").format("tables.employees"))
	_ensure_starter_staff_groups()
	frappe.flags.ignore_permissions = True
	names = frappe.get_all("Employee Group", pluck="name", order_by="name asc", ignore_permissions=True)
	store = _load_perm_store()
	out = []
	for n in names:
		doc = frappe.get_doc("Employee Group", n)
		out.append(
			{
				"name": doc.name,
				"employee_group_name": doc.employee_group_name,
				"permissions": _normalize_permission_ids(store.get(doc.name) or []),
				"members": [
					{
						"employee": r.employee,
						"employee_name": r.employee_name,
						"user_id": r.user_id,
					}
					for r in (doc.employee_list or [])
				],
			}
		)
	return {"groups": out}


@frappe.whitelist()
def save_employee_group(name=None, employee_group_name=None, members=None, permissions=None):
	_require_app_permission("settings.manage_groups")
	if isinstance(members, str):
		members = frappe.parse_json(members)
	members = members or []
	title = (employee_group_name or name or "").strip()
	if not title:
		frappe.throw(_("Group name is required"))

	frappe.flags.ignore_permissions = True
	if name and frappe.db.exists("Employee Group", name):
		doc = frappe.get_doc("Employee Group", name)
		if title and title != doc.employee_group_name:
			doc.employee_group_name = title
		doc.set("employee_list", [])
	else:
		if frappe.db.exists("Employee Group", title):
			frappe.throw(_("Group {0} already exists").format(title))
		doc = frappe.new_doc("Employee Group")
		doc.employee_group_name = title

	for m in members:
		emp = m.get("employee") if isinstance(m, dict) else m
		if not emp or not frappe.db.exists("Employee", emp):
			continue
		emp_name, user_id = frappe.db.get_value("Employee", emp, ["employee_name", "user_id"])
		doc.append(
			"employee_list",
			{"employee": emp, "employee_name": emp_name, "user_id": user_id},
		)
	if name and frappe.db.exists("Employee Group", name):
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)

	if permissions is not None:
		store = _load_perm_store()
		store[doc.name] = _normalize_permission_ids(permissions)
		_save_perm_store(store)
		for m in doc.employee_list or []:
			if m.user_id:
				_apply_group_roles_to_user(m.user_id, m.employee)

	frappe.db.commit()
	return {
		"ok": True,
		"group": {
			"name": doc.name,
			"employee_group_name": doc.employee_group_name,
			"permissions": _permissions_for_group(doc.name),
		},
	}


@frappe.whitelist()
def save_employee_group_permissions(name, permissions=None):
	"""Attach/detach app permissions on an Employee Group (AWS-style)."""
	_require_app_permission("settings.manage_groups")
	if not name or not frappe.db.exists("Employee Group", name):
		frappe.throw(_("Group {0} not found").format(name))
	ids = _normalize_permission_ids(permissions)
	store = _load_perm_store()
	store[name] = ids
	_save_perm_store(store)
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Employee Group", name)
	for m in doc.employee_list or []:
		if m.user_id:
			_apply_group_roles_to_user(m.user_id, m.employee)
	frappe.db.commit()
	return {"ok": True, "name": name, "permissions": ids}


@frappe.whitelist()
def delete_employee_group(name):
	_require_app_permission("settings.manage_groups")
	if not frappe.db.exists("Employee Group", name):
		frappe.throw(_("Group {0} not found").format(name))
	frappe.delete_doc("Employee Group", name, ignore_permissions=True)
	store = _load_perm_store()
	if name in store:
		store.pop(name, None)
		_save_perm_store(store)
	frappe.db.commit()
	return {"ok": True}


def _find_employee_group_by_title(title: str) -> str | None:
	wanted = (title or "").strip()
	if not wanted:
		return None
	if frappe.db.exists("Employee Group", wanted):
		return wanted
	exact = frappe.db.get_value("Employee Group", {"employee_group_name": wanted}, "name")
	if exact:
		return exact
	# Case-insensitive match (repositor vs Repositor).
	for row in frappe.get_all(
		"Employee Group",
		fields=["name", "employee_group_name"],
		ignore_permissions=True,
	):
		if str(row.employee_group_name or "").strip().lower() == wanted.lower():
			return row.name
		if str(row.name or "").strip().lower() == wanted.lower():
			return row.name
	return None


def _ensure_starter_staff_groups() -> dict:
	"""Create repositor / caja / admin if missing. Do not overwrite non-empty permission lists."""
	if getattr(frappe.local, "_staff_starter_ensured", False):
		return {"created": [], "attached": [], "skipped": []}
	frappe.local._staff_starter_ensured = True
	frappe.flags.ignore_permissions = True
	store = _load_perm_store()
	created = []
	attached = []
	skipped = []
	dirty = False
	for spec in STARTER_STAFF_GROUPS:
		title = spec["employee_group_name"]
		perms = _normalize_permission_ids(spec["permissions"])
		existing = _find_employee_group_by_title(title)
		if existing:
			current = _normalize_permission_ids(store.get(existing) or [])
			if not current:
				store[existing] = perms
				attached.append({"name": existing, "employee_group_name": title, "permissions": perms})
				dirty = True
			else:
				skipped.append({"name": existing, "employee_group_name": title, "permissions": current})
			continue
		doc = frappe.new_doc("Employee Group")
		doc.employee_group_name = title
		doc.insert(ignore_permissions=True)
		store[doc.name] = perms
		created.append({"name": doc.name, "employee_group_name": title, "permissions": perms})
		dirty = True
	if dirty:
		_save_perm_store(store)
		frappe.db.commit()
	return {"created": created, "attached": attached, "skipped": skipped}


@frappe.whitelist()
def ensure_starter_staff_groups():
	"""Create the default groups (repositor, caja, admin) if missing."""
	_require_app_permission("settings.manage_groups")
	result = _ensure_starter_staff_groups()
	return {
		"ok": True,
		**result,
		"groups": list_employee_groups().get("groups") or [],
	}


@frappe.whitelist()
def list_employee_meta():
	"""Branches, companies, and the app permission catalog for the editor UI."""
	_require_app_permission("tables.employees")
	_ensure_starter_staff_groups()
	from erpnext.erpnext_integrations.ecommerce_api.company_context import (
		allowed_company_names,
		resolve_company,
	)

	company = resolve_company() or frappe.db.get_value("Company", {}, "name")
	branches = (
		frappe.get_all("Branch", pluck="name", order_by="name asc", ignore_permissions=True)
		if frappe.db.exists("DocType", "Branch")
		else []
	)
	catalog = list_app_permissions()
	return {
		"company": company,
		"companies": allowed_company_names(),
		"branches": branches or [],
		"roles": [],
		"permissions": catalog["permissions"],
		"permission_groups": catalog["groups"],
	}
