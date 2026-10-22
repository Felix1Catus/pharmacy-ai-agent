# -*- coding: utf-8 -*-
"""Voice notes -> text (CPU, faster-whisper) tuned for Gulf-Arabic pharmacy customers.

  audio bytes (WhatsApp .ogg/opus, browser .webm, iPhone .m4a, .mp3, .wav)
    -> ffmpeg: 16 kHz mono wav, at most MAX_SECONDS
    -> language: Arabic unless Whisper is confident it is English (short Gulf clips are
       otherwise sometimes detected as Persian/Urdu)
    -> Whisper with a Gulf pharmacy vocabulary prompt (ابغى، وش، عندكم، بنادول، توصيل ...)
    -> spoken digit runs ("صفر خمسة واحد ...") -> digits, so phone numbers / order ids work
The transcript then goes through the normal agent (guards, menu, customer flow, model).

Model: ASR_MODEL in .env (default small, int8). Loaded once, on first use.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

MAX_SECONDS = int(os.environ.get("ASR_MAX_SECONDS", "120"))
# small + the Gulf prompt measured best on this 4-vCPU server: 2.4 s/note, 7.9% character errors,
# 91% drug names (large-v3-turbo: 6.9 s/note, no better on drug names) - results/asr_bench.json
MODEL_NAME = os.environ.get("ASR_MODEL", "small")
CPU_THREADS = int(os.environ.get("ASR_THREADS") or os.cpu_count() or 4)

GULF_PROMPT = ("محادثة واتساب مع صيدلية في السعودية باللهجة الخليجية: ابغى، ابي، وش، عندكم، "
               "كم سعره، علبة، علبتين، حبوب، شراب، بنادول، بروفين، ادفيل، اسبرين، اوجمنتين، "
               "سنتروم، فيتامين سي، اوميغا ثلاثة، غافيسكون، توصيل، استلام، فرع، الرياض، جدة، "
               "الدمام، طلبي، رقمي.")

_DIGIT_WORDS = {
    "صفر": "0", "زيرو": "0", "واحد": "1", "وحده": "1", "واحدة": "1", "اثنين": "2", "اثنان": "2",
    "إثنين": "2", "ثنين": "2", "ثلاثة": "3", "ثلاثه": "3", "ثلاث": "3", "تلاتة": "3", "اربعة": "4",
    "أربعة": "4", "اربعه": "4", "اربع": "4", "خمسة": "5", "خمسه": "5", "خمس": "5", "ستة": "6",
    "سته": "6", "ست": "6", "سبعة": "7", "سبعه": "7", "سبع": "7", "ثمانية": "8", "ثمانيه": "8",
    "ثمان": "8", "تمانية": "8", "تسعة": "9", "تسعه": "9", "تسع": "9",
    # frequent Whisper mishearings of Gulf digits (only ever converted inside a run of digits)
    "وافد": "1", "وحد": "1", "صف": "0", "سفر": "0", "سفى": "0", "زفر": "0", "اثنن": "2",
    "ثماني": "8", "ثمانى": "8", "تماني": "8",
    "تسع": "9", "ستّة": "6",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "oh": "0",
}
_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


# pharmacy words Whisper sometimes splits in two ("في تامين" -> "فيتامين")
_VOCAB = {w.strip("،.") for w in GULF_PROMPT.split()} | {
    "فيتامين", "بنادول", "بانادول", "بروفين", "سنتروم", "اوميغا", "اوميجا", "غافيسكون", "اسبرين",
    "اوجمنتين", "اموكسيسيلين", "باراسيتامول", "ايبوبروفين", "كلاريتين", "ديتول", "ستربسلز",
    "فولتارين", "نيوروفين", "كالسيوم", "مغنيسيوم", "حديد", "مضاد", "حيوي", "مسكن", "بخاخ", "مرهم",
    "طلبي", "رقمي", "عنواني", "بيتي", "طلباتي"}


def _norm(w: str) -> str:
    return (w.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه")
             .replace("ى", "ي").strip("،,.؟?"))


_VOCAB_N = {_norm(w) for w in _VOCAB}


def rejoin_split_words(text: str) -> str:
    """'عندكم في تامين سي' -> 'عندكم فيتامين سي': two neighbours that together are a known
    pharmacy word are merged (only then - normal word pairs are untouched)."""
    words, out, i = text.split(), [], 0
    while i < len(words):
        # "طلب اي" -> "طلبي", "رقم اي" -> "رقمي": a possessive ي heard as a separate "اي"
        if i + 1 < len(words) and _norm(words[i + 1]) in ("اي", "ي") \
                and _norm(words[i] + "ي") in _VOCAB_N:
            out.append(words[i] + "ي")
            i += 2
            continue
        if i + 1 < len(words) and _norm(words[i] + words[i + 1]) in _VOCAB_N and not (
                _norm(words[i]) in _VOCAB_N and _norm(words[i + 1]) in _VOCAB_N):
            out.append(words[i] + words[i + 1])
            i += 2
            continue
        out.append(words[i])
        i += 1
    return " ".join(out)


def spoken_digits_to_numbers(text: str, min_run: int = 4) -> str:
    """Runs of at least `min_run` spoken single digits become one number: 'رقمي صفر خمسة واحد
    اثنين ثلاثة' -> 'رقمي 05123'. Shorter runs ('علبتين ... ثلاث علب') are left as words."""
    words = text.translate(_DIG).split()
    out, run = [], []

    def flush():
        # a run counts if it is long, or already holds a multi-digit number that a spoken digit
        # continues ("051234567 ثماني" -> "0512345678")
        if len(run) >= min_run or (len(run) >= 2 and any(w.isdigit() and len(w) >= 3 for w in run)):
            out.append("".join(_DIGIT_WORDS.get(w.strip("،,.؟?").lower(), w) for w in run))
        else:
            out.extend(run)
        run.clear()

    for w in words:
        bare = w.strip("،,.؟?").lower()
        bare = bare[1:] if bare.startswith("و") and bare[1:] in _DIGIT_WORDS else bare   # "وخمسة"
        if bare in _DIGIT_WORDS or bare.isdigit():
            run.append(bare if bare.isdigit() else bare)
        else:
            flush()
            out.append(w)
    flush()
    return re.sub(r"(?<=\d) (?=\d)", "", " ".join(out))


class Transcriber:
    """Thread-safe, lazily loaded faster-whisper model (one transcription at a time on CPU)."""

    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                       cpu_threads=CPU_THREADS)
        return self._model

    def warm_up(self):
        with self._lock:
            self._load()

    def transcribe(self, data: bytes, filename: str | None = None) -> dict:
        """-> {"text", "language", "audio_seconds", "seconds", "truncated", "empty"}"""
        t0 = time.time()
        try:
            audio, total_s = decode(data, filename)
        except RuntimeError as e:
            return {"text": "", "language": None, "audio_seconds": 0.0,
                    "seconds": round(time.time() - t0, 2), "truncated": False, "empty": True,
                    "error": str(e)[-300:]}
        with self._lock:
            model = self._load()
            lang, prob, _ = model.detect_language(audio)
            language = "en" if lang == "en" and prob >= 0.7 else "ar"
            text = ""
            # the silence filter (VAD) can drop a whole quiet or close-mic note: retry without it
            for vad in (True, False):
                segs, _info = model.transcribe(
                    audio, language=language, beam_size=5, vad_filter=vad,
                    initial_prompt=GULF_PROMPT if language == "ar" else None,
                    condition_on_previous_text=False)
                text = " ".join(s.text.strip() for s in segs).strip()
                if len(re.sub(r"\W", "", text)) >= 2:
                    break
        text = spoken_digits_to_numbers(rejoin_split_words(text))
        return {"text": text, "language": language, "audio_seconds": round(total_s, 1),
                "seconds": round(time.time() - t0, 2), "truncated": total_s > MAX_SECONDS,
                "empty": len(re.sub(r"\W", "", text)) < 2}


def decode(data: bytes, filename: str | None = None):
    """Any audio -> (16 kHz mono float32 samples, at most MAX_SECONDS; full length in seconds).
    ffmpeg does the decoding (no PyAV dependency, which breaks between versions)."""
    import numpy as np
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / ("in" + (Path(filename).suffix if filename else ".bin"))
        src.write_bytes(data)
        # dynaudnorm lifts quiet recordings (browser microphones) to a normal speaking level
        r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src),
                            "-af", "dynaudnorm", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                           capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("could not decode audio: " + r.stderr.decode(errors="ignore"))
    samples = np.frombuffer(r.stdout, dtype=np.float32)
    total_s = len(samples) / 16000
    return samples[: MAX_SECONDS * 16000].copy(), total_s


_default: Transcriber | None = None


def transcriber() -> Transcriber:
    global _default
    if _default is None:
        _default = Transcriber()
    return _default
