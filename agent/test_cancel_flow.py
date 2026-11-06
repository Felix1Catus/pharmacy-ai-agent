"""Cancelling an order in the chat (live Odoo): a fixed wizard, never the model.

The bug it replaces: "الاخير" -> the model invented an order ("1 box Panadol, delivery in 24 hours"),
then asked for confirmation again and again, tried another customer's order and finally said
"the order hasn't been placed yet".

Run:  pytest agent/test_cancel_flow.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo")]

from agent import Conversation, PharmacyAgent  # noqa: E402
from gateway import Gateway  # noqa: E402

try:
    GW = Gateway()
    GW.c.search("pharmacy.handover", [], limit=1)
except Exception:  # noqa: BLE001
    GW = None
pytestmark = pytest.mark.skipif(GW is None, reason="Odoo not reachable")


class NoLLM:
    def chat(self, *a, **k):
        raise AssertionError("the model was called")


class ScriptLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def chat(self, *a, **k):
        return self.outputs.pop(0) if self.outputs else {"content": "تمام", "tool_calls": []}


def _customer(phone):
    prof = GW.register_customer("ZZ Cancel Test", phone, "الرياض، حي العليا، شارع 3")
    for o in GW.c.search("sale.order", [["partner_id", "=", prof["customer_id"]], ["state", "in", ["draft", "sent"]]]):
        GW.c.execute_kw("sale.order", "action_cancel", [[o]])
    return prof


def _order(cid, qty=1):
    pid = GW.c.search("product.product", [["name", "=", "Panadol 500mg"]])[0]
    branch = next(b["branch_id"] for b in GW.check_stock(pid)["stock"] if b["qty_available"] >= qty)
    return GW.create_order(cid, [{"product_id": pid, "qty": qty}], "pickup", branch_id=branch)


def _conv(prof, llm=None):
    a = PharmacyAgent(llm=llm or NoLLM(), gateway=GW)
    conv = Conversation(customer_id=prof["customer_id"], customer_facing=True, lang="ar")
    conv.started, conv.last_ts = True, time.time()
    conv.customer_name, conv.profile = prof["name"], {"address": prof["address"]}
    return a, conv


def _state(oid):
    return GW.c.read("sale.order", oid, ["state"])[0]["state"]


def test_two_open_orders_pick_confirm_cancel():
    prof = _customer("0500003301")
    o1, o2 = _order(prof["customer_id"]), _order(prof["customer_id"], 2)
    a, conv = _conv(prof)
    reply = a.respond(conv, "الغاء")
    assert "أي طلب" in reply and o1["order_ref"] in reply and o2["order_ref"] in reply
    assert [b["id"] for b in conv.last_menu][:2] == [f"cancel:{o2['order_ref']}", f"cancel:{o1['order_ref']}"]
    reply = a.respond(conv, conv.last_menu[0]["label"])          # the newest order's button
    assert o2["order_ref"] in reply and "Panadol 500mg × 2" in reply
    reply = a.respond(conv, "✅ نعم، ألغِ الطلب")
    assert reply.startswith("✅ تم إلغاء طلبك " + o2["order_ref"])
    assert _state(o2["order_id"]) == "cancel" and _state(o1["order_id"]) == "draft"
    GW.customer_cancels(o1["order_id"], prof["customer_id"])


def test_single_order_goes_straight_to_confirmation_and_no_keeps_it():
    prof = _customer("0500003302")
    o = _order(prof["customer_id"])
    a, conv = _conv(prof)
    reply = a.respond(conv, "ابي الغي طلبي")
    assert o["order_ref"] in reply and [b["id"] for b in conv.last_menu] == ["yes", "no"]
    reply = a.respond(conv, "لا")
    assert "ما ألغيت" in reply and _state(o["order_id"]) == "draft"
    GW.customer_cancels(o["order_id"], prof["customer_id"])


def test_another_customers_order_is_never_touched():
    prof, other = _customer("0500003303"), _customer("0500003304")
    mine, theirs = _order(prof["customer_id"]), _order(other["customer_id"])
    a, conv = _conv(prof)
    reply = a.respond(conv, f"الغي الطلب {theirs['order_ref']}")
    assert "ما لقيت الطلب" in reply and mine["order_ref"] in reply
    assert _state(theirs["order_id"]) == "draft"
    for o, p in ((mine, prof), (theirs, other)):
        GW.customer_cancels(o["order_id"], p["customer_id"])


def test_no_open_order():
    prof = _customer("0500003305")
    a, conv = _conv(prof)
    assert "ما عندك طلب مفتوح" in a.respond(conv, "cancel my order")


def test_confirmed_order_goes_to_a_colleague_with_the_order():
    prof = _customer("0500003306")
    o = _order(prof["customer_id"])
    GW.c.execute_kw("sale.order", "action_confirm", [[o["order_id"]]])
    a, conv = _conv(prof)
    reply = a.respond(conv, f"الغي {o['order_ref']}")
    assert "مؤكد" in reply and conv.last_guard == "handover"
    h = GW.c.search_read("pharmacy.handover", [["conv_id", "=", conv.conv_id]], ["sale_order_id"])
    assert h and h[0]["sale_order_id"][0] == o["order_id"]


def test_invented_order_details_are_replaced_by_the_real_orders():
    prof = _customer("0500003307")
    o = _order(prof["customer_id"])
    llm = ScriptLLM([{"content": "استلمت طلبك الأخير:\n📦 1 علبة بنادول 500 ملغ\n🚚 التوصيل في 24 ساعة",
                      "tool_calls": []}])
    a, conv = _conv(prof, llm)
    reply = a.respond(conv, "الاخير")
    assert o["order_ref"] in reply and "24 ساعة" not in reply and conv.last_guard == "order_status"
    GW.customer_cancels(o["order_id"], prof["customer_id"])
