# -*- coding: utf-8 -*-
"""Test-run hygiene: the tests talk to the real (demo) Odoo, so everything a run creates -
quotations/holds, prescriptions and "ZZ Test ..." contacts - is removed when the run ends.
Otherwise test quotations show up in Pharmacy > WhatsApp orders next to real chat orders."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "odoo"))

TEST_PREFIX = "ZZ Test"


def _max_id(c, model):
    ids = c.search(model, [], order="id desc", limit=1)
    return ids[0] if ids else 0


@pytest.fixture(scope="session", autouse=True)
def _remove_test_records():
    try:
        from odoo_client import OdooClient, load_env
        load_env()
        c = OdooClient()
        start = {m: _max_id(c, m) for m in ("sale.order", "pharmacy.prescription")}
    except Exception:                    # Odoo down: those tests skip anyway
        yield
        return
    yield
    orders = c.search("sale.order", [["id", ">", start["sale.order"]]])
    test_partners = c.search("res.partner", [["name", "=like", TEST_PREFIX + "%"]])
    orders = sorted(set(orders) | set(c.search("sale.order", [["partner_id", "in", test_partners]])))
    for oid in orders:                   # one at a time: Odoo 18 cancels a single order per call
        try:
            state = c.read("sale.order", oid, ["state"])[0]["state"]
            if state not in ("draft", "sent", "cancel"):   # confirmed: Odoo cancels via its wizard
                w = c.create("sale.order.cancel", {"order_id": oid})
                c.execute_kw("sale.order.cancel", "action_cancel", [[w]])
            c.execute_kw("sale.order", "unlink", [[oid]])
        except Exception:
            pass
    rx = c.search("pharmacy.prescription", ["|", ["id", ">", start["pharmacy.prescription"]],
                                            ["partner_id", "in", test_partners]])
    if rx:
        c.execute_kw("pharmacy.prescription", "unlink", [rx])
    for pid in test_partners:
        try:
            c.execute_kw("res.partner", "unlink", [[pid]])
        except Exception:                # still referenced somewhere: archive instead
            c.write("res.partner", [pid], {"active": False})
