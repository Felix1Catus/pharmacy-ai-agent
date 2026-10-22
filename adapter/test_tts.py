# -*- coding: utf-8 -*-
"""Spoken replies: chat text -> speakable sentences, sentence jobs with prefetch and
interruption, WhatsApp voice-note answer. Piper parts skip if the voice isn't installed.

Run:  pytest adapter/test_tts.py -q
"""
from __future__ import annotations

import io
import sys
import time
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "adapter")]

from speech_text import sentences, speakable  # noqa: E402


def test_list_reply_becomes_speakable_sentences():
    s = sentences("لقيت لك 🔎\n1. بنادول ٥٠٠ ملغ — 12.00 ريال\n2. بنادول اكسترا — 15.00 ريال")
    assert s == ["لقيت لك", "بنادول خمسمائة ملغ، اثنا عشر ريال", "بنادول اكسترا، خمسة عشر ريال"]


def test_order_ref_money_and_link():
    s = sentences("✅ تم إنشاء عرض السعر رقم S00120 بإجمالي 51.75 ريال.\n💳 ادفع من هنا:\nhttp://x/my/orders/120")
    assert s[0] == "تم إنشاء عرض السعر رقم مائة وعشرون بإجمالي واحد وخمسون ريال وخمسة وسبعون هللة"
    assert "الرابط موجود في الرسالة" in s and not any("http" in x for x in s)


def test_phone_numbers_are_read_digit_by_digit():
    assert "صفر خمسة صفر واحد اثنين" in speakable("رقم جوالك 0501234567")


def test_english_reply():
    assert sentences("Quotation S00555 created, total 12.50 SAR.", "en") == [
        "Quotation number five hundred and fifty-five created, total twelve SAR and fifty halalas"]


def test_emojis_markdown_and_buttons_text_are_not_spoken():
    out = speakable("**تمام** 👍 اختر من القائمة 👇")
    assert "*" not in out and "👍" not in out and "تمام" in out


def test_long_sentence_is_split_for_low_latency():
    long = "، ".join(["بنادول اكسترا متوفر في فرع العليا"] * 12)
    assert all(len(x) <= 220 for x in sentences(long)) and len(sentences(long)) >= 2


# ---- sentence jobs (Piper) --------------------------------------------------------------
def _piper_ready():
    try:
        import piper  # noqa: F401
        import tts
        return Path(tts.PIPER_MODEL).exists()
    except Exception:
        return False


piper = pytest.mark.skipif(not _piper_ready(), reason="Piper voice not installed")


@piper
def test_job_speaks_sentence_by_sentence():
    import tts
    job = tts.new_job("أهلاً بك. بنادول اكسترا متوفر بخمسة عشر ريال.", "ar")
    assert job and len(job.sentences) == 2
    wav = job.audio(0)
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnframes() / w.getframerate() > 0.3
    assert job.spoken == 1


@piper
def test_interrupt_stops_the_job_and_keeps_what_was_heard():
    import tts
    job = tts.new_job("الجملة الأولى. الجملة الثانية. الجملة الثالثة.", "ar")
    job.audio(0)
    job.cancel()                                       # customer started talking
    assert job.audio(1) is None and job.spoken == 1 and job.cancelled


@piper
def test_next_sentence_is_prepared_while_the_current_plays():
    import tts
    job = tts.new_job("الجملة الأولى هنا. الجملة الثانية هنا.", "ar")
    job.audio(0)
    time.sleep(1.0)                                    # sentence 1 was prefetched meanwhile
    t0 = time.time()
    assert job.audio(1) and time.time() - t0 < 0.2


@piper
def test_whatsapp_voice_note_answer_is_opus_ogg():
    import tts
    ogg = tts.whole_reply_ogg("تم إنشاء طلبك. شكراً لك.", "ar")
    assert ogg and ogg[:4] == b"OggS"


# ---- test chat endpoints (needs Odoo for the agent) ------------------------------------
def test_chat_speech_endpoints_and_interrupt_note():
    if not _piper_ready():
        pytest.skip("Piper voice not installed")
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "odoo"), str(ROOT / "channels" / "webchat")]
        import os
        os.environ.setdefault("WEBCHAT_PASSWORD", "test")
        from fastapi.testclient import TestClient
        import server
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"chat server not importable: {e}")
    c = TestClient(server.app)
    auth = (server.USER, server.PASSWORD)
    d = c.post("/api/start", json={}, auth=auth).json()
    d = c.post("/api/chat", json={"session_id": d["session_id"], "text": "القائمة", "speak": True},
               auth=auth).json()
    sp = d["speech"]
    assert sp and sp["count"] >= 2
    r = c.get(f"/api/tts/{sp['job']}/0", auth=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    c.post(f"/api/tts/{sp['job']}/cancel", json={"heard": 1}, auth=auth)
    conv = server._sessions[d["session_id"]]["conv"]
    assert conv.interrupt_note and "interrupted" in conv.interrupt_note
    assert c.get(f"/api/tts/{sp['job']}/1", auth=auth).status_code == 204
