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
    "جرعه", "جرعت", "كم اعطي", "كم مره اخذ", "كم ملغ", "كم ملجم",
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


# opening hours ("مفتوحين 24 ساعة يومياً", "open daily") are store facts, not a dosing schedule
_HOURS = re.compile(r"(24\s*(ساعه|ساعات|/\s*7|hours?)(\s*(يوميا|في اليوم|باليوم|طول الاسبوع|daily|a day))?|"
                    r"طوال (اليوم|الاسبوع|الوقت)|(مفتوح\w*|نفتح|تفتح|يفتح|يعمل|نعمل|تعمل|شغال\w*|الدوام|دوام|"
                    r"ساعات العمل|open\w*)[^.؟?\n]{0,40})", re.I)


def reply_gives_medical_advice(reply: str) -> bool:
    return bool(_REPLY_DOSE.search(_HOURS.sub(" ", normalize(reply))))


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


# ---- 1c'. questions ABOUT a product (label facts, answered by get_product_details) ----
# storage / fridge / temperature, shelf life / expiry, maker / country, form, pack size, strength,
# prescription needed, ingredient. Not use, dose or suitability (those stay medical questions).
_INFO_TERMS = [
    "ثلاجه", "الثلاجه", "بالثلاجه", "تلاجه", "التلاجه", "بالتلاجه", "تبريد", "مبرد", "يتبرد", "بارد", "فريزر",
    "يحفظ", "يتحفظ", "احفظ", "احفظه", "احفظها", "حفظه", "حفظها", "تخزين", "يخزن", "اخزن", "اخزنه", "درجه حراره",
    "حراره الغرفه", "يتحمل الحر", "في الشمس", "من الضوء", "رطوبه", "يتجمد", "يجمد",
    "صلاحي", "انتهاء", "ينتهي", "تنتهي",
    "الشركه", "شركه", "المصنع", "مصنع", "صنع", "صناعه", "امريكي", "اوروبي", "الماني", "سعودي ولا",
    "صناعه سعوديه", "هندي", "اي بلد", "وين مصنوع", "منشا",
    "كم حبه في", "كم حبه فيه", "كم قرص في", "كم في العلبه", "حجم العبوه", "حجم العلبه", "كم مل",
    "شراب ولا", "حبوب ولا", "كبسول ولا", "اقراص ولا", "ابره ولا", "التركيز", "تركيزه", "كم تركيز",
    "يحتاج وصفه", "يبغاله وصفه", "يبي وصفه", "يحتاج روشته", "بدون وصفه", "بوصفه", "المادة الفعاله",
    "الماده الفعاله", "المكونات", "مكوناته", "وش فيه", "ايش فيه",
    "fridge", "refrigerat", "store it", "storage", "stored", "temperature", "room temp", "freeze",
    "expiry", "expire", "shelf life", "manufactur", "made in", "made by", "which company", "country",
    "how many tablets in", "how many in a box", "pack size", "box size", "how many ml", "syrup or",
    "tablets or", "strength", "need a prescription", "needs a prescription", "without a prescription",
    "prescription needed", "active ingredient", "ingredients",
]


_INFO_RE = re.compile(r"(how many|كم)\s+\S+(\s+\S+)?\s+(are\s+)?(in|per|inside|في|فيه|بال|داخل)\s+"
                      r"(a|one|the|each|every|ال)?\s*(box|pack|packet|bottle|pen|علبه|العلبه|عبوه|الباكيت|كرتون|قلم)",
                      re.I)


def product_info_intent(text: str) -> bool:
    """A question about a product's label facts (storage, fridge, shelf life, maker, form, pack,
    strength, prescription status) - answered from get_product_details, not the order wizard."""
    n = normalize(text)
    if medical_advice_intent(text):
        return False
    return bool(_INFO_RE.search(n)) or any(t in n for t in (normalize(x) for x in _INFO_TERMS))


# ---- 1c''. account requests the model handles with a tool (not the order wizard) ------------
_STOCK_ALERT = re.compile(r"(بلغني|بلغوني|نبهني|نبهوني|خبرني|خبروني|ابلغني|كلموني|راسلوني|ارسلولي|"
                          r"تعلموني|اعلمني|notify me|let me know when|tell me when|message me when|"
                          r"alert me)", re.I)
_ADDRESS_CHANGE = re.compile(r"((غير|غيري|غيرو|بدل|حدث|عدل|تغيير|تحديث|تعديل)\s*(لي\s*)?(ال)?عنوان|"
                             r"عنواني (الجديد|تغير|صار)|عنوان (جديد|ثاني)|change (my )?address|"
                             r"new address|update (my )?address)", re.I)


def service_intent(text: str) -> str | None:
    """'stock_alert' ("tell me when it's back") / 'address' (a new delivery address) or None."""
    n = normalize(text)
    if _STOCK_ALERT.search(n):
        return "stock_alert"
    if _ADDRESS_CHANGE.search(n):
        return "address"
    return None


def looks_in_scope(text: str) -> bool:
    n = normalize(text)
    if len(n.split()) <= 3 or product_info_intent(text) or service_intent(text):
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


# ---- 1b. abuse --------------------------------------------------------------
# Insults/swearing aimed at the pharmacy or staff -> fixed polite reply + handover (reason=abusive).
# The model prefers to de-escalate and rarely escalates abuse itself, so this is deterministic too.
# Whole words only (with Arabic prefixes/suffixes): "خراج" (abscess) must not match "خرا", and
# product words such as "زبالة" (أكياس زبالة, trash bags) are deliberately not listed.
_ABUSE_AR = ["غبي", "اغبيا", "حمار", "حمير", "كلب", "كلاب", "حيوان", "حقير", "تافه", "زفت", "خرا", "فاشل",
             "حرامي", "حراميه", "حراميه", "نصاب", "نصابين", "لصوص", "هباب", "وسخ", "قذر", "منحط", "سافل",
             "يلعن", "لعنه", "انقلع", "اخرس", "اخرسي", "تفو", "متخلف", "متخلفين", "ابله", "اهبل", "هبل"]
_ABUSE_AR_RE = re.compile(r"(?<![\w])(?:و|ف|ب|ال|وال|يا ?)?(?:" + "|".join(_ABUSE_AR) + r")(?:ين|ون|ات|ه|ها|كم|كن)?(?![\w])")
_ABUSE_EN_RE = re.compile(r"\b(idiots?|stupid|useless|morons?|dumb|fuck\w*|shit\w*|bastards?|scam(mers?)?|"
                          r"thie(f|ves)|incompetent|pathetic|you suck|losers?|damn you|go to hell)\b")


def abusive_intent(text: str) -> bool:
    n = normalize(text)
    return bool(_ABUSE_AR_RE.search(n) or _ABUSE_EN_RE.search(n))


# ---- 1e. complaints for the pharmacist ----------------------------------------
# Vague or possibly serious complaints never get a product list: the pharmacist has to ask about them
# first ("بطني يوجعني" has many causes). Urgent ones (chest pain, breathing, bleeding, fainting) also
# get emergency advice. Kept tight: "عندي ضغط" (blood pressure), "جهاز قياس ضغط الدم", "فحص دم" don't match.
_BODY_STOMACH = r"(بطن|بطني|بطنه|بطنها|بطنك|معده|معدتي|معدته|معدتها)"
_HURTS = r"(يوجع|توجع|يعور|تعور|يالم|تالم|الم|وجع|اوجاع|واجع|واجعني|واجعه|بيوجع|بتوجع|مقطع|مولع)"
_REFER = [
    rf"{_BODY_STOMACH}.{{0,25}}{_HURTS}|{_HURTS}.{{0,25}}{_BODY_STOMACH}",
    r"(?<![\w])(مغص|مغصه|ومغص)(?![\w])",
    r"stomach ?(ache|pain|hurts|cramps?)|abdominal pain|tummy ache|belly (ache|pain)|my stomach (hurts|is killing)",
]
_URGENT = [
    r"(الم|وجع|ضيق|ضغط|نغزه|نغزات) (في |ب)?(ال)?صدر|صدري (يوجعني|يعورني|ضايق|ضاغط|واجعني|يالمني)",
    r"ضيق (في |ب)?(ال)?(تنفس|نفس)|ما (اقدر|اقدرش|قادر|قادره) (ا|ات)?تنفس|صعوبه (في |ب)?(ال)?تنفس|مخنوق|اختناق",
    r"(?<![\w])(نزيف|ينزف|تنزف|نزف)(?![\w])|(يستفرغ|تستفرغ|استفراغ|ترجيع|يرجع|يكح|كحه|براز|بول) (فيه |مع |معه )?دم",
    r"(?<![\w])(اغمي|اغماء|انغمي|فقد الوعي|فقدت الوعي|غايب عن الوعي)(?![\w])",
    r"chest (pain|tightness|pressure)|can'?t breathe|short(ness)? of breath|trouble breathing|bleeding|"
    r"(vomiting|coughing) blood|blood in (my )?(stool|urine|vomit)|faint(ed|ing)|unconscious|passed out",
]
_REFER_RE = [re.compile(p) for p in _REFER]
_URGENT_RE = [re.compile(p) for p in _URGENT]


def pharmacist_complaint(text: str) -> str | None:
    """"urgent" (emergency advice + pharmacist), "refer" (pharmacist), or None."""
    n = normalize(text)
    if any(rx.search(n) for rx in _URGENT_RE):
        return "urgent"
    if any(rx.search(n) for rx in _REFER_RE):
        return "refer"
    return None


# ---- 1c. order status -------------------------------------------------------
# "Where is my order / has it shipped?" is answered with track_order by fixed logic: given all tools the
# model often reached for create_order instead. Needs BOTH the customer's own order ("طلبي", "my order")
# and a status word, so a new order ("Order 1 Centrum for delivery") is never routed here.
_MY_ORDER = (r"(طلبي|طلبيتي|طلباتي|اوردري|الطلب (تبعي|بتاعي|حقي|مالي|تاعي)|الاوردر (تبعي|بتاعي|حقي|مالي)|"
             r"my orders?\b|order status|status of (my )?order|track (my )?order|my delivery|my parcel)")
_STATUS = (r"(وين|فين|اين|متي|امتي|ايمتي|وصل|يوصل|توصل|وصلو|شحن|اتشحن|انشحن|تاخر|اتاخر|متاخر|حال|صار|"
           r"where|when|arriv|ship|track|late|delay|status|deliver|order)")


def order_status_intent(text: str) -> bool:
    n = normalize(text)
    return bool(re.search(_MY_ORDER, n) and re.search(_STATUS, n))


# ---- canned replies ------------------------------------------------------
# No handover in these replies: a handover is only made when it is tied to an order / cart
# quotation (or names the medicine), so a pharmacist has something to act on.
PHARMACIST_REFERRAL = (
    "سلامتك 🙏 هذي الأعراض يحتاج الصيدلي يسألك عنها بنفسه، تقدر تزور أي فرع من فروعنا ويخدمك مباشرة. "
    "وإذا تبغى منتج معيّن تقدر تكتب اسمه. "
    "— Sorry you're unwell. A pharmacist needs to ask you about these symptoms in person - any of our "
    "branches can help you straight away. If you want a specific product, you can type its name."
)

URGENT_REFERRAL = (
    "سلامتك 🙏 إذا الأعراض شديدة أو مفاجئة، اتصل على الإسعاف 997 أو توجّه لأقرب طوارئ فوراً. "
    "— If the symptoms are severe or sudden, call an ambulance on 997 or go to the nearest emergency "
    "department now."
)

ABUSE_HANDOVER = (
    "نعتذر إن كانت تجربتك غير مرضية. إذا المشكلة في طلب، اكتب رقم الطلب أو اسم الدواء وأحوّلها لزميل يتابعها معك. "
    "— We're sorry your experience wasn't good. If the problem is with an order, send the order number "
    "or the medicine's name and I'll pass it to a colleague to sort out."
)

MEDICAL_REFUSAL = (
    "عذراً، ما أقدر أعطي نصائح طبية أو جرعات. إذا الدواء ضمن طلبك أو سلتك، أرسل صورة الوصفة 📎 "
    "وأقرأ لك الجرعة المكتوبة فيها. ويقدر الصيدلي في أي فرع يجاوب سؤالك. "
    "— Sorry, I can't give medical advice or dosages. If the medicine is in your order or cart, send a "
    "photo of the prescription 📎 and I'll read you the dose written on it. A pharmacist at any of our "
    "branches can also answer your question."
)

# ---- dosage questions about an order / cart: the prescription's own words -----------------
DOSE_ASK_PHOTO = {
    "ar": "سؤالك عن {items} ({ref}). أرسل صورة الوصفة 📎 وأقرأ لك الجرعة المكتوبة فيها كما كتبها الطبيب.",
    "en": "Your question is about {items} ({ref}). Send a photo of the prescription 📎 and I'll read you "
          "the dose exactly as the doctor wrote it.",
}
DOSE_AS_WRITTEN = {
    "ar": "📝 مكتوب في وصفتك:\n{lines}\nهذا كما كتبه الطبيب. إذا فيه شيء غير صحيح أو مختلف عن كلام الطبيب، "
          "قل لي وأحوّل سؤالك مع طلبك {ref} للصيدلي.",
    "en": "📝 Your prescription says:\n{lines}\nThat is exactly as the doctor wrote it. If something is wrong "
          "or different from what the doctor told you, say so and I'll pass your question with order {ref} "
          "to the pharmacist.",
}
DOSE_UNREADABLE = {
    "ar": "ما قدرت أقرأ الجرعة من الوصفة بوضوح، فحوّلت سؤالك مع طلبك {ref} والوصفة للصيدلي ويرد عليك قريباً.",
    "en": "I couldn't read the dose on the prescription clearly, so I've passed your question with order "
          "{ref} and the prescription to the pharmacist, who will reply shortly.",
}
DOSE_DISPUTED = {
    "ar": "تمام، حوّلت سؤالك مع طلبك {ref} ووصفتك للصيدلي، وبيراجعها ويرد عليك قريباً 👨‍⚕️",
    "en": "OK - I've passed your question with order {ref} and your prescription to the pharmacist, who "
          "will check it and reply shortly 👨‍⚕️",
}
_DISPUTE = re.compile(r"(غلط|خطا|خطأ|مو صحيح|مش صحيح|موب صحيح|غير صحيح|مب صحيح|مختلف|ماهو كذا|مو كذا|"
                      r"الدكتور قال|قال لي الدكتور|not right|wrong|incorrect|not correct|different|doctor said)", re.I)


def disputes_directions(text: str) -> bool:
    return bool(_DISPUTE.search(normalize(text)))


NEEDS_CONTEXT = {
    "ar": "عشان أحوّلك لزميل يقدر يساعدك، اكتب رقم الطلب أو اسم الدواء اللي تقصده.",
    "en": "So I can pass this to a colleague who can help, please send the order number or the name of "
          "the medicine it is about.",
}

# Scheduled (مقيد) medicines are dispensed in person only, against a doctor's stamped
# prescription: refused here, never escalated, no photo asked for. Availability only.
SCHEDULED_REFUSAL = (
    "هذا دواء مقيّد (خاضع للرقابة) 🔒: يُصرف حضورياً فقط من الفرع مع وصفة طبية مختومة من الطبيب، ولا يمكن "
    "طلبه أو توصيله عبر المحادثة. أقدر أفيدك بتوفّره فقط. "
    "— This is a controlled (scheduled) medicine 🔒: it is dispensed in person only, at a branch, against a "
    "doctor's stamped prescription, and can't be ordered or delivered through the chat. I can only tell "
    "you where it is available."
)
_SCHED_AVAIL = {"ar": ("📍 متوفر حالياً في: {b}", "📍 حالياً غير متوفر في فروعنا."),
                "en": ("📍 Available now at: {b}", "📍 Not available at our branches right now.")}


def scheduled_refusal(lang: str | None, branches: list[str] | None) -> str:
    """The refusal + where it is in stock (branches=None: availability unknown, e.g. a name the
    denylist caught that is not in the catalogue)."""
    from menu import localize
    text = localize(SCHEDULED_REFUSAL, lang)          # lang None (staff / evals): both languages
    if branches is None:
        return text
    have, none = _SCHED_AVAIL[lang if lang in _SCHED_AVAIL else "ar"]
    return text + "\n" + (have.format(b="، ".join(branches) if (lang or "ar") == "ar" else ", ".join(branches))
                          if branches else none)

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
