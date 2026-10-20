from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # The customer's WhatsApp number, normalised to international digits ("9665XXXXXXXX"), which
    # is how the WhatsApp Cloud API identifies the sender (wa_id). The assistant looks customers
    # up by it (and by phone/mobile) to greet them by name and reuse their saved address.
    whatsapp_number = fields.Char(string="WhatsApp number", index=True, copy=False)
    registered_via = fields.Selection(
        [("whatsapp", "WhatsApp assistant"), ("staff", "Staff")],
        string="Registered via", copy=False)
