# Pharmacy AI Agent

WhatsApp ordering assistant for a Saudi pharmacy franchise: customers write (Arabic dialects or
English) → opening menu + customer recognition (WhatsApp number → Odoo contact) → deterministic
safety guards → **Qwen3-4B-Instruct-2507** (Ollama, CPU) calls tools → **Odoo 18** (products,
branches, stock, prescriptions, quotations) → reply.

**Start here:** [docs/STATUS.md](docs/STATUS.md) (status, results, next steps) ·
[docs/RESTORE.md](docs/RESTORE.md) (rebuild on a new server) ·
[docs/operator-guide.md](docs/operator-guide.md) (day-to-day operation) ·
[docs/customer-flow.md](docs/customer-flow.md) (menu, customer recognition/registration, quotations, WhatsApp).

## Layout
| Path | What |
|---|---|
| `agent/` | conversation loop, system prompt, deterministic guards, opening menu (`menu.py`), customer recognition/registration (`customer.py`), CLI chat |
| `adapter/` | gateway over Odoo JSON-RPC (tools, customers, quotations), phonetic + active-ingredient search, prescription OCR checks, tests |
| `contract/` | JSON schemas of the 8 tools |
| `odoo/` | docker-compose (Odoo 18 + Postgres), `pharmacy_agent` module, sample-data seeder |
| `data_sources/sfda/` | SFDA drugs registry scraper, reference database, Odoo import file |
| `channels/webchat/` | WhatsApp-style test page (:8090), simulated WhatsApp number |
| `channels/whatsapp/` | Meta WhatsApp Cloud API webhook (dry run until credentials) |
| `evals/` | datasets (106 dialect messages) and runners |
| `finetune/` | CPU LoRA fine-tuning pipeline + datasets |
| `results/` | eval outputs |
| `scripts/bootstrap_server.sh` | one-command setup of a fresh server |

Quick start on a fresh Ubuntu 24.04 server: `sudo bash scripts/bootstrap_server.sh`.
