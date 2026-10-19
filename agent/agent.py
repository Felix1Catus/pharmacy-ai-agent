"""Pharmacy ordering agent — conversation state + tool-calling loop + guardrails (T-B1/T-B2).

Wires Qwen3-4B-Instruct-2507 (via agent/llm.py) to the contract tools, executed through the
adapter Gateway (adapter/gateway.py) over the real Odoo 18 instance. The model only ever sees
the 7-action contract; it never touches Odoo directly.

Guardrails:
- prescription-only items can be ordered/held only with an auto-verified prescription photo of
  this customer (adapter/prescription.py: dated within 5 days + invoice number + the medicine on
  it); otherwise the Gateway returns {"error":{"code":"prescription_required"}} and the agent asks
  for the photo. Scheduled/controlled items are never ordered/held (scheduled_item -> escalate).
- medical/dosage questions must escalate (system prompt + a terminal escalate action).
- a max number of tool calls per turn (prevents loops); on overrun the agent escalates
  (reason=low_confidence) rather than guessing.
- the customer_id is injected by the agent, never taken from the model.
- every tool call + reply is written to an append-only audit log (results/audit.jsonl).

Confirmation-before-order is currently prompt-driven (the system prompt tells the model to
confirm). Tracking confirmation state programmatically is a flagged next iteration.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapter"))
sys.path.insert(0, str(ROOT / "agent"))

import contract  # noqa: E402
from gateway import Gateway, dispatch  # noqa: E402
from gateway import err  # noqa: E402
from guards import (ScheduledDenylist, medical_advice_intent,  # noqa: E402
                    prescription_rejected, MEDICAL_REFUSAL, PRESCRIPTION_HANDOVER,
                    PRESCRIPTION_NEEDED, SCHEDULED_REFUSAL)
from llm import LLMClient  # noqa: E402

AUDIT = ROOT / "results" / "audit.jsonl"
MAX_TOOL_CALLS = 6

SYSTEM_PROMPT = (
    "You are the ordering assistant for a pharmacy franchise on WhatsApp. Customers write in "
    "Arabic dialects (Gulf, Egyptian, Levantine, MSA) or English, sometimes mixed. Reply in the "
    "customer's own language and dialect, briefly and politely.\n\n"
    "You act by calling the provided tools. Prefer ACTING (a tool call) over asking questions. "
    "Rules:\n"
    "1. The moment the customer names or asks about ANY product, call search_product right away "
    "(Arabic/brand/ingredient terms) — do NOT ask 'which product?' or answer from memory first. "
    "Call search_product, then show the matches and let them pick if several match.\n"
    "2. For availability call check_stock; for order status ('where is my order', 'وين طلبي', "
    "'فين الاوردر') call track_order IMMEDIATELY — if the customer gave an order number use it, "
    "otherwise call track_order to look up their recent orders; do NOT ask for an order number "
    "first. When the customer names a branch ('جدة', 'Riyadh'), call list_branches to get its "
    "branch_id — never invent an id or assume the branch is missing.\n"
    "3. Before create_order, CONFIRM the exact items, quantities and pickup/delivery with the "
    "customer in words; only call create_order after they confirm. Use hold_item to reserve.\n"
    "4. NEVER give dosage, diagnosis, drug-interaction or any medical advice, not even to decline "
    "with advice. For ANY such question you MUST call escalate_to_human with "
    "reason='medical_advice' — do not answer in plain text.\n"
    "4b. A search candidate with match='spelling variant' IS the product the customer meant, "
    "written differently or in another language (e.g. 'سرفاركس' = CERVARIX VACCINE): confirm it "
    "('تقصد CERVARIX VACCINE؟') and carry on - never call it a different or 'similar' product. "
    "Never say a product is unavailable unless check_stock returned zero.\n"
    "5. Prescription-only medicines (search result requires_prescription=true, not scheduled): "
    "you MAY confirm availability (check_stock) and price (price_sar from search_product). Each "
    "such result has prescription_on_file. If prescription_on_file=true, handle it like any other "
    "medicine (confirm, then create_order / hold_item). If it is false and the customer wants to "
    "buy, order or reserve it, do NOT call create_order or escalate: reply asking them to attach "
    "a photo of their prescription with the 📎 button (in Arabic say: 'يحتاج وصفة طبية، أرسل "
    "صورة الوصفة من زر المرفقات 📎 وأكمل لك الطلب') - never mix English words into Arabic "
    "replies. A message starting with [Prescription verified] lists the medicines the customer "
    "may now order. Scheduled/controlled medicines (is_scheduled=true) are fully "
    "blocked: give no availability or price, call escalate_to_human reason='restricted_item'.\n"
    "6. If the message is vague/unintelligible or you cannot map it to a product, order or "
    "action, call escalate_to_human reason='unclear' — do NOT reply with a free-text question. "
    "If the message is insulting or abusive, call escalate_to_human reason='abusive'. Never try "
    "to handle unclear or abusive messages with plain text.\n"
    "7. Reply with plain text (no tool call) ONLY to present results you already fetched or to "
    "ask a short clarifying question after you have already searched."
)

# tools that operate on behalf of a specific customer — agent injects the id.
_NEEDS_CUSTOMER = {"create_order", "hold_item"}


class Conversation:
    def __init__(self, customer_id: int, conv_id: str | None = None):
        self.customer_id = customer_id
        self.conv_id = conv_id or f"conv-{int(time.time())}"
        self.messages: list[dict] = []


class PharmacyAgent:
    def __init__(self, llm: LLMClient | None = None, gateway: Gateway | None = None):
        self.llm = llm or LLMClient()
        self.gw = gateway or Gateway()
        self.tools = contract.tools_for_llm()
        # built from Odoo's is_scheduled flag (official scheduling) at startup
        self.denylist = ScheduledDenylist.from_odoo(self.gw.c)

    def _escalate(self, conv, reason, summary):
        esc = dispatch(self.gw, "escalate_to_human",
                       {"reason": reason, "customer_id": conv.customer_id, "summary": summary})
        self._audit(conv.conv_id, "guard_escalate", {"reason": reason, "result": esc})
        return esc

    def _audit(self, conv_id, kind, payload):
        AUDIT.parent.mkdir(exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "conv": conv_id,
               "kind": kind, **payload}
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _fill_defaults(self, name, args, conv):
        args = dict(args or {})
        # always THIS conversation's customer: the model sometimes invents an id, which would
        # act on another customer's account (and miss their prescriptions)
        if name in _NEEDS_CUSTOMER or name == "escalate_to_human":
            args["customer_id"] = conv.customer_id
        # track_order: "where is my order" with no order number -> look up THIS customer
        if name == "track_order" and not args.get("order_id") and not args.get("customer_id"):
            args["customer_id"] = conv.customer_id
        return args

    def _prescription_note(self, res: dict) -> str:
        lines = [f"[Prescription verified] The customer attached a prescription photo; it was "
                 f"verified automatically (invoice {res['invoice_number']}, dated {res['date']}). "
                 f"Medicines found on it:"]
        for m in res["medications"]:
            what = ("SCHEDULED - blocked, cannot be ordered" if m["is_scheduled"] else
                    "prescription-only - may now be ordered/held" if m["requires_prescription"]
                    else "over the counter")
            lines.append(f"- {m['name_en']} / {m['name_ar']} (product_id {m['product_id']}): {what}")
        lines.append("Tell the customer which medicines you found and help them order: check "
                     "stock, ask pickup branch or delivery address, confirm, then create_order.")
        return "\n".join(lines)

    def _annotate_prescriptions(self, conv, result):
        """Tell the model, per prescription-only search result, whether THIS customer has a
        valid prescription for it (the gateway enforces the same rule on create_order)."""
        for c in result.get("candidates") or []:
            if c.get("requires_prescription") and not c.get("is_scheduled"):
                c["prescription_on_file"] = bool(
                    self.gw.valid_prescription(conv.customer_id, c["product_id"]))

    def respond(self, conv: Conversation, user_text: str, image: bytes | None = None,
                image_name: str | None = None) -> str:
        user_text = user_text or ""
        conv.messages.append({"role": "user", "content": user_text})
        self._audit(conv.conv_id, "user", {"text": user_text, "image": bool(image)})

        # --- deterministic guards BEFORE the LLM (do not depend on the model) ---
        if medical_advice_intent(user_text):
            self._escalate(conv, "medical_advice", f"deterministic medical guard: {user_text}")
            conv.messages.append({"role": "assistant", "content": MEDICAL_REFUSAL})
            self._audit(conv.conv_id, "reply", {"text": MEDICAL_REFUSAL, "guard": "medical"})
            return MEDICAL_REFUSAL
        hit = self.denylist.match(user_text)
        if hit:
            self._escalate(conv, "restricted_item",
                           f"deterministic scheduled-med guard matched {hit!r}: {user_text}")
            conv.messages.append({"role": "assistant", "content": SCHEDULED_REFUSAL})
            self._audit(conv.conv_id, "reply", {"text": SCHEDULED_REFUSAL,
                                                "guard": "scheduled", "matched": hit})
            return SCHEDULED_REFUSAL

        # --- attached prescription photo: OCR + deterministic checks (not the model) ---
        if image:
            try:
                res = self.gw.submit_prescription(conv.customer_id, image, image_name)
            except Exception as e:                   # OCR missing/failed: treat as unreadable
                res = {"ok": False, "problems": ["unreadable"], "date": None, "error": str(e)}
            self._audit(conv.conv_id, "prescription", res)
            if not res["ok"]:
                reply = prescription_rejected(res["problems"], res.get("date"))
                conv.messages.append({"role": "assistant", "content": reply})
                self._audit(conv.conv_id, "reply", {"text": reply, "guard": "prescription_check"})
                return reply
            note = self._prescription_note(res)
            conv.messages[-1]["content"] = note + ("\n\nCustomer message: " + user_text
                                                   if user_text.strip() else "")

        calls_made = 0

        while calls_made <= MAX_TOOL_CALLS:
            out = self.llm.chat([{"role": "system", "content": SYSTEM_PROMPT}] + conv.messages,
                                tools=self.tools)
            if not out["tool_calls"]:
                reply = out["content"].strip() or "عفواً، ممكن توضح طلبك؟ (Could you clarify?)"
                conv.messages.append({"role": "assistant", "content": reply})
                self._audit(conv.conv_id, "reply", {"text": reply})
                return reply

            tc = out["tool_calls"][0]
            name, args = tc["name"], self._fill_defaults(tc["name"], tc["arguments"], conv)
            conv.messages.append({"role": "assistant", "content": out["content"],
                                  "tool_calls": [{"function": {"name": name,
                                                               "arguments": args}}]})
            result = (dispatch(self.gw, name, args) if name in contract.ACTIONS
                      else err("invalid_input", f"unknown action {name!r}"))
            if name == "search_product" and isinstance(result, dict):
                self._annotate_prescriptions(conv, result)
            calls_made += 1
            self._audit(conv.conv_id, "tool_call",
                        {"action": name, "args": args, "result": result})
            conv.messages.append({"role": "tool", "tool_name": name,
                                  "content": json.dumps(result, ensure_ascii=False)})

            # deterministic: a scheduled/restricted error ends the turn with a handover,
            # regardless of what the model would do next.
            if isinstance(result, dict) and "error" in result:
                code = result["error"].get("code")
                if code == "scheduled_item":
                    self._escalate(conv, "restricted_item", f"tool returned scheduled_item: {name}")
                    conv.messages.append({"role": "assistant", "content": SCHEDULED_REFUSAL})
                    self._audit(conv.conv_id, "reply", {"text": SCHEDULED_REFUSAL,
                                                        "guard": "scheduled_tool"})
                    return SCHEDULED_REFUSAL
                if code == "prescription_required":  # no valid prescription: ask for the photo
                    conv.messages.append({"role": "assistant", "content": PRESCRIPTION_NEEDED})
                    self._audit(conv.conv_id, "reply", {"text": PRESCRIPTION_NEEDED,
                                                        "guard": "prescription_tool"})
                    return PRESCRIPTION_NEEDED
                if code == "restricted_item":       # legacy code: purchase via pharmacist
                    self._escalate(conv, "restricted_item", f"tool returned restricted_item: {name}")
                    conv.messages.append({"role": "assistant", "content": PRESCRIPTION_HANDOVER})
                    self._audit(conv.conv_id, "reply", {"text": PRESCRIPTION_HANDOVER,
                                                        "guard": "prescription_tool"})
                    return PRESCRIPTION_HANDOVER

            # deterministic: a search whose results are ALL Scheduled medicines (e.g. a misspelt
            # controlled-drug name the pre-check list missed) is refused, not discussed.
            if name == "search_product" and isinstance(result, dict):
                cands = result.get("candidates") or []
                if cands and all(c.get("is_scheduled") for c in cands):
                    self._escalate(conv, "restricted_item", "search matched only scheduled medicines")
                    conv.messages.append({"role": "assistant", "content": SCHEDULED_REFUSAL})
                    self._audit(conv.conv_id, "reply", {"text": SCHEDULED_REFUSAL,
                                                        "guard": "scheduled_search"})
                    return SCHEDULED_REFUSAL

            # terminal: a completed handover ends the turn with its acknowledgement
            if name == "escalate_to_human" and isinstance(result, dict) and "handover_id" in result:
                reply = result.get("acknowledgement", "تم تحويلك إلى موظف.")
                conv.messages.append({"role": "assistant", "content": reply})
                self._audit(conv.conv_id, "reply", {"text": reply, "escalated": True})
                return reply
            # otherwise loop so the model can read the tool result and continue

        # too many tool calls without resolving -> escalate rather than guess
        esc = dispatch(self.gw, "escalate_to_human",
                       {"reason": "low_confidence", "customer_id": conv.customer_id,
                        "summary": f"agent exceeded {MAX_TOOL_CALLS} tool calls without a reply"})
        reply = esc.get("acknowledgement", "تم تحويلك إلى موظف.") if isinstance(esc, dict) else \
            "تم تحويلك إلى موظف."
        conv.messages.append({"role": "assistant", "content": reply})
        self._audit(conv.conv_id, "reply", {"text": reply, "escalated": True,
                                            "reason": "max_tool_calls"})
        return reply
