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
