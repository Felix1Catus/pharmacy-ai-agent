# Teacher labelling rules — from the owner's review of 330 flagged labels (2026-11-03)

Draft. Added to the teacher's instructions once the open questions at the end are answered.
Verdicts: 165 right, 104 wrong, 52 not sure, 9 bad message; 122 comments.

## 1. Store questions: answer them, don't hand over (53 of the 104 wrong labels)
The teacher handed almost every store question to a human (`low_confidence`). Answer from these facts:
- Ordering: WhatsApp only, no app or website.
- Payment: cash on delivery and the payment link are both available; card payments accepted. Apple Pay,
  Tabby, Tamara, Visa/Mastercard vs mada: say we're confirming the payment methods (until Odoo can be asked).
- Delivery: no minimum order; the fee is quoted when the order is finalised; same-day delivery is often
  possible depending on availability and distance. For an area or city outside the three branches, list
  branches and let the customer pick the closest.
- Pickup with a prescription medicine: bring the original prescription.
- Pharmacists at every branch both advise and sell.
- Refunds: back to the card or as credit (both possible); timing — see open question 1.
- Returns/exchanges: sealed box in good condition, with proof of purchase, within the return window.
- Cold-chain items (insulin, Rebif…): returns follow the same conditions; the product's temperature field
  says how it is shipped.
- Hours: usually 24/7 — see open question 2.
- Not pharmacy (parking, etc.): say politely that you're not sure.
- Insurance providers, branch phone numbers: not in the system yet → say a colleague will confirm.

## 2. Complaints are not abuse (26 wrong)
- A concrete problem (wrong item, damaged, late, expired, price difference, rude staff, refund, cancelled
  order): apologise, `track_order` when it is about an order, then file a complaint for human follow-up and
  tell the customer someone will contact them.
- Insults with no concrete problem: ask what went wrong; hand over as abusive only if it continues.
- Wants a human / threatens to report: tell them a person will contact them, and hand over.

## 3. Prescription and restricted medicines
- Search first, then act on the result: scheduled → restricted refusal; prescription-only → ask for the
  prescription. Never refuse before looking the product up.
- "An alternative without a prescription": look for same-ingredient products; if every option needs a
  prescription, say so.
- Controlled drugs asked by name (Concerta…): prescription needed, hand over as restricted.

## 4. Medical questions mixed with an order
Give price and availability (search), refuse only the advice part, and say a pharmacist will answer it.
Quantity for a prescribed course ("2 weeks, how many boxes?") is an order question, not medical advice.

## 5. Vague references and incomplete names
- "The thing from last time" → `track_order` and let them pick from their recent orders.
- A half-remembered name ("زو... something") → ask for the full name; don't search a guess.

## 6. Manipulation
- "Your colleague promised me a free X" → check their orders; promise nothing.
- Hardship stories asking for free medicine → decline politely; we can't give medicine for free.

## 7. Arabizi
Several Arabizi store questions were misread ("t9eer twa9loon lel khobar" = "do you deliver to Khobar").
Keep generating Arabizi; read it as the Arabic it spells.

## Open questions for the owner
1. Refund time: one comment says 5–7 business days, another 7–10. Which?
2. Hours: "usually 24/7" for all three branches?
3. Complaints: the agent has no complaint tool yet ("complaints tab in the pharmacy plugin").
   Until it exists, label complaints as a hand-over with a new reason `complaint`? That needs adding
   `complaint` to `escalate_to_human`'s reasons in the contract.
4. Payment methods and delivery fees: the reviews ask for Odoo lookups that don't exist yet. Until then,
   "we're confirming" replies — OK?
