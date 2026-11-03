"""Dormant plugin activation and readiness owner for a Hermes profile."""

from __future__ import annotations

from daemons_companion.runtime.bootstrap import HostCommandError, _source_io_path

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Protocol

from daemons_companion.setup.plan import PlanStep
from daemons_companion.hermes.profile import (
    validate_hermes_profile_scope,
    HermesProfileLayout,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    runtime_bundle_digest,
    validate_companion_home,
    verify_current_release,
)
from daemon_core.memory.integrity import atomic_write_text


HERMES_ACTIVATION_STEP = "hermes-activation"
_PLUGINS = ("daemon_core", "daemon_voice")
_SNAPSHOT_SCHEMA = "daemon.companion-hermes-activation-snapshot.v1"
_ROLLBACK_MARKER_SCHEMA = "daemon.companion-hermes-activation-rollback.v1"


@dataclass(frozen=True)
class HermesActivationLayout:
    runtime: CompanionRuntimeLayout
    hermes_home: Path


@dataclass(frozen=True)
class HermesControlState:
    plugins: tuple[tuple[str, bool], ...]
    gateway: str

    def public_dict(self) -> dict[str, object]:
        return {"plugins": dict(self.plugins), "gateway": self.gateway}


class HermesControlBackend(Protocol):
    hermes_home: Path

    def state(self) -> HermesControlState: ...

    def enable_plugin(self, name: str) -> None: ...

    def disable_plugin(self, name: str) -> None: ...

    def start_gateway(self) -> None: ...

    def restart_gateway(self) -> None: ...

    def stop_gateway(self) -> None: ...

    def gateway_ready(self) -> bool: ...


@dataclass(frozen=True)
class HermesCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


HermesCommandRunner = Callable[
    [Sequence[str], Mapping[str, str]], HermesCommandResult
]
ReadinessProbe = Callable[[str, Path, Path], bool]


class HermesCliBackend:
    """Bounded Hermes CLI adapter with an explicit profile environment."""

    def __init__(
        self,
        executable: Path,
        hermes_home: Path,
        *,
        runner: HermesCommandRunner | None = None,
    ) -> None:
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("Hermes executable must be an absolute file path.")
        if not hermes_home.is_absolute() or hermes_home.is_symlink():
            raise ValueError("Hermes profile must be an absolute direct path.")
        self.executable = executable
        self.hermes_home = hermes_home
        self._runner = runner or _subprocess_runner

    def state(self) -> HermesControlState:
        result = self._run("plugins", "list", "--json")
        if result.returncode != 0:
            raise RuntimeError("Hermes plugin inventory failed.") from HostCommandError(result.returncode, result.stderr)
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Hermes plugin inventory is invalid.") from error
        if not isinstance(rows, list):
            raise RuntimeError("Hermes plugin inventory is invalid.")
        enabled: dict[str, bool] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Hermes plugin inventory is invalid.")
            name = row.get("name")
            status = row.get("status")
            if name in _PLUGINS:
                if status not in {"enabled", "not enabled", "disabled"}:
                    raise RuntimeError("Hermes plugin status is invalid.")
                enabled[str(name)] = status == "enabled"
        return HermesControlState(
            tuple((name, enabled.get(name, False)) for name in _PLUGINS),
            "running" if self._run("gateway", "status").returncode == 0 else "stopped",
        )

    def enable_plugin(self, name: str) -> None:
        self._plugin_command("enable", name)

    def disable_plugin(self, name: str) -> None:
        self._plugin_command("disable", name)

    def start_gateway(self) -> None:
        self._required("gateway", "start")

    def restart_gateway(self) -> None:
        self._required("gateway", "restart")

    def stop_gateway(self) -> None:
        self._required("gateway", "stop")

    def gateway_ready(self) -> bool:
        return self._run("gateway", "status", "--deep").returncode == 0

    def _plugin_command(self, action: str, name: str) -> None:
        if name not in _PLUGINS:
            raise ValueError("Hermes plugin is not managed by the companion.")
        self._required("plugins", action, name)

    def _required(self, *arguments: str) -> None:
        result = self._run(*arguments)
        if result.returncode != 0:
            raise RuntimeError("Hermes command failed.") from HostCommandError(result.returncode, result.stderr)

    def _run(self, *arguments: str) -> HermesCommandResult:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {
                "COMSPEC",
                "HOME",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "USERPROFILE",
                "WINDIR",
            }
        }
        environment["HERMES_HOME"] = str(self.hermes_home)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return self._runner((str(self.executable), *arguments), environment)


def hermes_activation_step(layout: HermesActivationLayout) -> PlanStep:
    _validate_planned_layout(layout)
    return PlanStep(
        HERMES_ACTIVATION_STEP,
        "Enable both Hermes plugins and prove gateway plus after-turn readiness.",
        (
            "Plugins: daemon_core, daemon_voice",
            f"Hermes profile: {layout.hermes_home}",
            f"Runtime revision: {runtime_bundle_digest(layout.runtime.bundle_root)}",
            "Gateway and common worker readiness",
        ),
    )


class HermesActivationAdapter:
    def __init__(
        self,
        layout: HermesActivationLayout,
        *,
        control: HermesControlBackend,
        readiness_probe: ReadinessProbe | None = None,
        readiness_attempts: int = 40,
        readiness_timeout_seconds: float = 120,
        clock: Callable[[], float] = time.monotonic,
        readiness_interval_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
        mutation_hook=None,
    ) -> None:
        self.layout = layout
        self.control = control
        if control.hermes_home != layout.hermes_home:
            raise ValueError("Hermes control backend targets another profile.")
        self.readiness_probe = readiness_probe or _readiness_probe
        if readiness_attempts < 1 or not 0 <= readiness_interval_seconds <= 5:
            raise ValueError("Hermes readiness retry settings are invalid.")
        self.readiness_attempts = readiness_attempts
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._clock = clock
        self.progress = lambda _message: None
        self.readiness_interval_seconds = readiness_interval_seconds
        self._sleeper = sleeper
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        _validate_layout(self.layout)
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            raise ValueError("Hermes activation snapshot directory is invalid.")
        if hasattr(self.control, "track_config"):
            self.control.track_config(snapshot_dir, create=True)
        state = self.control.state()
        _validate_control_state(state)
        atomic_write_text(
            _source_io_path(snapshot_dir / "manifest.json"),
            json.dumps(
                {"schema": _SNAPSHOT_SCHEMA, "before": state.public_dict()},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        _validate_layout(self.layout)
        snapshot = self._snapshot_dirs.get(step.code)
        if snapshot is None:
            raise RuntimeError("Hermes activation snapshot is required before apply.")
        before = _snapshot_state(snapshot)
        if self.control.state() != before:
            raise RuntimeError("Hermes control state changed after its snapshot.")
        enabled = dict(before.plugins)
        for name in _PLUGINS:
            if not enabled[name]:
                self.control.enable_plugin(name)
                self._mutation_hook(name)
        if before.gateway == "running":
            self.control.restart_gateway()
        else:
            self.control.start_gateway()
        self._mutation_hook("gateway")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        _validate_layout(self.layout)
        state = self.control.state()
        if state.gateway != "running" or not all(dict(state.plugins).values()):
            raise RuntimeError("Hermes plugins or gateway are not active.")
        release = verify_current_release(self.layout.runtime)
        gateway_ready = False
        worker_ready = False
        deadline = self._clock() + self.readiness_timeout_seconds
        previous_progress = None
        for attempt in range(self.readiness_attempts):
            gateway_ready = self.control.gateway_ready()
            worker_ready = self.readiness_probe(
                release.name,
                self.layout.runtime.memory_root,
                self.layout.runtime.state_root / "after-turn-spool",
            )
            if gateway_ready and worker_ready:
                return
            progress = ("Telegram connected. Waiting for the memory worker..." if gateway_ready
                        else "Waiting for the Telegram gateway to connect...")
            if progress != previous_progress:
                self.progress(progress)
                previous_progress = progress
            if self._clock() >= deadline:
                break
            if attempt + 1 < self.readiness_attempts:
                self._sleeper(self.readiness_interval_seconds)
        if not gateway_ready:
            raise RuntimeError("Hermes gateway readiness failed.")
        if not worker_ready:
            raise RuntimeError("Hermes after-turn readiness failed.")

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_scope()
        before = _snapshot_state(snapshot_dir)
        if hasattr(self.control, "track_config"):
            self.control.track_config(snapshot_dir)
        current = self.control.state()
        previous = dict(before.plugins)
        now = dict(current.plugins)
        conflicts = [
            name for name in _PLUGINS if now[name] not in {previous[name], True}
        ]
        if current.gateway not in {before.gateway, "running"}:
            conflicts.append("gateway")
        if conflicts:
            raise RuntimeError("Hermes control state changed concurrently; rollback refused.")
        if current.gateway == "running":
            self.control.stop_gateway()
        for name in reversed(_PLUGINS):
            if now[name] != previous[name]:
                if previous[name]:
                    self.control.enable_plugin(name)
                else:
                    self.control.disable_plugin(name)
        if hasattr(self.control, "restore_config"):
            self.control.restore_config()
        atomic_write_text(
            _source_io_path(snapshot_dir / "rollback-ready.json"),
            json.dumps({"schema": _ROLLBACK_MARKER_SCHEMA}) + "\n",
        )

    def restore_gateway_after_rollback(self, snapshot_dir: Path) -> None:
        """Restore prior gateway state after every file owner has rolled back."""

        marker = _source_io_path(snapshot_dir / "rollback-ready.json")
        try:
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Hermes activation rollback is not ready to finalize.") from error
        if marker_payload != {"schema": _ROLLBACK_MARKER_SCHEMA}:
            raise RuntimeError("Hermes activation rollback marker is invalid.")
        self._validate_scope()
        before = _snapshot_state(snapshot_dir)
        current = self.control.state()
        if before.gateway == "running":
            if current.gateway == "running":
                self.control.restart_gateway()
            else:
                self.control.start_gateway()
        elif current.gateway == "running":
            self.control.stop_gateway()

    def _validate_scope(self) -> None:
        # Finalization runs after the runtime owner may have removed its release.
        # Validate authority without requiring that removed release to exist.
        scope = self.layout.runtime.installation_scope
        if scope is not None:
            scope.validate_home(self.layout.runtime.companion_home)
            scope.validate_profile(self.layout.hermes_home)

    def _validate_step(self, step: PlanStep) -> None:
        if step != hermes_activation_step(self.layout):
            raise ValueError("Hermes activation requires the exact canonical step.")


def _validate_layout(layout: HermesActivationLayout) -> Path:
    _validate_planned_layout(layout)
    validate_companion_home(layout.runtime.companion_home, layout.runtime.installation_scope)
    release = verify_current_release(layout.runtime)
    profile = HermesProfileLayout(layout.hermes_home, release)
    validate_hermes_profile_scope(profile.hermes_home, layout.runtime.installation_scope)
    from daemons_companion.hermes.profile import _copy_descriptor
    for destination, source in ((profile.memory_plugin_link, profile.memory_plugin_source),
                                (profile.voice_plugin_link, profile.voice_plugin_source)):
        if destination.is_symlink():
            valid = destination.resolve() == source
        else:
            try:
                valid = _copy_descriptor(destination) == _copy_descriptor(source)
            except ValueError:
                valid = False
        if not valid:
            raise ValueError("Hermes activation requires the prepared disposable profile or authorized private profile.")
    return release


def _validate_planned_layout(layout: HermesActivationLayout) -> None:
    if (
        not layout.runtime.companion_home.is_absolute()
        or layout.runtime.companion_home.is_symlink()
        or not layout.hermes_home.is_absolute()
        or layout.hermes_home.is_symlink()
    ):
        raise ValueError("Hermes activation paths are invalid.")


def _validate_control_state(state: HermesControlState) -> None:
    if tuple(name for name, _enabled in state.plugins) != _PLUGINS:
        raise RuntimeError("Hermes control state has an invalid plugin set.")
    if any(not isinstance(enabled, bool) for _name, enabled in state.plugins):
        raise RuntimeError("Hermes control state has an invalid plugin value.")
    if state.gateway not in {"running", "stopped"}:
        raise RuntimeError("Hermes control state has an invalid gateway value.")


def _snapshot_state(snapshot_dir: Path) -> HermesControlState:
    path = _source_io_path(snapshot_dir / "manifest.json")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Hermes activation snapshot is unavailable.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != _SNAPSHOT_SCHEMA:
        raise RuntimeError("Hermes activation snapshot is invalid.")
    before = payload.get("before")
    if not isinstance(before, dict) or set(before) != {"plugins", "gateway"}:
        raise RuntimeError("Hermes activation snapshot is invalid.")
    plugins = before["plugins"]
    if not isinstance(plugins, dict) or set(plugins) != set(_PLUGINS):
        raise RuntimeError("Hermes activation snapshot is invalid.")
    state = HermesControlState(
        tuple((name, plugins[name]) for name in _PLUGINS),
        before["gateway"],
    )
    _validate_control_state(state)
    return state


def _readiness_probe(revision: str, memory_root: Path, spool_root: Path, *, expected_profile=None) -> bool:
    from daemon_core.after_turn_service import read_after_turn_service_status
    from daemons_companion.runtime.bootstrap import _source_io_path

    return bool(
        read_after_turn_service_status(
            expected_revision=revision,
            memory_root=_source_io_path(memory_root),
            spool_root=_source_io_path(spool_root),
            expected_profile=expected_profile,
        )["ok"]
    )


def _subprocess_runner(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> HermesCommandResult:
    completed = subprocess.run(
        list(command),
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    return HermesCommandResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


__all__ = [
    "HERMES_ACTIVATION_STEP",
    "HermesActivationAdapter",
    "HermesActivationLayout",
    "HermesCliBackend",
    "HermesCommandResult",
    "HermesControlBackend",
    "HermesControlState",
    "hermes_activation_step",
]
