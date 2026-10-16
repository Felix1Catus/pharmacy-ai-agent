# Manual end-to-end test of Update List, run INSIDE `odoo shell -d <scratch db>` (never the live
# DB). Scenarios: first sync creates drugs; second sync finds no changes; simulated SFDA changes
# are detected (price change, newly controlled, relaxed control -> review, never unticked);
# importing the generated file afterwards + syncing again shows 0 changes (mapping consistency).
import csv
import json

U = env["pharmacy.sfda.updater"]._get_singleton()  # noqa: F821
U.write({"first_page": 10, "last_page": 13, "workers": 4, "delay": 0.5})
P = env["product.template"].with_context(active_test=False)  # noqa: F821
Run = env["pharmacy.sfda.run"]  # noqa: F821
out = {}


def sync(label):
    run = Run.create({"updater_id": U.id, "trigger": "manual"})
    env.cr.commit()  # noqa: F821
    U._process(run)
    run.invalidate_recordset()
    out[label] = {k: run[k] for k in ("state", "scanned", "new", "changed", "unchanged",
                                      "newly_scheduled", "relaxed", "errors")}
    out[label]["notes_head"] = (run.notes or "").splitlines()[:4]
    return run


sync("1_first_sync")
out["after_first"] = {"products": P.search_count([("sfda_reg_no", "!=", False)]),
                      "scheduled": P.search_count([("is_scheduled", "=", True)]),
                      "xmlids": env["ir.model.data"].search_count([("module", "=", "sfda")])}  # noqa: F821
sync("2_second_sync_no_changes")

# simulate SFDA-side changes
a = P.search([("sfda_reg_no", "!=", False), ("is_scheduled", "=", False)], limit=1)
a.write({"sfda_public_price": 1.0})                               # -> CHANGED (price)
b = P.search([("is_scheduled", "=", True)], limit=1)
b.write({"is_scheduled": False})                                  # -> SCHEDULED again
c = P.search([("is_scheduled", "=", False), ("id", "!=", a.id)], limit=1)
c.write({"is_scheduled": True, "sfda_control": "مقيد"})          # SFDA says غير مقيد -> REVIEW
env.cr.commit()  # noqa: F821
sync("3_detect_changes")
out["checks"] = {"price_restored": a.sfda_public_price != 1.0,
                 "rescheduled": b.is_scheduled,
                 "relaxed_still_scheduled": c.is_scheduled,
                 "relaxed_control_now": c.sfda_control}

# file import (what the owner will do) on top, then sync again -> must be 0 changes
rows = list(csv.reader(open("/sfda/sfda_products_odoo_import_nobom.csv", encoding="utf-8")))
res = P.load(rows[0], rows[1:])
env.cr.commit()  # noqa: F821
out["file_import"] = {"errors": len([m for m in res["messages"] if m.get("type") == "error"]),
                      "products": P.search_count([("sfda_reg_no", "!=", False)])}
sync("4_sync_after_file_import")
print("SYNC_TEST " + json.dumps(out, ensure_ascii=False, default=str))
