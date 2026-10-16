# Tool contract

The agent (Qwen3) may call **only** these seven actions. Each has a JSON schema in
`contract/schemas/<name>.json`. The schemas are the source of truth; this file explains
intent, the confirmed-intent rule, and error handling. The FastAPI **adapter** (`adapter/`)
implements every action against the real Odoo 18 instance; the agent never speaks raw Odoo.

Load them with `contract.load_contract()` / `contract.tools_for_llm()`.

## Confirmed-intent rule
Actions with `requires_confirmed_intent: true` change state on the customer's behalf and may
only be called **after the customer has explicitly confirmed** the specifics (items, quantity,
pickup/delivery). The agent must confirm before calling them.

| Action | Confirmed intent | State-changing | Notes |
|---|---|---|---|
| `search_product` | no | no | resolve the product first; never guess between candidates |
| `check_stock` | no | no | never invent stock numbers |
| `create_order` | **yes** | yes | restricted/prescription items → escalate, do **not** order |
| `create_dispatch` | **yes** | yes | only after a successful `create_order` |
| `hold_item` | **yes** | yes | restricted/prescription items → escalate |
| `track_order` | no | no | read-only |
| `escalate_to_human` | no | (hands over) | terminal for the turn |

## Guardrails encoded in the contract
- **No medical advice.** Dosage / diagnosis / "is this safe with…" → `escalate_to_human(reason="medical_advice")`. Never answered by any other tool.
- **Restricted / prescription items** are never ordered or held → `escalate_to_human(reason="restricted_item")`. `create_order`/`hold_item` also return `restricted_item` as a safety net if called anyway.
- **Low confidence / unclear / abusive** → `escalate_to_human` with the matching reason.

## Error shape
Every action returns, on failure, an object:
```json
{ "error": { "code": "<code>", "message": "<human text>", "details": { } } }
```
Codes used across the contract: `not_found`, `out_of_stock`, `ambiguous_product`,
`restricted_item`, `invalid_input`, `unauthorized`. Per-action error cases are listed in each
schema's `errors` array. The agent must handle errors (e.g. retry a search on
`ambiguous_product`, escalate on `restricted_item`) rather than fabricate a reply.

## Branch model
Branches are Odoo **warehouses**. `create_order` with `fulfillment="pickup"` requires a
`branch_id` that has stock; with `fulfillment="delivery"` the adapter picks a branch that has
stock. `check_stock` without a branch returns availability per branch.
