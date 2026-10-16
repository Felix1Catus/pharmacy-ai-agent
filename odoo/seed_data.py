"""Seed the local Odoo 18 `pharmacy` DB with dummy pharmacy data.

Idempotent: products keyed by internal reference (default_code), branches by code,
custom fields by name. Safe to re-run. Dummy data only — no real customer/health data.

Run:  python odoo/seed_data.py
Reads ODOO_* from environment / .env.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from catalogue import BRANCHES, PRODUCTS  # noqa: E402
from odoo_client import OdooClient  # noqa: E402

c = OdooClient()


CUSTOM_FIELDS = [
    ("x_name_ar", "Arabic name", "char"),
    ("x_brand", "Brand", "char"),
    ("x_ingredient_en", "Ingredient (EN)", "char"),
    ("x_ingredient_ar", "Ingredient (AR)", "char"),
    ("x_is_restricted", "Restricted item", "boolean"),
    ("x_requires_prescription", "Requires prescription", "boolean"),
]


def ensure_custom_fields():
    model_id = c.search("ir.model", [["model", "=", "product.template"]])[0]
    for name, label, ttype in CUSTOM_FIELDS:
        if not c.search("ir.model.fields",
                        [["model", "=", "product.template"], ["name", "=", name]]):
            c.create("ir.model.fields", {
                "name": name, "field_description": label, "model_id": model_id,
                "ttype": ttype, "state": "manual",
            })
            print(f"  + custom field {name}")
    print("custom fields ready")


def ensure_branches():
    out = {}
    for b in BRANCHES:
        ids = c.search("stock.warehouse", [["code", "=", b["code"]]])
        wh_id = ids[0] if ids else c.create("stock.warehouse",
                                            {"name": b["name"], "code": b["code"]})
        if not ids:
            print(f"  + branch {b['name']} ({b['code']})")
        wh = c.read("stock.warehouse", wh_id, ["lot_stock_id", "name"])[0]
        out[b["code"]] = {"wh": wh_id, "loc": wh["lot_stock_id"][0], "name": b["name"]}
    print(f"branches ready: {list(out)}")
    return out


def ensure_products():
    created = updated = 0
    variants = []
    for p in PRODUCTS:
        vals = {
            "name": p["name_en"], "list_price": p["price"], "sale_ok": True,
            "type": "consu", "is_storable": True,
            "default_code": p["code"],
            "x_name_ar": p["name_ar"], "x_brand": p["brand"],
            "x_ingredient_en": p["ingredient_en"], "x_ingredient_ar": p["ingredient_ar"],
            "x_is_restricted": p["restricted"], "x_requires_prescription": p["rx"],
            # scheduled/controlled flags come from the pharmacy_agent module
            "is_scheduled": p["scheduled"], "schedule_class": p["schedule_class"],
        }
        ids = c.search("product.template", [["default_code", "=", p["code"]]])
        if ids:
            c.write("product.template", ids, vals)
            tmpl_id, updated = ids[0], updated + 1
        else:
            tmpl_id, created = c.create("product.template", vals), created + 1
        var = c.read("product.template", tmpl_id, ["product_variant_id"])[0]
        variants.append((var["product_variant_id"][0], p["code"], p["restricted"]))
    print(f"products ready: {created} created, {updated} updated, {len(PRODUCTS)} total")
    return variants


def set_stock(variants, branches):
    """Set on-hand qty per branch via inventory adjustment (stock.quant)."""
    codes = list(branches)
    n = 0
    for i, (vid, code, restricted) in enumerate(variants):
        for j, bcode in enumerate(codes):
            loc = branches[bcode]["loc"]
            if restricted:
                qty = 0 if j == 1 else 5
            else:
                qty = (i * 7 + j * 13) % 200
                if (i + j) % 11 == 0:
                    qty = 0
            q = c.search("stock.quant",
                         [["product_id", "=", vid], ["location_id", "=", loc]])
            if q:
                c.write("stock.quant", q, {"inventory_quantity": qty})
                qid = q
            else:
                qid = [c.create("stock.quant",
                                {"product_id": vid, "location_id": loc,
                                 "inventory_quantity": qty})]
            c.execute_kw("stock.quant", "action_apply_inventory", [qid])
            n += 1
    print(f"stock set: {n} (product, branch) quants")


def ensure_customer():
    ids = c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])
    vals = {"name": "Demo Customer", "ref": "CUST-DEMO-1",
            "phone": "+966500000001", "customer_rank": 1}
    if ids:
        c.write("res.partner", ids, vals)
        return ids[0]
    pid = c.create("res.partner", vals)
    print(f"  + customer Demo Customer (id {pid})")
    return pid


def ensure_sample_orders(customer_id, variants):
    by_code = {code: vid for vid, code, _ in variants}
    specs = [
        ("SO-DEMO-1", [("PH-0001", 2), ("PH-0017", 1)]),
        ("SO-DEMO-2", [("PH-0007", 1), ("PH-0024", 3)]),
    ]
    for ref, lines in specs:
        if c.search("sale.order", [["client_order_ref", "=", ref]]):
            continue
        order_lines = [(0, 0, {"product_id": by_code[c_], "product_uom_qty": q})
                       for c_, q in lines]
        oid = c.create("sale.order", {
            "partner_id": customer_id, "client_order_ref": ref, "order_line": order_lines,
        })
        c.execute_kw("sale.order", "action_confirm", [[oid]])
        print(f"  + sample order {ref} (id {oid})")


def main():
    print(f"Seeding {c.db} at {c.url} as uid {c.uid} ...")
    ensure_custom_fields()
    branches = ensure_branches()
    variants = ensure_products()
    set_stock(variants, branches)
    cust = ensure_customer()
    ensure_sample_orders(cust, variants)
    n_prod = (c.search_count("product.template", [["default_code", "like", "PH-%"]]) +
              c.search_count("product.template", [["default_code", "like", "RX-%"]]))
    n_rx = c.search_count("product.template", [["x_is_restricted", "=", True]])
    n_so = c.search_count("sale.order", [["client_order_ref", "like", "SO-DEMO-%"]])
    print(f"\nDONE. products={n_prod} restricted={n_rx} "
          f"branches={len(branches)} sample_orders={n_so}")


if __name__ == "__main__":
    main()
