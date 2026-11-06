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
    stock = {1: {2: 50, 3: 50, 4: 50}, 2: {2: 20, 3: 20, 4: 20}, 3: {2: 5, 3: 0, 4: 5}, 9: {2: 3, 3: 3, 4: 3}}
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
def test_out_of_stock_everywhere_offers_back_in_stock_message(shop, monkeypatch):
    monkeypatch.setattr(GW, "check_stock", lambda product_id, branch_id=None: {"stock": [
        {"branch_id": b["branch_id"], "branch_name": b["name"], "qty_available": 0} for b in BRANCHES]})
    alerts = []
    monkeypatch.setattr(GW, "request_stock_alert", lambda **kw: alerts.append(kw) or {"recorded": True})
    llm = ScriptLLM([_search_call("بنادول اكسترا")])
    a, conv, prof = _identified(llm)
    reply = a.respond(conv, "ابغى بنادول اكسترا")
    assert "غير متوفر" in reply and [b["id"] for b in conv.last_menu] == ["notify", "other_product"]
    reply = a.respond(conv, "🔔 بلغني لما يتوفر")
    assert "سجّلنا" in reply and conv.order is None
    assert alerts == [{"product_id": 2, "customer_id": prof["customer_id"]}]


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
    llm = ScriptLLM([{"content": "فروعنا في الرياض وجدة والدمام", "tool_calls": []}])
    a, conv, prof = _identified(llm)
    a.respond(conv, "وين فروعكم بالضبط؟")              # not a product: goes to the model
    names = {t["function"]["name"] for t in llm.seen[0][1]}
    assert not names & {"create_order", "hold_item", "create_dispatch"}   # orders: the fixed wizard only
    assert names == {"search_product", "check_stock", "list_branches", "track_order", "escalate_to_human",
                     "get_product_details", "get_order_details", "cancel_order", "list_prescriptions",
                     "request_stock_alert", "update_delivery_address"}


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


# ---- from the 2026-10-22 voice test (owner's transcript) -----------------------------
def test_phone_and_number_words_are_not_searched():
    from order_flow import _core
    heard = ("أبي، علبتين، بنادول، استلام اليوم، رقم جوال، الف وشرين، تين، انتين، وشرين، تين، "
             "وشرين، وشرين.")
    assert _core(heard) == "بنادول"                     # not "رقم"/"الف" -> no baby milk, no scale
    assert _core("ابغى بنادول اكسترا رقمي 0501234567") == "بنادول اكسترا"
    assert _core("حليب نان رقم ١") == "حليب نان رقم 1"   # product names keep their numbers


@live
def test_switch_from_pickup_to_delivery_at_the_branch_question(shop):
    llm = ScriptLLM([_search_call("بنادول")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "ابي علبتين بنادول استلام")
    reply = a.respond(conv, "1")
    assert conv.order["step"] == "branch" and "delivery" in [b["id"] for b in conv.last_menu]
    reply = a.respond(conv, "ابي توصيل للمنزل")          # changed their mind
    assert conv.order["fulfillment"] == "delivery" and "العليا" in reply   # saved address offered
    a.respond(conv, "✅ نعم لعنواني")
    a.respond(conv, "نعم")
    assert shop and shop[0]["fulfillment"] == "delivery"


@live
def test_switch_from_delivery_to_pickup_at_the_address_question(shop):
    llm = ScriptLLM([_search_call("بنادول")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "ابي علبة بنادول توصيل")
    a.respond(conv, "1")
    assert conv.order["step"] == "address_saved"
    reply = a.respond(conv, "لا خلاص استلام من الفرع")
    assert conv.order["fulfillment"] == "pickup" and conv.order["step"] == "branch"


# ---- from the 2026-10-22 owner transcript: confirm weak matches, allow corrections / cancel ----
@live
def test_weak_single_match_is_confirmed_before_quantity(shop, monkeypatch):
    # a lone sound-alike match (not exact) must be confirmed, not locked straight into quantity
    one = [{"product_id": 9, "name_en": "STIEPROX 1.5% Shampoo", "name_ar": "ستيبروكس شامبو",
            "price_sar": 28.85, "match": "sound-alike", "requires_prescription": False,
            "is_scheduled": False}]
    monkeypatch.setattr(GW, "search_product", lambda **kw: {"candidates": [dict(c) for c in one]})
    llm = ScriptLLM([_search_call("سترابرونكس")])
    a, conv, prof = _identified(llm)
    reply = a.respond(conv, "متاح سترابرونكس")
    assert conv.order["step"] == "confirm_item" and "تقصده" in reply
    reply = a.respond(conv, "✅ نعم، هذا")
    assert conv.order["step"] == "qty" and "كم علبة" in reply


@live
def test_correction_at_quantity_step_researches_instead_of_looping(shop):
    llm = ScriptLLM([_search_call("بنادول اكسترا")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "ابغى بنادول اكسترا")               # clearly named -> straight to quantity
    assert conv.order["step"] == "qty"
    reply = a.respond(conv, "اقصد بنادول كولد")          # a correction, not a count
    assert "اكتب الكمية" not in reply                   # no longer loops the quantity prompt
    assert "كولد" in reply                              # re-searched for the new product


@live
def test_cancel_word_cancels_mid_order(shop):
    llm = ScriptLLM([_search_call("بنادول اكسترا")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "ابغى بنادول اكسترا")
    reply = a.respond(conv, "الغاء")                    # typed cancel, not the button
    assert conv.order is None and "إلغاء" in reply


def test_parse_choice_cardinals_and_phrasing():
    from order_flow import parse_choice
    assert parse_choice("رقم اثنين", 5) == 1
    assert parse_choice("اختيار رقم 2", 5) == 1
    assert parse_choice("الخيار الثالث", 5) == 2
    assert parse_choice("5", 3) is None


@live
def test_voice_selection_during_order_not_blocked_by_medical_guard(shop):
    # owner transcript 2026-10-22: "اعطيني البنادول اكسترا ..." selected a product but hit the
    # medical-advice guard. It must select, not refuse.
    llm = ScriptLLM([_search_call("بنادول")])
    a, conv, prof = _identified(llm)
    a.respond(conv, "عندكم بنادول؟")                          # -> numbered list (choose step)
    assert conv.order["step"] == "choose"
    reply = a.respond(conv, "اعطيني البنادول اكسترا اختيار رقم اثنين")  # name beats the (disagreeing) position number
    assert "نصائح طبية" not in reply                          # not the medical refusal
    assert reply.startswith("✅ بنادول اكسترا") and "كم علبة" in reply


def test_cancel_only_when_the_customer_asked(monkeypatch):
    cancelled = []
    monkeypatch.setattr(GW, "cancel_order", lambda **kw: cancelled.append(kw) or {"cancelled": True})
    monkeypatch.setattr(GW, "get_order_details", lambda **kw: {"order_ref": "S00001", "can_cancel": True})
    call = {"content": "", "tool_calls": [{"name": "cancel_order", "arguments": {"order_ref": "S00001"}}]}
    llm = ScriptLLM([call, {"content": "طلبك S00001 قيد التجهيز", "tool_calls": []},
                     call, {"content": "تأكد الإلغاء؟", "tool_calls": []},
                     call, {"content": "تم", "tool_calls": []}])
    a = PharmacyAgent(llm=llm, gateway=GW)
    conv = Conversation(customer_id=7)
    a.respond(conv, "ابي تفاصيل طلبي S00001")          # not asked: refused by the gate
    a.respond(conv, "طيب الغيه")                      # asked, but no "yes" yet: refused
    assert cancelled == []
    a.respond(conv, "نعم")                            # asked + confirmed
    assert cancelled and cancelled[0]["order_ref"] == "S00001"
