"""Resolve an exact OpenClaw delivery target from its native session store."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


def configured_agent_database() -> Path:
    configured = os.getenv("DAEMON_CORE_OPENCLAW_AGENT_DB", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    )


def resolve_delivery_origin(
    session_id: str,
    *,
    database_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return host-owned channel coordinates, or an empty mapping.

    The session database is an OpenClaw projection. It supplies routing only;
    no message content is read or copied into daemon memory.
    """
    normalized_session = str(session_id or "").strip()
    path = Path(database_path).expanduser() if database_path else configured_agent_database()
    if not normalized_session or not path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT
                    windows.session_key,
                    conversations.channel,
                    conversations.account_id,
                    conversations.delivery_target,
                    conversations.peer_id,
                    conversations.thread_id
                FROM session_windows AS windows
                JOIN conversations
                  ON conversations.conversation_id = windows.primary_conversation_id
                WHERE windows.session_id = ?
                LIMIT 1
                """,
                (normalized_session,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {}
    if row is None:
        return {}
    channel = str(row["channel"] or "").strip()
    destination = str(row["delivery_target"] or row["peer_id"] or "").strip()
    if not channel or not destination:
        return {}
    origin: dict[str, Any] = {
        "channel": channel,
        "to": destination,
        "account_id": str(row["account_id"] or "").strip(),
        "session_key": str(row["session_key"] or "").strip(),
    }
    thread_id = str(row["thread_id"] or "").strip()
    if thread_id:
        origin["thread_id"] = thread_id
    # This reference comes from the dedicated bridge process, not dialogue.
    # Retain an invalid explicit reference so scheduling can fail closed rather
    # than silently dispatching through another host's default profile.
    host_ref = os.getenv("DAEMON_CORE_HOST_REF", "").strip()
    if host_ref:
        origin["host_ref"] = host_ref
    return {key: value for key, value in origin.items() if value}


__all__ = ["configured_agent_database", "resolve_delivery_origin"]
