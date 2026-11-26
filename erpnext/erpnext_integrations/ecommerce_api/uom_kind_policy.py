"""UOM kind (medida | contenedor | unidad) + nested unit_sku leaf factors.

Retro-compatible: when ``UOM.custom_uom_kind`` is missing or blank, kind is
inferred from the same name aliases SilkOS already used (WEIGHT / CAJA / Nos).
"""

from __future__ import annotations

from frappe import _
from frappe.utils import cint, cstr, flt

UOM_KINDS = ("medida", "contenedor", "unidad")

# Built-in masters — kind is locked after seed.
_BUILTIN_KIND = {
	"WEIGHT": "medida",
	"CAJA": "contenedor",
	"Nos": "unidad",
}

_MEDIDA_NAME_TOKENS = frozenset(
	{
		"weight",
		"por peso",
		"porpeso",
		"kg",
		"kgs",
		"kilogram",
		"kilograms",
		"g",
		"gr",
		"gram",
		"grams",
		"l",
		"lt",
		"liter",
		"litre",
		"liters",
		"litres",
		"ml",
		"milliliter",
		"millilitre",
		"lb",
		"lbs",
		"oz",
	}
)

_CONTENEDOR_NAME_TOKENS = frozenset({"caja", "box", "boxes", "cajas"})

_UNIDAD_NAME_TOKENS = frozenset(
	{"nos", "nos (single)", "nos(single)", "unit", "units", "unidad", "unidades"}
)


def _norm(name) -> str:
	return cstr(name or "").strip()


def _norm_key(name) -> str:
	return _norm(name).lower()


def ensure_uom_kind_custom_field() -> None:
	"""Idempotent Custom Field on UOM + seed kinds on built-ins."""
	import frappe
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"UOM": [
				{
					"fieldname": "custom_uom_kind",
					"fieldtype": "Select",
					"label": "UOM Kind",
					"options": "\n".join(UOM_KINDS),
					"insert_after": "enabled",
					"reqd": 0,
				},
			]
		}
	)
	_seed_builtin_uom_kinds()


def _seed_builtin_uom_kinds() -> None:
	import frappe

	if not frappe.db.has_column("UOM", "custom_uom_kind"):
		return
	for name, kind in _BUILTIN_KIND.items():
		if not frappe.db.exists("UOM", name):
			continue
		cur = frappe.db.get_value("UOM", name, "custom_uom_kind")
		if cstr(cur or "").strip() != kind:
			frappe.db.set_value("UOM", name, "custom_uom_kind", kind, update_modified=False)


def kind_from_name_legacy(uom_name) -> str | None:
	"""Infer kind from historical name aliases (pre-custom_uom_kind)."""
	key = _norm_key(uom_name)
	if not key:
		return None
	if key in _CONTENEDOR_NAME_TOKENS:
		return "contenedor"
	if key in _UNIDAD_NAME_TOKENS:
		return "unidad"
	if key in _MEDIDA_NAME_TOKENS:
		return "medida"
	# Exact built-in master names
	raw = _norm(uom_name)
	if raw in _BUILTIN_KIND:
		return _BUILTIN_KIND[raw]
	return None


def uom_kind(uom_name, kind_hint=None) -> str:
	"""Resolve kind: hint → DB custom field → legacy name → default unidad."""
	import frappe

	hint = cstr(kind_hint or "").strip().lower()
	if hint in UOM_KINDS:
		return hint

	name = _norm(uom_name)
	if not name:
		return "unidad"

	if frappe.db.has_column("UOM", "custom_uom_kind") and frappe.db.exists("UOM", name):
		stored = cstr(frappe.db.get_value("UOM", name, "custom_uom_kind") or "").strip().lower()
		if stored in UOM_KINDS:
			return stored

	legacy = kind_from_name_legacy(name)
	if legacy:
		return legacy
	return "unidad"


def is_medida_uom(uom_name, kind_hint=None) -> bool:
	return uom_kind(uom_name, kind_hint) == "medida"


def is_contenedor_uom(uom_name, kind_hint=None) -> bool:
	return uom_kind(uom_name, kind_hint) == "contenedor"


def is_unidad_uom(uom_name, kind_hint=None) -> bool:
	return uom_kind(uom_name, kind_hint) == "unidad"


def list_uoms_with_kind() -> list[dict]:
	"""Enabled UOMs as ``{name, kind}`` — kind always resolved (DB or legacy)."""
	import frappe

	ensure_uom_kind_custom_field()
	has_kind = frappe.db.has_column("UOM", "custom_uom_kind")
	fields = ["name"]
	if has_kind:
		fields.append("custom_uom_kind")
	try:
		rows = frappe.get_all(
			"UOM",
			filters={"enabled": 1},
			fields=fields,
			order_by="name",
			ignore_permissions=True,
		)
	except Exception:
		return []
	out = []
	for r in rows:
		name = r.get("name")
		if not name:
			continue
		hint = r.get("custom_uom_kind") if has_kind else None
		out.append({"name": name, "kind": uom_kind(name, hint)})
	return out


def ensure_uom_with_kind(name: str, kind: str | None = None) -> str | None:
	"""Create or update a UOM; set kind when provided (built-ins keep locked kind)."""
	import frappe

	ensure_uom_kind_custom_field()
	uom_name = _norm(name)
	if not uom_name:
		return None

	want_kind = cstr(kind or "").strip().lower() or None
	if want_kind and want_kind not in UOM_KINDS:
		frappe.throw(_("Invalid UOM kind: {0}").format(want_kind))

	# Built-ins always use their locked kind.
	locked = _BUILTIN_KIND.get(uom_name)
	if locked:
		want_kind = locked

	if not frappe.db.exists("UOM", uom_name):
		if not want_kind:
			want_kind = kind_from_name_legacy(uom_name) or "unidad"
		doc = {
			"doctype": "UOM",
			"uom_name": uom_name,
			"enabled": 1,
		}
		if want_kind == "medida":
			doc["must_be_whole_number"] = 0
		else:
			doc["must_be_whole_number"] = 1
		if frappe.db.has_column("UOM", "custom_uom_kind"):
			doc["custom_uom_kind"] = want_kind
		frappe.get_doc(doc).insert(ignore_permissions=True)
		return uom_name

	if want_kind and frappe.db.has_column("UOM", "custom_uom_kind"):
		cur = cstr(frappe.db.get_value("UOM", uom_name, "custom_uom_kind") or "").strip()
		if locked and cur and cur != locked:
			frappe.db.set_value("UOM", uom_name, "custom_uom_kind", locked, update_modified=False)
		elif not cur or (not locked and cur != want_kind):
			# Allow setting kind on blank custom UOMs; do not silently flip locked built-ins.
			if not locked:
				frappe.db.set_value("UOM", uom_name, "custom_uom_kind", want_kind, update_modified=False)
			elif not cur:
				frappe.db.set_value("UOM", uom_name, "custom_uom_kind", locked, update_modified=False)
	return uom_name


def _item_pack_link(item_code: str) -> tuple[str | None, float]:
	"""(unit_sku or None, pack_qty). Self / empty unit_sku → leaf."""
	import frappe

	code = _norm(item_code)
	if not code or not frappe.db.exists("Item", code):
		return None, 1.0
	unit = ""
	pack = 1.0
	if frappe.db.has_column("Item", "custom_unit_sku"):
		unit = cstr(frappe.db.get_value("Item", code, "custom_unit_sku") or "").strip()
	if frappe.db.has_column("Item", "custom_pack_qty"):
		pack = flt(frappe.db.get_value("Item", code, "custom_pack_qty") or 0) or 1.0
	if not unit or unit == code:
		return None, 1.0
	return unit, pack


def leaf_factor(item_code: str, max_depth: int = 32) -> dict:
	"""Walk unit_sku chain → leaf. Returns ``{leaf, factor, path, depth}``.

	``factor`` = product of pack_qty along the path (1 for a leaf).
	Raises ValidationError on cycles.
	"""
	import frappe

	start = _norm(item_code)
	if not start:
		frappe.throw(_("Item code is required"))

	path = [start]
	factor = 1.0
	seen = {start}
	cur = start
	for _ in range(max_depth):
		child, pack = _item_pack_link(cur)
		if not child:
			return {
				"leaf": cur,
				"factor": factor,
				"path": path,
				"depth": len(path) - 1,
			}
		if child in seen:
			frappe.throw(
				_("Unit SKU cycle detected: {0} → {1}").format(" → ".join(path), child)
			)
		factor *= max(flt(pack), 1.0)
		path.append(child)
		seen.add(child)
		cur = child

	frappe.throw(_("Unit SKU chain too deep for {0}").format(start))


def validate_unit_sku_link(item_code: str, unit_sku: str | None, pack_qty=None) -> None:
	"""Reject self-cycles and DAG cycles when setting unit_sku."""
	import frappe

	code = _norm(item_code)
	unit = _norm(unit_sku)
	if not code or not unit or unit == code:
		return
	if not frappe.db.exists("Item", unit):
		frappe.throw(_("Unit SKU {0} not found").format(unit))
	# Detect cycle: following unit's chain must not reach code.
	seen = {code}
	cur = unit
	for _ in range(32):
		if cur in seen:
			frappe.throw(
				_("Unit SKU would create a cycle ({0} → … → {1})").format(code, cur)
			)
		seen.add(cur)
		child, _pack = _item_pack_link(cur)
		if not child:
			break
		cur = child
	pack = flt(pack_qty) if pack_qty is not None else None
	if pack is not None and pack < 1:
		frappe.throw(_("Pack qty must be at least 1 for a container link"))


def enrich_rows_with_pack_graph(rows: list) -> None:
	"""In-place: add uom_kind, leaf_sku, leaf_factor, leaf_title (+ pack fields if missing).

	Works for PM rows (``client_sku``) and sales/armado lines (``item_code``).
	"""
	import frappe

	if not rows:
		return

	codes = []
	for r in rows:
		sku = cstr(r.get("client_sku") or r.get("item_code") or "").strip()
		if sku:
			codes.append(sku)
	codes = list(dict.fromkeys(codes))

	item_meta: dict[str, dict] = {}
	if codes:
		fields = ["name", "item_name", "stock_uom"]
		if frappe.db.has_column("Item", "custom_pack_qty"):
			fields.append("custom_pack_qty")
		if frappe.db.has_column("Item", "custom_unit_sku"):
			fields.append("custom_unit_sku")
		for it in frappe.get_all(
			"Item",
			filters={"name": ("in", codes)},
			fields=fields,
			ignore_permissions=True,
		):
			item_meta[it.name] = it

	uoms = set()
	for r in rows:
		u = cstr(r.get("stock_uom") or r.get("uom") or "").strip()
		if u:
			uoms.add(u)
		sku = cstr(r.get("client_sku") or r.get("item_code") or "").strip()
		meta = item_meta.get(sku) or {}
		if meta.get("stock_uom"):
			uoms.add(cstr(meta.stock_uom).strip())

	kind_by_uom: dict[str, str] = {}
	has_kind_col = frappe.db.has_column("UOM", "custom_uom_kind")
	if uoms and has_kind_col:
		for u in frappe.get_all(
			"UOM",
			filters={"name": ("in", list(uoms))},
			fields=["name", "custom_uom_kind"],
			ignore_permissions=True,
		):
			kind_by_uom[u.name] = cstr(u.custom_uom_kind or "").strip().lower()

	leaf_cache: dict[str, dict] = {}
	titles: dict[str, str] = {}

	def _title(code: str) -> str:
		if code in titles:
			return titles[code]
		meta = item_meta.get(code)
		if meta and meta.get("item_name"):
			titles[code] = cstr(meta.item_name)
			return titles[code]
		t = ""
		if frappe.db.exists("Item", code):
			t = cstr(frappe.db.get_value("Item", code, "item_name") or code)
		titles[code] = t or code
		return titles[code]

	for row in rows:
		sku = cstr(row.get("client_sku") or row.get("item_code") or "").strip()
		meta = item_meta.get(sku) or {}
		if not row.get("stock_uom") and meta.get("stock_uom"):
			row["stock_uom"] = meta.stock_uom
		if row.get("pack_qty") is None and meta.get("custom_pack_qty") is not None:
			row["pack_qty"] = meta.custom_pack_qty
		if not row.get("unit_sku") and meta.get("custom_unit_sku"):
			row["unit_sku"] = meta.custom_unit_sku
		uom = cstr(row.get("stock_uom") or row.get("uom") or "").strip()
		row["uom_kind"] = uom_kind(uom, kind_by_uom.get(uom))
		if not sku:
			row["leaf_sku"] = None
			row["leaf_factor"] = 1
			row["leaf_title"] = None
			continue
		if sku not in leaf_cache:
			try:
				leaf_cache[sku] = leaf_factor(sku)
			except Exception:
				leaf_cache[sku] = {"leaf": sku, "factor": 1.0, "path": [sku], "depth": 0}
		info = leaf_cache[sku]
		leaf = info["leaf"]
		row["leaf_sku"] = leaf
		row["leaf_factor"] = flt(info["factor"]) or 1.0
		row["leaf_title"] = _title(leaf) if leaf else None
		# Ready-made armado/POS line (es).
		q = flt(row.get("qty") or 1) or 1
		if row["uom_kind"] == "contenedor" and row["leaf_factor"] > 1:
			leaf_qty = q * flt(row["leaf_factor"])
			row["pack_contents_line"] = (
				f"{cint(q) if q == int(q) else q} cajas "
				f"({cint(leaf_qty) if leaf_qty == int(leaf_qty) else leaf_qty} de {leaf})"
			)
		else:
			row["pack_contents_line"] = None


def format_pack_contents(qty, item_code: str, lang: str = "es") -> dict:
	"""UI payload for catalog / POS / armado contents lines."""
	q = flt(qty) or 0
	info = leaf_factor(item_code)
	leaf = info["leaf"]
	factor = flt(info["factor"]) or 1.0
	leaf_qty = q * factor
	import frappe

	leaf_title = leaf
	if leaf and frappe.db.exists("Item", leaf):
		leaf_title = cstr(frappe.db.get_value("Item", leaf, "item_name") or leaf)
	child, pack = _item_pack_link(item_code)
	child_title = child
	if child and frappe.db.exists("Item", child):
		child_title = cstr(frappe.db.get_value("Item", child, "item_name") or child)
	pack_label = cint(pack) if pack == int(pack) else pack
	if lang == "en":
		line = f"{cint(q) if q == int(q) else q} boxes ({cint(leaf_qty) if leaf_qty == int(leaf_qty) else leaf_qty} of {leaf})"
		contains = f"Contains {pack_label} × {child_title}" if child else ""
	elif lang == "zh":
		line = f"{cint(q) if q == int(q) else q} 箱（{cint(leaf_qty) if leaf_qty == int(leaf_qty) else leaf_qty} × {leaf}）"
		contains = f"含 {pack_label} × {child_title}" if child else ""
	else:
		line = f"{cint(q) if q == int(q) else q} cajas ({cint(leaf_qty) if leaf_qty == int(leaf_qty) else leaf_qty} de {leaf})"
		contains = f"Contiene {pack_label} × {child_title}" if child else ""
	return {
		"boxes": q,
		"leaf_sku": leaf,
		"leaf_title": leaf_title,
		"leaf_qty": leaf_qty,
		"factor": factor,
		"child_sku": child,
		"child_qty": (q * flt(pack)) if child else None,
		"line": line,
		"contains": contains,
	}
