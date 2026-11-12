#!/usr/bin/env bash
# Rebuild the whole Pharmacy AI Agent stack on a FRESH Ubuntu 24.04 server (run as root, from the
# cloned repo):   sudo bash scripts/bootstrap_server.sh [--with-training] [--with-finetuned]
#
#   base            : apt deps, Docker, Ollama + Qwen3-4B-Instruct-2507 GGUF, Python venv,
#                     Odoo 18 + Postgres (modules incl. pharmacy_agent), sample data, fresh secrets
#   --with-training : CPU fine-tune toolchain (torch/transformers/peft + llama.cpp convert/quantize)
#   --with-finetuned: also download the LoRA adapter(s) from this repo's GitHub Release and build
#                     the fine-tuned Ollama model(s) (implies --with-training; needs `gh auth login`)
# Idempotent: safe to re-run; finished steps are skipped. Secrets are generated locally into .env and
# odoo/config/odoo.conf (both gitignored) — nothing secret comes from git.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
WITH_TRAIN=0; WITH_FT=0
for a in "$@"; do case "$a" in --with-training) WITH_TRAIN=1;; --with-finetuned) WITH_TRAIN=1; WITH_FT=1;; esac; done
log() { echo -e "\n\033[1m[bootstrap $(date +%H:%M:%S)] $*\033[0m"; }
export DEBIAN_FRONTEND=noninteractive
AGENT_MODEL_TAG="hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
LLAMACPP_COMMIT="7182ef7"     # llama.cpp revision the export was verified with
rnd() { python3 -c "import secrets;print(secrets.token_urlsafe($1))"; }

log "1/8 system packages"
apt-get update -q
apt-get install -y -q ca-certificates curl git python3.12-venv ffmpeg cmake build-essential jq \
  tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng fonts-dejavu-core poppler-utils   # prescription OCR + PDF

log "2/8 Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -q && apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

log "3/8 Ollama + agent model ($AGENT_MODEL_TAG)"
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama >/dev/null 2>&1 || true
for i in $(seq 1 30); do curl -sf http://localhost:11434/api/version >/dev/null && break; sleep 2; done
ollama list | grep -q "Qwen3-4B-Instruct-2507" || ollama pull "$AGENT_MODEL_TAG"

log "   text-to-speech voice (Piper ar_JO-kareem, adapter/tts.py)"
mkdir -p /opt/tts/piper
for f in ar_JO-kareem-medium.onnx ar_JO-kareem-medium.onnx.json; do
  [ -s /opt/tts/piper/$f ] || curl -fsSL -o /opt/tts/piper/$f \
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/ar/ar_JO/kareem/medium/$f"
done

log "4/8 Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

log "5/8 secrets (.env, odoo.conf) — generated locally, never from git"
[ -f .env ] || cp .env.example .env
grep -q '^ODOO_MASTER_PASSWORD=' .env || echo "ODOO_MASTER_PASSWORD=$(rnd 24)" >> .env
MASTER=$(grep '^ODOO_MASTER_PASSWORD=' .env | cut -d= -f2-)
if [ ! -f odoo/config/odoo.conf ]; then
  sed "s|^admin_passwd = .*|admin_passwd = $MASTER|" odoo/config/odoo.conf.example > odoo/config/odoo.conf
fi

log "6/8 Odoo 18 + Postgres (Docker) and the pharmacy database"
cd odoo
docker compose pull -q
docker compose up -d db
for i in $(seq 1 30); do docker compose ps db | grep -q healthy && break; sleep 3; done
if ! docker compose exec -T db psql -U odoo -d postgres -tAc "select 1 from pg_database where datname='pharmacy'" | grep -q 1; then
  docker compose run --rm odoo odoo -i base,product,stock,sale_management,sale_stock,contacts,pharmacy_agent \
    -d pharmacy --without-demo=all --stop-after-init --db_host=db --db_user=odoo --db_password=odoo
  FRESH_DB=1
else
  docker compose run --rm odoo odoo -i contacts -u pharmacy_agent -d pharmacy --stop-after-init \
    --db_host=db --db_user=odoo --db_password=odoo
  FRESH_DB=0
fi
docker compose up -d odoo
cd "$ROOT"
for i in $(seq 1 40); do curl -sf -o /dev/null http://localhost:8069/web/login && break; sleep 3; done

if [ "$FRESH_DB" = "1" ]; then
  log "   seeding sample pharmacy data + rotating the Odoo admin password"
  sed -i "s|^ODOO_PASSWORD=.*|ODOO_PASSWORD=admin|" .env      # a fresh DB starts as admin/admin
  .venv/bin/python odoo/seed_data.py
  NEWPW=$(rnd 18)
  .venv/bin/python - "$NEWPW" <<'PY'
import sys; sys.path.insert(0, "odoo")
from odoo_client import OdooClient
c = OdooClient(); c.write("res.users", [c.uid], {"password": sys.argv[1]})
PY
  sed -i "s|^ODOO_PASSWORD=.*|ODOO_PASSWORD=$NEWPW|" .env
  # the chat services get their own least-privilege Odoo login; the administrator's stays in
  # .env as ODOO_ADMIN_USER / ODOO_ADMIN_PASSWORD for set-up scripts and tests
  .venv/bin/python scripts/create_service_user.py --write-env
fi
chmod 600 .env

log "   WhatsApp-style test chat page (systemd service on :8090, password in .env)"
grep -q '^WEBCHAT_PASSWORD=..*' .env || { sed -i '/^WEBCHAT_PASSWORD=/d;/^WEBCHAT_USER=/d' .env
  printf 'WEBCHAT_USER=pharmacy\nWEBCHAT_PASSWORD=%s\n' "$(rnd 12)" >> .env; }
cp channels/webchat/pharmacy-webchat.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pharmacy-webchat >/dev/null 2>&1 || true

log "7/8 smoke test (adapter against Odoo)"
.venv/bin/python -m pytest adapter/test_adapter.py -q | tail -1

if [ "$WITH_TRAIN" = "1" ]; then
  log "8/8 fine-tune toolchain (CPU) + llama.cpp convert/quantize"
  .venv/bin/pip install -q -r requirements-train.txt
  if [ ! -d /opt/llama.cpp-src/.git ]; then
    git clone -q https://github.com/ggml-org/llama.cpp /opt/llama.cpp-src
  fi
  git -C /opt/llama.cpp-src fetch -q --depth 50 origin 2>/dev/null || true
  git -C /opt/llama.cpp-src checkout -q "$LLAMACPP_COMMIT" 2>/dev/null || echo "   (pinned commit not found, using current llama.cpp)"
  .venv/bin/pip install -q /opt/llama.cpp-src/gguf-py
  [ -x /opt/llama.cpp-src/build/bin/llama-quantize ] || {
    cmake -S /opt/llama.cpp-src -B /opt/llama.cpp-src/build -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build /opt/llama.cpp-src/build --target llama-quantize -j"$(nproc)" >/dev/null; }
else
  log "8/8 (skipped training toolchain — re-run with --with-training)"
fi

if [ "$WITH_FT" = "1" ]; then
  log "   fine-tuned model(s) from the GitHub Release"
  mkdir -p results/release
  gh release download models-v1 --dir results/release --clobber -p '*.tar.gz'
  for t in results/release/*.tar.gz; do tar -xzf "$t" -C results/; done
  for spec in "results/ft_4b_r3/adapter:pharmacy-4b-ft3" "results/ft_4b/adapter:pharmacy-4b-ft"; do
    dir=${spec%%:*}; name=${spec##*:}
    [ -f "$dir/adapter_model.safetensors" ] || continue
    ollama list | grep -q "^$name" || DROP_BASE_CACHE=1 \
      TEMPLATE_FROM="$AGENT_MODEL_TAG" finetune/export_to_ollama.sh Qwen/Qwen3-4B-Instruct-2507 "$dir" "$name"
  done
fi

log "DONE. Odoo: http://$(curl -s https://api.ipify.org 2>/dev/null || echo localhost):8069 (db pharmacy, user admin,
   password = ODOO_PASSWORD in .env). Test chat: http://<server>:8090 (user/password =
   WEBCHAT_USER/WEBCHAT_PASSWORD in .env). Both listen on all interfaces. NOTE: Odoo's port is
   published by Docker, which bypasses ufw - :8069 is reachable from outside whatever ufw says.
   To keep Odoo private: cp deploy/odoo-compose.override.yml odoo/docker-compose.override.yml &&
   (cd odoo && docker compose up -d), then put HTTPS in front (deploy/Caddyfile.template).
   Chat with the agent: .venv/bin/python agent/chat.py
   Next steps / status: docs/RESTORE.md and the TO-DO list in docs/progress.md"
