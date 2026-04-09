import frappe


def execute():
    from erpnext.image_search.legacy_candidate_import import import_all_legacy_candidates

    summary = import_all_legacy_candidates(skip_existing=True)
    frappe.logger().info("Legacy image candidate import summary: %s", frappe.as_json(summary))