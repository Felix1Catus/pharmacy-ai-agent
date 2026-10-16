# -*- coding: utf-8 -*-
"""Scrape the SFDA registered-drugs list (https://www.sfda.gov.sa/ar/drugs-list) + each drug's
details page (التفاصيل), politely and resumably.

- list pages:   /ar/drugs-list?page=N            (20 drugs/page; the pager is 0-based)
- details page: /ar/details_data?nid=...&id=<registration no>  (label/value table incl. المراقبة)

Politeness: ~1 request/second (+ jitter), retries with backoff on errors/429/5xx, identifying
User-Agent. robots.txt (checked 2026-10-16) does not disallow these paths.
Resumable: every fetched page is cached under cache/ (gitignored); re-running skips them.

Usage: python data_sources/sfda/scrape_sfda.py [first_page] [last_page]   (default 0 100)
Output: data/sfda_list.jsonl, data/sfda_details.jsonl
"""
from __future__ import annotations

import html
import json
import random
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://www.sfda.gov.sa"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
DATA = HERE / "data"
UA = "Mozilla/5.0 (X11; Linux x86_64) pharmacy-catalogue-research (polite)"
import os  # noqa: E402
import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

DELAY = float(os.environ.get("SFDA_DELAY", "0.5"))     # per-worker pause between requests
WORKERS = int(os.environ.get("SFDA_WORKERS", "4"))     # parallel detail-page fetchers

_local = threading.local()


def _session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers.update({"User-Agent": UA, "Accept-Language": "ar"})
    return _local.s


def fetch(url: str, cache_file: Path) -> str:
    if cache_file.exists() and cache_file.stat().st_size > 1000:
        return cache_file.read_text(encoding="utf-8")
    for attempt in range(5):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = _session().get(url, timeout=30)
            if r.status_code == 200:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(r.text, encoding="utf-8")
                return r.text
            wait = 30 * (attempt + 1) if r.status_code in (429, 503) else 5 * (attempt + 1)
            print(f"   HTTP {r.status_code} on {url} -> retry in {wait}s", flush=True)
        except requests.RequestException as e:
            wait = 5 * (attempt + 1)
            print(f"   {type(e).__name__} on {url} -> retry in {wait}s", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"giving up on {url}")


def clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ; ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_list(page_html: str, page: int) -> list[dict]:
    rows = []
    for tr in re.findall(r'<tr class="(?:odd|even)">(.*?)</tr>', page_html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        m = re.search(r'href="(/ar/details_data\?[^"]+)"', tr)
        if len(tds) < 5 or not m:
            continue
        href = html.unescape(m.group(1))
        reg = re.search(r"[?&]id=([^&]+)", href).group(1)
        rows.append({"page": page, "reg_no": reg, "scientific_name": clean(tds[0]),
                     "trade_name": clean(tds[1]), "strength": clean(tds[2]),
                     "dosage_form": clean(tds[3]), "price": clean(tds[4]),
                     "details_url": BASE + href})
    return rows


def parse_details(page_html: str) -> dict:
    m = re.search(r'<tbody class="attachmentstr">(.*?)</tbody>', page_html, re.S)
    if not m:
        return {}
    return {clean(k): clean(v) for k, v in
            re.findall(r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", m.group(1), re.S)}


def main():
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    last = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    DATA.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for p in range(first, last + 1):
        rows = parse_list(fetch(f"{BASE}/ar/drugs-list?page={p}", CACHE / "list" / f"page_{p}.html"), p)
        all_rows += rows
        print(f"[list] page {p}: {len(rows)} drugs (total {len(all_rows)})", flush=True)
    with (DATA / "sfda_list.jsonl").open("w", encoding="utf-8") as fh:
        for r in all_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    jobs, seen = [], set()
    for r in all_rows:
        key = r["details_url"].split("details_data?")[1].split("&page=")[0]
        if key not in seen:
            seen.add(key); jobs.append((key, r))

    done, lock = [0], threading.Lock()
    restricted = [0]

    def work(job):
        key, r = job
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
        d = parse_details(fetch(r["details_url"], CACHE / "details" / f"{safe}.html"))
        d["_reg_no_from_link"] = r["reg_no"]; d["_details_url"] = r["details_url"]
        with lock:
            done[0] += 1; restricted[0] += d.get("المراقبة") == "مقيد"
            if done[0] % 25 == 0 or done[0] == len(jobs):
                print(f"[details] {done[0]}/{len(jobs)}  (مقيد so far: {restricted[0]})", flush=True)
        return d

    print(f"[details] {len(jobs)} detail pages, {WORKERS} workers, {DELAY}s pause", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        details = list(ex.map(work, jobs))   # keeps original order
    with (DATA / "sfda_details.jsonl").open("w", encoding="utf-8") as fh:
        for d in details:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"DONE: {len(all_rows)} list rows, {len(details)} detail pages", flush=True)


if __name__ == "__main__":
    main()
