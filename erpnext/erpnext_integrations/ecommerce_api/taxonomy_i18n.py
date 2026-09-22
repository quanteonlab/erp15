"""
Item Group + Brand taxonomy i18n: aliases + thumbnail + auto-generate job.

Primary locales stay on Data fields (``custom_alias_es|en|zh``). Extra locales
and the full map live in ``custom_aliases_json``::

    {"es": "Almacén", "en": "Grocery", "zh": "食品杂货", "pt": "Mercearia"}

Run:
  bench --site <site> execute \\
    erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n.ensure_taxonomy_i18n

  bench --site <site> execute \\
    erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n.enqueue_generate_taxonomy_aliases \\
    --kwargs '{"langs":"es,en,zh,pt","only_missing":1}'
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, cstr
from frappe.utils.background_jobs import enqueue

# Canonical name → {es, en, zh}. Used only when the alias is still empty.
ITEM_GROUP_ALIAS_GIFTS: dict[str, dict[str, str]] = {
	"All Item Groups": {
		"es": "Todos los grupos",
		"en": "All item groups",
		"zh": "全部组别",
	},
	"ALMACEN": {"es": "Almacén", "en": "Grocery", "zh": "食品杂货"},
	"BAZAR": {"es": "Bazar", "en": "Housewares", "zh": "日用百货"},
	"BEBIDA": {"es": "Bebida", "en": "Drinks", "zh": "饮料"},
	'BEBIDAS "A"(S/ALCOHOL)': {
		"es": "Bebidas sin alcohol",
		"en": "Non-alcoholic drinks",
		"zh": "软饮料",
	},
	'BEBIDAS "V"(CON ALCOHOL)': {
		"es": "Bebidas con alcohol",
		"en": "Alcoholic drinks",
		"zh": "酒精饮料",
	},
	"Books": {"es": "Libros", "en": "Books", "zh": "图书"},
	"CERVEZA": {"es": "Cerveza", "en": "Beer", "zh": "啤酒"},
	"CHAMPAGNE": {"es": "Champagne", "en": "Champagne", "zh": "香槟"},
	"Consumable": {"es": "Consumible", "en": "Consumable", "zh": "消耗品"},
	"ELECTRODOMESTICO": {"es": "Electrodoméstico", "en": "Appliances", "zh": "家电"},
	"FIAMBRERIA": {"es": "Fiambrería", "en": "Deli", "zh": "熟食"},
	"GALLETAS": {"es": "Galletas", "en": "Cookies / biscuits", "zh": "饼干"},
	"KIOSCO": {"es": "Kiosco", "en": "Kiosk snacks", "zh": "零食小卖"},
	"LACTEOS": {"es": "Lácteos", "en": "Dairy", "zh": "乳制品"},
	"LICOR": {"es": "Licor", "en": "Liquor", "zh": "烈酒"},
	"LIMPIEZA": {"es": "Limpieza", "en": "Cleaning", "zh": "清洁用品"},
	"Manufactured Goods": {
		"es": "Manufacturados",
		"en": "Manufactured goods",
		"zh": "制成品",
	},
	"OFERTA": {"es": "Oferta", "en": "Offers", "zh": "特价"},
	"PAN*BIZOCHUELO*PIONONO": {
		"es": "Pan / bizcochuelo / pionono",
		"en": "Bread & cakes",
		"zh": "面包蛋糕",
	},
	"PERFUMERIA": {"es": "Perfumería", "en": "Personal care", "zh": "个护美妆"},
	"POS Supplies": {"es": "Insumos POS", "en": "POS supplies", "zh": "收银耗材"},
	"PRODUCTOS CONGELADOS": {
		"es": "Productos congelados",
		"en": "Frozen foods",
		"zh": "冷冻食品",
	},
	"PRODUCTOS FRESCOS": {
		"es": "Productos frescos",
		"en": "Fresh foods",
		"zh": "生鲜",
	},
	"Products": {"es": "Productos", "en": "Products", "zh": "产品"},
	"Q": {"es": "Q", "en": "Q", "zh": "Q"},
	"Raw Material": {"es": "Materia prima", "en": "Raw material", "zh": "原材料"},
	"Raw Materials": {"es": "Materias primas", "en": "Raw materials", "zh": "原材料"},
	"Services": {"es": "Servicios", "en": "Services", "zh": "服务"},
	"Stationery": {"es": "Papelería", "en": "Stationery", "zh": "文具"},
	"Sub Assemblies": {"es": "Subensambles", "en": "Sub assemblies", "zh": "半成品"},
	"VINOS": {"es": "Vinos", "en": "Wines", "zh": "葡萄酒"},
	"VODKA": {"es": "Vodka", "en": "Vodka", "zh": "伏特加"},
	"WHISKY": {"es": "Whisky", "en": "Whisky", "zh": "威士忌"},
}

PRIMARY_ALIAS_FIELDS = (
	("es", "custom_alias_es", "Alias (Español)", "Spanish display alias for catalog / POS."),
	("en", "custom_alias_en", "Alias (English)", "English display alias for catalog / POS."),
	("zh", "custom_alias_zh", "Alias (中文)", "Chinese display alias for catalog / POS."),
)

PRIMARY_LANGS = ("es", "en", "zh")
DEFAULT_GENERATE_LANGS = ("es", "en", "zh", "pt", "fr", "it")
JSON_FIELD = "custom_aliases_json"
_LANG_RE = re.compile(r"^[a-z]{2}(-[a-z0-9]+)?$", re.I)


def ensure_taxonomy_i18n() -> dict:
	"""after_migrate: ensure custom fields + gift empty aliases (never overwrite)."""
	ensure_taxonomy_custom_fields()
	gifted = gift_taxonomy_aliases()
	frappe.db.commit()
	return {"ok": True, **gifted}


def ensure_taxonomy_custom_fields() -> None:
	"""Add primary alias fields + JSON aliases + thumbnail on Item Group and Brand."""
	group_fields: list[dict] = []
	insert_after = "item_group_name"
	for i, (_lang, fieldname, label, description) in enumerate(PRIMARY_ALIAS_FIELDS):
		group_fields.append(
			{
				"fieldname": fieldname,
				"fieldtype": "Data",
				"label": label,
				"insert_after": insert_after if i == 0 else PRIMARY_ALIAS_FIELDS[i - 1][1],
				"description": description,
				"in_list_view": 0,
				"translatable": 0,
			}
		)
	group_fields.append(
		{
			"fieldname": JSON_FIELD,
			"fieldtype": "Long Text",
			"label": "Aliases (JSON)",
			"insert_after": "custom_alias_zh",
			"description": 'Extra / full alias map JSON, e.g. {"pt":"Mercearia","fr":"Épicerie"}. '
			"Primary es/en/zh keys sync with the Alias fields above.",
		}
	)
	group_fields.append(
		{
			"fieldname": "custom_thumbnail",
			"fieldtype": "Attach Image",
			"label": "Thumbnail",
			"insert_after": JSON_FIELD,
			"description": "Catalog / chip thumbnail. Falls back to the DocType image if empty.",
		}
	)

	brand_fields: list[dict] = []
	for i, (_lang, fieldname, label, description) in enumerate(PRIMARY_ALIAS_FIELDS):
		brand_fields.append(
			{
				"fieldname": fieldname,
				"fieldtype": "Data",
				"label": label,
				"insert_after": "brand" if i == 0 else PRIMARY_ALIAS_FIELDS[i - 1][1],
				"description": description,
				"in_list_view": 0,
				"translatable": 0,
			}
		)
	brand_fields.append(
		{
			"fieldname": JSON_FIELD,
			"fieldtype": "Long Text",
			"label": "Aliases (JSON)",
			"insert_after": "custom_alias_zh",
			"description": 'Extra / full alias map JSON, e.g. {"pt":"Coca-Cola","fr":"Coca-Cola"}. '
			"Primary es/en/zh keys sync with the Alias fields above.",
		}
	)
	brand_fields.append(
		{
			"fieldname": "custom_thumbnail",
			"fieldtype": "Attach Image",
			"label": "Thumbnail",
			"insert_after": JSON_FIELD,
			"description": "Catalog / chip thumbnail. Falls back to Brand image if empty.",
		}
	)

	create_custom_fields(
		{"Item Group": group_fields, "Brand": brand_fields},
		ignore_validate=True,
	)
	_unhide_standard_image("Item Group")
	_unhide_standard_image("Brand")
	frappe.clear_cache(doctype="Item Group")
	frappe.clear_cache(doctype="Brand")


def _unhide_standard_image(doctype: str) -> None:
	existing = frappe.db.exists(
		"Property Setter",
		{"doc_type": doctype, "field_name": "image", "property": "hidden"},
	)
	if existing:
		frappe.db.set_value("Property Setter", existing, "value", "0", update_modified=False)
		return
	doc = frappe.get_doc(
		{
			"doctype": "Property Setter",
			"doctype_or_field": "DocField",
			"doc_type": doctype,
			"field_name": "image",
			"property": "hidden",
			"property_type": "Check",
			"value": "0",
		}
	)
	doc.insert(ignore_permissions=True)


# ─── alias map helpers ───────────────────────────────────────────────────────


def parse_aliases_json(raw: Any) -> dict[str, str]:
	"""Parse aliases JSON (dict or JSON string) → {lang: label}."""
	if raw is None or raw == "":
		return {}
	if isinstance(raw, dict):
		data = raw
	else:
		text = cstr(raw).strip()
		if not text:
			return {}
		try:
			data = json.loads(text)
		except Exception:
			return {}
	if not isinstance(data, dict):
		return {}
	out: dict[str, str] = {}
	for key, value in data.items():
		lang = _normalize_lang(key)
		label = cstr(value).strip()
		if lang and label:
			out[lang] = label
	return out


def _normalize_lang(lang: str | None) -> str:
	key = cstr(lang or "").strip().lower().replace("_", "-")
	if not key:
		return ""
	# zh-cn → zh
	if key.startswith("zh"):
		return "zh"
	base = key.split("-", 1)[0]
	if _LANG_RE.match(base):
		return base
	return ""


def _parse_lang_list(langs: Any) -> list[str]:
	if langs is None or langs == "":
		return list(DEFAULT_GENERATE_LANGS)
	if isinstance(langs, (list, tuple)):
		raw = langs
	else:
		raw = re.split(r"[,;\s]+", cstr(langs))
	out: list[str] = []
	seen: set[str] = set()
	for item in raw:
		lang = _normalize_lang(item)
		if lang and lang not in seen:
			seen.add(lang)
			out.append(lang)
	return out or list(DEFAULT_GENERATE_LANGS)


def collect_aliases(row: dict) -> dict[str, str]:
	"""Merge primary Data fields + JSON map (Data fields win when non-empty)."""
	merged = parse_aliases_json(row.get(JSON_FIELD))
	for lang, field, _label, _desc in PRIMARY_ALIAS_FIELDS:
		value = cstr(row.get(field) or "").strip()
		if value:
			merged[lang] = value
	return merged


def aliases_to_patch(aliases: dict[str, str], *, only_missing_against: dict | None = None) -> dict[str, str]:
	"""Build DB patch for primary fields + JSON. Optionally skip keys already set."""
	existing = collect_aliases(only_missing_against or {})
	merged = dict(existing)
	for lang, label in (aliases or {}).items():
		key = _normalize_lang(lang)
		value = cstr(label).strip()
		if not key or not value:
			continue
		if only_missing_against is not None and cstr(existing.get(key) or "").strip():
			continue
		merged[key] = value

	patch: dict[str, str] = {}
	for lang, field, _label, _desc in PRIMARY_ALIAS_FIELDS:
		value = merged.get(lang) or ""
		current = cstr((only_missing_against or {}).get(field) or "").strip()
		if only_missing_against is not None and current:
			continue
		if value and value != current:
			patch[field] = value

	# Always rewrite JSON when we have a merged map change (extras live only there).
	json_text = json.dumps(merged, ensure_ascii=False, sort_keys=True) if merged else ""
	current_json = cstr((only_missing_against or {}).get(JSON_FIELD) or "").strip()
	if only_missing_against is None or json_text != current_json:
		if json_text or current_json:
			patch[JSON_FIELD] = json_text
	return patch


def gift_taxonomy_aliases() -> dict:
	"""Fill empty aliases for existing groups/brands. Never overwrites edits."""
	return {
		"item_groups_updated": _gift_item_group_aliases(),
		"brands_updated": _gift_brand_aliases(),
	}


def _gift_item_group_aliases() -> int:
	if not frappe.db.has_column("Item Group", "custom_alias_es"):
		return 0
	fields = [
		"name",
		"item_group_name",
		"custom_alias_es",
		"custom_alias_en",
		"custom_alias_zh",
	]
	if frappe.db.has_column("Item Group", JSON_FIELD):
		fields.append(JSON_FIELD)
	updated = 0
	rows = frappe.get_all("Item Group", fields=fields, ignore_permissions=True)
	for row in rows:
		gifts = ITEM_GROUP_ALIAS_GIFTS.get(row.name) or ITEM_GROUP_ALIAS_GIFTS.get(
			(row.item_group_name or "").strip()
		)
		fallback = (row.item_group_name or row.name or "").strip()
		proposed = dict(gifts or {})
		for lang in PRIMARY_LANGS:
			proposed.setdefault(lang, fallback)
		patch = aliases_to_patch(proposed, only_missing_against=row)
		if not patch:
			continue
		frappe.db.set_value("Item Group", row.name, patch, update_modified=False)
		updated += 1
	return updated


def _gift_brand_aliases() -> int:
	if not frappe.db.has_column("Brand", "custom_alias_es"):
		return 0
	fields = ["name", "brand", "custom_alias_es", "custom_alias_en", "custom_alias_zh"]
	if frappe.db.has_column("Brand", JSON_FIELD):
		fields.append(JSON_FIELD)
	updated = 0
	rows = frappe.get_all("Brand", fields=fields, ignore_permissions=True)
	for row in rows:
		fallback = (row.brand or row.name or "").strip()
		if not fallback:
			continue
		proposed = {lang: fallback for lang in PRIMARY_LANGS}
		patch = aliases_to_patch(proposed, only_missing_against=row)
		if not patch:
			continue
		frappe.db.set_value("Brand", row.name, patch, update_modified=False)
		updated += 1
	return updated


def _resolve_thumbnail(image: str | None, custom_thumbnail: str | None) -> str | None:
	thumb = (custom_thumbnail or "").strip() or (image or "").strip()
	return thumb or None


def _row_label(aliases: dict[str, str], row: dict, lang: str) -> str:
	lang = _normalize_lang(lang) or "es"
	return (
		cstr(aliases.get(lang) or "").strip()
		or cstr(aliases.get("es") or "").strip()
		or cstr(aliases.get("en") or "").strip()
		or (row.get("item_group_name") or row.get("brand") or row.get("name") or "").strip()
	)


@frappe.whitelist(allow_guest=True)
def get_catalog_taxonomy(lang: str | None = None) -> dict:
	"""
	Catalog / POS labels + thumbnails for Item Groups and Brands.
	Each entry includes ``aliases`` (full JSON map) plus primary alias_* keys.
	"""
	lang = _normalize_lang(lang or frappe.form_dict.get("lang") or "es") or "es"
	groups: dict[str, dict] = {}
	brands: dict[str, dict] = {}

	group_fields = ["name", "item_group_name", "image", "parent_item_group", "is_group"]
	brand_fields = ["name", "brand", "image"]
	extra = ["custom_alias_es", "custom_alias_en", "custom_alias_zh", JSON_FIELD, "custom_thumbnail"]
	for f in extra:
		if frappe.db.has_column("Item Group", f):
			group_fields.append(f)
		if frappe.db.has_column("Brand", f):
			brand_fields.append(f)

	for row in frappe.get_all("Item Group", fields=group_fields, ignore_permissions=True):
		if row.name == "All Item Groups":
			continue
		aliases = collect_aliases(row)
		parent = cstr(row.get("parent_item_group") or "").strip()
		path_parent = "" if parent in ("", "All Item Groups") else parent
		leaf = row.name
		path = f"{path_parent}>{leaf}" if path_parent else leaf
		groups[row.name] = {
			"name": row.name,
			"label": _row_label(aliases, row, lang),
			"aliases": aliases,
			"alias_es": aliases.get("es"),
			"alias_en": aliases.get("en"),
			"alias_zh": aliases.get("zh"),
			"image": _resolve_thumbnail(row.get("image"), row.get("custom_thumbnail")),
			"parent_item_group": path_parent,
			"is_group": cint(row.get("is_group")),
			"path": path,
		}

	for row in frappe.get_all("Brand", fields=brand_fields, ignore_permissions=True):
		aliases = collect_aliases(row)
		brands[row.name] = {
			"name": row.name,
			"label": _row_label(aliases, row, lang),
			"aliases": aliases,
			"alias_es": aliases.get("es"),
			"alias_en": aliases.get("en"),
			"alias_zh": aliases.get("zh"),
			"image": _resolve_thumbnail(row.get("image"), row.get("custom_thumbnail")),
		}

	return {"lang": lang, "groups": groups, "brands": brands}


# ─── auto-generate job ───────────────────────────────────────────────────────


def _source_text_for_row(doctype: str, row: dict) -> str:
	if doctype == "Item Group":
		return (row.get("item_group_name") or row.get("name") or "").strip()
	return (row.get("brand") or row.get("name") or "").strip()


def _gift_map_for_row(doctype: str, row: dict) -> dict[str, str]:
	if doctype != "Item Group":
		return {}
	return (
		ITEM_GROUP_ALIAS_GIFTS.get(row.name)
		or ITEM_GROUP_ALIAS_GIFTS.get((row.item_group_name or "").strip())
		or {}
	)


def _auto_translate(text: str, source_lang: str, target_lang: str) -> str | None:
	"""Best-effort free translation (MyMemory). Failures return None."""
	text = cstr(text).strip()
	src = _normalize_lang(source_lang) or "es"
	tgt = _normalize_lang(target_lang)
	if not text or not tgt or src == tgt:
		return text if text else None
	# Brand-like tokens (mostly Latin proper nouns) — keep as-is for CJK only when short.
	if tgt != "zh" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'\-]{0,40}", text):
		# Still try translate for common words; MyMemory handles brands poorly → keep
		pass
	try:
		url = (
			"https://api.mymemory.translated.net/get"
			f"?q={quote(text[:450])}&langpair={quote(src)}|{quote(tgt)}"
		)
		req = Request(url, headers={"User-Agent": "IntegratedCommerce/1.0"})
		with urlopen(req, timeout=8) as resp:
			payload = json.loads(resp.read().decode("utf-8", errors="replace"))
		translated = cstr((payload.get("responseData") or {}).get("translatedText") or "").strip()
		if not translated or translated.lower() == text.lower():
			return None
		# MyMemory sometimes returns "INVALID SOURCE LANGUAGE ..." etc.
		if "INVALID" in translated.upper() and "LANGUAGE" in translated.upper():
			return None
		return translated
	except Exception:
		return None


def _propose_aliases_for_row(
	doctype: str,
	row: dict,
	langs: list[str],
	*,
	use_online_translate: bool = True,
) -> dict[str, str]:
	existing = collect_aliases(row)
	source = _source_text_for_row(doctype, row)
	gifts = _gift_map_for_row(doctype, row)
	source_lang = "es" if gifts or doctype == "Item Group" else "en"
	source_label = (
		cstr(existing.get(source_lang) or "").strip()
		or cstr(gifts.get(source_lang) or "").strip()
		or source
	)
	proposed: dict[str, str] = {}
	for lang in langs:
		if cstr(existing.get(lang) or "").strip():
			continue
		if gifts.get(lang):
			proposed[lang] = gifts[lang]
			continue
		if lang in PRIMARY_LANGS and source:
			# Brands: seed primary langs with brand name when no gift.
			if doctype == "Brand":
				proposed[lang] = source
				continue
		if use_online_translate and source_label:
			translated = _auto_translate(source_label, source_lang, lang)
			if translated:
				proposed[lang] = translated
				continue
		if source:
			proposed[lang] = source
	return proposed


def _load_rows(doctype: str, names: list[str] | None, limit: int | None) -> list[dict]:
	fields = ["name"]
	if doctype == "Item Group":
		fields.append("item_group_name")
	else:
		fields.append("brand")
	for _lang, field, _l, _d in PRIMARY_ALIAS_FIELDS:
		if frappe.db.has_column(doctype, field):
			fields.append(field)
	if frappe.db.has_column(doctype, JSON_FIELD):
		fields.append(JSON_FIELD)

	filters: dict[str, Any] = {}
	if names:
		filters["name"] = ["in", names]
	elif doctype == "Item Group":
		filters["name"] = ["!=", "All Item Groups"]

	kwargs: dict[str, Any] = {
		"fields": fields,
		"filters": filters or None,
		"order_by": "name asc",
		"ignore_permissions": True,
	}
	if limit and limit > 0:
		kwargs["limit_page_length"] = limit
	return frappe.get_all(doctype, **kwargs)


@frappe.whitelist()
def enqueue_generate_taxonomy_aliases(
	doctype: str | None = None,
	names: str | list | None = None,
	langs: str | list | None = None,
	only_missing: int | str | None = 1,
	limit: int | str | None = None,
	use_online_translate: int | str | None = 1,
	now: int | str | None = 0,
) -> dict:
	"""
	Queue (or run) alias auto-generation for Item Group and/or Brand.

	``langs`` — comma list, default ``es,en,zh,pt,fr,it``.
	``only_missing`` — skip locales that already have a value (default 1).
	``use_online_translate`` — call MyMemory for missing locales (default 1).
	``now`` — run inline instead of RQ (default 0).
	"""
	targets = _normalize_doctypes(doctype)
	lang_list = _parse_lang_list(langs)
	name_list = _parse_names(names)
	payload = {
		"doctypes": targets,
		"names": name_list,
		"langs": lang_list,
		"only_missing": cint(only_missing if only_missing is not None else 1),
		"limit": cint(limit) if limit not in (None, "", 0, "0") else None,
		"use_online_translate": cint(use_online_translate if use_online_translate is not None else 1),
	}
	if cint(now):
		result = generate_taxonomy_aliases(**payload)
		frappe.db.commit()
		return {"ok": True, "mode": "inline", **result}

	enqueue(
		"erpnext.erpnext_integrations.ecommerce_api.taxonomy_i18n.generate_taxonomy_aliases",
		queue="long",
		timeout=1800,
		is_async=True,
		job_name="taxonomy-alias-generate",
		**payload,
	)
	return {"ok": True, "mode": "queued", **payload}


def _normalize_doctypes(doctype: str | None) -> list[str]:
	raw = cstr(doctype or "").strip()
	if not raw or raw.lower() in ("all", "*", "both"):
		return ["Item Group", "Brand"]
	key = raw.lower()
	if key in ("item group", "item_group", "group", "groups"):
		return ["Item Group"]
	if key in ("brand", "brands", "marca", "marcas"):
		return ["Brand"]
	frappe.throw(f"Unsupported doctype for alias generation: {doctype}")


def _parse_names(names: Any) -> list[str] | None:
	if names is None or names == "":
		return None
	if isinstance(names, (list, tuple)):
		out = [cstr(n).strip() for n in names if cstr(n).strip()]
		return out or None
	text = cstr(names).strip()
	if text.startswith("["):
		try:
			parsed = json.loads(text)
			if isinstance(parsed, list):
				out = [cstr(n).strip() for n in parsed if cstr(n).strip()]
				return out or None
		except Exception:
			pass
	out = [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()]
	return out or None


def generate_taxonomy_aliases(
	doctypes: list[str] | None = None,
	names: list[str] | None = None,
	langs: list[str] | None = None,
	only_missing: int = 1,
	limit: int | None = None,
	use_online_translate: int = 1,
) -> dict:
	"""Background worker: fill taxonomy aliases (JSON + primary fields)."""
	ensure_taxonomy_custom_fields()
	targets = doctypes or ["Item Group", "Brand"]
	lang_list = langs or list(DEFAULT_GENERATE_LANGS)
	only_miss = cint(only_missing)
	online = cint(use_online_translate)

	summary: dict[str, Any] = {"updated": 0, "skipped": 0, "by_doctype": {}}
	for dt in targets:
		rows = _load_rows(dt, names, limit)
		updated = 0
		skipped = 0
		for row in rows:
			existing = collect_aliases(row)
			if only_miss and all(cstr(existing.get(lang) or "").strip() for lang in lang_list):
				skipped += 1
				continue
			proposed = _propose_aliases_for_row(
				dt,
				row,
				lang_list,
				use_online_translate=bool(online),
			)
			if not proposed:
				skipped += 1
				continue
			patch = aliases_to_patch(
				proposed,
				only_missing_against=row if only_miss else None,
			)
			if not patch:
				skipped += 1
				continue
			frappe.db.set_value(dt, row.name, patch, update_modified=False)
			updated += 1
		summary["by_doctype"][dt] = {"updated": updated, "skipped": skipped, "scanned": len(rows)}
		summary["updated"] += updated
		summary["skipped"] += skipped

	frappe.db.commit()
	return summary


def weekly_generate_missing_taxonomy_aliases() -> dict:
	"""Scheduler entry: fill missing primary + pt/fr/it aliases without overwriting."""
	return generate_taxonomy_aliases(
		doctypes=["Item Group", "Brand"],
		langs=list(DEFAULT_GENERATE_LANGS),
		only_missing=1,
		use_online_translate=1,
	)


@frappe.whitelist()
def set_taxonomy_aliases(
	doctype: str,
	name: str,
	aliases: str | dict | None = None,
	overwrite: int | str | None = 0,
) -> dict:
	"""
	Set / merge aliases for one Item Group or Brand.
	``aliases`` is a JSON object: ``{"pt":"…","fr":"…"}``.
	"""
	dt = _normalize_doctypes(doctype)[0]
	docname = cstr(name).strip()
	if not docname or not frappe.db.exists(dt, docname):
		frappe.throw(f"{dt} not found: {name}")
	ensure_taxonomy_custom_fields()
	incoming = parse_aliases_json(aliases)
	if not incoming:
		frappe.throw("aliases must be a non-empty JSON object of {lang: label}")

	fields = ["name", "custom_alias_es", "custom_alias_en", "custom_alias_zh"]
	if dt == "Item Group":
		fields.append("item_group_name")
	else:
		fields.append("brand")
	if frappe.db.has_column(dt, JSON_FIELD):
		fields.append(JSON_FIELD)
	row = frappe.get_all(dt, fields=fields, filters={"name": docname}, limit_page_length=1, ignore_permissions=True)
	row = row[0] if row else {"name": docname}
	patch = aliases_to_patch(
		incoming,
		only_missing_against=None if cint(overwrite) else row,
	)
	if patch:
		frappe.db.set_value(dt, docname, patch, update_modified=True)
		frappe.db.commit()
	merged = collect_aliases({**row, **patch})
	return {"ok": True, "doctype": dt, "name": docname, "aliases": merged}
