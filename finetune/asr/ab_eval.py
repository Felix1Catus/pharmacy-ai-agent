# -*- coding: utf-8 -*-
"""A/B: base whisper-large-v3 vs the fine-tuned ct2 model, on a HELD-OUT Gulf test set voiced by
NAMAA (phrasings not used in training). Reports WER + drug-name recall. Both models get the same
Gulf prompt + catalogue hotwords, decoded via ffmpeg (voice.decode)."""
import sys

import requests

sys.path[:0] = ["/home/pharmacy-ai-agent/adapter", "/home/pharmacy-ai-agent/agent"]
from faster_whisper import WhisperModel
from fuzzy_search import normalize, sound_key
from rapidfuzz.distance import Levenshtein
from voice import GULF_PROMPT, catalogue_hotwords, decode

NAMAA = "http://127.0.0.1:8095"
DRUGS_42 = ["بنادول", "اوجمنتين", "سنتروم", "اسبرين", "بروفين", "زاناكس", "كونسيرتا", "جافيسكون"]
# held-out utterances: NEW templates/phrasings (not in training)
TESTS = [("ممكن اطلب {}".format(d), d) for d in DRUGS_42] + \
        [("ابي اشتري {}".format(d), d) for d in DRUGS_42[:5]] + \
        [(d, d) for d in DRUGS_42[:5]] + \
        [(t, None) for t in [
            "هلا بغيت اسأل عن دواء", "فيه عندكم شي للبرد", "ابي اعرف سعر الدوا",
            "وصلوني الطلب امس", "ابي اكلم صيدلي", "متى بيجي الطلب حقي", "ودي اعرف الفروع",
            "عندكم توصيل لجدة اليوم", "ابي اغير العنوان", "كم يطلع علي التوصيل",
            "ابغى اطلب كم دوا", "عطوني خصم لو سمحتو", "ابي ارجع الطلب", "فيه صيدلي يرد علي",
            "دواني خلص ابي اطلب", "وش احسن شي للصداع"]]

hot = catalogue_hotwords()
base = WhisperModel("large-v3", device="cuda", device_index=1, compute_type="float16")
ft = WhisperModel("/opt/asr-ft/ct2", device="cuda", device_index=1, compute_type="float16")


def tx(m, arr):
    segs, _ = m.transcribe(arr, language="ar", beam_size=5, vad_filter=False,
                           initial_prompt=GULF_PROMPT, hotwords=hot, no_repeat_ngram_size=3,
                           condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs).strip()


def wer(ref, hyp):
    r, h = normalize(ref).split(), normalize(hyp).split()
    return Levenshtein.distance(r, h) / max(1, len(r))


def hit(drug, hyp):
    k = sound_key(drug)
    return any(Levenshtein.normalized_similarity(k, sound_key(w)) >= 0.75 for w in hyp.split() if len(w) >= 3)


bw = fw = 0.0
bh = fh = dn = 0
ex = []
for text, drug in TESTS:
    wav = requests.post(f"{NAMAA}/synth", json={"text": text, "lang": "ar"}, timeout=120).content
    arr, _ = decode(wav, "x.wav")
    hb, hf = tx(base, arr), tx(ft, arr)
    bw += wer(text, hb); fw += wer(text, hf)
    if drug:
        dn += 1; bh += hit(drug, hb); fh += hit(drug, hf)
    ex.append((text, hb, hf))

n = len(TESTS)
print(f"\n=== A/B over {n} held-out Gulf clips (NAMAA-voiced) ===")
print(f"WER   base {100*bw/n:5.1f}%   fine-tuned {100*fw/n:5.1f}%")
print(f"drug recall ({dn})  base {bh}/{dn}   fine-tuned {fh}/{dn}")
print("\n--- examples (text | base | fine-tuned) ---")
for text, hb, hf in ex[:14]:
    print(f"{text[:28]!r:30}\n   base: {hb[:40]!r}\n   ft  : {hf[:40]!r}")
