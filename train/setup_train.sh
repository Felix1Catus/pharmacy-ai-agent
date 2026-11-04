#!/usr/bin/env bash
# Training environment for QLoRA on the A16s: its own venv (/opt/train-venv) with Unsloth + PyTorch CUDA,
# and the pre-quantized Qwen3-8B base from Hugging Face. Kept apart from the agent and NAMAA venvs.
set -uo pipefail
L=/home/bench/logs
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$L/progress.log"; }
fail(){ log "FAILED: $*"; exit 1; }
log "== Train setup: venv + Unsloth =="
[ -d /opt/train-venv ] || python3 -m venv /opt/train-venv || fail "venv"
/opt/train-venv/bin/pip install -q --upgrade pip >/dev/null
/opt/train-venv/bin/pip install -q unsloth >"$L/pip_train.log" 2>&1 || fail "unsloth install (see $L/pip_train.log)"
log "installed: $(/opt/train-venv/bin/python -c 'import torch, unsloth, trl, peft, transformers; print("torch", torch.__version__, "cuda", torch.cuda.is_available(), "| unsloth", unsloth.__version__, "| trl", trl.__version__, "| transformers", transformers.__version__)' 2>&1 | tail -1)"
log "downloading base model unsloth/Qwen3-8B-unsloth-bnb-4bit"
/opt/train-venv/bin/python -c "
from huggingface_hub import snapshot_download
p = snapshot_download('unsloth/Qwen3-8B-unsloth-bnb-4bit', local_dir='/opt/models/qwen3-8b-bnb4')
print('base model at', p)" 2>&1 | tail -1 | tee -a "$L/progress.log"
du -sh /opt/models/qwen3-8b-bnb4 | tee -a "$L/progress.log"
log "== Train setup done =="
