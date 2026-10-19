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
    # original file when the customer sent a PDF (image then holds a PNG of its first page)
    document = fields.Binary(string="Prescription PDF", attachment=True)
    document_filename = fields.Char()

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
    # automatic checks (adapter/prescription.py): computerised prescriptions with an invoice
    # number, dated within the last 5 days, naming a catalogue medicine -> auto_verified
    invoice_number = fields.Char(index=True, help="Invoice / prescription number read by OCR.")
    prescription_date = fields.Date(help="Date printed on the prescription (read by OCR).")
    matched_product_ids = fields.Many2many(
        "product.product", string="Medicines found",
        help="Catalogue medicines named on the prescription. Once auto-verified, the agent may "
             "order/hold these (never Scheduled ones) for this customer, for one order.")

    # Human-in-the-loop: a pharmacist ALWAYS verifies before anything is acted on.
    state = fields.Selection([
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("auto_verified", "Auto-verified (OCR)"),
        ("verified", "Pharmacist-verified"),
        ("used", "Used (order placed)"),
        ("rejected", "Rejected"),
    ], default="draft", index=True)
    verified_by = fields.Many2one("res.users", string="Verified by (pharmacist)")
    notes = fields.Text()
