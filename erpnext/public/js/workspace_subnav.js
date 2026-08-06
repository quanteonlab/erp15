// Adds a hover-dropdown tab strip to the top of every Workspace page, mirroring
// the workspace's own "card" sections (e.g. "Stock Transactions", "Stock Reports")
// as quick-access tabs — similar to the zone nav in the nextcommerce storefront app.
//
// The same tab strip is also shown on that workspace's own list-view pages
// (e.g. "Stock"'s tabs on the "Price List" list view) — see
// render_for_list_view / patch_list_view below — so the parent workspace's
// navigation stays reachable without going back to the workspace page first.
frappe.provide("erpnext.workspace_subnav");

erpnext.workspace_subnav = (function () {
	function build_route(link) {
		let link_type = (link.link_type || "").toLowerCase();
		let opts = {
			name: link.link_to,
			type: link.link_type,
			doctype: link.doctype,
			is_query_report: link.is_query_report,
			report_ref_doctype: link.report_ref_doctype,
		};
		if (link_type === "report" && !link.is_query_report) {
			opts.doctype = link.dependencies;
		}
		if (!opts.type || !opts.name) return null;
		try {
			return frappe.utils.generate_route(opts);
		} catch (e) {
			return null;
		}
	}

	function links_to_group(label, links) {
		let mapped = (links || [])
			.filter((l) => !l.hidden)
			.map((l) => ({
				label: l.label || l.link_to || l.name,
				route: build_route(l),
			}))
			.filter((l) => l.route);
		return mapped.length ? { title: label, links: mapped } : null;
	}

	function get_card_groups(workspace) {
		// Workspace page: walk `content` blocks so we mirror the page's own
		// card order, and pick up the "Custom Documents"/"Custom Reports"
		// synthetic cards the page injects (see add_custom_cards_in_content
		// in frappe's workspace.js).
		let content = workspace.content || [];
		let card_items =
			(workspace.page_data && workspace.page_data.cards && workspace.page_data.cards.items) || [];

		let groups = [];
		content.forEach((block) => {
			if (!block || block.type !== "card" || !block.data || !block.data.card_name) return;

			let card_name = block.data.card_name;
			let card = card_items.find(
				(c) =>
					frappe.utils.unescape_html(c.label || "") ===
					frappe.utils.unescape_html(card_name)
			);
			if (!card) return;

			// A card can legitimately repeat inside a page (rare); keep first occurrence only.
			if (groups.some((g) => g.title === card_name)) return;

			let group = links_to_group(card_name, card.links);
			if (group) groups.push(group);
		});
		return groups;
	}

	function groups_from_cards(cards) {
		// List-view page: no `content` block layout to walk (we never loaded
		// the workspace page itself), so just use the cards as the server
		// returns them — already in the workspace's own link order.
		return (cards || []).map((c) => links_to_group(c.label, c.links)).filter(Boolean);
	}

	function close_all($nav) {
		$nav.find(".workspace-subnav-tab.open").removeClass("open");
	}

	// Bound once: closes whichever subnav is currently on screen when the
	// user clicks outside of it. Queried live so it keeps working across
	// workspace switches instead of closing over a stale, detached $nav.
	$(document).on("click.workspace-subnav", (e) => {
		if ($(e.target).closest(".workspace-subnav-tab").length) return;
		close_all($(".workspace-subnav"));
	});

	function render_groups($body, groups) {
		if (!$body || !$body.length) return;

		let $nav = $body.find(".workspace-subnav");

		if (!groups.length) {
			$nav.remove();
			return;
		}

		if (!$nav.length) {
			$nav = $('<div class="workspace-subnav"></div>').prependTo($body);
		}
		$nav.empty();

		groups.forEach((group) => {
			let $tab = $(`
				<div class="workspace-subnav-tab">
					<button type="button" class="workspace-subnav-tab-btn">
						<span>${frappe.utils.escape_html(__(group.title))}</span>
						${frappe.utils.icon("es-line-down", "xs")}
					</button>
					<div class="workspace-subnav-menu"></div>
				</div>
			`).appendTo($nav);

			let $menu = $tab.find(".workspace-subnav-menu");
			group.links.forEach((link) => {
				$(`
					<a href="${link.route}" class="workspace-subnav-item">
						${frappe.utils.escape_html(__(link.label))}
					</a>
				`)
					.appendTo($menu)
					.on("click", () => close_all($nav));
			});

			$tab.on("mouseenter", () => {
				close_all($nav);
				$tab.addClass("open");
			});
			$tab.on("mouseleave", () => $tab.removeClass("open"));
			$tab.find(".workspace-subnav-tab-btn").on("click", (e) => {
				e.stopPropagation();
				let was_open = $tab.hasClass("open");
				close_all($nav);
				$tab.toggleClass("open", !was_open);
			});
		});
	}

	function render(workspace) {
		render_groups(workspace.body, get_card_groups(workspace));
	}

	// Section headers like "Your Shortcuts" / "Reports & Masters" are just
	// redundant now that the subnav tabs above already label each section —
	// and the shortcuts themselves read better living right under the tabs
	// instead of wherever the page's own block order happens to put them
	// (often below a chart/onboarding banner). Matched by rendered text/DOM
	// shape rather than editing each workspace's stored content, since this
	// is presentation-only: editing the underlying blocks would touch live
	// workspace records and still needs to survive re-edits in the workspace
	// customizer.
	let SHORTCUT_HEADINGS = ["your shortcuts", "shortcuts", "quick access"];
	let MASTERS_HEADINGS = ["reports & masters", "masters & reports", "reports and masters", "masters and reports"];

	function declutter_body(workspace, attempt = 0) {
		let $body = workspace.body;
		if (!$body || !$body.length) return;

		let $blocks = $body.find(".codex-editor__redactor > .ce-block");
		if (!$blocks.length) {
			// EditorJS renders blocks asynchronously after show_page resolves;
			// keep polling briefly rather than acting on an empty page.
			if (attempt < 40) setTimeout(() => declutter_body(workspace, attempt + 1), 50);
			return;
		}

		$blocks.each(function () {
			let $header = $(this).find(".ce-header").first();
			if (!$header.length) return;
			let text = ($header.text() || "").trim().toLowerCase();
			if (SHORTCUT_HEADINGS.includes(text) || MASTERS_HEADINGS.includes(text)) {
				$(this).remove();
			}
		});

		let $shortcut_blocks = $body
			.find(".codex-editor__redactor > .ce-block")
			.filter(function () {
				return $(this).find(".shortcut-widget-box").length > 0;
			});
		if (!$shortcut_blocks.length) return;

		let $nav = $body.find(".workspace-subnav");
		if (!$nav.length) return;

		let $group = $body.find(".workspace-relocated-shortcuts");
		if (!$group.length) {
			$group = $('<div class="workspace-relocated-shortcuts"></div>').insertAfter($nav);
		} else {
			$group.insertAfter($nav).empty();
		}
		$shortcut_blocks.appendTo($group);
	}

	let workspace_cards_cache = {};

	function workspace_name_for_module(module) {
		let names = frappe.boot.module_wise_workspaces && frappe.boot.module_wise_workspaces[module];
		return (names && names[0]) || null;
	}

	function render_for_list_view(list_view) {
		let doctype = list_view && list_view.doctype;
		let meta = doctype && frappe.get_meta(doctype);
		let module = meta && meta.module;
		let workspace_name = module && workspace_name_for_module(module);
		if (!workspace_name) return;

		let $body = list_view.page && list_view.page.main;
		if (!$body || !$body.length) return;

		if (workspace_cards_cache[workspace_name]) {
			render_groups($body, groups_from_cards(workspace_cards_cache[workspace_name]));
			return;
		}

		frappe
			.call("frappe.desk.desktop.get_desktop_page", {
				page: { name: workspace_name, public: 1 },
			})
			.then((r) => {
				let cards = ((r.message && r.message.cards) || {}).items || [];
				workspace_cards_cache[workspace_name] = cards;
				render_groups($body, groups_from_cards(cards));
			});
	}

	return { render, render_for_list_view, declutter_body };
})();

(function patch_workspace_view() {
	if (!(frappe.views && frappe.views.Workspace)) {
		// frappe's core desk bundle hasn't finished evaluating yet, retry shortly.
		setTimeout(patch_workspace_view, 50);
		return;
	}

	let original_show_page = frappe.views.Workspace.prototype.show_page;
	frappe.views.Workspace.prototype.show_page = async function (page) {
		await original_show_page.call(this, page);
		erpnext.workspace_subnav.render(this);
		erpnext.workspace_subnav.declutter_body(this);
	};
})();

(function patch_list_view() {
	if (!(frappe.views && frappe.views.ListView)) {
		setTimeout(patch_list_view, 50);
		return;
	}

	let original_setup_page_head = frappe.views.ListView.prototype.setup_page_head;
	frappe.views.ListView.prototype.setup_page_head = function () {
		original_setup_page_head.call(this);
		erpnext.workspace_subnav.render_for_list_view(this);
	};
})();
