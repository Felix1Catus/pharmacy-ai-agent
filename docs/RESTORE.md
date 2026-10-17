# Restore on a new server — and continue from where we stopped

Everything needed to rebuild lives in this repository, except secrets (generated fresh on the new
server) and model weights (base model pulled from Hugging Face/Ollama; our fine-tuned LoRA
adapters are attached to the GitHub Release `models-v1`).

## 1. New server (Ubuntu 24.04, root, CPU is fine; 32 vCPU / 64+ GB RAM recommended)
```bash
apt-get update && apt-get install -y git gh
gh auth login --web --scopes repo           # only needed for a private repo / the model release
gh repo clone <owner>/pharmacy-ai-agent /home/pharmacy-ai-agent
cd /home/pharmacy-ai-agent
sudo bash scripts/bootstrap_server.sh                   # base stack
# or:  sudo bash scripts/bootstrap_server.sh --with-training     (+ CPU fine-tune toolchain)
# or:  sudo bash scripts/bootstrap_server.sh --with-finetuned    (+ rebuild fine-tuned models)
```
What the bootstrap does (idempotent):
1. apt deps (python venv, ffmpeg, cmake…), **Docker**, **Ollama 0.40+** and the agent model
   `hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M`.
2. Python venv from `requirements.txt` (and `requirements-train.txt` with `--with-training`).
3. Fresh secrets: `.env` (from `.env.example`) and `odoo/config/odoo.conf` (from
   `odoo.conf.example`) with a random **master password**; after seeding, the Odoo **admin
   password is rotated** to a random value stored as `ODOO_PASSWORD` in `.env`.
4. **Odoo 18 + Postgres** via `odoo/docker-compose.yml`, database `pharmacy` with modules
   `product, stock, sale_management, sale_stock, pharmacy_agent` (no demo data), then the
   **sample pharmacy data** (`odoo/seed_data.py`: 42 products, 3 branches, demo customer).
5. Smoke test: `pytest adapter/test_adapter.py` (expect 20 passed).
6. Optional: llama.cpp (pinned) for GGUF export; download adapters from the release and build
   `pharmacy-4b-ft3` / `pharmacy-4b-ft` in Ollama.

Things that are deliberately NOT automatic (decide on the new server):
- Odoo is published on :8069 — run `ufw allow 8069/tcp` only if it should be public, and put
  HTTPS in front before real data (docs/operator-guide.md §3.5).
- **SFDA drugs data is not imported**: import `data_sources/sfda/data/sfda_products_odoo_import.xlsx`
  yourself (Inventory → Products → ⚙ → Import records), then optionally switch on
  *Update List → Automatic updates* (use pages 0–458 for the full registry).
- SSH hardening (key-only login), WhatsApp credentials.

## 2. Where we stopped
See **docs/STATUS.md** (summary, results, recommended next steps) and the TO-DO list in
**docs/progress.md**. Production = base `Qwen3-4B-Instruct-2507` + phonetic search + prescription
policy (agent eval **93%**, 100% safety). Fine-tune adapters (not adopted) are in Release
`models-v1`. On the old server the full SFDA catalogue was imported into Odoo — on a new server
import `data_sources/sfda/data/sfda_products_odoo_import.xlsx` again (or move the DB, §4).

## 3. Daily commands
```bash
cd /home/pharmacy-ai-agent && . .venv/bin/activate
python agent/chat.py "عندكم بنادول؟"                 # talk to the agent
python evals/runners/agent_eval.py                   # full agent eval (T-B4, 106 msgs)
python -m pytest adapter/test_adapter.py -q          # adapter/Odoo tests
cd odoo && docker compose ps                         # Odoo / Postgres status
```
Fine-tuning (CPU): see docs/operator-guide.md §2 and `finetune/run_round3.sh` (template for a
round: train → export → probe → agent eval on committed code → held-out test).

## 4. Moving the data, not just the code (optional)
The bootstrap recreates the *sample* data. To carry the exact Odoo database over instead:
```bash
# old server
cd odoo && docker compose exec -T db pg_dump -U odoo -Fc pharmacy > /tmp/pharmacy.dump
# new server (after bootstrap): stop odoo, restore, start
docker compose stop odoo
docker compose exec -T db dropdb -U odoo pharmacy
docker compose exec -T db createdb -U odoo pharmacy
docker compose exec -T db pg_restore -U odoo -d pharmacy < /tmp/pharmacy.dump
docker compose up -d odoo
```
(then put the old `ODOO_PASSWORD` into the new `.env`, since the users table comes with the dump.)
