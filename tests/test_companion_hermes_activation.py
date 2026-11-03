from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from daemons_companion.setup.plan import PlanStep
from daemons_companion.hermes.activation import (
    HermesActivationAdapter,
    HermesActivationLayout,
    HermesCliBackend,
    HermesCommandResult,
    HermesControlState,
    hermes_activation_step,
)
from daemons_companion.hermes.profile import create_disposable_profile_marker
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
    verify_current_release,
)


class FakeControl:
    def __init__(
        self,
        hermes_home: Path,
        *,
        memory: bool = False,
        voice: bool = False,
        gateway: str = "stopped",
        ready: bool = True,
    ) -> None:
        self.hermes_home = hermes_home
        self.plugins = {"daemon_core": memory, "daemon_voice": voice}
        self.gateway = gateway
        self.ready = ready
        self.calls: list[str] = []

    def state(self) -> HermesControlState:
        return HermesControlState(tuple(self.plugins.items()), self.gateway)

    def enable_plugin(self, name: str) -> None:
        self.plugins[name] = True
        self.calls.append(f"enable:{name}")

    def disable_plugin(self, name: str) -> None:
        self.plugins[name] = False
        self.calls.append(f"disable:{name}")

    def start_gateway(self) -> None:
        self.gateway = "running"
        self.calls.append("start")

    def restart_gateway(self) -> None:
        self.gateway = "running"
        self.calls.append("restart")

    def stop_gateway(self) -> None:
        self.gateway = "stopped"
        self.calls.append("stop")

    def gateway_ready(self) -> bool:
        return self.ready and self.gateway == "running"


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "example==1 \\\n    --hash=sha256:"
        + "0" * 64
        + "\n",
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
    return root.absolute()


def _layout(tmp_path: Path) -> HermesActivationLayout:
    companion = (tmp_path / "companion").absolute()
    profile = (tmp_path / "profile").absolute()
    create_disposable_companion_marker(companion)
    runtime = CompanionRuntimeLayout(companion, _bundle(tmp_path / "bundle"))
    snapshot = tmp_path / "runtime-snapshot"
    snapshot.mkdir()
    adapter = CompanionRuntimeAdapter(runtime)
    step = PlanStep("runtime-layout", "runtime", ())
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    adapter.verify(step)
    release = verify_current_release(runtime)

    create_disposable_profile_marker(profile)
    plugins = profile / "plugins"
    plugins.mkdir()
    (plugins / "daemon_core").symlink_to(release / "integrations/hermes_plugin")
    (plugins / "daemon_voice").symlink_to(release / "integrations/hermes_voice_plugin")
    return HermesActivationLayout(runtime, profile)


def test_activation_enables_only_two_plugins_and_starts_stopped_gateway(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(layout.hermes_home)
    adapter = HermesActivationAdapter(
        layout,
        control=control,
        readiness_probe=lambda revision, memory, spool: (
            revision == verify_current_release(layout.runtime).name
            and memory == layout.runtime.memory_root
            and spool == layout.runtime.state_root / "after-turn-spool"
        ),
    )
    step = hermes_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()

    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    adapter.verify(step)

    assert control.plugins == {"daemon_core": True, "daemon_voice": True}
    assert control.gateway == "running"
    assert control.calls == [
        "enable:daemon_core",
        "enable:daemon_voice",
        "start",
    ]


def test_activation_restarts_running_gateway_and_restores_previous_state(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(layout.hermes_home, memory=True, gateway="running")
    adapter = HermesActivationAdapter(
        layout,
        control=control,
        readiness_probe=lambda *_arguments: True,
    )
    step = hermes_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()

    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    adapter.verify(step)
    adapter.rollback(step, snapshot)
    assert control.state() == HermesControlState(
        (("daemon_core", True), ("daemon_voice", False)),
        "stopped",
    )

    adapter.restore_gateway_after_rollback(snapshot)
    assert control.state() == HermesControlState(
        (("daemon_core", True), ("daemon_voice", False)),
        "running",
    )


def test_activation_rejects_changed_control_state_before_apply(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(layout.hermes_home)
    adapter = HermesActivationAdapter(layout, control=control)
    step = hermes_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()
    adapter.snapshot(step, snapshot)
    control.plugins["daemon_voice"] = True

    with pytest.raises(RuntimeError, match="changed after its snapshot"):
        adapter.apply(step)


def test_activation_does_not_finalize_a_conflicted_rollback(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(layout.hermes_home, memory=True)
    adapter = HermesActivationAdapter(
        layout,
        control=control,
        readiness_probe=lambda *_arguments: True,
    )
    step = hermes_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    control.plugins["daemon_core"] = False

    with pytest.raises(RuntimeError, match="concurrently"):
        adapter.rollback(step, snapshot)
    with pytest.raises(RuntimeError, match="not ready to finalize"):
        adapter.restore_gateway_after_rollback(snapshot)
    assert control.gateway == "running"


def test_activation_requires_gateway_and_worker_readiness(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    step = hermes_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()
    gateway = FakeControl(layout.hermes_home, ready=False)
    adapter = HermesActivationAdapter(
        layout,
        control=gateway,
        readiness_probe=lambda *_arguments: True,
        readiness_attempts=1,
    )
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    with pytest.raises(RuntimeError, match="gateway readiness"):
        adapter.verify(step)

    worker = FakeControl(layout.hermes_home)
    other_snapshot = tmp_path / "activation-snapshot-2"
    other_snapshot.mkdir()
    adapter = HermesActivationAdapter(
        layout,
        control=worker,
        readiness_probe=lambda *_arguments: False,
        readiness_attempts=1,
    )
    adapter.snapshot(step, other_snapshot)
    adapter.apply(step)
    with pytest.raises(RuntimeError, match="after-turn readiness"):
        adapter.verify(step)


def test_cli_backend_uses_explicit_profile_and_drops_inherited_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "hermes"
    executable.write_text("synthetic", encoding="utf-8")
    profile = (tmp_path / "profile").absolute()
    profile.mkdir()
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(command, environment):
        command = tuple(command)
        calls.append((command, dict(environment)))
        if command[1:] == ("plugins", "list", "--json"):
            return HermesCommandResult(
                0,
                json.dumps(
                    [
                        {"name": "daemon_core", "status": "enabled"},
                        {"name": "daemon_voice", "status": "not enabled"},
                        {"name": "third-party", "status": "enabled"},
                    ]
                ),
            )
        if command[1:] == ("gateway", "status"):
            return HermesCommandResult(0)
        return HermesCommandResult(0)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    backend = HermesCliBackend(executable.absolute(), profile, runner=runner)

    assert backend.state() == HermesControlState(
        (("daemon_core", True), ("daemon_voice", False)),
        "running",
    )
    backend.enable_plugin("daemon_voice")
    backend.disable_plugin("daemon_core")
    backend.restart_gateway()
    assert backend.gateway_ready()
    assert [command[1:] for command, _environment in calls] == [
        ("plugins", "list", "--json"),
        ("gateway", "status"),
        ("plugins", "enable", "daemon_voice"),
        ("plugins", "disable", "daemon_core"),
        ("gateway", "restart"),
        ("gateway", "status", "--deep"),
    ]
    assert all(environment["HERMES_HOME"] == str(profile) for _, environment in calls)
    assert all(
        environment["PYTHONDONTWRITEBYTECODE"] == "1" for _, environment in calls
    )
    assert all("OPENAI_API_KEY" not in environment for _, environment in calls)


def test_cli_backend_rejects_unmanaged_plugin(tmp_path: Path) -> None:
    executable = tmp_path / "hermes"
    executable.write_text("synthetic", encoding="utf-8")
    profile = (tmp_path / "profile").absolute()
    profile.mkdir()
    backend = HermesCliBackend(
        executable.absolute(),
        profile,
        runner=lambda *_arguments: HermesCommandResult(0),
    )

    with pytest.raises(ValueError, match="not managed"):
        backend.enable_plugin("third-party")


def test_activation_rejects_control_backend_for_another_profile(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    other = (tmp_path / "other-profile").absolute()

    with pytest.raises(ValueError, match="another profile"):
        HermesActivationAdapter(layout, control=FakeControl(other))


def test_activation_layout_rejects_checkout_or_unprepared_profile(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    os.unlink(layout.hermes_home / "plugins/daemon_voice")
    control = FakeControl(layout.hermes_home)
    adapter = HermesActivationAdapter(layout, control=control)
    step = hermes_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()

    with pytest.raises(ValueError, match="prepared disposable profile"):
        adapter.snapshot(step, snapshot)


def test_readiness_uses_same_extended_roots_as_private_worker(tmp_path, monkeypatch):
    from daemon_core import after_turn_service
    from daemons_companion.runtime import bootstrap as host_bootstrap
    from daemons_companion.hermes.activation import _readiness_probe

    memory, spool = tmp_path / "memory", tmp_path / "spool"
    mapped = {memory: tmp_path / "extended-memory", spool: tmp_path / "extended-spool"}
    monkeypatch.setattr(host_bootstrap, "_source_io_path", mapped.__getitem__)

    def status(**kwargs):
        assert kwargs == {
            "expected_revision": "synthetic",
            "memory_root": mapped[memory],
            "spool_root": mapped[spool],
            "expected_profile": None,
        }
        return {"ok": True}

    monkeypatch.setattr(after_turn_service, "read_after_turn_service_status", status)
    assert _readiness_probe("synthetic", memory, spool)


def test_readiness_uses_elapsed_deadline_and_identifies_memory_wait(tmp_path):
    layout = _layout(tmp_path)
    step = hermes_activation_step(layout)
    control = FakeControl(layout.hermes_home)
    now = [0.0]

    def probe(*args):
        now[0] += 10
        return False

    adapter = HermesActivationAdapter(
        layout,
        control=control,
        readiness_probe=probe,
        readiness_attempts=480,
        readiness_timeout_seconds=15,
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    messages = []
    adapter.progress = messages.append
    snapshot = tmp_path / "activation"
    snapshot.mkdir()
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    with pytest.raises(RuntimeError, match="after-turn readiness"):
        adapter.verify(step)
    assert now[0] == 20
    assert messages == ["Telegram connected. Waiting for the memory worker..."]
