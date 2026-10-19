# -*- coding: utf-8 -*-
"""Generate a sample COMPUTERISED prescription image (dummy data) to test the prescription
photo flow in the chat page and in adapter/test_prescription.py.

  python evals/datasets/make_prescription.py out.png                     # today, Augmentin 1g
  python evals/datasets/make_prescription.py out.png --days-ago 9        # too old -> rejected
  python evals/datasets/make_prescription.py out.png --drug "Ventolin Inhaler" --invoice INV-2026-00777
  python evals/datasets/make_prescription.py out.png --no-invoice
  python evals/datasets/make_prescription.py out.pdf                     # PDF with a text layer
  python evals/datasets/make_prescription.py out.pdf --scanned           # image-only PDF (OCR)
"""
from __future__ import annotations

import argparse
import io
from datetime import timedelta

from PIL import Image, ImageDraw, ImageFont

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "adapter"))
from prescription import today_ksa  # noqa: E402


def _font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _lines(drugs, days_ago, invoice) -> list[str]:
    d = today_ksa() - timedelta(days=days_ago)
    lines = ["AL SHIFA MEDICAL CENTER - OUTPATIENT CLINIC", "Electronic Prescription", "",
             f"Invoice No: {invoice}" if invoice else "Clinic: Internal Medicine",
             f"Date: {d:%d/%m/%Y}",
             "Patient: Demo Customer        MRN: 552190", "Physician: Dr. Sample Doctor", "",
             "Medication                     Qty    Instructions"]
    lines += [f"{i + 1}. {name:<28} 1      as directed" for i, name in enumerate(drugs)]
    return lines + ["", "This prescription is computer generated."]


def _image(lines):
    img = Image.new("RGB", (1400, 120 + 52 * len(lines)), "white")
    draw, font = ImageDraw.Draw(img), _font(32)
    for i, line in enumerate(lines):
        draw.text((60, 60 + 52 * i), line, fill="black", font=font)
    return img


def make_prescription_png(drugs=("Augmentin 1g",), days_ago=0, invoice="INV-2026-004512") -> bytes:
    buf = io.BytesIO()
    _image(_lines(drugs, days_ago, invoice)).save(buf, "PNG")
    return buf.getvalue()


def make_prescription_pdf(drugs=("Augmentin 1g",), days_ago=0, invoice="INV-2026-004512",
                          scanned=False) -> bytes:
    """A computerised PDF with a real text layer (like hospital systems export), or with
    scanned=True an image-only PDF (like a scanner app), which has to be OCR'd."""
    lines = _lines(drugs, days_ago, invoice)
    if scanned:
        buf = io.BytesIO()
        _image(lines).save(buf, "PDF", resolution=150)
        return buf.getvalue()
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    content = ("BT /F1 12 Tf 50 790 Td 18 TL " +
               " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET").encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--drug", action="append", help="medicine line (repeatable)")
    ap.add_argument("--days-ago", type=int, default=0)
    ap.add_argument("--invoice", default="INV-2026-004512")
    ap.add_argument("--no-invoice", action="store_true")
    ap.add_argument("--scanned", action="store_true", help="PDF only: image-only (needs OCR)")
    a = ap.parse_args()
    args = (a.drug or ["Augmentin 1g"], a.days_ago, None if a.no_invoice else a.invoice)
    Path(a.out).write_bytes(make_prescription_pdf(*args, scanned=a.scanned)
                            if a.out.lower().endswith(".pdf") else make_prescription_png(*args))
    print(f"wrote {a.out}")
