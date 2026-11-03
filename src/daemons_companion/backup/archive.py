"""Portable exact-byte archives of a daemon's memory vault.

This module never stops services, reads credentials or activates a runtime. The
caller supplies a settled copy (see snapshot.py), never a vault being written.
Restoration only creates a new vault; installation and activation are separate.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile

from daemon_core.memory.integrity import atomic_write_text
from daemon_core.memory.library_layout import vault_root
from daemon_core.platform_support import io_path
from daemons_companion.desktop.memory import initialize_empty_vault

SCHEMA = "daemon.portable-memory.v2"
# A whole-memory export also keeps, byte for byte and at the same place, files
# this contract does not know (older daemons' archives and technical state).
SCHEMA_PRESERVING = "daemon.portable-memory.v3"
PRESERVABLE = {"archive.unknown-technical-state", "archive.legacy-state-review-required"}
# Even a whole-memory export never carries what is named like a credential store.
CREDENTIAL_NAME = re.compile(
    r"(?:^\.env$|\.env$|^\.env\.|credential|token|secret|password|auth.*\.json$|\.(?:key|pem|p12)$|^provider\.json$)",
    re.IGNORECASE,
)
MAX_FILES = 100_000
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024 * 1024
MAX_MANIFEST = 32 * 1024 * 1024
CHUNK = 1024 * 1024
REQUIRED = {".daemon/core/daemon_identity.json"}


def has_voice(files) -> bool:
    """A persona: shared in core/ (older daemons) or per user (Companion)."""
    return any(
        name == ".daemon/core/persona.md"
        or (name.startswith(".daemon/users/") and name.endswith("/persona.md") and name.count("/") == 3)
        for name in files
    )
RECREATED = {".daemon/vault-layout.json", ".daemon/.memory-structure-prototype.json"}
SYSTEM_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# Junctions and other Windows reparse points are links, never memory.
REPARSE_POINT = 0x400
# Durable daemon state under .daemon/levels. Anything not listed below is
# unknown and blocks the backup, so new state needs an explicit decision here.
LEVELS_INCLUDED = {
    "common_ground_shadow",
    "memory_relation_shadow",
    "note_relocations",
    # Relation candidates waiting for the nightly judge: the new daemon resumes them.
    "relation_gardening",
}
LEVELS_REBUILT = {
    "memory_graph.json",
    "capsule_recalls.md",
    "metadata_maintenance.jsonl",
    "registre_passes.jsonl",
    "embeddings",
    "atlas",
}
TEXT_STATE = {".md", ".json", ".jsonl"}
# The only JSON files kept beside Markdown in core/ and users/<id>/.
IDENTITY_STATE = {"daemon_identity.json", "first_meeting.json"}


class BackupError(ValueError):
    """A bounded error code, safe for diagnostics (never file contents).

    `area` optionally names where it happened, never a note's name.
    """

    def __init__(self, code: str, area: str = ""):
        super().__init__(code)
        self.area = area


def area(relative: str) -> str:
    """The technical folder of a path, or "notes" for the user's own files."""
    parts = relative.split("/")
    if parts[0] != ".daemon":
        return "notes"
    return "/".join(parts[: min(3, len(parts) - 1)]) or ".daemon"


def checked_relative(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(c in value for c in '<>:"|?*')
    ):
        raise BackupError("archive.path")
    parts = value.split("/")
    if len(parts) > 24 or any(
        not p
        or p in {".", ".."}
        or p.endswith((" ", "."))
        or len(p.encode("utf-8")) > 240
        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
        for p in parts
    ):
        raise BackupError("archive.path")
    return value


def policy(relative: str) -> str:
    """An explicit portable contract. Unknown technical data blocks export."""
    checked_relative(relative)
    parts = relative.split("/")
    if relative in RECREATED:
        return "recreate"
    if parts[-1] in SYSTEM_FILES:
        return "system"
    if any(p.endswith((".lock", ".tmp")) for p in parts):
        return "transient"
    if parts[0] != ".daemon":
        # The user's own files: notes, attachments, canvases, at any depth.
        # Hidden entries belong to editors (.obsidian, .trash) or the system.
        if any(p.startswith(".") for p in parts):
            return "editor-settings"
        return "include"
    tail = parts[1:]
    if parts[-1].startswith("."):
        # Atomic writes stage a hidden sibling, then rename it over the target.
        return "transient"
    suffix = Path(relative).suffix
    if tail[0] in {"core", "users", "capsules"} and len(tail) >= 2:
        if suffix == ".md" or tail[-1] in IDENTITY_STATE:
            return "include"
        raise BackupError("archive.unknown-technical-state")
    if tail == ["root-migration.json"]:
        return "include"
    if tail[0] == "followups":
        # Scheduled reminders travel in the archive but are restored only by a
        # move, when the original daemon no longer sends them.
        if (len(tail) == 3 and tail[1] == "intents" and suffix == ".json") or tail == ["followups", "moved.json"]:
            return "move"
        if len(tail) == 4 and tail[1] == "users" and tail[3] == "disabled":
            return "include"
        return "external-actions-not-restored"
    if tail[0] == "levels" and len(tail) >= 2:
        name = tail[1]
        if name == "L0_ground_zero" and len(tail) >= 4:
            if tail[2] == "conversations" and len(tail) == 4 and suffix == ".md":
                return "include"
            if tail[2] == "idempotency":
                return "channel-state"
        if name in LEVELS_INCLUDED and len(tail) >= 3 and suffix in TEXT_STATE:
            return "include"
        if len(tail) == 2 and re.fullmatch(
            r"(?:rem_state_|rem_episodic_state_)[A-Za-z0-9_-]+\.json", name
        ):
            return "include"
        if name.startswith("lock_") or name == ".relation-publication":
            return "transient"
        if name == "dreaming" or (
            len(tail) == 2 and re.fullmatch(r"single_pass_trace_[A-Za-z0-9_-]+\.json", name)
        ):
            return "diagnostic"
        if name == "memory_import_backups":
            return "local-backup"
        if name in LEVELS_REBUILT or name.startswith("scene_verdict_"):
            return "rebuild"
    if tail[0] in {"archive", "templates"}:
        # Older layouts can keep authoritative material there; moving such a
        # memory is a separate, reviewed migration.
        raise BackupError("archive.legacy-state-review-required")
    raise BackupError("archive.unknown-technical-state")


def direct(path: Path) -> Path:
    path = Path(path)
    if (
        not path.is_absolute()
        or path.resolve() != path
        or path.is_symlink()
        or getattr(path, "is_junction", lambda: False)()
    ):
        raise BackupError("archive.indirect-path")
    return path


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def preserved_policy(relative: str) -> str:
    """How a whole-memory export treats a file the contract does not know:
    ``preserve`` it unchanged, or leave a ``credential`` behind; empty otherwise."""
    try:
        policy(relative)
    except BackupError as error:
        if str(error) in PRESERVABLE:
            return "credential" if CREDENTIAL_NAME.search(relative.rsplit("/", 1)[-1]) else "preserve"
    return ""


IDENTITY = ".daemon/core/daemon_identity.json"


def supplied_identity(name: str, gender: str) -> bytes:
    """Identity for a memory whose daemon never recorded one (older daemons)."""
    from daemon_core.daemon_identity import VALID_GENDERS, validate_name

    if gender not in VALID_GENDERS:
        raise BackupError("archive.invalid-identity")
    try:
        name = validate_name(name)
    except ValueError:
        raise BackupError("archive.invalid-identity") from None
    # The layout daemon_core writes, so the installed file needs no rewrite.
    return (json.dumps({"name": name, "gender": gender}, ensure_ascii=False, indent=2) + "\n").encode()


def inventory(
    root: Path, *, preserve_unknown: bool = False, identity: bytes | None = None
) -> tuple[dict, dict, dict, dict]:
    direct(root)
    if vault_root(root / ".daemon") != root:
        raise BackupError("archive.parga-required")
    files, moving, excluded, preserved, seen = {}, {}, {}, {}, set()
    total = 0
    count = 0
    # File operations use the extended spelling on Windows, so deep notes past
    # the legacy 260-character limit are read like any other.
    walk_root = io_path(root)
    for parent, directories, names in os.walk(walk_root, followlinks=False):
        for name in sorted(directories + names):
            count += 1
            if count > MAX_FILES * 2:
                raise BackupError("archive.too-many-files")
            path = Path(parent) / name
            relative = checked_relative(path.relative_to(walk_root).as_posix())
            key = unicodedata.normalize("NFC", relative).casefold()
            if key in seen:
                raise BackupError("archive.path-collision")
            seen.add(key)
            try:
                info = path.lstat()
            except FileNotFoundError:
                # Removed between listing and reading: the daemon is writing.
                raise BackupError("archive.source-changed") from None
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
                raise BackupError("archive.indirect-path")
            if stat.S_ISDIR(info.st_mode):
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_FILE
            ):
                raise BackupError("archive.unsafe-member")
            try:
                disposition = policy(relative)
            except BackupError:
                if not (preserve_unknown and preserved_policy(relative)):
                    raise
                disposition = preserved_policy(relative)
            if disposition == "transient":
                # Locks and atomic-write staging come and go; never read them.
                continue
            if disposition not in {"include", "move", "preserve"}:
                # Left behind and never read: editor settings, logs, indexes and
                # other projections change all the time without affecting the
                # copy, and another program may hold them open.
                excluded[relative] = {"size": info.st_size, "reason": disposition}
                continue
            try:
                entry = {"size": info.st_size, "sha256": digest_file(path)}
            except FileNotFoundError:
                raise BackupError("archive.source-changed", area(relative)) from None
            except PermissionError:
                # Windows refuses reads of a file another process holds open
                # without sharing.
                raise BackupError("archive.file-unreadable", area(relative)) from None
            total += info.st_size
            if total > MAX_TOTAL or len(files) >= MAX_FILES:
                raise BackupError("archive.too-large")
            if disposition == "include":
                files[relative] = entry
            elif disposition == "preserve":
                preserved[relative] = entry
            else:
                moving[relative] = entry
    if identity is not None:
        # Only for a memory without one: an existing identity is never replaced.
        if IDENTITY in files:
            raise BackupError("archive.identity-exists")
        files[IDENTITY] = {"size": len(identity), "sha256": hashlib.sha256(identity).hexdigest()}
    if not REQUIRED <= files.keys():
        raise BackupError("archive.missing-identity", "identity")
    if not has_voice(files):
        raise BackupError("archive.missing-identity", "persona")
    return files, moving, excluded, preserved


def create_archive(
    snapshot: Path, destination: Path, *, preserve_unknown: bool = False, identity: bytes | None = None
) -> dict:
    """Export only an offline snapshot. Never modify or activate its source.

    ``preserve_unknown`` makes a whole-memory export: files the contract does
    not know travel unchanged instead of blocking the backup.
    """
    snapshot, destination = direct(snapshot), direct(destination)
    direct(destination.parent)
    if destination.exists() or destination.is_relative_to(snapshot):
        raise BackupError("archive.new-external-file-required")
    files, moving, excluded, preserved = inventory(snapshot, preserve_unknown=preserve_unknown, identity=identity)
    manifest = {
        "schema": SCHEMA_PRESERVING if preserve_unknown else SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "moving": moving,
        "excluded": excluded,
    }
    if preserve_unknown:
        manifest["preserved"] = preserved
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    if len(raw) > MAX_MANIFEST:
        raise BackupError("archive.manifest-too-large")
    fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            with zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                archive.writestr("manifest.json", raw)
                for relative in sorted({**files, **moving, **preserved}):
                    if identity is not None and relative == IDENTITY:
                        archive.writestr("memory/" + relative, identity)
                        continue
                    try:
                        archive.write(io_path(snapshot / relative), "memory/" + relative)
                    except FileNotFoundError:
                        raise BackupError("archive.source-changed") from None
                    except PermissionError:
                        raise BackupError("archive.file-unreadable") from None
            output.flush()
            os.fsync(output.fileno())
        # Every file the backup carries must be unchanged since it was read.
        after, after_moving, _, after_preserved = inventory(
            snapshot, preserve_unknown=preserve_unknown, identity=identity)
        if (after, after_moving, after_preserved) != (files, moving, preserved):
            before_all, after_all = {**files, **moving, **preserved}, {**after, **after_moving, **after_preserved}
            changed = sorted(
                {k for k, v in after_all.items() if before_all.get(k) != v}
                | (before_all.keys() - after_all.keys())
            )
            raise BackupError("archive.source-changed", area(changed[0]) if changed else "")
        inspect_archive(Path(temporary))
        # Hard-link publication refuses an existing destination on both platforms.
        os.link(temporary, destination)
        return manifest
    finally:
        Path(temporary).unlink(missing_ok=True)


def _excluded_reason_matches(relative: str, reason, schema: str) -> bool:
    try:
        expected = policy(relative)
    except BackupError:
        # Only a whole-memory export records an unknown file it left behind.
        return schema == SCHEMA_PRESERVING and reason == "credential" == preserved_policy(relative)
    return expected != "include" and reason == expected


def _manifest(archive: zipfile.ZipFile) -> dict:
    members = archive.infolist()
    if len(members) > MAX_FILES + 1 or len({m.filename for m in members}) != len(
        members
    ):
        raise BackupError("archive.duplicate-or-excessive-members")
    if any(
        m.flag_bits & 1
        or m.compress_type != zipfile.ZIP_STORED
        or m.is_dir()
        or stat.S_IFMT(m.external_attr >> 16) not in (0, stat.S_IFREG)
        for m in members
    ):
        raise BackupError("archive.unsafe-member")
    try:
        header = archive.getinfo("manifest.json")
        if header.file_size > MAX_MANIFEST:
            raise BackupError("archive.manifest-too-large")
        manifest = json.loads(archive.read(header))
    except (KeyError, ValueError, UnicodeError) as error:
        raise BackupError("archive.invalid-manifest") from error
    keys = {"schema", "created_at", "files", "moving", "excluded"}
    if not isinstance(manifest, dict) or not (
        (manifest.get("schema") == SCHEMA and set(manifest) == keys)
        or (manifest.get("schema") == SCHEMA_PRESERVING and set(manifest) == keys | {"preserved"})
    ):
        raise BackupError("archive.unsupported-version")
    preserved = manifest.setdefault("preserved", {}) if manifest["schema"] == SCHEMA_PRESERVING else {}
    files = manifest["files"]
    if (
        not isinstance(files, dict)
        or not REQUIRED <= files.keys()
        or not has_voice(files)
        or len(files) > MAX_FILES
    ):
        raise BackupError("archive.invalid-manifest")
    if not isinstance(manifest["created_at"], str) or len(manifest["created_at"]) > 64:
        raise BackupError("archive.invalid-manifest")
    try:
        if datetime.fromisoformat(manifest["created_at"]).tzinfo is None:
            raise ValueError()
    except ValueError as error:
        raise BackupError("archive.invalid-manifest") from error
    excluded = manifest["excluded"]
    if not isinstance(excluded, dict) or len(excluded) > MAX_FILES:
        raise BackupError("archive.invalid-manifest")
    for relative, entry in excluded.items():
        if (
            not isinstance(entry, dict)
            # Older archives fingerprinted what they left behind.
            or set(entry) not in ({"size", "reason"}, {"size", "sha256", "reason"}, {"size", "mtime_ns", "reason"})
            or not _excluded_reason_matches(relative, entry["reason"], manifest["schema"])
            or type(entry["size"]) is not int
            or not 0 <= entry["size"] <= MAX_FILE
            # A file another program held open is recorded by modification time.
            or ("sha256" in entry and not (isinstance(entry["sha256"], str) and re.fullmatch("[0-9a-f]{64}", entry["sha256"])))
            or ("mtime_ns" in entry and type(entry["mtime_ns"]) is not int)
        ):
            raise BackupError("archive.invalid-manifest")
    moving = manifest["moving"]
    if (
        not isinstance(moving, dict)
        or not isinstance(preserved, dict)
        or len(files) + len(moving) + len(preserved) > MAX_FILES
    ):
        raise BackupError("archive.invalid-manifest")
    seen, total = set(), 0
    for relative, entry in [*files.items(), *moving.items(), *preserved.items()]:
        if relative in preserved:
            checked_relative(relative)
            expected = preserved_policy(relative) == "preserve"
        else:
            expected = policy(relative) == ("include" if relative in files else "move")
        if not expected or not isinstance(entry, dict) or set(entry) != {"size", "sha256"}:
            raise BackupError("archive.invalid-manifest")
        if (
            type(entry["size"]) is not int
            or not 0 <= entry["size"] <= MAX_FILE
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", entry["sha256"])
        ):
            raise BackupError("archive.invalid-manifest")
        total += entry["size"]
        key = unicodedata.normalize("NFC", relative).casefold()
        if key in seen:
            raise BackupError("archive.path-collision")
        seen.add(key)
        try:
            if archive.getinfo("memory/" + relative).file_size != entry["size"]:
                raise BackupError("archive.size-mismatch")
        except KeyError as error:
            raise BackupError("archive.missing-member") from error
    if total > MAX_TOTAL or {m.filename for m in members} != {
        "manifest.json",
        *("memory/" + p for p in [*files, *moving, *preserved]),
    }:
        raise BackupError("archive.unexpected-members")
    for key in seen:
        if any(
            "/".join(key.split("/")[:i]) in seen for i in range(1, len(key.split("/")))
        ):
            raise BackupError("archive.overlapping-members")
    return manifest


def _check_members(
    archive: zipfile.ZipFile, manifest: dict, destination: Path | None = None, *, move: bool = False
):
    carried = {**manifest["files"], **manifest.get("preserved", {})}
    for relative, entry in [*carried.items(), *manifest["moving"].items()]:
        # Every member is verified; reminders are written only by a move.
        written = destination is not None and (move or relative in carried)
        digest = hashlib.sha256()
        size = 0
        output = None
        try:
            if written:
                target = io_path(destination / relative)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output = target.open("xb")
                os.chmod(target, 0o600)
            with archive.open("memory/" + relative) as source:
                while chunk := source.read(CHUNK):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise BackupError("archive.size-mismatch")
                    digest.update(chunk)
                    if output is not None:
                        output.write(chunk)
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise BackupError("archive.integrity")
        finally:
            if output is not None:
                output.close()


def _bounded_zip(source: Path) -> None:
    """Bound the central directory BEFORE zipfile allocates entry objects."""
    size = source.stat().st_size
    if size < 22 or size > MAX_TOTAL + MAX_MANIFEST + MAX_FILES * 4096:
        raise BackupError("archive.too-large")
    with source.open("rb") as handle:
        handle.seek(max(0, size - 65557))
        tail = handle.read(65557)
        index = tail.rfind(b"PK\x05\x06")
        if index < 0 or len(tail) - index < 22:
            raise BackupError("archive.invalid-zip")
        _, disk, central_disk, on_disk, entries, central_size, offset, comment = (
            struct.unpack("<4s4H2LH", tail[index : index + 22])
        )
        if (
            disk
            or central_disk
            or on_disk != entries
            or index + 22 + comment != len(tail)
        ):
            raise BackupError("archive.invalid-zip")
        end_position = size - len(tail) + index
        if entries == 65535 or central_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            if end_position < 20:
                raise BackupError("archive.invalid-zip64")
            handle.seek(end_position - 20)
            signature, disk, location, disks = struct.unpack("<4sLQL", handle.read(20))
            if (
                signature != b"PK\x06\x07"
                or disk
                or disks != 1
                or location + 56 > end_position - 20
            ):
                raise BackupError("archive.invalid-zip64")
            handle.seek(location)
            record = handle.read(56)
            (
                signature,
                length,
                made,
                needed,
                disk,
                central_disk,
                on_disk,
                entries,
                central_size,
                offset,
            ) = struct.unpack("<4sQ2H2L4Q", record)
            if (
                signature != b"PK\x06\x06"
                or length != 44
                or disk
                or central_disk
                or on_disk != entries
            ):
                raise BackupError("archive.invalid-zip64")
            end_position = location
        if (
            entries > MAX_FILES + 1
            or central_size > MAX_MANIFEST * 2
            or offset + central_size != end_position
        ):
            raise BackupError("archive.central-directory-bound")


def inspect_archive(source: Path) -> dict:
    direct(source)
    if (
        not source.is_file()
        or source.stat().st_size > MAX_TOTAL + MAX_MANIFEST + MAX_FILES * 4096
    ):
        raise BackupError("archive.too-large")
    _bounded_zip(source)
    with zipfile.ZipFile(source) as archive:
        manifest = _manifest(archive)
        _check_members(archive, manifest)
    return manifest


def restore_archive(source: Path, destination: Path, *, move: bool = False) -> dict:
    """Restore exact bytes into a NEW vault. No services, credentials or jobs.

    A move also restores the pending reminders and lists them for the new host,
    which schedules them on its own channel at its first completed turn.
    """
    direct(source)
    destination = direct(destination)
    direct(destination.parent)
    if destination.exists():
        raise BackupError("archive.destination-exists")
    manifest = inspect_archive(source)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        initialize_empty_vault(staging)
        (staging / ".daemon/restore-incomplete.json").write_text(
            json.dumps({"schema": SCHEMA})
        )
        with zipfile.ZipFile(source) as archive:
            if _manifest(archive) != manifest:
                raise BackupError("archive.source-changed")
            _check_members(archive, manifest, staging, move=move)
        if move:
            pending = []
            for relative in manifest["moving"]:
                if relative.startswith(".daemon/followups/intents/"):
                    intent = json.loads((staging / relative).read_text(encoding="utf-8"))
                    if isinstance(intent, dict) and intent.get("state") == "pending" and isinstance(intent.get("id"), str):
                        pending.append(intent["id"])
            listed = staging / ".daemon/followups/moved.json"
            if listed.exists():
                pending.extend(item for item in json.loads(listed.read_text(encoding="utf-8")) if isinstance(item, str))
            if pending:
                listed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write_text(listed, json.dumps(sorted(set(pending))) + "\n")
        # Reserve destination without replacing a concurrent directory. Staging
        # is not an installation; callers activate only after this returns.
        destination.mkdir(mode=0o700)
        try:
            for child in staging.iterdir():
                child.rename(destination / child.name)
            internal = destination / ".daemon"
            atomic_write_text(
                internal / ".memory-structure-prototype.json",
                json.dumps({"schema": 1, "memory_root": str(internal)}) + "\n",
            )
            atomic_write_text(
                internal / "vault-layout.json",
                json.dumps(
                    {
                        "schema": 1,
                        "layout": "root-folders-v1",
                        "vault_root": str(destination),
                        "runtime_root": str(internal),
                    }
                )
                + "\n",
            )
        except BaseException:
            shutil.rmtree(destination)
            raise
        (destination / ".daemon/restore-incomplete.json").unlink()
        return manifest
    finally:
        shutil.rmtree(staging)
