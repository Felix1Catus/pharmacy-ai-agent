# -*- coding: utf-8 -*-
"""Build the eval dataset JSON files from the curated item lists.

Outputs (committed):
  evals/datasets/text_messages.json   ~100 dialect text items + expected tool call
  evals/datasets/voice_manifest.json  voice items: audio path, reference transcript, expected
  evals/datasets/recording_script.md   a prompt sheet for the human to record voice notes

Run:  python evals/datasets/build_datasets.py
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(HERE))
from text_items import ITEMS  # noqa: E402
from voice_items import VOICE_ITEMS  # noqa: E402

REVIEW = "machine-drafted"  # flip to "native-reviewed" per item after review


def build_text():
    rows = []
    for i, (lang, cat, text, action, note) in enumerate(ITEMS, 1):
        row = {
            "id": f"txt-{i:04d}",
            "lang": lang,
            "category": cat,
            "text": text,
            "expected": {"action": action},
            "review_status": REVIEW,
        }
        if action == "escalate_to_human":
            row["expected"]["reason"] = note
        else:
            row["expected"]["note"] = note
        rows.append(row)
    out = HERE / "text_messages.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, out


def build_voice():
    rows = []
    audio_dir = "evals/datasets/audio"
    for i, (lang, text, action, note) in enumerate(VOICE_ITEMS, 1):
        row = {
            "id": f"voice-{i:04d}",
            "lang": lang,
            "audio_path": f"{audio_dir}/voice-{i:04d}.ogg",
            "reference_transcript": text,
            "expected": {"action": action},
            "review_status": REVIEW,
            "audio_recorded": False,
        }
        if action == "escalate_to_human":
            row["expected"]["reason"] = note.split()[0]
        else:
            row["expected"]["note"] = note
        rows.append(row)
    out = HERE / "voice_manifest.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, out


def build_recording_script(voice_rows):
    lines = [
        "# Voice-note recording script (for the human)",
        "",
        "Record each line below as a WhatsApp voice note, in a natural voice. Aim for a mix of",
        "quiet and noisy surroundings. Save each as the given filename under",
        "`evals/datasets/audio/`, then set `audio_recorded: true` for it in",
        "`voice_manifest.json` (or re-run the eval, which checks the files exist).",
        "",
        "Please record **real** voice notes — do not rely on text-to-speech for accuracy numbers.",
        "Vary speakers/dialects if you can. Numbers, phone numbers, order IDs and drug names are",
        "the whole point, so speak them the way a customer naturally would.",
        "",
        "| Save as | Dialect | Say this |",
        "|---|---|---|",
    ]
    for r in voice_rows:
        fname = Path(r["audio_path"]).name
        say = r["reference_transcript"].replace("|", "/")
        lines.append(f"| `{fname}` | {r['lang']} | {say} |")
    lines += [
        "",
        "## Also wanted (section 4 of the plan)",
        "- 50+ voice notes total if possible (these 12 are a starting prompt set).",
        "- Include some with background noise, some very quiet.",
        "- Include a few with phone numbers and order IDs spoken as digits.",
        "- A correct reference transcript for each (fix the text above if you say it differently).",
    ]
    out = HERE / "recording_script.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main():
    text_rows, tpath = build_text()
    voice_rows, vpath = build_voice()
    spath = build_recording_script(voice_rows)

    langs = Counter(r["lang"] for r in text_rows)
    actions = Counter(r["expected"]["action"] for r in text_rows)
    print(f"text items: {len(text_rows)} -> {tpath.name}")
    print("  by lang:", dict(langs))
    print("  by action:", dict(actions))
    # plan/threshold note: per-dialect scoring needs >=15 items; gulf & mixed were called out.
    for lang in ("gulf", "mixed"):
        flag = "OK" if langs[lang] >= 15 else "LOW (<15, per-dialect number won't count)"
        print(f"  {lang}: {langs[lang]} [{flag}]")
    print(f"voice items: {len(voice_rows)} -> {vpath.name} (audio not yet recorded)")
    print(f"recording script -> {spath.name}")


if __name__ == "__main__":
    main()
