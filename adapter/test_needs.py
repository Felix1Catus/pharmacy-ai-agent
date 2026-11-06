# -*- coding: utf-8 -*-
"""Customer needs ("something for a headache") -> over-the-counter products (adapter/needs.py).

The pure tests run anywhere; the catalogue tests use the real (demo) Odoo and skip if it is down.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "adapter")]

from needs import NEEDS, detect_need  # noqa: E402


@pytest.mark.parametrize("text,need", [
    ("ابغي دوا للصداع", "pain_headache"), ("راسي يعورني مرة", "pain_headache"), ("ابي مسكن", "pain_headache"),
    ("عندي حرارة", "fever"), ("ولدي فيه سخونية", "fever"), ("ابي شي للزكام", "cold_flu"),
    ("خشمي مسدود", "blocked_nose"), ("something for a blocked nose", "blocked_nose"),
    ("زوري واجعني", "sore_throat"), ("عندي كحة", "cough"), ("حرقة في المعدة", "heartburn"),
    ("ولدي عنده اسهال", "diarrhea_dehydration"), ("بشرتي ناشفة", "dry_skin"), ("حساسية وعطاس", "allergy"),
    ("واقي شمس", "sun_protection"), ("جرح بسيط في يدي", "wound_care"), ("شي يقوي المناعة", "immunity_vitamins"),
])
def test_detect_need(text, need):
    assert detect_need(text) == need


@pytest.mark.parametrize("text", ["عندكم بنادول؟", "كم سعر سنتروم", "Order 1 Centrum for delivery", "وين فروعكم",
                                  "المنتج متوفر؟", "العالم كله", "احجز لي ديتول",
                                  "بطني يوجعني"])        # stomach pain: many causes, left to the pharmacist
def test_no_need_in_product_and_other_messages(text):
    assert detect_need(text) is None


def test_model_is_not_offered_symptom_search():
    # choosing a medicine for a symptom is the licensed pharmacist's call: no `need` for the model
    schema = json.loads((ROOT / "contract" / "schemas" / "search_product.json").read_text(encoding="utf-8"))
    assert "need" not in schema["parameters"]["properties"]
    for key, n in NEEDS.items():
        assert n["terms"] and n["ar"] and n["en"], key


def _gw():
    try:
        from gateway import Gateway
        g = Gateway()
        return g if g.c.search_count("product.template", [["sfda_scientific_name", "ilike", "paracetamol"]]) else None
    except Exception:
        return None


@pytest.fixture()
def gw(monkeypatch):
    g = _gw()
    if g is None:
        pytest.skip("Odoo or the SFDA catalogue is not available")
    import gateway
    monkeypatch.setattr(gateway, "SYMPTOM_SUGGESTIONS", True)   # the pharmacist-approved table, opt-in
    return g


def test_symptoms_return_no_products_by_default():
    g = _gw()
    if g is None:
        pytest.skip("Odoo or the SFDA catalogue is not available")
    for kw in ({"need": "sore_throat"}, {"query": "سعال"}, {"query": "something for a headache"}):
        r = g.search_product(**kw)
        assert r["candidates"] == [] and r.get("symptom_request") and "pharmacist" in r["hint"], kw
    assert g.search_product(query="بنادول")["candidates"]         # a named product is still found


def test_need_lists_over_the_counter_products_only(gw):
    for key in NEEDS:
        r = gw.search_product(need=key)
        assert r.get("need") == key
        for c in r.get("candidates") or []:
            assert not (c.get("requires_prescription") or c.get("is_restricted") or c.get("is_scheduled")), (key, c)
    r = gw.search_product(need="pain_headache")
    names = {(c.get("scientific_name") or "").lower() for c in r["candidates"]}
    assert any("paracetamol" in n for n in names) and any("ibuprofen" in n for n in names)


def test_symptom_words_fall_back_to_need(gw):
    r = gw.search_product(query="ابغي دوا للصداع")
    assert r.get("need") == "pain_headache" and r["candidates"]


def test_need_name_put_in_query_is_treated_as_need(gw):
    r = gw.search_product(query="blocked_nose")          # seen live: the model put the enum value in query
    assert r.get("need") == "blocked_nose" and r["candidates"]


def test_weak_sound_alike_does_not_beat_a_need(gw):
    r = gw.search_product(query="خشمي مسدود", need="blocked_nose")
    assert r.get("need") == "blocked_nose"


def test_named_product_wins_over_need(gw):
    r = gw.search_product(query="بنادول", need="heartburn")
    assert "need" not in r and any("panadol" in (c.get("name_en") or "").lower() for c in r["candidates"])


def test_unknown_need_is_rejected_without_query(gw):
    assert gw.search_product(need="make_me_rich")["error"]["code"] == "invalid_input"
