# -*- coding: utf-8 -*-
"""Resume the NAMAA synthetic set across BOTH GPUs: two NAMAA instances (:8095 on GPU0, :8096 on
GPU1), 2 concurrent workers. Skips clips already synthesized; rebuilds the manifest at the end.
Utterance list is identical to gen_synth.py (same seed) so u{i}.wav indices align."""
import json
import os
import random
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path[:0] = ["/home/pharmacy-ai-agent/adapter", "/home/pharmacy-ai-agent/odoo"]
from gateway import Gateway

ENDPOINTS = os.environ.get("NAMAA_ENDPOINTS",
                           "http://127.0.0.1:8095,http://127.0.0.1:8096").split(",")
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
for nm in names:
    for t in DRUG_TEMPLATES:
        utterances.append(t.format(n=nm))
for d in DIALECT:
    for _ in range(14):
        utterances.append(d)
random.shuffle(utterances)

remaining = [(i, t) for i, t in enumerate(utterances) if not os.path.exists(f"{OUT}/u{i:05d}.wav")]
print(f"total {len(utterances)}, remaining {len(remaining)}, endpoints {ENDPOINTS}", flush=True)
lock, done = threading.Lock(), [0]


def to16k(b, p):
    return subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "-",
                           "-ac", "1", "-ar", "16000", p], input=b, capture_output=True).returncode == 0


def work(arg):
    i, text = arg
    ep = ENDPOINTS[i % len(ENDPOINTS)]
    try:
        r = requests.post(f"{ep}/synth", json={"text": text, "lang": "ar"}, timeout=180)
        if r.status_code == 200 and r.content and to16k(r.content, f"{OUT}/u{i:05d}.wav"):
            with lock:
                done[0] += 1
                if done[0] % 100 == 0:
                    print(f"{done[0]}/{len(remaining)}", flush=True)
    except Exception:
        pass


with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
    list(ex.map(work, remaining))

with open(f"{OUT}/manifest.jsonl", "w", encoding="utf-8") as man:
    n = 0
    for i, text in enumerate(utterances):
        if os.path.exists(f"{OUT}/u{i:05d}.wav"):
            man.write(json.dumps({"audio": f"{OUT}/u{i:05d}.wav", "text": text}, ensure_ascii=False) + "\n")
            n += 1
print(f"DONE: {n} clips -> {OUT}/manifest.jsonl", flush=True)
