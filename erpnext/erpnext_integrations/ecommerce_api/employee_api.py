"""Employee + Employee Group APIs for Tables > Empleados."""

from __future__ import annotations

import json
import re
import secrets
import string
import unicodedata

import frappe
from frappe import _
from frappe.utils import cint, flt, today
from frappe.utils.password import update_password as _update_password

from erpnext.erpnext_integrations.ecommerce_api.table_history import log_field_changes

# JSON map of Employee Group name -> permission ids (Table Extra Schema reuse; no migrate).
PERM_STORE_SCOPE = "settings.staff_group_permissions"
# Employee -> POS login barcode (numeric, printable). No migrate required.
STAFF_LOGIN_SCOPE = "settings.staff_login_barcodes"
STAFF_LOGIN_PREFIX = "99"
STAFF_LOGIN_LEN = 12
# Employee -> 6-digit ops PIN (armado/check/roleplay). Plaintext, unique vs admin PIN.
STAFF_OPS_PIN_SCOPE = "settings.staff_ops_pins"
STAFF_OPS_PIN_LEN = 6

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
		"desc_en": "Show Catalog in navbar / home. The /catalog page stays reachable without this.",
		"desc_es": "Mostrar Catálogo en la barra y en Inicio. /catalog sigue accesible sin este permiso.",
		"desc_zh": "在导航/首页显示目录。无此权限仍可打开 /catalog。",
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
		"id": "ops.delivery",
		"group": "operaciones",
		"label_en": "Delivery",
		"label_es": "Entregas",
		"label_zh": "配送",
		"desc_en": "See assigned delivery routes and record proof of delivery.",
		"desc_es": "Ver las rutas de entrega asignadas y registrar la prueba de entrega.",
		"desc_zh": "查看已分配的配送路线并记录送达凭证。",
	},
	{
		"id": "ops.check",
		"group": "operaciones",
		"label_en": "Check",
		"label_es": "Check",
		"label_zh": "复核",
		"desc_en": "Show Check in navbar / home. /check stays reachable (PIN gate) without this.",
		"desc_es": "Mostrar Check en la barra y en Inicio. /check sigue accesible (PIN) sin este permiso.",
		"desc_zh": "在导航/首页显示复核。无此权限仍可打开 /check（PIN）。",
	},
	{
		"id": "ops.armado",
		"group": "operaciones",
		"label_en": "Assembly",
		"label_es": "Armado",
		"label_zh": "配货",
		"desc_en": "Show Armado in navbar / home. /armado stays reachable (PIN gate) without this.",
		"desc_es": "Mostrar Armado en la barra y en Inicio. /armado sigue accesible (PIN) sin este permiso.",
		"desc_zh": "在导航/首页显示配货。无此权限仍可打开 /armado（PIN）。",
	},
	{
		"id": "ops.preventa",
		"group": "operaciones",
		"label_en": "Preventa",
		"label_es": "Preventa",
		"label_zh": "预售",
		"desc_en": "Work the personal sales pipeline (Kanban) and share the seller link.",
		"desc_es": "Trabajar el pipeline de ventas personal (Kanban) y compartir el link de vendedor.",
		"desc_zh": "管理个人销售看板并分享销售员链接。",
	},
	{
		"id": "ops.buying",
		"group": "operaciones",
		"label_en": "Buying",
		"label_es": "Compras",
		"label_zh": "采购",
		"desc_en": "Simple purchase orders: expected date, product search, quick create.",
		"desc_es": "Órdenes de compra simples: fecha esperada, búsqueda y alta rápida de productos.",
		"desc_zh": "简易采购单：预计到货日、商品搜索与快速建品。",
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
		"id": "log.prints",
		"group": "logistica",
		"label_en": "Print templates",
		"label_es": "Plantillas de impresión",
		"label_zh": "打印模板",
		"desc_en": "Design A4/thermal print templates for invoices, receiving, and catalog.",
		"desc_es": "Diseñar plantillas de impresión A4/térmica para facturas, recepción y catálogo.",
		"desc_zh": "设计发票、收货与目录的 A4/热敏打印模板。",
	},
	{
		"id": "log.rutas",
		"group": "logistica",
		"label_en": "Route planning",
		"label_es": "Rutas",
		"label_zh": "路线规划",
		"desc_en": "Plan, optimize, and publish delivery routes on a map.",
		"desc_es": "Planificar, optimizar y publicar rutas de entrega en un mapa.",
		"desc_zh": "在地图上规划、优化并发布配送路线。",
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
		"label_en": "Suggestions",
		"label_es": "Sugerencias",
		"label_zh": "建议",
		"desc_en": "Review product drafts and field-change suggestions from operaciones.",
		"desc_es": "Revisar borradores de productos y sugerencias de campos desde operaciones.",
		"desc_zh": "审核来自作业端的商品草稿与字段修改建议。",
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
		"id": "sales.see_all",
		"group": "ventas_scope",
		"label_en": "See all clients",
		"label_es": "Ver todos los clientes",
		"label_zh": "查看全部客户",
		"desc_en": "RM: view every customer.",
		"desc_es": "RM: ver todos los clientes.",
		"desc_zh": "关系管理：查看全部客户。",
	},
	{
		"id": "sales.see_assigned",
		"group": "ventas_scope",
		"label_en": "See assigned clients",
		"label_es": "Ver clientes asignados",
		"label_zh": "查看已分配客户",
		"desc_en": "RM: view only customers assigned to this salesman.",
		"desc_es": "RM: ver solo clientes asignados a este vendedor.",
		"desc_zh": "关系管理：仅查看分配给自己的客户。",
	},
	{
		"id": "sales.commit_all",
		"group": "ventas_scope",
		"label_en": "Commit all sales",
		"label_es": "Cerrar ventas de todos",
		"label_zh": "提交全部销售",
		"desc_en": "Preventa: win/convert for any client.",
		"desc_es": "Preventa: ganar/convertir cualquier cliente.",
		"desc_zh": "预售：可对任意客户成交/转化。",
	},
	{
		"id": "sales.commit_assigned",
		"group": "ventas_scope",
		"label_en": "Commit assigned sales",
		"label_es": "Cerrar ventas asignadas",
		"label_zh": "提交已分配销售",
		"desc_en": "Preventa: win/convert only assigned clients.",
		"desc_es": "Preventa: ganar/convertir solo clientes asignados.",
		"desc_zh": "预售：仅可对自己被分配的客户成交/转化。",
	},
	{
		"id": "tables.compras",
		"group": "tablas",
		"label_en": "Purchases",
		"label_es": "Compras",
		"label_zh": "采购单",
		"desc_en": "Purchase orders: ETA, receive/bill pipeline, open value.",
		"desc_es": "Órdenes de compra: ETA, pipeline recepción/factura, valor abierto.",
		"desc_zh": "采购单：到货日、收货/开票进度、未收货金额。",
	},
	{
		"id": "tables.lotes",
		"group": "tablas",
		"label_en": "Lots",
		"label_es": "Lotes",
		"label_zh": "批次",
		"desc_en": "Receive lots with sell-by estimates and soft FIFO remaining qty.",
		"desc_es": "Lotes de recepción con plazo comercial estimado y saldo FIFO suave.",
		"desc_zh": "收货批次：估算出售期限与软 FIFO 剩余数量。",
	},
	{
		"id": "tables.mats",
		"group": "tablas",
		"label_en": "MATs / routes",
		"label_es": "MATs / rutas",
		"label_zh": "MAT / 路线",
		"desc_en": "Planned delivery trips (MAT-DT): driver, vehicle, stops, PoD.",
		"desc_es": "Viajes de entrega planificados (MAT-DT): conductor, auto, paradas, PoD.",
		"desc_zh": "已规划配送行程（MAT-DT）：司机、车辆、停靠点、签收。",
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
		"id": "tables.orders.own",
		"group": "tablas",
		"label_en": "Own orders",
		"label_es": "Pedidos propios",
		"label_zh": "自己的订单",
		"desc_en": "Guest preorders created by this user.",
		"desc_es": "Pedidos de invitados creados por este usuario.",
		"desc_zh": "此用户创建的访客预订单。",
	},
	{
		"id": "tables.orders.all",
		"group": "tablas",
		"label_en": "All orders",
		"label_es": "Todos los pedidos",
		"label_zh": "全部订单",
		"desc_en": "Every guest preorder, regardless of owner or tag.",
		"desc_es": "Todos los pedidos de invitados, sin filtro de dueño ni tag.",
		"desc_zh": "全部访客预订单，不按创建人或标签过滤。",
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
		"id": "tools.catalog_pdf",
		"group": "herramientas",
		"label_en": "Catalog PDF",
		"label_es": "Catálogo PDF",
		"label_zh": "目录 PDF",
		"desc_en": "Export catalog PDFs (layouts, categories, selections).",
		"desc_es": "Exportar PDF del catálogo (diseños, categorías, selecciones).",
		"desc_zh": "导出目录 PDF（版式、分类、选集）。",
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

ORDER_TAG_PREFIX = "tables.orders.tag:"
DEFAULT_CAJA_ORDER_TAG = "caja"
CATALOG_ORDER_TAG = "catalogo"


def _normalize_order_tag(raw) -> str:
	s = str(raw or "").strip().lower()
	s = re.sub(r"\s+", "-", s)
	s = re.sub(r"[^a-z0-9_-]", "", s)
	return s[:40]


def _order_tag_permission_id(tag) -> str:
	slug = _normalize_order_tag(tag)
	return f"{ORDER_TAG_PREFIX}{slug}" if slug else ""


def _accept_permission_id(key: str) -> str | None:
	key = str(key or "").strip()
	# Legacy single Pedidos permission means every order.
	if key == "tables.orders":
		key = "tables.orders.all"
	# Buying moved from Logistics → Operations.
	if key == "log.buying":
		key = "ops.buying"
	if key in KNOWN_PERMISSION_IDS:
		return key
	if key.startswith(ORDER_TAG_PREFIX):
		pid = _order_tag_permission_id(key[len(ORDER_TAG_PREFIX) :])
		return pid or None
	return None

PERMISSION_TO_ROLES = {
	"ops.pos": ["Sales User"],
	"ops.catalog": ["Sales User"],
	"ops.receiving": ["Stock User", "Purchase User"],
	"ops.delivery": ["Stock User"],
	"ops.check": ["Stock User"],
	"ops.armado": ["Stock User"],
	"ops.preventa": ["Sales User"],
	"sales.see_all": ["Sales User"],
	"sales.see_assigned": ["Sales User"],
	"sales.commit_all": ["Sales User"],
	"sales.commit_assigned": ["Sales User"],
	"ops.buying": ["Purchase User", "Purchase Manager", "Stock User"],
	"log.reports": ["Accounts User"],
	"log.accounting": ["Accounts User"],
	"log.sections": ["Stock User"],
	"log.prints": ["Stock User"],
	"log.rutas": ["Stock User"],
	"tables.products": ["Stock User", "Item Manager"],
	"tables.rentability": ["Stock User"],
	"tables.promotions": ["Sales Manager"],
	"tables.review": ["Stock User", "Purchase User"],
	"tables.variants": ["Item Manager"],
	"tables.crm": ["Sales User"],
	"tables.compras": ["Purchase User", "Purchase Manager", "Stock User"],
	"tables.lotes": ["Stock User", "Purchase User", "Purchase Manager"],
	"tables.mats": ["Stock User", "Sales User"],
	"tables.employees": ["HR User"],
	"tables.cajas": ["Accounts User"],
	"tables.orders": ["Sales User"],
	"tables.orders.own": ["Sales User"],
	"tables.orders.all": ["Sales User"],
	"tools.sync": ["Stock Manager"],
	"tools.labels": ["Stock User"],
	"tools.catalog_pdf": ["Stock Manager", "Sales Manager"],
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
# Starter floor roles intentionally omit log.* and tables.* — ops (+ tools) only.
# Frontend flattens Operaciones into top-level header pills when those zones are hidden.
_STARTER_REPOSITOR = [
	"ops.receiving",
	"ops.catalog",
	"ops.buying",
	"ops.check",
	"ops.armado",
	"tools.labels",
	"tools.catalog_pdf",
	"tools.sync",
	"sales.see_all",
	"sales.commit_assigned",
]
_STARTER_CAJA = [
	"ops.pos",
	"ops.catalog",
	"tools.labels",
	"sales.see_all",
	"sales.commit_assigned",
]
# Default sales scope for every non-admin group: see all clients, commit only assigned.
_DEFAULT_SALES_SCOPE = [
	"sales.see_all",
	"sales.commit_assigned",
]
_SALES_SCOPE_IDS = frozenset(
	{
		"sales.see_all",
		"sales.see_assigned",
		"sales.commit_all",
		"sales.commit_assigned",
	}
)
# Official sales floor role title is ``Sales`` (EN). Legacy sites may still have ``ventas``.
# No Entregas / Check / Armado — those are opt-in via group permissions.
# Catalog PDF is a normal seller tool (share price lists); still toggleable in Groups.
_STARTER_SALES = [
	"ops.preventa",
	"ops.catalog",
	"tools.sync",
	"tools.catalog_pdf",
	"tables.crm",
	# Own Pedidos so Operaciones → Orden (create + confirm) works without full tables.orders.
	"tables.orders.own",
	"sales.see_all",
	"sales.commit_assigned",
]
# Field driver / conductor app — assigned trips + PoD, not full route planning.
_STARTER_DRIVER = [
	"ops.delivery",
	"sales.see_all",
	"sales.commit_assigned",
]
# Floor ops that Sales must not keep if they were inherited from older broad gates.
_SALES_STRIP_OPS = frozenset({"ops.delivery", "ops.check", "ops.armado"})

# Prior starter lists (pre ops-only defaults). Matching groups are upgraded in place
# so unmodified floor roles lose log.*/tables.* without touching customised groups.
# ``ventas`` stays here so existing sites still get the ops-only upgrade; it is not seeded.
_FLOOR_STARTER_TITLES = frozenset({"repositor", "caja", "ventas", "Sales", "Driver"})
_LEGACY_STARTER_BY_TITLE = {
	"repositor": [
		[
			"ops.receiving",
			"ops.catalog",
			"tables.products",
			"tables.review",
			"tables.variants",
			"ops.buying",
			"tables.compras",
			"log.sections",
			"tools.labels",
			"tools.catalog_pdf",
			"tools.sync",
		],
	],
	"caja": [
		[
			"ops.pos",
			"ops.catalog",
			"tables.orders.own",
			"tables.orders.tag:caja",
			"tables.cajas",
			"log.accounting",
			"tools.labels",
		],
	],
	"ventas": [
		[
			"ops.preventa",
			"ops.catalog",
			"tables.orders",
			"tables.orders.all",
			"tables.crm",
			"tools.catalog_pdf",
		],
	],
	"Sales": [
		[
			"ops.preventa",
			"ops.catalog",
			"tables.orders",
			"tables.orders.all",
			"tables.crm",
			"tools.catalog_pdf",
		],
	],
}


def _perm_set(ids) -> frozenset:
	return frozenset(_normalize_permission_ids(ids))


def _is_legacy_starter_perms(title: str, current: list[str]) -> bool:
	cur = _perm_set(current)
	for legacy in _LEGACY_STARTER_BY_TITLE.get(title) or []:
		if cur == _perm_set(legacy):
			return True
	return False


def _floor_starter_needs_ops_only_upgrade(title: str, current: list[str]) -> bool:
	"""True when a named floor starter still carries tables.* / log.* access."""
	if title not in _FLOOR_STARTER_TITLES:
		return False
	return any(p.startswith("tables.") or p.startswith("log.") for p in current)


STARTER_STAFF_GROUPS = [
	{"employee_group_name": "repositor", "permissions": list(_STARTER_REPOSITOR)},
	{"employee_group_name": "caja", "permissions": list(_STARTER_CAJA)},
	{"employee_group_name": "Sales", "permissions": list(_STARTER_SALES)},
	{"employee_group_name": "Driver", "permissions": list(_STARTER_DRIVER)},
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


def _optional_staff_password(password) -> str | None:
	"""Return stripped password or None (caller should mint random). Rejects too-short values."""
	if password is None:
		return None
	pwd = str(password).strip()
	if not pwd:
		return None
	if len(pwd) < 6:
		frappe.throw(_("Password must be at least 6 characters"))
	return pwd


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
		key = _accept_permission_id(str(item or "").strip())
		if key and key not in seen:
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
	"ops.preventa": "pos",
	"ops.check": "receiving",
	"ops.armado": "receiving",
	"ops.delivery": "receiving",
	"sales.see_all": "pos",
	"sales.see_assigned": "pos",
	"sales.commit_all": "pos",
	"sales.commit_assigned": "pos",
	"tables.orders": "pos",
	"tables.orders.own": "pos",
	"tables.orders.all": "pos",
	"tools.labels": "pos",
	"tools.catalog_pdf": "receiving",
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
	"tables.compras": "receiving",
	"tables.lotes": "receiving",
	"tables.mats": "receiving",
	"tables.employees": "receiving",
	"ops.buying": "receiving",
	"log.sections": "receiving",
	"log.prints": "receiving",
	"log.rutas": "receiving",
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
	if not flag and str(pid or "").startswith(ORDER_TAG_PREFIX):
		flag = "pos"
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
		if str(pid or "").startswith(ORDER_TAG_PREFIX):
			roles.add("Sales User")
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
	login_username = None
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		roles = [r for r in frappe.get_roles(emp.user_id) if r not in ("All", "Guest", "Desk User")]
		user_enabled = cint(frappe.db.get_value("User", emp.user_id, "enabled"))
		login_username = frappe.db.get_value("User", emp.user_id, "username") or None
	login_barcode = None
	store = _load_staff_login_store()
	entry = (store.get("by_employee") or {}).get(emp.name)
	if isinstance(entry, dict):
		login_barcode = str(entry.get("code") or "").strip() or None
	ops_pin = None
	ops_store = _load_ops_pin_store()
	ops_entry = (ops_store.get("by_employee") or {}).get(emp.name)
	if isinstance(ops_entry, dict):
		ops_pin = str(ops_entry.get("pin") or "").strip() or None
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
		"username": login_username,
		"has_user": bool(emp.user_id),
		"user_enabled": user_enabled,
		"roles": roles,
		"permissions": _permission_ids_for_employee(emp.name),
		"groups": groups,
		"login_barcode": login_barcode,
		"ops_pin": ops_pin,
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
			{"id": "ventas_scope", "label_en": "Sales scope", "label_es": "Alcance ventas", "label_zh": "销售范围"},
		],
	}


def cashier_names_in_shared_groups(cashier_name: str) -> list[str]:
	"""Employee names sharing at least one Employee Group with the given cashier label."""
	cashier_name = (cashier_name or "").strip()
	if not cashier_name:
		return []
	emp = frappe.db.get_value("Employee", {"employee_name": cashier_name}, "name")
	if not emp:
		emp = frappe.db.get_value("Employee", {"user_id": cashier_name}, "name")
	if not emp:
		return [cashier_name]
	group_ids = frappe.get_all(
		"Employee Group Table",
		filters={"employee": emp},
		pluck="parent",
		ignore_permissions=True,
	)
	if not group_ids:
		return [cashier_name]
	member_rows = frappe.get_all(
		"Employee Group Table",
		filters={"parent": ["in", group_ids]},
		fields=["employee", "employee_name"],
		ignore_permissions=True,
	)
	names: set[str] = {cashier_name}
	for row in member_rows or []:
		label = (row.employee_name or "").strip()
		if not label and row.employee:
			label = (frappe.db.get_value("Employee", row.employee, "employee_name") or "").strip()
		if label:
			names.add(label)
	return sorted(names)


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
	_ensure_starter_staff_groups()
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
		email_in = (data.get("prefered_email") or data.get("company_email") or "").strip()
		if email_in:
			_sync_employee_login_email(doc, email_in)
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
		email_keys = {"company_email", "prefered_email"}
		email_in = None
		if "prefered_email" in data or "company_email" in data:
			email_in = (
				data.get("prefered_email")
				if data.get("prefered_email") not in (None, "")
				else data.get("company_email")
			)
			if email_in not in (None, ""):
				changes.extend(_sync_employee_login_email(doc, str(email_in)))
		for key in tracked:
			if key not in data:
				continue
			if key in email_keys:
				# Handled by _sync_employee_login_email (also renames User).
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
		try:
			doc.save(ignore_permissions=True)
		except (frappe.ValidationError, frappe.DuplicateEntryError):
			# Inline table edits (status / branch / salary / name) must not be
			# blocked by unrelated pre-existing data (e.g. two employees sharing a
			# login user) — common for offline-replayed edits. Write just the
			# changed scalar fields; anything else re-raises.
			simple = {
				"employee_name",
				"first_name",
				"last_name",
				"status",
				"branch",
				"ctc",
				"bio",
				"cell_number",
				"company_email",
				"prefered_email",
				"user_id",
			}
			patch = {k: doc.get(k) for k, _o, _n in changes}
			for k in ("first_name", "last_name", "employee_name", "company_email", "prefered_email", "user_id"):
				if k in data or k in {c[0] for c in changes}:
					patch[k] = doc.get(k)
			if not set(patch).issubset(simple):
				raise
			if patch:
				frappe.db.set_value("Employee", doc.name, patch, update_modified=True)
		if changes:
			log_field_changes("Employee", doc.name, changes)

		# Custom login handle (User.username). Blank → clear so login is email only.
		if "username" in data and doc.user_id and frappe.db.exists("User", doc.user_id):
			prev_u = frappe.db.get_value("User", doc.user_id, "username") or ""
			next_u = _set_user_login_username(doc.user_id, data.get("username"))
			if str(prev_u or "") != str(next_u or ""):
				log_field_changes(
					"Employee",
					doc.name,
					[("username", prev_u or None, next_u)],
				)

	if "groups" in data:
		_set_employee_groups(doc.name, data.get("groups") or [])

	if doc.user_id:
		_apply_group_roles_to_user(doc.user_id, doc.name)
	elif "roles" in data and doc.user_id:
		_set_user_roles(doc.user_id, data.get("roles") or [])

	# New employees always get a random 6-digit ops PIN (armado/check/roleplay).
	if is_new:
		_issue_ops_pin_for_employee(doc.name)

	# Optional: create User in the same save (Nuevo empleado + checkbox).
	credentials = None
	if is_new and cint(data.get("create_user")):
		_require_app_permission("employees.create_user")
		# create_employee_user commits; return its credential payload for the one-time flash.
		created = create_employee_user(
			doc.name,
			email=data.get("prefered_email") or data.get("company_email"),
			password=data.get("password"),
			username=data.get("username"),
		)
		credentials = {
			"user_id": created.get("user_id"),
			"email": created.get("email"),
			"username": created.get("username"),
			"password": created.get("password"),
			"password_was_set": created.get("password_was_set"),
			"linked_existing": created.get("linked_existing"),
		}
		return {
			"ok": True,
			"employee": created.get("employee") or _serialize_employee(doc.name),
			"credentials": credentials,
		}

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


def _email_local_slug(text: str, max_len: int = 18) -> str:
	"""ASCII-only local-part from a name (accents stripped). Frappe rejects non-ASCII emails."""
	folded = unicodedata.normalize("NFKD", text or "")
	ascii_only = folded.encode("ascii", "ignore").decode("ascii")
	slug = "".join(ch for ch in ascii_only.lower() if ch.isalnum())[:max_len]
	return slug or "staff"


def _ensure_username_login_enabled() -> None:
	"""Frappe only resolves User.username at login when this System Setting is on."""
	if cint(frappe.db.get_single_value("System Settings", "allow_login_using_user_name")):
		return
	frappe.flags.ignore_permissions = True
	frappe.db.set_single_value("System Settings", "allow_login_using_user_name", 1)


def _normalize_login_username(raw) -> str:
	"""Custom login handle (employee code / username). Blank → login via email only."""
	import re

	s = str(raw or "").strip().strip("@")
	s = re.sub(r"\s+", "", s)
	if not s:
		return ""
	if "@" in s:
		frappe.throw(_("Username cannot be an email — leave blank to use the email as login"))
	if not re.match(r"^[A-Za-z0-9._-]{2,64}$", s):
		frappe.throw(
			_("Username may only contain letters, numbers, dots, underscores and hyphens (2–64 chars)")
		)
	return s


def _set_user_login_username(user_name: str, username) -> str | None:
	"""Set or clear ``User.username``. Returns the stored username (or None)."""
	_ensure_username_login_enabled()
	wanted = _normalize_login_username(username)
	frappe.flags.ignore_permissions = True
	if not wanted:
		frappe.db.set_value("User", user_name, "username", None, update_modified=False)
		return None
	other = frappe.db.get_value(
		"User", {"username": wanted, "name": ["!=", user_name]}, "name"
	)
	if other:
		frappe.throw(_("Username {0} is already taken").format(wanted))
	# Avoid colliding with another account's email/login id.
	if frappe.db.exists("User", wanted) and wanted != user_name:
		frappe.throw(_("Username {0} conflicts with an existing login email").format(wanted))
	frappe.db.set_value("User", user_name, "username", wanted, update_modified=False)
	return wanted


def _unique_staff_email(base_email: str, employee_name: str) -> str:
	"""Return a free login email.

	If ``base_email`` is valid and unused, keep it. Otherwise mint
	``{local}{random5}@{domain}`` so auto-created users never collide with
	existing User names (e.g. two "Bruno" salespeople).
	"""
	from frappe.utils import validate_email_address

	email = (base_email or "").strip().lower()
	if email and validate_email_address(email) and not frappe.db.exists("User", email):
		return email

	if email and "@" in email and validate_email_address(email):
		local, _, domain = email.rpartition("@")
		local = _email_local_slug(local, max_len=24) or _email_local_slug(employee_name or "staff")
		domain = (domain or "employees.local").strip() or "employees.local"
	else:
		local = _email_local_slug(employee_name or "staff")
		domain = "employees.local"

	for _ in range(48):
		suffix = secrets.randbelow(90000) + 10000  # 10000–99999
		candidate = f"{local}{suffix}@{domain}"
		if not frappe.db.exists("User", candidate):
			return candidate
	return f"{local}{secrets.token_hex(4)}@{domain}"


def _sync_employee_login_email(doc, new_email: str) -> list[tuple]:
	"""Set company/prefered email; rename linked User.name (Frappe requires name=email).

	Custom login handles live on ``User.username`` and are preserved across rename.
	Returns a list of (field, old, new) change tuples for logging.
	"""
	from frappe.utils import validate_email_address

	changes: list[tuple] = []
	wanted = (new_email or "").strip().lower()
	if not wanted:
		return changes
	if not validate_email_address(wanted):
		frappe.throw(_("Invalid email: {0}").format(wanted))

	old_company = doc.company_email
	old_prefered = doc.prefered_email
	if str(old_company or "") != wanted:
		changes.append(("company_email", old_company, wanted))
	if str(old_prefered or "") != wanted:
		changes.append(("prefered_email", old_prefered, wanted))
	doc.company_email = wanted
	doc.prefered_email = wanted
	doc.prefered_contact_email = "Company Email"

	old_uid = (doc.user_id or "").strip()
	if not old_uid or not frappe.db.exists("User", old_uid):
		return changes
	prev_username = frappe.db.get_value("User", old_uid, "username")
	if old_uid.lower() == wanted:
		# Keep User.email aligned even if name already matches.
		frappe.db.set_value("User", old_uid, "email", wanted, update_modified=False)
		return changes
	if old_uid in ("Administrator", "Guest"):
		frappe.throw(_("Cannot rename system user {0}").format(old_uid))
	if frappe.db.exists("User", wanted):
		frappe.throw(_("User {0} already exists").format(wanted))
	other = frappe.db.get_value(
		"Employee", {"user_id": wanted, "name": ["!=", doc.name]}, "name"
	)
	if other:
		frappe.throw(_("User {0} is already linked to employee {1}").format(wanted, other))

	frappe.flags.ignore_permissions = True
	frappe.rename_doc("User", old_uid, wanted, force=True, merge=False)
	frappe.db.set_value("User", wanted, "email", wanted, update_modified=False)
	if prev_username:
		_set_user_login_username(wanted, prev_username)
	changes.append(("user_id", old_uid, wanted))
	doc.user_id = wanted
	return changes


@frappe.whitelist()
def create_employee_user(employee, email=None, roles=None, password=None, username=None):
	"""Create or link a User for the employee.

	``password`` optional — when omitted a random one-time password is minted.
	``username`` optional custom login handle (employee code). Blank → login with email only.
	Frappe User.name is always the email; custom handles live on User.username.
	"""
	_require_app_permission("employees.create_user")
	if isinstance(roles, str):
		roles = frappe.parse_json(roles)
	chosen = _optional_staff_password(password)
	wanted_username = _normalize_login_username(username)

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

	password = chosen if chosen is not None else _rand_password()
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

	# Clear Frappe's auto first_name scrub username unless a custom handle was requested.
	stored_username = _set_user_login_username(user_name, wanted_username or None)

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
		"username": stored_username,
		"password": password,
		"password_was_set": chosen is not None,
		"linked_existing": linked_existing,
		"employee": _serialize_employee(emp.name),
	}


@frappe.whitelist()
def reset_employee_user_password(employee, password=None):
	"""Set a specific password or mint a new random one for the linked user (returned once)."""
	_require_app_permission("employees.reset_password")
	chosen = _optional_staff_password(password)
	frappe.flags.ignore_permissions = True
	emp = frappe.get_doc("Employee", employee)
	if not emp.user_id or not frappe.db.exists("User", emp.user_id):
		frappe.throw(_("Employee has no user"))
	password = chosen if chosen is not None else _rand_password()
	user = frappe.get_doc("User", emp.user_id)
	user.flags.ignore_password_policy = True
	user.flags.no_welcome_mail = True
	user.save(ignore_permissions=True)
	_set_user_password(user.name, password)
	frappe.db.commit()
	return {
		"ok": True,
		"user_id": user.name,
		"email": user.email,
		"password": password,
		"password_was_set": chosen is not None,
	}


@frappe.whitelist()
def change_own_password(current_password=None, new_password=None):
	"""Signed-in staff changes their own login password (Profile settings)."""
	from frappe.utils.password import check_password

	user = _acting_username()
	if not user or user in ("Guest",) or not frappe.db.exists("User", user):
		frappe.throw(_("Not signed in"))
	cur = (current_password or "").strip() if current_password is not None else ""
	new = _optional_staff_password(new_password)
	if not cur:
		frappe.throw(_("Current password is required"))
	if not new:
		frappe.throw(_("New password is required"))
	if cur == new:
		frappe.throw(_("New password must be different from the current password"))
	try:
		check_password(user, cur)
	except frappe.AuthenticationError:
		frappe.throw(_("Current password is incorrect"))
	except Exception:
		frappe.throw(_("Current password is incorrect"))
	frappe.flags.ignore_permissions = True
	_set_user_password(user, new)
	frappe.db.commit()
	return {"ok": True}


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
		normalized = _normalize_permission_ids(permissions)
		# New / empty permission lists get the default sales scope (except full *).
		if not normalized:
			normalized = list(_DEFAULT_SALES_SCOPE)
		else:
			normalized = _with_default_sales_scope(normalized)
		store[doc.name] = normalized
		_save_perm_store(store)
		for m in doc.employee_list or []:
			if m.user_id:
				_apply_group_roles_to_user(m.user_id, m.employee)
	elif not (name and frappe.db.exists("Employee Group", name)):
		# Brand-new group with no permissions arg — seed sales scope defaults.
		store = _load_perm_store()
		store[doc.name] = list(_DEFAULT_SALES_SCOPE)
		_save_perm_store(store)

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
	try:
		from erpnext.erpnext_integrations.ecommerce_api.field_acl import drop_group_field_acl

		drop_group_field_acl(name)
	except Exception:
		frappe.log_error(title="drop_group_field_acl")
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


def _order_visibility_scope() -> dict | None:
	"""None = unrestricted. Else {user, own, tags} for Pedidos list/detail."""
	info = _acting_perm_info()
	if not info:
		return None
	perms = info.get("permissions") or []
	if info.get("source") == "admin" or "*" in perms:
		return None
	if "tables.orders" in perms or "tables.orders.all" in perms:
		return None
	tags = []
	seen = set()
	for pid in perms:
		if not str(pid).startswith(ORDER_TAG_PREFIX):
			continue
		tag = _normalize_order_tag(str(pid)[len(ORDER_TAG_PREFIX) :])
		if tag and tag not in seen:
			seen.add(tag)
			tags.append(tag)
	return {
		"user": _acting_username(),
		"own": "tables.orders.own" in perms,
		"tags": tags,
	}


def guest_preorder_matches_scope(owner: str, tag_text: str, scope: dict | None) -> bool:
	if scope is None:
		return True
	user = str(scope.get("user") or "").strip()
	tokens = {part.strip() for part in str(tag_text or "").split("|") if part.strip()}
	# Creator can always follow their own guest pedido (Orden submit, edits) even
	# without tables.orders.own — Sales has catalog/preventa but not Pedidos table.
	if user and (str(owner or "").strip() == user or f"order_owner:{user}" in tokens):
		return True
	if not scope.get("own") and not scope.get("tags"):
		return False
	for tag in scope.get("tags") or []:
		if f"order_tag:{tag}" in tokens:
			return True
	return False


def _apply_caja_orders_split(store: dict) -> bool:
	"""One-time: caja loses blanket Pedidos and gets own + tag caja."""
	meta = store.get("_migrations")
	if not isinstance(meta, dict):
		meta = {}
	if meta.get("caja_orders_v1"):
		return False
	existing = _find_employee_group_by_title("caja")
	if not existing:
		meta["caja_orders_v1"] = True
		store["_migrations"] = meta
		return True
	raw = store.get(existing) or []
	if isinstance(raw, str):
		raw = _parse_json(raw, [])
	if not isinstance(raw, list) or not raw:
		meta["caja_orders_v1"] = True
		store["_migrations"] = meta
		return True
	next_ids = []
	seen = set()
	for item in raw:
		key = str(item or "").strip()
		if key in ("tables.orders", "tables.orders.all"):
			continue
		accepted = _accept_permission_id(key)
		if accepted and accepted not in seen:
			seen.add(accepted)
			next_ids.append(accepted)
	for extra in ("tables.orders.own", _order_tag_permission_id(DEFAULT_CAJA_ORDER_TAG)):
		if extra and extra not in seen:
			seen.add(extra)
			next_ids.append(extra)
	store[existing] = next_ids
	meta["caja_orders_v1"] = True
	store["_migrations"] = meta
	return True


def _with_default_sales_scope(perms: list[str]) -> list[str]:
	"""Ensure non-admin groups get see_all + commit_assigned when no sales.* set yet."""
	ids = list(perms or [])
	if "*" in ids:
		return ids
	if any(p in _SALES_SCOPE_IDS for p in ids):
		return ids
	return _normalize_permission_ids(ids + list(_DEFAULT_SALES_SCOPE))


def _apply_default_sales_scope_to_store(store: dict) -> bool:
	"""Backfill default sales scope onto every group that has none (admin → all four)."""
	dirty = False
	for name, raw in list(store.items()):
		if name.startswith("_"):
			continue
		if not isinstance(raw, list):
			continue
		title = ""
		try:
			if frappe.db.exists("Employee Group", name):
				title = frappe.db.get_value("Employee Group", name, "employee_group_name") or name
		except Exception:
			title = name
		current = _normalize_permission_ids(raw)
		if "*" in current or (title or "").strip().lower() == "admin":
			# Admin / wildcard: ensure every sales.* flag is present for the matrix UI.
			missing = [p for p in sorted(_SALES_SCOPE_IDS) if p not in current]
			if missing:
				store[name] = _normalize_permission_ids(current + missing)
				dirty = True
			continue
		# Upgrade our previous ventas default (see_assigned+commit_assigned only) once.
		scope_only = [p for p in current if p in _SALES_SCOPE_IDS]
		if _perm_set(scope_only) == _perm_set(["sales.see_assigned", "sales.commit_assigned"]):
			next_perms = [p for p in current if p not in _SALES_SCOPE_IDS] + list(_DEFAULT_SALES_SCOPE)
			store[name] = _normalize_permission_ids(next_perms)
			dirty = True
			continue
		next_perms = _with_default_sales_scope(current)
		if _perm_set(next_perms) != _perm_set(current):
			store[name] = next_perms
			dirty = True
	return dirty


def _strip_sales_floor_ops_extras(store: dict) -> bool:
	"""Sales/ventas must not keep Entregas / Check / Armado from older broad gates."""
	dirty = False
	for name, raw in list(store.items()):
		if name.startswith("_") or not isinstance(raw, list):
			continue
		title = ""
		try:
			if frappe.db.exists("Employee Group", name):
				title = frappe.db.get_value("Employee Group", name, "employee_group_name") or name
		except Exception:
			title = name
		title_l = (title or "").strip().lower()
		if title_l not in ("sales", "ventas"):
			continue
		current = _normalize_permission_ids(raw)
		if "*" in current:
			continue
		next_perms = [p for p in current if p not in _SALES_STRIP_OPS]
		if _perm_set(next_perms) != _perm_set(current):
			store[name] = next_perms
			dirty = True
	return dirty


def _backfill_floor_fulfillment_perms(store: dict) -> bool:
	"""Attach new ops.check / ops.armado onto repositor + admin; catalog PDF onto Sales."""
	dirty = False
	for name, raw in list(store.items()):
		if name.startswith("_") or not isinstance(raw, list):
			continue
		title = ""
		try:
			if frappe.db.exists("Employee Group", name):
				title = frappe.db.get_value("Employee Group", name, "employee_group_name") or name
		except Exception:
			title = name
		title_l = (title or "").strip().lower()
		current = _normalize_permission_ids(raw)
		if "*" in current:
			continue
		if title_l == "admin":
			missing = sorted(KNOWN_PERMISSION_IDS - set(current))
			if missing:
				store[name] = _normalize_permission_ids(current + missing)
				dirty = True
			continue
		if title_l == "repositor":
			need = ["ops.check", "ops.armado"]
			missing = [p for p in need if p not in current]
			if missing:
				store[name] = _normalize_permission_ids(current + missing)
				dirty = True
			continue
		if title_l in ("sales", "ventas"):
			# Soft-add catalog PDF + own Pedidos when missing; never re-add stripped floor ops.
			need = ["tools.catalog_pdf", "tables.orders.own"]
			missing = [p for p in need if p not in current]
			if missing:
				store[name] = _normalize_permission_ids(current + missing)
				dirty = True
	return dirty


def _ensure_starter_staff_groups() -> dict:
	"""Create repositor / caja / admin if missing. Do not overwrite customised lists.

	Empty lists get the current starter perms. Groups still on a known legacy starter
	fingerprint (pre ops-only) are upgraded in place.
	"""
	if getattr(frappe.local, "_staff_starter_ensured", False):
		return {"created": [], "attached": [], "upgraded": [], "skipped": []}
	frappe.local._staff_starter_ensured = True
	frappe.flags.ignore_permissions = True
	store = _load_perm_store()
	created = []
	attached = []
	upgraded = []
	skipped = []
	dirty = _apply_caja_orders_split(store)
	dirty = _apply_default_sales_scope_to_store(store) or dirty
	dirty = _strip_sales_floor_ops_extras(store) or dirty
	dirty = _backfill_floor_fulfillment_perms(store) or dirty
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
			elif (
				_is_legacy_starter_perms(title, current)
				or _floor_starter_needs_ops_only_upgrade(title, current)
			) and _perm_set(current) != _perm_set(perms):
				store[existing] = perms
				upgraded.append({"name": existing, "employee_group_name": title, "permissions": perms})
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
	# Seed field ACL matrices for starters that have none yet (settings UI).
	try:
		from erpnext.erpnext_integrations.ecommerce_api.field_acl import ensure_starter_field_acls

		ensure_starter_field_acls(
			[
				{"name": r["name"], "employee_group_name": r["employee_group_name"]}
				for r in (created + attached + upgraded + skipped)
				if r.get("name")
			]
		)
	except Exception:
		frappe.log_error(title="ensure_starter_field_acls")
	return {"created": created, "attached": attached, "upgraded": upgraded, "skipped": skipped}


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


def _load_staff_login_store() -> dict:
	if not frappe.db.exists("Table Extra Schema", STAFF_LOGIN_SCOPE):
		return {"by_employee": {}, "by_code": {}}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", STAFF_LOGIN_SCOPE)
	data = _parse_json(doc.columns_json, {})
	if not isinstance(data, dict):
		return {"by_employee": {}, "by_code": {}}
	by_employee = data.get("by_employee") if isinstance(data.get("by_employee"), dict) else {}
	by_code = data.get("by_code") if isinstance(data.get("by_code"), dict) else {}
	return {"by_employee": by_employee, "by_code": by_code}


def _save_staff_login_store(data: dict) -> None:
	payload = json.dumps(
		{
			"by_employee": data.get("by_employee") or {},
			"by_code": data.get("by_code") or {},
		},
		ensure_ascii=False,
	)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", STAFF_LOGIN_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", STAFF_LOGIN_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": STAFF_LOGIN_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _gen_staff_login_code(used: set[str]) -> str:
	"""12-digit numeric code starting with 99 (HID scanners + existing digit-only POS hook)."""
	suffix_len = STAFF_LOGIN_LEN - len(STAFF_LOGIN_PREFIX)
	for _ in range(80):
		suffix = "".join(secrets.choice(string.digits) for _ in range(suffix_len))
		code = f"{STAFF_LOGIN_PREFIX}{suffix}"
		if code not in used:
			return code
	frappe.throw(_("Could not allocate a unique staff login barcode"))


def _load_ops_pin_store() -> dict:
	if not frappe.db.exists("Table Extra Schema", STAFF_OPS_PIN_SCOPE):
		return {"by_employee": {}, "by_pin": {}}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", STAFF_OPS_PIN_SCOPE)
	data = _parse_json(doc.columns_json, {})
	if not isinstance(data, dict):
		return {"by_employee": {}, "by_pin": {}}
	by_employee = data.get("by_employee") if isinstance(data.get("by_employee"), dict) else {}
	by_pin = data.get("by_pin") if isinstance(data.get("by_pin"), dict) else {}
	return {"by_employee": by_employee, "by_pin": by_pin}


def _save_ops_pin_store(data: dict) -> None:
	payload = json.dumps(
		{
			"by_employee": data.get("by_employee") or {},
			"by_pin": data.get("by_pin") or {},
		},
		ensure_ascii=False,
	)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", STAFF_OPS_PIN_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", STAFF_OPS_PIN_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": STAFF_OPS_PIN_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _normalize_ops_pin(pin) -> str:
	raw = str(pin or "").strip()
	if not raw.isdigit() or len(raw) != STAFF_OPS_PIN_LEN:
		return ""
	return raw


def employee_ops_pin_taken(pin: str) -> bool:
	"""True when ``pin`` is already assigned to an employee."""
	raw = _normalize_ops_pin(pin)
	if not raw:
		return False
	store = _load_ops_pin_store()
	return raw in (store.get("by_pin") or {})


def _admin_pin_matches(pin: str) -> bool:
	"""Lazy import — avoid circular import with pos_session_api."""
	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import _verify_pin_value

	return bool(_verify_pin_value(pin))


def _gen_ops_pin(used: set[str]) -> str:
	for _ in range(120):
		candidate = "".join(secrets.choice(string.digits) for _ in range(STAFF_OPS_PIN_LEN))
		if candidate in used:
			continue
		if _admin_pin_matches(candidate):
			continue
		return candidate
	frappe.throw(_("Could not allocate a unique employee PIN"))


def _normalize_employee_list(employees) -> list[str]:
	if isinstance(employees, str):
		raw = employees.strip()
		if not raw or raw in ("null", "undefined", "None"):
			return []
		employees = frappe.parse_json(raw)
	if not isinstance(employees, list):
		return []
	out = []
	seen = set()
	for item in employees:
		name = str(item or "").strip()
		if name and name not in seen:
			seen.add(name)
			out.append(name)
	return out


def _can_manage_staff_login_barcodes() -> bool:
	return (
		_can_app("tables.employees")
		or _can_app("tools.labels")
		or _can_app("employees.edit")
		or _can_app("employees.create_user")
	)


def _employee_in_admin_group(emp_name: str) -> bool:
	"""True when the employee is in the starter `admin` group (or a group with `*`)."""
	if not emp_name:
		return False
	groups = frappe.get_all(
		"Employee Group Table",
		filters={"employee": emp_name},
		pluck="parent",
		ignore_permissions=True,
	)
	for g in groups:
		title = (frappe.db.get_value("Employee Group", g, "employee_group_name") or g or "").strip().lower()
		if title == "admin" or str(g).strip().lower() == "admin":
			return True
		if "*" in _permissions_for_group(g):
			return True
	return False


def _scanner_login_forbidden(user_id: str, emp_name: str | None = None) -> bool:
	"""Scanner sign-in is for cashiers / non-admin staff only."""
	uid = (user_id or "").strip()
	if not uid or uid in ("Administrator", "Guest"):
		return True
	roles = set(frappe.get_roles(uid) or [])
	if "Administrator" in roles or "System Manager" in roles:
		return True
	if emp_name and _employee_in_admin_group(emp_name):
		return True
	return False


@frappe.whitelist()
def ensure_staff_login_barcodes(employees=None, rotate=0):
	"""Issue (or rotate) numeric POS login barcodes for employees that have a User.

	Returns printable rows for Tools > Labels. Requires employees or labels permission.
	"""
	if not _can_manage_staff_login_barcodes():
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	names = _normalize_employee_list(employees)
	if not names:
		frappe.throw(_("employees is required"))

	rotate = cint(rotate)
	store = _load_staff_login_store()
	by_employee = dict(store.get("by_employee") or {})
	by_code = dict(store.get("by_code") or {})
	used = set(str(k) for k in by_code.keys())
	rows = []
	changed = False

	for emp_name in names:
		if not frappe.db.exists("Employee", emp_name):
			rows.append(
				{
					"name": emp_name,
					"ok": False,
					"error": "not_found",
				}
			)
			continue
		frappe.flags.ignore_permissions = True
		emp = frappe.get_doc("Employee", emp_name)
		user_id = (emp.user_id or "").strip()
		if not user_id or not frappe.db.exists("User", user_id):
			rows.append(
				{
					"name": emp.name,
					"employee_name": emp.employee_name,
					"user_id": user_id or None,
					"ok": False,
					"error": "no_user",
				}
			)
			continue
		if cint(frappe.db.get_value("User", user_id, "enabled")) == 0:
			rows.append(
				{
					"name": emp.name,
					"employee_name": emp.employee_name,
					"user_id": user_id,
					"ok": False,
					"error": "user_disabled",
				}
			)
			continue
		if _scanner_login_forbidden(user_id, emp.name):
			rows.append(
				{
					"name": emp.name,
					"employee_name": emp.employee_name,
					"user_id": user_id,
					"ok": False,
					"error": "admin_forbidden",
				}
			)
			continue

		existing = by_employee.get(emp.name) if isinstance(by_employee.get(emp.name), dict) else None
		code = str((existing or {}).get("code") or "").strip()
		if rotate or not code or len(code) != STAFF_LOGIN_LEN or not code.startswith(STAFF_LOGIN_PREFIX):
			if code and code in by_code:
				by_code.pop(code, None)
				used.discard(code)
			code = _gen_staff_login_code(used)
			used.add(code)
			by_employee[emp.name] = {"code": code, "user_id": user_id}
			by_code[code] = emp.name
			changed = True
		else:
			# Keep mapping in sync if user_id changed.
			by_employee[emp.name] = {"code": code, "user_id": user_id}
			by_code[code] = emp.name

		rows.append(
			{
				"name": emp.name,
				"employee_name": emp.employee_name,
				"user_id": user_id,
				"login_barcode": code,
				"ok": True,
			}
		)

	if changed:
		_save_staff_login_store({"by_employee": by_employee, "by_code": by_code})

	return {"rows": rows}


@frappe.whitelist()
def resolve_staff_login_barcode(code=None):
	"""Resolve a staff login barcode to an enabled non-admin User.

	Intended for the Next.js `/api/auth/login-barcode` server route (API token).
	Does not grant a Frappe session by itself. Administrators / System Managers /
	admin-group members must use password sign-in.
	"""
	raw = str(code or "").strip()
	if not raw:
		frappe.throw(_("code is required"))
	if not raw.isdigit() or len(raw) != STAFF_LOGIN_LEN or not raw.startswith(STAFF_LOGIN_PREFIX):
		frappe.throw(_("Scan not recognized"))

	store = _load_staff_login_store()
	emp_name = (store.get("by_code") or {}).get(raw)
	if not emp_name:
		frappe.throw(_("Scan not recognized"))

	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("Employee", emp_name):
		frappe.throw(_("Scan not recognized"))
	emp = frappe.get_doc("Employee", emp_name)
	if (emp.status or "") != "Active":
		frappe.throw(_("Scan not recognized"))
	user_id = (emp.user_id or "").strip()
	if not user_id or not frappe.db.exists("User", user_id):
		frappe.throw(_("Scan not recognized"))
	if cint(frappe.db.get_value("User", user_id, "enabled")) == 0:
		frappe.throw(_("Scan not recognized"))

	# Stale mapping guard
	entry = (store.get("by_employee") or {}).get(emp.name)
	if isinstance(entry, dict) and str(entry.get("code") or "") != raw:
		frappe.throw(_("Scan not recognized"))

	if _scanner_login_forbidden(user_id, emp.name):
		frappe.throw(_("Scanner sign-in is not available for this account. Use password."))

	return {
		"username": user_id,
		"employee": emp.name,
		"employee_name": emp.employee_name,
		"login_barcode": raw,
	}


def _issue_ops_pin_for_employee(employee: str, *, rotate: bool = False) -> str | None:
	"""Allocate a unique 6-digit ops PIN for an employee (no permission check)."""
	emp = str(employee or "").strip()
	if not emp or not frappe.db.exists("Employee", emp):
		return None
	store = _load_ops_pin_store()
	by_employee = dict(store.get("by_employee") or {})
	by_pin = dict(store.get("by_pin") or {})
	used = set(str(k) for k in by_pin.keys())
	existing = by_employee.get(emp) if isinstance(by_employee.get(emp), dict) else None
	pin = str((existing or {}).get("pin") or "").strip()
	if rotate or not _normalize_ops_pin(pin):
		if pin and pin in by_pin:
			by_pin.pop(pin, None)
			used.discard(pin)
		pin = _gen_ops_pin(used)
		by_employee[emp] = {"pin": pin}
		by_pin[pin] = emp
		_save_ops_pin_store({"by_employee": by_employee, "by_pin": by_pin})
	return pin


@frappe.whitelist()
def ensure_employee_ops_pins(employees=None, rotate=0):
	"""Issue (or rotate) unique 6-digit ops PINs for employees.

	No User required — used for kiosk identity / roleplay. Requires employees permission.
	"""
	if not (
		_can_app("tables.employees")
		or _can_app("employees.edit")
		or _can_app("employees.create_user")
		or _can_app("tools.settings")
	):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	names = _normalize_employee_list(employees)
	if not names:
		frappe.throw(_("employees is required"))

	rotate = cint(rotate)
	rows = []
	for emp_name in names:
		if not frappe.db.exists("Employee", emp_name):
			rows.append({"name": emp_name, "ok": False, "error": "not_found"})
			continue
		frappe.flags.ignore_permissions = True
		emp = frappe.get_doc("Employee", emp_name)
		pin = _issue_ops_pin_for_employee(emp.name, rotate=bool(rotate))
		rows.append(
			{
				"name": emp.name,
				"employee_name": emp.employee_name,
				"user_id": (emp.user_id or "").strip() or None,
				"ops_pin": pin,
				"ok": True,
			}
		)

	return {"rows": rows}


@frappe.whitelist(allow_guest=True)
def resolve_ops_pin(pin=None):
	"""Resolve a 6-digit PIN to admin or employee identity (kiosk / roleplay)."""
	raw = _normalize_ops_pin(pin)
	if not raw:
		return {
			"authorized": False,
			"kind": None,
			"employee": None,
			"employee_name": None,
			"user_id": None,
			"permissions": [],
			"pin_configured": False,
		}

	from erpnext.erpnext_integrations.ecommerce_api.pos_session_api import (
		_pin_configured,
		_verify_pin_value,
	)

	if _verify_pin_value(raw):
		return {
			"authorized": True,
			"kind": "admin",
			"employee": None,
			"employee_name": None,
			"user_id": None,
			"permissions": ["*"],
			"pin_configured": _pin_configured(),
		}

	store = _load_ops_pin_store()
	emp_name = (store.get("by_pin") or {}).get(raw)
	if not emp_name:
		return {
			"authorized": False,
			"kind": None,
			"employee": None,
			"employee_name": None,
			"user_id": None,
			"permissions": [],
			"pin_configured": _pin_configured() or bool(store.get("by_pin")),
		}

	frappe.flags.ignore_permissions = True
	if not frappe.db.exists("Employee", emp_name):
		return {
			"authorized": False,
			"kind": None,
			"employee": None,
			"employee_name": None,
			"user_id": None,
			"permissions": [],
			"pin_configured": True,
		}
	emp = frappe.get_doc("Employee", emp_name)
	if (emp.status or "") != "Active":
		return {
			"authorized": False,
			"kind": None,
			"employee": None,
			"employee_name": None,
			"user_id": None,
			"permissions": [],
			"pin_configured": True,
		}
	entry = (store.get("by_employee") or {}).get(emp.name)
	if isinstance(entry, dict) and str(entry.get("pin") or "") != raw:
		return {
			"authorized": False,
			"kind": None,
			"employee": None,
			"employee_name": None,
			"user_id": None,
			"permissions": [],
			"pin_configured": True,
		}

	user_id = (emp.user_id or "").strip() or None
	return {
		"authorized": True,
		"kind": "employee",
		"employee": emp.name,
		"employee_name": emp.employee_name,
		"user_id": user_id,
		"permissions": _permission_ids_for_employee(emp.name),
		"pin_configured": True,
	}



# ---------------------------------------------------------------------------
# Employee sales (multi salesman assignment + seller_ref orders)
# ---------------------------------------------------------------------------

CUSTOMER_SELLERS_SCOPE = "settings.customer_assigned_sellers"


def _norm_optional_str(val) -> str:
	if val is None:
		return ""
	s = str(val).strip()
	if s.lower() in ("null", "undefined", "none", ""):
		return ""
	return s


def _resolve_seller(employee=None, user_id=None) -> dict:
	"""Resolve Employee + User for sales panels. Accepts either key."""
	emp_name = _norm_optional_str(employee)
	uid = _norm_optional_str(user_id)
	frappe.flags.ignore_permissions = True

	emp_doc = None
	if emp_name and frappe.db.exists("Employee", emp_name):
		emp_doc = frappe.get_doc("Employee", emp_name)
		uid = (emp_doc.user_id or "").strip() or uid
	elif uid:
		found = frappe.db.get_value("Employee", {"user_id": uid}, "name")
		if found:
			emp_doc = frappe.get_doc("Employee", found)
			emp_name = emp_doc.name
			uid = (emp_doc.user_id or "").strip() or uid

	if not emp_name and not uid:
		frappe.throw(_("Employee or user_id required"))

	employee_name = None
	if emp_doc:
		employee_name = emp_doc.employee_name
	elif uid and frappe.db.exists("User", uid):
		employee_name = frappe.db.get_value("User", uid, "full_name") or uid

	return {
		"employee": emp_name or None,
		"user_id": uid or None,
		"employee_name": employee_name or emp_name or uid,
		"has_user": bool(uid),
	}


def _load_customer_sellers_store() -> dict:
	if not frappe.db.exists("Table Extra Schema", CUSTOMER_SELLERS_SCOPE):
		return {"by_customer": {}, "by_user": {}}
	frappe.flags.ignore_permissions = True
	doc = frappe.get_doc("Table Extra Schema", CUSTOMER_SELLERS_SCOPE)
	data = _parse_json(doc.columns_json, {})
	if not isinstance(data, dict):
		return {"by_customer": {}, "by_user": {}}
	by_customer = data.get("by_customer") if isinstance(data.get("by_customer"), dict) else {}
	by_user = data.get("by_user") if isinstance(data.get("by_user"), dict) else {}
	# normalize values to list[str]
	by_customer = {
		str(k): [str(x).strip() for x in (v or []) if str(x).strip()]
		for k, v in by_customer.items()
		if isinstance(v, (list, tuple))
	}
	by_user = {
		str(k): [str(x).strip() for x in (v or []) if str(x).strip()]
		for k, v in by_user.items()
		if isinstance(v, (list, tuple))
	}
	return {"by_customer": by_customer, "by_user": by_user}


def _save_customer_sellers_store(data: dict) -> None:
	payload = json.dumps(
		{
			"by_customer": data.get("by_customer") or {},
			"by_user": data.get("by_user") or {},
		},
		ensure_ascii=False,
	)
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Table Extra Schema", CUSTOMER_SELLERS_SCOPE):
		doc = frappe.get_doc("Table Extra Schema", CUSTOMER_SELLERS_SCOPE)
		doc.columns_json = payload
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "Table Extra Schema", "scope": CUSTOMER_SELLERS_SCOPE, "columns_json": payload}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()


def _sync_account_manager(customer: str, salesmen: list[str]) -> None:
	"""Keep ERPNext Customer.account_manager = first salesman (desk compat)."""
	if not frappe.db.has_column("Customer", "account_manager"):
		return
	primary = salesmen[0] if salesmen else None
	frappe.db.set_value("Customer", customer, "account_manager", primary, update_modified=False)


def _migrate_account_manager_into_store(store: dict, customer: str) -> list[str]:
	"""If store empty for customer, seed from account_manager once."""
	cur = list((store.get("by_customer") or {}).get(customer) or [])
	if cur:
		return cur
	if not frappe.db.has_column("Customer", "account_manager"):
		return []
	am = frappe.db.get_value("Customer", customer, "account_manager") or ""
	am = str(am).strip()
	if not am:
		return []
	return [am]


def customer_salesmen(customer: str) -> list[str]:
	cust = _norm_optional_str(customer)
	if not cust:
		return []
	store = _load_customer_sellers_store()
	users = _migrate_account_manager_into_store(store, cust)
	# Persist migration lazily
	if users and cust not in (store.get("by_customer") or {}):
		_set_customer_salesmen_users(cust, users, store=store)
		store = _load_customer_sellers_store()
		users = list((store.get("by_customer") or {}).get(cust) or [])
	return list(users)


def _rebuild_by_user(by_customer: dict) -> dict:
	by_user: dict[str, list[str]] = {}
	for cust, users in (by_customer or {}).items():
		for u in users or []:
			by_user.setdefault(u, [])
			if cust not in by_user[u]:
				by_user[u].append(cust)
	return by_user


def _sales_group_names() -> list[str]:
	"""Official ``Sales`` group only; fall back to legacy ``ventas`` if Sales missing."""
	if frappe.db.exists("Employee Group", "Sales"):
		return ["Sales"]
	found = _find_employee_group_by_title("Sales")
	if found:
		return [found]
	# Legacy ES title from older seeds — do not dual-attach once Sales exists.
	if frappe.db.exists("Employee Group", "ventas"):
		return ["ventas"]
	found_legacy = _find_employee_group_by_title("ventas")
	if found_legacy:
		return [found_legacy]
	return []


def _add_employee_to_sales_groups(employee: str) -> None:
	"""Append employee to the official Sales group (legacy ventas only if Sales absent)."""
	emp = _norm_optional_str(employee)
	if not emp or not frappe.db.exists("Employee", emp):
		return
	groups = _sales_group_names()
	if not groups:
		try:
			_ensure_starter_staff_groups()
		except Exception:
			pass
		groups = _sales_group_names()
	if not groups:
		return
	emp_name = frappe.db.get_value("Employee", emp, "employee_name")
	user_id = frappe.db.get_value("Employee", emp, "user_id")
	frappe.flags.ignore_permissions = True
	for g in groups:
		already = frappe.db.exists(
			"Employee Group Table", {"parent": g, "employee": emp}
		)
		if already:
			continue
		parent = frappe.get_doc("Employee Group", g)
		parent.append(
			"employee_list",
			{"employee": emp, "employee_name": emp_name, "user_id": user_id},
		)
		parent.save(ignore_permissions=True)
	if user_id and frappe.db.exists("User", user_id):
		_apply_group_roles_to_user(user_id, emp)


def _create_user_for_employee_sales(emp) -> str:
	"""Create/link a User for an Employee (CRM salesman path; no create_user perm)."""
	frappe.flags.ignore_permissions = True
	if emp.user_id and frappe.db.exists("User", emp.user_id):
		return emp.user_id

	requested_email = (
		emp.prefered_email or emp.company_email or emp.personal_email or ""
	).strip()
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
			requested_email = ""
		else:
			user_name = requested_email
			linked_existing = True

	login_email = (
		requested_email
		if linked_existing
		else _unique_staff_email(requested_email, emp.employee_name or emp.name)
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
		# Attach roles before insert so Frappe does not warn "no roles enabled".
		for role in ("Employee", "Sales User", "Desk User"):
			if frappe.db.exists("Role", role):
				user.append("roles", {"role": role})
		user.insert(ignore_permissions=True)
		user_name = user.name
		_set_user_password(user_name, password)

	_set_user_roles(user_name, ["Employee", "Sales User"])
	emp.user_id = user_name
	emp.create_user_permission = 0
	emp.save(ignore_permissions=True)
	log_field_changes("Employee", emp.name, [("user_id", None, user_name)])
	return user_name


def _find_employee_by_label(label: str):
	"""Match Employee by user_id, name, or employee_name (case-insensitive)."""
	q = _norm_optional_str(label)
	if not q:
		return None
	frappe.flags.ignore_permissions = True
	if frappe.db.exists("Employee", {"user_id": q}):
		return frappe.get_doc("Employee", {"user_id": q})
	if frappe.db.exists("Employee", q):
		return frappe.get_doc("Employee", q)
	# Exact employee_name (case-insensitive via SQL)
	found = frappe.db.sql(
		"""
		SELECT name FROM `tabEmployee`
		WHERE LOWER(TRIM(COALESCE(employee_name, ''))) = %(q)s
		ORDER BY modified DESC
		LIMIT 1
		""",
		{"q": q.lower()},
	)
	if found:
		return frappe.get_doc("Employee", found[0][0])
	# Partial name match only when unique
	partial = frappe.db.sql(
		"""
		SELECT name FROM `tabEmployee`
		WHERE LOWER(TRIM(COALESCE(employee_name, ''))) LIKE %(pat)s
		ORDER BY modified DESC
		LIMIT 2
		""",
		{"pat": f"%{q.lower()}%"},
	)
	if len(partial) == 1:
		return frappe.get_doc("Employee", partial[0][0])
	return None


def _ensure_salesman_user_id(label: str) -> tuple[str, dict]:
	"""
	Resolve a typed CRM label to a User id.
	If no Employee/User matches, create Employee + User and add to Sales.
	Returns (user_id, meta).
	"""
	q = _norm_optional_str(label)
	if not q:
		frappe.throw(_("Salesman name required"))
	frappe.flags.ignore_permissions = True
	created = False

	# Direct User id / email
	if frappe.db.exists("User", q):
		uid = q
		emp = _find_employee_by_label(q)
		if emp:
			if not emp.user_id:
				uid = _create_user_for_employee_sales(emp)
				created = True
			else:
				uid = emp.user_id
			_add_employee_to_sales_groups(emp.name)
			return uid, {
				"user_id": uid,
				"employee": emp.name,
				"label": (emp.employee_name or uid).strip() or uid,
				"created": created,
			}
		# User without Employee: still assignable (legacy Administrator etc.)
		full = frappe.db.get_value("User", uid, "full_name") or uid
		return uid, {
			"user_id": uid,
			"employee": None,
			"label": full,
			"created": False,
		}

	emp = _find_employee_by_label(q)
	if emp:
		uid = (emp.user_id or "").strip()
		if not uid or not frappe.db.exists("User", uid):
			uid = _create_user_for_employee_sales(emp)
			created = True
		_add_employee_to_sales_groups(emp.name)
		return uid, {
			"user_id": uid,
			"employee": emp.name,
			"label": (emp.employee_name or uid).strip() or uid,
			"created": created,
		}

	# Create Employee + User in Sales
	from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company

	company = resolve_company() or frappe.db.get_value("Company", {}, "name")
	parts = q.split()
	first = parts[0]
	last = " ".join(parts[1:]) if len(parts) > 1 else ""
	doc = frappe.new_doc("Employee")
	doc.company = company
	doc.first_name = first
	doc.last_name = last or None
	doc.employee_name = q
	doc.status = "Active"
	doc.date_of_joining = today()
	doc.gender = "Prefer not to say"
	doc.date_of_birth = "1990-01-01"
	doc.insert(ignore_permissions=True)
	log_field_changes(
		"Employee",
		doc.name,
		[("employee_name", None, doc.employee_name), ("status", None, doc.status)],
	)
	uid = _create_user_for_employee_sales(doc)
	_add_employee_to_sales_groups(doc.name)
	_issue_ops_pin_for_employee(doc.name)
	frappe.db.commit()
	return uid, {
		"user_id": uid,
		"employee": doc.name,
		"label": q,
		"created": True,
	}


def _salesman_label_for_user(uid: str) -> str:
	uid = _norm_optional_str(uid)
	if not uid:
		return ""
	emp_name = frappe.db.get_value("Employee", {"user_id": uid}, "employee_name")
	if emp_name:
		return emp_name
	if frappe.db.exists("User", uid):
		return frappe.db.get_value("User", uid, "full_name") or uid
	return uid


def _set_customer_salesmen_users(customer: str, user_ids, store=None) -> list[str]:
	cust = _norm_optional_str(customer)
	if not cust:
		frappe.throw(_("Customer required"))
	if not frappe.db.exists("Customer", cust):
		frappe.throw(_("Customer {0} not found").format(cust))
	if isinstance(user_ids, str):
		try:
			user_ids = json.loads(user_ids)
		except Exception:
			user_ids = [x.strip() for x in user_ids.split(",") if x.strip()]
	cleaned = []
	seen = set()
	for u in user_ids or []:
		raw = _norm_optional_str(u)
		if not raw:
			continue
		uid, _meta = _ensure_salesman_user_id(raw)
		if not uid or uid in seen:
			continue
		seen.add(uid)
		cleaned.append(uid)
	store = store or _load_customer_sellers_store()
	by_customer = dict(store.get("by_customer") or {})
	if cleaned:
		by_customer[cust] = cleaned
	else:
		by_customer.pop(cust, None)
	by_user = _rebuild_by_user(by_customer)
	_save_customer_sellers_store({"by_customer": by_customer, "by_user": by_user})
	_sync_account_manager(cust, cleaned)
	return cleaned


def _assigned_customer_names(user_id: str) -> list[str]:
	uid = _norm_optional_str(user_id)
	if not uid:
		return []
	store = _load_customer_sellers_store()
	names = list((store.get("by_user") or {}).get(uid) or [])
	# Also include account_manager legacy rows not yet migrated
	if frappe.db.has_column("Customer", "account_manager"):
		legacy = frappe.get_all(
			"Customer",
			filters={"account_manager": uid, "disabled": 0},
			pluck="name",
			ignore_permissions=True,
		)
		for n in legacy:
			if n not in names:
				names.append(n)
	return names


def _is_sales_admin() -> bool:
	info = _acting_perm_info()
	if not info:
		return True
	if info.get("source") == "admin" or "*" in (info.get("permissions") or []):
		return True
	return False


def sales_visibility_scope(user_id=None) -> str:
	"""Return 'all' | 'assigned' | 'none' for RM client list."""
	if _is_sales_admin():
		return "all"
	if _can_app("sales.see_all"):
		return "all"
	if _can_app("sales.see_assigned"):
		return "assigned"
	# Legacy: tables.crm without explicit sales.* → all
	if _can_app("tables.crm"):
		return "all"
	return "none"


def sales_commit_scope(user_id=None) -> str:
	"""Return 'all' | 'assigned' | 'none' for Preventa win/convert."""
	if _is_sales_admin():
		return "all"
	if _can_app("sales.commit_all"):
		return "all"
	if _can_app("sales.commit_assigned"):
		return "assigned"
	# Legacy preventa without explicit commit flags → assigned (safer)
	if _can_app("ops.preventa"):
		return "assigned"
	return "none"


def can_see_customer(customer: str, user_id=None) -> bool:
	scope = sales_visibility_scope()
	if scope == "all":
		return True
	if scope == "none":
		return False
	uid = _norm_optional_str(user_id) or _acting_username() or ""
	if not uid:
		return False
	return _norm_optional_str(customer) in set(_assigned_customer_names(uid))


def can_commit_customer(customer: str | None, user_id=None) -> bool:
	scope = sales_commit_scope()
	if scope == "all":
		return True
	if scope == "none":
		return False
	uid = _norm_optional_str(user_id) or _acting_username() or ""
	cust = _norm_optional_str(customer)
	if not cust:
		# New lead / no customer yet: allow own pipeline commit when assigned-scope
		return bool(uid)
	return cust in set(_assigned_customer_names(uid))


def require_sales_commit_for_customer(customer: str | None = None) -> None:
	if can_commit_customer(customer):
		return
	frappe.throw(_("Not permitted to commit sales for this client (sales.commit_*)"))


def _customer_row(name: str) -> dict | None:
	if not name or not frappe.db.exists("Customer", name):
		return None
	fields = ["name", "customer_name", "mobile_no", "email_id", "disabled"]
	if frappe.db.has_column("Customer", "account_manager"):
		fields.append("account_manager")
	r = frappe.db.get_value("Customer", name, fields, as_dict=True)
	if not r:
		return None
	salesmen = customer_salesmen(r.name)
	stage = None
	stage_label = None
	try:
		from erpnext.erpnext_integrations.ecommerce_api.api import _customer_preventa_stage_map

		st = (_customer_preventa_stage_map([r.name]).get(r.name) or {})
		stage = st.get("stage")
		stage_label = st.get("stage_label")
	except Exception:
		pass
	return {
		"name": r.name,
		"customer_name": r.customer_name,
		"phone": r.mobile_no,
		"email": r.email_id,
		"account_manager": getattr(r, "account_manager", None) or (salesmen[0] if salesmen else None),
		"salesmen": salesmen,
		"disabled": cint(r.disabled),
		"stage": stage,
		"stage_label": stage_label,
	}


@frappe.whitelist(allow_guest=True)
def set_customer_salesmen(customer=None, user_ids=None):
	"""Replace the multi salesman list on a Customer (admin / CRM editors).

	``user_ids`` may be User ids, emails, or employee display names. Unknown
	labels create an Employee + User and attach them to Sales.

	Sales floor may self-assign only (so they can own temporal clients they create).
	"""
	acting = _acting_username() or ""
	raw_ids = user_ids
	if isinstance(raw_ids, str):
		try:
			raw_ids = json.loads(raw_ids)
		except Exception:
			raw_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
	normalized = [_norm_optional_str(u) for u in (raw_ids or [])]
	normalized = [u for u in normalized if u]
	self_only = (
		bool(acting)
		and len(normalized) == 1
		and normalized[0].lower() == acting.lower()
		and (
			_can_app("sales.commit_assigned")
			or _can_app("sales.commit_all")
			or _can_app("ops.catalog")
			or _can_app("ops.preventa")
		)
	)
	if not (
		_can_app("tables.crm")
		or _can_app("employees.edit")
		or _is_sales_admin()
		or self_only
	):
		_require_app_permission("tables.crm")
	cust = _norm_optional_str(customer)
	users = _set_customer_salesmen_users(cust, user_ids)
	labels = [_salesman_label_for_user(u) for u in users]
	frappe.db.commit()
	return {
		"ok": True,
		"customer": cust,
		"salesmen": users,
		"salesmen_labels": labels,
	}


@frappe.whitelist(allow_guest=True)
def get_customer_salesmen(customer=None):
	cust = _norm_optional_str(customer)
	if not cust:
		frappe.throw(_("Customer required"))
	users = customer_salesmen(cust)
	return {
		"ok": True,
		"customer": cust,
		"salesmen": users,
		"salesmen_labels": [_salesman_label_for_user(u) for u in users],
	}


@frappe.whitelist(allow_guest=True)
def ensure_salesman(name=None):
	"""Resolve or create a salesman Employee+User for CRM vendedores pickers."""
	if not (_can_app("tables.crm") or _can_app("employees.edit") or _is_sales_admin()):
		_require_app_permission("tables.crm")
	label = _norm_optional_str(name)
	if not label:
		frappe.throw(_("Salesman name required"))
	uid, meta = _ensure_salesman_user_id(label)
	frappe.db.commit()
	return {"ok": True, **meta}


@frappe.whitelist(allow_guest=True)
def list_salesman_options(search=None, page_length=200):
	"""Employee/User options for the CRM Vendedores multi-select."""
	if not (
		_can_app("tables.crm")
		or _can_app("tables.employees")
		or _can_app("employees.edit")
		or _is_sales_admin()
	):
		_require_app_permission("tables.crm")
	q = _norm_optional_str(search)
	limit = max(1, min(cint(page_length) or 200, 500))
	frappe.flags.ignore_permissions = True
	filters = {"status": "Active"}
	rows = frappe.get_all(
		"Employee",
		filters=filters,
		fields=["name", "employee_name", "user_id", "status"],
		order_by="employee_name asc",
		limit_page_length=limit * 2 if q else limit,
		ignore_permissions=True,
	)
	out = []
	seen_users = set()
	for r in rows:
		label = (r.employee_name or r.name or "").strip()
		uid = (r.user_id or "").strip() or None
		if q:
			hay = f"{label} {uid or ''} {r.name}".lower()
			if q.lower() not in hay:
				continue
		if uid:
			seen_users.add(uid)
		out.append(
			{
				"employee": r.name,
				"user_id": uid,
				"label": label or uid or r.name,
				"has_user": bool(uid),
			}
		)
		if len(out) >= limit:
			break
	return {"ok": True, "options": out}

def _seller_ref_like(user_id: str) -> str:
	return f"%seller_ref:{user_id}%"


def _so_tag_field() -> str | None:
	from erpnext.erpnext_integrations.ecommerce_api.api import _guest_preorder_tag_fieldname

	return _guest_preorder_tag_fieldname()


def _list_seller_ref_orders(uid: str, *, start=0, page_length=50) -> list[dict]:
	"""Sales Orders tagged seller_ref:<uid> on remarks/terms."""
	if not uid:
		return []
	tag_fn = _so_tag_field()
	if not tag_fn:
		return []
	from erpnext.erpnext_integrations.ecommerce_api.api import _seller_ref_from_guest_preorder

	fields = [
		"name",
		"customer",
		"customer_name",
		"transaction_date",
		"delivery_date",
		"grand_total",
		"status",
		"currency",
		tag_fn,
	]
	raw = frappe.get_all(
		"Sales Order",
		filters={"docstatus": ["<", 2], tag_fn: ["like", _seller_ref_like(uid)]},
		fields=fields,
		order_by="transaction_date desc, creation desc",
		limit_start=0,
		limit_page_length=max(start + page_length + 80, 100),
		ignore_permissions=True,
	)
	matched = []
	for o in raw:
		if (_seller_ref_from_guest_preorder(o) or "") != uid:
			continue
		matched.append(
			{
				"name": o.name,
				"customer": o.customer,
				"customer_name": o.customer_name,
				"transaction_date": str(o.transaction_date) if o.transaction_date else None,
				"delivery_date": str(o.delivery_date) if o.delivery_date else None,
				"grand_total": flt(o.grand_total),
				"status": o.status,
				"currency": o.currency,
			}
		)
	return matched[start : start + page_length]


@frappe.whitelist(allow_guest=True)
def get_employee_sales(employee=None, user_id=None):
	"""Overview for Empleados/Pedidos Ventas tab: seller identity + clients + recent orders."""
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	customers = []
	for n in _assigned_customer_names(uid):
		row = _customer_row(n)
		if row:
			customers.append(row)

	orders = _list_seller_ref_orders(uid, start=0, page_length=25) if uid else []

	order_total = sum(flt(o.get("grand_total")) for o in orders)
	return {
		"ok": True,
		"seller": seller,
		"customers": customers,
		"orders": orders,
		"summary": {
			"customers_count": len(customers),
			"orders_count": len(orders),
			"orders_total": order_total,
		},
	}


@frappe.whitelist(allow_guest=True)
def list_employee_customers(employee=None, user_id=None):
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	rows = []
	for n in _assigned_customer_names(uid):
		row = _customer_row(n)
		if row:
			# Light order count attributed to this seller for the customer
			cnt = 0
			if uid:
				tag_fn = _so_tag_field()
				if tag_fn:
					cnt = cint(
						frappe.db.count(
							"Sales Order",
							{
								"customer": n,
								"docstatus": ["<", 2],
								tag_fn: ["like", _seller_ref_like(uid)],
							},
						)
					)
			row["orders_count"] = cnt
			rows.append(row)
	return {"ok": True, "seller": seller, "customers": rows}


@frappe.whitelist(allow_guest=True)
def assign_employee_customer(employee=None, user_id=None, customer=None):
	"""Add salesman to Customer multi-assignment list."""
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	cust = _norm_optional_str(customer)
	if not uid:
		frappe.throw(_("Employee has no login user — create a user first"))
	if not cust:
		frappe.throw(_("Customer required"))
	users = customer_salesmen(cust)
	if uid not in users:
		users.append(uid)
	_set_customer_salesmen_users(cust, users)
	row = _customer_row(cust)
	return {"ok": True, "seller": seller, "customer": row}


@frappe.whitelist(allow_guest=True)
def unassign_employee_customer(employee=None, user_id=None, customer=None):
	"""Remove salesman from Customer multi-assignment list."""
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	cust = _norm_optional_str(customer)
	if not cust:
		frappe.throw(_("Customer required"))
	users = [u for u in customer_salesmen(cust) if u != uid]
	_set_customer_salesmen_users(cust, users)
	row = _customer_row(cust)
	return {"ok": True, "seller": seller, "customer": row}


@frappe.whitelist(allow_guest=True)
def list_employee_orders(employee=None, user_id=None, start=0, page_length=50):
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	start = max(0, cint(start) or 0)
	page_length = max(1, min(200, cint(page_length) or 50))
	if not uid:
		return {"ok": True, "seller": seller, "total": 0, "rows": []}

	# Fetch a bounded pool then slice for paging.
	matched = _list_seller_ref_orders(uid, start=0, page_length=500)
	total = len(matched)
	rows = matched[start : start + page_length]
	return {"ok": True, "seller": seller, "total": total, "rows": rows}


@frappe.whitelist(allow_guest=True)
def list_employee_invoices(
	employee=None, user_id=None, is_return=0, start=0, page_length=50
):
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	start = max(0, cint(start) or 0)
	page_length = max(1, min(200, cint(page_length) or 50))
	is_ret = 1 if cint(is_return) else 0
	customers = _assigned_customer_names(uid)
	if not customers:
		return {"ok": True, "seller": seller, "total": 0, "rows": [], "is_return": is_ret}

	filters = {
		"customer": ["in", customers],
		"docstatus": ["<", 2],
		"is_return": is_ret,
	}
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"status",
			"docstatus",
			"is_return",
			"return_against",
			"currency",
			"customer",
			"customer_name",
		],
		order_by="posting_date desc, creation desc",
		limit_start=start,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = frappe.db.count("Sales Invoice", filters)
	out = []
	for r in rows:
		out.append(
			{
				"name": r.name,
				"posting_date": str(r.posting_date) if r.posting_date else None,
				"due_date": str(r.due_date) if r.due_date else None,
				"grand_total": flt(r.grand_total),
				"outstanding_amount": flt(r.outstanding_amount),
				"status": r.status,
				"docstatus": cint(r.docstatus),
				"is_return": cint(r.is_return),
				"return_against": r.return_against,
				"currency": r.currency,
				"customer": r.customer,
				"customer_name": r.customer_name,
			}
		)
	return {"ok": True, "seller": seller, "total": cint(total), "rows": out, "is_return": is_ret}


@frappe.whitelist(allow_guest=True)
def list_employee_payments(employee=None, user_id=None, start=0, page_length=50):
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	start = max(0, cint(start) or 0)
	page_length = max(1, min(200, cint(page_length) or 50))
	customers = _assigned_customer_names(uid)
	if not customers:
		return {"ok": True, "seller": seller, "total": 0, "rows": []}

	filters = {
		"party_type": "Customer",
		"party": ["in", customers],
		"docstatus": ["<", 2],
	}
	rows = frappe.get_all(
		"Payment Entry",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"payment_type",
			"mode_of_payment",
			"paid_amount",
			"received_amount",
			"status",
			"docstatus",
			"party",
			"party_name",
			"reference_no",
			"reference_date",
		],
		order_by="posting_date desc, creation desc",
		limit_start=start,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	total = frappe.db.count("Payment Entry", filters)
	out = []
	for r in rows:
		amt = flt(r.received_amount) if r.payment_type == "Receive" else flt(r.paid_amount)
		out.append(
			{
				"name": r.name,
				"posting_date": str(r.posting_date) if r.posting_date else None,
				"payment_type": r.payment_type,
				"mode_of_payment": r.mode_of_payment,
				"amount": amt,
				"status": r.status,
				"docstatus": cint(r.docstatus),
				"party": r.party,
				"party_name": r.party_name,
				"reference_no": r.reference_no,
				"reference_date": str(r.reference_date) if r.reference_date else None,
			}
		)
	return {"ok": True, "seller": seller, "total": cint(total), "rows": out}


@frappe.whitelist(allow_guest=True)
def list_employee_products(employee=None, user_id=None, page_length=100):
	"""Aggregate SI items for assigned customers (same idea as CRM party products)."""
	seller = _resolve_seller(employee=employee, user_id=user_id)
	uid = seller.get("user_id") or ""
	page_length = max(1, min(300, cint(page_length) or 100))
	customers = _assigned_customer_names(uid)
	if not customers:
		return {"ok": True, "seller": seller, "products": []}

	inv_names = frappe.get_all(
		"Sales Invoice",
		filters={"customer": ["in", customers], "docstatus": 1, "is_return": 0},
		pluck="name",
		ignore_permissions=True,
	)
	if not inv_names:
		return {"ok": True, "seller": seller, "products": []}

	items = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", inv_names]},
		fields=["item_code", "item_name", "qty", "amount", "rate", "parent"],
		ignore_permissions=True,
	)
	inv_dates = {
		r.name: r.posting_date
		for r in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", inv_names]},
			fields=["name", "posting_date"],
			ignore_permissions=True,
		)
	}
	agg: dict[str, dict] = {}
	for it in items:
		code = it.item_code or ""
		if not code:
			continue
		slot = agg.setdefault(
			code,
			{
				"id": code,
				"item_code": code,
				"item_name": it.item_name,
				"qty_total": 0.0,
				"amount_total": 0.0,
				"invoice_count": 0,
				"last_date": None,
				"last_rate": 0.0,
				"_parents": set(),
			},
		)
		slot["qty_total"] += flt(it.qty)
		slot["amount_total"] += flt(it.amount)
		slot["_parents"].add(it.parent)
		slot["last_rate"] = flt(it.rate)
		d = inv_dates.get(it.parent)
		if d and (not slot["last_date"] or str(d) > str(slot["last_date"])):
			slot["last_date"] = str(d)

	products = []
	for slot in agg.values():
		parents = slot.pop("_parents", set())
		slot["invoice_count"] = len(parents)
		products.append(slot)
	products.sort(key=lambda p: (-flt(p["amount_total"]), p["item_code"]))
	return {"ok": True, "seller": seller, "products": products[:page_length]}
