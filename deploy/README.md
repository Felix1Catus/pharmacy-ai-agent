# Rebuild on a new GPU server

Everything needed to bring the live stack up on another machine, as of 2026-11-04. Secrets are not in git:
the bootstrap generates them into `.env` (and `odoo/config/odoo.conf`) on the new server.

## What runs
| Piece | Where | Notes |
|---|---|---|
| Qwen3-8B Q4_K_M (Ollama's `qwen3:8b` weights) | `pharmacy-llm` container, llama-server, 127.0.0.1:8080 | `-sm tensor` across all GPUs when there are 2+; `LLM_API=openai` in `.env` |
| Agent + webchat | systemd `pharmacy-webchat`, 127.0.0.1:8090 | Whisper `large-v3` on GPU1 (GPU0 with one GPU) |
| NAMAA-Saudi-TTS | systemd `pharmacy-tts-namaa`, 127.0.0.1:8095, GPU0 | own venv `/opt/tts/namaa-venv`; Piper is the fallback voice |
| Odoo 18 + Postgres 16 | `odoo/docker-compose.yml` + `deploy/odoo-compose.override.yml` | Odoo on 127.0.0.1:8069 only; sample data + SFDA catalogue (2,014 products) |
| Public HTTPS | `pharmacy-caddy` container (`deploy/Caddyfile.template`) | `chat.<ip>.sslip.io`, `odoo.<ip>.sslip.io` (Let's Encrypt); `https://<ip>:8443` / `:8069` self-signed |
| Distillation pipeline | systemd `pharmacy-distill` (off by default) | see `distill/README.md` |

## Steps
Prerequisites: Ubuntu 24.04, NVIDIA driver, Docker with nvidia-container-toolkit (`docker info | grep nvidia`).
Tested on 2x NVIDIA A16 (16 GB each); a single 24 GB+ GPU works too (no tensor split).

```bash
git clone https://github.com/Felix1Catus/pharmacy-ai-agent /home/pharmacy-ai-agent
cd /home/pharmacy-ai-agent && git checkout fix/agent-abuse-and-order-status   # until PR #1 is merged
sudo bash deploy/bootstrap_gpu.sh            # PUBLIC=0 to skip public HTTPS and firewall changes
```
Then:
1. Nothing to change by hand for Odoo: on a fresh database the bootstrap rotates the administrator's
   password (`ODOO_ADMIN_PASSWORD` in `.env`) and creates the chat services' own least-privilege login
   (`ODOO_USER=pharmacy-assistant`). The webchat password is generated into `.env` too. Give each
   pharmacist the group *Pharmacy: pharmacist (prescription review)*.
2. Check: `curl -s 127.0.0.1:8080/health`, open `https://chat.<ip-with-dashes>.sslip.io`, send «عندكم بنادول؟».
3. Run the full agent eval: `.venv/bin/python evals/runners/agent_eval.py` (2026-11-04 on 2x A16:
   90% acceptable, 55% exact, 0 wrong actions, 100% medical refusal / restricted safe).

## Moving the data instead of re-seeding
The bootstrap seeds sample data and imports the SFDA file. To carry the live Odoo database over, use the
`pg_dump` / `pg_restore` steps in `docs/RESTORE.md` §4, then set `ODOO_PASSWORD` to the old admin password.

## Not automated
- WhatsApp: the service `pharmacy-whatsapp` and the `/webhook` proxy route are installed but not
  started. Add the `WHATSAPP_*` values to `.env` (incl. the approved template name), then
  `systemctl enable --now pharmacy-whatsapp` (`docs/operator-guide.md` §7).
- An Odoo on another server instead of the local one: `docs/odoo-connection.md`.
- The distillation teacher's Claude Code login (`distill/README.md`).
- Fine-tuned models: see `train/README.md` (the dataset is in `distill/data/`; `train/pipeline_v2.sh` runs labels -> train -> eval unattended).
