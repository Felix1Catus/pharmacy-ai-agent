# -*- coding: utf-8 -*-
"""SYNTHETIC voice notes for plumbing tests and a first ASR signal - NOT accuracy evidence.

Speaks every prompt of voice_items.py with Saudi neural TTS voices (Microsoft edge-tts:
ar-SA-HamedNeural, ar-SA-ZariyahNeural) and converts to WhatsApp-style Opus .ogg (16 kHz mono),
written to evals/datasets/audio_synthetic/<id>-<voice>.ogg plus a manifest. Real customer voice
notes (evals/datasets/recording_script.md) are still what counts for accuracy.

  .venv/bin/python evals/datasets/make_synthetic_voice.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import edge_tts

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from voice_items import VOICE_ITEMS  # noqa: E402

OUT = HERE / "audio_synthetic"
VOICES = {"hamed": "ar-SA-HamedNeural", "zariyah": "ar-SA-ZariyahNeural"}


async def speak(text: str, voice: str, dest: Path) -> None:
    with tempfile.TemporaryDirectory() as d:
        mp3 = Path(d) / "a.mp3"
        await edge_tts.Communicate(text, voice).save(str(mp3))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1",
                        "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(dest)], check=True)


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    manifest = []
    for i, (lang, text, action, note) in enumerate(VOICE_ITEMS, 1):
        for short, voice in VOICES.items():
            dest = OUT / f"voice-{i:04d}-{short}.ogg"
            if not dest.exists():
                await speak(text, voice, dest)
            manifest.append({"id": f"voice-{i:04d}-{short}", "lang": lang, "voice": voice,
                             "audio_path": str(dest.relative_to(HERE.parents[1])),
                             "reference_transcript": text, "expected_action": action,
                             "note": note, "synthetic": True})
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"{len(manifest)} synthetic voice notes in {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
