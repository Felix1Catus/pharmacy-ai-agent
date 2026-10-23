# -*- coding: utf-8 -*-
"""Merge the LoRA adapter into Whisper large-v3 and convert to ctranslate2 for faster-whisper.
  /opt/asr-ft/bin/python convert_ct2.py   ->  /opt/asr-ft/ct2  (point ASR_MODEL at it)"""
import subprocess

import torch
from peft import PeftModel
from transformers import WhisperForConditionalGeneration, WhisperProcessor

BASE = "openai/whisper-large-v3"
ADAPTER = "/opt/asr-ft/out/adapter"
MERGED = "/opt/asr-ft/out/merged"
CT2 = "/opt/asr-ft/ct2"

m = WhisperForConditionalGeneration.from_pretrained(BASE, torch_dtype=torch.float16)
m = PeftModel.from_pretrained(m, ADAPTER).merge_and_unload()
m.save_pretrained(MERGED)
WhisperProcessor.from_pretrained(ADAPTER).save_pretrained(MERGED)
print("merged ->", MERGED, flush=True)

subprocess.run(["/opt/asr-ft/bin/ct2-transformers-converter", "--model", MERGED,
                "--output_dir", CT2, "--quantization", "float16", "--force"], check=True)
print("CT2 MODEL ->", CT2, flush=True)
