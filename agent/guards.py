# -*- coding: utf-8 -*-
"""Deterministic safety guards that run BEFORE the LLM.

Two instant guards, independent of the model's cooperation:
1. medical_advice_intent(text): dosage / drug-safety / interaction questions -> the agent
   refuses with a fixed polite message and escalates. Guarantees the 100% medical gate.
2. ScheduledDenylist: controlled/scheduled medicines (flagged is_scheduled in Odoo, i.e. from
   the pharmacy's official scheduling) are matched by name/brand/ingredient in the incoming
   message and hard-blocked, never routed to the ordering tools.

Arabic text is normalized (alef/ya/ta-marbuta + diacritics) so dialectal spellings still match.
"""
from __future__ import annotations

import re

# ---- Arabic normalization ------------------------------------------------
_AR_DIACRITICS = re.compile(r"[ؗ-ًؚ-ْٰـ]")  # harakat + tatweel


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = _AR_DIACRITICS.sub("", t)
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
           .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    return re.sub(r"\s+", " ", t).strip()


# ---- 1. medical-advice intent -------------------------------------------
# Dosage, drug-safety, interactions, diagnosis. Deliberately NOT triggered by ordinary orders
# ("I want panadol for my kid") — only by advice/dosage phrasing.
_MEDICAL_TERMS = [
    # dosage (ar) — "جرعه" plus administration verbs ("give/take") which, unlike bare "كم حبة"
    # (also a stock question), signal a dosage/medical question.
    "جرعه", "كم اعطي", "كم مره اخذ", "كم ملغ", "كم ملجم",
    # advice / recommendation / diagnosis (ar)
    "المناسب لي", "الدواء المناسب", "العلاج المناسب", "وش اعطي", "ايش اعطي", "وش الدوا",
    "ايش الدوا", "اخد ايه", "اخد ايش", "اخذ ايش", "وش اخذ", "ايش اخذ", "ايه اخد", "شو اخذ",
    "تنصحني", "تنصح", "انصح", "امن مع", "ينفع اخذ", "ينفع اشرب", "اقدر اخذ", "اخذها مع",
    "مع بعض", "اعراض", "تشخيص", "حامل اخذ", "للحامل",
    # english
    "dose", "dosage", "how often", "is it safe", "can i take", "should i take",
    "safe to take", "interact", "interaction", "side effect", "symptom", "diagnos",
    "prescribe me", "overdose",
]


# "كم <unit> ... <take/per day>" in any dialect = dosage ("كم حباية بنادول باخد باليوم"), while
# "كم حبة في العلبة" (pack size) is a product question and stays allowed.
_DOSE_UNIT = r"(حبه|حبايه|حبات|حبوب|حباية|قرص|اقراص|ملعقه|معلقه|ملاعق|مره|مرات|نقطه|نقط|بخه|بخات|كبسوله|ml|مل)"
_DOSE_CTX = (r"(باليوم|في اليوم|فاليوم|يوميا|اليوم|كل يوم|بالنهار|بالليل|كل \d+ ساع|اخذ|اخد|باخد|باخذ|"
             r"بخد|ناخذ|ناخد|ياخذ|ياخد|تاخذ|تاخد|اشرب|بشرب|يشرب|اعطي|نعطي|للطفل|لطفل|لابني|لبنتي|"
             r"عشان|علشان|مشان|لاجل|عمره|عمرها|سنه|سنين|كيلو)")
_PACK = r"(علبه|العلبه|الباكيت|باكيت|الكرتون|كرتون|الشريط|شريط|العبوه|عبوه)"
_EN_DOSE = (r"(how many|how much).{0,40}(take|give|per day|a day|daily|every|times)|"
            r"(per day|a day|daily|every \d+ hours?)\b.{0,20}\?|how often|max(imum)? (dose|per day)")


def medical_advice_intent(text: str) -> bool:
    n = normalize(text)
    if re.search(_EN_DOSE, n):
        return True
    if re.search(rf"كم\s.{{0,25}}{_DOSE_UNIT}", n) and re.search(_DOSE_CTX, n) \
            and not re.search(_PACK, n):
        return True
    return any(term in n for term in _MEDICAL_TERMS)


# Safety net on the MODEL'S reply: dosage / frequency / treatment instructions are never sent,
# whatever the model was talked into; the agent replaces them with the medical refusal.
_REPLY_DOSE = re.compile(
    r"(يوميا|يومياً|باليوم|في اليوم|كل \d+\s*(ساعه|ساعات)|مرتين|ثلاث مرات|مره واحده|كحد اقصي|"
    r"الجرعه|جرعه|حسب توجيه الطبيب|قبل الاكل|بعد الاكل|على معده|"
    r"\bdaily\b|per day|a day\b|every \d+ hours?|times a day|twice a day|\bdose\b|dosage|"
    r"max(imum)? (of )?\d|before meals|after meals|empty stomach)", re.I)


def reply_gives_medical_advice(reply: str) -> bool:
    return bool(_REPLY_DOSE.search(normalize(reply)))


# ---- 1b. prompt injection / manipulation ----------------------------------
# Customer text is DATA. Attempts to re-instruct the assistant, pull its prompt, impersonate the
# system or drive tools by name are answered with a fixed reply and never reach the model.
_INJ_VERB = r"(ignore|forget|disregard|override|bypass|تجاهل|انس|انسي|انسا|تخط)"
_INJ_OBJ = (r"(previous|prior|above|earlier|all|every|everything|your|system|instruction|instructions|"
            r"prompt|prompts|rules|guidelines|restrictions|context|التعليمات|تعليمات|الاوامر|اوامر|"
            r"السابق|السابقه|كل|القواعد|قواعد|النظام|البرومبت|القيود|البرمجه)")
_INJECTION = [
    rf"{_INJ_VERB}\b[\s\w,]{{0,30}}?\b{_INJ_OBJ}",
    r"system\s*prompt", r"system\s*message", r"(previous|prior|initial|original|hidden)\s+(prompt|instruction)",
    r"new\s+(instructions|rules|role|persona)", r"you\s+are\s+(now|no longer|free)",
    r"\bact\s+as\b", r"\bpretend\b", r"role\s*-?\s*play", r"jail\s*break", r"developer\s+mode",
    r"do\s+anything\s+now", r"\bsudo\b", r"admin\s+mode", r"god\s+mode",
    r"(reveal|show|print|repeat|tell me|output|leak)\s+(me\s+)?(your|the)\s+(prompt|instructions|rules|system)",
    r"(call|run|execute|invoke|use)\s+(the\s+)?(tool|function|api|action)",
    r"<\|?im_(start|end)\|?>", r"</?tool_call>", r"\[\s*(system|prescription verified|inst)\b",
    r"\b(search_product|list_branches|check_stock|create_order|create_dispatch|hold_item|"
    r"track_order|escalate_to_human|customer_id|product_id|branch_id)\b",
    r"تعليمات\s+النظام", r"موجه\s+النظام", r"انت\s+الان", r"من\s+الان\s+انت", r"تقمص", r"تظاهر",
    r"بدون\s+(قيود|قواعد)", r"اكسر\s+القيود", r"وضع\s+المطور", r"نفذ\s+(الامر|الاداه|الداله)",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.I)


def injection_attempt(text: str) -> bool:
    return bool(_INJECTION_RE.search(normalize(text)))


# ---- 1c. scope: is this message about the pharmacy at all? ----------------
# Deterministic first pass; the model is also told to refuse anything off-topic. Short messages
# (a product name, "yes", an address piece) always pass - the search then decides.
_SCOPE_TERMS = [
    # ar: products / ordering / logistics
    "دواء", "دوا", "ادويه", "علاج", "صيدل", "وصفه", "روشته", "فرع", "فروع", "طلب", "اطلب", "اوردر",
    "توصيل", "توصلون", "استلام", "سعر", "اسعار", "بكم", "كم سعر", "متوفر", "متاح", "موجود", "عندكم",
    "عندك", "فيه", "حجز", "احجز", "كميه", "علبه", "حبه", "حبوب", "شراب", "كريم", "بخاخ", "قطره",
    "ابره", "حقن", "فيتامين", "مكمل", "حليب", "حفاض", "معقم", "كمام", "ريال", "الغي", "الغاء",
    "تتبع", "وين", "شحن", "مندوب", "عنوان", "حي", "شارع", "مدينه", "الرياض", "جده", "الدمام", "مكه",
    "ابغى", "ابغي", "ابي", "بدي", "عايز", "محتاج", "منتج", "ماركه", "بديل", "تركيز", "ملغ", "ملجم",
    "صداع", "حراره", "زكام", "كحه", "سعال", "الم", "حساسيه", "بشره", "شعر", "اسنان", "اطفال", "رضيع",
    # en
    "medicine", "medication", "drug", "pharmac", "prescription", "branch", "order", "deliver",
    "pickup", "pick up", "collect", "price", "cost", "how much", "availab", "in stock", "stock",
    "do you have", "have you got", "do you sell", "reserve", "hold", "quantity", "box", "pack",
    "tablet", "pill", "capsule", "syrup", "cream", "spray", "drops", "inhaler", "vitamin",
    "supplement", "diaper", "nappy", "mask", "sanitizer", "riyal", "sar", "cancel", "track",
    "where is", "shipping", "address", "street", "riyadh", "jeddah", "dammam", "product", "brand",
    "generic", "alternative", "mg", "headache", "fever", "cold", "cough", "pain", "allergy", "skin",
    "hair", "baby", "need", "want", "buy",
]
_SMALL_TALK = ["السلام", "سلام", "مرحبا", "هلا", "اهلا", "صباح", "مساء", "شكرا", "مشكور", "يعطيك",
               "تمام", "طيب", "اوكي", "نعم", "لا", "ايوه", "hi", "hello", "hey", "thanks", "thank",
               "good morning", "good evening", "ok", "yes", "no"]


def looks_in_scope(text: str) -> bool:
    n = normalize(text)
    if len(n.split()) <= 3:
        return True
    return any(t in n for t in _SCOPE_TERMS) or any(
        re.search(rf"(?<!\w){re.escape(t)}(?!\w)", n) for t in _SMALL_TALK)


# ---- 1d. confirmation before state-changing tools -------------------------
_AFFIRM = ["نعم", "ايوه", "ايوا", "ايه", "اكيد", "اكد", "اكدت", "اكده", "اكدها", "تاكيد", "موكد",
           "ثبت", "ثبته", "ثبتها", "يلا", "يالله", "تمام", "موافق", "اوكي", "يب",
           "صح", "كمل", "اعتمد", "اطلبه", "اطلبها", "احجزه", "احجزها", "تم", "ابشر", "زين", "طيب",
           "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "go ahead",
           "please do", "place it", "do it", "correct", "right"]


def is_affirmation(text: str) -> bool:
    n = normalize(text)
    if re.search(r"(?<!\w)(لا|no|not|cancel|الغ|مو|مش)(?!\w)", n):
        return False
    return any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", n) for a in _AFFIRM)


# ---- 2. scheduled/controlled medicine denylist ---------------------------
class ScheduledDenylist:
    """Terms (name/brand/ingredient, normalized) for products flagged is_scheduled in Odoo."""

    def __init__(self, terms: list[str]):
        self.terms = [normalize(t) for t in terms if t and len(normalize(t)) >= 3]

    @classmethod
    def from_odoo(cls, client) -> "ScheduledDenylist":
        rows = client.search_read(
            "product.template", [["is_scheduled", "=", True]],
            ["name", "x_name_ar", "x_brand", "x_ingredient_en", "x_ingredient_ar",
             "sfda_scientific_name"])
        terms: list[str] = []
        for r in rows:
            # SFDA active ingredient ("PREGABALIN"), whole, single-ingredient only: a combination
            # like "PARACETAMOL, CODEINE" must not block plain paracetamol
            sci = (r.get("sfda_scientific_name") or "").strip()
            if sci and not re.search(r"[,+/&]| and ", sci.lower()):
                terms.append(sci)
            for k in ("name", "x_name_ar", "x_brand", "x_ingredient_en", "x_ingredient_ar"):
                v = r.get(k)
                if v:
                    # first token too (e.g. "Tramadol 50mg" -> "tramadol")
                    terms.append(v)
                    terms.append(v.split()[0])
        return cls(terms)

    def match(self, text: str) -> str | None:
        n = normalize(text)
        for t in self.terms:
            if t and t in n:
                return t
        return None


# ---- canned replies ------------------------------------------------------
MEDICAL_REFUSAL = (
    "عذراً، لا أستطيع تقديم نصائح طبية أو تحديد الجرعات. سأحوّلك إلى الصيدلي للمساعدة. "
    "يمكنني مساعدتك في إيجاد الأدوية ومعرفة أسعارها وتوفّرها (عدا الأدوية المجدولة/الخاضعة للرقابة). "
    "— Sorry, I can't give medical advice or dosages; I'll refer you to a pharmacist. "
    "I can help you find medicines, prices and availability (except scheduled/controlled medicines)."
)

SCHEDULED_REFUSAL = (
    "هذا الدواء من الأدوية المجدولة/الخاضعة للرقابة ولا يمكن طلبه عبر المساعد. سأحوّلك إلى الصيدلي. "
    "يمكنني مساعدتك في الأدوية الأخرى وأسعارها وتوفّرها. "
    "— That medicine is scheduled/controlled and can't be ordered through the assistant; "
    "I'll refer you to a pharmacist. I can help with other medicines, prices and availability."
)

PRESCRIPTION_HANDOVER = (
    "هذا الدواء يحتاج وصفة طبية، لذلك لا يمكن طلبه أو حجزه عبر المحادثة. سأحوّلك إلى الصيدلي "
    "لإكمال الشراء بعد التحقق من الوصفة. "
    "— This medicine needs a prescription, so it can't be ordered or held through the chat. "
    "I'll pass you to a pharmacist to complete the purchase after checking the prescription."
)

PRESCRIPTION_NEEDED = (
    "هذا الدواء يحتاج وصفة طبية. أرسل صورة أو ملف PDF للوصفة الإلكترونية من زر المرفقات 📎 (بتاريخ آخر 5 أيام "
    "وفيها رقم الفاتورة واسم الدواء) وأكمل لك الطلب مباشرة، أو أحوّلك إلى الصيدلي إذا تفضّل. "
    "— This medicine needs a prescription. Attach a photo or PDF of your computerised prescription with "
    "the 📎 button (dated within the last 5 days, showing the invoice number and the medicine) and "
    "I'll complete the order right away — or I can pass you to a pharmacist."
)

# why an attached prescription was not accepted (codes from adapter/prescription.py)
_RX_PROBLEMS = {
    "unreadable": ("الصورة غير واضحة ولم أستطع قراءتها", "the photo couldn't be read"),
    "no_date": ("لم أجد تاريخ الوصفة", "I couldn't find the prescription date"),
    "date_too_old": ("تاريخ الوصفة أقدم من 5 أيام", "the prescription is older than 5 days"),
    "date_in_future": ("تاريخ الوصفة غير صحيح", "the prescription date isn't valid"),
    "no_invoice": ("لم أجد رقم الفاتورة/الوصفة", "I couldn't find the invoice/prescription number"),
    "no_medication": ("لم أجد في الوصفة دواءً من أدويتنا", "I couldn't find one of our medicines on it"),
    "invoice_already_used": ("هذه الوصفة مستخدمة من قبل", "this prescription was already used"),
}


def prescription_rejected(problems: list[str], rx_date: str | None = None) -> str:
    ar = [_RX_PROBLEMS.get(p, (p, p))[0] for p in problems]
    en = [_RX_PROBLEMS.get(p, (p, p))[1] for p in problems]
    when = f" ({rx_date})" if rx_date and "date_too_old" in problems else ""
    return ("استلمت صورة الوصفة لكن لم أستطع اعتمادها: " + "، ".join(ar) + when + ". "
            "أرسل صورة أوضح لوصفة إلكترونية حديثة (آخر 5 أيام) فيها رقم الفاتورة واسم الدواء، "
            "أو أحوّلك إلى الصيدلي. "
            "— I received the prescription photo but couldn't accept it: " + "; ".join(en) + when +
            ". Send a clearer photo of a recent computerised prescription (last 5 days) showing the "
            "invoice number and the medicine, or I can pass you to a pharmacist.")
