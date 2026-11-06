from odoo import api, fields, models


class PharmacyStockAlert(models.Model):
    """"Tell me when it's back": the assistant records the request; when the product is in stock
    again (at the branch, or anywhere) the WhatsApp notifier messages the customer once."""
    _name = "pharmacy.stock.alert"
    _description = "Back-in-stock request from a customer"
    _order = "create_date desc"

    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True)
    product_id = fields.Many2one("product.product", string="Product", required=True, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch",
                                   help="Empty = any branch.")
    state = fields.Selection([("waiting", "Waiting"), ("notified", "Customer notified"),
                              ("cancelled", "Cancelled")], default="waiting", required=True, index=True)
    qty_now = fields.Float(string="Available now", compute="_compute_qty_now")

    @api.depends("product_id", "warehouse_id")
    def _compute_qty_now(self):
        for a in self:
            p = a.product_id
            if a.warehouse_id:
                p = p.with_context(warehouse_id=a.warehouse_id.id)
            a.qty_now = p.free_qty if p else 0.0
