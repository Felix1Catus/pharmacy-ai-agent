"""Shared Odoo JSON-RPC client (used by the seeder and the FastAPI adapter).

JSON-RPC (not XML-RPC) so that methods returning None/null marshal cleanly.
"""
from __future__ import annotations

import itertools
import os
from pathlib import Path

import requests


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class OdooError(RuntimeError):
    pass


class OdooClient:
    def __init__(self, url=None, db=None, user=None, password=None, timeout=30):
        load_env()
        self.url = (url or os.environ.get("ODOO_URL", "http://localhost:8069")).rstrip("/")
        self.db = db or os.environ.get("ODOO_DB", "pharmacy")
        self.user = user or os.environ.get("ODOO_USER", "admin")
        self.password = password or os.environ.get("ODOO_PASSWORD", "admin")
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._uid = None
        self._session = requests.Session()

    def _rpc(self, service, method, args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": next(self._ids),
        }
        r = self._session.post(f"{self.url}/jsonrpc", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            msg = err.get("data", {}).get("message") or err.get("message") or str(err)
            raise OdooError(msg)
        # Odoo omits "result" entirely when a method returns None; treat as None.
        return data.get("result")

    @property
    def uid(self):
        if self._uid is None:
            self._uid = self._rpc("common", "authenticate",
                                  [self.db, self.user, self.password, {}])
            if not self._uid:
                raise OdooError(f"authentication failed for {self.user}@{self.db}")
        return self._uid

    def execute_kw(self, model, method, args=None, kw=None):
        return self._rpc("object", "execute_kw",
                         [self.db, self.uid, self.password, model, method,
                          args or [], kw or {}])

    # convenience wrappers
    def search(self, model, domain, **kw):
        return self.execute_kw(model, "search", [domain], kw)

    def search_read(self, model, domain, fields=None, **kw):
        if fields is not None:
            kw["fields"] = fields
        return self.execute_kw(model, "search_read", [domain], kw)

    def read(self, model, ids, fields=None):
        kw = {"fields": fields} if fields else {}
        return self.execute_kw(model, "read", [ids], kw)

    def create(self, model, vals):
        return self.execute_kw(model, "create", [vals])

    def write(self, model, ids, vals):
        return self.execute_kw(model, "write", [ids, vals])

    def search_count(self, model, domain):
        return self.execute_kw(model, "search_count", [domain])
