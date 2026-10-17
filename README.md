# Pharmacy AI Agent

WhatsApp ordering assistant for a Saudi pharmacy franchise: customers write (Arabic dialects or
English) → deterministic safety guards → **Qwen3-4B-Instruct-2507** (Ollama, CPU) calls tools →
**Odoo 18** (products, branches, stock, orders) → reply.

**Start here:** [docs/STATUS.md](docs/STATUS.md) (status, results, next steps) ·
[docs/RESTORE.md](docs/RESTORE.md) (rebuild on a new server) ·
[docs/operator-guide.md](docs/operator-guide.md) (day-to-day operation).

## Layout
| Path | What |
|---|---|
| `agent/` | conversation loop, system prompt, deterministic guards, CLI chat |
| `adapter/` | gateway implementing the tool contract over Odoo JSON-RPC, phonetic search, tests |
| `contract/` | JSON schemas of the 8 tools |
| `odoo/` | docker-compose (Odoo 18 + Postgres), `pharmacy_agent` module, sample-data seeder |
| `data_sources/sfda/` | SFDA drugs registry scraper, reference database, Odoo import file |
| `channels/webchat/` | WhatsApp-style test page (:8090) |
| `evals/` | datasets (106 dialect messages) and runners |
| `finetune/` | CPU LoRA fine-tuning pipeline + datasets |
| `results/` | eval outputs |
| `scripts/bootstrap_server.sh` | one-command setup of a fresh server |

Quick start on a fresh Ubuntu 24.04 server: `sudo bash scripts/bootstrap_server.sh`.
