"""Disposable-only versioned runtime operation for the companion app."""

from __future__ import annotations

# Keep logical paths in plans; use the extended Win32 namespace only for I/O.
from daemons_companion.runtime.bootstrap import _source_io_path as _io_path

from daemons_companion.runtime.installation_scope import InstallationScope

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable

from daemons_companion.setup.plan import PlanStep


RUNTIME_LAYOUT_STEP = "runtime-layout"
DISPOSABLE_HOME_MARKER = ".daemon-harness-disposable-companion"
_MARKER_CONTENT = "daemon-harness disposable companion home v1\n"
_RELEASE_MANIFEST = ".daemon-harness-release.json"
_RELEASE_SCHEMA = "daemon.companion-release.v1"
_SNAPSHOT_SCHEMA = "daemon.companion-runtime-snapshot.v1"
_POINTER_SCHEMA = "daemon.companion-runtime-pointer.v1"
_MAX_FILES = 4096
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_REQUIRED_FILES = frozenset({
    "requirements-runtime.lock",
    "requirements.txt",
    "src/daemon_core/__init__.py",
    "integrations/hermes_plugin/__init__.py",
    "integrations/hermes_voice_plugin/__init__.py",
    "integrations/openclaw_plugin/index.js",
    "integrations/after_turn_service/service.sh",
    "integrations/dreaming_service/service.sh",
    "src/daemons_companion/runtime/service_entry.py",
    "src/daemon_core/nightly_maintenance.py",
    "src/daemons_companion/runtime/services.py",
})


@dataclass(frozen=True)
class CompanionRuntimeLayout:
    companion_home: Path
    bundle_root: Path
    installation_scope: InstallationScope | None = None
    root_folder_memory: bool = False

    @property
    def data_root(self) -> Path:
        return self.companion_home / "data"

    @property
    def runtime_root(self) -> Path:
        return self.data_root / "runtime"

    @property
    def releases_dir(self) -> Path:
        return self.runtime_root / "releases"

    @property
    def current_pointer(self) -> Path:
        return self.runtime_root / "current.json"

    @property
    def memory_root(self) -> Path:
        return self.data_root / "memory" / ".daemon" if self.root_folder_memory else self.data_root / "memory"

    @property
    def state_root(self) -> Path:
        return self.data_root / "state"


def create_disposable_companion_marker(companion_home: Path) -> None:
    """Mark an empty synthetic home so the dormant adapter may use it."""

    root = _absolute_direct_path(companion_home, "Disposable companion home")
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("Disposable companion home must be a directory.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / DISPOSABLE_HOME_MARKER
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Disposable companion marker is invalid.")
        if marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
            raise ValueError("Disposable companion marker has unexpected content.")
        return
    _atomic_write(marker, _MARKER_CONTENT.encode("utf-8"), 0o600)


def validate_disposable_companion_home(companion_home: Path) -> Path:
    """Return a direct disposable home or fail before a managed write."""

    home = _absolute_direct_path(companion_home, "Companion home")
    marker = home / DISPOSABLE_HOME_MARKER
    if (
        not home.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != _MARKER_CONTENT
    ):
        raise ValueError("Operation requires a marked disposable companion home.")
    _reject_symlink_components(home)
    return home


def validate_companion_home(companion_home: Path, scope: InstallationScope | None = None) -> Path:
    if scope is None:
        return validate_disposable_companion_home(companion_home)
    if not isinstance(scope, InstallationScope):
        raise ValueError("Invalid explicit installation scope.")
    return scope.validate_home(companion_home)


class CompanionRuntimeAdapter:
    """Materialize one content-addressed runtime in a disposable home."""

    def __init__(
        self,
        layout: CompanionRuntimeLayout,
        *,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.layout = layout
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        _validate_snapshot_dir(snapshot_dir)
        bundle = _bundle_manifest(self.layout.bundle_root)
        release_id = str(bundle["digest"])
        release = self.layout.releases_dir / release_id
        stage = self.layout.runtime_root / f".staging-{release_id}"
        if stage.exists() or stage.is_symlink():
            raise RuntimeError("A staging tree already exists for this runtime bundle.")
        before = self._state(release, stage)
        if before["release"]["kind"] != "absent":
            _verify_release(release, bundle)
        if before["current"]["kind"] == "file":
            current_bytes = self.layout.current_pointer.read_bytes()
            _atomic_write(snapshot_dir / "current.before", current_bytes, 0o600)
        expected_pointer = _pointer_bytes(release_id)
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "release_id": release_id,
            "bundle": bundle,
            "before": before,
            "expected_pointer": _file_descriptor_bytes(expected_pointer, 0o600),
        }
        _atomic_write(
            snapshot_dir / "manifest.json",
            _json_bytes(manifest),
            0o600,
        )
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Runtime snapshot is required before apply.")
        manifest = _load_snapshot(snapshot_dir)
        bundle = manifest["bundle"]
        if _bundle_manifest(self.layout.bundle_root) != bundle:
            raise RuntimeError("Runtime bundle changed after its snapshot.")
        release_id = str(manifest["release_id"])
        release = self.layout.releases_dir / release_id
        stage = self.layout.runtime_root / f".staging-{release_id}"
        if self._state(release, stage) != manifest["before"]:
            raise RuntimeError("Runtime layout changed after its snapshot.")

        _mkdir_private(self.layout.runtime_root)
        _mkdir_private(self.layout.releases_dir)
        _copy_bundle(self.layout.bundle_root, stage, bundle)
        self._mutation_hook("staging")

        if manifest["before"]["release"]["kind"] == "absent":
            os.replace(_io_path(stage), _io_path(release))
        else:
            _verify_release(release, bundle)
            _remove_owned_tree(stage, bundle, allow_partial=False)
        self._mutation_hook("release")

        _mkdir_private(self.layout.memory_root)
        _mkdir_private(self.layout.state_root)
        self._mutation_hook("shared_roots")

        _atomic_write(
            self.layout.current_pointer,
            _pointer_bytes(release_id),
            0o600,
        )
        self._mutation_hook("current")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Runtime snapshot is required before verify.")
        manifest = _load_snapshot(snapshot_dir)
        release_id = str(manifest["release_id"])
        release = self.layout.releases_dir / release_id
        _verify_release(release, manifest["bundle"])
        if _path_descriptor(self.layout.current_pointer) != manifest["expected_pointer"]:
            raise RuntimeError("Active runtime pointer does not select this release.")
        for root in (self.layout.memory_root, self.layout.state_root):
            if root.is_symlink() or not root.is_dir():
                raise RuntimeError("Shared runtime roots are unavailable.")

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        manifest = _load_snapshot(snapshot_dir)
        bundle = manifest["bundle"]
        release_id = str(manifest["release_id"])
        release = self.layout.releases_dir / release_id
        stage = self.layout.runtime_root / f".staging-{release_id}"
        before = manifest["before"]

        conflicts: list[str] = []
        current = _path_descriptor(self.layout.current_pointer)
        if current not in (before["current"], manifest["expected_pointer"]):
            conflicts.append("current")
        release_now = _path_descriptor(release, recursive=True)
        if before["release"]["kind"] == "absent":
            if release_now["kind"] != "absent" and not _release_matches(release, bundle):
                conflicts.append("release")
        elif release_now != before["release"]:
            conflicts.append("release")
        if (stage.exists() or stage.is_symlink()) and not _tree_matches_bundle(
            stage,
            bundle,
            allow_partial=True,
        ):
            conflicts.append("staging")
        for name, root in (("memory", self.layout.memory_root), ("state", self.layout.state_root)):
            now = _path_descriptor(root, recursive=True)
            previous = before[name]
            if previous["kind"] == "absent":
                if now["kind"] not in {"absent", "directory"}:
                    conflicts.append(name)
            elif now["kind"] != "directory":
                conflicts.append(name)
        if conflicts:
            raise RuntimeError("Runtime layout has concurrent changes; rollback was refused.")

        _restore_pointer(
            self.layout.current_pointer,
            before["current"],
            snapshot_dir / "current.before",
        )
        if before["release"]["kind"] == "absent" and release.exists():
            _remove_owned_tree(release, bundle, allow_partial=False)
        if stage.exists():
            _remove_owned_tree(stage, bundle, allow_partial=True)
        for root, previous in (
            (self.layout.state_root, before["state"]),
            (self.layout.memory_root, before["memory"]),
        ):
            if previous["kind"] == "absent" and root.exists():
                if not any(root.iterdir()):
                    root.rmdir()
            elif (
                previous["kind"] == "directory"
                and root.exists()
                and os.name != "nt"
            ):
                root.chmod(int(previous["mode"]))
        _remove_created_empty_dirs(self.layout, before)

    def _state(self, release: Path, stage: Path) -> dict[str, dict[str, object]]:
        return {
            "data_root": _path_descriptor(self.layout.data_root, recursive=True),
            "runtime_root": _path_descriptor(self.layout.runtime_root, recursive=True),
            "releases_dir": _path_descriptor(self.layout.releases_dir, recursive=True),
            "current": _path_descriptor(self.layout.current_pointer),
            "release": _path_descriptor(release, recursive=True),
            "stage": _path_descriptor(stage, recursive=True),
            "memory": _path_descriptor(self.layout.memory_root, recursive=True),
            "state": _path_descriptor(self.layout.state_root, recursive=True),
        }

    def _validate_step(self, step: PlanStep) -> None:
        if step.code != RUNTIME_LAYOUT_STEP:
            raise ValueError("Runtime adapter accepts only the runtime-layout step.")

    def _validate_layout(self) -> None:
        home = validate_companion_home(self.layout.companion_home, self.layout.installation_scope)
        bundle = _absolute_direct_path(self.layout.bundle_root, "Runtime bundle")
        if not bundle.is_dir():
            raise ValueError("Runtime bundle must be a directory.")
        if _contains(home, bundle) or _contains(bundle, home):
            raise ValueError("Runtime bundle and companion home must be separate trees.")
        _reject_symlink_components(home)
        _reject_symlink_components(bundle)


def resolve_current_release(layout: CompanionRuntimeLayout) -> Path:
    """Resolve and validate the portable active-release pointer."""

    pointer = _io_path(layout.current_pointer)
    if pointer.is_symlink() or not pointer.is_file():
        raise RuntimeError("Active runtime pointer is unavailable.")
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    if set(payload) != {"schema", "release_id"} or payload["schema"] != _POINTER_SCHEMA:
        raise RuntimeError("Active runtime pointer is invalid.")
    release_id = payload["release_id"]
    if not _valid_digest(release_id):
        raise RuntimeError("Active runtime release id is invalid.")
    release = layout.releases_dir / release_id
    if _io_path(release).is_symlink() or not _io_path(release).is_dir():
        raise RuntimeError("Active runtime release is unavailable.")
    return release


def verify_current_release(layout: CompanionRuntimeLayout) -> Path:
    """Resolve the active release and verify every content-addressed entry."""

    release = resolve_current_release(layout)
    manifest_path = _io_path(release) / _RELEASE_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Active runtime release manifest is unavailable.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Active runtime release manifest is invalid.") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != _RELEASE_SCHEMA:
        raise RuntimeError("Active runtime release manifest is invalid.")
    if manifest.get("digest") != release.name:
        raise RuntimeError("Active runtime release identifier is inconsistent.")
    _verify_release(release, manifest)
    return release


def runtime_bundle_digest(bundle_root: Path) -> str:
    """Validate a prepared bundle and return its content-addressed identifier."""

    root = _absolute_direct_path(bundle_root, "Runtime bundle")
    if not root.is_dir():
        raise ValueError("Runtime bundle must be a directory.")
    _reject_symlink_components(root)
    return str(_bundle_manifest(root)["digest"])


def _bundle_manifest(root: Path) -> dict[str, object]:
    root = _io_path(root)
    entries: list[dict[str, object]] = []
    total = 0
    files = 0
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == _RELEASE_MANIFEST:
            raise ValueError("Runtime bundle contains a reserved manifest path.")
        if path.is_symlink():
            raise ValueError("Runtime bundle must not contain symbolic links.")
        if path.is_dir():
            entry = {"path": relative, "kind": "directory"}
            hasher.update(f"d\0{relative}\0".encode("utf-8"))
        elif path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            executable = bool(mode & 0o111)
            size = path.stat().st_size
            total += size
            files += 1
            if files > _MAX_FILES or total > _MAX_TOTAL_BYTES:
                raise ValueError("Runtime bundle exceeds its bounded size.")
            digest = _file_sha256(path)
            entry = {
                "path": relative,
                "kind": "file",
                "size": size,
                "sha256": digest,
                "executable": executable,
            }
            hasher.update(
                f"f\0{relative}\0{int(executable)}\0{size}\0{digest}\0".encode("utf-8")
            )
        else:
            raise ValueError("Runtime bundle contains a special file.")
        entries.append(entry)
    present_files = {str(entry["path"]) for entry in entries if entry["kind"] == "file"}
    missing = sorted(_REQUIRED_FILES - present_files)
    if missing:
        raise ValueError("Runtime bundle is incomplete: " + ", ".join(missing))
    return {
        "schema": _RELEASE_SCHEMA,
        "digest": hasher.hexdigest(),
        "file_count": files,
        "total_bytes": total,
        "entries": entries,
    }


def _copy_bundle(source: Path, stage: Path, bundle: dict[str, object]) -> None:
    source = _io_path(source)
    stage = _io_path(stage)
    if stage.exists() or stage.is_symlink():
        raise RuntimeError("Runtime staging path is already occupied.")
    stage.mkdir(parents=True, mode=0o700)
    for entry in bundle["entries"]:
        target = stage / str(entry["path"])
        if entry["kind"] == "directory":
            target.mkdir(mode=0o700)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (source / str(entry["path"])).read_bytes()
        expected = str(entry["sha256"])
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("Runtime bundle changed while it was copied.")
        _atomic_write(target, data, 0o700 if entry["executable"] else 0o600)
    _atomic_write(stage / _RELEASE_MANIFEST, _json_bytes(bundle), 0o600)
    if not _tree_matches_bundle(stage, bundle, allow_partial=False):
        raise RuntimeError("Staged runtime verification failed.")


def _verify_release(path: Path, bundle: dict[str, object]) -> None:
    if not _release_matches(path, bundle):
        raise RuntimeError("Runtime release differs from its content identifier.")


def _release_matches(path: Path, bundle: dict[str, object]) -> bool:
    return _tree_matches_bundle(path, bundle, allow_partial=False)


def _tree_matches_bundle(path: Path, bundle: dict[str, object], *, allow_partial: bool) -> bool:
    path = _io_path(path)
    if path.is_symlink() or not path.is_dir():
        return False
    expected = {str(entry["path"]): entry for entry in bundle["entries"]}
    expected[_RELEASE_MANIFEST] = {"path": _RELEASE_MANIFEST, "kind": "manifest"}
    seen: set[str] = set()
    for child in path.rglob("*"):
        relative = child.relative_to(path).as_posix()
        entry = expected.get(relative)
        if entry is None or child.is_symlink():
            return False
        seen.add(relative)
        if entry["kind"] == "directory":
            if not child.is_dir():
                return False
        elif entry["kind"] == "file":
            if not child.is_file() or child.stat().st_size != entry["size"]:
                return False
            if _file_sha256(child) != entry["sha256"]:
                return False
            executable = bool(stat.S_IMODE(child.stat().st_mode) & 0o111)
            if executable != entry["executable"]:
                return False
        else:
            if not child.is_file() or child.read_bytes() != _json_bytes(bundle):
                return False
    if allow_partial:
        return True
    return seen == set(expected)


def _remove_owned_tree(path: Path, bundle: dict[str, object], *, allow_partial: bool) -> None:
    path = _io_path(path)
    if not _tree_matches_bundle(path, bundle, allow_partial=allow_partial):
        raise RuntimeError("Managed runtime tree changed; removal was refused.")
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


def _remove_created_empty_dirs(
    layout: CompanionRuntimeLayout,
    before: dict[str, dict[str, object]],
) -> None:
    for path, key in (
        (layout.releases_dir, "releases_dir"),
        (layout.runtime_root, "runtime_root"),
        (layout.data_root, "data_root"),
    ):
        if before[key]["kind"] == "absent" and path.exists():
            try:
                path.rmdir()
            except OSError:
                # Memory and state are durable data, not runtime-owned debris.
                pass


def _restore_pointer(path: Path, before: dict[str, object], saved: Path) -> None:
    path = _io_path(path)
    saved = _io_path(saved)
    if before["kind"] == "absent":
        path.unlink(missing_ok=True)
        return
    if before["kind"] != "file" or not saved.is_file():
        raise RuntimeError("Previous runtime pointer snapshot is unavailable.")
    _atomic_write(path, saved.read_bytes(), int(before.get("mode", 0o600)))


def _pointer_bytes(release_id: str) -> bytes:
    return _json_bytes({"schema": _POINTER_SCHEMA, "release_id": release_id})


def _path_descriptor(path: Path, *, recursive: bool = False) -> dict[str, object]:
    path = _io_path(path)
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_file():
        return _file_descriptor_bytes(path.read_bytes(), mode)
    if path.is_dir():
        descriptor: dict[str, object] = {"kind": "directory"}
        if os.name != "nt":
            descriptor["mode"] = mode
        if recursive:
            descriptor["entries"] = _tree_descriptor(path)
        return descriptor
    return {"kind": "special", "mode": mode}


def _tree_descriptor(root: Path) -> list[dict[str, object]]:
    root = _io_path(root)
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        item = _path_descriptor(path)
        item["path"] = relative
        entries.append(item)
    return entries


def _file_descriptor_bytes(data: bytes, mode: int) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "kind": "file",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if os.name != "nt":
        descriptor["mode"] = mode
    return descriptor


def _load_snapshot(snapshot_dir: Path) -> dict[str, object]:
    snapshot_dir = _io_path(snapshot_dir)
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Runtime snapshot manifest is unavailable.")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or loaded.get("schema") != _SNAPSHOT_SCHEMA:
        raise RuntimeError("Runtime snapshot manifest is invalid.")
    release_id = loaded.get("release_id")
    bundle = loaded.get("bundle")
    if not _valid_digest(release_id) or not isinstance(bundle, dict):
        raise RuntimeError("Runtime snapshot manifest is invalid.")
    if bundle.get("digest") != release_id:
        raise RuntimeError("Runtime snapshot release identifier is inconsistent.")
    return loaded


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_snapshot_dir(path: Path) -> None:
    if not path.is_absolute() or _io_path(path).is_symlink() or not _io_path(path).is_dir():
        raise ValueError("Runtime snapshot directory must be direct and absolute.")


def _absolute_direct_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute.")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link.")
    return path


def _reject_symlink_components(path: Path) -> None:
    path = _io_path(path)
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise ValueError("Managed paths must not traverse symbolic links.")
        current = current.parent


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _mkdir_private(path: Path) -> None:
    path = _io_path(path)
    if path.is_symlink():
        raise ValueError("Managed runtime directories must not be symbolic links.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("Managed runtime path must be a directory.")
    if os.name != "nt":
        path.chmod(0o700)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path = _io_path(path)
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


def _file_sha256(path: Path) -> str:
    path = _io_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


__all__ = [
    "CompanionRuntimeAdapter",
    "CompanionRuntimeLayout",
    "DISPOSABLE_HOME_MARKER",
    "RUNTIME_LAYOUT_STEP",
    "create_disposable_companion_marker",
    "resolve_current_release",
    "runtime_bundle_digest",
    "validate_disposable_companion_home",
    "verify_current_release",
]
