"""Contract -> Odoo gateway.

Implements each of the seven contract actions against the real Odoo 18 instance.
`product_id` in the contract is an Odoo product.product (variant) id; `branch_id` is a
stock.warehouse id. Every function returns either a success dict (matching the schema's
`returns`) or {"error": {"code", "message", "details"}}.

The agent and the FastAPI app call these functions; tests call them directly.
"""
from __future__ import annotations

import base64
import re
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
RX_OK_STATES = ["verified"]                # pharmacist-approved only; OCR is a pre-check
RX_PRODUCT_FIELDS = ["name", "x_name_ar", "x_brand", "sfda_scientific_name", "product_variant_id",
                     "x_is_restricted", "x_requires_prescription", "sfda_requires_prescription",
                     "is_scheduled"]


_PHONE_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CITIES = {"الرياض": "Riyadh", "جده": "Jeddah", "جدة": "Jeddah", "الدمام": "Dammam",
           "مكه": "Makkah", "مكة": "Makkah", "المدينه": "Madinah", "المدينة": "Madinah",
           "الخبر": "Khobar", "الطايف": "Taif", "الطائف": "Taif", "ابها": "Abha", "أبها": "Abha",
           "تبوك": "Tabuk", "بريده": "Buraydah", "بريدة": "Buraydah", "الاحساء": "Al Ahsa",
           "الأحساء": "Al Ahsa", "حايل": "Hail", "حائل": "Hail", "جازان": "Jazan", "نجران": "Najran",
           "دمام": "Dammam", "رياض": "Riyadh", "خبر": "Khobar", "مدينه": "Madinah",
           "riyadh": "Riyadh", "jeddah": "Jeddah", "dammam": "Dammam", "makkah": "Makkah",
           "mecca": "Makkah", "madinah": "Madinah", "medina": "Madinah", "khobar": "Khobar"}


def normalize_phone(raw) -> str | None:
    """Phone number -> international digits without '+', the WhatsApp wa_id format.
    Saudi mobiles in any local form (05XXXXXXXX, 5XXXXXXXX, +966..., 00966...) -> 9665XXXXXXXX."""
    if not raw:
        return None
    d = "".join(ch for ch in str(raw).translate(_PHONE_DIGITS) if ch.isdigit())
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 10 and d.startswith("05"):
        d = "966" + d[1:]
    elif len(d) == 9 and d.startswith("5"):
        d = "966" + d
    elif d.startswith("9660"):
        d = "966" + d[4:]
    elif len(d) == 11 and d.startswith("01"):     # Egyptian mobile 01XXXXXXXXX -> 201XXXXXXXXX
        d = "20" + d[1:]
    return d if 9 <= len(d) <= 15 else None


def guess_city(address: str) -> str | None:
    """City named in a free-text address: the FIRST city word, so "6 دمام شارع جدة الجديد"
    is Dammam (the street is named after Jeddah)."""
    low = (address or "").lower()
    hits = [(m.start(), en) for k, en in _CITIES.items()
            for m in re.finditer(rf"(?<!\w){re.escape(k)}(?!\w)", low)]
    return min(hits)[1] if hits else None


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
        if branch_id is not None:
            # a branch filter must never turn "in our catalogue" into "not found": an unknown
            # branch id (the model guessed one) is ignored, and if nothing is in stock at a real
            # branch the products are still returned with a note
            b = self._branch(branch_id)
            unfiltered = self.search_product(query, None, limit, active_ingredient)
            if not b:
                unfiltered["note_branch"] = (f"branch_id {branch_id} does not exist - call "
                                             f"list_branches; results are for all branches")
                return unfiltered
            filtered = self._search(query, branch_id, limit, active_ingredient)
            if filtered.get("candidates") or filtered.get("alternatives"):
                return filtered
            if unfiltered.get("candidates") or unfiltered.get("alternatives"):
                unfiltered["note_branch"] = (f"none of these is in stock at {b['name']}; they are "
                                             f"in our catalogue - check_stock shows other branches")
            return unfiltered
        return self._search(query, None, limit, active_ingredient)

    def _search(self, query, branch_id, limit, active_ingredient):
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
                if have and strength not in have and strength not in self._ingr.strengths(pool):
                    out["note"] = (f"{ingr.fmt_mg(strength)} is not available for this active "
                                   f"ingredient; closest strengths we have: "
                                   f"{', '.join(self._ingr.nearest(have, strength))}")
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
        rx_review = False
        rx_pids: list[int] = []
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
                if rid:
                    rx_used.add(rid)             # already pharmacist-approved for this customer
                else:
                    rx_review = True             # quotation goes to the pharmacist for review
                    rx_pids.append(it["product_id"])
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
        # A QUOTATION (not confirmed): a human confirms/invoices it at the dispensary for pickup,
        # or approves it for delivery (then the Delivery Dispatcher takes over - future module).
        vals = {"partner_id": customer_id, "warehouse_id": chosen, "order_line": lines,
                "wa_order": True, "fulfillment_type": fulfillment,
                "dispatch_status": "awaiting_approval" if fulfillment == "delivery" else "none",
                "origin": "WhatsApp assistant"}
        if fulfillment == "delivery":
            vals["note"] = f"Delivery to: {delivery_address}"
            vals["delivery_address_text"] = delivery_address
        if rx_used:
            refs = self.c.search_read("pharmacy.prescription", [["id", "in", list(rx_used)]],
                                      ["name", "invoice_number"])
            vals["note"] = ((vals.get("note") or "") + " | Prescription(s): " + ", ".join(
                f"{r['name']} (invoice {r['invoice_number']})" for r in refs)).strip(" |")
        try:
            oid = self.c.create("sale.order", vals)
        except OdooError as e:
            return err("invalid_input", str(e))
        if rx_used:      # one prescription = one order: it cannot be used again
            self.c.write("pharmacy.prescription", list(rx_used),
                         {"sale_order_id": oid, "state": "used"})
        review = "none"
        if rx_review:
            # Only attach a prescription the customer sent JUST NOW for one of THIS order's
            # rx products (photo-first flow); never a stale or unrelated one - otherwise ask
            # for the photo for this order. A pharmacist then approves/rejects it in Odoo.
            import datetime as _dt
            cutoff = (_dt.datetime.utcnow() - _dt.timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
            sent = self.c.search("pharmacy.prescription", [
                ["partner_id", "=", customer_id], ["sale_order_id", "=", False],
                ["state", "in", ["submitted", "auto_verified"]],
                ["create_date", ">=", cutoff],
                ["matched_product_ids", "in", rx_pids]], order="id desc", limit=3) if rx_pids else []
            if sent:
                self.c.write("pharmacy.prescription", sent, {"sale_order_id": oid, "state": "submitted"})
            review = "pending" if sent else "awaiting_photo"
            self.c.write("sale.order", [oid], {"rx_review_state": review})
        o = self.c.read("sale.order", oid, ["name", "state", "amount_total"])[0]
        return {"order_id": oid, "order_ref": o["name"], "state": o["state"],
                "total": o["amount_total"], "branch_id": chosen,
                "branch_name": b["name"], "fulfillment": fulfillment, "rx_review": review,
                "next_step": ("prescription review by the pharmacist" if review != "none"
                              else "customer pays via the payment link, then it is confirmed")}

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

    # ---- customers (not LLM tools: the chat channel's fixed identify/register flow) ------
    _PARTNER_FIELDS = ["name", "phone", "mobile", "whatsapp_number", "street", "street2", "city"]

    def _profile(self, p: dict) -> dict:
        street = ", ".join(x for x in (p.get("street"), p.get("street2")) if x)
        # the city is only added when the address text doesn't already name one
        addr = street if guess_city(street) else ", ".join(x for x in (street, p.get("city")) if x)
        return {"customer_id": p["id"], "name": p["name"], "phone": p.get("phone") or "",
                "alt_phone": p.get("mobile") or "", "address": addr,
                "city": p.get("city") or "", "whatsapp_number": p.get("whatsapp_number") or ""}

    def find_customer(self, phone: str) -> dict | None:
        """Customer whose WhatsApp number, phone or mobile is this number (any format:
        0501234567, +966 50 123 4567, 966501234567...), or None."""
        norm = normalize_phone(phone)
        if not norm:
            return None
        rows = self.c.search_read("res.partner", [
            "|", "|", ["whatsapp_number", "=", norm], ["phone", "ilike", norm[-4:]],
            ["mobile", "ilike", norm[-4:]]], self._PARTNER_FIELDS, order="id", limit=50)
        for p in rows:
            if norm in {p.get("whatsapp_number"), normalize_phone(p.get("phone")),
                        normalize_phone(p.get("mobile"))}:
                return self._profile(p)
        return None

    def get_customer(self, customer_id: int) -> dict | None:
        rows = self.c.search_read("res.partner", [["id", "=", customer_id]], self._PARTNER_FIELDS)
        return self._profile(rows[0]) if rows else None

    def register_customer(self, name: str, phone: str, address: str,
                          alt_phone: str | None = None) -> dict:
        """Create the Odoo contact for a new customer (or return the existing one for this
        number), so later orders recognise them and invoices carry their details."""
        existing = self.find_customer(phone)
        if existing:
            return existing
        norm = normalize_phone(phone)
        sa = self.c.search("res.country", [["code", "=", "SA"]], limit=1)
        pid = self.c.create("res.partner", {
            "name": name.strip(), "phone": "+" + norm, "whatsapp_number": norm,
            "mobile": ("+" + normalize_phone(alt_phone)) if alt_phone and normalize_phone(alt_phone) else False,
            "street": address.strip(), "city": guess_city(address) or False,
            "country_id": sa[0] if sa else False, "customer_rank": 1,
            "registered_via": "whatsapp",
            "comment": "Registered by the WhatsApp ordering assistant.",
        })
        return self.get_customer(pid)

    def update_customer_address(self, customer_id: int, address: str) -> dict | None:
        self.c.write("res.partner", [customer_id], {"street": address.strip(), "street2": False,
                                                    "city": guess_city(address) or False})
        return self.get_customer(customer_id)

    # ---- prescriptions (not an LLM tool: the chat channel calls these) --------
    def _rx_required(self, flags, product_id):
        return err("prescription_required",
                   f"{flags['name']} needs a prescription: ask the customer to attach a photo of a "
                   f"computerised prescription dated within the last "
                   f"{rx.PRESCRIPTION_MAX_AGE_DAYS} days that shows this medicine and an invoice "
                   f"number", product_id=product_id)

    # ---- prescription review results, confirmation and payment (chat channels) --------
    def attach_prescription(self, order_id: int, prescription_id: int, customer_id: int) -> dict:
        """A photo sent after the quotation: put it on that order -> pending pharmacist review."""
        if not self.c.search("sale.order", [["id", "=", order_id], ["partner_id", "=", customer_id]]):
            return err("not_found", "no such order for this customer")
        self.c.write("pharmacy.prescription", [prescription_id],
                     {"sale_order_id": order_id, "state": "submitted"})
        self.c.write("sale.order", [order_id], {"rx_review_state": "pending"})
        o = self.c.read("sale.order", order_id, ["name", "amount_total"])[0]
        return {"order_id": order_id, "order_ref": o["name"], "total": o["amount_total"]}

    def order_summary(self, order_id: int) -> dict:
        o = self.c.read("sale.order", order_id, [
            "name", "partner_id", "amount_total", "fulfillment_type", "delivery_address_text",
            "warehouse_id", "rx_review_state", "rx_review_note", "state", "payment_link"])[0]
        lines = self.c.search_read("sale.order.line", [["order_id", "=", order_id],
                                                       ["display_type", "=", False]],
                                   ["name", "product_uom_qty", "price_total"])
        return {"order_id": order_id, "order_ref": o["name"], "customer_id": o["partner_id"][0],
                "total": o["amount_total"], "fulfillment": o["fulfillment_type"],
                "address": o["delivery_address_text"] or "",
                "branch_name": (o["warehouse_id"] or [0, ""])[1],
                "review": o["rx_review_state"], "note": o["rx_review_note"] or "",
                "state": o["state"], "payment_link": o["payment_link"] or "",
                "lines": [{"name": l["name"].split("\n")[0], "qty": l["product_uom_qty"],
                           "total": l["price_total"]} for l in lines]}

    def pending_notifications(self, customer_id: int | None = None) -> list[dict]:
        """Review results the pharmacist decided that the customer has not been told yet."""
        dom = [["notify_state", "=", "pending"]]
        if customer_id:
            dom.append(["partner_id", "=", customer_id])
        out = []
        for oid in self.c.search("sale.order", dom, order="id"):
            s = self.order_summary(oid)
            p = self.c.read("res.partner", s["customer_id"], ["name", "whatsapp_number", "phone"])[0]
            s.update(customer_name=p["name"],
                     whatsapp_number=p["whatsapp_number"] or normalize_phone(p["phone"]))
            out.append(s)
        return out

    def mark_notified(self, order_id: int) -> None:
        self.c.write("sale.order", [order_id], {"notify_state": "sent"})

    def payment_link(self, order_id: int) -> str:
        """Link the customer pays from: the quotation's Odoo customer page (online payment is
        enabled there once a payment provider is configured - payment capture/recording is the
        next phase)."""
        base = (self.c.search_read("ir.config_parameter", [["key", "=", "web.base.url"]],
                                   ["value"]) or [{"value": ""}])[0]["value"].rstrip("/")
        path = self.c.execute_kw("sale.order", "get_portal_url", [[order_id]])
        url = base + path
        self.c.write("sale.order", [order_id], {"payment_link": url, "payment_state_wa": "link_sent"})
        return url

    def customer_confirms(self, order_id: int, customer_id: int) -> dict:
        """The customer accepted an approved quotation: record it and hand back the payment link."""
        if not self.c.search("sale.order", [["id", "=", order_id], ["partner_id", "=", customer_id],
                                            ["state", "in", ["draft", "sent"]]]):
            return err("not_found", "no open quotation for this customer")
        self.c.write("sale.order", [order_id], {"customer_confirmed": True})
        return {**self.order_summary(order_id), "payment_link": self.payment_link(order_id)}

    def customer_cancels(self, order_id: int, customer_id: int) -> dict:
        if not self.c.search("sale.order", [["id", "=", order_id], ["partner_id", "=", customer_id],
                                            ["state", "in", ["draft", "sent"]]]):
            return err("not_found", "no open quotation for this customer")
        self.c.execute_kw("sale.order", "action_cancel", [[order_id]])
        return {"order_id": order_id, "cancelled": True}

    def valid_prescription(self, customer_id, product_id) -> int | None:
        """Id of an unused, PHARMACIST-approved prescription of this customer, dated within the
        allowed window, that lists this product — or None (OCR alone never approves)."""
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
        # Prescriptions name the ACTIVE INGREDIENT ("Metformin - 500 mg tablet") while our
        # catalogue lists brands ("DIALON 500 MG", "METFORMIN HYDROCHLORIDE"): match each Rx line
        # by ingredient set, narrowed to the prescribed strength when we stock it.
        variant_of = {r["id"]: r for r in rows}
        named = {m.get("template_id") for m in res["medications"]}
        for item in ingr.prescription_items(text, self._ingr):
            pool = self._ingr.same_ingredients(item["set"])
            if item["strength_mg"]:
                pool = [t for t in pool if self._ingr.products[t]["strength_mg"] == item["strength_mg"]] or pool
            found = [variant_of[t] for t in pool if t in variant_of]
            allowed = [r for r in found if not r.get("is_scheduled")]
            if not found or any(r["id"] in named for r in allowed):
                continue
            label = " + ".join(sorted(item["set"])).upper() + \
                (f" {ingr.fmt_mg(item['strength_mg'])}" if item["strength_mg"] else "")
            res["medications"].append({
                "product_id": (allowed or found)[0]["product_variant_id"][0],
                "template_id": (allowed or found)[0]["id"],
                "name_en": f"{label} ({len(allowed)} product(s) in our catalogue)",
                "name_ar": "", "is_scheduled": not allowed,
                "requires_prescription": any(_needs_prescription(r) for r in allowed),
                "matched": "active ingredient", "score": 100.0,
                "pool": [r["product_variant_id"][0] for r in allowed]})
        if res["medications"] and "no_medication" in res["problems"]:
            res["problems"].remove("no_medication")
        res["ok"] = not res["problems"]
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
        for m in meds:
            if m.get("pool"):              # ingredient match: already the prescribed strength's products
                unlocked.update(m["pool"])
                continue
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
                                  ["name", "state", "amount_total", "delivery_status",
                                   "rx_review_state", "payment_state_wa"],
                                  order="id desc", limit=10)
        if not rows:
            return err("not_found", "no matching order")
        orders = [{"order_id": r["id"], "name": r["name"], "state": r["state"],
                   "total": r["amount_total"],
                   "dispatch_state": r.get("delivery_status") or "none",
                   "prescription_review": r.get("rx_review_state") or "none",
                   "payment": r.get("payment_state_wa") or "none"}
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
