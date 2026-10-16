# -*- coding: utf-8 -*-
"""Build (1) the full SFDA drugs database file and (2) an Odoo product import file.

Reads the scrape cache (data_sources/sfda/cache/, from scrape_sfda.py) so it also works on a
partial scrape. Mapping rules:
  المراقبة == "مقيد"                  -> is_scheduled = True   (Odoo "Scheduled" checkbox)
  طريقة الصرف starts with "يحتاج"     -> sfda_requires_prescription = True
  ("لا يحتاج ..." -> False)
All other SFDA fields go to the sfda_* fields of the pharmacy_agent module (Pharmacy tab).

Outputs (data_sources/sfda/data/):
  sfda_drugs_database.csv / .xlsx   every SFDA field, Arabic headers  (the reference database)
  sfda_products_odoo_import.csv / .xlsx   Odoo import (Inventory > Products > Import records)
The import uses external IDs (column "id" = sfda.drug_<reg no>), so re-importing an updated
file UPDATES existing products instead of duplicating them.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scrape_sfda import parse_details, parse_list  # noqa: E402

CACHE, DATA = HERE / "cache", HERE / "data"

# Arabic SFDA label -> our key (order = column order of the database file)
FIELDS = [
    ("رقم التسجيل", "reg_no"), ("الاسم التجاري", "trade_name"), ("الاسم العلمي", "scientific_name"),
    ("التركيز", "strength"), ("وحدة التركيز", "strength_unit"), ("الشكل الصيدلاني", "dosage_form"),
    ("طريقة الإستعمال", "route"), ("الحجم", "size"), ("وحدة الحجم", "size_unit"),
    ("نوع العبوة", "pack_type"), ("حجم العبوة", "pack_size"), ("طريقة الصرف", "dispensing"),
    ("المراقبة", "control"), ("مكان التوزيع", "distribution"), ("مدة الصلاحية", "shelf_life_months"),
    ("ظروف التخزين", "storage"), ("الشركات المصنعة", "manufacturers"),
    ("الشركة المسوقة", "marketing_company"), ("بلد الشركة المسوقة", "marketing_company_country"),
    ("الوكلاء", "agents"), ("رمز التصنيف الكيميائي العلاجي التشريحي 1", "atc_code_1"),
    ("رمز التصنيف الكيميائي العلاجي التشريحي 2", "atc_code_2"),
    ("رمز التركيبة الدوائية", "formulation_code"), ("حالة الترخيص", "license_status"),
    ("حالة التسويق", "marketing_status"), ("السعر", "price"),
]


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return 0.0


def load_records():
    recs, seen = [], set()
    pages = sorted((CACHE / "list").glob("page_*.html"), key=lambda p: int(p.stem.split("_")[1]))
    for pf in pages:
        for row in parse_list(pf.read_text(encoding="utf-8"), int(pf.stem.split("_")[1])):
            key = row["details_url"].split("details_data?")[1].split("&page=")[0]
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
            dfile = CACHE / "details" / f"{safe}.html"
            if key in seen or not dfile.exists():
                continue
            seen.add(key)
            d = parse_details(dfile.read_text(encoding="utf-8"))
            rec = {k: d.get(ar, "") for ar, k in FIELDS}
            rec["reg_no"] = rec["reg_no"] or row["reg_no"]
            rec["price"] = rec["price"] or row["price"]
            rec["list_page"] = row["page"]; rec["details_url"] = row["details_url"]
            recs.append(rec)
    return recs


def write_table(path_base: Path, header: list[str], rows: list[list]):
    with open(f"{path_base}.csv", "w", newline="", encoding="utf-8-sig") as fh:   # Excel-friendly
        w = csv.writer(fh); w.writerow(header); w.writerows(rows)
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.sheet_view.rightToLeft = False
        ws.append(header)
        for r in rows:
            ws.append(r)
        wb.save(f"{path_base}.xlsx")
    except ImportError:
        print("   (openpyxl not installed: skipped .xlsx)")


def main():
    recs = load_records()
    DATA.mkdir(parents=True, exist_ok=True)

    # 1) reference database: every SFDA field, Arabic headers + a few derived columns
    db_header = [ar for ar, _ in FIELDS] + ["مقيد؟", "يحتاج وصفة؟", "صفحة القائمة", "رابط التفاصيل"]
    db_rows = [[r[k] for _, k in FIELDS] +
               ["نعم" if r["control"] == "مقيد" else "لا",
                "نعم" if r["dispensing"].startswith("يحتاج") else "لا",
                r["list_page"], r["details_url"]] for r in recs]
    write_table(DATA / "sfda_drugs_database", db_header, db_rows)

    # 2) Odoo import (technical field names -> auto-mapped by Odoo's import screen)
    imp_header = ["id", "name", "default_code", "type", "is_storable", "sale_ok", "list_price",
                  "is_scheduled", "schedule_class",
                  "sfda_reg_no", "sfda_scientific_name", "sfda_strength", "sfda_dosage_form",
                  "sfda_route", "sfda_pack", "sfda_dispensing", "sfda_requires_prescription",
                  "sfda_control", "sfda_distribution", "sfda_atc_code", "sfda_shelf_life",
                  "sfda_storage", "sfda_manufacturer", "sfda_marketing_company", "sfda_agent",
                  "sfda_license_status", "sfda_marketing_status", "sfda_public_price"]
    imp_rows, used = [], set()
    for r in recs:
        ext = "sfda.drug_" + re.sub(r"[^A-Za-z0-9_]", "_", r["reg_no"])
        if ext in used:          # SFDA lists a few drugs twice (same reg no/pack/price): keep one
            continue
        used.add(ext)
        size = " ".join(x for x in [r["size"], r["size_unit"]] if x)
        pack = " ".join(x for x in [r["pack_size"], r["pack_type"], f"({size})" if size else ""] if x)
        # some SFDA entries have no trade name (e.g. contrast agents): Odoo requires a name, so
        # fall back to "scientific name strength unit form", then to the registration number
        name = r["trade_name"] or " ".join(x for x in [
            r["scientific_name"], r["strength"], r["strength_unit"], r["dosage_form"]] if x) \
            or f"SFDA {r['reg_no']}"
        imp_rows.append([
            ext, name, r["reg_no"], "consu", "True", "True", num(r["price"]),
            "True" if r["control"] == "مقيد" else "False", r["control"],
            r["reg_no"], r["scientific_name"],
            " ".join(x for x in [r["strength"], r["strength_unit"]] if x), r["dosage_form"],
            r["route"], pack, r["dispensing"],
            "True" if r["dispensing"].startswith("يحتاج") else "False",
            r["control"], r["distribution"], r["atc_code_1"], r["shelf_life_months"], r["storage"],
            r["manufacturers"], r["marketing_company"], r["agents"], r["license_status"],
            r["marketing_status"], num(r["price"])])
    write_table(DATA / "sfda_products_odoo_import", imp_header, imp_rows)
    # Odoo also gets a BOM-free CSV (safest for its CSV importer)
    with open(DATA / "sfda_products_odoo_import_nobom.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(imp_header); w.writerows(imp_rows)

    n = len(recs)
    restricted = sum(r["control"] == "مقيد" for r in recs)
    rx = sum(r["dispensing"].startswith("يحتاج") for r in recs)
    print(f"{n} drugs | مقيد (-> Scheduled): {restricted} | prescription: {rx} | "
          f"control values: {sorted({r['control'] for r in recs})}")


if __name__ == "__main__":
    main()
