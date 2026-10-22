# -*- coding: utf-8 -*-
"""Text-to-speech for the agent's replies, built for conversation mode (interruptions).

A reply becomes a SpeechJob: speakable sentences (adapter/speech_text.py) synthesised one at a
time. The player asks for sentence 0, 1, 2 ... and the job prepares the next sentence while the
current one plays, so audio starts after ~one sentence and there are no gaps. Interrupting
(the customer starts talking, taps 🎤, types) = job.cancel(): nothing more is synthesised and
job.spoken tells the agent how far the customer actually heard.

Engines (TTS_ENGINE in .env):
  piper  - ONNX on CPU inside this process; Arabic voice ar_JO-kareem (~0.3 s per sentence on
           4 vCPU). Default.
  namaa  - NAMAA-Saudi-TTS (Chatterbox, Saudi dialect, MIT) served by tts_server.py from its own
           venv (/opt/tts/namaa-venv) on TTS_NAMAA_URL; heavy - better on a GPU.
  off    - no speech.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

import requests

from speech_text import sentences

ENGINE = os.environ.get("TTS_ENGINE", "piper")
PIPER_MODEL = os.environ.get("TTS_PIPER_MODEL", "/opt/tts/piper/ar_JO-kareem-medium.onnx")
PIPER_MODEL_EN = os.environ.get("TTS_PIPER_MODEL_EN", "")        # optional English voice
NAMAA_URL = os.environ.get("TTS_NAMAA_URL", "http://127.0.0.1:8095")
# Piper voice character (.env): speed <1 = faster; expressive/rhythm = livelier intonation and
# timing; pitch >1 = brighter. Defaults = the voice as shipped.
SPEED = float(os.environ.get("TTS_SPEED", "1.0"))           # Piper length_scale
EXPRESSIVE = float(os.environ.get("TTS_EXPRESSIVE", "0.667"))  # Piper noise_scale
RHYTHM = float(os.environ.get("TTS_RHYTHM", "0.8"))          # Piper noise_w_scale
PITCH = float(os.environ.get("TTS_PITCH", "1.0"))
JOB_TTL_S = 600


# ---- synthesis cache: a given (engine, lang, text) is voiced once, then served instantly ------
# Most spoken replies are fixed strings (menu, wizard steps, confirmations); caching makes their
# read-back instant instead of paying the model's first-sentence latency on every reply.
_CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR",
                                 str(Path(__file__).resolve().parent.parent / "results" / "tts_cache")))
_CACHE_MAX = int(os.environ.get("TTS_CACHE_MAX", "4000"))
_cache_mem: dict[str, bytes] = {}
_cache_lock = threading.Lock()


def _cache_key(text: str, lang: str) -> str:
    return hashlib.sha1(f"{ENGINE}|{lang}|{text}".encode("utf-8")).hexdigest()


def synth_cached(text: str, lang: str = "ar") -> bytes:
    """engine().synth(text, lang), cached on disk + in memory so repeated/known lines are instant."""
    eng = engine()
    if eng is None:
        return b""
    key = _cache_key(text, lang)
    with _cache_lock:
        hit = _cache_mem.get(key)
    if hit is not None:
        return hit
    f = _CACHE_DIR / f"{key}.wav"
    try:
        if f.exists():
            data = f.read_bytes()
            with _cache_lock:
                _cache_mem[key] = data
            return data
    except OSError:
        pass
    data = eng.synth(text, lang)
    if data:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(f)                        # atomic: a half-written file is never served
        except OSError:
            pass
        with _cache_lock:
            _cache_mem[key] = data
            if len(_cache_mem) > _CACHE_MAX:
                _cache_mem.clear()
    return data


def _fixed_prompts(lang: str) -> list:
    """Static (no-placeholder) wizard strings worth pre-voicing."""
    out = []
    try:
        import order_flow as _of
        tt = _of.T.get(lang, {})
        out += [v for v in tt.values() if isinstance(v, str) and "{" not in v]
        if "bad_qty" in tt:
            out.append(tt["bad_qty"].format(max=_of.MAX_QTY))
    except Exception:
        pass
    return out


def prewarm(lang: str = "ar", extra: list | None = None) -> None:
    """Voice the common fixed prompts into the cache (call from a background thread at startup)."""
    if engine() is None:
        return
    for s in _fixed_prompts(lang) + list(extra or []):
        for sent in sentences(s, lang):
            try:
                synth_cached(sent, lang)
            except Exception:
                pass


class PiperEngine:
    def __init__(self):
        from piper import PiperVoice
        self._voices = {"ar": PiperVoice.load(PIPER_MODEL)}
        if PIPER_MODEL_EN and Path(PIPER_MODEL_EN).exists():
            self._voices["en"] = PiperVoice.load(PIPER_MODEL_EN)
        self._lock = threading.Lock()

    def synth(self, text: str, lang: str = "ar") -> bytes:
        from piper import SynthesisConfig
        voice = self._voices.get(lang) or self._voices["ar"]
        cfg = SynthesisConfig(length_scale=SPEED, noise_scale=EXPRESSIVE, noise_w_scale=RHYTHM)
        buf = io.BytesIO()
        with self._lock, wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w, syn_config=cfg)
        data = buf.getvalue()
        return pitch_shift(data, PITCH) if abs(PITCH - 1.0) > 0.005 else data


def pitch_shift(wav_bytes: bytes, factor: float) -> bytes:
    """Raise/lower the voice without changing its speed (resample + tempo correction)."""
    with wave.open(io.BytesIO(wav_bytes)) as r:
        rate = r.getframerate()
    af = f"asetrate={int(rate * factor)},aresample={rate},atempo={1 / factor:.4f}"
    # raw samples out (a WAV written to a pipe has no real length in its header), header added here
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "wav", "-i", "-", "-af", af,
                        "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                       input=wav_bytes, capture_output=True, timeout=30)
    if p.returncode != 0 or not p.stdout:
        return wav_bytes
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(p.stdout)
    return out.getvalue()


class NamaaEngine:
    """Client of tts_server.py (NAMAA-Saudi-TTS runs in its own process/venv)."""

    def synth(self, text: str, lang: str = "ar") -> bytes:
        r = requests.post(f"{NAMAA_URL}/synth", json={"text": text, "lang": lang}, timeout=300)
        r.raise_for_status()
        return r.content


_engine = None
_engine_lock = threading.Lock()


def engine():
    """The configured engine (loaded once), or None when speech is off / unavailable."""
    global _engine
    with _engine_lock:
        if _engine is None and ENGINE != "off":
            try:
                _engine = PiperEngine() if ENGINE == "piper" else NamaaEngine()
            except Exception:                     # missing model / package: no speech, no crash
                _engine = False
        return _engine or None


class SpeechJob:
    """One spoken reply: sentences synthesised on demand, next one prefetched, cancellable."""

    def __init__(self, text: str, lang: str = "ar"):
        self.id = uuid.uuid4().hex[:16]
        self.lang = lang
        self.sentences = sentences(text, lang)
        self.created = time.time()
        self.spoken = 0                           # sentences actually delivered to the listener
        self._audio: dict[int, bytes] = {}
        self._events = {i: threading.Event() for i in range(len(self.sentences))}
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._started: set[int] = set()
        if self.sentences:
            self._prepare(0)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        """The customer interrupted: stop synthesising; keep `spoken` as what they heard."""
        self._cancel.set()
        for e in self._events.values():
            e.set()

    def _prepare(self, i: int):
        if i >= len(self.sentences) or self.cancelled:
            return
        with self._lock:
            if i in self._started:
                return
            self._started.add(i)

        def work():
            try:
                if not self.cancelled:
                    self._audio[i] = synth_cached(self.sentences[i], self.lang)
            except Exception:
                self._audio[i] = b""
            finally:
                self._events[i].set()
        threading.Thread(target=work, daemon=True).start()

    def audio(self, i: int, timeout: float = 120) -> bytes | None:
        """WAV of sentence i (waits for it), and starts preparing i+1. None if cancelled/none."""
        if i >= len(self.sentences) or self.cancelled:
            return None
        self._prepare(i)
        self._prepare(i + 1)
        self._events[i].wait(timeout)
        if self.cancelled:
            return None
        self.spoken = max(self.spoken, i + 1)
        return self._audio.get(i) or None


_jobs: dict[str, SpeechJob] = {}


def new_job(text: str, lang: str = "ar") -> SpeechJob | None:
    """Start speaking a reply (None when speech is off or there is nothing to say)."""
    if engine() is None:
        return None
    now = time.time()
    for k in [k for k, j in _jobs.items() if now - j.created > JOB_TTL_S]:
        _jobs.pop(k, None)
    job = SpeechJob(text, lang)
    if not job.sentences:
        return None
    _jobs[job.id] = job
    return job


def get_job(job_id: str) -> SpeechJob | None:
    return _jobs.get(job_id)


def whole_reply_ogg(text: str, lang: str = "ar") -> bytes | None:
    """The full reply as one Opus .ogg voice note (WhatsApp voice reply), or None."""
    eng = engine()
    if eng is None:
        return None
    parts = [synth_cached(s, lang) for s in sentences(text, lang)]
    parts = [p for p in parts if p]
    if not parts:
        return None
    with tempfile.TemporaryDirectory() as d:
        lst = Path(d) / "list.txt"
        names = []
        for i, p in enumerate(parts):
            f = Path(d) / f"{i:03d}.wav"
            f.write_bytes(p)
            names.append(f"file '{f}'")
        lst.write_text("\n".join(names))
        out = Path(d) / "reply.ogg"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                            "-b:a", "24k", str(out)], capture_output=True, timeout=120)
        return out.read_bytes() if r.returncode == 0 and out.exists() else None
