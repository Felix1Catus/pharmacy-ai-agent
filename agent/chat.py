"""Interactive / one-shot driver to test the pharmacy agent and iterate on it.

Needs: Odoo stack up (cd odoo && docker compose up -d), data seeded, Ollama running with the
AGENT_MODEL pulled.

  python agent/chat.py                         # REPL (type messages, Ctrl-D to quit)
  python agent/chat.py "عندكم بنادول؟"          # one-shot
  python agent/chat.py --customer CUST-DEMO-1   # pick the customer by Odoo ref
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapter"))
sys.path.insert(0, str(ROOT / "agent"))

from agent import Conversation, PharmacyAgent  # noqa: E402
from gateway import Gateway  # noqa: E402
from llm import LLMClient  # noqa: E402


def resolve_customer(gw: Gateway, ref: str) -> int:
    ids = gw.c.search("res.partner", [["ref", "=", ref]])
    if not ids:
        sys.exit(f"customer ref {ref!r} not found — run python odoo/seed_data.py")
    return ids[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="*", help="one-shot message; omit for a REPL")
    ap.add_argument("--customer", default="CUST-DEMO-1", help="Odoo partner ref")
    args = ap.parse_args()

    llm = LLMClient()
    if not llm.health():
        sys.exit(f"model {llm.model} not available on {llm.url} — `ollama pull` it / start ollama")
    gw = Gateway()
    agent = PharmacyAgent(llm=llm, gateway=gw)
    conv = Conversation(customer_id=resolve_customer(gw, args.customer))
    print(f"[agent ready | model={llm.model} | customer={args.customer} | conv={conv.conv_id}]")

    if args.message:
        text = " ".join(args.message)
        print(f"> {text}\n{agent.respond(conv, text)}")
        return

    print("Type a message (Ctrl-D to quit).")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            print()
            break
        if not text:
            continue
        print(agent.respond(conv, text))


if __name__ == "__main__":
    main()
