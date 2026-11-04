# Distillation: Claude as teacher for the Qwen3-8B router

Built 2026-11-02 to 04. Claude Opus 5.5 (through the Claude Code CLI on a Claude subscription) writes realistic
customer messages and labels the agent's first action; the result is fine-tuning data for the local model.

## Dataset (`data/`)
| File | What |
|---|---|
| `train.jsonl.gz` / `val.jsonl.gz` | 4,712 / 288 examples, same shape as `finetune/data/sft_train.jsonl` (messages + tools), each with the exact production system prompt and the 5 customer tools |
| `review.jsonl` | 475 labels the checks flagged (teacher unsure, or action not expected for the intent) |
| `owner_reviews_round1.jsonl` | the owner's verdicts and comments on 330 flagged labels (round 1) -> `teacher_rules.md` |
| `state.db.gz` | the whole pipeline state (messages, captures, labels) to resume or regenerate: `gunzip -k data/state.db.gz && mv data/state.db .` |
| `student_gap*_summary.json` | the live 8B vs the teacher, by intent / dialect / action |
| `stats.json` | counts at the end of the run |

Mix of accepted examples: symptom requests 806, store info 699, order status 677, medical advice 421, unclear 288,
restricted drugs 277, misspelt products 276, complaints 268, out of scope 240, first orders 135, stock/price 108,
reserves 76, manipulation 59. 4,142 generated messages never reach the model (menu, guards, direct search) and
are left out.

## How it works (`distill.py`)
generate (13 intents x 9 dialects, real catalogue names) -> capture (the real agent with a stub model; only
messages that would reach the model are kept, with the exact prompt/tools) -> label (teacher, owner's rules) ->
validate (tool exists, arguments match the contract schema, no leaked internals, right language, fits the
intent) -> export. Resumable from `state.db`; sleeps through Claude usage limits.

```bash
CLAUDE_CONFIG_DIR=/opt/claude-distill /opt/claude-cli/claude      # once: /login with the Claude account, /exit
cp -r . /opt/agent_snapshot && sed -i "s|DISTILL_REPO=.*|DISTILL_REPO=/opt/agent_snapshot|" /etc/systemd/system/pharmacy-distill.service
systemctl daemon-reload && systemctl start pharmacy-distill       # freeze the agent first (above) so prompts don't drift
python3 -I distill/dashboard.py                                   # live progress
.venv/bin/python distill/distill.py status
.venv/bin/python distill/distill.py reset-labels                  # after changing the agent prompt or teacher rules
.venv/bin/python distill/student_gap.py --sample 500              # where the live model disagrees with the teacher
```
The CLI binary: copy the `claude` executable to `/opt/claude-cli/claude` (any Claude Code install).

## Review page
`review_page/` (index.html + items.json) is the round-1 review tool (published as a private claude.ai artifact).
