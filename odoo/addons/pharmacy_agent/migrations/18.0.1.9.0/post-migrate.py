"""1.9.0: the demo catalogue's custom prescription flags become the module's own field, and
branches get their city / district from the "City - District" warehouse names."""


def migrate(cr, version):
    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name='product_template' "
               "AND column_name IN ('x_requires_prescription', 'x_is_restricted')")
    cols = [r[0] for r in cr.fetchall()]
    if cols:
        cr.execute("UPDATE product_template SET rx_required_manual = TRUE WHERE "
                   + " OR ".join(f"{c} IS TRUE" for c in cols))
    cr.execute("""UPDATE stock_warehouse SET
                    pharmacy_city = COALESCE(pharmacy_city, trim(split_part(name, '-', 1))),
                    pharmacy_district = COALESCE(pharmacy_district, NULLIF(trim(split_part(name, '-', 2)), '')),
                    pharmacy_hours = COALESCE(pharmacy_hours, '24/7'),
                    pharmacy_delivery = COALESCE(pharmacy_delivery, TRUE)
                  WHERE code != 'WH' AND name LIKE '%-%'""")
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    t = env["product.template"].with_context(active_test=False).search([])
    env.add_to_compute(t._fields["dispensing_class"], t)
    t.flush_model()
