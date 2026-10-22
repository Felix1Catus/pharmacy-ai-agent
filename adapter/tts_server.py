# -*- coding: utf-8 -*-
"""NAMAA-Saudi-TTS (Saudi dialect, MIT; Chatterbox multilingual) as a tiny local service, run
from its own venv so PyTorch never loads into the agent:

  /opt/tts/namaa-venv/bin/pip install fastapi uvicorn
  /opt/tts/namaa-venv/bin/uvicorn --app-dir adapter tts_server:app --host 127.0.0.1 --port 8095
  .env:  TTS_ENGINE=namaa   TTS_NAMAA_URL=http://127.0.0.1:8095

Precision (GPU): T3 (the language model that fixes the Saudi accent / speech tokens) stays
full fp32, so the tokens are byte-identical to the fp32 reference and the accent is unchanged;
only the S3Gen vocoder, which just renders those tokens to a waveform, runs in bf16. Measured
on an NVIDIA A16: fp32 1.46 s / bf16 0.75 s for S3Gen, so a sentence drops from ~4.7 s (RTF
1.10) to ~4.0 s (RTF 0.93 - real time) with no audible change. bf16 keeps fp32's exponent
range (unlike fp16), and the vocoder output is cast back to fp32 for watermarking. Override
with TTS_NAMAA_VOCODER_DTYPE = bf16 | fp16 | fp32.

Measured on the 4-vCPU server: ~22 s per sentence (real-time factor ~8) - too slow for
conversation on CPU; use it on a GPU server (device picked automatically).
Optional voice: TTS_NAMAA_VOICE=/path/to/5-10s-clean-saudi-reference.wav
"""
from __future__ import annotations

import functools
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
_DTYPES = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}
VOCODER_DTYPE = _DTYPES.get(os.environ.get("TTS_NAMAA_VOCODER_DTYPE", "bf16").lower(), torch.bfloat16)

ckpt = snapshot_download(repo_id="NAMAA-Space/NAMAA-Saudi-TTS", repo_type="model")
model = mtl_tts.ChatterboxMultilingualTTS.from_pretrained(device=DEVICE)
model.t3.load_state_dict(load_file(f"{ckpt}/t3_mtl23ls_v2.safetensors", device=DEVICE))
model.t3.to(DEVICE).eval()

# fp32 T3 + reduced-precision vocoder: wrap only s3gen.inference in autocast, leaving the T3
# token generation (where the accent lives) and the watermark step in fp32. The vocoder's
# output is forced back to fp32 so generate()'s numpy/watermark path never sees bf16.
if DEVICE == "cuda" and VOCODER_DTYPE is not torch.float32:
    _orig_s3_inference = model.s3gen.inference

    @functools.wraps(_orig_s3_inference)
    def _s3_autocast(*args, **kwargs):
        with torch.autocast("cuda", dtype=VOCODER_DTYPE):
            out = _orig_s3_inference(*args, **kwargs)
        if isinstance(out, tuple):
            head = out[0].float() if torch.is_tensor(out[0]) else out[0]
            return (head,) + tuple(out[1:])
        return out.float() if torch.is_tensor(out) else out

    model.s3gen.inference = _s3_autocast

# internal bot: skip the Perth audio watermark (CPU numpy, ~1 s/synth) - it adds nothing here
try:
    model.watermarker.apply_watermark = lambda wav, sample_rate=None: wav
except Exception:
    pass

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
    return {"ok": True, "device": DEVICE,
            "vocoder_dtype": str(VOCODER_DTYPE).replace("torch.", "")}
