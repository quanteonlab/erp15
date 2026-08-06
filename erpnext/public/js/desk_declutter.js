// Desk decluttering:
//  1) "Toggle Developer Mode" navbar menu item (inserted once via
//     erpnext.patches.v15_0.add_toggle_dev_mode_navbar_item) that hides
//     still-experimental modules from the sidebar — see desk_declutter.scss
//     for the actual [item-name="..."] list.
//  2) A single sidebar-toggle button lives in the persistent navbar. It only
//     ever controls the *module* sidebar (the one built by
//     frappe.views.Workspace on #page-Workspaces), so it's shown/active only
//     while that page is the current one, and hidden everywhere else — the
//     per-page hamburger on every other page now reads as a filter icon
//     instead (see restyle_list_sidebar_toggle_icons below), since on list
//     views that's what it actually toggles.
//  3) "Toggle Language" navbar menu item (inserted via
//     erpnext.patches.v15_0.add_toggle_language_navbar_item) flips the
//     current user between the two languages configured in System Settings
//     (erpnext.erpnext_integrations.ecommerce_api.desk_settings).
//  4) Every breadcrumb trail always starts with a "Silk OS" crumb linking
//     home, even on pages that don't register their own breadcrumb.
frappe.provide("erpnext");

erpnext.dev_mode_storage_key = "erpnext_dev_mode";

erpnext.is_dev_mode = function () {
	return JSON.parse(localStorage.getItem(erpnext.dev_mode_storage_key) || "false");
};

erpnext.apply_dev_mode = function () {
	$(document.body).toggleClass("erpnext-non-dev-mode", !erpnext.is_dev_mode());
};

erpnext.toggle_dev_mode = function () {
	let enabled = !erpnext.is_dev_mode();
	localStorage.setItem(erpnext.dev_mode_storage_key, JSON.stringify(enabled));
	erpnext.apply_dev_mode();
	frappe.show_alert({
		message: enabled
			? __("Developer Mode enabled — experimental modules are now visible in the sidebar")
			: __("Developer Mode disabled — experimental modules are hidden from the sidebar"),
		indicator: enabled ? "orange" : "green",
	});
};

// Applied immediately (not on a DOM-ready callback) so the sidebar never
// flashes the experimental items before this runs.
erpnext.apply_dev_mode();

erpnext.toggle_language = function () {
	frappe.call({
		method: "erpnext.erpnext_integrations.ecommerce_api.desk_settings.toggle_my_language",
		freeze: true,
		callback: (r) => {
			if (r.message && r.message.language) {
				window.location.reload();
			}
		},
	});
};

(function setup_navbar_sidebar_toggle() {
	function workspace_toggle_btn() {
		return $("#page-Workspaces").find(".sidebar-toggle-btn").first();
	}

	function workspace_sidebar() {
		return $("#page-Workspaces").find(".layout-side-section").first();
	}

	function on_workspaces_page() {
		return $("#page-Workspaces").is(":visible");
	}

	function sync_icon($navbar_btn) {
		let $page_btn = workspace_toggle_btn();

		if (!$page_btn.length || !on_workspaces_page()) {
			$navbar_btn.addClass("hide");
			return;
		}
		$navbar_btn.removeClass("hide");

		let $sidebar = workspace_sidebar();
		let visible = $sidebar.length ? $sidebar.is(":visible") : true;
		$navbar_btn
			.find("use")
			.attr("href", visible ? "#es-line-sidebar-collapse" : "#es-line-sidebar-expand");
	}

	function init() {
		if (!$(".navbar-home").length) {
			// navbar hasn't rendered yet, retry shortly
			setTimeout(init, 50);
			return;
		}
		if ($(".navbar-sidebar-toggle-btn").length) return;

		let $btn = $(`
			<button type="button" class="btn-reset navbar-sidebar-toggle-btn hide" title="${__(
				"Toggle Modules Sidebar"
			)}">
				<svg class="es-icon icon-md"><use href="#es-line-sidebar-collapse"></use></svg>
			</button>
		`).insertAfter(".navbar-home");

		$btn.tooltip({ delay: { show: 600, hide: 100 } });

		$btn.on("click", () => {
			let $page_btn = workspace_toggle_btn();
			if (!$page_btn.length || !on_workspaces_page()) return;
			// Delegate to the real (CSS-hidden) per-page button so every existing
			// behaviour — mobile overlay mode, the `toggleSidebar` event, etc. —
			// keeps working exactly as core implemented it.
			$page_btn.trigger("click");
			sync_icon($btn);
		});

		$(document).on("page-change", () => sync_icon($btn));
		sync_icon($btn);
	}

	init();
})();

(function restyle_list_sidebar_toggle_icons() {
	// On list-family pages (List/Report/Kanban/…) the per-page hamburger
	// toggles the *filter* sidebar, not the module tree — swap its icon for a
	// filter glyph so it doesn't look like a duplicate of the navbar button.
	// Core re-writes the original icons' href on every click, so instead of
	// fighting that we hide the originals via CSS (desk_declutter.scss) and
	// inject one static icon of our own that core never touches.
	function restyle() {
		$('.page-container[data-page-route^="List/"] .sidebar-toggle-btn').each(function () {
			let $btn = $(this);
			if ($btn.find(".filter-icon-override").length) return;
			$btn.append(
				`<span class="filter-icon-override">${frappe.utils.icon("filter", "md")}</span>`
			);
		});
	}

	$(document).on("page-change", restyle);
	restyle();
})();

(function setup_home_breadcrumb() {
	// Bound to the global "page-change" event rather than patching
	// frappe.breadcrumbs.update(): several pages (e.g. Workspaces itself)
	// never call frappe.breadcrumbs.add()/update() at all, so patching update()
	// alone would silently never run there.
	function ensure_home_crumb() {
		let $breadcrumbs = $("#navbar-breadcrumbs");
		if (!$breadcrumbs.length || $breadcrumbs.find(".erpnext-home-breadcrumb").length) return;
		$(
			`<li class="erpnext-home-breadcrumb"><a href="/app/home">${__("Silk OS")}</a></li>`
		).prependTo($breadcrumbs);
	}

	// Core empties #navbar-breadcrumbs every time frappe.breadcrumbs.update()
	// runs (frappe.breadcrumbs.clear()), which would wipe our crumb along with
	// everything else, and also hides the row entirely when a page never
	// registered breadcrumbs — patch it (defensively; ensure_home_crumb below
	// covers this even if frappe.breadcrumbs isn't ready yet) to reassert both.
	if (frappe.breadcrumbs && frappe.breadcrumbs.update) {
		let original_update = frappe.breadcrumbs.update;
		frappe.breadcrumbs.update = function () {
			original_update.call(this);
			ensure_home_crumb();
			this.toggle(true);
		};
	}

	function init() {
		if (!$("#navbar-breadcrumbs").length) {
			setTimeout(init, 50);
			return;
		}
		ensure_home_crumb();
	}

	$(document).on("page-change", ensure_home_crumb);
	init();
})();
