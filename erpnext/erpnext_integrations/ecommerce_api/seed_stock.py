"""
Seed stock for promotion test items via Material Receipt Stock Entries.
Run:
  bench --site dev_site_a execute erpnext.erpnext_integrations.ecommerce_api.seed_stock.run
"""

import frappe
from frappe.utils import today

WAREHOUSE = "POSNET Stores - L"

# item_code → (item_name, uom, qty_to_add)
ITEMS = [
    ("1e350db6-b302-4b66-846d-cbff4c196379", "Cosmos",                                      "Nos", 10),
    ("5d15f1af-1233-4f3a-ab9a-48de2f3b6712", "Sapiens: De animales a dioses",               "Nos", 8),
    ("81ce6c61-8c72-47be-be11-8f7f30ca6dac", "Homo Deus",                                   "Nos", 5),
    ("68d4ee8c-d1ce-4ede-a5fc-c48eb84e3d7f", "Breves respuestas a las grandes preguntas",   "Nos", 5),
    ("99d7f6b8-f9a8-4153-8d1b-4e8e20ecb79e", "El cerebro femenino",                         "Nos", 5),
    ("937ccab6-80b7-4f20-8f02-8d5e01819ff0", "El cerebro masculino",                        "Nos", 5),
    ("016f1be3-491a-47d9-b37f-a709a96a4b75", "El gen egoísta",                              "Nos", 8),
    ("5583fe33-d2ed-458b-b6c2-6dc9c65c9960", "El universo elegante",                        "Nos", 5),
    ("ba92bb15-7c83-48d2-b7c8-af5df6599430", "El universo en una cáscara de nuez",          "Nos", 5),
]


def run():
    print(f"\nSeeding stock into '{WAREHOUSE}'…")

    se = frappe.get_doc({
        "doctype": "Stock Entry",
        "stock_entry_type": "Material Receipt",
        "posting_date": today(),
        "to_warehouse": WAREHOUSE,
        "items": [
            {
                "item_code": item_code,
                "item_name": item_name,
                "qty": qty,
                "uom": uom,
                "stock_uom": uom,
                "t_warehouse": WAREHOUSE,
                "basic_rate": 1,   # required for valuation; use 1 as placeholder
            }
            for item_code, item_name, uom, qty in ITEMS
        ],
        "remarks": "Seed stock for i009/i010 promotion testing",
    })
    se.insert(ignore_permissions=True)
    se.submit()
    frappe.db.commit()

    print(f"  Created & submitted: {se.name}")
    for item_code, item_name, _, qty in ITEMS:
        print(f"  +{qty:>3}  {item_name}")
    print("\nDone.")
