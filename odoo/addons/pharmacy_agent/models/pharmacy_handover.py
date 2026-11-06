from odoo import api, fields, models

REASONS = [
    ("medical_advice", "Medical question"),
    ("restricted_item", "Scheduled / restricted medicine"),
    ("complaint", "Complaint"),
    ("abusive", "Abusive messages"),
    ("low_confidence", "Assistant unsure"),
    ("unclear", "Unclear request"),
    ("out_of_scope", "Out of scope"),
    ("prescription", "Prescription question"),
    ("privacy_request", "Privacy request (PDPL: access / deletion / consent withdrawn)"),
]


class PharmacyHandover(models.Model):
    """A conversation the assistant handed to a human (escalate_to_human). Staff work the queue;
    the assistant reads the state back when the customer asks about their request."""
    _name = "pharmacy.handover"
    _description = "Assistant handover to a pharmacist / staff member"
    _order = "create_date desc"

    name = fields.Char(default="New", required=True, copy=False, readonly=True)
    partner_id = fields.Many2one("res.partner", string="Customer", index=True)
    sale_order_id = fields.Many2one("sale.order", string="Related order")
    reason = fields.Selection(REASONS, required=True, index=True)
    summary = fields.Text(required=True)
    channel = fields.Selection([("whatsapp", "WhatsApp"), ("webchat", "Web chat"),
                                ("other", "Other")], default="other")
    conv_id = fields.Char(string="Conversation id", index=True)
    state = fields.Selection([("new", "New"), ("in_progress", "In progress"), ("done", "Resolved"),
                              ("cancelled", "Cancelled")], default="new", required=True, index=True)
    user_id = fields.Many2one("res.users", string="Handled by")
    resolution = fields.Text(help="What was done - the assistant may tell the customer.")

    @api.model_create_multi
    def create(self, vals_list):
        for v in vals_list:
            if v.get("name", "New") == "New":
                v["name"] = self.env["ir.sequence"].next_by_code("pharmacy.handover") or "New"
        return super().create(vals_list)

    def action_start(self):
        self.write({"state": "in_progress", "user_id": self.env.user.id})

    def action_done(self):
        self.write({"state": "done", "user_id": self.env.user.id})
