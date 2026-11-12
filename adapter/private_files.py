"""Files that hold customers' messages or health data (audit log, saved chats, outboxes).

They are created readable by the service user only (0600, directories 0700), and written
through one place so retention (scripts/prune_logs.py) and permissions stay consistent.
On Windows the mode bits are ignored by the OS; production runs on Linux.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("pharmacy.files")
_lock = threading.Lock()


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line; the file is private from its first byte."""
    private_dir(path.parent)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _lock:
        new = not path.exists()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
        if new:
            _restrict(path)


def write_private(path: Path, text: str) -> None:
    """Replace a file atomically (tmp + rename), private."""
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    _restrict(tmp)
    tmp.replace(path)


def write_private_bytes(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    _restrict(path)
