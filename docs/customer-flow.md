# Customer recognition, registration and WhatsApp orders

How a customer is recognised, registered and billed, from the first WhatsApp message to the
quotation a pharmacist confirms. Code: `agent/customer.py` (flow), `agent/menu.py` (menu),
`adapter/gateway.py` (Odoo contacts + quotations), `channels/whatsapp/webhook.py` (WhatsApp),
`odoo/addons/pharmacy_agent` (fields + menus). Every step here is fixed logic — the language
model is never involved, so it is instant and cannot be talked around.

## 1. Opening menu (every conversation)
Greeting + five items (a WhatsApp *list message*; buttons on the test chat):

| # | Arabic | English | What happens |
|---|---|---|---|
| 1 | 🛒 طلب جديد | 🛒 New order | asks for the medicine name → agent searches / quotes |
| 2 | 📦 تتبع طلب | 📦 Track order | lists the customer's own recent orders (identifies them first if needed) |
| 3 | ❓ أسئلة واستفسارات | ❓ Questions / inquiries | free questions about products, prices, availability, branches |
| 4 | 👤 عميل حالي / جديد | 👤 Existing / new customer | identify by phone → confirm saved profile, or register |
| 5 | 🌐 English / العربية | | switches every reply (fixed texts and the model) |

The menu comes back after 6 hours of inactivity, or when the customer types «القائمة» / "menu".

## 2. Who is the customer?
- **WhatsApp (production):** the sender's number (`wa_id`, e.g. `966501234567`) is looked up in
  Odoo Contacts (`whatsapp_number`, `phone`, `mobile` — any format: `0501234567`,
  `+966 50 123 4567`, `00966…` all match). A known customer is greeted by name
  («أهلاً عبدالعزيز 👋») and their saved address is reused.
- **Test chat (demo):** a conversation starts as an unknown guest, like a message from a new
  number. The header field «📱 WhatsApp no. (sim)» simulates the sender number, so the same
  lookup runs.
- Until identified, account-bound actions (order, reservation, tracking, dispatch) never run;
  the chat asks the customer to identify first and then resumes what they asked for.

## 3. «عميل حالي / جديد» — existing or new customer
```
phone number (skipped on WhatsApp: the sender's number is used)
   ├─ found     → "أهلاً {name}, your saved details: 📍 address · 📞 alt phone — use them for this order?"
   │               ✅ نعم، اعتمدها → identified          ✏️ تعديل العنوان → new address saved to the contact
   └─ not found → 👤 full name → 📍 address (city, district, street, building) → 📞 alt phone (or ⏭️ تخطي)
                   → summary: ✅ حفظ → Odoo contact created (registered_via = WhatsApp assistant)
                              ✏️ إعادة الإدخال → start again from the name
```
The Odoo contact gets: name, phone (`+966…`), `whatsapp_number`, alternative phone (`mobile`),
address (`street`; city detected from the text), country Saudi Arabia, customer flag.

## 4. Ordering → registration at checkout → quotation
1. The customer orders (menu 1 or free text); the agent searches, checks stock, asks
   pickup/delivery and **asks for a clear "yes"** before placing anything.
2. If the customer is not identified yet, the confirmed order is held and the registration
   runs (section 3) — «قبل ما نثبّت طلبك نحتاج بياناتك للفاتورة والتوصيل 🧾». Then the order is
   placed automatically under the new contact; a delivery goes to the address just entered.
3. The order is created in **Sales as a Quotation** (not confirmed), with:
   customer contact · lines · branch · `wa_order` ✔ · fulfilment (pickup / delivery) ·
   delivery address as given · `dispatch_status` = *Awaiting pharmacy approval* for deliveries.
   Prescriptions used are noted on it and marked *Used*.
4. The customer gets a **fixed confirmation** (never model-written): their name, order number,
   total, delivery address or pickup branch, and that the pharmacy will confirm it. They follow
   it from «📦 تتبع طلب» (shown as «بانتظار تأكيد الصيدلية» until a human confirms it). The
   assistant never promises tracking links, SMS or delivery times (a filter removes them).
5. Once identified, the customer stays identified for the whole chat: the model is told their
   name and saved address every turn, never asks for them again, and every quotation is
   created under that contact (the model cannot choose another customer).

## 5. In Odoo (staff)
- **Pharmacy → WhatsApp orders:** all quotations from the assistant (customer, fulfilment,
  delivery address, dispatch status, total). Open one → tab *WhatsApp / Delivery*.
- **Pickup:** confirm the quotation and invoice it at the dispensary when the customer comes.
- **Delivery:** a human approves it (confirm) — then it is handed to the Delivery Dispatcher.
- **Contacts:** customers registered by the assistant show *Registered via: WhatsApp assistant*
  and their WhatsApp number.

## 6. Next module (context only — not built): Delivery Dispatcher
Approved delivery orders move to a new Odoo module for drivers: a driver accepts a job, sees
pickup (branch) and drop-off (customer address), is tracked in real time, and marks it
delivered. `sale.order.dispatch_status` already carries the hand-off states for it:
`awaiting_approval → ready → assigned → picked_up → delivered`.

## 7. WhatsApp channel (Meta Cloud API)
`channels/whatsapp/webhook.py` — run: `.venv/bin/uvicorn --app-dir channels/whatsapp webhook:app --port 8091`
(put HTTPS in front: Meta only calls `https://…/webhook`). Settings in `.env`:
`WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` (signature check), `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`. Without the token / phone id it runs in **dry run**: nothing is
sent and every reply is written to `results/whatsapp_outbox.jsonl` (tests use this).
Handles text, menu/button taps, and prescription photos/PDFs (downloaded from Meta and sent to
the same OCR checks); replies as text, list message (menu) or reply buttons (flow).
