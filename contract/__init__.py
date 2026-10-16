"""Tool contract: load the JSON schemas and expose them for the agent and the adapter."""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent / "schemas"

# The seven actions the agent may call. Order is stable for prompts/evals.
ACTIONS = [
    "search_product",
    "check_stock",
    "create_order",
    "create_dispatch",
    "hold_item",
    "track_order",
    "escalate_to_human",
]


def load_contract() -> dict[str, dict]:
    """Return {action_name: schema_dict} for all actions, validated to exist."""
    contract: dict[str, dict] = {}
    for name in ACTIONS:
        path = SCHEMA_DIR / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"missing contract schema: {path}")
        schema = json.loads(path.read_text(encoding="utf-8"))
        if schema.get("name") != name:
            raise ValueError(f"{path}: 'name' is {schema.get('name')!r}, expected {name!r}")
        contract[name] = schema
    return contract


def tools_for_llm() -> list[dict]:
    """Contract in the OpenAI/Qwen 'tools' shape: {type:function, function:{name,description,parameters}}."""
    out = []
    for name, schema in load_contract().items():
        out.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            }
        )
    return out


def actions_requiring_confirmed_intent() -> set[str]:
    return {n for n, s in load_contract().items() if s.get("requires_confirmed_intent")}
