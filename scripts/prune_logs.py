#!/usr/bin/env python3
"""Delete chat data older than the retention period (PDPL: keep personal data no longer than
needed). Covers everything the assistant writes outside Odoo:

  results/audit.jsonl, handovers.jsonl, whatsapp_outbox.jsonl   line by line, by timestamp
  results/webchat_sessions/, whatsapp_sessions/                 saved chats idle for that long
  results/whatsapp_media/                                       spoken replies (dry run), by age

It also tightens the permissions of those files (0600 / 0700) in case an older version created
them world-readable.

  python scripts/prune_logs.py              # LOG_RETENTION_DAYS from .env, default 90
  python scripts/prune_logs.py --days 30 --dry-run
Runs daily from deploy/systemd/pharmacy-prune-logs.timer. Odoo records (orders, prescriptions,
handovers) follow the pharmacy's own record-keeping rules and are not touched here.
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

RESULTS = ROOT / "results"
LOGS = [RESULTS / "audit.jsonl", RESULTS / "handovers.jsonl", RESULTS / "whatsapp_outbox.jsonl"]
# whole files, deleted when not modified for the retention period
FILE_DIRS = [RESULTS / "webchat_sessions", RESULTS / "whatsapp_sessions", RESULTS / "whatsapp_media"]


def _ts(value) -> datetime:
    """ISO text (audit, handovers) or epoch seconds (WhatsApp outbox)."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


def prune_files(folder: Path, cutoff: datetime, dry: bool) -> tuple[int, int]:
    if not folder.is_dir():
        return 0, 0
    _private(folder)
    kept = dropped = 0
    for f in folder.iterdir():
        if not f.is_file():
            continue
        if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
            dropped += 1
            if not dry:
                f.unlink(missing_ok=True)
        else:
            kept += 1
            _private(f)
    return kept, dropped


def prune(path: Path, cutoff: datetime, dry: bool) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    kept, dropped = [], 0
    for line in path.open(encoding="utf-8"):
        try:
            ts = _ts(json.loads(line)["ts"])
        except (ValueError, KeyError, TypeError, OSError, OverflowError):
            kept.append(line)                    # unreadable lines are kept for a human to check
            continue
        if ts < cutoff:
            dropped += 1
        else:
            kept.append(line)
    if dropped and not dry:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(kept))
        tmp.replace(path)
    _private(path)
    return len(kept), dropped


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.environ.get("LOG_RETENTION_DAYS", "90")))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    verb = "would drop" if a.dry_run else "dropped"
    for p in LOGS:
        kept, dropped = prune(p, cutoff, a.dry_run)
        print(f"{p.name}: {verb} {dropped}, kept {kept} (older than {a.days} days)")
    for d in FILE_DIRS:
        kept, dropped = prune_files(d, cutoff, a.dry_run)
        print(f"{d.name}/: {verb} {dropped} file(s), kept {kept} (idle for {a.days} days)")


if __name__ == "__main__":
    main()
