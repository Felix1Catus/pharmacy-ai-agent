#!/usr/bin/env bash
# After training: merge the LoRA adapter into the full-precision Qwen3-8B (on CPU, so it can run while the GPUs
# serve), convert to GGUF and quantize to Q4_K_M, the same format the live model uses.
#   bash merge_to_gguf.sh [adapter dir] [output name]
set -euo pipefail
ADAPTER=${1:-/home/bench/train/runs/qwen3-8b-pharmacy/adapter}
NAME=${2:-qwen3-8b-pharmacy-v1}
BASE=/opt/models/qwen3-8b-bf16
MERGED=/opt/models/$NAME-merged
IMG=ghcr.io/ggml-org/llama.cpp:full-cuda

echo "[1/3] merge $ADAPTER into $BASE -> $MERGED"
/opt/train-venv/bin/python - "$BASE" "$ADAPTER" "$MERGED" <<'PY'
import sys, torch
from transformers import AutoModelForCausalLM
from peft import PeftModel
base, adapter, out = sys.argv[1:]
model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.bfloat16, device_map={"": "cpu"})
model = PeftModel.from_pretrained(model, adapter).merge_and_unload()
model.save_pretrained(out, safe_serialization=True, max_shard_size="5GB")
print("merged ->", out)
PY
# the base tokenizer + chat template, byte for byte (transformers 5 rewrites tokenizer_config.json in a form the
# llama.cpp image's older transformers can't read)
rm -f "$MERGED/chat_template.jinja"
cp "$BASE"/{tokenizer.json,tokenizer_config.json,vocab.json,merges.txt} "$MERGED"/

echo "[2/3] convert to GGUF f16"
docker run --rm -v /opt/models:/models --entrypoint python3 $IMG /app/convert_hf_to_gguf.py \
  "/models/$NAME-merged" --outtype f16 --outfile "/models/$NAME-f16.gguf"

echo "[3/3] quantize to Q4_K_M"
docker run --rm -v /opt/models:/models --entrypoint /app/llama-quantize $IMG \
  "/models/$NAME-f16.gguf" "/models/$NAME-q4_k_m.gguf" Q4_K_M
rm -f "/opt/models/$NAME-f16.gguf"
ls -lh "/opt/models/$NAME-q4_k_m.gguf"
