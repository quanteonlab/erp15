import frappe
from frappe.utils import getdate
from erpnext.erpnext_integrations.ecommerce_api import tms_api as t
def run():
    dn = "MAT-DN-2026-00018"
    keep = [x.name for x in t._find_reusable_driver_day_trips("HR-DRI-2026-00001", getdate())]
    print("jose keep trips today:", keep)
    print("would move:", t._release_dns_from_other_draft_trips([dn], keep, dry_run=True))
    print("message if hard:", t._assignment_conflict_message([dn]))
