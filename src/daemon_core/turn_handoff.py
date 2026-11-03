"""Short-lived handoff between host prefetch and canonical voice composition."""

from __future__ import annotations

import json
import time
from pathlib import Path

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT


TURN_HANDOFF_MAX_AGE_SECONDS = 120
TURN_HOT_SOUVENIR_MAX_AGE_SECONDS = 30 * 60
MAX_TURN_HOT_SOUVENIRS = 8


def _turn_handoff_path(user_id: str) -> Path:
    # Keep the historical filename while both plugin generations may coexist.
    return (
        MEMORY_ROOT
        / "levels"
        / f"single_pass_trace_{safe_memory_user_id(user_id)}.json"
    )


def write_turn_handoff(
    user_id: str,
    message: str,
    trace_id: str,
    prefetch_started_at: float,
) -> None:
    """Correlate host prefetch with the delegated canonical voice turn."""

    path = _turn_handoff_path(user_id)
    data = {
        "ts": time.time(),
        "message": message,
        "trace_id": trace_id,
        "prefetch_started_at": prefetch_started_at,
    }
    with memory_file_lock(path):
        atomic_write_text(
            path,
            json.dumps(data, ensure_ascii=False),
        )


def read_turn_handoff(
    user_id: str,
    prompt_user_text: str,
) -> tuple[str, str, float | None] | None:
    data = _read_handoff_data(user_id, TURN_HANDOFF_MAX_AGE_SECONDS)
    if data is None:
        return None
    try:
        message = str(data.get("message", ""))
        if not message or message not in prompt_user_text:
            return None
        started_at = data.get("prefetch_started_at")
        return (
            message,
            str(data.get("trace_id", "")),
            float(started_at) if started_at is not None else None,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def write_turn_hot_souvenirs(
    user_id: str,
    trace_id: str,
    souvenir_names: tuple[str, ...] | list[str],
) -> bool:
    """Attach exact voice reads to the matching short-lived host handoff."""

    trace_id = str(trace_id).strip()
    if not trace_id:
        return False
    path = _turn_handoff_path(user_id)
    with memory_file_lock(path):
        data = _read_handoff_data(user_id, TURN_HANDOFF_MAX_AGE_SECONDS)
        if data is None or str(data.get("trace_id", "")) != trace_id:
            return False
        data["hot_capsules"] = list(_normalized_souvenir_names(souvenir_names))
        data["hot_capsules_updated_at"] = time.time()
        atomic_write_text(path, json.dumps(data, ensure_ascii=False))
    return True


def read_turn_hot_souvenirs(
    user_id: str,
    trace_id: str,
) -> tuple[str, ...]:
    """Read voice-loaded souvenirs only for the exact completed host turn."""

    trace_id = str(trace_id).strip()
    if not trace_id:
        return ()
    data = _read_handoff_data(user_id, TURN_HOT_SOUVENIR_MAX_AGE_SECONDS)
    if data is None or str(data.get("trace_id", "")) != trace_id:
        return ()
    return _normalized_souvenir_names(data.get("hot_capsules", ()))


def _read_handoff_data(user_id: str, max_age_seconds: int) -> dict | None:
    try:
        data = json.loads(
            _turn_handoff_path(user_id).read_text(encoding="utf-8")
        )
        if not isinstance(data, dict):
            return None
        if time.time() - float(data.get("ts", 0)) > max_age_seconds:
            return None
        return data
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _normalized_souvenir_names(values) -> tuple[str, ...]:
    names = []
    for value in values or ():
        name = str(value).strip().replace("\\", "/").removeprefix("capsules/")
        if name.endswith(".md"):
            name = name[:-3]
        candidate = Path(name)
        if (
            not name
            or len(name) > 240
            or candidate.is_absolute()
            or ".." in candidate.parts
        ):
            continue
        if name not in names:
            names.append(name)
        if len(names) >= MAX_TURN_HOT_SOUVENIRS:
            break
    return tuple(names)


__all__ = [
    "MAX_TURN_HOT_SOUVENIRS",
    "TURN_HANDOFF_MAX_AGE_SECONDS",
    "TURN_HOT_SOUVENIR_MAX_AGE_SECONDS",
    "read_turn_handoff",
    "read_turn_hot_souvenirs",
    "write_turn_handoff",
    "write_turn_hot_souvenirs",
]
