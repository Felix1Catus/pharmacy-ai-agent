# -*- coding: utf-8 -*-
"""Fixed cancel wizard: "الغي طلبي" -> which order (buttons) -> its items + ✅ cancel / ↩️ keep ->
cancelled in Odoo. No model calls: the model once invented an order ("1 box Panadol, delivery in
24 hours") and an order number belonging to another customer, then looped on confirmations.

Only the customer's own open quotations (draft / sent, not the chat cart) can be cancelled here;
a confirmed order is handed to a colleague, tied to that order.
"""
from __future__ import annotations

import re

from guards import is_affirmation, normalize

# "cancel" said about an ORDER (the cart has its own "clear cart"; inside the order wizard
# "الغاء" stops the wizard and is handled there)
_CANCEL = re.compile(r"(?<!\w)(الغ\w*|كنسل\w*|cancel\w*|ابطل\w*)(?!\w)", re.I)
_REF = re.compile(r"(?<![a-z\d])s?\s?0*(\d{2,6})(?!\d)", re.I)

T = {
    "ar": {
        "pick": "أي طلب تبغى تلغي؟",
        "pick_row": "❌ {ref} — {total:.2f} ريال",
        "confirm": "تبغى نلغي الطلب {ref}؟\n{lines}\n💰 {total:.2f} ريال",
        "line": "• {name} × {qty:g}",
        "yes": "✅ نعم، ألغِ الطلب", "no": "↩️ لا، خلّه",
        "done": "✅ تم إلغاء طلبك {ref}. إذا احتجت شيئاً ثانياً أنا هنا.",
        "kept": "تمام، ما ألغيت شيئاً. طلبك {ref} باقٍ كما هو.",
        "kept_none": "تمام، ما ألغيت شيئاً.",
        "none": "ما عندك طلب مفتوح يمكن إلغاؤه حالياً.",
        "confirmed": "طلبك {ref} مؤكد، فإلغاؤه يحتاج زميلنا. حوّلته له مع طلبك وبيتواصل معك قريباً.",
        "not_yours": "ما لقيت الطلب {ref} على حسابك. هذي طلباتك المفتوحة:",
    },
    "en": {
        "pick": "Which order would you like to cancel?",
        "pick_row": "❌ {ref} — {total:.2f} SAR",
        "confirm": "Cancel order {ref}?\n{lines}\n💰 {total:.2f} SAR",
        "line": "• {name} × {qty:g}",
        "yes": "✅ Yes, cancel it", "no": "↩️ No, keep it",
        "done": "✅ Your order {ref} is cancelled. Anything else, I'm here.",
        "kept": "OK, nothing was cancelled. Your order {ref} stays as it is.",
        "kept_none": "OK, nothing was cancelled.",
        "none": "You have no open order that can be cancelled right now.",
        "confirmed": "Order {ref} is already confirmed, so a colleague has to cancel it. I've passed it "
                     "to them with your order; they'll contact you shortly.",
        "not_yours": "I couldn't find order {ref} on your account. These are your open orders:",
    },
}


def cancel_intent(text: str) -> bool:
    n = normalize(text or "")
    return bool(_CANCEL.search(n)) and not re.search(r"(سله|سلتي|cart|basket)", n)


def _pressed(raw: str, key: str) -> bool:
    n = normalize(raw)
    return any(n == normalize(T[lg][key]) or n == normalize(re.sub(r"^\W+", "", T[lg][key])) for lg in T)


class CancelFlow:
    def __init__(self, agent):
        self.a = agent

    def _t(self, conv):
        return T[conv.lang or "ar"]

    def _open_orders(self, customer_id) -> list[dict]:
        return self.a.gw.c.search_read(
            "sale.order", [["partner_id", "=", customer_id], ["state", "in", ["draft", "sent"]],
                           ["wa_cart", "=", False]],
            ["name", "amount_total"], order="id desc", limit=5)

    def _ref_in(self, raw: str) -> str | None:
        m = _REF.search(normalize(raw))
        return f"S{int(m.group(1)):05d}" if m else None

    @staticmethod
    def _discussed(conv) -> str | None:
        """"ابي الغيه" right after an order was shown: that order (from the last few messages)."""
        for m in reversed(conv.messages[-6:]):
            if m["role"] == "tool" and m.get("tool_name") in ("get_order_details", "track_order"):
                hit = re.search(r'"(?:order_ref|name)":\s*"(S\d{3,6})"', m["content"])
                if hit and m.get("tool_name") == "get_order_details":
                    return hit.group(1)
        return None

    # ---- start: "الغي طلبي" / "cancel S00175" ---------------------------------------------
    def start(self, conv, raw: str):
        t = self._t(conv)
        if not conv.customer_id:                 # an order belongs to a contact: identify first
            return self.a.customer.start(conv, pending={"kind": "text", "text": raw})
        ref = self._ref_in(raw) or self._discussed(conv)
        if ref:
            d = self.a.gw.get_order_details(order_ref=ref, customer_id=conv.customer_id)
            if "error" not in d:
                if d.get("can_cancel"):
                    return self._confirm(conv, d)
                return self._confirmed_handover(conv, d)
            head = t["not_yours"].format(ref=ref) + "\n"
        else:
            head = ""
        orders = self._open_orders(conv.customer_id)
        if not orders:
            conv.cancel_ctx = None
            return self.a._say(conv, head + t["none"], guard="cancel")
        if len(orders) == 1 and not head:
            d = self.a.gw.get_order_details(order_id=orders[0]["id"], customer_id=conv.customer_id)
            return self._confirm(conv, d)
        conv.cancel_ctx = {"step": "pick", "orders": {o["name"]: o["id"] for o in orders}}
        buttons = [{"id": f"cancel:{o['name']}", "label": t["pick_row"].format(ref=o["name"], total=o["amount_total"])}
                   for o in orders]
        return self.a._say(conv, head + t["pick"] + "\n" + "\n".join(b["label"] for b in buttons),
                           guard="cancel", buttons=buttons + [{"id": "no", "label": t["no"]}])

    def _confirm(self, conv, d):
        t = self._t(conv)
        conv.cancel_ctx = {"step": "confirm", "order_id": d["order_id"], "ref": d["order_ref"]}
        lines = "\n".join(t["line"].format(name=i["name"], qty=i["qty"]) for i in d.get("items") or [])
        return self.a._say(conv, t["confirm"].format(ref=d["order_ref"], lines=lines, total=d["total_sar"]),
                           guard="cancel", buttons=[{"id": "yes", "label": t["yes"]},
                                                    {"id": "no", "label": t["no"]}])

    def _confirmed_handover(self, conv, d):
        conv.cancel_ctx = None
        self.a._handover(conv, "complaint", f"customer asks to cancel confirmed order {d['order_ref']}",
                         order_id=d["order_id"])
        return self.a._say(conv, self._t(conv)["confirmed"].format(ref=d["order_ref"]), guard="handover",
                           escalated=True)

    # ---- next step ------------------------------------------------------------------------
    def handle(self, conv, raw: str):
        """The answer to the wizard's question, or None (the agent handles it; wizard dropped)."""
        ctx, t = conv.cancel_ctx, self._t(conv)
        if ctx["step"] == "pick":
            n = normalize(raw)
            if _pressed(raw, "no"):
                conv.cancel_ctx = None
                return self.a._say(conv, t["kept_none"], guard="cancel")
            ref = next((r for r in ctx["orders"] if normalize(r) in n or r.lstrip("S0") in n), None) \
                or self._ref_in(raw)
            if ref in ctx["orders"]:
                d = self.a.gw.get_order_details(order_id=ctx["orders"][ref], customer_id=conv.customer_id)
                return self._confirm(conv, d)
            conv.cancel_ctx = None
            return None
        if ctx["step"] == "confirm":
            if _pressed(raw, "yes") or (is_affirmation(raw) and not _pressed(raw, "no")):
                conv.cancel_ctx = None
                res = self.a._run_tool(conv, "cancel_order", {"order_id": ctx["order_id"],
                                                              "customer_id": conv.customer_id,
                                                              "reason": "customer cancelled in chat"})
                if isinstance(res, dict) and res.get("cancelled"):
                    return self.a._say(conv, t["done"].format(ref=ctx["ref"]), guard="cancel")
                d = self.a.gw.get_order_details(order_id=ctx["order_id"], customer_id=conv.customer_id)
                if "error" not in d:
                    return self._confirmed_handover(conv, d)
                return self.a._say(conv, t["none"], guard="cancel")
            if _pressed(raw, "no") or re.search(r"(?<!\w)(لا|لأ|no|nope|خله|خليه|تراجع)(?!\w)", normalize(raw)):
                conv.cancel_ctx = None
                return self.a._say(conv, t["kept"].format(ref=ctx["ref"]), guard="cancel")
            conv.cancel_ctx = None
            return None
        conv.cancel_ctx = None
        return None
