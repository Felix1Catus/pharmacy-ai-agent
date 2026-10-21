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

import base64
import binascii
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
from gateway import Gateway, normalize_phone  # noqa: E402
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
_agents: dict[str, PharmacyAgent] = {}
_sessions: dict[str, dict] = {}
# load the model in the background at startup so the first message doesn't pay ~90 s
threading.Thread(target=lambda: LLMClient(model=DEFAULT_MODEL).warm_up(), daemon=True).start()


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


MAX_IMAGE = 8 * 1024 * 1024


class ChatIn(BaseModel):
    session_id: str | None = None
    text: str = ""
    model: str | None = None
    image: str | None = None          # prescription photo or PDF: base64 or data: URL
    image_name: str | None = None


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


GUARD_LABELS = {
    "menu": "menu (fixed reply)", "injection": "prompt-injection guard",
    "off_topic": "off-topic guard", "output_filter": "output filter (leak blocked)",
    "medical": "medical-advice guard", "scheduled": "scheduled-medicine guard",
    "scheduled_tool": "scheduled-medicine guard", "scheduled_search": "scheduled-medicine guard",
    "scheduled_prescription": "scheduled-medicine guard (prescription)",
    "prescription_tool": "prescription needed -> asked for photo",
    "prescription_check": "prescription photo rejected (checks)", "handover": "handed to pharmacist",
    "customer": "customer identify/register flow", "order": "order placed (fixed confirmation)",
    "rx_review": "prescription quotation -> pharmacist review",
    "review_result": "pharmacist review result (sent by the system)",
}


def _session(sid: str | None, model: str, wa_phone: str | None = None):
    """One conversation per browser chat. It starts UNIDENTIFIED, like a WhatsApp message from
    an unknown number; a simulated WhatsApp number (the test page's field) is looked up in Odoo
    Contacts exactly like the real WhatsApp sender id will be."""
    sid = sid or uuid.uuid4().hex
    s = _sessions.get(sid)
    if not s or s["model"] != model:                     # new chat (or model switched)
        s = {"model": model, "conv": Conversation(customer_id=None, conv_id=f"web-{sid[:8]}",
                                                  customer_facing=True,
                                                  wa_phone=normalize_phone(wa_phone))}
        _sessions[sid] = s
    return sid, s["conv"]


def _out(sid, conv, reply, model, tools, t0):
    return {"session_id": sid, "reply": reply, "model": model, "menu": conv.last_menu,
            "lang": conv.lang,
            "customer": ({"id": conv.customer_id, "name": conv.customer_name}
                         if conv.customer_id else None),
            "wa_phone": conv.wa_phone,
            "debug": {"tools": tools, "guard": GUARD_LABELS.get(conv.last_guard, conv.last_guard),
                      "seconds": round(time.time() - t0, 1)}}


class StartIn(BaseModel):
    session_id: str | None = None
    model: str | None = None
    wa_phone: str | None = None       # simulated WhatsApp sender number (optional)


@app.post("/api/start", dependencies=[Depends(auth)])
def start(m: StartIn):
    """Open a conversation: greeting + menu (instant, no model call)."""
    model = m.model or DEFAULT_MODEL
    sid, conv = _session(m.session_id, model, m.wa_phone)
    t0 = time.time()
    reply = agent_for(model).greet(conv)
    return _out(sid, conv, reply, model, [], t0)


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(m: ChatIn):
    text = (m.text or "").strip()[:2000]
    image = None
    if m.image:
        try:
            image = base64.b64decode(m.image.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "bad image data")
        if len(image) > MAX_IMAGE:
            raise HTTPException(413, "image too large (max 8 MB)")
    if not text and not image:
        raise HTTPException(400, "empty message")
    model = m.model or DEFAULT_MODEL
    sid, conv = _session(m.session_id, model)
    before = len(conv.messages)
    t0 = time.time()
    # model calls queue inside LLMClient; menu/guard replies are instant even while busy
    reply = agent_for(model).respond(conv, text, image=image, image_name=m.image_name)
    new = conv.messages[before:]
    tools, results = [], [x for x in new if x.get("role") == "tool"]
    for i, call in enumerate([x for x in new if x.get("tool_calls")]):
        fn = call["tool_calls"][0]["function"]
        res = results[i]["content"] if i < len(results) else ""
        tools.append({"name": fn["name"], "args": fn.get("arguments") or {},
                      "result": res[:600] + ("..." if len(res) > 600 else "")})
    if image and new and str(new[0].get("content", "")).startswith("[Prescription verified]"):
        tools.insert(0, {"name": "prescription_check", "args": {},
                         "result": new[0]["content"][:600]})
    return _out(sid, conv, reply, model, tools, t0)


class PollIn(BaseModel):
    session_id: str


@app.post("/api/poll", dependencies=[Depends(auth)])
def poll(m: PollIn):
    """Messages the system sends on its own, e.g. the pharmacist's prescription decision
    (on WhatsApp these are pushed; the test page asks every few seconds)."""
    s = _sessions.get(m.session_id)
    if not s:
        return {"messages": []}
    conv = s["conv"]
    msgs = agent_for(s["model"]).customer.deliver_notifications(conv)
    return {"messages": [{"reply": x["reply"], "menu": x["menu"],
                          "debug": {"tools": [], "guard": GUARD_LABELS["review_result"],
                                    "seconds": 0}} for x in msgs]}


@app.get("/api/health")
def health():
    return {"ok": True}
