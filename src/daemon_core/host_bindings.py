"""Resolve explicit host origins from daemon-owned technical registrations.

An unbound historical origin retains its host adapter's legacy behavior. An
explicit reference never falls back to the process's default host. The registry
is written by the confirmed installation flow, never by a model or memory tool.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass

SCHEMA = "daemon.host-execution.v1"


def _direct(path: str, *, directory: bool = False) -> Path:
    value = Path(path)
    if not value.is_absolute() or value.resolve() != value or value.is_symlink():
        raise ValueError("Host binding path is not direct.")
    if directory and not value.is_dir():
        raise ValueError("Host binding directory is unavailable.")
    return value


def _same_location(first, second):
    # Windows runtime I/O uses extended paths; persisted host registrations keep
    # readable absolute paths. Compare only after both direct paths were checked.
    if os.name == "nt":
        import ntpath
        from daemon_core.platform_support import windows_extended_path as _windows_extended_path

        return ntpath.normcase(_windows_extended_path(str(first))) == ntpath.normcase(
            _windows_extended_path(str(second))
        )
    return first == second


def _bytes(path: Path, maximum: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError("Host binding file is invalid.")
    value = path.read_bytes()
    if len(value) > maximum:
        raise ValueError("Host binding file is too large.")
    return value


@dataclass(frozen=True)
class HostExecution:
    host_ref: str
    profile: Path
    argv: tuple[str, ...]
    registry: Path
    state: Path

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        env = {
            k: v
            for k, v in base.items()
            if not k.startswith(("OPENCLAW_", "NODE_OPTIONS", "NODE_PATH"))
        }
        env.update(
            OPENCLAW_STATE_DIR=str(self.profile),
            OPENCLAW_CONFIG_PATH=str(self.profile / "openclaw.json"),
            DAEMON_CORE_HOST_REF=self.host_ref,
            DAEMON_CORE_HOST_BINDINGS_DIR=str(self.registry),
            DAEMON_CORE_STATE_ROOT=str(self.state),
        )
        return env

    def relay_environment(self) -> dict[str, str]:
        return self.environment({})


def resolve_host_execution(
    origin: dict,
    *,
    host: str,
    user_id: str,
    memory: Path,
    environment: dict[str, str] | None = None,
) -> HostExecution | None:
    """Return a fully checked binding or raise, never discover a default host."""
    if "host_ref" not in origin:
        return None
    reference = origin["host_ref"]
    if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{32}", reference):
        raise ValueError("The originating host reference is invalid.")
    env = os.environ if environment is None else environment
    state = _direct(env.get("DAEMON_CORE_STATE_ROOT", ""), directory=True)
    registry = _direct(
        env.get("DAEMON_CORE_HOST_BINDINGS_DIR", str(state / "host-bindings")),
        directory=True,
    )
    if not _same_location(registry, state / "host-bindings"):
        raise ValueError("Host registry is outside the daemon state.")
    path = _direct(str(registry / (reference + ".json")))
    data = json.loads(_bytes(path, 16384))
    if set(data) != {
        "schema",
        "host_ref",
        "status",
        "host",
        "user_id",
        "memory",
        "profile",
        "executables",
    }:
        raise ValueError("Host registration fields are invalid.")
    if (
        data["schema"] != SCHEMA
        or data["host_ref"] != reference
        or data["status"] != "active"
        or data["host"] != host
        or data["user_id"] != user_id
    ):
        raise ValueError("The originating host is not active for this daemon.")
    if not _same_location(
        _direct(data["memory"], directory=True), _direct(str(memory), directory=True)
    ):
        raise ValueError("The originating host belongs to another daemon memory.")
    profile = _direct(data["profile"], directory=True)
    config = _direct(str(profile / "openclaw.json"))
    _bytes(config, 2 * 1024 * 1024)
    items = data["executables"]
    if host != "openclaw" or not isinstance(items, list) or not 1 <= len(items) <= 2:
        raise ValueError("Unsupported registered host command.")
    argv = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("Invalid host executable binding.")
        executable = _direct(item["path"])
        if (
            hashlib.sha256(_bytes(executable, 256 * 1024 * 1024)).hexdigest()
            != item["sha256"]
        ):
            raise ValueError("The originating host executable changed.")
        argv.append(str(executable))
    return HostExecution(reference, profile, tuple(argv), registry, state)
