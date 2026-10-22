# -*- coding: utf-8 -*-
"""Speech-to-text benchmark on CPU (faster-whisper) for Gulf-Arabic pharmacy voice notes.

For each model x prompt setting: seconds per note, character error rate (CER) against the
reference transcript (after Arabic normalisation, punctuation removed), and drug-name recall
(the medicine word comes through, compared by sound so spelling variants count).

  .venv/bin/python evals/runners/asr_bench.py [--manifest evals/datasets/audio_synthetic/manifest.json]
      [--models small large-v3-turbo] [--out results/asr_bench.json]

Synthetic audio = plumbing + first signal only; real voice notes are the accuracy evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "adapter"), str(ROOT / "agent")]
from faster_whisper import WhisperModel  # noqa: E402
from rapidfuzz.distance import Levenshtein  # noqa: E402

from fuzzy_search import normalize, sound_key  # noqa: E402
from voice import decode  # noqa: E402

GULF_PROMPT = ("محادثة واتساب مع صيدلية في السعودية باللهجة الخليجية: ابغى، ابي، وش، عندكم، "
               "كم سعره، علبة، علبتين، حبوب، بنادول، بروفين، ادفيل، اسبرين، اوجمنتين، سنتروم، "
               "فيتامين سي، اوميغا ثلاثة، غافيسكون، توصيل، استلام، فرع، الرياض، جدة، الدمام، طلبي.")
DRUGS = ["بنادول", "بروفين", "فيتامين", "سنتروم", "ادفيل", "غافيسكون", "panadol", "اسبرين",
         "اوجمنتين", "اوميغا"]


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalize(t))).strip()


def cer(ref: str, hyp: str) -> float:
    r, h = clean(ref).replace(" ", ""), clean(hyp).replace(" ", "")
    return Levenshtein.distance(r, h) / max(1, len(r))


def drug_hit(ref: str, hyp: str) -> bool | None:
    want = [d for d in DRUGS if d in clean(ref)]
    if not want:
        return None
    keys = {sound_key(w) for w in clean(hyp).split() if len(w) >= 3}
    return all(any(sound_key(d) == k or Levenshtein.normalized_similarity(sound_key(d), k) >= 0.75
                   for k in keys) for d in want)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(ROOT / "evals/datasets/audio_synthetic/manifest.json"))
    ap.add_argument("--models", nargs="+", default=["small", "large-v3-turbo"])
    ap.add_argument("--out", default=str(ROOT / "results/asr_bench.json"))
    a = ap.parse_args()
    items = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    report = {}
    for name in a.models:
        t0 = time.time()
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
        load_s = time.time() - t0
        for prompt_name, prompt in (("plain", None), ("gulf_prompt", GULF_PROMPT)):
            rows = []
            for it in items:
                t1 = time.time()
                audio, total_s = decode((ROOT / it["audio_path"]).read_bytes(), it["audio_path"])
                segs, info = model.transcribe(audio, language="ar", beam_size=5, vad_filter=True,
                                              initial_prompt=prompt)
                hyp = " ".join(s.text.strip() for s in segs)
                dt = time.time() - t1
                rows.append({"id": it["id"], "ref": it["reference_transcript"], "hyp": hyp,
                             "seconds": round(dt, 2), "audio_s": round(total_s, 2),
                             "cer": round(cer(it["reference_transcript"], hyp), 3),
                             "drug_ok": drug_hit(it["reference_transcript"], hyp)})
            drugs = [r["drug_ok"] for r in rows if r["drug_ok"] is not None]
            key = f"{name}/{prompt_name}"
            report[key] = {
                "model_load_s": round(load_s, 1),
                "avg_seconds_per_note": round(sum(r["seconds"] for r in rows) / len(rows), 2),
                "avg_audio_seconds": round(sum(r["audio_s"] for r in rows) / len(rows), 2),
                "avg_cer": round(sum(r["cer"] for r in rows) / len(rows), 3),
                "drug_name_recall": round(sum(drugs) / len(drugs), 3) if drugs else None,
                "items": rows}
            print(f"{key:32s} {report[key]['avg_seconds_per_note']:6.2f}s/note  "
                  f"CER {report[key]['avg_cer']:.3f}  drug recall {report[key]['drug_name_recall']}",
                  flush=True)
    Path(a.out).parent.mkdir(exist_ok=True)
    Path(a.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
