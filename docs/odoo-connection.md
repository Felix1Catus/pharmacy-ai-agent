# Connecting the assistant to Odoo (local or on another server)

The chat services (`channels/webchat`, `channels/whatsapp`) talk to Odoo 18 over JSON-RPC through
`odoo/odoo_client.py`. Nothing in the assistant assumes Odoo is on the same machine.

## 1. What Odoo needs

- Odoo **18** with the modules `product, stock, sale_management, sale_stock, contacts` and this
  repo's **`pharmacy_agent` 18.0.1.12.0** (`odoo/addons/pharmacy_agent`).
- On an Odoo that already has an older `pharmacy_agent`: copy the new module folder to its addons
  path and upgrade it — `odoo -d <db> -u pharmacy_agent --stop-after-init` (or Apps → Pharmacy AI
  Agent → Upgrade). The upgrade adds the two security groups, the "release expired holds" cron and
  the `hold_expires_at` field. Until then holds are still created, just without an expiry.

## 2. The assistant's own login (not the administrator)

```bash
# with the administrator's login in .env (ODOO_USER/ODOO_PASSWORD, or ODOO_ADMIN_USER/ODOO_ADMIN_PASSWORD)
.venv/bin/python scripts/create_service_user.py --write-env
```

This creates the user `pharmacy-assistant` whose only group is **Pharmacy: WhatsApp assistant
(service account)**, writes it to `.env` as `ODOO_USER` / `ODOO_PASSWORD`, and keeps the
administrator as `ODOO_ADMIN_USER` / `ODOO_ADMIN_PASSWORD` (used only by `odoo/seed_data.py`, this
script and the test clean-up). Restart the chat services afterwards.

| The service account can | It cannot |
|---|---|
| read products, stock, branches | open Settings, users, accounting |
| create / update contacts | approve or reject a prescription (pharmacists only) |
| create quotations, confirm an **approved** one, cancel an open one | delete prescriptions, orders or contacts |
| store prescriptions, handovers, stock alerts | read Odoo's system parameters |

For a remote Odoo prefer an **API key**: log in to Odoo as `pharmacy-assistant` once →
Preferences → Account Security → *New API Key*, put it in `.env` as `ODOO_API_KEY` (it is used in
place of the password) and clear `ODOO_PASSWORD`.

**Pharmacists:** give each licensed pharmacist the group **Pharmacy: pharmacist (prescription
review)** (Settings → Users). Only they (and the administrator) see *Pharmacy → Prescriptions* /
*Prescription review* and the Approve / Reject buttons.

## 3. `.env` for an Odoo on another server

```ini
ODOO_URL=https://odoo.your-pharmacy.example      # always https to another host
ODOO_DB=pharmacy
ODOO_USER=pharmacy-assistant
ODOO_API_KEY=<the key from step 2>
ODOO_PUBLIC_URL=https://odoo.your-pharmacy.example   # the address customers open for payment links
ODOO_TIMEOUT=30
ODOO_RETRIES=2
# only for a private certificate authority:
# ODOO_CA_BUNDLE=/etc/ssl/certs/pharmacy-ca.pem
```

- `ODOO_PUBLIC_URL` matters when the assistant reaches Odoo over a private address (VPN, internal
  DNS, `http://10.x`): payment links must use the public one. If it is empty the assistant tries
  Odoo's own `web.base.url` (the service account cannot read it) and then falls back to `ODOO_URL`,
  with a warning in the log.
- Leave `ODOO_VERIFY_SSL=1`. `0` exists only for a self-signed **test** server.
- The remote Odoo's firewall should allow the assistant server's IP on 443 only. Odoo's own
  setting `proxy_mode = True` is needed behind its reverse proxy.
- Each call is retried (`ODOO_RETRIES`) only when the server could not be reached at all; a call
  that timed out while running is not repeated, so an order is never created twice.

Check the connection:

```bash
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "odoo")
from odoo_client import OdooClient
c = OdooClient(); print("uid", c.uid, "products", c.search_count("product.template", []))
PY
```

## 4. What happens when Odoo is unreachable

The customer gets a fixed "the system is busy, try again in a minute" reply (never silence, never
an invented answer), the failure is logged with its traceback (`journalctl -u pharmacy-whatsapp`),
handovers fall back to `results/handovers.jsonl`, and the notifier simply retries on its next round.

## 5. A local Odoo for development

- Linux / macOS with Docker: `cd odoo && docker compose up -d`, then `python odoo/seed_data.py`.
- Windows without Docker: `scripts/dev_odoo_wsl.sh` (Ubuntu 24.04 in WSL, same Odoo 18):
  ```
  wsl --install -d Ubuntu-24.04 --no-launch
  wsl -d Ubuntu-24.04 -u root -- bash /mnt/<drive>/<repo>/scripts/dev_odoo_wsl.sh setup
  wsl -d Ubuntu-24.04 -u root -- bash /mnt/<drive>/<repo>/scripts/dev_odoo_wsl.sh run
  python odoo/seed_data.py && python scripts/create_service_user.py --write-env
  ```
  Use `ODOO_URL=http://127.0.0.1:8069` (on Windows `localhost` adds ~2 s to every call).
  After changing the module: `... dev_odoo_wsl.sh upgrade`, then `run` again.

Then `pytest` runs the whole suite against it (365 tests; the ones needing Tesseract, the SFDA
catalogue or the voice models skip themselves when those are not installed).
