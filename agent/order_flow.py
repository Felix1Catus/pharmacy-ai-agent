# -*- coding: utf-8 -*-
"""Fixed order wizard: search results -> pick -> quantity -> pickup/delivery -> branch/address ->
summary -> ✅ confirm -> quotation. No model calls (fast, and the model can never invent an order).

The model's only job in an order is to turn the customer's words into a search_product call. The
results are shown here as a numbered list with prices; whatever the first message already said
("علبتين بنادول استلام من فرع جدة" = 2 boxes, pickup, Jeddah) is filled in, and only the missing
details are asked, each with buttons. The quotation is created only by the ✅ confirm button.
"""
from __future__ import annotations

import re

from gateway import guess_city
from fuzzy_search import _STOP as _FUZZY_STOP
from guards import is_affirmation, normalize, service_intent
from rapidfuzz import fuzz

MAX_QTY = 10
MAX_CHOICES = 5

# a message during the wizard that means "cancel" or "I meant a different product"
_CANCEL = re.compile(r"(?<!\w)(الغاء|إلغاء|الغي|إلغي|انهاء|إنهاء|انهي|إنهي|توقف|خروج|cancel|stop|abort|quit)(?!\w)")
_CORRECTION = re.compile(r"(اقصد|قصدي|ليس هذا|ليس هو|مو هذا|مو هو|مب هذا|مش هذا|مش ده|غلط|خطا|غير هذا|غيره|not this|wrong|i mean|instead|different|other one)")

T = {
    "ar": {
        "list": "لقيت لك 🔎\n{rows}\n\nاختر رقم المنتج أو اضغط عليه 👇",
        "row": "{n}. {name} — {price}{rx}",
        "rx": " (يحتاج وصفة)",
        "no_price": "السعر عند الصيدلي",
        "alt_head": "بنفس المادة الفعالة:",
        "not_found": "ما لقيت «{q}» في منتجاتنا 🙁\nاكتب الاسم بطريقة ثانية، أو المادة الفعالة المكتوبة على العلبة.",
        "need_note": "هذي منتجات بدون وصفة عندنا تخص {need}.\nللجرعة أو أي واحد يناسبك، اسأل الصيدلي 👨‍⚕️",
        "picked": "✅ {name} — {price}",
        "confirm_item": "لقيت: {name} — {price}\nهذا اللي تقصده؟",
        "yes_item": "✅ نعم، هذا", "no_item": "🔎 لا، دوّر غيره",
        "ask_name": "تمام، اكتب اسم الدواء اللي تبغاه 🛒",
        "ask_qty": "كم علبة تبغى؟",
        "bad_qty": "اكتب الكمية رقماً من 1 إلى {max}.",
        "ask_fulfil": "توصيل أو استلام من الفرع؟",
        "delivery": "🚚 توصيل", "pickup": "🏪 استلام من فرع",
        "ask_branch": "من أي فرع تستلم؟",
        "ask_address_saved": "نوصّله لعنوانك المسجّل؟\n📍 {addr}",
        "use_saved": "✅ نعم لعنواني", "other_address": "✏️ عنوان آخر",
        "ask_address": "📍 اكتب عنوان التوصيل (المدينة، الحي، الشارع):",
        "no_stock_branch": "⚠️ {name} غير متوفر بالكمية المطلوبة في {branch}.",
        "no_stock_any": "⚠️ للأسف {name} غير متوفر حالياً بالكمية المطلوبة.",
        "other_branches": "متوفر في:",
        "summary": "🧾 ملخص طلبك:\n{lines}\n💰 الإجمالي قبل الضريبة: {total:.2f} ريال (تُضاف ضريبة القيمة المضافة 15%)\n{how}\n\nنثبّت الطلب؟",
        "line": "• {name} × {qty} — {sub:.2f} ريال{rx}",
        "how_pickup": "🏪 استلام من: {branch}", "how_delivery": "🚚 توصيل إلى: {addr}",
        "confirm": "✅ تأكيد الطلب", "add": "➕ منتج آخر", "cancel": "❌ إلغاء",
        "add_prompt": "تمام، اكتب اسم المنتج الثاني 🛒",
        "cancelled": "تم إلغاء الطلب. اكتب «القائمة» إذا احتجت شيئاً.",
        "rx_note": "\n📝 فيه دواء يحتاج وصفة: بعد التأكيد نطلب صورتها ويراجعها الصيدلي.",
        "oos": "⚠️ للأسف {name} غير متوفر حالياً في فروعنا.\nتبغى نرسل لك رسالة هنا أول ما يتوفر؟",
        "notify": "🔔 بلغني لما يتوفر", "other_product": "🔎 منتج آخر",
        "alert_saved": "تمام ✅ سجّلنا طلبك، ونرسل لك هنا أول ما يتوفر {name}.",
    },
    "en": {
        "list": "Here's what I found 🔎\n{rows}\n\nPick a product number or tap it 👇",
        "row": "{n}. {name} — {price}{rx}",
        "rx": " (prescription)",
        "no_price": "price at the pharmacy",
        "alt_head": "Same active ingredient:",
        "not_found": "I couldn't find “{q}” in our products 🙁\nTry another spelling, or the active ingredient written on the box.",
        "need_note": "These are our over-the-counter products for {need}.\nFor the dose or which one suits you, ask our pharmacist 👨‍⚕️",
        "picked": "✅ {name} — {price}",
        "confirm_item": "I found: {name} — {price}\nIs this the one you mean?",
        "yes_item": "✅ Yes, this one", "no_item": "🔎 No, search again",
        "ask_name": "OK, type the name of the medicine you want 🛒",
        "ask_qty": "How many boxes?",
        "bad_qty": "Type the quantity as a number from 1 to {max}.",
        "ask_fulfil": "Delivery or pickup from a branch?",
        "delivery": "🚚 Delivery", "pickup": "🏪 Pickup",
        "ask_branch": "Which branch will you pick up from?",
        "ask_address_saved": "Deliver to your saved address?\n📍 {addr}",
        "use_saved": "✅ Yes, my address", "other_address": "✏️ Other address",
        "ask_address": "📍 Type the delivery address (city, district, street):",
        "no_stock_branch": "⚠️ {name} isn't available in that quantity at {branch}.",
        "no_stock_any": "⚠️ Sorry, {name} isn't available in that quantity right now.",
        "other_branches": "Available at:",
        "summary": "🧾 Your order:\n{lines}\n💰 Total before VAT: {total:.2f} SAR (15% VAT is added)\n{how}\n\nPlace the order?",
        "line": "• {name} × {qty} — {sub:.2f} SAR{rx}",
        "how_pickup": "🏪 Pickup from: {branch}", "how_delivery": "🚚 Delivery to: {addr}",
        "confirm": "✅ Confirm order", "add": "➕ Add product", "cancel": "❌ Cancel",
        "add_prompt": "OK, type the name of the next product 🛒",
        "cancelled": "Order cancelled. Type 'menu' if you need anything.",
        "rx_note": "\n📝 It includes a prescription medicine: after you confirm we ask for the prescription photo for the pharmacist.",
        "oos": "⚠️ Sorry, {name} is out of stock at all our branches right now.\nShall we message you here as soon as it is back?",
        "notify": "🔔 Notify me", "other_product": "🔎 Another product",
        "alert_saved": "Done ✅ we'll message you here as soon as {name} is back in stock.",
    },
}

# ---- parsing (pure) --------------------------------------------------------------
_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_QTY_WORDS = [  # normalised forms; longest first so "علبتين" wins over "علبه"
    ("عشره", 10), ("عشر", 10), ("تسعه", 9), ("تسع", 9), ("ثمانيه", 8), ("ثمان", 8), ("سبعه", 7),
    ("سبع", 7), ("سته", 6), ("ست", 6), ("خمسه", 5), ("خمس", 5), ("اربعه", 4), ("اربع", 4),
    ("ثلاثه", 3), ("ثلاث", 3), ("تلاته", 3), ("علبتين", 2), ("حبتين", 2), ("اثنين", 2), ("اتنين", 2),
    ("ثنتين", 2), ("كرتونين", 2), ("وحده", 1), ("واحده", 1), ("واحد", 1), ("علبه", 1), ("حبه", 1),
    ("ten", 10), ("nine", 9), ("eight", 8), ("seven", 7), ("six", 6), ("five", 5), ("four", 4),
    ("three", 3), ("two", 2), ("one", 1), ("a box", 1), ("single", 1),
]
_STRENGTH_NUM = re.compile(r"\d+(?:[.,]\d+)?\s*(mg|g|gm|ml|mcg|ملغ|ملجم|مجم|جم|جرام|مل|غ)", re.I)


def parse_qty(text: str) -> int | None:
    """Quantity in a message: '3', 'علبتين', 'ثلاث علب', 'two boxes' (not the 500 of '500 mg')."""
    n = normalize(text).translate(_DIG)
    n = _STRENGTH_NUM.sub(" ", n)
    # a bare number is a quantity only if small: "بنادول 500" is a strength, not 500 boxes
    for m in re.finditer(r"(?<![\d.])(\d{1,3})(?![\d.])", n):
        if 1 <= int(m.group(1)) <= 99:
            return int(m.group(1))
    for w, q in _QTY_WORDS:
        if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", n):
            return q
    return None


_QTY_SET = {w for w, _ in _QTY_WORDS}
_FILLER = {"من", "الى", "على", "عن", "لو", "سمحت", "ابغا", "ودي", "ممكن", "اريد", "عايزه", "want", "need",
           "have", "you", "do", "i", "a", "the", "price", "box", "boxes", "علب", "عبوه", "جده", "الرياض",
           "الدمام", "توصيل", "استلام", "فرع"}


# a phone number is never part of a product name: "رقم جوال صفر خمسة ..." / "رقمي 05..." up to the
# end of that clause is dropped before searching (it pulled in "حليب نان رقم ١" and "ميزان رقمي")
_CONTACT = re.compile(r"(رقم\s*(ال)?(جوال|موبايل|تلفون|هاتف|واتس\w*)|رقمي|جوالي|my (phone |mobile )?number|"
                      r"phone number|mobile number)[^،,.؟?\n]*")
_NUM_WORDS = {"الف", "الفين", "مليون", "ميه", "ميتين", "مائه", "مئه", "عشرين", "ثلاثين", "اربعين", "خمسين",
              "ستين", "سبعين", "ثمانين", "تسعين", "صفر", "زيرو", "وشرين", "انتين", "تين", "اثنعش", "احدعش"}
_TIME = {"اليوم", "بكره", "بكرا", "الحين", "الان", "today", "tomorrow", "now", "tonight", "الليله"}


def _junk(t: str) -> bool:
    bare = t[1:] if t.startswith("و") and t[1:] in _NUM_WORDS else t
    return bare in _NUM_WORDS or t in _TIME or (t.isdigit() and len(t) >= 6)


def _core(text: str) -> str:
    """Product words of a message or a product name, numbers included (Arabic digits -> 0-9).
    Phone numbers, spoken number words and time words are not product words."""
    n = _CONTACT.sub(" ", normalize(text or "").translate(_DIG))
    toks = re.findall(r"[a-zء-يٱ-ۓ]+|\d+(?:[.,]\d+)?", n)
    return " ".join(t for t in toks if t not in _FUZZY_STOP and t not in _QTY_SET and t not in _FILLER
                    and not _junk(t) and (len(t) >= 2 or t.isdigit()))


def parse_fulfilment(text: str) -> str | None:
    n = normalize(text)
    if re.search(r"(توصيل|وصل|توصلون|ارسل|delivery|deliver|ship)", n):
        return "delivery"
    if re.search(r"(استلام|استلم|من الفرع|من فرع|بمر|اجي|pickup|pick up|collect)", n):
        return "pickup"
    return None


def match_branch(text: str, branches: list[dict]) -> int | None:
    """Branch named in the text: by city ('جدة' -> 'Jeddah - Tahlia') or by its own name."""
    low = (text or "").lower()
    city = guess_city(text) or ""
    for b in branches:
        name = b["name"].lower()
        if (city and city.lower() in name) or any(w in low for w in name.replace("-", " ").split() if len(w) > 3):
            return b["branch_id"]
    return None


def parse_choice(text: str, n: int) -> int | None:
    """'2', '٢', '2.', 'رقم اثنين', 'اختيار رقم 2', 'الثاني', 'the second' -> index 0..n-1."""
    t = normalize(text).translate(_DIG)
    m = (re.search(r"(?:رقم|اختيار|الخيار|خيار|option|number|no|#)\s*[:.]?\s*(\d{1,2})", t)
         or re.match(r"^(\d{1,2})(\s*[.)-].*)?$", t.strip(" .)-")))
    if m and 1 <= int(m.group(1)) <= n:
        return int(m.group(1)) - 1
    for i, words in enumerate([("الاول", "اول", "first"),
                               ("الثاني", "ثاني", "اثنين", "اثنان", "ثنين", "second"),
                               ("الثالث", "ثالث", "ثلاثه", "ثلاث", "third"),
                               ("الرابع", "رابع", "اربعه", "اربع", "fourth"),
                               ("الخامس", "خامس", "خمسه", "خمس", "fifth")][:n]):
        if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) for w in words):
            return i
    return None


def _pressed(raw, key):
    n = normalize(raw)
    return any(n == normalize(T[lg][key]) or n == normalize(re.sub(r"^\W+", "", T[lg][key]))
               for lg in T)


class OrderFlow:
    def __init__(self, agent):
        self.a = agent

    def _t(self, conv):
        return T[conv.lang or "ar"]

    def _btns(self, conv, *keys):
        t = self._t(conv)
        return [{"id": k, "label": t[k]} for k in keys]

    @staticmethod
    def _price(t, c):
        return f"{c['price_sar']:.2f} {'ريال' if t is T['ar'] else 'SAR'}" if c.get("price_sar") else t["no_price"]

    # ---- search results -> numbered list -------------------------------------------
    def present(self, conv, result, raw):
        """Show search results (candidates, then same-ingredient alternatives) as a numbered
        list; one clear match is selected straight away."""
        t = self._t(conv)
        cands = [c for c in (result.get("candidates") or []) if not c.get("is_scheduled")]
        # closest to what the CUSTOMER wrote first ("بنادول اكسترا" -> Panadol Extra on top,
        # "بنادول 500" -> Panadol 500 on top: numbers count, quantity words don't)
        core = _core(raw)
        if core:
            score = lambda c: max(fuzz.token_set_ratio(core, _core(c.get("name_ar") or "")),  # noqa: E731
                                  fuzz.token_set_ratio(core, _core(c.get("name_en") or "")))
            cands = sorted(cands, key=score, reverse=True)
            ranked = [score(c) for c in cands]
            # one product clearly named: select it straight away (a general "بنادول" ties -> list)
            clear = len(ranked) >= 1 and ranked[0] >= 90 and (len(ranked) == 1 or ranked[1] <= ranked[0] - 10)
        else:
            clear = False
        alts = [c for c in (result.get("alternatives") or []) if not c.get("is_scheduled")]
        choices = (cands + alts)[:MAX_CHOICES]
        if not choices:
            q = raw.strip()[:40] or "…"
            return self.a._say(conv, t["not_found"].format(q=q), guard="search_list")
        order = conv.order or {"items": []}
        draft = order.get("draft") or {}
        draft.setdefault("qty", parse_qty(raw))
        draft["fulfillment"] = draft.get("fulfillment") or parse_fulfilment(raw)
        draft["branch_id"] = draft.get("branch_id") or match_branch(raw, self.a.gw.list_branches()["branches"])
        order.update(step="choose", draft=draft, choices=[
            {"product_id": c["product_id"], "name": c.get("name_ar") or c["name_en"],
             "price_sar": c.get("price_sar"), "rx": bool(c.get("requires_prescription"))}
            for c in choices])
        conv.order = order
        exact = [c for c in cands if c.get("match") == "exact"]
        need = result.get("need")                # "something for a headache": the customer chooses
        if not need and (clear or (len(exact) == 1 and len(cands) == 1)):
            return self._pick(conv, 0, prefix="")
        if len(choices) == 1 and not need:       # a single, weak/sound-alike match: confirm first
            return self._confirm_item(conv, 0)
        rows, n = [], 1
        for i, c in enumerate(order["choices"]):
            if i == len(cands) and alts:
                rows.append(t["alt_head"])
            rows.append(t["row"].format(n=n, name=c["name"], price=self._price(t, c),
                                        rx=t["rx"] if c["rx"] else ""))
            n += 1
        note = ("\n" + result["note"]) if result.get("note") else ""
        if need and result.get("need_label"):
            label = result["need_label"].get(conv.lang or "ar") or result["need_label"]["en"]
            note += "\n\n" + t["need_note"].format(need=label)
        buttons = [{"id": f"pick{i}", "label": f"{i + 1}. {c['name']}"[:24]} for i, c in enumerate(order["choices"])]
        return self.a._say(conv, t["list"].format(rows="\n".join(rows)) + note, guard="search_list",
                           buttons=buttons)

    # ---- the steps ----------------------------------------------------------------------
    def handle(self, conv, raw):
        """Next step of an order in progress, or None if the message isn't an answer to it
        (then the agent hands it to the model as usual)."""
        o, t = conv.order, self._t(conv)
        step = o.get("step")
        if _pressed(raw, "cancel") or _CANCEL.search(normalize(raw)):
            conv.order = None
            return self.a._say(conv, t["cancelled"], guard="order_flow")
        if step == "confirm_item":
            if _pressed(raw, "yes_item") or is_affirmation(raw):
                return self._pick(conv, o.pop("confirm_idx", 0), prefix="")
            o.pop("confirm_idx", None)
            if _pressed(raw, "no_item"):
                o["step"] = "await_name"
                return self.a._say(conv, t["ask_name"], guard="order_flow")
            return self._research(conv, raw)     # they typed a different medicine
        if step == "await_name":
            core = _core(raw)
            if not core:
                return self.a._say(conv, t["ask_name"], guard="order_flow")
            return self.present(conv, self.a.gw.search_product(query=core), raw)
        if step == "choose":
            nr = normalize(raw)
            labels = [normalize(f"{i + 1}. {c['name']}"[:24]) for i, c in enumerate(o["choices"])]
            idx = labels.index(nr) if nr in labels else None
            if idx is None:                  # a listed product NAMED beats a position number
                scored = []
                for i, c in enumerate(o["choices"]):
                    cn = normalize(c["name"])
                    toks = [w for w in cn.split() if len(w) >= 3]
                    if cn and (cn in nr or nr in cn):
                        scored.append((i, 99))
                    elif toks and all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", nr) for w in toks):
                        scored.append((i, len(toks)))
                scored.sort(key=lambda x: -x[1])
                if scored and (len(scored) == 1 or scored[0][1] > scored[1][1]):
                    idx = scored[0][0]
            if idx is None:                  # else a position number / ordinal / cardinal
                idx = parse_choice(raw, len(o["choices"]))
            if idx is None:
                return None
            return self._pick(conv, idx, prefix="")
        if step == "oos":                        # picked an out-of-stock product
            if _pressed(raw, "notify") or is_affirmation(raw) or service_intent(raw) == "stock_alert":
                return self._stock_alert(conv)
            if _pressed(raw, "other_product"):
                conv.order = None
                return self.a._say(conv, t["ask_name"], guard="order_flow")
            conv.order = None
            return None                          # something else: the agent handles it
        if step == "qty":
            q = parse_qty(raw)
            if q is not None and 1 <= q <= MAX_QTY:
                o["current"]["qty"] = q
                return self._next(conv)
            if self._wants_other_product(raw):   # "اقصد X" / a different medicine, not a count
                return self._research(conv, raw)
            if q is not None:                     # a number, but outside 1..MAX_QTY
                return self.a._say(conv, t["bad_qty"].format(max=MAX_QTY), guard="order_flow")
            return self.a._say(conv, t["bad_qty"].format(max=MAX_QTY), guard="order_flow") \
                if len(raw.split()) <= 3 else None
        if step == "fulfil":
            f = "delivery" if _pressed(raw, "delivery") else "pickup" if _pressed(raw, "pickup") \
                else parse_fulfilment(raw)
            if not f:
                return None if len(raw.split()) > 3 else self._ask_fulfil(conv)
            o["fulfillment"] = f
            return self._next(conv)
        if step == "branch":
            if _pressed(raw, "delivery") or parse_fulfilment(raw) == "delivery":
                o["fulfillment"], o["branch_id"] = "delivery", None    # changed their mind
                return self._next(conv)
            branches = self.a.gw.list_branches()["branches"]
            bid = next((b["branch_id"] for b in branches if normalize("🏪 " + b["name"]) == normalize(raw)
                        or normalize(b["name"]) == normalize(raw)), None) or match_branch(raw, branches)
            idx = parse_choice(raw, len(branches)) if bid is None else None
            if bid is None and idx is not None:
                bid = branches[idx]["branch_id"]
            if bid is None:
                return None if len(raw.split()) > 3 else self._ask_branch(conv)
            o["branch_id"] = bid
            return self._next(conv)
        if step in ("address_saved", "address") and (
                _pressed(raw, "pickup") or parse_fulfilment(raw) == "pickup"):
            o["fulfillment"], o["address"] = "pickup", None            # changed their mind
            return self._next(conv)
        if step == "address_saved":
            if _pressed(raw, "use_saved") or is_affirmation(raw):
                o["address"] = (conv.profile or {}).get("address")
                return self._next(conv)
            if _pressed(raw, "other_address") or re.search(r"(اخر|ثاني|غير|other|another)", normalize(raw)):
                o["step"] = "address"
                return self.a._say(conv, t["ask_address"], guard="order_flow")
            return None
        if step == "address":
            if len(raw.strip()) < 8:
                return self.a._say(conv, t["ask_address"], guard="order_flow")
            o["address"] = raw.strip()
            return self._next(conv)
        if step == "summary":
            if _pressed(raw, "confirm") or is_affirmation(raw):
                return self._place(conv)
            if _pressed(raw, "add") or re.search(r"(اضاف|زياده|كمان|منتج اخر|منتج ثاني|add|another)", normalize(raw)):
                o["step"] = "add"
                return self.a._say(conv, t["add_prompt"], guard="order_flow")
            return None
        return None

    def _confirm_item(self, conv, idx):
        o, t = conv.order, self._t(conv)
        c = o["choices"][idx]
        o["step"], o["confirm_idx"] = "confirm_item", idx
        return self.a._say(conv, t["confirm_item"].format(name=c["name"], price=self._price(t, c)),
                           guard="order_flow", buttons=self._btns(conv, "yes_item", "no_item"))

    def _wants_other_product(self, raw):
        """At the quantity step, tell a correction / different-medicine message from a count."""
        if _CORRECTION.search(normalize(raw)):
            return True
        core = _core(raw)
        return bool(core) and parse_qty(raw) is None and any(len(w) >= 3 for w in core.split())

    def _research(self, conv, raw):
        """Customer named a different product mid-order: search again from their words."""
        o, t = conv.order, self._t(conv)
        o["current"] = None
        n = _CORRECTION.sub(" ", normalize(raw))
        core = _core(n)
        if core:
            return self.present(conv, self.a.gw.search_product(query=core), raw)
        o["step"] = "await_name"
        return self.a._say(conv, t["ask_name"], guard="order_flow")

    def _pick(self, conv, idx, prefix):
        o, t = conv.order, self._t(conv)
        c = dict(o["choices"][idx])
        c["qty"] = o["draft"].pop("qty", None)
        if c["qty"] and not 1 <= c["qty"] <= MAX_QTY:
            c["qty"] = None
        st = self.a.gw.check_stock(c["product_id"])
        if isinstance(st, dict) and "stock" in st and not any(s["qty_available"] > 0 for s in st["stock"]):
            # nothing at any branch: say so now (not after quantity and delivery) and offer the
            # back-in-stock message instead
            o["step"], o["oos"] = "oos", c
            return self.a._say(conv, t["oos"].format(name=c["name"]), guard="order_flow",
                               buttons=self._btns(conv, "notify", "other_product"))
        o["current"] = c
        o.setdefault("fulfillment", o["draft"].get("fulfillment"))
        o.setdefault("branch_id", o["draft"].get("branch_id"))
        return self._next(conv, picked=t["picked"].format(name=c["name"], price=self._price(t, c)) + "\n")

    def _stock_alert(self, conv, item=None):
        t = self._t(conv)
        c = item or conv.order["oos"]
        args = {"product_id": c["product_id"], "customer_id": conv.customer_id}
        if not conv.customer_id:                 # the request belongs to a contact: identify first
            conv.order = None
            return self.a.customer.start(conv, pending={"kind": "stock_alert", "item": c})
        conv.order = None
        self.a._run_tool(conv, "request_stock_alert", args)
        return self.a._say(conv, t["alert_saved"].format(name=c["name"]), guard="stock_alert")

    def _next(self, conv, picked=""):
        """Ask for the first missing detail, or show the summary."""
        o, t = conv.order, self._t(conv)
        cur = o.get("current")
        if cur and not cur.get("qty"):
            o["step"] = "qty"
            return self.a._say(conv, picked + t["ask_qty"], guard="order_flow")
        if cur:                                  # item complete: into the cart
            o["items"] = [i for i in o.get("items", []) if i["product_id"] != cur["product_id"]] + [cur]
            o["current"] = None
        if not o.get("fulfillment"):
            return self._ask_fulfil(conv, picked)
        if o["fulfillment"] == "pickup" and not o.get("branch_id"):
            return self._ask_branch(conv, picked)
        if o["fulfillment"] == "delivery" and not o.get("address"):
            saved = (conv.profile or {}).get("address")
            if saved:
                o["step"] = "address_saved"
                return self.a._say(conv, picked + t["ask_address_saved"].format(addr=saved),
                                   guard="order_flow", buttons=self._btns(conv, "use_saved", "other_address"))
            o["step"] = "address"
            return self.a._say(conv, picked + t["ask_address"], guard="order_flow")
        return self._summary(conv, picked)

    def _ask_fulfil(self, conv, picked=""):
        conv.order["step"] = "fulfil"
        return self.a._say(conv, picked + self._t(conv)["ask_fulfil"], guard="order_flow",
                           buttons=self._btns(conv, "delivery", "pickup"))

    def _ask_branch(self, conv, picked="", head=""):
        conv.order["step"] = "branch"
        branches = self.a.gw.list_branches()["branches"]
        return self.a._say(conv, picked + head + self._t(conv)["ask_branch"], guard="order_flow",
                           buttons=[{"id": f"b{b['branch_id']}", "label": "🏪 " + b["name"]} for b in branches]
                           + self._btns(conv, "delivery"))

    def _stock_problem(self, conv):
        """Every item in stock at the chosen branch (pickup) / some branch (delivery)?
        Returns None, ("ask", reply) when another branch has it, or ("missing", item, message)."""
        o, t = conv.order, self._t(conv)
        for it in o["items"]:
            st = self.a.gw.check_stock(it["product_id"])
            if "error" in st:
                continue
            ok = [s for s in st["stock"] if s["qty_available"] >= it["qty"]]
            if o["fulfillment"] == "pickup":
                if any(s["branch_id"] == o["branch_id"] for s in ok):
                    continue
                branch = next((s["branch_name"] for s in st["stock"] if s["branch_id"] == o["branch_id"]), "")
                msg = t["no_stock_branch"].format(name=it["name"], branch=branch)
                if ok:
                    o["branch_id"] = None
                    return ("ask", self._ask_branch(conv, head=msg + "\n" + t["other_branches"] + " " +
                                                    ", ".join(s["branch_name"] for s in ok) + "\n"))
                return ("missing", it, msg)
            if not ok:
                return ("missing", it, t["no_stock_any"].format(name=it["name"]))
        return None

    def _summary(self, conv, picked=""):
        o, t = conv.order, self._t(conv)
        problem = self._stock_problem(conv)
        if problem and problem[0] == "ask":
            return problem[1]                    # asked for a branch that has it
        if problem:
            _, gone, msg = problem
            o["items"] = [i for i in o["items"] if i["product_id"] != gone["product_id"]]
            picked = picked + msg + "\n"
            if not o["items"]:
                conv.order = None
                return self.a._say(conv, picked.strip(), guard="order_flow")
        lines, total = [], 0.0
        for it in o["items"]:
            sub = (it.get("price_sar") or 0) * it["qty"]
            total += sub
            lines.append(t["line"].format(name=it["name"], qty=it["qty"], sub=sub, rx=t["rx"] if it["rx"] else ""))
        if o["fulfillment"] == "pickup":
            bname = next((b["name"] for b in self.a.gw.list_branches()["branches"]
                          if b["branch_id"] == o["branch_id"]), "")
            how = t["how_pickup"].format(branch=bname)
        else:
            how = t["how_delivery"].format(addr=o["address"])
        rx_note = t["rx_note"] if any(i["rx"] for i in o["items"]) else ""
        o["step"] = "summary"
        return self.a._say(conv, picked + t["summary"].format(lines="\n".join(lines), total=total, how=how) + rx_note,
                           guard="order_flow", buttons=self._btns(conv, "confirm", "add", "cancel"))

    def _place(self, conv):
        """✅ pressed: the only way a chat creates a quotation."""
        o = conv.order
        args = {"items": [{"product_id": i["product_id"], "qty": i["qty"]} for i in o["items"]],
                "fulfillment": o["fulfillment"]}
        if o["fulfillment"] == "pickup":
            args["branch_id"] = o["branch_id"]
        else:
            args["delivery_address"] = o["address"]
        conv.order = None
        if not conv.customer_id:                 # register / identify first, then place it
            return self.a.customer.start(conv, intro="intro_checkout",
                                         pending={"kind": "tool", "name": "create_order", "args": args})
        return self.a.customer.place_order(conv, "create_order", args)
