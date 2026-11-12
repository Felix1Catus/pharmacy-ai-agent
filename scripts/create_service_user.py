#!/usr/bin/env python3
"""Create (or repair) the assistant's own Odoo login, so the chat services stop using the
administrator account.

  python scripts/create_service_user.py                 # prints the new login + password once
  python scripts/create_service_user.py --write-env     # also puts them into .env
  python scripts/create_service_user.py --login bot@pharmacy.example --rotate --write-env

It connects as the administrator (ODOO_ADMIN_USER / ODOO_ADMIN_PASSWORD in .env, or the current
ODOO_USER / ODOO_PASSWORD if those are still the administrator's) and creates an internal user
whose only group is "Pharmacy: WhatsApp assistant (service account)" (pharmacy_agent module
18.0.1.12.0+). That group can read the catalogue and stock, create contacts, quotations,
prescriptions, handovers and stock alerts - and nothing else: no settings, no users, no
accounting, and it can never approve a prescription.

Works the same for an Odoo on another server: point ODOO_URL at it (https) first.
For an API key instead of a password (recommended for a remote Odoo): log in to Odoo as this
user once, Preferences -> Account Security -> New API Key, and put it in .env as ODOO_API_KEY.
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
from odoo_client import OdooClient, OdooError  # noqa: E402

GROUP_XMLID = ("pharmacy_agent", "group_pharmacy_assistant")


def set_env(key: str, value: str) -> None:
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    env.write_text(text, encoding="utf-8")
    try:
        env.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--login", default="pharmacy-assistant")
    ap.add_argument("--name", default="WhatsApp assistant (service account)")
    ap.add_argument("--rotate", action="store_true", help="set a new password on an existing user")
    ap.add_argument("--write-env", action="store_true",
                    help="write ODOO_USER / ODOO_PASSWORD to .env (the administrator's login is "
                         "kept as ODOO_ADMIN_USER / ODOO_ADMIN_PASSWORD for set-up scripts and tests)")
    a = ap.parse_args()

    admin = OdooClient.admin()
    try:
        ref = admin.search_read("ir.model.data", [["module", "=", GROUP_XMLID[0]], ["name", "=", GROUP_XMLID[1]]],
                                ["res_id"])
    except OdooError as e:
        print(f"Cannot read Odoo's groups as {admin.user}: {e}\nRun this with the administrator's "
              f"login (ODOO_ADMIN_USER / ODOO_ADMIN_PASSWORD in .env).", file=sys.stderr)
        return 1
    if not ref:
        print("The group 'Pharmacy: WhatsApp assistant' does not exist: upgrade the pharmacy_agent "
              "module to 18.0.1.12.0 or later first (odoo -u pharmacy_agent).", file=sys.stderr)
        return 1
    group_id = ref[0]["res_id"]

    existing = admin.search("res.users", [["login", "=", a.login]], context={"active_test": False})
    password = None
    if existing:
        uid = existing[0]
        vals = {"groups_id": [(6, 0, [group_id])], "active": True}
        if a.rotate:
            password = secrets.token_urlsafe(24)
            vals["password"] = password
        admin.write("res.users", [uid], vals)
        print(f"user {a.login} (id {uid}) updated: only the assistant group" +
              ("; new password set" if a.rotate else "; password unchanged (use --rotate for a new one)"))
    else:
        password = secrets.token_urlsafe(24)
        uid = admin.create("res.users", {"name": a.name, "login": a.login, "password": password,
                                         "groups_id": [(6, 0, [group_id])]})
        print(f"user {a.login} (id {uid}) created with only the assistant group")

    if password and a.write_env:
        if admin.user != a.login:                 # keep the administrator's login for set-up + tests
            set_env("ODOO_ADMIN_USER", admin.user)
            set_env("ODOO_ADMIN_PASSWORD", admin.password)
        set_env("ODOO_USER", a.login)
        set_env("ODOO_PASSWORD", password)
        print(".env updated: ODOO_USER / ODOO_PASSWORD are now the service account "
              "(administrator kept as ODOO_ADMIN_USER / ODOO_ADMIN_PASSWORD). Restart the chat services.")
    elif password:
        print(f"\n  ODOO_USER={a.login}\n  ODOO_PASSWORD={password}\n\n"
              f"Put these in .env (shown once), or re-run with --rotate --write-env.")

    # prove the new login works and is NOT an administrator
    if password:
        bot = OdooClient(user=a.login, password=password)
        bot.search("product.template", [], limit=1)
        try:
            bot.search("ir.config_parameter", [], limit=1)
            print("WARNING: the service user can read system settings - check its groups.", file=sys.stderr)
        except OdooError:
            print("checked: the service user reads the catalogue and has no access to system settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
