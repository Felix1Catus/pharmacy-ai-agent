# -*- coding: utf-8 -*-
"""Quick check: does a given Ollama model actually emit tool calls with our contract?

Usage: python evals/runners/toolcheck.py <model-tag>
Sends a few unambiguous prompts with the 7 contract tools and reports, per prompt, whether the
model returned a tool_call (name) or just text. If it never emits tool_calls, it cannot drive
our agent regardless of language quality.
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import contract  # noqa: E402

OLLAMA = "http://localhost:11434"
TOOLS = contract.tools_for_llm()
SYS = ("You are a pharmacy ordering assistant. Call exactly one of the provided tools for the "
       "customer's message. To find a product call search_product; for order status call "
       "track_order; for dosage/medical questions call escalate_to_human reason=medical_advice.")

PROMPTS = [
    ("search a product (en)", "Do you have panadol?"),
    ("search a product (ar)", "عندكم بنادول؟"),
    ("order status (ar)", "وين وصل طلبي رقم 3؟"),
    ("medical -> escalate (ar)", "كم حبة بنادول اعطي طفلي؟"),
]


def probe(model: str):
    print(f"=== tool-calling probe: {model} ===", flush=True)
    emitted = 0
    for label, text in PROMPTS:
        payload = {"model": model,
                   "messages": [{"role": "system", "content": SYS},
                                {"role": "user", "content": text}],
                   "tools": TOOLS, "stream": False, "think": False,
                   "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 300}}
        try:
            r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=120)
            r.raise_for_status()
            msg = r.json().get("message", {})
        except Exception as e:  # noqa: BLE001
            print(f"  [{label}] ERROR: {e}", flush=True)
            return
        calls = msg.get("tool_calls") or []
        if calls:
            emitted += 1
            fn = calls[0]["function"]
            print(f"  [{label}] TOOL -> {fn['name']} {fn.get('arguments')}", flush=True)
        else:
            snippet = (msg.get("content", "") or "").replace("\n", " ")[:80]
            print(f"  [{label}] no tool call; text: {snippet}", flush=True)
    print(f"=> emitted tool calls on {emitted}/{len(PROMPTS)} prompts", flush=True)
    print("   VERDICT: " + ("supports tool calling" if emitted >= 2 else
                            "does NOT reliably tool-call -> unusable as our agent LLM"), flush=True)


if __name__ == "__main__":
    probe(sys.argv[1] if len(sys.argv) > 1 else
          "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M")
