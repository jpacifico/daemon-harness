"""Dormant transactional ownership of the two companion background jobs."""

from __future__ import annotations

from daemons_companion.runtime.bootstrap import HostCommandError

import base64
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time
from typing import Callable, Mapping, Protocol, Sequence
from xml.etree import ElementTree

from daemons_companion.setup.plan import PlanStep
from daemons_companion.runtime import service_entry
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


BACKGROUND_SERVICES_STEP = "background-services"
AFTER_TURN_JOB = "com.daemonharness.after-turn-worker"
NIGHTLY_JOB = "com.daemonharness.dreaming"
OPENCLAW_API_JOB = "com.daemonharness.openclaw-api"
HERMES_GATEWAY_JOB = "com.daemonharness.hermes-gateway"
OPENCLAW_GATEWAY_JOB = "com.daemonharness.openclaw-gateway"
EMBEDDINGS_JOB = "com.daemonharness.embeddings"
_SNAPSHOT_SCHEMA = "daemon.companion-background-services-snapshot.v1"
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class BackgroundServicesLayout:
    runtime: CompanionRuntimeLayout
    runtime_environment: CompanionRuntimeEnvironmentLayout
    target_os: str
    user_id: str
    job_namespace: str | None = None


@dataclass(frozen=True)
class BackgroundJob:
    code: str
    executable: str
    arguments: tuple[str, ...]
    working_directory: str
    log_path: str
    persistent: bool
    hour: int | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "executable": self.executable,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "log_path": self.log_path,
            "persistent": self.persistent,
            "hour": self.hour,
        }


class SchedulerBackend(Protocol):
    target_os: str

    def capture(self, job_code: str) -> bytes | None: ...

    def install(self, job: BackgroundJob, definition: bytes) -> None: ...

    def matches(self, job: BackgroundJob, definition: bytes) -> bool: ...

    def restore(self, job_code: str, capture: bytes | None) -> None: ...


@dataclass(frozen=True)
class SchedulerCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


SchedulerCommandRunner = Callable[
    [Sequence[str], Mapping[str, str]], SchedulerCommandResult
]


class MacLaunchdSchedulerBackend:
    """Native per-user launchd backend with exact file/load-state capture."""

    target_os = "macos"

    def __init__(
        self,
        launch_agents_dir: Path,
        *,
        user_id: int,
        runner: SchedulerCommandRunner | None = None,
        job_namespace: str | None = None,
    ) -> None:
        if user_id < 1:
            raise ValueError("launchd user id is invalid.")
        if not launch_agents_dir.is_absolute() or launch_agents_dir.is_symlink():
            raise ValueError("LaunchAgents directory must be an absolute direct path.")
        _validate_job_namespace(job_namespace)
        self.job_namespace = job_namespace
        self.launch_agents_dir = launch_agents_dir
        self.user_id = user_id
        self._runner = runner or _scheduler_subprocess_runner

    def capture(self, job_code: str) -> bytes | None:
        path = self._path(job_code)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("LaunchAgent definition is not a regular file.")
        if path.exists():
            _validate_launchd_label(path.read_bytes(), job_code)
        loaded = self._is_loaded(job_code)
        if not path.exists() and not loaded:
            return None
        payload = {
            "schema": "daemon.launchd-capture.v1",
            "loaded": loaded,
            "definition": (
                base64.b64encode(path.read_bytes()).decode("ascii")
                if path.exists()
                else None
            ),
        }
        return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        _validate_native_job(job, self.job_namespace)
        if definition != render_job_definition(job, "macos"):
            raise ValueError("LaunchAgent definition differs from the canonical job.")
        path = self._path(job.code)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("LaunchAgent definition is not a regular file.")
        if path.exists():
            _validate_launchd_label(path.read_bytes(), job.code)
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write_definition(path, definition)
        self._bootout(job.code)
        result = self._run((
            "/bin/launchctl",
            "bootstrap",
            f"gui/{self.user_id}",
            str(self._path(job.code)),
        ))
        if result.returncode != 0:
            raise RuntimeError("launchd refused the companion job.")

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        path = self._path(job.code)
        return (
            path.is_file()
            and not path.is_symlink()
            and path.read_bytes() == definition
            and self._is_loaded(job.code)
        )

    def restore(self, job_code: str, capture: bytes | None) -> None:
        path = self._path(job_code)
        payload = _launchd_capture(capture) if capture is not None else None
        if payload is not None:
            encoded = payload["definition"]
            if encoded is not None:
                _validate_launchd_label(base64.b64decode(encoded, validate=True), job_code)
            elif payload["loaded"]:
                raise RuntimeError("Loaded LaunchAgent capture has no definition.")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("LaunchAgent definition is not a regular file.")
        if path.exists():
            _validate_launchd_label(path.read_bytes(), job_code)
        self._bootout(job_code)
        if capture is None:
            path.unlink(missing_ok=True)
            return
        payload = _launchd_capture(capture)
        encoded = payload["definition"]
        if encoded is None:
            path.unlink(missing_ok=True)
        else:
            self.launch_agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._write_definition(path, base64.b64decode(encoded, validate=True))
        if payload["loaded"]:
            if encoded is None:
                raise RuntimeError("Loaded LaunchAgent capture has no definition.")
            result = self._run((
                "/bin/launchctl",
                "bootstrap",
                f"gui/{self.user_id}",
                str(path),
            ))
            if result.returncode != 0:
                raise RuntimeError("launchd restore failed.")

    def _path(self, job_code: str) -> Path:
        _validate_job_code(job_code, self.job_namespace)
        return self.launch_agents_dir / f"{job_code}.plist"

    def _is_loaded(self, job_code: str) -> bool:
        return self._run((
            "/bin/launchctl",
            "print",
            f"gui/{self.user_id}/{job_code}",
        )).returncode == 0

    def _bootout(self, job_code: str) -> None:
        self._run((
            "/bin/launchctl",
            "bootout",
            f"gui/{self.user_id}/{job_code}",
        ))
        if self.job_namespace is not None:
            # launchd can retain the job briefly while its process exits. Never
            # delete an owned release or report rollback before removal settles.
            for _ in range(100):
                if not self._is_loaded(job_code):
                    return
                time.sleep(0.1)
            raise RuntimeError("Private launchd job did not finish stopping.")

    @staticmethod
    def _write_definition(path: Path, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _run(self, command: Sequence[str]) -> SchedulerCommandResult:
        return self._runner(command, _scheduler_environment())


class WindowsTaskSchedulerBackend:
    """Native per-user Task Scheduler backend using fixed PowerShell scripts."""

    target_os = "windows"
    _TASK_PATH = "\\DaemonHarness"
    # Use the native COM API without triggering PowerShell module discovery.
    # Only FILE/PATH_NOT_FOUND means absence; permissions and service errors fail.
    _CONNECT = (
        "$ErrorActionPreference='Stop';"
        "$scheduler=[Activator]::CreateInstance([Type]::GetTypeFromProgID('Schedule.Service'));$scheduler.Connect();"
    )
    _LOOKUP = _CONNECT + (
        "$task=$null;try{"
        "$folder=$scheduler.GetFolder($args[0]);$task=$folder.GetTask($args[1])"
        "}catch{if($_.Exception.GetBaseException().HResult -notin "
        "@(-2147024894,-2147024893)){throw}};"
    )

    def __init__(
        self, *, runner: SchedulerCommandRunner | None = None,
        job_namespace: str | None = None,
        start_timeout: float = 15, clock=time.monotonic, sleep=time.sleep,
    ) -> None:
        _validate_job_namespace(job_namespace)
        self.job_namespace = job_namespace
        self._runner = runner or _scheduler_subprocess_runner
        self._start_timeout, self._clock, self._sleep = start_timeout, clock, sleep

    def capture(self, job_code: str) -> bytes | None:
        name = _windows_task_name(job_code, self.job_namespace)
        result = self._run(_powershell_command(
            self._LOOKUP +
            "if($null -eq $task){exit 3};"
            "$xml=$task.Xml;"
            "$running=if($task.State -eq 4){'true'}else{'false'};"
            "$definition=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($xml));"
            "[Console]::Out.WriteLine('{\"schema\":\"daemon.windows-task-capture.v1\",\"running\":'+"
            "$running+',\"definition\":\"'+$definition+'\"}')",
            self._TASK_PATH,
            name,
        ))
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise RuntimeError("Task Scheduler capture failed.") from HostCommandError(result.returncode, result.stderr)
        return _windows_task_capture(result.stdout)

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        _validate_native_job(job, self.job_namespace)
        if definition != render_job_definition(job, "windows"):
            raise ValueError("Scheduled task definition differs from the canonical job.")
        self._register(job.code, definition)
        if job.persistent:
            self._start(job.code)

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        current = self.capture(job.code)
        if current is None:
            return False
        payload = _load_windows_task_capture(current)
        return (
            _windows_definition_signature(payload["definition"])
            == _windows_definition_signature(definition)
            and (not job.persistent or payload["running"])
        )

    def verify_active(self, job: BackgroundJob, definition: bytes) -> None:
        deadline = self._clock() + self._start_timeout
        reason = "missing"
        while True:
            captured = self.capture(job.code)
            if captured is None:
                break
            current = _load_windows_task_capture(captured)
            if _windows_definition_signature(current["definition"]) != _windows_definition_signature(definition):
                reason = "definition_mismatch"
                break
            if not job.persistent or current["running"]:
                return
            reason = "not_running"
            if self._clock() >= deadline:
                break
            self._sleep(min(.5, max(0, deadline - self._clock())))
        # Read scheduler metadata before rollback removes the owned task. Never
        # include its XML, command, environment or worker logs in this report.
        name = _windows_task_name(job.code, self.job_namespace)
        state = result_code = None
        try:
            result = self._run(_powershell_command(
                self._LOOKUP +
                "if($null -eq $task){exit 3};"
                "$state=[int]$task.State;$result=[long]$task.LastTaskResult;"
                "[Console]::Out.WriteLine('{\"state\":'+$state+',\"result\":'+$result+'}')",
                self._TASK_PATH, name))
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if not isinstance(data, dict):
                    raise ValueError("Invalid scheduler status")
                if type(data.get("state")) is int and data["state"] in range(5):
                    state = data["state"]
                if type(data.get("result")) is int and -(2**31) <= data["result"] < 2**32:
                    result_code = data["result"]
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass
        detail = f"reason={reason}; task_state={state}; last_result={result_code}"
        if result_code is not None:
            detail += f" (0x{result_code & 0xffffffff:08X})"
        error = RuntimeError(f"Background job is not active: {job.code}. {detail}")
        if result_code is not None:
            error.exit_code = result_code
        raise error

    def restore(self, job_code: str, capture: bytes | None) -> None:
        _validate_job_code(job_code, self.job_namespace)
        if capture is not None:
            _load_windows_task_capture(capture)
        self._stop(job_code)
        if capture is None:
            name = _windows_task_name(job_code, self.job_namespace)
            result = self._run(_powershell_command(
                self._LOOKUP +
                "if($null -ne $task){$folder.DeleteTask($args[1],0)}",
                self._TASK_PATH,
                name,
            ))
            if result.returncode != 0:
                raise RuntimeError("Task Scheduler removal failed.") from HostCommandError(result.returncode, result.stderr)
            return
        payload = _load_windows_task_capture(capture)
        self._register(job_code, payload["definition"])
        if payload["running"]:
            self._start(job_code)

    def _register(self, job_code: str, definition: bytes) -> None:
        name = _windows_task_name(job_code, self.job_namespace)
        encoded = base64.b64encode(_windows_definition_utf16(definition)).decode("ascii")
        result = self._run(_powershell_command(
            self._CONNECT +
            "try{$folder=$scheduler.GetFolder($args[0])}catch{"
            "if($_.Exception.GetBaseException().HResult -notin @(-2147024894,-2147024893)){throw};"
            "$folder=$scheduler.GetFolder('\\').CreateFolder($args[0].Trim('\\'))};"
            "$xml=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($args[2]));"
            "$null=$folder.RegisterTask($args[1],$xml,6,$null,$null,3,$null)",
            self._TASK_PATH,
            name,
            encoded,
        ))
        if result.returncode != 0:
            raise RuntimeError("Task Scheduler registration failed.") from HostCommandError(result.returncode, result.stderr)

    def _start(self, job_code: str) -> None:
        name = _windows_task_name(job_code, self.job_namespace)
        result = self._run(_powershell_command(
            self._CONNECT +
            "$null=$scheduler.GetFolder($args[0]).GetTask($args[1]).Run($null)",
            self._TASK_PATH,
            name,
        ))
        if result.returncode != 0:
            raise RuntimeError("Task Scheduler could not start the companion worker.") from HostCommandError(result.returncode, result.stderr)

    def _stop(self, job_code: str) -> None:
        name = _windows_task_name(job_code, self.job_namespace)
        result = self._run(_powershell_command(
            self._LOOKUP +
            "if($null -ne $task -and $task.State -eq 4){$task.Stop(0)}",
            self._TASK_PATH,
            name,
        ))
        if result.returncode != 0:
            raise RuntimeError("Task Scheduler could not stop the companion worker.") from HostCommandError(result.returncode, result.stderr)

    def _run(self, command: Sequence[str]) -> SchedulerCommandResult:
        return self._runner(command, _scheduler_environment())


def background_services_step(layout: BackgroundServicesLayout) -> PlanStep:
    _validate_background_plan_layout(layout)
    return PlanStep(
        BACKGROUND_SERVICES_STEP,
        "Register background maintenance and provisioned local memory search through the native user scheduler.",
        (
            f"{scoped_job_code(AFTER_TURN_JOB, layout.job_namespace)}: persistent",
            f"{scoped_job_code(NIGHTLY_JOB, layout.job_namespace)}: daily at 04:00",
            *(() if layout.runtime.installation_scope is None else
              (f"{scoped_job_code(EMBEDDINGS_JOB, layout.job_namespace)}: private local search when provisioned",)),
        ),
    )


def build_background_jobs(layout: BackgroundServicesLayout, *, legacy: bool = False) -> tuple[BackgroundJob, ...]:
    _validate_background_layout(layout)
    user_id = _plain_value(layout.user_id, "User id", 128)
    release = verify_current_release(layout.runtime)
    environment = resolve_release_environment(layout.runtime_environment)
    python = release_environment_interpreter(environment, layout.target_os)
    from daemons_companion.runtime.bootstrap import _source_io_path
    extended = layout.target_os == "windows" and not legacy
    path = _source_io_path if extended else lambda value: value
    launch_python = path(python)
    launch_source = path(release / "src")
    state = layout.runtime.state_root
    bootstrap = (
        "import runpy,sys;"
        "sys.dont_write_bytecode=True;"
        f"sys.path.insert(0,{str(launch_source)!r});"
        + (f"sys.path.insert(1,{str(path(release))!r});"
           "from pathlib import Path;"
           "from daemons_companion.runtime.bootstrap import _source_io_path;"
           "sys.executable=str(_source_io_path(Path(sys.executable)));" if extended else "")
        + f"runpy.run_module({service_entry.__name__!r},run_name='__main__')"
    )
    if not legacy and layout.runtime.installation_scope is not None:
        from daemons_companion.runtime.process_settings import private_diagnostic_setup
        bootstrap = bootstrap.rsplit("runpy.run_module", 1)[0] + (
            f"import {service_entry.__name__} as entry;"
            "entry._configure(entry._parser().parse_args());"
            + private_diagnostic_setup(state) + "entry.main()"
        )
    common = (
        "-B", "-c", bootstrap, "{service}",
        "--release", str(path(release)),
        "--python", str(path(python)),
        "--memory-root", str(path(layout.runtime.memory_root)),
        "--state-root", str(path(state)),
        "--provider-config", str(path(state / "config" / "provider.json")),
        "--runtime-revision", release.name,
        "--user-id", user_id,
    )
    if layout.runtime.installation_scope is not None:
        common += ("--installation-attempt", str(path(layout.runtime.installation_scope.attempt)))
    jobs = (
        BackgroundJob(
            scoped_job_code(AFTER_TURN_JOB, layout.job_namespace),
            str(launch_python),
            tuple("after-turn" if value == "{service}" else value for value in common),
            str(release),
            str(state / "after-turn.log"),
            True,
        ),
        BackgroundJob(
            scoped_job_code(NIGHTLY_JOB, layout.job_namespace),
            str(launch_python),
            tuple("nightly" if value == "{service}" else value for value in common),
            str(release),
            str(state / "nightly.log"),
            False,
            4,
        ),
    )
    scope = layout.runtime.installation_scope
    if scope is not None:
        from daemons_companion.setup.embeddings import configuration, build_service_job
        if configuration(scope) is not None:
            # Start local search before its consumers; rollback stops it last.
            jobs = (build_service_job(scope, launch_python, release, layout.job_namespace), *jobs)
    return jobs


def _validate_background_layout(layout: BackgroundServicesLayout) -> None:
    validate_companion_home(layout.runtime.companion_home, layout.runtime.installation_scope)
    _validate_background_plan_layout(layout)


def _validate_background_plan_layout(layout: BackgroundServicesLayout) -> None:
    _validate_job_namespace(layout.job_namespace)
    if layout.runtime.installation_scope is not None:
        layout.runtime.installation_scope.validate_namespace(layout.job_namespace)
    if layout.target_os not in {"macos", "windows"}:
        raise ValueError("Background services support macOS and Windows only.")
    if layout.runtime_environment.runtime != layout.runtime:
        raise ValueError("Runtime environment does not belong to this runtime.")
    if layout.runtime_environment.target_os != layout.target_os:
        raise ValueError("Runtime environment target differs from the scheduler target.")
    _plain_value(layout.user_id, "User id", 128)


def render_job_definition(job: BackgroundJob, target_os: str) -> bytes:
    if target_os == "macos":
        return _render_launchd(job)
    if target_os == "windows":
        return _render_windows_task(job)
    raise ValueError("Background services support macOS and Windows only.")


class CompanionBackgroundServicesAdapter:
    """Apply one reversible pair of registrations to an injected scheduler."""

    def __init__(
        self,
        layout: BackgroundServicesLayout,
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
        jobs = self._validate(step)
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            raise ValueError("Background service snapshot directory is invalid.")
        before: dict[str, str | None] = {}
        for job in jobs:
            capture = self.backend.capture(job.code)
            if capture is not None and len(capture) > _MAX_CAPTURE_BYTES:
                raise ValueError("Scheduler capture is too large.")
            before[job.code] = (
                base64.b64encode(capture).decode("ascii") if capture is not None else None
            )
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "target_os": self.layout.target_os,
            "jobs": [job.public_dict() for job in jobs],
            "before": before,
        }
        atomic_write_text(
            snapshot_dir / "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        jobs = self._validate(step)
        snapshot = self._snapshot_dirs.get(step.code)
        if snapshot is None:
            raise RuntimeError("Background service snapshot is required before apply.")
        manifest = _load_manifest(snapshot)
        if manifest["jobs"] != [job.public_dict() for job in jobs]:
            raise RuntimeError("Background service inputs changed after snapshot.")
        scope = self.layout.runtime.installation_scope
        if scope is not None:
            from daemons_companion.setup.embeddings import configuration, require_free_port
            data = configuration(scope)
            if data is not None:
                # Fresh installs must not mistake an unrelated Ollama endpoint
                # for their own service after the acquisition probe stopped.
                require_free_port(data['port'])
        for job in jobs:
            self.backend.install(job, render_job_definition(job, self.layout.target_os))
            self._mutation_hook(job.code)

    def verify(self, step: PlanStep) -> None:
        for job in self._validate(step):
            definition = render_job_definition(job, self.layout.target_os)
            if isinstance(self.backend, WindowsTaskSchedulerBackend):
                self.backend.verify_active(job, definition)
            elif not self.backend.matches(job, definition):
                raise RuntimeError(f"Background job is not active: {job.code}")
        if self.layout.runtime.installation_scope is not None:
            from daemons_companion.setup.embeddings import configuration, wait_ready
            data = configuration(self.layout.runtime.installation_scope)
            if data is not None:
                wait_ready(data['port'])

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        jobs = self._validate(step)
        manifest = _load_manifest(snapshot_dir)
        if manifest["jobs"] != [job.public_dict() for job in jobs]:
            raise RuntimeError("Background service snapshot no longer matches the plan.")
        conflicts = []
        for job in jobs:
            capture = self.backend.capture(job.code)
            before = _decode_capture(manifest["before"][job.code])
            expected = render_job_definition(job, self.layout.target_os)
            # A crashed process is not a concurrent definition change. Keep
            # the exact command/identity/trigger checks while ignoring liveness
            # only for Windows rollback (registration/start verification is strict).
            comparison = replace(job, persistent=False) if self.layout.target_os == "windows" else job
            if capture != before and not self.backend.matches(comparison, expected):
                conflicts.append(job.code)
        if conflicts:
            raise RuntimeError("Background scheduler has concurrent changes; rollback was refused.")
        for job in reversed(jobs):
            self.backend.restore(job.code, _decode_capture(manifest["before"][job.code]))

    def _validate(self, step: PlanStep) -> tuple[BackgroundJob, ...]:
        if step != background_services_step(self.layout):
            raise ValueError("Background services require the exact canonical step.")
        return build_background_jobs(self.layout)


def _render_launchd(job: BackgroundJob) -> bytes:
    program = [job.executable, *job.arguments]
    if not job.persistent:
        program = ["/usr/bin/caffeinate", "-s", *program]
    document: dict[str, object] = {
        "Label": job.code,
        "ProgramArguments": program,
        "WorkingDirectory": job.working_directory,
        "RunAtLoad": job.persistent,
        "StandardOutPath": job.log_path,
        "StandardErrorPath": job.log_path,
        "ProcessType": "Background",
    }
    if job.persistent:
        # launchd restarts a crashed service without limit. One minute, as on
        # Windows, keeps a crash loop slow; a service that ran longer than
        # this restarts at once.
        document.update({"KeepAlive": True, "ThrottleInterval": 60})
    else:
        document["StartCalendarInterval"] = {"Hour": job.hour, "Minute": 0}
    return plistlib.dumps(document, sort_keys=True)


def _windows_scheduler_user_sid() -> str | None:
    # Cross-platform review can render an unbound template. Native execution
    # always resolves the process token, never an environment-supplied name.
    if os.name != "nt":
        return None
    from daemon_core.platform_support import _user_sid

    return _user_sid()


# Windows starts a console program, such as python.exe, in a visible window
# when the task runs in the user's session. Tasks run the windowless
# pythonw.exe of the same environment instead. The prelude gives the script
# null output streams and starts its child programs without a window too.
# It has no spaces, double quotes or backslashes, so the command line keeps
# it verbatim and the signature can recognise it.
_WINDOWLESS_PRELUDE = (
    "os=__import__('os');subprocess=__import__('subprocess');sys=__import__('sys');"
    "sys.stdout=[sys.stdout,open(os.devnull,'w')][sys.stdout==None];"
    "sys.stderr=[sys.stderr,open(os.devnull,'w')][sys.stderr==None];"
    "_daemon_popen=subprocess.Popen.__init__;"
    "subprocess.Popen.__init__=lambda*a,**k:_daemon_popen(*a,**{**k,'creationflags':"
    "((k.get('creationflags')or(0))|(0x08000000*(not((k.get('creationflags')or(0))&24))))});"
)


def _windowless_action(job: BackgroundJob) -> tuple[str, tuple[str, ...]]:
    executable, arguments = job.executable, list(job.arguments)
    stem = executable[: -len("python.exe")] if executable.lower().endswith("python.exe") else None
    if stem is None or "-c" not in arguments[:-1] or (stem and stem[-1] not in "\\/"):
        return executable, job.arguments
    script = arguments.index("-c") + 1
    arguments[script] = _WINDOWLESS_PRELUDE + arguments[script]
    return stem + "pythonw.exe", tuple(arguments)


def _render_windows_task(job: BackgroundJob) -> bytes:
    user_sid = _windows_scheduler_user_sid()
    ElementTree.register_namespace("", _TASK_NAMESPACE)
    task = ElementTree.Element(f"{{{_TASK_NAMESPACE}}}Task", {"version": "1.4"})
    triggers = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Triggers")
    trigger_name = "LogonTrigger" if job.persistent else "CalendarTrigger"
    trigger = ElementTree.SubElement(triggers, f"{{{_TASK_NAMESPACE}}}{trigger_name}")
    ElementTree.SubElement(trigger, f"{{{_TASK_NAMESPACE}}}Enabled").text = "true"
    if job.persistent and user_sid is not None:
        # An omitted user means *every* logon and requires administrator rights.
        ElementTree.SubElement(trigger, f"{{{_TASK_NAMESPACE}}}UserId").text = user_sid
    if not job.persistent:
        ElementTree.SubElement(trigger, f"{{{_TASK_NAMESPACE}}}StartBoundary").text = f"2000-01-01T{job.hour:02d}:00:00"
        schedule = ElementTree.SubElement(trigger, f"{{{_TASK_NAMESPACE}}}ScheduleByDay")
        ElementTree.SubElement(schedule, f"{{{_TASK_NAMESPACE}}}DaysInterval").text = "1"
    principals = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Principals")
    principal = ElementTree.SubElement(principals, f"{{{_TASK_NAMESPACE}}}Principal", {"id": "Author"})
    if user_sid is not None:
        ElementTree.SubElement(principal, f"{{{_TASK_NAMESPACE}}}UserId").text = user_sid
    ElementTree.SubElement(principal, f"{{{_TASK_NAMESPACE}}}LogonType").text = "InteractiveToken"
    ElementTree.SubElement(principal, f"{{{_TASK_NAMESPACE}}}RunLevel").text = "LeastPrivilege"
    settings = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Settings")
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("ExecutionTimeLimit", "PT0S" if job.persistent else "PT2H"),
        ("Enabled", "true"),
    ):
        ElementTree.SubElement(settings, f"{{{_TASK_NAMESPACE}}}{key}").text = value
    if job.persistent:
        restart = ElementTree.SubElement(settings, f"{{{_TASK_NAMESPACE}}}RestartOnFailure")
        ElementTree.SubElement(restart, f"{{{_TASK_NAMESPACE}}}Interval").text = "PT1M"
        ElementTree.SubElement(restart, f"{{{_TASK_NAMESPACE}}}Count").text = "3"
    actions = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Actions", {"Context": "Author"})
    execute = ElementTree.SubElement(actions, f"{{{_TASK_NAMESPACE}}}Exec")
    executable, arguments = _windowless_action(job)
    ElementTree.SubElement(execute, f"{{{_TASK_NAMESPACE}}}Command").text = executable
    ElementTree.SubElement(execute, f"{{{_TASK_NAMESPACE}}}Arguments").text = subprocess.list2cmdline(list(arguments))
    ElementTree.SubElement(execute, f"{{{_TASK_NAMESPACE}}}WorkingDirectory").text = job.working_directory
    return ElementTree.tostring(task, encoding="utf-16", xml_declaration=True)


def _plain_value(value: str, label: str, maximum: int) -> str:
    result = value.strip()
    if not result or len(result) > maximum or any(char in result for char in "\r\n\0"):
        raise ValueError(f"{label} is invalid.")
    return result


def _load_manifest(snapshot_dir: Path) -> dict[str, object]:
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Background service snapshot is unavailable.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != _SNAPSHOT_SCHEMA:
        raise RuntimeError("Background service snapshot is invalid.")
    return payload


def _decode_capture(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Scheduler capture is invalid.")
    try:
        result = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise RuntimeError("Scheduler capture is invalid.") from error
    if len(result) > _MAX_CAPTURE_BYTES:
        raise RuntimeError("Scheduler capture is too large.")
    return result


def _validate_job_namespace(namespace: str | None) -> None:
    if namespace is not None and (
        not isinstance(namespace, str) or re.fullmatch(r"[0-9a-f]{32}", namespace) is None
    ):
        raise ValueError("Scheduler namespace must contain 32 lowercase hexadecimal characters.")


def scoped_job_code(job_code: str, namespace: str | None = None) -> str:
    """Name an explicit qualification scope without accepting arbitrary job labels."""
    _validate_job_namespace(namespace)
    if job_code not in {AFTER_TURN_JOB, NIGHTLY_JOB, OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB, HERMES_GATEWAY_JOB, EMBEDDINGS_JOB}:
        raise ValueError("Scheduler job identifier is not managed by the companion.")
    if namespace is None:
        return job_code
    return f"com.daemonharness.test.{namespace}.{job_code.rsplit('.', 1)[-1]}"


def _validate_job_code(job_code: str, namespace: str | None = None) -> None:
    allowed = {scoped_job_code(code, namespace) for code in (
        AFTER_TURN_JOB, NIGHTLY_JOB, OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB, HERMES_GATEWAY_JOB, EMBEDDINGS_JOB,
    )}
    if job_code not in allowed:
        raise ValueError("Scheduler job identifier is outside the configured scope.")


def _validate_launchd_label(definition: bytes, job_code: str) -> None:
    try:
        payload = plistlib.loads(definition)
    except (ValueError, plistlib.InvalidFileException) as error:
        raise RuntimeError("LaunchAgent definition is invalid.") from error
    if not isinstance(payload, dict) or payload.get("Label") != job_code:
        raise RuntimeError("LaunchAgent Label differs from the configured scope.")


def _validate_native_job(job: BackgroundJob, namespace: str | None = None) -> None:
    _validate_job_code(job.code, namespace)
    if not Path(job.executable).is_absolute() or not Path(job.working_directory).is_absolute():
        raise ValueError("Background job paths must be absolute.")


def _launchd_capture(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("LaunchAgent capture is invalid.") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "daemon.launchd-capture.v1"
        or set(payload) != {"schema", "loaded", "definition"}
        or not isinstance(payload.get("loaded"), bool)
        or payload.get("definition") is not None
        and not isinstance(payload.get("definition"), str)
    ):
        raise RuntimeError("LaunchAgent capture is invalid.")
    return payload


def _windows_task_name(job_code: str, namespace: str | None = None) -> str:
    _validate_job_code(job_code, namespace)
    if namespace is not None:
        return job_code
    return {
        AFTER_TURN_JOB: "Daemon Harness After-Turn",
        NIGHTLY_JOB: "Daemon Harness Nightly",
        OPENCLAW_API_JOB: "Daemon Harness OpenClaw API",
        HERMES_GATEWAY_JOB: "Daemon Harness Hermes Gateway",
        OPENCLAW_GATEWAY_JOB: "Daemon Harness OpenClaw Gateway",
    }[job_code]


def _powershell_command(script: str, *arguments: str) -> tuple[str, ...]:
    if any("\0" in argument or "\r" in argument or "\n" in argument for argument in arguments):
        raise ValueError("PowerShell argument is invalid.")
    return (
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        f"& {{ {script} }}",
        *arguments,
    )


def _windows_task_identity(value: str) -> str:
    # Task Scheduler exports the logon SID as DOMAIN\user, while its principal
    # stays a SID. Resolve the account, never discard identity from comparison.
    if os.name != "nt" or re.fullmatch(r"S-1-(?:[0-9]+-)*[0-9]+", value):
        return value
    from daemon_core.platform_support import _account_sid

    return _account_sid(value)


def _windows_definition_signature(content: bytes) -> tuple[tuple[str, str], ...]:
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, UnicodeError) as error:
        raise RuntimeError("Scheduled task definition is invalid.") from error
    wanted = {
        "LogonTrigger",
        "CalendarTrigger",
        "StartBoundary",
        "DaysInterval",
        "LogonType",
        "RunLevel",
        "MultipleInstancesPolicy",
        "StartWhenAvailable",
        "ExecutionTimeLimit",
        "Interval",
        "Count",
        "Command",
        "Arguments",
        "WorkingDirectory",
    }
    values = []
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in wanted:
            values.append((name, _console_form(name, (element.text or "").strip())))
    if not any(name == "Command" and value for name, value in values):
        raise RuntimeError("Scheduled task definition has no command.")
    # Identity belongs to its parent: swapping the execution user and the logon
    # user, or broadening the trigger to everyone, is a real concurrent change.
    for parent in root.iter():
        parent_name = parent.tag.rsplit("}", 1)[-1]
        if parent_name in {"Principal", "LogonTrigger"}:
            for child in parent:
                name = child.tag.rsplit("}", 1)[-1]
                if name in {"UserId", "GroupId"}:
                    values.append((f"{parent_name}/{name}", _windows_task_identity((child.text or "").strip())))
    # Task Scheduler reorders XML siblings and omits the default run level
    # when exporting registered tasks. Compare the semantic values instead.
    if not any(name == "RunLevel" for name, _ in values):
        values.append(("RunLevel", "LeastPrivilege"))
    return tuple(sorted(values))


def _console_form(name: str, value: str) -> str:
    """Compare a windowless task with the console form registered before it."""
    if name == "Command" and value.lower().endswith(("\\pythonw.exe", "/pythonw.exe")):
        return value[: -len("pythonw.exe")] + "python.exe"
    if name == "Arguments":
        return value.replace(_WINDOWLESS_PRELUDE, "", 1)
    return value


def _windows_definition_utf16(content: bytes) -> bytes:
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, UnicodeError) as error:
        raise RuntimeError("Scheduled task definition is invalid.") from error
    return ElementTree.tostring(root, encoding="utf-16", xml_declaration=True)


def _windows_task_capture(content: str) -> bytes:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError("Scheduled task capture is invalid.") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "running", "definition"}
        or payload.get("schema") != "daemon.windows-task-capture.v1"
        or not isinstance(payload.get("running"), bool)
        or not isinstance(payload.get("definition"), str)
    ):
        raise RuntimeError("Scheduled task capture is invalid.")
    try:
        definition = base64.b64decode(payload["definition"], validate=True)
    except ValueError as error:
        raise RuntimeError("Scheduled task capture is invalid.") from error
    _windows_definition_signature(definition)
    normalized = {
        "schema": payload["schema"],
        "running": payload["running"],
        "definition": base64.b64encode(definition).decode("ascii"),
    }
    return (json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_windows_task_capture(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Scheduled task capture is invalid.") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "running", "definition"}
        or payload.get("schema") != "daemon.windows-task-capture.v1"
        or not isinstance(payload.get("running"), bool)
        or not isinstance(payload.get("definition"), str)
    ):
        raise RuntimeError("Scheduled task capture is invalid.")
    try:
        definition = base64.b64decode(payload["definition"], validate=True)
    except ValueError as error:
        raise RuntimeError("Scheduled task capture is invalid.") from error
    _windows_definition_signature(definition)
    return {"running": payload["running"], "definition": definition}


def _scheduler_environment() -> dict[str, str]:
    allowed = frozenset({
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
    })
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    if os.name == "nt":
        system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
        if not system_root:
            raise RuntimeError("Windows system directory is unavailable.")
        # Keep any incidental module lookup limited to OS modules. The fixed
        # scripts themselves use only language intrinsics and .NET, no autoload.
        environment["PSModulePath"] = os.path.join(
            system_root, "System32", "WindowsPowerShell", "v1.0", "Modules"
        )
    return environment


def _scheduler_subprocess_runner(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> SchedulerCommandResult:
    completed = subprocess.run(
        list(command),
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return SchedulerCommandResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


__all__ = [
    "AFTER_TURN_JOB",
    "BACKGROUND_SERVICES_STEP",
    "NIGHTLY_JOB",
    "OPENCLAW_API_JOB",
    "OPENCLAW_GATEWAY_JOB",
    "BackgroundJob",
    "BackgroundServicesLayout",
    "CompanionBackgroundServicesAdapter",
    "MacLaunchdSchedulerBackend",
    "SchedulerBackend",
    "SchedulerCommandResult",
    "WindowsTaskSchedulerBackend",
    "background_services_step",
    "build_background_jobs",
    "render_job_definition",
    "scoped_job_code",
]
