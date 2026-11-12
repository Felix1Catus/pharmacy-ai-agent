"""The Odoo-backed tools added in pharmacy_agent 1.9.0: label facts (storage / fridge / shelf life /
maker), dispensing class, order details, cancel, prescriptions, back-in-stock requests, branches,
handovers in Odoo. Storage parsing runs without Odoo; the rest against the live `pharmacy` DB.

Run:  pytest adapter/test_pharmacy_tools.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# the Odoo module's pure parser (not on the shared test path: that folder also has a prescription.py)
sys.path.append(str(ROOT / "odoo" / "addons" / "pharmacy_agent" / "models"))

from storage_parse import manufacturer_country, parse_storage, shelf_life_months  # noqa: E402


# ---- storage text -> fields (every SFDA wording seen in the catalogue) -----------------------
@pytest.mark.parametrize("text, cls, lo, hi", [
    ("يخزن الدواء في الثلاجة في درجة حرارة ( 2 درجة الى 8 درجة مئوية )", "fridge", 2, 8),
    ("يحفظ في درجة حرارة الغرفة ( بحيث لا تتجاوز 25 درجة مئوية )", "room", None, 25),
    ("يحفظ في درجة حرارة الغرفة ( بحيث لا تتجاوز 30 مئوية )", "room", None, 30),
    ("يحفظ في جهاز التبريد ( درجة حرارة أقل من 10 درجة مئوية )", "fridge", None, 10),
    ("يحفظ بين (-15 درجة مئوية إلى - 25 درجة مئوية)", "freezer", -25, -15),
    ("Store below 30°C, Store in a dry place", "room", None, 30),
    ("يحفظ في درجة حرارة الغرفة ( 2 درجة الى 25 درجة مئوية )", "room", 2, 25),
    ("", "unknown", None, None),
])
def test_storage_class_and_range(text, cls, lo, hi):
    p = parse_storage(text)
    assert (p["storage_class"], p["min_c"], p["max_c"]) == (cls, lo, hi)
    assert p["cold_chain"] == (cls in ("fridge", "freezer", "cool"))


def test_storage_extra_conditions():
    p = parse_storage("يحفظ في درجة حرارة الغرفة (أقل من درجة حرارة 30 مئوية ) ، يحفظ بعيدًا عن الضوء و الرطوبة")
    assert p["protect_light"] and p["protect_moisture"] and not p["no_freeze"]
    assert parse_storage("لا يحفظ بدرجة حرارة أعلى من 30 درجة مئوية، لا يجمد")["no_freeze"]
    assert parse_storage("يحفظ في درجة حرارة الغرفة (أقل من درجة حرارة 30 مئوية ) ، لا تدع الدواء يتجمد")["no_freeze"]


def test_label_helpers():
    assert shelf_life_months("36") == 36 and shelf_life_months(None) == 0
    assert manufacturer_country("الاسم: A | الدولة: Germany ; الاسم: B | الدولة: Belgium") == "Germany, Belgium"


# ---- live Odoo ---------------------------------------------------------------------------
try:
    from gateway import Gateway  # noqa: E402
    _GW = Gateway()
    _GW.c.search("pharmacy.handover", [], limit=1)
except Exception:  # noqa: BLE001  (Odoo down or module not upgraded)
    _GW = None
live = pytest.mark.skipif(_GW is None, reason="Odoo not reachable / pharmacy_agent < 1.9.0")


@pytest.fixture(scope="module")
def gw():
    return _GW


@pytest.fixture(scope="module")
def cust(gw):
    return gw.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0]


def _pid(gw, name):
    ids = gw.c.search("product.product", [["name", "=", name]], limit=1)
    if not ids:
        pytest.skip(f"{name} is not in this catalogue (SFDA import needed)")
    return ids[0]


@live
def test_fridge_medicine_details(gw):
    d = gw.get_product_details(_pid(gw, "CERVARIX VACCINE"))["products"][0]
    assert d["storage"]["class"] == "fridge" and d["storage"]["min_c"] == 2 and d["storage"]["max_c"] == 8
    assert d["cold_chain_delivery"] and d["shelf_life_months"] == 60
    assert "Germany" in d["made_in"] and d["dispensing"] == "prescription"
    assert "dose" not in str(d).lower()


@live
def test_missing_facts_are_spelled_out_never_guessed(gw):
    r = gw.get_product_details(_pid(gw, "Panadol 500mg"))
    d = r["products"][0]
    assert d["storage"].startswith("NOT RECORDED") and d["pack"].startswith("NOT RECORDED") and "hint" in r
    assert d["dispensing"] == "otc"


@live
def test_scheduled_details_refused(gw):
    r = gw.get_product_details(product_ids=[_pid(gw, "Panadol 500mg"), _pid(gw, "Tramadol 50mg")])
    assert r["error"]["code"] == "scheduled_item"


@live
def test_dispensing_class_mapping(gw):
    rows = {r["name"]: r["dispensing_class"] for r in gw.c.search_read(
        "product.template", [["name", "in", ["Panadol 500mg", "Amoxil 500mg", "Xanax 0.5mg"]]],
        ["name", "dispensing_class"])}
    assert rows == {"Panadol 500mg": "otc", "Amoxil 500mg": "prescription", "Xanax 0.5mg": "scheduled"}


@live
def test_branches_by_city(gw):
    if not any(b.get("city") for b in gw.branches()):
        pytest.skip("branch cities are not filled in on this Odoo")
    r = gw.list_branches("جدة")
    assert [b["city"] for b in r["branches"]] == ["Jeddah"] and r["branches"][0]["hours"] == "24/7"
    assert "note" in gw.list_branches("tabuk") and "loc_id" not in gw.list_branches()["branches"][0]


@live
def test_order_details_cancel_and_ownership(gw, cust):
    pid = _pid(gw, "Panadol 500mg")
    branch = next(b["branch_id"] for b in gw.check_stock(pid)["stock"] if b["qty_available"] >= 1)
    res = gw.create_order(cust, [{"product_id": pid, "qty": 1}], "pickup", branch_id=branch)
    oid = res["order_id"]
    d = gw.get_order_details(order_ref=res["order_ref"], customer_id=cust)
    assert d["order_id"] == oid and d["can_cancel"] and d["items"][0]["name"] == "Panadol 500mg"
    other = gw.c.search("res.partner", [["id", "!=", cust], ["customer_rank", ">", 0]], limit=1)
    if other:                                   # someone else's order is never readable
        assert gw.get_order_details(oid, customer_id=other[0])["error"]["code"] == "not_found"
    assert gw.cancel_order(oid, reason="ordered twice", customer_id=cust)["cancelled"]
    assert gw.cancel_order(oid, customer_id=cust)["error"]["code"] == "not_cancellable"


@live
def test_stock_alert(gw, cust):
    pid = _pid(gw, "Panadol 500mg")
    assert gw.request_stock_alert(pid, customer_id=cust).get("already_available")
    empty = next((p for p in gw.c.search("product.product", [["is_scheduled", "=", False],
                                                             ["sfda_reg_no", "!=", False]], limit=50)
                  if all(gw._avail_at(p, b["loc_id"]) <= 0 for b in gw.branches())), None)
    if empty is None:
        pytest.skip("every product is in stock")
    r = gw.request_stock_alert(empty, customer_id=cust)
    assert r["recorded"]
    gw.request_stock_alert(empty, customer_id=cust)          # asked twice: one request
    ids = gw.c.search("pharmacy.stock.alert", [["partner_id", "=", cust], ["product_id", "=", empty],
                                               ["state", "=", "waiting"]])
    assert len(ids) == 1
    gw.c.write("pharmacy.stock.alert", ids, {"state": "cancelled"})


@live
def test_handover_is_an_odoo_record(gw, cust):
    r = gw.escalate_to_human("complaint", "test: wrong item delivered", customer_id=cust, medicine="Panadol",
                             conv_id="pytest", channel="webchat")
    assert r["handover_ref"].startswith("HO-")
    req = gw.track_order(customer_id=cust)["requests"]
    assert req[0]["ref"] == r["handover_ref"] and req[0]["status"].startswith("received")
    gw.c.write("pharmacy.handover", [r["handover_id"]], {"state": "cancelled"})


@live
def test_prescriptions_list(gw, cust):
    r = gw.list_prescriptions(customer_id=cust)
    assert "prescriptions" in r
    assert gw.list_prescriptions()["error"]["code"] == "customer_required"
