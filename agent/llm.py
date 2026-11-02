"""LLM client for the agent — Ollama /api/chat (default) or llama-server's OpenAI-compatible API.

Returns the assistant message with any tool calls so the agent loop can execute them. Model +
endpoint come from .env: AGENT_MODEL, OLLAMA_URL, and LLM_API=ollama|openai.
LLM_API=openai talks to llama-server (/v1/chat/completions), which unlike Ollama can split one model
across GPUs with tensor parallelism: Qwen3-8B on the 2x A16 box answers in ~0.7 s (p50) vs ~2.9 s
on Ollama. llama-server renders the tools as real JSON schemas (Ollama 0.40.13 printed them as a Go
struct dump), so re-run the agent eval when switching.
"""
from __future__ import annotations

import json
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
API = os.environ.get("LLM_API", "ollama").lower()          # ollama | openai (llama-server)


def _to_openai(messages: list[dict]) -> list[dict]:
    """The agent keeps history in Ollama's shape; llama-server needs OpenAI's: each assistant tool
    call with an id, type "function" and JSON-string arguments, and each tool result pointing back
    to its call id (otherwise the follow-up call after a tool result fails with HTTP 500)."""
    out, pending = [], []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                cid = tc.get("id") or f"call_{len(out)}_{len(calls)}"
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name"),
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)}})
                pending.append(cid)
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m.get("role") == "tool":
            out.append({"role": "tool", "content": m.get("content", ""),
                        "tool_call_id": m.get("tool_call_id") or (pending.pop(0) if pending else "call_0")})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


class LLMClient:
    def __init__(self, model: str | None = None, url: str | None = None,
                 temperature: float = 0.0, num_ctx: int = 4096, num_predict: int = 300,
                 api: str | None = None):
        self.model = model or MODEL
        self.url = (url or OLLAMA).rstrip("/")
        self.api = (api or API).lower()
        self.options = {"temperature": temperature, "num_ctx": num_ctx,
                        "num_predict": num_predict, "num_thread": NUM_THREAD}

    def warm_up(self) -> None:
        """Load the model into RAM now (and keep it there) so the first customer doesn't wait."""
        if self.api == "openai":                 # llama-server loads the model at start
            return
        requests.post(f"{self.url}/api/generate", timeout=300,
                      json={"model": self.model, "keep_alive": int(KEEP_ALIVE)
                            if KEEP_ALIVE.lstrip("-").isdigit() else KEEP_ALIVE})

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             timeout: int = 300) -> dict:
        """Return {"content": str, "tool_calls": [{"name","arguments"}]}."""
        if self.api == "openai":
            # thinking off = the empty <think></think> block Ollama's think=False adds
            payload = {"model": self.model, "messages": _to_openai(messages), "stream": False,
                       "temperature": self.options["temperature"],
                       "max_tokens": self.options["num_predict"],
                       "chat_template_kwargs": {"enable_thinking": False}}
            if tools:
                payload["tools"] = tools
            with _MODEL_LOCK:
                r = requests.post(f"{self.url}/v1/chat/completions", json=payload, timeout=timeout)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        else:
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
            if self.api == "openai":
                return requests.get(f"{self.url}/health", timeout=10).json().get("status") == "ok"
            tags = requests.get(f"{self.url}/api/tags", timeout=10).json()
            names = [m["name"] for m in tags.get("models", [])]
            return self.model in names
        except Exception:  # noqa: BLE001
            return False
