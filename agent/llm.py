"""LLM client for the agent — Qwen3-4B-Instruct-2507 served by Ollama (CPU, non-thinking).

Thin wrapper over Ollama /api/chat. Returns the assistant message with any tool calls so the
agent loop can execute them. Model + endpoint come from .env (AGENT_MODEL, OLLAMA_URL).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import requests

# One model call at a time (CPU box, one Ollama slot). Only the model call waits here, so
# instant replies (menu, guards, tracking) never queue behind another customer's chat.
_MODEL_LOCK = threading.Lock()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
from odoo_client import load_env  # reuse the tiny .env loader  # noqa: E402

load_env()
MODEL = os.environ.get("AGENT_MODEL", "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
# Ollama defaults to one thread per PHYSICAL core; on small hyper-threaded VMs (Vultr 4 vCPU =
# 2 cores) all logical CPUs measured ~1.8x faster (prompt 22->40 tok/s, gen 6->10 tok/s).
# Override with AGENT_NUM_THREAD on big servers if that turns out slower there.
NUM_THREAD = int(os.environ.get("AGENT_NUM_THREAD") or os.cpu_count() or 4)
# keep the model in RAM (default Ollama unloads after 5 idle minutes -> ~90 s first reply)
KEEP_ALIVE = os.environ.get("AGENT_KEEP_ALIVE", "-1")


class LLMClient:
    def __init__(self, model: str | None = None, url: str | None = None,
                 temperature: float = 0.0, num_ctx: int = 4096, num_predict: int = 300):
        self.model = model or MODEL
        self.url = (url or OLLAMA).rstrip("/")
        self.options = {"temperature": temperature, "num_ctx": num_ctx,
                        "num_predict": num_predict, "num_thread": NUM_THREAD}

    def warm_up(self) -> None:
        """Load the model into RAM now (and keep it there) so the first customer doesn't wait."""
        requests.post(f"{self.url}/api/generate", timeout=300,
                      json={"model": self.model, "keep_alive": int(KEEP_ALIVE)
                            if KEEP_ALIVE.lstrip("-").isdigit() else KEEP_ALIVE})

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             timeout: int = 300) -> dict:
        """Return {"content": str, "tool_calls": [{"name","arguments"}]}."""
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "think": False, "options": self.options,
                   "keep_alive": int(KEEP_ALIVE) if KEEP_ALIVE.lstrip("-").isdigit() else KEEP_ALIVE}
        if tools:
            payload["tools"] = tools
        with _MODEL_LOCK:
            r = requests.post(f"{self.url}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
        msg = r.json().get("message", {})
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                import json
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001
                    args = {}
            calls.append({"name": fn.get("name"), "arguments": args or {}})
        return {"content": msg.get("content", "") or "", "tool_calls": calls}

    def health(self) -> bool:
        try:
            tags = requests.get(f"{self.url}/api/tags", timeout=10).json()
            names = [m["name"] for m in tags.get("models", [])]
            return self.model in names
        except Exception:  # noqa: BLE001
            return False
