#!/usr/bin/env python3
"""First-action agreement with the teacher on the held-out validation set (distill/out/val.jsonl, never trained
on), for whatever model llama-server on :8080 is serving. Same request shape as production (thinking off).
  python3 -I val_agreement.py <tag>      -> runs/val_agreement_<tag>.jsonl + a summary printed and saved
"""
import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

URL = "http://127.0.0.1:8080/v1/chat/completions"
VAL = Path(__import__("os").environ.get("VAL_FILE", "/home/bench/distill/out/val.jsonl"))
OUT = Path(__import__("os").environ.get("TRAIN_RUNS", "/home/bench/train/runs"))
STATE_CHANGING = {"create_order", "hold_product"}
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from llm import _to_openai  # noqa: E402  (history in the exact shape the agent sends)


def act(m):
    calls = m.get("tool_calls") or []
    if not calls:
        return "reply", {}
    fn = calls[0]["function"]
    a = fn.get("arguments")
    a = json.loads(a) if isinstance(a, str) else (a or {})
    return fn["name"] + (f":{a.get('reason')}" if fn["name"] == "escalate_to_human" else ""), a


def main():
    tag = sys.argv[1]
    rows = [json.loads(l) for l in open(VAL, encoding="utf-8")]
    by = defaultdict(lambda: [0, 0])
    agree = wrong_state = 0
    lat = []
    with open(OUT / f"val_agreement_{tag}.jsonl", "w", encoding="utf-8") as f:
        for i, r in enumerate(rows, 1):
            body = {"messages": _to_openai(r["messages"][:-1]), "tools": r["tools"], "temperature": 0, "max_tokens": 300,
                    "cache_prompt": False, "chat_template_kwargs": {"enable_thinking": False}}
            t = time.time()
            req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
            try:
                got, ga = act(json.load(urllib.request.urlopen(req, timeout=300))["choices"][0]["message"])
            except Exception as e:  # noqa: BLE001
                got, ga = "error", {"error": str(e)[:100]}
            lat.append(time.time() - t)
            gold, _ = act(r["messages"][-1])
            ok = got == gold
            agree += ok
            wrong_state += (got.split(":")[0] in STATE_CHANGING and not ok)
            by[gold][0] += ok
            by[gold][1] += 1
            f.write(json.dumps({"text": r["messages"][-1 - 1]["content"][:200] if r["messages"][-2]["role"] == "user" else "",
                                "gold": gold, "got": got, "got_args": ga, "agree": ok}, ensure_ascii=False) + "\n")
            if i % 50 == 0:
                print(f"{i}/{len(rows)} agree {agree / i:.1%}", flush=True)
    lat.sort()
    s = {"model": tag, "n": len(rows), "agree": round(agree / len(rows), 3), "wrong_state_changing": wrong_state,
         "p50_s": round(lat[len(lat) // 2], 2),
         "by_teacher_action": {k: f"{a}/{n}" for k, (a, n) in sorted(by.items(), key=lambda x: -x[1][1])}}
    (OUT / f"val_agreement_{tag}_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
