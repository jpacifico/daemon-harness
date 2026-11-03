"""Exact souvenir loaders and legacy handoff exports.

The full-memory-book response composer is retired. These loaders remain
shared by the canonical Atlas, bounded search and exact-memory library.
Candidate feature-flag readers are retired; the active handoff is unchanged.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from daemon_core.paths import MEMORY_ROOT
from daemon_core.turn_handoff import (
    TURN_HANDOFF_MAX_AGE_SECONDS as SINGLE_PASS_TRACE_MAX_AGE_SECONDS,  # noqa: F401
    read_turn_handoff as read_single_pass_trace,  # noqa: F401
    write_turn_handoff as write_single_pass_trace,  # noqa: F401
)


def load_active_memory_entries(
    memory_root: Path = MEMORY_ROOT,
    *,
    today: date | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Load exact active thematic souvenirs without rendering a single book."""

    return _load_memory_entries(
        memory_root,
        today=today,
        include_chronicles=False,
    )


def load_searchable_memory_entries(
    memory_root: Path = MEMORY_ROOT,
    *,
    today: date | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Load souvenirs eligible for bounded search, including past chronicles."""

    return _load_memory_entries(
        memory_root,
        today=today,
        include_chronicles=True,
    )


def _load_memory_entries(
    memory_root: Path,
    *,
    today: date | None,
    include_chronicles: bool,
) -> tuple[list[tuple[str, str]], list[str]]:
    from daemon_core.memory.library_layout import unique_note_locations, vault_root
    root_folders = vault_root(memory_root) is not None
    entries: list[tuple[str, str]] = []
    expired: list[str] = []
    reference_day = today or _active_memory_today()
    for item in unique_note_locations(memory_root):
        name, path = item.name, item.path
        if not root_folders and not name.startswith("theme/"):
            continue
        relative = Path(name)
        if not include_chronicles and any(
            cold_domain in relative.parts
            for cold_domain in ("chroniques", "scenes_episodiques")
        ):
            continue
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        if _souvenir_expired(content, reference_day):
            expired.append(name)
            continue
        entries.append((name, content))
    return entries, expired


def _active_memory_today() -> date:
    forced = os.getenv("DAEMON_CORE_EVAL_NOW", "").strip()
    if forced:
        try:
            return date.fromisoformat(forced[:10])
        except ValueError:
            pass
    return date.today()


def _souvenir_expired(content: str, reference_day: date) -> bool:
    match = re.search(r"(?m)^valid_until:\s*(\d{4}-\d{2}-\d{2})\s*$", content)
    if not match:
        return False
    try:
        return date.fromisoformat(match.group(1)) < reference_day
    except ValueError:
        return False
