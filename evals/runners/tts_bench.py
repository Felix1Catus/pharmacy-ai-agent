# -*- coding: utf-8 -*-
"""Text-to-speech benchmark on CPU: per-sentence synthesis time (= first-audio latency in
conversation mode) and real-time factor, plus WAV samples to listen to.

  .venv/bin/python evals/runners/tts_bench.py --engine piper
  /opt/tts/namaa-venv/bin/python evals/runners/tts_bench.py --engine namaa
Samples: results/tts_samples/<engine>/NN.wav ; numbers: results/tts_bench_<engine>.json
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "adapter"))
from speech_text import sentences  # noqa: E402

# typical replies of the agent (Saudi dialect), as they would be sent in the chat
REPLIES = [
    "أهلاً وسهلاً بك في الصيدلية 👋 كيف أقدر أخدمك اليوم؟",
    "لقيت لك 🔎\n1. بنادول ٥٠٠ ملغ — 12.00 ريال\n2. بنادول اكسترا — 15.00 ريال",
    "✅ تم إنشاء عرض السعر رقم S00120 بإجمالي 51.75 ريال.\n🏪 الاستلام من: فرع العليا في الرياض",
    "هذا الدواء يحتاج وصفة طبية. أرسل صورة الوصفة من زر المرفقات وأكمل لك الطلب.",
    "سجّلنا بياناتك ورقم جوالك 0501234567، وسيراجع الصيدلي الوصفة الآن. سنراسلك هنا فور مراجعتها.",
]


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def piper_engine():
    from piper import PiperVoice
    voice = PiperVoice.load("/opt/tts/piper/ar_JO-kareem-medium.onnx")

    def synth(text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            if hasattr(voice, "synthesize_wav"):
                voice.synthesize_wav(text, w)
            else:
                voice.synthesize(text, w)
        return buf.getvalue()
    return synth


def namaa_engine():
    import torch
    import torchaudio as ta
    from chatterbox import mtl_tts
    from huggingface_hub import snapshot_download
    from safetensors.torch import load_file
    torch.set_num_threads(4)
    ckpt = snapshot_download(repo_id="NAMAA-Space/NAMAA-Saudi-TTS", repo_type="model")
    model = mtl_tts.ChatterboxMultilingualTTS.from_pretrained(device="cpu")
    model.t3.load_state_dict(load_file(f"{ckpt}/t3_mtl23ls_v2.safetensors", device="cpu"))
    model.t3.eval()

    def synth(text: str) -> bytes:
        with torch.inference_mode():
            wav = model.generate(text, language_id="ar")
        buf = io.BytesIO()
        ta.save(buf, wav, model.sr, format="wav")
        return buf.getvalue()
    return synth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["piper", "namaa"], required=True)
    a = ap.parse_args()
    t0 = time.time()
    synth = {"piper": piper_engine, "namaa": namaa_engine}[a.engine]()
    load_s = time.time() - t0
    out_dir = ROOT / "results" / "tts_samples" / a.engine
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, n = [], 0
    for reply in REPLIES:
        for s in sentences(reply, "ar"):
            n += 1
            t1 = time.time()
            data = synth(s)
            dt = time.time() - t1
            secs = wav_seconds(data)
            (out_dir / f"{n:02d}.wav").write_bytes(data)
            rows.append({"n": n, "text": s, "synth_s": round(dt, 2), "audio_s": round(secs, 2),
                         "rtf": round(dt / max(secs, 0.01), 2)})
            print(f"{n:02d} {dt:6.2f}s for {secs:5.2f}s audio (RTF {dt / max(secs, .01):.2f})  {s[:60]}",
                  flush=True)
    summary = {"engine": a.engine, "load_s": round(load_s, 1),
               "avg_first_audio_s": round(sum(r["synth_s"] for r in rows) / len(rows), 2),
               "max_sentence_s": max(r["synth_s"] for r in rows),
               "rtf": round(sum(r["synth_s"] for r in rows) / sum(r["audio_s"] for r in rows), 2),
               "sentences": rows}
    (ROOT / "results" / f"tts_bench_{a.engine}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "sentences"}, indent=2))


if __name__ == "__main__":
    main()
