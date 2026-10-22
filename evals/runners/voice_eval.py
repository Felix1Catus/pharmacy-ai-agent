# -*- coding: utf-8 -*-
"""End-to-end voice eval: voice note -> speech-to-text -> agent -> action, versus the same
sentence typed. Shows how much understanding is lost between voice and text.

  .venv/bin/python evals/runners/voice_eval.py [--manifest evals/datasets/audio_synthetic/manifest.json]

Uses the production transcriber (adapter/voice.py, ASR_MODEL) and agent; scoring as the text
eval (agent_eval.decide + acceptable actions). Synthetic audio = plumbing + first signal only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "adapter"), str(ROOT / "agent"), str(ROOT / "odoo"),
                str(ROOT / "evals" / "runners")]
from odoo_client import load_env  # noqa: E402

load_env()
from agent import Conversation, PharmacyAgent  # noqa: E402
from agent_eval import decide  # noqa: E402
from asr_bench import cer  # noqa: E402
from gateway import Gateway  # noqa: E402
from llm import LLMClient  # noqa: E402
from qwen_baseline import STATE_CHANGING, acceptable_actions  # noqa: E402
from voice import transcriber  # noqa: E402


def run_item(agent, cust, text):
    conv = Conversation(customer_id=cust)
    t0 = time.time()
    try:
        reply = agent.respond(conv, text)
    except Exception as e:  # noqa: BLE001
        reply = f"__ERROR__ {e}"
    return decide(conv, reply), round(time.time() - t0, 1), reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(ROOT / "evals/datasets/audio_synthetic/manifest.json"))
    ap.add_argument("--out", default=str(ROOT / "results/voice_eval.json"))
    a = ap.parse_args()
    items = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    gw = Gateway()
    agent = PharmacyAgent(llm=LLMClient(), gateway=gw)
    cust = gw.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0]
    tr = transcriber()
    typed_cache, rows = {}, []
    for n, it in enumerate(items, 1):
        exp = it["expected_action"]
        exp_reason = "restricted_item" if "restricted" in it["note"] else (
            "medical_advice" if "medical" in it["note"] else None)
        acc = acceptable_actions(exp, exp_reason)
        heard = tr.transcribe((ROOT / it["audio_path"]).read_bytes(), it["audio_path"])
        d_voice, lat_v, _ = run_item(agent, cust, heard["text"])
        ref = it["reference_transcript"]
        if ref not in typed_cache:
            typed_cache[ref] = run_item(agent, cust, ref)
        d_text, lat_t, _ = typed_cache[ref]
        rows.append({
            "id": it["id"], "lang": it["lang"], "expected": exp, "reference": ref,
            "heard": heard["text"], "asr_seconds": heard["seconds"], "cer": round(cer(ref, heard["text"]), 3),
            "voice_action": d_voice["action"], "voice_ok": d_voice["action"] in acc,
            "text_action": d_text["action"], "text_ok": d_text["action"] in acc,
            "voice_wrong_state_change": d_voice["action"] in STATE_CHANGING and d_voice["action"] not in acc,
            "agent_seconds_voice": lat_v})
        print(f"[{n}/{len(items)}] {it['id']}: voice={d_voice['action']} text={d_text['action']} "
              f"expected={exp} cer={rows[-1]['cer']}", flush=True)
    k = len(rows)
    summary = {
        "n": k, "asr_model": tr.model_name, "synthetic_audio": True,
        "voice_acceptable": round(sum(r["voice_ok"] for r in rows) / k, 3),
        "typed_acceptable": round(sum(r["text_ok"] for r in rows) / k, 3),
        "avg_cer": round(sum(r["cer"] for r in rows) / k, 3),
        "avg_asr_seconds": round(sum(r["asr_seconds"] for r in rows) / k, 2),
        "voice_wrong_state_changes": sum(r["voice_wrong_state_change"] for r in rows)}
    Path(a.out).write_text(json.dumps({"summary": summary, "items": rows}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
