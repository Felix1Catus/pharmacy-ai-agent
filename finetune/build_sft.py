# -*- coding: utf-8 -*-
"""Build a TOOL-USE SFT dataset for fine-tuning Qwen3-4B-Instruct-2507 (T-D3).

Every example maps a customer message (in Arabic dialects / English, with Najdi texture mined
from HeshamHaroon/saudi-dialect-conversations) to the CORRECT tool call(s) — never a chat
reply. Grounded in the real Odoo catalogue (product ids, names, scheduled/restricted flags) and
the 7+1 contract tools, so tool-call arguments are valid.

Teaches the behaviours we tuned for:
- product mention -> search_product first
- order status -> track_order
- branch name -> list_branches
- medical/dosage -> escalate_to_human(medical_advice)   [deterministic in prod, taught here too]
- scheduled/controlled or prescription -> escalate_to_human(restricted_item)
- abusive -> escalate_to_human(abusive); vague -> one clarifying question (clarify-once)
- multi-turn: after search/branch results, call check_stock / create_order / hold_item with the
  resolved ids; confirm before create_order

customer_id is NEVER emitted by the model (the agent injects it), matching runtime.

Output: finetune/data/sft_train.jsonl, sft_test.jsonl (+ stats). Each line: {"messages":[...],
"tools":[...]} in OpenAI/Qwen tool format. MACHINE-DRAFTED, unreviewed — see finetune/DATACARD.md.
Training (QLoRA) is deferred to the GPU box (T-D4).

Run: python finetune/build_sft.py
"""
from __future__ import annotations

import json
import random
import sys
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

# --- Najdi/Gulf texture harvested from the mined frames (authentic markers) ---
GULF_REQ = ["أبي", "ابي", "أبغى", "ابغى", "ودي", "ممكن", "عطني"]
GULF_GREET = ["", "", "السلام عليكم، ", "هلا والله، ", "الله يعطيك العافية، ", "يا طيب، "]
GULF_Q = ["فيه عندكم", "عندكم", "وش عندكم من", "متوفر عندكم"]


def _odoo():
    c = OdooClient()
    tmpls = c.search_read(
        "product.template", [["default_code", "like", "PH-%"]],
        ["name", "x_name_ar", "x_brand", "x_ingredient_ar", "product_variant_id"]) + \
        c.search_read("product.template", [["default_code", "like", "RX-%"]],
                      ["name", "x_name_ar", "x_brand", "x_ingredient_ar",
                       "is_scheduled", "x_requires_prescription", "product_variant_id"])
    prods = []
    for t in tmpls:
        prods.append({
            "vid": t["product_variant_id"][0], "en": t["name"],
            "ar": t.get("x_name_ar") or "", "brand": t.get("x_brand") or "",
            "ing_ar": t.get("x_ingredient_ar") or "",
            "scheduled": bool(t.get("is_scheduled")),
            "rx": bool(t.get("x_requires_prescription")),
        })
    branches = [{"id": b["id"], "name": b["name"],
                 "ar": b["name"].split(" - ")[-1]}
                for b in c.search_read("stock.warehouse", [["code", "!=", "WH"]],
                                       ["name"])]
    return prods, branches


def ex(messages):
    return {"messages": messages, "tools": TOOLS}


def asst_tool(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"type": "function", "function": {"name": name, "arguments": args}}]}


def tool_result(name, payload):
    return {"role": "tool", "name": name, "content": json.dumps(payload, ensure_ascii=False)}


def sys_user(user):
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def gulf_wrap(term):
    g = random.choice(GULF_GREET); r = random.choice(GULF_REQ)
    return random.choice([f"{g}{r} {term}", f"{g}{random.choice(GULF_Q)} {term}؟",
                          f"{g}{r} {term} لو سمحت"])


def build():
    prods, branches = _odoo()
    otc = [p for p in prods if not p["scheduled"] and not p["rx"]]
    scheduled = [p for p in prods if p["scheduled"]]
    rx_only = [p for p in prods if p["rx"] and not p["scheduled"]]
    rows = []

    # 1) search_product (product mention -> search). Multi-dialect phrasings.
    def term_variants(p):
        return [t for t in [p["brand"], p["ar"], p["en"].split()[0], p["ing_ar"]] if t]
    for p in otc:
        for term in term_variants(p):
            rows.append(("search", ex(sys_user(gulf_wrap(term)) + [asst_tool("search_product", {"query": term})])))
            rows.append(("search", ex(sys_user(f"عايز {term}") + [asst_tool("search_product", {"query": term})])))  # egyptian
            rows.append(("search", ex(sys_user(f"بدي {term}") + [asst_tool("search_product", {"query": term})])))  # levantine
            rows.append(("search", ex(sys_user(f"Do you have {p['en']}?") + [asst_tool("search_product", {"query": p['en'].split()[0]})])))

    # 2) list_branches (branch name mentioned on its own / branch question)
    for b in branches:
        for u in [f"وش فروعكم؟", f"فين فرع {b['ar']}؟", f"عندكم فرع في {b['ar']}؟",
                  f"which branches do you have?"]:
            rows.append(("branches", ex(sys_user(u) + [asst_tool("list_branches", {})])))

    # 3) track_order (status). No number -> track_order() ; number -> order_id
    track_u = ["وين وصل طلبي؟", "فين الاوردر بتاعي؟", "وين طلبي؟", "where is my order?",
               "بدي اعرف حالة طلبي", "طلبي متى يوصل؟", "تم شحن طلبي؟", "وين صار الطلب تبعي؟",
               "الاوردر بتاعي فين وصل؟", "ابي اعرف حالة اخر طلب"]
    for u in track_u:
        rows.append(("track", ex(sys_user(u) + [asst_tool("track_order", {})])))
    for n in (1, 2, 3):
        rows.append(("track", ex(sys_user(f"وين طلبي رقم {n}؟") + [asst_tool("track_order", {"order_id": n})])))
        rows.append(("track", ex(sys_user(f"status of order {n}") + [asst_tool("track_order", {"order_id": n})])))

    # 4) escalate: medical (templates x product fills x dialect)
    med = ["كم حبة {x} اعطي طفلي؟", "جرعة {x} للحامل كم؟", "ينفع اخذ {x} مع دواء الضغط؟",
           "وش اعطي طفلي عنده حرارة؟", "is {x} safe with my blood pressure meds?",
           "اخد ايه علشان الضغط؟", "الدواء المناسب لالتهاب الحلق الشديد؟",
           "كم مرة اخذ {x} باليوم؟", "عندي سكري، ينفع اخذ {x}؟", "شو بتنصحني اخد لوجع المعدة؟",
           "how many {x} can I give a 2 year old?", "جرعة {x} المناسبة كم؟"]
    for tmpl in med:
        for _ in range(3):
            x = random.choice(otc)["brand"]
            rows.append(("escalate_medical", ex(sys_user(tmpl.format(x=x)) + [
                asst_tool("escalate_to_human", {"reason": "medical_advice",
                                                "summary": "customer asked for medical advice/dosage"})])))

    # 5) escalate: scheduled / restricted (x dialect phrasings)
    sched_frames = ["{b}", "ابغى اطلب {b}", "order {b}", "عايز {b}", "بدي اطلب {b}",
                    "ممكن {b}؟", "احجز لي {b}", "ابي علبة {b}"]
    for p in scheduled:
        for fr in sched_frames:
            rows.append(("escalate_scheduled", ex(sys_user(fr.format(b=p["brand"])) + [
                asst_tool("escalate_to_human", {"reason": "restricted_item",
                                                "summary": f"scheduled/controlled: {p['en']}"})])))
    for p in rx_only:
        for fr in ["ابغى اطلب {b}", "عايز {b}", "بدي {b}", "order {b}"]:
            rows.append(("escalate_restricted", ex(sys_user(fr.format(b=p["brand"])) + [
                asst_tool("escalate_to_human", {"reason": "restricted_item",
                                                "summary": f"prescription-only: {p['en']}"})])))

    # 6) abusive -> escalate ; unclear -> one clarifying question (clarify-once)
    for u in ["انتم خدمتكم زفت وما تنفعون", "يا غبي رد علي بسرعة", "you are useless idiots",
              "كلكم فاشلين", "انتو بتوع نصب", "this is the worst pharmacy ever",
              "ما تفهمون شي", "روح بعيد يا هباب"]:
        rows.append(("abusive", ex(sys_user(u) + [
            asst_tool("escalate_to_human", {"reason": "abusive", "summary": "abusive message"})])))
    clarify = "عفواً، ممكن توضح وش تقصد بالضبط؟ اسم المنتج أو رقم الطلب يساعدني. (Could you clarify — the product name or order number?)"
    for u in ["هاه؟", "ذاك الشي اللي كان عندكم", "the thing from before", "الحاجة دي بتاعة امبارح",
              "هيدا الشي", "ممم", "وش قلت؟", "اها"]:
        rows.append(("unclear", ex(sys_user(u) + [{"role": "assistant", "content": clarify}])))

    # 7) multi-turn: check_stock after search (+branch)
    for p in (otc + otc)[:30]:
        b = random.choice(branches)
        msgs = sys_user(f"{random.choice(GULF_Q)} {p['brand']} في فرع {b['ar']}؟")
        msgs += [asst_tool("search_product", {"query": p["brand"]}),
                 tool_result("search_product", {"candidates": [
                     {"product_id": p["vid"], "name_en": p["en"], "name_ar": p["ar"],
                      "is_restricted": False, "is_scheduled": False}]}),
                 asst_tool("list_branches", {}),
                 tool_result("list_branches", {"branches": [
                     {"branch_id": bb["id"], "name": bb["name"]} for bb in branches]}),
                 asst_tool("check_stock", {"product_id": p["vid"], "branch_id": b["id"]})]
        rows.append(("check_stock_mt", ex(msgs)))

    # 8) multi-turn: create_order after confirm (pickup) ; hold_item
    for p in (otc + otc)[:30]:
        b = random.choice(branches); qty = random.choice([1, 2, 3])
        base = sys_user(f"ابغى {qty} {p['brand']} استلام من فرع {b['ar']}")
        base += [asst_tool("search_product", {"query": p["brand"]}),
                 tool_result("search_product", {"candidates": [
                     {"product_id": p["vid"], "name_en": p["en"], "name_ar": p["ar"],
                      "is_restricted": False, "is_scheduled": False}]}),
                 asst_tool("list_branches", {}),
                 tool_result("list_branches", {"branches": [
                     {"branch_id": bb["id"], "name": bb["name"]} for bb in branches]}),
                 {"role": "assistant",
                  "content": f"تأكيد: {qty} {p['ar']} استلام من {b['name']}. أؤكد الطلب؟"},
                 {"role": "user", "content": random.choice(["ايه اكد", "نعم", "اوكي اكد", "تمام"])},
                 asst_tool("create_order", {"items": [{"product_id": p["vid"], "qty": qty}],
                                            "fulfillment": "pickup", "branch_id": b["id"]})]
        rows.append(("create_order_mt", ex(base)))

        b2 = random.choice(branches)
        hmsgs = sys_user(f"احجز لي {p['brand']} في فرع {b2['ar']} اجي اخذها")
        hmsgs += [asst_tool("search_product", {"query": p["brand"]}),
                  tool_result("search_product", {"candidates": [
                      {"product_id": p["vid"], "name_en": p["en"], "name_ar": p["ar"],
                       "is_restricted": False, "is_scheduled": False}]}),
                  asst_tool("list_branches", {}),
                  tool_result("list_branches", {"branches": [
                      {"branch_id": bb["id"], "name": bb["name"]} for bb in branches]}),
                  asst_tool("hold_item", {"product_id": p["vid"], "branch_id": b2["id"], "qty": 1})]
        rows.append(("hold_item_mt", ex(hmsgs)))

    random.shuffle(rows)
    return rows


# cap per intent so the set is balanced (search otherwise dominates)
TARGETS = {"search": 180, "check_stock_mt": 30, "create_order_mt": 30, "hold_item_mt": 30,
           "track": 40, "branches": 40, "escalate_medical": 60, "escalate_scheduled": 40,
           "escalate_restricted": 30, "unclear": 30, "abusive": 24}


def main():
    rows = build()
    from collections import defaultdict
    byk = defaultdict(list)
    for k, r in rows:
        byk[k].append(r)
    # balance: sample each intent down to its target (keep all if fewer)
    for k, rs in byk.items():
        cap = TARGETS.get(k, len(rs))
        if len(rs) > cap:
            byk[k] = random.sample(rs, cap)
    train, test = [], []
    for k, rs in byk.items():
        random.shuffle(rs)
        n_test = max(1, int(round(len(rs) * 0.15)))
        test += [(k, r) for r in rs[:n_test]]
        train += [(k, r) for r in rs[n_test:]]
    random.shuffle(train); random.shuffle(test)

    (HERE / "data" / "sft_train.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for _, r in train) + "\n", encoding="utf-8")
    (HERE / "data" / "sft_test.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for _, r in test) + "\n", encoding="utf-8")

    total = sum(len(v) for v in byk.values())
    print(f"total={total}  train={len(train)}  test={len(test)}")
    print("by intent:", {k: len(v) for k, v in sorted(byk.items())})


if __name__ == "__main__":
    main()
