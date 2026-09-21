"""Ensure ERP desk uses the black SilkOS mark (not the blue React icon).

Black asset: /assets/erpnext/images/erpnext-logo.png (+ svg favicon).
Blue asset (React only): erpnext-ecommerce/public/{favicon,icon-256}.png
  and /assets/erpnext/images/erpnext-logo-blue.png / silkos-*.png.
"""

from __future__ import annotations

import frappe

DESK_ICON = "/assets/erpnext/images/erpnext-logo.png"
DESK_FAVICON = "/assets/erpnext/images/erpnext-favicon.svg"

# Previous mistaken blue overrides — force back to black on migrate.
_BLUE_OR_SILKOS = (
	"/assets/erpnext/images/silkos-icon.png",
	"/assets/erpnext/images/silkos-icon-128.png",
	"/assets/erpnext/images/silkos-icon-256.png",
	"/assets/erpnext/images/silkos-favicon.png",
	"/assets/erpnext/images/silkos-logo.png",
	"/assets/erpnext/images/erpnext-logo-blue.png",
)


def ensure_silkos_branding():
	"""Keep Website / Navbar desk logos on the black mark."""
	try:
		ws = frappe.get_single("Website Settings")
		changed = False
		fav = (ws.favicon or "").strip()
		splash = (ws.splash_image or "").strip()
		if not fav or fav in _BLUE_OR_SILKOS:
			ws.favicon = DESK_FAVICON
			changed = True
		if not splash or splash in _BLUE_OR_SILKOS:
			ws.splash_image = DESK_ICON
			changed = True
		if changed:
			ws.flags.ignore_permissions = True
			ws.save(ignore_permissions=True)

		ns = frappe.get_single("Navbar Settings")
		logo = (ns.app_logo or "").strip()
		if not logo or logo in _BLUE_OR_SILKOS:
			ns.app_logo = DESK_ICON
			ns.flags.ignore_permissions = True
			ns.save(ignore_permissions=True)

		frappe.db.commit()
	except Exception:
		frappe.log_error(title="ensure_silkos_branding")
