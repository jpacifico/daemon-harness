from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from daemons_companion.runtime.services import (
    AFTER_TURN_JOB,
    NIGHTLY_JOB,
    OPENCLAW_API_JOB,
    BackgroundJob,
    BackgroundServicesLayout,
    CompanionBackgroundServicesAdapter,
    MacLaunchdSchedulerBackend,
    SchedulerCommandResult,
    WindowsTaskSchedulerBackend,
    background_services_step,
    build_background_jobs,
    render_job_definition,
    scoped_job_code,
)
from daemons_companion.setup.plan import HostKind, HostProbe, SetupPlan
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)
from daemons_companion.runtime.environment import (
    CommandResult,
    CompanionRuntimeEnvironmentAdapter,
    CompanionRuntimeEnvironmentLayout,
)
from daemons_companion.setup.transaction import (
    PlanConfirmationError,
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)
from daemons_companion.runtime import service_entry as background_entry


class FakeEnvironmentRunner:
    def __init__(self, target_os: str) -> None:
        self.target_os = target_os

    def __call__(self, command, environment) -> CommandResult:
        command = list(command)
        if command[1:] == ["--version"]:
            return CommandResult(0, "uv 0.12.16 synthetic\n")
        if command[1:3] == ["python", "install"]:
            root = Path(command[command.index("--install-dir") + 1])
            folder = root / ("cpython-3.12.13-windows-x86_64-none" if self.target_os == "windows" else "cpython")
            folder.mkdir(parents=True)
            (folder / ("python.exe" if self.target_os == "windows" else "python")).write_text("managed", encoding="utf-8")
            return CommandResult(0)
        if command[1:2] == ["venv"]:
            venv = Path(command[2])
            interpreter = venv / (
                "Scripts/python.exe" if self.target_os == "windows" else "bin/python"
            )
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("python", encoding="utf-8")
            interpreter.chmod(0o700)
            if self.target_os == "windows":
                home = Path(command[command.index("--python") + 1]).parent
                (venv / "pyvenv.cfg").write_text(f"home = {home}\nrelocatable = true\n", encoding="utf-8")
            return CommandResult(0)
        if command[1:3] == ["pip", "sync"]:
            interpreter = Path(command[command.index("--python") + 1])
            marker = interpreter.parent.parent / "site-packages/locked.txt"
            marker.parent.mkdir(parents=True)
            marker.write_text("locked", encoding="utf-8")
            return CommandResult(0)
        if command[0].endswith(("/python", "python.exe")):
            return CommandResult(0, "daemon-runtime-ok\n")
        return CommandResult(1, stderr="unexpected command")


class FakeScheduler:
    def __init__(self, target_os: str, initial: dict[str, bytes] | None = None) -> None:
        self.target_os = target_os
        self.state = dict(initial or {})
        self.installs: list[str] = []
        self.restores: list[str] = []

    def capture(self, job_code: str) -> bytes | None:
        return self.state.get(job_code)

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        self.state[job.code] = definition
        self.installs.append(job.code)

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        return self.state.get(job.code) == definition

    def restore(self, job_code: str, capture: bytes | None) -> None:
        if capture is None:
            self.state.pop(job_code, None)
        else:
            self.state[job_code] = capture
        self.restores.append(job_code)


class FakeLaunchctlRunner:
    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(self, command, environment):
        command = tuple(command)
        self.calls.append((command, dict(environment)))
        action = command[1]
        label = command[-1].rsplit("/", 1)[-1].removesuffix(".plist")
        if action == "print":
            return SchedulerCommandResult(0 if label in self.loaded else 113)
        if action == "bootout":
            self.loaded.discard(label)
            return SchedulerCommandResult(0)
        if action == "bootstrap":
            self.loaded.add(label)
            return SchedulerCommandResult(0)
        return SchedulerCommandResult(2)


class FakePowerShellRunner:
    def __init__(self) -> None:
        self.tasks: dict[str, bytes] = {}
        self.running: set[str] = set()
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(self, command, environment):
        command = tuple(command)
        self.calls.append((command, dict(environment)))
        script = command[7]
        name = command[9]
        if "$xml=$task.Xml" in script:
            if name not in self.tasks:
                return SchedulerCommandResult(3)
            return SchedulerCommandResult(
                0,
                json.dumps(
                    {
                        "schema": "daemon.windows-task-capture.v1",
                        "running": name in self.running,
                        "definition": __import__("base64").b64encode(
                            self.tasks[name]
                        ).decode("ascii"),
                    }
                ),
            )
        if ".RegisterTask(" in script:
            self.tasks[name] = __import__("base64").b64decode(command[10])
            return SchedulerCommandResult(0)
        if ".Run($null)" in script:
            self.running.add(name)
            return SchedulerCommandResult(0)
        if ".Stop(0)" in script:
            self.running.discard(name)
            return SchedulerCommandResult(0)
        if ".DeleteTask(" in script:
            self.tasks.pop(name, None)
            self.running.discard(name)
            return SchedulerCommandResult(0)
        return SchedulerCommandResult(2)


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "example==1 \\\n    --hash=sha256:" + "0" * 64 + "\n",
        "requirements.txt": "example==1\n",
        "src/daemon_core/__init__.py": "# synthetic\n",
        "integrations/hermes_plugin/__init__.py": "# memory\n",
        "integrations/hermes_voice_plugin/__init__.py": "# voice\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\n",
        "src/daemons_companion/runtime/service_entry.py": "# entry\n",
        "src/daemon_core/nightly_maintenance.py": "# nightly\n",
        "src/daemons_companion/runtime/services.py": "# services\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if path.name == "service.sh" else 0o644)
    return root.absolute()


def _fixture(tmp_path: Path, target_os: str):
    home = (tmp_path / "companion").absolute()
    create_disposable_companion_marker(home)
    uv = (tmp_path / ("uv.exe" if target_os == "windows" else "uv")).absolute()
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o700)
    runtime = CompanionRuntimeLayout(home, _bundle(tmp_path / "bundle"))
    runtime_step = __import__(
        "daemons_companion.runtime.release", fromlist=["RUNTIME_LAYOUT_STEP"]
    ).RUNTIME_LAYOUT_STEP
    from daemons_companion.setup.plan import PlanStep

    snapshot = tmp_path / "runtime-snapshot"
    snapshot.mkdir()
    runtime_adapter = CompanionRuntimeAdapter(runtime)
    step = PlanStep(runtime_step, "runtime", ())
    runtime_adapter.snapshot(step, snapshot)
    runtime_adapter.apply(step)
    runtime_adapter.verify(step)

    environment = CompanionRuntimeEnvironmentLayout(
        runtime,
        uv,
        hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os,
    )
    environment_snapshot = tmp_path / "environment-snapshot"
    environment_snapshot.mkdir()
    environment_adapter = CompanionRuntimeEnvironmentAdapter(
        environment,
        runner=FakeEnvironmentRunner(target_os),
    )
    environment_step = PlanStep("runtime-environment", "environment", ())
    environment_adapter.snapshot(environment_step, environment_snapshot)
    environment_adapter.apply(environment_step)
    environment_adapter.verify(environment_step)
    (runtime.state_root / "config").mkdir(parents=True)
    (runtime.state_root / "config/provider.json").write_text("{}\n", encoding="utf-8")
    layout = BackgroundServicesLayout(runtime, environment, target_os, "synthetic-user")
    plan = SetupPlan(
        HostProbe(HostKind.HERMES, True, True, config_path="synthetic"),
        "Iris",
        "feminine",
        {"kind": "synthetic"},
        (background_services_step(layout),),
    )
    return layout, plan


def _execute(tmp_path: Path, plan: SetupPlan, adapter, transaction="services"):
    return TransactionEngine(tmp_path / "transactions").execute(
        plan,
        adapter,
        transaction_id=transaction,
        confirmed_plan_digest=public_plan_digest(plan),
    )


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_jobs_use_only_the_verified_private_release(tmp_path: Path, target_os: str):
    layout, _plan = _fixture(tmp_path, target_os)
    jobs = build_background_jobs(layout)

    assert [job.code for job in jobs] == [AFTER_TURN_JOB, NIGHTLY_JOB]
    assert jobs[0].persistent is True
    assert jobs[1].persistent is False and jobs[1].hour == 4
    for job in jobs:
        joined = " ".join((job.executable, *job.arguments))
        assert str(layout.runtime.companion_home) in joined
        assert "daemons_companion.runtime.service_entry" in joined
        assert job.arguments[:2] == ("-B", "-c")
        assert str(layout.runtime.runtime_root / "releases") in job.arguments[2]
        assert "sys.path.insert" in job.arguments[2]
        assert "sys.dont_write_bytecode=True" in job.arguments[2]
        assert "synthetic-user" in joined
        assert "API_KEY" not in joined
        assert "secret" not in joined
        assert str(layout.runtime.bundle_root) not in joined


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_a_companion_worker_launcher_reaches_its_entry(tmp_path: Path, monkeypatch, target_os: str):
    """Run the launcher of an installation made by the app up to its entry.

    Its import is text inside the task definition, so only running it shows
    that the module it names still exists.
    """
    import sys
    import daemons_companion.runtime.services as services
    from daemons_companion.setup import embeddings
    from daemon_core import scene_model

    layout, _plan = _fixture(tmp_path, target_os)
    scope = SimpleNamespace(attempt=tmp_path / "attempt")
    layout = replace(layout, runtime=replace(layout.runtime, installation_scope=scope))
    monkeypatch.setattr(services, "_validate_background_layout", lambda layout: None)
    monkeypatch.setattr(embeddings, "configuration", lambda scope: None)
    worker = build_background_jobs(layout)[0]
    calls = []
    monkeypatch.setattr(background_entry, "_parser", lambda: SimpleNamespace(parse_args=lambda: "arguments"))
    monkeypatch.setattr(background_entry, "_configure", lambda args: calls.append(args))
    monkeypatch.setattr(background_entry, "main", lambda: calls.append("main"))
    monkeypatch.setattr(scene_model, "SHADOW_LOG", scene_model.SHADOW_LOG)
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(worker.arguments[2], {})
    assert calls == ["arguments", "main"]


def test_launchd_definitions_are_one_persistent_and_one_nightly_job(tmp_path: Path):
    layout, _plan = _fixture(tmp_path, "macos")
    after_turn, nightly = build_background_jobs(layout)
    worker = plistlib.loads(render_job_definition(after_turn, "macos"))
    dream = plistlib.loads(render_job_definition(nightly, "macos"))

    assert worker["KeepAlive"] is True
    assert worker["RunAtLoad"] is True
    assert worker["ProgramArguments"][0] == after_turn.executable
    assert "EnvironmentVariables" not in worker
    assert dream["RunAtLoad"] is False
    assert dream["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert dream["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-s"]
    assert dream["ProgramArguments"][2] == nightly.executable


def test_windows_definitions_use_logon_and_daily_triggers_without_privilege(tmp_path: Path):
    layout, _plan = _fixture(tmp_path, "windows")
    worker, nightly = build_background_jobs(layout)
    worker_text = render_job_definition(worker, "windows").decode("utf-16")
    nightly_text = render_job_definition(nightly, "windows").decode("utf-16")

    assert "<LogonTrigger>" in worker_text
    assert "<RestartOnFailure>" in worker_text
    assert "<RunLevel>LeastPrivilege</RunLevel>" in worker_text
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in worker_text
    assert "<CalendarTrigger>" in nightly_text
    assert "2000-01-01T04:00:00" in nightly_text
    assert "<DaysInterval>1</DaysInterval>" in nightly_text
    assert "synthetic-user" in nightly_text
    assert "API_KEY" not in worker_text + nightly_text


def test_native_launchd_backend_captures_installs_and_restores_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout, _plan = _fixture(tmp_path, "macos")
    job = build_background_jobs(layout)[0]
    definition = render_job_definition(job, "macos")
    runner = FakeLaunchctlRunner()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    backend = MacLaunchdSchedulerBackend(
        (tmp_path / "home/Library/LaunchAgents").absolute(),
        user_id=501,
        runner=runner,
    )

    assert backend.capture(job.code) is None
    backend.install(job, definition)
    assert backend.matches(job, definition)
    capture = backend.capture(job.code)
    assert capture is not None and b"must-not-leak" not in capture
    backend.restore(job.code, None)
    assert backend.capture(job.code) is None
    backend.restore(job.code, capture)
    assert backend.matches(job, definition)
    assert all("OPENAI_API_KEY" not in environment for _, environment in runner.calls)


def test_native_windows_backend_uses_fixed_commands_and_semantic_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout, _plan = _fixture(tmp_path, "windows")
    job = build_background_jobs(layout)[1]
    definition = render_job_definition(job, "windows")
    runner = FakePowerShellRunner()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    backend = WindowsTaskSchedulerBackend(runner=runner)

    assert backend.capture(job.code) is None
    backend.install(job, definition)
    assert backend.matches(job, definition)
    assert runner.running == set()
    capture = backend.capture(job.code)
    assert capture is not None and b"must-not-leak" not in capture
    backend.restore(job.code, None)
    assert backend.capture(job.code) is None
    backend.restore(job.code, capture)
    assert backend.matches(job, definition)
    for command, environment in runner.calls:
        assert command[:7] == (
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
        )
        assert "OPENAI_API_KEY" not in environment


def test_native_windows_backend_starts_worker_and_restores_prior_running_state(
    tmp_path: Path,
):
    layout, _plan = _fixture(tmp_path, "windows")
    worker = build_background_jobs(layout)[0]
    definition = render_job_definition(worker, "windows")
    runner = FakePowerShellRunner()
    backend = WindowsTaskSchedulerBackend(runner=runner)

    backend.install(worker, definition)
    assert backend.matches(worker, definition)
    assert runner.running == {"Daemon Harness After-Turn"}
    capture = backend.capture(worker.code)
    assert capture is not None

    backend.restore(worker.code, None)
    assert runner.running == set()
    backend.restore(worker.code, capture)
    assert backend.matches(worker, definition)
    assert runner.running == {"Daemon Harness After-Turn"}


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_transaction_is_idempotent_and_restores_preexisting_jobs(tmp_path: Path, target_os: str):
    layout, plan = _fixture(tmp_path, target_os)
    before = {AFTER_TURN_JOB: b"old worker", NIGHTLY_JOB: b"old nightly"}
    scheduler = FakeScheduler(target_os, before)
    adapter = CompanionBackgroundServicesAdapter(layout, backend=scheduler)

    result = _execute(tmp_path, plan, adapter)
    assert result.status == "committed"
    expected = dict(scheduler.state)
    second = _execute(tmp_path, plan, adapter, transaction="services-second")
    assert second.status == "committed"
    assert scheduler.state == expected

    rollback_snapshot = tmp_path / "manual-snapshot"
    rollback_snapshot.mkdir()
    adapter.snapshot(plan.steps[0], rollback_snapshot)
    adapter.apply(plan.steps[0])
    adapter.rollback(plan.steps[0], rollback_snapshot)
    assert scheduler.state == expected


@pytest.mark.parametrize("failure_job", [AFTER_TURN_JOB, NIGHTLY_JOB])
def test_failure_rolls_both_jobs_back_exactly(tmp_path: Path, failure_job: str):
    layout, plan = _fixture(tmp_path, "macos")
    before = {AFTER_TURN_JOB: b"old worker", NIGHTLY_JOB: b"old nightly"}
    scheduler = FakeScheduler("macos", before)

    def fail(code: str) -> None:
        if code == failure_job:
            raise RuntimeError("synthetic failure")

    adapter = CompanionBackgroundServicesAdapter(
        layout,
        backend=scheduler,
        mutation_hook=fail,
    )
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, plan, adapter)
    assert scheduler.state == before


@pytest.mark.parametrize("interrupted_job", [AFTER_TURN_JOB, NIGHTLY_JOB])
def test_interruption_recovers_with_fresh_adapter(
    tmp_path: Path,
    interrupted_job: str,
):
    layout, plan = _fixture(tmp_path, "windows")
    before = {AFTER_TURN_JOB: b"old worker"}
    scheduler = FakeScheduler("windows", before)

    def interrupt(code: str) -> None:
        if code == interrupted_job:
            raise KeyboardInterrupt

    adapter = CompanionBackgroundServicesAdapter(
        layout,
        backend=scheduler,
        mutation_hook=interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        _execute(tmp_path, plan, adapter)

    recovered = TransactionEngine(tmp_path / "transactions").recover(
        plan,
        CompanionBackgroundServicesAdapter(layout, backend=scheduler),
        transaction_id="services",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert recovered.status == "rolled_back"
    assert scheduler.state == before


def test_concurrent_scheduler_change_is_preserved(tmp_path: Path):
    layout, plan = _fixture(tmp_path, "macos")
    scheduler = FakeScheduler("macos")

    def conflict(code: str) -> None:
        if code == NIGHTLY_JOB:
            scheduler.state[AFTER_TURN_JOB] = b"third-party"
            raise RuntimeError("synthetic failure")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            plan,
            CompanionBackgroundServicesAdapter(
                layout,
                backend=scheduler,
                mutation_hook=conflict,
            ),
        )
    assert scheduler.state[AFTER_TURN_JOB] == b"third-party"


def test_rejects_wrong_step_backend_and_unverified_environment(tmp_path: Path):
    layout, plan = _fixture(tmp_path, "macos")
    with pytest.raises(ValueError):
        CompanionBackgroundServicesAdapter(layout, backend=FakeScheduler("windows"))
    with pytest.raises(ValueError):
        build_background_jobs(replace(layout, target_os="linux"))
    bad_step = plan.steps[0].__class__("other", "other", ())
    adapter = CompanionBackgroundServicesAdapter(layout, backend=FakeScheduler("macos"))
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with pytest.raises(ValueError):
        adapter.snapshot(bad_step, snapshot)
    environment_root = layout.runtime_environment.environments_dir
    manifest = next(environment_root.rglob(".daemon-harness-environment.json"))
    payload = json.loads(manifest.read_text())
    payload["release_id"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError):
        build_background_jobs(layout)


@pytest.mark.parametrize('root_folders', [False, True])
def test_background_entry_drops_inherited_secrets_and_binds_private_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_folders: bool,
):
    layout, _plan = _fixture(tmp_path, "macos")
    if root_folders:
        from daemons_companion.desktop.memory import initialize_empty_vault
        initialize_empty_vault(layout.runtime.memory_root)
        runtime = replace(layout.runtime, root_folder_memory=True)
        layout = replace(layout, runtime=runtime,
                         runtime_environment=replace(layout.runtime_environment, runtime=runtime))
    job = build_background_jobs(layout)[0]
    arguments = list(job.arguments)
    values = {
        arguments[index]: arguments[index + 1]
        for index in range(4, len(arguments), 2)
    }
    linked_python = Path(values["--python"])
    managed_python = linked_python.parents[2] / "python/bin/python"
    managed_python.parent.mkdir(parents=True)
    managed_python.write_text("managed python", encoding="utf-8")
    managed_python.chmod(0o700)
    linked_python.unlink()
    linked_python.symlink_to(Path("../../python/bin/python"))
    args = SimpleNamespace(
        release=values["--release"],
        python=values["--python"],
        memory_root=values["--memory-root"],
        state_root=values["--state-root"],
        provider_config=values["--provider-config"],
        runtime_revision=values["--runtime-revision"],
        user_id=values["--user-id"],
    )
    monkeypatch.setattr(background_entry.sys, "executable", args.python)
    with patch.dict(
        os.environ,
        {"HOME": str(tmp_path), "PATH": "/safe", "OPENAI_API_KEY": "must-drop"},
        clear=True,
    ):
        background_entry._configure(args)
        assert os.environ["HOME"] == str(tmp_path)
        assert os.environ["PATH"] == "/safe"
        assert "OPENAI_API_KEY" not in os.environ
        assert os.environ["DAEMON_CORE_AFTER_TURN_SPOOL"] == "on"
        assert os.environ["DAEMON_CORE_PROVIDER_CONFIG"] == args.provider_config
        assert os.environ["DAEMON_CORE_USER_ID"] == "synthetic-user"

    if root_folders:
        marker = Path(args.memory_root) / 'vault-layout.json'
        original = marker.read_bytes()
        marker.unlink()
        with pytest.raises(ValueError, match='exact layout marker'):
            background_entry._configure(args)
        marker.write_bytes(original)

    linked_python.unlink()
    outside_python = tmp_path / "outside-python"
    outside_python.write_text("outside", encoding="utf-8")
    outside_python.chmod(0o700)
    linked_python.symlink_to(outside_python)
    with pytest.raises(
        ValueError,
        match="Private interpreter does not belong to the active release",
    ):
        background_entry._configure(args)


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_scoped_native_lifecycle_preserves_other_jobs(tmp_path, target_os):
    layout, _ = _fixture(tmp_path, target_os)
    namespace = "a" * 32
    layout = replace(layout, job_namespace=namespace)
    jobs = (*build_background_jobs(layout), replace(
        build_background_jobs(layout)[0],
        code=scoped_job_code(OPENCLAW_API_JOB, namespace),
    ))
    sentinels = {AFTER_TURN_JOB, NIGHTLY_JOB, OPENCLAW_API_JOB,
                 scoped_job_code(AFTER_TURN_JOB, "b" * 32)}
    if target_os == "macos":
        runner = FakeLaunchctlRunner()
        runner.loaded.update(sentinels)
        backend = MacLaunchdSchedulerBackend(
            tmp_path / "agents", user_id=501, runner=runner, job_namespace=namespace,
        )
    else:
        runner = FakePowerShellRunner()
        sentinels = {"Daemon Harness After-Turn", "Daemon Harness Nightly",
                     "Daemon Harness OpenClaw API", scoped_job_code(AFTER_TURN_JOB, "b" * 32)}
        runner.tasks.update({name: b"sentinel" for name in sentinels})
        runner.running.update(sentinels)
        backend = WindowsTaskSchedulerBackend(runner=runner, job_namespace=namespace)
    for job in jobs:
        definition = render_job_definition(job, target_os)
        assert backend.capture(job.code) is None
        backend.install(job, definition)
        assert backend.matches(job, definition)
        capture = backend.capture(job.code)
        backend.restore(job.code, None)
        assert backend.capture(job.code) is None
        backend.restore(job.code, capture)
        assert backend.matches(job, definition)
        backend.restore(job.code, None)
    if target_os == "macos":
        assert runner.loaded == sentinels
        assert list((tmp_path / "agents").iterdir()) == []
    else:
        assert runner.running == sentinels
        assert runner.tasks == {name: b"sentinel" for name in sentinels}


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize("namespace", [None, "a" * 32])
def test_native_scope_rejects_every_foreign_operation_before_command(tmp_path, target_os, namespace):
    layout, _ = _fixture(tmp_path, target_os)
    calls = []
    def runner(command, environment):
        calls.append(command)
        raise AssertionError("A rejected scope must not reach the scheduler")
    if target_os == "macos":
        backend = MacLaunchdSchedulerBackend(
            tmp_path / "agents", user_id=501, runner=runner, job_namespace=namespace,
        )
    else:
        backend = WindowsTaskSchedulerBackend(runner=runner, job_namespace=namespace)
    foreign = [scoped_job_code(AFTER_TURN_JOB, "b" * 32), "../escape", "arbitrary"]
    if namespace is not None:
        foreign.extend([AFTER_TURN_JOB, NIGHTLY_JOB, OPENCLAW_API_JOB])
    for code in foreign:
        job = replace(build_background_jobs(layout)[0], code=code)
        definition = render_job_definition(job, target_os)
        for action in (
            lambda: backend.capture(code), lambda: backend.install(job, definition),
            lambda: backend.matches(job, definition), lambda: backend.restore(code, None),
            lambda: backend.restore(code, b"invalid"),
        ):
            with pytest.raises(ValueError, match="scope"):
                action()
    assert calls == []
    assert not (tmp_path / "agents").exists()


@pytest.mark.parametrize("namespace", ["", "A" * 32, "a" * 31, "a" * 33, "../x", 12, True])
def test_invalid_namespace_rejected(tmp_path, namespace):
    with pytest.raises(ValueError, match="namespace"):
        scoped_job_code(AFTER_TURN_JOB, namespace)
    with pytest.raises(ValueError, match="namespace"):
        WindowsTaskSchedulerBackend(job_namespace=namespace)
    with pytest.raises(ValueError, match="namespace"):
        MacLaunchdSchedulerBackend(tmp_path / "agents", user_id=501, job_namespace=namespace)


def test_launchd_foreign_label_cannot_escape_scope_during_restore_or_capture(tmp_path):
    import base64
    namespace = "a" * 32
    code = scoped_job_code(AFTER_TURN_JOB, namespace)
    runner = FakeLaunchctlRunner()
    backend = MacLaunchdSchedulerBackend(
        tmp_path / "agents", user_id=501, runner=runner, job_namespace=namespace,
    )
    definition = plistlib.dumps({"Label": AFTER_TURN_JOB})
    capture = json.dumps({"schema": "daemon.launchd-capture.v1", "loaded": True,
                          "definition": base64.b64encode(definition).decode()}).encode()
    with pytest.raises(RuntimeError, match="Label"):
        backend.restore(code, capture)
    assert runner.calls == []
    backend.launch_agents_dir.mkdir()
    path = backend.launch_agents_dir / f"{code}.plist"
    path.write_bytes(definition)
    with pytest.raises(RuntimeError, match="Label"):
        backend.capture(code)
    with pytest.raises(RuntimeError, match="Label"):
        backend.restore(code, None)
    assert runner.calls == []
    assert path.read_bytes() == definition


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_scoped_transaction_recovers_without_touching_production(tmp_path, target_os, interrupted):
    layout, plan = _fixture(tmp_path, target_os)
    layout = replace(layout, job_namespace="a" * 32)
    scoped_plan = replace(plan, steps=(background_services_step(layout),))
    assert public_plan_digest(scoped_plan) != public_plan_digest(plan)
    before = {AFTER_TURN_JOB: b"personal worker", NIGHTLY_JOB: b"personal nightly"}
    scheduler = FakeScheduler(target_os, before)
    def fail(code):
        if code == scoped_job_code(NIGHTLY_JOB, layout.job_namespace):
            if interrupted:
                raise KeyboardInterrupt
            raise RuntimeError("injected failure")
    adapter = CompanionBackgroundServicesAdapter(layout, backend=scheduler, mutation_hook=fail)
    with pytest.raises(KeyboardInterrupt if interrupted else TransactionApplyError):
        _execute(tmp_path, scoped_plan, adapter)
    if interrupted:
        fresh = CompanionBackgroundServicesAdapter(layout, backend=scheduler)
        TransactionEngine(tmp_path / "transactions").recover(
            scoped_plan, fresh, transaction_id="services",
            confirmed_plan_digest=public_plan_digest(scoped_plan),
        )
    assert scheduler.state == before


def test_scope_change_invalidates_confirmation_before_snapshot(tmp_path):
    layout, plan = _fixture(tmp_path, "macos")
    first = replace(layout, job_namespace="a" * 32)
    second = replace(layout, job_namespace="b" * 32)
    first_plan = replace(plan, steps=(background_services_step(first),))
    second_plan = replace(plan, steps=(background_services_step(second),))
    scheduler = FakeScheduler("macos", {AFTER_TURN_JOB: b"personal"})
    adapter = CompanionBackgroundServicesAdapter(second, backend=scheduler)
    with pytest.raises(PlanConfirmationError):
        TransactionEngine(tmp_path / "transactions").execute(
            second_plan, adapter, transaction_id="changed-scope",
            confirmed_plan_digest=public_plan_digest(first_plan),
        )
    assert scheduler.state == {AFTER_TURN_JOB: b"personal"}
    assert scheduler.installs == scheduler.restores == []
    assert not (tmp_path / "transactions").exists()


def test_private_launchd_stop_waits_for_loaded_job_to_disappear(tmp_path, monkeypatch):
    import daemons_companion.runtime.services as module
    polls = []
    def runner(command, env):
        if command[1] == "print":
            polls.append(command[-1])
            return SchedulerCommandResult(0 if len(polls) < 3 else 113)
        return SchedulerCommandResult(0)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    namespace = "a" * 32
    scheduler = MacLaunchdSchedulerBackend(tmp_path / "agents", user_id=501, runner=runner, job_namespace=namespace)
    scheduler.restore(scoped_job_code(AFTER_TURN_JOB, namespace), None)
    assert len(polls) == 3


def test_windows_scheduler_uses_only_system_modules_in_filtered_environment(monkeypatch):
    import ntpath
    from types import SimpleNamespace
    import daemons_companion.runtime.services as module
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt", path=ntpath, environ={
        "SYSTEMROOT": r"C:\Windows", "PSMODULEPATH": r"C:\user-modules",
        "OPENAI_API_KEY": "synthetic-secret", "PATH": "synthetic-path"}))
    environment = module._scheduler_environment()
    assert environment == {"SYSTEMROOT": r"C:\Windows", "PATH": "synthetic-path",
        "PSModulePath": r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"}


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_background_flags_prevent_bytecode_before_bootstrap(tmp_path, target_os):
    import subprocess
    import sys
    import shutil
    layout, _ = _fixture(tmp_path, target_os)
    job = build_background_jobs(layout)[0]
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text("value = 42\n", encoding="utf-8")
    environment = dict(os.environ)
    for key in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = str(startup)
    bootstrap = "import sys;sys.dont_write_bytecode=True;import sitecustomize;assert sitecustomize.value==42"
    # Setting the flag inside -c is too late for site/venv startup imports.
    subprocess.run([sys.executable, "-c", bootstrap], env=environment, check=True)
    assert list(startup.rglob("*.pyc"))
    shutil.rmtree(startup / "__pycache__")
    flags = list(job.arguments[:job.arguments.index("-c") + 1])
    subprocess.run([sys.executable, *flags, bootstrap], env=environment, check=True)
    assert not list(startup.rglob("*.pyc"))


def test_windows_task_signature_matches_real_native_export():
    from daemons_companion.runtime.services import _windows_definition_signature
    fixture = Path(__file__).resolve().parents[1] / "docs/evaluations/fixtures/windows_recovery_2026_10_06/scheduler-normalization.json"
    evidence = json.loads(fixture.read_text(encoding="utf-8"))
    from xml.etree import ElementTree
    for captured in evidence["captures"]:
        expected = ElementTree.fromstring(evidence["expected_xml"])
        actual = ElementTree.fromstring(captured["xml"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        # Historical template was unbound; Windows added the caller's SID.
        # Bind that expected principal before checking unrelated normalization.
        principal = expected.find("t:Principals/t:Principal", ns)
        user = actual.find("t:Principals/t:Principal/t:UserId", ns)
        ElementTree.SubElement(principal, user.tag).text = user.text
        assert captured["running"] is True
        assert _windows_definition_signature(ElementTree.tostring(actual)) == _windows_definition_signature(ElementTree.tostring(expected))


@pytest.mark.parametrize("changed", ["Command", "Arguments", "WorkingDirectory", "RunLevel", "LogonType"])
def test_windows_normalization_still_refuses_changed_job_contract(changed):
    from xml.etree import ElementTree
    from daemons_companion.runtime.services import _windows_definition_signature
    fixture = Path(__file__).resolve().parents[1] / "docs/evaluations/fixtures/windows_recovery_2026_10_06/scheduler-normalization.json"
    evidence = json.loads(fixture.read_text(encoding="utf-8"))
    root = ElementTree.fromstring(evidence["expected_xml"])
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == changed:
            node.text = "HighestAvailable" if changed == "RunLevel" else "concurrent-change"
    assert _windows_definition_signature(ElementTree.tostring(root)) != _windows_definition_signature(evidence["captures"][0]["xml"].encode("utf-16"))


@pytest.mark.parametrize("persistent", [True, False])
def test_windows_job_binds_execution_and_logon_to_process_owner(monkeypatch, persistent):
    from xml.etree import ElementTree
    import daemons_companion.runtime.services as module
    sid = "S-1-5-21-100-200-300-1001"
    monkeypatch.setattr(module, "_windows_scheduler_user_sid", lambda: sid)
    job = BackgroundJob(AFTER_TURN_JOB if persistent else NIGHTLY_JOB,
        r"C:\private\python.exe", ("-B", "-c", "pass"), r"C:\private", "job.log", persistent, None if persistent else 4)
    root = ElementTree.fromstring(render_job_definition(job, "windows"))
    ns = {"t": module._TASK_NAMESPACE}
    assert root.findtext("t:Principals/t:Principal/t:UserId", namespaces=ns) == sid
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    trigger = root.find("t:Triggers/t:LogonTrigger/t:UserId", ns)
    assert (trigger.text if trigger is not None else None) == (sid if persistent else None)


@pytest.mark.parametrize("parent_name", ["Principal", "LogonTrigger"])
@pytest.mark.parametrize("mutation", ["different-user", "missing-user", "group"])
def test_windows_job_identity_change_is_not_hidden_by_normalization(monkeypatch, parent_name, mutation):
    from xml.etree import ElementTree
    import daemons_companion.runtime.services as module
    monkeypatch.setattr(module, "_windows_scheduler_user_sid", lambda: "S-1-5-21-100-200-300-1001")
    job = BackgroundJob(AFTER_TURN_JOB, r"C:\private\python.exe", ("-B",), r"C:\private", "job.log", True)
    definition = render_job_definition(job, "windows")
    root = ElementTree.fromstring(definition)
    parent = next(node for node in root.iter() if node.tag.endswith("}" + parent_name))
    user = next(node for node in parent if node.tag.endswith("}UserId"))
    if mutation == "missing-user":
        parent.remove(user)
    elif mutation == "group":
        user.tag = user.tag.replace("UserId", "GroupId")
    else:
        user.text = "S-1-5-21-100-200-300-9999"
    assert module._windows_definition_signature(definition) != module._windows_definition_signature(ElementTree.tostring(root))


def test_windows_native_account_name_and_sid_compare_as_same_identity(monkeypatch):
    import daemons_companion.runtime.services as module
    fixture = Path(__file__).resolve().parents[1] / "docs/evaluations/fixtures/windows_user_tasks_2026_10_06/account-name-export.json"
    evidence = json.loads(fixture.read_text(encoding="utf-8"))
    from xml.etree import ElementTree
    ns = {"t": module._TASK_NAMESPACE}
    record = evidence["jobs"][0]
    expected = ElementTree.fromstring(record["expected_xml"])
    sid = expected.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns)
    for observation in record["observations"]:
        root = ElementTree.fromstring(observation["xml"])
        account = root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns)
        assert account != sid
        monkeypatch.setattr(module, "_windows_task_identity", lambda value: sid if value == account else value)
        assert module._windows_definition_signature(ElementTree.tostring(expected)) == module._windows_definition_signature(ElementTree.tostring(root))
        root.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text = "OTHER\\user"
        assert module._windows_definition_signature(ElementTree.tostring(expected)) != module._windows_definition_signature(ElementTree.tostring(root))


@pytest.mark.parametrize("modified", [False, True])
def test_windows_rollback_removes_stopped_owned_job_but_refuses_changed_definition(tmp_path, modified):
    layout, plan = _fixture(tmp_path, "windows")
    runner = FakePowerShellRunner()
    backend = WindowsTaskSchedulerBackend(runner=runner, start_timeout=0)
    adapter = CompanionBackgroundServicesAdapter(layout, backend=backend)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    step = plan.steps[0]
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    runner.running.clear()
    with pytest.raises(RuntimeError, match="not active"):
        adapter.verify(step)
    if modified:
        name = next(iter(runner.tasks))
        runner.tasks[name] = runner.tasks[name].decode("utf-16").replace("-B", "-Xchanged").encode("utf-16")
        with pytest.raises(RuntimeError, match="concurrent"):
            adapter.rollback(step, snapshot)
        assert runner.tasks
    else:
        adapter.rollback(step, snapshot)
        assert not runner.tasks


@pytest.mark.parametrize("outcome", ["delayed", "stopped", "changed"])
def test_windows_start_verification_waits_but_preserves_failure_evidence(tmp_path, outcome):
    layout, plan = _fixture(tmp_path, "windows")
    job = build_background_jobs(layout)[0]
    definition = render_job_definition(job, "windows")
    runner = FakePowerShellRunner()
    now = [0.0]
    def call(command, env):
        script = command[7]
        if "$task.LastTaskResult" in script:
            return SchedulerCommandResult(0, '{"state":3,"result":1}')
        if "$xml=$task.Xml" in script and outcome == "delayed" and now[0] >= 1:
            runner.running.add(command[9])
        return runner(command, env)
    backend = WindowsTaskSchedulerBackend(runner=call, start_timeout=2,
        clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0]+delay))
    backend.install(job, definition)
    runner.running.clear()
    if outcome == "changed":
        name = next(iter(runner.tasks))
        runner.tasks[name] = definition.decode("utf-16").replace("-B", "-Xchanged").encode("utf-16")
    if outcome == "delayed":
        backend.verify_active(job, definition)
        assert now[0] == 1
    else:
        with pytest.raises(RuntimeError, match="not active") as failed:
            backend.verify_active(job, definition)
        assert failed.value.exit_code == 1
        assert "task_state=3" in str(failed.value) and "0x00000001" in str(failed.value)
        assert ("definition_mismatch" if outcome == "changed" else "not_running") in str(failed.value)
        assert now[0] == (0 if outcome == "changed" else 2)
    assert runner.tasks  # Verification never deletes an installation.


def _console_definition(job) -> bytes:
    """The definition registered before tasks became windowless."""
    from xml.etree import ElementTree
    import subprocess as sp

    root = ElementTree.fromstring(render_job_definition(job, "windows"))
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name == "Command":
            element.text = job.executable
        if name == "Arguments":
            element.text = sp.list2cmdline(list(job.arguments))
    return ElementTree.tostring(root, encoding="utf-16", xml_declaration=True)


def test_windows_services_start_without_a_console_window(tmp_path: Path):
    from daemons_companion.runtime import services as module

    layout, _plan = _fixture(tmp_path, "windows")
    for job in build_background_jobs(layout):
        from xml.etree import ElementTree

        root = ElementTree.fromstring(render_job_definition(job, "windows"))
        values = {e.tag.rsplit("}", 1)[-1]: e.text for e in root.iter()}
        assert job.executable.endswith("python.exe")
        assert values["Command"] == job.executable[: -len("python.exe")] + "pythonw.exe"
        assert module._WINDOWLESS_PRELUDE in values["Arguments"]


def test_windowless_task_matches_the_console_task_it_replaces(tmp_path: Path):
    from daemons_companion.runtime.services import _windows_definition_signature

    layout, _plan = _fixture(tmp_path, "windows")
    for job in build_background_jobs(layout):
        new = render_job_definition(job, "windows")
        assert _windows_definition_signature(new) == _windows_definition_signature(_console_definition(job))
        changed = replace(job, arguments=job.arguments + ("--extra",))
        assert _windows_definition_signature(new) != _windows_definition_signature(_console_definition(changed))


def test_non_python_windows_tasks_keep_their_command():
    from daemons_companion.runtime import services as module

    job = module.BackgroundJob("c", "C:\\tools\\node.exe", ("gateway",), "C:\\w", "C:\\l.log", True)
    assert module._windowless_action(job) == (job.executable, job.arguments)


def test_windows_services_accept_their_windowless_interpreter(monkeypatch, tmp_path):
    from daemons_companion.runtime import bootstrap

    scripts = tmp_path / "venv/Scripts"
    declared = scripts / "python.exe"
    assert bootstrap.same_interpreter(declared, declared)
    assert not bootstrap.same_interpreter(scripts / "pythonw.exe", declared)
    monkeypatch.setattr(bootstrap.os, "name", "nt")
    monkeypatch.setattr(bootstrap, "_source_io_path", lambda path: path)
    assert bootstrap.same_interpreter(scripts / "pythonw.exe", declared)
    assert not bootstrap.same_interpreter(tmp_path / "other/pythonw.exe", declared)
    assert not bootstrap.same_interpreter(scripts / "python3.exe", declared)
