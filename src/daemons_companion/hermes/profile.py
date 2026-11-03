"""Disposable-only Hermes profile operation for the future companion app."""

from __future__ import annotations

# Keep logical paths in plans; use the extended Win32 namespace only for I/O.
from daemons_companion.runtime.bootstrap import _source_io_path as _io_path, private_runtime_path_environment

from daemons_companion.runtime.installation_scope import InstallationScope

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import tempfile
from typing import Mapping

from daemons_companion.setup.plan import HostKind, PlanStep


HERMES_PROFILE_STEP = "hermes-profile"
DISPOSABLE_PROFILE_MARKER = ".daemon-harness-disposable-profile"
_MARKER_CONTENT = "daemon-harness disposable Hermes profile v1\n"

HERMES_CONFIG_VALUES: tuple[tuple[str, object], ...] = (
    ("memory.provider", "daemon_core"),
    ("memory.memory_enabled", False),
    ("memory.user_profile_enabled", False),
    ("skills.creation_nudge_interval", 0),
    ("auxiliary.background_review.enabled", False),
    ("display.memory_notifications", "off"),
    ("cron.wrap_response", False),
    ("display.platforms.telegram.tool_progress", "all"),
    ("display.platforms.telegram.cleanup_progress", True),
    ("display.busy_input_mode", "steer"),
    ("telegram.reply_to_mode", "off"),
    # Conversation settings of the qualified reference daemon. Searching with
    # a Hermes-run backend keeps tool progress visible and lets the voice
    # plugin clean its progress preamble; native model search does neither.
    ("web.search_backend", "ddgs"),
    ("agent.reasoning_effort", "medium"),
    ("display.show_reasoning", False),
    ("approvals.mode", "manual"),
    # The gateway reads config.yaml without merging Hermes defaults, so the
    # reference daemon's materialized display values must be explicit here.
    ("display.platforms.telegram.streaming", True),
    ("display.tool_preview_length", 0),
    ("display.busy_ack_detail", True),
)

_ENV_KEYS = (
    "DAEMON_CORE_SRC",
    "DAEMON_CORE_FOLLOWUPS",
    "DAEMON_CORE_USER_ID",
    "DAEMON_CORE_SOVEREIGN_VOICE",
    "DAEMON_CORE_MEMORY_ROOT",
    "DAEMON_CORE_STATE_ROOT",
    "DAEMON_CORE_PROVIDER_CONFIG",
    "DAEMON_CORE_PYTHON",
    "DAEMON_CORE_AFTER_TURN_OWNER",
    "DAEMON_CORE_AFTER_TURN_SPOOL",
    "DAEMON_CORE_AFTER_TURN_SPOOL_DIR",
    "DAEMON_CORE_LATENCY_LOG",
    "PYTHONDONTWRITEBYTECODE",
)
_COMPOSITION_ENV_KEYS = frozenset(_ENV_KEYS[4:-1])
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class HermesProfileLayout:
    hermes_home: Path
    repository_root: Path
    installation_scope: InstallationScope | None = None
    plugin_copies: bool = False

    @property
    def config_path(self) -> Path:
        return self.hermes_home / "config.yaml"

    @property
    def env_path(self) -> Path:
        return self.hermes_home / ".env"

    @property
    def plugins_dir(self) -> Path:
        return self.hermes_home / "plugins"

    @property
    def memory_plugin_link(self) -> Path:
        return self.plugins_dir / "daemon_core"

    @property
    def voice_plugin_link(self) -> Path:
        return self.plugins_dir / "daemon_voice"

    @property
    def memory_plugin_source(self) -> Path:
        return self.repository_root / "integrations" / "hermes_plugin"

    @property
    def voice_plugin_source(self) -> Path:
        return self.repository_root / "integrations" / "hermes_voice_plugin"


class HermesProfileAdapter:
    """Apply one reversible profile-only step to an explicitly disposable home."""

    def __init__(
        self,
        layout: HermesProfileLayout,
        *,
        selected_host: HostKind,
        user_id: str,
        runtime_environment: Mapping[str, str] | None = None,
    ) -> None:
        if selected_host != HostKind.HERMES:
            raise ValueError("Hermes profile adapter requires the Hermes host.")
        self.layout = layout
        self.user_id = _validated_env_value(user_id, "Hermes user id", maximum=128)
        supplied = dict(runtime_environment or {})
        if not set(supplied).issubset(_COMPOSITION_ENV_KEYS):
            raise ValueError("Hermes runtime environment contains unmanaged keys.")
        self.runtime_environment = {
            key: _validated_env_value(value, key, maximum=4096)
            for key, value in supplied.items()
        }
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        _validate_snapshot_directory(snapshot_dir)
        before = self._descriptors()
        config_before = _read_regular_bytes(self.layout.config_path)
        env_before = _read_regular_bytes(self.layout.env_path)
        config_expected = _patch_hermes_config(config_before or b"")
        env_expected = _patch_env(env_before or b"", self._environment_values())
        expected = {
            "config": _file_descriptor(
                config_expected,
                _desired_mode(before["config"]),
            ),
            "env": _file_descriptor(env_expected, 0o600),
            "memory_plugin": self._expected_plugin(self.layout.memory_plugin_source),
            "voice_plugin": self._expected_plugin(self.layout.voice_plugin_source),
            "plugins_dir": {
                "kind": "directory",
                "mode": _desired_directory_mode(before["plugins_dir"]),
                "entries": sorted({
                    *before["plugins_dir"].get("entries", []),
                    "daemon_core",
                    "daemon_voice",
                }),
            },
        }
        if config_before is not None:
            _write_private_bytes(snapshot_dir / "config.before", config_before, 0o600)
        if env_before is not None:
            _write_private_bytes(snapshot_dir / "env.before", env_before, 0o600)
        manifest = {"schema": "daemon.hermes-profile-snapshot.v1", "before": before, "expected": expected}
        _write_private_bytes(
            snapshot_dir / "manifest.json",
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            0o600,
        )
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Hermes profile snapshot is required before apply.")
        manifest = _load_manifest(snapshot_dir)
        if self._descriptors() != manifest["before"]:
            raise RuntimeError("Hermes profile changed after its snapshot.")

        config_before = _read_regular_bytes(self.layout.config_path) or b""
        config_expected = _patch_hermes_config(config_before)
        _write_private_bytes(
            self.layout.config_path,
            config_expected,
            _desired_mode(manifest["before"]["config"]),
        )
        self._after_mutation("config")

        env_before = _read_regular_bytes(self.layout.env_path) or b""
        env_expected = _patch_env(env_before, self._environment_values())
        _write_private_bytes(
            self.layout.env_path,
            env_expected,
            0o600,
        )
        self._after_mutation("env")

        self.layout.plugins_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._install_plugin(self.layout.memory_plugin_link, self.layout.memory_plugin_source)
        self._after_mutation("memory_plugin")
        self._install_plugin(self.layout.voice_plugin_link, self.layout.voice_plugin_source)
        self._after_mutation("voice_plugin")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        config = _load_yaml_mapping(self.layout.config_path)
        for dotted, expected in HERMES_CONFIG_VALUES:
            if _mapping_value(config, dotted) != expected:
                raise RuntimeError(f"Hermes setting was not applied: {dotted}")
        env = _read_env_values(self.layout.env_path)
        for key, expected in self._environment_values().items():
            if env.get(key) != expected:
                raise RuntimeError(f"Hermes environment setting was not applied: {key}")
        self._verify_plugin(self.layout.memory_plugin_link, self.layout.memory_plugin_source)
        self._verify_plugin(self.layout.voice_plugin_link, self.layout.voice_plugin_source)

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        manifest = _load_manifest(snapshot_dir)
        current = self._descriptors()
        before = manifest["before"]
        expected = manifest["expected"]
        conflicts = [
            name for name in before
            if not _rollback_state_is_safe(
                name,
                current[name],
                before[name],
                expected[name],
            )
        ]
        if conflicts:
            raise RuntimeError("Hermes profile has concurrent changes; rollback was refused.")

        self._restore_plugin(self.layout.voice_plugin_link, before["voice_plugin"])
        self._restore_plugin(self.layout.memory_plugin_link, before["memory_plugin"])
        _restore_directory(self.layout.plugins_dir, before["plugins_dir"])
        _restore_file(self.layout.env_path, before["env"], snapshot_dir / "env.before")
        _restore_file(
            self.layout.config_path,
            before["config"],
            snapshot_dir / "config.before",
        )

    def _environment_values(self) -> dict[str, str]:
        from daemon_core.installation_profile import HOST_VOICE
        return {
            "DAEMON_CORE_SRC": str(_io_path(self.layout.repository_root / "src")),
            # Order is kept: new keys are appended to .env in this sequence.
            "DAEMON_CORE_FOLLOWUPS": HOST_VOICE["DAEMON_CORE_FOLLOWUPS"],
            "DAEMON_CORE_USER_ID": self.user_id,
            "DAEMON_CORE_SOVEREIGN_VOICE": HOST_VOICE["DAEMON_CORE_SOVEREIGN_VOICE"],
            **private_runtime_path_environment(self.runtime_environment),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _descriptors(self) -> dict[str, dict[str, object]]:
        return {
            "config": _path_descriptor(self.layout.config_path, allow_file=True),
            "env": _path_descriptor(self.layout.env_path, allow_file=True),
            "memory_plugin": _path_descriptor(
                self.layout.memory_plugin_link,
                allow_symlink=True,
                allow_copy=self.layout.plugin_copies,
            ),
            "voice_plugin": _path_descriptor(
                self.layout.voice_plugin_link,
                allow_symlink=True,
                allow_copy=self.layout.plugin_copies,
            ),
            "plugins_dir": _path_descriptor(
                self.layout.plugins_dir,
                allow_directory=True,
            ),
        }

    def _validate_layout(self) -> None:
        home = self.layout.hermes_home
        repository = self.layout.repository_root
        if not home.is_absolute() or not repository.is_absolute():
            raise ValueError("Hermes profile and repository roots must be absolute.")
        if home.is_symlink() or repository.is_symlink():
            raise ValueError("Hermes profile and repository roots must not be symbolic links.")
        if home.resolve() != home or repository.resolve() != repository:
            raise ValueError("Hermes profile and repository roots must use direct paths.")
        validate_hermes_profile_scope(home, self.layout.installation_scope)
        if self.layout.plugins_dir.is_symlink():
            raise ValueError("Hermes plugins directory must not be a symbolic link.")
        for path in (self.layout.config_path, self.layout.env_path):
            if path.is_symlink():
                raise ValueError("Hermes profile files must not be symbolic links.")
            if path.exists() and not path.is_file():
                raise ValueError("Hermes profile files must be regular files.")
        for source in (
            self.layout.repository_root / "src",
            self.layout.memory_plugin_source,
            self.layout.voice_plugin_source,
        ):
            if _io_path(source).is_symlink() or not _io_path(source).is_dir():
                raise ValueError("Hermes adapter source directories are invalid.")
            if not source.resolve().is_relative_to(repository.resolve()):
                raise ValueError("Hermes adapter source escapes the repository root.")
        required_files = (
            self.layout.repository_root / "src" / "daemon_core" / "__init__.py",
            self.layout.repository_root / "src" / "daemon_core" / "loop.py",
            self.layout.memory_plugin_source / "__init__.py",
            self.layout.voice_plugin_source / "__init__.py",
        )
        if any(_io_path(path).is_symlink() or not _io_path(path).is_file() for path in required_files):
            raise ValueError("Hermes adapter source contract is incomplete.")
        for destination in (
            self.layout.memory_plugin_link,
            self.layout.voice_plugin_link,
        ):
            if destination.exists() and not destination.is_symlink():
                if not self.layout.plugin_copies or not destination.is_dir():
                    raise ValueError("Hermes plugin destination is not a managed plugin.")
                _copy_descriptor(destination)
            if self.layout.plugin_copies and destination.is_symlink():
                raise ValueError("Copied plugins must not replace existing links.")

    def _expected_plugin(self, source):
        if self.layout.plugin_copies:
            return _copy_descriptor(source)
        return {"kind": "symlink", "target": str(source)}

    def _install_plugin(self, destination, source):
        if not self.layout.plugin_copies:
            _replace_symlink(destination, source)
            return
        expected = _copy_descriptor(source)
        if destination.exists():
            if _copy_descriptor(destination) != expected:
                raise RuntimeError("Existing plugin copy differs; replacement refused.")
            return
        temporary = destination.with_name(destination.name + ".installing")
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("Plugin staging directory is occupied.")
        try:
            shutil.copytree(_io_path(source), _io_path(temporary))
            if _copy_descriptor(temporary) != expected:
                raise RuntimeError("Plugin copy integrity check failed.")
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _verify_plugin(self, destination, source):
        if self.layout.plugin_copies:
            if _copy_descriptor(destination) != _copy_descriptor(source):
                raise RuntimeError("Plugin copy integrity check failed.")
        else:
            _verify_symlink(destination, source)

    def _restore_plugin(self, path, before):
        if not self.layout.plugin_copies:
            _restore_link(path, before)
        elif before["kind"] == "missing":
            if path.exists():
                shutil.rmtree(path)  # complete descriptor was checked before rollback
        elif before["kind"] == "copy":
            if _copy_descriptor(path) != before:
                raise RuntimeError("Plugin copy changed; rollback refused.")
        else:
            raise ValueError("Unexpected copied plugin snapshot.")

    @staticmethod
    def _validate_step(step: PlanStep) -> None:
        if step.code != HERMES_PROFILE_STEP:
            raise ValueError("Hermes profile adapter only accepts hermes-profile.")

    def _after_mutation(self, _artifact: str) -> None:
        """Test seam invoked after each atomic artifact mutation."""


def validate_hermes_profile_scope(home: Path, scope: InstallationScope | None = None):
    if scope is not None:
        scope.validate_profile(home)
        return
    marker = home / DISPOSABLE_PROFILE_MARKER
    if not home.is_dir() or marker.is_symlink() or not marker.is_file():
        raise ValueError("Hermes profile is not marked as disposable.")
    if marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
        raise ValueError("Hermes disposable profile marker is invalid.")


def create_disposable_profile_marker(hermes_home: Path) -> None:
    """Create the explicit test-only gate; never called by production discovery."""

    if not hermes_home.is_absolute() or hermes_home.is_symlink():
        raise ValueError("Disposable Hermes profile root must be absolute and direct.")
    hermes_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    hermes_home.chmod(0o700)
    _write_private_bytes(
        hermes_home / DISPOSABLE_PROFILE_MARKER,
        _MARKER_CONTENT.encode("utf-8"),
        0o600,
    )


def _patch_hermes_config(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Hermes config must be UTF-8.") from error
    result = text
    for dotted, value in HERMES_CONFIG_VALUES:
        result = _set_yaml_scalar(result, dotted, _render_yaml_scalar(value))
    parsed = _safe_load_yaml(result) if result.strip() else {}
    if not isinstance(parsed, dict):
        raise ValueError("Hermes config must be a YAML mapping.")
    for dotted, expected in HERMES_CONFIG_VALUES:
        if _mapping_value(parsed, dotted) != expected:
            raise ValueError(f"Hermes config patch failed for {dotted}.")
    return result.encode("utf-8")


def _set_yaml_scalar(text: str, dotted: str, rendered: str) -> str:
    parts = dotted.split(".")
    if any(not _PLAIN_KEY.fullmatch(part) for part in parts):
        raise ValueError("Unsupported Hermes configuration path.")
    lines = text.splitlines(keepends=True)
    newline = _preferred_newline(lines)
    records = _yaml_records(lines)
    matches = [record for record in records if record["path"] == tuple(parts)]
    if len(matches) > 1:
        raise ValueError(f"Hermes config contains duplicate path: {dotted}")
    if matches:
        record = matches[0]
        if record["container"]:
            raise ValueError(f"Hermes config path is not scalar: {dotted}")
        index = int(record["index"])
        lines[index] = _replace_yaml_value(lines[index], rendered)
        return "".join(lines)

    prefix_length = 0
    parent_record: dict[str, object] | None = None
    for length in range(len(parts) - 1, 0, -1):
        prefix = tuple(parts[:length])
        candidates = [record for record in records if record["path"] == prefix]
        if len(candidates) > 1:
            raise ValueError(f"Hermes config contains duplicate path: {'.'.join(prefix)}")
        if candidates:
            parent_record = candidates[0]
            if not parent_record["container"]:
                raise ValueError(f"Hermes config parent is not a mapping: {'.'.join(prefix)}")
            prefix_length = length
            break

    if parent_record is None:
        insert_at = len(lines)
        if lines and not _line_has_ending(lines[-1]):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        indent = 0
    else:
        parent_index = int(parent_record["index"])
        parent_indent = int(parent_record["indent"])
        insert_at = _mapping_block_end(lines, parent_index, parent_indent)
        indent = _child_indent(records, tuple(parts[:prefix_length]), parent_indent)

    additions: list[str] = []
    for offset, part in enumerate(parts[prefix_length:]):
        current_indent = indent + (offset * 2)
        suffix = rendered if offset == len(parts[prefix_length:]) - 1 else ""
        additions.append(f"{' ' * current_indent}{part}: {suffix}".rstrip() + newline)
    lines[insert_at:insert_at] = additions
    return "".join(lines)


def _yaml_records(lines: list[str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if not body.strip() or body.lstrip().startswith("#") or body.startswith("\t"):
            continue
        match = re.match(r"^( *)([A-Za-z_][A-Za-z0-9_-]*):(.*)$", body)
        if not match:
            continue
        indent = len(match.group(1))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        value_without_comment = _without_yaml_comment(match.group(3)).strip()
        path = tuple(key for _level, key in stack) + (match.group(2),)
        container = not value_without_comment
        records.append({
            "index": index,
            "indent": indent,
            "path": path,
            "container": container,
        })
        if container:
            stack.append((indent, match.group(2)))
    return records


def _replace_yaml_value(line: str, rendered: str) -> str:
    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    body = line[:-len(ending)] if ending else line
    match = re.match(r"^( *[A-Za-z_][A-Za-z0-9_-]*:\s*)(.*)$", body)
    if not match:
        raise ValueError("Unsupported Hermes YAML scalar line.")
    comment = _yaml_comment(match.group(2))
    suffix = f" {comment}" if comment else ""
    return f"{match.group(1)}{rendered}{suffix}{ending}"


def _without_yaml_comment(value: str) -> str:
    comment_index = _yaml_comment_index(value)
    return value if comment_index is None else value[:comment_index]


def _yaml_comment(value: str) -> str:
    comment_index = _yaml_comment_index(value)
    return "" if comment_index is None else value[comment_index:].strip()


def _yaml_comment_index(value: str) -> int | None:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if not quote:
                quote = character
            elif quote == character:
                quote = ""
            continue
        if character == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return index
    return None


def _mapping_block_end(lines: list[str], parent_index: int, parent_indent: int) -> int:
    for index in range(parent_index + 1, len(lines)):
        body = lines[index].rstrip("\r\n")
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        indent = len(body) - len(body.lstrip(" "))
        if indent <= parent_indent:
            return index
    return len(lines)


def _child_indent(
    records: list[dict[str, object]],
    parent_path: tuple[str, ...],
    parent_indent: int,
) -> int:
    for record in records:
        path = record["path"]
        if len(path) == len(parent_path) + 1 and path[:-1] == parent_path:
            return int(record["indent"])
    return parent_indent + 2


def _render_yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError("Unsupported Hermes scalar value.")


def _patch_env(content: bytes, values: Mapping[str, str]) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Hermes environment file must be UTF-8.") from error
    lines = text.splitlines(keepends=True)
    newline = _preferred_newline(lines)
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if not body or body.lstrip().startswith("#") or "=" not in body:
            continue
        key = body.split("=", 1)[0].strip()
        if key in values:
            if key in positions:
                raise ValueError(f"Hermes environment contains duplicate key: {key}")
            positions[key] = index
    for key, value in values.items():
        if not _ENV_KEY.fullmatch(key):
            raise ValueError("Unsupported Hermes environment key.")
        _validated_env_value(value, key, maximum=2048)
        if key in positions:
            ending = (
                "\r\n" if lines[positions[key]].endswith("\r\n")
                else "\n" if lines[positions[key]].endswith("\n")
                else ""
            )
            lines[positions[key]] = f"{key}={value}{ending}"
        else:
            if lines and not _line_has_ending(lines[-1]):
                lines[-1] += newline
            lines.append(f"{key}={value}{newline}")
    return "".join(lines).encode("utf-8")


def _read_env_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in result:
            raise ValueError(f"Hermes environment contains duplicate key: {key}")
        result[key] = value
    return result


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    loaded = _safe_load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Hermes config must be a YAML mapping.")
    return loaded


def _safe_load_yaml(value: str) -> object:
    import yaml

    return yaml.safe_load(value)


def _mapping_value(mapping: Mapping[str, object], dotted: str) -> object:
    current: object = mapping
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


_MISSING = object()


def _preferred_newline(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"


def _line_has_ending(line: str) -> bool:
    return line.endswith("\n")


def _validated_env_value(value: str, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError(f"{label} is invalid.")
    return value


def _path_descriptor(
    path: Path,
    *,
    allow_file: bool = False,
    allow_symlink: bool = False,
    allow_directory: bool = False,
    allow_copy: bool = False,
) -> dict[str, object]:
    if path.is_symlink():
        if not allow_symlink:
            raise ValueError("Hermes profile path must not be a symbolic link.")
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "missing"}
    if allow_copy and path.is_dir():
        return _copy_descriptor(path)
    if allow_file and path.is_file():
        return _file_descriptor(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    if allow_directory and path.is_dir():
        return {
            "kind": "directory",
            "mode": 0o700 if os.name == "nt" else stat.S_IMODE(path.stat().st_mode),
            "entries": sorted(child.name for child in path.iterdir()),
        }
    raise ValueError("Hermes profile path has an unsupported type.")


def _file_descriptor(content: bytes, mode: int) -> dict[str, object]:
    return {
        "kind": "file",
        "sha256": hashlib.sha256(content).hexdigest(),
        "mode": (0o600 if mode & 0o200 else 0o400) if os.name == "nt" else mode,
    }


def _desired_mode(descriptor: Mapping[str, object]) -> int:
    if descriptor.get("kind") == "file":
        return int(descriptor["mode"])
    return 0o600


def _desired_directory_mode(descriptor: Mapping[str, object]) -> int:
    if descriptor.get("kind") == "directory":
        return int(descriptor["mode"])
    return 0o700


def _read_regular_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError("Hermes profile files must not be symbolic links.")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Hermes profile files must be regular files.")
    return path.read_bytes()


def _write_private_bytes(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_symlink(path: Path, target: Path) -> None:
    if path.exists() and not path.is_symlink():
        raise ValueError("Hermes plugin destination is not a symbolic link.")
    temporary = path.with_name(f".{path.name}.daemon-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Hermes plugin temporary link already exists.")
    try:
        os.symlink(str(target), temporary)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _verify_symlink(path: Path, expected: Path) -> None:
    if not path.is_symlink() or os.readlink(path) != str(expected):
        raise RuntimeError("Hermes plugin link verification failed.")


def _restore_link(path: Path, descriptor: Mapping[str, object]) -> None:
    if descriptor["kind"] == "missing":
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            raise RuntimeError("Hermes plugin rollback encountered a conflicting path.")
        return
    if descriptor["kind"] != "symlink":
        raise ValueError("Hermes plugin snapshot is invalid.")
    target = descriptor.get("target")
    if not isinstance(target, str):
        raise ValueError("Hermes plugin snapshot target is invalid.")
    _replace_symlink(path, Path(target))


def _restore_file(path: Path, descriptor: Mapping[str, object], backup: Path) -> None:
    if descriptor["kind"] == "missing":
        if path.is_symlink():
            raise RuntimeError("Hermes profile rollback encountered a symbolic link.")
        path.unlink(missing_ok=True)
        return
    if descriptor["kind"] != "file" or not backup.is_file() or backup.is_symlink():
        raise ValueError("Hermes profile snapshot file is invalid.")
    content = backup.read_bytes()
    if hashlib.sha256(content).hexdigest() != descriptor.get("sha256"):
        raise ValueError("Hermes profile snapshot checksum is invalid.")
    _write_private_bytes(path, content, int(descriptor["mode"]))


def _restore_directory(path: Path, descriptor: Mapping[str, object]) -> None:
    if descriptor["kind"] == "missing":
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("Hermes plugin directory rollback encountered a conflict.")
        path.rmdir()
        return
    if descriptor["kind"] != "directory" or not path.is_dir() or path.is_symlink():
        raise ValueError("Hermes plugin directory snapshot is invalid.")
    path.chmod(int(descriptor["mode"]))


def _rollback_state_is_safe(
    name: str,
    current: Mapping[str, object],
    before: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if current == before or current == expected:
        return True
    if name != "plugins_dir" or current.get("kind") != "directory":
        return False
    if current.get("mode") != expected.get("mode"):
        return False
    before_entries = (
        set(before.get("entries", [])) if before.get("kind") == "directory" else set()
    )
    current_entries = set(current.get("entries", []))
    expected_entries = set(expected.get("entries", []))
    return before_entries.issubset(current_entries) and current_entries.issubset(
        expected_entries
    )


def _load_manifest(snapshot_dir: Path) -> dict[str, dict[str, dict[str, object]]]:
    _validate_snapshot_directory(snapshot_dir)
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Hermes profile snapshot manifest is missing.")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or set(loaded) != {"schema", "before", "expected"}:
        raise ValueError("Hermes profile snapshot manifest is invalid.")
    if loaded["schema"] != "daemon.hermes-profile-snapshot.v1":
        raise ValueError("Hermes profile snapshot version is unsupported.")
    names = {"config", "env", "memory_plugin", "voice_plugin", "plugins_dir"}
    if not isinstance(loaded["before"], dict) or set(loaded["before"]) != names:
        raise ValueError("Hermes profile before-snapshot is invalid.")
    if not isinstance(loaded["expected"], dict) or set(loaded["expected"]) != names:
        raise ValueError("Hermes profile expected-snapshot is invalid.")
    allowed_before = {
        "config": {"missing", "file"},
        "env": {"missing", "file"},
        "memory_plugin": {"missing", "symlink", "copy"},
        "voice_plugin": {"missing", "symlink", "copy"},
        "plugins_dir": {"missing", "directory"},
    }
    required_expected = {
        "config": "file",
        "env": "file",
        "memory_plugin": "symlink",
        "voice_plugin": "symlink",
        "plugins_dir": "directory",
    }
    for name in names:
        _validate_descriptor(loaded["before"][name], allowed_before[name])
        kinds = {required_expected[name]}
        if name in {"memory_plugin", "voice_plugin"}:
            kinds.add("copy")
        _validate_descriptor(loaded["expected"][name], kinds)
    return loaded


def _validate_snapshot_directory(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("Hermes profile snapshot directory is invalid.")
    if path.resolve() != path:
        raise ValueError("Hermes profile snapshot directory must use a direct path.")


def _validate_descriptor(descriptor: object, allowed_kinds: set[str]) -> None:
    if not isinstance(descriptor, dict) or descriptor.get("kind") not in allowed_kinds:
        raise ValueError("Hermes profile snapshot descriptor is invalid.")
    kind = descriptor["kind"]
    if kind == "missing":
        expected_keys = {"kind"}
    elif kind == "file":
        expected_keys = {"kind", "sha256", "mode"}
        digest = descriptor.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Hermes profile snapshot checksum is invalid.")
        _validate_mode(descriptor.get("mode"))
    elif kind == "copy":
        expected_keys = {"kind", "files"}
        files = descriptor.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Plugin copy snapshot is empty.")
        for name, digest in files.items():
            if (not isinstance(name, str) or name.startswith("/") or ".." in name.split("/")
                    or "\\" in name or ":" in name or not isinstance(digest, str)
                    or (digest != "directory" and re.fullmatch(r"[0-9a-f]{64}", digest) is None)):
                raise ValueError("Plugin copy snapshot is invalid.")
    elif kind == "symlink":
        expected_keys = {"kind", "target"}
        target = descriptor.get("target")
        if not isinstance(target, str) or not target or "\x00" in target:
            raise ValueError("Hermes profile snapshot symlink target is invalid.")
    elif kind == "directory":
        expected_keys = {"kind", "mode", "entries"}
        _validate_mode(descriptor.get("mode"))
        entries = descriptor.get("entries")
        if (
            not isinstance(entries, list)
            or entries != sorted(set(entries))
            or any(
                not isinstance(entry, str)
                or not entry
                or entry in {".", ".."}
                or "/" in entry
                or "\\" in entry
                or "\x00" in entry
                for entry in entries
            )
        ):
            raise ValueError("Hermes profile snapshot directory entries are invalid.")
    else:  # pragma: no cover - guarded above.
        raise ValueError("Hermes profile snapshot descriptor kind is invalid.")
    if set(descriptor) != expected_keys:
        raise ValueError("Hermes profile snapshot descriptor fields are invalid.")


def _validate_mode(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0o777:
        raise ValueError("Hermes profile snapshot mode is invalid.")


__all__ = [
    "DISPOSABLE_PROFILE_MARKER",
    "HERMES_CONFIG_VALUES",
    "HERMES_PROFILE_STEP",
    "HermesProfileAdapter",
    "HermesProfileLayout",
    "create_disposable_profile_marker",
]


def _copy_descriptor(root: Path) -> dict[str, object]:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Plugin copy must be a direct directory.")
    root = _io_path(root)
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("Plugin copy must be a direct directory.")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("Plugin copy must not contain links.")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            files[path.relative_to(root).as_posix()] = "directory"
        else:
            raise ValueError("Plugin copy contains a special file.")
    if not files:
        raise ValueError("Plugin copy is empty.")
    return {"kind": "copy", "files": files}
