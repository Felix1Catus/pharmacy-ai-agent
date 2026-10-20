# Project status — Pharmacy AI Agent (as of 2026-10-20)

Repo: https://github.com/Felix1Catus/pharmacy-ai-agent (private) · server: Vultr 155.138.193.148
(4 vCPU / 16 GB RAM, Ubuntu 24.04, **no GPU**; the earlier 32-vCPU server was 155.138.207.131).
Rebuild elsewhere: `docs/RESTORE.md`. Customer / ordering flow: `docs/customer-flow.md`.

## What is running now
| Component | State |
|---|---|
| **Agent LLM** | `Qwen3-4B-Instruct-2507` (Q4_K_M GGUF, non-thinking) via Ollama on CPU — all logical CPUs, kept loaded, warmed at start |
| **Agent** (`agent/`) | tool-calling loop over an 8-action contract + fixed-logic layers: opening menu, customer recognition/registration, safety guards, prescription checks, audit log |
| **Odoo 18** (Docker) | db `pharmacy`, ~4,000 products (SFDA + 42 sample), 3 branches; public on :8069; module `pharmacy_agent` **18.0.1.7.0** (Prescriptions, WhatsApp orders, contact WhatsApp number) |
| **Test chat** | http://155.138.193.148:8090 (password in `.env`), systemd `pharmacy-webchat`; simulated WhatsApp number field |
| **WhatsApp channel** | `channels/whatsapp/webhook.py` built and tested in **dry run** (no Meta credentials yet; not running as a service) |
| **SFDA Update List** | Inventory → Products → Update List (manual button + auto every N days, **auto OFF**) |

## Headline results (agent end-to-end eval, 106 dialect messages)
| Configuration | Acceptable action | Safety (medical / restricted-safe / dangerous) |
|---|---|---|
| Prompt-only baseline, before tuning | 69% | 100% / 100% / 0 |
| Prompt-tuned (sample catalogue) | 92% | 100% / 100% / 0 |
| + full SFDA catalogue + phonetic search | 92% | 100% / 100% / 0 |
| + prescription policy (2026-10-17) | **93%** | **100% / 100% / 0** |
Per dialect (2026-10-17): gulf 95 · levantine 92 · mixed 100 · msa 100 · english 89 · **egyptian 79**.
⚠️ **Not re-run since 2026-10-19** (prescription photos, guard rails, menu, ingredient search,
customer flow). Automated tests: 159 passing on the server.

## Policy (current)
- **OTC:** full service — search, stock, price, quotation.
- **Prescription-only (يحتاج وصفة طبية):** availability + price; ordering allowed once the customer
  sends a **verified prescription photo/PDF** (computerised, dated within 5 days, invoice/Rx number,
  the medicine on it); otherwise the chat asks for it. One prescription = one order. Same active
  ingredient(s) at the prescribed strength are accepted (generic substitution).
- **Scheduled (مقيد / خاضعة للرقابة):** fully blocked — never shown to the model, never unlocked by a
  prescription, fixed refusal + pharmacist handover.
- **Orders are quotations** in Sales: a human confirms/invoices them (pickup) or approves them for
  delivery (future Delivery Dispatcher).

## What was built (by area)
- **Customer recognition + registration** (`agent/customer.py`, 2026-10-20): WhatsApp number →
  Odoo contact → greeted by name; menu «👤 عميل حالي / جديد»: phone → saved profile (confirm /
  edit address) or registration (name → address → alt phone → confirm → contact created); a
  guest's confirmed order / prescription / tracking request runs it first, then resumes.
- **WhatsApp quotations:** AI orders are draft quotations tagged WhatsApp with fulfilment,
  delivery address and dispatch status (Pharmacy → WhatsApp orders).
- **WhatsApp channel** (`channels/whatsapp/`): Meta Cloud API webhook — verification, signature
  check, text / list / button / image / document; menu as a list message; dry run until credentials.
- **Opening menu + language** (`agent/menu.py`): new order · track order · questions · customer ·
  Arabic/English (all fixed replies and the model follow the choice).
- **Guard rails:** prompt-injection and off-topic checks before the model; output filter for leaked
  internals and dosage wording; dosage questions in every dialect; orders/holds only after the
  customer's explicit "yes"; max 10 per item; customer id always the conversation's own; tracking
  and dispatch only on the customer's own orders.
- **Prescription photos/PDFs** (`adapter/prescription.py`): Tesseract OCR (ara+eng) or the PDF text
  layer; checks date / invoice number / medicines (by name or active ingredient + strength).
- **Active-ingredient search** (`adapter/ingredients.py`): ingredient queries rank single-ingredient
  products by form/strength; brand queries return same-ingredient alternatives; an unavailable
  strength is reported; the model may not invent ingredients (only the customer's or the prescription's).
- Earlier: model selection (CPU), phonetic Arabic↔English search, Odoo Scheduled flags + SFDA fields
  + Update List sync, SFDA data, CPU fine-tuning experiments (adapters in Release `models-v1`),
  `scripts/bootstrap_server.sh` for a fresh server.

## Open items needing the owner (blockers)
1. **Meta WhatsApp Cloud API** test number + credentials (`WHATSAPP_*` in `.env`) and a **domain
   with HTTPS** (Meta only calls https webhooks).
2. **Real prescriptions** (personal data blurred) to tune OCR on real phone photos / layouts.
3. **Real voice notes** with correct transcripts (50+) — for Whisper / voice testing.
4. **Native-speaker review** of the machine-drafted Arabic eval + training data.
5. **Pharmacist review of catalogue flags** (e.g. some amoxicillin brands are not marked prescription-only).

## Recommended next steps (in order)
1. **Re-run the 106-message eval** on the current build and fix regressions.
2. **Security before real customers:** HTTPS (domain + reverse proxy), SSH key-only login, keep
   :8069/:8090 closed or behind the proxy.
3. **Go live on WhatsApp:** add credentials, run `channels/whatsapp/webhook.py` as a service behind
   HTTPS, register the webhook in Meta.
4. **Delivery Dispatcher module** (drivers accept jobs, pickup/drop-off, live tracking, mark
   delivered) — `sale.order.dispatch_status` already carries the hand-off states.
5. **Agent quality:** grow the eval set (Egyptian, multi-turn order flows); re-validate the parked
   `wip/clarify-once` branch.
6. **Voice (Sprint C):** WhatsApp voice notes → Whisper → same agent.
