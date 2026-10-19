# -*- coding: utf-8 -*-
"""Prescription photo checks: pure parsing (always), gateway policy against live Odoo (skips if
Odoo is down), and full OCR on a generated image (skips if `tesseract` is not installed).

Run:  pytest adapter/test_prescription.py -q
"""
from __future__ import annotations

import shutil
import time
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "evals" / "datasets")]

import prescription as rx  # noqa: E402

TODAY = date(2026, 10, 19)
PRODUCTS = [
    {"product_variant_id": [36, "Augmentin 1g"], "name": "Augmentin 1g", "x_name_ar": "اوجمنتين ١ جرام",
     "sfda_scientific_name": "AMOXICILLIN, CLAVULANIC ACID", "x_requires_prescription": True},
    {"product_variant_id": [37, "Amoxil 500mg"], "name": "Amoxil 500mg", "x_name_ar": "اموكسيل ٥٠٠",
     "sfda_scientific_name": "AMOXICILLIN", "x_requires_prescription": True},
    {"product_variant_id": [40, "Tramadol 50mg"], "name": "Tramadol 50mg", "x_name_ar": "ترامادول ٥٠",
     "x_requires_prescription": True, "is_scheduled": True},
    {"product_variant_id": [5, "Vitamin C 1000mg"], "name": "Vitamin C 1000mg", "x_name_ar": "فيتامين سي"},
]
GOOD = """AL SHIFA MEDICAL CENTER
Invoice No: INV-2026-004512
Date: 17/10/2026
Patient: Demo   MRN 552190   DOB: 03/04/1990
1. Augmentin 1g     1   as directed
Valid until 2026-11-17"""


# ---- pure parsing -----------------------------------------------------------
def test_good_prescription_passes():
    r = rx.evaluate(GOOD, PRODUCTS, TODAY)
    assert r["ok"], r["problems"]
    assert r["date"] == "2026-10-17"                       # not DOB, not "valid until"
    assert r["invoice_number"] == "INV-2026-004512"
    assert [m["product_id"] for m in r["medications"]] == [36]


def test_old_prescription_rejected():
    r = rx.evaluate(GOOD.replace("17/10/2026", "10/10/2026"), PRODUCTS, TODAY)
    assert not r["ok"] and "date_too_old" in r["problems"]


def test_five_days_is_still_valid():
    assert rx.prescription_date("Date: 14/10/2026", TODAY) == (date(2026, 10, 14), None)
    assert rx.prescription_date("Date: 13/10/2026", TODAY)[1] == "date_too_old"


def test_future_only_date_rejected():
    assert rx.prescription_date("Date: 25/10/2026", TODAY)[1] == "date_in_future"


def test_arabic_digits_and_labels():
    text = "رقم الفاتورة: ٧٧٨٨١٢\nالتاريخ: ١٨/١٠/٢٠٢٦\nاوجمنتين ١ جرام"
    r = rx.evaluate(text, PRODUCTS, TODAY)
    assert r["ok"], r["problems"]
    assert r["invoice_number"] == "778812" and r["date"] == "2026-10-18"


def test_iso_and_month_name_dates():
    assert rx.find_dates("2026-10-18") == [date(2026, 10, 18)]
    assert rx.find_dates("18 Oct 2026") == [date(2026, 10, 18)]
    assert rx.find_dates("Hijri 1448/04/27") == []          # Hijri year ignored


def test_no_invoice_rejected():
    r = rx.evaluate(GOOD.replace("Invoice No: INV-2026-004512", "Clinic: Internal"), PRODUCTS, TODAY)
    assert "no_invoice" in r["problems"]


def test_date_is_not_taken_as_invoice_number():
    assert rx.find_invoice_number("Prescription date: 17/10/2026") is None


def test_ocr_typo_still_matches():
    r = rx.match_medications("1. Augrnentin 1g  1 as directed", PRODUCTS)
    assert [m["product_id"] for m in r] == [36]


def test_scientific_first_word_does_not_unlock_other_products():
    # "Amoxicillin" alone is Amoxil's scientific name, not Augmentin's (amoxicillin+clavulanic)
    ids = [m["product_id"] for m in rx.match_medications("Amoxicillin 500mg caps", PRODUCTS)]
    assert 36 not in ids and 37 in ids


def test_generic_word_alone_matches_nothing():
    assert rx.match_medications("vitamin d3 drops", PRODUCTS) == []


def test_no_medicine_rejected():
    r = rx.evaluate(GOOD.replace("Augmentin 1g", "Something Else"), PRODUCTS, TODAY)
    assert "no_medication" in r["problems"]


# ---- gateway policy against live Odoo -------------------------------------
def _odoo():
    try:
        from gateway import Gateway
        g = Gateway()
        g.c.search("pharmacy.prescription", [], limit=1)
        return g
    except Exception:
        return None


GW = _odoo()
odoo = pytest.mark.skipif(GW is None, reason="Odoo not reachable / module not upgraded")


@pytest.fixture()
def ctx():
    c = GW.c
    # dedicated customer, so prescriptions sent while trying the chat page never interfere
    ids = c.search("res.partner", [["ref", "=", "CUST-RXTEST"]])
    cust = ids[0] if ids else c.create("res.partner", {"name": "RX Test Customer",
                                                       "ref": "CUST-RXTEST"})
    left = c.search("pharmacy.prescription", [["partner_id", "=", cust]])
    if left:
        c.write("pharmacy.prescription", left, {"state": "rejected"})
    var = lambda code: c.search_read("product.template", [["default_code", "=", code]],  # noqa: E731
                                     ["product_variant_id"])[0]["product_variant_id"][0]
    # test orders use up stock: top the first branch up to 20 of each product the tests order
    b0 = GW.branches()[0]
    for code in ("RX-0001", "RX-0003"):
        vid = var(code)
        if GW._avail_at(vid, b0["loc_id"]) < 5:
            q = c.create("stock.quant", {"product_id": vid, "location_id": b0["loc_id"],
                                         "inventory_quantity": 20})
            c.execute_kw("stock.quant", "action_apply_inventory", [[q]])
    branch = b0["branch_id"]
    made = []

    def make(products, days_ago=0, state="auto_verified", invoice="TEST-INV-0001"):
        rid = c.create("pharmacy.prescription", {
            "name": "TEST-RX", "partner_id": cust, "state": state, "invoice_number": invoice,
            "prescription_date": (rx.today_ksa() - timedelta(days=days_ago)).isoformat(),
            "matched_product_ids": [(6, 0, products)]})
        made.append(rid)
        return rid

    yield {"cust": cust, "var": var, "branch": branch, "make": make}
    if made:
        c.execute_kw("pharmacy.prescription", "unlink", [made])


def _order(ctx, pid):
    return GW.create_order(ctx["cust"], [{"product_id": pid, "qty": 1}], "pickup",
                           branch_id=ctx["branch"])


@odoo
def test_order_with_verified_prescription_then_consumed(ctx):
    aug = ctx["var"]("RX-0001")
    rid = ctx["make"]([aug])
    r = _order(ctx, aug)
    assert "order_id" in r, r
    p = GW.c.read("pharmacy.prescription", rid, ["state", "sale_order_id"])[0]
    assert p["state"] == "used" and p["sale_order_id"][0] == r["order_id"]
    assert _order(ctx, aug)["error"]["code"] == "prescription_required"   # one use only


@odoo
def test_old_prescription_does_not_unlock(ctx):
    aug = ctx["var"]("RX-0001")
    ctx["make"]([aug], days_ago=rx.PRESCRIPTION_MAX_AGE_DAYS + 1)
    assert _order(ctx, aug)["error"]["code"] == "prescription_required"


@odoo
def test_prescription_only_unlocks_listed_medicine(ctx):
    ctx["make"]([ctx["var"]("RX-0001")])
    assert _order(ctx, ctx["var"]("RX-0003"))["error"]["code"] == "prescription_required"


@odoo
def test_unverified_prescription_does_not_unlock(ctx):
    aug = ctx["var"]("RX-0001")
    ctx["make"]([aug], state="submitted")
    assert _order(ctx, aug)["error"]["code"] == "prescription_required"


@odoo
def test_scheduled_stays_blocked_even_with_prescription(ctx):
    tram = ctx["var"]("RX-0005")
    ctx["make"]([tram])
    assert _order(ctx, tram)["error"]["code"] == "scheduled_item"


@odoo
def test_hold_with_verified_prescription(ctx):
    ven = ctx["var"]("RX-0003")
    ctx["make"]([ven])
    b = ctx["branch"]
    r = GW.hold_item(ven, b, 1, ctx["cust"])
    assert "hold_id" in r, r


# ---- full OCR on a generated image ---------------------------------------
@odoo
@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_submit_generated_prescription_photo(ctx):
    from make_prescription import make_prescription_png
    inv = f"INV-TEST-{time.time_ns() % 10**10}"
    res = GW.submit_prescription(ctx["cust"], make_prescription_png(["Augmentin 1g"], 0, inv))
    try:
        assert res["ok"], res
        assert res["invoice_number"] == inv
        assert ctx["var"]("RX-0001") in [m["product_id"] for m in res["medications"]]
        old = GW.submit_prescription(ctx["cust"], make_prescription_png(["Augmentin 1g"], 9, inv + "9"))
        assert "date_too_old" in old["problems"]
    finally:
        ids = [i for i in (res.get("prescription_id"), locals().get("old", {}).get("prescription_id")) if i]
        GW.c.execute_kw("pharmacy.prescription", "unlink", [ids])


# ---- PDFs ---------------------------------------------------------------------
def test_is_pdf():
    from make_prescription import make_prescription_pdf
    assert rx.is_pdf(make_prescription_pdf())
    assert not rx.is_pdf(b"\x89PNG\r\n\x1a\n")


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler-utils not installed")
def test_read_text_pdf_uses_text_layer():
    from make_prescription import make_prescription_pdf
    doc = rx.read_document(make_prescription_pdf(["Augmentin 1g"], 0, "INV-2026-123456"))
    assert doc["kind"] == "pdf" and "text layer" in doc["engine"]
    assert doc["preview"] and doc["preview"][:4] == b"\x89PNG"
    r = rx.evaluate(doc["text"], PRODUCTS, rx.today_ksa())
    assert r["ok"], r["problems"]
    assert r["invoice_number"] == "INV-2026-123456"


@odoo
@pytest.mark.skipif(not (shutil.which("pdftotext") and shutil.which("tesseract")),
                    reason="poppler-utils / tesseract not installed")
def test_submit_scanned_pdf_is_ocrd_and_stored(ctx):
    from make_prescription import make_prescription_pdf
    inv = f"INV-PDF-{time.time_ns() % 10**10}"
    res = GW.submit_prescription(ctx["cust"], make_prescription_pdf(["Augmentin 1g"], 0, inv,
                                                                     scanned=True), "rx.pdf")
    try:
        assert res["ok"], res
        p = GW.c.read("pharmacy.prescription", res["prescription_id"],
                      ["ocr_engine", "document_filename", "image_filename", "state"])[0]
        assert "scanned pdf" in p["ocr_engine"] and p["state"] == "auto_verified"
        assert p["document_filename"] == "rx.pdf" and p["image_filename"] == "page1.png"
    finally:
        GW.c.execute_kw("pharmacy.prescription", "unlink", [[res["prescription_id"]]])
