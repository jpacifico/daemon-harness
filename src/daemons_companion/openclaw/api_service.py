"""Transactional ownership of the companion OpenClaw loopback API job."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path

from daemons_companion.runtime.services import (
    OPENCLAW_API_JOB,
    BackgroundJob,
    SchedulerBackend,
    render_job_definition,
    scoped_job_code,
)
from daemons_companion.setup.plan import PlanStep
from daemons_companion.openclaw.profile import (
    validate_openclaw_state,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    validate_companion_home,
    verify_current_release,
)
from daemons_companion.runtime.environment import (
    CompanionRuntimeEnvironmentLayout,
    release_environment_interpreter,
    resolve_release_environment,
)
from daemon_core.memory.integrity import atomic_write_text


OPENCLAW_API_SERVICE_STEP = "openclaw-api-service"
_SNAPSHOT_SCHEMA = "daemon.companion-openclaw-api-service-snapshot.v1"
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class OpenClawApiServiceLayout:
    runtime: CompanionRuntimeLayout
    runtime_environment: CompanionRuntimeEnvironmentLayout
    openclaw_state: Path
    target_os: str
    user_id: str
    port: int = 8000
    job_namespace: str | None = None


def openclaw_api_service_step(layout: OpenClawApiServiceLayout) -> PlanStep:
    _validate_planned_layout(layout)
    return PlanStep(
        OPENCLAW_API_SERVICE_STEP,
        "Install the OpenClaw loopback API through the native user scheduler.",
        (
            f"{scoped_job_code(OPENCLAW_API_JOB, layout.job_namespace)}: persistent",
            f"Endpoint: 127.0.0.1:{layout.port}",
            "Private runtime, memory and state",
        ),
    )


def build_openclaw_api_job(layout: OpenClawApiServiceLayout) -> BackgroundJob:
    _validate_layout(layout)
    release = verify_current_release(layout.runtime)
    environment = resolve_release_environment(layout.runtime_environment)
    python = release_environment_interpreter(environment, layout.target_os)
    state = layout.runtime.state_root
    from daemons_companion.runtime.bootstrap import _source_io_path
    io_path = _source_io_path if layout.target_os == "windows" else lambda p: p
    bootstrap = (
        "import runpy,sys;"
        "sys.dont_write_bytecode=True;"
        f"sys.path.insert(0,{str(io_path(release))!r});"
        f"sys.path.insert(0,{str(io_path(release / 'src'))!r});"
        "runpy.run_module('daemons_companion.runtime.service_entry',run_name='__main__')"
    )
    arguments = (
        "-I", "-B", "-c",
        bootstrap,
        "openclaw-api",
        "--release",
        str(release),
        "--python",
        str(python),
        "--memory-root",
        str(layout.runtime.memory_root),
        "--state-root",
        str(state),
        "--provider-config",
        str(state / "config" / "provider.json"),
        "--runtime-revision",
        release.name,
        "--user-id",
        layout.user_id,
        "--openclaw-state",
        str(layout.openclaw_state),
        "--port",
        str(layout.port),
    )
    if layout.runtime.installation_scope is not None:
        layout.runtime.installation_scope.validate_namespace(layout.job_namespace)
        arguments += ("--installation-attempt", str(layout.runtime.installation_scope.attempt))
    return BackgroundJob(
        scoped_job_code(OPENCLAW_API_JOB, layout.job_namespace),
        str(io_path(python)),
        arguments,
        str(release),
        str(state / "openclaw-api.log"),
        True,
        conversational=True,
    )


class CompanionOpenClawApiServiceAdapter:
    """Apply one reversible API registration to an injected native scheduler."""

    def __init__(
        self,
        layout: OpenClawApiServiceLayout,
        *,
        backend: SchedulerBackend,
        mutation_hook=None,
    ) -> None:
        if backend.target_os != layout.target_os:
            raise ValueError("Scheduler backend differs from the requested target OS.")
        self.layout = layout
        self.backend = backend
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        job = self._validate(step)
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            raise ValueError("OpenClaw API snapshot directory is invalid.")
        capture = self.backend.capture(job.code)
        if capture is not None and len(capture) > _MAX_CAPTURE_BYTES:
            raise ValueError("OpenClaw API scheduler capture is too large.")
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "job": job.public_dict(),
            "before": (
                base64.b64encode(capture).decode("ascii")
                if capture is not None
                else None
            ),
        }
        atomic_write_text(
            snapshot_dir / "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        job = self._validate(step)
        snapshot = self._snapshot_dirs.get(step.code)
        if snapshot is None:
            raise RuntimeError("OpenClaw API snapshot is required before apply.")
        manifest = _load_manifest(snapshot)
        if manifest["job"] != job.public_dict():
            raise RuntimeError("OpenClaw API inputs changed after snapshot.")
        self.backend.install(job, render_job_definition(job, self.layout.target_os))
        self._mutation_hook(job.code)

    def verify(self, step: PlanStep) -> None:
        job = self._validate(step)
        definition = render_job_definition(job, self.layout.target_os)
        if not self.backend.matches(job, definition):
            raise RuntimeError("OpenClaw API scheduler job is not active.")

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        job = self._validate(step)
        manifest = _load_manifest(snapshot_dir)
        if manifest["job"] != job.public_dict():
            raise RuntimeError("OpenClaw API snapshot no longer matches the plan.")
        before = _decode_capture(manifest["before"])
        current = self.backend.capture(job.code)
        expected = render_job_definition(job, self.layout.target_os)
        if current != before and not self.backend.matches(job, expected):
            raise RuntimeError(
                "OpenClaw API scheduler has concurrent changes; rollback was refused."
            )
        self.backend.restore(job.code, before)

    def _validate(self, step: PlanStep) -> BackgroundJob:
        if step != openclaw_api_service_step(self.layout):
            raise ValueError("OpenClaw API service requires the exact canonical step.")
        return build_openclaw_api_job(self.layout)


def _validate_layout(layout: OpenClawApiServiceLayout) -> None:
    _validate_planned_layout(layout)
    validate_companion_home(layout.runtime.companion_home, layout.runtime.installation_scope)
    validate_openclaw_state(layout.openclaw_state, layout.runtime.installation_scope)
    config = layout.openclaw_state / "openclaw.json"
    if config.is_symlink() or not config.is_file():
        raise ValueError("OpenClaw API requires a configured disposable profile.")


def _validate_planned_layout(layout: OpenClawApiServiceLayout) -> None:
    if (
        not layout.runtime.companion_home.is_absolute()
        or layout.runtime.companion_home.is_symlink()
        or not layout.openclaw_state.is_absolute()
        or layout.openclaw_state.is_symlink()
    ):
        raise ValueError("OpenClaw API service paths are invalid.")
    if layout.target_os not in {"macos", "windows"}:
        raise ValueError("OpenClaw API service supports macOS and Windows only.")
    if layout.runtime_environment.runtime != layout.runtime:
        raise ValueError("Runtime environment does not belong to this runtime.")
    if layout.runtime_environment.target_os != layout.target_os:
        raise ValueError("Runtime environment target differs from the scheduler target.")
    if (
        not isinstance(layout.port, int)
        or isinstance(layout.port, bool)
        or not 1024 <= layout.port <= 65535
    ):
        raise ValueError("OpenClaw API service requires a non-privileged TCP port.")
    if (
        not isinstance(layout.user_id, str)
        or not layout.user_id
        or len(layout.user_id) > 128
        or any(character in layout.user_id for character in "\r\n\x00")
    ):
        raise ValueError("OpenClaw API user id is invalid.")


def _load_manifest(snapshot_dir: Path) -> dict[str, object]:
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("OpenClaw API snapshot is unavailable.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("OpenClaw API snapshot is invalid.") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "job", "before"}
        or payload.get("schema") != _SNAPSHOT_SCHEMA
        or not isinstance(payload.get("job"), dict)
        or payload.get("before") is not None
        and not isinstance(payload.get("before"), str)
    ):
        raise RuntimeError("OpenClaw API snapshot is invalid.")
    return payload


def _decode_capture(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("OpenClaw API scheduler capture is invalid.")
    try:
        content = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise RuntimeError("OpenClaw API scheduler capture is invalid.") from error
    if len(content) > _MAX_CAPTURE_BYTES:
        raise RuntimeError("OpenClaw API scheduler capture is too large.")
    return content


__all__ = [
    "OPENCLAW_API_SERVICE_STEP",
    "CompanionOpenClawApiServiceAdapter",
    "OpenClawApiServiceLayout",
    "build_openclaw_api_job",
    "openclaw_api_service_step",
]
