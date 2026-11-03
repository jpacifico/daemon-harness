"""Structured, append-only latency events shared by the Hermes plugins."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def latency_log_path() -> Path:
    configured = os.getenv("DAEMON_CORE_LATENCY_LOG", "").strip()
    if configured:
        return Path(configured).expanduser()
    src = Path(os.getenv("DAEMON_CORE_SRC", ".")).expanduser()
    return src.parent / "exports" / "latency_hermes.jsonl"


def append_latency_event(event: str, trace_id: str, **fields: Any) -> bool:
    """Append one compact JSON event with a single atomic OS write."""
    try:
        payload = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "epoch": round(time.time(), 6),
            "event": event,
            "trace_id": trace_id,
            **fields,
        }
        path = latency_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False
