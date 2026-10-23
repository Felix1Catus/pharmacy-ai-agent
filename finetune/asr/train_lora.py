# -*- coding: utf-8 -*-
"""LoRA fine-tune of Whisper large-v3 for Gulf pharmacy Arabic: synthetic (NAMAA) + FLEURS ar.
Audio is decoded with soundfile (no torchcodec dependency).
  torchrun --nproc_per_node=2 train_lora.py  ->  /opt/asr-ft/out/adapter"""
import io
import json
import os

import evaluate
import librosa
import numpy as np
import soundfile as sf
import torch
from datasets import Audio, Dataset, load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (Seq2SeqTrainer, Seq2SeqTrainingArguments, WhisperForConditionalGeneration,
                          WhisperProcessor)

BASE = "openai/whisper-large-v3"
SYNTH = "/opt/asr-ft/data/synth/manifest.jsonl"
OUT = "/opt/asr-ft/out"
FLEURS_N = int(os.environ.get("FLEURS_N", "2000"))
processor = WhisperProcessor.from_pretrained(BASE, language="arabic", task="transcribe")

rows = []
for _l in open(SYNTH, encoding="utf-8"):
    _r = json.loads(_l)
    rows.append({"sentence": _r["text"], "path": _r["audio"], "bytes": None})
try:
    fl = load_dataset("google/fleurs", "ar_eg", split="train")
    fl = fl.select(range(min(FLEURS_N, len(fl)))).cast_column("audio", Audio(decode=False))
    for r in fl:
        a = r["audio"]
        rows.append({"sentence": r["transcription"], "path": a.get("path"), "bytes": a.get("bytes")})
    print("FLEURS added:", min(FLEURS_N, len(fl)), flush=True)
except Exception as e:
    print("FLEURS unavailable, synth only:", str(e)[:150], flush=True)

ds = Dataset.from_list(rows).train_test_split(test_size=0.05, seed=7)
print("train/eval:", len(ds["train"]), len(ds["test"]), flush=True)


def prep(b):
    arr, sr = sf.read(io.BytesIO(b["bytes"])) if b.get("bytes") else sf.read(b["path"])
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    if sr != 16000:
        arr = librosa.resample(arr, orig_sr=sr, target_sr=16000)
    b["input_features"] = processor.feature_extractor(arr, sampling_rate=16000).input_features[0]
    b["labels"] = processor.tokenizer(b["sentence"]).input_ids
    return b


ds = ds.map(prep, remove_columns=ds["train"].column_names, num_proc=2)

model = WhisperForConditionalGeneration.from_pretrained(BASE, torch_dtype=torch.float16)
model.config.forced_decoder_ids = None
model.config.suppress_tokens = []
model.config.use_cache = False
model.generation_config.language = "arabic"
model.generation_config.task = "transcribe"
model.generation_config.forced_decoder_ids = None
model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05,
                                         target_modules=["q_proj", "v_proj"]))
model.enable_input_require_grads()
model.print_trainable_parameters()

wer = evaluate.load("wer")


class Collator:
    def __call__(self, feats):
        inp = processor.feature_extractor.pad([{"input_features": f["input_features"]} for f in feats],
                                              return_tensors="pt")
        lab = processor.tokenizer.pad([{"input_ids": f["labels"]} for f in feats], return_tensors="pt")
        labels = lab["input_ids"].masked_fill(lab.attention_mask.ne(1), -100)
        if (labels[:, 0] == processor.tokenizer.bos_token_id).all().cpu().item():
            labels = labels[:, 1:]
        inp["labels"] = labels
        return inp


def metrics(p):
    pred, lab = p.predictions, p.label_ids
    pred = np.where(pred == -100, processor.tokenizer.pad_token_id, pred)
    lab = np.where(lab == -100, processor.tokenizer.pad_token_id, lab)
    pt = processor.tokenizer.batch_decode(pred, skip_special_tokens=True)
    lt = processor.tokenizer.batch_decode(lab, skip_special_tokens=True)
    return {"wer": 100 * wer.compute(predictions=pt, references=lt)}


args = Seq2SeqTrainingArguments(
    output_dir=OUT, per_device_train_batch_size=8, gradient_accumulation_steps=1,
    learning_rate=1e-3, warmup_steps=40, num_train_epochs=2, fp16=True,
    eval_strategy="no", save_strategy="no", logging_steps=20,
    predict_with_generate=False, report_to=[],
    remove_unused_columns=False, label_names=["labels"], ddp_find_unused_parameters=False,
    gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
    dataloader_num_workers=2)
trainer = Seq2SeqTrainer(model=model, args=args, train_dataset=ds["train"], eval_dataset=ds["test"],
                         data_collator=Collator(), compute_metrics=metrics,
                         processing_class=processor.feature_extractor)
trainer.train()
model.save_pretrained(f"{OUT}/adapter")
processor.save_pretrained(f"{OUT}/adapter")
print("ADAPTER SAVED ->", f"{OUT}/adapter", flush=True)
