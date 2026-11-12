"""Shared Odoo JSON-RPC client (used by the seeder, the gateway and the chat channels).

JSON-RPC (not XML-RPC) so that methods returning None/null marshal cleanly.

Works against the local docker Odoo and against an Odoo on another server. Settings (.env):

  ODOO_URL            http://localhost:8069, or https://odoo.example.com for a remote server
  ODOO_DB             database name
  ODOO_USER           login of the assistant's own service user (scripts/create_service_user.py);
                      not the administrator
  ODOO_API_KEY        that user's API key (preferred for a remote server), or
  ODOO_PASSWORD       its password
  ODOO_VERIFY_SSL     1 (default) | 0 - only switch off for a self-signed test certificate
  ODOO_CA_BUNDLE      path to a CA file when the server uses a private certificate authority
  ODOO_TIMEOUT        seconds per call (default 30)
  ODOO_RETRIES        extra attempts for a call that could not reach the server (default 2)
  ODOO_PUBLIC_URL     address customers open (payment links); defaults to ODOO_URL
  ODOO_ADMIN_USER / ODOO_ADMIN_PASSWORD
                      administrator login, used only by set-up scripts and the test clean-up
"""
from __future__ import annotations

import itertools
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("pharmacy.odoo")


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class OdooError(RuntimeError):
    pass


class OdooUnavailable(OdooError):
    """The Odoo server could not be reached (network, TLS, timeout, 5xx)."""


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


class OdooClient:
    def __init__(self, url=None, db=None, user=None, password=None, timeout=None):
        load_env()
        self.url = (url or os.environ.get("ODOO_URL", "http://localhost:8069")).rstrip("/")
        self.db = db or os.environ.get("ODOO_DB", "pharmacy")
        self.user = user or os.environ.get("ODOO_USER", "admin")
        # an API key is sent in place of the password (Odoo accepts either for RPC calls)
        self.password = password or os.environ.get("ODOO_API_KEY") or os.environ.get("ODOO_PASSWORD", "admin")
        self.timeout = float(timeout or os.environ.get("ODOO_TIMEOUT") or 30)
        self.retries = int(os.environ.get("ODOO_RETRIES") or 2)
        self.public_url = (os.environ.get("ODOO_PUBLIC_URL") or self.url).rstrip("/")
        self._ids = itertools.count(1)
        self._uid = None
        self._session = requests.Session()
        ca = os.environ.get("ODOO_CA_BUNDLE", "").strip()
        self._session.verify = ca or _flag("ODOO_VERIFY_SSL")
        if self.url.startswith("http://") and not any(
                h in self.url for h in ("//localhost", "//127.0.0.1", "//[::1]", "//db", "//odoo")):
            log.warning("ODOO_URL %s is plain http to another host: the login and customer data "
                        "travel unencrypted - use https", self.url)

    @classmethod
    def admin(cls) -> "OdooClient":
        """Administrator connection for set-up scripts and test clean-up (falls back to the
        normal login when no separate administrator is configured)."""
        load_env()
        return cls(user=os.environ.get("ODOO_ADMIN_USER") or None,
                   password=os.environ.get("ODOO_ADMIN_PASSWORD") or None)

    def _rpc(self, service, method, args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": next(self._ids),
        }
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self._session.post(f"{self.url}/jsonrpc", json=payload, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                # the request never got an answer; a read timeout may still have run on the
                # server, so only calls that failed to CONNECT are repeated
                last = e
                if isinstance(e, requests.ReadTimeout) or attempt == self.retries:
                    break
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code in (502, 503, 504) and attempt < self.retries:
                last = OdooUnavailable(f"Odoo answered HTTP {r.status_code}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise OdooUnavailable(f"Odoo answered HTTP {r.status_code} at {self.url}")
            try:
                data = r.json()
            except ValueError as e:
                raise OdooUnavailable(f"Odoo at {self.url} did not answer with JSON "
                                      f"(wrong ODOO_URL or a proxy page?)") from e
            if "error" in data:
                err = data["error"]
                msg = err.get("data", {}).get("message") or err.get("message") or str(err)
                raise OdooError(msg)
            # Odoo omits "result" entirely when a method returns None; treat as None.
            return data.get("result")
        raise OdooUnavailable(f"cannot reach Odoo at {self.url}: {last}") from last

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


def make_client() -> OdooClient:
    """The Odoo connection the assistant uses (settings from .env, see the module docstring)."""
    return OdooClient()
