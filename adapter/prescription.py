# -*- coding: utf-8 -*-
"""Prescription photo/PDF -> text -> automatic checks (computerised/printed prescriptions only).

A customer attaches a photo or PDF of a prescription in the chat. We read photos with Tesseract
(Arabic + English, CPU), PDFs from their text layer (or OCR if scanned), and check three things:
  1. a date within the last PRESCRIPTION_MAX_AGE_DAYS days (Saudi time, not in the future),
  2. an invoice / prescription number next to a label ("Invoice No", "رقم الفاتورة", "Rx No"...),
  3. at least one catalogue medicine named on it.
If all three pass, the prescription is auto-verified and the gateway lets the customer order or
hold the prescription-only (non-Scheduled) medicines found on it. Scheduled/controlled medicines
stay fully blocked whatever the prescription says.

The parsing functions are pure (text in, dict out) so they are unit-tested without Tesseract;
ocr_image() is the only part that needs the `tesseract` binary (apt: tesseract-ocr
tesseract-ocr-ara).
"""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz

from fuzzy_search import normalize

PRESCRIPTION_MAX_AGE_DAYS = 5
KSA = timezone(timedelta(hours=3))           # Saudi Arabia: UTC+3, no DST
OCR_ENGINE = "tesseract (ara+eng)"
MATCH_THRESHOLD = 90                          # rapidfuzz partial_ratio for a medicine name

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
# Arabic month names (Gulf / Egypt) and the Levantine ones, as normalize() writes them
_AR_MONTHS = {**{m: i + 1 for i, m in enumerate(
    ["يناير", "فبراير", "مارس", "ابريل", "مايو", "يونيو", "يوليو", "اغسطس", "سبتمبر", "اكتوبر",
     "نوفمبر", "ديسمبر"])},
    **{m: i + 1 for i, m in enumerate(
        ["كانون الثاني", "شباط", "اذار", "نيسان", "ايار", "حزيران", "تموز", "اب", "ايلول",
         "تشرين الاول", "تشرين الثاني", "كانون الاول"])},
    "يونيه": 6, "يوليه": 7, "ابريل": 4, "إبريل": 4}


def today_ksa() -> date:
    return datetime.now(KSA).date()


_BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]")


def tidy(text: str) -> str:
    """Undo PDF text-extraction artefacts (pdftotext on Arabic prescriptions, e.g. Wasfaty):
    invisible direction marks (U+202A-202E...), the lam-alef ligature split out of order
    ("اإللكترونية" -> "الإلكترونية"), mirrored brackets "()Packs 2" and list numbers
    written backwards (".1" -> "1.")."""
    t = _BIDI.sub("", text or "")
    t = re.sub(r"ا([اأإآ])ل", r"ال\1", t)
    t = re.sub(r"\(\)([^\n()]+?)(?=\s*$)", r"(\1)", t, flags=re.M)
    t = re.sub(r"^(\s*)\.(\d{1,2})(?=\s*\S)", r"\1\2. ", t, flags=re.M)
    return t


def clean(text: str) -> str:
    """Normalise OCR text: Arabic-Indic digits -> ASCII, Arabic orthography, lowercase."""
    return normalize(tidy(text).translate(_DIGITS))


# ---- 1. OCR ---------------------------------------------------------------
def ocr_image(data: bytes) -> str:
    """Run Tesseract twice (auto layout + single block) and return both texts joined, which
    recovers more of the date/invoice lines on receipt-style layouts than either pass alone."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
    if img.width < 1600:                                  # small phone shots: upscale for OCR
        f = 1600 / img.width
        img = img.resize((1600, int(img.height * f)), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    texts = []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rx.png"
        img.save(p)
        for psm in ("3", "6"):
            r = subprocess.run(["tesseract", str(p), "stdout", "-l", "ara+eng", "--psm", psm],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                texts.append(r.stdout)
    return "\n".join(texts)


PDF_MAX_PAGES = 3


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b"%PDF-")


def read_document(data: bytes) -> dict:
    """Text of a prescription photo or PDF, plus a PNG preview for Odoo when it was a PDF.
    Computerised PDFs carry their own text layer, which is read directly (exact, instant);
    a scanned PDF without one is rendered page by page and OCR'd. Needs poppler-utils
    (pdftotext / pdftoppm) for PDFs and Tesseract for images.
    Returns {"text", "engine", "preview" (PNG bytes or None), "kind": "pdf"|"image"}."""
    if not is_pdf(data):
        return {"text": ocr_image(data), "engine": OCR_ENGINE, "preview": None, "kind": "image"}
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "rx.pdf"
        pdf.write_bytes(data)
        last = ["-f", "1", "-l", str(PDF_MAX_PAGES)]
        # reading order keeps "label / value" and each medicine on its own lines; layout order
        # keeps a label and its value on one line (invoice numbers) - both, like two OCR passes
        texts = []
        for mode in ([], ["-layout"]):
            r = subprocess.run(["pdftotext", *mode, *last, str(pdf), "-"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                texts.append(tidy(r.stdout))
        text = "\n".join(texts)
        subprocess.run(["pdftoppm", "-png", "-r", "200", *last, str(pdf), str(Path(d) / "p")],
                       capture_output=True, timeout=120)
        pages = sorted(Path(d).glob("p*.png"))
        preview = pages[0].read_bytes() if pages else None
        if len(clean(text)) >= 20:
            return {"text": text, "engine": "pdf text layer (pdftotext)", "preview": preview,
                    "kind": "pdf"}
        # scanned PDF: no usable text layer -> OCR each rendered page
        text = "\n".join(ocr_image(p.read_bytes()) for p in pages)
        return {"text": text, "engine": f"{OCR_ENGINE} on scanned pdf", "preview": preview,
                "kind": "pdf"}


# ---- 2. dates -------------------------------------------------------------
def _mk(y: int, m: int, d: int) -> date | None:
    if not 2000 <= y <= 2100:                 # also drops Hijri years (14xx)
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str) -> list[date]:
    t = clean(text)
    out: list[date] = []
    for y, m, d in re.findall(r"\b(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\b", t):
        out.append(_mk(int(y), int(m), int(d)))
    for a, b, y in re.findall(r"\b(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})\b", t):
        # Saudi prescriptions are day/month/year; fall back to month/day if that is invalid
        out.append(_mk(int(y), int(b), int(a)) or _mk(int(y), int(a), int(b)))
    for d, mon, y in re.findall(r"\b(\d{1,2})[\s\-]*([a-z]{3})[a-z]*\.?[\s\-,]*(\d{4})\b", t):
        if mon in _MONTHS:
            out.append(_mk(int(y), _MONTHS[mon], int(d)))
    for mon, d, y in re.findall(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", t):
        if mon in _MONTHS:
            out.append(_mk(int(y), _MONTHS[mon], int(d)))
    months = "|".join(sorted((re.escape(normalize(m)) for m in _AR_MONTHS), key=len, reverse=True))
    ar = {normalize(k): v for k, v in _AR_MONTHS.items()}
    for d, mon, y in re.findall(rf"(?<!\d)(\d{{1,2}})\s*(?:من\s*)?({months})\s*,?\s*(\d{{4}})(?!\d)", t):
        out.append(_mk(int(y), ar[mon], int(d)))
    return sorted({x for x in out if x})


def prescription_date(text: str, today: date | None = None) -> tuple[date | None, str | None]:
    """The most recent date on the prescription that is not in the future (birth dates are
    older, expiry/"valid until" dates are in the future). Returns (date, problem)."""
    today = today or today_ksa()
    dates = find_dates(text)
    if not dates:
        return None, "no_date"
    past = [d for d in dates if d <= today]
    if not past:
        return dates[0], "date_in_future"
    d = max(past)
    if (today - d).days > PRESCRIPTION_MAX_AGE_DAYS:
        return d, "date_too_old"
    return d, None


# ---- 3. invoice / prescription number ------------------------------------
_LABEL = re.compile(
    r"(invoice|inv\b|bill\b|receipt|prescription|presc\b|rx\b|e-?rx|"
    r"فاتوره|الفاتوره|الوصفه|وصفه|الايصال|ايصال)", re.I)
_DATE_LIKE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")


_NUMBER_LABEL = re.compile(r"(رقم\s*(ال)?(فاتوره|وصفه)|(invoice|prescription|rx|presc)\.?\s*(no|number|#|num)\b)", re.I)


def find_invoice_number(text: str) -> str | None:
    """First token with >= 4 digits within 40 characters after an invoice/prescription label,
    on the same line (skipping words like "No.", "رقم", ":" and anything that is a date). An
    explicit number label ("رقم الوصفة", "Invoice No.") wins over a bare word such as a
    platform name ("وصفتي") next to a barcode."""
    lines = clean(text).splitlines() or [clean(text)]
    for label in (_NUMBER_LABEL, _LABEL):
        for tok in _numbers_after(lines, label):
            return tok
    return None


def _numbers_after(lines, label):
    for line in lines:
        for m in label.finditer(line):
            window = line[m.end(): m.end() + 40]
            for tok in re.findall(r"[a-z0-9][a-z0-9\-/]*", window):
                tok = tok.strip("-/")
                if sum(ch.isdigit() for ch in tok) >= 4 and not _DATE_LIKE.match(tok):
                    yield tok.upper()


# ---- 4. medicines ---------------------------------------------------------
# generic words that must never identify a product on their own
_GENERIC = {"vitamin", "tablet", "tablets", "capsule", "capsules", "syrup", "cream", "insulin",
            "inhaler", "drops", "injection", "solution", "suspension", "ointment", "spray",
            "فيتامين", "حبوب", "شراب", "كريم", "بخاخ", "قطره", "انسولين", "ابره", "مرهم",
            # everyday English words that start product names ("Digital Thermometer") and also
            # appear on prescriptions ("digital MOH seal", "surgical", "pregnancy")
            "digital", "surgical", "pregnancy", "signal", "sunscreen", "calcium", "omega", "medical",
            "health", "baby", "seal", "clinic", "first", "family", "daily", "super", "extra"}
_STRENGTH = re.compile(r"\b\d+([.,]\d+)?\s*(mg|g|ml|mcg|iu|%|ملغ|ملجم|جرام|مل)?\b")


def _terms(p: dict) -> set[str]:
    out = set()
    for k in ("name", "x_name_ar", "x_brand", "sfda_scientific_name"):
        v = clean(p.get(k) or "")
        if not v:
            continue
        base = re.sub(r"\s+", " ", _STRENGTH.sub(" ", v.translate(_DIGITS))).strip()
        # first word alone only for names/brands: a scientific name's first word ("amoxicillin")
        # is shared by many products and must not authorise all of them
        first = base.split(" ")[0] if base and k != "sfda_scientific_name" else ""
        for t in (base, first):
            if len(t) >= 5 and t not in _GENERIC:
                out.add(t)
    return out


_OCR_CONFUSIONS = [("rn", "m"), ("vv", "w"), ("cl", "d")]


def _fold(s: str) -> str:
    """Collapse letter pairs Tesseract commonly confuses, applied to BOTH sides of a match."""
    for a, b in _OCR_CONFUSIONS:
        s = s.replace(a, b)
    return s


def _score(term: str, t: str) -> float:
    """Match score of one product term in the OCR text, 0 if it does not count as a match.
    A term that is mostly generic words ("vitamin c") must appear exactly as a whole word, so
    "vitamin d3" never unlocks it; other terms match fuzzily, a little looser for long names
    (one OCR slip such as m -> rn in "augmentin" costs more on a short word)."""
    core = "".join(w for w in term.split() if w not in _GENERIC)
    if len(core) < 4:
        return 100.0 if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", t) else 0.0
    s = fuzz.partial_ratio(_fold(term), _fold(t))
    return s if s >= (MATCH_THRESHOLD - 6 if len(term) >= 8 else MATCH_THRESHOLD) else 0.0


_COMBO = re.compile(r"^\s*(\+|,|/|&|and\b|with\b|مع\b|و\s?\w)")


def _part_of_combination(term: str, t: str, p: dict) -> bool:
    """A single-ingredient product's scientific name ("amoxicillin") found only as part of a
    combination on the prescription ("Amoxicillin + Clavulanic Acid") does not count: that
    prescription is for the combination, not plain amoxicillin."""
    sci = clean(p.get("sfda_scientific_name") or "")
    if not sci or term not in sci or "," in sci or "+" in sci:
        return False                          # a brand/name match, or the product IS a combination
    hits = [m.end() for m in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", t)]
    return bool(hits) and all(_COMBO.match(t[h:h + 12]) for h in hits)


def match_medications(text: str, products: list[dict]) -> list[dict]:
    """Catalogue products whose name/brand/scientific name appears on the prescription
    (fuzzy, to survive OCR slips). `products` rows: product.template search_read with
    product_variant_id, name, x_name_ar, x_brand, sfda_scientific_name + safety flags."""
    t = clean(text)
    found = []
    for p in products:
        best, term = 0, ""
        for term_ in _terms(p):
            if term_[:4] not in t and term_[-4:] not in t:     # cheap pre-filter
                continue
            s = _score(term_, t)
            if s > best:
                best, term = s, term_
        if best and _part_of_combination(term, t, p):
            continue
        if best:
            found.append({
                "product_id": p["product_variant_id"][0], "template_id": p.get("id"),
                "name_en": p.get("name") or "", "name_ar": p.get("x_name_ar") or "",
                "is_scheduled": bool(p.get("is_scheduled")),
                "requires_prescription": bool(p.get("x_is_restricted")
                                              or p.get("x_requires_prescription")
                                              or p.get("sfda_requires_prescription")),
                "matched": term, "score": round(best, 1)})
    # a medicine named by brand/name ("Augmentin 1g") is what was prescribed: other products
    # that only share its scientific name are not listed (same-ingredient brands are allowed
    # separately, as alternatives, by the gateway)
    sci = {p["product_variant_id"][0]: clean(p.get("sfda_scientific_name") or "") for p in products}
    by_name = [f for f in found if f["matched"] != sci.get(f["product_id"])]
    if by_name:
        found = by_name
    # prescription-only first (they are what the check unlocks), then best/longest match
    found.sort(key=lambda f: (not f["requires_prescription"], -f["score"], -len(f["matched"])))
    return found[:10]


# ---- 4b. directions as written ("Sig: 1 tab twice daily with meals") ----------
_ITEM = re.compile(r"^\s*(?:\d{1,2}\s*[.)\-]|[.\-•*]|rx\s*\d*[:.]?)\s*(?P<name>[A-Za-z\u0600-\u06FF][^\n]{2,80})$", re.I)
_SIG_LABEL = re.compile(r"^\s*(sig|directions?|dose|dosage|instructions?|usage|الجرعه|الجرعة|جرعه الدواء|"
                        r"جرعة الدواء|طريقه الاستخدام|طريقة الاستعمال|طريقة الاستخدام|طريقه الاستعمال|"
                        r"الاستعمال|الاستخدام)\s*\S?\s*[:\-]\s*(?P<sig>.+)$", re.I)
_SIG_WORDS = re.compile(r"(once|twice|three times|every \d+\s*h|\bdaily\b|\bat night\b|morning|before|after|"
                        r"with meals|empty stomach|\b(od|bid|tid|qid|prn|hs)\b|مر[ةه]|مرتين|يومي|كل \d+ ساع|"
                        r"صباح|مساء|قبل|بعد|على الريق|عند النوم)", re.I)
_NOT_ITEM = re.compile(r"^(qty|quantity|refills?|الكميه|الكمية|patient|date|invoice|doctor|dr\.|license|"
                       r"diagnosis|prescriber|dispensed|sample)\b", re.I)


def extract_directions(text: str) -> list[dict]:
    """[{medicine, directions}] for each numbered Rx line, the directions copied as written. The
    assistant repeats these to the customer; it never derives or changes a dose."""
    out, cur = [], None
    for raw in tidy(text).translate(_DIGITS).splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _ITEM.match(line)
        sig = _SIG_LABEL.match(line)
        if sig and cur is not None:          # every labelled line, as written (dose, how to take it)
            part = sig.group("sig").strip()
            if part and part not in cur["directions"]:
                cur["directions"] = (cur["directions"] + " — " + part) if cur["directions"] else part
            continue
        if m and not _NOT_ITEM.match(m.group("name")) and not sig:
            name = m.group("name").strip()
            if any(o["medicine"] == name for o in out):     # OCR text often repeats the page
                cur = None
                continue
            cur = {"medicine": name, "directions": ""}
            out.append(cur)
            continue
        if cur is not None and not cur["directions"] and _SIG_WORDS.search(line) \
                and not _NOT_ITEM.match(line):
            cur["directions"] = line
    return [o for o in out if o["directions"]]


# ---- 5. all checks ---------------------------------------------------------
def evaluate(text: str, products: list[dict], today: date | None = None) -> dict:
    problems = []
    if len(clean(text)) < 20:
        problems.append("unreadable")
    d, date_problem = prescription_date(text, today)
    if date_problem:
        problems.append(date_problem)
    invoice = find_invoice_number(text)
    if not invoice:
        problems.append("no_invoice")
    meds = match_medications(text, products)
    if not meds:
        problems.append("no_medication")
    return {"ok": not problems, "date": d.isoformat() if d else None, "invoice_number": invoice,
            "medications": meds, "problems": problems, "directions": extract_directions(text)}
