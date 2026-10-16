#!/usr/bin/env bash
# Phase C: evaluate the fine-tuned 4B. Logs: /tmp/ft_phase_c.log
#  1) export adapter -> merged -> GGUF Q4_K_M -> Ollama model "pharmacy-4b-ft"
#     (same template/params as the production model, for a fair comparison)
#  2) quick tool-calling probe
#  3) T-B4 agent eval with the fine-tuned model, on the COMMITTED agent code (the code that
#     produced the 92% baseline), via a clean git worktree
#  4) held-out model test (base vs fine-tuned, transformers on CPU)
set -u
cd "$(dirname "$0")/.."
ROOT=$PWD; PY=$ROOT/.venv/bin/python
BASE_HF=Qwen/Qwen3-4B-Instruct-2507
PROD=hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
log() { echo "[$(date '+%H:%M:%S')] $*"; }

if ! ollama list | grep -q '^pharmacy-4b-ft'; then
  log "C1 export"
  DROP_BASE_CACHE=1 TEMPLATE_FROM=$PROD finetune/export_to_ollama.sh $BASE_HF results/ft_4b/adapter pharmacy-4b-ft \
    || { log "C1 export FAILED"; exit 1; }
fi

log "C2 tool-calling probe"
$PY evals/runners/toolcheck.py pharmacy-4b-ft:latest

if [ ! -f results/ft_4b/agent_text_eval_ft.json ]; then
  log "C3 agent eval (committed code, worktree)"
  WT=/tmp/agent-eval-wt
  git worktree remove --force $WT 2>/dev/null; git worktree add -q --detach $WT HEAD
  cp .env $WT/.env
  (cd $WT && AGENT_MODEL=pharmacy-4b-ft:latest $PY evals/runners/agent_eval.py) \
    && cp $WT/results/agent_text_eval.json results/ft_4b/agent_text_eval_ft.json \
    || log "C3 agent eval FAILED"
  git worktree remove --force $WT
fi

if [ ! -f results/ft_4b/eval.json ]; then
  log "C4 held-out test: base vs fine-tuned 4B (re-downloads base weights)"
  FT_BASE=$BASE_HF FT_ADAPTER=results/ft_4b/adapter FT_EVAL_OUT=results/ft_4b/eval.json \
    $PY finetune/test_lora_cpu.py || log "C4 FAILED"
fi
log "PHASE C DONE"
