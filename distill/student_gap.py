#!/usr/bin/env python3
"""Where is the student weak? Run the live model (llama-server, Qwen3-8B) on the teacher-labelled messages
with the exact captured production inputs and compare its first action with the teacher's.

Writes out/student_gap.jsonl (one row per message) and out/student_gap_summary.json (agreement by intent,
dialect and teacher action). No Claude usage: only the local model is called.
Usage: python student_gap.py [--limit N]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8080/v1/chat/completions"
sys.path.insert(0, str(HERE.parent / "agent"))
from llm import _to_openai  # noqa: E402


def action(kind, tool, args, reply=""):
    if kind == "reply":
        return "reply"
    return tool + (f":{(args or {}).get('reason')}" if tool == "escalate_to_human" else "")


def student(cap):
    body = {"messages": _to_openai(cap["messages"]), "tools": cap["tools"], "stream": False,
            "temperature": 0, "max_tokens": 300, "chat_template_kwargs": {"enable_thinking": False}}
    m = requests.post(URL, json=body, timeout=120).json()["choices"][0]["message"]
    calls = m.get("tool_calls") or []
    if calls:
        fn = calls[0]["function"]
        args = fn.get("arguments")
        args = json.loads(args) if isinstance(args, str) else (args or {})
        return "tool", fn["name"], args, ""
    return "reply", "", {}, (m.get("content") or "").strip()


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10**9
    c = sqlite3.connect(HERE / "state.db")
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM messages WHERE status IN ('valid','review') ORDER BY id LIMIT ?", (limit,)).fetchall()
    if "--sample" in sys.argv:          # quick check: a fixed random sample (seeded, so reruns compare)
        import random
        n = int(sys.argv[sys.argv.index("--sample") + 1])
        rows = random.Random(7).sample(rows, min(n, len(rows)))
    tag = "_sample" if "--sample" in sys.argv else ""
    out = open(HERE / "out" / f"student_gap{tag}.jsonl", "w", encoding="utf-8")
    by = {k: defaultdict(lambda: [0, 0]) for k in ("category", "dialect", "teacher_action")}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        cap, lab = json.loads(r["captured"]), json.loads(r["label"])
        t_act = action(lab["kind"], lab.get("tool"), lab.get("arguments") or json.loads(lab.get("arguments_json") or "{}"))
        try:
            kind, tool, args, reply = student(cap)
            s_act = action(kind, tool, args)
        except Exception as e:  # noqa: BLE001
            kind, tool, args, reply, s_act = "error", "", {}, str(e)[:100], "error"
        agree = s_act == t_act
        row = {"id": r["id"], "status": r["status"], "category": r["category"], "dialect": r["dialect"],
               "text": r["text"], "teacher": t_act, "teacher_args": lab.get("arguments") or lab.get("reply"),
               "student": s_act, "student_args": args or reply, "agree": agree}
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        if r["status"] == "valid":
            for k, v in (("category", r["category"]), ("dialect", r["dialect"]), ("teacher_action", t_act)):
                by[k][v][0] += agree
                by[k][v][1] += 1
        if i % 100 == 0:
            print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(rows)} scored ({time.time() - t0:.0f}s)", flush=True)
    out.close()
    summary = {k: {v: {"agree": a, "n": n, "rate": round(a / n, 3)} for v, (a, n) in sorted(d.items(), key=lambda x: x[1][0] / x[1][1])}
               for k, d in by.items()}
    (HERE / "out" / f"student_gap{tag}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", json.dumps({k: {v: s["rate"] for v, s in d.items()} for k, d in summary.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
