# -*- coding: utf-8 -*-
"""WhatsApp Cloud API channel (Meta) for the pharmacy agent.

  GET  /webhook   Meta's verification handshake (hub.verify_token == WHATSAPP_VERIFY_TOKEN)
  POST /webhook   incoming messages: X-Hub-Signature-256 checked with WHATSAPP_APP_SECRET, then
                  each message is answered in the background (Meta wants a 200 within seconds)

The sender's number (wa_id, e.g. "966501234567") identifies the conversation and is looked up in
Odoo Contacts, so a known customer is greeted by name and their saved address reused; an unknown
number goes through registration when they first order. A WhatsApp chat is ALWAYS identified by
its own sender number - never by a number typed into the chat. Supported inbound types: text,
menu taps (interactive list/button replies), voice notes, and prescription photos/PDFs.
Replies: plain text, or the menu as an interactive LIST message, or up to 3 reply BUTTONS for
the customer flow (✅ نعم / ✏️ تعديل ...).

State that survives a restart: every conversation is saved after each message
(results/whatsapp_sessions/, private files, no prescription images), so a deploy never loses an
order in progress, a pending consent question or "waiting for the prescription photo".

Messages the pharmacy starts (a pharmacist's decision, "it's back in stock"): Meta only delivers
free text within 24 hours of the customer's last message. Outside that window the notifier sends
the approved template named in WHATSAPP_TEMPLATE_NOTIFY ("you have an update, reply to see it")
at most once a day, and delivers the real message as soon as the customer answers. Nothing is
marked as delivered unless Meta accepted it.

DRY RUN: without WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_NUMBER_ID nothing is sent - every
outgoing message is appended to results/whatsapp_outbox.jsonl instead (used by the tests and to
try the channel before the Meta credentials exist).

Run:  .venv/bin/uvicorn --app-dir channels/whatsapp webhook:app --host 127.0.0.1 --port 8091
      (behind the HTTPS reverse proxy; deploy/systemd/pharmacy-whatsapp.service)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo")]
from odoo_client import load_env  # noqa: E402

load_env()
from agent import UNAVAILABLE, Conversation, PharmacyAgent  # noqa: E402
from gateway import Gateway, phone_key  # noqa: E402
from private_files import append_jsonl, private_dir, write_private, write_private_bytes  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pharmacy.whatsapp")

GRAPH = "https://graph.facebook.com/v21.0"
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
PHONE_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
DRY_RUN = not (TOKEN and PHONE_ID)
# approved utility template (no variables) used to reopen the 24-hour window, and its language
TEMPLATE_NOTIFY = os.environ.get("WHATSAPP_TEMPLATE_NOTIFY", "").strip()
TEMPLATE_LANG = os.environ.get("WHATSAPP_TEMPLATE_LANG", "ar").strip() or "ar"
VOICE_REPLIES = os.environ.get("WHATSAPP_VOICE_REPLIES", "1") == "1"
NOTIFY_EVERY_S = int(os.environ.get("WHATSAPP_NOTIFY_EVERY_S", "30"))
OUTBOX = ROOT / "results" / "whatsapp_outbox.jsonl"
MEDIA_DIR = ROOT / "results" / "whatsapp_media"
SESS_DIR = ROOT / "results" / "whatsapp_sessions"
MAX_MEDIA = 8 * 1024 * 1024
WINDOW_S = 24 * 3600 - 300                    # Meta's customer-service window, less a margin
SEEN_TTL_S = 2 * 3600                         # Meta retries a delivery for a while, not for days
MAX_CACHED_CONVS = 2000                       # the rest stay on disk until they write again
SUPPORTED = ("text", "interactive", "button", "image", "document", "audio")

_agent: PharmacyAgent | None = None
_convs: dict[str, Conversation] = {}          # wa_id -> conversation (cache of SESS_DIR)
_meta: dict[str, dict] = {}                   # wa_id -> {"last_inbound": ts, "template_sent": ts}
_seen: dict[str, float] = {}                  # message ids already handled (Meta retries)
_locks: dict[str, threading.Lock] = {}        # one reply at a time per customer, in order
_state_lock = threading.Lock()


def agent() -> PharmacyAgent:
    global _agent
    if _agent is None:
        _agent = PharmacyAgent(gateway=Gateway())
    return _agent


# ---- conversations: cached in memory, saved to disk after every message -----------------
def _safe(wa_id: str) -> str:
    return "".join(ch for ch in str(wa_id) if ch.isdigit())[:20] or "unknown"


def _sess_file(wa_id: str) -> Path:
    return SESS_DIR / f"{_safe(wa_id)}.json"


def conversation(wa_id: str) -> Conversation:
    conv = _convs.get(wa_id)
    if conv is None:
        f = _sess_file(wa_id)
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                conv = Conversation.from_dict(d["conv"])
                _meta[wa_id] = d.get("meta") or {}
            except Exception:                 # corrupt/old file: start a new chat
                log.exception("saved conversation for %s could not be read; starting fresh", _safe(wa_id))
                conv = None
        if conv is None:
            conv = Conversation(customer_id=None, conv_id=f"wa-{wa_id}", customer_facing=True)
        # always the sender's own number, also for a number format we do not recognise
        conv.wa_phone = phone_key(wa_id) or _safe(wa_id)
        conv.customer_facing = True
        with _state_lock:
            if len(_convs) >= MAX_CACHED_CONVS:
                for old in sorted(_convs, key=lambda k: _convs[k].last_ts)[: MAX_CACHED_CONVS // 10]:
                    _convs.pop(old, None)
                    _meta.pop(old, None)
                    _locks.pop(old, None)
            _convs[wa_id] = conv
    return conv


def save(wa_id: str) -> None:
    conv = _convs.get(wa_id)
    if conv is None:
        return
    try:
        private_dir(SESS_DIR)
        write_private(_sess_file(wa_id), json.dumps(
            {"conv": conv.to_dict(), "meta": _meta.get(wa_id) or {}}, ensure_ascii=False))
    except Exception:
        log.exception("conversation for %s not saved", _safe(wa_id))


def _lock(wa_id: str) -> threading.Lock:
    with _state_lock:
        return _locks.setdefault(wa_id, threading.Lock())


def _already_seen(msg_id: str | None) -> bool:
    """Meta delivers a message again when our 200 was slow: answer each id once."""
    if not msg_id:
        return False
    now = time.time()
    with _state_lock:
        if len(_seen) > 5000:
            for k in [k for k, ts in _seen.items() if now - ts > SEEN_TTL_S]:
                _seen.pop(k, None)
        if msg_id in _seen:
            return True
        _seen[msg_id] = now
        return False


# ---- Meta verification + signature -------------------------------------------
def signature_ok(body: bytes, header: str | None) -> bool:
    if not APP_SECRET:
        return DRY_RUN                         # no secret configured: only acceptable in dry run
    want = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(want, header)


def parse_messages(payload: dict) -> list[dict]:
    """Flatten Meta's envelope into [{id, wa_id, name, type, text, media_id, filename}]."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind, text, media, fname = m.get("type"), "", None, None
                if not m.get("from"):
                    continue
                if kind == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif kind == "interactive":           # menu / button tap -> its title, as typed text
                    it = m.get("interactive") or {}
                    text = (it.get("list_reply") or it.get("button_reply") or {}).get("title", "")
                elif kind == "button":
                    text = (m.get("button") or {}).get("text", "")
                elif kind in ("image", "document"):
                    body = m.get(kind) or {}
                    media, text, fname = body.get("id"), body.get("caption", ""), body.get("filename")
                elif kind == "audio":                 # voice note (Opus .ogg) or audio file
                    media, fname = (m.get("audio") or {}).get("id"), "voice-note.ogg"
                out.append({"id": m.get("id"), "wa_id": m.get("from"), "name": names.get(m.get("from")),
                            "type": kind, "text": text, "media_id": media, "filename": fname})
    return out


# ---- answer one message --------------------------------------------------------
def handle(msg: dict) -> None:
    """Answer one incoming message. Never lets an error pass silently: the customer gets the
    fixed "try again shortly" line and the failure is logged with its traceback."""
    wa = msg["wa_id"]
    with _lock(wa):
        conv = conversation(wa)
        _meta.setdefault(wa, {})["last_inbound"] = time.time()
        try:
            _answer(conv, msg)
        except Exception:
            log.exception("message %s from %s could not be answered", msg.get("id"), _safe(wa))
            send(wa, UNAVAILABLE["en" if conv.lang == "en" else "ar"], None)
        finally:
            save(wa)
        try:
            _deliver_pending(wa, conv)            # anything the pharmacy was waiting to tell them
        except Exception:
            log.exception("pending notifications for %s not delivered", _safe(wa))
        finally:
            save(wa)


def _answer(conv: Conversation, msg: dict) -> None:
    wa, lang = msg["wa_id"], ("en" if conv.lang == "en" else "ar")
    if msg["type"] not in SUPPORTED:
        send(wa, ("أرسل رسالة نصية أو صوتية، أو صورة/ملف الوصفة من فضلك." if lang == "ar"
                  else "Please send a text or voice message, or a photo/PDF of the prescription."), None)
        return
    media = None
    if msg.get("media_id"):
        media = download_media(msg["media_id"])
        if media is None and not DRY_RUN:
            send(wa, ("ما قدرت أستلم الملف (الحد الأقصى 8 ميجا). ممكن ترسله مرة ثانية؟" if lang == "ar"
                      else "I couldn't receive that file (8 MB at most). Could you send it again?"), None)
            return
    if msg["type"] == "audio":
        # voice note -> Gulf-tuned speech-to-text -> the same agent; the reply quotes what
        # was understood so the customer can correct a mishearing
        reply = (agent().respond_voice(conv, media, msg.get("filename")) if media is not None
                 else agent().respond(conv, ""))
        send(wa, reply, conv.last_menu, conv.menu_shown)
        if VOICE_REPLIES:                         # answer a voice note with a voice note too
            said = next((x["content"] for x in reversed(conv.messages)
                         if x.get("role") == "assistant" and x.get("content")), "")
            send_voice(wa, said, "en" if conv.lang == "en" else "ar")
        return
    # first contact / after 6 h idle: respond() greets (by name if the number is a known
    # contact) and shows the menu; otherwise it continues the conversation
    reply = agent().respond(conv, msg["text"], image=media, image_name=msg.get("filename"))
    send(wa, reply, conv.last_menu, conv.menu_shown)


def download_media(media_id: str) -> bytes | None:
    """The attached file, or None (too large / Meta refused / network)."""
    if DRY_RUN:
        return None
    h = {"Authorization": f"Bearer {TOKEN}"}
    try:
        meta = requests.get(f"{GRAPH}/{media_id}", headers=h, timeout=30).json()
        if not meta.get("url"):
            log.error("media %s: Meta returned no url: %.300s", media_id, meta)
            return None
        if int(meta.get("file_size") or 0) > MAX_MEDIA:
            return None
        with requests.get(meta["url"], headers=h, timeout=60, stream=True) as r:
            if not r.ok:
                log.error("media %s: download failed with HTTP %s", media_id, r.status_code)
                return None
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_MEDIA:
                    return None
            return data
    except (requests.RequestException, ValueError):
        log.exception("media %s could not be downloaded", media_id)
        return None


# ---- sending -------------------------------------------------------------------------
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


def _post_graph(path: str, **kw) -> dict | None:
    """POST to the Graph API; the JSON answer, or None if Meta did not accept it (logged).
    One retry for a network error or a 5xx - never for a 4xx (the message itself is refused)."""
    for attempt in (1, 2):
        try:
            r = requests.post(f"{GRAPH}/{PHONE_ID}/{path}", headers={"Authorization": f"Bearer {TOKEN}"},
                              timeout=kw.pop("timeout", 30), **kw)
        except requests.RequestException as e:
            log.warning("WhatsApp %s: network error (attempt %s): %s", path, attempt, e)
            time.sleep(1)
            continue
        if r.ok:
            try:
                return r.json()
            except ValueError:
                return {}
        log.error("WhatsApp %s refused with HTTP %s: %.500s", path, r.status_code, r.text)
        if r.status_code < 500:
            return None
        time.sleep(1)
    return None


def send(to: str, text: str, buttons: list[dict] | None, is_menu: bool = False) -> bool:
    """Send a reply; True only if it went out (dry run: written to the outbox)."""
    msg = build_message(to, text or "", buttons, is_menu)
    if DRY_RUN:
        append_jsonl(OUTBOX, {"ts": time.time(), **msg})
        return True
    ok = _post_graph("messages", json=msg) is not None
    if not ok and msg["type"] == "interactive":      # buttons refused (e.g. a label Meta rejects):
        ok = _post_graph("messages", json=build_message(to, text or "", None)) is not None   # plain text
    return ok


def send_template(to: str) -> bool:
    """The approved "you have an update" template: the only message Meta delivers outside the
    24-hour window."""
    if not TEMPLATE_NOTIFY:
        return False
    msg = {"messaging_product": "whatsapp", "to": to, "type": "template",
           "template": {"name": TEMPLATE_NOTIFY, "language": {"code": TEMPLATE_LANG}}}
    if DRY_RUN:
        append_jsonl(OUTBOX, {"ts": time.time(), **msg})
        return True
    return _post_graph("messages", json=msg) is not None


def send_voice(to: str, text: str, lang: str = "ar") -> None:
    """Speak the reply (adapter/tts.py) and send it as a WhatsApp voice note (Opus .ogg)."""
    try:
        import tts
        ogg = tts.whole_reply_ogg(text, lang)
    except Exception:
        log.warning("spoken reply skipped: text-to-speech failed", exc_info=True)
        ogg = None
    if not ogg:
        return
    if DRY_RUN:
        path = MEDIA_DIR / f"reply-{_safe(to)}-{int(time.time() * 1000)}.ogg"
        write_private_bytes(path, ogg)
        append_jsonl(OUTBOX, {"ts": time.time(), "messaging_product": "whatsapp", "to": to,
                              "type": "audio", "audio": {"file": str(path)}})
        return
    up = _post_graph("media", data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                     files={"file": ("reply.ogg", ogg, "audio/ogg")}, timeout=60)
    if up and up.get("id"):
        _post_graph("messages", json={"messaging_product": "whatsapp", "to": to, "type": "audio",
                                      "audio": {"id": up["id"]}})


# ---- messages the pharmacy starts: review results, back-in-stock ---------------------
def window_open(wa_id: str) -> bool:
    """May free text be sent now? (the customer wrote within the last 24 hours)"""
    if DRY_RUN:
        return True
    return time.time() - float((_meta.get(wa_id) or {}).get("last_inbound") or 0) < WINDOW_S


def _knock(wa_id: str) -> None:
    """Outside the window: the template, at most once a day; the real message follows when the
    customer answers (handle -> _deliver_pending)."""
    meta = _meta.setdefault(wa_id, {})
    if time.time() - float(meta.get("template_sent") or 0) < 24 * 3600:
        return
    if not TEMPLATE_NOTIFY:
        log.warning("customer %s has an update but the 24-hour window is closed and "
                    "WHATSAPP_TEMPLATE_NOTIFY is not set: staff should call them", _safe(wa_id))
        meta["template_sent"] = time.time()          # warn once a day, not every 30 seconds
        return
    if send_template(wa_id):
        meta["template_sent"] = time.time()


def _deliver_pending(wa: str, conv: Conversation, reviews=None, alerts=None) -> int:
    """Send this customer what the pharmacy has for them. Marked as delivered only when Meta
    accepted the message; otherwise it stays pending for the next round."""
    a = agent()
    if not conv.customer_id:
        return 0
    reviews = a.gw.pending_notifications(conv.customer_id) if reviews is None else reviews
    alerts = a.gw.due_stock_alerts(conv.customer_id) if alerts is None else alerts
    if not (reviews or alerts):
        return 0
    if not window_open(wa):
        _knock(wa)
        return 0
    sent = 0
    for s in reviews:
        before = (conv.awaiting_confirm_order, conv.awaiting_rx_order, len(conv.messages))
        text = a.customer.review_message(conv, s)
        if send(wa, text, conv.last_menu):
            a.gw.mark_notified(s["order_id"])
            sent += 1
        else:                                        # not delivered: the chat must not wait for an answer
            conv.awaiting_confirm_order, conv.awaiting_rx_order = before[0], before[1]
            del conv.messages[before[2]:]
    for al in alerts:
        n = len(conv.messages)
        text = a.customer.stock_alert_message(conv, al)
        if send(wa, text, None):
            a.gw.mark_stock_alert_sent(al["alert_id"])
            sent += 1
        else:
            del conv.messages[n:]
    return sent


def notify_pending() -> int:
    """Push every pharmacist decision and back-in-stock message the customer hasn't been told
    about (approved -> quotation details + ✅ confirm & pay / ❌ cancel buttons). Returns how many."""
    a, sent = agent(), 0
    todo: dict[str, dict] = {}
    for s in a.gw.pending_notifications():
        if s.get("whatsapp_number"):                  # none: staff calls the customer
            todo.setdefault(s["whatsapp_number"], {"cid": s["customer_id"], "reviews": [], "alerts": []})["reviews"].append(s)
    for al in a.gw.due_stock_alerts():
        if al.get("whatsapp_number"):
            todo.setdefault(al["whatsapp_number"], {"cid": al["customer_id"], "reviews": [], "alerts": []})["alerts"].append(al)
    for wa, job in todo.items():
        try:
            with _lock(wa):
                conv = conversation(wa)
                if not conv.customer_id:
                    prof = a.gw.get_customer(job["cid"])
                    if prof:
                        a.identify(conv, prof)
                if conv.customer_id != job["cid"]:    # the number now belongs to another contact
                    log.warning("notification for customer %s skipped: %s is contact %s",
                                job["cid"], _safe(wa), conv.customer_id)
                    continue
                n = _deliver_pending(wa, conv, job["reviews"], job["alerts"])
                if n:
                    conv.started, conv.last_ts = True, time.time()
                sent += n
                save(wa)
        except Exception:
            log.exception("notifications for %s failed; retried next round", _safe(wa))
    return sent


def _notify_loop():
    while True:
        try:
            notify_pending()
        except Exception:                             # Odoo restarting etc.: try again next round
            log.warning("notifier round failed; retrying in %ss", NOTIFY_EVERY_S, exc_info=True)
        time.sleep(NOTIFY_EVERY_S)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not DRY_RUN and not APP_SECRET:
        log.error("WHATSAPP_APP_SECRET is not set: every incoming message will be rejected")
    if DRY_RUN:
        log.warning("DRY RUN: no WhatsApp credentials - replies go to %s", OUTBOX)
    if os.environ.get("WHATSAPP_NOTIFIER", "1") == "1":
        threading.Thread(target=_notify_loop, daemon=True, name="wa-notifier").start()
    yield


app = FastAPI(title="Pharmacy agent - WhatsApp webhook", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and \
            hmac.compare_digest(q.get("hub.verify_token", ""), VERIFY_TOKEN):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/webhook")
async def incoming(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not signature_ok(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "not JSON")
    for msg in parse_messages(payload if isinstance(payload, dict) else {}):
        if not _already_seen(msg["id"]):
            background.add_task(handle, msg)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "dry_run": DRY_RUN, "conversations": len(_convs)}
