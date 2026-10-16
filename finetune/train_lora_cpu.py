# -*- coding: utf-8 -*-
"""CPU LoRA fine-tune (demonstration / small-model experiment) on the tool-use SFT set.

No GPU here, so we can't use QLoRA (bitsandbytes/unsloth are CUDA-only). This does plain LoRA
with transformers+peft on CPU. Default base is the SMALL Qwen3-0.6B so it finishes on CPU; the
same script runs on Qwen3-4B with a GPU (set FT_BASE and run there).

Steps (what fine-tuning actually does):
  1. load base model + tokenizer on CPU (bf16)
  2. attach small trainable LoRA adapters; freeze the 4B/0.6B base
  3. format each example with the chat template (incl. tools + tool calls) into one text string
  4. tokenize; train the adapter to predict those tokens (loss goes down as it learns)
  5. save the adapter (a small file) under results/ft_cpu/adapter

Env knobs: FT_BASE (default Qwen/Qwen3-0.6B), FT_MAX_STEPS (default 70), FT_EPOCHS (default 1).
Run: python finetune/train_lora_cpu.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import (AutoModelForCausalLM, AutoTokenizer, DataCollatorForSeq2Seq,
                          Trainer, TrainerCallback, TrainingArguments)
from transformers.trainer_utils import get_last_checkpoint


class PrintLoss(TrainerCallback):
    """Print loss explicitly so it shows in the log (not hidden under the progress bar)."""

    def on_log(self, args, state, control, logs=None, **kw):
        if logs and "loss" in logs:
            print(f"step {state.global_step}/{state.max_steps}  loss={float(logs['loss']):.4f}",
                  flush=True)

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "finetune" / "data" / "sft_train.jsonl"
OUT = Path(os.environ.get("FT_OUT", ROOT / "results" / "ft_cpu"))
BASE = os.environ.get("FT_BASE", "Qwen/Qwen3-0.6B")
MAX_STEPS = int(os.environ.get("FT_MAX_STEPS", "70"))
EPOCHS = float(os.environ.get("FT_EPOCHS", "1"))
MAX_LEN = int(os.environ.get("FT_MAX_LEN", "768"))

torch.set_num_threads(os.cpu_count())


def main():
    print(f"[1/5] loading base {BASE} on CPU (bf16)...", flush=True)
    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(BASE, dtype=torch.bfloat16)
    model.config.use_cache = False

    print("[2/5] attaching LoRA adapters (freezing the base)...", flush=True)
    model = get_peft_model(model, LoraConfig(
        r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"]))
    model.print_trainable_parameters()

    print("[3/5] formatting examples + masking: learn ONLY the assistant's answer...", flush=True)
    rows = [json.loads(l) for l in TRAIN.read_text(encoding="utf-8").splitlines() if l.strip()]

    # Completion-only loss: the prompt (system + tools + history) is context, NOT something to
    # learn. Its labels are -100 (ignored); only the final assistant turn is scored. Without this
    # the model mostly learns to recite the ~1.5k-token system prompt and collapses.
    feats, skipped = [], 0
    for ex in rows:
        prompt = tok.apply_chat_template(ex["messages"][:-1], tools=ex["tools"], tokenize=False,
                                         add_generation_prompt=True, enable_thinking=False)
        full = tok.apply_chat_template(ex["messages"], tools=ex["tools"], tokenize=False,
                                       add_generation_prompt=False)
        if not full.startswith(prompt):
            skipped += 1
            continue
        p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
        a_ids = tok(full[len(prompt):], add_special_tokens=False)["input_ids"]
        ids = (p_ids + a_ids)[-MAX_LEN:]
        labels = ([-100] * len(p_ids) + a_ids)[-MAX_LEN:]
        feats.append({"input_ids": ids, "attention_mask": [1] * len(ids), "labels": labels})
    print(f"   {len(feats)} examples ready ({skipped} skipped: template prefix mismatch)", flush=True)
    ds = Dataset.from_list(feats)
    collator = DataCollatorForSeq2Seq(tok, padding=True, label_pad_token_id=-100)

    print(f"[4/5] training (<= {MAX_STEPS} steps, {EPOCHS} epoch)...", flush=True)
    args = TrainingArguments(
        output_dir=str(OUT / "checkpoints"), per_device_train_batch_size=1,
        gradient_accumulation_steps=8, learning_rate=1e-4, num_train_epochs=EPOCHS,
        max_steps=MAX_STEPS, logging_steps=5, save_strategy="steps", save_steps=25,
        save_total_limit=2, bf16=True, use_cpu=True, report_to=[], dataloader_num_workers=2,
        disable_tqdm=True)
    trainer = Trainer(model=model, args=args, train_dataset=ds, data_collator=collator,
                      callbacks=[PrintLoss()])
    ckpt_dir = OUT / "checkpoints"
    last = get_last_checkpoint(str(ckpt_dir)) if ckpt_dir.exists() else None
    if last:
        print(f"resuming from {last}", flush=True)
    trainer.train(resume_from_checkpoint=last)

    print(f"[5/5] saving adapter -> {OUT/'adapter'}", flush=True)
    (OUT / "adapter").mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(OUT / "adapter"))
    tok.save_pretrained(str(OUT / "adapter"))
    print("DONE. Adapter saved. Test with finetune/test_lora_cpu.py", flush=True)


if __name__ == "__main__":
    main()
