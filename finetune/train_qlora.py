# -*- coding: utf-8 -*-
"""QLoRA fine-tune of Qwen3-4B-Instruct-2507 on the tool-use SFT set (T-D4).

RUN ON THE GPU BOX (L40S), NOT on the CPU workstation. Requires:
    pip install "unsloth[cu121]" trl peft bitsandbytes accelerate datasets
(or the unsloth install matching the box's CUDA). This script is intentionally not run in the
CPU phase; it is the ready recipe for when the GPU is available.

What it does:
  - loads the base model in 4-bit, attaches a LoRA adapter
  - trains on finetune/data/sft_train.jsonl (messages + tools, chat-template formatted)
  - saves the adapter and a merged 16-bit model under results/ft/
  - prints where to go next (eval + GGUF export)

After training: convert the merged model to GGUF (llama.cpp convert_hf_to_gguf.py), quantize to
Q4_K_M, import into Ollama, and RE-RUN the evals (evals/runners/agent_eval.py, qwen_baseline.py)
to compare against the Qwen3-4B-Instruct-2507 baseline. Keep the fine-tune only if it beats the
baseline and adds no new wrong-action cases (T-D5).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "finetune" / "data" / "sft_train.jsonl"
OUT = ROOT / "results" / "ft"
BASE = "unsloth/Qwen3-4B-Instruct-2507"  # or the exact base id available on the GPU box

# QLoRA hyperparameters (starting point; tune on the box)
MAX_SEQ_LEN = 4096
LORA_R = 16
LORA_ALPHA = 32
LR = 2e-4
EPOCHS = 2
BATCH = 2
GRAD_ACCUM = 8


def load_examples(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main():
    try:
        from unsloth import FastLanguageModel
        from trl import SFTConfig, SFTTrainer
        from datasets import Dataset
    except ImportError as e:  # noqa: BLE001
        raise SystemExit(
            "Missing training deps — run this on the GPU box after:\n"
            "  pip install 'unsloth[cu121]' trl peft bitsandbytes accelerate datasets\n"
            f"(import error: {e})")

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=BASE, max_seq_length=MAX_SEQ_LEN, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=LORA_R, lora_alpha=LORA_ALPHA,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth")

    rows = load_examples(TRAIN)

    def to_text(ex):
        # render messages (with tools) via the model's chat template, including tool calls
        return {"text": tokenizer.apply_chat_template(
            ex["messages"], tools=ex["tools"], tokenize=False,
            add_generation_prompt=False)}

    ds = Dataset.from_list(rows).map(to_text)

    OUT.mkdir(parents=True, exist_ok=True)
    trainer = SFTTrainer(
        model=model, tokenizer=tokenizer, train_dataset=ds,
        args=SFTConfig(output_dir=str(OUT / "checkpoints"), dataset_text_field="text",
                       max_seq_length=MAX_SEQ_LEN, per_device_train_batch_size=BATCH,
                       gradient_accumulation_steps=GRAD_ACCUM, learning_rate=LR,
                       num_train_epochs=EPOCHS, logging_steps=10, save_steps=200,
                       bf16=True, report_to=[]))
    stats = trainer.train()
    (OUT / "train_stats.json").write_text(
        json.dumps({"loss_history": getattr(stats, "metrics", {})}, indent=2))

    model.save_pretrained(str(OUT / "adapter"))
    tokenizer.save_pretrained(str(OUT / "adapter"))
    try:
        model.save_pretrained_merged(str(OUT / "merged_16bit"), tokenizer, save_method="merged_16bit")
    except Exception as e:  # noqa: BLE001
        print("merged save skipped:", e)

    print(f"done -> {OUT}")
    print("NEXT: convert merged_16bit -> GGUF (llama.cpp), quantize Q4_K_M, import to Ollama,")
    print("      then re-run evals/runners/agent_eval.py and qwen_baseline.py vs the baseline (T-D5).")


if __name__ == "__main__":
    main()
