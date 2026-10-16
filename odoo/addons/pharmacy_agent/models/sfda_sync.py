# -*- coding: utf-8 -*-
"""SFDA registry "Update List": keep products in sync with the SFDA registered-drugs list.

Inventory > Products > Update List. A run scrapes the configured SFDA list pages and each drug's
details page (same parsing + field mapping as data_sources/sfda/build_odoo_import.py), then:
  - creates drugs not yet in Odoo (optional),
  - updates sfda_* fields that changed,
  - ticks Scheduled when SFDA marks a drug controlled — but NEVER unticks it: if SFDA relaxes a
    drug's control status the run only reports it for human review,
  - leaves the pharmacy's own sale price alone unless "Update sale price" is enabled.
Products are matched on the SFDA registration number; new products get the same external ID as
the import file (sfda.drug_<reg no>), so file imports and automatic updates share records.

Runs execute in the background through ir.cron (manual button -> on-demand cron trigger;
automatic -> periodic cron, off by default). Network work is threaded; all ORM work stays in the
cron's own thread/cursor.
"""
import html
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

BASE = "https://www.sfda.gov.sa"
UA = "Mozilla/5.0 (X11; Linux x86_64) pharmacy-catalogue-sync (polite)"
# SFDA "المراقبة" values that tick the Scheduled box (owner decision 2026-10-16)
SCHEDULED_VALUES = {"مقيد", "خاضعة للرقابة وفق أحكام وشروط خاصة"}
LABELS = {
    "رقم التسجيل": "reg_no", "الاسم التجاري": "trade_name", "الاسم العلمي": "scientific_name",
    "التركيز": "strength", "وحدة التركيز": "strength_unit", "الشكل الصيدلاني": "dosage_form",
    "طريقة الإستعمال": "route", "الحجم": "size", "وحدة الحجم": "size_unit",
    "نوع العبوة": "pack_type", "حجم العبوة": "pack_size", "طريقة الصرف": "dispensing",
    "المراقبة": "control", "مكان التوزيع": "distribution", "مدة الصلاحية": "shelf_life_months",
    "ظروف التخزين": "storage", "الشركات المصنعة": "manufacturers",
    "الشركة المسوقة": "marketing_company", "الوكلاء": "agents",
    "رمز التصنيف الكيميائي العلاجي التشريحي 1": "atc_code_1", "حالة الترخيص": "license_status",
    "حالة التسويق": "marketing_status", "السعر": "price",
}
SFDA_FIELDS = ["sfda_reg_no", "sfda_scientific_name", "sfda_strength", "sfda_dosage_form",
               "sfda_route", "sfda_pack", "sfda_dispensing", "sfda_requires_prescription",
               "sfda_control", "sfda_distribution", "sfda_atc_code", "sfda_shelf_life",
               "sfda_storage", "sfda_manufacturer", "sfda_marketing_company", "sfda_agent",
               "sfda_license_status", "sfda_marketing_status", "sfda_public_price"]

_local = threading.local()


# ---------------------------------------------------------------- scraping (no ORM here)
def _session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers.update({"User-Agent": UA, "Accept-Language": "ar"})
    return _local.s


def _fetch(url, delay):
    for attempt in range(5):
        time.sleep(delay + random.uniform(0, 0.5))
        try:
            r = _session().get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            wait = 30 * (attempt + 1) if r.status_code in (429, 503) else 5 * (attempt + 1)
        except requests.RequestException:
            wait = 5 * (attempt + 1)
        time.sleep(wait)
    raise RuntimeError("giving up on %s" % url)


def _clean(s):
    s = re.sub(r"<br\s*/?>", " ; ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _parse_list(page_html):
    rows = []
    for tr in re.findall(r'<tr class="(?:odd|even)">(.*?)</tr>', page_html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        m = re.search(r'href="(/ar/details_data\?[^"]+)"', tr)
        if len(tds) < 5 or not m:
            continue
        href = html.unescape(m.group(1))
        rows.append({"reg_no": re.search(r"[?&]id=([^&]+)", href).group(1),
                     "price": _clean(tds[4]), "details_url": BASE + href,
                     "key": href.split("details_data?")[1].split("&page=")[0]})
    return rows


def _parse_details(page_html):
    m = re.search(r'<tbody class="attachmentstr">(.*?)</tbody>', page_html, re.S)
    if not m:
        return {}
    raw = {_clean(k): _clean(v) for k, v in
           re.findall(r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", m.group(1), re.S)}
    return {key: raw.get(label, "") for label, key in LABELS.items()}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return 0.0


def _sfda_vals(r):
    """SFDA record -> sfda_* field values (identical to build_odoo_import.py)."""
    size = " ".join(x for x in [r["size"], r["size_unit"]] if x)
    return {
        "sfda_reg_no": r["reg_no"], "sfda_scientific_name": r["scientific_name"],
        "sfda_strength": " ".join(x for x in [r["strength"], r["strength_unit"]] if x),
        "sfda_dosage_form": r["dosage_form"], "sfda_route": r["route"],
        "sfda_pack": " ".join(x for x in [r["pack_size"], r["pack_type"],
                                          f"({size})" if size else ""] if x),
        "sfda_dispensing": r["dispensing"],
        "sfda_requires_prescription": r["dispensing"].startswith("يحتاج"),
        "sfda_control": r["control"], "sfda_distribution": r["distribution"],
        "sfda_atc_code": r["atc_code_1"], "sfda_shelf_life": r["shelf_life_months"],
        "sfda_storage": r["storage"], "sfda_manufacturer": r["manufacturers"],
        "sfda_marketing_company": r["marketing_company"], "sfda_agent": r["agents"],
        "sfda_license_status": r["license_status"], "sfda_marketing_status": r["marketing_status"],
        "sfda_public_price": _num(r["price"]),
    }


def _display_name(r):
    return r["trade_name"] or " ".join(x for x in [
        r["scientific_name"], r["strength"], r["strength_unit"], r["dosage_form"]] if x) \
        or "SFDA %s" % r["reg_no"]


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return abs((a or 0.0) - (b or 0.0)) < 0.005
    return (a or "") == (b or "") if not isinstance(a, bool) else bool(a) == bool(b)


# ---------------------------------------------------------------- models
class PharmacySfdaUpdater(models.Model):
    _name = "pharmacy.sfda.updater"
    _description = "SFDA registry updater (Update List)"

    name = fields.Char(default="SFDA registry", required=True)
    auto_update = fields.Boolean(string="Automatic updates", default=False,
                                 help="Check SFDA for new/changed drugs every N days.")
    interval_days = fields.Integer(string="Every (days)", default=3)
    next_auto_run = fields.Datetime(compute="_compute_next_auto_run", string="Next automatic run")
    first_page = fields.Integer(default=0, help="SFDA list pages are 0-based (page 0 = first).")
    last_page = fields.Integer(default=100)
    create_new = fields.Boolean(string="Create new drugs", default=True,
                                help="Create products for SFDA drugs not yet in Odoo.")
    update_sale_price = fields.Boolean(
        string="Update sale price", default=False,
        help="Also overwrite the product's sale price with the SFDA price. Off: only the "
             "'SFDA public price' field is updated; your own prices stay.")
    workers = fields.Integer(string="Parallel requests", default=4)
    delay = fields.Float(string="Pause per request (s)", default=0.5)
    run_ids = fields.One2many("pharmacy.sfda.run", "updater_id", string="Runs")
    last_run_id = fields.Many2one("pharmacy.sfda.run", compute="_compute_last_run")
    last_run_state = fields.Selection(related="last_run_id.state", string="Last run status")
    last_run_summary = fields.Char(related="last_run_id.summary", string="Last run")

    def _compute_next_auto_run(self):
        cron = self.env.ref("pharmacy_agent.ir_cron_sfda_auto", raise_if_not_found=False)
        for rec in self:
            rec.next_auto_run = cron.nextcall if cron and cron.active else False

    def _compute_last_run(self):
        for rec in self:
            rec.last_run_id = rec.run_ids[:1]

    def write(self, vals):
        res = super().write(vals)
        if {"auto_update", "interval_days"} & set(vals):
            cron = self.env.ref("pharmacy_agent.ir_cron_sfda_auto").sudo()
            rec = self[:1]
            days = max(1, rec.interval_days or 3)
            cron_vals = {"active": rec.auto_update, "interval_number": days,
                         "interval_type": "days"}
            if rec.auto_update and "auto_update" in vals:
                cron_vals["nextcall"] = fields.Datetime.now() + timedelta(days=days)
            cron.write(cron_vals)
        return res

    @api.model
    def _get_singleton(self):
        return self.env.ref("pharmacy_agent.sfda_updater_singleton")

    def _check_not_running(self):
        stale = fields.Datetime.now() - timedelta(hours=3)
        busy = self.env["pharmacy.sfda.run"].search(
            [("state", "in", ("queued", "running")), ("create_date", ">", stale)], limit=1)
        if busy:
            raise UserError(_("An update is already %s (started %s). Wait for it to finish.")
                            % (busy.state, busy.create_date))

    def action_run_now(self):
        self.ensure_one()
        self._check_not_running()
        self.env["pharmacy.sfda.run"].create({"updater_id": self.id, "trigger": "manual"})
        self.env.ref("pharmacy_agent.ir_cron_sfda_manual").sudo()._trigger()
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("SFDA update started"),
            "message": _("Running in the background (about 15-20 minutes for 100 pages). "
                         "Reload this screen to follow progress."),
            "type": "info", "sticky": False,
            "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    def action_open_runs(self):
        return {"type": "ir.actions.act_window", "name": _("SFDA update runs"),
                "res_model": "pharmacy.sfda.run", "view_mode": "list,form",
                "domain": [("updater_id", "=", self.id)]}

    # ---------------------------------------------------------------- cron entry points
    @api.model
    def _cron_auto(self):
        upd = self._get_singleton()
        if not upd.auto_update:
            return
        try:
            upd._check_not_running()
        except UserError:
            return
        run = self.env["pharmacy.sfda.run"].create({"updater_id": upd.id, "trigger": "auto"})
        self.env.cr.commit()
        upd._process(run)

    @api.model
    def _cron_manual(self):
        for run in self.env["pharmacy.sfda.run"].search([("state", "=", "queued")], order="id"):
            run.updater_id._process(run)

    # ---------------------------------------------------------------- the sync itself
    def _process(self, run):
        self.ensure_one()
        cr = self.env.cr
        run.write({"state": "running", "started": fields.Datetime.now()})
        cr.commit()
        try:
            counts = self._do_sync(run)
            run.write(dict(counts, state="done", finished=fields.Datetime.now()))
        except Exception as e:  # noqa: BLE001
            cr.rollback()
            _logger.exception("SFDA update failed")
            run.write({"state": "failed", "finished": fields.Datetime.now(),
                       "notes": (run.notes or "") + "\nFAILED: %s" % e})
        cr.commit()

    def _do_sync(self, run):
        cr = self.env.cr
        delay, workers = max(0.1, self.delay), max(1, min(self.workers, 8))
        pages = list(range(self.first_page, self.last_page + 1))

        # 1) list pages (threaded HTTP)
        def get_list(p):
            try:
                return _parse_list(_fetch("%s/ar/drugs-list?page=%d" % (BASE, p), delay))
            except Exception:  # noqa: BLE001
                return None
        with ThreadPoolExecutor(max_workers=workers) as ex:
            lists = list(ex.map(get_list, pages))
        errors = sum(1 for x in lists if x is None)
        rows, seen = [], set()
        for page_rows in lists:
            for r in page_rows or []:
                if r["key"] not in seen:
                    seen.add(r["key"]); rows.append(r)
        run.write({"pages": len(pages), "scanned": len(rows), "errors": errors,
                   "summary": _("fetching %d drug pages...") % len(rows)})
        cr.commit()

        # 2) details pages (threaded HTTP)
        def get_details(r):
            try:
                d = _parse_details(_fetch(r["details_url"], delay))
                d["reg_no"] = d.get("reg_no") or r["reg_no"]
                d["price"] = d.get("price") or r["price"]
                return d if d.get("reg_no") else None
            except Exception:  # noqa: BLE001
                return None
        with ThreadPoolExecutor(max_workers=workers) as ex:
            recs = list(ex.map(get_details, rows))
        errors += sum(1 for x in recs if x is None)
        recs = [r for r in recs if r]
        if rows and errors > 0.2 * (len(rows) + len(pages)):
            raise RuntimeError("too many fetch errors (%d) - SFDA site unreachable?" % errors)

        # 3) upsert (ORM, this thread only)
        Product = self.env["product.template"].with_context(active_test=False,
                                                           tracking_disable=True)
        IMD = self.env["ir.model.data"].sudo()
        by_reg = {}
        for r in recs:
            by_reg.setdefault(r["reg_no"], r)          # SFDA lists a few drugs twice
        existing = {p.sfda_reg_no: p for p in Product.search([("sfda_reg_no", "in", list(by_reg))])}
        c = dict(new=0, changed=0, unchanged=0, newly_scheduled=0, relaxed=0)
        notes = []
        for i, (reg, r) in enumerate(by_reg.items(), 1):
            vals = _sfda_vals(r)
            controlled = r["control"] in SCHEDULED_VALUES
            prod = existing.get(reg)
            if not prod:
                if not self.create_new:
                    continue
                prod = Product.create(dict(
                    vals, name=_display_name(r), default_code=reg, type="consu",
                    is_storable=True, sale_ok=True, list_price=vals["sfda_public_price"],
                    is_scheduled=controlled, schedule_class=r["control"]))
                xmlid = "drug_" + re.sub(r"[^A-Za-z0-9_]", "_", reg)
                if not IMD.search_count([("module", "=", "sfda"), ("name", "=", xmlid)]):
                    IMD.create({"module": "sfda", "name": xmlid, "model": "product.template",
                                "res_id": prod.id, "noupdate": True})
                c["new"] += 1
                notes.append("NEW %s %s%s" % (reg, prod.name, " [Scheduled]" if controlled else ""))
            else:
                diff = {f: v for f, v in vals.items() if not _same(prod[f], v)}
                if controlled and not prod.is_scheduled:
                    diff.update(is_scheduled=True, schedule_class=r["control"])
                    c["newly_scheduled"] += 1
                    notes.append("SCHEDULED %s %s (SFDA: %s)" % (reg, prod.name, r["control"]))
                elif not controlled and prod.is_scheduled and prod.sfda_control in SCHEDULED_VALUES:
                    c["relaxed"] += 1    # never auto-untick: a human reviews
                    notes.append("REVIEW %s %s: SFDA control now '%s' (still Scheduled in Odoo)"
                                 % (reg, prod.name, r["control"] or "-"))
                if self.update_sale_price and not _same(prod.list_price, vals["sfda_public_price"]):
                    diff["list_price"] = vals["sfda_public_price"]
                if diff:
                    changes = ", ".join("%s: %s -> %s" % (f, prod[f], v) for f, v in diff.items()
                                        if f not in ("schedule_class",))
                    prod.write(diff)
                    c["changed"] += 1
                    notes.append("CHANGED %s %s: %s" % (reg, prod.name, changes[:400]))
                else:
                    c["unchanged"] += 1
            if i % 200 == 0:
                run.write(dict(c, summary=_("processed %d/%d") % (i, len(by_reg))))
                cr.commit()
        c["errors"] = errors
        c["notes"] = "\n".join(notes[:2000]) or _("No new drugs and no changes.")
        c["summary"] = _("%(new)d new, %(changed)d changed, %(unchanged)d unchanged, "
                         "%(sched)d newly Scheduled, %(rev)d to review, %(err)d errors") % {
            "new": c["new"], "changed": c["changed"], "unchanged": c["unchanged"],
            "sched": c["newly_scheduled"], "rev": c["relaxed"], "err": errors}
        return c


class PharmacySfdaRun(models.Model):
    _name = "pharmacy.sfda.run"
    _description = "SFDA registry update run"
    _order = "id desc"

    updater_id = fields.Many2one("pharmacy.sfda.updater", required=True, ondelete="cascade")
    trigger = fields.Selection([("manual", "Manual"), ("auto", "Automatic")], default="manual")
    state = fields.Selection([("queued", "Queued"), ("running", "Running"), ("done", "Done"),
                              ("failed", "Failed")], default="queued", index=True)
    started = fields.Datetime()
    finished = fields.Datetime()
    pages = fields.Integer()
    scanned = fields.Integer(string="Drugs scanned")
    new = fields.Integer(string="New")
    changed = fields.Integer(string="Changed")
    unchanged = fields.Integer(string="Unchanged")
    newly_scheduled = fields.Integer(string="Newly Scheduled")
    relaxed = fields.Integer(string="To review",
                             help="SFDA relaxed the control status of a Scheduled product. "
                                  "Odoo keeps it Scheduled until a person unticks it.")
    errors = fields.Integer()
    summary = fields.Char()
    notes = fields.Text(string="Details")
