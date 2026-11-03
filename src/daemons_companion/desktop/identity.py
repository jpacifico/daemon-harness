"""Disposable companion operation for daemon identity metadata."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable

from daemons_companion.setup.plan import PlanStep
from daemons_companion.runtime.release import validate_companion_home
from daemons_companion.runtime.installation_scope import InstallationScope
from daemon_core.daemon_identity import (
    VALID_GENDERS,
    identity_path,
    identity_document,
    save_daemon_identity,
    validate_name,
)


IDENTITY_STEP = "identity"
_SNAPSHOT_SCHEMA = "daemon.companion-identity-snapshot.v1"
_VOICE_FILENAMES = frozenset({"persona.md", "conversation.md"})


@dataclass(frozen=True)
class CompanionIdentityLayout:
    companion_home: Path
    installation_scope: InstallationScope | None = None
    root_folder_memory: bool = False

    @property
    def memory_root(self) -> Path:
        root = self.companion_home / "data" / "memory"
        return root / ".daemon" if self.root_folder_memory else root

    @property
    def identity_file(self) -> Path:
        return identity_path(self.memory_root)


class CompanionIdentityAdapter:
    """Store exact identity settings in the private installation transaction."""

    def __init__(
        self,
        layout: CompanionIdentityLayout,
        *,
        name: str,
        gender: str,
        user: dict[str, str] | None = None,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.layout = layout
        self.name = validate_name(name)
        if gender not in VALID_GENDERS:
            raise ValueError("Gender must be feminine or masculine.")
        self.gender = gender
        from daemon_core.onboarding_user import validate_bound_user
        self.user = validate_bound_user(user) if user is not None else None
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        _validate_snapshot_dir(snapshot_dir)
        before = _path_descriptor(self.layout.identity_file)
        if before["kind"] == "file":
            _atomic_write(
                snapshot_dir / "identity.before",
                self.layout.identity_file.read_bytes(),
                0o600,
            )
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "before": before,
            "expected": _file_descriptor_bytes(self._expected_bytes(), 0o600),
            "core_before": _path_descriptor(self.layout.identity_file.parent),
            "voice_before": _voice_descriptors(self.layout.memory_root),
        }
        _atomic_write(snapshot_dir / "manifest.json", _json_bytes(manifest), 0o600)
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Identity snapshot is required before apply.")
        manifest = _load_manifest(snapshot_dir)
        if _path_descriptor(self.layout.identity_file) != manifest["before"]:
            raise RuntimeError("Identity changed after its snapshot.")
        if _voice_descriptors(self.layout.memory_root) != manifest["voice_before"]:
            raise RuntimeError("Voice files changed after the identity snapshot.")
        save_daemon_identity(
            self.layout.memory_root,
            self.name,
            self.gender,
            user=self.user,
        )
        core_before = manifest["core_before"]
        if os.name != "nt" and core_before["kind"] == "directory":
            self.layout.identity_file.parent.chmod(int(core_before["mode"]))
        self._mutation_hook("identity")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Identity snapshot is required before verify.")
        manifest = _load_manifest(snapshot_dir)
        if _path_descriptor(self.layout.identity_file) != manifest["expected"]:
            raise RuntimeError("Daemon identity was not applied exactly.")
        if _voice_descriptors(self.layout.memory_root) != manifest["voice_before"]:
            raise RuntimeError("A canonical voice file changed during identity setup.")

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        manifest = _load_manifest(snapshot_dir)
        current = _path_descriptor(self.layout.identity_file)
        if current not in (manifest["before"], manifest["expected"]):
            raise RuntimeError("Identity has concurrent changes; rollback was refused.")
        if _voice_descriptors(self.layout.memory_root) != manifest["voice_before"]:
            raise RuntimeError("Voice files have concurrent changes; rollback was refused.")

        before = manifest["before"]
        if before["kind"] == "absent":
            self.layout.identity_file.unlink(missing_ok=True)
        elif before["kind"] == "file":
            saved = snapshot_dir / "identity.before"
            if saved.is_symlink() or not saved.is_file():
                raise RuntimeError("Previous identity snapshot is unavailable.")
            _atomic_write(
                self.layout.identity_file,
                saved.read_bytes(),
                int(before.get("mode", 0o600)),
            )
        else:
            raise RuntimeError("Previous identity state cannot be restored safely.")
        if manifest["core_before"]["kind"] == "absent":
            self.layout.identity_file.parent.rmdir()

    def _expected_bytes(self) -> bytes:
        return (
            json.dumps(
                identity_document(self.layout.memory_root, self.name, self.gender, user=self.user),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    def _validate_step(self, step: PlanStep) -> None:
        if step.code != IDENTITY_STEP:
            raise ValueError("Identity adapter accepts only the identity step.")

    def _validate_layout(self) -> None:
        home = validate_companion_home(self.layout.companion_home, self.layout.installation_scope)
        memory = self.layout.memory_root
        if memory.is_symlink() or not memory.is_dir():
            raise ValueError("Identity adapter requires the existing shared memory root.")
        try:
            memory.relative_to(home)
        except ValueError as error:  # pragma: no cover - fixed layout property.
            raise ValueError("Identity memory root escapes the companion home.") from error
        core = self.layout.identity_file.parent
        if core.is_symlink():
            raise ValueError("Identity directory must not be a symbolic link.")
        if self.layout.identity_file.is_symlink():
            raise ValueError("Identity settings must not be a symbolic link.")


def _voice_descriptors(memory_root: Path) -> dict[str, dict[str, object]]:
    descriptors: dict[str, dict[str, object]] = {}
    for path in sorted(memory_root.rglob("*")):
        if path.name not in _VOICE_FILENAMES:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Canonical voice files must be regular files.")
        descriptors[path.relative_to(memory_root).as_posix()] = _path_descriptor(path)
    return descriptors


def _path_descriptor(path: Path) -> dict[str, object]:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    if path.is_file():
        return _file_descriptor_bytes(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    if path.is_dir():
        descriptor: dict[str, object] = {"kind": "directory"}
        if os.name != "nt":
            descriptor["mode"] = stat.S_IMODE(path.stat().st_mode)
        return descriptor
    return {"kind": "special"}


def _file_descriptor_bytes(data: bytes, mode: int) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "kind": "file",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if os.name != "nt":
        descriptor["mode"] = mode
    return descriptor


def _load_manifest(snapshot_dir: Path) -> dict[str, object]:
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Identity snapshot manifest is unavailable.")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or set(loaded) != {
        "schema",
        "before",
        "expected",
        "core_before",
        "voice_before",
    }:
        raise RuntimeError("Identity snapshot manifest is invalid.")
    if loaded["schema"] != _SNAPSHOT_SCHEMA:
        raise RuntimeError("Identity snapshot version is unsupported.")
    return loaded


def _validate_snapshot_dir(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("Identity snapshot directory must be direct and absolute.")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


__all__ = [
    "CompanionIdentityAdapter",
    "CompanionIdentityLayout",
    "IDENTITY_STEP",
]
