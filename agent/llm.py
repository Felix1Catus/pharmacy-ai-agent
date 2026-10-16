"""LLM client for the agent — Qwen3-4B-Instruct-2507 served by Ollama (CPU, non-thinking).

Thin wrapper over Ollama /api/chat. Returns the assistant message with any tool calls so the
agent loop can execute them. Model + endpoint come from .env (AGENT_MODEL, OLLAMA_URL).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
from odoo_client import load_env  # reuse the tiny .env loader  # noqa: E402

load_env()
MODEL = os.environ.get("AGENT_MODEL", "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


class LLMClient:
    def __init__(self, model: str | None = None, url: str | None = None,
                 temperature: float = 0.0, num_ctx: int = 4096, num_predict: int = 512):
        self.model = model or MODEL
        self.url = (url or OLLAMA).rstrip("/")
        self.options = {"temperature": temperature, "num_ctx": num_ctx,
                        "num_predict": num_predict}

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             timeout: int = 180) -> dict:
        """Return {"content": str, "tool_calls": [{"name","arguments"}]}."""
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "think": False, "options": self.options}
        if tools:
            payload["tools"] = tools
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
