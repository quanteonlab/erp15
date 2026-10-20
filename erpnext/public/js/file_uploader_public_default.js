// Desk FileUploader defaults to private (`make_attachments_public` falsy →
// `file.private = true`). Product images and storefront assets need public URLs,
// so force the opposite default. Users can still tick "Private" per file.
//
// apps/frappe is never deployed — wrap frappe.ui.FileUploader from this bundle
// instead (see local_docs/erp-frappe-docs/01-desk-ui-customization.md).
// file_uploader.bundle.js may load after erpnext.bundle.js, so intercept
// assignment as well as wrap an already-present constructor.
(function patch_file_uploader_public_default() {
	frappe.provide("frappe.ui");

	function wrap(Original) {
		if (!Original || Original.__erpnext_public_default) {
			return Original;
		}

		class PatchedFileUploader extends Original {
			constructor(opts = {}) {
				super(Object.assign({}, opts, { make_attachments_public: true }));
			}
		}

		PatchedFileUploader.UploadOptions = Original.UploadOptions;
		PatchedFileUploader.__erpnext_public_default = true;
		return PatchedFileUploader;
	}

	let current = frappe.ui.FileUploader ? wrap(frappe.ui.FileUploader) : null;

	Object.defineProperty(frappe.ui, "FileUploader", {
		configurable: true,
		enumerable: true,
		get() {
			return current;
		},
		set(value) {
			current = wrap(value);
		},
	});
})();
