"""Local 256×256 JPEG thumbs for Item.image (i032)."""

from __future__ import annotations

import glob
import io
import os
from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _
from frappe.utils import get_files_path
from frappe.utils.file_manager import get_content_hash

THUMB_SIZE = 256
JPEG_QUALITY = 85
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 12
USER_AGENT = (
	"Mozilla/5.0 (compatible; ERPNextImageThumb/1.0; +https://frappeframework.com)"
)


def sku_image_stem(item_code: str) -> str:
	return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(item_code))


def thumb_filename(item_code: str) -> str:
	return f"{sku_image_stem(item_code)}.jpg"


def download_image_bytes(url: str) -> bytes:
	if not url or not str(url).strip():
		frappe.throw(_("Empty image URL"))
	url = str(url).strip()
	try:
		resp = requests.get(
			url,
			timeout=DOWNLOAD_TIMEOUT_S,
			headers={"User-Agent": USER_AGENT},
			stream=True,
		)
		resp.raise_for_status()
		chunks = []
		total = 0
		for chunk in resp.iter_content(chunk_size=64 * 1024):
			if not chunk:
				continue
			total += len(chunk)
			if total > MAX_DOWNLOAD_BYTES:
				frappe.throw(_("Image download exceeds size limit"))
			chunks.append(chunk)
		data = b"".join(chunks)
		if not data:
			frappe.throw(_("Empty image download"))
		return data
	except frappe.ValidationError:
		raise
	except Exception as exc:
		frappe.throw(_("Could not download image: {0}").format(str(exc)))


def _open_rgb(image_bytes: bytes):
	try:
		from PIL import Image
	except ImportError:
		frappe.throw(_("Pillow is required for product thumbnails"))

	img = Image.open(io.BytesIO(image_bytes))
	img.load()
	if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
		rgba = img.convert("RGBA")
		bg = Image.new("RGB", rgba.size, (255, 255, 255))
		bg.paste(rgba, mask=rgba.split()[-1])
		return bg
	return img.convert("RGB")


def _resample_filter():
	from PIL import Image

	# Pillow ≥9.1 uses Image.Resampling; older builds expose LANCZOS on Image.
	return getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)


def _center_cover(img, size: int = THUMB_SIZE):
	w, h = img.size
	if w <= 0 or h <= 0:
		frappe.throw(_("Invalid image dimensions"))
	scale = max(size / w, size / h)
	nw = max(1, int(round(w * scale)))
	nh = max(1, int(round(h * scale)))
	resized = img.resize((nw, nh), _resample_filter())
	left = (nw - size) // 2
	top = (nh - size) // 2
	return resized.crop((left, top, left + size, top + size))


def _apply_relative_crop(img, crop: Dict[str, Any], size: int = THUMB_SIZE):
	w, h = img.size
	try:
		x = float(crop.get("x", 0))
		y = float(crop.get("y", 0))
		cw = float(crop.get("width", 1))
		ch = float(crop.get("height", 1))
	except (TypeError, ValueError):
		frappe.throw(_("Invalid crop box"))

	# Clamp to [0, 1]
	x = max(0.0, min(1.0, x))
	y = max(0.0, min(1.0, y))
	cw = max(0.01, min(1.0 - x, cw))
	ch = max(0.01, min(1.0 - y, ch))

	left = int(round(x * w))
	top = int(round(y * h))
	right = int(round((x + cw) * w))
	bottom = int(round((y + ch) * h))
	left = max(0, min(w - 1, left))
	top = max(0, min(h - 1, top))
	right = max(left + 1, min(w, right))
	bottom = max(top + 1, min(h, bottom))

	cropped = img.crop((left, top, right, bottom))
	return cropped.resize((size, size), _resample_filter())


def encode_thumb_jpeg(image_bytes: bytes, crop: Optional[Dict[str, Any]] = None) -> bytes:
	img = _open_rgb(image_bytes)
	if crop:
		out = _apply_relative_crop(img, crop)
	else:
		out = _center_cover(img)
	buf = io.BytesIO()
	out.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
	return buf.getvalue()


def _delete_item_image_files(item_code: str, fname: str) -> None:
	"""Remove prior Item image File rows and leftover sku / sku-thumb disk files."""
	stem = sku_image_stem(item_code)
	existing = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Item", "attached_to_name": item_code},
		fields=["name", "file_name", "file_url"],
		ignore_permissions=True,
	)
	for row in existing:
		fn = str(row.file_name or "")
		fu = str(row.file_url or "")
		is_sku_image = (
			fn == fname
			or fn.startswith(f"{stem}-thumb")
			or (fn.startswith(stem) and fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
			or fu.startswith(f"/files/{stem}")
		)
		if not is_sku_image:
			continue
		try:
			frappe.delete_doc("File", row.name, ignore_permissions=True, force=True)
		except Exception:
			frappe.log_error(title="Item image file delete failed", message=f"{item_code} {row.name}")

	folder = get_files_path(is_private=False)
	patterns = [
		os.path.join(folder, fname),
		os.path.join(folder, f"{stem}-thumb.jpg"),
		*glob.glob(os.path.join(folder, f"{stem}-thumb*.jpg")),
		*glob.glob(os.path.join(folder, f"{stem}.jpg")),
	]
	seen = set()
	for path in patterns:
		if path in seen:
			continue
		seen.add(path)
		if os.path.isfile(path):
			try:
				os.remove(path)
			except OSError:
				frappe.log_error(title="Item image disk delete failed", message=path)


def materialize_item_thumb(
	item_code: str,
	image_bytes: bytes,
	crop: Optional[Dict[str, Any]] = None,
	*,
	commit: bool = True,
) -> str:
	"""
	Encode a 256×256 JPEG as /files/{sku}.jpg, overwriting any previous SKU image.
	"""
	if not item_code:
		frappe.throw(_("Item code required"))
	if not image_bytes:
		frappe.throw(_("No image data"))

	jpeg = encode_thumb_jpeg(image_bytes, crop)
	fname = thumb_filename(item_code)
	file_url = f"/files/{fname}"
	_delete_item_image_files(item_code, fname)

	folder = get_files_path(is_private=False)
	frappe.create_folder(folder)
	disk_path = os.path.join(folder, fname)
	with open(disk_path, "wb") as out:
		out.write(jpeg)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": fname,
			"file_url": file_url,
			"attached_to_doctype": "Item",
			"attached_to_name": item_code,
			"is_private": 0,
			"file_size": len(jpeg),
			"content_hash": get_content_hash(jpeg),
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.flags.copy_from_existing_file = True
	file_doc.flags.ignore_duplicate_entry_error = True
	file_doc.insert(ignore_permissions=True)

	frappe.db.set_value("Item", item_code, "image", file_url)
	if commit:
		frappe.db.commit()
	return file_url


def _normalize_file_url(file_url: str) -> str:
	from urllib.parse import unquote, urlparse

	raw = str(file_url or "").strip()
	if not raw:
		return ""
	if raw.startswith("http://") or raw.startswith("https://"):
		parsed = urlparse(raw)
		raw = parsed.path or ""
	else:
		raw = raw.split("?", 1)[0].split("#", 1)[0]
	raw = unquote(raw).strip()
	if raw and not raw.startswith("/"):
		raw = "/" + raw
	return raw


def read_local_file_bytes(file_url: str) -> bytes:
	"""Read bytes for a site-relative /files/ or /private/files/ URL."""
	import os
	from urllib.parse import unquote

	from frappe.utils import get_files_path

	url = _normalize_file_url(file_url)
	if not url.startswith("/"):
		frappe.throw(_("Not a local file URL"))

	file_doc = frappe.db.get_value("File", {"file_url": url}, "name")
	if not file_doc:
		fname = unquote(url.rsplit("/", 1)[-1])
		matches = frappe.get_all(
			"File",
			filters={"file_name": fname},
			pluck="name",
			limit=5,
			ignore_permissions=True,
		)
		file_doc = matches[0] if matches else None

	if file_doc:
		frappe.flags.ignore_permissions = True
		try:
			doc = frappe.get_doc("File", file_doc)
			content = doc.get_content()
		finally:
			frappe.flags.ignore_permissions = False
		if isinstance(content, str):
			content = content.encode("utf-8")
		if content:
			return content

	rel = url.lstrip("/")
	path = None
	if rel.startswith("files/"):
		path = get_files_path(rel[len("files/") :], is_private=False)
	elif rel.startswith("private/files/"):
		path = get_files_path(rel[len("private/files/") :], is_private=True)
	else:
		frappe.throw(_("Unsupported local file path"))

	if path and os.path.isfile(path):
		with open(path, "rb") as f:
			return f.read()

	frappe.throw(_("Local image file not found"))


def mark_candidate_downloaded(candidate_name: str, file_url: str) -> None:
	frappe.db.set_value(
		"Product Image Candidate",
		candidate_name,
		{"is_downloaded": 1, "file_attachment": file_url},
	)
