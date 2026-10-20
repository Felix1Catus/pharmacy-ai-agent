# -*- coding: utf-8 -*-
"""WhatsApp webhook in DRY RUN (no Meta credentials): verification, signature, number-based
customer recognition, menu as a list message, registration buttons. Needs the live Odoo.

Run:  pytest channels/whatsapp/test_webhook.py -q
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.update({"WHATSAPP_VERIFY_TOKEN": "test-verify", "WHATSAPP_APP_SECRET": "test-secret",
                   "WHATSAPP_ACCESS_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": ""})
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fastapi.testclient import TestClient
    import webhook
    webhook.agent()                                   # Odoo reachable?
except Exception as e:                                # noqa: BLE001
    pytest.skip(f"Odoo not reachable: {e}", allow_module_level=True)

client = TestClient(webhook.app)


def _post(wa_id, text=None, kind="text", name="Test User", extra=None):
    m = {"from": wa_id, "id": f"wamid.{time.time_ns()}", "timestamp": str(int(time.time())), "type": kind}
    m.update({"text": {"body": text}} if kind == "text" else extra or {})
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
        "messages": [m]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig,
                                                            "Content-Type": "application/json"})


def _last_out(to):
    rows = [json.loads(l) for l in webhook.OUTBOX.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["to"] == to][-1]


def test_verification_handshake():
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify",
                                        "hub.challenge": "1234"})
    assert ok.status_code == 200 and ok.text == "1234"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                          "hub.challenge": "1"}).status_code == 403


def test_bad_signature_rejected():
    r = client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00"})
    assert r.status_code == 401


def test_known_number_greeted_by_name_with_menu_list():
    gw = webhook.agent().gw
    prof = gw.register_customer("Webhook Known Customer", "0500000101", "Riyadh, Al Olaya, King Fahd Rd 1")
    wa = "966500000101"
    webhook._convs.pop(wa, None)
    assert _post(wa, "السلام عليكم").status_code == 200
    out = _last_out(wa)
    assert out["type"] == "interactive" and out["interactive"]["type"] == "list"
    assert prof["name"] in out["interactive"]["body"]["text"]
    titles = [r["title"] for r in out["interactive"]["action"]["sections"][0]["rows"]]
    assert "👤 عميل حالي / جديد" in titles and len(titles) == 5


def test_unknown_number_registers_through_buttons():
    wa = f"9665{time.time_ns() % 10**8:08d}"
    webhook._convs.pop(wa, None)
    _post(wa, "مرحبا")                                    # greeting + menu (Arabic)
    _post(wa, None, kind="interactive", extra={"interactive": {
        "type": "list_reply", "list_reply": {"id": "customer", "title": "👤 عميل حالي / جديد"}}})
    assert "اسمك" in _last_out(wa)["text"]["body"]       # not found -> asks for the name
    _post(wa, "Test Registrant")
    _post(wa, "الرياض، حي النرجس، شارع الامير سعود 12")
    out = _last_out(wa)
    assert out["type"] == "interactive" and out["interactive"]["type"] == "button"   # ⏭️ تخطي
    _post(wa, None, kind="interactive", extra={"interactive": {
        "type": "button_reply", "button_reply": {"id": "skip", "title": "⏭️ تخطي"}}})
    _post(wa, None, kind="interactive", extra={"interactive": {
        "type": "button_reply", "button_reply": {"id": "save", "title": "✅ حفظ"}}})
    prof = webhook.agent().gw.find_customer(wa)
    assert prof and prof["name"] == "Test Registrant" and "النرجس" in prof["address"]
    assert webhook._convs[wa].customer_id == prof["customer_id"]
