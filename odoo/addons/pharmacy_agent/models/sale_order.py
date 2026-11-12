from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


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

    # ---- prescription review (prescription-only medicines) ---------------------------
    rx_review_state = fields.Selection([
        ("none", "No prescription needed"),
        ("awaiting_photo", "Awaiting prescription photo"),
        ("pending", "Pending approval"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ], string="Prescription review", default="none", copy=False, index=True, tracking=True)
    rx_review_note = fields.Text(string="Review note / rejection reason", copy=False)
    prescription_ids = fields.One2many("pharmacy.prescription", "sale_order_id", string="Prescriptions")
    rx_reviewed_by = fields.Many2one("res.users", string="Reviewed by", copy=False, readonly=True)

    # ---- customer messaging + payment (the assistant reads these) -------------------
    # notify_state=pending: the assistant sends the review result to the customer, then "sent"
    notify_state = fields.Selection(
        [("none", "Nothing to send"), ("pending", "Message pending"), ("sent", "Message sent")],
        string="Customer message", default="none", copy=False, index=True)
    customer_confirmed = fields.Boolean(string="Customer confirmed", copy=False)
    payment_state_wa = fields.Selection(
        [("none", "Not requested"), ("link_sent", "Payment link sent"), ("paid", "Paid")],
        string="Payment (WhatsApp)", default="none", copy=False)
    payment_link = fields.Char(string="Payment link", copy=False)

    def _check_pharmacist(self):
        """Only a pharmacist decides on a prescription - never the assistant's service account
        or another internal user calling the method directly."""
        if not (self.env.user.has_group("pharmacy_agent.group_pharmacy_pharmacist")
                or self.env.is_superuser()):
            raise AccessError(_("Only a pharmacist can approve or reject a prescription."))

    def action_rx_approve(self):
        self._check_pharmacist()
        for order in self:
            if order.rx_review_state != "pending":
                raise UserError(_("Only quotations pending prescription approval can be approved."))
            order.prescription_ids.write({"state": "verified", "verified_by": self.env.user.id})
            order.write({"rx_review_state": "approved", "rx_reviewed_by": self.env.user.id,
                         "notify_state": "pending"})
        return True

    def action_rx_reject(self):
        self._check_pharmacist()
        for order in self:
            if order.rx_review_state not in ("pending", "awaiting_photo"):
                raise UserError(_("Only quotations waiting for prescription review can be rejected."))
            if not (order.rx_review_note or "").strip():
                raise UserError(_("Write the rejection reason in 'Review note / rejection reason' "
                                  "first - it is sent to the customer."))
            order.prescription_ids.write({"state": "rejected"})
            order.write({"rx_review_state": "rejected", "rx_reviewed_by": self.env.user.id,
                         "notify_state": "pending"})
        return True

    # ---- cold chain + handovers (read by the assistant and the dispatcher) -----------
    needs_cold_chain = fields.Boolean(
        string="Needs cold chain", compute="_compute_needs_cold_chain", store=True,
        help="A fridge / freezer item is on the order: deliver in a cool box.")
    handover_ids = fields.One2many("pharmacy.handover", "sale_order_id", string="Handovers")
    cancel_reason_wa = fields.Char(string="Cancellation reason (customer)", copy=False)

    @api.depends("order_line.product_id.cold_chain")
    def _compute_needs_cold_chain(self):
        for o in self:
            o.needs_cold_chain = any(l.product_id.cold_chain for l in o.order_line)

    # ---- the chat cart: a quotation the customer is still filling in the chat -----------
    wa_cart = fields.Boolean(
        string="Chat cart (not checked out)", copy=False, index=True,
        help="Items the customer added in the chat but has not checked out yet. A pharmacist "
             "answering a question about them sees the items here.")

    # ---- dispensing rules enforced in Odoo itself, whoever (or whatever) confirms ----------
    def action_confirm(self):
        """A quotation whose prescription has not been approved is never confirmed, and a
        chat order never contains a scheduled medicine - also when a colleague presses Confirm
        in Odoo or another integration calls the method."""
        waiting = {"awaiting_photo": _("it is waiting for the customer's prescription"),
                   "pending": _("a pharmacist has not reviewed its prescription yet"),
                   "rejected": _("its prescription was rejected")}
        for order in self:
            if order.rx_review_state in waiting:
                raise UserError(_("%(order)s cannot be confirmed: %(why)s.",
                                  order=order.name, why=waiting[order.rx_review_state]))
            if order.wa_order or order.wa_cart:
                scheduled = order.order_line.filtered(lambda l: l.product_id.is_scheduled)
                if scheduled:
                    raise UserError(_(
                        "%(order)s is a chat order with a scheduled medicine (%(names)s): "
                        "scheduled medicines are dispensed in person only.",
                        order=order.name, names=", ".join(scheduled.mapped("product_id.display_name"))))
        return super().action_confirm()

    # ---- holds: a draft quotation kept aside until hold_expires_at -------------------------
    hold_expires_at = fields.Datetime(
        string="Held until", copy=False, index=True,
        help="Set on a hold placed through the assistant. Until then the assistant counts the "
             "quantity as unavailable for other customers; afterwards the hold is cancelled.")

    @api.model
    def _cron_expire_holds(self):
        expired = self.search([("hold_expires_at", "!=", False),
                               ("hold_expires_at", "<", fields.Datetime.now()),
                               ("state", "in", ("draft", "sent"))])
        for order in expired:
            order.message_post(body=_("Hold expired: released automatically."))
        if expired:
            expired._action_cancel()
        return True
