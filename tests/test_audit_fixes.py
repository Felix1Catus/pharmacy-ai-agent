# -*- coding: utf-8 -*-
"""Regression tests for the hardening round (identity, prescription enforcement, holds, history
window, WhatsApp channel robustness, health-data handling, guards).

The first half is pure (no services). The second half needs the Odoo with the pharmacy_agent
module 18.0.1.12.0+ and the sample data, and skips itself otherwise.

Run:  pytest tests/test_audit_fixes.py -q
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ.update({"WHATSAPP_VERIFY_TOKEN": "test-verify", "WHATSAPP_APP_SECRET": "test-secret",
                   "WHATSAPP_ACCESS_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": ""})

from agent import Conversation, PharmacyAgent, _redact_prescription, history_window  # noqa: E402
from gateway import Gateway, normalize_phone, phone_key  # noqa: E402
from guards import ScheduledDenylist, is_affirmation, medical_advice_intent  # noqa: E402
from odoo_client import OdooClient, OdooError  # noqa: E402


# =============================================================== pure
def test_phone_key_keeps_a_whatsapp_id_we_do_not_recognise():
    assert phone_key("0501234567") == "966501234567" == phone_key("+966 50 123 4567")
    # a Saudi landline business number: not a mobile, but it IS the sender's identity
    assert normalize_phone("966112345678") is None and phone_key("966112345678") == "966112345678"
    assert phone_key("abc") is None and phone_key("12") is None


@pytest.mark.parametrize("text,yes", [
    ("نعم", True), ("تمام", True), ("yes please", True), ("ok", True), ("ايوه اكد", True),
    ("تمام بس غير الكمية", False),        # qualified: still changing the order
    ("طيب ما ابي", False),                 # negated with "ما"
    ("ok but make it 3", False), ("yes, wait", False), ("طيب لحظة", False),
    ("تمام؟", False), ("لا", False), ("no", False),
])
def test_only_a_plain_yes_confirms(text, yes):
    assert is_affirmation(text) is yes


@pytest.mark.parametrize("text,medical", [
    ("ابي بنادول وفيتامين سي مع بعض", False),       # two products in one order
    ("اقدر اخذهم مع بعض؟", True),                    # combining medicines
    ("ابي فيتامينات للحامل", False),                 # a product for pregnant women
    ("هل البنادول امن للحامل؟", True),               # a safety question
    ("is it safe during pregnancy?", True),
    ("prenatal vitamins for my pregnant wife", False),
])
def test_medical_guard_tells_orders_from_advice(text, medical):
    assert medical_advice_intent(text) is medical


def test_denylist_short_terms_need_a_whole_word():
    d = ScheduledDenylist(["Xan", "Tramadol 50mg", "tramadol", "New Lyrica", "new", "ليريكا"])
    assert d.match("do you sell xanthan gum?") is None          # 'xan' inside another word
    assert d.match("ابي xan لو سمحت") == "xan"
    assert d.match("عندكم بليريكا؟") == "ليريكا"                 # Arabic prefix
    assert d.match("tramadol50mg") == "tramadol"                 # long terms match inside a word
    assert d.match("I want a new order") is None                 # a generic first word is not a medicine


def test_history_window_keeps_this_turn_and_starts_at_a_customer_message():
    msgs = []
    for i in range(40):
        msgs += [{"role": "user", "content": f"سؤال {i} " + "x" * 80},
                 {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_product",
                                                                                   "arguments": {"query": "q"}}}]},
                 {"role": "tool", "tool_name": "search_product", "content": "{}" + "y" * 80},
                 {"role": "assistant", "content": "جواب " + "z" * 80}]
    turn = len(msgs) - 4
    win = history_window(msgs, turn, budget=1500)
    assert win[-4:] == msgs[turn:] and win[0]["role"] == "user"
    assert len(win) < len(msgs) and sum(len(m["content"]) for m in win) <= 1500 + 400
    # a tool result is never sent without the assistant call before it
    for i, m in enumerate(win):
        if m["role"] == "tool":
            assert i > 0 and win[i - 1].get("tool_calls")
    # the turn itself is never dropped, however small the budget
    assert history_window(msgs, turn, budget=10) == msgs[turn:]
    assert history_window([], 0) == []


def test_audit_record_of_a_prescription_has_no_health_data():
    rec = _redact_prescription({"prescription_id": 5, "ok": True, "problems": [], "invoice_number": "INV-9",
                                "date": "2026-11-01", "medications": [{"name_en": "Metformin 500"}],
                                "directions": [{"medicine": "Metformin", "directions": "1 tab twice daily"}]})
    text = json.dumps(rec)
    assert rec["medicines_found"] == 1 and rec["ok"] and rec["prescription_id"] == 5
    assert "Metformin" not in text and "INV-9" not in text and "twice" not in text


def test_saved_conversation_never_contains_a_waiting_prescription_photo():
    conv = Conversation(1, customer_facing=True)
    conv.consent_pending = {"image": "QUJDREVGRw==", "image_name": "rx.jpg", "text": ""}
    saved = json.dumps(conv.to_dict())
    assert "QUJDREVGRw" not in saved and conv.consent_pending["image"] == "QUJDREVGRw=="
    back = Conversation.from_dict(json.loads(saved))
    assert back.consent_pending["image"] is None and back.consent_pending["image_lost"]


def test_prune_logs_covers_outbox_and_saved_chats(tmp_path):
    import prune_logs
    now = datetime.now(timezone.utc)
    old, new = now - timedelta(days=200), now - timedelta(days=1)
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps({"ts": t.isoformat(), "kind": "user"}) for t in (old, new)) + "\n",
                     encoding="utf-8")
    outbox = tmp_path / "whatsapp_outbox.jsonl"          # epoch timestamps
    outbox.write_text("\n".join(json.dumps({"ts": t.timestamp(), "to": "9665"}) for t in (old, new)) + "\n",
                      encoding="utf-8")
    cutoff = now - timedelta(days=90)
    assert prune_logs.prune(audit, cutoff, False) == (1, 1)
    assert prune_logs.prune(outbox, cutoff, False) == (1, 1)
    sess = tmp_path / "sessions"
    sess.mkdir()
    (sess / "old.json").write_text("{}")
    (sess / "new.json").write_text("{}")
    os.utime(sess / "old.json", (old.timestamp(), old.timestamp()))
    assert prune_logs.prune_files(sess, cutoff, True) == (1, 1) and (sess / "old.json").exists()   # dry run
    assert prune_logs.prune_files(sess, cutoff, False) == (1, 1) and not (sess / "old.json").exists()


def test_odoo_client_settings_for_a_remote_server(monkeypatch):
    for k in ("ODOO_API_KEY", "ODOO_VERIFY_SSL", "ODOO_CA_BUNDLE", "ODOO_PUBLIC_URL", "ODOO_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ODOO_PASSWORD", "pw")
    c = OdooClient(url="https://odoo.example.com/")
    assert c.url == "https://odoo.example.com" and c.public_url == c.url
    assert c.password == "pw" and c._session.verify is True
    monkeypatch.setenv("ODOO_API_KEY", "key-123")              # an API key wins over the password
    monkeypatch.setenv("ODOO_PUBLIC_URL", "https://shop.example.com")
    monkeypatch.setenv("ODOO_CA_BUNDLE", "/etc/ssl/private-ca.pem")
    monkeypatch.setenv("ODOO_TIMEOUT", "12")
    c = OdooClient(url="https://10.0.0.5:8069")
    assert c.password == "key-123" and c.public_url == "https://shop.example.com"
    assert c._session.verify == "/etc/ssl/private-ca.pem" and c.timeout == 12


def test_adapter_api_refuses_calls_without_the_token(monkeypatch):
    pytest.importorskip("jsonschema")
    from fastapi.testclient import TestClient
    import app as adapter_app
    monkeypatch.setenv("ADAPTER_TOKEN", "s3cret")
    client = TestClient(adapter_app.app)
    assert client.post("/tool/list_branches", json={}).status_code == 401
    assert client.post("/tool/list_branches", json={}, headers={"X-Adapter-Token": "wrong"}).status_code == 401


# =============================================================== live Odoo
try:
    GW = Gateway()
    GW.c.search("pharmacy.handover", [], limit=1)
    STAFF = OdooClient.admin()
    STAFF.search("res.users", [], limit=1)
    _HAS_HOLDS = bool(STAFF.execute_kw("sale.order", "fields_get", [["hold_expires_at"]]))
except Exception:  # noqa: BLE001
    GW = STAFF = None
    _HAS_HOLDS = False
live = pytest.mark.skipif(GW is None or not _HAS_HOLDS,
                          reason="Odoo not reachable / pharmacy_agent module older than 18.0.1.12.0")


def _phone() -> str:
    return f"05{time.time_ns() % 10**8:08d}"


def _variant(code):
    return GW.c.search_read("product.template", [["default_code", "=", code]],
                            ["product_variant_id"])[0]["product_variant_id"][0]


def _stock_up(vid, branch, qty=20):
    if GW._avail_at(vid, branch["loc_id"]) < 5:
        q = STAFF.create("stock.quant", {"product_id": vid, "location_id": branch["loc_id"],
                                         "inventory_quantity": qty})
        STAFF.execute_kw("stock.quant", "action_apply_inventory", [[q]])


class NoLLM:
    def chat(self, *a, **k):
        raise AssertionError("the model was called")


@live
def test_alternative_phone_does_not_identify_a_customer():
    main, alt = _phone(), _phone()
    prof = GW.register_customer("ZZ Test Alt Owner", main, "الرياض، حي العليا، شارع 1", alt_phone=alt)
    assert GW.find_customer(main)["customer_id"] == prof["customer_id"]
    assert prof["alt_phone"] and GW.find_customer(alt) is None      # whoever owns `alt` is NOT this customer


@live
def test_whatsapp_sender_is_never_asked_to_type_a_phone():
    wa = f"96611{time.time_ns() % 10**7:07d}"                        # a number format we do not parse
    assert normalize_phone(wa) is None
    a = PharmacyAgent(llm=NoLLM(), gateway=GW)
    conv = Conversation(None, customer_facing=True, lang="ar", wa_phone=phone_key(wa))
    reply = a.customer.start(conv)
    assert "اكتب رقم جوالك" not in reply and conv.flow["step"] == "ask_name"
    assert conv.flow["data"]["phone"] == wa                           # registration uses the sender's own id


@live
def test_prescription_of_another_customer_cannot_be_attached():
    mine = GW.register_customer("ZZ Test Rx Mine", _phone(), "الرياض، حي العليا، شارع 2")["customer_id"]
    other = GW.register_customer("ZZ Test Rx Other", _phone(), "الرياض، حي العليا، شارع 3")["customer_id"]
    b0, rx_pid = GW.branches()[0], _variant("RX-0001")
    _stock_up(rx_pid, b0)
    order = GW.create_order(mine, [{"product_id": rx_pid, "qty": 1}], "pickup", branch_id=b0["branch_id"])
    assert order["rx_review"] == "awaiting_photo"
    theirs = GW.c.create("pharmacy.prescription", {"name": "ZZ-TEST-OTHER", "partner_id": other,
                                                   "state": "submitted"})
    res = GW.attach_prescription(order["order_id"], theirs, mine)
    assert res["error"]["code"] == "not_found"
    assert GW.c.read("sale.order", order["order_id"], ["rx_review_state"])[0]["rx_review_state"] == "awaiting_photo"
    assert not GW.c.read("pharmacy.prescription", theirs, ["sale_order_id"])[0]["sale_order_id"]


@live
def test_unapproved_prescription_order_is_never_confirmed():
    cust = GW.register_customer("ZZ Test Rx Confirm", _phone(), "الرياض، حي العليا، شارع 4")["customer_id"]
    b0, rx_pid = GW.branches()[0], _variant("RX-0001")
    _stock_up(rx_pid, b0)
    order = GW.create_order(cust, [{"product_id": rx_pid, "qty": 1}], "pickup", branch_id=b0["branch_id"])
    oid = order["order_id"]
    # the gateway refuses ...
    assert GW.customer_confirms(oid, cust)["error"]["code"] == "prescription_not_approved"
    # ... and so does Odoo itself, for staff pressing Confirm or any other integration
    for client in (GW.c, STAFF):
        with pytest.raises(OdooError):
            client.execute_kw("sale.order", "action_confirm", [[oid]])
    STAFF.write("sale.order", [oid], {"rx_review_state": "pending"})
    with pytest.raises(OdooError):
        STAFF.execute_kw("sale.order", "action_confirm", [[oid]])
    assert GW.c.read("sale.order", oid, ["state"])[0]["state"] == "draft"
    # approved by the pharmacist: now the customer's yes confirms it and returns the payment link
    STAFF.execute_kw("sale.order", "action_rx_approve", [[oid]])
    res = GW.customer_confirms(oid, cust)
    assert "error" not in res and res["state"] == "sale" and "/my/orders/" in res["payment_link"]


@live
def test_only_a_pharmacist_approves():
    if GW.c.user == STAFF.user:
        pytest.skip("the assistant is still using the administrator login (scripts/create_service_user.py)")
    cust = GW.register_customer("ZZ Test Rx Approve", _phone(), "الرياض، حي العليا، شارع 5")["customer_id"]
    b0, rx_pid = GW.branches()[0], _variant("RX-0001")
    _stock_up(rx_pid, b0)
    oid = GW.create_order(cust, [{"product_id": rx_pid, "qty": 1}], "pickup", branch_id=b0["branch_id"])["order_id"]
    GW.c.write("sale.order", [oid], {"rx_review_state": "pending"})
    with pytest.raises(OdooError, match="pharmacist"):
        GW.c.execute_kw("sale.order", "action_rx_approve", [[oid]])
    assert GW.c.read("sale.order", oid, ["rx_review_state"])[0]["rx_review_state"] == "pending"
    # and the service account has no way into Odoo's settings or users
    with pytest.raises(OdooError):
        GW.c.search("ir.config_parameter", [], limit=1)
    with pytest.raises(OdooError):
        GW.c.write("res.users", [GW.c.uid], {"groups_id": [(4, 1)]})


@live
def test_a_hold_keeps_stock_from_other_customers_until_it_expires():
    a = GW.register_customer("ZZ Test Hold A", _phone(), "الرياض، حي العليا، شارع 6")["customer_id"]
    b0, pid = GW.branches()[0], _variant("PH-0004")
    _stock_up(pid, b0)
    bid = b0["branch_id"]
    before = GW._avail_map([pid], [b0])[pid][bid]
    hold = GW.hold_item(pid, bid, 2, a, hours=2)
    try:
        assert GW._avail_map([pid], [b0])[pid][bid] == before - 2                 # others see less
        assert GW._avail_map([pid], [b0], except_customer=a)[pid][bid] == before  # the holder does not
        stock = {s["branch_id"]: s["qty_available"] for s in GW.check_stock(pid)["stock"]}
        assert stock[bid] == before - 2
        # expired: Odoo's cron cancels it and the stock is free again
        STAFF.write("sale.order", [hold["hold_id"]], {"hold_expires_at": "2020-01-01 00:00:00"})
        assert GW._avail_map([pid], [b0])[pid][bid] == before
        cron = STAFF.search_read("ir.model.data", [["module", "=", "pharmacy_agent"],
                                                   ["name", "=", "ir_cron_expire_holds"]], ["res_id"])[0]["res_id"]
        STAFF.execute_kw("ir.cron", "method_direct_trigger", [[cron]])
        assert STAFF.read("sale.order", hold["hold_id"], ["state"])[0]["state"] == "cancel"
    finally:
        if STAFF.read("sale.order", hold["hold_id"], ["state"])[0]["state"] != "cancel":
            STAFF.execute_kw("sale.order", "action_cancel", [[hold["hold_id"]]])


@live
def test_back_in_stock_request_is_delivered_once():
    prof = GW.register_customer("ZZ Test Alert", _phone(), "الرياض، حي العليا، شارع 7")
    cid, pid = prof["customer_id"], _variant("PH-0001")
    _stock_up(pid, GW.branches()[0])
    alert = GW.c.create("pharmacy.stock.alert", {"partner_id": cid, "product_id": pid})   # asked while it was out
    try:
        due = GW.due_stock_alerts(cid)
        assert [d["alert_id"] for d in due] == [alert] and due[0]["branches"] and due[0]["whatsapp_number"]
        agent = PharmacyAgent(llm=NoLLM(), gateway=GW)
        conv = Conversation(cid, customer_facing=True, lang="ar")
        msgs = agent.customer.deliver_notifications(conv)
        assert len(msgs) == 1 and "متوفر" in msgs[0]["reply"]
        assert GW.due_stock_alerts(cid) == [] and agent.customer.deliver_notifications(conv) == []
    finally:
        GW.c.write("pharmacy.stock.alert", [alert], {"state": "cancelled"})


@live
def test_model_outage_gets_a_fixed_reply_not_an_exception():
    import requests

    class DownLLM:
        def chat(self, *a, **k):
            raise requests.ConnectionError("model server down")

    prof = GW.register_customer("ZZ Test Outage", _phone(), "الرياض، حي العليا، شارع 8")
    a = PharmacyAgent(llm=DownLLM(), gateway=GW)
    conv = Conversation(prof["customer_id"], customer_facing=True, lang="en")
    conv.started, conv.last_ts = True, time.time()
    reply = a.respond(conv, "what are your payment methods and do you accept installments for an order?")
    assert "busy" in reply and conv.last_guard == "unavailable"


# ---- WhatsApp channel (dry run) -------------------------------------------------------
@pytest.fixture()
def wa():
    if GW is None or not _HAS_HOLDS:
        pytest.skip("Odoo not reachable")
    import webhook
    webhook.agent()
    return webhook


def _incoming(webhook, wa_id, text):
    webhook.handle({"id": f"wamid.{time.time_ns()}", "wa_id": wa_id, "name": "T", "type": "text",
                    "text": text, "media_id": None, "filename": None})


def _outbox(webhook, to):
    rows = [json.loads(l) for l in webhook.OUTBOX.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["to"] == to]


@live
def test_whatsapp_conversation_survives_a_restart(wa):
    num = f"9665{time.time_ns() % 10**8:08d}"
    _incoming(wa, num, "مرحبا")
    conv = wa._convs[num]
    conv.awaiting_rx_order = 4242                          # e.g. waiting for a prescription photo
    wa.save(num)
    wa._convs.clear()                                      # the service restarted
    wa._meta.clear()
    back = wa.conversation(num)
    assert back is not conv and back.awaiting_rx_order == 4242 and back.wa_phone == num
    assert back.messages == conv.messages and wa._meta[num]["last_inbound"] > time.time() - 60
    wa._sess_file(num).unlink()


@live
def test_whatsapp_failure_still_answers_the_customer(wa, monkeypatch):
    num = f"9665{time.time_ns() % 10**8:08d}"
    monkeypatch.setattr(wa.agent(), "respond", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _incoming(wa, num, "hello there")                      # must not raise
    last = _outbox(wa, num)[-1]
    assert last["type"] == "text" and "🙏" in last["text"]["body"]
    wa._sess_file(num).unlink(missing_ok=True)


def _review(order_id=991):
    return {"order_id": order_id, "order_ref": "S00991", "review": "approved", "note": "", "total": 30.0,
            "fulfillment": "pickup", "address": "", "branch_name": "Riyadh - Olaya",
            "lines": [{"name": "Amoxil 500mg", "qty": 1, "total": 30.0}]}


@live
def test_review_result_is_only_marked_sent_when_whatsapp_accepted_it(wa, monkeypatch):
    num, marked = f"9665{time.time_ns() % 10**8:08d}", []
    conv = wa.conversation(num)
    conv.customer_id, conv.lang = 7, "ar"
    monkeypatch.setattr(wa.agent().gw, "mark_notified", lambda oid: marked.append(oid))
    monkeypatch.setattr(wa, "send", lambda *a, **k: False)            # Meta refused it
    n_msgs = len(conv.messages)
    assert wa._deliver_pending(num, conv, [_review()], []) == 0
    assert marked == [] and conv.awaiting_confirm_order is None and len(conv.messages) == n_msgs
    monkeypatch.setattr(wa, "send", lambda *a, **k: True)
    assert wa._deliver_pending(num, conv, [_review()], []) == 1
    assert marked == [991] and conv.awaiting_confirm_order == 991


@live
def test_outside_the_24_hour_window_a_template_knocks_once(wa, monkeypatch):
    num, sent, marked = f"9665{time.time_ns() % 10**8:08d}", [], []
    conv = wa.conversation(num)
    conv.customer_id, conv.lang = 7, "ar"
    monkeypatch.setattr(wa, "window_open", lambda wa_id: False)
    monkeypatch.setattr(wa, "TEMPLATE_NOTIFY", "order_update")
    monkeypatch.setattr(wa, "send_template", lambda to: sent.append(to) or True)
    monkeypatch.setattr(wa, "send", lambda *a, **k: pytest.fail("free text outside the window"))
    monkeypatch.setattr(wa.agent().gw, "mark_notified", lambda oid: marked.append(oid))
    assert wa._deliver_pending(num, conv, [_review()], []) == 0
    assert wa._deliver_pending(num, conv, [_review()], []) == 0       # next notifier round: no second template
    assert sent == [num] and marked == []
