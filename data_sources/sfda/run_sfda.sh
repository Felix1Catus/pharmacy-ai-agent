#!/usr/bin/env bash
# After the scrape: build the files, then test-import them into the SCRATCH database
# (sfda_import_test) — never into the live 'pharmacy' DB. Log: /tmp/sfda_pipeline.log
set -u
cd "$(dirname "$0")/../.."
log() { echo "[$(date '+%H:%M:%S')] $*"; }
log "waiting for scrape to finish"
while ! grep -q "^DONE:" /tmp/sfda_scrape.log; do
  grep -q "giving up\|Traceback" /tmp/sfda_scrape.log && { log "scrape FAILED"; exit 1; }
  sleep 30
done
log "build files"; .venv/bin/python data_sources/sfda/build_odoo_import.py
log "test import into scratch DB sfda_import_test"
cd odoo && docker compose run --rm -T -v "$PWD/../data_sources/sfda/data:/sfda:ro" odoo \
  odoo shell -d sfda_import_test --db_host=db --db_user=odoo --db_password=odoo --no-http \
  < ../data_sources/sfda/test_import_odoo_shell.py 2>/dev/null | grep "^IMPORT_RESULT"
log "SFDA PIPELINE DONE"
