# Progress log

> Keep this short. Update at the end of each task/sprint: what was done, the numbers,
> what is blocked on the human, what you recommend next. Nothing is "done" unless its
> acceptance check ran and its result file exists.

## BLOCKED (needs the human — see docs/blockers for detail)
- **Target dialect(s)** — assuming Saudi/Gulf Arabic + English until confirmed. [A]
- **Real voice notes + transcripts** (50+, noisy/quiet, numbers, drug/brand names). [A, C]
- **Native-speaker review** of dialect text + fine-tuning dataset. [A, D]
- **Product list sample** (Arabic + English) — using a machine-drafted pharmacy set until provided. [A]
- **Meta WhatsApp Cloud API** test-number credentials. [B]
- **GPU server (L40S 48 GB)** started + SSH access — Whisper/Qwen3/TTS/fine-tune all wait on this. [A onward]

All GPU-dependent benchmarks (T-A5 Whisper, T-A6 TTS, T-A7 Qwen3) are blocked on the GPU box.
Proceeding with everything that does not depend on the above.

---

## 2026-10-16 — Environment & Sprint A kickoff (new workstation)
- Rebuilt the project fresh on a new Vultr workstation (32 vCPU / 125 GB RAM, Ubuntu 24.04).
  Previous workstation was 4 vCPU / 7.7 GB. GPU work still offloaded to the L40S box over SSH.
- Installed: ffmpeg, python3.12-venv, Docker engine 29.9.1 + compose plugin.
- **Scope change (human-approved):** replaced the planned *mock* Odoo with a **real Odoo 18**
  instance in Docker, fronted by a FastAPI contract adapter. See docs/decisions.md.

## 2026-10-16 — Sprint A (buildable parts) complete
Done (acceptance checks ran):
- **T-A1 contract** — 7 action schemas + loader + contract.md. Loader returns the Qwen/OpenAI tools shape.
- **T-A2 thresholds** — docs/thresholds.md committed *before* any benchmark (plan rule 4).
- **T-A3 (revised to real Odoo)** — Odoo 18 + Postgres in Docker, 42 products (35 OTC / 7 restricted),
  3 branches with per-branch stock, demo customer, sample orders. FastAPI contract adapter over Odoo
  JSON-RPC implements all 7 actions with guardrails (restricted items refused) and typed errors.
  **16/16 adapter integration tests pass.**
- **T-A4 test sets** — evals/datasets/text_messages.json (106 dialect messages: gulf 42, egyptian 14,
  msa 13, levantine 12, mixed 16, english 9; actions: search 16, stock 13, order 17, hold 11, track 11,
  escalate 38 [medical 14 / restricted 12 / unclear 6 / abusive 6]). voice_manifest.json (12 items) +
  recording_script.md for the human. Everything tagged `machine-drafted`.

Blocked (unchanged — all need the human, section 4):
- **GPU box (L40S)** → T-A5 Whisper, T-A6 TTS, T-A7 Qwen3 sizes cannot run. Gate A stays open on these.
- **Real voice notes + transcripts** → audio files in voice_manifest are placeholders (audio_recorded:false).
- **Native review** of the Arabic text/voice drafts before any accuracy number counts.
- **Dialect confirmation + product list** → assumed Saudi/Gulf + English; catalogue is machine-drafted.

Recommend next (when unblocked): start the GPU box and run T-A5/A6/A7; get native review of the
datasets; then Gate A (recommend a Qwen size + a TTS with numbers). Meanwhile Sprint B (agent loop,
guardrails, WhatsApp simulator, text evals) can start against the adapter — it does not need the GPU.

## 2026-10-16 — T-A7 Qwen3 baseline run on CPU (GPU box still unavailable)
Ran the prompt-only Qwen3 tool-call baseline on THIS CPU workstation via Ollama (human asked to
try Qwen locally, no GPU). Sizes 0.6B/1.7B/4B/8B over the 106-item text set, thinking off.
- **0.6B, 1.7B fail the safety gates** (medical refusal 0% / 79%; dangerous transactions 6 / 2).
- **Qwen3-4B-Instruct-2507 and 8B both pass** (100% medical + restricted-safe, 0 dangerous,
  0% wrong-action, 95% / 97% acceptable action accuracy). 4B ~1.3s/turn, 8B ~2.5s/turn on CPU.
- **Recommended size: Qwen3-4B-Instruct-2507** (smallest that clears every gate; CPU-friendly).
  Details + table in docs/decisions.md. Results: results/qwen_baseline_*.json.
- Finding: default `qwen3:4b` is unusable here (won't stop "thinking"); used the non-thinking
  Instruct-2507 build. Stack: Ollama 0.40.5, CPU-only.

Note on Gate A: the model-size recommendation is now evidence-backed on CPU. Whisper (T-A5) and
TTS (T-A6) remain blocked on the GPU box + real voice notes, so Gate A is not fully closed.

## 2026-10-16 — Sprint B T-B1/T-B2: agent loop on Qwen3-4B-Instruct-2507 (CPU)
Implemented the text agent on the recommended model and cleaned up (removed unused Ollama
models 0.6B/1.7B/8B + default qwen3:4b, freeing ~9 GB; re-pullable). Agent:
- agent/llm.py: Ollama client for AGENT_MODEL (non-thinking).
- agent/agent.py: conversation state, tool-calling loop over the 7 contract actions via the
  Gateway, max tool calls/turn with escalate-on-overrun, customer_id injected by the agent,
  append-only audit log (results/audit.jsonl). Guardrails: restricted refused by Gateway ->
  escalate; medical -> escalate; unclear/abusive -> escalate.
- agent/chat.py: REPL / one-shot driver for iteration.

End-to-end smoke (3 scenarios) findings (ITERATION BACKLOG):
1. Restricted (اوجمنتين) -> escalate_to_human. WORKS.
2. Medical dosage -> model refuses in TEXT but does NOT call escalate_to_human (no handover).
   Safe, but should escalate; tighten prompt / add a check.
3. Order pickup BLOCKED: no way to map a branch NAME ("جدة") to a branch_id. The contract has
   no branch-lookup; check_stock returns branch ids but the flow is unreliable. -> add a
   list_branches tool (next iteration).

Next: add list_branches to the contract+gateway so pickup orders resolve; then run the full
text set through the agent (T-B4) for per-dialect end-to-end numbers (stub was prompt-only).
