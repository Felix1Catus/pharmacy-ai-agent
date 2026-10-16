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
