# What the assistant does with Odoo, scenario by scenario

The tools the model can call, and which Odoo fields they read and write
(`pharmacy_agent` 18.0.1.9.0). Each scenario is one chat thread that may trigger several tools
in a row. The ✅ scenarios are tested end to end against the real model and Odoo by
`evals/runners/conversation_eval.py`: 11 conversations, 32 turns.

## Odoo fields the assistant reads

| Model | Field(s) | Used for |
|---|---|---|
| product.template | `dispensing_class` (otc / prescription / scheduled), computed from `is_scheduled`, `sfda_requires_prescription`, `rx_required_manual` | what the assistant may do: OTC orders freely, prescription → order + pharmacist reviews the photo, scheduled → never priced, stocked, described or ordered |
| product.template | `storage_class`, `storage_min_c` / `storage_max_c`, `storage_protect_light`, `storage_protect_moisture`, `storage_no_freeze`, `cold_chain`, parsed from `sfda_storage` or overridden by `storage_note` | "does it need the fridge?", "can it take the heat?" (2,013 SFDA products: 1,870 room, 142 fridge, 1 freezer) |
| product.template | `shelf_life_months`, `manufacturer_country`, `sfda_manufacturer`, `sfda_pack`, `sfda_strength`, `sfda_dosage_form`, `sfda_route`, `sfda_reg_no` | expiry, maker and country, pack size, strength, form |
| sale.order | `rx_review_state` + `rx_review_note`, `payment_state_wa`, `dispatch_status`, `needs_cold_chain`, `cancel_reason_wa` | order detail, cancel, what the pharmacist said |
| stock.warehouse | `pharmacy_city`, `pharmacy_district`, `pharmacy_address`, `pharmacy_phone`, `pharmacy_hours`, `pharmacy_maps_url`, `pharmacy_delivery` | branches by city, hours, delivery |
| pharmacy.handover (new) | reason, summary, order, state, resolution | every handover is a staff task (Pharmacy → Handovers); "did anyone answer my complaint?" |
| pharmacy.stock.alert (new) | customer, product, branch, state | back-in-stock requests (Pharmacy → Back-in-stock requests) |
| pharmacy.prescription | state, matched medicines, order | "what happened to my prescription?" |

Not filled yet, so the assistant says a colleague will confirm: branch phone, address and map
link (fill them in Inventory → Warehouses → Pharmacy branch), and storage or pack for the 42
demo products that have no SFDA record (fill **Storage (pharmacy note)** on the product).

## Scenarios

### Product facts
- ✅ Fridge medicine (Victoza): needs the fridge → 2-8 °C; shelf life 30 months; maker Novo
  Nordisk, Denmark/Vietnam; needs a prescription. Then a dose question → pharmacist handover.
- ✅ Two products compared (Zyrtec / Claritine): tablets per box, prescription status, which one
  is made in Saudi Arabia, price.
- ✅ Same in English (fridge, pens per box, maker).
- ✅ A fact we don't hold (Panadol Extra pack size, heat): "not recorded, the pharmacist will
  confirm". An invented number or storage claim is caught by the fact check and replaced.
- ✅ Storage question + dose question in one message → the dose wins (handover).
- ✅ Scheduled medicine (Xanax, Tramadol): no storage or maker either; refused and handed over.
- ✅ Product question in the middle of an order; the order wizard carries on afterwards.
- Planned: "is X in stock and does it need the fridge" in one message (stock + details);
  "a cheaper alternative with the same ingredient" (search alternatives + details).

### Orders already placed
- ✅ Order detail by number: items, total, branch, status.
- ✅ Cancel: details → the bot names the order and asks → "yes" → cancelled in Odoo → status
  shows cancelled. Cancel is refused if the customer never asked, or before their "yes".
- ✅ "Order 1 Centrum for pickup at Olaya" is a NEW order: searched and taken by the wizard,
  never read as order #1.
- Planned: change the quantity or the address of a quotation (today: cancel + new order);
  reorder "the same as last time" directly from `get_order_details` items.

### Stock and branches
- ✅ Out of stock everywhere: said straight away (not after quantity and delivery), with
  🔔 notify me → `pharmacy.stock.alert`.
- ✅ Branch by city (Jeddah → Tahlia, 24/7, delivers); a city with no branch (Tabuk).
- Planned: the WhatsApp notifier sends `due_stock_alerts()` (gateway) like the prescription
  review results, then `mark_stock_alert_sent`.

### Account
- ✅ Prescription status ("waiting for the pharmacist").
- ✅ Complaint follow-up: the latest complaint's state from `pharmacy.handover`.
- ✅ New delivery address saved on the contact; claiming "updated" without the tool is blocked.

## Safety rails added with these tools
- Product ids must come from a search result in the same chat; the address must be the
  customer's own words.
- `cancel_order` needs the customer's cancel request plus their "yes" to the bot's question.
- A reply claiming an address change, cancellation or stock request that did not happen is
  regenerated once, then replaced with "nothing has been changed yet".
- Product answers: every number must appear in a tool result (or in the customer's message),
  and storage claims need storage data. Otherwise the model gets one retry, then the reply
  becomes "not recorded, the pharmacist will confirm".
- Opening-hours replies ("24 hours daily") no longer trip the dosing filter; "جرعته" ("its
  dose") is caught by the medical guard.
