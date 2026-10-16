"""Integration tests for the contract adapter against the live Odoo `pharmacy` DB.

Requires the Odoo stack up (cd odoo && docker compose up -d) and data seeded
(python odoo/seed_data.py). Tests auto-skip if Odoo is unreachable.

Run:  pytest adapter/test_adapter.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapter"))

from app import app  # noqa: E402
from gateway import Gateway  # noqa: E402

client = TestClient(app)

# skip the whole module if Odoo is down
_h = client.get("/health")
pytestmark = pytest.mark.skipif(
    _h.status_code != 200, reason="Odoo not reachable / not seeded")


@pytest.fixture(scope="module")
def gw():
    return Gateway()


@pytest.fixture(scope="module")
def customer_id(gw):
    return gw.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0]


def _call(action, body):
    return client.post(f"/tool/{action}", json=body)


def _variant(gw, code):
    t = gw.c.search_read("product.template", [["default_code", "=", code]],
                         ["product_variant_id"])[0]
    return t["product_variant_id"][0]


# ---- search --------------------------------------------------------------
def test_search_arabic_name():
    r = _call("search_product", {"query": "بنادول"})
    assert r.status_code == 200
    names = [c["name_en"] for c in r.json()["candidates"]]
    assert any("Panadol" in n for n in names)


def test_search_by_ingredient():
    r = _call("search_product", {"query": "paracetamol"})
    assert r.status_code == 200
    assert len(r.json()["candidates"]) >= 3


def test_search_flags_restricted():
    r = _call("search_product", {"query": "augmentin"})
    c = r.json()["candidates"][0]
    assert c["is_restricted"] and c["requires_prescription"]


def test_search_empty_query_is_422():
    r = _call("search_product", {"query": ""})
    assert r.status_code == 422  # schema query has minLength 1


# ---- stock ---------------------------------------------------------------
def test_check_stock_per_branch(gw):
    pid = _variant(gw, "PH-0001")
    r = _call("check_stock", {"product_id": pid})
    assert r.status_code == 200
    stock = r.json()["stock"]
    assert len(stock) == 3
    assert all("qty_available" in s for s in stock)


def test_check_stock_not_found():
    r = _call("check_stock", {"product_id": 99999999})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "not_found"


# ---- create_order --------------------------------------------------------
def test_create_order_pickup_success(gw, customer_id):
    pid = _variant(gw, "PH-0019")  # Centrum, usually in stock
    stock = gw.check_stock(pid)["stock"]
    branch = next((s["branch_id"] for s in stock if s["qty_available"] >= 1), None)
    assert branch, "need a branch with stock for this test"
    r = _call("create_order", {"customer_id": customer_id,
                               "items": [{"product_id": pid, "qty": 1}],
                               "fulfillment": "pickup", "branch_id": branch})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "sale"


def test_create_order_restricted_refused(gw, customer_id):
    pid = _variant(gw, "RX-0001")  # Augmentin
    r = _call("create_order", {"customer_id": customer_id,
                               "items": [{"product_id": pid, "qty": 1}],
                               "fulfillment": "pickup", "branch_id": 2})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "restricted_item"


def test_create_order_pickup_requires_branch(gw, customer_id):
    pid = _variant(gw, "PH-0001")
    r = _call("create_order", {"customer_id": customer_id,
                               "items": [{"product_id": pid, "qty": 1}],
                               "fulfillment": "pickup"})
    assert r.status_code == 422  # schema: branch_id required when pickup


def test_create_order_out_of_stock(gw, customer_id):
    pid = _variant(gw, "PH-0001")
    r = _call("create_order", {"customer_id": customer_id,
                               "items": [{"product_id": pid, "qty": 999999}],
                               "fulfillment": "pickup", "branch_id": 2})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "out_of_stock"


# ---- hold ----------------------------------------------------------------
def test_hold_otc_success(gw, customer_id):
    pid = _variant(gw, "PH-0019")
    stock = gw.check_stock(pid)["stock"]
    branch = next((s["branch_id"] for s in stock if s["qty_available"] >= 1), None)
    r = _call("hold_item", {"product_id": pid, "branch_id": branch,
                            "qty": 1, "customer_id": customer_id})
    assert r.status_code == 200, r.text
    assert "hold_id" in r.json()


def test_hold_scheduled_refused(gw, customer_id):
    pid = _variant(gw, "RX-0005")  # Tramadol — scheduled/controlled
    r = _call("hold_item", {"product_id": pid, "branch_id": 2,
                            "qty": 1, "customer_id": customer_id})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "scheduled_item"


def test_hold_restricted_refused(gw, customer_id):
    pid = _variant(gw, "RX-0003")  # Ventolin — prescription, not scheduled
    r = _call("hold_item", {"product_id": pid, "branch_id": 2,
                            "qty": 1, "customer_id": customer_id})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "restricted_item"


# ---- track ---------------------------------------------------------------
def test_track_by_customer(customer_id):
    r = _call("track_order", {"customer_id": customer_id})
    assert r.status_code == 200
    assert len(r.json()["orders"]) >= 1


def test_track_requires_arg():
    r = _call("track_order", {})
    assert r.status_code == 422  # schema anyOf requires order_id or customer_id


# ---- escalate ------------------------------------------------------------
def test_escalate():
    r = _call("escalate_to_human", {"reason": "medical_advice",
                                    "summary": "asked for dosage"})
    assert r.status_code == 200
    assert "handover_id" in r.json()


def test_escalate_bad_reason_is_422():
    r = _call("escalate_to_human", {"reason": "nope", "summary": "x"})
    assert r.status_code == 422


# ---- SFDA registry prescription flag (gateway must block it like our own flags) ----
@pytest.fixture()
def sfda_rx_product(gw):
    """Temporary product flagged ONLY via the SFDA import field (no x_ flags, not scheduled)."""
    tmpl = gw.c.create("product.template", {
        "name": "TEST SFDA RX ONLY", "type": "consu", "sale_ok": True,
        "sfda_reg_no": "TEST-SFDA-RX", "sfda_requires_prescription": True,
        "sfda_dispensing": "يحتاج الى وصفة طبية", "sfda_control": "غير مقيد"})
    vid = gw.c.read("product.template", tmpl, ["product_variant_id"])[0]["product_variant_id"][0]
    yield vid
    gw.c.execute_kw("product.template", "unlink", [[tmpl]])


def test_create_order_sfda_prescription_refused(sfda_rx_product, customer_id):
    r = _call("create_order", {"customer_id": customer_id,
                               "items": [{"product_id": sfda_rx_product, "qty": 1}],
                               "fulfillment": "pickup", "branch_id": 2})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "restricted_item"


def test_hold_sfda_prescription_refused(sfda_rx_product, customer_id):
    r = _call("hold_item", {"product_id": sfda_rx_product, "branch_id": 2,
                            "qty": 1, "customer_id": customer_id})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "restricted_item"


def test_search_reports_sfda_prescription(sfda_rx_product):
    c = _call("search_product", {"query": "TEST SFDA RX ONLY"}).json()["candidates"][0]
    assert c["requires_prescription"] and c["is_restricted"] and not c["is_scheduled"]
