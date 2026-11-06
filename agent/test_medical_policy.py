"""Escalation policy (live Odoo + Tesseract): no empty handovers.

- scheduled (مقيد): refused with where it is in stock; never escalated, no photo asked for
- a dosage question with no order / cart behind it: refused, no handover
- a dosage question about an item in the cart: ask for the prescription photo, read the doctor's
  directions back as written; only "that's wrong" hands it to the pharmacist, tied to the cart
  quotation
- the cart is an Odoo quotation (wa_cart) and the order reference is matched exactly

Run:  pytest agent/test_medical_policy.py -q
"""
from __future__ import annotations

import shutil
import sys
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
pytestmark = pytest.mark.skipif(GW is None, reason="Odoo not reachable / module < 1.10")
SAMPLE_RX = ROOT / "evals" / "datasets" / "sample_rx_metformin_atorvastatin.png"


class NoLLM:
    """Every reply here must come from fixed logic: any model call fails the test."""
    def chat(self, *a, **k):
        raise AssertionError("the model was called")


def _customer(phone):
    prof = GW.register_customer("ZZ Policy Test", phone, "الرياض، حي العليا، شارع 3")
    GW.record_health_consent(prof["customer_id"])         # PDPL consent given earlier
    GW.cart_sync(prof["customer_id"], [])
    for o in GW.c.search("sale.order", [["partner_id", "=", prof["customer_id"]], ["state", "in", ["draft", "sent"]]]):
        GW.c.execute_kw("sale.order", "action_cancel", [[o]])
    return prof


def _conv(prof):
    a = PharmacyAgent(llm=NoLLM(), gateway=GW)
    conv = Conversation(customer_id=prof["customer_id"], customer_facing=True, lang="ar")
    conv.started = True
    conv.last_ts = __import__("time").time()
    conv.customer_name, conv.profile = prof["name"], {"address": prof["address"]}
    return a, conv


def _handovers(conv):
    return GW.c.search_read("pharmacy.handover", [["conv_id", "=", conv.conv_id]],
                            ["reason", "sale_order_id", "summary"])


def test_scheduled_is_refused_with_availability_never_escalated():
    a, conv = _conv(_customer("0500002201"))
    reply = a.respond(conv, "عندكم ترامادول؟")
    assert "حضورياً" in reply and "مختومة" in reply and "📍" in reply
    assert "سأحوّلك" not in reply and "صورة" not in reply
    assert _handovers(conv) == []


def test_dose_question_without_an_order_is_refused_not_escalated():
    a, conv = _conv(_customer("0500002202"))
    reply = a.respond(conv, "كم جرعة البنادول باليوم؟")
    assert reply == "عذراً، ما أقدر أعطي نصائح طبية أو جرعات. إذا الدواء ضمن طلبك أو سلتك، أرسل صورة الوصفة 📎 " \
                    "وأقرأ لك الجرعة المكتوبة فيها. ويقدر الصيدلي في أي فرع يجاوب سؤالك."
    assert _handovers(conv) == []


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_dose_question_on_cart_item_reads_prescription_then_dispute_escalates_with_quote():
    prof = _customer("0500002203")
    metformin = GW.c.search("product.product", [["sfda_scientific_name", "ilike", "metformin"],
                                                ["is_scheduled", "=", False]], limit=1)
    assert metformin, "catalogue has no metformin product"
    cart = GW.cart_sync(prof["customer_id"], [{"product_id": metformin[0], "qty": 1}])
    a, conv = _conv(prof)
    reply = a.respond(conv, "كم مرة اخذ الدواء اللي في سلتي باليوم؟")
    assert cart["cart_ref"] in reply and "صورة الوصفة" in reply and conv.dose_ctx
    reply = a.respond(conv, "", image=SAMPLE_RX.read_bytes(), image_name="rx.png")
    assert "1 tab twice daily with meals" in reply and "1 tab at night" in reply
    assert _handovers(conv) == []                         # read back, nobody bothered yet
    rx = GW.c.search_read("pharmacy.prescription", [["partner_id", "=", prof["customer_id"]]],
                          ["sale_order_id", "directions"], order="id desc", limit=1)[0]
    assert rx["sale_order_id"][0] == cart["cart_id"] and "twice daily" in rx["directions"]
    reply = a.respond(conv, "لا هذا غلط، الدكتور قال مرة وحدة")
    assert cart["cart_ref"] in reply
    h = _handovers(conv)
    assert len(h) == 1 and h[0]["reason"] == "medical_advice" and h[0]["sale_order_id"][0] == cart["cart_id"]
    GW.cart_sync(prof["customer_id"], [])


def test_handover_without_order_or_medicine_is_refused():
    r = GW.escalate_to_human("complaint", "something went wrong", customer_id=None)
    assert r["error"]["code"] == "needs_context"


def test_cart_is_a_quotation_and_checkout_reuses_it():
    prof = _customer("0500002204")
    pid = GW.c.search("product.product", [["name", "=", "Panadol 500mg"]])[0]
    c = GW.cart_sync(prof["customer_id"], [{"product_id": pid, "qty": 2}])
    assert GW.get_cart(prof["customer_id"])["items"][0]["qty"] == 2
    branch = next(b["branch_id"] for b in GW.check_stock(pid)["stock"] if b["qty_available"] >= 2)
    o = GW.create_order(prof["customer_id"], [{"product_id": pid, "qty": 2}], "pickup", branch_id=branch,
                        cart_id=c["cart_id"])
    assert o["order_id"] == c["cart_id"]                  # the same quotation, now checked out
    assert GW.get_cart(prof["customer_id"])["items"] == []
    GW.customer_cancels(o["order_id"], prof["customer_id"])


def test_order_ref_is_matched_exactly():
    prof = _customer("0500002205")
    pid = GW.c.search("product.product", [["name", "=", "Panadol 500mg"]])[0]
    branch = next(b["branch_id"] for b in GW.check_stock(pid)["stock"] if b["qty_available"] >= 1)
    o = GW.create_order(prof["customer_id"], [{"product_id": pid, "qty": 1}], "pickup", branch_id=branch)
    digits = o["order_ref"].lstrip("S").lstrip("0")
    assert GW.get_order_details(order_ref=digits, customer_id=prof["customer_id"])["order_id"] == o["order_id"]
    assert GW.get_order_details(order_ref=digits + "9", customer_id=prof["customer_id"])["error"]["code"] == "not_found"
    assert GW.cancel_order(customer_id=prof["customer_id"])["error"]["code"] == "invalid_input"
    GW.customer_cancels(o["order_id"], prof["customer_id"])


def test_another_prescriptions_directions_are_never_read_back():
    prof = _customer("0500002206")
    cid = prof["customer_id"]
    panadol = GW.c.search("product.product", [["name", "=", "Panadol 500mg"]])[0]
    amoxil = GW.c.search("product.product", [["name", "=", "Amoxil 500mg"]])[0]
    rid = GW.c.create("pharmacy.prescription", {"name": "ZZ-OTHER", "partner_id": cid, "state": "submitted",
                                                "directions": "Amoxil: 1 cap every 8 hours",
                                                "matched_product_ids": [(6, 0, [amoxil])]})
    cart = GW.cart_sync(cid, [{"product_id": panadol, "qty": 1}])
    assert GW.prescription_directions(cid, cart["cart_id"]) is None     # it names Amoxil, not Panadol
    GW.cart_sync(cid, [{"product_id": amoxil, "qty": 1}])
    assert GW.prescription_directions(cid, cart["cart_id"])["directions"] == ["Amoxil: 1 cap every 8 hours"]
    GW.c.write("pharmacy.prescription", [rid], {"state": "rejected"})
    GW.cart_sync(cid, [])


def test_prescription_needs_explicit_consent_first(monkeypatch):
    """PDPL: nothing from a prescription is read or stored before an explicit yes; a no keeps
    nothing; consent can be withdrawn by message and is queued for staff."""
    prof = GW.register_customer("ZZ Consent Test", "0500002206", "الرياض، حي العليا، شارع 3")
    GW.c.write("res.partner", [prof["customer_id"]], {"pdpl_health_consent": False, "pdpl_consent_withdrawn": False})
    submitted = []
    monkeypatch.setattr(GW, "submit_prescription", lambda *a, **k: submitted.append(a) or {
        "ok": False, "problems": ["no_invoice"], "date": None, "medications": [], "directions": []})
    a, conv = _conv(prof)
    reply = a.respond(conv, "", image=b"fake-image", image_name="rx.jpg")
    assert "موافقتك" in reply and [b["id"] for b in conv.last_menu] == ["consent_yes", "consent_no"]
    assert submitted == []                                # nothing read before the yes
    reply = a.respond(conv, "❌ لا أوافق")
    assert "ما استلمنا" in reply and submitted == [] and not GW.has_health_consent(prof["customer_id"])
    a.respond(conv, "", image=b"fake-image", image_name="rx.jpg")
    a.respond(conv, "✅ أوافق")
    assert len(submitted) == 1 and GW.has_health_consent(prof["customer_id"])
    reply = a.respond(conv, "اسحب موافقتي لو سمحت")
    assert "سحب موافقتك" in reply and not GW.has_health_consent(prof["customer_id"])
    h = GW.c.search_read("pharmacy.handover", [["conv_id", "=", conv.conv_id]], ["reason"])
    assert [x["reason"] for x in h] == ["privacy_request"]
