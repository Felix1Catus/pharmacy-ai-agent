# Project status — Pharmacy AI Agent (as of 2026-10-17)

Repo: https://github.com/Felix1Catus/pharmacy-ai-agent (private) · server: Vultr 155.138.207.131
(32 vCPU / 125 GB RAM, Ubuntu 24.04, **no GPU**). Rebuild elsewhere: `docs/RESTORE.md`.

## What is running now
| Component | State |
|---|---|
| **Agent LLM** | `Qwen3-4B-Instruct-2507` (Q4_K_M GGUF, non-thinking) via **Ollama 0.40.5** on CPU |
| **Agent** (`agent/`) | tool-calling loop over an 8-action contract, deterministic safety guards, audit log |
| **Odoo 18** (Docker) | db `pharmacy`, **2,056 products** (2,014 SFDA + 42 sample), 3 branches; public on :8069 (hardened passwords, DB manager off); module `pharmacy_agent` 18.0.1.4.0 |
| **Test chat** | WhatsApp-style page http://155.138.207.131:8090 (password in `.env`), systemd `pharmacy-webchat` |
| **SFDA Update List** | Inventory → Products → Update List (manual button + auto every N days, **auto OFF**) |

## Headline results (agent end-to-end eval, 106 dialect messages)
| Configuration | Acceptable action | Safety (medical / restricted-safe / dangerous) |
|---|---|---|
| Prompt-only baseline, before tuning | 69% | 100% / 100% / 0 |
| Prompt-tuned (sample catalogue) | 92% | 100% / 100% / 0 |
| + full SFDA catalogue + phonetic search | 92% | 100% / 100% / 0 |
| **+ prescription policy (current production)** | **93%** | **100% / 100% / 0** |
Per dialect (current): gulf 95 · levantine 92 · mixed 100 · msa 100 · english 89 · **egyptian 79**.

## What was built (by area)
- **Model selection (CPU):** benchmarked Qwen3 0.6B/1.7B/4B/8B; 0.6B/1.7B fail safety; default
  `qwen3:4b` unusable (won't stop "thinking"); chose the non-thinking 4B Instruct-2507.
  Rejected swaps: ASL-4B-v1 / Qwen3.5-saudi-dialect (new `qwen3_5` arch, no tool calling — the
  dialect GGUF emitted 0/4 tool calls).
- **Safety (deterministic, independent of the model):** medical-advice pre-filter (14/14 recall,
  0 false positives); Scheduled-medicine block list from Odoo; gateway refuses orders/holds of
  Scheduled and prescription drugs (incl. SFDA flag); Scheduled drugs get no stock/price; a
  search that only matches Scheduled drugs is refused.
- **Policy:** OTC = full service; prescription-only = availability + price, purchase handed to a
  pharmacist; Scheduled (`مقيد` / `خاضعة للرقابة وفق أحكام وشروط خاصة`) = fully blocked.
- **Search:** exact match + **phonetic Arabic↔English** matching (`adapter/fuzzy_search.py`):
  "سرفاركس" → CERVARIX VACCINE, "بندول" → Panadol; greetings/chat words match nothing.
- **Odoo:** Scheduled checkbox + Pharmacy tab, 19 SFDA registry fields, Prescriptions model
  (spots for future OCR), **Update List** SFDA sync (chunked, resumable background runs; ticks
  Scheduled automatically, never unticks — logs "REVIEW").
- **SFDA data:** scraped pages 0–100 (2,020 entries → 2,014 drugs); reference database + Odoo
  import file (`data_sources/sfda/data/`).
- **Fine-tuning on CPU (experiments, none adopted):** LoRA on the 4B — held-out bare-model
  accuracy 65% → 87% (round 2) → **97% (round 3)**, but in the full agent 88% / 81% vs 92%
  (over-escalation / repeated searching when results are empty). Lesson: the training data lacks
  "read results → answer" and "nothing found → say so" turns. Adapters in Release `models-v1`.
- **Portability:** `scripts/bootstrap_server.sh` rebuilds everything on a fresh server; pinned
  `requirements*.txt`; secrets generated locally, never in git.

## Open items needing the owner (blockers)
1. **Meta WhatsApp Cloud API** test number + credentials (for the real WhatsApp channel).
2. **Real voice notes** with correct transcripts (50+) — for Whisper / voice testing.
3. **Native-speaker review** of the machine-drafted Arabic eval + training data.
4. **Domain name** — to put HTTPS in front of Odoo and the chat page.

## Recommended next steps (in order)
1. **Quick data fixes (minutes):** re-import the current `sfda_products_odoo_import.xlsx` so the
   30 "خاضعة للرقابة…" drugs become Scheduled too (live has 48 Scheduled, should be 78); set Update
   List pages to **0–458**; then decide whether to switch on automatic updates.
2. **Security before any real customer traffic:** HTTPS (domain + reverse proxy), SSH key-only
   login, keep :8069/:8090 closed or behind the proxy.
3. **WhatsApp channel (T-B3):** Meta Cloud API webhook with signature check + a local simulator
   (buildable now; plug in credentials when available). This is the step that makes it real.
4. **Agent quality:** re-validate the parked `wip/clarify-once` branch on current `main` (merge
   only if ≥ 93%); grow the eval set beyond 106 messages, especially **Egyptian** (weakest, 79%)
   and multi-turn order flows; native review.
5. **Voice (Sprint C):** WhatsApp voice notes → Whisper (CPU-feasible with faster-whisper) →
   same agent; spoken replies later (needs the owner's real recordings).
6. **Optional fine-tune round 4:** only if gaps remain after 4 — add post-search reply and
   empty-result examples.
7. **Parked:** owner / ops-manager assistant (read-only Odoo analytics); prescription OCR
   (design in docs/prescription-ocr-design.md); real Odoo 18 production integration.
