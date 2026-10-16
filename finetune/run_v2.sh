#!/usr/bin/env bash
# Fine-tune pipeline v2 (CPU, detached-safe). Logs: /tmp/ft_v2.log
#   A) 0.6B on the rebalanced set -> held-out eval (compare to v1: base 52%, ft 71%)
#   B) wait for the 4B download, then the same recipe on Qwen3-4B-Instruct-2507
# Each training run checkpoints every 25 steps and auto-resumes, so re-running this script after
# an interruption continues where it stopped (finished phases are skipped via their DONE marker).
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
log() { echo "[$(date '+%H:%M:%S')] $*"; }

if [ ! -f results/ft_cpu_v2/adapter/adapter_model.safetensors ]; then
  log "PHASE A: train Qwen3-0.6B on rebalanced data"
  FT_OUT=results/ft_cpu_v2 $PY finetune/train_lora_cpu.py || { log "phase A train FAILED"; exit 1; }
fi
if [ ! -f results/ft_cpu_v2/eval.json ]; then
  log "PHASE A: held-out eval (fine-tuned 0.6B v2)"
  FT_ONLY=1 FT_ADAPTER=results/ft_cpu_v2/adapter FT_EVAL_OUT=results/ft_cpu_v2/eval.json \
    $PY finetune/test_lora_cpu.py || log "phase A eval FAILED"
fi

log "PHASE B: waiting for the 4B download"
while [ ! -f /tmp/qwen4b_download.done ]; do sleep 30; done
if [ ! -f results/ft_4b/adapter/adapter_model.safetensors ]; then
  log "PHASE B: train Qwen3-4B-Instruct-2507"
  FT_BASE=Qwen/Qwen3-4B-Instruct-2507 FT_OUT=results/ft_4b $PY finetune/train_lora_cpu.py \
    || { log "phase B train FAILED"; exit 1; }
fi
log "ALL DONE"
