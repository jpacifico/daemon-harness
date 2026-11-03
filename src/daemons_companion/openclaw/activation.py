"""Gateway activation and readiness owner for a disposable OpenClaw profile."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Protocol
import urllib.error
import urllib.request

from daemons_companion.setup.plan import PlanStep
from daemons_companion.openclaw.profile import (
    OpenClawProfileLayout,
    validate_disposable_openclaw_state,
    validate_openclaw_state,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    runtime_bundle_digest,
    validate_companion_home,
    verify_current_release,
)
from daemon_core.memory.integrity import atomic_write_text


OPENCLAW_ACTIVATION_STEP = "openclaw-activation"
_SNAPSHOT_SCHEMA = "daemon.companion-openclaw-activation-snapshot.v1"
_ROLLBACK_MARKER_SCHEMA = "daemon.companion-openclaw-activation-rollback.v1"


@dataclass(frozen=True)
class OpenClawActivationLayout:
    runtime: CompanionRuntimeLayout
    openclaw_state: Path
    api_port: int = 8000
    gateway_port: int = 18789


@dataclass(frozen=True)
class OpenClawControlState:
    gateway: str

    def public_dict(self) -> dict[str, str]:
        return {"gateway": self.gateway}


class OpenClawControlBackend(Protocol):
    openclaw_state: Path

    def state(self) -> OpenClawControlState: ...

    def start_gateway(self) -> None: ...

    def restart_gateway(self) -> None: ...

    def stop_gateway(self) -> None: ...

    def gateway_ready(self) -> bool: ...

    def plugin_ready(self) -> bool: ...


@dataclass(frozen=True)
class OpenClawCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class OpenClawReadinessError(RuntimeError):
    """Readiness failure, with the last probe answers for the diagnostic."""

    def __init__(self, missing, detail=""):
        super().__init__(f"OpenClaw readiness failed: {', '.join(missing)}.")
        self.diagnostic = str(detail)[-8000:]


OpenClawCommandRunner = Callable[
    [Sequence[str], Mapping[str, str]], OpenClawCommandResult
]
ReadinessProbe = Callable[[str, Path, Path], bool]
ApiProbe = Callable[[int], bool]


class OpenClawCliBackend:
    """Bounded OpenClaw CLI adapter with an explicit disposable state root."""

    def __init__(
        self,
        executable: Path,
        openclaw_state: Path,
        gateway_port: int,
        *,
        runner: OpenClawCommandRunner | None = None,
    ) -> None:
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("OpenClaw executable must be an absolute file path.")
        validate_disposable_openclaw_state(openclaw_state)
        _validate_port(gateway_port, "OpenClaw gateway")
        self.executable = executable
        self.openclaw_state = openclaw_state
        self.gateway_port = gateway_port
        self._runner = runner or _subprocess_runner

    def state(self) -> OpenClawControlState:
        return OpenClawControlState(
            "running" if self.gateway_ready() else "stopped"
        )

    def start_gateway(self) -> None:
        self._required("gateway", "start")

    def restart_gateway(self) -> None:
        self._required("gateway", "restart")

    def stop_gateway(self) -> None:
        self._required("gateway", "stop")

    def gateway_ready(self) -> bool:
        return self._run(
            "gateway",
            "health",
            "--json",
            "--port",
            str(self.gateway_port),
            "--timeout",
            "1000",
        ).returncode == 0

    def plugin_ready(self) -> bool:
        result = self._run("plugins", "info", "daemon-harness", "--json", "--runtime")
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        plugin = payload.get("plugin", payload) if isinstance(payload, dict) else None
        if not isinstance(plugin, dict):
            return False
        return (
            plugin.get("id") == "daemon-harness"
            and plugin.get("enabled") is not False
            and plugin.get("status") not in {"disabled", "error", "failed"}
        )

    def _required(self, *arguments: str) -> None:
        if self._run(*arguments).returncode != 0:
            raise RuntimeError("OpenClaw command failed.")

    def _run(self, *arguments: str) -> OpenClawCommandResult:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
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
        environment["OPENCLAW_STATE_DIR"] = str(self.openclaw_state)
        environment["OPENCLAW_CONFIG_PATH"] = str(
            self.openclaw_state / "openclaw.json"
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["OPENCLAW_SKIP_CHANNELS"] = "1"
        return self._runner((str(self.executable), *arguments), environment)


def openclaw_activation_step(layout: OpenClawActivationLayout) -> PlanStep:
    _validate_planned_layout(layout)
    return PlanStep(
        OPENCLAW_ACTIVATION_STEP,
        "Start OpenClaw and prove plugin, API and after-turn readiness.",
        (
            f"OpenClaw profile: {layout.openclaw_state}",
            f"Gateway: 127.0.0.1:{layout.gateway_port}",
            f"Daemon API: 127.0.0.1:{layout.api_port}",
            f"Runtime revision: {runtime_bundle_digest(layout.runtime.bundle_root)}",
        ),
    )


class OpenClawActivationAdapter:
    def __init__(
        self,
        layout: OpenClawActivationLayout,
        *,
        control: OpenClawControlBackend,
        readiness_probe: ReadinessProbe | None = None,
        api_probe: ApiProbe | None = None,
        readiness_attempts: int = 40,
        readiness_interval_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.layout = layout
        self.control = control
        if control.openclaw_state != layout.openclaw_state:
            raise ValueError("OpenClaw control backend targets another profile.")
        self.readiness_probe = readiness_probe or _readiness_probe
        self.api_probe = api_probe or _api_probe
        if readiness_attempts < 1 or not 0 <= readiness_interval_seconds <= 5:
            raise ValueError("OpenClaw readiness retry settings are invalid.")
        self.readiness_attempts = readiness_attempts
        self.readiness_interval_seconds = readiness_interval_seconds
        self._sleeper = sleeper
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        _validate_layout(self.layout)
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            raise ValueError("OpenClaw activation snapshot directory is invalid.")
        state = self.control.state()
        _validate_control_state(state)
        atomic_write_text(
            snapshot_dir / "manifest.json",
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
            raise RuntimeError("OpenClaw activation snapshot is required before apply.")
        before = _snapshot_state(snapshot)
        if self.control.state() != before:
            raise RuntimeError("OpenClaw control state changed after its snapshot.")
        if before.gateway == "running":
            self.control.restart_gateway()
        else:
            self.control.start_gateway()
        self._mutation_hook("gateway")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        _validate_layout(self.layout)
        release = verify_current_release(self.layout.runtime)
        gateway_ready = plugin_ready = api_ready = worker_ready = False
        # A new private gateway took 146 s to open its port on the owner's
        # Windows PC, then 21 s for health and 67-86 s for the plugin probe.
        deadline = time.monotonic() + (420 if self.layout.runtime.installation_scope else 600)
        for attempt in range(self.readiness_attempts):
            if time.monotonic() >= deadline:
                break
            gateway_ready = self.control.gateway_ready()
            plugin_ready = self.control.plugin_ready()
            api_ready = self.api_probe(self.layout.api_port)
            worker_ready = self.readiness_probe(
                release.name,
                self.layout.runtime.memory_root,
                self.layout.runtime.state_root / "after-turn-spool",
            )
            if gateway_ready and plugin_ready and api_ready and worker_ready:
                return
            if attempt + 1 < self.readiness_attempts:
                self._sleeper(self.readiness_interval_seconds)
        missing = [
            label
            for label, ready in (
                ("gateway", gateway_ready),
                ("plugin", plugin_ready),
                ("API", api_ready),
                ("after-turn", worker_ready),
            )
            if not ready
        ]
        detail = getattr(self.control, "readiness_detail", None)
        raise OpenClawReadinessError(missing, detail() if callable(detail) else "")

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        before = _snapshot_state(snapshot_dir)
        current = self.control.state()
        if current.gateway not in {before.gateway, "running"}:
            raise RuntimeError(
                "OpenClaw control state changed concurrently; rollback refused."
            )
        if current.gateway == "running":
            self.control.stop_gateway()
        atomic_write_text(
            snapshot_dir / "rollback-ready.json",
            json.dumps({"schema": _ROLLBACK_MARKER_SCHEMA}) + "\n",
        )

    def restore_gateway_after_rollback(self, snapshot_dir: Path) -> None:
        marker = snapshot_dir / "rollback-ready.json"
        try:
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "OpenClaw activation rollback is not ready to finalize."
            ) from error
        if marker_payload != {"schema": _ROLLBACK_MARKER_SCHEMA}:
            raise RuntimeError("OpenClaw activation rollback marker is invalid.")
        before = _snapshot_state(snapshot_dir)
        current = self.control.state()
        if before.gateway == "running":
            if current.gateway == "running":
                self.control.restart_gateway()
            else:
                self.control.start_gateway()
        elif current.gateway == "running":
            self.control.stop_gateway()

    def _validate_step(self, step: PlanStep) -> None:
        if step != openclaw_activation_step(self.layout):
            raise ValueError("OpenClaw activation requires the exact canonical step.")


def _validate_layout(layout: OpenClawActivationLayout) -> Path:
    _validate_planned_layout(layout)
    validate_companion_home(layout.runtime.companion_home, layout.runtime.installation_scope)
    validate_openclaw_state(layout.openclaw_state, layout.runtime.installation_scope)
    release = verify_current_release(layout.runtime)
    profile = OpenClawProfileLayout(layout.runtime, layout.openclaw_state)
    if (
        profile.config_path.is_symlink()
        or not profile.config_path.is_file()
        or profile.workspace_marker.is_symlink()
        or not profile.workspace_marker.is_file()
        or profile.workspace_agents.is_symlink()
        or not profile.workspace_agents.is_file()
    ):
        raise ValueError(
            "OpenClaw activation requires the prepared disposable profile."
        )
    return release


def _validate_planned_layout(layout: OpenClawActivationLayout) -> None:
    if (
        not layout.runtime.companion_home.is_absolute()
        or layout.runtime.companion_home.is_symlink()
        or not layout.openclaw_state.is_absolute()
        or layout.openclaw_state.is_symlink()
    ):
        raise ValueError("OpenClaw activation paths are invalid.")
    _validate_port(layout.api_port, "OpenClaw companion API")
    _validate_port(layout.gateway_port, "OpenClaw gateway")
    if layout.api_port == layout.gateway_port:
        raise ValueError("OpenClaw API and gateway ports must differ.")


def _validate_port(port: int, label: str) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ValueError(f"{label} requires a non-privileged TCP port.")


def _validate_control_state(state: OpenClawControlState) -> None:
    if state.gateway not in {"running", "stopped"}:
        raise RuntimeError("OpenClaw control state has an invalid gateway value.")


def _snapshot_state(snapshot_dir: Path) -> OpenClawControlState:
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("OpenClaw activation snapshot is unavailable.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("OpenClaw activation snapshot is invalid.") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _SNAPSHOT_SCHEMA
        or not isinstance(payload.get("before"), dict)
        or set(payload["before"]) != {"gateway"}
    ):
        raise RuntimeError("OpenClaw activation snapshot is invalid.")
    state = OpenClawControlState(payload["before"]["gateway"])
    _validate_control_state(state)
    return state


def _readiness_probe(revision: str, memory_root: Path, spool_root: Path) -> bool:
    from daemon_core.after_turn_service import read_after_turn_service_status

    return bool(
        read_after_turn_service_status(
            expected_revision=revision,
            memory_root=memory_root,
            spool_root=spool_root,
        )["ok"]
    )


def _api_probe(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=1,
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _subprocess_runner(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> OpenClawCommandResult:
    completed = subprocess.run(
        list(command),
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    return OpenClawCommandResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


__all__ = [
    "OPENCLAW_ACTIVATION_STEP",
    "OpenClawActivationAdapter",
    "OpenClawActivationLayout",
    "OpenClawCliBackend",
    "OpenClawCommandResult",
    "OpenClawControlBackend",
    "OpenClawControlState",
    "openclaw_activation_step",
]
