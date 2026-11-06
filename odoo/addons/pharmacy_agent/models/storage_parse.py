"""Structured storage conditions from the SFDA storage text (no Odoo imports: unit-testable).

SFDA writes storage as free Arabic/English text, e.g.
  "يخزن الدواء في الثلاجة في درجة حرارة ( 2 درجة الى 8 درجة مئوية )"
  "يحفظ في درجة حرارة الغرفة ( بحيث لا تتجاوز 30 درجة مئوية ) ، يحفظ بعيدًا عن الضوء"
  "Store below 30°C, Store in a dry place"
parse_storage() turns it into a class (fridge / room / cool / freezer), a temperature range and
the extra conditions, so the assistant can answer "does it need the fridge?" from fields.
"""
from __future__ import annotations

import re

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

STORAGE_CLASSES = [
    ("fridge", "Refrigerate (2-8 °C)"),
    ("freezer", "Freezer"),
    ("cool", "Cool (below 15 °C)"),
    ("room", "Room temperature"),
    ("unknown", "Not recorded"),
]


def _temps(t: str) -> list[float]:
    # numbers followed (within a few words) by a degree word: "2 درجة الى 8 درجة مئوية", "30°C"
    return [float(x.replace(" ", "")) for x in
            re.findall(r"((?:-\s*)?\d+(?:\.\d+)?)\s*(?:°|درج|مئوي|c\b|degrees?)", t)]


def parse_storage(text: str | None) -> dict:
    """{storage_class, min_c, max_c, protect_light, protect_moisture, no_freeze, cold_chain}."""
    out = {"storage_class": "unknown", "min_c": None, "max_c": None, "protect_light": False,
           "protect_moisture": False, "no_freeze": False, "cold_chain": False}
    if not text or not text.strip():
        return out
    t = text.translate(_AR_DIGITS).lower()
    out["protect_light"] = bool(re.search(r"الضوء|light", t))
    out["protect_moisture"] = bool(re.search(r"الرطوب|رطوبه|dry place|moisture|humidity", t))
    out["no_freeze"] = bool(re.search(r"لا\s*(تدع\s*\S+\s*)?(يجمد|يتجمد|تجمد)|do not freeze|not be frozen", t))
    temps = _temps(t)
    fridge = bool(re.search(r"الثلاج|ثلاجه|التبريد|refrigerat|fridge", t))
    freezer = bool(re.search(r"المجمد|فريزر|freezer|frozen at|below -", t)) or any(x < 0 for x in temps)
    below = re.search(r"(لا\s*تتجاوز|اقل\s*من|أقل\s*من|لا\s*يحفظ\s*بدرجة\s*حرارة\s*أعلى\s*من|"
                      r"اعلى\s*من|below|not above|not exceeding|up to)\s*(?:درجة\s*حرارة\s*)?(-?\d+)", t)
    if freezer:
        out["storage_class"] = "freezer"
        neg = [x for x in temps if x < 0]
        out["min_c"], out["max_c"] = (min(neg), max(neg)) if len(neg) >= 2 else (None, max(neg) if neg else None)
    elif len(temps) >= 2 and max(temps) <= 8.5 and min(temps) >= 0:
        out["storage_class"] = "fridge"
        out["min_c"], out["max_c"] = min(temps), max(temps)
    elif fridge:
        out["storage_class"] = "fridge" if not temps or max(temps) <= 10 else "cool"
        out["min_c"] = min(temps) if len(temps) >= 2 else (2.0 if not temps else None)
        out["max_c"] = max(temps) if temps else 8.0
    elif below or temps:
        mx = float(below.group(2)) if below else max(temps)
        out["max_c"] = mx
        if len(temps) >= 2 and min(temps) < mx:
            out["min_c"] = min(temps)
        out["storage_class"] = "cool" if mx <= 15 else "room"
    elif re.search(r"حرار[ةه]\s*الغرف|room temperature", t):
        out["storage_class"] = "room"
    out["cold_chain"] = out["storage_class"] in ("fridge", "freezer", "cool")
    return out


def shelf_life_months(raw: str | None) -> int:
    m = re.search(r"\d+", (raw or "").translate(_AR_DIGITS))
    return int(m.group()) if m else 0


def manufacturer_country(raw: str | None) -> str:
    """'الاسم: X | الدولة: Saudi Arabia' (several manufacturers separated by new lines) -> countries."""
    seen = []
    for c in re.findall(r"الدولة\s*:\s*([^|\n;]+)", raw or ""):
        c = c.strip()
        if c and c not in seen:
            seen.append(c)
    return ", ".join(seen)


def manufacturer_names(raw: str | None) -> str:
    seen = []
    for n in re.findall(r"الاسم\s*:\s*([^|\n;]+)", raw or ""):
        n = n.strip()
        if n and n not in seen:
            seen.append(n)
    return ", ".join(seen) or (raw or "").strip()
