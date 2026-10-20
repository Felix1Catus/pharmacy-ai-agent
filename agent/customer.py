# -*- coding: utf-8 -*-
"""Customer recognition + registration flow (fixed steps, no model calls).

On WhatsApp the sender's number (wa_id) is known: the customer is looked up in Odoo Contacts and
greeted by name. Otherwise (test chat without a number, or a customer who wants another
profile) the "existing / new customer" menu item asks for the phone number:

  found      -> show saved name / address / alt phone -> "use these for this order?" (or edit address)
  not found  -> name -> address -> alt phone (optional) -> confirm -> Odoo contact created

The same flow runs automatically when an unidentified customer confirms an order (the quotation
needs a contact for invoicing and delivery), sends a prescription, or asks to track orders; the
interrupted action is then resumed.
"""
from __future__ import annotations

import re

from gateway import guess_city, normalize_phone  # noqa: F401  (guess_city re-exported for tests)
from guards import is_affirmation, normalize

T = {
    "ar": {
        "ask_phone": "📱 اكتب رقم جوالك (مثال: 0501234567)",
        "bad_phone": "الرقم غير صحيح. اكتب رقم جوال مثل 0501234567",
        "intro_checkout": "قبل ما نثبّت طلبك نحتاج بياناتك للفاتورة والتوصيل 🧾",
        "intro_identify": "عشان أكمّل، خلّني أتعرّف عليك أولاً 🙂",
        "found": "أهلاً {name} 👋\nبياناتك المسجّلة:\n📍 العنوان: {address}\n📞 جوال بديل: {alt}\n"
                 "هل نعتمد هذه البيانات لهذا الطلب؟",
        "not_found": "ما لقينا حساباً بهذا الرقم، خلّنا نسجّلك 🙂\n👤 اكتب اسمك الكامل:",
        "bad_name": "اكتب اسمك الكامل (حروف فقط).",
        "ask_address": "📍 اكتب عنوانك للتوصيل (المدينة، الحي، الشارع، رقم المبنى):",
        "bad_address": "العنوان قصير، اكتب المدينة والحي والشارع.",
        "ask_alt": "📞 اكتب رقم جوال بديل، أو اضغط «تخطي»:",
        "confirm_new": "تأكّد من بياناتك:\n👤 {name}\n📱 {phone}\n📍 {address}\n📞 {alt}\nنحفظها؟",
        "saved": "تم تسجيلك بنجاح يا {name} ✅",
        "updated": "تم تحديث عنوانك ✅",
        "welcome": "أهلاً {name} 👋\nكيف أقدر أخدمك اليوم؟ اختر من القائمة:",
        "none": "لا يوجد",
        "yes": "✅ نعم، اعتمدها", "edit": "✏️ تعديل العنوان", "save": "✅ حفظ",
        "redo": "✏️ إعادة الإدخال", "skip": "⏭️ تخطي",
        "order_done": "✅ تم تسجيل طلبك رقم {ref} بإجمالي {total:.2f} ريال.\n{how}\n"
                      "ستراجع الصيدلية الطلب وتؤكّده قريباً، وتقدر تتابعه من «📦 تتبع طلب».",
        "how_delivery": "🚚 التوصيل إلى: {addr}",
        "how_pickup": "🏪 الاستلام من: {branch}",
        "order_failed": "تعذّر تسجيل الطلب: {why}. تقدر تعدّل طلبك أو أحوّلك إلى الصيدلي.",
    },
    "en": {
        "ask_phone": "📱 Type your mobile number (e.g. 0501234567)",
        "bad_phone": "That number isn't valid. Type a mobile number like 0501234567",
        "intro_checkout": "Before we place your order we need your details for the invoice and delivery 🧾",
        "intro_identify": "To continue, let me identify you first 🙂",
        "found": "Welcome back, {name} 👋\nYour saved details:\n📍 Address: {address}\n📞 Alt. phone: {alt}\n"
                 "Use these for this order?",
        "not_found": "We couldn't find an account with this number — let's register you 🙂\n👤 Type your full name:",
        "bad_name": "Please type your full name (letters only).",
        "ask_address": "📍 Type your delivery address (city, district, street, building no.):",
        "bad_address": "That address is too short — include the city, district and street.",
        "ask_alt": "📞 Type an alternative phone number, or tap “Skip”:",
        "confirm_new": "Please check your details:\n👤 {name}\n📱 {phone}\n📍 {address}\n📞 {alt}\nSave them?",
        "saved": "You're registered, {name} ✅",
        "updated": "Your address has been updated ✅",
        "welcome": "Welcome back, {name} 👋\nHow can I help you today? Choose from the menu:",
        "none": "none",
        "yes": "✅ Yes, use them", "edit": "✏️ Edit address", "save": "✅ Save",
        "redo": "✏️ Re-enter", "skip": "⏭️ Skip",
        "order_done": "✅ Your order {ref} has been placed, total {total:.2f} SAR.\n{how}\n"
                      "The pharmacy will review and confirm it shortly — follow it from “📦 Track order”.",
        "how_delivery": "🚚 Delivery to: {addr}",
        "how_pickup": "🏪 Pickup from: {branch}",
        "order_failed": "I couldn't place the order: {why}. You can change it, or I can pass you to a pharmacist.",
    },
}
_EDIT_WORDS = re.compile(r"(?<!\w)(تعديل|عدل|غير|تغيير|لا|edit|change|no|redo|اعاده)(?!\w)")
_SKIP_WORDS = re.compile(r"(?<!\w)(تخطي|تخطى|لا يوجد|ما عندي|skip|none|no)(?!\w)")


def _btn(lang, *keys):
    return [{"id": k, "label": T[lang][k]} for k in keys]


def _pressed(raw: str, lang: str, key: str) -> bool:
    """The customer tapped this button (its label in either language)."""
    n = normalize(raw)
    return any(n == normalize(T[lg][key]) or n == normalize(re.sub(r"^\W+", "", T[lg][key]))
               for lg in T)


class CustomerFlow:
    def __init__(self, agent):
        self.a = agent

    # ---- entry points ------------------------------------------------------
    def start(self, conv, pending=None, intro="intro_identify"):
        """Begin identification; `pending` is resumed afterwards ({"kind": "tool"|"image"|"menu"})."""
        lang = conv.lang or "ar"
        conv.flow = {"step": "ask_phone", "data": {}, "pending": pending}
        head = T[lang][intro] + "\n" if intro else ""
        if conv.wa_phone:                       # WhatsApp: the number is the sender's
            return self._lookup(conv, conv.wa_phone, head)
        return self.a._say(conv, head + T[lang]["ask_phone"], guard="customer")

    def show_profile(self, conv):
        """'Existing / new customer' pressed by an already-identified customer."""
        prof = self.a.gw.get_customer(conv.customer_id)
        conv.flow = {"step": "confirm_profile", "data": dict(prof), "pending": None}
        return self._ask_confirm_profile(conv, prof, "")

    # ---- steps ---------------------------------------------------------------
    def handle(self, conv, raw: str):
        lang, f = conv.lang or "ar", conv.flow
        t, step, d = T[lang], f["step"], f["data"]
        n = normalize(raw)
        if step == "ask_phone":
            if not normalize_phone(raw):
                return self.a._say(conv, t["bad_phone"], guard="customer")
            return self._lookup(conv, raw, "")
        if step == "confirm_profile":
            if _pressed(raw, lang, "edit") or _EDIT_WORDS.search(n):
                f["step"] = "edit_address"
                return self.a._say(conv, t["ask_address"], guard="customer")
            if _pressed(raw, lang, "yes") or is_affirmation(raw):
                return self._finish(conv, d, "")
            return self._ask_confirm_profile(conv, d, "")
        if step == "edit_address":
            if len(raw.strip()) < 8:
                return self.a._say(conv, t["bad_address"], guard="customer")
            prof = self.a.gw.update_customer_address(d["customer_id"], raw)
            return self._finish(conv, prof, t["updated"])
        if step == "ask_name":
            name = re.sub(r"\s+", " ", raw).strip()
            if not (2 <= len(name) <= 60) or re.search(r"[\d@#<>{}\[\]]", name):
                return self.a._say(conv, t["bad_name"], guard="customer")
            d["name"], f["step"] = name, "ask_address"
            return self.a._say(conv, t["ask_address"], guard="customer")
        if step == "ask_address":
            if len(raw.strip()) < 8:
                return self.a._say(conv, t["bad_address"], guard="customer")
            d["address"], f["step"] = raw.strip(), "ask_alt"
            return self.a._say(conv, t["ask_alt"], guard="customer", buttons=_btn(lang, "skip"))
        if step == "ask_alt":
            if _pressed(raw, lang, "skip") or _SKIP_WORDS.search(n):
                d["alt_phone"] = None
            elif normalize_phone(raw):
                d["alt_phone"] = normalize_phone(raw)
            else:
                return self.a._say(conv, t["bad_phone"], guard="customer", buttons=_btn(lang, "skip"))
            f["step"] = "confirm_new"
            return self.a._say(conv, t["confirm_new"].format(
                name=d["name"], phone="+" + d["phone"], address=d["address"],
                alt=("+" + d["alt_phone"]) if d.get("alt_phone") else t["none"]),
                guard="customer", buttons=_btn(lang, "save", "redo"))
        if step == "confirm_new":
            if _pressed(raw, lang, "redo") or _EDIT_WORDS.search(n):
                f["step"] = "ask_name"
                return self.a._say(conv, T[lang]["not_found"].split("\n")[-1], guard="customer")
            if _pressed(raw, lang, "save") or is_affirmation(raw):
                prof = self.a.gw.register_customer(d["name"], d["phone"], d["address"],
                                                   d.get("alt_phone"))
                self.a._audit(conv.conv_id, "customer_registered", {"customer_id": prof["customer_id"]})
                return self._finish(conv, prof, t["saved"].format(name=prof["name"]))
            return self.a._say(conv, t["confirm_new"].format(
                name=d["name"], phone="+" + d["phone"], address=d["address"],
                alt=("+" + d["alt_phone"]) if d.get("alt_phone") else t["none"]),
                guard="customer", buttons=_btn(lang, "save", "redo"))
        conv.flow = None
        return None

    def _lookup(self, conv, phone, head):
        lang, f = conv.lang or "ar", conv.flow
        prof = self.a.gw.find_customer(phone)
        if prof:
            f["data"], f["step"] = dict(prof), "confirm_profile"
            return self._ask_confirm_profile(conv, prof, head)
        f["data"], f["step"] = {"phone": normalize_phone(phone)}, "ask_name"
        return self.a._say(conv, head + T[lang]["not_found"], guard="customer")

    def _ask_confirm_profile(self, conv, prof, head):
        lang = conv.lang or "ar"
        t = T[lang]
        return self.a._say(conv, head + t["found"].format(
            name=prof["name"], address=prof.get("address") or t["none"],
            alt=prof.get("alt_phone") or t["none"]), guard="customer", buttons=_btn(lang, "yes", "edit"))

    # ---- done: identify the conversation and resume what was interrupted ------
    def _finish(self, conv, prof, prefix):
        lang = conv.lang or "ar"
        pending = (conv.flow or {}).get("pending")
        conv.flow = None
        self.a.identify(conv, prof)
        head = (prefix + "\n") if prefix else ""
        if not pending:
            return self.a._say(conv, head + self.a.menu_text_for(conv), guard="menu", menu=True)
        if pending["kind"] == "tool":
            return self.place_order(conv, pending["name"], pending["args"], head)
        if pending["kind"] == "menu":
            return self.a._menu_choice(conv, pending["choice"], prefix=head)
        if pending["kind"] == "image":
            return self.a.respond(conv, pending.get("text") or "", image=pending["image"],
                                  image_name=pending.get("image_name"), resumed=True)
        return self.a._say(conv, head + self.a.menu_text_for(conv), guard="menu", menu=True)

    def place_order(self, conv, name, args, head=""):
        """Run the order the customer already confirmed, now under their own contact."""
        lang = conv.lang or "ar"
        t = T[lang]
        args = self.a._fill_defaults(name, args, conv)
        result = self.a._run_tool(conv, name, args)
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code")
            if code in ("prescription_required", "scheduled_item"):
                return self.a._tool_error_reply(conv, name, code)
            return self.a._say(conv, head + t["order_failed"].format(why=result["error"].get("message", code)),
                               guard="order")
        if name == "hold_item":
            ref, total, how = result.get("ref", ""), 0.0, ""
        else:
            ref, total = result.get("order_ref", result.get("order_id")), result.get("total", 0.0)
            how = (t["how_delivery"].format(addr=args.get("delivery_address", ""))
                   if result.get("fulfillment") == "delivery"
                   else t["how_pickup"].format(branch=result.get("branch_name", "")))
        return self.a._say(conv, head + t["order_done"].format(ref=ref, total=total or 0.0, how=how),
                           guard="order")
