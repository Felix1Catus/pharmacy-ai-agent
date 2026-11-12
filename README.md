# Pharmacy AI Agent

WhatsApp ordering assistant for a Saudi pharmacy franchise: customers write (Arabic dialects or
English) → opening menu + customer recognition (WhatsApp number → Odoo contact) → deterministic
safety guards → the model (**Qwen3-8B** on llama-server with a GPU, or **Qwen3-4B-Instruct-2507**
on Ollama/CPU) calls tools → **Odoo 18** (products, branches, stock, prescriptions, quotations;
local or on another server) → reply.

**Start here:** [docs/HANDOVER.md](docs/HANDOVER.md) (current state, tool calls, open items) ·
[docs/odoo-connection.md](docs/odoo-connection.md) (Odoo local / remote, service account) ·
[docs/STATUS.md](docs/STATUS.md) (results and history up to 2026-10-21) ·
[docs/RESTORE.md](docs/RESTORE.md) (rebuild on a new server) ·
[docs/operator-guide.md](docs/operator-guide.md) (day-to-day operation) ·
[docs/customer-flow.md](docs/customer-flow.md) (menu, customer recognition/registration, quotations, WhatsApp).

## Layout
| Path | What |
|---|---|
| `agent/` | conversation loop, system prompt, deterministic guards, opening menu (`menu.py`), customer recognition/registration (`customer.py`), CLI chat |
| `adapter/` | gateway over Odoo JSON-RPC (tools, customers, quotations), phonetic + active-ingredient search, prescription OCR checks, tests |
| `contract/` | JSON schemas of the model's tools (listed in docs/HANDOVER.md) |
| `odoo/` | docker-compose (Odoo 18 + Postgres), `pharmacy_agent` module, sample-data seeder |
| `data_sources/sfda/` | SFDA drugs registry scraper, reference database, Odoo import file |
| `channels/webchat/` | WhatsApp-style test page (:8090), simulated WhatsApp number |
| `channels/whatsapp/` | Meta WhatsApp Cloud API webhook + notifier (dry run until credentials) |
| `tests/` | regression tests for the hardening round (identity, prescriptions, holds, WhatsApp) |
| `deploy/` | GPU-server bootstrap, systemd units, HTTPS proxy template |
| `evals/` | datasets (106 dialect messages) and runners |
| `finetune/` | CPU LoRA fine-tuning pipeline + datasets |
| `results/` | eval outputs |
| `scripts/` | server bootstrap, Odoo service account, log retention, local Odoo on Windows/WSL |

Quick start on a fresh Ubuntu 24.04 server: `sudo bash scripts/bootstrap_server.sh`
(GPU server: `sudo bash deploy/bootstrap_gpu.sh`).

## Tests
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # everything; tests that need Odoo / OCR / voice models skip without them
ruff check agent adapter channels odoo/odoo_client.py scripts conftest.py tests
```
With an Odoo to test against (docs/odoo-connection.md §5) the whole suite runs: 365 tests.
CI (`.github/workflows/tests.yml`) runs the lint + unit tests, and the full suite against Odoo 18.
