# -*- coding: utf-8 -*-
"""Phonetic, cross-script product matching (Arabic <-> English) for search_product.

Customers write drug names the way they sound: "سرفاركس" / "سرفركس" for CERVARIX, "بنتوفير" for
بانتوفير, "باراسيتامول" for PARACETAMOL. Plain text search fails across alphabets and on vowel
spelling. Here every word is reduced to a CONSONANT SOUND KEY in a shared alphabet:
  - vowels and vowel-like letters are dropped (a e i o u w y / ا و ي ى ء ع ...),
  - letters that sound alike map to one symbol (س ص ث / s c(e,i,y) -> s ; ق ك خ / k q c ch -> k ;
    ف ڤ / f v ph -> f ; ب پ / b p -> b ; ج غ / j g -> j ; x -> ks ...),
  - doubled letters collapse.
So CERVARIX, سيرفاريكس and سرفاركس all become "srfrks". Products are ranked by how closely their
words' keys match the query's keys (rapidfuzz), after any exact/normalised substring match.
"""
from __future__ import annotations

import re
import time

from rapidfuzz import fuzz

_DIAC = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")   # tashkeel + tatweel


def normalize(text: str) -> str:
    """Arabic orthographic normalisation + lowercase (for substring matching)."""
    t = _DIAC.sub("", (text or "").lower())
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
           .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    return re.sub(r"\s+", " ", t).strip()


_AR = {
    "ب": "b", "پ": "b", "ت": "t", "ط": "t", "ث": "t", "ج": "j", "غ": "j", "ح": "h", "ه": "h",
    "خ": "k", "ك": "k", "ق": "k", "گ": "j", "د": "d", "ض": "d", "ذ": "z", "ز": "z", "ظ": "z",
    "ر": "r", "س": "s", "ص": "s", "ش": "S", "ف": "f", "ڤ": "f", "ل": "l", "م": "m", "ن": "n",
}
_LAT2 = [("ph", "f"), ("sh", "S"), ("ch", "k"), ("ck", "k"), ("th", "t"), ("gh", "j"),
         ("qu", "k"), ("kh", "k"), ("x", "ks")]
_LAT = {"b": "b", "p": "b", "d": "d", "f": "f", "v": "f", "j": "j", "g": "j", "h": "h", "k": "k",
        "q": "k", "l": "l", "m": "m", "n": "n", "r": "r", "s": "s", "z": "z", "t": "t"}


def sound_key(word: str) -> str:
    """Consonant sound key of ONE word, script-independent."""
    w = normalize(word)
    if re.search(r"[؀-ۿ]", w):                       # Arabic
        out = [_AR.get(ch, "") for ch in w]
    else:                                                       # Latin
        w = re.sub(r"[^a-z]", "", w)
        for a, b in _LAT2:
            w = w.replace(a, b)
        out = []
        for i, ch in enumerate(w):
            if ch == "c":                                       # c -> s before e/i/y, else k
                out.append("s" if i + 1 < len(w) and w[i + 1] in "eiy" else "k")
            else:
                out.append(_LAT.get(ch, ch if ch in "S" else ""))
    key = re.sub(r"(.)\1+", r"\1", "".join(out))                # collapse doubles
    return key


_STOP = {"mg", "ml", "mcg", "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule",
         "syrup", "cream", "gel", "injection", "solution", "for", "and", "of", "with", "the",
         "ملجم", "ملغ", "مل", "مجم", "جم", "اقراص", "قرص", "كبسول", "كبسولات", "شراب", "حقنه",
         "محلول", "كريم", "عندكم", "عندكو", "فيه", "في", "هل", "ابي", "ابغى", "ابغي", "عايز",
         "بدي", "متاح", "متوفر", "موجود", "لقاح", "دواء", "دوا", "علبه", "حبوب",
         # greetings / chat words: never product names
         "السلام", "عليكم", "سلام", "مرحبا", "هلا", "اهلا", "شكرا", "مشكور", "تمام", "طيب",
         "نعم", "ايوه", "ايه", "لا", "اوكي", "ok", "hi", "hello", "thanks", "please", "yes", "no",
         "اطلب", "احجز", "سعر", "بكم", "كم", "وين", "طلبي", "فرع", "توصيل", "استلام"}


def words(text: str) -> list[str]:
    toks = re.findall(r"[a-z؀-ۿ]+", normalize(text))
    return [t for t in toks if t not in _STOP and len(t) >= 2]


class FuzzyIndex:
    """In-memory index of product names -> sound keys. Rebuilt when the catalogue changes."""

    FIELDS = ["name", "x_name_ar", "x_brand", "x_ingredient_en", "x_ingredient_ar",
              "sfda_scientific_name"]
    REFRESH_S = 600

    def __init__(self, client):
        self.c = client
        self.items: list[tuple[int, str, list[str]]] = []   # (template id, normalised text, keys)
        self.built = 0.0
        self.count = -1

    def _fields(self):
        have = set(self.c.execute_kw("product.template", "fields_get", [], {"attributes": ["type"]}))
        return [f for f in self.FIELDS if f in have]

    def ensure(self):
        n = self.c.search_count("product.template", [["sale_ok", "=", True]])
        if self.items and n == self.count and time.time() - self.built < self.REFRESH_S:
            return
        fields = self._fields()
        rows = self.c.search_read("product.template", [["sale_ok", "=", True]], fields)
        items = []
        for r in rows:
            text = " ".join(str(r.get(f) or "") for f in fields)
            keys = sorted({k for w in words(text) for k in [sound_key(w)] if len(k) >= 3})
            items.append((r["id"], normalize(text), keys))
        self.items, self.count, self.built = items, n, time.time()

    @staticmethod
    def _need(qk: str) -> float:
        """Short keys are ambiguous: 3 consonants must match exactly, 4 nearly, 5+ loosely."""
        return 99.0 if len(qk) <= 3 else 88.0 if len(qk) == 4 else 82.0

    def search(self, query: str, limit: int = 5) -> list[tuple[int, float]]:
        """[(template id, score 0-100)] best first. Exact normalised substring = 100."""
        self.ensure()
        qn = normalize(query)
        qkeys = [k for k in (sound_key(w) for w in words(query)) if len(k) >= 3]
        scored = []
        for tid, text, keys in self.items:
            if len(qn) >= 4 and qn in text:
                scored.append((tid, 100.0)); continue
            best = 0.0
            for qk in qkeys:
                need = self._need(qk)
                for k in keys:
                    if qk == k:
                        s = 99.0
                    elif abs(len(qk) - len(k)) <= 2:
                        s = fuzz.ratio(qk, k)
                    elif len(qk) >= 4 and len(k) > len(qk) and k.startswith(qk):
                        s = 90.0                                  # prefix: "srfr" -> "srfrks"
                    else:
                        continue
                    if s >= need:
                        best = max(best, s)
            if best:
                scored.append((tid, best))
        scored.sort(key=lambda x: -x[1])
        if scored and scored[0][1] >= 95:                 # a clear hit: drop clearly worse ones
            scored = [x for x in scored if x[1] >= scored[0][1] - 6]
        return scored[:limit]
