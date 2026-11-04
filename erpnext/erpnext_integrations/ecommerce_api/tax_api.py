"""i059 — per-product taxes (IVA) for Tablas → Productos / Rentabilidad / Cuentas.

Source of truth is the native ERPNext pair:
  * ``Item Tax Template`` — the tax catalog ("IVA 21%", "Exento", ...). One account + rate per detail row.
  * ``Item Tax`` — the child table on Item that points an item at one template.

The UI shows templates as "tag" chips. ERPNext treats several ``Item Tax`` rows as *alternatives* (picked by the
customer's tax category), not as a sum, so picking more than one chip builds (or reuses) a single **compound**
template whose detail rows are the picked taxes (accounts must differ). The price stored on Item Price stays the
**final** price; ``net = final / (1 + rate)`` is computed backwards, so no totals change.

Reads use ``ignore_permissions=True`` (API-key user). Writes need an acting staff user with ``tables.products``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, today

from erpnext.erpnext_integrations.ecommerce_api.company_context import resolve_company
from erpnext.erpnext_integrations.ecommerce_api.coupons_api import _require_staff

TITLE_MAX = 60
COMPOUND_SEP = " + "

# Argentina catalog seeded by ``ensure_tax_catalog``. ``kind`` drives how the UI and (later) AFIP treat the row.
AR_CATALOG: tuple[dict, ...] = (
	{"title": "IVA 21%", "rate": 21.0, "kind": "iva"},
	{"title": "IVA 10.5%", "rate": 10.5, "kind": "iva"},
	{"title": "IVA 27%", "rate": 27.0, "kind": "iva"},
	{"title": "IVA 5%", "rate": 5.0, "kind": "iva"},
	{"title": "IVA 2.5%", "rate": 2.5, "kind": "iva"},
	{"title": "IVA 0%", "rate": 0.0, "kind": "iva"},
	{"title": "Exento", "rate": 0.0, "kind": "exento"},
	{"title": "No gravado", "rate": 0.0, "kind": "no_gravado"},
)
CATALOGS = {"AR": AR_CATALOG}
DEFAULT_COUNTRY = "AR"
_CATALOG_KIND = {c["title"]: c["kind"] for c in AR_CATALOG}


def _company(company=None) -> str:
	co = resolve_company(company)
	if not co:
		frappe.throw(_("Company is required"), frappe.ValidationError)
	return co


def _abbr(company: str) -> str:
	return cstr(frappe.get_cached_value("Company", company, "abbr"))


def _tpl_name(company: str, title: str) -> str:
	return f"{title} - {_abbr(company)}"


def _clean_title(raw) -> str:
	return " ".join(cstr(raw).split())[:TITLE_MAX]


def _clean_rate(raw) -> float:
	if raw is None or cstr(raw).strip().lower() in ("", "null", "undefined", "none"):
		frappe.throw(_("Tax rate is required"), frappe.ValidationError)
	try:
		rate = flt(raw)
	except Exception:
		rate = -1
	if rate < 0 or rate > 100:
		frappe.throw(_("Tax rate must be between 0 and 100"), frappe.ValidationError)
	return round(rate, 4)


# ── accounts ───────────────────────────────────────────────────────────────────────────────────────────────────
def _duties_parent(company: str) -> str | None:
	return frappe.db.get_value(
		"Account", {"company": company, "account_name": "Duties and Taxes", "is_group": 1}, "name"
	)


def _create_tax_account(company: str, account_name: str) -> str:
	parent = _duties_parent(company)
	if not parent:
		frappe.throw(
			_("Company {0} has no 'Duties and Taxes' account group; create a tax account first").format(company)
		)
	name = frappe.db.get_value("Account", {"company": company, "account_name": account_name}, "name")
	if name:
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": parent,
			"company": company,
			"account_type": "Tax",
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def vat_account(company: str) -> str:
	"""The shared IVA account: an existing non-group Tax account named VAT/IVA, else any Tax account, else a new 'IVA'."""
	rows = frappe.db.sql(
		"""SELECT name FROM `tabAccount`
		   WHERE company = %s AND account_type = 'Tax' AND is_group = 0 AND disabled = 0
		   ORDER BY (name LIKE 'VAT%%' OR name LIKE 'IVA%%') DESC, name ASC LIMIT 1""",
		(company,),
	)
	return rows[0][0] if rows else _create_tax_account(company, "IVA")


# ── catalog ────────────────────────────────────────────────────────────────────────────────────────────────────
def _upsert_template(company: str, title: str, rate: float, account: str) -> str:
	name = _tpl_name(company, title)
	if frappe.db.exists("Item Tax Template", name):
		frappe.flags.ignore_permissions = True
		try:
			doc = frappe.get_doc("Item Tax Template", name)
		finally:
			frappe.flags.ignore_permissions = False
		if len(doc.taxes) == 1 and flt(doc.taxes[0].tax_rate) == flt(rate) and doc.taxes[0].tax_type == account and not doc.disabled:
			return name
		if len(doc.taxes) == 1:
			doc.taxes[0].tax_rate = rate
			doc.taxes[0].tax_type = account
			doc.disabled = 0
			doc.save(ignore_permissions=True)
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Item Tax Template",
			"title": title,
			"company": company,
			"taxes": [{"tax_type": account, "tax_rate": rate}],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def seed_tax_catalog(company: str, country: str = DEFAULT_COUNTRY) -> list[str]:
	"""Idempotent: creates only missing templates (existing rates are never overwritten)."""
	catalog = CATALOGS.get(cstr(country).upper() or DEFAULT_COUNTRY) or AR_CATALOG
	account = vat_account(company)
	made = []
	for entry in catalog:
		name = _tpl_name(company, entry["title"])
		if not frappe.db.exists("Item Tax Template", name):
			made.append(_upsert_template(company, entry["title"], entry["rate"], account))
	return made


def _template_rows(company: str, include_compound: bool = False) -> list[dict]:
	rows = frappe.db.sql(
		"""SELECT t.name, t.title, t.disabled,
		          d.tax_type AS account, d.tax_rate AS rate
		   FROM `tabItem Tax Template` t
		   LEFT JOIN `tabItem Tax Template Detail` d ON d.parent = t.name
		   WHERE t.company = %s AND t.disabled = 0
		   ORDER BY t.title ASC, d.idx ASC""",
		(company,),
		as_dict=True,
	)
	by: dict[str, dict] = {}
	for r in rows:
		cur = by.setdefault(
			r.name,
			{"name": r.name, "title": r.title, "rate": 0.0, "accounts": [], "compound": False},
		)
		if r.account:
			cur["accounts"].append(r.account)
			cur["rate"] = round(cur["rate"] + flt(r.rate), 4)
	out = []
	for cur in by.values():
		cur["compound"] = len(cur["accounts"]) > 1
		cur["kind"] = _CATALOG_KIND.get(cur["title"], "other")
		if cur["compound"] and not include_compound:
			continue
		out.append(cur)
	return out


@frappe.whitelist(allow_guest=True)
def list_taxes(company=None, include_compound=0):
	"""Atomic taxes for the chip picker. ``catalog_ready`` is False until ``ensure_tax_catalog`` ran."""
	co = _company(company)
	taxes = _template_rows(co, bool(cint(include_compound)))
	order = {c["title"]: i for i, c in enumerate(AR_CATALOG)}
	taxes.sort(key=lambda t: (order.get(t["title"], 99), t["title"]))
	return {
		"company": co,
		"taxes": taxes,
		"catalog_ready": any(t["title"] == AR_CATALOG[0]["title"] for t in taxes),
		"default_country": DEFAULT_COUNTRY,
	}


@frappe.whitelist()
def ensure_tax_catalog(country=None, company=None):
	_require_staff("tables.products")
	co = _company(company)
	made = seed_tax_catalog(co, cstr(country) or DEFAULT_COUNTRY)
	frappe.db.commit()
	return {"ok": True, "created": made, **list_taxes(co)}


def ensure_tax_catalog_all_companies() -> None:
	"""after_migrate hook: seed the default catalog on every company, never failing the migrate."""
	for co in frappe.get_all("Company", pluck="name", ignore_permissions=True):
		try:
			seed_tax_catalog(co)
		except Exception:
			frappe.log_error(title=f"tax catalog seed {co}")
	frappe.db.commit()


@frappe.whitelist()
def save_tax(title=None, rate=None, kind=None, company=None):
	"""Create a tax (idempotent on title). ``kind='other'`` gets its own account (e.g. an internal tax) so it can
	be combined with IVA; everything else shares the IVA account. An existing tax with that title is returned
	unchanged unless the rate differs, in which case its rate is updated."""
	_require_staff("tables.products")
	co = _company(company)
	clean = _clean_title(title)
	if not clean:
		frappe.throw(_("Tax name is required"), frappe.ValidationError)
	if COMPOUND_SEP.strip() in clean:
		frappe.throw(_("Tax name cannot contain '+'"), frappe.ValidationError)
	r = _clean_rate(rate)
	account = _create_tax_account(co, clean) if cstr(kind).lower() == "other" else vat_account(co)
	name = _upsert_template(co, clean, r, account)
	frappe.db.commit()
	return {"ok": True, "name": name, "title": clean, "rate": r, "account": account}


# ── per-item assignment ────────────────────────────────────────────────────────────────────────────────────────
def _resolve_templates(company: str, picked) -> list[str]:
	"""Accept template names or titles (or a single comma string) → existing atomic template names, in order."""
	if isinstance(picked, str):
		import json

		try:
			picked = json.loads(picked)
		except Exception:
			picked = [p for p in picked.split(",")]
	names: list[str] = []
	for raw in picked or []:
		s = cstr(raw).strip()
		if not s or s.lower() in ("null", "undefined", "none"):
			continue
		hit = s if frappe.db.exists("Item Tax Template", {"name": s, "company": company}) else _tpl_name(company, s)
		if not frappe.db.exists("Item Tax Template", hit):
			frappe.throw(_("Unknown tax: {0}").format(s), frappe.ValidationError)
		if hit not in names:
			names.append(hit)
	return names


def _compound_template(company: str, names: list[str]) -> str:
	if len(names) == 1:
		return names[0]
	titles, rows, seen = [], [], set()
	for n in names:
		frappe.flags.ignore_permissions = True
		try:
			doc = frappe.get_doc("Item Tax Template", n)
		finally:
			frappe.flags.ignore_permissions = False
		titles.append(doc.title)
		for d in doc.taxes:
			if d.tax_type in seen:
				frappe.throw(
					_("Taxes {0} use the same account; pick one of them or create the second as a separate tax").format(
						COMPOUND_SEP.join(titles)
					),
					frappe.ValidationError,
				)
			seen.add(d.tax_type)
			rows.append({"tax_type": d.tax_type, "tax_rate": d.tax_rate})
	title = COMPOUND_SEP.join(titles)[:140]
	name = _tpl_name(company, title)
	if frappe.db.exists("Item Tax Template", name):
		return name
	doc = frappe.get_doc({"doctype": "Item Tax Template", "title": title, "company": company, "taxes": rows})
	doc.insert(ignore_permissions=True)
	return doc.name


def _apply_item_taxes(item_code: str, company: str, names: list[str]) -> str | None:
	"""Replace the item's base tax (the ``Item Tax`` row without a tax category). Category-specific rows stay."""
	template = _compound_template(company, names) if names else None
	frappe.flags.ignore_permissions = True
	try:
		item = frappe.get_doc("Item", item_code)
	finally:
		frappe.flags.ignore_permissions = False
	keep = [r for r in item.get("taxes") or [] if cstr(r.tax_category)]
	item.set("taxes", keep)
	if template:
		item.append("taxes", {"item_tax_template": template})
	item.save(ignore_permissions=True)
	return template


def apply_item_taxes(item_code: str, picked, company=None) -> str | None:
	"""Python entry used by ``product_manager`` (no commit — caller commits)."""
	co = _company(company)
	return _apply_item_taxes(item_code, co, _resolve_templates(co, picked))


@frappe.whitelist()
def set_item_taxes(item_code=None, taxes=None, company=None):
	_require_staff("tables.products")
	code = cstr(item_code).strip()
	if not code or not frappe.db.exists("Item", code):
		frappe.throw(_("Item not found"), frappe.DoesNotExistError)
	co = _company(company)
	template = _apply_item_taxes(code, co, _resolve_templates(co, taxes))
	frappe.db.commit()
	return {"ok": True, "item_code": code, "template": template, **item_tax_map([code], co).get(code, {})}


@frappe.whitelist()
def bulk_set_item_taxes(item_codes=None, taxes=None, mode="replace", company=None):
	"""mode: ``replace`` (default) | ``add`` (keep current + picked) | ``clear`` (no tax). Per-item failures are
	reported, the rest still apply."""
	_require_staff("tables.products")
	import json

	if isinstance(item_codes, str):
		try:
			item_codes = json.loads(item_codes)
		except Exception:
			item_codes = [c for c in item_codes.split(",")]
	codes = [c for c in (cstr(c).strip() for c in item_codes or []) if c]
	if not codes:
		frappe.throw(_("Select at least one product"), frappe.ValidationError)
	if len(codes) > 2000:
		frappe.throw(_("Too many products in one call"), frappe.ValidationError)
	co = _company(company)
	mode = cstr(mode).lower() or "replace"
	if mode not in ("replace", "add", "clear"):
		frappe.throw(_("Invalid mode"), frappe.ValidationError)
	picked = [] if mode == "clear" else _resolve_templates(co, taxes)
	if mode != "clear" and not picked:
		frappe.throw(_("Pick at least one tax"), frappe.ValidationError)
	current = item_tax_map(codes, co) if mode == "add" else {}
	done, failed = [], []
	for code in codes:
		try:
			if not frappe.db.exists("Item", code):
				raise frappe.DoesNotExistError(_("Item not found"))
			names = picked
			if mode == "add":
				have = _split_names((current.get(code) or {}).get("tax_names") or [], co)
				names = have + [n for n in picked if n not in have]
			_apply_item_taxes(code, co, names)
			done.append(code)
		except Exception as e:
			failed.append({"item_code": code, "error": cstr(e)[:200]})
	frappe.db.commit()
	return {"ok": not failed, "updated": len(done), "failed": failed}


def _split_names(titles: list[str], company: str) -> list[str]:
	return [_tpl_name(company, t) for t in titles if frappe.db.exists("Item Tax Template", _tpl_name(company, t))]


# ── read helpers ───────────────────────────────────────────────────────────────────────────────────────────────
def item_tax_map(item_codes, company=None) -> dict[str, dict]:
	"""``{item_code: {tax_rate, tax_names[], tax_template}}`` for items that have a base (category-less) tax.

	Items with no tax are absent (callers treat that as "unknown", not "0%"). Never raises: a failure here must
	not break the Productos table.
	"""
	codes = [c for c in {cstr(c) for c in item_codes or []} if c]
	if not codes:
		return {}
	try:
		co = _company(company)
		ph = ", ".join(["%s"] * len(codes))
		links = frappe.db.sql(
			f"""SELECT it.parent AS item_code, it.item_tax_template AS template, it.valid_from
			    FROM `tabItem Tax` it
			    WHERE it.parenttype = 'Item' AND it.parent IN ({ph})
			      AND IFNULL(it.tax_category, '') = ''
			      AND (it.valid_from IS NULL OR it.valid_from <= %s)
			    ORDER BY it.parent, it.valid_from ASC, it.idx ASC""",
			tuple(codes) + (today(),),
			as_dict=True,
		)
		chosen: dict[str, str] = {}
		for r in links:  # ascending valid_from → the last one wins
			chosen[r.item_code] = r.template
		if not chosen:
			return {}
		tpls = {t["name"]: t for t in _template_rows(co, include_compound=True)}
		out: dict[str, dict] = {}
		for code, tpl in chosen.items():
			t = tpls.get(tpl)
			if not t:
				continue
			out[code] = {
				"tax_rate": t["rate"],
				"tax_names": t["title"].split(COMPOUND_SEP) if t["compound"] else [t["title"]],
				"tax_template": t["name"],
			}
		return out
	except Exception:
		frappe.log_error(title="item_tax_map")
		return {}


_LINE_TABLES = {
	"Sales Order": "Sales Order Item",
	"Sales Invoice": "Sales Invoice Item",
	"Purchase Order": "Purchase Order Item",
	"Purchase Invoice": "Purchase Invoice Item",
}


def implied_doc_tax(doctype: str, names, company=None) -> dict[str, dict]:
	"""Tax contained in each document's total → ``{name: {tax, total, known}}``.

	* A document with real tax rows (``total_taxes_and_charges`` ≠ 0) reports ERPNext's own figure.
	* Otherwise tax is derived backwards from each line: ``amount × r / (100 + r)`` using the item's rate.
	  ``known`` is False when no line has a tax assigned (so the UI can show "—" instead of a misleading 0).
	* BLACK sales (``sale_mode:BLACK`` in remarks) carry no tax.
	"""
	line_table = _LINE_TABLES.get(doctype)
	docs = [n for n in {cstr(n) for n in names or []} if n]
	if not line_table or not docs:
		return {}
	has_remarks = frappe.db.has_column(doctype, "remarks")
	ph = ", ".join(["%s"] * len(docs))
	heads = {
		h.name: h
		for h in frappe.db.sql(
			f"""SELECT name, grand_total, IFNULL(total_taxes_and_charges, 0) AS taxes
			    {", IFNULL(remarks, '') AS remarks" if has_remarks else ", '' AS remarks"}
			    FROM `tab{doctype}` WHERE name IN ({ph})""",
			tuple(docs),
			as_dict=True,
		)
	}
	lines = frappe.db.sql(
		f"SELECT parent, item_code, IFNULL(amount, 0) AS amount FROM `tab{line_table}` WHERE parent IN ({ph})",
		tuple(docs),
		as_dict=True,
	)
	rates = item_tax_map([ln.item_code for ln in lines], company)
	acc: dict[str, dict] = {}
	for ln in lines:
		cur = acc.setdefault(ln.parent, {"tax": 0.0, "known": False})
		info = rates.get(ln.item_code)
		if info is None:
			continue
		cur["known"] = True
		r = flt(info["tax_rate"])
		if r > 0:
			cur["tax"] += flt(ln.amount) * r / (100.0 + r)
	out: dict[str, dict] = {}
	for name, head in heads.items():
		total = flt(head.grand_total)
		if "sale_mode:BLACK" in cstr(head.remarks):
			out[name] = {"tax": 0.0, "total": total, "known": True}
		elif flt(head.taxes):
			out[name] = {"tax": flt(head.taxes), "total": total, "known": True}
		else:
			cur = acc.get(name) or {"tax": 0.0, "known": False}
			out[name] = {"tax": round(cur["tax"], 2), "total": total, "known": cur["known"]}
	return out
