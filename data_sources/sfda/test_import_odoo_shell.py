# Runs INSIDE `odoo shell` (see run_sfda.sh). Imports the generated file with Odoo's own import
# engine (Model.load = what Inventory > Products > Import uses), then checks the mapping and that
# a second import updates (via external IDs) instead of duplicating.
import csv
import json

rows = list(csv.reader(open("/sfda/sfda_products_odoo_import_nobom.csv", encoding="utf-8")))
header, data = rows[0], rows[1:]
P = env["product.template"].with_context(tracking_disable=True)  # noqa: F821  (odoo shell global)

res = P.load(header, data)
errors = [m for m in res["messages"] if m.get("type") == "error"]
expected_sched = sum(1 for r in data if r[header.index("is_scheduled")] == "True")
expected_rx = sum(1 for r in data if r[header.index("sfda_requires_prescription")] == "True")
out = {
    "rows_in_file": len(data),
    "imported_ids": len([i for i in (res["ids"] or []) if i]),
    "errors": len(errors),
    "first_errors": [m.get("message") for m in errors[:5]],
    "products_with_sfda_reg_no": P.search_count([("sfda_reg_no", "!=", False)]),
    "scheduled_in_odoo": P.search_count([("is_scheduled", "=", True)]),
    "scheduled_expected_from_file": expected_sched,
    "prescription_in_odoo": P.search_count([("sfda_requires_prescription", "=", True)]),
    "prescription_expected_from_file": expected_rx,
}
res2 = P.load(header, data)
out["reimport_errors"] = len([m for m in res2["messages"] if m.get("type") == "error"])
out["products_after_reimport"] = P.search_count([("sfda_reg_no", "!=", False)])
sample = P.search([("is_scheduled", "=", True)], limit=3)
out["sample_scheduled"] = [(p.name, p.sfda_control, p.sfda_scientific_name) for p in sample]
env.cr.commit()  # noqa: F821
print("IMPORT_RESULT " + json.dumps(out, ensure_ascii=False))
