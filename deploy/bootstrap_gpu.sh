#!/usr/bin/env bash
# One-command rebuild of the live stack on a fresh Ubuntu 24.04 GPU server (NVIDIA driver + Docker +
# nvidia-container-toolkit already installed, as on Vultr GPU images). Idempotent: safe to re-run.
#
#   git clone https://github.com/Felix1Catus/pharmacy-ai-agent /home/pharmacy-ai-agent
#   cd /home/pharmacy-ai-agent && sudo bash deploy/bootstrap_gpu.sh            # everything
#   PUBLIC=0 sudo bash deploy/bootstrap_gpu.sh                                  # no public HTTPS
#
# Builds: Odoo 18 + SFDA catalogue (localhost only) -> Qwen3-8B Q4_K_M on llama-server (tensor split over
# all GPUs when there are 2+) -> NAMAA-Saudi-TTS (GPU0) -> webchat with Whisper large-v3 (GPU1) -> Caddy
# HTTPS (chat.<ip>.sslip.io, odoo.<ip>.sslip.io). Secrets are generated here and kept in .env (never git).
# See deploy/README.md for what this does step by step and what stays manual.
set -uo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
PUBLIC=${PUBLIC:-1}
LOG=${LOG:-/var/log/pharmacy-bootstrap.log}
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
fail(){ log "FAILED: $*"; exit 1; }
cd "$REPO"

log "== 1/9 checks"
command -v nvidia-smi >/dev/null || fail "no NVIDIA driver (nvidia-smi missing)"
GPUS=$(nvidia-smi -L | wc -l); log "GPUs: $GPUS"
docker info 2>/dev/null | grep -q nvidia || fail "Docker with the nvidia runtime is required (nvidia-container-toolkit)"

log "== 2/9 system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null && apt-get install -y -q python3.12-venv ffmpeg tesseract-ocr tesseract-ocr-ara curl ufw >/dev/null \
  || fail "apt"

log "== 3/9 agent venv (requirements.txt)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -r requirements.txt || fail "pip requirements"

log "== 4/9 .env (secrets generated here, never committed)"
[ -f .env ] || cp .env.example .env
setv(){ grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >> .env; }
setv LLM_API openai; setv OLLAMA_URL http://127.0.0.1:8080; setv AGENT_MODEL qwen3-8b-q4_k_m
setv ASR_MODEL large-v3; setv ASR_GPU "$([ "$GPUS" -ge 2 ] && echo 1 || echo 0)"; setv TTS_ENGINE namaa
setv TTS_NAMAA_URL http://127.0.0.1:8095
grep -q "^WEBCHAT_PASSWORD=..*" .env || setv WEBCHAT_PASSWORD "$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
grep -q "^ODOO_PASSWORD=..*" .env || setv ODOO_PASSWORD admin
chmod 600 .env

log "== 5/9 Odoo 18 + Postgres (localhost only) + sample data + SFDA catalogue"
[ -f odoo/config/odoo.conf ] || sed "s|^admin_passwd = .*|admin_passwd = $(openssl rand -hex 16)|" \
  odoo/config/odoo.conf.example > odoo/config/odoo.conf
cp deploy/odoo-compose.override.yml odoo/docker-compose.override.yml
( cd odoo && docker compose pull -q && docker compose up -d db ) || fail "postgres"
for i in $(seq 1 40); do (cd odoo && docker compose ps db | grep -q healthy) && break; sleep 3; done
if ! (cd odoo && docker compose exec -T db psql -U odoo -d postgres -tAc "select 1 from pg_database where datname='pharmacy'" | grep -q 1); then
  (cd odoo && docker compose run --rm odoo odoo -i base,product,stock,sale_management,sale_stock,contacts,pharmacy_agent \
    -d pharmacy --without-demo=all --stop-after-init --db_host=db --db_user=odoo --db_password=odoo) >>"$LOG" 2>&1 \
    || fail "Odoo module install"
  FRESH_DB=1
fi
(cd odoo && docker compose up -d odoo) || fail "odoo start"
for i in $(seq 1 60); do curl -sf -o /dev/null 127.0.0.1:8069/web/login && break; sleep 3; done
if [ "${FRESH_DB:-0}" = 1 ]; then
  .venv/bin/python odoo/seed_data.py >>"$LOG" 2>&1 || fail "seed"
  (cd odoo && docker compose run --rm -T -v "$REPO/data_sources/sfda/data:/sfda:ro" odoo odoo shell -d pharmacy \
    --db_host=db --db_user=odoo --db_password=odoo --no-http < "$REPO/data_sources/sfda/test_import_odoo_shell.py") 2>/dev/null \
    | grep "^IMPORT_RESULT" | cut -c1-200 | tee -a "$LOG"
fi

log "== 6/9 model: Qwen3-8B Q4_K_M (the exact qwen3:8b weights the evals used) on llama-server"
mkdir -p /opt/models
if [ ! -s /opt/models/qwen3-8b-q4_k_m.gguf ]; then
  docker rm -f ollama-fetch >/dev/null 2>&1
  docker run -d --name ollama-fetch -v /opt/ollama-fetch:/root/.ollama ollama/ollama:latest >/dev/null
  sleep 5; docker exec ollama-fetch ollama pull qwen3:8b >/dev/null || fail "ollama pull qwen3:8b"
  DIGEST=$(python3 -c "import json; m=json.load(open('/opt/ollama-fetch/models/manifests/registry.ollama.ai/library/qwen3/8b')); print(next(l['digest'] for l in m['layers'] if l['mediaType'].endswith('.model')).replace(':','-'))")
  cp "/opt/ollama-fetch/models/blobs/$DIGEST" /opt/models/qwen3-8b-q4_k_m.gguf && docker rm -f ollama-fetch >/dev/null
fi
SM=$([ "$GPUS" -ge 2 ] && echo tensor || echo none)
docker rm -f pharmacy-llm >/dev/null 2>&1
docker run -d --name pharmacy-llm --restart unless-stopped --gpus all -p 127.0.0.1:8080:8080 -v /opt/models:/models:ro \
  --entrypoint /app/llama-server ghcr.io/ggml-org/llama.cpp:full-cuda -m /models/qwen3-8b-q4_k_m.gguf -ngl 99 -fa on \
  -sm "$SM" -c 8192 -np 1 --jinja --host 0.0.0.0 --port 8080 >/dev/null || fail "llama-server"
for i in $(seq 1 90); do curl -sf 127.0.0.1:8080/health >/dev/null && break; sleep 2; done
log "llama-server: $(curl -s 127.0.0.1:8080/health) (split mode $SM)"

log "== 7/9 voice: NAMAA-Saudi-TTS venv + service, Piper fallback voice"
mkdir -p /opt/tts/piper
for f in ar_JO-kareem-medium.onnx ar_JO-kareem-medium.onnx.json; do
  [ -s /opt/tts/piper/$f ] || curl -fsSL -o /opt/tts/piper/$f "https://huggingface.co/rhasspy/piper-voices/resolve/main/ar/ar_JO/kareem/medium/$f"
done
[ -d /opt/tts/namaa-venv ] || python3 -m venv /opt/tts/namaa-venv
# setuptools<81: chatterbox's watermarker (resemble-perth) still imports pkg_resources
/opt/tts/namaa-venv/bin/pip install -q chatterbox-tts fastapi uvicorn safetensors huggingface_hub "setuptools<81" \
  || fail "namaa venv"

log "== 8/9 services"
for u in pharmacy-webchat pharmacy-tts-namaa pharmacy-distill; do
  sed -e "s|@REPO@|$REPO|g" -e "s|@SNAPSHOT@|$REPO|g" "deploy/systemd/$u.service" > "/etc/systemd/system/$u.service"
done
systemctl daemon-reload
systemctl enable --now pharmacy-tts-namaa pharmacy-webchat >/dev/null 2>&1   # distill stays off until you start it
for i in $(seq 1 60); do curl -s -o /dev/null -w "%{http_code}" 127.0.0.1:8090/ | grep -q 401 && break; sleep 3; done

log "== 9/9 public HTTPS"
if [ "$PUBLIC" = 1 ]; then
  IP=$(ip -4 -o route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p'); DASHED=${IP//./-}
  mkdir -p /opt/caddy && sed -e "s|@IP@|$IP|g" -e "s|@DASHED@|$DASHED|g" deploy/Caddyfile.template > /opt/caddy/Caddyfile
  docker rm -f pharmacy-caddy >/dev/null 2>&1
  docker run -d --name pharmacy-caddy --restart unless-stopped --network host -v /opt/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v caddy-data:/data caddy:2 >/dev/null || fail "caddy"
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw allow 8443/tcp >/dev/null; ufw allow 8069/tcp >/dev/null; ufw --force enable >/dev/null
  log "chat: https://chat.$DASHED.sslip.io   odoo: https://odoo.$DASHED.sslip.io"
fi
log "== done. Webchat user: $(grep ^WEBCHAT_USER= .env | cut -d= -f2-), password in $REPO/.env; Odoo user admin."
log "   Change the Odoo admin password before sharing the links (deploy/README.md)."
