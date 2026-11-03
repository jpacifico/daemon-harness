from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from daemons_companion.runtime.services import (
    AFTER_TURN_JOB,
    NIGHTLY_JOB,
    BackgroundJob,
    scoped_job_code,
)
from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupPlan,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.hermes.activation import HermesControlState
from daemons_companion.hermes.composition import HermesCompositionLayout
from daemons_companion.hermes.profile import create_disposable_profile_marker
from daemons_companion.hermes.ready_composition import (
    HERMES_READY_STEP_CODES,
    HermesReadyCompositionAdapter,
    build_dormant_hermes_ready_plan,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)
from daemons_companion.runtime.environment import CommandResult
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


class FakeCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def put(self, reference: str, protected_value: str) -> None:
        self.values[reference] = protected_value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


class FakeRunner:
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
            cache = Path(environment["UV_CACHE_DIR"])
            cache.mkdir(parents=True, exist_ok=True)
            return CommandResult(0)
        if command[0].endswith(("/python", "python.exe")):
            return CommandResult(0, "daemon-runtime-ok\n")
        return CommandResult(1, stderr="unexpected command")


class FakeScheduler:
    def __init__(self, target_os: str, initial: dict[str, bytes] | None = None) -> None:
        self.target_os = target_os
        self.state = dict(initial or {})

    def capture(self, job_code: str) -> bytes | None:
        return self.state.get(job_code)

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        self.state[job.code] = definition

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        return self.state.get(job.code) == definition

    def restore(self, job_code: str, capture: bytes | None) -> None:
        if capture is None:
            self.state.pop(job_code, None)
        else:
            self.state[job_code] = capture


class FakeControl:
    def __init__(self, hermes_home: Path) -> None:
        self.hermes_home = hermes_home
        self.plugins = {"daemon_core": False, "daemon_voice": False}
        self.gateway = "stopped"
        self.ready = True
        self.start_observations: list[str] = []

    def state(self) -> HermesControlState:
        return HermesControlState(tuple(self.plugins.items()), self.gateway)

    def enable_plugin(self, name: str) -> None:
        self.plugins[name] = True

    def disable_plugin(self, name: str) -> None:
        self.plugins[name] = False

    def start_gateway(self) -> None:
        config = self.hermes_home / "config.yaml"
        self.start_observations.append(
            config.read_text(encoding="utf-8") if config.is_file() else "absent"
        )
        self.gateway = "running"

    def restart_gateway(self) -> None:
        self.gateway = "running"

    def stop_gateway(self) -> None:
        self.gateway = "stopped"

    def gateway_ready(self) -> bool:
        return self.ready and self.gateway == "running"


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "example==1 \\\n    --hash=sha256:" + "0" * 64 + "\n",
        "requirements.txt": "example==1\n",
        "src/daemon_core/__init__.py": "# synthetic\n",
        "src/daemon_core/loop.py": "# synthetic loop\n",
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


def _fixture(tmp_path: Path, target_os: str = "macos", job_namespace=None):
    companion = (tmp_path / "companion").absolute()
    profile = (tmp_path / "profile").absolute()
    create_disposable_companion_marker(companion)
    create_disposable_profile_marker(profile)
    (profile / "config.yaml").write_text("existing: true\n", encoding="utf-8")
    (profile / ".env").write_text("KEEP_ME=yes\n", encoding="utf-8")
    memory = companion / "data/memory"
    memory.mkdir(parents=True)
    (memory / "persona.md").write_text("canonical voice\n", encoding="utf-8")
    uv = (tmp_path / ("uv.exe" if target_os == "windows" else "uv")).absolute()
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o700)
    runtime = CompanionRuntimeLayout(companion, _bundle(tmp_path / "bundle"))
    layout = HermesCompositionLayout(
        runtime,
        profile,
        uv,
        hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os,
        job_namespace=job_namespace,
    )
    selection = ProviderSelection(
        ProviderKind.OPENAI_API,
        "synthetic-model",
        credential_available=True,
    )
    reviewed = build_setup_plan(
        SetupRequest(HostKind.HERMES, "Lyra", "feminine", selection),
        (
            HostProbe(
                HostKind.HERMES,
                True,
                True,
                executable="/synthetic/hermes",
                config_path=str(profile),
                version="synthetic",
                version_status="verified",
            ),
        ),
    )
    plan = build_dormant_hermes_ready_plan(reviewed, layout)
    return layout, plan, selection, FakeCredentialStore(), FakeRunner(target_os)


def _adapter(
    layout,
    plan,
    selection,
    store,
    runner,
    scheduler,
    control,
    *,
    credential_value="synthetic-secret",
    recovery_only=False,
    mutation_hook=None,
):
    return HermesReadyCompositionAdapter(
        layout,
        plan=plan,
        provider_selection=selection,
        credential_store=store,
        credential_value=credential_value,
        user_id="synthetic-user",
        scheduler=scheduler,
        control=control,
        readiness_probe=lambda *_arguments: True,
        runner=runner,
        recovery_only=recovery_only,
        mutation_hook=mutation_hook,
    )


def _execute(tmp_path: Path, plan: SetupPlan, adapter, transaction="ready"):
    return TransactionEngine(tmp_path / "transactions").execute(
        plan,
        adapter,
        transaction_id=transaction,
        confirmed_plan_digest=public_plan_digest(plan),
    )


def _tree_state(root: Path) -> dict[str, tuple[object, ...]]:
    state: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[relative] = ("directory", stat.S_IMODE(path.stat().st_mode))
        else:
            state[relative] = (
                "file",
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
    return state


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize("job_namespace", [None, "a" * 32])
def test_ready_composition_is_coherent_on_both_platforms(
    tmp_path: Path,
    target_os: str,
    job_namespace,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path, target_os, job_namespace)
    scheduler = FakeScheduler(target_os)
    control = FakeControl(layout.hermes_home)

    result = _execute(
        tmp_path,
        plan,
        _adapter(layout, plan, selection, store, runner, scheduler, control),
    )

    assert result.status == "committed"
    assert tuple(step.code for step in plan.steps) == HERMES_READY_STEP_CODES
    assert set(scheduler.state) == {
        scoped_job_code(code, job_namespace) for code in (AFTER_TURN_JOB, NIGHTLY_JOB,)
    }
    assert control.state() == HermesControlState(
        (("daemon_core", True), ("daemon_voice", True)),
        "running",
    )
    env = (layout.hermes_home / ".env").read_text(encoding="utf-8")
    assert "DAEMON_CORE_AFTER_TURN_OWNER=external" in env
    assert "DAEMON_CORE_AFTER_TURN_SPOOL=on" in env
    assert "PYTHONDONTWRITEBYTECODE=1" in env
    assert (
        "DAEMON_CORE_LATENCY_LOG="
        f"{layout.runtime.state_root / 'latency/hermes.jsonl'}"
    ) in env
    assert (
        "DAEMON_CORE_AFTER_TURN_SPOOL_DIR="
        f"{layout.runtime.state_root / 'after-turn-spool'}"
    ) in env
    assert (layout.runtime.memory_root / "persona.md").read_text() == "canonical voice\n"
    assert "synthetic-secret" not in json.dumps(plan.to_public_dict())


def test_second_ready_composition_preserves_installed_state(tmp_path: Path) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler = FakeScheduler("macos")
    control = FakeControl(layout.hermes_home)
    _execute(
        tmp_path,
        plan,
        _adapter(layout, plan, selection, store, runner, scheduler, control),
        "first",
    )
    companion_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.hermes_home)
    jobs_before = dict(scheduler.state)
    credentials_before = dict(store.values)

    result = _execute(
        tmp_path,
        plan,
        _adapter(
            layout,
            plan,
            selection,
            store,
            FakeRunner("macos"),
            scheduler,
            control,
        ),
        "second",
    )

    assert result.status == "committed"
    assert _tree_state(layout.runtime.companion_home) == companion_before
    assert _tree_state(layout.hermes_home) == profile_before
    assert scheduler.state == jobs_before
    assert store.values == credentials_before


@pytest.mark.parametrize(
    "boundary",
    [AFTER_TURN_JOB, NIGHTLY_JOB, "daemon_core", "daemon_voice", "gateway"],
)
def test_failure_at_each_new_boundary_restores_every_owner(
    tmp_path: Path,
    boundary: str,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler_before = {AFTER_TURN_JOB: b"old worker", NIGHTLY_JOB: b"old nightly"}
    scheduler = FakeScheduler("macos", scheduler_before)
    control = FakeControl(layout.hermes_home)
    control.gateway = "running"
    control_before = control.state()
    companion_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.hermes_home)

    def fail(artifact: str) -> None:
        if artifact == boundary:
            raise RuntimeError("synthetic boundary failure containing synthetic-secret")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            plan,
            _adapter(
                layout,
                plan,
                selection,
                store,
                runner,
                scheduler,
                control,
                mutation_hook=fail,
            ),
        )

    assert _tree_state(layout.runtime.companion_home) == companion_before
    assert _tree_state(layout.hermes_home) == profile_before
    assert scheduler.state == scheduler_before
    assert control.state() == control_before
    assert store.values == {}
    journal = (tmp_path / "transactions/ready/journal.json").read_text()
    assert "synthetic-secret" not in journal
    if boundary in {"daemon_core", "daemon_voice", "gateway"}:
        assert control.start_observations[-1] == "existing: true\n"


@pytest.mark.parametrize(
    "boundary",
    [AFTER_TURN_JOB, NIGHTLY_JOB, "daemon_core", "daemon_voice", "gateway"],
)
def test_interruption_at_each_new_boundary_recovers_cleanly(
    tmp_path: Path,
    boundary: str,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler_before = {AFTER_TURN_JOB: b"old worker"}
    scheduler = FakeScheduler("macos", scheduler_before)
    control = FakeControl(layout.hermes_home)
    companion_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.hermes_home)

    def interrupt(artifact: str) -> None:
        if artifact == boundary:
            raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt):
        _execute(
            tmp_path,
            plan,
            _adapter(
                layout,
                plan,
                selection,
                store,
                runner,
                scheduler,
                control,
                mutation_hook=interrupt,
            ),
        )

    recovery = _adapter(
        layout,
        plan,
        selection,
        store,
        FakeRunner("macos"),
        scheduler,
        control,
        credential_value=None,
        recovery_only=True,
    )
    result = TransactionEngine(tmp_path / "transactions").recover(
        plan,
        recovery,
        transaction_id="ready",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert _tree_state(layout.runtime.companion_home) == companion_before
    assert _tree_state(layout.hermes_home) == profile_before
    assert scheduler.state == scheduler_before
    assert control.state() == HermesControlState(
        (("daemon_core", False), ("daemon_voice", False)),
        "stopped",
    )
    assert store.values == {}


def test_scheduler_concurrency_is_preserved_and_surfaces_rollback_failure(
    tmp_path: Path,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler = FakeScheduler("macos")
    control = FakeControl(layout.hermes_home)

    def conflict(artifact: str) -> None:
        if artifact == NIGHTLY_JOB:
            scheduler.state[AFTER_TURN_JOB] = b"third-party"
            raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError, match="manual recovery"):
        _execute(
            tmp_path,
            plan,
            _adapter(
                layout,
                plan,
                selection,
                store,
                runner,
                scheduler,
                control,
                mutation_hook=conflict,
            ),
        )

    assert scheduler.state[AFTER_TURN_JOB] == b"third-party"


def test_reordered_or_wrong_scheduler_plan_fails_before_mutation(tmp_path: Path) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler = FakeScheduler("windows")
    control = FakeControl(layout.hermes_home)
    reordered = SetupPlan(
        plan.selected_host,
        plan.daemon_name,
        plan.daemon_gender,
        plan.provider,
        (*plan.steps[:-2], plan.steps[-1], plan.steps[-2]),
    )

    with pytest.raises(ValueError, match="incomplete or out of order"):
        _adapter(
            layout,
            reordered,
            selection,
            store,
            runner,
            FakeScheduler("macos"),
            control,
        )
    with pytest.raises(ValueError, match="scheduler differs"):
        _adapter(layout, plan, selection, store, runner, scheduler, control)
    assert not (layout.runtime.runtime_root).exists()
    assert store.values == {}


def test_plan_can_be_built_before_private_release_exists(tmp_path: Path) -> None:
    layout, plan, _selection, _store, _runner = _fixture(tmp_path)

    assert not layout.runtime.current_pointer.exists()
    assert tuple(step.code for step in plan.steps) == HERMES_READY_STEP_CODES
