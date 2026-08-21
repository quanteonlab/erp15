"""Local 256×256 JPEG thumbs for Item.image (i032)."""

from __future__ import annotations

import io
from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _
from frappe.utils.file_manager import save_file

THUMB_SIZE = 256
JPEG_QUALITY = 85
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 12
USER_AGENT = (
	"Mozilla/5.0 (compatible; ERPNextImageThumb/1.0; +https://frappeframework.com)"
)


def thumb_filename(item_code: str) -> str:
	safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(item_code))
	return f"{safe}-thumb.jpg"


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


def _delete_existing_thumb_files(item_code: str, fname: str) -> None:
	"""Remove prior thumb File rows for this Item so names do not pile up."""
	existing = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Item",
			"attached_to_name": item_code,
			"file_name": fname,
		},
		pluck="name",
		ignore_permissions=True,
	)
	for name in existing:
		try:
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
		except Exception:
			frappe.log_error(title="Thumb file delete failed", message=f"{item_code} {name}")


def materialize_item_thumb(
	item_code: str,
	image_bytes: bytes,
	crop: Optional[Dict[str, Any]] = None,
	*,
	commit: bool = True,
) -> str:
	"""
	Encode a 256×256 JPEG, attach as public File on Item, set Item.image.
	Returns the local file_url (/files/...).
	"""
	if not item_code:
		frappe.throw(_("Item code required"))
	if not image_bytes:
		frappe.throw(_("No image data"))

	jpeg = encode_thumb_jpeg(image_bytes, crop)
	fname = thumb_filename(item_code)
	_delete_existing_thumb_files(item_code, fname)

	ret = save_file(fname, jpeg, "Item", item_code, is_private=0)
	file_url = getattr(ret, "file_url", None)
	if not file_url and isinstance(ret, dict):
		file_url = ret.get("file_url")
	if not file_url and getattr(ret, "name", None):
		file_url = frappe.db.get_value("File", ret.name, "file_url")
	if not file_url:
		frappe.throw(_("Could not store image file"))

	frappe.db.set_value("Item", item_code, "image", file_url)
	if commit:
		frappe.db.commit()
	return file_url


def read_local_file_bytes(file_url: str) -> bytes:
	"""Read bytes for a site-relative /files/ or /private/files/ URL."""
	if not file_url or not str(file_url).startswith("/"):
		frappe.throw(_("Not a local file URL"))
	file_doc = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_doc:
		# Fallback: path under sites
		from frappe.utils import get_files_path
		import os

		rel = str(file_url).lstrip("/")
		if rel.startswith("files/"):
			path = get_files_path(rel[len("files/") :], is_private=False)
		elif rel.startswith("private/files/"):
			path = get_files_path(rel[len("private/files/") :], is_private=True)
		else:
			frappe.throw(_("Unsupported local file path"))
		if not os.path.isfile(path):
			frappe.throw(_("Local image file not found"))
		with open(path, "rb") as f:
			return f.read()

	frappe.flags.ignore_permissions = True
	try:
		doc = frappe.get_doc("File", file_doc)
		content = doc.get_content()
	finally:
		frappe.flags.ignore_permissions = False
	if isinstance(content, str):
		content = content.encode("utf-8")
	if not content:
		frappe.throw(_("Empty local image file"))
	return content


def mark_candidate_downloaded(candidate_name: str, file_url: str) -> None:
	frappe.db.set_value(
		"Product Image Candidate",
		candidate_name,
		{"is_downloaded": 1, "file_attachment": file_url},
	)
