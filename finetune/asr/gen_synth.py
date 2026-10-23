# -*- coding: utf-8 -*-
"""Gulf-accent synthetic ASR data via NAMAA: the catalogue's Arabic drug names in carrying
phrases + a rich set of Gulf pharmacy order/dialect phrases -> 16 kHz wavs + manifest.jsonl.
Dialect-focused run. Uses the live NAMAA HTTP service (no training deps)."""
import json
import os
import random
import subprocess
import sys

import requests

sys.path[:0] = ["/home/pharmacy-ai-agent/adapter", "/home/pharmacy-ai-agent/odoo"]
from gateway import Gateway

NAMAA = os.environ.get("TTS_NAMAA_URL", "http://127.0.0.1:8095")
OUT = "/opt/asr-ft/data/synth"
os.makedirs(OUT, exist_ok=True)
random.seed(7)

DRUG_TEMPLATES = ["ابغى {n}", "عندكم {n}", "ودي {n} لو سمحت", "{n} كم سعره", "ابي علبتين {n}",
                  "دورلي على {n}", "{n} متوفر عندكم", "ابي اطلب {n}", "{n}"]

DIALECT = [
    "السلام عليكم", "هلا والله", "مرحبا ابي اطلب", "هلا كيف اقدر اطلب", "صباح الخير",
    "ابغى اطلب دواء", "ودي اطلب شي", "ابي استفسر عن دواء", "عندكم دواء للصداع", "ابي شي للزكام",
    "وش عندكم للكحة", "ابغى مسكن", "ودي فيتامينات", "ابي دوا للحراره", "عندكم شي للمغص",
    "ابي توصيل للبيت", "توصلون للرياض", "ابي استلم من الفرع", "وش مدة التوصيل", "التوصيل كم ياخذ",
    "ابي استلام اليوم", "عندكم توصيل سريع", "ابي يجيني اليوم", "ممكن توصيل بكره",
    "وين فروعكم", "عندكم فرع في جدة", "متى تسكرون", "كم ساعات العمل", "الفرع مفتوح الحين",
    "دوامكم لين متى", "عندكم فرع قريب مني",
    "كم السعر", "وش اسعاركم", "ابي علبتين", "ابي ثلاث علب", "بكم العلبة", "فيه خصم", "كم يطلع",
    "تمام اثبت الطلب", "ايوه ابي اطلب", "لا الغي الطلب", "خلاص كذا تمام", "ابي اعدل الطلب", "ايه صح",
    "عندي وصفة طبية", "ابي ارفع الوصفة", "الدوا يحتاج وصفة", "كيف ارسل الروشته", "عندي روشته من الدكتور",
    "كيف ادفع", "ابي ادفع اونلاين", "وين وصل طلبي", "متى يجيني الطلب", "ابي رقم الطلب", "كيف اتابع طلبي",
    "ممكن تساعدني", "عطني معلومات", "ابي اسأل سؤال", "مشكور", "يعطيك العافية", "تسلم",
    "وش رايك", "طيب", "اوكي تمام", "زين",
    "طلبي تاخر", "وصلني دوا غلط", "ابي استرجع", "عندي مشكله في الطلب",
    "رقمي يبدا بصفر خمسه", "اكتب رقمي بعدين", "عطني رقم عرض السعر",
]

gw = Gateway()
rows = gw.c.search_read("product.template", [], ["x_name_ar", "x_brand"], limit=6000)
names, seen = [], set()
for r in rows:
    for f in ("x_name_ar", "x_brand"):
        v = (r.get(f) or "").strip()
        if 3 <= len(v) <= 50 and any("؀" <= c <= "ۿ" for c in v) and v not in seen:
            seen.add(v)
            names.append(v)

utterances = []
for nm in names:                                      # every Arabic name, all phrasings
    for t in DRUG_TEMPLATES:
        utterances.append(t.format(n=nm))
for d in DIALECT:
    for _ in range(14):                               # many takes; NAMAA sampling varies prosody
        utterances.append(d)
random.shuffle(utterances)
print(f"utterances: {len(utterances)} (drug names {len(names)}, dialect {len(DIALECT)})", flush=True)


def to16k(wav_bytes, path):
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "-",
                        "-ac", "1", "-ar", "16000", path], input=wav_bytes, capture_output=True)
    return p.returncode == 0


with open(f"{OUT}/manifest.jsonl", "w", encoding="utf-8") as man:
    ok = 0
    for i, text in enumerate(utterances):
        try:
            r = requests.post(f"{NAMAA}/synth", json={"text": text, "lang": "ar"}, timeout=120)
            if r.status_code != 200 or not r.content:
                continue
            path = f"{OUT}/u{i:05d}.wav"
            if not to16k(r.content, path):
                continue
            man.write(json.dumps({"audio": path, "text": text}, ensure_ascii=False) + "\n")
            man.flush()
            ok += 1
            if ok % 100 == 0:
                print(f"{ok}/{len(utterances)}", flush=True)
        except Exception as e:
            print(f"skip {i}: {str(e)[:80]}", flush=True)
    print(f"DONE: {ok} clips -> {OUT}/manifest.jsonl", flush=True)
