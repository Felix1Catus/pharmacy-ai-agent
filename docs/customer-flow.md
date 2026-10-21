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
**Order wizard** (`agent/order_flow.py`, fixed steps): a message that clearly names a catalogue
product is searched directly (no model call); otherwise the model only turns the words into a
search. Results are a numbered list with prices + buttons (a clearly named product is selected
at once). Details already in the message are reused («علبتين بنادول استلام من فرع جدة» = 2,
pickup, Jeddah); only missing ones are asked with buttons: quantity → 🚚 توصيل / 🏪 استلام →
branch (real branches) or saved address → stock check (other branches offered) → summary
(total before VAT) with ✅ تأكيد الطلب / ➕ منتج آخر / ❌ إلغاء. **Only ✅ creates the quotation**;
in customer chats the model has no order tools at all. A typical order takes ~1 s end to end.
1. The customer orders (menu 1 or free text); the agent searches, checks stock, asks
   pickup/delivery and **asks for a clear "yes"** before placing anything.
2. If the customer is not identified yet, the confirmed order is held and the registration
   runs (section 3) — «قبل ما نثبّت طلبك نحتاج بياناتك للفاتورة والتوصيل 🧾». Then the order is
   placed automatically under the new contact; a delivery goes to the address just entered.
3. The order is created in **Sales as a Quotation** (not confirmed), with:
   customer contact · lines · branch · `wa_order` ✔ · fulfilment (pickup / delivery) ·
   delivery address as given · `dispatch_status` = *Awaiting pharmacy approval* for deliveries.
4. The customer gets a **fixed message** (never model-written):
   - **no prescription needed:** order number, total, pickup branch / delivery address and the
     **payment link** — «💳 لإتمام الطلب ادفع من هذا الرابط … بعد الدفع يتم تأكيد الطلب وإصدار الفاتورة»;
   - **prescription-only medicine:** see section 4b.
   They follow orders from «📦 تتبع طلب». The assistant never promises tracking links, SMS or
   delivery times (a filter removes them).
5. Once identified, the customer stays identified for the whole chat: the model is told their
   name and saved address every turn, never asks for them again, and every quotation is
   created under that contact (the model cannot choose another customer).

## 4b. Prescription-only medicines → pharmacist review → confirm & pay
1. The customer orders as usual (the bot never refuses a prescription-only medicine). On "yes"
   the quotation is created with **Prescription review = Pending approval** if the customer
   already sent the prescription photo/PDF in this chat, otherwise **Awaiting prescription
   photo** — and the bot asks for it («أرسل صورة الوصفة أو ملف PDF من زر المرفقات 📎»).
2. The photo is read by OCR as a **pre-check** (date within 5 days, invoice/Rx number,
   medicines). Obvious problems (too old, unreadable, no invoice number) are answered at once;
   otherwise it is attached to the quotation → *Pending approval*. OCR never approves anything.
3. The bot **closes the conversation**: «استلمنا طلبك رقم S000xx مع صورة الوصفة. سجّلنا بياناتك ورقم
   جوالك +966…، وسيراجع الصيدلي الوصفة الآن. سنراسلك هنا فور مراجعتها لتأكيد الطلب والدفع».
4. The pharmacist reviews it in Odoo (**Pharmacy → Prescription review**) and clicks
   **✅ Approve prescription** or **❌ Reject prescription** (the reason is required and is sent).
5. The bot messages the customer automatically (WhatsApp push; the test chat checks every 8 s):
   - **approved:** «✅ وافق الصيدلي على وصفتك! عرض السعر رقم … • items … 💰 الإجمالي … 🏪/🚚 … هل تؤكد
     الطلب وتنتقل للدفع؟» with **✅ أؤكد وأدفع / ❌ إلغاء الطلب**. Confirm → the **payment link**;
     cancel → the quotation is cancelled.
   - **rejected:** the reason, and a new prescription photo goes back to the same quotation.
6. **Payment (next phase):** the link is the quotation's Odoo customer page
   (`/my/orders/<id>?access_token=…`), where Odoo takes online payment once a payment provider
   is configured. Recording the payment, confirming the order and creating the invoice
   automatically is the next step; `payment_state_wa` (*Payment link sent → Paid*) is ready for it.
A prescription already approved by a pharmacist (valid 5 days, one order) skips the review.
Scheduled (مقيد) medicines never enter this flow — they stay fully blocked.

## 5. In Odoo (staff)
- **Pharmacy → Prescription review:** the reviewer's queue (default filter *Pending approval*):
  open a quotation → tab *Prescription review* (prescription image, OCR pre-check, medicines
  found) → **Approve** / **Reject** (write the reason first). A ribbon shows *Pending approval*,
  *Awaiting prescription* or *Rx rejected*.
- **Pharmacy → WhatsApp orders:** all quotations from the assistant (customer, review badge,
  fulfilment, delivery address, payment state, total). Tab *WhatsApp / Delivery / Payment*
  shows the payment link, whether the customer confirmed, and whether the message was sent.
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
the same OCR checks); replies as text, list message (menu) or reply buttons (flow). A background
loop (every 30 s, `WHATSAPP_NOTIFY_EVERY_S`) sends the pharmacist's decisions to the customer's
WhatsApp number (`whatsapp_number`, else phone).
