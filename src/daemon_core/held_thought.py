"""Shared scene scope helpers retained after retiring Held Thought.

Canonical voice, Scene Model and Hermes still use these exact helpers.
The historical thought revision, storage and shadow engine is removed;
existing memory files are neither read nor deleted by this module.
"""

from __future__ import annotations

import os
from typing import Any

from daemon_core.memory.writer import safe_memory_user_id


SHARED_WORLD_STATE_ENV = "DAEMON_CORE_SHARED_WORLD_STATE"
SHARED_WORLD_SCENE_SCOPE = "world"
_ENABLED_VALUES = {"1", "true", "yes", "on", "active", "canary"}


def shared_world_state_enabled() -> bool:
    """Whether every host should consume the same per-user working scene."""

    return (
        os.environ.get(SHARED_WORLD_STATE_ENV, "").strip().lower()
        in _ENABLED_VALUES
    )


def scene_scope_from_origin(origin: dict[str, Any] | None = None, session_id: str = "") -> str:
    if shared_world_state_enabled():
        # The user id already owns the parent memory directory.  A stable leaf
        # deliberately removes the host/session from the daemon's world state.
        return SHARED_WORLD_SCENE_SCOPE
    origin = origin or {}
    platform = str(origin.get("platform", "")).strip()
    chat_id = str(origin.get("chat_id", "")).strip()
    thread_id = str(origin.get("thread_id", "")).strip()
    if platform and chat_id:
        raw = "__".join(part for part in (platform, chat_id, thread_id) if part)
    elif session_id.strip():
        raw = f"session__{session_id.strip()}"
    else:
        raw = "primary"
    return safe_memory_user_id(raw)[:160] or "primary"
