# Pharmacy AI Agent — AI layer

Ordering agent for a pharmacy franchise: WhatsApp (Arabic dialect text + voice notes)
→ Whisper → Qwen3 (tool calls) → Odoo (via a contract adapter) → text / voice reply.

**This phase** builds and evaluates the AI layer. Odoo is a **real local Odoo 18** instance
(see `docs/decisions.md` — human overrode the original "mock Odoo" plan), reached through a
thin FastAPI **contract adapter** so the agent only ever speaks the tool contract.

## Layout
- `docs/` — progress, decisions, sources, thresholds
- `contract/` — tool JSON schemas (one per action) + contract.md
- `odoo/` — docker-compose stack, config, seed script (dummy pharmacy data)
- `adapter/` — FastAPI app implementing the tool contract over Odoo JSON-RPC
- `agent/` — conversation state, tool-call loop, guardrails, handover
- `channels/whatsapp/` — webhook, media download, simulator
- `speech/` — stt.py, tts.py, audio utils
- `evals/` — datasets, runners, scoring
- `results/` — JSON eval outputs

## Quick start
    cp .env.example .env            # fill secrets
    cd odoo && docker compose up -d # start Odoo 18 + Postgres
    # see docs/progress.md for current state
