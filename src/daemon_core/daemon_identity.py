"""Explicit setup identities, kept apart from generated voice and memory."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import stat
import unicodedata

from daemon_core.onboarding_user import validate_bound_user
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock


IDENTITY_FILENAME = "daemon_identity.json"
DEFAULT_DAEMON_NAME = "Daemon"
VALID_GENDERS = frozenset({"feminine", "masculine"})


@dataclass(frozen=True)
class DaemonIdentity:
    name: str
    gender: str = ""
    configured: bool = False
    user: dict[str, str] | None = None


def validate_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Daemon name must be text.")
    name = value.strip()
    if not name or len(name) > 40 or any(
        not (unicodedata.category(character)[0] in {"L", "M", "N"}
             or character in " .-'’")
        for character in name
    ):
        raise ValueError(
            "Daemon name must be 1–40 letters, numbers, spaces, dots, "
            "apostrophes or hyphens."
        )
    return name


def identity_path(memory_root: Path) -> Path:
    return memory_root / "core" / IDENTITY_FILENAME


def identity_document(memory_root: Path, name: str, gender: str, *, user: dict[str, str] | None = None) -> dict[str, object]:
    name = validate_name(name)
    if gender not in VALID_GENDERS:
        raise ValueError("Gender must be feminine or masculine.")
    result = {"name": name, "gender": gender}
    target = identity_path(memory_root)
    if target.is_symlink():
        raise ValueError("Identity settings must not be a symbolic link.")
    existing = _read_valid_identity(target) if target.exists() else {}
    # A daemon rename must not silently erase a previous user's setup answers.
    chosen = user if user is not None else existing.get("user")
    if chosen is not None:
        result["user"] = validate_bound_user(chosen)
    return result


def save_daemon_identity(memory_root: Path, name: str, gender: str, *, user: dict[str, str] | None = None) -> str:
    """Persist only identity metadata; never read or write a persona file."""

    name = validate_name(name)
    if gender not in VALID_GENDERS:
        raise ValueError("Gender must be feminine or masculine.")
    if user is not None:
        validate_bound_user(user)
    root = memory_root.expanduser().resolve()
    target = identity_path(root)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    if not target.parent.resolve().is_relative_to(root):
        raise ValueError("Identity settings escape the memory root.")

    with memory_file_lock(target):
        if target.is_symlink():
            raise ValueError("Identity settings must not be a symbolic link.")
        document = identity_document(root, name, gender, user=user)
        content = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        existing = None
        if target.exists():
            existing = _read_valid_identity(target)
            # Same values in another layout (an older daemon's file) are
            # rewritten, so the file always has the one canonical form.
            if target.read_bytes() == content.encode("utf-8"):
                _protect_identity_file(target)
                return "unchanged"
        atomic_write_text(target, content)
        _protect_identity_file(target)
    return "updated" if existing is not None else "created"


def load_daemon_identity(memory_root: Path | None = None) -> DaemonIdentity:
    if memory_root is None:
        from daemon_core.paths import MEMORY_ROOT

        memory_root = MEMORY_ROOT
    target = identity_path(memory_root.expanduser())
    if target.exists() and not target.is_symlink():
        try:
            data = _read_valid_identity(target)
            return DaemonIdentity(
                name=data["name"],
                gender=data["gender"],
                configured=True,
                user=data.get("user"),
            )
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            logging.getLogger(__name__).warning(
                "Invalid daemon identity settings; keeping the default identity"
            )
    try:
        fallback = validate_name(
            os.getenv("DAEMON_CORE_AGENT_NAME", DEFAULT_DAEMON_NAME)
        )
    except ValueError:
        fallback = DEFAULT_DAEMON_NAME
    return DaemonIdentity(name=fallback)


def _read_valid_identity(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) not in ({"name", "gender"}, {"name", "gender", "user"}):
        raise ValueError("Identity settings have an unknown format.")
    name = validate_name(data["name"])
    gender = data["gender"]
    if gender not in VALID_GENDERS:
        raise ValueError("Identity settings contain an invalid gender.")
    result = {"name": name, "gender": gender}
    if "user" in data:
        result["user"] = validate_bound_user(data["user"])
    return result


def _protect_identity_file(path: Path) -> None:
    path.chmod(0o600)
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PermissionError("Identity settings are not private.")


__all__ = [
    "DaemonIdentity",
    "DEFAULT_DAEMON_NAME",
    "VALID_GENDERS",
    "identity_path",
    "load_daemon_identity",
    "save_daemon_identity",
    "validate_name",
]
