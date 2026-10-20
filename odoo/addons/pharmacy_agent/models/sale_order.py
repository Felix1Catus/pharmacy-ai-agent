from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # Orders placed by the WhatsApp assistant arrive as QUOTATIONS: a human confirms and invoices
    # them at the dispensary (pickup) or approves them for delivery.
    wa_order = fields.Boolean(string="WhatsApp order", copy=False, index=True)
    fulfillment_type = fields.Selection(
        [("pickup", "Pickup at branch"), ("delivery", "Delivery")], string="Fulfilment", copy=False)
    delivery_address_text = fields.Char(string="Delivery address (as given)", copy=False)
    # Hand-off to the future "Delivery Dispatcher" module (drivers accept the job, see pickup and
    # drop-off, are tracked live, mark it delivered). Only the first states are used today.
    dispatch_status = fields.Selection([
        ("none", "Not a delivery"),
        ("awaiting_approval", "Awaiting pharmacy approval"),
        ("ready", "Ready for dispatch"),
        ("assigned", "Driver assigned"),
        ("picked_up", "Picked up"),
        ("delivered", "Delivered"),
    ], string="Dispatch status", default="none", copy=False, index=True)
