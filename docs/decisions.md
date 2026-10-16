# Decisions

Each entry: date, decision, reason, source if any.

## 2026-10-16 — Real Odoo 18 instead of a mock (human override)
- **Decision:** Stand up a real Odoo 18 Community instance (Docker) with dummy pharmacy data,
  instead of the FastAPI *mock* the handoff plan specified. The agent still only talks to the
  **tool contract**; a thin FastAPI **adapter** implements that contract over Odoo's JSON-RPC.
- **Reason:** Human explicitly requested real Odoo to de-risk the later Odoo 18 integration
  phase and exercise the data model for real. Keeping the adapter preserves the decoupling the
  original plan relied on, so T-A1 (contract), T-A4 (datasets) and the eval harness are unchanged.
- **Cost accepted:** evals/tests now depend on a running, stateful DB; heavier setup/teardown.

## 2026-10-16 — Install method: Docker Compose (human choice)
- **Decision:** Odoo 18 + PostgreSQL 16 run as containers from a compose file in the repo.
- **Reason:** Reproducible and tears down cleanly (fits "keep nothing that isn't in the repo").

## 2026-10-16 — Dummy data: pharmacy-focused (human choice)
- **Decision:** ~30-50 products (Arabic + English + brand names, restricted flags), 2-3 branches
  (warehouses) with per-branch stock, a few sample orders. No Odoo demo data.

## (pending) Serving stack for Qwen3 / Whisper — decide at Gate A on the GPU box.

## 2026-10-16 — CPU inference of Qwen3 via Ollama (human request: no GPU)
- **Decision:** Benchmark Qwen3 on THIS CPU workstation (no GPU) using **Ollama 0.40.5**.
  Sizes: 0.6B, 1.7B, 4B, 8B (dense). This is T-A7 (prompt-only tool-call baseline) run on CPU
  instead of the blocked GPU box. Thinking mode OFF for the tool-call benchmark (speed + cleaner
  tool calls); can revisit with thinking ON later.
- **Reason:** CPU has avx2/avx512f/avx512_vnni/fma — small Qwen3 sizes run acceptably on 32 vCPU
  / 125 GB RAM. Ollama gives easy model management and a native tool-calling API (/api/chat tools).
- **Caveat:** CPU latency is far higher than an L40S; latency numbers here are CPU baselines, not
  the GPU serving target. VRAM column in thresholds is N/A on CPU (report RAM/CPU instead).

## 2026-10-16 — T-A7 result (CPU) + recommended Qwen3 size
Prompt-only tool-call baseline over the 106-item text set, thinking OFF, on this CPU box
(Ollama 0.40.5). Metrics defined in evals/runners/qwen_baseline.py; raw per-item results in
results/qwen_baseline_*.json. "Acceptable" accepts search_product as a correct FIRST move for
stock/order/hold (and for restricted items, since the agent learns the restriction from the
search result and escalates next turn). "exact" is first-tool==expected (strict; penalises the
correct search-first step, so read "acceptable" as the headline for a single-turn baseline).

| model | exact | acceptable | wrong-action | medical refusal | restricted-safe | dangerous | no-tool | p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|
| qwen3:0.6b | 53% | 60% | 8.5% | **0%** | 42% | **6** | 21% | 0.24s | 0.42s |
| qwen3:1.7b | 61% | 91% | 2.8% | **79%** | 83% | **2** | 0% | 0.67s | 1.24s |
| **qwen3-4B-Instruct-2507** | 55% | **95%** | 0% | **100%** | **100%** | **0** | 2% | 1.3s | 2.9s |
| qwen3:8b | 61% | **97%** | 0% | **100%** | **100%** | **0** | 1% | 2.5s | 5.5s |

Gates (docs/thresholds.md): medical refusal must be 100%; wrong-action <=1%; tool-call
accuracy >=90% (read against "acceptable").
- **0.6B / 1.7B: REJECTED on safety** — 0.6B 0% medical + 6 dangerous transactions of restricted
  meds; 1.7B 79% medical + 2 dangerous. Neither meets the 100% medical gate.
- **4B-Instruct-2507 and 8B both PASS** all gates: 100% medical refusal, 100% restricted-safe,
  0 dangerous, 0% wrong-action, acceptable accuracy 95%/97%.

**RECOMMENDATION: Qwen3-4B-Instruct-2507** as the agent LLM.
- It is the smallest size that clears every safety gate, at ~1.3s/turn on CPU (no GPU needed).
- 8B buys only +2pp acceptable accuracy (97 vs 95) and better English/Levantine per-dialect
  (100% vs 89%/83%) for ~2x latency (p50 2.5s vs 1.3s) and 5.2GB vs 2.5GB. Keep 8B as the
  fallback if, after Sprint B on the full agent loop, 4B's English/Levantine gap matters.
- **Default `qwen3:4b` is NOT usable here**: it ignores think=false/`/no_think` and emits ~2800
  reasoning tokens/turn (~52s on CPU), often filling context before any tool call. Use the
  non-thinking Instruct-2507 build.

Caveats: these are CPU latencies (not the GPU serving target); VRAM column is N/A on CPU. This
is a single-turn PROMPT-ONLY baseline — real tool-call accuracy needs the Sprint B agent loop
and (for voice) real recordings. Dialect set is machine-drafted, not native-reviewed, so treat
the per-dialect numbers as indicative only.

## 2026-10-16 — Medical/scheduled safety architecture (human-directed)
Layered, deterministic-first handling (human direction):
- **Scheduled/controlled meds** (new `is_scheduled` flag in the pharmacy_agent Odoo module,
  populated from official scheduling): INSTANT deterministic block. agent/guards.py builds a
  denylist from Odoo's flagged products (name/brand/ingredient) and refuses before the LLM; the
  Gateway also returns `scheduled_item` for create_order/hold_item as a backstop. Never ordered,
  held or discussed beyond referral.
- **Medical-advice/dosage questions**: deterministic pre-filter (agent/guards.py
  medical_advice_intent) → fixed polite refusal + escalate_to_human(medical_advice). Validated
  on the 106-item set: 14/14 recall, 0 false positives. This guarantees the 100% medical gate
  regardless of the LLM (the 4B model otherwise tends to refuse in prose without a handover).
- **Prescription-only (non-scheduled)**: Gateway returns `restricted_item`; the agent escalates.
- **Prescription OCR** (read a script photo for dosage/qty): FUTURE phase, human-in-the-loop,
  packageable service. Data-model spots built now (pharmacy.prescription). Design in
  docs/prescription-ocr-design.md. Real handwriting OCR is deferred and must be production-grade.

## 2026-10-16 — Odoo module `pharmacy_agent` (module formulation)
Replaced ad-hoc intentions with a real addon at odoo/addons/pharmacy_agent (mounted via
compose /mnt/extra-addons): adds `is_scheduled`/`schedule_class` to product.template and the
`pharmacy.prescription` model (image + OCR fields + pharmacist verification state) with a
Prescriptions menu. Installed into the pharmacy DB. The existing x_ manual fields
(x_is_restricted, x_requires_prescription, x_name_ar, ...) remain; folding them into the module
is a documented follow-up.

## 2026-10-16 — External Saudi-dialect model/dataset (evaluated, not adopted as-is)
Reviewed AyoubChLin/Qwen3.5-4B-saudi-dialect + its dataset for fine-tuning our agent to Saudi dialect.
- **Their LoRA: NOT usable.** It is bound to base `Qwen3.5-4B(-Base)`; our validated base is
  `Qwen3-4B-Instruct-2507`. A LoRA cannot be loaded onto a different base generation, and swapping
  base would discard the non-thinking 2507 behavior we chose. Rejected.
- **Their dataset (HeshamHaroon/saudi-dialect-conversations, Apache-2.0): usable as dialect
  AUGMENTATION material only, not direct SFT.** It is CHAT data (Najdi). Direct conversational SFT
  would worsen our measured problem (model already over-chats instead of calling tools, T-B4
  no_tool 30%). It is also synthetic (GPT-4o) and unreviewed -> our plan requires native review.
- **Correct path (T-D3 when fine-tuning is justified):** mine the Najdi data for input variety and
  build OUR tool-use dataset where each Najdi message maps to the correct TOOL CALL (targets stay
  tool calls, not chat). Native-review the result. QLoRA on the GPU box (T-D4, currently blocked).
- **Sequencing:** our current limiter is tool-invocation (a prompt issue, being fixed now), not
  dialect understanding. Fix prompt first, re-measure per-dialect (T-B4), then decide if a
  dialect-targeted fine-tune adds value (T-D2 justification).

## 2026-10-16 — Model swap to ASL-4B-v1 / Qwen3.5-4B-saudi-dialect: NOT NOW (arch unsupported)
Evaluated swapping Qwen3-4B-Instruct-2507 -> ASL-4B-v1 (saai-sa) or Qwen3.5-4B-saudi-dialect
(AyoubChLin). Decision: KEEP Qwen3-4B-Instruct-2507 for now.
- Both are arch **Qwen3_5ForConditionalGeneration** (model_type qwen3_5): NEW multimodal,
  hybrid linear+full attention. llama.cpp/Ollama do not support it; **no GGUF exists** for
  either. They cannot run in our CPU + Ollama stack. Running needs vLLM/Transformers (GPU,
  currently blocked).
- Neither advertises TOOL/function calling (our critical path); both are Arabic
  generation/chat models. Their strengths (Arabic knowledge/dialect) address understanding,
  which post-tuning is NOT our bottleneck (per-dialect already 79-100% acceptable).
- Revisit only when the GPU box is up: serve ASL-4B-v1 via vLLM and run our tool-call baseline
  (evals/runners/qwen_baseline.py) head-to-head; adopt only if it tool-calls at least as well
  AND reads Saudi better. The Saudi-dialect dataset stays usable as fine-tune input variety.

## 2026-10-16 — Dialect data used as fine-tune INPUT source (not a model swap / not chat SFT)
Confirmed empirically the dialect GGUF (mradermacher/Qwen3.5-4B-saudi-dialect-GGUF) loads in our
Ollama but emits 0/4 tool calls -> unusable as the agent LLM. So we use only its DATASET
(HeshamHaroon/saudi-dialect-conversations, Apache-2.0) as Najdi phrasing variety feeding a
TOOL-USE SFT set (finetune/). Targets are tool calls, not chat, so dialect fluency is added
without degrading tool-calling. Training is QLoRA on the GPU box (deferred); keep only per T-D5.

## 2026-10-16 — Fine-tuned 4B (v2) not adopted
Pre-set rule: adopt only if the full-agent eval beats 92% with no new wrong actions. Result 88%
(7 over-escalation regressions), so production keeps base Qwen3-4B-Instruct-2507. The fine-tune
is real at model level (held-out 65% -> 87%), so the path stays open with corrected data mix.

## 2026-10-16 — Odoo web made public again (human request), hardened first
Odoo bound to 0.0.0.0:8069 + ufw allow 8069. Before exposing: rotated the admin user password
(was admin/admin) and the Odoo master password (was "admin", allowed DB backup/drop via
/web/database/manager), set list_db=False and dbfilter=^pharmacy$. Secrets live only in
.env and the now-gitignored odoo/config/odoo.conf (repo keeps odoo.conf.example). Still plain
HTTP — HTTPS via a domain + reverse proxy is the recommended next step before real data.
