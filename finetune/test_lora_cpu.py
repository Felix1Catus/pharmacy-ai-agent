# -*- coding: utf-8 -*-
"""Before/after eval of the CPU LoRA fine-tune on the HELD-OUT test split.

For every test example we give the model everything except the final assistant turn and ask it
to generate that turn. We then check whether it produced the correct action:
  - gold is a tool call  -> did it emit a tool call with the same tool name?
  - gold is a text reply -> did it reply in text (no tool call)?
Runs the base model and base+adapter on the same examples, so the difference is the fine-tune.

Run: python finetune/test_lora_cpu.py           (both)
     FT_ONLY=1 python finetune/test_lora_cpu.py (adapter only)
Writes results/ft_cpu/eval.json
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "finetune" / "data" / "sft_test.jsonl"
ADAPTER = Path(os.environ.get("FT_ADAPTER", ROOT / "results" / "ft_cpu" / "adapter"))
BASE = os.environ.get("FT_BASE", "Qwen/Qwen3-0.6B")
torch.set_num_threads(os.cpu_count())
TC = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def gold_of(ex):
    last = ex["messages"][-1]
    if last.get("tool_calls"):
        return last["tool_calls"][0]["function"]["name"]
    return "(text)"


def pred_of(text):
    m = TC.search(text)
    if not m:
        return "(text)"
    try:
        return json.loads(m.group(1)).get("name") or "(bad)"
    except Exception:  # noqa: BLE001
        return "(bad)"


def run(model, tok, rows, label):
    per = []
    t0 = time.time()
    for i, ex in enumerate(rows, 1):
        prompt = tok.apply_chat_template(ex["messages"][:-1], tools=ex["tools"], tokenize=False,
                                         add_generation_prompt=True, enable_thinking=False)
        ids = tok(prompt, return_tensors="pt")
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=120, do_sample=False,
                                 pad_token_id=tok.eos_token_id)
        text = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=False)
        g, p = gold_of(ex), pred_of(text)
        per.append({"gold": g, "pred": p, "ok": g == p})
        if i % 10 == 0:
            acc = sum(x["ok"] for x in per) / len(per)
            print(f"  [{label}] {i}/{len(rows)}  running acc={acc:.0%}", flush=True)
    acc = sum(x["ok"] for x in per) / len(per)
    by = defaultdict(lambda: [0, 0])
    for x in per:
        by[x["gold"]][0] += x["ok"]; by[x["gold"]][1] += 1
    print(f"== {label}: action accuracy {acc:.0%} ({sum(x['ok'] for x in per)}/{len(per)}) "
          f"in {time.time()-t0:.0f}s", flush=True)
    return {"accuracy": round(acc, 4), "by_gold_action": {k: f"{a}/{b}" for k, (a, b) in by.items()},
            "items": per}


def main():
    rows = [json.loads(l) for l in TEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    tok = AutoTokenizer.from_pretrained(BASE)
    base = AutoModelForCausalLM.from_pretrained(BASE, dtype=torch.bfloat16)
    res = {}
    if not os.environ.get("FT_ONLY"):
        print(f"--- BASE {BASE} (no fine-tune) on {len(rows)} held-out examples ---", flush=True)
        res["base"] = run(base, tok, rows, "base")
    print("--- BASE + LoRA adapter (fine-tuned) ---", flush=True)
    ft = PeftModel.from_pretrained(base, str(ADAPTER))
    res["finetuned"] = run(ft, tok, rows, "finetuned")
    out = ROOT / "results" / "ft_cpu" / "eval.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    if "base" in res:
        print(f"\nRESULT: base {res['base']['accuracy']:.0%}  ->  fine-tuned "
              f"{res['finetuned']['accuracy']:.0%}")
        print("per action (base -> finetuned):")
        for k in sorted(res["finetuned"]["by_gold_action"]):
            print(f"  {k:20} {res['base']['by_gold_action'].get(k,'-'):>6} -> "
                  f"{res['finetuned']['by_gold_action'][k]}")


if __name__ == "__main__":
    main()
