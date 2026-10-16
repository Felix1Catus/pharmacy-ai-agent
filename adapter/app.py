"""FastAPI contract adapter.

Exposes one endpoint per contract action. Request bodies are validated against the JSON
schema in contract/schemas/<action>.json before the call reaches Odoo. The agent talks to
THIS service, never to Odoo directly.

Run:  uvicorn adapter.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapter"))

import contract  # noqa: E402
from gateway import Gateway, err  # noqa: E402

app = FastAPI(title="Pharmacy AI Agent — Odoo contract adapter", version="0.1.0")

_CONTRACT = contract.load_contract()
_VALIDATORS = {name: Draft202012Validator(schema["parameters"])
               for name, schema in _CONTRACT.items()}
_gateway: Gateway | None = None


def gw() -> Gateway:
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
    return _gateway


@app.get("/health")
def health():
    try:
        uid = gw().c.uid
        return {"status": "ok", "odoo_uid": uid, "db": gw().c.db}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "down", "detail": str(e)})


@app.get("/contract")
def get_contract():
    return {"actions": contract.ACTIONS,
            "requires_confirmed_intent": sorted(contract.actions_requiring_confirmed_intent())}


def _handle(action: str, body: dict):
    validator = _VALIDATORS.get(action)
    if validator is None:
        return JSONResponse(status_code=404, content=err("invalid_input", f"unknown action {action}"))
    errors = sorted(validator.iter_errors(body or {}), key=lambda e: e.path)
    if errors:
        msg = "; ".join(e.message for e in errors[:3])
        return JSONResponse(status_code=422, content=err("invalid_input", msg))
    result = getattr(gw(), action)(**(body or {}))
    status = 400 if isinstance(result, dict) and "error" in result else 200
    return JSONResponse(status_code=status, content=result)


# one route per action keeps the OpenAPI surface explicit
for _action in contract.ACTIONS:
    def _make(action):
        async def endpoint(body: dict | None = None):
            return _handle(action, body or {})
        endpoint.__name__ = f"tool_{action}"
        return endpoint
    app.post(f"/tool/{_action}", name=f"tool_{_action}")(_make(_action))
