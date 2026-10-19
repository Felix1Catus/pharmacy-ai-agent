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
    "جرعه", "اعطي", "اعطيه", "اعطيها", "كم اعطي", "كم مره اخذ", "كم ملغ", "كم ملجم",
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


def medical_advice_intent(text: str) -> bool:
    n = normalize(text)
    # English "how many/much ... take" guard so "how much is panadol" (a price q) doesn't trip
    if ("how many" in n or "how much" in n) and "take" not in n and "give" not in n:
        pass  # not a dosage question by itself
    else:
        if "how many" in n or "how much" in n or "how often" in n:
            return True
    return any(term in n for term in _MEDICAL_TERMS)


# ---- 2. scheduled/controlled medicine denylist ---------------------------
class ScheduledDenylist:
    """Terms (name/brand/ingredient, normalized) for products flagged is_scheduled in Odoo."""

    def __init__(self, terms: list[str]):
        self.terms = [normalize(t) for t in terms if t and len(normalize(t)) >= 3]

    @classmethod
    def from_odoo(cls, client) -> "ScheduledDenylist":
        rows = client.search_read(
            "product.template", [["is_scheduled", "=", True]],
            ["name", "x_name_ar", "x_brand", "x_ingredient_en", "x_ingredient_ar"])
        terms: list[str] = []
        for r in rows:
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
    "هذا الدواء يحتاج وصفة طبية. أرسل صورة الوصفة الإلكترونية من زر المرفقات 📎 (بتاريخ آخر 5 أيام "
    "وفيها رقم الفاتورة واسم الدواء) وأكمل لك الطلب مباشرة، أو أحوّلك إلى الصيدلي إذا تفضّل. "
    "— This medicine needs a prescription. Attach a photo of your computerised prescription with "
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
