from odoo import fields, models


class PharmacyPrescription(models.Model):
    _name = "pharmacy.prescription"
    _description = "Customer prescription (image + OCR extraction, pharmacist-verified)"
    _order = "create_date desc"

    name = fields.Char(default="New", required=True)
    partner_id = fields.Many2one("res.partner", string="Customer", index=True)
    sale_order_id = fields.Many2one("sale.order", string="Related order")

    # The captured image (from WhatsApp). Stored as an attachment-backed image.
    image = fields.Image(string="Prescription image", max_width=2000, max_height=2000)
    image_filename = fields.Char()

    # OCR pipeline results (populated by the future OCR service — see docs/prescription-ocr-design.md).
    ocr_status = fields.Selection([
        ("pending", "Pending"),
        ("ocr_done", "OCR done"),
        ("needs_review", "Needs human review"),
        ("unreadable", "Unreadable"),
    ], default="pending")
    ocr_engine = fields.Char(help="Which OCR/vision model produced the extraction.")
    ocr_confidence = fields.Float(help="0..1 confidence from the OCR pipeline.")
    ocr_text = fields.Text(string="Raw OCR text")
    extracted_drug = fields.Char()
    extracted_dosage = fields.Char()
    extracted_quantity = fields.Char()

    # Human-in-the-loop: a pharmacist ALWAYS verifies before anything is acted on.
    state = fields.Selection([
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("verified", "Pharmacist-verified"),
        ("rejected", "Rejected"),
    ], default="draft", index=True)
    verified_by = fields.Many2one("res.users", string="Verified by (pharmacist)")
    notes = fields.Text()
