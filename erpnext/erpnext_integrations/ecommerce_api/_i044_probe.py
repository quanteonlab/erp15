import time, frappe
from collections import Counter
from erpnext.erpnext_integrations.ecommerce_api import tms_api as t

def run():
    c, u = t._active_geocoded_client_rows()
    print("clients", len(c), "ungeo", u)
    for k, days in ((6, ["Mon","Tue","Wed","Thu","Fri"]), (5, ["Mon","Tue","Wed","Thu","Fri"]), (3, ["Mon","Wed","Fri"])):
        s = time.time()
        p = t.preview_rebalance_zones(k=k, working_days=days)
        print(f"k={k} days={len(days)} zones={p['k']} took={time.time()-s:.2f}s ideal={p['ideal_per_zone']} moved={p['moved_total']} warn={p['warnings']} stale={len(p['stale_zones'])}")
        for tr in p["territories"]:
            print("  ", tr["code"], tr["driver"], tr["client_count"], [g["client_count"] for g in p["groups"] if g["territory"]==tr["code"]])
        # compactness: mean dist to own centroid
        tot = 0; cnt = 0
        for g in p["groups"]:
            for r in g["clients"]:
                tot += t._haversine_km(r["lat"], r["lng"], g["centroid"]["lat"], g["centroid"]["lng"]); cnt += 1
        print("   mean km to zone centroid", round(tot/cnt, 2))
    print("dirty", t.preview_rebalance_zones(k=None, working_days=None, max_clients_per_zone="null")["k"],
          t.preview_rebalance_zones(k="", working_days="[]", max_clients_per_zone="")["k"],
          t.preview_rebalance_zones(max_clients_per_zone=3)["warnings"])

def commit_twice():
    days = ["Mon","Tue","Wed","Thu","Fri"]
    p = t.preview_rebalance_zones(k=6, working_days=days)
    r = t.commit_rebalance_zones(preview=frappe.as_json(p), remove_stale_zones=1)
    print("commit1", len(r["zones_written"]), r["addresses_updated"], "removed", r["zones_removed"], r["errors"][:3])
    p2 = t.preview_rebalance_zones(k=6, working_days=days)
    print("re-preview moved (stability)", p2["moved_total"], "stale", p2["stale_zones"])
    # new client flow B
    addr = frappe.get_all("Address", filters={"address_title": ["like", "Calle Demo%"]}, pluck="name", limit=1)[0]
    old = frappe.db.get_value("Address", addr, "custom_zone")
    frappe.db.set_value("Address", addr, "custom_zone", "")
    print("assign", t.assign_zone_for_client(address=addr), "was", old)
    frappe.db.set_value("Address", addr, "custom_zone", "")
    pm = t.preview_rebalance_zones(only_missing_zone=1)
    print("only_missing", pm["mode"], pm["clients_total"], [(g["code"], g["added"]) for g in pm["groups"]])
    r = t.commit_rebalance_zones(preview=pm)
    print("commit missing", r["addresses_updated"], r["zones_written"], r["errors"])
    frappe.db.commit()

def prof():
    import cProfile, pstats, io
    pr = cProfile.Profile(); pr.enable()
    for i in range(5):
        cust = frappe.get_doc({"doctype": "Customer", "customer_name": f"ZZProbe {i}", "customer_type": "Company", "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"), "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")})
        cust.flags.ignore_mandatory = True
        cust.insert(ignore_permissions=True)
        a = frappe.get_doc({"doctype": "Address", "address_title": f"ZZProbe {i}", "address_type": "Shipping", "address_line1": "x", "city": "BA", "country": "Argentina", "links": [{"link_doctype": "Customer", "link_name": cust.name}]})
        a.insert(ignore_permissions=True)
    pr.disable()
    s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(35); print(s.getvalue()[:6000])
    frappe.db.rollback()

def seed_reset():
    s = time.time(); r = t.seed_tms_demo(reset=1); print("reset seed", round(time.time()-s,1), [x for x in r["created"] if "Zone" in x or "Bulk" in x])
    c,_ = t._active_geocoded_client_rows(); print(Counter((x.get("zone") or "").upper() for x in c).most_common(8))
    print([z["code"] for z in t._load_tms_zones()["zones"]])
    s = time.time(); r = t.seed_tms_demo(); print("plain seed", round(time.time()-s,1))

def prof_reset():
    import cProfile, pstats, io
    pr = cProfile.Profile(); pr.enable()
    t.seed_tms_demo(reset=1)
    pr.disable()
    s = io.StringIO(); st = pstats.Stats(pr, stream=s).sort_stats("cumulative"); st.print_stats("tms_api|delete_doc|document.py:.*(insert|submit|cancel|save)", 30); print(s.getvalue()[-5000:])
