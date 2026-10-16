#!/usr/bin/env bash
# Fine-tune round 3 (CPU, detached-safe, resumable). Log: /tmp/ft_r3.log
#   data: agent's real system prompt, escalation ~13%, first-move + symptom/category searches,
#         confirm-before-hold, full-length examples (FT_MAX_LEN=2304, no truncation)
#   1) LoRA on Qwen3-4B-Instruct-2507   2) export -> Ollama "pharmacy-4b-ft3"
#   3) tool probe   4) T-B4 agent eval on the COMMITTED agent code (the 92% baseline code)
#   5) held-out bare-model test (frozen v1 examples, fine-tuned only; base was 65% in v2)
set -u
cd "$(dirname "$0")/.."
ROOT=$PWD; PY=$ROOT/.venv/bin/python
BASE_HF=Qwen/Qwen3-4B-Instruct-2507; PROD=hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
OUT=results/ft_4b_r3; NAME=pharmacy-4b-ft3
log() { echo "[$(date '+%H:%M:%S')] $*"; }
mkdir -p $OUT

if [ ! -f $OUT/adapter/adapter_model.safetensors ]; then
  log "R3-1 train 4B (full length, 66 steps)"
  FT_BASE=$BASE_HF FT_OUT=$OUT FT_MAX_LEN=2304 FT_MAX_STEPS=66 $PY finetune/train_lora_cpu.py \
    || { log "R3-1 train FAILED"; exit 1; }
fi
if ! ollama list | grep -q "^$NAME"; then
  log "R3-2 export"
  DROP_BASE_CACHE=1 TEMPLATE_FROM=$PROD finetune/export_to_ollama.sh $BASE_HF $OUT/adapter $NAME \
    || { log "R3-2 export FAILED"; exit 1; }
fi
log "R3-3 tool probe"; $PY evals/runners/toolcheck.py $NAME:latest
if [ ! -f $OUT/agent_text_eval_ft3.json ]; then
  log "R3-4 agent eval (committed code, worktree)"
  WT=/tmp/agent-eval-wt; git worktree remove --force $WT 2>/dev/null; git worktree add -q --detach $WT HEAD
  cp .env $WT/.env
  (cd $WT && AGENT_MODEL=$NAME:latest $PY evals/runners/agent_eval.py) \
    && cp $WT/results/agent_text_eval.json $OUT/agent_text_eval_ft3.json || log "R3-4 FAILED"
  git worktree remove --force $WT
fi
if [ ! -f $OUT/eval.json ]; then
  log "R3-5 held-out test (fine-tuned only)"
  FT_ONLY=1 FT_BASE=$BASE_HF FT_ADAPTER=$OUT/adapter FT_EVAL_OUT=$OUT/eval.json $PY finetune/test_lora_cpu.py \
    || log "R3-5 FAILED"
fi
log "ROUND 3 DONE"
