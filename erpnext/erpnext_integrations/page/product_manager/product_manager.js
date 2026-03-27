/* Product Manager Page — i016
 * Inline Excel-style editor for ERPNext Items.
 * Depends on: AG Grid Community v31 (loaded from CDN)
 *             Cropper.js v1 (loaded from CDN, for image modal)
 */

frappe.pages['product-manager'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Product Manager',
		single_column: true,
	});
	new ProductManagerPage(wrapper, page);
};

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const PM_API = 'erpnext.erpnext_integrations.ecommerce_api.api';

const COLOR_PALETTE = [
	'#EF4444','#F97316','#F59E0B','#84CC16','#22C55E',
	'#10B981','#14B8A6','#06B6D4','#3B82F6','#6366F1',
	'#8B5CF6','#A855F7','#D946EF','#EC4899','#F43F5E',
	'#64748B','#78716C','#0EA5E9','#EAB308','#737373',
];

const UOM_OPTIONS   = ['Nos', 'Kg', 'g', 'mL', 'L', 'Meter', 'Box'];
const UNIT_OPTIONS  = ['', 'G', 'ML', 'KG', 'L', 'UN'];
const LS_COLORS_CAT = 'pm_cat_colors_v1';
const LS_COLORS_BRD = 'pm_brd_colors_v1';
const LS_HIDDEN_COL = 'pm_hidden_cols_v1';

// ─────────────────────────────────────────────────────────────────────────────
// ProductManagerPage
// ─────────────────────────────────────────────────────────────────────────────

class ProductManagerPage {
	constructor(wrapper, page) {
		this.wrapper   = wrapper;
		this.page      = page;
		this.gridApi   = null;
		this.dirtyRows = new Map();   // item_code -> { field: value }
		this.colorMode = false;
		this.catColors = JSON.parse(localStorage.getItem(LS_COLORS_CAT) || '{}');
		this.brdColors = JSON.parse(localStorage.getItem(LS_COLORS_BRD) || '{}');
		this.catPalIdx = Object.keys(this.catColors).length;
		this.brdPalIdx = Object.keys(this.brdColors).length;
		this.categories   = [];
		this.filters      = { search: '', category: '', brand: '', active_only: false };
		this._syncBtn     = null;
		this._dirtyCount  = 0;

		this._init();
	}

	// ── bootstrap ─────────────────────────────────────────────────────────────

	async _init() {
		await this._loadScripts();
		this._buildToolbar();
		this._buildFilterBar();
		this._buildGridContainer();
		await this._loadCategories();
		this._initGrid();
		this._loadRows();
		window.addEventListener('beforeunload', this._onBeforeUnload.bind(this));
	}

	_loadScripts() {
		const load = (url, isStyle = false) => new Promise((resolve, reject) => {
			if (isStyle) {
				if (document.querySelector(`link[href="${url}"]`)) return resolve();
				const el = document.createElement('link');
				el.rel = 'stylesheet'; el.href = url;
				el.onload = resolve; el.onerror = reject;
				document.head.appendChild(el);
			} else {
				if (window.agGrid) return resolve();
				const el = document.createElement('script');
				el.src = url;
				el.onload = resolve; el.onerror = reject;
				document.head.appendChild(el);
			}
		});

		return Promise.all([
			load('https://cdn.jsdelivr.net/npm/ag-grid-community@31.3.2/styles/ag-grid.css', true),
			load('https://cdn.jsdelivr.net/npm/ag-grid-community@31.3.2/styles/ag-theme-alpine.css', true),
		]).then(() => load('https://cdn.jsdelivr.net/npm/ag-grid-community@31.3.2/dist/ag-grid-community.min.noStyle.js'));
	}

	// ── toolbar ───────────────────────────────────────────────────────────────

	_buildToolbar() {
		// Sync button (primary)
		this._syncBtn = this.page.set_primary_action('Sync (0)', () => this._syncDirty(), 'refresh');

		// Right-side secondary buttons
		this.page.add_button('Color Off', () => this._toggleColorMode(), { icon: 'palette' });
		this.page.add_button('Export All', () => this._exportRows(), { icon: 'download' });

		// Menu items (selection-based bulk actions)
		this.page.add_menu_item('Set Active', () => this._setBulkActive(true));
		this.page.add_menu_item('Set Inactive', () => this._setBulkActive(false));
		this.page.add_menu_item('Export Selected', () => this._exportSelected());
		this.page.add_menu_item('Reset Column Visibility', () => this._resetColumnVisibility());
	}

	_updateSyncBadge() {
		this._dirtyCount = this.dirtyRows.size;
		const btn = this.wrapper.querySelector('.page-actions .btn-primary');
		if (btn) btn.textContent = `Sync (${this._dirtyCount})`;
	}

	// ── filter bar ────────────────────────────────────────────────────────────

	_buildFilterBar() {
		const bar = document.createElement('div');
		bar.className = 'pm-filter-bar';
		bar.innerHTML = `
			<input class="pm-filter-search" type="text" placeholder="Search title, SKU, brand…" />
			<select class="pm-filter-cat"><option value="">All Categories</option></select>
			<select class="pm-filter-brand"><option value="">All Brands</option></select>
			<label class="pm-filter-active-label">
				<input type="checkbox" class="pm-filter-active" /> Active only
			</label>
			<button class="btn btn-sm btn-default pm-filter-clear">Clear</button>
		`;
		this.page.body.prepend(bar);

		this._injectStyles();

		const search = bar.querySelector('.pm-filter-search');
		const cat    = bar.querySelector('.pm-filter-cat');
		const brand  = bar.querySelector('.pm-filter-brand');
		const active = bar.querySelector('.pm-filter-active');
		const clear  = bar.querySelector('.pm-filter-clear');

		let searchTimer;
		search.addEventListener('input', () => {
			clearTimeout(searchTimer);
			searchTimer = setTimeout(() => { this.filters.search = search.value; this._loadRows(); }, 350);
		});
		cat.addEventListener('change', () => { this.filters.category = cat.value; this._loadRows(); });
		brand.addEventListener('change', () => { this.filters.brand = brand.value; this._loadRows(); });
		active.addEventListener('change', () => { this.filters.active_only = active.checked; this._loadRows(); });
		clear.addEventListener('click', () => {
			search.value = ''; cat.value = ''; brand.value = ''; active.checked = false;
			this.filters = { search: '', category: '', brand: '', active_only: false };
			this._loadRows();
		});

		this._filterCatEl   = cat;
		this._filterBrandEl = brand;
	}

	async _loadCategories() {
		const r = await frappe.call({ method: `${PM_API}.get_category_list` });
		this.categories = r.message || [];
		if (this._filterCatEl) {
			this.categories.forEach(c => {
				const o = document.createElement('option');
				o.value = o.textContent = c;
				this._filterCatEl.appendChild(o);
			});
		}
	}

	// ── grid container ────────────────────────────────────────────────────────

	_buildGridContainer() {
		const el = document.createElement('div');
		el.id = 'pm-grid';
		el.className = 'ag-theme-alpine';
		el.style.cssText = 'flex:1; min-height:0; width:100%;';
		this.page.body.appendChild(el);
		this.page.body.style.cssText = 'display:flex; flex-direction:column; height:calc(100vh - 80px); padding:0;';
	}

	// ── grid init ─────────────────────────────────────────────────────────────

	_initGrid() {
		const self = this;
		const hiddenCols = JSON.parse(localStorage.getItem(LS_HIDDEN_COL) || '[]');

		const gridOptions = {
			columnDefs: this._buildColumnDefs(hiddenCols),
			rowData: [],
			rowSelection: 'multiple',
			suppressRowClickSelection: true,
			getRowId: params => params.data.item_code,
			defaultColDef: {
				resizable: true,
				sortable: true,
				filter: true,
				editable: false,
				menuTabs: ['generalMenuTab', 'columnsMenuTab'],
			},
			rowClassRules: {
				'pm-dirty': params => !!params.data._dirty,
			},
			onCellValueChanged: params => self._onCellChanged(params),
			onColumnVisible: params => self._persistHiddenCols(),
			components: {
				imageCellRenderer: ImageCellRenderer,
				activeCellRenderer: ActiveCellRenderer,
				confidenceCellRenderer: ConfidenceCellRenderer,
				notesCellRenderer: NotesCellRenderer,
				tagsCellRenderer: TagsCellRenderer,
				tagsCellEditor: TagsCellEditor,
				timestampCellRenderer: TimestampCellRenderer,
				brandCellEditor: BrandCellEditor,
			},
		};

		this.gridApi = agGrid.createGrid(document.getElementById('pm-grid'), gridOptions);
	}

	_buildColumnDefs(hiddenCols = []) {
		const self = this;
		const hide = col => hiddenCols.includes(col);

		return [
			// ── selection
			{
				headerCheckboxSelection: true,
				checkboxSelection: true,
				width: 40, minWidth: 40,
				pinned: 'left',
				sortable: false, filter: false, editable: false, resizable: false,
				suppressSizeToFit: true, suppressMenu: true,
				cellStyle: { padding: '0 4px', display: 'flex', alignItems: 'center' },
			},
			// ── image
			{
				field: 'image', headerName: 'Img',
				width: 60, minWidth: 60, pinned: 'left',
				editable: false, sortable: false, filter: false,
				cellRenderer: 'imageCellRenderer',
				cellStyle: { padding: '2px', display: 'flex', alignItems: 'center', justifyContent: 'center' },
				hide: hide('image'),
				onCellDoubleClicked: p => self._openImageModal(p.data.item_code, p.data.image),
			},
			// ── SKU
			{
				field: 'item_code', headerName: 'SKU',
				width: 80, minWidth: 60, pinned: 'left',
				editable: false,
				cellStyle: { fontFamily: 'monospace', fontSize: '11px' },
				hide: hide('item_code'),
			},
			// ── title
			{
				field: 'item_name', headerName: 'Title',
				width: 280, minWidth: 120, editable: true,
				hide: hide('item_name'),
			},
			// ── brand
			{
				field: 'brand', headerName: 'Brand',
				width: 120, minWidth: 80, editable: true,
				cellEditor: 'brandCellEditor',
				cellStyle: p => self.colorMode ? self._brandStyle(p.value) : null,
				hide: hide('brand'),
			},
			// ── category
			{
				field: 'item_group', headerName: 'Category',
				width: 130, minWidth: 80, editable: true,
				cellEditor: 'agSelectCellEditor',
				cellEditorParams: { values: this.categories },
				cellStyle: p => self.colorMode ? self._catStyle(p.value) : null,
				hide: hide('item_group'),
			},
			// ── barcode
			{
				field: 'barcode', headerName: 'Barcode',
				width: 130, minWidth: 80, editable: true,
				cellStyle: { fontFamily: 'monospace', fontSize: '11px' },
				hide: hide('barcode'),
			},
			// ── price
			{
				field: 'list_price', headerName: 'Price',
				width: 90, minWidth: 70, editable: true,
				type: 'numericColumn',
				cellEditor: 'agNumberCellEditor',
				valueFormatter: p => p.value != null ? '$' + Number(p.value).toLocaleString('es-AR') : '',
				filter: 'agNumberColumnFilter',
				hide: hide('list_price'),
			},
			// ── stock UOM
			{
				field: 'stock_uom', headerName: 'UOM',
				width: 70, minWidth: 60, editable: true,
				cellEditor: 'agSelectCellEditor',
				cellEditorParams: { values: UOM_OPTIONS },
				headerTooltip: 'Nos → unit count | Kg → kilograms | mL → milliliters',
				hide: hide('stock_uom'),
			},
			// ── pack qty
			{
				field: 'custom_pack_qty', headerName: 'Pack Qty',
				width: 80, minWidth: 60, editable: true,
				type: 'numericColumn', cellEditor: 'agNumberCellEditor',
				hide: hide('custom_pack_qty'),
			},
			// ── pack size
			{
				field: 'custom_pack_size', headerName: 'Pack Size',
				width: 80, minWidth: 60, editable: true,
				type: 'numericColumn', cellEditor: 'agNumberCellEditor',
				hide: hide('custom_pack_size'),
			},
			// ── pack unit
			{
				field: 'custom_pack_unit', headerName: 'Unit',
				width: 65, minWidth: 50, editable: true,
				cellEditor: 'agSelectCellEditor',
				cellEditorParams: { values: UNIT_OPTIONS },
				hide: hide('custom_pack_unit'),
			},
			// ── active
			{
				field: 'is_active', headerName: 'Active',
				width: 70, minWidth: 60, editable: true,
				cellRenderer: 'activeCellRenderer',
				cellEditor: 'agCheckboxCellEditor',
				filter: false,
				hide: hide('is_active'),
			},
			// ── normalized title
			{
				field: 'custom_normalized_title', headerName: 'Normalized',
				width: 220, minWidth: 80, editable: true,
				hide: hide('custom_normalized_title'),
			},
			// ── confidence
			{
				field: 'custom_match_confidence', headerName: 'Conf',
				width: 65, minWidth: 55, editable: false,
				cellRenderer: 'confidenceCellRenderer',
				filter: 'agNumberColumnFilter',
				hide: hide('custom_match_confidence'),
			},
			// ── notes
			{
				field: 'custom_review_notes', headerName: 'Notes',
				width: 160, minWidth: 80, editable: true,
				cellRenderer: 'notesCellRenderer',
				hide: hide('custom_review_notes'),
			},
			// ── tags
			{
				field: 'tags', headerName: 'Tags',
				width: 200, minWidth: 100,
				editable: true,
				cellRenderer: 'tagsCellRenderer',
				cellEditor: 'tagsCellEditor',
				sortable: false, filter: false,
				hide: hide('tags'),
			},
			// ── synced
			{
				field: 'modified', headerName: 'Synced',
				width: 95, minWidth: 80, pinned: 'right',
				editable: false, filter: false,
				cellRenderer: 'timestampCellRenderer',
				hide: hide('modified'),
			},
		];
	}

	// ── data loading ──────────────────────────────────────────────────────────

	async _loadRows() {
		this.page.set_indicator('Loading…', 'blue');
		try {
			const r = await frappe.call({
				method: `${PM_API}.get_product_rows`,
				args: { filters: JSON.stringify(this.filters), page: 1, page_length: 300 },
			});
			const rows = (r.message || {}).rows || [];
			const total = (r.message || {}).total || 0;
			this.gridApi.setGridOption('rowData', rows);
			this.page.set_indicator(`${total} items`, 'gray');
		} catch (e) {
			this.page.set_indicator('Load error', 'red');
			frappe.msgprint({ title: 'Error loading products', message: e.message, indicator: 'red' });
		}
	}

	// ── cell change ───────────────────────────────────────────────────────────

	_onCellChanged(params) {
		const code = params.data.item_code;
		const col  = params.column.getColId();

		// Map AG Grid field → API change key
		const FIELD_MAP = {
			item_name: 'source_title',
			brand: 'brand',
			item_group: 'source_category',
			barcode: 'barcode',
			list_price: 'list_price',
			stock_uom: 'stock_uom',
			custom_pack_qty: 'pack_qty',
			custom_pack_size: 'pack_size',
			custom_pack_unit: 'unit',
			is_active: 'is_active',
			custom_normalized_title: 'normalized_title',
			custom_review_notes: 'review_notes',
			tags: 'tags',
		};

		if (!FIELD_MAP[col]) return;
		if (!this.dirtyRows.has(code)) this.dirtyRows.set(code, {});
		this.dirtyRows.get(code)[FIELD_MAP[col]] = params.newValue;

		// Mark row dirty for visual highlight
		const node = this.gridApi.getRowNode(code);
		if (node) {
			node.setDataValue('_dirty', true);
			this.gridApi.refreshCells({ rowNodes: [node], force: true });
		}
		this._updateSyncBadge();
	}

	// ── sync ──────────────────────────────────────────────────────────────────

	async _syncDirty() {
		if (this.dirtyRows.size === 0) {
			frappe.show_alert('No unsaved changes', 3);
			return;
		}
		const rows = [];
		for (const [item_code, changes] of this.dirtyRows) {
			rows.push({ item_code, changes });
		}
		this.page.set_indicator('Saving…', 'blue');
		try {
			const r = await frappe.call({
				method: `${PM_API}.save_product_rows_bulk`,
				args: { rows: JSON.stringify(rows) },
			});
			const result = r.message || {};
			// Clear dirty state per saved row
			for (const code of Object.keys(result.results || {})) {
				this.dirtyRows.delete(code);
				const node = this.gridApi.getRowNode(code);
				if (node) {
					const modified = (result.results[code] || {}).modified;
					if (modified) node.setDataValue('modified', modified);
					node.setDataValue('_dirty', false);
					this.gridApi.refreshCells({ rowNodes: [node], force: true });
				}
			}
			if ((result.errors || []).length) {
				frappe.msgprint({
					title: 'Some rows failed',
					message: result.errors.map(e => `${e.item_code}: ${e.error}`).join('<br>'),
					indicator: 'orange',
				});
			}
			this._updateSyncBadge();
			this.page.set_indicator('Saved', 'green');
		} catch (e) {
			this.page.set_indicator('Save error', 'red');
			frappe.msgprint({ title: 'Sync failed', message: e.message, indicator: 'red' });
		}
	}

	// ── bulk actions ──────────────────────────────────────────────────────────

	_getSelectedCodes() {
		return this.gridApi.getSelectedRows().map(r => r.item_code);
	}

	async _setBulkActive(is_active) {
		const codes = this._getSelectedCodes();
		if (!codes.length) { frappe.show_alert('Select rows first', 3); return; }
		await frappe.call({
			method: `${PM_API}.set_items_active_bulk`,
			args: { item_codes: JSON.stringify(codes), is_active },
		});
		codes.forEach(code => {
			const node = this.gridApi.getRowNode(code);
			if (node) {
				node.setDataValue('is_active', is_active ? 1 : 0);
				node.setDataValue('disabled', is_active ? 0 : 1);
			}
		});
		frappe.show_alert({ message: `${codes.length} items updated`, indicator: 'green' }, 3);
	}

	async _exportRows() {
		const r = await frappe.call({
			method: `${PM_API}.export_product_rows`,
			args: { filters: JSON.stringify(this.filters) },
		});
		const csv = r.message || '';
		const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url; a.download = 'product_manager_export.csv';
		a.click();
		URL.revokeObjectURL(url);
	}

	_exportSelected() {
		this.gridApi.exportDataAsCsv({ onlySelected: true, fileName: 'product_manager_selected.csv' });
	}

	// ── color mode ────────────────────────────────────────────────────────────

	_toggleColorMode() {
		this.colorMode = !this.colorMode;
		const btn = this.wrapper.querySelector('.page-actions .btn-default');
		if (btn) btn.textContent = this.colorMode ? 'Color On' : 'Color Off';
		this.gridApi.refreshCells({ force: true });
	}

	_assignColor(map, key, storageKey, idxRef) {
		if (!key) return null;
		if (!map[key]) {
			map[key] = COLOR_PALETTE[idxRef % COLOR_PALETTE.length];
			idxRef++;
			localStorage.setItem(storageKey, JSON.stringify(map));
		}
		return map[key];
	}

	_catStyle(value) {
		const hex = this._assignColor(this.catColors, value, LS_COLORS_CAT, this.catPalIdx++);
		if (!hex) return null;
		return { backgroundColor: hex + '28', borderLeft: `3px solid ${hex}` };
	}

	_brandStyle(value) {
		const hex = this._assignColor(this.brdColors, value, LS_COLORS_BRD, this.brdPalIdx++);
		if (!hex) return null;
		return { backgroundColor: hex + '1A' };
	}

	// ── column visibility persistence ─────────────────────────────────────────

	_persistHiddenCols() {
		const hidden = [];
		this.gridApi.getColumns().forEach(col => {
			if (!col.isVisible()) hidden.push(col.getColId());
		});
		localStorage.setItem(LS_HIDDEN_COL, JSON.stringify(hidden));
	}

	_resetColumnVisibility() {
		this.gridApi.getColumns().forEach(col => {
			if (!col.isVisible()) this.gridApi.setColumnVisible(col.getColId(), true);
		});
		localStorage.removeItem(LS_HIDDEN_COL);
	}

	// ── image modal ───────────────────────────────────────────────────────────

	_openImageModal(item_code, image_url) {
		const erpBase = (frappe.boot && frappe.boot.siteUrl)
			? frappe.boot.siteUrl.replace(/\/$/, '')
			: window.location.origin;
		const imgSrc = image_url
			? (image_url.startsWith('http') ? image_url : erpBase + image_url)
			: null;

		const d = new frappe.ui.Dialog({
			title: `Image — ${item_code}`,
			size: 'large',
			fields: [{ fieldtype: 'HTML', fieldname: 'body_html' }],
		});

		const body = d.get_field('body_html').$wrapper[0];
		body.innerHTML = `
			<div class="pm-img-modal-wrap">
				<div class="pm-img-preview-area">
					${imgSrc
						? `<img id="pm-crop-img" src="${imgSrc}" style="max-width:100%;max-height:400px;" />`
						: `<div class="pm-img-placeholder">No image — upload one below</div>`}
				</div>
				<div class="pm-img-actions">
					<label class="btn btn-sm btn-default">
						Replace image <input type="file" id="pm-img-file" accept="image/*" style="display:none;" />
					</label>
					<button class="btn btn-sm btn-primary" id="pm-img-save">Save as 200×200</button>
					<span class="pm-img-note">Double-click the image to activate crop handles</span>
				</div>
			</div>
		`;

		let cropper = null;
		const imgEl = body.querySelector('#pm-crop-img');
		if (imgEl) {
			this._ensureCropperJs().then(() => {
				imgEl.addEventListener('dblclick', () => {
					if (cropper) return;
					cropper = new Cropper(imgEl, {
						aspectRatio: NaN,
						viewMode: 1,
						autoCropArea: 1,
					});
				});
			});
		}

		body.querySelector('#pm-img-file').addEventListener('change', async (e) => {
			const file = e.target.files[0];
			if (!file) return;
			const reader = new FileReader();
			reader.onload = ev => {
				if (cropper) { cropper.destroy(); cropper = null; }
				if (imgEl) {
					imgEl.src = ev.target.result;
				} else {
					const img = document.createElement('img');
					img.id = 'pm-crop-img';
					img.src = ev.target.result;
					img.style.cssText = 'max-width:100%;max-height:400px;';
					body.querySelector('.pm-img-preview-area').replaceChildren(img);
				}
			};
			reader.readAsDataURL(file);
		});

		body.querySelector('#pm-img-save').addEventListener('click', async () => {
			const currentImg = body.querySelector('#pm-crop-img');
			if (!currentImg) { frappe.show_alert('No image to save', 3); return; }

			const canvas = document.createElement('canvas');
			canvas.width = 200; canvas.height = 200;
			const ctx = canvas.getContext('2d');

			if (cropper) {
				const croppedCanvas = cropper.getCroppedCanvas({ width: 200, height: 200 });
				ctx.drawImage(croppedCanvas, 0, 0, 200, 200);
			} else {
				ctx.drawImage(currentImg, 0, 0, 200, 200);
			}

			canvas.toBlob(async (blob) => {
				const reader = new FileReader();
				reader.onload = async ev => {
					const b64 = ev.target.result.split(',')[1];
					const fname = `${item_code}.jpg`;
					const r = await frappe.call({
						method: `${PM_API}.import_catalog_image_batch`,
						args: { images: JSON.stringify([{ file_name: fname, content_base64: b64 }]) },
					});
					const msg = r.message || {};
					if ((msg.updated_items || 0) > 0) {
						const newUrl = `/files/${fname}`;
						const node = this.gridApi.getRowNode(item_code);
						if (node) {
							node.setDataValue('image', newUrl);
							this.gridApi.refreshCells({ rowNodes: [node], columns: ['image'], force: true });
						}
						frappe.show_alert({ message: 'Image saved', indicator: 'green' }, 3);
						d.hide();
					} else {
						frappe.show_alert({ message: 'Image upload failed', indicator: 'red' }, 4);
					}
				};
				reader.readAsDataURL(blob);
			}, 'image/jpeg', 0.85);
		});

		d.show();
	}

	_ensureCropperJs() {
		if (window.Cropper) return Promise.resolve();
		return new Promise((resolve, reject) => {
			const link = document.createElement('link');
			link.rel = 'stylesheet';
			link.href = 'https://cdn.jsdelivr.net/npm/cropperjs@1.6.1/dist/cropper.min.css';
			document.head.appendChild(link);

			const script = document.createElement('script');
			script.src = 'https://cdn.jsdelivr.net/npm/cropperjs@1.6.1/dist/cropper.min.js';
			script.onload = resolve; script.onerror = reject;
			document.head.appendChild(script);
		});
	}

	// ── unload guard ──────────────────────────────────────────────────────────

	_onBeforeUnload(e) {
		if (this.dirtyRows.size > 0) {
			e.preventDefault();
			e.returnValue = '';
		}
	}

	// ── styles ────────────────────────────────────────────────────────────────

	_injectStyles() {
		if (document.getElementById('pm-styles')) return;
		const style = document.createElement('style');
		style.id = 'pm-styles';
		style.textContent = `
			.pm-filter-bar {
				display: flex; gap: 8px; align-items: center;
				padding: 8px 12px; background: #f8fafc;
				border-bottom: 1px solid #e2e8f0; flex-shrink: 0;
			}
			.pm-filter-bar input[type=text], .pm-filter-bar select {
				padding: 4px 8px; font-size: 12px;
				border: 1px solid #cbd5e1; border-radius: 4px;
				background: #fff; height: 28px;
			}
			.pm-filter-search { width: 220px; }
			.pm-filter-cat, .pm-filter-brand { width: 140px; }
			.pm-filter-active-label { font-size: 12px; display: flex; gap: 4px; align-items: center; color: #475569; white-space: nowrap; }
			.pm-dirty { border-left: 3px solid #f59e0b !important; }
			.pm-thumb { width: 36px; height: 36px; object-fit: contain; border-radius: 3px; background: #f1f5f9; }
			.pm-thumb-placeholder { width: 36px; height: 36px; background: #e2e8f0; border-radius: 3px; }
			.pm-active-badge {
				display: inline-block; padding: 1px 6px; border-radius: 10px; font-size: 10px; font-weight: 600;
			}
			.pm-active-badge.on  { background: #dcfce7; color: #16a34a; }
			.pm-active-badge.off { background: #fee2e2; color: #dc2626; }
			.pm-conf-bar { height: 6px; border-radius: 3px; margin-top: 2px; }
			.pm-note-chip {
				display: inline-block; background: #fef3c7; color: #92400e;
				border-radius: 4px; padding: 1px 5px; font-size: 10px; max-width: 140px;
				overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
			}
			.pm-tag { display: inline-block; background: #e0f2fe; color: #0369a1; border-radius: 10px; padding: 1px 7px; font-size: 10px; margin: 1px 2px; }
			.pm-tag-rm { cursor: pointer; margin-left: 3px; opacity: 0.6; }
			.pm-tag-rm:hover { opacity: 1; }
			.pm-tags-editor {
				background: #fff; border: 1px solid #94a3b8; border-radius: 4px;
				padding: 4px 6px; min-width: 180px; display: flex; flex-wrap: wrap;
				gap: 3px; align-items: center; box-shadow: 0 2px 8px rgba(0,0,0,.1);
			}
			.pm-tags-editor input { border: none; outline: none; font-size: 12px; min-width: 60px; flex: 1; }
			.pm-img-modal-wrap { padding: 8px; }
			.pm-img-preview-area { display: flex; justify-content: center; margin-bottom: 12px; min-height: 120px; background: #f8fafc; border-radius: 6px; padding: 8px; }
			.pm-img-placeholder { color: #94a3b8; font-size: 13px; display: flex; align-items: center; }
			.pm-img-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
			.pm-img-note { font-size: 11px; color: #94a3b8; }
			.pm-brand-editor { background:#fff; border:1px solid #94a3b8; border-radius:4px; min-width:140px; box-shadow:0 2px 8px rgba(0,0,0,.1); overflow:hidden; }
			.pm-brand-editor input { width:100%; border:none; outline:none; padding:5px 8px; font-size:12px; }
			.pm-brand-suggestions { max-height: 160px; overflow-y: auto; }
			.pm-brand-suggestion { padding: 5px 10px; font-size:12px; cursor:pointer; }
			.pm-brand-suggestion:hover, .pm-brand-suggestion.active { background:#e0f2fe; }
			.pm-brand-suggestion.create { color: #2563eb; font-style: italic; }
		`;
		document.head.appendChild(style);
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Cell Renderers
// ─────────────────────────────────────────────────────────────────────────────

function ImageCellRenderer() {}
ImageCellRenderer.prototype.init = function (params) {
	this.eGui = document.createElement('div');
	const erpBase = (frappe.boot && frappe.boot.siteUrl)
		? frappe.boot.siteUrl.replace(/\/$/, '') : window.location.origin;
	const src = params.value
		? (params.value.startsWith('http') ? params.value : erpBase + params.value)
		: null;
	this.eGui.innerHTML = src
		? `<img class="pm-thumb" src="${src}" alt="" />`
		: `<div class="pm-thumb-placeholder"></div>`;
};
ImageCellRenderer.prototype.getGui = function () { return this.eGui; };
ImageCellRenderer.prototype.refresh = function (params) {
	const erpBase = (frappe.boot && frappe.boot.siteUrl)
		? frappe.boot.siteUrl.replace(/\/$/, '') : window.location.origin;
	const src = params.value
		? (params.value.startsWith('http') ? params.value : erpBase + params.value)
		: null;
	const img = this.eGui.querySelector('img');
	if (img && src) { img.src = src; return true; }
	return false;
};

function ActiveCellRenderer() {}
ActiveCellRenderer.prototype.init = function (params) {
	this.eGui = document.createElement('span');
	this._render(params.value);
};
ActiveCellRenderer.prototype._render = function (val) {
	const on = val == 1 || val === true;
	this.eGui.className = `pm-active-badge ${on ? 'on' : 'off'}`;
	this.eGui.textContent = on ? 'Active' : 'Inactive';
};
ActiveCellRenderer.prototype.getGui = function () { return this.eGui; };
ActiveCellRenderer.prototype.refresh = function (p) { this._render(p.value); return true; };

function ConfidenceCellRenderer() {}
ConfidenceCellRenderer.prototype.init = function (params) {
	this.eGui = document.createElement('div');
	this._render(params.value);
};
ConfidenceCellRenderer.prototype._render = function (val) {
	const pct = val != null ? Math.round(val * 100) : null;
	if (pct == null) { this.eGui.textContent = '—'; return; }
	const hue = Math.round(pct * 1.2); // 0=red 100=green (roughly)
	this.eGui.innerHTML = `
		<span style="font-size:11px;">${pct}%</span>
		<div class="pm-conf-bar" style="width:${pct}%; background: hsl(${hue},70%,50%);"></div>
	`;
};
ConfidenceCellRenderer.prototype.getGui = function () { return this.eGui; };
ConfidenceCellRenderer.prototype.refresh = function (p) { this._render(p.value); return true; };

function NotesCellRenderer() {}
NotesCellRenderer.prototype.init = function (params) {
	this.eGui = document.createElement('div');
	this._render(params.value);
};
NotesCellRenderer.prototype._render = function (val) {
	this.eGui.innerHTML = val
		? `<span class="pm-note-chip" title="${val}">${val}</span>` : '';
};
NotesCellRenderer.prototype.getGui = function () { return this.eGui; };
NotesCellRenderer.prototype.refresh = function (p) { this._render(p.value); return true; };

function TagsCellRenderer() {}
TagsCellRenderer.prototype.init = function (params) {
	this.eGui = document.createElement('div');
	this.eGui.style.cssText = 'display:flex;flex-wrap:wrap;gap:2px;padding:2px 0;align-items:center;';
	this._render(params.value);
};
TagsCellRenderer.prototype._render = function (tags) {
	this.eGui.innerHTML = (tags || []).map(t => `<span class="pm-tag">${t}</span>`).join('');
};
TagsCellRenderer.prototype.getGui = function () { return this.eGui; };
TagsCellRenderer.prototype.refresh = function (p) { this._render(p.value); return true; };

function TimestampCellRenderer() {}
TimestampCellRenderer.prototype.init = function (params) {
	this.eGui = document.createElement('span');
	this.eGui.style.cssText = 'font-size:10px;color:#94a3b8;';
	this._render(params.value);
};
TimestampCellRenderer.prototype._render = function (val) {
	if (!val) { this.eGui.textContent = '—'; return; }
	const d = new Date(val);
	if (isNaN(d)) { this.eGui.textContent = '—'; return; }
	const diff = Date.now() - d.getTime();
	const mins  = Math.floor(diff / 60000);
	const hours = Math.floor(mins / 60);
	const days  = Math.floor(hours / 24);
	let label;
	if (mins < 2)       label = 'just now';
	else if (mins < 60) label = `${mins}m ago`;
	else if (hours < 24) label = `${hours}h ago`;
	else                label = `${days}d ago`;
	this.eGui.textContent = label;
	this.eGui.title = d.toLocaleString();
};
TimestampCellRenderer.prototype.getGui = function () { return this.eGui; };
TimestampCellRenderer.prototype.refresh = function (p) { this._render(p.value); return true; };

// ─────────────────────────────────────────────────────────────────────────────
// Cell Editors
// ─────────────────────────────────────────────────────────────────────────────

// ── Tags Editor ───────────────────────────────────────────────────────────────

function TagsCellEditor() {}
TagsCellEditor.prototype.init = function (params) {
	this._tags  = [...(params.value || [])];
	this._eGui  = document.createElement('div');
	this._eGui.className = 'pm-tags-editor';
	this._render();
};
TagsCellEditor.prototype._render = function () {
	this._eGui.innerHTML = '';
	this._tags.forEach((tag, i) => {
		const chip = document.createElement('span');
		chip.className = 'pm-tag';
		chip.innerHTML = `${tag}<span class="pm-tag-rm" data-i="${i}">×</span>`;
		chip.querySelector('.pm-tag-rm').addEventListener('click', () => {
			this._tags.splice(i, 1);
			this._render();
		});
		this._eGui.appendChild(chip);
	});
	const inp = document.createElement('input');
	inp.placeholder = 'add tag…';
	inp.addEventListener('keydown', e => {
		if (e.key === 'Enter' || e.key === ',') {
			e.preventDefault();
			const v = inp.value.trim().replace(/,/g, '');
			if (v && !this._tags.includes(v)) { this._tags.push(v); this._render(); }
			else inp.value = '';
		}
		if (e.key === 'Backspace' && !inp.value && this._tags.length) {
			this._tags.pop(); this._render();
		}
		if (e.key === 'Escape') { this._eGui.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); }
	});
	this._eGui.appendChild(inp);
	setTimeout(() => inp.focus(), 0);
};
TagsCellEditor.prototype.getGui      = function () { return this._eGui; };
TagsCellEditor.prototype.getValue    = function () { return this._tags; };
TagsCellEditor.prototype.isPopup     = function () { return true; };
TagsCellEditor.prototype.getPopupPosition = function () { return 'under'; };
TagsCellEditor.prototype.isCancelBeforeStart = function () { return false; };

// ── Brand Editor (text + autocomplete dropdown) ───────────────────────────────

function BrandCellEditor() {}
BrandCellEditor.prototype.init = function (params) {
	this._value = params.value || '';
	this._eGui  = document.createElement('div');
	this._eGui.className = 'pm-brand-editor';
	this._inp   = document.createElement('input');
	this._inp.value = this._value;
	this._drop  = document.createElement('div');
	this._drop.className = 'pm-brand-suggestions';
	this._eGui.appendChild(this._inp);
	this._eGui.appendChild(this._drop);
	this._active = -1;

	let timer;
	this._inp.addEventListener('input', () => {
		this._value = this._inp.value;
		clearTimeout(timer);
		timer = setTimeout(() => this._suggest(this._inp.value), 280);
	});
	this._inp.addEventListener('keydown', e => {
		const items = this._drop.querySelectorAll('.pm-brand-suggestion');
		if (e.key === 'ArrowDown') { this._active = Math.min(this._active + 1, items.length - 1); this._highlight(items); e.preventDefault(); }
		else if (e.key === 'ArrowUp') { this._active = Math.max(this._active - 1, 0); this._highlight(items); e.preventDefault(); }
		else if (e.key === 'Enter') {
			if (this._active >= 0 && items[this._active]) { items[this._active].click(); }
			e.stopPropagation();
		}
		else if (e.key === 'Escape') { this._drop.innerHTML = ''; }
	});
	setTimeout(() => { this._inp.focus(); this._inp.select(); }, 0);
};
BrandCellEditor.prototype._suggest = async function (q) {
	const r = await frappe.call({ method: `${PM_API}.get_brand_suggestions`, args: { query: q } });
	const names = r.message || [];
	this._drop.innerHTML = '';
	this._active = -1;
	const exact = names.map(n => n.toLowerCase()).includes((q || '').toLowerCase());
	if (!exact && q) {
		const create = document.createElement('div');
		create.className = 'pm-brand-suggestion create';
		create.textContent = `+ Create "${q}"`;
		create.addEventListener('click', () => { this._value = q; this._inp.value = q; this._drop.innerHTML = ''; });
		this._drop.appendChild(create);
	}
	names.forEach(name => {
		const el = document.createElement('div');
		el.className = 'pm-brand-suggestion';
		el.textContent = name;
		el.addEventListener('click', () => { this._value = name; this._inp.value = name; this._drop.innerHTML = ''; });
		this._drop.appendChild(el);
	});
};
BrandCellEditor.prototype._highlight = function (items) {
	items.forEach((el, i) => el.classList.toggle('active', i === this._active));
};
BrandCellEditor.prototype.getGui   = function () { return this._eGui; };
BrandCellEditor.prototype.getValue = function () { return this._inp.value || this._value; };
BrandCellEditor.prototype.isPopup  = function () { return true; };
BrandCellEditor.prototype.getPopupPosition = function () { return 'under'; };
BrandCellEditor.prototype.isCancelBeforeStart = function () { return false; };
