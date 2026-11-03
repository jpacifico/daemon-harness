from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupPlan,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.hermes.composition import (
    HERMES_COMPOSITION_STEP_CODES,
    HermesCompositionAdapter,
    HermesCompositionLayout,
    build_dormant_hermes_composition_plan,
)
from daemons_companion.hermes.profile import create_disposable_profile_marker
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
    resolve_current_release,
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
    def __init__(self, target_os: str = "macos") -> None:
        self.target_os = target_os
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command, environment) -> CommandResult:
        command = list(command)
        environment = dict(environment)
        self.calls.append((command, environment))
        if command[1:] == ["--version"]:
            return CommandResult(0, "uv 0.12.16 (synthetic)\n")
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
            (cache / "wheel").write_text("temporary", encoding="utf-8")
            return CommandResult(0)
        if command[0].endswith(("/python", "python.exe")):
            return CommandResult(0, "daemon-runtime-ok\n")
        return CommandResult(1, stderr="unexpected command")


def _bundle(root: Path) -> Path:
    lock = "example==1 \\\n    --hash=sha256:" + ("0" * 64) + "\n"
    files = {
        "requirements-runtime.lock": lock,
        "requirements.txt": "example==1\n",
        "src/daemon_core/__init__.py": 'VERSION = "synthetic"\n',
        "src/daemon_core/loop.py": "# synthetic loop\n",
        "integrations/hermes_plugin/__init__.py": "# memory\n",
        "integrations/hermes_voice_plugin/__init__.py": "# voice\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\nexit 0\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\nexit 0\n",
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


def _fixture(
    tmp_path: Path,
    *,
    target_os: str = "macos",
    provider_kind=ProviderKind.OPENAI_API,
) -> tuple[
    HermesCompositionLayout,
    SetupPlan,
    ProviderSelection,
    FakeCredentialStore,
    FakeRunner,
]:
    companion_home = (tmp_path / "companion").absolute()
    hermes_home = (tmp_path / "profiles/.hermes").absolute()
    create_disposable_companion_marker(companion_home)
    create_disposable_profile_marker(hermes_home)
    memory = companion_home / "data/memory"
    memory.mkdir(parents=True)
    (memory / "persona.md").write_text("canonical voice\n", encoding="utf-8")
    uv = (tmp_path / ("uv.exe" if target_os == "windows" else "uv")).absolute()
    uv.write_bytes(b"synthetic uv 0.12.16")
    uv.chmod(0o700)
    runtime = CompanionRuntimeLayout(companion_home, _bundle(tmp_path / "bundle"))
    layout = HermesCompositionLayout(
        runtime=runtime,
        hermes_home=hermes_home,
        uv_executable=uv,
        uv_sha256=hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os=target_os,
    )
    selection = ProviderSelection(
        provider_kind,
        "synthetic-model",
        credential_available=True,
    )
    probe = HostProbe(
        HostKind.HERMES,
        detected=True,
        usable=True,
        executable="/synthetic/hermes",
        config_path=str(hermes_home),
        version="synthetic",
        version_status="verified",
    )
    reviewed = build_setup_plan(
        SetupRequest(
            host=HostKind.HERMES,
            daemon_name="Lyra",
            daemon_gender="feminine",
            provider=selection,
        ),
        (probe,),
    )
    plan = build_dormant_hermes_composition_plan(reviewed, layout)
    return layout, plan, selection, FakeCredentialStore(), FakeRunner(target_os)


def _adapter(
    layout: HermesCompositionLayout,
    plan: SetupPlan,
    selection: ProviderSelection,
    store: FakeCredentialStore,
    runner: FakeRunner,
    *,
    credential_value: str | None = "synthetic-secret",
    recovery_only: bool = False,
    mutation_hook=None,
) -> HermesCompositionAdapter:
    return HermesCompositionAdapter(
        layout,
        plan=plan,
        provider_selection=selection,
        credential_store=store,
        credential_value=credential_value,
        user_id="synthetic-user",
        runner=runner,
        recovery_only=recovery_only,
        mutation_hook=mutation_hook,
    )


def _execute(
    tmp_path: Path,
    plan: SetupPlan,
    adapter: HermesCompositionAdapter,
    *,
    transaction_id: str = "hermes-composition",
):
    return TransactionEngine(tmp_path / "transactions").execute(
        plan,
        adapter,
        transaction_id=transaction_id,
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
def test_composes_consistent_private_hermes_installation(
    tmp_path: Path,
    target_os: str,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path, target_os=target_os)
    layout.hermes_home.joinpath("unrelated.txt").write_text("keep", encoding="utf-8")

    result = _execute(tmp_path, plan, _adapter(layout, plan, selection, store, runner))

    assert result.status == "committed"
    release = resolve_current_release(layout.runtime)
    environment = layout.runtime_environment.environment_path(release.name)
    identity = json.loads(
        (layout.runtime.memory_root / "core/daemon_identity.json").read_text()
    )
    assert identity == {"name": "Lyra", "gender": "feminine"}
    assert (layout.runtime.memory_root / "persona.md").read_text() == "canonical voice\n"
    provider_text = (layout.runtime.state_root / "config/provider.json").read_text()
    assert "synthetic-secret" not in provider_text
    assert len(store.values) == 1

    env_text = layout.hermes_home.joinpath(".env").read_text(encoding="utf-8")
    expected_interpreter = environment / (
        "venv/Scripts/python.exe" if target_os == "windows" else "venv/bin/python"
    )
    for expected in (
        f"DAEMON_CORE_SRC={release / 'src'}",
        f"DAEMON_CORE_MEMORY_ROOT={layout.runtime.memory_root}",
        f"DAEMON_CORE_STATE_ROOT={layout.runtime.state_root}",
        "DAEMON_CORE_PROVIDER_CONFIG="
        f"{layout.runtime.state_root / 'config/provider.json'}",
        f"DAEMON_CORE_PYTHON={expected_interpreter}",
        f"DAEMON_CORE_LATENCY_LOG={layout.runtime.state_root / 'latency/hermes.jsonl'}",
    ):
        assert expected in env_text
    for name, source in (("daemon_core", "hermes_plugin"), ("daemon_voice", "hermes_voice_plugin")):
        destination = layout.hermes_home / "plugins" / name
        original = release / "integrations" / source
        if target_os == "windows":
            assert not destination.is_symlink()
            assert (destination / "__init__.py").read_bytes() == (original / "__init__.py").read_bytes()
        else:
            assert os.readlink(destination) == str(original)
    assert layout.hermes_home.joinpath("unrelated.txt").read_text() == "keep"
    public = json.dumps(plan.to_public_dict())
    journal = (tmp_path / "transactions/hermes-composition/journal.json").read_text()
    assert "synthetic-secret" not in public
    assert "synthetic-secret" not in journal
    assert layout.uv_sha256 in public


def test_second_composition_is_byte_idempotent_and_reuses_credential(
    tmp_path: Path,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    _execute(
        tmp_path,
        plan,
        _adapter(layout, plan, selection, store, runner),
        transaction_id="first",
    )
    home_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.hermes_home)
    credential_before = dict(store.values)
    second_runner = FakeRunner()

    _execute(
        tmp_path,
        plan,
        _adapter(layout, plan, selection, store, second_runner),
        transaction_id="second",
    )

    assert _tree_state(layout.runtime.companion_home) == home_before
    assert _tree_state(layout.hermes_home) == profile_before
    assert store.values == credential_before
    assert not any(call[0][1:3] == ["python", "install"] for call in second_runner.calls)


@pytest.mark.parametrize("boundary", HERMES_COMPOSITION_STEP_CODES)
def test_each_completed_step_failure_restores_both_roots_and_credential_store(
    tmp_path: Path,
    boundary: str,
    provider_kind=ProviderKind.OPENAI_API,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path, provider_kind=provider_kind)
    companion_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.hermes_home)

    def fail(step_code: str) -> None:
        if step_code == boundary:
            raise RuntimeError("synthetic failure containing synthetic-secret")

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
                mutation_hook=fail,
            ),
        )

    assert _tree_state(layout.runtime.companion_home) == companion_before
    assert _tree_state(layout.hermes_home) == profile_before
    assert store.values == {}
    journal = (tmp_path / "transactions/hermes-composition/journal.json").read_text()
    assert "synthetic-secret" not in journal


@pytest.mark.parametrize("boundary", HERMES_COMPOSITION_STEP_CODES)
@pytest.mark.parametrize("provider_kind", [ProviderKind.OPENAI_API, ProviderKind.MISTRAL_API, ProviderKind.ANTHROPIC_API])
def test_each_completed_step_interruption_recovers_without_provider_secret(
    tmp_path: Path,
    boundary: str,
    provider_kind,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path, provider_kind=provider_kind)
    companion_before = _tree_state(layout.runtime.companion_home)
    profile_before = _tree_state(layout.hermes_home)

    def interrupt(step_code: str) -> None:
        if step_code == boundary:
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
                mutation_hook=interrupt,
            ),
        )

    recovery = _adapter(
        layout,
        plan,
        selection,
        store,
        FakeRunner(),
        credential_value=None,
        recovery_only=True,
    )
    result = TransactionEngine(tmp_path / "transactions").recover(
        plan,
        recovery,
        transaction_id="hermes-composition",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert _tree_state(layout.runtime.companion_home) == companion_before
    assert _tree_state(layout.hermes_home) == profile_before
    assert store.values == {}


def test_child_conflict_is_preserved_and_surfaces_rollback_failure(tmp_path: Path) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)

    def conflict(step_code: str) -> None:
        if step_code != "identity":
            return
        identity = layout.runtime.memory_root / "core/daemon_identity.json"
        identity.write_text('{"third_party":true}\n', encoding="utf-8")
        raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            plan,
            _adapter(
                layout,
                plan,
                selection,
                store,
                runner,
                mutation_hook=conflict,
            ),
        )

    assert json.loads(
        (layout.runtime.memory_root / "core/daemon_identity.json").read_text()
    ) == {"third_party": True}
    assert TransactionEngine(tmp_path / "transactions").inspect(
        "hermes-composition"
    ).status == "rollback_failed"


def test_public_reordered_other_host_and_provider_mismatch_are_rejected(
    tmp_path: Path,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    probe = plan.selected_host
    public = build_setup_plan(
        SetupRequest(
            HostKind.HERMES,
            "Lyra",
            "feminine",
            selection,
        ),
        (probe,),
    )
    with pytest.raises(ValueError, match="incomplete or out of order"):
        _adapter(layout, public, selection, store, runner)

    reordered = SetupPlan(
        selected_host=plan.selected_host,
        daemon_name=plan.daemon_name,
        daemon_gender=plan.daemon_gender,
        provider=plan.provider,
        steps=tuple(reversed(plan.steps)),
    )
    with pytest.raises(ValueError, match="incomplete or out of order"):
        _adapter(layout, reordered, selection, store, runner)

    other_host = SetupPlan(
        selected_host=HostProbe(HostKind.OPENCLAW, True, True),
        daemon_name=plan.daemon_name,
        daemon_gender=plan.daemon_gender,
        provider=plan.provider,
        steps=plan.steps,
    )
    with pytest.raises(ValueError, match="Hermes host"):
        _adapter(layout, other_host, selection, store, runner)

    mismatched = ProviderSelection(
        ProviderKind.OPENAI_API,
        "other-model",
        credential_available=True,
    )
    with pytest.raises(ValueError, match="provider differs"):
        _adapter(layout, plan, mismatched, store, runner)


def test_bundle_change_after_confirmation_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    layout, plan, selection, store, runner = _fixture(tmp_path)
    adapter = _adapter(layout, plan, selection, store, runner)
    before_companion = _tree_state(layout.runtime.companion_home)
    before_profile = _tree_state(layout.hermes_home)
    (layout.runtime.bundle_root / "requirements.txt").write_text(
        "changed==2\n",
        encoding="utf-8",
    )

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, plan, adapter)

    assert _tree_state(layout.runtime.companion_home) == before_companion
    assert _tree_state(layout.hermes_home) == before_profile
    assert store.values == {}
    assert not any(call[0][1:3] == ["python", "install"] for call in runner.calls)
