"""Disposable-only OpenClaw profile operation for the companion app."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping
from urllib.parse import urlsplit

# Release files sit deeper than Windows MAX_PATH; read them through the
# extended namespace while configuration keeps their logical paths.
from daemons_companion.runtime.bootstrap import _source_io_path as _io_path
from daemons_companion.setup.plan import HostKind, PlanStep
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    validate_companion_home,
    verify_current_release,
)


OPENCLAW_PROFILE_STEP = "openclaw-profile"
DISPOSABLE_OPENCLAW_MARKER = ".daemon-harness-disposable-profile"
_PROFILE_MARKER_CONTENT = "daemon-harness disposable OpenClaw profile v1\n"
_WORKSPACE_MARKER = ".daemon-harness-managed-workspace"
_WORKSPACE_MARKER_CONTENT = "daemon-harness managed OpenClaw workspace v1\n"
_SNAPSHOT_SCHEMA = "daemon.openclaw-profile-snapshot.v1"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MANAGED_TOOLS = ("daemon_read_souvenirs", "daemon_recall_l0", "browser")
_DENIED_TOOLS = ("memory_search", "memory_get")


@dataclass(frozen=True)
class OpenClawProfileLayout:
    runtime: CompanionRuntimeLayout
    openclaw_state: Path

    @property
    def config_path(self) -> Path:
        return self.openclaw_state / "openclaw.json"

    @property
    def workspace(self) -> Path:
        return self.openclaw_state / "workspace-daemon-harness"

    @property
    def workspace_marker(self) -> Path:
        return self.workspace / _WORKSPACE_MARKER

    @property
    def workspace_agents(self) -> Path:
        return self.workspace / "AGENTS.md"


class OpenClawProfileAdapter:
    """Apply one reversible OpenClaw profile projection to disposable state."""

    def __init__(
        self,
        layout: OpenClawProfileLayout,
        *,
        selected_host: HostKind,
        user_id: str,
        daemon_url: str = "http://127.0.0.1:8000",
    ) -> None:
        if selected_host != HostKind.OPENCLAW:
            raise ValueError("OpenClaw profile adapter requires the OpenClaw host.")
        self.layout = layout
        self.user_id = _bounded_text(user_id, "OpenClaw user id", 128)
        _validate_daemon_url(daemon_url)
        self.daemon_url = daemon_url
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        release = self._validate_layout()
        _validate_snapshot_directory(snapshot_dir)
        before = self._descriptors()
        config_before = _read_regular_bytes(self.layout.config_path)
        config_expected = _render_config(
            config_before,
            plugin_source=release / "integrations" / "openclaw_plugin",
            workspace=self.layout.workspace,
            user_id=self.user_id,
            daemon_url=self.daemon_url,
            runtime_health_path=self.layout.runtime.state_root
            / "openclaw-runtime-health.json",
        )
        agents_expected = self._canonical_agents(release)
        expected = {
            "config": _file_descriptor(
                config_expected,
                _desired_mode(before["config"]),
            ),
            "workspace": {
                "kind": "directory",
                "mode": _desired_directory_mode(before["workspace"]),
                "entries": sorted({
                    *before["workspace"].get("entries", []),
                    _WORKSPACE_MARKER,
                    "AGENTS.md",
                }),
            },
            "workspace_marker": _file_descriptor(
                _WORKSPACE_MARKER_CONTENT.encode("utf-8"),
                0o600,
            ),
            "workspace_agents": _file_descriptor(
                agents_expected,
                _desired_mode(before["workspace_agents"]),
            ),
        }
        for name, path in (
            ("config", self.layout.config_path),
            ("workspace_marker", self.layout.workspace_marker),
            ("workspace_agents", self.layout.workspace_agents),
        ):
            content = _read_regular_bytes(path)
            if content is not None:
                _write_private_bytes(snapshot_dir / f"{name}.before", content, 0o600)
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "release": str(release),
            "before": before,
            "expected": expected,
        }
        _write_private_bytes(
            snapshot_dir / "manifest.json",
            _json_bytes(manifest),
            0o600,
        )
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        release = self._validate_layout()
        snapshot = self._snapshot_dirs.get(step.code)
        if snapshot is None:
            raise RuntimeError("OpenClaw profile snapshot is required before apply.")
        manifest = _load_manifest(snapshot)
        if manifest["release"] != str(release):
            raise RuntimeError("OpenClaw runtime release changed after its snapshot.")
        if self._descriptors() != manifest["before"]:
            raise RuntimeError("OpenClaw profile changed after its snapshot.")

        self.layout.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.layout.workspace.chmod(_desired_directory_mode(manifest["before"]["workspace"]))
        self._after_mutation("workspace")

        _write_private_bytes(
            self.layout.workspace_marker,
            _WORKSPACE_MARKER_CONTENT.encode("utf-8"),
            0o600,
        )
        self._after_mutation("workspace_marker")

        _write_private_bytes(
            self.layout.workspace_agents,
            self._canonical_agents(release),
            _desired_mode(manifest["before"]["workspace_agents"]),
        )
        self._after_mutation("workspace_agents")

        config_before = _read_regular_bytes(self.layout.config_path)
        config_expected = _render_config(
            config_before,
            plugin_source=release / "integrations" / "openclaw_plugin",
            workspace=self.layout.workspace,
            user_id=self.user_id,
            daemon_url=self.daemon_url,
            runtime_health_path=self.layout.runtime.state_root
            / "openclaw-runtime-health.json",
        )
        _write_private_bytes(
            self.layout.config_path,
            config_expected,
            _desired_mode(manifest["before"]["config"]),
        )
        self._after_mutation("config")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        release = self._validate_layout()
        snapshot = self._snapshot_dirs.get(step.code)
        if snapshot is None:
            raise RuntimeError("OpenClaw profile snapshot is required before verify.")
        manifest = _load_manifest(snapshot)
        if manifest["release"] != str(release):
            raise RuntimeError("OpenClaw runtime release changed before verification.")
        if self._descriptors() != manifest["expected"]:
            raise RuntimeError("OpenClaw profile projection is incomplete.")
        config = _load_config(self.layout.config_path.read_bytes())
        _verify_effective_config(
            config,
            plugin_source=release / "integrations" / "openclaw_plugin",
            workspace=self.layout.workspace,
            user_id=self.user_id,
            daemon_url=self.daemon_url,
            runtime_health_path=self.layout.runtime.state_root
            / "openclaw-runtime-health.json",
        )

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        manifest = _load_manifest(snapshot_dir)
        before = manifest["before"]
        expected = manifest["expected"]
        current = self._descriptors()
        conflicts = [
            name
            for name in before
            if not _rollback_state_is_safe(name, current[name], before[name], expected[name])
        ]
        if conflicts:
            raise RuntimeError(
                "OpenClaw profile has concurrent changes; rollback was refused."
            )

        _restore_file(
            self.layout.config_path,
            before["config"],
            snapshot_dir / "config.before",
        )
        _restore_file(
            self.layout.workspace_agents,
            before["workspace_agents"],
            snapshot_dir / "workspace_agents.before",
        )
        _restore_file(
            self.layout.workspace_marker,
            before["workspace_marker"],
            snapshot_dir / "workspace_marker.before",
        )
        _restore_directory(self.layout.workspace, before["workspace"])

    def current(self) -> bool:
        """Whether the profile already holds this release's exact projection.

        A runtime update re-points a profile it installed itself; any other
        edit of the managed paths needs a separate migration.
        """
        release = self._validate_layout()
        config = _read_regular_bytes(self.layout.config_path)
        if config is None:
            return False
        expected = _render_config(
            config,
            plugin_source=release / "integrations" / "openclaw_plugin",
            workspace=self.layout.workspace,
            user_id=self.user_id,
            daemon_url=self.daemon_url,
            runtime_health_path=self.layout.runtime.state_root
            / "openclaw-runtime-health.json",
        )
        return (
            expected == config
            and _read_regular_bytes(self.layout.workspace_marker)
            == _WORKSPACE_MARKER_CONTENT.encode("utf-8")
            and _read_regular_bytes(self.layout.workspace_agents)
            == self._canonical_agents(release)
        )

    def _canonical_agents(self, release: Path) -> bytes:
        source = release / "integrations" / "openclaw_plugin" / "workspace" / "AGENTS.md"
        content = _read_regular_bytes(_io_path(source))
        if content is None:
            raise ValueError("OpenClaw canonical workspace instructions are unavailable.")
        return content

    def _descriptors(self) -> dict[str, dict[str, object]]:
        return {
            "config": _path_descriptor(self.layout.config_path, allow_file=True),
            "workspace": _path_descriptor(self.layout.workspace, allow_directory=True),
            "workspace_marker": _path_descriptor(
                self.layout.workspace_marker,
                allow_file=True,
            ),
            "workspace_agents": _path_descriptor(
                self.layout.workspace_agents,
                allow_file=True,
            ),
        }

    def _validate_layout(self) -> Path:
        validate_companion_home(self.layout.runtime.companion_home, self.layout.runtime.installation_scope)
        release = verify_current_release(self.layout.runtime)
        validate_openclaw_state(self.layout.openclaw_state, self.layout.runtime.installation_scope)
        for path in (self.layout.config_path, self.layout.workspace):
            if path.is_symlink():
                raise ValueError("OpenClaw managed paths must not be symbolic links.")
        if self.layout.config_path.exists() and not self.layout.config_path.is_file():
            raise ValueError("OpenClaw config must be a regular file.")
        if self.layout.workspace.exists() and not self.layout.workspace.is_dir():
            raise ValueError("OpenClaw workspace must be a directory.")
        if self.layout.workspace.exists():
            marker_path = self.layout.workspace_marker
            if marker_path.exists() or marker_path.is_symlink():
                if (
                    marker_path.is_symlink()
                    or not marker_path.is_file()
                    or marker_path.read_text(encoding="utf-8") != _WORKSPACE_MARKER_CONTENT
                ):
                    raise ValueError("OpenClaw managed workspace marker is invalid.")
            elif any(self.layout.workspace.iterdir()):
                raise ValueError("Existing OpenClaw workspace is not companion-owned.")
        for path in (self.layout.workspace_marker, self.layout.workspace_agents):
            if path.is_symlink() or path.exists() and not path.is_file():
                raise ValueError("OpenClaw workspace files must be regular files.")
        plugin = _io_path(release / "integrations" / "openclaw_plugin")
        if plugin.is_symlink() or not plugin.is_dir():
            raise ValueError("OpenClaw plugin source is unavailable in the active release.")
        self._canonical_agents(release)
        return release

    @staticmethod
    def _validate_step(step: PlanStep) -> None:
        if step.code != OPENCLAW_PROFILE_STEP:
            raise ValueError("OpenClaw profile adapter only accepts openclaw-profile.")

    def _after_mutation(self, _artifact: str) -> None:
        """Test seam invoked after every managed artifact mutation."""


def create_disposable_openclaw_marker(openclaw_state: Path) -> None:
    """Create the explicit test-only profile gate."""

    state = _absolute_direct(openclaw_state, "Disposable OpenClaw state")
    if state.exists() and not state.is_dir():
        raise ValueError("Disposable OpenClaw state must be a directory.")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        state.chmod(0o700)
    marker = state / DISPOSABLE_OPENCLAW_MARKER
    if marker.exists() or marker.is_symlink():
        if (
            marker.is_symlink()
            or not marker.is_file()
            or marker.read_text(encoding="utf-8") != _PROFILE_MARKER_CONTENT
        ):
            raise ValueError("Disposable OpenClaw marker is invalid.")
        return
    _write_private_bytes(marker, _PROFILE_MARKER_CONTENT.encode("utf-8"), 0o600)


def validate_disposable_openclaw_state(openclaw_state: Path) -> Path:
    """Return a direct disposable OpenClaw state root or fail closed."""

    state = _absolute_direct(openclaw_state, "OpenClaw state")
    marker = state / DISPOSABLE_OPENCLAW_MARKER
    if (
        not state.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != _PROFILE_MARKER_CONTENT
    ):
        raise ValueError("OpenClaw profile is not marked as disposable.")
    return state


def _validate_daemon_url(daemon_url: str) -> None:
    try:
        parsed = urlsplit(daemon_url)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("OpenClaw companion API URL is invalid.") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1024 <= port <= 65535
        or daemon_url != f"http://127.0.0.1:{port}"
    ):
        raise ValueError("OpenClaw companion API must use an explicit loopback port.")


def _render_config(
    content: bytes | None,
    *,
    plugin_source: Path,
    workspace: Path,
    user_id: str,
    daemon_url: str,
    runtime_health_path: Path,
) -> bytes:
    config = _load_config(content or b"{}\n")
    newline = "\r\n" if content and b"\r\n" in content else "\n"
    indent = _detect_indent(content)
    projected = _project_config(
        config,
        plugin_source=plugin_source,
        workspace=workspace,
        user_id=user_id,
        daemon_url=daemon_url,
        runtime_health_path=runtime_health_path,
    )
    rendered = json.dumps(projected, ensure_ascii=False, indent=indent)
    return (rendered.replace("\n", newline) + newline).encode("utf-8")


def _project_config(
    source: Mapping[str, object],
    *,
    plugin_source: Path,
    workspace: Path,
    user_id: str,
    daemon_url: str,
    runtime_health_path: Path,
) -> dict[str, object]:
    config = deepcopy(dict(source))
    _set_path(config, ("agents", "defaults", "workspace"), str(workspace))
    _set_path(config, ("agents", "defaults", "skipBootstrap"), True)
    _set_path(
        config,
        ("hooks", "internal", "entries", "session-memory", "enabled"),
        False,
    )
    _set_path(config, ("plugins", "entries", "memory-core", "enabled"), False)
    _set_path(config, ("skills", "workshop", "autonomous", "mode"), "off")
    _set_path(config, ("plugins", "entries", "browser", "enabled"), True)
    if _get_path(config, ("browser", "headless"), _MISSING) is _MISSING:
        _set_path(config, ("browser", "headless"), True)
    _append_unique(config, ("tools", "deny"), _DENIED_TOOLS)
    _append_unique(config, ("tools", "alsoAllow"), _MANAGED_TOOLS)
    if _get_path(config, ("plugins", "allow"), _MISSING) is not _MISSING:
        _append_unique(config, ("plugins", "allow"), ("browser",))
    codex_entry = _get_path(config, ("plugins", "entries", "codex"), _MISSING)
    if codex_entry is not _MISSING:
        if not isinstance(codex_entry, dict):
            raise ValueError("OpenClaw Codex plugin entry must be an object.")
        _set_path(
            config,
            ("plugins", "entries", "codex", "config", "codexDynamicToolsLoading"),
            "searchable",
        )
    _drop_other_releases(config, plugin_source)
    _append_unique(config, ("plugins", "load", "paths"), (str(plugin_source),))
    _set_path(config, ("plugins", "slots", "contextEngine"), "daemon-harness")
    _set_path(config, ("plugins", "entries", "daemon-harness", "enabled"), True)
    _set_path(
        config,
        (
            "plugins",
            "entries",
            "daemon-harness",
            "hooks",
            "allowConversationAccess",
        ),
        True,
    )
    _set_path(
        config,
        ("plugins", "entries", "daemon-harness", "config", "daemonUrl"),
        daemon_url,
    )
    _set_path(
        config,
        ("plugins", "entries", "daemon-harness", "config", "userId"),
        user_id,
    )
    _set_path(
        config,
        (
            "plugins",
            "entries",
            "daemon-harness",
            "config",
            "runtimeHealthPath",
        ),
        str(runtime_health_path),
    )
    return config


def _verify_effective_config(
    config: Mapping[str, object],
    *,
    plugin_source: Path,
    workspace: Path,
    user_id: str,
    daemon_url: str,
    runtime_health_path: Path,
) -> None:
    expected = _project_config(
        config,
        plugin_source=plugin_source,
        workspace=workspace,
        user_id=user_id,
        daemon_url=daemon_url,
        runtime_health_path=runtime_health_path,
    )
    if dict(config) != expected:
        raise RuntimeError("OpenClaw effective configuration is incomplete.")


def _load_config(content: bytes) -> dict[str, object]:
    if len(content) > _MAX_CONFIG_BYTES:
        raise ValueError("OpenClaw config is too large.")
    try:
        text = content.decode("utf-8")
        loaded = json.loads(text, object_pairs_hook=_unique_object)
    except UnicodeDecodeError as error:
        raise ValueError("OpenClaw config must be UTF-8.") from error
    except json.JSONDecodeError as error:
        raise ValueError("OpenClaw config must be valid JSON.") from error
    if not isinstance(loaded, dict):
        raise ValueError("OpenClaw config must be a JSON object.")
    return loaded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"OpenClaw config contains duplicate key: {key}")
        result[key] = value
    return result


def _set_path(mapping: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = mapping
    for part in path[:-1]:
        existing = current.get(part)
        if existing is None:
            child: dict[str, object] = {}
            current[part] = child
            current = child
        elif isinstance(existing, dict):
            current = existing
        else:
            raise ValueError(f"OpenClaw config path is not an object: {'.'.join(path[:-1])}")
    current[path[-1]] = value


def _get_path(mapping: Mapping[str, object], path: tuple[str, ...], default: object) -> object:
    current: object = mapping
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _drop_other_releases(mapping: dict[str, object], plugin_source: Path) -> None:
    """A runtime update points the profile at its new release only.

    The previous release keeps its own plugin copy for rollback; loading both
    would register the daemon plugin twice. Paths outside this daemon's
    releases are the owner's and stay.
    """
    current = _get_path(mapping, ("plugins", "load", "paths"), _MISSING)
    if not isinstance(current, list):
        return
    releases = plugin_source.parents[2]

    def other_release(item: object) -> bool:
        if not isinstance(item, str) or item == str(plugin_source):
            return False
        path = Path(item)
        return (
            len(path.parents) > 2
            and path.parents[2] == releases
            and path.parent.name == "integrations"
            and path.name == "openclaw_plugin"
        )

    kept = [item for item in current if not other_release(item)]
    if kept != current:
        _set_path(mapping, ("plugins", "load", "paths"), kept)


def _append_unique(
    mapping: dict[str, object],
    path: tuple[str, ...],
    values: tuple[str, ...],
) -> None:
    current = _get_path(mapping, path, _MISSING)
    if current is _MISSING:
        _set_path(mapping, path, list(values))
        return
    if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
        raise ValueError(f"OpenClaw config path must be a string array: {'.'.join(path)}")
    result = list(current)
    for value in values:
        if value not in result:
            result.append(value)
    _set_path(mapping, path, result)


_MISSING = object()


def _detect_indent(content: bytes | None) -> int:
    if not content:
        return 2
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return 2
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if stripped.startswith('"') and len(line) > len(stripped):
            return min(max(len(line) - len(stripped), 1), 8)
    return 2


def _path_descriptor(
    path: Path,
    *,
    allow_file: bool = False,
    allow_directory: bool = False,
) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("OpenClaw managed path must not be a symbolic link.")
    if not path.exists():
        return {"kind": "missing"}
    if allow_file and path.is_file():
        return _file_descriptor(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    if allow_directory and path.is_dir():
        return {
            "kind": "directory",
            # Windows reports every directory as 0o777 and ignores chmod; the
            # private profile relies on its ACL instead, as for Hermes.
            "mode": 0o700 if os.name == "nt" else stat.S_IMODE(path.stat().st_mode),
            "entries": sorted(child.name for child in path.iterdir()),
        }
    raise ValueError("OpenClaw managed path has an unsupported type.")


def _file_descriptor(content: bytes, mode: int) -> dict[str, object]:
    return {
        "kind": "file",
        "sha256": hashlib.sha256(content).hexdigest(),
        # Windows files read 0o666 or 0o444: only the read-only bit is real.
        "mode": (0o600 if mode & 0o200 else 0o400) if os.name == "nt" else mode,
    }


def _read_regular_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError("OpenClaw managed files must not be symbolic links.")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("OpenClaw managed files must be regular files.")
    return path.read_bytes()


def _write_private_bytes(path: Path, content: bytes, mode: int) -> None:
    if path.parent.is_symlink():
        raise ValueError("OpenClaw managed parent must not be a symbolic link.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_file(path: Path, descriptor: Mapping[str, object], backup: Path) -> None:
    if descriptor["kind"] == "missing":
        if path.is_symlink():
            raise RuntimeError("OpenClaw rollback encountered a symbolic link.")
        path.unlink(missing_ok=True)
        return
    if descriptor["kind"] != "file" or backup.is_symlink() or not backup.is_file():
        raise ValueError("OpenClaw profile snapshot file is invalid.")
    content = backup.read_bytes()
    if hashlib.sha256(content).hexdigest() != descriptor.get("sha256"):
        raise ValueError("OpenClaw profile snapshot checksum is invalid.")
    _write_private_bytes(path, content, int(descriptor["mode"]))


def _restore_directory(path: Path, descriptor: Mapping[str, object]) -> None:
    if descriptor["kind"] == "missing":
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("OpenClaw workspace rollback encountered a conflict.")
        path.rmdir()
        return
    if descriptor["kind"] != "directory" or path.is_symlink() or not path.is_dir():
        raise ValueError("OpenClaw workspace snapshot is invalid.")
    if os.name != "nt":
        path.chmod(int(descriptor["mode"]))


def _rollback_state_is_safe(
    name: str,
    current: Mapping[str, object],
    before: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if current == before or current == expected:
        return True
    if name != "workspace" or current.get("kind") != "directory":
        return False
    if current.get("mode") != expected.get("mode"):
        return False
    before_entries = set(before.get("entries", [])) if before.get("kind") == "directory" else set()
    current_entries = set(current.get("entries", []))
    expected_entries = set(expected.get("entries", []))
    return before_entries.issubset(current_entries) and current_entries.issubset(expected_entries)


def _desired_mode(descriptor: Mapping[str, object]) -> int:
    return int(descriptor["mode"]) if descriptor.get("kind") == "file" else 0o600


def _desired_directory_mode(descriptor: Mapping[str, object]) -> int:
    return int(descriptor["mode"]) if descriptor.get("kind") == "directory" else 0o700


def _load_manifest(snapshot_dir: Path) -> dict[str, object]:
    _validate_snapshot_directory(snapshot_dir)
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("OpenClaw profile snapshot manifest is missing.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("OpenClaw profile snapshot manifest is invalid.") from error
    names = {"config", "workspace", "workspace_marker", "workspace_agents"}
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "release", "before", "expected"}
        or payload.get("schema") != _SNAPSHOT_SCHEMA
        or not isinstance(payload.get("release"), str)
        or not isinstance(payload.get("before"), dict)
        or set(payload["before"]) != names
        or not isinstance(payload.get("expected"), dict)
        or set(payload["expected"]) != names
    ):
        raise ValueError("OpenClaw profile snapshot manifest is invalid.")
    return payload


def _validate_snapshot_directory(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("OpenClaw snapshot directory must be absolute and direct.")


def _absolute_direct(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError(f"{label} must be an absolute direct path.")
    try:
        if path.exists() and path.resolve() != path:
            raise ValueError(f"{label} must not contain symbolic links.")
    except OSError as error:
        raise ValueError(f"{label} cannot be resolved.") from error
    return path


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError(f"{label} is invalid.")
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


__all__ = [
    "DISPOSABLE_OPENCLAW_MARKER",
    "OPENCLAW_PROFILE_STEP",
    "OpenClawProfileAdapter",
    "OpenClawProfileLayout",
    "create_disposable_openclaw_marker",
    "validate_disposable_openclaw_state",
]


def validate_openclaw_state(state, scope=None):
    if scope is None:
        return validate_disposable_openclaw_state(state)
    if scope.host_kind != "openclaw":
        raise ValueError("OpenClaw requires its own explicit installation scope.")
    return scope.validate_profile(state)
