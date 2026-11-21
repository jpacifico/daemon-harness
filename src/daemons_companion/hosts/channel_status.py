"""Whether an OpenClaw daemon created by the app is connected to Telegram.

Read-only. The daemon's own gateway answers its health query, the same one the
app uses to decide that a new or updated daemon is ready; only status fields
are kept, never messages or credentials. Hermes Agent writes the same answer to
a state file, which the desktop app reads directly.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess

from daemons_companion.openclaw.host_install import checked_file, direct

# Telegram answers getMe with these when the bot token is revoked or wrong.
TOKEN_REJECTED = frozenset({401, 404})
QUERY_TIMEOUT_SECONDS = 60


def telegram_state(health: object, enabled: bool) -> str:
    """One state from OpenClaw's gateway health answer."""
    if not enabled:
        return "none"
    if not isinstance(health, dict) or health.get("ok") is not True:
        return "unknown"
    channel = (health.get("channels") or {}).get("telegram")
    if not isinstance(channel, dict):
        return "disconnected"
    probe = channel.get("probe") if isinstance(channel.get("probe"), dict) else {}
    if probe.get("status") in TOKEN_REJECTED:
        return "token-rejected"
    if channel.get("terminalDisconnect") is True:
        return "disconnected"
    lifecycle = channel.get("lifecycle")
    if channel.get("running") is True and lifecycle not in {"starting", "recovering", "stopped", "blocked"}:
        return "connected"
    if lifecycle in {"starting", "recovering"}:
        return "connecting"
    return "disconnected"


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def openclaw_telegram(scope) -> str:
    from daemons_companion.openclaw.gateway import host_environment, host_for

    host = host_for(scope)
    port = scope.receipt()["host"].get("gateway_port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("The OpenClaw gateway port is ambiguous.")
    config = json.loads(checked_file(host.profile / "openclaw.json", 2 * 1024 * 1024))
    enabled = config.get("channels", {}).get("telegram", {}).get("enabled") is True
    if not enabled:
        return "none"
    # A stopped gateway is the host check's answer, not a channel state.
    if not _listening(port):
        return "unknown"
    try:
        result = subprocess.run(
            [str(host.node), str(host.entry), "gateway", "health", "--json",
             "--port", str(port), "--timeout", "10000"],
            env=host_environment(scope),
            cwd=host.attempt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
        )
        health = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        health = None
    return telegram_state(health, enabled)


def dispatch(request: dict) -> dict:
    from daemons_companion.runtime.installation_scope import InstallationScope

    if set(request) != {"operation", "source_profile", "base"}:
        raise ValueError("Unsupported channel request.")
    base = direct(Path(request["base"]))
    profile = direct(Path(request["source_profile"]))
    if not profile.is_relative_to(base):
        raise ValueError("The daemon is not an owned private installation.")
    scope = InstallationScope(profile.parent)
    scope.validate_profile(profile)
    if scope.host_kind != "openclaw":
        raise ValueError("Hermes Agent reports its channels in its state file.")
    return {"telegram": openclaw_telegram(scope)}
