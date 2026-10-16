#!/usr/bin/env bash
# Export a LoRA fine-tune into Ollama so the agent can use it.
#   usage: finetune/export_to_ollama.sh <hf_base_id> <adapter_dir> <ollama_name>
# Steps (deleting intermediates as it goes, to fit the disk):
#   1) merge the LoRA adapter into the base weights      (bf16 safetensors)
#   2) convert to GGUF f16                               (llama.cpp convert_hf_to_gguf.py)
#   3) quantize to Q4_K_M (same as our baseline GGUF)    (llama-quantize)
#   4) ollama create <name> from the Q4_K_M file
# Ollama >=0.40 no longer applies LoRA adapters at runtime, hence the merge.
set -euo pipefail
BASE="$1"; ADAPTER="$2"; NAME="$3"
cd "$(dirname "$0")/.."
PY=.venv/bin/python; LCPP=/opt/llama.cpp-src
WORK=results/gguf/work-$NAME; mkdir -p "$WORK"
log() { echo "[$(date '+%H:%M:%S')] export $NAME: $*"; }

log "1/4 merge adapter into base"
$PY - "$BASE" "$ADAPTER" "$WORK/merged" <<'PY'
import sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
base, adapter, out = sys.argv[1:4]
m = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16)
m = PeftModel.from_pretrained(m, adapter).merge_and_unload()
m.save_pretrained(out, safe_serialization=True)
AutoTokenizer.from_pretrained(base).save_pretrained(out)
print("merged ->", out)
PY

if [ "${DROP_BASE_CACHE:-0}" = "1" ]; then   # free disk; base re-downloads quickly if needed
  rm -rf "$HOME/.cache/huggingface/hub/models--${BASE//\//--}"; log "dropped HF cache for $BASE"
fi

log "2/4 convert to GGUF f16"
$PY $LCPP/convert_hf_to_gguf.py "$WORK/merged" --outtype f16 --outfile "$WORK/f16.gguf" 2>&1 | tail -1
rm -rf "$WORK/merged"

log "3/4 quantize to Q4_K_M"
$LCPP/build/bin/llama-quantize "$WORK/f16.gguf" "$WORK/q4_k_m.gguf" Q4_K_M 2>&1 | tail -1
rm -f "$WORK/f16.gguf"

log "4/4 ollama create $NAME"
if [ -n "${TEMPLATE_FROM:-}" ]; then
  # reuse the reference model's chat template (incl. tool calling) + parameters, swap the weights
  ollama show "$TEMPLATE_FROM" --modelfile | sed "s|^FROM .*|FROM $PWD/$WORK/q4_k_m.gguf|" > "$WORK/Modelfile"
else
  printf 'FROM %s/%s/q4_k_m.gguf\n' "$PWD" "$WORK" > "$WORK/Modelfile"
fi
ollama create "$NAME" -f "$WORK/Modelfile" 2>&1 | tail -1
rm -f "$WORK/q4_k_m.gguf"
log "done -> ollama model '$NAME'"
