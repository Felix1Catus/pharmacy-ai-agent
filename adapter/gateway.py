"""Contract -> Odoo gateway.

Implements each of the seven contract actions against the real Odoo 18 instance.
`product_id` in the contract is an Odoo product.product (variant) id; `branch_id` is a
stock.warehouse id. Every function returns either a success dict (matching the schema's
`returns`) or {"error": {"code", "message", "details"}}.

The agent and the FastAPI app call these functions; tests call them directly.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from odoo_client import OdooClient, OdooError  # noqa: E402
from fuzzy_search import FuzzyIndex, normalize  # noqa: E402
import ingredients as ingr  # noqa: E402
import prescription as rx  # noqa: E402

RESULTS = ROOT / "results"
HANDOVER_LOG = RESULTS / "handovers.jsonl"

SEARCH_FIELDS = ["name", "default_code", "x_name_ar", "x_brand",
                 "x_ingredient_en", "x_ingredient_ar", "sfda_scientific_name"]


def _needs_prescription(flags: dict) -> bool:
    """Prescription/restricted from our own flags OR the SFDA registry import
    (sfda_requires_prescription, set from SFDA "طريقة الصرف = يحتاج الى وصفة طبية")."""
    return bool(flags.get("x_is_restricted") or flags.get("x_requires_prescription")
                or flags.get("sfda_requires_prescription"))


# prescription states that let the agent order/hold the medicines found on it
RX_OK_STATES = ["auto_verified", "verified"]
RX_PRODUCT_FIELDS = ["name", "x_name_ar", "x_brand", "sfda_scientific_name", "product_variant_id",
                     "x_is_restricted", "x_requires_prescription", "sfda_requires_prescription",
                     "is_scheduled"]


def normalize_name(name: str) -> str:
    return " ".join(normalize(name or "").split())


def err(code: str, message: str, **details) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


class Gateway:
    def __init__(self, client: OdooClient | None = None):
        self.c = client or OdooClient()
        self._branch_cache = None
        self._fuzzy = FuzzyIndex(self.c)      # phonetic Arabic<->English name matching
        self._ingr = ingr.IngredientIndex(self.c)   # same active ingredient, other brands

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
    _CAND_FIELDS = ["name", "x_name_ar", "x_brand", "x_is_restricted", "x_requires_prescription",
                    "sfda_requires_prescription", "is_scheduled", "schedule_class",
                    "sfda_scientific_name", "x_ingredient_en", "list_price", "product_variant_id"]

    def _name_matches(self, q: str, lim: int) -> list[tuple[dict, str]]:
        """Exact (case-insensitive substring) matches first, then products whose names SOUND
        like the query across Arabic/English (fuzzy_search.py: "سرفاركس" -> CERVARIX)."""
        domain = ["|"] * (len(SEARCH_FIELDS) - 1) + [[f, "ilike", q] for f in SEARCH_FIELDS]
        found = [(t, "exact") for t in self.c.search_read("product.template", domain,
                                                          self._CAND_FIELDS, limit=lim)]
        if len(found) < lim:
            seen = {t["id"] for t, _ in found}
            ranked = [(tid, s) for tid, s in self._fuzzy.search(q, limit=lim * 3) if tid not in seen]
            ranked = ranked[: lim - len(found)]
            if ranked:
                rows = {r["id"]: r for r in self.c.search_read(
                    "product.template", [["id", "in", [tid for tid, _ in ranked]]], self._CAND_FIELDS)}
                found += [(rows[tid], "spelling variant") for tid, _ in ranked if tid in rows]
        return found

    def _candidate(self, t: dict, how: str, branch_id=None) -> dict | None:
        vid = t["product_variant_id"][0]
        if branch_id is not None:
            b = self._branch(branch_id)
            if not b or self._avail_at(vid, b["loc_id"]) <= 0:
                return None
        return {
            "product_id": vid,
            "name_en": t["name"],
            "name_ar": t.get("x_name_ar") or "",
            "brand": t.get("x_brand") or "",
            "scientific_name": t.get("sfda_scientific_name") or t.get("x_ingredient_en") or "",
            # policy: price is shown for OTC and prescription-only drugs, never for Scheduled
            "price_sar": None if t.get("is_scheduled") else t.get("list_price"),
            "match": how,
            "is_restricted": bool(t.get("x_is_restricted") or t.get("sfda_requires_prescription")),
            "requires_prescription": bool(t.get("x_requires_prescription")
                                          or t.get("sfda_requires_prescription")),
            "is_scheduled": bool(t.get("is_scheduled")),
            "schedule_class": t.get("schedule_class") or "",
        }

    def _rows(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        rows = {r["id"]: r for r in self.c.search_read("product.template", [["id", "in", ids]],
                                                       self._CAND_FIELDS)}
        return [rows[i] for i in ids if i in rows]

    def search_product(self, query: str = "", branch_id: int | None = None, limit: int = 5,
                       active_ingredient: str | None = None):
        """Find what the customer means, by name OR by active ingredient.

        - A brand/name query ("augmentin", "بندول") returns name matches (exact, then sound-alike)
          plus `alternatives`: other brands with exactly the same active ingredient(s).
        - An ingredient query ("amoxicillin", "اموكسيسيلين 500 ملجم كبسول") or `active_ingredient`
          (the model's knowledge of a brand we don't carry) lists products containing it:
          single-ingredient first, then the requested dosage form and closest strength.
        - A requested strength that we don't have is reported with the strengths we do have."""
        if not (query and query.strip()) and not active_ingredient:
            return err("invalid_input", "query is required")
        q, lim = (query or "").strip(), max(1, min(limit, 20))
        pq = ingr.parse_query(q)
        core = pq["core"] or q
        strength, form = pq["strength_mg"], pq["form"]

        ing = self._ingr.resolve_name(active_ingredient or core)
        named_ingredient = bool(ing)
        found = [] if active_ingredient else self._name_matches(q, lim * 2)
        if found is not None and core and core != normalize(q) and not active_ingredient \
                and not any(how == "exact" for _, how in found):
            found = self._name_matches(core, lim * 2) or found     # "augmentin 625" -> "augmentin"
        if not ing and found:                    # brand query: its own active ingredient(s)
            ing = self._ingr.set_of(found[0][0]["id"])

        out: dict = {}
        if named_ingredient:
            # everything containing the ingredient, ranked by the ingredient rules: exactly this
            # ingredient set first (plain amoxicillin before amoxicillin+clavulanate), then the
            # requested dosage form, then the closest strength
            direct = {t["id"] for t, how in found if how == "exact"}
            pool = self._ingr.rank(set(self._ingr.same_ingredients(ing, include_combos=True)) | direct,
                                   ing, strength, form)
            picked = [(r, "exact" if r["id"] in direct else "active ingredient")
                      for r in self._rows(pool[: lim * 3])]
        else:
            ranked = self._ingr.rank([t["id"] for t, _ in found], ing, strength, form) \
                if (strength or form) and ing else [t["id"] for t, _ in found]
            by_id = {t["id"]: (t, how) for t, how in found}
            picked = [by_id[i] for i in ranked if i in by_id] + \
                     [x for x in found if x[0]["id"] not in ranked]
        cands, names = [], set()
        for t, how in picked:
            key = normalize_name(t["name"])
            if key in names:                     # SFDA lists some products twice
                continue
            c = self._candidate(t, how, branch_id)
            if c:
                names.add(key)
                cands.append(c)
            if len(cands) >= lim:
                break
        out["candidates"] = cands

        if ing:
            out["active_ingredient"] = " + ".join(sorted(ing)).upper()
            if not named_ingredient:             # brand query: same ingredient, other brands
                shown = {c["product_id"] for c in cands}
                alt_ids = self._ingr.rank(self._ingr.same_ingredients(ing), ing, strength, form)
                alts = []
                for t in self._rows(alt_ids[: lim * 4]):
                    key = normalize_name(t["name"])
                    if t["product_variant_id"][0] in shown or key in names:
                        continue
                    c = self._candidate(t, "same active ingredient, other brand", branch_id)
                    if c:
                        names.add(key)
                        alts.append(c)
                    if len(alts) >= lim:
                        break
                out["alternatives"] = alts
            if strength:
                pool = self._ingr.same_ingredients(ing)
                have = self._ingr.strengths(pool, form) or self._ingr.strengths(pool)
                if have and ingr.fmt_mg(strength) not in have:
                    out["note"] = (f"{ingr.fmt_mg(strength)} is not available for this active "
                                   f"ingredient; available strengths: {', '.join(have)}")
        if not cands and not out.get("alternatives"):
            out["hint"] = ("Not in our catalogue. Tell the customer, and ask them for the active "
                           "ingredient written on the box or prescription (or a photo of the "
                           "prescription) so you can find the same medicine under another brand. "
                           "Never guess the active ingredient yourself.")
        return out

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
        rx_used: set[int] = set()
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
                rid = self.valid_prescription(customer_id, it["product_id"])
                if not rid:
                    return self._rx_required(flags, it["product_id"])
                rx_used.add(rid)
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
        if rx_used:
            refs = self.c.search_read("pharmacy.prescription", [["id", "in", list(rx_used)]],
                                      ["name", "invoice_number"])
            vals["note"] = ((vals.get("note") or "") + " | Prescription(s): " + ", ".join(
                f"{r['name']} (invoice {r['invoice_number']})" for r in refs)).strip(" |")
        try:
            oid = self.c.create("sale.order", vals)
            self.c.execute_kw("sale.order", "action_confirm", [[oid]])
        except OdooError as e:
            return err("invalid_input", str(e))
        if rx_used:      # one prescription = one order: it cannot be used again
            self.c.write("pharmacy.prescription", list(rx_used),
                         {"sale_order_id": oid, "state": "used"})
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
    def create_dispatch(self, order_id, courier_note=None, customer_id=None):
        domain = [["id", "=", order_id]]
        if customer_id:                     # agent-injected: only the customer's own orders
            domain.append(["partner_id", "=", customer_id])
        rows = self.c.search_read("sale.order", domain, ["picking_ids", "state"])
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
        if _needs_prescription(flags) and not self.valid_prescription(customer_id, product_id):
            return self._rx_required(flags, product_id)
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

    # ---- prescriptions (not an LLM tool: the chat channel calls these) --------
    def _rx_required(self, flags, product_id):
        return err("prescription_required",
                   f"{flags['name']} needs a prescription: ask the customer to attach a photo of a "
                   f"computerised prescription dated within the last "
                   f"{rx.PRESCRIPTION_MAX_AGE_DAYS} days that shows this medicine and an invoice "
                   f"number", product_id=product_id)

    def valid_prescription(self, customer_id, product_id) -> int | None:
        """Id of an unused, verified prescription of this customer, dated within the allowed
        window, that lists this product — or None."""
        if not customer_id:
            return None
        oldest = (rx.today_ksa() - timedelta(days=rx.PRESCRIPTION_MAX_AGE_DAYS)).isoformat()
        ids = self.c.search("pharmacy.prescription", [
            ["partner_id", "=", customer_id], ["state", "in", RX_OK_STATES],
            ["sale_order_id", "=", False], ["prescription_date", ">=", oldest],
            ["matched_product_ids", "in", [product_id]]], order="id desc", limit=1)
        return ids[0] if ids else None

    def submit_prescription(self, customer_id, image: bytes, filename: str | None = None):
        """Read a prescription photo or PDF, run the checks, store it in Odoo (Pharmacy →
        Prescriptions) and return the outcome. Auto-verified only if date + invoice number + a
        medicine pass, and the invoice number was not already used by another customer or
        another order. A PDF is kept as the original file plus a PNG of its first page."""
        doc = rx.read_document(image)
        text = doc["text"]
        b64 = lambda b: base64.b64encode(b).decode() if b else False  # noqa: E731
        if doc["kind"] == "pdf":
            files = {"image": b64(doc["preview"]), "image_filename": "page1.png",
                     "document": b64(image), "document_filename": filename or "prescription.pdf"}
        else:
            files = {"image": b64(image), "image_filename": filename or "prescription.jpg"}
        rows = self.c.search_read("product.template", [["sale_ok", "=", True]], RX_PRODUCT_FIELDS)
        res = rx.evaluate(text, rows)
        # Scheduled (مقيد / controlled) medicines are never verified by OCR: they are dropped
        # from what the prescription unlocks; one listing ONLY Scheduled medicines is refused.
        res["scheduled_count"] = sum(m["is_scheduled"] for m in res["medications"])
        res["medications"] = [m for m in res["medications"] if not m["is_scheduled"]]
        if res["scheduled_count"] and not res["medications"]:
            res["problems"].append("only_scheduled")
            res["ok"] = False
        if res["invoice_number"]:
            reused = self.c.search_count("pharmacy.prescription", [
                ["invoice_number", "=", res["invoice_number"]],
                "|", ["sale_order_id", "!=", False], ["partner_id", "!=", customer_id]])
            if reused:
                res["problems"].append("invoice_already_used")
                res["ok"] = False
        meds = res["medications"]
        # generic substitution: the prescription also unlocks other brands with EXACTLY the same
        # active ingredient(s) (Augmentin -> أوجمنتين / JULMENTIN / أموكلان), never Scheduled ones
        unlocked = {m["product_id"] for m in meds}
        variant_of = {r["id"]: r for r in rows}
        for m in meds:
            s = self._ingr.set_of(m.get("template_id"))
            for tid in self._ingr.same_ingredients(s) if s else []:
                r = variant_of.get(tid)
                if r and not r.get("is_scheduled"):
                    unlocked.add(r["product_variant_id"][0])
        res["same_ingredient_count"] = len(unlocked) - len({m["product_id"] for m in meds})
        rid = self.c.create("pharmacy.prescription", {
            "name": f"RX-{datetime.now(rx.KSA):%Y%m%d-%H%M%S}",
            "partner_id": customer_id,
            **files,
            "ocr_engine": doc["engine"],
            "ocr_text": text,
            "ocr_status": "ocr_done" if res["ok"] else
                          ("unreadable" if "unreadable" in res["problems"] else "needs_review"),
            "ocr_confidence": 1.0 if res["ok"] else 0.0,
            "invoice_number": res["invoice_number"] or False,
            "prescription_date": res["date"] or False,
            "matched_product_ids": [(6, 0, sorted(unlocked))],
            "extracted_drug": ", ".join(m["name_en"] for m in meds)[:250] or False,
            "state": "auto_verified" if res["ok"] else "submitted",
            "notes": ("checks failed: " + ", ".join(res["problems"])) if res["problems"]
                     else "auto-verified: date, invoice number and medicine(s) found",
        })
        return {"prescription_id": rid, **res}

    # ---- 6. track_order ------------------------------------------------------
    def track_order(self, order_id=None, customer_id=None):
        # with both, the order must belong to that customer (the agent always passes its own
        # customer, so a chat can never read someone else's order by guessing a number)
        domain = []
        if order_id:
            domain.append(["id", "=", order_id])
        if customer_id:
            domain.append(["partner_id", "=", customer_id])
        if not domain:
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
