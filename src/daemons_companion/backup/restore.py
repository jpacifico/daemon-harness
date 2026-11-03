"""Restore a backup as the memory of a new daemon installation.

The desktop app decrypts the chosen backup into a private file and passes its
path in DAEMONS_RESTORE_ARCHIVE. Setup then takes the daemon's name, gender and
conversation language from the backup, accepts only the Telegram account the
memory is filed under, and writes the archive as the new memory folder before
any service starts. Nothing else is read from the backup.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

from daemon_core.daemon_identity import VALID_GENDERS, validate_name
from daemons_companion.backup.archive import BackupError, inspect_archive, restore_archive

ENVIRONMENT = "DAEMONS_RESTORE_ARCHIVE"
# Set by the app when the user confirms the original daemon no longer runs.
MOVE_ENVIRONMENT = "DAEMONS_RESTORE_MOVE"
LANGUAGES = {"en", "fr"}


@dataclass(frozen=True)
class RestoreSource:
    archive: Path
    name: str
    gender: str
    owner: str
    language: str | None
    manifest_sha256: str
    move: bool = False


def _member(archive: zipfile.ZipFile, relative: str, limit: int = 8192):
    info = archive.getinfo("memory/" + relative)
    if info.file_size > limit:
        raise BackupError("restore.invalid-identity")
    return json.loads(archive.read(info).decode("utf-8"))


def read_source(path: Path) -> RestoreSource:
    """Validate the whole archive, then read what setup needs from it."""
    manifest = inspect_archive(path)
    owners = sorted({
        parts[2]
        for parts in (name.split("/") for name in manifest["files"])
        if len(parts) >= 4 and parts[:2] == [".daemon", "users"]
        and re.fullmatch(r"[1-9][0-9]{0,19}", parts[2])
    })
    # Companion memories belong to one Telegram account; anything else needs
    # a reviewed migration, not an automatic choice.
    if len(owners) != 1:
        raise BackupError("restore.owner-ambiguous")
    try:
        with zipfile.ZipFile(path) as archive:
            identity = _member(archive, ".daemon/core/daemon_identity.json")
            meeting_name = f".daemon/users/{owners[0]}/first_meeting.json"
            meeting = _member(archive, meeting_name) if meeting_name in manifest["files"] else {}
        name = validate_name(identity.get("name"))
        gender = identity.get("gender")
    except (KeyError, ValueError, UnicodeError, AttributeError) as error:
        raise BackupError("restore.invalid-identity") from error
    if gender not in VALID_GENDERS:
        raise BackupError("restore.invalid-identity")
    language = meeting.get("language") if isinstance(meeting, dict) else None
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return RestoreSource(Path(path), name, gender, owners[0],
                         language if language in LANGUAGES else None, digest)


_PENDING: dict[tuple[str, bool], RestoreSource] = {}


def pending_restore() -> RestoreSource | None:
    """The backup this setup restores, or None for an ordinary new daemon."""
    value = os.environ.get(ENVIRONMENT, "").strip()
    if not value:
        return None
    move = os.environ.get(MOVE_ENVIRONMENT, "") == "1"
    if (value, move) not in _PENDING:
        _PENDING[(value, move)] = replace(read_source(Path(value)), move=move)
    return _PENDING[(value, move)]


def restore_memory(source: RestoreSource, root: Path, user_id: str) -> None:
    """Write the backup as a new memory folder; never into an existing one."""
    if user_id != source.owner:
        raise BackupError("restore.other-telegram-account")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise BackupError("archive.destination-exists")
    if root.exists():
        root.rmdir()
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restore_archive(source.archive, root, move=source.move)
