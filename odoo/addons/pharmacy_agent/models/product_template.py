from odoo import api, fields, models

from . import storage_parse


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

    # ---- what the assistant reads: one dispensing class + structured storage -------------
    rx_required_manual = fields.Boolean(
        string="Prescription required (pharmacy)",
        help="The pharmacy's own flag, for products the SFDA import does not cover or marks "
             "differently. Prescription-only if this OR the SFDA flag is set.")
    dispensing_class = fields.Selection(
        [("otc", "Over the counter"), ("prescription", "Prescription only"),
         ("scheduled", "Scheduled / controlled")],
        string="Dispensing", compute="_compute_dispensing_class", store=True, index=True,
        help="What the assistant may do: OTC is ordered freely; prescription-only is ordered and "
             "a pharmacist reviews the prescription; scheduled is never ordered, priced or "
             "discussed - the customer is handed to a pharmacist.")

    storage_note = fields.Char(
        string="Storage (pharmacy note)",
        help="Overrides the SFDA storage text, e.g. for products without SFDA data.")
    storage_class = fields.Selection(
        storage_parse.STORAGE_CLASSES, string="Storage", compute="_compute_storage", store=True,
        index=True)
    storage_min_c = fields.Float(string="Min °C", compute="_compute_storage", store=True)
    storage_max_c = fields.Float(string="Max °C", compute="_compute_storage", store=True)
    storage_has_min = fields.Boolean(compute="_compute_storage", store=True)
    storage_has_max = fields.Boolean(compute="_compute_storage", store=True)
    storage_protect_light = fields.Boolean(string="Keep away from light", compute="_compute_storage",
                                           store=True)
    storage_protect_moisture = fields.Boolean(string="Keep dry", compute="_compute_storage", store=True)
    storage_no_freeze = fields.Boolean(string="Do not freeze", compute="_compute_storage", store=True)
    cold_chain = fields.Boolean(
        string="Cold chain", compute="_compute_storage", store=True, index=True,
        help="Fridge / freezer / cool item: deliveries need a cool box.")
    shelf_life_months = fields.Integer(string="Shelf life (months, parsed)", compute="_compute_label",
                                       store=True)
    manufacturer_country = fields.Char(string="Country of manufacture", compute="_compute_label",
                                       store=True)

    @api.depends("is_scheduled", "sfda_requires_prescription", "rx_required_manual")
    def _compute_dispensing_class(self):
        for t in self:
            # x_requires_prescription / x_is_restricted are the demo catalogue's custom fields
            legacy = bool(t._fields.get("x_requires_prescription") and t["x_requires_prescription"]) or \
                bool(t._fields.get("x_is_restricted") and t["x_is_restricted"])
            if t.is_scheduled:
                t.dispensing_class = "scheduled"
            elif t.sfda_requires_prescription or t.rx_required_manual or legacy:
                t.dispensing_class = "prescription"
            else:
                t.dispensing_class = "otc"

    @api.depends("sfda_storage", "storage_note")
    def _compute_storage(self):
        for t in self:
            p = storage_parse.parse_storage(t.storage_note or t.sfda_storage)
            t.storage_class = p["storage_class"]
            t.storage_has_min, t.storage_has_max = p["min_c"] is not None, p["max_c"] is not None
            t.storage_min_c = p["min_c"] or 0.0
            t.storage_max_c = p["max_c"] or 0.0
            t.storage_protect_light = p["protect_light"]
            t.storage_protect_moisture = p["protect_moisture"]
            t.storage_no_freeze = p["no_freeze"]
            t.cold_chain = p["cold_chain"]

    @api.depends("sfda_shelf_life", "sfda_manufacturer")
    def _compute_label(self):
        for t in self:
            t.shelf_life_months = storage_parse.shelf_life_months(t.sfda_shelf_life)
            t.manufacturer_country = storage_parse.manufacturer_country(t.sfda_manufacturer)

    def write(self, vals):
        # the demo catalogue's custom prescription flags feed the module's own field, so the
        # stored dispensing class follows them (x_ fields cannot be listed in @api.depends)
        legacy = {k: vals[k] for k in ("x_requires_prescription", "x_is_restricted") if k in vals}
        res = super().write(vals)
        if legacy:
            for t in self:
                flag = bool(t._fields.get("x_requires_prescription") and t["x_requires_prescription"]) or \
                    bool(t._fields.get("x_is_restricted") and t["x_is_restricted"])
                if t.rx_required_manual != flag:
                    super(ProductTemplate, t).write({"rx_required_manual": flag})
        return res
