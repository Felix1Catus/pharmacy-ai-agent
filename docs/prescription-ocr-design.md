# Prescription OCR pipeline

## Implemented now (2026-10-19): auto-verification of computerised prescriptions
Owner decision: for **prescription-only, non-Scheduled** medicines the agent no longer hands the
purchase to a pharmacist when the customer attaches a valid prescription photo in the chat (📎).
- `adapter/prescription.py` — Tesseract OCR (ara+eng, CPU) + three deterministic checks:
  **date** within the last 5 days (Saudi time, not in the future; DOB / "valid until" ignored),
  an **invoice/prescription number** next to a label (Invoice No, رقم الفاتورة, Rx No…), and at
  least one **catalogue medicine** named on it (fuzzy, survives OCR slips).
- `Gateway.submit_prescription` stores every photo in Odoo (Pharmacy → Prescriptions) with the OCR
  text, invoice number, date, medicines found and state `auto_verified` or `submitted` (+ why).
  An invoice number already used for an order, or sent by another customer, is refused.
- `create_order` / `hold_item` allow a prescription-only item only if this customer has an unused
  `auto_verified` (or pharmacist-`verified`) prescription, within 5 days, listing that product;
  the order consumes it (`state=used`, linked order). Otherwise → `prescription_required` and the
  agent asks for the photo. **Scheduled medicines stay fully blocked** regardless.
- Known limits: handwritten scripts are not supported; quantities on the prescription are not
  enforced; OCR cannot detect a forged/edited image — pharmacists can review the Prescriptions list.
- Sample image for testing: `python evals/datasets/make_prescription.py rx.png [--days-ago 9]`.

The original design below (pharmacist verifies everything) still applies to Scheduled medicines
and to anything the automatic checks reject.

---

# Original design (pharmacist-verified pipeline)

Goal: let a customer send a photo of a prescription over WhatsApp; the system reads the drug,
dosage and quantity, and uses that to help — **without the AI giving medical advice** and
**without ever auto-dispensing scheduled/controlled or prescription-only medicines**. A
pharmacist always verifies before anything is acted on.

Status: **not implemented.** The data model spots exist (`pharmacy.prescription` in the
`pharmacy_agent` Odoo module); real OCR/vision is deferred. This doc records the intended design
so it can be built as a self-contained, production-grade, packageable service.

## Why this is hard / must be done carefully
- Doctors' handwriting is notoriously inconsistent; general OCR fails on it. Reliable extraction
  needs a model tuned for medical handwriting plus structured validation, not a plain OCR call.
- A wrong dosage read is a safety hazard. Therefore the pipeline is **assistive, never
  authoritative**: it drafts, a human pharmacist confirms. No dosage is ever spoken to the
  customer by the agent (that stays an `escalate_to_human(medical_advice)`).

## Flow
1. **Capture** — WhatsApp media webhook downloads the image (T-C1 ingest path). Store it on a
   `pharmacy.prescription` record (`image`), linked to the customer (`partner_id`).
2. **Pre-process** — deskew, denoise, contrast-normalize, crop to the script region.
3. **Extract** — a vision/OCR step writes `ocr_text`, `extracted_drug`, `extracted_dosage`,
   `extracted_quantity`, `ocr_engine`, `ocr_confidence`. Printed scripts: high-accuracy OCR.
   Handwritten: a handwriting-capable model; low confidence routes to human review.
4. **Classify** — map the extracted drug against the catalogue. If it is `is_scheduled` →
   **stop**, `state=submitted`, pharmacist-only; the agent never proceeds. If prescription-only
   (`x_requires_prescription`) → pharmacist verifies the script is valid.
5. **Human verification (mandatory)** — a pharmacist reviews the image + extraction in Odoo and
   sets `state=verified` (or `rejected`), `verified_by`. Nothing is ordered before this.
6. **Act** — only after `verified`, and only for non-scheduled items, the agent may proceed with
   the normal order flow for the verified quantity.

## Confidence gating
- `ocr_confidence` below a threshold, or any handwriting, or any scheduled/controlled drug →
  `ocr_status=needs_review`/`unreadable` → the agent replies with the standard line:
  *"sorry, I can't read/advise on this; I'll refer you to a pharmacist. I can help you find
  medicines, prices and availability (except scheduled medicines)."* (see agent/guards.py
  SCHEDULED_REFUSAL / MEDICAL_REFUSAL).

## Packaging
Build as a standalone service (own repo/container) exposing e.g. `POST /ocr` → structured JSON,
so it is swappable and testable in isolation. The Odoo module stores inputs/outputs; the agent
only reads the **verified** result. Keep all images out of git (gitignored); real prescriptions
are sensitive — mock/sample images only in this phase.

## What exists now
- `odoo/addons/pharmacy_agent/models/prescription.py` — the `pharmacy.prescription` model with
  image + OCR + verification fields and a Prescriptions menu in Odoo.
- Scheduled/controlled flagging (`is_scheduled`, `schedule_class`) with deterministic agent
  guards that already refuse scheduled meds and medical-advice questions (agent/guards.py).
