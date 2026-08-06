// Adds a quick-delete button next to the pencil (edit) icon on every editable
// grid row, so a row can be removed without first checking its checkbox and
// using the toolbar "Delete" button. Every GridRow fires a "grid-row-render"
// event on the form wrapper after it renders (frappe/public/js/frappe/form/
// grid_row.js), which bubbles to document — that's the extension point used
// here instead of patching the (unexposed) GridRow class itself.
(function () {
	function remove_quick_delete_button(row) {
		row.find(".btn-quick-delete-row").remove();
	}

	function on_grid_row_render(e, grid_row) {
		if (!grid_row || !grid_row.doc || !grid_row.row) return;
		if (grid_row.grid.df.cannot_delete_rows) return;

		let $row = grid_row.row;

		if (!grid_row.grid.is_editable()) {
			remove_quick_delete_button($row);
			return;
		}

		if ($row.find(".btn-quick-delete-row").length) return;

		let $edit_col = $row.find(".btn-open-row").closest(".col");
		if (!$edit_col.length) return;

		let delete_msg = __("Delete", "", "Delete grid row");
		let $delete_col = $(`
			<div class="col">
				<div class="btn-open-row btn-quick-delete-row" data-toggle="tooltip" data-placement="right" title="${delete_msg}">
					<a>${frappe.utils.icon("delete-active", "xs")}</a>
				</div>
			</div>
		`).insertAfter($edit_col);

		$delete_col.find(".btn-quick-delete-row").tooltip({ delay: { show: 600, hide: 100 } });

		$delete_col.on("click", (ev) => {
			ev.stopPropagation();
			grid_row.remove();
		});
	}

	$(document).on("grid-row-render", on_grid_row_render);
})();
