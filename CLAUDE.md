# Claude Instructions (erpnext custom app)

Follow bench-level instructions from:
- `/root/code/frappe-bench-custom-test/CLAUDE.md`

## Critical rule for this repo

**Every `frappe.get_all` and `frappe.get_doc` call MUST pass `ignore_permissions=True`.**

Without it, reads silently return empty results when called via API key auth,
even though writes with `ignore_permissions=True` succeed. This causes the
classic symptom: save returns success + a document ID, but subsequent reads
return nothing.

See the bench CLAUDE.md for the full pattern and checklist.
