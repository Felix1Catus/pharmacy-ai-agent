#!/usr/bin/env python3
"""QLoRA fine-tune of Qwen3-8B on the distillation set (distill/data), on all GPUs (one model copy per GPU).

Each example = the production prompt (system prompt + 5 customer tools, rendered by Qwen3's chat template with
thinking off, exactly what llama-server sends) + the teacher's first action as the completion; loss on the
completion only. Run:
  torchrun --nproc_per_node=2 train_qlora.py --max-steps 15            # speed test
  torchrun --nproc_per_node=2 train_qlora.py --epochs 2                # full run (resumes from checkpoints)
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import unsloth  # noqa: F401  (patches transformers before it is imported)
from unsloth import FastLanguageModel
import torch
from datasets import Dataset
from trl import SFTConfig, SFTTrainer
from transformers import TrainerCallback

BASE = os.environ.get("BASE_MODEL", "/opt/models/qwen3-8b-bnb4")
DATA = Path(os.environ.get("DISTILL_OUT", "/home/bench/distill/out"))
OUT = Path(os.environ.get("TRAIN_OUT", "/home/bench/train/runs/qwen3-8b-pharmacy"))
MAX_LEN = int(os.environ.get("MAX_LEN", "2304"))   # first step ~1,680 tokens, after a tool result up to ~1,920; answers <200


def completion(asst: dict) -> str:
    if asst.get("tool_calls"):
        fn = asst["tool_calls"][0]["function"]
        return ("<tool_call>\n" + json.dumps({"name": fn["name"], "arguments": fn["arguments"]}, ensure_ascii=False)
                + "\n</tool_call><|im_end|>")
    return asst["content"].strip() + "<|im_end|>"


def load(tok, name: str) -> Dataset:
    """<name>.jsonl (first step) + followup_<name>.jsonl (the step after a tool result), when present."""
    rows, skipped = [], 0
    for path in (DATA / f"{name}.jsonl", DATA / f"followup_{name}.jsonl"):
        if not path.exists():
            continue
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            prompt = tok.apply_chat_template(r["messages"][:-1], tools=r["tools"], add_generation_prompt=True,
                                             enable_thinking=False, tokenize=False)
            comp = completion(r["messages"][-1])
            if len(tok(prompt + comp)["input_ids"]) > MAX_LEN:      # would be cut: the answer is lost
                skipped += 1
                continue
            rows.append({"prompt": prompt, "completion": comp})
    if int(os.environ.get("LOCAL_RANK", 0)) == 0:
        print(f"[data] {name}: {len(rows)} examples ({skipped} over {MAX_LEN} tokens skipped)", flush=True)
    return Dataset.from_list(rows)


class Speed(TrainerCallback):
    def __init__(self):
        self.t0 = None

    def on_step_end(self, args, state, control, **kw):
        if state.global_step == 2:
            self.t0 = time.time()                                   # skip warm-up steps
        if self.t0 and state.global_step > 2 and int(os.environ.get("LOCAL_RANK", 0)) == 0:
            per = (time.time() - self.t0) / (state.global_step - 2)
            mem = max(torch.cuda.max_memory_allocated(i) for i in range(torch.cuda.device_count())) / 2**30
            left = (state.max_steps - state.global_step) * per
            print(f"[speed] step {state.global_step}/{state.max_steps}: {per:.1f} s/step, peak {mem:.1f} GiB, "
                  f"~{left / 3600:.1f} h left", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--accum", type=int, default=4)
    a = ap.parse_args()
    rank = int(os.environ.get("LOCAL_RANK", 0))
    model, tok = FastLanguageModel.from_pretrained(BASE, max_seq_length=MAX_LEN, load_in_4bit=True,
                                                   device_map={"": rank})
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=32, lora_dropout=0, bias="none", use_gradient_checkpointing="unsloth",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"], random_state=7)
    train, val = load(tok, "train"), load(tok, "val")
    cfg = SFTConfig(output_dir=str(OUT), per_device_train_batch_size=a.batch, gradient_accumulation_steps=a.accum,
                    per_device_eval_batch_size=a.batch, learning_rate=2e-4, lr_scheduler_type="cosine",
                    warmup_ratio=0.03, num_train_epochs=a.epochs, max_steps=a.max_steps, logging_steps=5,
                    eval_strategy="steps" if a.max_steps < 0 else "no", eval_steps=100, save_steps=100,
                    save_total_limit=3, bf16=True, optim="adamw_8bit", max_length=MAX_LEN,
                    completion_only_loss=True, ddp_find_unused_parameters=False, report_to=[], seed=7,
                    dataloader_num_workers=2)
    trainer = SFTTrainer(model=model, processing_class=tok, train_dataset=train, eval_dataset=val, args=cfg,
                         callbacks=[Speed()])
    resume = a.max_steps < 0 and any(OUT.glob("checkpoint-*"))
    trainer.train(resume_from_checkpoint=resume or None)
    if rank == 0 and a.max_steps < 0:
        model.save_pretrained(str(OUT / "adapter"))
        tok.save_pretrained(str(OUT / "adapter"))
        print("[done] adapter saved to", OUT / "adapter", flush=True)


if __name__ == "__main__":
    main()
