"""Contract -> Odoo gateway.

Implements each of the seven contract actions against the real Odoo 18 instance.
`product_id` in the contract is an Odoo product.product (variant) id; `branch_id` is a
stock.warehouse id. Every function returns either a success dict (matching the schema's
`returns`) or {"error": {"code", "message", "details"}}.

The agent and the FastAPI app call these functions; tests call them directly.
"""
from __future__ import annotations

import base64
import os
import re
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from odoo_client import OdooClient, OdooError  # noqa: E402
from fuzzy_search import FuzzyIndex, normalize  # noqa: E402
from needs import NEEDS, detect_need  # noqa: E402
import ingredients as ingr  # noqa: E402
import prescription as rx  # noqa: E402

RESULTS = ROOT / "results"
# Choosing a medicine for a symptom is a licensed pharmacist's call (pharmacy practice rules), so the
# assistant does not list products for a need ("something for a sore throat") unless the pharmacy
# deliberately re-enables the pharmacist-approved table in adapter/needs.py.
SYMPTOM_SUGGESTIONS = os.environ.get("SYMPTOM_SUGGESTIONS", "0") == "1"
# cosmetics, not a medicine choice: still listed (moisturiser for dry skin, sunscreen)
COSMETIC_NEEDS = {"dry_skin", "sun_protection"}
SYMPTOM_HINT = ("We do not suggest medicines for symptoms: a licensed pharmacist must advise. Tell the "
                "customer to ask the pharmacist at any branch, or to name the product they want.")
HANDOVER_LOG = RESULTS / "handovers.jsonl"

SEARCH_FIELDS = ["name", "default_code", "x_name_ar", "x_brand",
                 "x_ingredient_en", "x_ingredient_ar", "sfda_scientific_name"]


def _needs_prescription(flags: dict) -> bool:
    """Prescription/restricted from our own flags OR the SFDA registry import
    (sfda_requires_prescription, set from SFDA "طريقة الصرف = يحتاج الى وصفة طبية")."""
    return bool(flags.get("x_is_restricted") or flags.get("x_requires_prescription")
                or flags.get("sfda_requires_prescription")
                or flags.get("dispensing_class") in ("prescription", "scheduled"))


# prescription states that let the agent order/hold the medicines found on it
RX_OK_STATES = ["verified"]                # pharmacist-approved only; OCR is a pre-check
RX_PRODUCT_FIELDS = ["name", "x_name_ar", "x_brand", "sfda_scientific_name", "product_variant_id",
                     "x_is_restricted", "x_requires_prescription", "sfda_requires_prescription",
                     "is_scheduled", "dispensing_class"]


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
                ["name", "code", "lot_stock_id"] + self._BRANCH_FIELDS)
            self._branch_cache = [
                {"branch_id": w["id"], "name": w["name"], "loc_id": w["lot_stock_id"][0],
                 **{f.removeprefix("pharmacy_"): w.get(f) or None for f in self._BRANCH_FIELDS}}
                for w in whs]
        return self._branch_cache

    def _branch(self, branch_id: int):
        return next((b for b in self.branches() if b["branch_id"] == branch_id), None)

    _BRANCH_FIELDS = ["pharmacy_city", "pharmacy_district", "pharmacy_address", "pharmacy_phone",
                      "pharmacy_hours", "pharmacy_maps_url", "pharmacy_delivery"]

    # ---- 0. list_branches ----------------------------------------------------
    def list_branches(self, city: str | None = None):
        """Branches with city, district, address, hours, phone and whether they deliver. `city`
        (any spelling: الرياض / riyadh) narrows the list; no branch there -> all, with a note."""
        rows = [{k: v for k, v in b.items() if k != "loc_id" and v is not None}
                for b in self.branches()]
        if city and city.strip():
            want = guess_city(city) or city.strip()
            hit = [b for b in rows if normalize(b.get("city") or "") == normalize(want)
                   or normalize(want) in normalize(b["name"])]
            if hit:
                return {"branches": hit}
            return {"branches": rows, "note": f"no branch in {city}; these are all our branches - "
                                               f"delivery reaches cities near them"}
        return {"branches": rows}

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
             "is_scheduled", "schedule_class", "dispensing_class"])
        return rows[0] if rows else None

    # ---- 1. search_product ---------------------------------------------------
    _CAND_FIELDS = ["name", "x_name_ar", "x_brand", "x_is_restricted", "x_requires_prescription",
                    "sfda_requires_prescription", "is_scheduled", "schedule_class",
                    "sfda_scientific_name", "x_ingredient_en", "list_price", "product_variant_id",
                    "dispensing_class"]

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
            "dispensing": t.get("dispensing_class") or "otc",
        }

    def _rows(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        rows = {r["id"]: r for r in self.c.search_read("product.template", [["id", "in", ids]],
                                                       self._CAND_FIELDS)}
        return [rows[i] for i in ids if i in rows]

    def search_product(self, query: str = "", branch_id: int | None = None, limit: int = 5,
                       active_ingredient: str | None = None, need: str | None = None):
        """Find what the customer means: by name or active ingredient (_find), and - when nothing
        is named - by NEED ("something for a headache"): the model passes `need` (or the customer's
        words reveal one, needs.detect_need) and the fixed NEEDS table picks over-the-counter products.
        A named product always wins; the model never chooses a medicine for a need itself."""
        need = need if need in NEEDS else None
        if (query or "").strip() in NEEDS:       # the model put the need's name in query
            need, query = need or query.strip(), ""
        if not (query and query.strip()) and not active_ingredient:
            if not need:
                return err("invalid_input", "query or need is required")
            if not SYMPTOM_SUGGESTIONS and need not in COSMETIC_NEEDS:
                return {"candidates": [], "symptom_request": need, "hint": SYMPTOM_HINT}
            return self._need_search(need, branch_id, limit)
        found = self._find(query, branch_id, limit, active_ingredient)
        # a real match (exact name or active ingredient) wins; loose sound-alikes don't beat a need
        strong = any(c.get("match") in ("exact", "active ingredient")
                     for c in (found.get("candidates") or []) + (found.get("alternatives") or []))
        if strong or active_ingredient:
            return found
        key = need or detect_need(query)
        if not key and (found.get("candidates") or found.get("alternatives")):
            return found
        if key and not SYMPTOM_SUGGESTIONS and key not in COSMETIC_NEEDS:   # the pharmacist's call
            return {"candidates": [], "symptom_request": key, "hint": SYMPTOM_HINT}
        if key:
            by_need = self._need_search(key, branch_id, limit)
            if by_need.get("candidates"):
                return by_need
        return found

    def _need_search(self, key: str, branch_id: int | None, limit: int) -> dict:
        """Over-the-counter products for a customer need, from the fixed NEEDS table: each term's
        direct matches, interleaved (paracetamol, ibuprofen, paracetamol, ...), never Rx/scheduled."""
        lim = max(1, min(limit, 20))
        per_term = []
        for term in NEEDS[key]["terms"]:
            r = self._find(term, branch_id, lim, None)
            per_term.append([c for c in (r.get("candidates") or [])
                             if c.get("match") in ("exact", "active ingredient")
                             and not (c.get("requires_prescription") or c.get("is_restricted")
                                      or c.get("is_scheduled"))])
        cands, seen = [], set()
        for row in zip_longest(*per_term):
            for c in row:
                if c and c["product_id"] not in seen:
                    seen.add(c["product_id"])
                    cands.append(c)
        label = {"ar": NEEDS[key]["ar"], "en": NEEDS[key]["en"]}
        if not cands:
            return {"candidates": [], "need": key, "need_label": label,
                    "hint": "Nothing over-the-counter in stock for this need - offer the pharmacist."}
        return {"candidates": cands[:lim], "need": key, "need_label": label,
                "hint": (f"Over-the-counter products we carry for {label['en']}. Show them; never say which "
                         f"one to take or how much - dose and suitability questions go to the pharmacist.")}

    def _find(self, query: str = "", branch_id: int | None = None, limit: int = 5,
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
            unfiltered = self._find(query, None, limit, active_ingredient)
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
                     branch_id=None, delivery_address=None, cart_id=None):
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
        cart = self._cart_ids(customer_id, cart_id) if cart_id else []   # only the cart being checked out
        try:
            if cart:                             # check out the chat cart: the same quotation
                vals.update(wa_cart=False, order_line=[(5, 0, 0)] + lines)
                self.c.write("sale.order", cart[:1], vals)
                oid = cart[0]
            else:
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
                "lines": [{"name": re.sub(r"^\[[^]]*\]\s*", "", l["name"].split("\n")[0]), "qty": l["product_uom_qty"],
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
        try:                                 # approved quote -> confirmed sales order, then payment
            self.c.execute_kw("sale.order", "action_confirm", [[order_id]])
        except OdooError:
            pass                             # keep the confirmation recorded even if confirm validates
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
            # used = on an order, or held by another customer; a rejected upload (unreadable,
            # a test, the wrong patient) never blocks the real owner from sending it again
            reused = self.c.search_count("pharmacy.prescription", [
                ["invoice_number", "=", res["invoice_number"]], ["state", "!=", "rejected"],
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
            "directions": "\n".join(f"{d['medicine']}: {d['directions']}"
                                    for d in res.get("directions") or []) or False,
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
        out = {"orders": orders}
        if customer_id:
            try:
                req = self.open_requests(customer_id)
            except Exception:  # noqa: BLE001
                req = []
            if req:
                out["requests"] = req
        return out

    # ---- 8. get_product_details ----------------------------------------------
    _DETAIL_FIELDS = ["name", "x_name_ar", "x_brand", "sfda_scientific_name", "x_ingredient_en",
                      "sfda_strength", "sfda_dosage_form", "sfda_route", "sfda_pack", "list_price",
                      "dispensing_class", "is_scheduled", "sfda_storage", "storage_note",
                      "storage_class", "storage_min_c", "storage_max_c", "storage_has_min",
                      "storage_has_max", "storage_protect_light", "storage_protect_moisture",
                      "storage_no_freeze", "cold_chain", "shelf_life_months", "sfda_manufacturer",
                      "manufacturer_country", "sfda_marketing_company", "sfda_reg_no",
                      "sfda_marketing_status", "product_variant_id"]
    _STORAGE_TEXT = {
        "fridge": ("Keep in the fridge", "يحفظ في الثلاجة"),
        "freezer": ("Keep frozen", "يحفظ في المجمد (الفريزر)"),
        "cool": ("Keep cool", "يحفظ في مكان بارد"),
        "room": ("Room temperature", "يحفظ في درجة حرارة الغرفة"),
    }
    _DISPENSING = {
        "otc": "over the counter - no prescription needed",
        "prescription": "prescription only - it can be ordered here and a pharmacist reviews the "
                        "customer's prescription (photo) before it is dispensed",
    }

    def _details(self, t: dict) -> dict:
        from_text = bool(t.get("storage_note") or t.get("sfda_storage"))
        storage = None
        if t.get("storage_class") and t["storage_class"] != "unknown":
            en, ar = self._STORAGE_TEXT[t["storage_class"]]
            storage = {"class": t["storage_class"], "summary_en": en, "summary_ar": ar,
                       "min_c": t["storage_min_c"] if t.get("storage_has_min") else None,
                       "max_c": t["storage_max_c"] if t.get("storage_has_max") else None,
                       "keep_away_from_light": t.get("storage_protect_light") or False,
                       "keep_dry": t.get("storage_protect_moisture") or False,
                       "do_not_freeze": t.get("storage_no_freeze") or False,
                       "label_text": t.get("storage_note") or t.get("sfda_storage")}
        elif from_text:
            storage = {"class": "see label_text", "label_text": t.get("storage_note") or t.get("sfda_storage")}
        made_by = ", ".join(dict.fromkeys(
            x.strip() for x in re.findall(r"الاسم\s*:\s*([^|\n;]+)", t.get("sfda_manufacturer") or "")))
        out = {
            "product_id": t["product_variant_id"][0],
            "name_en": t["name"], "name_ar": t.get("x_name_ar") or "",
            "brand": t.get("x_brand") or "",
            "active_ingredient": t.get("sfda_scientific_name") or t.get("x_ingredient_en") or None,
            "strength": t.get("sfda_strength") or None,
            "dosage_form": t.get("sfda_dosage_form") or None,
            "route": t.get("sfda_route") or None,
            "pack": t.get("sfda_pack") or None,
            "price_sar": t.get("list_price"),
            "dispensing": t.get("dispensing_class") or "otc",
            "dispensing_meaning": self._DISPENSING.get(t.get("dispensing_class") or "otc"),
            "storage": storage,
            "cold_chain_delivery": bool(t.get("cold_chain")),
            "shelf_life_months": t.get("shelf_life_months") or None,
            "manufacturer": made_by or (t.get("sfda_marketing_company") or None),
            "made_in": t.get("manufacturer_country") or None,
            "sfda_registration": t.get("sfda_reg_no") or None,
        }
        # a fact we do not hold is spelled out, so the model cannot "fill it in" from memory
        for k in ("storage", "pack", "strength", "dosage_form", "shelf_life_months", "made_in",
                  "manufacturer"):
            if out.get(k) in (None, "", []):
                out[k] = "NOT RECORDED - the pharmacist will confirm"
        return {k: v for k, v in out.items() if v not in (None, "", [])}

    def get_product_details(self, product_id: int | None = None, product_ids: list | None = None):
        """Label facts for up to 3 products (from search_product): what it is, strength, form,
        pack, price, prescription status, storage / fridge / temperature, shelf life, maker and
        country. Never dose, use or suitability. Scheduled -> scheduled_item (nothing disclosed)."""
        ids = [i for i in ([product_id] if product_id else []) + list(product_ids or [])
               if isinstance(i, int)][:3]
        if not ids:
            return err("invalid_input", "product_id is required")
        rows = self.c.search_read("product.product", [["id", "in", ids]], self._DETAIL_FIELDS)
        by = {r["id"]: r for r in rows}
        out = []
        for i in ids:
            t = by.get(i)
            if not t:
                return err("not_found", f"no product with id {i}")
            if t.get("is_scheduled") or t.get("dispensing_class") == "scheduled":
                return err("scheduled_item", f"{t['name']} is a scheduled/controlled substance - "
                           f"no details via the assistant; refer to a pharmacist", product_id=i)
            t["product_variant_id"] = [i]
            out.append(self._details(t))
        res = {"products": out}
        if any("NOT RECORDED" in str(p) for p in out):
            res["hint"] = ("NOT RECORDED facts are not in our system: say the pharmacist will "
                           "confirm them; never answer them from memory.")
        return res

    # ---- 9. get_order_details --------------------------------------------------
    _ORDER_STATE = {"draft": "received - waiting for the pharmacy to confirm",
                    "sent": "waiting for the pharmacy to confirm",
                    "sale": "confirmed", "cancel": "cancelled"}

    def get_order_details(self, order_id=None, order_ref=None, customer_id=None):
        """One of the customer's orders in full: items, quantities, prices, pickup branch or
        delivery address, prescription review (with the pharmacist's note), payment, delivery
        progress, cold-chain flag. With no order given: the latest one."""
        if not customer_id:
            return err("customer_required", "identify or register the customer first")
        dom = [["partner_id", "=", customer_id]]
        if order_id:
            dom.append(["id", "=", int(order_id)])
        elif order_ref:
            # exact: "S46", "s00046", "46" -> S00046 (never a substring: 46 must not match S00146)
            ref = str(order_ref).strip().upper()
            digits = re.sub(r"\D", "", ref)
            names = {ref} | ({f"S{int(digits):05d}"} if digits else set())
            dom.append(["name", "in", sorted(names)])
        rows = self.c.search_read("sale.order", dom, [
            "name", "state", "date_order", "amount_total", "fulfillment_type", "delivery_address_text",
            "warehouse_id", "rx_review_state", "rx_review_note", "payment_state_wa", "dispatch_status",
            "delivery_status", "needs_cold_chain", "customer_confirmed", "client_order_ref"],
            order="id desc", limit=1)
        if not rows:
            return err("not_found", "no such order for this customer")
        o = rows[0]
        lines = self.c.search_read("sale.order.line", [["order_id", "=", o["id"]],
                                                       ["display_type", "=", False]],
                                   ["product_id", "name", "product_uom_qty", "price_total"])
        out = {
            "order_id": o["id"], "order_ref": o["name"], "date": (o["date_order"] or "")[:10],
            "status": self._ORDER_STATE.get(o["state"], o["state"]),
            "is_hold": (o.get("client_order_ref") or "").startswith("HOLD-"),
            "items": [{"product_id": l["product_id"][0] if l["product_id"] else None,
                       "name": re.sub(r"^\[[^]]*\]\s*", "", l["name"].split("\n")[0]), "qty": l["product_uom_qty"],
                       "total_sar": l["price_total"]} for l in lines],
            "total_sar": o["amount_total"],
            "fulfillment": o.get("fulfillment_type") or "pickup",
            "branch": (o["warehouse_id"] or [0, ""])[1],
            "prescription_review": o.get("rx_review_state") or "none",
            "payment": o.get("payment_state_wa") or "none",
            "delivery_progress": o.get("dispatch_status") or "none",
            "cold_chain": bool(o.get("needs_cold_chain")),
            "can_cancel": o["state"] in ("draft", "sent"),
        }
        if o.get("delivery_address_text"):
            out["delivery_address"] = o["delivery_address_text"]
        if o.get("rx_review_note") and o.get("rx_review_state") in ("rejected", "approved"):
            out["pharmacist_note"] = o["rx_review_note"]
        return out

    # ---- 10. cancel_order ------------------------------------------------------
    def cancel_order(self, order_id=None, order_ref=None, reason=None, customer_id=None):
        """Cancel one of the customer's open quotations (not yet confirmed / paid). A confirmed
        order cannot be cancelled here: the customer is handed to staff."""
        if not (order_id or order_ref):
            return err("invalid_input", "which order? pass the order_ref the customer confirmed")
        d = self.get_order_details(order_id, order_ref, customer_id)
        if "error" in d:
            return d
        if not d["can_cancel"]:
            return err("not_cancellable", f"{d['order_ref']} is {d['status']}: only a pharmacy "
                       f"colleague can cancel it now - hand over with reason complaint",
                       order_ref=d["order_ref"])
        if reason:
            self.c.write("sale.order", [d["order_id"]], {"cancel_reason_wa": str(reason)[:200]})
        self.c.execute_kw("sale.order", "action_cancel", [[d["order_id"]]])
        return {"order_ref": d["order_ref"], "cancelled": True}

    # ---- 11. list_prescriptions ------------------------------------------------
    _RX_STATE = {"draft": "received", "submitted": "waiting for the pharmacist",
                 "auto_verified": "checked automatically, waiting for the pharmacist",
                 "verified": "approved by the pharmacist", "used": "used for an order",
                 "rejected": "rejected"}

    def list_prescriptions(self, customer_id=None):
        """The customer's prescriptions: date, review status, medicines found, linked order."""
        if not customer_id:
            return err("customer_required", "identify or register the customer first")
        rows = self.c.search_read("pharmacy.prescription", [["partner_id", "=", customer_id]],
                                  ["name", "prescription_date", "state", "matched_product_ids",
                                   "sale_order_id", "create_date"], order="id desc", limit=5)
        if not rows:
            return {"prescriptions": [], "hint": "No prescription on file: they can send a photo "
                                                 "of it here."}
        pids = sorted({p for r in rows for p in r["matched_product_ids"]})
        names = {p["id"]: (p.get("x_name_ar") or p["name"]) for p in self.c.search_read(
            "product.product", [["id", "in", pids]], ["name", "x_name_ar", "is_scheduled"])
            if not p.get("is_scheduled")} if pids else {}
        return {"prescriptions": [{
            "received": (r["create_date"] or "")[:10],
            "prescription_date": r["prescription_date"] or None,
            "status": self._RX_STATE.get(r["state"], r["state"]),
            "medicines": [names[p] for p in r["matched_product_ids"] if p in names],
            "order_ref": r["sale_order_id"][1] if r["sale_order_id"] else None}
            for r in rows]}

    # ---- 12. request_stock_alert ---------------------------------------------
    def request_stock_alert(self, product_id, branch_id=None, customer_id=None):
        """'Tell me when it's back': recorded in Odoo (Pharmacy -> Back-in-stock requests); the
        customer gets one WhatsApp message when it is available again."""
        if not customer_id:
            return err("customer_required", "identify or register the customer first")
        flags = self._product_flags(product_id)
        if not flags:
            return err("not_found", f"no product with id {product_id}")
        if flags.get("is_scheduled"):
            return err("scheduled_item", f"{flags['name']} is a scheduled/controlled substance",
                       product_id=product_id)
        b = self._branch(branch_id) if branch_id else None
        if branch_id and not b:
            return err("not_found", f"no branch with id {branch_id} - call list_branches")
        avail = self._avail_at(product_id, b["loc_id"]) if b else \
            sum(self._avail_at(product_id, x["loc_id"]) for x in self.branches())
        if avail > 0:
            return {"already_available": True,
                    "where": b["name"] if b else "in stock - check_stock shows the branches",
                    "hint": "It is in stock now: offer to order it instead."}
        dom = [["partner_id", "=", customer_id], ["product_id", "=", product_id],
               ["state", "=", "waiting"]]
        existing = self.c.search("pharmacy.stock.alert", dom, limit=1)
        if not existing:
            self.c.create("pharmacy.stock.alert", {"partner_id": customer_id, "product_id": product_id,
                                                   "warehouse_id": b["branch_id"] if b else False})
        return {"recorded": True, "product": flags.get("x_name_ar") or flags["name"],
                "branch": b["name"] if b else "any branch",
                "next_step": "we message the customer here once it is back in stock"}

    def due_stock_alerts(self) -> list[dict]:
        """Waiting back-in-stock requests whose product is available again (for the notifier)."""
        out = []
        for a in self.c.search_read("pharmacy.stock.alert", [["state", "=", "waiting"]],
                                    ["partner_id", "product_id", "warehouse_id"]):
            pid = a["product_id"][0]
            bs = [self._branch(a["warehouse_id"][0])] if a["warehouse_id"] else self.branches()
            where = [b["name"] for b in bs if b and self._avail_at(pid, b["loc_id"]) > 0]
            if where:
                p = self.c.read("res.partner", a["partner_id"][0], ["name", "whatsapp_number", "phone"])[0]
                out.append({"alert_id": a["id"], "customer_id": a["partner_id"][0],
                            "customer_name": p["name"],
                            "whatsapp_number": p["whatsapp_number"] or normalize_phone(p["phone"]),
                            "product": a["product_id"][1], "branches": where})
        return out

    def mark_stock_alert_sent(self, alert_id: int) -> None:
        self.c.write("pharmacy.stock.alert", [alert_id], {"state": "notified"})

    # ---- 13. update_delivery_address -------------------------------------------
    def update_delivery_address(self, address: str, customer_id=None):
        """Save a new delivery address on the customer's Odoo contact (future orders use it)."""
        if not customer_id:
            return err("customer_required", "identify or register the customer first")
        if not address or len(address.strip()) < 6:
            return err("invalid_input", "ask the customer for the full address (district, street)")
        p = self.update_customer_address(customer_id, address)
        return {"saved": True, "address": p["address"] if p else address.strip()}

    # ---- chat cart: a draft quotation (wa_cart) kept in step with the chat ----------------
    def _cart_ids(self, customer_id, cart_id=None) -> list[int]:
        if not customer_id:
            return []
        dom = [["partner_id", "=", customer_id], ["wa_cart", "=", True], ["state", "=", "draft"]]
        if cart_id:
            dom.append(["id", "=", cart_id])
        return self.c.search("sale.order", dom, order="id desc", limit=1)

    def cart_sync(self, customer_id, items) -> dict:
        """Make the customer's cart quotation hold exactly `items` [{product_id, qty}]: created on
        the first item, emptied (cancelled) when items is empty. A pharmacist answering a
        question about the cart sees these lines."""
        if not customer_id:
            return err("customer_required", "identify or register the customer first")
        ids = self._cart_ids(customer_id)
        if not items:
            if ids:
                self.c.execute_kw("sale.order", "action_cancel", [ids])
            return {"cart_id": None, "items": []}
        lines = [(0, 0, {"product_id": it["product_id"], "product_uom_qty": it["qty"]}) for it in items]
        if ids:
            self.c.write("sale.order", ids, {"order_line": [(5, 0, 0)] + lines})
            oid = ids[0]
        else:
            oid = self.c.create("sale.order", {"partner_id": customer_id, "wa_cart": True,
                                               "origin": "WhatsApp cart", "order_line": lines})
        return self.get_cart(customer_id) | {"cart_id": oid}

    def get_cart(self, customer_id) -> dict:
        ids = self._cart_ids(customer_id)
        if not ids:
            return {"cart_id": None, "items": []}
        o = self.c.read("sale.order", ids[0], ["name", "amount_untaxed"])[0]
        lines = self.c.search_read("sale.order.line", [["order_id", "=", ids[0]], ["display_type", "=", False]],
                                   ["product_id", "name", "product_uom_qty", "price_subtotal"])
        return {"cart_id": ids[0], "cart_ref": o["name"], "total": o["amount_untaxed"],
                "items": [{"product_id": l["product_id"][0], "qty": int(l["product_uom_qty"]),
                           "name": re.sub(r"^\[[^]]*\]\s*", "", l["name"].split("\n")[0]),
                           "subtotal": l["price_subtotal"]} for l in lines if l["product_id"]]}

    # ---- scheduled medicines: availability only (never ordered, priced or escalated) ------
    def scheduled_availability(self, product_ids=None, name=None) -> dict:
        """Branches that have a scheduled (مقيد) medicine in stock, for the fixed refusal:
        dispensed in person only, with a stamped doctor's prescription. No price."""
        ids = list(product_ids or [])
        if not ids and name:
            ids = self.c.search("product.product", [["is_scheduled", "=", True], "|", "|",
                                                    ["name", "ilike", name], ["x_name_ar", "ilike", name],
                                                    ["sfda_scientific_name", "ilike", name]], limit=5)
        where = []
        for b in self.branches():
            if any(self._avail_at(i, b["loc_id"]) > 0 for i in ids):
                where.append(b["name"])
        names = [r["name"] for r in self.c.read("product.product", ids, ["name"])] if ids else []
        return {"products": names, "branches": where}

    # ---- prescription directions (what the doctor wrote), for a dosage question ------------
    def prescription_directions(self, customer_id, order_id=None) -> dict | None:
        """The directions read from the customer's prescription FOR THIS ORDER: one linked to it,
        or an unlinked one sent in the last 30 days that names one of its items. Never another
        prescription's directions. {prescription_id, order_id, directions: [lines]} or None."""
        if not customer_id or not order_id:
            return None
        base = [["partner_id", "=", customer_id], ["state", "!=", "rejected"], ["directions", "!=", False]]
        fields = ["directions", "sale_order_id"]
        rows = self.c.search_read("pharmacy.prescription", base + [["sale_order_id", "=", order_id]],
                                  fields, order="id desc", limit=1)
        if not rows:                           # sent before the order existed: must name its items
            items = [l["product_id"][0] for l in self.c.search_read(
                "sale.order.line", [["order_id", "=", order_id], ["display_type", "=", False]],
                ["product_id"]) if l["product_id"]]
            since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
            rows = self.c.search_read("pharmacy.prescription", base + [
                ["sale_order_id", "=", False], ["create_date", ">=", since],
                ["matched_product_ids", "in", items]], fields, order="id desc", limit=1) if items else []
        if not rows:
            return None
        return {"prescription_id": rows[0]["id"], "order_id": order_id,
                "directions": [l for l in rows[0]["directions"].splitlines() if l.strip()]}

    # ---- PDPL: explicit consent before health data (prescriptions) is stored ----------------
    CONSENT_VERSION = os.environ.get("PDPL_CONSENT_VERSION", "2026-11-v1")

    def has_health_consent(self, customer_id) -> bool:
        if not customer_id:
            return False
        p = self.c.read("res.partner", customer_id, ["pdpl_health_consent", "pdpl_consent_withdrawn"])[0]
        return bool(p["pdpl_health_consent"]) and not (
            p["pdpl_consent_withdrawn"] and p["pdpl_consent_withdrawn"] >= p["pdpl_health_consent"])

    def record_health_consent(self, customer_id) -> None:
        self.c.write("res.partner", [customer_id], {
            "pdpl_health_consent": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "pdpl_consent_version": self.CONSENT_VERSION, "pdpl_consent_withdrawn": False})

    def withdraw_health_consent(self, customer_id, summary="", conv_id=None, channel=None) -> dict:
        """Consent withdrawn / data request: recorded on the contact and queued for staff (who
        handle access and deletion requests within the PDPL time limits)."""
        self.c.write("res.partner", [customer_id], {
            "pdpl_consent_withdrawn": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
        hid = self.c.create("pharmacy.handover", {
            "reason": "privacy_request", "partner_id": customer_id,
            "summary": (summary or "customer withdrew consent / asked about their data")[:2000],
            "conv_id": conv_id or False, "channel": channel if channel in ("whatsapp", "webchat") else "other"})
        return {"handover_ref": self.c.read("pharmacy.handover", hid, ["name"])[0]["name"]}

    def link_prescription(self, prescription_id: int, order_id: int, customer_id: int) -> None:
        """A prescription sent for a dosage question goes on that order (Pharmacy -> Prescriptions
        shows it with the order); a prescription-only quotation then goes to pharmacist review."""
        if not self.c.search("sale.order", [["id", "=", order_id], ["partner_id", "=", customer_id]]):
            return
        o = self.c.read("sale.order", order_id, ["rx_review_state", "state"])[0]
        self.c.write("pharmacy.prescription", [prescription_id], {"sale_order_id": order_id})
        if o["state"] in ("draft", "sent") and o["rx_review_state"] == "awaiting_photo":
            self.c.write("sale.order", [order_id], {"rx_review_state": "pending"})

    def active_order(self, customer_id) -> dict | None:
        """What a question or handover is about: the chat cart, else the latest open quotation
        or order of the last 30 days. {order_id, order_ref, items:[names], product_ids}."""
        if not customer_id:
            return None
        ids = self._cart_ids(customer_id) or self.c.search("sale.order", [
            ["partner_id", "=", customer_id], ["state", "in", ["draft", "sent", "sale"]],
            ["create_date", ">=", (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")]],
            order="id desc", limit=1)
        if not ids:
            return None
        o = self.c.read("sale.order", ids[0], ["name"])[0]
        lines = self.c.search_read("sale.order.line", [["order_id", "=", ids[0]], ["display_type", "=", False]],
                                   ["product_id", "name"])
        return {"order_id": ids[0], "order_ref": o["name"],
                "items": [re.sub(r"^\[[^]]*\]\s*", "", l["name"].split("\n")[0]) for l in lines],
                "product_ids": [l["product_id"][0] for l in lines if l["product_id"]]}

    # ---- 7. escalate_to_human ------------------------------------------------
    _HANDOVER_REASONS = {"medical_advice", "restricted_item", "complaint", "abusive", "low_confidence",
                         "unclear", "out_of_scope", "prescription", "privacy_request"}

    def escalate_to_human(self, reason, summary, customer_id=None, order_id=None, medicine=None,
                          conv_id=None, channel=None):
        """Hand the conversation to staff: a pharmacy.handover record in Odoo (Pharmacy ->
        Handovers, the staff queue) plus the local log as a fallback if Odoo is unreachable.
        A handover must be solvable: it is tied to an order / cart quotation, or at least names
        the medicine - otherwise needs_context (ask the customer which order or medicine)."""
        if not order_id and not (medicine and str(medicine).strip()):
            return err("needs_context", "Ask the customer which order (number) or which medicine "
                       "this is about; a handover needs one of them.")
        if medicine:
            summary = f"[{medicine}] {summary}"
        RESULTS.mkdir(exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "reason": reason,
                 "summary": summary, "customer_id": customer_id}
        with HANDOVER_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handover_id, ref = None, None
        try:
            vals = {"reason": reason if reason in self._HANDOVER_REASONS else "unclear",
                    "summary": (summary or "-")[:2000], "partner_id": customer_id or False,
                    "conv_id": conv_id or False,
                    "channel": channel if channel in ("whatsapp", "webchat") else "other"}
            if order_id and customer_id and self.c.search(
                    "sale.order", [["id", "=", order_id], ["partner_id", "=", customer_id]]):
                vals["sale_order_id"] = order_id
            handover_id = self.c.create("pharmacy.handover", vals)
            ref = self.c.read("pharmacy.handover", handover_id, ["name"])[0]["name"]
        except Exception:  # noqa: BLE001  (Odoo down / timeout: the local log still has it)
            handover_id = sum(1 for _ in HANDOVER_LOG.open(encoding="utf-8"))
        ack = ("حوّلت طلبك إلى صيدلي/موظف وسيتواصل معك قريبًا."
               " (Handed over to a human pharmacist; they'll follow up shortly.)")
        return {"handover_id": handover_id, "handover_ref": ref, "acknowledgement": ack}

    def open_requests(self, customer_id) -> list[dict]:
        """The customer's recent handovers and their state (shown with track_order)."""
        rows, seen = [], set()
        for r in self.c.search_read("pharmacy.handover", [["partner_id", "=", customer_id],
                                                          ["state", "!=", "cancelled"]],
                                    ["name", "reason", "state", "resolution", "create_date"],
                                    order="id desc", limit=20):
            if r["reason"] not in seen:          # the latest request of each kind
                seen.add(r["reason"])
                rows.append(r)
        state = {"new": "received - no colleague has picked it up yet",
                 "in_progress": "a colleague has taken it and is working on it now",
                 "done": "resolved", "cancelled": "closed"}
        about = {"medical_advice": "medical question", "restricted_item": "controlled medicine",
                 "complaint": "complaint", "abusive": "conversation closed", "prescription": "prescription",
                 "low_confidence": "question for a colleague", "unclear": "question for a colleague",
                 "out_of_scope": "question for a colleague"}
        return [{"ref": r["name"], "about": about.get(r["reason"], r["reason"]),
                 "status": state.get(r["state"], r["state"]),
                 "date": (r["create_date"] or "")[:10],
                 **({"resolution": r["resolution"]} if r.get("resolution") else {})} for r in rows]


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
