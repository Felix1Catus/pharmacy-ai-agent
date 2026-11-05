#!/usr/bin/env python3
"""Live terminal view of the distillation run: progress to the target, accepted examples by intent,
teacher calls, and the latest log lines. Reads state.db read-only. Ctrl-C to quit."""
import os
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = Path(os.environ.get("DISTILL_LOG", HERE / "distill.log"))
TARGET = int(os.environ.get("TARGET_VALID", "5000"))
G, Y, D, B, R = "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
NAMES = {"symptom_need": "Symptom requests", "store_info": "Store info", "order_status": "Order status",
         "medical_advice": "Medical advice", "fuzzy_product": "Misspelt products", "unclear": "Unclear",
         "restricted": "Restricted drugs", "rude": "Rude / complaints", "out_of_scope": "Out of scope",
         "order_first": "First orders", "stock_price": "Stock / price", "reserve_first": "Reserve requests",
         "injection": "Manipulation"}


def bar(n, total, width):
    fill = int(width * min(n, total) / total) if total else 0
    return f"{G}{'█' * fill}{D}{'░' * (width - fill)}{R}"


def frame(c, start):
    k = dict(c.execute("SELECT status, COUNT(*) FROM messages GROUP BY status").fetchall())
    valid = k.get("valid", 0)
    cats = c.execute("SELECT category, COUNT(*) FROM messages WHERE status='valid' GROUP BY category ORDER BY 2 DESC").fetchall()
    ok, failed = c.execute("SELECT SUM(ok), SUM(1 - ok) FROM calls").fetchone()
    last_hour = c.execute("SELECT COUNT(*) FROM messages WHERE status='valid' AND created > ?", (time.time() - 3600,)).fetchone()[0]
    rate = valid - start[1]
    mins = (time.time() - start[0]) / 60
    eta = f"~{(TARGET - valid) / (rate / mins):.0f} min at this pace" if rate > 0 and mins > 1 else "measuring pace…"
    try:
        state = os.popen("systemctl is-active pharmacy-distill").read().strip()
    except Exception:  # noqa: BLE001
        state = "?"
    lines = [f"{B}Distillation run{R}   {D}{time.strftime('%H:%M:%S')} UTC · service {state} · Ctrl-C to quit{R}", "",
             f"  {bar(valid, TARGET, 40)}  {B}{valid:,}{R} / {TARGET:,} accepted  ({100 * valid / TARGET:.0f}%)",
             f"  {D}{eta} · {last_hour} new messages this hour (all stages){R}", "",
             f"  waiting to label {Y}{k.get('model', 0)}{R} · new {k.get('new', 0)} · flagged for review "
             f"{Y}{k.get('review', 0)}{R} · never reach the model {k.get('handled', 0):,} · set aside {k.get('label_failed', 0)}",
             f"  teacher calls: {ok or 0} ok, {failed or 0} failed", "", f"{B}Accepted by intent{R}"]
    top = max((n for _, n in cats), default=1)
    for cat, n in cats:
        lines.append(f"  {NAMES.get(cat, cat):20s} {bar(n, top, 28)} {n:>5}")
    lines += ["", f"{B}Latest{R}"]
    try:
        tail = LOG.read_text(encoding="utf-8").splitlines()[-8:]
    except OSError:
        tail = []
    lines += [f"  {D}{l[:150]}{R}" for l in tail]
    return "\n".join(lines)


def main():
    c = sqlite3.connect(f"file:{HERE / 'state.db'}?mode=ro", uri=True, timeout=10)
    start = (time.time(), c.execute("SELECT COUNT(*) FROM messages WHERE status='valid'").fetchone()[0])
    while True:
        try:
            print("\033[2J\033[H" + frame(c, start), flush=True)
            time.sleep(3)
        except KeyboardInterrupt:
            break
        except sqlite3.OperationalError:
            time.sleep(1)


if __name__ == "__main__":
    main()
