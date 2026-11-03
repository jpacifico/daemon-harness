"""No Hermes installation is needed for the scoped first OpenClaw transaction."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.openclaw.host_install import (
    OpenClawHostLayout,
    plan,
    OPENCLAW_VERSION,
)
from daemons_companion.openclaw.ready_composition import (
    OpenClawReadyCompositionAdapter,
    build_dormant_openclaw_ready_plan,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    validate_companion_home,
)
from daemons_companion.setup.plan import (
    HostProbe,
    HostKind,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.setup.transaction import TransactionApplyError
from tests.test_companion_openclaw_ready_composition import (
    _fixture,
    _execute,
    FakeScheduler,
    FakeControl,
)


def owned_host(root, target="macos"):
    lock = Path(__file__).resolve().parents[1] / "apps/companion/openclaw-runtime"
    host = OpenClawHostLayout(root.resolve(), "f" * 32, target, lock)
    for p in (host.profile, host.host, host.attempt / "os-home"):
        p.mkdir(parents=True, exist_ok=True)
    host.node.parent.mkdir(parents=True)
    host.node.write_bytes(b"synthetic-node")
    host.entry.parent.mkdir(parents=True)
    host.entry.write_bytes(b"synthetic-entry")
    (host.entry.parent / "package.json").write_text(
        json.dumps({"version": OPENCLAW_VERSION})
    )
    for n in ("package.json", "package-lock.json"):
        shutil.copyfile(lock / n, host.host / n)
    proof = plan(host)
    owner = dict(
        proof,
        status="installed",
        node_executable_sha256=hashlib.sha256(host.node.read_bytes()).hexdigest(),
        entry_sha256=hashlib.sha256(host.entry.read_bytes()).hexdigest(),
    )
    host.receipt.write_text(json.dumps(owner))
    (host.root / "current.json").write_text(
        json.dumps(
            dict(
                schema="daemon.openclaw-first.v1",
                installation_id=host.installation_id,
                target_os=target,
                acquisition=proof["revision"],
                api_port=18000,
                gateway_port=18790,
            )
        )
    )
    return host


@pytest.mark.parametrize("target", ["macos", "windows"])
def test_scope_requires_full_pinned_proof_and_never_accepts_a_personal_marker(
    tmp_path, target
):
    host = owned_host(tmp_path / "owned", target)
    scope = InstallationScope(host.attempt)
    scope.initialize()
    assert validate_companion_home(scope.home, scope) == scope.home
    with pytest.raises(ValueError):
        validate_companion_home(scope.home)
    with pytest.raises(ValueError):
        scope.validate_namespace(None)
    host.node.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        scope.validate_home(scope.home)


@pytest.mark.parametrize("target", ["macos", "windows"])
@pytest.mark.parametrize("fail", [None, "openclaw-profile", "gateway"])
@pytest.mark.parametrize("subscription", [False, True])
def test_first_transaction_and_rollback_without_hermes(tmp_path, target, fail, subscription):
    host = owned_host(tmp_path / "owned", target)
    scope = InstallationScope(host.attempt)
    scope.initialize()
    (host.attempt / "connection.json").write_text(
        json.dumps(
            dict(
                schema="daemon.private-connection.v2",
                provider_kind="openai_api",
                user_id="777001",
            )
        )
    )
    (host.profile / "openclaw.json").write_text('{"gateway":{"mode":"local"}}\n')
    baseline = (host.profile / "openclaw.json").read_bytes()
    layout, _, selection, store, runner = _fixture(tmp_path / "fixture", target)
    runtime = CompanionRuntimeLayout(scope.home, layout.runtime.bundle_root, scope)
    runtime.memory_root.mkdir(parents=True)
    (runtime.memory_root / "keep.md").write_text("synthetic retained memory")
    layout = replace(
        layout,
        runtime=runtime,
        openclaw_state=host.profile,
        job_namespace=scope.installation_id,
    )
    if subscription:
        from daemons_companion.setup.plan import ProviderSelection, ProviderKind
        selection = ProviderSelection(ProviderKind.CODEX_CLI, "gpt-6-astra")
        auth = host.attempt / "os-home/.codex"
        auth.mkdir()
        binary = host.attempt / "codex"
        binary.write_bytes(b"synthetic codex")
        (host.attempt / "connection.json").write_text(json.dumps(dict(
            schema="daemon.private-connection.v1", codex=str(binary),
            codex_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), user_id="777001")))
    reviewed = build_setup_plan(
        SetupRequest(HostKind.OPENCLAW, "Aster", "feminine", selection),
        (
            HostProbe(
                HostKind.OPENCLAW,
                True,
                True,
                str(host.entry),
                str(host.profile),
                OPENCLAW_VERSION,
                "verified",
            ),
        ),
        codex_available=subscription,
    )
    prepared = build_dormant_openclaw_ready_plan(reviewed, layout)
    scheduler = FakeScheduler(target)
    control = FakeControl(host.profile)
    reached = []

    def hook(code):
        reached.append(code)
        if code == fail:
            raise RuntimeError("synthetic interrupted step")

    adapter = OpenClawReadyCompositionAdapter(
        layout,
        plan=prepared,
        provider_selection=selection,
        credential_store=store,
        credential_value=None if subscription else "synthetic-secret",
        codex_available=subscription,
        user_id="777001",
        scheduler=scheduler,
        control=control,
        runner=runner,
        readiness_probe=lambda *_: True,
        api_probe=lambda *_: True,
        mutation_hook=hook,
    )
    if fail:
        with pytest.raises(TransactionApplyError):
            _execute(tmp_path, prepared, adapter)
        assert fail in reached
        assert (host.profile / "openclaw.json").read_bytes() == baseline
        assert not (host.profile / ".env").exists()
        assert not list((runtime.state_root / "host-bindings").glob("*.json"))
        assert not scheduler.state
        assert control.gateway == "stopped"
    else:
        result = _execute(tmp_path, prepared, adapter)
        assert result.status == "committed"
        binding = json.loads(
            next((runtime.state_root / "host-bindings").glob("*.json")).read_text()
        )
        assert (
            binding["profile"] == str(host.profile) and binding["user_id"] == "777001"
        )
        assert "--installation-attempt" in "\n".join(
            s.decode("utf-16" if s.startswith(b"\xff\xfe") else "utf8")
            for s in scheduler.state.values()
        )
    assert (runtime.memory_root / "keep.md").read_text() == "synthetic retained memory"
    assert not (host.companion_home / "hosts/hermes").exists()


@pytest.mark.parametrize("target", ["macos", "windows"])
def test_unfinished_openclaw_cleanup_preserves_memory_and_refuses_completed(
    tmp_path, target, monkeypatch
):
    from daemons_companion.setup.discard import Discard
    from daemons_companion.removal.errors import Refused

    base = tmp_path.resolve() / "Companion"
    root = base / "instance-ABC123"
    host = owned_host(root, target)
    scope = InstallationScope(host.attempt)
    scope.initialize()
    memory = scope.home / "data/memory"
    memory.mkdir(parents=True)
    (memory / "keep.md").write_text("retained synthetic memory")
    agents = tmp_path.resolve() / "LaunchAgents"
    agents.mkdir()

    class Scheduler(FakeScheduler):
        def require_exclusive(self, *args):
            pass

    scheduler = Scheduler(target)
    operation = Discard(
        root,
        base,
        launch_agents=agents,
        scheduler_factory=lambda _: scheduler,
        processes=lambda: "",
        target_os=target,
        user_sid="S-1-5-21-123",
    )
    # Native Windows deletion itself is exercised separately on Windows. This
    # platform-layout test uses the real POSIX deletion implementation here.
    if target == "windows":
        monkeypatch.setattr(
            "daemons_companion.setup.discard.remove_tree",
            lambda p, identity: shutil.rmtree(p),
        )
    review = operation.inspect()
    assert any(p.endswith("/host") for p in review["programs"])
    assert any(p.endswith("/node") for p in review["programs"])
    completed = host.attempt / "daemon-setup.json"
    completed.write_text("{}")
    with pytest.raises(Refused) as error:
        operation.inspect()
    assert error.value.code == "completed"
    completed.unlink()
    assert operation.apply(review["revision"])["done"]
    assert not host.host.exists() and not host.node_root.exists()
    assert (memory / "keep.md").read_text() == "retained synthetic memory"
    assert host.profile.is_dir()


@pytest.mark.parametrize(
    "channel,ready",
    [
        ({"configured": True, "running": True, "lifecycle": "running"}, True),
        ({"configured": True, "running": False}, False),
        ({"configured": True, "running": True, "terminalDisconnect": True}, False),
        ({"configured": True, "running": True, "lifecycle": "starting"}, False),
        ({"configured": True, "running": True, "probe": {"ok": False}}, False),
        ({}, False),
    ],
)
def test_first_gateway_requires_telegram_running(tmp_path, channel, ready):
    from types import SimpleNamespace
    from daemons_companion.openclaw.gateway import PrivateOpenClawControl

    (tmp_path / "openclaw.json").write_text(
        json.dumps({"channels": {"telegram": {"enabled": True}}})
    )
    control = object.__new__(PrivateOpenClawControl)
    control.host = SimpleNamespace(profile=tmp_path)
    control.port = 18789
    control._probe = lambda *args, **_options: {"ok": True, "channels": {"telegram": channel}}
    assert control.gateway_ready() is ready


@pytest.mark.parametrize("target", ["macos", "windows"])
def test_first_host_choice_routes_without_a_hermes_install(target, monkeypatch):
    from types import SimpleNamespace
    from test_companion_terminal import FakeUI, backend
    from daemons_companion.desktop.terminal import run_wizard
    from daemons_companion.openclaw import first_install as openclaw_first

    owner = SimpleNamespace(
        context=SimpleNamespace(target_os=target), resume=lambda ui: False
    )
    seen = []
    monkeypatch.setattr(
        openclaw_first,
        "run",
        lambda setup, ui: seen.append(setup.selected_host) or 0,
    )
    assert (
        run_wizard(
            FakeUI(choices=[1]),
            backend=backend(host="absent"),
            host_setup=owner,
            supported_only=True,
        )
        == 0
    )
    assert seen == ["openclaw"]


@pytest.mark.parametrize("target", ["macos", "windows"])
def test_native_host_uses_its_private_npm_and_refuses_changed_launcher(
    tmp_path, target
):
    from daemons_companion.openclaw.gateway import private_npm_environment

    host = owned_host(tmp_path / "private host", target)
    env = private_npm_environment(host, prepare=True)
    assert env["NPM_CONFIG_USERCONFIG"] == str(host.attempt / "empty.npmrc")
    assert env["NPM_CONFIG_CACHE"] == str(host.attempt / "npm-cache")
    assert private_npm_environment(host) == env
    launcher = host.attempt / "tools" / ("npm.cmd" if target == "windows" else "npm")
    assert str(host.node) in launcher.read_text()
    assert str(host.npm) in launcher.read_text()
    launcher.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        private_npm_environment(host, prepare=True)
    assert launcher.read_text() == "changed"


def test_windows_npm_preserves_percent_and_exclamation_in_owned_path(tmp_path):
    from daemons_companion.openclaw.gateway import private_npm_environment

    host = owned_host(tmp_path / "test %USERNAME% !folder!", "windows")
    private_npm_environment(host, prepare=True)
    content = (host.attempt / "tools/npm.cmd").read_text()
    assert "DisableDelayedExpansion" in content
    assert "%%USERNAME%% !folder!" in content
