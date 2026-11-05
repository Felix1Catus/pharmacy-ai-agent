# Plan: complaints module and payment / delivery lookups

Status (2026-11-03): planned. Until these exist, the agent uses stop-gaps:
- complaints: `escalate_to_human` with `reason=complaint` (written to `results/handovers.jsonl`, the
  customer gets an apology and "a colleague will contact you");
- payment methods beyond cash on delivery / the payment link / cards, and delivery fees: the agent says
  we are confirming (STORE FACTS in `agent/agent.py`).

Both come from the owner's review of 330 distillation labels (`/home/bench/distill/teacher_rules.md`).

## 1. Complaints module (Odoo `pharmacy_agent`)
**Goal:** every complaint becomes a record a person owns and closes, and the customer gets a number.

Odoo (`odoo/addons/pharmacy_agent`):
- New model `pharmacy.complaint`: name (sequence, e.g. `CMP-00042`), partner_id, sale_order_id
  (optional), category (wrong item, damaged, late delivery, price difference, staff, refund, expired,
  prescription/OCR, other), priority (normal / high — high when a wrong strength or a chronic or child's
  medicine is involved), description (customer's words + agent summary), channel (WhatsApp / test chat),
  state (new → in progress → resolved / rejected), user_id (owner), resolution note, resolved date.
- Menu **Pharmacy → Complaints** (list, kanban by state, filters by category/priority/branch), plus a
  smart button on the sales order and on the contact.
- Activity / email to the branch manager on a new high-priority complaint.
- Access rights: pharmacy users read/write, managers configure categories.

Agent / gateway:
- New contract action `create_complaint(category, summary, sale_order_id?, priority?)` returning
  `{complaint_id, number}`; schema in `contract/schemas/create_complaint.json`.
- Gateway writes the record over JSON-RPC; `escalate_to_human reason=complaint` becomes a thin wrapper
  that calls it, so existing labels and prompts keep working.
- Reply template: apology + complaint number + who follows up.
- When the customer is not identified: run the customer flow first (as orders do), then file it.

Refunds: `create_complaint(category="refund")` records the request; the customer is told 5 to 7
business days.

Tests: model unit tests (`tests_manual`), gateway tests with a live Odoo, agent tests for order
complaints (track first, then file) and for the not-identified path.

## 2. Payment methods and delivery-fee lookups
**Goal:** answer "do you take Apple Pay / Tabby / Tamara?" and "how much is delivery to Al Narjis?"
from Odoo instead of "we're confirming".

Odoo:
- Payment methods: read the enabled `payment.provider` / `payment.method` records (Odoo 18 payment
  module), plus a boolean "cash on delivery" on the company or per branch.
- Delivery: `delivery.carrier` (Odoo `delivery` module) with fixed or rule-based prices per zone
  (city / district), a minimum-order rule if the owner wants one, and an "available same day" flag.
  Zones keyed by branch so the closest branch decides the fee.

Agent / gateway:
- New actions `payment_methods()` → `[{name, available_for: delivery|pickup|both}]` and
  `delivery_quote(city, district?, branch_id?)` → `{fee_sar, same_day, branch_id}`.
- Customer-facing tool set gains both (read-only, no confirmation needed).
- STORE FACTS keeps only what never changes; the rest comes from these calls.

## Order of work
1. Complaints model + menu + `create_complaint` (largest customer impact: 26 of 104 wrong labels).
2. Payment methods lookup (small).
3. Delivery zones and quotes (needs the owner's zone/fee table).
4. Re-label the affected distillation examples with the new tools and re-run the agent eval.
