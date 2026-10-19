# -*- coding: utf-8 -*-
"""Generate a sample COMPUTERISED prescription image (dummy data) to test the prescription
photo flow in the chat page and in adapter/test_prescription.py.

  python evals/datasets/make_prescription.py out.png                     # today, Augmentin 1g
  python evals/datasets/make_prescription.py out.png --days-ago 9        # too old -> rejected
  python evals/datasets/make_prescription.py out.png --drug "Ventolin Inhaler" --invoice INV-2026-00777
  python evals/datasets/make_prescription.py out.png --no-invoice
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


def make_prescription_png(drugs=("Augmentin 1g",), days_ago=0, invoice="INV-2026-004512") -> bytes:
    d = today_ksa() - timedelta(days=days_ago)
    lines = ["AL SHIFA MEDICAL CENTER - OUTPATIENT CLINIC", "Electronic Prescription", "",
             f"Invoice No: {invoice}" if invoice else "Clinic: Internal Medicine",
             f"Date: {d:%d/%m/%Y}",
             "Patient: Demo Customer        MRN: 552190", "Physician: Dr. Sample Doctor", "",
             "Medication                     Qty    Instructions"]
    lines += [f"{i + 1}. {name:<28} 1      as directed" for i, name in enumerate(drugs)]
    lines += ["", "This prescription is computer generated."]
    img = Image.new("RGB", (1400, 120 + 52 * len(lines)), "white")
    draw, font = ImageDraw.Draw(img), _font(32)
    for i, line in enumerate(lines):
        draw.text((60, 60 + 52 * i), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--drug", action="append", help="medicine line (repeatable)")
    ap.add_argument("--days-ago", type=int, default=0)
    ap.add_argument("--invoice", default="INV-2026-004512")
    ap.add_argument("--no-invoice", action="store_true")
    a = ap.parse_args()
    Path(a.out).write_bytes(make_prescription_png(
        a.drug or ["Augmentin 1g"], a.days_ago, None if a.no_invoice else a.invoice))
    print(f"wrote {a.out}")
