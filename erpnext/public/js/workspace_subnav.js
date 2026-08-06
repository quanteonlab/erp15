// Adds a hover-dropdown tab strip to the top of every Workspace page, mirroring
// the workspace's own "card" sections (e.g. "Stock Transactions", "Stock Reports")
// as quick-access tabs — similar to the zone nav in the nextcommerce storefront app.
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

	function get_card_groups(workspace) {
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
			if (!card || !card.links || !card.links.length) return;

			let links = card.links
				.filter((l) => !l.hidden)
				.map((l) => ({
					label: l.label || l.link_to || l.name,
					route: build_route(l),
				}))
				.filter((l) => l.route);

			if (!links.length) return;
			// A card can legitimately repeat inside a page (rare); keep first occurrence only.
			if (groups.some((g) => g.title === card_name)) return;
			groups.push({ title: card_name, links });
		});
		return groups;
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

	function render(workspace) {
		let body = workspace.body;
		if (!body || !body.length) return;

		let groups = get_card_groups(workspace);
		let $nav = body.find(".workspace-subnav");

		if (!groups.length) {
			$nav.remove();
			return;
		}

		if (!$nav.length) {
			$nav = $('<div class="workspace-subnav"></div>').prependTo(body);
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

	return { render };
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
	};
})();
