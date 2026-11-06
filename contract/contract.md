# Tool contract

The agent (Qwen3) may call **only** these fourteen actions. Each has a JSON schema in
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
| `search_product` | no | no | resolve the product first; never guess between candidates. `need` (fixed list) = what the customer wants something FOR when no product is named; the system picks over-the-counter products (adapter/needs.py), never the model |
| `check_stock` | no | no | never invent stock numbers |
| `create_order` | **yes** | yes | restricted/prescription items → escalate, do **not** order |
| `create_dispatch` | **yes** | yes | only after a successful `create_order` |
| `hold_item` | **yes** | yes | restricted/prescription items → escalate |
| `track_order` | no | no | read-only |
| `escalate_to_human` | no | (hands over) | terminal for the turn; creates a `pharmacy.handover` in Odoo (staff queue: Pharmacy → Handovers) |
| `get_product_details` | no | no | label facts of up to 3 products from `search_product`: ingredient, strength, form, pack, price, dispensing class, storage (fridge / room, min-max °C, light, moisture, no-freeze), shelf life, maker + country. Missing facts come back as `NOT RECORDED`. Scheduled → `scheduled_item` |
| `get_order_details` | no | no | one of the customer's orders in full (items, pharmacist's note, payment, delivery progress, `can_cancel`) |
| `cancel_order` | **yes** | yes | only a quotation; the customer asked to cancel AND said yes to the bot's "cancel S0…?"; confirmed orders → `not_cancellable` → handover |
| `list_prescriptions` | no | no | the customer's prescriptions and their review status |
| `request_stock_alert` | no | records a request | "tell me when it's back" → `pharmacy.stock.alert`; `already_available` if it is in stock |
| `update_delivery_address` | no | yes (own contact) | only an address the customer wrote in this chat |

`list_branches` takes an optional `city` and returns city, district, address, hours, phone and
delivery per branch; `track_order` also returns the customer's latest request of each kind
(complaint, medical question...) and its state. Account tools (`get_order_details`,
`cancel_order`, `list_prescriptions`, `request_stock_alert`, `update_delivery_address`) run only
for an identified customer, whose id the agent injects; product ids must come from a search
result in the same conversation (`unknown_product` otherwise).

## Guardrails encoded in the contract
- **No medical advice.** Dosage / diagnosis / "is this safe with…" → `escalate_to_human(reason="medical_advice")`. Never answered by any other tool.
- **Restricted / prescription items** are never ordered or held → `escalate_to_human(reason="restricted_item")`. `create_order`/`hold_item` also return `restricted_item` as a safety net if called anyway.
- **Low confidence / unclear / abusive** → `escalate_to_human` with the matching reason.

## Customer identity and quotations (enforced by the agent, not the model)
- `create_order`, `hold_item`, `track_order`, `create_dispatch` only run for an **identified**
  customer (WhatsApp number or the «عميل حالي / جديد» flow); the agent always injects that
  customer's id, whatever the model passes. A guest's confirmed order triggers registration
  first and is then placed automatically (`docs/customer-flow.md`).
- `create_order` creates a **quotation** (draft `sale.order`, `wa_order`, fulfilment, delivery
  address, `dispatch_status`), returning `order_ref`; a human confirms / invoices it.
- Prescription-only items need a verified prescription (`prescription_required` otherwise);
  Scheduled items always return `scheduled_item`.

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
