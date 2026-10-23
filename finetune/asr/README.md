# Gulf-Arabic ASR fine-tune (Whisper large-v3, LoRA)

Dialect-focused LoRA fine-tune of `openai/whisper-large-v3` for Saudi/Gulf pharmacy voice input.
Serves via faster-whisper from the ctranslate2 export at `/opt/asr-ft/ct2` (`ASR_MODEL` in `.env`).

## Pipeline (run on the GPU box, venv `/opt/asr-ft`)
1. `gen_par.py` — NAMAA voices the data across both GPUs (run 2 NAMAA instances; `NAMAA_ENDPOINTS`):
   catalogue Arabic drug names in carrying phrases + Gulf order/dialect templates -> wavs + manifest.
2. `prefetch.py` — cache whisper-large-v3 weights + FLEURS `ar_eg`.
3. `torchrun --nproc_per_node=2 train_lora.py` — LoRA r=16 (q/v), synth + FLEURS, soundfile decode,
   gradient checkpointing (fits 16 GB), fp16. Adapter -> `/opt/asr-ft/out/adapter`.
4. `convert_ct2.py` — merge LoRA -> ctranslate2 float16 -> `/opt/asr-ft/ct2` (copy tokenizer files in).
5. `ab_eval.py` — A/B vs base large-v3 on a held-out NAMAA Gulf set.

## Result (held-out NAMAA Gulf clips, 2 epochs)
WER 42.8% (base) -> 12.8% (fine-tuned); drug-name recall 18/18 both. Gain is dialect.
Caveat: test audio is NAMAA TTS (optimistic); validate on real customer voice. Catalogue has only
42 Arabic drug names, so drug-name gains come from hotwords/#1, dialect gains from this fine-tune.

## Serving
`.env`: `ASR_MODEL=/opt/asr-ft/ct2`, `ASR_GPU=1` (keep ASR off GPU0 where NAMAA lives).
