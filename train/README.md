# Fine-tuning Qwen3-8B on the distillation set

QLoRA (Unsloth) on the teacher-labelled data in `distill/data/`, on both GPUs (one model copy per GPU, DDP).
Tested 2026-11-04/05 on 2x NVIDIA A16 16 GB: 4 examples per GPU x 2 accumulation steps (16 per step),
~48 s/step, 8.9 GiB peak per GPU; 2 epochs of 4,712 examples = 590 steps = ~8 h.

| File | What |
|---|---|
| `setup_train.sh` | `/opt/train-venv` (Unsloth + PyTorch CUDA) and the 4-bit base `unsloth/Qwen3-8B-unsloth-bnb-4bit` |
| `train_qlora.py` | the trainer: production prompt (system prompt + tools, thinking off) as the prompt, the teacher's action as the completion, loss on the completion only; reads `<split>.jsonl` + `followup_<split>.jsonl`; resumes from checkpoints |
| `merge_to_gguf.sh` | adapter -> merged into the full-precision `Qwen/Qwen3-8B` (CPU) -> GGUF -> Q4_K_M, the live model's format |
| `val_agreement.py` | first-action agreement with the teacher on a held-out file, for whatever llama-server on :8080 serves |
| `pipeline_v2.sh` | the whole thing unattended (systemd `deploy/systemd/pharmacy-pipeline-v2.service`): follow-up labels -> data -> train -> merge -> eval -> live stack restored |

```bash
bash train/setup_train.sh
torchrun --nproc_per_node=2 train/train_qlora.py --max-steps 15 --batch 4 --accum 2     # speed test
TRAIN_OUT=runs/x DISTILL_OUT=distill/out torchrun --nproc_per_node=2 train/train_qlora.py --epochs 2 --batch 4 --accum 2
bash train/merge_to_gguf.sh runs/x/adapter qwen3-8b-pharmacy-x
```
Training needs both GPUs to itself: stop `pharmacy-webchat`, `pharmacy-tts-namaa` and the `pharmacy-llm` container
first (the pipeline does this and brings them back).

## v1 (2026-11-05): first action only - not adopted
Held-out first action matches the teacher 96.2% (current Qwen3-8B: 74.3%; medical advice 23/24 vs 5/24,
order status 63/63 vs 37/63, complaints 4/5 vs 0/5). But every training example ended right after the
customer's message, so the model never learned to stop after a tool result: in the agent eval it averaged
4.2 tool calls per message (current: 1.0), often hit the 7-call limit and placed orders nobody asked for.
The agent eval's first-action `wrong_action` metric missed it; `evals/runners/agent_eval.py` now also reports
`unrequested_state_change_rate` and `avg_tool_calls` over the whole chain, and has a customer mode
(`EVAL_MODE=customer`), the path WhatsApp / webchat customers take.

## v2: adds the step after a tool result
`distill/followup.py`: for each first-step `track_order` / `list_branches` label (the only tool results a
customer-chat model sees: search results go to the fixed order wizard, handovers end the turn), the real agent
runs the teacher's call against Odoo and the next model request is labelled by the teacher (normally the reply).
Not made live automatically: compare in `runs/v2/REPORT.md` first.
