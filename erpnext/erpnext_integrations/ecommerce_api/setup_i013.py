"""
i013 — Bazar Intelligence Dashboards
Setup script: creates custom fields, Query Reports, Number Cards,
Dashboard Charts, and the Bazar Intelligence workspace.

Run:
  bench --site dev_site_a execute \
    erpnext.erpnext_integrations.ecommerce_api.setup_i013.run
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


# ─── Custom Fields ────────────────────────────────────────────────────────────

def add_custom_fields():
	"""Add account mode (white/black) to Sales Invoice."""
	create_custom_fields(
		{
			"Sales Invoice": [
				{
					"fieldname": "custom_account_mode",
					"fieldtype": "Select",
					"label": "Modo de cuenta",
					"options": "\nwhite\nblack",
					"insert_after": "company",
					"in_list_view": 0,
					"in_standard_filter": 1,
					"default": "white",
				}
			]
		},
		ignore_validate=True,
	)
	print("  ✓ Custom fields: Sales Invoice.custom_account_mode")


# ─── Query Reports ────────────────────────────────────────────────────────────

REPORTS = [
	# ── 1. White / Black Control ──────────────────────────────────────────────
	{
		"name": "Bazar White Black Control",
		"ref_doctype": "Sales Invoice",
		"description": "Ventas por modo de cuenta (white/black): ingresos, tickets, ticket promedio",
		"query": """
SELECT
    COALESCE(NULLIF(si.custom_account_mode, ''), 'white')  AS account_mode,
    COUNT(DISTINCT si.name)                                AS invoice_count,
    ROUND(SUM(si.grand_total), 2)                          AS total_revenue,
    ROUND(AVG(si.grand_total), 2)                          AS avg_ticket,
    ROUND(SUM(si.grand_total) * 100.0
          / NULLIF(SUM(SUM(si.grand_total)) OVER (), 0), 1)  AS revenue_share_pct
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
  AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
GROUP BY account_mode
ORDER BY total_revenue DESC
""",
		"js": """
frappe.query_reports["Bazar White Black Control"] = {
    filters: [
        {
            fieldname: "from_date", label: __("Desde"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.add_days(frappe.datetime.get_today(), -30)
        },
        {
            fieldname: "to_date", label: __("Hasta"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.get_today()
        }
    ],
    formatter(value, row, column, data, default_formatter) {
        if (column.fieldname === "account_mode") {
            const color = value === "black" ? "#e03" : "#0a0";
            return `<b style="color:${color}">${value || "white"}</b>`;
        }
        if (column.fieldname === "revenue_share_pct") {
            return `<span>${value}%</span>`;
        }
        return default_formatter(value, row, column, data);
    }
};
""",
	},

	# ── 2. Product Success & Rotation ─────────────────────────────────────────
	{
		"name": "Bazar Product Success",
		"ref_doctype": "Sales Invoice Item",
		"description": "Top productos por ingresos, unidades y velocidad de venta",
		"query": """
SELECT
    sii.item_code,
    sii.item_name,
    i.item_group,
    SUM(sii.qty)                                                           AS units_sold,
    ROUND(SUM(sii.net_amount), 2)                                          AS revenue,
    COUNT(DISTINCT si.name)                                                AS invoices,
    ROUND(AVG(sii.net_rate), 2)                                            AS avg_price,
    ROUND(SUM(sii.qty) / NULLIF(DATEDIFF(%(to_date)s, %(from_date)s), 0), 2) AS daily_velocity,
    MAX(si.posting_date)                                                   AS last_sold,
    COALESCE(b.actual_qty, 0)                                              AS stock_now
FROM `tabSales Invoice Item` sii
INNER JOIN `tabSales Invoice` si  ON si.name = sii.parent
INNER JOIN `tabItem` i            ON i.item_code = sii.item_code
LEFT  JOIN `tabBin` b             ON b.item_code = sii.item_code
                                  AND b.warehouse = %(warehouse)s
WHERE si.docstatus = 1
  AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
GROUP BY sii.item_code, sii.item_name, i.item_group
ORDER BY revenue DESC
LIMIT 100
""",
		"js": """
frappe.query_reports["Bazar Product Success"] = {
    filters: [
        {
            fieldname: "from_date", label: __("Desde"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.add_days(frappe.datetime.get_today(), -30)
        },
        {
            fieldname: "to_date", label: __("Hasta"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.get_today()
        },
        {
            fieldname: "warehouse", label: __("Bodega"), fieldtype: "Link",
            options: "Warehouse", default: "POSNET Stores - L"
        },
        {
            fieldname: "item_group", label: __("Grupo"), fieldtype: "Link",
            options: "Item Group", default: ""
        }
    ],
    formatter(value, row, column, data, default_formatter) {
        if (column.fieldname === "daily_velocity") {
            if (value >= 3) return `<b style="color:#0a0">${value}</b>`;
            if (value < 0.3) return `<span style="color:#e03">${value}</span>`;
        }
        if (column.fieldname === "stock_now" && value <= 0) {
            return `<b style="color:#e03">0</b>`;
        }
        return default_formatter(value, row, column, data);
    }
};
""",
	},

	# ── 3. Replenishment & Risk ────────────────────────────────────────────────
	{
		"name": "Bazar Replenishment Risk",
		"ref_doctype": "Item",
		"description": "Ítems con riesgo de quiebre de stock ordenados por urgencia",
		"query": """
SELECT
    i.item_code,
    i.item_name,
    i.item_group,
    COALESCE(b.actual_qty, 0)                                                           AS stock_qty,
    COALESCE(s30.units_30d, 0)                                                          AS units_30d,
    ROUND(COALESCE(s30.units_30d, 0) / 30, 2)                                           AS daily_velocity,
    ROUND(
        COALESCE(b.actual_qty, 0) / NULLIF(COALESCE(s30.units_30d, 0) / 30, 0),
        1
    )                                                                                   AS days_of_cover,
    CASE
        WHEN COALESCE(b.actual_qty, 0) <= 0                                              THEN 'AGOTADO'
        WHEN COALESCE(s30.units_30d, 0) = 0                                              THEN 'SIN MOVIMIENTO'
        WHEN b.actual_qty / (s30.units_30d / 30) <= 7                                   THEN 'URGENTE'
        WHEN b.actual_qty / (s30.units_30d / 30) <= 14                                  THEN 'REABASTECER'
        ELSE 'OK'
    END                                                                                 AS risk_level
FROM `tabItem` i
LEFT JOIN `tabBin` b
    ON  b.item_code = i.item_code
    AND b.warehouse = %(warehouse)s
LEFT JOIN (
    SELECT sii.item_code, SUM(sii.qty) AS units_30d
    FROM   `tabSales Invoice Item` sii
    INNER  JOIN `tabSales Invoice` si ON si.name = sii.parent
    WHERE  si.docstatus = 1
      AND  si.posting_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
    GROUP  BY sii.item_code
) s30 ON s30.item_code = i.item_code
WHERE i.disabled = 0
  AND i.is_stock_item = 1
  AND (
      COALESCE(b.actual_qty, 0) <= 0
      OR (
          COALESCE(s30.units_30d, 0) > 0
          AND b.actual_qty / (s30.units_30d / 30) <= %(days_threshold)s
      )
  )
ORDER BY
    FIELD(risk_level, 'AGOTADO', 'URGENTE', 'REABASTECER', 'SIN MOVIMIENTO', 'OK'),
    days_of_cover ASC
LIMIT 100
""",
		"js": """
frappe.query_reports["Bazar Replenishment Risk"] = {
    filters: [
        {
            fieldname: "warehouse", label: __("Bodega"), fieldtype: "Link",
            options: "Warehouse", default: "POSNET Stores - L", reqd: 1
        },
        {
            fieldname: "days_threshold", label: __("Umbral días de cobertura"),
            fieldtype: "Int", default: 30
        }
    ],
    formatter(value, row, column, data, default_formatter) {
        if (column.fieldname === "risk_level") {
            const colors = {
                "AGOTADO": "#e03", "URGENTE": "#f80", "REABASTECER": "#07f",
                "SIN MOVIMIENTO": "#999", "OK": "#0a0"
            };
            return `<b style="color:${colors[value] || '#000'}">${value}</b>`;
        }
        if (column.fieldname === "days_of_cover") {
            if (!value || value <= 0) return `<b style="color:#e03">0</b>`;
            if (value <= 7)  return `<b style="color:#e03">${value}</b>`;
            if (value <= 14) return `<span style="color:#f80">${value}</span>`;
        }
        if (column.fieldname === "stock_qty" && value <= 0) {
            return `<b style="color:#e03">0</b>`;
        }
        return default_formatter(value, row, column, data);
    }
};
""",
	},

	# ── 4. Promotions Effectiveness ───────────────────────────────────────────
	{
		"name": "Bazar Promotions Effectiveness",
		"ref_doctype": "Sales Invoice Item",
		"description": "Impacto de descuentos por producto: volumen descontado vs total",
		"query": """
SELECT
    sii.item_code,
    sii.item_name,
    SUM(sii.qty)                                                                        AS units_total,
    ROUND(SUM(sii.net_amount), 2)                                                       AS net_revenue,
    COUNT(CASE WHEN sii.discount_percentage > 0 OR sii.discount_amount > 0 THEN 1 END) AS discounted_lines,
    ROUND(
        AVG(CASE WHEN sii.discount_percentage > 0 THEN sii.discount_percentage END),
        1
    )                                                                                   AS avg_discount_pct,
    ROUND(
        SUM(CASE WHEN sii.discount_percentage > 0 OR sii.discount_amount > 0
                 THEN sii.discount_amount ELSE 0 END),
        2
    )                                                                                   AS total_discount_given,
    ROUND(
        SUM(CASE WHEN sii.discount_percentage > 0 OR sii.discount_amount > 0
                 THEN sii.net_amount ELSE 0 END)
        / NULLIF(SUM(sii.net_amount), 0) * 100,
        1
    )                                                                                   AS promo_revenue_pct
FROM `tabSales Invoice Item` sii
INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
WHERE si.docstatus = 1
  AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
GROUP BY sii.item_code, sii.item_name
HAVING discounted_lines > 0
ORDER BY total_discount_given DESC
LIMIT 100
""",
		"js": """
frappe.query_reports["Bazar Promotions Effectiveness"] = {
    filters: [
        {
            fieldname: "from_date", label: __("Desde"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.add_days(frappe.datetime.get_today(), -30)
        },
        {
            fieldname: "to_date", label: __("Hasta"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.get_today()
        }
    ],
    formatter(value, row, column, data, default_formatter) {
        if (column.fieldname === "promo_revenue_pct") {
            if (value > 50) return `<b style="color:#e03">${value}%</b>`;
            if (value > 25) return `<span style="color:#f80">${value}%</span>`;
            return `<span style="color:#0a0">${value}%</span>`;
        }
        if (column.fieldname === "avg_discount_pct" && value > 30) {
            return `<b style="color:#e03">${value}%</b>`;
        }
        return default_formatter(value, row, column, data);
    }
};
""",
	},

	# ── 5. Dead Stock Aging ───────────────────────────────────────────────────
	{
		"name": "Bazar Dead Stock Aging",
		"ref_doctype": "Item",
		"description": "Inventario sin movimiento por antigüedad (30/60/90/120+ días)",
		"query": """
SELECT
    i.item_code,
    i.item_name,
    i.item_group,
    COALESCE(b.actual_qty, 0)                                            AS stock_qty,
    COALESCE(b.valuation_rate, 0)                                        AS valuation_rate,
    ROUND(COALESCE(b.actual_qty, 0) * COALESCE(b.valuation_rate, 0), 2) AS stock_value,
    COALESCE(last_sale.last_sold, 'Nunca')                               AS last_sold,
    CASE
        WHEN last_sale.last_sold IS NULL                                   THEN '120+ días'
        WHEN DATEDIFF(CURDATE(), last_sale.last_sold) > 120               THEN '120+ días'
        WHEN DATEDIFF(CURDATE(), last_sale.last_sold) > 90                THEN '90-120 días'
        WHEN DATEDIFF(CURDATE(), last_sale.last_sold) > 60                THEN '60-90 días'
        WHEN DATEDIFF(CURDATE(), last_sale.last_sold) > 30                THEN '30-60 días'
        ELSE 'Reciente'
    END                                                                  AS aging_bucket
FROM `tabItem` i
INNER JOIN `tabBin` b
    ON  b.item_code = i.item_code
    AND b.warehouse = %(warehouse)s
    AND b.actual_qty > 0
LEFT JOIN (
    SELECT sii.item_code, MAX(si.posting_date) AS last_sold
    FROM   `tabSales Invoice Item` sii
    INNER  JOIN `tabSales Invoice` si ON si.name = sii.parent
    WHERE  si.docstatus = 1
    GROUP  BY sii.item_code
) last_sale ON last_sale.item_code = i.item_code
WHERE i.disabled = 0
  AND i.is_stock_item = 1
  AND (
      last_sale.last_sold IS NULL
      OR DATEDIFF(CURDATE(), last_sale.last_sold) > %(min_days)s
  )
ORDER BY
    FIELD(aging_bucket, '120+ días', '90-120 días', '60-90 días', '30-60 días', 'Reciente'),
    stock_value DESC
LIMIT 100
""",
		"js": """
frappe.query_reports["Bazar Dead Stock Aging"] = {
    filters: [
        {
            fieldname: "warehouse", label: __("Bodega"), fieldtype: "Link",
            options: "Warehouse", default: "POSNET Stores - L", reqd: 1
        },
        {
            fieldname: "min_days", label: __("Sin movimiento mínimo (días)"),
            fieldtype: "Int", default: 30
        }
    ],
    formatter(value, row, column, data, default_formatter) {
        if (column.fieldname === "aging_bucket") {
            const colors = {
                "120+ días": "#e03", "90-120 días": "#f80",
                "60-90 días": "#fa0", "30-60 días": "#07f", "Reciente": "#0a0"
            };
            return `<b style="color:${colors[value] || '#000'}">${value}</b>`;
        }
        if (column.fieldname === "stock_value" && value > 0) {
            return `<b>$${value.toFixed(2)}</b>`;
        }
        return default_formatter(value, row, column, data);
    }
};
""",
	},

	# ── 6. Store Operations Health ────────────────────────────────────────────
	{
		"name": "Bazar Store Operations Health",
		"ref_doctype": "Stock Entry",
		"description": "Estado de recepciones, sesiones pendientes y actividad del día",
		"query": """
SELECT
    se.stock_entry_type                          AS operation_type,
    COUNT(se.name)                               AS count,
    ROUND(SUM(se.total_outgoing_value
             + se.total_incoming_value), 2)      AS total_value,
    MIN(se.posting_date)                         AS earliest,
    MAX(se.posting_date)                         AS latest
FROM `tabStock Entry` se
WHERE se.docstatus = 1
  AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
GROUP BY se.stock_entry_type
ORDER BY count DESC
""",
		"js": """
frappe.query_reports["Bazar Store Operations Health"] = {
    filters: [
        {
            fieldname: "from_date", label: __("Desde"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.add_days(frappe.datetime.get_today(), -7)
        },
        {
            fieldname: "to_date", label: __("Hasta"), fieldtype: "Date", reqd: 1,
            default: frappe.datetime.get_today()
        }
    ]
};
""",
	},
]


def _upsert_report(r):
	if frappe.db.exists("Report", r["name"]):
		doc = frappe.get_doc("Report", r["name"])
		doc.query = r["query"].strip()
		doc.javascript = r["js"].strip()
		if r.get("description"):
			doc.description = r["description"]
		doc.save(ignore_permissions=True)
		print(f"  ↺ Updated report: {r['name']}")
	else:
		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": r["name"],
				"report_type": "Query Report",
				"ref_doctype": r["ref_doctype"],
				"is_standard": "No",
				"description": r.get("description", ""),
				"query": r["query"].strip(),
				"javascript": r["js"].strip(),
			}
		).insert(ignore_permissions=True)
		print(f"  + Created report: {r['name']}")


def create_reports():
	for r in REPORTS:
		_upsert_report(r)
	frappe.db.commit()
	print(f"  ✓ {len(REPORTS)} reports ready")


# ─── Number Cards ─────────────────────────────────────────────────────────────

NUMBER_CARDS = [
	{
		"label": "Ventas Hoy",
		"type": "Document Type",
		"document_type": "Sales Invoice",
		"function": "Sum",
		"aggregate_function_based_on": "grand_total",
		"filters_json": json.dumps(
			[
				["Sales Invoice", "docstatus", "=", 1, False],
				["Sales Invoice", "posting_date", "=", "Today", False],
			]
		),
		"color": "#5e64ff",
		"is_public": 1,
		"show_percentage_stats": 1,
		"stats_time_interval": "Daily",
	},
	{
		"label": "Tickets Hoy",
		"type": "Document Type",
		"document_type": "Sales Invoice",
		"function": "Count",
		"aggregate_function_based_on": "name",
		"filters_json": json.dumps(
			[
				["Sales Invoice", "docstatus", "=", 1, False],
				["Sales Invoice", "posting_date", "=", "Today", False],
			]
		),
		"color": "#2490ef",
		"is_public": 1,
		"show_percentage_stats": 1,
		"stats_time_interval": "Daily",
	},
	{
		"label": "Ventas últimos 7 días",
		"type": "Document Type",
		"document_type": "Sales Invoice",
		"function": "Sum",
		"aggregate_function_based_on": "grand_total",
		"filters_json": json.dumps(
			[
				["Sales Invoice", "docstatus", "=", 1, False],
				["Sales Invoice", "posting_date", ">=", "7 days ago", False],
			]
		),
		"color": "#9b46ff",
		"is_public": 1,
		"show_percentage_stats": 1,
		"stats_time_interval": "Weekly",
	},
	{
		"label": "Recepciones (30d)",
		"type": "Document Type",
		"document_type": "Stock Entry",
		"function": "Count",
		"aggregate_function_based_on": "name",
		"filters_json": json.dumps(
			[
				["Stock Entry", "docstatus", "=", 1, False],
				["Stock Entry", "stock_entry_type", "=", "Material Receipt", False],
				["Stock Entry", "posting_date", ">=", "30 days ago", False],
			]
		),
		"color": "#f8814f",
		"is_public": 1,
		"show_percentage_stats": 1,
		"stats_time_interval": "Monthly",
	},
]


def create_number_cards():
	for c in NUMBER_CARDS:
		# Number Card autonames from label — look up by label
		existing = frappe.db.get_value("Number Card", {"label": c["label"]}, "name")
		if existing:
			doc = frappe.get_doc("Number Card", existing)
			doc.update(c)
			doc.save(ignore_permissions=True)
			print(f"  ↺ Updated number card: {c['label']}")
		else:
			frappe.get_doc({"doctype": "Number Card", **c}).insert(ignore_permissions=True)
			print(f"  + Created number card: {c['label']}")
	frappe.db.commit()
	print(f"  ✓ {len(NUMBER_CARDS)} number cards ready")


# ─── Dashboard Charts ─────────────────────────────────────────────────────────

CHARTS = [
	{
		"name": "Bazar Ventas Diarias",
		"chart_name": "Bazar Ventas Diarias",
		"chart_type": "Sum",
		"document_type": "Sales Invoice",
		"value_based_on": "grand_total",
		"based_on": "posting_date",
		"timespan": "Last Month",
		"time_interval": "Daily",
		"filters_json": json.dumps([["Sales Invoice", "docstatus", "=", 1, False]]),
		"type": "Line",
		"color": "#5e64ff",
		"is_public": 1,
		"timeseries": 1,
	},
	{
		"name": "Bazar Tickets Diarios",
		"chart_name": "Bazar Tickets Diarios",
		"chart_type": "Count",
		"document_type": "Sales Invoice",
		"based_on": "posting_date",
		"timespan": "Last Month",
		"time_interval": "Daily",
		"filters_json": json.dumps([["Sales Invoice", "docstatus", "=", 1, False]]),
		"type": "Bar",
		"color": "#2490ef",
		"is_public": 1,
		"timeseries": 1,
	},
	{
		"name": "Bazar Recepciones Mensuales",
		"chart_name": "Bazar Recepciones Mensuales",
		"chart_type": "Count",
		"document_type": "Stock Entry",
		"based_on": "posting_date",
		"timespan": "Last Quarter",
		"time_interval": "Monthly",
		"filters_json": json.dumps(
			[
				["Stock Entry", "docstatus", "=", 1, False],
				["Stock Entry", "stock_entry_type", "=", "Material Receipt", False],
			]
		),
		"type": "Bar",
		"color": "#f8814f",
		"is_public": 1,
		"timeseries": 1,
	},
	{
		"name": "Bazar Top Items Por Cantidad",
		"chart_name": "Bazar Top Items Por Cantidad",
		"chart_type": "Group By",
		"document_type": "Sales Invoice Item",
		"parent_document_type": "Sales Invoice",
		"group_by_based_on": "item_name",
		"group_by_type": "Sum",
		"aggregate_function_based_on": "qty",
		"number_of_groups": 10,
		"filters_json": json.dumps(
			[
				["Sales Invoice Item", "docstatus", "=", 1, False],
			]
		),
		"type": "Bar",
		"color": "#0f9",
		"is_public": 1,
		"timeseries": 0,
	},
]


def create_charts():
	for c in CHARTS:
		if frappe.db.exists("Dashboard Chart", c["name"]):
			doc = frappe.get_doc("Dashboard Chart", c["name"])
			doc.update(c)
			doc.save(ignore_permissions=True)
			print(f"  ↺ Updated chart: {c['name']}")
		else:
			frappe.get_doc({"doctype": "Dashboard Chart", **c}).insert(ignore_permissions=True)
			print(f"  + Created chart: {c['name']}")
	frappe.db.commit()
	print(f"  ✓ {len(CHARTS)} charts ready")


# ─── Workspace ────────────────────────────────────────────────────────────────

def _workspace_content():
	"""Build the workspace content JSON (visual layout blocks)."""
	blocks = [
		# ── KPI header ──────────────────────────────────────────────────────
		{"type": "header", "data": {"text": "<span class=\"h4\"><b>Indicadores del Día</b></span>", "col": 12}},
		{"type": "spacer", "data": {"col": 12}},

		# ── Reports header ───────────────────────────────────────────────────
		{"type": "header", "data": {"text": "<span class=\"h4\"><b>Reportes Operativos</b></span>", "col": 12}},
		{"type": "shortcut", "data": {"shortcut_name": "White/Black Control", "col": 3}},
		{"type": "shortcut", "data": {"shortcut_name": "Product Success", "col": 3}},
		{"type": "shortcut", "data": {"shortcut_name": "Replenishment Risk", "col": 3}},
		{"type": "shortcut", "data": {"shortcut_name": "Promotions Effectiveness", "col": 3}},
		{"type": "shortcut", "data": {"shortcut_name": "Dead Stock Aging", "col": 3}},
		{"type": "shortcut", "data": {"shortcut_name": "Store Operations Health", "col": 3}},
		{"type": "spacer", "data": {"col": 12}},

		# ── Trends header ────────────────────────────────────────────────────
		{"type": "header", "data": {"text": "<span class=\"h4\"><b>Tendencias</b></span>", "col": 12}},
	]
	return json.dumps(blocks)


SHORTCUTS = [
	{"label": "White/Black Control",      "type": "Report",   "link_to": "Bazar White Black Control"},
	{"label": "Product Success",          "type": "Report",   "link_to": "Bazar Product Success"},
	{"label": "Replenishment Risk",       "type": "Report",   "link_to": "Bazar Replenishment Risk"},
	{"label": "Promotions Effectiveness", "type": "Report",   "link_to": "Bazar Promotions Effectiveness"},
	{"label": "Dead Stock Aging",         "type": "Report",   "link_to": "Bazar Dead Stock Aging"},
	{"label": "Store Operations Health",  "type": "Report",   "link_to": "Bazar Store Operations Health"},
	{"label": "Sales Invoice",            "type": "DocType",  "link_to": "Sales Invoice"},
	{"label": "Stock Entry",              "type": "DocType",  "link_to": "Stock Entry"},
]

WORKSPACE_NAME = "Bazar Intelligence"


def create_workspace():
	content = _workspace_content()

	# Resolve actual DB names (Number Card autonames from label)
	number_cards = []
	for c in NUMBER_CARDS:
		real_name = frappe.db.get_value("Number Card", {"label": c["label"]}, "name")
		if real_name:
			number_cards.append({"number_card_name": real_name})

	charts = [{"chart_name": c["name"]} for c in CHARTS]

	if frappe.db.exists("Workspace", WORKSPACE_NAME):
		doc = frappe.get_doc("Workspace", WORKSPACE_NAME)
		doc.content = content
		doc.set("number_cards", number_cards)
		doc.set("charts", charts)
		doc.set("shortcuts", SHORTCUTS)
		doc.save(ignore_permissions=True)
		print(f"  ↺ Updated workspace: {WORKSPACE_NAME}")
	else:
		frappe.get_doc(
			{
				"doctype": "Workspace",
				"label": WORKSPACE_NAME,
				"title": WORKSPACE_NAME,
				"icon": "bar-chart",
				"is_hidden": 0,
				"public": 1,
				"content": content,
				"number_cards": number_cards,
				"charts": charts,
				"shortcuts": SHORTCUTS,
			}
		).insert(ignore_permissions=True)
		print(f"  + Created workspace: {WORKSPACE_NAME}")

	frappe.db.commit()
	print(f"  ✓ Workspace ready")


# ─── Entry Point ──────────────────────────────────────────────────────────────

def run():
	print("\n=== i013 Bazar Intelligence Dashboards ===\n")

	print("1/5  Custom fields…")
	add_custom_fields()

	print("2/5  Query reports…")
	create_reports()

	print("3/5  Number cards…")
	create_number_cards()

	print("4/5  Dashboard charts…")
	create_charts()

	print("5/5  Workspace…")
	create_workspace()

	print("\nDone. Open ERPNext → Bazar Intelligence workspace.")
	print("Run: bench --site dev_site_a execute "
		  "erpnext.erpnext_integrations.ecommerce_api.setup_i013.run\n")
