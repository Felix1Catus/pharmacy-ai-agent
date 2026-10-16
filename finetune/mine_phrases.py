# -*- coding: utf-8 -*-
"""Mine authentic Najdi request/question frames from HeshamHaroon/saudi-dialect-conversations.

The dataset is general-domain Najdi chat, so we don't use its content — we harvest the DIALECT:
frequent user-turn openers and request/question markers, to diversify the phrasing of the
pharmacy tool-use training examples (finetune/build_sft.py). Output: finetune/data/najdi_phrases.json
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
PARQUET = HERE / "data" / "najdi_conversations.parquet"
OUT = HERE / "data" / "najdi_phrases.json"

# Najdi/Gulf request + question markers we care about for pharmacy intents.
REQUEST_MARKERS = ["أبي", "ابي", "أبغى", "ابغى", "أبا", "ودي", "عطني", "أعطني", "ناولني",
                   "سجل لي", "ثبت لي", "احجز", "ممكن", "ابغي", "أبغي", "دور لي", "عايز"]
QUESTION_MARKERS = ["وش", "ايش", "إيش", "كم", "وين", "متى", "فيه", "عندكم", "عندكو", "هل",
                    "كيف", "متوفر", "موجود"]
_AR = re.compile(r"[ؗ-ًؚ-ْٰـ]")


def norm(t):
    return _AR.sub("", t or "").strip()


def main():
    tbl = pq.read_table(PARQUET, columns=["messages"])
    openers = Counter()
    req_frames = Counter()
    q_frames = Counter()
    user_turns = 0
    for row in tbl.to_pylist():
        for m in row["messages"]:
            if m.get("role") != "user":
                continue
            user_turns += 1
            text = norm(m.get("content", ""))
            words = text.split()
            if words:
                openers[" ".join(words[:2])] += 1
            # capture short frames that start with a request/question marker
            for mk in REQUEST_MARKERS:
                if text.startswith(mk) or f" {mk} " in f" {text} ":
                    frag = " ".join(words[:4])
                    req_frames[frag] += 1
                    break
            for mk in QUESTION_MARKERS:
                if text.startswith(mk):
                    q_frames[" ".join(words[:3])] += 1
                    break

    out = {
        "source": "HeshamHaroon/saudi-dialect-conversations (Najdi, Apache-2.0, synthetic/GPT-4o)",
        "user_turns_scanned": user_turns,
        "top_openers": openers.most_common(40),
        "top_request_frames": req_frames.most_common(40),
        "top_question_frames": q_frames.most_common(40),
        "request_markers": REQUEST_MARKERS,
        "question_markers": QUESTION_MARKERS,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"scanned {user_turns} user turns -> {OUT.name}")
    print("top request frames:")
    for f, c in req_frames.most_common(12):
        print(f"  {c:4}  {f}")
    print("top question frames:")
    for f, c in q_frames.most_common(12):
        print(f"  {c:4}  {f}")


if __name__ == "__main__":
    main()
