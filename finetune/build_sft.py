# -*- coding: utf-8 -*-
"""Build a TOOL-USE SFT dataset for fine-tuning Qwen3-4B-Instruct-2507 (T-D3).

Every example maps a customer message (Arabic dialects / English / mixed, with Najdi texture
mined from HeshamHaroon/saudi-dialect-conversations) to the CORRECT tool call(s) — never a chat
reply (except greetings and one clarifying question). Grounded in the live Odoo catalogue (real
ids, scheduled/restricted flags) and the 8-action contract.

Generalization: a few OTC products are HELD OUT into the test split only, so the test set
measures transfer to unseen products, not just memorized phrasings.

customer_id is never emitted by the model (the agent injects it), matching runtime.

Output: finetune/data/sft_train.jsonl, sft_test.jsonl (+ stats). Lines: {"messages","tools"} in
OpenAI/Qwen tool format. MACHINE-DRAFTED, unreviewed — see finetune/DATACARD.md. Training (QLoRA)
is deferred to the GPU box (T-D4).

Run: python finetune/build_sft.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "odoo")]
import contract  # noqa: E402
from odoo_client import OdooClient  # noqa: E402

random.seed(7)
HERE = Path(__file__).resolve().parent
TOOLS = contract.tools_for_llm()

SYSTEM = (
    "You are the ordering assistant for a pharmacy franchise on WhatsApp. Customers write in "
    "Arabic dialects or English. Act by calling the provided tools; call search_product to "
    "resolve any product first, list_branches to map a branch name, check_stock for "
    "availability, track_order for order status, create_order/hold_item only after confirming, "
    "and escalate_to_human for medical advice, scheduled/prescription medicines, or abuse."
)

# ---- dialect frame banks -------------------------------------------------
GULF_GREET = ["", "", "", "السلام عليكم ", "هلا والله ", "الله يعطيك العافية ", "يا طيب "]
GULF_REQ = ["أبي", "ابي", "أبغى", "ابغى", "ودي", "ممكن", "عطني", "ابا"]

# product-wanted frames per dialect; {x}=product term, {g}=gulf greeting, {r}=gulf request verb
SEARCH = {
    "gulf": ["{g}{r} {x}", "{g}عندكم {x}؟", "{g}فيه {x}؟", "{g}وش عندكم من {x}؟",
             "{g}{r} {x} لو سمحت", "{g}دور لي {x}", "{g}ابي اشتري {x}", "{g}تبيعون {x}؟"],
    "egyptian": ["عايز {x}", "عندكو {x}؟", "فيه {x}؟", "محتاج {x}", "ممكن {x}؟", "في {x} عندكو؟"],
    "levantine": ["بدي {x}", "بتوفرو {x}؟", "في عندكن {x}؟", "لو سمحت بدي {x}", "عندكن {x}؟"],
    "msa": ["أريد {x}", "هل لديكم {x}؟", "أبحث عن {x}", "هل يتوفر {x}؟", "من فضلك أريد {x}"],
    "english": ["Do you have {x}?", "I need {x}", "Looking for {x}", "Got any {x}?",
                "Can I get {x}?"],
    "mixed": ["عندكم {x}؟", "I want {x} لو سمحت", "fih {x}؟", "do you have {x} عندكم؟",
              "محتاج {x} please"],
}
PRICE = {
    "gulf": ["{g}بكم {x}؟", "{g}كم سعر {x}؟", "{g}وش سعر {x}؟"],
    "egyptian": ["بكام {x}؟", "{x} بكام؟"],
    "levantine": ["قديش سعر {x}؟", "بقديش {x}؟"],
    "msa": ["كم سعر {x}؟", "ما ثمن {x}؟"],
    "english": ["How much is {x}?", "What's the price of {x}?"],
    "mixed": ["price of {x} كم؟"],
}
# order frames; {x}=product term, {q}=qty, {b}=branch ar name
ORDER_PICKUP = {
    "gulf": ["{g}{r} {q} {x} استلام من فرع {b}", "{g}سجل لي {q} {x} استلام {b}",
             "{g}ابي {q} {x} اجي اخذها من {b}"],
    "egyptian": ["عايز اطلب {q} {x} استلام من فرع {b}", "اطلبلي {q} {x} من فرع {b}"],
    "levantine": ["بدي اطلب {q} {x} استلام من فرع {b}", "بدي {q} {x} من فرع {b}"],
    "msa": ["أريد طلب {q} {x} للاستلام من فرع {b}", "اطلب لي {q} {x} استلام من {b}"],
    "english": ["Order {q} {x} for pickup at the {b} branch", "I want {q} {x}, pickup from {b}"],
    "mixed": ["اطلب لي {q} {x} pickup من فرع {b}", "order {q} {x} استلام {b}"],
}
ORDER_DELIVERY = {
    "gulf": ["{g}{r} {q} {x} توصيل على {addr}", "{g}ابي {q} {x} يوصل {addr}"],
    "egyptian": ["عايز {q} {x} ليفري على {addr}", "وصلولي {q} {x} على {addr}"],
    "levantine": ["بدي {q} {x} توصيل عالعنوان {addr}"],
    "msa": ["أريد {q} {x} مع التوصيل إلى {addr}"],
    "english": ["Deliver {q} {x} to {addr}", "Order {q} {x} for delivery to {addr}"],
    "mixed": ["اطلب {q} {x} delivery على {addr}"],
}
STOCK = {
    "gulf": ["{g}{x} متوفر في فرع {b}؟", "{g}فيه {x} في {b}؟", "{g}كم باقي من {x} في {b}؟"],
    "egyptian": ["{x} موجود في فرع {b}؟", "فيه {x} في {b}؟"],
    "levantine": ["في {x} بفرع {b}؟", "{x} متوفر بـ{b}؟"],
    "msa": ["هل يتوفر {x} في فرع {b}؟", "كم الكمية من {x} في {b}؟"],
    "english": ["Is {x} in stock at {b}?", "Do you have {x} at the {b} branch?"],
    "mixed": ["is {x} available في فرع {b}؟"],
}
HOLD = {
    "gulf": ["{g}احجز لي {x} في فرع {b} اجي اخذها", "{g}ثبت لي {x} بفرع {b}"],
    "egyptian": ["احجزلي {x} في فرع {b}", "ممكن تحجزولي {x} في {b}؟"],
    "levantine": ["احجزولي {x} بفرع {b}", "بدي احجز {x} بـ{b}"],
    "msa": ["أرجو حجز {x} في فرع {b}", "احجز لي {x} بفرع {b}"],
    "english": ["Hold {x} for me at {b}", "Please reserve {x} at the {b} branch"],
    "mixed": ["hold {x} لي at {b}"],
}
TRACK = {
    "gulf": ["وين وصل طلبي؟", "وين طلبي؟", "طلبي متى يوصل؟", "تم شحن طلبي؟", "وين صار الطلب؟"],
    "egyptian": ["الاوردر بتاعي فين؟", "طلبي وصل فين؟"],
    "levantine": ["وين صار الطلب تبعي؟", "طلبي وين وصل؟"],
    "msa": ["ما حالة طلبي؟", "أين طلبي الآن؟"],
    "english": ["Where is my order?", "What's the status of my order?"],
    "mixed": ["track طلبي لو سمحت", "my order وين وصل؟"],
}
BRANCHES = {
    "gulf": ["وش فروعكم؟", "عندكم فروع وين؟", "فين فروعكم؟"],
    "egyptian": ["فروعكو فين؟", "عندكو فروع فين؟"],
    "levantine": ["وين فروعكن؟", "شو فروعكن؟"],
    "msa": ["ما هي فروعكم؟", "أين تقع فروعكم؟"],
    "english": ["Which branches do you have?", "Where are your branches?"],
    "mixed": ["your فروع وين؟"],
}
MEDICAL = {
    "gulf": ["كم حبة {x} اعطي طفلي؟", "جرعة {x} للحامل كم؟", "ينفع اخذ {x} مع دواء الضغط؟",
             "وش اعطي طفلي عنده حرارة؟", "كم مرة اخذ {x} باليوم؟", "عندي سكري ينفع اخذ {x}؟",
             "جرعة {x} المناسبة كم؟"],
    "egyptian": ["اخد ايه علشان الضغط؟", "الجرعة المناسبة من {x} لطفل كام؟", "اخد {x} ازاي؟"],
    "levantine": ["شو بتنصحني اخد لوجع المعدة؟", "قديش جرعة {x} لابني؟"],
    "msa": ["ما العلاج المناسب لالتهاب الحلق الشديد؟", "هل {x} آمن مع مرض السكري؟"],
    "english": ["how many {x} can I give a 2 year old?", "is {x} safe with my BP meds?",
                "what dose of {x} for a child?"],
    "mixed": ["what dosage of {x} لطفل عمره سنتين؟"],
}
SCHED = {
    "gulf": ["{g}{r} {b}", "{g}ابي اطلب {b}", "{g}احجز لي {b}", "{g}عندكم {b}؟"],
    "egyptian": ["عايز {b}", "عايز اطلب {b}"],
    "levantine": ["بدي اطلب {b}", "بدي {b}"],
    "msa": ["أريد شراء {b}", "أريد {b}"],
    "english": ["order {b}", "I want {b}"],
    "mixed": ["order {b} لي delivery"],
}
UNCLEAR = ["هاه؟", "ذاك الشي اللي كان عندكم", "the thing from before", "الحاجة دي بتاعة امبارح",
           "هيدا الشي", "ممم", "وش قلت؟", "اها", "ايه؟", "هيك", "that one you know"]
ABUSIVE = ["انتم خدمتكم زفت وما تنفعون", "يا غبي رد علي بسرعة", "you are useless idiots",
           "كلكم فاشلين", "انتو بتوع نصب", "this is the worst pharmacy ever", "ما تفهمون شي",
           "روح بعيد يا هباب", "shut up", "خدمة سيئة جدا وانتو حمير"]
GREETING = {
    "gulf": ["السلام عليكم", "هلا", "مرحبا", "هلا والله"],
    "egyptian": ["ازيك", "اهلا"],
    "levantine": ["مرحبا كيفك", "هلا"],
    "msa": ["السلام عليكم ورحمة الله", "مرحبا"],
    "english": ["hi", "hello there"],
}
GREETING_REPLY = ("هلا وسهلا! كيف أقدر أساعدك؟ تقدر تسألني عن دواء وسعره وتوفره أو تتابع طلبك. "
                  "(Hi! I can help you find a medicine, its price and availability, or track an order.)")
CLARIFY = ("عفواً، ممكن توضح وش تقصد بالضبط؟ اسم المنتج أو رقم الطلب يساعدني. "
           "(Could you clarify — the product name or order number?)")
ADDR = {"gulf": "حي العليا الرياض", "egyptian": "شارع التحرير", "levantine": "شارع الحمرا",
        "msa": "حي الملز الرياض", "english": "King Fahd Rd, Riyadh", "mixed": "حي الروضة Jeddah"}
DIALECTS = list(SEARCH.keys())


def gfill(frame):
    return frame.replace("{g}", random.choice(GULF_GREET)).replace("{r}", random.choice(GULF_REQ))


def _odoo():
    c = OdooClient()
    tmpls = (c.search_read("product.template", [["default_code", "like", "PH-%"]],
                           ["name", "x_name_ar", "x_brand", "x_ingredient_ar", "product_variant_id"]) +
             c.search_read("product.template", [["default_code", "like", "RX-%"]],
                           ["name", "x_name_ar", "x_brand", "is_scheduled",
                            "x_requires_prescription", "product_variant_id"]))
    prods = [{"vid": t["product_variant_id"][0], "en": t["name"], "ar": t.get("x_name_ar") or "",
              "brand": t.get("x_brand") or "", "ing_ar": t.get("x_ingredient_ar") or "",
              "scheduled": bool(t.get("is_scheduled")), "rx": bool(t.get("x_requires_prescription"))}
             for t in tmpls]
    branches = [{"id": b["id"], "name": b["name"], "ar": b["name"].split(" - ")[-1]}
                for b in c.search_read("stock.warehouse", [["code", "!=", "WH"]], ["name"])]
    return prods, branches


def ex(messages):
    return {"messages": messages, "tools": TOOLS}


def asst_tool(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"type": "function", "function": {"name": name, "arguments": args}}]}


def tr(name, payload):
    return {"role": "tool", "name": name, "content": json.dumps(payload, ensure_ascii=False)}


def su(user):
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def term_variants(p):
    return [t for t in [p["brand"], p["ar"], p["en"].split()[0], p["ing_ar"]] if t]


def found(p):
    return tr("search_product", {"candidates": [
        {"product_id": p["vid"], "name_en": p["en"], "name_ar": p["ar"],
         "is_restricted": False, "is_scheduled": False}]})


def branch_list(branches):
    return tr("list_branches", {"branches": [{"branch_id": b["id"], "name": b["name"]} for b in branches]})


def gen_for_products(prods, branches, otc):
    """Generate product-dependent examples for a given product pool. Returns [(intent, ex)]."""
    rows = []
    for p in otc:
        for d in DIALECTS:
            # search
            for fr in random.sample(SEARCH[d], min(3, len(SEARCH[d]))):
                term = random.choice(term_variants(p))
                u = gfill(fr).format(x=term).strip()
                rows.append(("search", ex(su(u) + [asst_tool("search_product", {"query": term})])))
            # price -> also search_product first
            if d in PRICE:
                term = random.choice(term_variants(p))
                u = gfill(random.choice(PRICE[d])).format(x=term).strip()
                rows.append(("price", ex(su(u) + [asst_tool("search_product", {"query": term})])))
    # multi-turn stock / order(pickup+delivery) / hold / dispatch on a product sample
    sample = otc * 2
    for p in sample[:36]:
        d = random.choice(DIALECTS); b = random.choice(branches)
        term = random.choice(term_variants(p))
        # check_stock (with branch)
        u = gfill(random.choice(STOCK[d])).format(x=term, b=b["ar"]).strip()
        rows.append(("check_stock_mt", ex(su(u) + [
            asst_tool("search_product", {"query": term}), found(p),
            asst_tool("list_branches", {}), branch_list(branches),
            asst_tool("check_stock", {"product_id": p["vid"], "branch_id": b["id"]})])))
    for p in sample[:30]:
        d = random.choice(DIALECTS); b = random.choice(branches); q = random.choice([1, 2, 3])
        term = random.choice(term_variants(p))
        u = gfill(random.choice(ORDER_PICKUP[d])).format(x=term, q=q, b=b["ar"]).strip()
        rows.append(("create_order_mt", ex(su(u) + [
            asst_tool("search_product", {"query": term}), found(p),
            asst_tool("list_branches", {}), branch_list(branches),
            {"role": "assistant", "content": f"تأكيد: {q} {p['ar']} استلام من {b['name']}. أؤكد؟"},
            {"role": "user", "content": random.choice(["ايه اكد", "نعم", "اوكي اكد", "تمام", "yes"])},
            asst_tool("create_order", {"items": [{"product_id": p["vid"], "qty": q}],
                                       "fulfillment": "pickup", "branch_id": b["id"]})])))
    for p in sample[:20]:
        d = random.choice(DIALECTS); q = random.choice([1, 2]); term = random.choice(term_variants(p))
        addr = ADDR[d]
        u = gfill(random.choice(ORDER_DELIVERY[d])).format(x=term, q=q, addr=addr).strip()
        rows.append(("create_order_delivery", ex(su(u) + [
            asst_tool("search_product", {"query": term}), found(p),
            {"role": "assistant", "content": f"تأكيد: {q} {p['ar']} توصيل إلى {addr}. أؤكد؟"},
            {"role": "user", "content": random.choice(["نعم", "اكد", "yes"])},
            asst_tool("create_order", {"items": [{"product_id": p["vid"], "qty": q},],
                                       "fulfillment": "delivery", "delivery_address": addr})])))
    for p in sample[:24]:
        d = random.choice(DIALECTS); b = random.choice(branches); term = random.choice(term_variants(p))
        u = gfill(random.choice(HOLD[d])).format(x=term, b=b["ar"]).strip()
        rows.append(("hold_item_mt", ex(su(u) + [
            asst_tool("search_product", {"query": term}), found(p),
            asst_tool("list_branches", {}), branch_list(branches),
            asst_tool("hold_item", {"product_id": p["vid"], "branch_id": b["id"], "qty": 1})])))
    # dispatch after a delivery order
    for p in sample[:12]:
        oid = random.choice([10, 11, 12, 13])
        rows.append(("create_dispatch_mt", ex(
            su(gfill("{g}وصلوا طلبي بسرعة")) + [
                asst_tool("track_order", {}),
                tr("track_order", {"orders": [{"order_id": oid, "state": "sale",
                                               "dispatch_state": "pending"}]}),
                asst_tool("create_dispatch", {"order_id": oid})])))
    return rows


def gen_nonproduct(prods, branches):
    rows = []
    otc = [p for p in prods if not p["scheduled"] and not p["rx"]]
    scheduled = [p for p in prods if p["scheduled"]]
    rx_only = [p for p in prods if p["rx"] and not p["scheduled"]]
    # track
    for d in DIALECTS:
        for u in TRACK[d]:
            rows.append(("track", ex(su(u) + [asst_tool("track_order", {})])))
    for n in (1, 2, 3):
        rows.append(("track", ex(su(f"وين طلبي رقم {n}؟") + [asst_tool("track_order", {"order_id": n})])))
        rows.append(("track", ex(su(f"status of order {n}") + [asst_tool("track_order", {"order_id": n})])))
    # branches
    for d in DIALECTS:
        for u in BRANCHES[d]:
            rows.append(("branches", ex(su(u) + [asst_tool("list_branches", {})])))
    # medical (templates x dialect x several product fills -> plenty of variety)
    for d in DIALECTS:
        for tmpl in MEDICAL[d]:
            for _ in range(4):
                x = random.choice(otc)["brand"]
                rows.append(("escalate_medical", ex(su(gfill(tmpl).format(x=x)) + [
                    asst_tool("escalate_to_human", {"reason": "medical_advice",
                                                    "summary": "medical advice/dosage request"})])))
    # scheduled / restricted (all frames per product x dialect)
    for p in scheduled:
        for d in DIALECTS:
            for fr in SCHED[d]:
                u = gfill(fr).format(b=p["brand"]).strip()
                rows.append(("escalate_scheduled", ex(su(u) + [
                    asst_tool("escalate_to_human", {"reason": "restricted_item",
                                                    "summary": f"scheduled/controlled: {p['en']}"})])))
    for p in rx_only:
        for d in DIALECTS:
            for fr in SCHED[d]:
                u = gfill(fr).format(b=p["brand"]).strip()
                rows.append(("escalate_restricted", ex(su(u) + [
                    asst_tool("escalate_to_human", {"reason": "restricted_item",
                                                    "summary": f"prescription-only: {p['en']}"})])))
    # abusive / unclear / greeting
    for u in ABUSIVE:
        rows.append(("abusive", ex(su(u) + [
            asst_tool("escalate_to_human", {"reason": "abusive", "summary": "abusive message"})])))
    for u in UNCLEAR:
        rows.append(("unclear", ex(su(u) + [{"role": "assistant", "content": CLARIFY}])))
    for d in GREETING:
        for u in GREETING[d]:
            rows.append(("greeting", ex(su(u) + [{"role": "assistant", "content": GREETING_REPLY}])))
    return rows


# per-intent caps for TRAIN (test gets ~0.18x). Balanced: search no longer dominates.
TARGETS = {"search": 200, "price": 40, "check_stock_mt": 40, "create_order_mt": 44,
           "create_order_delivery": 28, "hold_item_mt": 34, "create_dispatch_mt": 16,
           "track": 26, "branches": 15, "escalate_medical": 72, "escalate_scheduled": 36,
           "escalate_restricted": 24, "unclear": 12, "abusive": 12, "greeting": 12}


def cap(rows, frac=1.0):
    byk = defaultdict(list)
    for k, r in rows:
        byk[k].append(r)
    out = []
    for k, rs in byk.items():
        random.shuffle(rs)
        c = max(1, int(round(TARGETS.get(k, len(rs)) * frac)))
        out += [(k, r) for r in rs[:c]]
    return out


def main():
    prods, branches = _odoo()
    otc = [p for p in prods if not p["scheduled"] and not p["rx"]]
    random.shuffle(otc)
    held = otc[:4]           # products reserved for the TEST split only
    train_otc = otc[4:]

    # product-dependent examples: train pool from train_otc, test pool from held-out products
    train_prod = gen_for_products(prods, branches, train_otc)
    test_prod = gen_for_products(prods, branches, held)
    # non-product examples: one pool, random 82/18 split
    nonprod = gen_nonproduct(prods, branches)
    random.shuffle(nonprod)
    cut = int(len(nonprod) * 0.18)
    test_np, train_np = nonprod[:cut], nonprod[cut:]

    train = cap(train_prod + train_np, frac=1.0)
    test = cap(test_prod + test_np, frac=0.18)
    random.shuffle(train); random.shuffle(test)

    (HERE / "data" / "sft_train.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for _, r in train) + "\n", encoding="utf-8")
    (HERE / "data" / "sft_test.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for _, r in test) + "\n", encoding="utf-8")

    tc = defaultdict(int)
    for k, _ in train:
        tc[k] += 1
    print(f"train={len(train)}  test={len(test)}  held-out test products={[p['brand'] for p in held]}")
    print("train by intent:", dict(sorted(tc.items())))


if __name__ == "__main__":
    main()
