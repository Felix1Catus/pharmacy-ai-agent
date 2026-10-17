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

## 2026-10-16 — T-D4 on CPU: LoRA fine-tune of Qwen3-0.6B (no GPU) — pipeline proven
No GPU, so QLoRA (CUDA-only) wasn't possible; ran plain LoRA (transformers+peft, bf16 on CPU)
on the small Qwen3-0.6B to prove the pipeline end-to-end (finetune/train_lora_cpu.py).
- Run 1 FAILED instructively: loss was computed over the whole sequence, so the model learned
  to recite the ~1.5k-token system prompt (loss -> 0.005) and collapsed, emitting "<tools></tools>"
  (held-out 52% -> ~12%). FIX: completion-only loss (labels=-100 on prompt; only the final
  assistant turn is scored), lr 1e-4, 70 steps (~1 epoch). Also: detached + checkpoint every 25
  steps + auto-resume (run 0 was killed at step 64 by a session restart).
- Run 2 (masked): 30 min, loss 0.77 -> ~0.06. Held-out test (98, incl. 4 unseen products):
  **base 52% -> fine-tuned 71%**. search_product 32/43 -> 43/43, create_dispatch 0/3 -> 3/3,
  create_order 5/13 -> 8/13, escalate 1/16 -> 5/16; regressions: text replies 3/4 -> 0/4,
  track 3/5 -> 2/5. Dominant new error: over-calling search_product (search is ~45% of data).
- Verdict: pipeline works on CPU and generalizes; 0.6B+FT is NOT a replacement for the 4B agent
  (92% acceptable, 100% safety). Next: rebalance/upweight escalation + text classes, then run the
  same recipe on Qwen3-4B (est. ~3.5 h on this CPU for 70 steps). Results: results/ft_cpu/eval.json.

## 2026-10-16 — Fine-tune v2: rebalanced data, 0.6B re-check, then Qwen3-4B on CPU
- Rebalanced train set (test set kept byte-identical): search 23%, escalation 30%, text 12%.
- 0.6B v2 held-out: 72% (v1 71%); text replies fixed (0/4 -> 3/4), escalation unchanged (5/16).
- **4B LoRA on CPU**: 70 steps in 96 min (loss 1.34 -> 0.018). Exported merge -> GGUF f16 ->
  Q4_K_M -> Ollama `pharmacy-4b-ft` (90 s; reuses production template/params).
- **Held-out (bare model): base 65% -> fine-tuned 87%** (create_order 0/13 -> 13/13,
  check_stock 4/7 -> 7/7, escalate 9/16 -> 12/16; hold_item 0/6 both; text 4/4 -> 2/4).
- **T-B4 full agent, committed code (the 92% run): fine-tuned 88% vs baseline 92% -> NOT
  ADOPTED.** Safety identical (100%/100%/0 dangerous, 0% wrong-action); no-tool 6% -> 1%;
  egyptian 79->86, english 78->89, but gulf 93->79. 7 regressions, all OVER-ESCALATION of plain
  requests (sanitizer, masks, ORS order, Gaviscon delivery, Omega hold); p95 latency 7.3 -> 24.6 s.
- Diagnosis: escalation upweighted to 30% overcorrected for the 4B (it already escalated well);
  plus 768-token truncation hides the system prompt, and training used a different system prompt
  than the live agent. Next try: escalation ~12-15%, symptom->search examples, agent's real
  prompt, longer max length. hold_item 0/6 for every model -> inspect those labels.
Production unchanged: AGENT_MODEL = Qwen3-4B-Instruct-2507 (base).

## TO-DO (current — see docs/STATUS.md for the full picture)
Done since the list was made: fine-tune round 3 (not adopted), gateway blocks SFDA prescription
drugs, SFDA import into live (by owner), phonetic search, prescription policy, test chat page,
GitHub repo + bootstrap.
1. ✅ 2026-10-17 **Data fixes:** live Odoo now 78 Scheduled (45 مقيد + 30 "خاضعة للرقابة…" +
   3 sample); Update List pages 0–458. Still to decide: switch on automatic updates.
2. **Security:** HTTPS (needs a domain), SSH key-only login, review public ports 8069/8090.
3. **WhatsApp channel (T-B3):** Cloud API webhook + signature check + local simulator.
4. **Clarify-once branch** (`wip/clarify-once`): rebase on main, re-test, T-B4; merge if ≥ 93%.
5. **Eval growth + native review:** more messages (Egyptian 79% weakest, multi-turn flows).
6. **Voice (Sprint C):** Whisper ingest of voice notes (needs owner recordings), TTS later.
7. Optional fine-tune round 4 (post-search reply + empty-result examples).
8. Parked: owner / ops-manager assistant; prescription OCR; Odoo production integration.

## 2026-10-16 — SFDA drugs registry scraped + Odoo import file
Scraped SFDA drugs-list pages 0-100 + all 2,020 details pages (4 parallel workers, 0 failures).
Built data_sources/sfda/data/: full reference DB (xlsx/csv) and an Odoo import (xlsx/csv) with
external IDs; المراقبة=مقيد -> Scheduled (45), يحتاج وصفة -> prescription (1,768); 2,014 unique
drugs. New pharmacy_agent 18.0.1.2.0 fields (19 sfda_*) + SFDA registry section on the Pharmacy
tab. Verified on a scratch DB: 2,014 imported, 0 errors, 45/45 Scheduled, re-import idempotent.
NOT imported into the live DB yet (waits for TO-DO 1b gateway fix + round-3 eval).

Update: owner decided 'خاضعة للرقابة وفق أحكام وشروط خاصة' also ticks Scheduled -> 75 Scheduled (45 مقيد + 30); re-verified on a fresh scratch DB (75/75, 0 errors).

## 2026-10-16 — Odoo "Update List" (SFDA auto/manual sync) — pharmacy_agent 18.0.1.3.0
Inventory/Sales > Products > Update List: manual "Check for updates now" (background cron
trigger) + automatic every N days (OFF by default). Creates new SFDA drugs, updates changed sfda_*
fields, auto-ticks Scheduled but never unticks (REVIEW log instead), keeps own sale prices.
Models pharmacy.sfda.updater (singleton settings) + pharmacy.sfda.run (history/log). Tested on a
scratch DB (create, idempotent re-sync, change detection, newly-scheduled, relaxed->review,
file-import consistency = 0 changes, button via background worker). Installed on live with no
data imported (42 products before/after); owner will do the file import.

## 2026-10-16 — Fine-tune round 3 (4B, CPU, full-length, agent prompt) — NOT adopted
Data: agent's real system prompt, escalation 13%, first-move + symptom/category searches,
confirm-before-hold, full-length examples (2,304 tokens; v2 truncated to 768 = ~60% cut).
66 steps on CPU (~4.5 h incl. contention), loss 1.0 -> 0.008. Export -> pharmacy-4b-ft3; probe 4/4.
**T-B4 agent eval (committed 92% code): 81% acceptable** (baseline 92%, round 2 88%); safety
unchanged (100%/100%/0, 0% wrong-action); p50 6.9 s, p95 28 s; 2.4 tool calls/msg vs 0.9.
Cause: all 15 regressions = search_product -> (empty result) -> search again -> escalate "unclear".
The model does search first now, but the training data has (almost) no "read results -> answer
the customer" turns and no "nothing found -> say so" turns, so it never learned to stop calling
tools. Also: catalogue misses like "معقم لليدين" vs "معقم اليدين" (no fuzzy search yet).
Next (if fine-tuning resumes): add post-search reply + empty-result examples; first implement fuzzy
product search (T-C2), which helps every model. Production stays on base Qwen3-4B-Instruct-2507.

## 2026-10-17 — Full SFDA DB imported (2,056 products): fuzzy search + prescription policy
Owner imported the SFDA file. Arabic queries for English-named drugs failed (e.g. "سرفاركس" vs
CERVARIX VACCINE — stored in English only). Fix in the search TOOL, not the model:
adapter/fuzzy_search.py = phonetic, script-independent consonant keys (CERVARIX = سرفاركس =
"srfrks"), exact matches first then "spelling variant" matches; chat words match nothing.
Policy (owner): prescription-only drugs -> availability + price allowed, purchase handed to a
pharmacist; Scheduled -> fully blocked (no stock/price; search matching only Scheduled refused).
Agent eval T-B4 (106 msgs):
| | 42 sample prod | 2,056 + fuzzy | + policy |
|---|---|---|---|
| acceptable | 92% | 92% | **93%** |
| wrong-action / medical / restricted-safe / dangerous | 0 / 100 / 100 / 0 | same | same |
| p50 latency | 3.6 s | 4.4 s | 5.1 s |
Policy run: 2 fixed (Centrum order, pregnancy-test hold), 0 broken. Per-dialect: gulf 95,
levantine 92, mixed 100, msa 100, english 89, egyptian 79. New production state = commit 50dc09d.
