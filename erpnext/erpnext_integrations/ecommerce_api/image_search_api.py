"""Public async image-search API (pollable results URL for Swagger / integrations)."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, get_url


CACHE_PREFIX = "api_image_search:"
CACHE_TTL_SEC = 60 * 60  # 1 hour


def _cache_key(job_id: str) -> str:
	return f"{CACHE_PREFIX}{job_id}"


def _read_job(job_id: str) -> dict | None:
	raw = frappe.cache().get_value(_cache_key(job_id))
	if raw is None:
		return None
	if isinstance(raw, (bytes, bytearray)):
		raw = raw.decode("utf-8")
	if isinstance(raw, str):
		try:
			return json.loads(raw)
		except Exception:
			return None
	if isinstance(raw, dict):
		return raw
	return None


def _write_job(job_id: str, payload: dict) -> None:
	frappe.cache().set_value(_cache_key(job_id), json.dumps(payload, default=str), expires_in_sec=CACHE_TTL_SEC)


def _results_method_path() -> str:
	return "erpnext.erpnext_integrations.ecommerce_api.api.get_image_search_results"


def _results_url(job_id: str) -> str:
	base = get_url().rstrip("/")
	return f"{base}/api/method/{_results_method_path()}?job_id={job_id}"


def start_image_search(
	query: str,
	limit: int = 9,
	store_in_erp: int | str = 0,
	item_code: str | None = None,
	set_item_image: int | str = 0,
) -> dict:
	"""
	Enqueue an image search. Poll ``results_url`` until status is completed/failed.

	Optional:
	  - store_in_erp: when 1, download found images into ERP File records
	    (and, if item_code is set, also create Product Image Candidate rows)
	  - set_item_image: when 1 with store_in_erp + item_code, set Item.image from top candidate
	"""
	q = (query or "").strip()
	if not q:
		frappe.throw(_("query is required"))

	limit = max(1, min(24, cint(limit) or 9))
	store = cint(store_in_erp)
	apply_primary = cint(set_item_image)
	sku = (item_code or "").strip() or None

	if sku and not frappe.db.exists("Item", sku):
		frappe.throw(_("Item {0} not found").format(sku))
	if apply_primary and not (store and sku):
		frappe.throw(_("set_item_image requires store_in_erp=1 and item_code"))

	job_id = frappe.generate_hash(length=16)
	payload = {
		"job_id": job_id,
		"status": "pending",
		"query": q,
		"limit": limit,
		"store_in_erp": bool(store),
		"item_code": sku,
		"set_item_image": bool(apply_primary),
		"images": [],
		"stored": [],
		"error": None,
	}
	_write_job(job_id, payload)

	frappe.enqueue(
		"erpnext.erpnext_integrations.ecommerce_api.image_search_api.run_image_search_job",
		queue="default",
		timeout=180,
		is_async=True,
		job_id=job_id,
		query=q,
		limit=limit,
		store_in_erp=store,
		item_code=sku,
		set_item_image=apply_primary,
	)

	return {
		"ok": True,
		"job_id": job_id,
		"status": "pending",
		"query": q,
		"results_url": _results_url(job_id),
		"poll_method": _results_method_path(),
		"message": _("Image search queued; poll results_url until completed"),
	}


def get_image_search_job(job_id: str) -> dict:
	jid = (job_id or "").strip()
	if not jid:
		frappe.throw(_("job_id is required"))
	payload = _read_job(jid)
	if not payload:
		frappe.throw(_("Unknown or expired job_id"), frappe.DoesNotExistError)
	out = dict(payload)
	out["ok"] = True
	out["results_url"] = _results_url(jid)
	return out


def run_image_search_job(
	job_id: str,
	query: str,
	limit: int = 9,
	store_in_erp: int = 0,
	item_code: str | None = None,
	set_item_image: int = 0,
) -> None:
	"""Background worker: fill cache entry with image links (and optional ERP storage)."""
	payload = _read_job(job_id) or {
		"job_id": job_id,
		"query": query,
		"limit": limit,
		"store_in_erp": bool(store_in_erp),
		"item_code": item_code,
		"set_item_image": bool(set_item_image),
	}
	payload["status"] = "running"
	_write_job(job_id, payload)

	try:
		from erpnext.image_search.search_service import ImageSearchService
		from erpnext.image_search.thumb import probe_image_url

		service = ImageSearchService()
		target = max(1, min(24, cint(limit) or 9))
		raw = service.search_images(query=query, target_count=max(target * 2, 12)) or []

		images: list[dict] = []
		for row in raw:
			if len(images) >= target:
				break
			url = (row.get("url") or "").strip()
			if not url:
				continue
			# Prefer reachable URLs when we can probe; keep link even if probe fails (client may fetch)
			thumb = (row.get("thumbnail_url") or url or "").strip()
			reachable = True
			try:
				reachable = bool(probe_image_url(url) or (thumb and probe_image_url(thumb)))
			except Exception:
				reachable = True
			if not reachable and thumb and thumb != url:
				url = thumb
			images.append(
				{
					"url": url,
					"thumbnail_url": thumb or url,
					"source": row.get("source") or "search",
					"width": row.get("width"),
					"height": row.get("height"),
					"quality_score": row.get("quality_score"),
				}
			)

		stored: list[dict] = []
		if cint(store_in_erp) and images:
			if item_code:
				stored = _store_candidates_for_item(
					item_code=item_code,
					images=images,
					set_item_image=cint(set_item_image),
				)
			else:
				stored = _store_files_only(job_id=job_id, images=images)

		payload.update(
			{
				"status": "completed",
				"images": images,
				"stored": stored,
				"error": None,
				"count": len(images),
			}
		)
		_write_job(job_id, payload)
		frappe.db.commit()
	except Exception as exc:
		payload.update(
			{
				"status": "failed",
				"images": [],
				"stored": [],
				"error": str(exc),
			}
		)
		_write_job(job_id, payload)
		frappe.log_error(frappe.get_traceback(), f"API image search failed ({job_id})")


def _store_files_only(job_id: str, images: list[dict]) -> list[dict]:
	"""Download search hits into public File records (no Item link)."""
	import os

	from frappe.utils import get_files_path
	from frappe.utils.file_manager import get_content_hash
	from erpnext.image_search.thumb import download_image_bytes

	stored: list[dict] = []
	frappe.flags.ignore_permissions = True
	try:
		folder = get_files_path(is_private=False)
		frappe.create_folder(folder)
		for i, image in enumerate(images):
			url = (image.get("url") or "").strip()
			if not url:
				continue
			try:
				data = download_image_bytes(url)
			except Exception:
				continue
			fname = f"img_search_{job_id}_{i + 1}.jpg"
			file_url = f"/files/{fname}"
			path = os.path.join(folder, fname)
			with open(path, "wb") as out:
				out.write(data)
			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": fname,
					"file_url": file_url,
					"is_private": 0,
					"file_size": len(data),
					"content_hash": get_content_hash(data),
				}
			)
			file_doc.flags.ignore_permissions = True
			file_doc.flags.copy_from_existing_file = True
			file_doc.insert(ignore_permissions=True)
			stored.append({"file": file_doc.name, "url": file_url, "source_url": url})
			image["url"] = file_url
			image["thumbnail_url"] = file_url
			image["stored_file"] = file_doc.name
	finally:
		frappe.flags.ignore_permissions = False
	return stored


def _store_candidates_for_item(item_code: str, images: list[dict], set_item_image: int = 0) -> list[dict]:
	"""Persist search hits as Product Image Candidate rows (and optionally set Item.image)."""
	stored: list[dict] = []
	frappe.flags.ignore_permissions = True
	try:
		# Clear previous API ranks for this item? Keep existing; append with next ranks.
		existing = frappe.db.count(
			"Product Image Candidate",
			{"product_type": "Item", "product_id": item_code},
		)
		rank_base = cint(existing) or 0
		for i, image in enumerate(images):
			doc = frappe.get_doc(
				{
					"doctype": "Product Image Candidate",
					"product_type": "Item",
					"product_id": item_code,
					"image_url": image.get("url"),
					"thumbnail_url": image.get("thumbnail_url"),
					"source": image.get("source") or "api_search",
					"width": image.get("width"),
					"height": image.get("height"),
					"quality_score": image.get("quality_score") or 0.5,
					"rank": rank_base + i + 1,
					"metadata": json.dumps({"via": "api.search_images"}),
				}
			)
			doc.insert(ignore_permissions=True)
			stored.append(
				{
					"candidate": doc.name,
					"url": doc.image_url,
					"rank": doc.rank,
				}
			)

		if set_item_image and stored:
			from erpnext.image_search.api import select_primary_image

			select_primary_image("Item", item_code, stored[0]["candidate"], stage="temp")
			image_url = frappe.db.get_value("Item", item_code, "image")
			stored[0]["item_image"] = image_url
	finally:
		frappe.flags.ignore_permissions = False
	return stored
