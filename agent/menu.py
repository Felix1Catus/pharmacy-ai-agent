# -*- coding: utf-8 -*-
"""Conversation opening menu + fixed texts per language (WhatsApp-style).

Every conversation starts with a greeting and five menu items:
  1. new order   2. track order   3. questions / inquiries   4. existing / new customer
  5. switch language (AR <-> EN)
The language choice ("ar" / "en") decides the language of every fixed reply and is passed to
the model. Menu choices are handled here deterministically (no model call), so they are instant.
On WhatsApp the items map to an interactive list message; the test chat shows them as buttons.
"""
from __future__ import annotations

import re

from guards import normalize

MENU_IDS = ["new_order", "track_order", "questions", "customer", "switch_lang"]

TEXTS = {
    "ar": {
        "greeting": "أهلاً وسهلاً بك في الصيدلية 👋\nكيف أقدر أخدمك اليوم؟ اختر من القائمة:",
        "labels": {"new_order": "🛒 طلب جديد", "track_order": "📦 تتبع طلب",
                   "questions": "❓ أسئلة واستفسارات", "customer": "👤 عميل حالي / جديد",
                   "switch_lang": "🌐 English"},
        "new_order": "تمام! اكتب اسم الدواء أو المنتج اللي تبغاه 🛒\n"
                     "وإذا كان يحتاج وصفة طبية تقدر ترسل صورتها أو ملف PDF من زر المرفقات 📎",
        "questions": "تفضّل، اكتب سؤالك عن منتجاتنا أو أسعارها أو توفّرها أو فروعنا 🙂",
        "track_none": "ما لقيت طلبات على حسابك حالياً. تقدر تبدأ طلب جديد من القائمة.",
        "track_head": "طلباتك الأخيرة:",
        "track_tail": "إذا تبغى تفاصيل طلب معيّن اكتب رقمه.",
        "off_topic": "عذراً، أنا مساعد الصيدلية وأقدر أساعدك فقط في منتجاتنا وأسعارها وتوفّرها، "
                     "والطلبات وتتبّعها. اختر من القائمة:",
        "injection": "عذراً، ما أقدر أنفّذ هذا الطلب. أنا مساعد الصيدلية فقط وأساعدك في المنتجات "
                     "والطلبات وتتبّعها. اختر من القائمة:",
        "menu_again": "اختر من القائمة:",
        "states": {"draft": "بانتظار تأكيد الصيدلية", "sent": "بانتظار تأكيد الصيدلية", "sale": "مؤكد",
                   "done": "مكتمل", "cancel": "ملغي"},
        "delivery": {"pending": "قيد التجهيز", "started": "خرج للتوصيل", "partial": "تم تسليم جزء",
                     "full": "تم التسليم", "none": "—"},
        "currency": "ريال",
    },
    "en": {
        "greeting": "Welcome to the pharmacy 👋\nHow can I help you today? Choose from the menu:",
        "labels": {"new_order": "🛒 New order", "track_order": "📦 Track order",
                   "questions": "❓ Questions / inquiries", "customer": "👤 Existing / new customer",
                   "switch_lang": "🌐 العربية"},
        "new_order": "Great! Type the name of the medicine or product you need 🛒\n"
                     "If it needs a prescription, you can send a photo or PDF of it with the 📎 button.",
        "questions": "Go ahead — ask about our products, prices, availability or branches 🙂",
        "track_none": "I couldn't find any orders on your account. You can start a new order from the menu.",
        "track_head": "Your recent orders:",
        "track_tail": "For details of a specific order, type its number.",
        "off_topic": "Sorry, I'm the pharmacy assistant — I can only help with our products, prices, "
                     "availability, and placing or tracking orders. Choose from the menu:",
        "injection": "Sorry, I can't do that. I'm only the pharmacy assistant and can help with "
                     "products, orders and order tracking. Choose from the menu:",
        "menu_again": "Choose from the menu:",
        "states": {"draft": "awaiting pharmacy confirmation", "sent": "awaiting pharmacy confirmation",
                   "sale": "confirmed", "done": "completed", "cancel": "cancelled"},
        "delivery": {"pending": "being prepared", "started": "out for delivery",
                     "partial": "partly delivered", "full": "delivered", "none": "—"},
        "currency": "SAR",
    },
}

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
# keywords per menu item (normalised); only used for SHORT messages so that
# "ابغى اطلب بنادول" goes to the agent, while "طلب جديد" / "track order" open the item
_KEYWORDS = {
    "new_order": ["طلب جديد", "اطلب جديد", "new order", "order now", "place an order"],
    "track_order": ["تتبع", "تتبيع", "track", "tracking", "وين طلبي", "وين وصل طلبي", "where is my order"],
    "questions": ["اسئله", "استفسار", "استفسارات", "سؤال", "question", "questions", "inquiry",
                  "inquiries", "enquiry"],
    "customer": ["عميل حالي", "عميل جديد", "عميل سابق", "حسابي", "بياناتي", "تسجيل",
                 "existing customer", "new customer", "my account", "my profile", "register"],
    "switch_lang": ["english", "انجليزي", "انقليزي", "الانجليزيه", "arabic", "عربي", "العربيه",
                    "بالعربي", "in english", "in arabic"],
}
_MENU_WORDS = {normalize(w) for w in
               ["menu", "main menu", "القائمة", "قائمة", "المنيو", "منيو", "start", "البداية", "0"]}


def detect_lang(text: str) -> str:
    """English only if the message has Latin letters and no Arabic ones; Arabic otherwise."""
    return "en" if re.search(r"[A-Za-z]", text or "") and not re.search(r"[؀-ۿ]", text or "") else "ar"


def menu_items(lang: str) -> list[dict]:
    return [{"id": i, "label": TEXTS[lang]["labels"][i]} for i in MENU_IDS]


def menu_text(lang: str, head: str | None = None) -> str:
    t = TEXTS[lang]
    lines = [head or t["greeting"]]
    lines += [f"{n}. {t['labels'][i]}" for n, i in enumerate(MENU_IDS, 1)]
    return "\n".join(lines)


def wants_menu(text: str) -> bool:
    return normalize(text).translate(_DIGITS).strip(" .!؟?") in _MENU_WORDS


def match_menu(text: str, menu_just_shown: bool) -> str | None:
    """Menu item chosen by this message, or None. A bare digit 1-5 counts only right after the
    menu was shown (otherwise "1" may be a quantity); labels/keywords count in short messages."""
    n = normalize(text).translate(_DIGITS)
    n = re.sub(r"[^\w\s]", " ", n).strip()               # drop emoji / punctuation
    if menu_just_shown and n in {"1", "2", "3", "4", "5"}:
        return MENU_IDS[int(n) - 1]
    for lang in TEXTS:                                     # exact button label (either language)
        for i, label in TEXTS[lang]["labels"].items():
            if n == re.sub(r"[^\w\s]", " ", normalize(label)).strip():
                return i
    if len(n.split()) <= 4:
        for i, kws in _KEYWORDS.items():
            if any(re.search(rf"(?<!\w){re.escape(normalize(k))}(?!\w)", n) for k in kws):
                return i
    return None


def localize(text: str, lang: str | None) -> str:
    """Fixed replies are written "Arabic — English"; keep only the conversation's language.
    lang=None (evals, CLI) keeps the bilingual text unchanged."""
    if not lang or " — " not in text:
        return text
    ar, en = text.split(" — ", 1)
    # only a real "Arabic — English" pair; an English sentence with a dash stays whole
    if not (re.search(r"[؀-ۿ]", ar) and re.search(r"[A-Za-z]", en) and not re.search(r"[؀-ۿ]", en)):
        return text
    return ar.strip() if lang == "ar" else en.strip()


def format_orders(result: dict, lang: str) -> str:
    t = TEXTS[lang]
    orders = (result or {}).get("orders") or []
    if not orders:
        return t["track_none"]
    lines = [t["track_head"]]
    for o in orders[:5]:
        state = t["states"].get(o.get("state"), o.get("state"))
        deliv = t["delivery"].get(o.get("dispatch_state") or "none", o.get("dispatch_state"))
        lines.append(f"• {o['name']} — {state} — {o['total']:.2f} {t['currency']} — {deliv}")
    return "\n".join(lines + [t["track_tail"]])
