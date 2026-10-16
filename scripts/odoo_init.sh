#!/usr/bin/env bash
# One-time Odoo DB init: pull images, start Postgres, create the `pharmacy` DB with the
# modules we need, no demo data. Idempotent-ish: re-running re-inits the DB module list.
set -euo pipefail
cd "$(dirname "$0")/../odoo"
echo "[odoo_init] pulling images..."
docker compose pull -q
echo "[odoo_init] starting postgres..."
docker compose up -d db
echo "[odoo_init] waiting for postgres healthy..."
for i in $(seq 1 30); do
  if docker compose ps db | grep -q healthy; then break; fi
  sleep 3
done
echo "[odoo_init] initialising pharmacy DB (base,product,stock,sale_management, no demo)..."
docker compose run --rm odoo odoo \
  -i base,product,stock,sale_management \
  -d pharmacy --stop-after-init --without-demo=all --db_host=db --db_user=odoo --db_password=odoo
echo "[odoo_init] starting odoo web..."
docker compose up -d odoo
echo "[odoo_init] done. Odoo should be at http://localhost:8069 (db: pharmacy)"
