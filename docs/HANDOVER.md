# Handover: state of the project and the next stages

Branch `feat/odoo-pharmacy-tools` (PR #2, stacked on PR #1 `fix/agent-abuse-and-order-status`).
Odoo module `pharmacy_agent` 18.0.1.10.0. Model: Qwen3-8B Q4_K_M on llama-server (:8080); the v2
QLoRA adapter was evaluated and **not** adopted.

## Tool calls the system accepts from the agent

### 1. Model tool calls (JSON schemas in `contract/schemas/`, executed by `adapter/gateway.py`)

| Tool | Kind | Who | What it does |
|---|---|---|---|
| `search_product` | read | customer + staff | Find products by name, brand or active ingredient (ar/en). Never for a bare symptom. |
| `check_stock` | read | customer + staff | Stock per branch for a `product_id`. |
| `get_product_details` | read | customer + staff | Label facts: ingredient, strength, form, pack, price, dispensing class, storage / fridge / temperature, shelf life, maker. Missing facts come back as `NOT RECORDED`. Scheduled items are refused. |
| `list_branches` | read | customer + staff | Branches: city, district, address, hours, phone, delivery. Optional city filter. |
| `track_order` | read | customer + staff | Recent orders and status, plus open requests to staff (handovers). |
| `get_order_details` | read | customer + staff | One order in full (items, prices, branch/address, prescription review, `can_cancel`). Exact ref match, own orders only. |
| `list_prescriptions` | read | customer + staff | Prescriptions on file with review status and matched medicines. |
| `request_stock_alert` | write | customer + staff | Back-in-stock request for an out-of-stock product (one branch or any). |
| `update_delivery_address` | write | customer + staff | Save the new delivery address on the customer's account. |
| `cancel_order` | write, confirmation required | customer + staff | Cancel a quotation (`can_cancel=true`) after the customer confirmed which order. |
| `escalate_to_human` | write | customer + staff | Handover (`pharmacy.handover`, HO-xxxx). Requires an `order_id` or `medicine`, otherwise returns `needs_context`. |
| `create_order` | write, confirmation required | staff (customers go through the order wizard) | Create a quotation from confirmed items, qty and pickup branch / delivery. |
| `hold_item` | write, confirmation required | staff | Reserve a product at a branch for N hours. |
| `create_dispatch` | write, confirmation required | staff | Send a confirmed order for delivery. |

Customer tool set: `CUSTOMER_TOOLS` in `agent/agent.py`. Account tools get the customer id injected
by the agent (the model never chooses it). Confirmation-gated tools: `_NEEDS_CONFIRMATION`.

### 2. Gateway calls made by the fixed wizards and guards (never chosen by the model)

| Area | Calls |
|---|---|
| Customer identity / consent | `find_customer`, `register_customer`, `has_health_consent`, `record_health_consent`, `withdraw_health_consent` |
| Order wizard + cart (quotes) | `search_product`, `check_stock`, `cart_sync`, `get_cart`, `create_order(cart_id=…)`, `active_order` |
| Cancel wizard | `get_order_details`, `cancel_order` (via `_run_tool`), `escalate_to_human` for confirmed orders |
| Prescriptions (OCR) | `submit_prescription`, `attach_prescription`, `link_prescription`, `valid_prescription`, `prescription_directions` |
| Scheduled (مقيد) medicines | `scheduled_availability` (availability per branch only; no order, no escalation) |
| Notifier (to be wired) | `due_stock_alerts` |

## Run / verify

```bash
.venv/bin/python -m pytest agent adapter -q
.venv/bin/python evals/runners/conversation_eval.py
```

Policy and scenario docs: `contract/contract.md`, `docs/assistant-scenarios.md`.

## Open items for the next round

- WhatsApp notifier does not yet send `due_stock_alerts`.
- Branch phones and addresses are empty in Odoo; 42 demo products lack storage / pack data.
- Ingredient-only prescription matches unlock every strength and form of that ingredient.
- conversation_eval: 2 known failures (complaint status wording; pack / heat question inside the order wizard).
- Production: llama-server runs `-np 1` (one chat at a time), no automated Odoo backups, no
  monitoring or rate limits, Odoo login is public with a weak password.
- Hardware for pre-production: one L40S 48 GB server (serving + Odoo, ~US$400–800/month), training
  on an hourly-rented H100.
