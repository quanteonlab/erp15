"""imgproxy helpers + durable local materialize for remotes.

Display preference (frontend): local /files → imgproxy(canonical) → raw.
Write path: always try to persist a local copy after accepting a remote image
(company logo, catalog CSV, product materialize).
"""

from __future__ import annotations

import os
import re

import frappe
from frappe.utils import cint, cstr


def get_imgproxy_base() -> str:
	"""Server-side imgproxy origin (site_config / env)."""
	base = (
		frappe.conf.get("imgproxy_url")
		or os.environ.get("IMGPROXY_URL")
		or os.environ.get("NEXT_PUBLIC_IMGPROXY_URL")
		or "https://imgproxy.l.l0l.in"
	)
	return cstr(base).rstrip("/")


def get_public_asset_base() -> str:
	base = (
		frappe.conf.get("public_asset_base")
		or os.environ.get("PUBLIC_ASSET_BASE")
		or os.environ.get("NEXT_PUBLIC_PUBLIC_ASSET_BASE")
		or ""
	)
	return cstr(base).rstrip("/")


def to_public_absolute_url(url: str) -> str:
	"""Make a URL fetchable by imgproxy (rewrite localhost → public asset base)."""
	u = cstr(url or "").strip()
	if not u:
		return ""
	if u.startswith("data:") or u.startswith("blob:"):
		return ""
	if u.startswith("http://") or u.startswith("https://"):
		from urllib.parse import urlparse

		parsed = urlparse(u)
		if parsed.hostname in ("127.0.0.1", "localhost"):
			base = get_public_asset_base()
			if not base:
				return ""
			return f"{base}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
		return u
	if u.startswith("/"):
		base = get_public_asset_base()
		if not base:
			return ""
		return f"{base}{u}"
	return ""


def build_imgproxy_url(
	source_url: str,
	width: int = 0,
	height: int = 0,
	resizing_type: str = "fit",
	gravity: str = "ce",
	enlarge: int = 1,
	extension: str = "",
) -> str:
	"""Build an /unsafe/ imgproxy URL (lab / playground style)."""
	absolute = to_public_absolute_url(source_url)
	if not absolute:
		# Allow already-absolute remotes even without public base
		absolute = cstr(source_url or "").strip()
		if not (absolute.startswith("http://") or absolute.startswith("https://")):
			return ""
	base = get_imgproxy_base()
	if not base:
		return ""
	w = max(0, cint(width))
	h = max(0, cint(height))
	parts = ["unsafe"]
	if w or h:
		rt = (resizing_type or "fit").strip() or "fit"
		parts.append(f"rs:{rt}:{w}:{h}:{1 if cint(enlarge) else 0}")
	g = (gravity or "ce").strip() or "ce"
	parts.append(f"g:{g}")
	parts.append(f"plain/{absolute}")
	url = f"{base}/{'/'.join(parts)}"
	ext = (extension or "").strip().lstrip("@")
	if ext:
		url = f"{url}@{ext}"
	return url


def download_image_prefer_imgproxy(url: str, max_edge: int = 1200) -> bytes:
	"""
	Download image bytes. Prefer an imgproxy-resized variant when possible
	(smaller, consistent), then fall back to the direct URL.
	"""
	from erpnext.image_search.thumb import download_image_bytes

	url = cstr(url or "").strip()
	if not url:
		frappe.throw("Empty image URL")

	if url.startswith("data:image/"):
		return download_image_bytes(url)

	if url.startswith("http://") or url.startswith("https://"):
		proxy = build_imgproxy_url(url, width=max_edge, height=max_edge, resizing_type="fit")
		if proxy and proxy != url:
			try:
				return download_image_bytes(proxy)
			except Exception:
				pass
		return download_image_bytes(url)

	if url.startswith("/"):
		from erpnext.image_search.thumb import read_local_file_bytes

		return read_local_file_bytes(url)

	frappe.throw("Unsupported image URL")


_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


def company_logo_stem(company_name: str) -> str:
	slug = _SAFE_STEM.sub("_", cstr(company_name or "company").strip())[:60] or "company"
	return f"company_{slug}_logo"


def materialize_company_logo_bytes(company_name: str, image_bytes: bytes, *, commit: bool = True) -> str:
	"""
	Write a durable public JPEG under /files/company_{slug}_logo.jpg and
	point Company.company_logo at it.
	"""
	from erpnext.image_search.thumb import encode_thumb_jpeg
	from frappe.utils.file_manager import save_file

	if not company_name or not image_bytes:
		frappe.throw("Company and image required")

	# Logos: keep more detail than product 256 thumbs (max edge 1024).
	jpeg = None
	try:
		import io
		from PIL import Image

		img = Image.open(io.BytesIO(image_bytes))
		if getattr(img, "n_frames", 1) > 1:
			img.seek(0)
		img.load()
		if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
			rgba = img.convert("RGBA")
			bg = Image.new("RGB", rgba.size, (255, 255, 255))
			bg.paste(rgba, mask=rgba.split()[-1])
			img = bg
		else:
			img = img.convert("RGB")
		max_edge = 1024
		w, h = img.size
		if max(w, h) > max_edge:
			scale = max_edge / float(max(w, h))
			img = img.resize(
				(max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
				getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS),
			)
		buf = io.BytesIO()
		img.save(buf, format="JPEG", quality=90, optimize=True)
		jpeg = buf.getvalue()
	except Exception:
		from erpnext.image_search.thumb import encode_thumb_jpeg

		jpeg = encode_thumb_jpeg(image_bytes, crop=None)

	fname = f"{company_logo_stem(company_name)}.jpg"
	# Remove prior same-named public file to avoid stale File rows
	existing = frappe.get_all(
		"File",
		filters={"file_name": fname, "attached_to_doctype": "Company", "attached_to_name": company_name},
		pluck="name",
		ignore_permissions=True,
	)
	for name in existing:
		try:
			frappe.delete_doc("File", name, ignore_permissions=True, force=1)
		except Exception:
			pass

	file_doc = save_file(fname, jpeg, "Company", company_name, is_private=0)
	file_url = file_doc.file_url
	frappe.db.set_value("Company", company_name, "company_logo", file_url)
	if commit:
		frappe.db.commit()
	return file_url
