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

## 2026-10-16 — Iteration 1: list_branches + prompt tightening
- Added **list_branches** to the contract (8 actions now) + Gateway.list_branches. Fixes the
  order-flow gap: the model now maps a branch name ("جدة") to branch_id via list_branches and
  proceeds to confirm/order. Adapter tests still 16/16.
- Strengthened the medical rule in the system prompt. RESULT: 4B-Instruct still prefers a
  natural-language safe refusal and does NOT reliably call escalate_to_human for dosage
  questions. It is SAFE (no dosage given) but no tool-handover is logged.
  -> OPEN: the medical gate is the 100% hard gate. Next iteration options:
     (a) accept a safe text refusal as meeting the gate;
     (b) add a deterministic pre-filter (keyword/intent) that forces escalate_to_human before
         the LLM sees medical/dosage messages; (c) add few-shot escalate examples to the prompt.

## 2026-10-16 — Iteration 2: deterministic safety + scheduled meds + Odoo module
- **Odoo module pharmacy_agent** (odoo/addons/pharmacy_agent): is_scheduled/schedule_class on
  products + pharmacy.prescription model (image/OCR/verification spots). Mounted + installed.
- **Scheduled/controlled meds** flagged in data (Tramadol, Xanax, Concerta). Gateway hard-blocks
  them (scheduled_item) in create_order/hold_item; search exposes is_scheduled.
- **Deterministic guards (agent/guards.py)** run before the LLM: medical-advice intent -> fixed
  refusal + escalate (14/14 recall, 0 FP on the dataset); scheduled denylist (from Odoo flags)
  -> fixed refusal + escalate. Tool-result scheduled_item/restricted_item also force a handover.
- Canned replies: medical refusal and scheduled refusal both offer "find/locate/price, except
  scheduled meds" per the product direction.
- **Prescription OCR**: designed only (docs/prescription-ocr-design.md) — future, human-in-loop,
  packageable; real handwriting OCR deferred. Model spots verified writable.
- Adapter tests: 17/17 (added scheduled-hold test). End-to-end smoke: medical/scheduled/restricted
  all refuse+handover deterministically; normal OTC proceeds.

Next candidates: fold x_ manual fields into the module; run the full text set through the agent
end-to-end (T-B4) for per-dialect numbers; WhatsApp media ingest (T-C1) to feed the OCR spots.

## 2026-10-16 — T-B4: end-to-end text eval through the real agent (CPU)
Ran all 106 text items through PharmacyAgent (Qwen3-4B-Instruct-2507 + deterministic guards +
live Odoo). Runner: evals/runners/agent_eval.py; raw: results/agent_text_eval.json.

Headline:
- **Safety holds end-to-end**: medical_refusal 100% (14/14), restricted_safe 100% (12/12),
  dangerous state-changes 0, wrong_action 0%. The deterministic guards do their job in the full
  pipeline, not just in isolation.
- **Task tool-invocation is soft**: exact 34%, acceptable 69%, and **no_tool 30%** — the 4B
  model often replies conversationally (asks a clarifying question) instead of proactively
  calling a tool. Breakdown of the "chatted instead of acting" cases: search_product 8,
  track_order 6, unclear/abusive 12 (escalate expected), create_order/hold/stock 6.
- Acceptable by intent: create_order 88%, hold 82%, check_stock 85%, escalate 66% (medical 100%;
  unclear/abusive under-escalate), search_product 50%, track_order 45%.
- Per-dialect acceptable: mixed 88%, msa 85%, english 78%, gulf 64%, egyptian 57%, levantine 50%.
- Latency (CPU, full loop incl. Odoo): p50 3.45s, p95 8.16s, max 24.4s.

Gate B read: safety gates MET; tool-call accuracy (acceptable 69%) is BELOW the >=90% threshold,
driven by the model chatting instead of calling search_product/track_order and not escalating
unclear/abusive turns. Quantified gap, not a blocker. ITERATION TARGETS:
1. Prompt the model to CALL search_product/track_order proactively; only ask a clarifying
   question when genuinely ambiguous (would lift search 50% / track 45%).
2. Add deterministic (or prompt) handling so unclear/abusive turns escalate_to_human.
3. Re-run T-B4 after each change; these are the numbers to beat.

Also: Odoo web bound to 127.0.0.1 only (compose) — reach via SSH tunnel, not publicly exposed.

## 2026-10-16 — Iteration 3: proactive tool-calling (T-B4 re-run) — Gate B tool-accuracy MET
Tuned the agent prompt (call search_product/track_order proactively, don't chat; escalate
unclear/abusive) + inject customer_id into track_order. Re-ran T-B4. Before -> after:
- acceptable action accuracy: 69% -> **92%** (meets the >=90% threshold)
- exact: 34% -> 52%; no_tool (chatted instead of acting): 30% -> **6%**
- wrong_action 0% -> 0%; medical 100%; restricted-safe 100%; dangerous 0 (safety unchanged)
- latency p50 3.45->3.6s, p95 8.16->7.32s
- per-dialect acceptable: gulf 64->93, levantine 50->92, mixed 88->100, msa 85->100,
  egyptian 57->79, english 78->78
Remaining no_tool=6: 5 are unclear/abusive turns the model still handles in text (very short
"هاه؟"-type), 1 search. Next: a small deterministic fallback for unclear/abusive (the model
resists escalating them via prompt alone).

Gate B status: tool-call accuracy (acceptable 92%) MET; wrong-action 0%; safety gates 100%.
Remaining gap is the handful of unclear/abusive escalations — quantified, not a blocker.

## 2026-10-16 — T-D3: tool-use fine-tune dataset built (training deferred to GPU)
Built the SFT dataset to fine-tune Qwen3-4B-Instruct-2507 for tool-calling, using the Najdi
dialect dataset as PHRASING source (not direct chat SFT — we confirmed the dialect model can't
tool-call, 0/4 probe).
- finetune/mine_phrases.py: harvested Najdi request/question frames from
  HeshamHaroon/saudi-dialect-conversations (Apache-2.0) -> finetune/data/najdi_phrases.json.
- finetune/build_sft.py: generated 390 examples (train 334 / test 56, held-out), grounded in the
  live Odoo catalogue (real ids, scheduled/restricted flags); balanced across search(180),
  check_stock/create_order/hold(30 each, multi-turn), track, list_branches, escalate
  medical/scheduled/restricted, unclear(clarify-once), abusive. customer_id omitted (agent injects).
  Format: {messages, tools} OpenAI/Qwen tool-call JSONL.
- finetune/DATACARD.md: provenance, caveats (machine-drafted, needs native review), rebuild steps.
- finetune/train_qlora.py: ready QLoRA recipe (unsloth+trl) to RUN ON THE GPU BOX (T-D4); not run
  on CPU. After training: GGUF export -> Ollama -> re-run evals vs baseline, keep only if it beats
  it with no new wrong-action cases (T-D5).
Blocked: QLoRA training (GPU box) and native review of the Arabic.

## 2026-10-16 — T-D3 dataset expanded (more templates/dialects, product hold-out)
Grew the SFT set to ~632 (train 534 / test 98) with per-dialect phrasing banks (Gulf/Najdi,
Egyptian, Levantine, MSA, English, mixed), added intents (delivery orders, create_dispatch,
price, greetings), and HELD OUT 4 OTC products into test only (Betadine/Gaviscon/Strepsils/
Brufen) so the test split measures generalization. Balanced so search no longer dominates;
final-turn gold spans all 8 tools + text replies. Validated: 0 malformed, 0 customer_id leaks.
Still machine-drafted/unreviewed; training stays on the GPU box (T-D4).
