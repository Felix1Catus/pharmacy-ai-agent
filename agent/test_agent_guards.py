# -*- coding: utf-8 -*-
"""Deterministic agent guards with a FAKE model (no Ollama): Scheduled (مقيد) medicines are never
shown to the model or routed to the prescription-photo flow. Needs the live Odoo (skips if down).

Run:  pytest agent/test_agent_guards.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo")]

try:
    from agent import Conversation, PharmacyAgent
    from gateway import Gateway
    from guards import SCHEDULED_REFUSAL
    GW = Gateway()
except Exception:                     # Odoo down
    GW = None
pytestmark = pytest.mark.skipif(GW is None, reason="Odoo not reachable")

EYE_DROP = {"product_id": 1, "name_en": "Systane eye drops", "match": "spelling variant",
            "is_scheduled": False, "requires_prescription": False, "price_sar": 31.0}
BREX = {"product_id": 2, "name_en": "BREX 150mg", "scientific_name": "PREGABALIN",
        "match": "spelling variant", "is_scheduled": True, "requires_prescription": True,
        "price_sar": None}
PANADOL = {"product_id": 3, "name_en": "Panadol 500mg", "match": "exact",
           "is_scheduled": False, "requires_prescription": False, "price_sar": 12.0}


class FakeLLM:
    """First turn: call search_product; afterwards: reply with text. Records what it saw."""
    def __init__(self):
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        if messages[-1]["role"] == "user":
            return {"content": "", "tool_calls": [{"name": "search_product",
                                                   "arguments": {"query": "x"}}]}
        return {"content": "ارسل صورة الوصفة", "tool_calls": []}


def _agent(monkeypatch, candidates):
    llm = FakeLLM()
    a = PharmacyAgent(llm=llm, gateway=GW)
    monkeypatch.setattr(GW, "search_product", lambda **kw: {"candidates": [dict(c) for c in candidates]})
    return a, llm, Conversation(customer_id=GW.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0])


def test_scheduled_next_to_loose_match_is_refused(monkeypatch):
    # the real case: "بريقابلين" -> an unrelated eye drop (spelling variant) + Brex (مقيد)
    a, llm, conv = _agent(monkeypatch, [EYE_DROP, BREX])
    assert a.respond(conv, "هل في بريقابلين") == SCHEDULED_REFUSAL


def test_scheduled_hidden_when_exact_otc_matches(monkeypatch):
    a, llm, conv = _agent(monkeypatch, [PANADOL, BREX])
    reply = a.respond(conv, "paracetamol")
    assert reply != SCHEDULED_REFUSAL
    tool_msgs = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert "BREX" not in tool_msgs[-1] and "Panadol" in tool_msgs[-1]


def test_ingredient_of_scheduled_blocked_before_model(monkeypatch):
    a, llm, conv = _agent(monkeypatch, [PANADOL])
    if not a.denylist.match("pregabalin"):
        pytest.skip("no Scheduled pregabalin product in this catalogue")
    assert a.respond(conv, "do you have pregabalin?") == SCHEDULED_REFUSAL
    assert llm.seen == []                       # the model was never called


def test_prescription_with_only_scheduled_is_refused(monkeypatch):
    a, llm, conv = _agent(monkeypatch, [PANADOL])
    monkeypatch.setattr(GW, "submit_prescription", lambda *a, **k: {
        "ok": False, "problems": ["only_scheduled"], "date": "2026-10-19",
        "invoice_number": "INV-1", "medications": [], "scheduled_count": 1})
    assert a.respond(conv, "", image=b"%PDF-fake") == SCHEDULED_REFUSAL
    assert llm.seen == []
