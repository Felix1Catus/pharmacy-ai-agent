# -*- coding: utf-8 -*-
"""Active-ingredient search: pure parsing (always) + search behaviour on the live catalogue
(skips if Odoo is down or the SFDA catalogue is not imported).

Run:  pytest adapter/test_ingredients.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "adapter")]

import ingredients as ingr  # noqa: E402


def test_ingredient_sets_normalised_across_sources():
    sfda = ingr.ingredient_set("AMOXICILLIN,CLAVULANIC ACID")
    ours = ingr.ingredient_set("Amoxicillin+Clavulanate")
    assert sfda == ours == {"amoxicillin", "clavulanic acid"}
    assert ingr.ingredient_set("AMOXICILLIN") != sfda          # combination is a different set
    assert ingr.ingredient_set("Calcium+VitD") == {"calcium", "vitamin d"}


def test_parse_query_strength_and_form():
    p = ingr.parse_query("amoxicillin 1mg tablet")
    assert p == {"core": "amoxicillin", "strength_mg": 1.0, "form": "tablet"}
    p = ingr.parse_query("اموكسيسيلين ٥٠٠ ملجم كبسول")
    assert p["strength_mg"] == 500 and p["form"] == "capsule" and p["core"] == "اموكسيسيلين"
    assert ingr.strength_mg("Augmentin 1g") == 1000 and ingr.strength_mg("JULMENTIN 1.2GM INJ.") == 1200
    assert ingr.fmt_mg(1000) == "1 g" and ingr.fmt_mg(625) == "625 mg"


def _gw():
    try:
        from gateway import Gateway
        g = Gateway()
        if g.c.search_count("product.template", [["sfda_scientific_name", "ilike", "amoxicillin"]]) < 10:
            return None
        return g
    except Exception:
        return None


GW = _gw()
live = pytest.mark.skipif(GW is None, reason="Odoo down or SFDA catalogue not imported")


@live
@pytest.mark.parametrize("q", ["amoxicillin 1mg tablet", "amoxicillin", "اموكسيسيلين"])
def test_ingredient_query_lists_plain_amoxicillin_first(q):
    r = GW.search_product(q)
    assert r["active_ingredient"] == "AMOXICILLIN"
    sets = [ingr.ingredient_set(c["scientific_name"]) for c in r["candidates"]]
    assert sets and sets[0] == {"amoxicillin"}                 # not an amoxicillin+clavulanate injection
    assert all(s == {"amoxicillin"} for s in sets[:3])


@live
def test_impossible_strength_reports_available_ones():
    r = GW.search_product("amoxicillin 1mg tablet")
    assert "1 mg is not available" in r["note"] and "500 mg" in r["note"]


@live
def test_brand_query_offers_same_ingredient_brands():
    r = GW.search_product("augmentin")
    assert r["active_ingredient"] == "AMOXICILLIN + CLAVULANIC ACID"
    alt_sets = {frozenset(ingr.ingredient_set(a["scientific_name"])) for a in r["alternatives"]}
    assert r["alternatives"] and alt_sets == {frozenset({"amoxicillin", "clavulanic acid"})}
    names = [c["name_en"] for c in r["candidates"] + r["alternatives"]]
    assert len(names) == len(set(names))                        # no duplicate listings


@live
def test_unknown_brand_by_active_ingredient():
    r = GW.search_product(query="", active_ingredient="amoxicillin, clavulanic acid")
    assert r["candidates"] and r["active_ingredient"] == "AMOXICILLIN + CLAVULANIC ACID"


@live
def test_nothing_found_gives_hint():
    r = GW.search_product("zzqxvbrandname")
    assert not r["candidates"] and "active ingredient" in r["hint"]


@live
def test_plain_brand_search_still_works():
    r = GW.search_product("panadol")
    assert any("panadol" in c["name_en"].lower() for c in r["candidates"])
