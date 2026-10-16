"""Pharmacy ordering agent — conversation state + tool-calling loop + guardrails (T-B1/T-B2).

Wires Qwen3-4B-Instruct-2507 (via agent/llm.py) to the contract tools, executed through the
adapter Gateway (adapter/gateway.py) over the real Odoo 18 instance. The model only ever sees
the 7-action contract; it never touches Odoo directly.

Guardrails:
- restricted/prescription items can never be ordered/held — the Gateway refuses and returns
  {"error":{"code":"restricted_item"}}, and the loop then escalates.
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
from guards import (ScheduledDenylist, medical_advice_intent,  # noqa: E402
                    MEDICAL_REFUSAL, SCHEDULED_REFUSAL)
from llm import LLMClient  # noqa: E402

AUDIT = ROOT / "results" / "audit.jsonl"
MAX_TOOL_CALLS = 6

SYSTEM_PROMPT = (
    "You are the ordering assistant for a pharmacy franchise on WhatsApp. Customers write in "
    "Arabic dialects (Gulf, Egyptian, Levantine, MSA) or English, sometimes mixed. Reply in the "
    "customer's own language and dialect, briefly and politely.\n\n"
    "You act by calling the provided tools. Rules:\n"
    "1. Resolve any product by calling search_product first (Arabic/brand/ingredient names). "
    "If several products match, ask the customer which one; never guess.\n"
    "2. Use check_stock for availability, track_order for order status. When the customer names "
    "a branch (e.g. 'جدة', 'Riyadh'), call list_branches to get its branch_id — never invent an "
    "id or assume the branch is missing.\n"
    "3. Before create_order, CONFIRM the exact items, quantities and pickup/delivery with the "
    "customer in words; only call create_order after they confirm. Use hold_item to reserve.\n"
    "4. NEVER give dosage, diagnosis, drug-interaction or any medical advice, not even to decline "
    "with advice. For ANY such question you MUST call escalate_to_human with "
    "reason='medical_advice' — do not answer in plain text.\n"
    "5. Restricted or prescription medicines (antibiotics, controlled drugs, insulin, inhalers) "
    "are NEVER ordered or held. If a tool returns a restricted_item error, or the customer asks "
    "for such a medicine, call escalate_to_human with reason='restricted_item'.\n"
    "6. If the request is unclear call escalate_to_human reason='unclear'; if abusive, "
    "reason='abusive'.\n"
    "7. When you have the answer for the customer, reply with plain text (no tool call)."
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
        if name in _NEEDS_CUSTOMER and "customer_id" not in args:
            args["customer_id"] = conv.customer_id
        if name == "escalate_to_human" and "customer_id" not in args:
            args["customer_id"] = conv.customer_id
        return args

    def respond(self, conv: Conversation, user_text: str) -> str:
        conv.messages.append({"role": "user", "content": user_text})
        self._audit(conv.conv_id, "user", {"text": user_text})

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
            result = dispatch(self.gw, name, args)
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
                if code == "restricted_item":
                    self._escalate(conv, "restricted_item", f"tool returned restricted_item: {name}")
                    conv.messages.append({"role": "assistant", "content": SCHEDULED_REFUSAL})
                    self._audit(conv.conv_id, "reply", {"text": SCHEDULED_REFUSAL,
                                                        "guard": "restricted_tool"})
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
