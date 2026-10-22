# -*- coding: utf-8 -*-
"""Chat reply text -> speakable sentences for text-to-speech (Arabic / English).

A chat reply is written for the eye: "• بنادول ٥٠٠ ملغ — 12.00 ريال 🛒", menu numbers, links,
order refs like S00120, phone numbers. Read aloud as-is it is noise. This turns it into short,
speakable sentences:
  - emojis, bullets, markdown and arrows removed; links -> "الرابط موجود في الرسالة"
  - prices / quantities / strengths -> number words (12.00 -> اثنا عشر; 500 ملغ -> خمسمائة ملغ)
  - phone numbers and long digit runs -> read digit by digit
  - order refs S00120 -> "رقم 120"
  - split into sentences (one per line / . ؟ ! ؛), so speech can start after the first sentence
    and be cut off between sentences when the customer interrupts (conversation mode)
"""
from __future__ import annotations

import re

try:
    from num2words import num2words
except ImportError:                      # still speakable, just as digits
    num2words = None

_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍←-⇿⬀-⯿]")
_URL = re.compile(r"https?://\S+")
_DIGIT_AR = ["صفر", "واحد", "اثنين", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة"]
_DIGIT_EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_LINK = {"ar": "الرابط موجود في الرسالة.", "en": "The link is in the message."}


def _num_words(n: str, lang: str) -> str:
    try:
        value = float(n) if "." in n else int(n)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, float):                       # 2.5 -> "اثنان فاصلة خمسة"
            whole, frac = n.split(".")
            sep = " فاصلة " if lang == "ar" else " point "
            return _num_words(whole, lang) + sep + _num_words(frac.lstrip("0") or "0", lang)
        if not num2words:
            return n
        words = num2words(value, lang="ar" if lang == "ar" else "en")
        return re.sub(r"\bو\s+", "و", words) if lang == "ar" else words   # "و خمسون" -> "وخمسون"
    except (ValueError, NotImplementedError, OverflowError):
        return n


def _money(m: re.Match, lang: str) -> str:
    """51.75 ريال -> واحد وخمسون ريال وخمسة وسبعون هللة"""
    riyals, halalas, unit = m.group(1), m.group(2), m.group(3)
    out = _num_words(riyals, lang) + " " + unit
    if int(halalas):
        out += (" و" if lang == "ar" else " and ") + _num_words(str(int(halalas)), lang) + \
               (" هللة" if lang == "ar" else " halalas")
    return out


def _digits(run: str, lang: str) -> str:
    names = _DIGIT_AR if lang == "ar" else _DIGIT_EN
    return " ".join(names[int(d)] for d in run if d.isdigit())


def speakable(text: str, lang: str = "ar") -> str:
    """One reply -> plain speakable text (sentences separated by newlines)."""
    t = (text or "").translate(_DIG)
    t = _URL.sub(_LINK.get(lang, _LINK["ar"]), t)
    t = _EMOJI.sub(" ", t)
    t = re.sub(r"[*_`#>|]+|[«»“”\"]", " ", t)                     # markdown, quotes
    t = re.sub(r"(?m)^\s*(\d{1,2}[.)]|[-•▪·])\s+", "", t)          # list numbers / bullets
    t = re.sub(r"\s[—–-]\s", "، ", t)                              # "بنادول — 12 ريال" -> pause
    # S00120 -> "رقم 120" (no second "رقم" when the text already says "رقم S00120")
    t = re.sub(r"(رقم\s+|number\s+)?\b[A-Z]{1,3}0*(\d{2,})\b",
               lambda m: (m.group(1) or ("رقم " if lang == "ar" else "number ")) + m.group(2), t)
    t = re.sub(r"\+?\d[\d ]{7,}\d", lambda m: _digits(m.group(0), lang), t)   # phones: digit by digit
    t = re.sub(r"\b\d{6,}\b", lambda m: _digits(m.group(0), lang), t)
    t = re.sub(r"(\d+)\.(\d{2})\s*(ريال|SAR|riyals?)", lambda m: _money(m, lang), t)
    t = re.sub(r"(\d+)\.00\b", r"\1", t)                           # 12.00 -> 12
    t = re.sub(r"\d+(?:\.\d+)?", lambda m: _num_words(m.group(0), lang), t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n+ *", "\n", t)
    return t.strip()


def sentences(text: str, lang: str = "ar", max_chars: int = 220) -> list[str]:
    """Speakable sentences, short enough for low first-audio latency and clean interruption."""
    out = []
    for line in speakable(text, lang).split("\n"):
        for part in re.split(r"(?<=[.!؟?؛;:])\s+", line):
            part = part.strip(" ،,.")
            if len(re.sub(r"\W", "", part)) < 2:
                continue
            while len(part) > max_chars:                            # very long: cut at a comma/space
                cut = max(part.rfind("،", 0, max_chars), part.rfind(",", 0, max_chars),
                          part.rfind(" ", 0, max_chars))
                cut = cut if cut > 40 else max_chars
                out.append(part[:cut].strip(" ،,"))
                part = part[cut:].strip(" ،,")
            if part:
                out.append(part)
    return out
