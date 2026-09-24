"""Local 256×256 product thumbs for Item.image (i032).

Default geometry is center-contain: fit the longer side, pad the shorter.
``materialize_item_thumb`` defaults to PNG (transparent pad / alpha kept), with
JPEG fallback if PNG encode fails. Explicit ``fmt="jpeg"`` still pads with white.
"""

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
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
	"(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def sku_image_stem(item_code: str) -> str:
	return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(item_code))


def _ext_for_format(fmt: str) -> str:
	return ".png" if str(fmt or "").lower() == "png" else ".jpg"


def thumb_filename(item_code: str, variant: str = "final", fmt: str = "jpeg") -> str:
	stem = sku_image_stem(item_code)
	kind = (variant or "final").strip().lower()
	ext = _ext_for_format(fmt)
	if kind == "temp":
		return f"{stem}_temp{ext}"
	if kind in ("temp_crop", "crop"):
		return f"{stem}_temp_crop{ext}"
	return f"{stem}{ext}"


def unwrap_image_url(url: str) -> str:
	"""Accept page-wrapper URLs (Google imgurl=, etc.) and return the inner image URL."""
	from urllib.parse import parse_qs, unquote, urlparse

	raw = str(url or "").strip()
	if not raw:
		return ""
	if raw.startswith("data:image/"):
		return raw
	parsed = urlparse(raw)
	qs = parse_qs(parsed.query)
	for key in ("imgurl", "mediaurl", "image", "imgrefurl", "url", "src"):
		vals = qs.get(key)
		if not vals:
			continue
		inner = unquote(vals[0]).strip()
		if inner.startswith("data:image/") or inner.startswith("http://") or inner.startswith("https://"):
			if key == "url" and "google." in (parsed.netloc or "").lower() and "imgurl" in qs:
				continue
			if inner.startswith("http") and any(
				inner.lower().endswith(ext) or f"{ext}?" in inner.lower()
				for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff")
			):
				return inner
			if key in ("imgurl", "mediaurl", "image", "src"):
				return inner
	return raw


def _decode_data_image(url: str) -> bytes:
	import base64

	header, _sep, payload = url.partition(",")
	if not payload:
		frappe.throw(_("Invalid data image URL"))
	try:
		return base64.b64decode(payload)
	except Exception:
		frappe.throw(_("Invalid data image URL"))


def probe_image_url(url: str) -> bool:
	"""Return True when the URL returns a fetchable image (same rules as download_image_bytes)."""
	if not url or not str(url).strip():
		return False
	url = unwrap_image_url(str(url).strip())
	if url.startswith("data:image/"):
		return True
	try:
		resp = requests.get(
			url,
			timeout=DOWNLOAD_TIMEOUT_S,
			headers={
				"User-Agent": USER_AGENT,
				"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
			},
			stream=True,
			allow_redirects=True,
		)
		if resp.status_code != 200:
			return False
		ctype = (resp.headers.get("content-type") or "").lower()
		total = 0
		sniff = b""
		for chunk in resp.iter_content(chunk_size=4096):
			if not chunk:
				continue
			if not sniff:
				sniff = chunk[:32]
			total += len(chunk)
			if total > MAX_DOWNLOAD_BYTES:
				return False
			if total >= 512:
				break
		if total <= 0:
			return False
		if "text/html" in ctype and not sniff.startswith((b"\x89PNG", b"\xff\xd8", b"RIFF", b"GIF")):
			return False
		return True
	except Exception:
		return False


def download_image_bytes(url: str) -> bytes:
	if not url or not str(url).strip():
		frappe.throw(_("Empty image URL"))
	url = unwrap_image_url(str(url).strip())
	if url.startswith("data:image/"):
		return _decode_data_image(url)
	try:
		resp = requests.get(
			url,
			timeout=DOWNLOAD_TIMEOUT_S,
			headers={
				"User-Agent": USER_AGENT,
				"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
			},
			stream=True,
			allow_redirects=True,
		)
		resp.raise_for_status()
		ctype = (resp.headers.get("content-type") or "").lower()
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
		if "text/html" in ctype and not data[:16].startswith((b"\x89PNG", b"\xff\xd8", b"RIFF", b"GIF")):
			frappe.throw(_("URL did not return an image (html page). Use a direct png/jpg/webp link."))
		return data
	except frappe.ValidationError:
		raise
	except Exception as exc:
		frappe.throw(_("Could not download image: {0}").format(str(exc)))


def _open_rgb(image_bytes: bytes):
	"""Decode → RGB, flattening alpha on white (JPEG path)."""
	try:
		from PIL import Image
	except ImportError:
		frappe.throw(_("Pillow is required for product thumbnails"))

	try:
		img = Image.open(io.BytesIO(image_bytes))
		if getattr(img, "n_frames", 1) > 1:
			img.seek(0)
		img.load()
	except Exception:
		frappe.throw(_("Could not decode image (use png, jpg, webp, gif, or similar)"))
	if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
		rgba = img.convert("RGBA")
		bg = Image.new("RGB", rgba.size, (255, 255, 255))
		bg.paste(rgba, mask=rgba.split()[-1])
		return bg
	return img.convert("RGB")


def _open_rgba(image_bytes: bytes):
	"""Decode → RGBA, keeping transparency (PNG path)."""
	try:
		from PIL import Image
	except ImportError:
		frappe.throw(_("Pillow is required for product thumbnails"))

	try:
		img = Image.open(io.BytesIO(image_bytes))
		if getattr(img, "n_frames", 1) > 1:
			img.seek(0)
		img.load()
	except Exception:
		frappe.throw(_("Could not decode image (use png, jpg, webp, gif, or similar)"))
	if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
		return img.convert("RGBA")
	return img.convert("RGBA")


def _resample_filter():
	from PIL import Image

	# Pillow ≥9.1 uses Image.Resampling; older builds expose LANCZOS on Image.
	return getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)


def _center_contain(img, size: int = THUMB_SIZE, *, transparent: bool = False):
	"""Fit the longer side into the square; pad the shorter side.

	``transparent=True`` (PNG) uses a clear RGBA canvas so cutouts stay cutouts.
	Otherwise pads with white RGB (JPEG).
	"""
	from PIL import Image

	w, h = img.size
	if w <= 0 or h <= 0:
		frappe.throw(_("Invalid image dimensions"))
	scale = min(size / w, size / h)
	nw = max(1, int(round(w * scale)))
	nh = max(1, int(round(h * scale)))
	resized = img.resize((nw, nh), _resample_filter())
	left = (size - nw) // 2
	top = (size - nh) // 2
	if transparent:
		if resized.mode != "RGBA":
			resized = resized.convert("RGBA")
		canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
		canvas.paste(resized, (left, top), resized)
		return canvas
	if resized.mode == "RGBA":
		canvas = Image.new("RGB", (size, size), (255, 255, 255))
		canvas.paste(resized, (left, top), resized)
		return canvas
	if resized.mode != "RGB":
		resized = resized.convert("RGB")
	canvas = Image.new("RGB", (size, size), (255, 255, 255))
	canvas.paste(resized, (left, top))
	return canvas


def _apply_relative_crop(img, crop: Dict[str, Any], size: int = THUMB_SIZE, *, transparent: bool = False):
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
	return _center_contain(cropped, size, transparent=transparent)


def encode_thumb_jpeg(image_bytes: bytes, crop: Optional[Dict[str, Any]] = None) -> bytes:
	img = _open_rgb(image_bytes)
	if crop:
		out = _apply_relative_crop(img, crop, transparent=False)
	else:
		out = _center_contain(img, transparent=False)
	buf = io.BytesIO()
	out.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
	return buf.getvalue()


def encode_thumb_png(image_bytes: bytes, crop: Optional[Dict[str, Any]] = None) -> bytes:
	"""256×256 PNG with alpha preserved (catalog migration / cutout assets)."""
	img = _open_rgba(image_bytes)
	if crop:
		out = _apply_relative_crop(img, crop, transparent=True)
	else:
		out = _center_contain(img, transparent=True)
	buf = io.BytesIO()
	out.save(buf, format="PNG", optimize=True)
	return buf.getvalue()


def remove_near_white_background(
	image_bytes: bytes,
	*,
	threshold: int = 248,
	feather: int = 18,
) -> bytes:
	"""Flood-fill near-white pixels from the edges → transparent PNG (full resolution).

	Studio / JPEG product shots that sit on a solid white pad become cutouts so
	catalog cards and the POS image viewer show the checkerboard, not a white box.
	Interior near-white (labels, highlights) is left alone unless connected to the edge.
	"""
	from collections import deque

	from PIL import Image

	try:
		img = Image.open(io.BytesIO(image_bytes))
		if getattr(img, "n_frames", 1) > 1:
			img.seek(0)
		img.load()
	except Exception:
		frappe.throw(_("Could not decode image (use png, jpg, webp, gif, or similar)"))

	rgba = img.convert("RGBA")
	w, h = rgba.size
	if w <= 0 or h <= 0:
		frappe.throw(_("Invalid image dimensions"))

	thr = max(1, min(255, int(threshold)))
	feather_n = max(0, min(thr, int(feather)))
	low = thr - feather_n

	px = rgba.load()
	# 0 = unseen, 1 = queued/visited as background candidate
	seen = bytearray(w * h)
	queue: deque = deque()

	def _maybe_queue(x: int, y: int) -> None:
		if x < 0 or y < 0 or x >= w or y >= h:
			return
		idx = y * w + x
		if seen[idx]:
			return
		r, g, b, _a = px[x, y]
		# Connected component only through near-white / soft-white pixels.
		if min(r, g, b) < low:
			return
		seen[idx] = 1
		queue.append((x, y))

	for x in range(w):
		_maybe_queue(x, 0)
		_maybe_queue(x, h - 1)
	for y in range(h):
		_maybe_queue(0, y)
		_maybe_queue(w - 1, y)

	while queue:
		x, y = queue.popleft()
		r, g, b, _a = px[x, y]
		m = min(r, g, b)
		if m >= thr:
			alpha = 0
		elif feather_n <= 0 or m <= low:
			alpha = 255
		else:
			alpha = int(round(255 * (thr - m) / float(feather_n)))
		px[x, y] = (r, g, b, alpha)
		if alpha < 255:
			_maybe_queue(x - 1, y)
			_maybe_queue(x + 1, y)
			_maybe_queue(x, y - 1)
			_maybe_queue(x, y + 1)

	buf = io.BytesIO()
	rgba.save(buf, format="PNG", optimize=True)
	return buf.getvalue()


def _delete_named_image_file(item_code: str, fname: str) -> None:
	"""Remove one SKU image filename (File rows + disk), nothing else."""
	file_url = f"/files/{fname}"
	existing = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Item", "attached_to_name": item_code},
		fields=["name", "file_name", "file_url"],
		ignore_permissions=True,
	)
	for row in existing:
		if str(row.file_name or "") == fname or str(row.file_url or "").split("?")[0] == file_url:
			try:
				frappe.delete_doc("File", row.name, ignore_permissions=True, force=True)
			except Exception:
				frappe.log_error(title="Item image file delete failed", message=f"{item_code} {row.name}")

	folder = get_files_path(is_private=False)
	path = os.path.join(folder, fname)
	if os.path.isfile(path):
		try:
			os.remove(path)
		except OSError:
			frappe.log_error(title="Item image disk delete failed", message=path)


def _delete_other_item_images(item_code: str, keep_fname: str) -> None:
	"""Drop leftover image attachments so Item.image is only /files/{sku}.jpg."""
	image_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
	existing = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Item", "attached_to_name": item_code},
		fields=["name", "file_name", "file_url"],
		ignore_permissions=True,
	)
	folder = get_files_path(is_private=False)
	for row in existing:
		fn = str(row.file_name or "")
		fu = str(row.file_url or "").split("?")[0]
		if fn == keep_fname or fu.endswith("/" + keep_fname):
			continue
		looks_image = fn.lower().endswith(image_exts) or fu.lower().endswith(image_exts)
		if not looks_image:
			continue
		try:
			frappe.delete_doc("File", row.name, ignore_permissions=True, force=True)
		except Exception:
			frappe.log_error(title="Item image cleanup failed", message=f"{item_code} {row.name}")
		disk = os.path.join(folder, fn) if fn else ""
		if disk and os.path.isfile(disk) and os.path.basename(disk) != keep_fname:
			try:
				os.remove(disk)
			except OSError:
				pass


def _write_item_thumb(
	item_code: str,
	content: bytes,
	variant: str = "final",
	*,
	fmt: str = "jpeg",
	set_item_image: bool = True,
	commit: bool = True,
) -> str:
	fname = thumb_filename(item_code, variant, fmt=fmt)
	file_url = f"/files/{fname}"
	_delete_named_image_file(item_code, fname)
	# Drop the other extension so Item.image never leaves a stale .jpg next to a new .png.
	alt_fmt = "jpeg" if str(fmt).lower() == "png" else "png"
	_delete_named_image_file(item_code, thumb_filename(item_code, variant, fmt=alt_fmt))
	if variant == "final":
		for kind in ("temp", "temp_crop"):
			_delete_named_image_file(item_code, thumb_filename(item_code, kind, fmt=fmt))
			_delete_named_image_file(item_code, thumb_filename(item_code, kind, fmt=alt_fmt))
		stem = sku_image_stem(item_code)
		folder = get_files_path(is_private=False)
		for path in glob.glob(os.path.join(folder, f"{stem}-thumb*.jpg")) + glob.glob(
			os.path.join(folder, f"{stem}-thumb*.png")
		):
			try:
				os.remove(path)
			except OSError:
				pass
		_delete_other_item_images(item_code, fname)

	folder = get_files_path(is_private=False)
	frappe.create_folder(folder)
	with open(os.path.join(folder, fname), "wb") as out:
		out.write(content)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": fname,
			"file_url": file_url,
			"attached_to_doctype": "Item",
			"attached_to_name": item_code,
			"is_private": 0,
			"file_size": len(content),
			"content_hash": get_content_hash(content),
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.flags.copy_from_existing_file = True
	file_doc.flags.ignore_duplicate_entry_error = True
	file_doc.insert(ignore_permissions=True)

	if set_item_image:
		frappe.db.set_value("Item", item_code, "image", file_url)
	if commit:
		frappe.db.commit()
	return file_url


# Back-compat alias
def _write_item_jpeg(
	item_code: str,
	jpeg: bytes,
	variant: str = "final",
	*,
	set_item_image: bool = True,
	commit: bool = True,
) -> str:
	return _write_item_thumb(
		item_code,
		jpeg,
		variant=variant,
		fmt="jpeg",
		set_item_image=set_item_image,
		commit=commit,
	)


def materialize_item_thumb(
	item_code: str,
	image_bytes: bytes,
	crop: Optional[Dict[str, Any]] = None,
	*,
	commit: bool = True,
	variant: str = "final",
	set_item_image: bool = True,
	fmt: str = "auto",
) -> str:
	"""Encode a 256×256 thumb as /files/{sku}.png or .jpg (and temp variants).

	Default ``fmt="auto"``: try PNG first (keeps alpha / cutouts), fall back to JPEG
	if PNG encode fails. Pass ``fmt="png"`` or ``fmt="jpeg"`` to force one format.
	"""
	if not item_code:
		frappe.throw(_("Item code required"))
	if not image_bytes:
		frappe.throw(_("No image data"))

	requested = str(fmt or "auto").lower().strip()
	if requested in ("jpg", "jpeg"):
		order = ("jpeg",)
	elif requested == "png":
		order = ("png",)
	else:
		# auto — prefer PNG locally, JPEG only if PNG cannot be produced
		order = ("png", "jpeg")

	last_exc: Optional[Exception] = None
	for kind in order:
		try:
			payload = encode_thumb_png(image_bytes, crop) if kind == "png" else encode_thumb_jpeg(image_bytes, crop)
			return _write_item_thumb(
				item_code,
				payload,
				variant=variant,
				fmt=kind,
				set_item_image=set_item_image,
				commit=commit,
			)
		except Exception as exc:
			last_exc = exc
			continue
	if last_exc:
		raise last_exc
	frappe.throw(_("Could not materialize item thumb"))
	return ""


def promote_item_image_to_final(item_code: str, source_variant: str = "temp_crop", *, commit: bool = True) -> str:
	"""Copy {sku}_temp_crop / {sku}_temp onto {sku}.jpg|.png (same format as source)."""
	folder = get_files_path(is_private=False)
	order = [source_variant]
	if source_variant != "temp_crop":
		order.append("temp_crop")
	if source_variant != "temp":
		order.append("temp")
	payload = None
	found_fmt = "jpeg"
	for kind in order:
		for fmt in ("png", "jpeg"):
			path = os.path.join(folder, thumb_filename(item_code, kind, fmt=fmt))
			if os.path.isfile(path):
				with open(path, "rb") as f:
					payload = f.read()
				found_fmt = fmt
				break
		if payload:
			break
	if not payload:
		frappe.throw(_("No temporary image to apply"))
	return _write_item_thumb(
		item_code, payload, variant="final", fmt=found_fmt, set_item_image=True, commit=commit
	)


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
	"""Read bytes for a site-relative /files/ or /private/files/ URL.

	When the exact path is missing, try the sibling extension (``.png`` first,
	then ``.jpg`` / ``.jpeg``) so rematerialized PNG thumbs still resolve from
	stale ``Item.image`` JPEG URLs and vice versa.
	"""
	import os
	from urllib.parse import unquote

	from frappe.utils import get_files_path

	url = _normalize_file_url(file_url)
	if not url.startswith("/"):
		frappe.throw(_("Not a local file URL"))

	def _candidates(u: str) -> list[str]:
		out = [u]
		lower = u.lower()
		if lower.endswith(".jpg") or lower.endswith(".jpeg"):
			stem = u.rsplit(".", 1)[0]
			out = [f"{stem}.png", u]
		elif lower.endswith(".png"):
			stem = u.rsplit(".", 1)[0]
			out = [u, f"{stem}.jpg", f"{stem}.jpeg"]
		# de-dupe preserve order
		seen = set()
		ordered = []
		for item in out:
			if item not in seen:
				seen.add(item)
				ordered.append(item)
		return ordered

	for candidate in _candidates(url):
		file_doc = frappe.db.get_value("File", {"file_url": candidate}, "name")
		if not file_doc:
			fname = unquote(candidate.rsplit("/", 1)[-1])
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

		rel = candidate.lstrip("/")
		path = None
		if rel.startswith("files/"):
			path = get_files_path(rel[len("files/") :], is_private=False)
		elif rel.startswith("private/files/"):
			path = get_files_path(rel[len("private/files/") :], is_private=True)
		else:
			continue

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
