#!/usr/bin/env bash
# Unattended v2 fine-tune, start to finish (systemd unit pharmacy-pipeline-v2; safe to restart at any point):
#   1 labels   Claude labels the step after a tool result (distill/followup.py; sleeps through usage limits)
#   2 data     first-step + follow-up examples -> runs/v2/data
#   3 train    live model/voice/webchat stopped (GPUs), QLoRA 2 epochs on both GPUs (resumes from checkpoints)
#   4 merge    adapter -> full-precision merge -> GGUF Q4_K_M
#   5 eval     tuned vs current: held-out first step + follow-up agreement, agent eval in customer + staff mode
#   6 restore  current model + voice + webchat back up (v2 is NOT made live: that is the owner's call)
# Each finished stage leaves runs/v2/.done-<stage>; a failure restores the live services before exiting.
#   bash pipeline_v2.sh          run (what the unit does)
#   bash pipeline_v2.sh status   where it is
set -uo pipefail
# paths: defaults are the 2026-11 server layout (working copies outside the repo); override for a new server
TRAIN=${TRAIN:-/home/bench/train}; RUN=${RUN:-$TRAIN/runs/v2}; REPO=${REPO:-/home/bench/pharmacy-ai-agent}; DIST=${DIST:-/home/bench/distill}
PY=$REPO/.venv/bin/python; NAME=qwen3-8b-pharmacy-v2; IMG=ghcr.io/ggml-org/llama.cpp:full-cuda
mkdir -p "$RUN/eval"; LOG=$RUN/pipeline.log
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
done_(){ [ -f "$RUN/.done-$1" ]; }
mark(){ touch "$RUN/.done-$1"; log "== stage $1 done"; }

if [ "${1:-}" = status ]; then
  for s in labels data train merge eval restore; do printf "%-8s %s\n" $s "$(done_ $s && echo done || echo -)"; done
  (cd $DIST && $PY -c "import sqlite3; c=sqlite3.connect('state.db'); print('follow-ups:', dict(c.execute('select status, count(*) from followups group by status').fetchall()))") 2>/dev/null
  grep -a "\[speed\]" "$RUN/train.log" 2>/dev/null | tail -1 | grep -o "\[speed\].*"
  tail -3 "$LOG"; exit 0
fi

restore_live(){
  docker rm -f pharmacy-llm-v2 >/dev/null 2>&1
  docker start pharmacy-llm >/dev/null 2>&1
  (cd $REPO/odoo && docker compose up -d >/dev/null 2>&1)
  for i in $(seq 1 90); do curl -sf 127.0.0.1:8080/health >/dev/null && break; sleep 2; done
  systemctl start pharmacy-tts-namaa pharmacy-webchat
}
fail(){ log "FAILED: $* -> restoring the live services (the unit retries in 10 min)"; restore_live; exit 1; }
serve(){  # serve(model gguf, container) on :8080 across both GPUs
  docker rm -f pharmacy-llm-v2 >/dev/null 2>&1; docker stop pharmacy-llm >/dev/null 2>&1
  if [ "$2" = pharmacy-llm ]; then docker start pharmacy-llm >/dev/null
  else docker run -d --name "$2" --gpus all -p 127.0.0.1:8080:8080 -v /opt/models:/models:ro --entrypoint /app/llama-server \
         $IMG -m "/models/$1" -ngl 99 -fa on -sm tensor -c 8192 -np 1 --jinja --host 0.0.0.0 --port 8080 >/dev/null; fi
  for i in $(seq 1 120); do curl -sf 127.0.0.1:8080/health >/dev/null && return 0; sleep 2; done; return 1
}
log "== pipeline v2 (re)start"

# 1 ------------------------------------------------------------------ labels (Claude, live stack stays up)
if ! done_ labels; then
  log "stage labels: Claude labels the step after a tool result"
  (cd $DIST && CLAUDE_CONFIG_DIR=/opt/claude-distill TEACHER=claude TEACHER_MODEL=opus \
     DISTILL_REPO=$DIST/agent_snapshot_v2 DISTILL_LOG=$RUN/followup.log nice -n 10 $PY followup.py run) \
     >>"$RUN/followup.out" 2>&1 || fail "follow-up labelling"
  mark labels
fi

# 2 ------------------------------------------------------------------ data
if ! done_ data; then
  mkdir -p "$RUN/data"
  cp $DIST/out/train.jsonl $DIST/out/val.jsonl $DIST/out/followup_train.jsonl $DIST/out/followup_val.jsonl "$RUN/data/" \
    || fail "copy data"
  log "data: $(wc -l < $RUN/data/train.jsonl) first-step + $(wc -l < $RUN/data/followup_train.jsonl) follow-up train, $(wc -l < $RUN/data/val.jsonl) + $(wc -l < $RUN/data/followup_val.jsonl) val"
  mark data
fi

# 3 ------------------------------------------------------------------ train (needs both GPUs)
if ! done_ train; then
  log "stage train: stopping the live model, voice and webchat for the GPUs"
  systemctl stop pharmacy-webchat pharmacy-tts-namaa; docker rm -f pharmacy-llm-v2 >/dev/null 2>&1; docker stop pharmacy-llm >/dev/null 2>&1
  sleep 5
  (cd $TRAIN && TRAIN_OUT=$RUN/model DISTILL_OUT=$RUN/data PYTHONUNBUFFERED=1 \
     /opt/train-venv/bin/torchrun --nproc_per_node=2 train_qlora.py --epochs 2 --batch 4 --accum 2) >>"$RUN/train.log" 2>&1 \
     || fail "training (see $RUN/train.log)"
  [ -f "$RUN/model/adapter/adapter_model.safetensors" ] || fail "no adapter after training"
  mark train
fi

# 4 ------------------------------------------------------------------ merge -> GGUF
if ! done_ merge; then
  (cd $TRAIN && nice -n 10 bash merge_to_gguf.sh "$RUN/model/adapter" $NAME) >>"$RUN/merge.log" 2>&1 || fail "merge (see $RUN/merge.log)"
  rm -rf /opt/models/$NAME-merged
  mark merge
fi

# 5 ------------------------------------------------------------------ eval: tuned vs current
if ! done_ eval; then
  systemctl stop pharmacy-webchat pharmacy-tts-namaa
  (cd $REPO/odoo && docker compose up -d >/dev/null 2>&1); for i in $(seq 1 40); do curl -sf -o /dev/null 127.0.0.1:8069/web/login && break; sleep 3; done
  for M in v2 base; do
    if [ $M = v2 ]; then serve $NAME-q4_k_m.gguf pharmacy-llm-v2 || fail "serve v2"; MODEL=$NAME
    else serve - pharmacy-llm || fail "serve base"; MODEL=qwen3-8b-q4_k_m; fi
    log "eval $M: held-out agreement"
    (cd $TRAIN && $PY val_agreement.py ${M}_first && VAL_FILE=$RUN/data/followup_val.jsonl $PY val_agreement.py ${M}_followup) >>"$RUN/eval/eval.log" 2>&1 || fail "val agreement $M"
    for MODE in customer staff; do
      log "eval $M: agent eval, $MODE mode"
      (cd $REPO && AGENT_MODEL=$MODEL EVAL_MODE=$MODE EVAL_TAG=_${M}_$MODE $PY evals/runners/agent_eval.py) >>"$RUN/eval/eval.log" 2>&1 || fail "agent eval $M $MODE"
      mv $REPO/results/agent_text_eval_${M}_$MODE.json "$RUN/eval/"
    done
  done
  mv $TRAIN/runs/val_agreement_v2_* $TRAIN/runs/val_agreement_base_* "$RUN/eval/" 2>/dev/null
  $PY - "$RUN" <<'PYR' >>"$LOG" 2>&1
import json, sys
from pathlib import Path
run = Path(sys.argv[1]); e = run / "eval"
def j(p): return json.loads((e / p).read_text())
rows = []
for m in ("base", "v2"):
    f1, f2 = j(f"val_agreement_{m}_first_summary.json"), j(f"val_agreement_{m}_followup_summary.json")
    cf, st = j(f"agent_text_eval_{m}_customer.json")["metrics"], j(f"agent_text_eval_{m}_staff.json")["metrics"]
    rows.append((m, f1["agree"], f2["agree"], cf, st))
out = ["# v2 fine-tune: tuned vs current Qwen3-8B", "",
       "| | current | v2 |", "|---|---|---|"]
def line(label, f):
    out.append(f"| {label} | " + " | ".join(str(f(r)) for r in rows) + " |")
line("held-out first action = teacher", lambda r: f"{r[1]:.1%}")
line("held-out step after a tool result = teacher", lambda r: f"{r[2]:.1%}")
for mode, k in (("customer", 3), ("staff", 4)):
    line(f"{mode} eval: acceptable", lambda r, k=k: f"{r[k]['acceptable_action_accuracy']:.0%}")
    line(f"{mode} eval: exact", lambda r, k=k: f"{r[k]['exact_action_accuracy']:.0%}")
    line(f"{mode} eval: wrong first action", lambda r, k=k: f"{r[k]['wrong_action_rate']:.1%}")
    line(f"{mode} eval: unrequested order/hold in chain", lambda r, k=k: f"{r[k].get('unrequested_state_change_rate', 'n/a')}")
    line(f"{mode} eval: avg tool calls", lambda r, k=k: f"{r[k].get('avg_tool_calls', 'n/a')}")
    line(f"{mode} eval: medical refusal / restricted safe", lambda r, k=k: f"{r[k]['medical_refusal_rate']} / {r[k]['restricted_safe_rate']}")
    line(f"{mode} eval: p50 latency", lambda r, k=k: f"{r[k]['latency_s']['p50']}s")
(run / "REPORT.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print("\n".join(out))
PYR
  mark eval
fi

# 6 ------------------------------------------------------------------ restore the live stack (current model)
if ! done_ restore; then
  restore_live
  log "live: model $(curl -s 127.0.0.1:8080/health), webchat $(systemctl is-active pharmacy-webchat), voice $(systemctl is-active pharmacy-tts-namaa)"
  mark restore
fi
log "== pipeline v2 finished: $RUN/REPORT.md; v2 model /opt/models/$NAME-q4_k_m.gguf (not live)"
