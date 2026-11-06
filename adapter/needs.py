# -*- coding: utf-8 -*-
"""Customer NEEDS -> over-the-counter products, for "I want something for a headache".

The model reads the need from the customer's words (search_product's `need` argument - it understands
dialects and phrasing far better than a keyword list). This fixed table then decides which products are
shown: the model never picks a medicine or an active ingredient itself, which would be medical
judgement. Over-the-counter only - prescription-only, restricted and scheduled products are never listed
for a need. detect_need() is the keyword fallback for when the model did not set `need`.

First draft for the pharmacist to review: each term is an active ingredient or a catalogue product name,
searched with the normal product search. Deliberately absent: vague complaints such as stomach pain
(many causes - left to the pharmacist) and anything needing a prescription.
"""
from __future__ import annotations

import re

NEEDS: dict[str, dict] = {
    "pain_headache": {"terms": ["paracetamol", "ibuprofen"], "ar": "الصداع والألم", "en": "headache and pain"},
    "fever": {"terms": ["paracetamol", "ibuprofen"], "ar": "الحرارة", "en": "fever"},
    "cold_flu": {"terms": ["Panadol Cold & Flu", "Congestal"], "ar": "الزكام والإنفلونزا", "en": "cold and flu"},
    "blocked_nose": {"terms": ["Otrivin"], "ar": "انسداد الأنف", "en": "a blocked nose"},
    "sore_throat": {"terms": ["Strepsils"], "ar": "التهاب الحلق", "en": "a sore throat"},
    "cough": {"terms": ["guaifenesin", "ambroxol"], "ar": "الكحة", "en": "cough"},
    "heartburn": {"terms": ["Gaviscon", "Rennie"], "ar": "الحموضة", "en": "heartburn"},
    "bloating_gas": {"terms": ["simethicone"], "ar": "الغازات والانتفاخ", "en": "gas and bloating"},
    "diarrhea_dehydration": {"terms": ["ORS"], "ar": "الإسهال والجفاف", "en": "diarrhoea and dehydration"},
    "allergy": {"terms": ["loratadine", "cetirizine"], "ar": "الحساسية", "en": "allergy"},
    "dry_skin": {"terms": ["CeraVe"], "ar": "جفاف البشرة", "en": "dry skin"},
    "sun_protection": {"terms": ["Sunscreen"], "ar": "الحماية من الشمس", "en": "sun protection"},
    "wound_care": {"terms": ["Betadine", "Dettol", "Band-Aid"], "ar": "الجروح والتعقيم", "en": "cuts and wounds"},
    "immunity_vitamins": {"terms": ["Vitamin C", "Zinc"], "ar": "المناعة والفيتامينات", "en": "immunity and vitamins"},
}

# keyword fallback, on normalized text; checked in this order (dry skin before dehydration: both say جفاف)
_W = r"(?<![\w])(?:و|ف|ب|ل|ال|لل|بال|وال)?"           # optional Arabic prefixes, whole words only
_PATTERNS: list[tuple[str, str]] = [
    ("pain_headache", _W + r"(صداع|مصدع|شقيقه|مسكن|مسكنات)|(وجع|الم|اوجاع) (ال)?(راس|ضرس|اسنان|ظهر|عضلات|مفاصل)|"
                      r"راسي (يعورني|يوجعني|بيوجعني|واجعني|بينفجر)|headache|migraine|toothache|back ?pain|"
                      r"muscle pain|painkillers?|pain relie"),
    ("fever", _W + r"(حمي|سخونه|سخونيه|حراره)(?![\w])|fever|high temperature"),
    ("blocked_nose", r"انسداد (ال)?انف|(ال)?انف مسدود|خشمي (مسدود|مكتوم)|احتقان (ال)?انف|blocked nose|stuffy nose|"
                     r"nasal congestion"),
    ("cold_flu", _W + r"(زكام|مزكوم|رشح|مرشح|انفلونزا|انفلونزه|نزله برد|نزلة برد)|(?<![\w])flu(?![\w])|common cold|"
                 r"runny nose"),
    ("sore_throat", r"(التهاب|وجع|الم) (ال)?(حلق|زور|بلعوم)|حلقي (يوجعني|يعورني|ملتهب|واجعني)|زوري (بيوجعني|واجعني)|"
                    r"sore throat"),
    ("cough", _W + r"(كحه|سعال|يكح|اكح|كحكحه)(?![\w])|cough"),
    ("heartburn", _W + r"(حموضه|حرقان|ارتجاع)|حرقه (ب|في )?(ال)?(معده|صدر)|heartburn|acid reflux|acidity|antacid"),
    ("bloating_gas", _W + r"(انتفاخ|غازات|نفخه)(?![\w])|bloating|(?<![\w])gas(?![\w])"),
    ("dry_skin", r"(بشره|جلد|بشرتي|جلدي) (جاف|جافه|ناشف|ناشفه)|جفاف (ال)?(بشره|جلد)|" + _W + r"(ترطيب|مرطب)|"
                 r"dry skin|moisturi"),
    ("diarrhea_dehydration", _W + r"(اسهال|جفاف)(?![\w])|diarrh|dehydrat"),
    ("allergy", _W + r"(حساسيه|عطاس|عطس|يعطس)(?![\w])|allerg|hay fever|sneez"),
    ("sun_protection", r"واقي (ال)?شمس|حروق (ال)?شمس|حرق (ال)?شمس|sunscreen|sunblock|sunburn|(?<![\w])spf"),
    ("wound_care", _W + r"(جرح|جروح|مجروح|تعقيم|لزقه|لزقات)(?![\w])|(?<![\w])cuts?(?![\w])|wound|graze|antiseptic"),
    ("immunity_vitamins", _W + r"(مناعه|فيتامينات)(?![\w])|immunity|immune|vitamins"),
]
_COMPILED = [(k, re.compile(p)) for k, p in _PATTERNS]

# product names that contain a symptom word: an order for a product, not a symptom
# ("ثلاث علب محلول جفاف" = oral rehydration salts, "ميزان حرارة" = a thermometer)
PRODUCT_PHRASES = re.compile(r"محلول (ال)?(جفاف|معالجه)|اكياس (ال)?جفاف|(ميزان|مقياس|ترمومتر) (ال)?حراره|"
                             r"(لزقه|لزقات|بلاستر) (ال)?جروح|thermometer|rehydration salts|(?<![\w])ors(?![\w])")


def _norm(text: str) -> str:
    t = re.sub(r"[ً-ْـ]", "", (text or "").lower())      # diacritics, tatweel
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def detect_need(text: str) -> str | None:
    """The need a message asks a product for ("ابغي دوا للصداع" -> pain_headache), or None."""
    n = _norm(text)
    return next((k for k, rx in _COMPILED if rx.search(n)), None)
