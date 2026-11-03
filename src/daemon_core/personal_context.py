"""Explicit, host-neutral canary for personally grounded conversational context."""
import os


def personal_context_canary_enabled(user_id: str, platform: str | None) -> bool:
    if os.getenv("DAEMON_CORE_PERSONAL_CONTEXT", "").strip().casefold() != "canary":
        return False
    users = {v.strip() for v in os.getenv("DAEMON_CORE_PERSONAL_CONTEXT_USER_IDS", "").split(",") if v.strip()}
    platforms = {v.strip().casefold() for v in os.getenv("DAEMON_CORE_PERSONAL_CONTEXT_PLATFORMS", "").split(",") if v.strip()}
    return bool(user_id and "*" not in users and user_id in users and platform and platform.casefold() in platforms and platform.casefold() in {"telegram", "openclaw"})
