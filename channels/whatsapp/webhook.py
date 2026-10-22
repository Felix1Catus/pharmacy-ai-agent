# -*- coding: utf-8 -*-
"""WhatsApp Cloud API channel (Meta) for the pharmacy agent.

  GET  /webhook   Meta's verification handshake (hub.verify_token == WHATSAPP_VERIFY_TOKEN)
  POST /webhook   incoming messages: X-Hub-Signature-256 checked with WHATSAPP_APP_SECRET, then
                  each message is answered in the background (Meta wants a 200 within seconds)

The sender's number (wa_id, e.g. "966501234567") identifies the conversation and is looked up in
Odoo Contacts, so a known customer is greeted by name and their saved address reused; an unknown
number goes through registration when they first order. Supported inbound types: text, menu
taps (interactive list/button replies), and prescription photos/PDFs (image / document).
Replies: plain text, or the menu as an interactive LIST message, or up to 3 reply BUTTONS for
the customer flow (✅ نعم / ✏️ تعديل ...).

DRY RUN: without WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_NUMBER_ID nothing is sent - every
outgoing message is appended to results/whatsapp_outbox.jsonl instead (used by the tests and to
try the channel before the Meta credentials exist).

Run:  .venv/bin/uvicorn --app-dir channels/whatsapp webhook:app --host 0.0.0.0 --port 8091
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo")]
from odoo_client import load_env  # noqa: E402

load_env()
from agent import Conversation, PharmacyAgent  # noqa: E402
from gateway import Gateway, normalize_phone  # noqa: E402

GRAPH = "https://graph.facebook.com/v21.0"
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
PHONE_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
DRY_RUN = not (TOKEN and PHONE_ID)
OUTBOX = ROOT / "results" / "whatsapp_outbox.jsonl"
MAX_MEDIA = 8 * 1024 * 1024

app = FastAPI(title="Pharmacy agent - WhatsApp webhook", docs_url=None, redoc_url=None)
_agent: PharmacyAgent | None = None
_convs: dict[str, Conversation] = {}          # wa_id -> conversation (agent restarts the menu after 6 h idle)
_seen: dict[str, float] = {}                  # message ids already handled (Meta retries)
_locks: dict[str, threading.Lock] = {}        # one reply at a time per customer, in order


def agent() -> PharmacyAgent:
    global _agent
    if _agent is None:
        _agent = PharmacyAgent(gateway=Gateway())
    return _agent


def conversation(wa_id: str) -> Conversation:
    if wa_id not in _convs:
        _convs[wa_id] = Conversation(customer_id=None, conv_id=f"wa-{wa_id}", customer_facing=True,
                                     wa_phone=normalize_phone(wa_id))
    return _convs[wa_id]


# ---- Meta verification + signature -------------------------------------------
@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and \
            hmac.compare_digest(q.get("hub.verify_token", ""), VERIFY_TOKEN):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


def signature_ok(body: bytes, header: str | None) -> bool:
    if not APP_SECRET:
        return DRY_RUN                         # no secret configured: only acceptable in dry run
    want = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(want, header)


@app.post("/webhook")
async def incoming(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not signature_ok(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    payload = json.loads(body or b"{}")
    for msg in parse_messages(payload):
        if msg["id"] in _seen:
            continue
        _seen[msg["id"]] = time.time()
        background.add_task(handle, msg)
    return {"ok": True}


def parse_messages(payload: dict) -> list[dict]:
    """Flatten Meta's envelope into [{id, wa_id, name, type, text, media_id, filename}]."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind, text, media, fname = m.get("type"), "", None, None
                if kind == "text":
                    text = m["text"].get("body", "")
                elif kind == "interactive":           # menu / button tap -> its title, as typed text
                    it = m["interactive"]
                    text = (it.get("list_reply") or it.get("button_reply") or {}).get("title", "")
                elif kind == "button":
                    text = m["button"].get("text", "")
                elif kind in ("image", "document"):
                    media, text = m[kind].get("id"), m[kind].get("caption", "")
                    fname = m[kind].get("filename")
                elif kind == "audio":                 # voice note (Opus .ogg) or audio file
                    media, fname = m["audio"].get("id"), "voice-note.ogg"
                out.append({"id": m.get("id"), "wa_id": m.get("from"), "name": names.get(m.get("from")),
                            "type": kind, "text": text, "media_id": media, "filename": fname})
    return out


# ---- answer one message --------------------------------------------------------
def handle(msg: dict) -> None:
    lock = _locks.setdefault(msg["wa_id"], threading.Lock())
    with lock:
        conv = conversation(msg["wa_id"])
        media = download_media(msg["media_id"]) if msg.get("media_id") else None
        if msg["type"] not in ("text", "interactive", "button", "image", "document", "audio"):
            reply = ("أرسل رسالة نصية أو صوتية، أو صورة/ملف الوصفة من فضلك." if (conv.lang or "ar") == "ar"
                     else "Please send a text or voice message, or a photo/PDF of the prescription.")
            send(msg["wa_id"], reply, None)
            return
        if msg["type"] == "audio":
            # voice note -> Gulf-tuned speech-to-text -> the same agent; the reply quotes what
            # was understood so the customer can correct a mishearing
            reply = (agent().respond_voice(conv, media, msg.get("filename")) if media is not None
                     else agent().respond(conv, ""))
            send(msg["wa_id"], reply, conv.last_menu, conv.menu_shown)
            if VOICE_REPLIES:                         # answer a voice note with a voice note too
                said = next((x["content"] for x in reversed(conv.messages)
                             if x.get("role") == "assistant" and x.get("content")), "")
                send_voice(msg["wa_id"], said, "en" if conv.lang == "en" else "ar")
            return
        # first contact / after 6 h idle: respond() greets (by name if the number is a known
        # contact) and shows the menu; otherwise it continues the conversation
        reply = agent().respond(conv, msg["text"], image=media, image_name=msg.get("filename"))
        send(msg["wa_id"], reply, conv.last_menu, conv.menu_shown)


VOICE_REPLIES = os.environ.get("WHATSAPP_VOICE_REPLIES", "1") == "1"
MEDIA_DIR = ROOT / "results" / "whatsapp_media"


def send_voice(to: str, text: str, lang: str = "ar") -> None:
    """Speak the reply (adapter/tts.py) and send it as a WhatsApp voice note (Opus .ogg)."""
    try:
        import tts
        ogg = tts.whole_reply_ogg(text, lang)
    except Exception:
        ogg = None
    if not ogg:
        return
    if DRY_RUN:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = MEDIA_DIR / f"reply-{to}-{int(time.time() * 1000)}.ogg"
        path.write_bytes(ogg)
        OUTBOX.parent.mkdir(exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "messaging_product": "whatsapp", "to": to,
                                 "type": "audio", "audio": {"file": str(path)}}) + "\n")
        return
    up = requests.post(f"{GRAPH}/{PHONE_ID}/media", headers={"Authorization": f"Bearer {TOKEN}"},
                       data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                       files={"file": ("reply.ogg", ogg, "audio/ogg")}, timeout=60).json()
    if up.get("id"):
        requests.post(f"{GRAPH}/{PHONE_ID}/messages", headers={"Authorization": f"Bearer {TOKEN}"},
                      json={"messaging_product": "whatsapp", "to": to, "type": "audio",
                            "audio": {"id": up["id"]}}, timeout=30)


def download_media(media_id: str) -> bytes | None:
    if DRY_RUN:
        return None
    h = {"Authorization": f"Bearer {TOKEN}"}
    meta = requests.get(f"{GRAPH}/{media_id}", headers=h, timeout=30).json()
    if int(meta.get("file_size") or 0) > MAX_MEDIA:
        return None
    r = requests.get(meta["url"], headers=h, timeout=60)
    return r.content if r.ok else None


def build_message(to: str, text: str, buttons: list[dict] | None, is_menu: bool = False) -> dict:
    """Text, interactive LIST (the menu, up to 10 rows) or reply BUTTONS (up to 3)."""
    base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    if not buttons:
        return {**base, "type": "text", "text": {"body": text[:4096], "preview_url": False}}
    if is_menu or len(buttons) > 3:
        body = text.split("\n1.")[0] if is_menu else text        # the list shows the items itself
        return {**base, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": body[:1024]},
            "action": {"button": "القائمة · Menu", "sections": [{"title": "Pharmacy", "rows": [
                {"id": b["id"], "title": b["label"][:24]} for b in buttons[:10]]}]}}}
    return {**base, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": text[:1024]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["label"][:20]}}
                               for b in buttons[:3]]}}}


def send(to: str, text: str, buttons: list[dict] | None, is_menu: bool = False) -> None:
    msg = build_message(to, text, buttons, is_menu)
    if DRY_RUN:
        OUTBOX.parent.mkdir(exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **msg}, ensure_ascii=False) + "\n")
        return
    requests.post(f"{GRAPH}/{PHONE_ID}/messages", json=msg, timeout=30,
                  headers={"Authorization": f"Bearer {TOKEN}"})


# ---- pushed messages: the pharmacist's prescription decision ----------------------
NOTIFY_EVERY_S = int(os.environ.get("WHATSAPP_NOTIFY_EVERY_S", "30"))


def notify_pending() -> int:
    """Send every approved/rejected prescription review the customer hasn't been told about
    (approved -> quotation details + ✅ confirm & pay / ❌ cancel buttons). Returns how many."""
    a, sent = agent(), 0
    for s in a.gw.pending_notifications():
        wa = s.get("whatsapp_number")
        if not wa:
            continue                                  # no WhatsApp number: staff calls the customer
        with _locks.setdefault(wa, threading.Lock()):
            conv = conversation(wa)
            if not conv.customer_id:
                prof = a.gw.get_customer(s["customer_id"])
                if prof:
                    a.identify(conv, prof)
            conv.started, conv.last_ts = True, time.time()
            text = a.customer.review_message(conv, s)
            send(wa, text, conv.last_menu)
            a.gw.mark_notified(s["order_id"])
            sent += 1
    return sent


def _notify_loop():
    while True:
        try:
            notify_pending()
        except Exception:                             # Odoo restarting etc.: try again next round
            pass
        time.sleep(NOTIFY_EVERY_S)


@app.on_event("startup")
def _start_notifier():
    if os.environ.get("WHATSAPP_NOTIFIER", "1") == "1":
        threading.Thread(target=_notify_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True, "dry_run": DRY_RUN, "conversations": len(_convs)}
