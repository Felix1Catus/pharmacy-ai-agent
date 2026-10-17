"""Contract -> Odoo gateway.

Implements each of the seven contract actions against the real Odoo 18 instance.
`product_id` in the contract is an Odoo product.product (variant) id; `branch_id` is a
stock.warehouse id. Every function returns either a success dict (matching the schema's
`returns`) or {"error": {"code", "message", "details"}}.

The agent and the FastAPI app call these functions; tests call them directly.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from odoo_client import OdooClient, OdooError  # noqa: E402
from fuzzy_search import FuzzyIndex  # noqa: E402

RESULTS = ROOT / "results"
HANDOVER_LOG = RESULTS / "handovers.jsonl"

SEARCH_FIELDS = ["name", "default_code", "x_name_ar", "x_brand",
                 "x_ingredient_en", "x_ingredient_ar", "sfda_scientific_name"]


def _needs_prescription(flags: dict) -> bool:
    """Prescription/restricted from our own flags OR the SFDA registry import
    (sfda_requires_prescription, set from SFDA "طريقة الصرف = يحتاج الى وصفة طبية")."""
    return bool(flags.get("x_is_restricted") or flags.get("x_requires_prescription")
                or flags.get("sfda_requires_prescription"))


def err(code: str, message: str, **details) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


class Gateway:
    def __init__(self, client: OdooClient | None = None):
        self.c = client or OdooClient()
        self._branch_cache = None
        self._fuzzy = FuzzyIndex(self.c)      # phonetic Arabic<->English name matching

    # ---- branches (warehouses, excluding the default 'WH') -------------------
    def branches(self) -> list[dict]:
        if self._branch_cache is None:
            whs = self.c.search_read(
                "stock.warehouse", [["code", "!=", "WH"]],
                ["name", "code", "lot_stock_id"])
            self._branch_cache = [
                {"branch_id": w["id"], "name": w["name"],
                 "loc_id": w["lot_stock_id"][0]} for w in whs]
        return self._branch_cache

    def _branch(self, branch_id: int):
        return next((b for b in self.branches() if b["branch_id"] == branch_id), None)

    # ---- 0. list_branches ----------------------------------------------------
    def list_branches(self):
        return {"branches": [{"branch_id": b["branch_id"], "name": b["name"]}
                             for b in self.branches()]}

    def _avail_at(self, variant_id: int, loc_id: int) -> float:
        quants = self.c.search_read(
            "stock.quant",
            [["product_id", "=", variant_id], ["location_id", "child_of", loc_id]],
            ["available_quantity"])
        return sum(q["available_quantity"] for q in quants)

    def _product_flags(self, variant_id: int) -> dict | None:
        rows = self.c.search_read(
            "product.product", [["id", "=", variant_id]],
            ["name", "default_code", "x_name_ar", "x_brand",
             "x_is_restricted", "x_requires_prescription", "sfda_requires_prescription",
             "is_scheduled", "schedule_class"])
        return rows[0] if rows else None

    # ---- 1. search_product ---------------------------------------------------
    def search_product(self, query: str, branch_id: int | None = None, limit: int = 5):
        """Exact (case-insensitive substring) matches first; if fewer than `limit`, add products
        whose names SOUND like the query across Arabic/English (fuzzy_search.py), so
        "سرفاركس" finds CERVARIX and "بنتوفير" finds بانتوفير. Each candidate says how it matched."""
        if not query or not query.strip():
            return err("invalid_input", "query is required")
        q, lim = query.strip(), max(1, min(limit, 20))
        fields = ["name", "x_name_ar", "x_brand", "x_is_restricted", "x_requires_prescription",
                  "sfda_requires_prescription", "is_scheduled", "schedule_class",
                  "sfda_scientific_name", "list_price", "product_variant_id"]
        domain = ["|"] * (len(SEARCH_FIELDS) - 1) + [[f, "ilike", q] for f in SEARCH_FIELDS]
        found = [(t, "exact") for t in self.c.search_read("product.template", domain, fields, limit=lim)]
        if len(found) < lim:
            seen = {t["id"] for t, _ in found}
            ranked = [(tid, s) for tid, s in self._fuzzy.search(q, limit=lim * 3) if tid not in seen]
            ranked = ranked[: lim - len(found)]
            if ranked:
                rows = {r["id"]: r for r in self.c.search_read(
                    "product.template", [["id", "in", [tid for tid, _ in ranked]]], fields)}
                found += [(rows[tid], "spelling variant") for tid, _ in ranked if tid in rows]
        candidates = []
        for t, how in found:
            vid = t["product_variant_id"][0]
            if branch_id is not None:
                b = self._branch(branch_id)
                if not b or self._avail_at(vid, b["loc_id"]) <= 0:
                    continue
            candidates.append({
                "product_id": vid,
                "name_en": t["name"],
                "name_ar": t.get("x_name_ar") or "",
                "brand": t.get("x_brand") or "",
                "scientific_name": t.get("sfda_scientific_name") or "",
                # policy: price is shown for OTC and prescription-only drugs, never for Scheduled
                "price_sar": None if t.get("is_scheduled") else t.get("list_price"),
                "match": how,
                "is_restricted": bool(t.get("x_is_restricted") or t.get("sfda_requires_prescription")),
                "requires_prescription": bool(t.get("x_requires_prescription")
                                              or t.get("sfda_requires_prescription")),
                "is_scheduled": bool(t.get("is_scheduled")),
                "schedule_class": t.get("schedule_class") or "",
            })
        return {"candidates": candidates}

    # ---- 2. check_stock ------------------------------------------------------
    def check_stock(self, product_id: int, branch_id: int | None = None):
        flags = self._product_flags(product_id)
        if not flags:
            return err("not_found", f"no product with id {product_id}")
        if flags.get("is_scheduled"):    # policy: no availability for Scheduled medicines
            return err("scheduled_item",
                       f"{flags['name']} is a scheduled/controlled substance — availability is "
                       f"not disclosed via the agent; refer to a pharmacist", product_id=product_id)
        branches = self.branches()
        if branch_id is not None:
            b = self._branch(branch_id)
            if not b:
                return err("not_found", f"no branch with id {branch_id}")
            branches = [b]
        stock = [{"branch_id": b["branch_id"], "branch_name": b["name"],
                  "qty_available": self._avail_at(product_id, b["loc_id"])}
                 for b in branches]
        return {"stock": stock}

    # ---- 3. create_order -----------------------------------------------------
    def create_order(self, customer_id, items, fulfillment,
                     branch_id=None, delivery_address=None):
        if not self.c.search("res.partner", [["id", "=", customer_id]]):
            return err("not_found", f"no customer with id {customer_id}")
        if not items:
            return err("invalid_input", "items is required")
        # resolve + guardrail restricted
        for it in items:
            flags = self._product_flags(it["product_id"])
            if not flags:
                return err("not_found", f"no product with id {it['product_id']}")
            if flags.get("is_scheduled"):
                return err("scheduled_item",
                           f"{flags['name']} is a scheduled/controlled substance — "
                           f"never dispensed via the agent; refer to a human",
                           product_id=it["product_id"])
            if _needs_prescription(flags):
                return err("restricted_item",
                           f"{flags['name']} is restricted/prescription — escalate to a human",
                           product_id=it["product_id"])
        # choose branch
        if fulfillment == "pickup":
            if not branch_id:
                return err("invalid_input", "branch_id is required for pickup")
            if not self._branch(branch_id):
                return err("not_found", f"no branch with id {branch_id}")
            chosen = branch_id
        elif fulfillment == "delivery":
            if not delivery_address:
                return err("invalid_input", "delivery_address is required for delivery")
            chosen = self._pick_branch_with_stock(items)
            if chosen is None:
                return err("out_of_stock", "no branch has all items in stock")
        else:
            return err("invalid_input", f"unknown fulfillment {fulfillment!r}")
        # stock check at chosen branch
        b = self._branch(chosen)
        for it in items:
            if self._avail_at(it["product_id"], b["loc_id"]) < it["qty"]:
                return err("out_of_stock",
                           f"product {it['product_id']} short at {b['name']}",
                           branch_id=chosen, product_id=it["product_id"])
        lines = [(0, 0, {"product_id": it["product_id"],
                         "product_uom_qty": it["qty"]}) for it in items]
        vals = {"partner_id": customer_id, "warehouse_id": chosen, "order_line": lines}
        if fulfillment == "delivery":
            vals["note"] = f"Delivery to: {delivery_address}"
        try:
            oid = self.c.create("sale.order", vals)
            self.c.execute_kw("sale.order", "action_confirm", [[oid]])
        except OdooError as e:
            return err("invalid_input", str(e))
        o = self.c.read("sale.order", oid, ["state", "amount_total"])[0]
        return {"order_id": oid, "state": o["state"], "total": o["amount_total"],
                "branch_id": chosen}

    def _pick_branch_with_stock(self, items):
        for b in self.branches():
            if all(self._avail_at(it["product_id"], b["loc_id"]) >= it["qty"]
                   for it in items):
                return b["branch_id"]
        return None

    # ---- 4. create_dispatch --------------------------------------------------
    def create_dispatch(self, order_id, courier_note=None):
        rows = self.c.search_read("sale.order", [["id", "=", order_id]],
                                  ["picking_ids", "state"])
        if not rows:
            return err("not_found", f"no order with id {order_id}")
        pickings = rows[0].get("picking_ids") or []
        if not pickings:
            return err("invalid_input", "order has no delivery to dispatch")
        pid = pickings[0]
        if courier_note:
            try:
                self.c.write("stock.picking", [pid], {"note": courier_note})
            except OdooError:
                pass
        p = self.c.read("stock.picking", pid, ["name", "state"])[0]
        return {"dispatch_id": pid, "state": p["state"], "tracking_ref": p["name"]}

    # ---- 5. hold_item --------------------------------------------------------
    def hold_item(self, product_id, branch_id, qty, customer_id, hours=24):
        flags = self._product_flags(product_id)
        if not flags:
            return err("not_found", f"no product with id {product_id}")
        if flags.get("is_scheduled"):
            return err("scheduled_item",
                       f"{flags['name']} is a scheduled/controlled substance — "
                       f"never held via the agent; refer to a human", product_id=product_id)
        if _needs_prescription(flags):
            return err("restricted_item",
                       f"{flags['name']} is restricted/prescription — escalate",
                       product_id=product_id)
        b = self._branch(branch_id)
        if not b:
            return err("not_found", f"no branch with id {branch_id}")
        if not self.c.search("res.partner", [["id", "=", customer_id]]):
            return err("not_found", f"no customer with id {customer_id}")
        if self._avail_at(product_id, b["loc_id"]) < qty:
            return err("out_of_stock", f"product {product_id} short at {b['name']}")
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        # A hold is modelled as a DRAFT sale order (not confirmed), tagged + dated.
        ref = f"HOLD-{int(time.time())}"
        oid = self.c.create("sale.order", {
            "partner_id": customer_id, "warehouse_id": branch_id,
            "client_order_ref": ref,
            "note": f"HOLD until {expires.isoformat()} at {b['name']}",
            "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": qty})],
        })
        return {"hold_id": oid, "expires_at": expires.isoformat(),
                "branch_id": branch_id, "ref": ref}

    # ---- 6. track_order ------------------------------------------------------
    def track_order(self, order_id=None, customer_id=None):
        if order_id:
            domain = [["id", "=", order_id]]
        elif customer_id:
            domain = [["partner_id", "=", customer_id]]
        else:
            return err("invalid_input", "order_id or customer_id is required")
        rows = self.c.search_read("sale.order", domain,
                                  ["name", "state", "amount_total", "delivery_status"],
                                  order="id desc", limit=10)
        if not rows:
            return err("not_found", "no matching order")
        orders = [{"order_id": r["id"], "name": r["name"], "state": r["state"],
                   "total": r["amount_total"],
                   "dispatch_state": r.get("delivery_status") or "none"}
                  for r in rows]
        return {"orders": orders}

    # ---- 7. escalate_to_human ------------------------------------------------
    def escalate_to_human(self, reason, summary, customer_id=None):
        RESULTS.mkdir(exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "reason": reason,
                 "summary": summary, "customer_id": customer_id}
        with HANDOVER_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handover_id = sum(1 for _ in HANDOVER_LOG.open(encoding="utf-8"))
        ack = ("حوّلت طلبك إلى صيدلي/موظف وسيتواصل معك قريبًا."
               " (Handed over to a human pharmacist; they'll follow up shortly.)")
        return {"handover_id": handover_id, "acknowledgement": ack}


# dispatch table so the app/agent can call by action name
def dispatch(gw: Gateway, action: str, args: dict):
    fn = getattr(gw, action, None)
    if fn is None:
        return err("invalid_input", f"unknown action {action!r}")
    try:
        return fn(**args)
    except TypeError as e:
        return err("invalid_input", f"bad arguments for {action}: {e}")
    except OdooError as e:
        return err("invalid_input", str(e))
