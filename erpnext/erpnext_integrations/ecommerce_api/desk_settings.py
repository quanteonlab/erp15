"""Desk-session settings not tied to any storefront/ecommerce flow — quick
account-menu toggles like the language switch live here."""

from __future__ import annotations

import frappe
from frappe import _

DEFAULT_DUAL_LANGUAGES = ("zh", "es")


def get_dual_languages() -> tuple[str, str]:
	settings = frappe.get_cached_doc("System Settings")
	lang_1 = settings.get("erpnext_dual_language_1") or DEFAULT_DUAL_LANGUAGES[0]
	lang_2 = settings.get("erpnext_dual_language_2") or DEFAULT_DUAL_LANGUAGES[1]
	return lang_1, lang_2


@frappe.whitelist(allow_guest=True)
def toggle_my_language():
	# allow_guest=True per this app's whitelisted-function convention, but this
	# action only makes sense for a real account — reject anonymous callers
	# explicitly rather than silently touching the Guest user's language.
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to change your language"), frappe.PermissionError)

	lang_1, lang_2 = get_dual_languages()
	current_lang = frappe.db.get_value("User", frappe.session.user, "language")
	new_lang = lang_2 if current_lang == lang_1 else lang_1

	frappe.db.set_value("User", frappe.session.user, "language", new_lang)
	frappe.db.commit()

	return {"language": new_lang}
