# -*- coding: utf-8 -*-
"""WhatsApp-style test page for the pharmacy agent (for the owner to try conversations).

Serves channels/webchat/index.html and a small JSON API that runs the REAL agent
(agent/agent.py: deterministic guards -> Qwen via Ollama -> tools via the Gateway -> live Odoo),
keeping one conversation per browser session. Each reply also returns debug info: the tool calls
made this turn (with arguments and a short result), whether a safety guard answered, and timing.

Protected by HTTP Basic auth: WEBCHAT_USER / WEBCHAT_PASSWORD from .env (required).
Run:  .venv/bin/uvicorn --app-dir channels/webchat server:app --host 0.0.0.0 --port 8090
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo")]
from odoo_client import load_env  # noqa: E402

load_env()
from agent import Conversation, PharmacyAgent  # noqa: E402
from gateway import Gateway  # noqa: E402
from guards import MEDICAL_REFUSAL, SCHEDULED_REFUSAL  # noqa: E402
from llm import LLMClient  # noqa: E402

USER = os.environ.get("WEBCHAT_USER", "pharmacy")
PASSWORD = os.environ.get("WEBCHAT_PASSWORD", "")
if not PASSWORD:
    sys.exit("WEBCHAT_PASSWORD is not set in .env - refusing to start an unprotected chat page")
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

app = FastAPI(title="Pharmacy agent - test chat", docs_url=None, redoc_url=None)
security = HTTPBasic()
gw = Gateway()
CUSTOMER = gw.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0]
_agents: dict[str, PharmacyAgent] = {}
_sessions: dict[str, dict] = {}
_lock = threading.Lock()          # one CPU: serve one model call at a time


def auth(c: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(c.username.encode(), USER.encode()) and \
        secrets.compare_digest(c.password.encode(), PASSWORD.encode())
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong user or password",
                            headers={"WWW-Authenticate": "Basic"})


def agent_for(model: str) -> PharmacyAgent:
    if model not in _agents:
        _agents[model] = PharmacyAgent(llm=LLMClient(model=model), gateway=gw)
    return _agents[model]


class ChatIn(BaseModel):
    session_id: str | None = None
    text: str
    model: str | None = None


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def page():
    return HTML


@app.get("/api/models", dependencies=[Depends(auth)])
def models():
    names = [m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=10).json()["models"]]
    usable = [n for n in names if "Qwen3-4B-Instruct-2507" in n or n.startswith("pharmacy-")]
    return {"default": DEFAULT_MODEL, "models": [DEFAULT_MODEL] + [n for n in usable if n != DEFAULT_MODEL]}


@app.post("/api/reset", dependencies=[Depends(auth)])
def reset(body: dict):
    _sessions.pop(body.get("session_id") or "", None)
    return {"ok": True}


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(m: ChatIn):
    text = (m.text or "").strip()[:2000]
    if not text:
        raise HTTPException(400, "empty message")
    model = m.model or DEFAULT_MODEL
    sid = m.session_id or uuid.uuid4().hex
    s = _sessions.get(sid)
    if not s or s["model"] != model:                     # new chat (or model switched)
        s = {"model": model, "conv": Conversation(customer_id=CUSTOMER, conv_id=f"web-{sid[:8]}")}
        _sessions[sid] = s
    conv = s["conv"]
    before = len(conv.messages)
    t0 = time.time()
    with _lock:
        reply = agent_for(model).respond(conv, text)
    new = conv.messages[before:]
    tools, results = [], [x for x in new if x.get("role") == "tool"]
    for i, call in enumerate([x for x in new if x.get("tool_calls")]):
        fn = call["tool_calls"][0]["function"]
        res = results[i]["content"] if i < len(results) else ""
        tools.append({"name": fn["name"], "args": fn.get("arguments") or {},
                      "result": res[:600] + ("..." if len(res) > 600 else "")})
    guard = ("medical-advice guard" if reply == MEDICAL_REFUSAL else
             "scheduled-medicine guard" if reply == SCHEDULED_REFUSAL and not tools else
             "restricted item (gateway)" if reply == SCHEDULED_REFUSAL else None)
    return {"session_id": sid, "reply": reply, "model": model,
            "debug": {"tools": tools, "guard": guard, "seconds": round(time.time() - t0, 1)}}


@app.get("/api/health")
def health():
    return {"ok": True}
