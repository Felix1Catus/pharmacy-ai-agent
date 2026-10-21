# -*- coding: utf-8 -*-
"""Fixed order wizard (agent/order_flow.py): parsing (pure) + whole orders with a scripted model,
counting model calls (the point: one call per order instead of one per step).

Run:  pytest agent/test_order_flow.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo")]

from order_flow import match_branch, parse_choice, parse_fulfilment, parse_qty  # noqa: E402

BRANCHES = [{"branch_id": 2, "name": "Riyadh - Olaya"}, {"branch_id": 3, "name": "Jeddah - Tahlia"},
            {"branch_id": 4, "name": "Dammam - Corniche"}]


@pytest.mark.parametrize("text,q", [("ابغى علبتين بنادول 500 استلام من فرع جدة", 2), ("3", 3), ("٣", 3),
                                    ("ثلاث علب", 3), ("علبة وحدة", 1), ("two boxes", 2),
                                    ("بنادول 500 ملغ", None), ("panadol 500", None), ("10", 10)])
def test_parse_qty(text, q):
    assert parse_qty(text) == q


def test_parse_fulfilment_and_branch():
    assert parse_fulfilment("استلام من فرع جدة") == "pickup"
    assert parse_fulfilment("توصيل لعنواني") == "delivery"
    assert parse_fulfilment("بنادول") is None
    assert match_branch("استلام من فرع جدة", BRANCHES) == 3
    assert match_branch("pickup at Olaya", BRANCHES) == 2
    assert match_branch("بنادول", BRANCHES) is None


def test_parse_choice():
    assert parse_choice("2", 3) == 1 and parse_choice("٢", 3) == 1 and parse_choice("1. بنادول", 3) == 0
    assert parse_choice("الثاني", 3) == 1 and parse_choice("5", 3) is None


# ---- whole orders with a scripted model (needs Odoo) ---------------------------------
try:
    from agent import Conversation, PharmacyAgent
    from gateway import Gateway
    GW = Gateway()
    GW.c.search("res.partner", [], limit=1)
except Exception:
    GW = None
live = pytest.mark.skipif(GW is None, reason="Odoo not reachable")

PANADOL = [
    {"product_id": 1, "name_en": "Panadol 500mg", "name_ar": "بنادول ٥٠٠ ملغ", "price_sar": 12.0,
     "match": "exact", "requires_prescription": False, "is_scheduled": False},
    {"product_id": 3, "name_en": "Panadol Cold & Flu", "name_ar": "بنادول كولد اند فلو", "price_sar": 18.0,
     "match": "exact", "requires_prescription": False, "is_scheduled": False},
    {"product_id": 2, "name_en": "Panadol Extra", "name_ar": "بنادول اكسترا", "price_sar": 15.0,
     "match": "exact", "requires_prescription": False, "is_scheduled": False},
]


class ScriptLLM:
    def __init__(self, outputs):
        self.outputs, self.seen = list(outputs), []

    def chat(self, messages, tools=None):
        self.seen.append((messages, tools))
        return self.outputs.pop(0) if self.outputs else {"content": "تمام", "tool_calls": []}


def _search_call(q):
    return {"content": "", "tool_calls": [{"name": "search_product", "arguments": {"query": q}}]}


@pytest.fixture()
def shop(monkeypatch):
    placed = []
    monkeypatch.setattr(GW, "search_product", lambda **kw: {"candidates": [dict(c) for c in PANADOL]})
    monkeypatch.setattr(GW, "list_branches", lambda: {"branches": BRANCHES})
    stock = {1: {2: 50, 3: 50, 4: 50}, 2: {2: 0, 3: 0, 4: 0}, 3: {2: 5, 3: 0, 4: 5}}
    monkeypatch.setattr(GW, "check_stock", lambda product_id, branch_id=None: {"stock": [
        {"branch_id": b["branch_id"], "branch_name": b["name"], "qty_available": stock[product_id][b["branch_id"]]}
        for b in BRANCHES]})
    monkeypatch.setattr(GW, "create_order", lambda **kw: placed.append(kw) or {
        "order_id": 4321, "order_ref": "S04321", "state": "draft", "total": 24.0,
        "branch_id": kw.get("branch_id"), "branch_name": "Jeddah - Tahlia",
        "fulfillment": kw["fulfillment"], "rx_review": "none"})
    monkeypatch.setattr(GW, "payment_link", lambda oid: f"http://odoo/my/orders/{oid}?access_token=t")
    return placed


def _identified(llm):
    prof = GW.register_customer("ZZ Test Wizard", "0500001414", "الرياض، حي العليا، شارع 3")
    a = PharmacyAgent(llm=llm, gateway=GW)
    conv = Conversation(None, customer_facing=True, wa_phone="966500001414")
    a.greet(conv)
    return a, conv, prof


@live
def test_full_sentence_order_needs_one_model_call(shop):
    llm = ScriptLLM([_search_call("بنادول 500")])
    a, conv, prof = _identified(llm)
    reply = a.respond(conv, "ابغى علبتين بنادول 500 استلام من فرع جدة")
    # one product clearly named + qty 2 + pickup Jeddah: straight to the summary
    assert "ملخص طلبك" in reply and "× 2" in reply and "Jeddah" in reply
    reply = a.respond(conv, "✅ تأكيد الطلب")
    assert shop == [{"items": [{"product_id": 1, "qty": 2}], "fulfillment": "pickup", "branch_id": 3,
                     "customer_id": prof["customer_id"]}]
    assert "S04321" in reply and "/my/orders/4321" in reply
    assert len(llm.seen) <= 1                                                # at most the search call


@live
def test_missing_details_are_asked_with_buttons(shop):
    llm = ScriptLLM([_search_call("بنادول")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "عندكم بنادول؟")
    assert "كم علبة" in a.respond(conv, "1")
    reply = a.respond(conv, "3")
    assert [b["id"] for b in conv.last_menu] == ["delivery", "pickup"]
    reply = a.respond(conv, "🚚 توصيل")
    assert "العليا" in reply and [b["id"] for b in conv.last_menu] == ["use_saved", "other_address"]
    reply = a.respond(conv, "✅ نعم لعنواني")
    assert "ملخص طلبك" in reply and "× 3" in reply
    a.respond(conv, "نعم")
    assert shop[0]["fulfillment"] == "delivery" and shop[0]["delivery_address"].startswith("الرياض")
    assert len(llm.seen) <= 1


@live
def test_out_of_stock_at_branch_offers_branches_that_have_it(shop):
    llm = ScriptLLM([_search_call("بنادول كولد")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "ابغى علبة بنادول كولد استلام من فرع جدة")
    reply = a.respond(conv, "2")                       # Cold & Flu: none in Jeddah, 5 in Riyadh/Dammam
    assert "غير متوفر" in reply and "Riyadh" in reply and conv.order["step"] == "branch"
    reply = a.respond(conv, "🏪 Riyadh - Olaya")
    assert "ملخص طلبك" in reply and "Riyadh" in reply


@live
def test_guest_confirm_registers_then_places(shop):
    llm = ScriptLLM([_search_call("بنادول")])
    a = PharmacyAgent(llm=llm, gateway=GW)
    conv = Conversation(None, customer_facing=True)
    a.greet(conv)
    a.respond(conv, "ابغى علبتين بنادول استلام من فرع جدة")
    a.respond(conv, "1")
    reply = a.respond(conv, "✅ تأكيد الطلب")
    assert shop == [] and conv.flow and conv.flow["pending"]["name"] == "create_order"
    assert "بياناتك" in reply


@live
def test_model_never_gets_order_tools_in_customer_chats(shop):
    llm = ScriptLLM([_search_call("بنادول")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "عندكم بنادول؟")
    names = {t["function"]["name"] for t in llm.seen[0][1]}
    assert names == {"search_product", "check_stock", "list_branches", "track_order", "escalate_to_human"}


@live
def test_not_found_is_a_fixed_reply(monkeypatch):
    monkeypatch.setattr(GW, "search_product", lambda **kw: {"candidates": [], "hint": "..."})
    llm = ScriptLLM([_search_call("zzqx")])
    a, conv, prof = _identified(llm)
    reply = a.respond(conv, "عندكم zzqx؟")
    assert "ما لقيت" in reply and len(llm.seen) == 1


@live
def test_product_question_answered_from_memory_is_replaced_by_real_search(shop):
    llm = ScriptLLM([{"content": "لا، عندنا سنتروم فقط للطلبات المحددة", "tool_calls": []}])
    a, conv, prof = _identified(llm)
    reply = a.respond(conv, "عندكم بنادول؟")          # model skipped the search
    assert "1. بنادول ٥٠٠ ملغ" in reply and "للطلبات المحددة" not in reply


@live
def test_clearly_named_product_is_selected_and_general_name_lists(shop):
    llm = ScriptLLM([_search_call("بنادول")])           # the model shortened the query
    a, conv, prof = _identified(llm)
    reply = a.respond(conv, "ابغى بنادول اكسترا توصيل")
    assert reply.startswith("✅ بنادول اكسترا") and "كم علبة" in reply
    llm2 = ScriptLLM([_search_call("بنادول")])
    a2, conv2, _ = _identified(llm2)
    assert "1. " in a2.respond(conv2, "عندكم بنادول؟")    # general name: the list


def test_arabic_punctuation_is_not_part_of_words():
    from order_flow import _core
    assert _core("كم سعر فيتامين سي؟") == _core("كم سعر فيتامين سي")
    from fuzzy_search import words
    assert "سي" in words("فيتامين سي؟")
