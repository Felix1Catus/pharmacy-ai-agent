# -*- coding: utf-8 -*-
"""Pre-download the heavy assets so the training run doesn't stall: Whisper large-v3 (HF weights
+ processor) and a FLEURS Arabic subset."""
import os

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
from datasets import load_dataset
from transformers import WhisperForConditionalGeneration, WhisperProcessor

print("downloading whisper-large-v3 processor+weights...", flush=True)
WhisperProcessor.from_pretrained("openai/whisper-large-v3", language="arabic", task="transcribe")
WhisperForConditionalGeneration.from_pretrained("openai/whisper-large-v3")
print("whisper-large-v3 ready", flush=True)

print("downloading FLEURS ar_eg ...", flush=True)
try:
    d = load_dataset("google/fleurs", "ar_eg", split="train", trust_remote_code=True)
    print("FLEURS ar_eg train size:", len(d), flush=True)
except Exception as e:
    print("FLEURS download FAILED:", str(e)[:200], flush=True)
print("PREFETCH_DONE", flush=True)
