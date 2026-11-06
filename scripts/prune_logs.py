#!/usr/bin/env python3
"""Delete chat log entries older than the retention period (PDPL: keep personal data no longer
than needed). The audit log holds customers' messages - health questions included.

  python scripts/prune_logs.py              # LOG_RETENTION_DAYS from .env, default 90
  python scripts/prune_logs.py --days 30 --dry-run
Run it daily (cron / systemd timer). Odoo records (orders, prescriptions, handovers) follow the
pharmacy's own record-keeping rules and are not touched here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "odoo"))
from odoo_client import load_env  # noqa: E402

LOGS = [ROOT / "results" / "audit.jsonl", ROOT / "results" / "handovers.jsonl"]


def prune(path: Path, cutoff: datetime, dry: bool) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    kept, dropped = [], 0
    for line in path.open(encoding="utf-8"):
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"])
        except (ValueError, KeyError, TypeError):
            kept.append(line)                    # unreadable lines are kept for a human to check
            continue
        if ts < cutoff:
            dropped += 1
        else:
            kept.append(line)
    if dropped and not dry:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        tmp.replace(path)
    return len(kept), dropped


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.environ.get("LOG_RETENTION_DAYS", "90")))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    for p in LOGS:
        kept, dropped = prune(p, cutoff, a.dry_run)
        print(f"{p.name}: {'would drop' if a.dry_run else 'dropped'} {dropped}, kept {kept} (older than {a.days} days)")


if __name__ == "__main__":
    main()
