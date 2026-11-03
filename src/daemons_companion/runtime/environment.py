"""Disposable builder for a release-owned private Python environment."""

from __future__ import annotations

# Keep logical paths in plans; use the extended Win32 namespace only for I/O.
from daemons_companion.runtime.bootstrap import _source_io_path as _io_path

from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence
import uuid

from daemons_companion.setup.plan import PlanStep
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    _atomic_write,
    validate_companion_home,
    verify_current_release,
)


RUNTIME_ENVIRONMENT_STEP = "runtime-environment"
SUPPORTED_UV_VERSION = "0.12.16"
MANAGED_PYTHON_REQUEST = "3.12"
_LOCK_FILE = "requirements-runtime.lock"
_ENVIRONMENT_MANIFEST = ".daemon-harness-environment.json"
_ENVIRONMENT_SCHEMA = "daemon.companion-environment.v1"
_SNAPSHOT_SCHEMA = "daemon.companion-environment-snapshot.v1"
_PROGRESS_SCHEMA = "daemon.companion-environment-progress.v1"
# Windows reads the stdlib before our long-path import hook exists, so the
# base Python takes this short name instead of uv's 36-character one.
_WINDOWS_PYTHON_HOME = "py"
_SAFE_ENVIRONMENT_KEYS = frozenset({
    "COMSPEC",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
    "https_proxy",
    "http_proxy",
    "no_proxy",
})


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Mapping[str, str]], CommandResult]


@dataclass(frozen=True)
class CompanionRuntimeEnvironmentLayout:
    runtime: CompanionRuntimeLayout
    uv_executable: Path
    uv_sha256: str
    target_os: str

    @property
    def environments_dir(self) -> Path:
        return self.runtime.runtime_root / "environments"

    def environment_path(self, release_id: str) -> Path:
        legacy = self.environments_dir / release_id
        if _io_path(legacy).exists() or _io_path(legacy).is_symlink():
            return legacy
        return self.environments_dir / environment_directory_name(release_id, self.target_os)

    def staging_path(self, release_id: str, *, compact: bool = True) -> Path:
        name = (f".env-{release_id[:16]}" if compact and self.target_os == "windows"
                else f".environment-{release_id}")
        return self.runtime.runtime_root / name


def environment_directory_name(release_id: str, target_os: str) -> str:
    """Short Windows paths; the manifest still verifies the full SHA-256."""
    if not _valid_digest(release_id):
        raise ValueError("Runtime release digest is invalid.")
    return release_id[:16] if target_os == "windows" else release_id


class CompanionRuntimeEnvironmentAdapter:
    """Build one relocatable, release-owned environment without host Python."""

    def __init__(
        self,
        layout: CompanionRuntimeEnvironmentLayout,
        *,
        runner: CommandRunner | None = None,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.layout = layout
        self._runner = runner or _subprocess_runner
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        release = self._validate_inputs()
        _validate_snapshot_dir(snapshot_dir)
        release_id = release.name
        final = self.layout.environment_path(release_id)
        stage = self.layout.staging_path(release_id)
        if stage.exists() or stage.is_symlink() or _is_junction(stage):
            raise RuntimeError("Runtime environment staging path is already occupied.")
        if self.layout.target_os == "windows" and os.name == "nt":
            for root in (stage, final):
                _validate_windows_startup_path(root)
        lock = release / _LOCK_FILE
        before = {
            "environments_dir": _path_descriptor(self.layout.environments_dir),
            "final": _path_descriptor(final, recursive=True),
            "stage": {"kind": "absent"},
        }
        if before["final"]["kind"] != "absent":
            _verify_environment(
                final,
                release_id=release_id,
                lock_sha256=_file_sha256(lock),
                target_os=self.layout.target_os,
            )
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "compact_paths": self.layout.target_os == "windows",
            "release_id": release_id,
            "lock_sha256": _file_sha256(lock),
            "uv_sha256": self.layout.uv_sha256,
            "uv_version": SUPPORTED_UV_VERSION,
            "python_request": MANAGED_PYTHON_REQUEST,
            "target_os": self.layout.target_os,
            "owner_token": uuid.uuid4().hex,
            "before": before,
        }
        _atomic_json_write(snapshot_dir / "manifest.json", manifest)
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        release = self._validate_inputs()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Runtime environment snapshot is required before apply.")
        manifest = _load_snapshot(snapshot_dir)
        self._validate_snapshot_against_inputs(manifest, release)
        release_id = str(manifest["release_id"])
        final = (self.layout.environment_path(release_id) if manifest.get("compact_paths") is True
                 else self.layout.environments_dir / release_id)
        stage = self.layout.staging_path(release_id, compact=manifest.get("compact_paths") is True)
        if self._state(final, stage) != manifest["before"]:
            raise RuntimeError("Runtime environment changed after its snapshot.")
        if manifest["before"]["final"]["kind"] != "absent":
            return

        self.layout.environments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage.mkdir(mode=0o700)
        _atomic_json_write(
            stage / ".owner.json",
            {"schema": _PROGRESS_SCHEMA, "owner_token": manifest["owner_token"]},
        )
        self._record_progress(snapshot_dir, stage, "stage")

        command_env = _command_environment(stage)
        uv = str(self.layout.uv_executable)
        self._run_stage_command(
            snapshot_dir,
            stage,
            "python",
            [
                uv,
                "python",
                "install",
                MANAGED_PYTHON_REQUEST,
                "--install-dir",
                str(stage / "python"),
                "--managed-python",
                "--no-bin",
                "--no-registry",
                "--no-config",
                "--no-progress",
            ],
            command_env,
        )
        python_request = MANAGED_PYTHON_REQUEST
        if self.layout.target_os == "windows":
            # uv's minor-version alias is a Windows junction, not a symlink.
            # A release-owned venv must use its direct, short-named directory.
            try:
                python_request = str(_direct_windows_python(stage / "python"))
            finally:
                self._record_progress(snapshot_dir, stage, "python-aliases")
        self._run_stage_command(
            snapshot_dir,
            stage,
            "venv",
            [
                uv,
                "venv",
                str(stage / "venv"),
                "--python",
                python_request,
                "--managed-python",
                "--no-python-downloads",
                "--no-project",
                "--relocatable",
                "--no-config",
                "--no-progress",
            ],
            command_env,
        )
        if self.layout.target_os == "windows":
            try:
                _install_windows_import_paths(stage)
            finally:
                self._record_progress(snapshot_dir, stage, "import-paths")
            self._mutation_hook("import-paths")
        interpreter = _interpreter(stage / "venv", self.layout.target_os)
        self._run_stage_command(
            snapshot_dir,
            stage,
            "sync",
            [
                uv,
                "pip",
                "sync",
                str(release / _LOCK_FILE),
                "--python",
                str(interpreter),
                "--require-hashes",
                "--strict",
                "--only-binary",
                ":all:",
                "--no-python-downloads",
                "--no-config",
                "--no-progress",
            ],
            command_env,
        )
        self._run_stage_command(
            snapshot_dir,
            stage,
            "verified",
            [
                str(_io_path(interpreter)),
                "-I",
                "-B",
                "-c",
                (
                    "import importlib.metadata._collections, sqlite3, ssl, fastapi, openai, pydantic, uvicorn, yaml; "
                    "print('daemon-runtime-ok')"
                ),
            ],
            _python_environment(),
            expected_stdout="daemon-runtime-ok",
        )

        if self.layout.target_os == "windows":
            try:
                _relocate_windows_python_home(stage, final, Path(python_request))
            finally:
                self._record_progress(snapshot_dir, stage, "python-home")
            self._mutation_hook("python-home")
        _relocate_internal_links(stage)
        _remove_cache(stage / "cache")
        (stage / ".owner.json").unlink()
        environment_manifest = {
            "schema": _ENVIRONMENT_SCHEMA,
            "release_id": release_id,
            "lock_sha256": manifest["lock_sha256"],
            "uv_version": SUPPORTED_UV_VERSION,
            "uv_sha256": manifest["uv_sha256"],
            "python_request": MANAGED_PYTHON_REQUEST,
            "target_os": self.layout.target_os,
            "owner_token": manifest["owner_token"],
            "tree_sha256": _tree_digest(stage, exclude_environment_manifest=True),
        }
        _atomic_json_write(stage / _ENVIRONMENT_MANIFEST, environment_manifest)
        _verify_environment(
            stage,
            release_id=release_id,
            lock_sha256=str(manifest["lock_sha256"]),
            target_os=self.layout.target_os,
        )
        self._record_progress(snapshot_dir, stage, "ready")
        os.replace(_io_path(stage), _io_path(final))
        self._record_progress(snapshot_dir, final, "published", published=True)
        self._mutation_hook("published")
        _verify_internal_links(final)
        self._run_stage_command(
            snapshot_dir, final, "published-verified",
            [str(_io_path(_interpreter(final / "venv", self.layout.target_os))), "-I", "-B", "-c",
             "import encodings, importlib.metadata._collections, sqlite3, ssl, fastapi, openai, pydantic, uvicorn, yaml; print('daemon-runtime-ok')"],
            _python_environment(), expected_stdout="daemon-runtime-ok", published=True,
        )
        _verify_environment(final, release_id=release_id,
                            lock_sha256=str(manifest["lock_sha256"]), target_os=self.layout.target_os)

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        release = self._validate_inputs()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Runtime environment snapshot is required before verify.")
        manifest = _load_snapshot(snapshot_dir)
        self._validate_snapshot_against_inputs(manifest, release)
        _verify_environment(
            self.layout.environment_path(release.name),
            release_id=release.name,
            lock_sha256=str(manifest["lock_sha256"]),
            target_os=self.layout.target_os,
        )

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        validate_companion_home(self.layout.runtime.companion_home, self.layout.runtime.installation_scope)
        manifest = _load_snapshot(snapshot_dir)
        release_id = str(manifest["release_id"])
        final = (self.layout.environment_path(release_id) if manifest.get("compact_paths") is True
                 else self.layout.environments_dir / release_id)
        stage = self.layout.staging_path(release_id, compact=manifest.get("compact_paths") is True)
        before = manifest["before"]
        progress = _load_progress(snapshot_dir)

        conflicts: list[str] = []
        if before["final"]["kind"] == "absent":
            if final.exists() or final.is_symlink():
                expected = progress if progress and progress.get("published") else None
                if expected is None or _path_descriptor(final, recursive=True) != expected["tree"]:
                    conflicts.append("published environment")
        elif _path_descriptor(final, recursive=True) != before["final"]:
            conflicts.append("existing environment")
        if stage.exists() or stage.is_symlink():
            expected = progress if progress and not progress.get("published") else None
            if expected is None or _path_descriptor(stage, recursive=True) != expected["tree"]:
                conflicts.append("staging environment")
        if conflicts:
            raise RuntimeError(
                "Runtime environment has concurrent changes; rollback was refused: "
                + ", ".join(conflicts)
            )

        if before["final"]["kind"] == "absent" and final.exists():
            _remove_tree(final)
        if stage.exists():
            _remove_tree(stage)
        if (
            before["environments_dir"]["kind"] == "absent"
            and self.layout.environments_dir.exists()
        ):
            self.layout.environments_dir.rmdir()

    def _run_stage_command(
        self,
        snapshot_dir: Path,
        stage: Path,
        boundary: str,
        command: list[str],
        environment: Mapping[str, str],
        *,
        expected_stdout: str | None = None,
        published: bool = False,
    ) -> None:
        error: BaseException | None = None
        result: CommandResult | None = None
        try:
            result = self._runner(command, environment)
            if result.returncode != 0:
                raise RuntimeError(f"Private runtime command failed at {boundary}.")
            if expected_stdout is not None and result.stdout.strip() != expected_stdout:
                raise RuntimeError("Private runtime verification returned unexpected output.")
        except BaseException as caught:
            error = caught
        finally:
            if stage.exists() and stage.is_dir():
                self._record_progress(snapshot_dir, stage, boundary, published=published)
        if error is not None:
            raise error
        self._mutation_hook(boundary)

    def _record_progress(
        self,
        snapshot_dir: Path,
        tree: Path,
        boundary: str,
        *,
        published: bool = False,
    ) -> None:
        _atomic_json_write(
            snapshot_dir / "progress.json",
            {
                "schema": _PROGRESS_SCHEMA,
                "boundary": boundary,
                "published": published,
                "tree": _path_descriptor(tree, recursive=True),
            },
        )

    def _validate_inputs(self) -> Path:
        validate_companion_home(self.layout.runtime.companion_home, self.layout.runtime.installation_scope)
        if self.layout.target_os not in {"macos", "windows"}:
            raise ValueError("Target operating system must be macos or windows.")
        uv = self.layout.uv_executable
        if not uv.is_absolute() or uv.is_symlink() or not uv.is_file():
            raise ValueError("uv executable must be an absolute regular non-symlink file.")
        if self.layout.target_os == "macos" and not stat.S_IMODE(uv.stat().st_mode) & 0o111:
            raise ValueError("uv executable is not executable.")
        if not _valid_digest(self.layout.uv_sha256):
            raise ValueError("uv SHA-256 is invalid.")
        if _file_sha256(uv) != self.layout.uv_sha256:
            raise RuntimeError("uv executable does not match its pinned SHA-256.")
        version = self._runner([str(uv), "--version"], _version_environment())
        if version.returncode != 0 or not version.stdout.startswith(
            f"uv {SUPPORTED_UV_VERSION} "
        ):
            raise RuntimeError("uv executable version is not supported.")
        release = verify_current_release(self.layout.runtime)
        lock = release / _LOCK_FILE
        if lock.is_symlink() or not lock.is_file():
            raise RuntimeError("Runtime dependency lock is unavailable.")
        if "--hash=sha256:" not in lock.read_text(encoding="utf-8"):
            raise RuntimeError("Runtime dependency lock is not hash-locked.")
        return release

    def _validate_snapshot_against_inputs(
        self,
        manifest: dict[str, object],
        release: Path,
    ) -> None:
        expected = {
            "release_id": release.name,
            "lock_sha256": _file_sha256(release / _LOCK_FILE),
            "uv_sha256": self.layout.uv_sha256,
            "uv_version": SUPPORTED_UV_VERSION,
            "python_request": MANAGED_PYTHON_REQUEST,
            "target_os": self.layout.target_os,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Runtime environment inputs changed after their snapshot.")

    def _state(self, final: Path, stage: Path) -> dict[str, object]:
        return {
            "environments_dir": _path_descriptor(self.layout.environments_dir),
            "final": _path_descriptor(final, recursive=True),
            "stage": _path_descriptor(stage, recursive=True),
        }

    @staticmethod
    def _validate_step(step: PlanStep) -> None:
        if step.code != RUNTIME_ENVIRONMENT_STEP:
            raise ValueError(
                "Runtime environment adapter accepts only the runtime-environment step."
            )


def _subprocess_runner(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> CommandResult:
    result = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env=dict(environment),
        timeout=900,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _command_environment(stage: Path) -> dict[str, str]:
    environment = _clean_environment()
    environment.update({
        "UV_CACHE_DIR": str(stage / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(stage / "python"),
        "UV_PYTHON_INSTALL_BIN": "0",
        "UV_PYTHON_NO_REGISTRY": "1",
    })
    return environment


def _version_environment() -> dict[str, str]:
    return _clean_environment()


def _python_environment() -> dict[str, str]:
    return _clean_environment()


def _clean_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENVIRONMENT_KEYS
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _interpreter(venv: Path, target_os: str) -> Path:
    return venv / ("Scripts/python.exe" if target_os == "windows" else "bin/python")


def resolve_release_environment(layout: CompanionRuntimeEnvironmentLayout) -> Path:
    """Return the verified private environment for the active release."""

    release = verify_current_release(layout.runtime)
    lock = release / _LOCK_FILE
    root = layout.environment_path(release.name)
    _verify_environment(
        root,
        release_id=release.name,
        lock_sha256=_file_sha256(lock),
        target_os=layout.target_os,
    )
    return root


def release_environment_interpreter(
    environment_root: Path,
    target_os: str,
) -> Path:
    """Return the platform interpreter path inside a verified environment."""

    if target_os not in {"macos", "windows"}:
        raise ValueError("Target operating system must be macos or windows.")
    return _interpreter(environment_root / "venv", target_os)


def _verify_environment(
    root: Path,
    *,
    release_id: str,
    lock_sha256: str,
    target_os: str,
) -> None:
    root = _io_path(root)
    if root.is_symlink() or _is_junction(root) or not root.is_dir():
        raise RuntimeError("Private runtime environment is unavailable.")
    manifest_path = root / _ENVIRONMENT_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Private runtime environment manifest is unavailable.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Private runtime environment manifest is invalid.") from error
    expected = {
        "schema": _ENVIRONMENT_SCHEMA,
        "release_id": release_id,
        "lock_sha256": lock_sha256,
        "uv_version": SUPPORTED_UV_VERSION,
        "python_request": MANAGED_PYTHON_REQUEST,
        "target_os": target_os,
    }
    if not isinstance(manifest, dict) or any(
        manifest.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("Private runtime environment manifest is inconsistent.")
    if not _valid_digest(manifest.get("uv_sha256")) or not isinstance(
        manifest.get("owner_token"), str
    ):
        raise RuntimeError("Private runtime environment manifest is invalid.")
    if manifest.get("tree_sha256") != _tree_digest(
        root,
        exclude_environment_manifest=True,
    ):
        raise RuntimeError("Private runtime environment content changed.")
    interpreter = _interpreter(root / "venv", target_os)
    if interpreter.is_symlink() and not interpreter.exists():
        raise RuntimeError("Private runtime interpreter link is broken.")
    if not interpreter.exists() or not interpreter.is_file():
        raise RuntimeError("Private runtime interpreter is unavailable.")
    _verify_internal_links(root)


def _verify_internal_links(root: Path) -> None:
    root = _io_path(root)
    resolved_root = root.resolve()
    for path in _walk_tree(root):
        if not (path.is_symlink() or _is_junction(path)):
            continue
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise RuntimeError("Private runtime contains an external or broken link.") from error


def _relocate_internal_links(root: Path) -> None:
    """Rewrite absolute in-tree links as relative links before atomic movement."""
    root = _io_path(root)

    resolved_root = root.resolve()
    for path in _walk_tree(root):
        if not (path.is_symlink() or _is_junction(path)):
            continue
        try:
            resolved_target = path.resolve(strict=True)
            target_relative = resolved_target.relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise RuntimeError("Private runtime contains an external or broken link.") from error
        if _is_junction(path):
            raise RuntimeError("Private runtime contains an unexpected directory junction before publication.")
        relative_target = os.path.relpath(root / target_relative, start=path.parent)
        path.unlink()
        path.symlink_to(relative_target, target_is_directory=resolved_target.is_dir())


def _install_windows_import_paths(stage: Path) -> None:
    """Retain extended paths for imports in this owned venv, including uv queries."""
    directory = _io_path(stage / "venv/Lib/site-packages")
    for folder in (directory.parent.parent, directory.parent, directory):
        if folder.is_symlink() or _is_junction(folder):
            raise RuntimeError("Private Windows site-packages must be a direct directory.")
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("windows_python_paths.py").read_bytes()
    for name, content in (
        ("_daemons_windows_paths.py", source),
        ("000_daemons_paths.pth", b"import _daemons_windows_paths; _daemons_windows_paths.enable()\n"),
    ):
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Private Windows import support already exists unexpectedly.")
        _atomic_write(destination, content, 0o600)


def _relocate_windows_python_home(stage: Path, final: Path, python: Path) -> None:
    """CPython's Windows venv launcher reads the absolute home in pyvenv.cfg.

    uv --relocatable relocates scripts, but leaves this base-Python path absolute.
    Point it at our final owned tree before sealing and publishing that tree.
    """
    relative_home = python.parent.relative_to(stage / "python")
    config = _io_path(stage / "venv/pyvenv.cfg")
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 65536:
        raise RuntimeError("Private Windows Python configuration is unavailable.")
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    homes = [(index, line.partition("=")[2].strip()) for index, line in enumerate(lines)
             if line.partition("=")[0].strip() == "home"]
    if len(homes) != 1:
        raise RuntimeError("Private Windows Python configuration must contain one home.")
    index, old_home = homes[0]
    if not Path(old_home).is_absolute() or _io_path(Path(old_home)).resolve(strict=True) != _io_path(python.parent).resolve(strict=True):
        raise RuntimeError("Private Windows Python home does not match its owned interpreter.")
    lines[index] = f"home = {final / 'python' / relative_home}\n"
    _atomic_write(config, "".join(lines).encode("utf-8"), stat.S_IMODE(config.stat().st_mode))


def _validate_windows_startup_path(root) -> None:
    probe = root / "python" / _WINDOWS_PYTHON_HOME / "Lib/encodings/__init__.py"
    if len(str(probe).encode("utf-16-le")) // 2 >= 250:
        raise ValueError("This Windows user folder path is too long for the daemon's private Python.")


def check_windows_installation_room(attempt: Path) -> None:
    """Refuse a new Windows installation before any download if Python could not start."""
    from daemons_companion.runtime.installation_scope import InstallationScope

    layout = CompanionRuntimeEnvironmentLayout(
        CompanionRuntimeLayout(InstallationScope(attempt).home, attempt), attempt, "0" * 64, "windows",
    )
    release_id = "f" * 64  # Only the length of a release digest matters here.
    try:
        for root in (layout.staging_path(release_id),
                     layout.environments_dir / environment_directory_name(release_id, "windows")):
            _validate_windows_startup_path(root)
    except ValueError as error:
        raise ValueError(f"{error} Nothing was installed.") from None


def _is_junction(path: Path) -> bool:
    check = getattr(path, "is_junction", None)
    if check is not None:
        return bool(check())
    # The installer uses Python 3.12; retain safe behavior for development on 3.11.
    try:
        return getattr(path.lstat(), "st_reparse_tag", None) == 0xA0000003
    except FileNotFoundError:
        return False


def _walk_tree(root: Path):
    """Enumerate owned entries without descending through any directory link."""
    pending = [root]
    while pending:
        for entry in pending.pop().iterdir():
            yield entry
            if not (entry.is_symlink() or _is_junction(entry)) and entry.is_dir():
                pending.append(entry)


def _direct_windows_python(root: Path) -> Path:
    logical_root = root
    root = _io_path(root)
    children = list(root.iterdir())
    candidates = [p for p in children
                  if re.fullmatch(r"cpython-3\.12\.\d+-windows-[a-zA-Z0-9_]+-none", p.name)
                  and not p.is_symlink() and not _is_junction(p) and (p / "python.exe").is_file()]
    if len(candidates) != 1:
        raise RuntimeError("Expected one direct private Windows Python installation.")
    chosen = candidates[0]
    aliases = [p for p in children if _is_junction(p) or p.is_symlink()]
    if any(p.resolve(strict=True) != chosen.resolve(strict=True) for p in aliases):
        raise RuntimeError("Managed Python alias points outside its private installation.")
    for alias in aliases:
        if _is_junction(alias):
            alias.rmdir()
        else:
            alias.unlink()
    # uv accepts this explicitly requested interpreter as the venv base.
    short = root / _WINDOWS_PYTHON_HOME
    if short.exists() or short.is_symlink():
        raise RuntimeError("Private Windows Python home is already occupied.")
    os.rename(chosen, short)
    return logical_root / _WINDOWS_PYTHON_HOME / "python.exe"


def _tree_digest(root: Path, *, exclude_environment_manifest: bool = False) -> str:
    root = _io_path(root)
    digest = hashlib.sha256()
    for path in sorted(_walk_tree(root), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if exclude_environment_manifest and relative == _ENVIRONMENT_MANIFEST:
            continue
        if _is_junction(path):
            digest.update(f"j\0{relative}\0{os.readlink(path)}\0".encode("utf-8"))
        elif path.is_symlink():
            digest.update(f"l\0{relative}\0{os.readlink(path)}\0".encode("utf-8"))
        elif path.is_dir():
            digest.update(f"d\0{relative}\0".encode("utf-8"))
        elif path.is_file():
            digest.update(
                f"f\0{relative}\0{path.stat().st_size}\0{_file_sha256(path)}\0".encode(
                    "utf-8"
                )
            )
        else:
            raise RuntimeError("Private runtime contains a special file.")
    return digest.hexdigest()


def _path_descriptor(path: Path, *, recursive: bool = False) -> dict[str, object]:
    path = _io_path(path)
    if _is_junction(path):
        return {"kind": "junction", "target": os.readlink(path)}
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    if path.is_file():
        return {
            "kind": "file",
            "size": path.stat().st_size,
            "sha256": _file_sha256(path),
        }
    if path.is_dir():
        result: dict[str, object] = {"kind": "directory"}
        if recursive:
            result["tree_sha256"] = _tree_digest(path)
        return result
    return {"kind": "special"}


def _remove_cache(path: Path) -> None:
    path = _io_path(path)
    if path.exists() or path.is_symlink():
        _remove_tree(path)


def _remove_tree(path: Path) -> None:
    path = _io_path(path)
    if path.is_symlink() or _is_junction(path) or not path.is_dir():
        raise RuntimeError("Owned runtime tree is not a direct directory.")
    for child in sorted(_walk_tree(path), key=lambda item: len(item.parts), reverse=True):
        if _is_junction(child):
            child.rmdir()  # Remove only the reparse point, never its target.
        elif child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
        else:
            raise RuntimeError("Owned runtime tree contains a special file.")
    path.rmdir()


def _load_snapshot(path: Path) -> dict[str, object]:
    path = _io_path(path)
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Runtime environment snapshot is unavailable.")
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or loaded.get("schema") != _SNAPSHOT_SCHEMA:
        raise RuntimeError("Runtime environment snapshot is invalid.")
    if not _valid_digest(loaded.get("release_id")) or not _valid_digest(
        loaded.get("lock_sha256")
    ):
        raise RuntimeError("Runtime environment snapshot is invalid.")
    return loaded


def _load_progress(path: Path) -> dict[str, object] | None:
    path = _io_path(path)
    progress_path = path / "progress.json"
    if not progress_path.exists():
        return None
    if progress_path.is_symlink() or not progress_path.is_file():
        raise RuntimeError("Runtime environment progress is invalid.")
    loaded = json.loads(progress_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or loaded.get("schema") != _PROGRESS_SCHEMA:
        raise RuntimeError("Runtime environment progress is invalid.")
    return loaded


def _validate_snapshot_dir(path: Path) -> None:
    if not path.is_absolute() or _io_path(path).is_symlink() or not _io_path(path).is_dir():
        raise ValueError("Runtime environment snapshot directory must be direct and absolute.")


def _atomic_json_write(path: Path, value: object) -> None:
    path = _io_path(path)
    data = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
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


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "CommandResult",
    "CompanionRuntimeEnvironmentAdapter",
    "CompanionRuntimeEnvironmentLayout",
    "MANAGED_PYTHON_REQUEST",
    "RUNTIME_ENVIRONMENT_STEP",
    "SUPPORTED_UV_VERSION",
    "check_windows_installation_room",
    "release_environment_interpreter",
    "resolve_release_environment",
]
