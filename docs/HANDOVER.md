# Handover: state of the project and the next stages

Branch `fix/audit-hardening`, on top of `feat/odoo-pharmacy-tools` (PR #2, itself stacked on PR #1
`fix/agent-abuse-and-order-status`); `main` is behind all three - merge them in that order.
Odoo module `pharmacy_agent` **18.0.1.12.0** (upgrade with `-u pharmacy_agent`). Model: Qwen3-8B
Q4_K_M on llama-server (:8080); the v2 QLoRA adapter was evaluated and **not** adopted.

## Hardening round (2026-11-12)

| Area | What changed |
|---|---|
| Customer identity | recognised by WhatsApp number or main phone only (never the unverified alternative phone); a WhatsApp chat is always its own sender number, also for formats `normalize_phone` rejects (`phone_key`) |
| Prescriptions | `customer_confirms` refuses an unapproved prescription; Odoo's `action_confirm` refuses it too (and a chat order with a scheduled medicine); a prescription can only be attached to its own customer's order; only the *pharmacist* group reads / approves prescriptions |
| Odoo access | the services log in as `pharmacy-assistant` (`scripts/create_service_user.py`), not the administrator; Odoo may be on another server (`docs/odoo-connection.md`: https, API key, CA bundle, retries, `ODOO_PUBLIC_URL`) |
| WhatsApp channel | conversations saved to disk; errors answered with a fixed reply and logged; sends checked; 24-hour window respected with a template; review results and **back-in-stock messages** pushed by the notifier; systemd unit + `/webhook` proxy route |
| Model context | the model is sent a window of the chat (`AGENT_HISTORY_CHARS`), never an over-long history; a model or Odoo outage gives a fixed "busy" reply |
| Health data | prescription contents no longer in the audit log; waiting photos never written to session files; private files (0600); daily retention timer covering all chat files |
| Stock | one query per branch for any number of products; holds really keep stock aside (`hold_expires_at`) and are released by a cron |
| Guards | a qualified or negated "yes" no longer confirms; "للحامل" / "مع بعض" are medical only as a question; short denylist terms need a whole word |
| Code | `respond()` is a list of named steps (`PRE_MODEL_STEPS`) + `_model_turn`; `logging` everywhere an error used to be swallowed; `pyproject.toml` (pytest paths, ruff); CI workflow; `tests/test_audit_fixes.py` |

Verified on a local Odoo 18 (2026-11-12): 365 passed, 38 skipped (SFDA catalogue, Tesseract and
voice models not installed there), running as the service account. **Not re-run:** the
106-message agent eval and `conversation_eval.py` (they need the model server) - run both before
adopting, since the guard changes and the history window can move results.

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
| Notifier (WhatsApp push / test-chat poll) | `pending_notifications`, `mark_notified`, `due_stock_alerts`, `mark_stock_alert_sent` |

## Run / verify

```bash
.venv/bin/python -m pytest agent adapter -q
.venv/bin/python evals/runners/conversation_eval.py
```

Policy and scenario docs: `contract/contract.md`, `docs/assistant-scenarios.md`.

## Open items for the next round

- Run the agent eval and `conversation_eval.py` on this branch (not re-run after the hardening round).
- The remaining `sys.path` set-up in the runtime modules and scripts (tests no longer need it):
  turning `agent/`, `adapter/`, `channels/` into one installable package means changing every
  import and the systemd `--app-dir` lines, so it should be its own change with a deploy test.
- `finetune/` (CPU LoRA) and `train/` (GPU QLoRA) overlap; `results/` and the datasets are tracked
  in git. Decide what to keep before pruning - the pipelines read those paths.
- WhatsApp: create and get the "you have an update" template approved (`WHATSAPP_TEMPLATE_NOTIFY`).
- Branch phones and addresses are empty in Odoo; 42 demo products lack storage / pack data.
- Ingredient-only prescription matches unlock every strength and form of that ingredient.
- conversation_eval: 2 known failures (complaint status wording; pack / heat question inside the order wizard).
- Production: llama-server runs `-np 1` (one chat at a time), no automated Odoo backups, no
  monitoring or rate limits. A fresh GPU bootstrap now rotates the Odoo administrator password; on
  the **existing** server change it by hand and run `scripts/create_service_user.py --write-env`.
- Hardware for pre-production: one L40S 48 GB server (serving + Odoo, ~US$400–800/month), training
  on an hourly-rented H100.
