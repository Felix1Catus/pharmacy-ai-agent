#!/usr/bin/env bash
# Local Odoo 18 + PostgreSQL for development on Windows without Docker: run INSIDE a WSL Ubuntu
# 24.04 distro, as root, from the repo (the same Odoo version as the docker image `odoo:18`).
#
#   wsl --install -d Ubuntu-24.04 --no-launch
#   wsl -d Ubuntu-24.04 -u root -- bash /mnt/<drive>/<path-to-repo>/scripts/dev_odoo_wsl.sh setup
#   wsl -d Ubuntu-24.04 -u root -- bash /mnt/<drive>/<path-to-repo>/scripts/dev_odoo_wsl.sh run
#   wsl -d Ubuntu-24.04 -u root -- bash .../scripts/dev_odoo_wsl.sh upgrade     # after module changes
#
# `setup` installs PostgreSQL + Odoo 18 and creates the `pharmacy` database with the
# pharmacy_agent module (no demo data). `run` serves it on http://localhost:8069 (login
# admin / admin - a throw-away local database, never expose it). Then, from Windows:
#   python odoo/seed_data.py
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ADDONS="$REPO/odoo/addons,/usr/lib/python3/dist-packages/odoo/addons"
DB=pharmacy
MODULES=base,product,stock,sale_management,sale_stock,contacts,pharmacy_agent
export DEBIAN_FRONTEND=noninteractive

pg_up() {
  pg_ctlcluster 16 main start 2>/dev/null || true
  for _ in $(seq 1 30); do su postgres -c "pg_isready -q" && return 0; sleep 1; done
  echo "postgres did not start" >&2; return 1
}
as_odoo() { su odoo -s /bin/bash -c "$*"; }

case "${1:-}" in
  setup)
    apt-get update -q
    apt-get install -y -q postgresql wget ca-certificates
    pg_up
    if ! command -v odoo >/dev/null; then
      wget -q -O /tmp/odoo18.deb https://nightly.odoo.com/18.0/nightly/deb/odoo_18.0.latest_all.deb
      apt-get install -y -q /tmp/odoo18.deb
      rm -f /tmp/odoo18.deb
    fi
    systemctl disable --now odoo >/dev/null 2>&1 || service odoo stop >/dev/null 2>&1 || true
    pkill -u odoo -f odoo-bin >/dev/null 2>&1 || true
    su postgres -c "psql -tAc \"select 1 from pg_roles where rolname='odoo'\"" | grep -q 1 \
      || su postgres -c "createuser -d odoo"
    if ! su postgres -c "psql -tAc \"select 1 from pg_database where datname='$DB'\"" | grep -q 1; then
      as_odoo "odoo -d $DB -i $MODULES --without-demo=all --stop-after-init --addons-path=$ADDONS --no-http"
    fi
    echo "SETUP_DONE $(odoo --version)"
    ;;
  upgrade)
    pg_up
    pkill -u odoo -f "/usr/bin/odoo" >/dev/null 2>&1 || true
    as_odoo "odoo -d $DB -u pharmacy_agent --stop-after-init --addons-path=$ADDONS --no-http"
    echo "UPGRADE_DONE"
    ;;
  run)
    pg_up
    exec su odoo -s /bin/bash -c "odoo -d $DB --db-filter='^$DB\$' --addons-path=$ADDONS --http-port=8069 --workers=0 --max-cron-threads=1"
    ;;
  *)
    echo "usage: $0 setup|run|upgrade" >&2; exit 2 ;;
esac
