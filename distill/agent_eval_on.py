#!/usr/bin/env python3
"""Run the repo's full agent eval (evals/runners/agent_eval.py) on another message set, unchanged otherwise.

"unseen" = first tool call of each finetune/data/sft_train.jsonl row, minus messages that also appear in the
held-out split (same construction as prompt_fix/prompt_eval.py). Usage (from the repo root):
  python3 agent_eval_on.py <repo> unseen <items json out>
"""
import importlib.util
import json
import sys
from pathlib import Path

repo, which, items_out = sys.argv[1:4]


def first_user(row):
    return next(m["content"] for m in row["messages"] if m["role"] == "user")


skip = {first_user(json.loads(x)) for x in open(f"{repo}/finetune/data/sft_test_frozen_v1.jsonl", encoding="utf-8")}
cat = {"medical_advice": "escalate_medical", "restricted_item": "escalate_restricted", "abusive": "abusive"}
items = []
for i, line in enumerate(open(f"{repo}/finetune/data/sft_train.jsonl", encoding="utf-8")):
    row = json.loads(line)
    user = first_user(row)
    first = next(m for m in row["messages"] if m["role"] == "assistant")
    calls = first.get("tool_calls") or []
    if user in skip or not calls:
        continue
    fn = calls[0]["function"]
    args = fn.get("arguments") or {}
    args = json.loads(args) if isinstance(args, str) else args
    items.append({"id": f"{which}-{i:03d}", "lang": which, "text": user,
                  "category": cat.get(args.get("reason"), fn["name"]),
                  "expected": {"action": fn["name"], "reason": args.get("reason")}})
Path(items_out).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

spec = importlib.util.spec_from_file_location("agent_eval", f"{repo}/evals/runners/agent_eval.py")
ae = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ae)
ae.DATASET = Path(items_out)
ae.main()
