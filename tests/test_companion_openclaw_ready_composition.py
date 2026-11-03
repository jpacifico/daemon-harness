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
    OPENCLAW_API_JOB,
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
from daemons_companion.openclaw.activation import OpenClawControlState
from daemons_companion.openclaw.profile import (
    create_disposable_openclaw_marker,
)
from daemons_companion.openclaw.ready_composition import (
    OPENCLAW_READY_STEP_CODES,
    OpenClawReadyCompositionAdapter,
    OpenClawReadyCompositionLayout,
    build_dormant_openclaw_ready_plan,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)
from daemons_companion.runtime.environment import CommandResult
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
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
            Path(environment["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
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
    def __init__(self, openclaw_state: Path) -> None:
        self.openclaw_state = openclaw_state
        self.gateway = "stopped"
        self.ready = True

    def state(self) -> OpenClawControlState:
        return OpenClawControlState(self.gateway)

    def start_gateway(self) -> None:
        self.gateway = "running"

    def restart_gateway(self) -> None:
        self.gateway = "running"

    def stop_gateway(self) -> None:
        self.gateway = "stopped"

    def gateway_ready(self) -> bool:
        return self.gateway == "running" and self.ready

    def plugin_ready(self) -> bool:
        return self.ready


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "example==1 \\\n    --hash=sha256:" + "0" * 64 + "\n",
        "requirements.txt": "example==1\n",
        "src/daemon_core/__init__.py": "# synthetic\n",
        "src/daemons_companion/runtime/service_entry.py": "# entry\n",
        "src/daemon_core/nightly_maintenance.py": "# nightly\n",
        "src/daemons_companion/runtime/services.py": "# services\n",
        "integrations/hermes_plugin/__init__.py": "# memory\n",
        "integrations/hermes_voice_plugin/__init__.py": "# voice\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/openclaw_plugin/workspace/AGENTS.md": "# Canonical\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if path.name == "service.sh" else 0o644)
    return root.absolute()


def _fixture(tmp_path: Path, target_os: str = "macos", job_namespace=None):
    companion = (tmp_path / "companion").absolute()
    profile = (tmp_path / "openclaw").absolute()
    create_disposable_companion_marker(companion)
    create_disposable_openclaw_marker(profile)
    (profile / "openclaw.json").write_text(
        json.dumps(
            {
                "existing": True,
                "gateway": {
                    "mode": "local",
                    "port": 18790,
                    "bind": "loopback",
                    "auth": {"mode": "none"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    memory = companion / "data/memory"
    memory.mkdir(parents=True)
    (memory / "persona.md").write_text("canonical voice\n", encoding="utf-8")
    uv = (tmp_path / ("uv.exe" if target_os == "windows" else "uv")).absolute()
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o700)
    runtime = CompanionRuntimeLayout(companion, _bundle(tmp_path / "bundle"))
    layout = OpenClawReadyCompositionLayout(
        runtime,
        profile,
        uv,
        hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os,
        18000,
        18790,
        job_namespace=job_namespace,
    )
    selection = ProviderSelection(
        ProviderKind.OPENAI_API,
        "synthetic-model",
        credential_available=True,
    )
    reviewed = build_setup_plan(
        SetupRequest(HostKind.OPENCLAW, "Lyra", "feminine", selection),
        (
            HostProbe(
                HostKind.OPENCLAW,
                True,
                True,
                executable="/synthetic/openclaw",
                config_path=str(profile),
                version="synthetic",
                version_status="verified",
            ),
        ),
    )
    plan = build_dormant_openclaw_ready_plan(reviewed, layout)
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
    mutation_hook=None,
):
    return OpenClawReadyCompositionAdapter(
        layout,
        plan=plan,
        provider_selection=selection,
        credential_store=store,
        credential_value="synthetic-secret",
        user_id="synthetic-user",
        scheduler=scheduler,
        control=control,
        readiness_probe=lambda *_arguments: True,
        api_probe=lambda port: port == 18000,
        runner=runner,
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
    control = FakeControl(layout.openclaw_state)

    result = _execute(
        tmp_path,
        plan,
        _adapter(layout, plan, selection, store, runner, scheduler, control),
    )

    assert result.status == "committed"
    assert tuple(step.code for step in plan.steps) == OPENCLAW_READY_STEP_CODES
    assert set(scheduler.state) == {
        scoped_job_code(code, job_namespace) for code in (AFTER_TURN_JOB, NIGHTLY_JOB, OPENCLAW_API_JOB,)
    }
    assert control.state() == OpenClawControlState("running")
    config = json.loads((layout.openclaw_state / "openclaw.json").read_text())
    plugin = config["plugins"]["entries"]["daemon-harness"]
    assert plugin["config"]["daemonUrl"] == "http://127.0.0.1:18000"
    assert plugin["hooks"]["allowConversationAccess"] is True
    assert (layout.runtime.memory_root / "persona.md").read_text() == "canonical voice\n"
    assert "synthetic-secret" not in json.dumps(plan.to_public_dict())


@pytest.mark.parametrize(
    "boundary",
    [AFTER_TURN_JOB, NIGHTLY_JOB, OPENCLAW_API_JOB, "gateway"],
)
def test_failure_at_each_service_boundary_restores_every_owner(
    tmp_path: Path,
    boundary: str,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler_before = {
        AFTER_TURN_JOB: b"old worker",
        NIGHTLY_JOB: b"old nightly",
        OPENCLAW_API_JOB: b"old api",
    }
    scheduler = FakeScheduler("macos", scheduler_before)
    control = FakeControl(layout.openclaw_state)
    control.gateway = "running"
    control_before = control.state()
    companion_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.openclaw_state)

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
    assert _tree_state(layout.openclaw_state) == profile_before
    assert scheduler.state == scheduler_before
    assert control.state() == control_before
    assert store.values == {}
    assert "synthetic-secret" not in (
        tmp_path / "transactions/ready/journal.json"
    ).read_text()


def test_plan_tampering_is_refused_before_mutation(tmp_path: Path) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    scheduler = FakeScheduler("macos")
    control = FakeControl(layout.openclaw_state)
    before = _tree_state(tmp_path)

    for changed in (
        SetupPlan(
            plan.selected_host,
            plan.daemon_name,
            plan.daemon_gender,
            plan.provider,
            tuple(reversed(plan.steps)),
        ),
        SetupPlan(
            HostProbe(HostKind.HERMES, True, True, config_path=str(layout.openclaw_state)),
            plan.daemon_name,
            plan.daemon_gender,
            plan.provider,
            plan.steps,
        ),
    ):
        with pytest.raises(ValueError):
            _adapter(
                layout,
                changed,
                selection,
                store,
                runner,
                scheduler,
                control,
            )

    assert _tree_state(tmp_path) == before
