from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from daemons_companion.runtime import service_entry as background_entry
from daemons_companion.runtime.services import (
    OPENCLAW_API_JOB,
    BackgroundJob,
    render_job_definition,
)
from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.openclaw.api_service import (
    CompanionOpenClawApiServiceAdapter,
    OpenClawApiServiceLayout,
    build_openclaw_api_job,
    openclaw_api_service_step,
)
from daemons_companion.openclaw.profile import (
    create_disposable_openclaw_marker,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)
from daemons_companion.runtime.environment import (
    CommandResult,
    CompanionRuntimeEnvironmentAdapter,
    CompanionRuntimeEnvironmentLayout,
    release_environment_interpreter,
    resolve_release_environment,
)
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


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
    def __init__(self, target_os: str, initial: bytes | None = None) -> None:
        self.target_os = target_os
        self.state = {OPENCLAW_API_JOB: initial} if initial is not None else {}
        self.installs = 0
        self.restores = 0

    def capture(self, job_code: str) -> bytes | None:
        return self.state.get(job_code)

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        self.state[job.code] = definition
        self.installs += 1

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        return self.state.get(job.code) == definition

    def restore(self, job_code: str, capture: bytes | None) -> None:
        if capture is None:
            self.state.pop(job_code, None)
        else:
            self.state[job_code] = capture
        self.restores += 1


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "example==1 \\\n+    --hash=sha256:" + "0" * 64 + "\n",
        "requirements.txt": "example==1\n",
        "src/daemon_core/__init__.py": "# synthetic\n",
        "src/daemons_companion/runtime/service_entry.py": "# entry\n",
        "src/daemon_core/nightly_maintenance.py": "# nightly\n",
        "src/daemons_companion/runtime/services.py": "# services\n",
        "integrations/hermes_plugin/__init__.py": "# memory\n",
        "integrations/hermes_voice_plugin/__init__.py": "# voice\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\n",
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
    runtime = CompanionRuntimeLayout(home, _bundle(tmp_path / "bundle"))
    runtime_snapshot = (tmp_path / "runtime-snapshot").absolute()
    runtime_snapshot.mkdir()
    runtime_adapter = CompanionRuntimeAdapter(runtime)
    runtime_step = PlanStep("runtime-layout", "runtime", ())
    runtime_adapter.snapshot(runtime_step, runtime_snapshot)
    runtime_adapter.apply(runtime_step)
    runtime_adapter.verify(runtime_step)

    uv = (tmp_path / ("uv.exe" if target_os == "windows" else "uv")).absolute()
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o700)
    environment = CompanionRuntimeEnvironmentLayout(
        runtime,
        uv,
        hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os,
    )
    environment_snapshot = (tmp_path / "environment-snapshot").absolute()
    environment_snapshot.mkdir()
    environment_adapter = CompanionRuntimeEnvironmentAdapter(
        environment,
        runner=FakeEnvironmentRunner(target_os),
    )
    environment_step = PlanStep("runtime-environment", "environment", ())
    environment_adapter.snapshot(environment_step, environment_snapshot)
    environment_adapter.apply(environment_step)
    environment_adapter.verify(environment_step)

    provider = runtime.state_root / "config" / "provider.json"
    provider.parent.mkdir(parents=True)
    provider.write_text("{}\n", encoding="utf-8")
    if os.name != "nt":
        provider.chmod(0o600)
    openclaw = (tmp_path / "openclaw").absolute()
    create_disposable_openclaw_marker(openclaw)
    (openclaw / "openclaw.json").write_text("{}\n", encoding="utf-8")
    layout = OpenClawApiServiceLayout(
        runtime,
        environment,
        openclaw,
        target_os,
        "synthetic-user",
    )
    plan = SetupPlan(
        HostProbe(HostKind.OPENCLAW, True, True, config_path=str(openclaw)),
        "Aster",
        "masculine",
        {"kind": "synthetic"},
        (openclaw_api_service_step(layout),),
    )
    return layout, plan


def _execute(tmp_path: Path, plan: SetupPlan, adapter, transaction: str = "api"):
    return TransactionEngine(tmp_path / "transactions").execute(
        plan,
        adapter,
        transaction_id=transaction,
        confirmed_plan_digest=public_plan_digest(plan),
    )


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_api_job_uses_only_verified_private_paths(tmp_path: Path, target_os: str) -> None:
    layout, _plan = _fixture(tmp_path, target_os)
    job = build_openclaw_api_job(layout)
    joined = " ".join((job.executable, *job.arguments))

    assert job.code == OPENCLAW_API_JOB
    assert job.persistent is True and job.hour is None
    assert job.arguments[:3] == ("-I", "-B", "-c")
    assert "daemons_companion.runtime.service_entry" in job.arguments[3]
    assert str(layout.runtime.companion_home) in joined
    assert str(layout.openclaw_state) in joined
    assert "openclaw-api" in job.arguments
    assert job.arguments[-2:] == ("--port", "8000")
    assert "API_KEY" not in joined
    assert "secret" not in joined.lower()
    assert str(layout.runtime.bundle_root) not in joined


def test_api_job_accepts_an_explicit_non_privileged_port(tmp_path: Path) -> None:
    layout, _plan = _fixture(tmp_path, "macos")
    alternate = OpenClawApiServiceLayout(**{**layout.__dict__, "port": 18000})

    job = build_openclaw_api_job(alternate)

    assert job.arguments[-2:] == ("--port", "18000")
    assert "Endpoint: 127.0.0.1:18000" in openclaw_api_service_step(
        alternate
    ).changes


def test_launchd_and_windows_definitions_are_persistent_and_unprivileged(
    tmp_path: Path,
) -> None:
    mac_layout, _plan = _fixture(tmp_path / "mac", "macos")
    win_layout, _plan = _fixture(tmp_path / "win", "windows")
    mac_job = build_openclaw_api_job(mac_layout)
    win_job = build_openclaw_api_job(win_layout)
    launchd = plistlib.loads(render_job_definition(mac_job, "macos"))
    windows = render_job_definition(win_job, "windows").decode("utf-16")

    assert launchd["KeepAlive"] is True
    assert launchd["RunAtLoad"] is True
    assert launchd["ProgramArguments"][0] == mac_job.executable
    assert "EnvironmentVariables" not in launchd
    assert "<LogonTrigger>" in windows
    assert "<RunLevel>LeastPrivilege</RunLevel>" in windows
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in windows
    assert "8000" in windows


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_transaction_installs_verifies_and_is_idempotent(
    tmp_path: Path,
    target_os: str,
) -> None:
    layout, plan = _fixture(tmp_path, target_os)
    scheduler = FakeScheduler(target_os)
    result = _execute(
        tmp_path,
        plan,
        CompanionOpenClawApiServiceAdapter(layout, backend=scheduler),
        "first",
    )
    expected = scheduler.state[OPENCLAW_API_JOB]
    second = _execute(
        tmp_path,
        plan,
        CompanionOpenClawApiServiceAdapter(layout, backend=scheduler),
        "second",
    )

    assert result.status == second.status == "committed"
    assert scheduler.state[OPENCLAW_API_JOB] == expected
    assert scheduler.installs == 2


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize("initial", [None, b"pre-existing-definition"])
def test_failure_restores_prior_scheduler_state(
    tmp_path: Path,
    target_os: str,
    initial: bytes | None,
) -> None:
    layout, plan = _fixture(tmp_path, target_os)
    scheduler = FakeScheduler(target_os, initial)
    adapter = CompanionOpenClawApiServiceAdapter(
        layout,
        backend=scheduler,
        mutation_hook=lambda _artifact: (_ for _ in ()).throw(
            RuntimeError("synthetic secret-value")
        ),
    )
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, plan, adapter, "failure")
    assert scheduler.capture(OPENCLAW_API_JOB) == initial


def test_interruption_is_rolled_back_on_recovery(tmp_path: Path) -> None:
    layout, plan = _fixture(tmp_path, "macos")
    scheduler = FakeScheduler("macos")
    adapter = CompanionOpenClawApiServiceAdapter(
        layout,
        backend=scheduler,
        mutation_hook=lambda _artifact: (_ for _ in ()).throw(
            KeyboardInterrupt("synthetic interruption")
        ),
    )
    engine = TransactionEngine(tmp_path / "transactions")
    digest = public_plan_digest(plan)
    with pytest.raises(KeyboardInterrupt):
        engine.execute(
            plan,
            adapter,
            transaction_id="interrupted",
            confirmed_plan_digest=digest,
        )
    with pytest.raises(TransactionRecoveryError):
        engine.execute(
            plan,
            CompanionOpenClawApiServiceAdapter(layout, backend=scheduler),
            transaction_id="interrupted",
            confirmed_plan_digest=digest,
        )
    assert scheduler.capture(OPENCLAW_API_JOB) is None


def test_concurrent_scheduler_change_is_preserved(tmp_path: Path) -> None:
    layout, plan = _fixture(tmp_path, "macos")
    scheduler = FakeScheduler("macos")

    def conflict(_artifact: str) -> None:
        scheduler.state[OPENCLAW_API_JOB] = b"third-party-definition"
        raise RuntimeError("synthetic failure")

    adapter = CompanionOpenClawApiServiceAdapter(
        layout,
        backend=scheduler,
        mutation_hook=conflict,
    )
    with pytest.raises(TransactionRecoveryError):
        _execute(tmp_path, plan, adapter, "conflict")
    assert scheduler.capture(OPENCLAW_API_JOB) == b"third-party-definition"


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda layout: OpenClawApiServiceLayout(**{**layout.__dict__, "port": 80}), "non-privileged"),
        (lambda layout: OpenClawApiServiceLayout(**{**layout.__dict__, "target_os": "linux"}), "macOS and Windows"),
        (lambda layout: OpenClawApiServiceLayout(**{**layout.__dict__, "user_id": "bad\nuser"}), "user id"),
    ],
)
def test_invalid_layouts_fail_before_scheduler_mutation(
    tmp_path: Path,
    change,
    match: str,
) -> None:
    layout, _plan = _fixture(tmp_path, "macos")
    invalid = change(layout)
    scheduler = FakeScheduler(invalid.target_os)
    with pytest.raises(ValueError, match=match):
        CompanionOpenClawApiServiceAdapter(invalid, backend=scheduler).snapshot(
            PlanStep("openclaw-api-service", "invalid", ()),
            (tmp_path / "missing-snapshot").absolute(),
        )
    assert scheduler.state == {}


def test_runtime_entry_sanitizes_environment_and_sets_canonical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _plan = _fixture(tmp_path, "macos")
    environment = resolve_release_environment(layout.runtime_environment)
    python = release_environment_interpreter(environment, "macos")
    release = Path(build_openclaw_api_job(layout).working_directory)
    monkeypatch.setattr(background_entry.sys, "executable", str(python))
    args = SimpleNamespace(
        service="openclaw-api",
        release=str(release),
        python=str(python),
        memory_root=str(layout.runtime.memory_root),
        state_root=str(layout.runtime.state_root),
        provider_config=str(layout.runtime.state_root / "config/provider.json"),
        runtime_revision=release.name,
        user_id="synthetic-user",
        openclaw_state=str(layout.openclaw_state),
        port=18000,
    )
    with patch.dict(
        os.environ,
        {"HOME": str(tmp_path / "home"), "OPENAI_API_KEY": "must-not-survive"},
        clear=True,
    ):
        configured_release, state = background_entry._configure(args)
        host, port = background_entry._configure_openclaw_api(
            args,
            configured_release,
            state,
        )
        effective = dict(os.environ)

    assert (host, port) == ("127.0.0.1", 18000)
    assert "OPENAI_API_KEY" not in effective
    assert effective["DAEMON_CORE_PROVIDER_CONFIG"].endswith("provider.json")
    assert effective["DAEMON_CORE_AFTER_TURN_OWNER"] == "external"
    assert effective["DAEMON_CORE_AFTER_TURN_SPOOL"] == "on"
    assert effective["DAEMON_CORE_CONTINUITY_RECEIPTS"] == "on"
    assert effective["DAEMON_CORE_SCENE_COALESCING"] == "on"
    assert effective["DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY"] == "on"
    assert effective["DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS"] == "openclaw"
    assert effective["DAEMON_CORE_OPENCLAW_AGENT_DB"].startswith(
        str(layout.openclaw_state)
    )
    assert effective["DAEMON_CORE_OPENCLAW_HEALTH_FILE"].startswith(
        str(layout.runtime.state_root)
    )
    assert effective["DAEMON_CORE_LATENCY_LOG"].startswith(
        str(layout.runtime.state_root)
    )
