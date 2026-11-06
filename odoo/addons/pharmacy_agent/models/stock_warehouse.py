from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    # A branch as the customer sees it: the assistant lists these and filters by city.
    pharmacy_city = fields.Char(string="City")
    pharmacy_district = fields.Char(string="District / area")
    pharmacy_address = fields.Char(string="Street address")
    pharmacy_phone = fields.Char(string="Branch phone")
    pharmacy_hours = fields.Char(string="Opening hours", default="24/7")
    pharmacy_maps_url = fields.Char(string="Map link")
    pharmacy_delivery = fields.Boolean(string="Delivers from this branch", default=True)
