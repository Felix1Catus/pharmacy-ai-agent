from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    # Controlled/scheduled substance: the STRICTEST class. The AI agent must never order, hold
    # or advise on these — only refer to a human. Source: official controlled-substance lists
    # (populated by the pharmacy; dummy data in this phase).
    is_scheduled = fields.Boolean(
        string="Scheduled / controlled substance",
        help="Controlled substance per official scheduling. The AI agent hard-blocks and "
             "escalates to a human; never ordered or held through the agent.")
    schedule_class = fields.Char(
        string="Schedule class",
        help="Free-text schedule/classification label, e.g. 'controlled', 'narcotic'.")

    # --- SFDA registry data (filled by the SFDA import: data_sources/sfda/) ---
    sfda_reg_no = fields.Char(string="SFDA registration no.", index=True)
    sfda_scientific_name = fields.Char(string="Scientific name (SFDA)")
    sfda_strength = fields.Char(string="Strength")
    sfda_dosage_form = fields.Char(string="Dosage form")
    sfda_route = fields.Char(string="Route of administration")
    sfda_pack = fields.Char(string="Pack")
    sfda_dispensing = fields.Char(string="Dispensing (طريقة الصرف)")
    sfda_requires_prescription = fields.Boolean(string="Prescription required (SFDA)")
    sfda_control = fields.Char(string="Control status (المراقبة)",
                               help="Raw SFDA value; 'مقيد' (restricted) sets Scheduled.")
    sfda_distribution = fields.Char(string="Distribution (مكان التوزيع)")
    sfda_atc_code = fields.Char(string="ATC code")
    sfda_shelf_life = fields.Char(string="Shelf life (months)")
    sfda_storage = fields.Char(string="Storage conditions")
    sfda_manufacturer = fields.Text(string="Manufacturers")
    sfda_marketing_company = fields.Char(string="Marketing company")
    sfda_agent = fields.Char(string="Local agent")
    sfda_license_status = fields.Char(string="License status")
    sfda_marketing_status = fields.Char(string="Marketing status")
    sfda_public_price = fields.Float(string="SFDA public price (SAR)")
