# -*- coding: utf-8 -*-
"""NAMAA-Saudi-TTS (Saudi dialect, MIT; Chatterbox multilingual) as a tiny local service, run
from its own venv so PyTorch never loads into the agent:

  /opt/tts/namaa-venv/bin/pip install fastapi uvicorn
  /opt/tts/namaa-venv/bin/uvicorn --app-dir adapter tts_server:app --host 127.0.0.1 --port 8095
  .env:  TTS_ENGINE=namaa   TTS_NAMAA_URL=http://127.0.0.1:8095

Measured on the 4-vCPU server: ~22 s per sentence (real-time factor ~8) - too slow for
conversation on CPU; use it on a GPU server (device picked automatically).
Optional voice: TTS_NAMAA_VOICE=/path/to/5-10s-clean-saudi-reference.wav
"""
from __future__ import annotations

import io
import os
import threading

import torch
import torchaudio as ta
from chatterbox import mtl_tts
from fastapi import FastAPI
from fastapi.responses import Response
from huggingface_hub import snapshot_download
from pydantic import BaseModel
from safetensors.torch import load_file

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
VOICE = os.environ.get("TTS_NAMAA_VOICE") or None

ckpt = snapshot_download(repo_id="NAMAA-Space/NAMAA-Saudi-TTS", repo_type="model")
model = mtl_tts.ChatterboxMultilingualTTS.from_pretrained(device=DEVICE)
model.t3.load_state_dict(load_file(f"{ckpt}/t3_mtl23ls_v2.safetensors", device=DEVICE))
model.t3.to(DEVICE).eval()
_lock = threading.Lock()
app = FastAPI(title="NAMAA Saudi TTS", docs_url=None, redoc_url=None)


class SynthIn(BaseModel):
    text: str
    lang: str = "ar"


@app.post("/synth")
def synth(m: SynthIn):
    with _lock, torch.inference_mode():
        wav = model.generate(m.text, language_id="ar" if m.lang != "en" else "en",
                             audio_prompt_path=VOICE)
    buf = io.BytesIO()
    ta.save(buf, wav.cpu(), model.sr, format="wav")
    return Response(buf.getvalue(), media_type="audio/wav")


@app.get("/health")
def health():
    return {"ok": True, "device": DEVICE}
