from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemons_companion.setup.plan import HostKind, PlanStep
from daemons_companion.openclaw.activation import (
    OpenClawActivationAdapter,
    OpenClawActivationLayout,
    OpenClawCliBackend,
    OpenClawCommandResult,
    OpenClawControlState,
    openclaw_activation_step,
)
from daemons_companion.openclaw.profile import (
    OpenClawProfileAdapter,
    OpenClawProfileLayout,
    create_disposable_openclaw_marker,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)


class FakeControl:
    def __init__(
        self,
        openclaw_state: Path,
        *,
        gateway: str = "stopped",
        gateway_ready: bool = True,
        plugin_ready: bool = True,
    ) -> None:
        self.openclaw_state = openclaw_state
        self.gateway = gateway
        self._gateway_ready = gateway_ready
        self._plugin_ready = plugin_ready
        self.calls: list[str] = []

    def state(self) -> OpenClawControlState:
        return OpenClawControlState(self.gateway)

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
        return self.gateway == "running" and self._gateway_ready

    def plugin_ready(self) -> bool:
        return self._plugin_ready


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "example==1 \\\n    --hash=sha256:" + "0" * 64 + "\n",
        "requirements.txt": "example==1\n",
        "src/daemon_core/__init__.py": "# synthetic\n",
        "integrations/hermes_plugin/__init__.py": "# memory\n",
        "integrations/hermes_voice_plugin/__init__.py": "# voice\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/openclaw_plugin/workspace/AGENTS.md": "# Canonical\n",
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


def _layout(tmp_path: Path) -> OpenClawActivationLayout:
    companion = (tmp_path / "companion").absolute()
    state = (tmp_path / "openclaw").absolute()
    create_disposable_companion_marker(companion)
    create_disposable_openclaw_marker(state)
    runtime = CompanionRuntimeLayout(companion, _bundle(tmp_path / "bundle"))
    runtime_snapshot = tmp_path / "runtime-snapshot"
    runtime_snapshot.mkdir()
    runtime_adapter = CompanionRuntimeAdapter(runtime)
    runtime_step = PlanStep("runtime-layout", "runtime", ())
    runtime_adapter.snapshot(runtime_step, runtime_snapshot)
    runtime_adapter.apply(runtime_step)
    runtime_adapter.verify(runtime_step)
    profile = OpenClawProfileLayout(runtime, state)
    profile_snapshot = tmp_path / "profile-snapshot"
    profile_snapshot.mkdir()
    profile_adapter = OpenClawProfileAdapter(
        profile,
        selected_host=HostKind.OPENCLAW,
        user_id="synthetic-user",
        daemon_url="http://127.0.0.1:18000",
    )
    profile_step = PlanStep("openclaw-profile", "profile", ())
    profile_adapter.snapshot(profile_step, profile_snapshot)
    profile_adapter.apply(profile_step)
    profile_adapter.verify(profile_step)
    return OpenClawActivationLayout(runtime, state, 18000, 18790)


def test_activation_starts_and_proves_all_readiness_signals(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(layout.openclaw_state)
    adapter = OpenClawActivationAdapter(
        layout,
        control=control,
        api_probe=lambda port: port == 18000,
        readiness_probe=lambda *_arguments: True,
    )
    step = openclaw_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()

    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    adapter.verify(step)

    assert control.state() == OpenClawControlState("running")
    assert control.calls == ["start"]


def test_activation_rolls_back_then_restores_a_previous_gateway(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(layout.openclaw_state, gateway="running")
    adapter = OpenClawActivationAdapter(
        layout,
        control=control,
        api_probe=lambda _port: True,
        readiness_probe=lambda *_arguments: True,
    )
    step = openclaw_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()

    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    adapter.rollback(step, snapshot)
    assert control.state() == OpenClawControlState("stopped")
    adapter.restore_gateway_after_rollback(snapshot)
    assert control.state() == OpenClawControlState("running")
    assert control.calls == ["restart", "stop", "start"]


@pytest.mark.parametrize(
    ("gateway", "plugin", "api", "worker", "missing"),
    [
        (False, True, True, True, "gateway"),
        (True, False, True, True, "plugin"),
        (True, True, False, True, "API"),
        (True, True, True, False, "after-turn"),
    ],
)
def test_activation_reports_each_missing_readiness_signal(
    tmp_path: Path,
    gateway: bool,
    plugin: bool,
    api: bool,
    worker: bool,
    missing: str,
) -> None:
    layout = _layout(tmp_path)
    control = FakeControl(
        layout.openclaw_state,
        gateway_ready=gateway,
        plugin_ready=plugin,
    )
    adapter = OpenClawActivationAdapter(
        layout,
        control=control,
        api_probe=lambda _port: api,
        readiness_probe=lambda *_arguments: worker,
        readiness_attempts=1,
    )
    step = openclaw_activation_step(layout)
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()
    adapter.snapshot(step, snapshot)
    adapter.apply(step)

    control.readiness_detail = lambda: "gateway health (exit 1): connect ECONNREFUSED"
    with pytest.raises(RuntimeError, match=missing) as failure:
        adapter.verify(step)
    # The diagnostic keeps the probe answers that explain the failure.
    assert failure.value.diagnostic == "gateway health (exit 1): connect ECONNREFUSED"


def test_cli_backend_uses_only_explicit_profile_and_drops_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = (tmp_path / "openclaw").absolute()
    executable.write_text("synthetic", encoding="utf-8")
    state = (tmp_path / "openclaw-state").absolute()
    create_disposable_openclaw_marker(state)
    (state / "openclaw.json").write_text("{}\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(command, environment):
        calls.append((tuple(command), dict(environment)))
        if "plugins" in command:
            return OpenClawCommandResult(
                0,
                json.dumps(
                    {"plugin": {"id": "daemon-harness", "enabled": True, "status": "loaded"}}
                ),
            )
        return OpenClawCommandResult(0)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    backend = OpenClawCliBackend(executable, state, 18790, runner=runner)

    assert backend.state() == OpenClawControlState("running")
    assert backend.plugin_ready()
    backend.restart_gateway()
    assert all(
        environment["OPENCLAW_STATE_DIR"] == str(state)
        and environment["OPENCLAW_CONFIG_PATH"] == str(state / "openclaw.json")
        and "OPENAI_API_KEY" not in environment
        for _command, environment in calls
    )


def test_activation_rejects_unprepared_profile_and_port_collision(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError, match="must differ"):
        openclaw_activation_step(
            OpenClawActivationLayout(
                layout.runtime,
                layout.openclaw_state,
                18000,
                18000,
            )
        )
    (layout.openclaw_state / "workspace-daemon-harness/AGENTS.md").unlink()
    adapter = OpenClawActivationAdapter(
        layout,
        control=FakeControl(layout.openclaw_state),
    )
    snapshot = tmp_path / "activation-snapshot"
    snapshot.mkdir()
    with pytest.raises(ValueError, match="prepared disposable profile"):
        adapter.snapshot(openclaw_activation_step(layout), snapshot)


def test_private_gateway_probes_keep_their_answers_for_the_diagnostic(monkeypatch) -> None:
    import subprocess
    from types import SimpleNamespace
    from daemons_companion.openclaw import gateway

    control = object.__new__(gateway.PrivateOpenClawControl)
    control.host = SimpleNamespace(node=Path("/node"), entry=Path("/entry"), attempt=Path("/attempt"))
    control.runtime = SimpleNamespace(installation_scope=None)
    control.port = 18789
    control.probes = {}
    control._listening = lambda: True
    monkeypatch.setattr(gateway, "host_environment", lambda _scope: {})
    answers = iter([
        subprocess.CompletedProcess([], 1, "", "Gateway not reachable: connect ECONNREFUSED"),
        subprocess.CompletedProcess([], 0, '{"plugin":{"id":"daemon-harness","status":"error"}}', ""),
    ])
    monkeypatch.setattr(gateway.subprocess, "run", lambda *_a, **_k: next(answers))
    assert control.gateway_ready() is False
    assert control.plugin_ready() is False
    detail = control.readiness_detail()
    assert "gateway health (exit 1): Gateway not reachable: connect ECONNREFUSED" in detail
    assert 'plugins info (exit 0): {"plugin":{"id":"daemon-harness","status":"error"}}' in detail


def _private_control(port):
    from types import SimpleNamespace
    from daemons_companion.openclaw import gateway

    control = object.__new__(gateway.PrivateOpenClawControl)
    control.host = SimpleNamespace(node=Path("/node"), entry=Path("/entry"), attempt=Path("/attempt"), profile=Path("/profile"))
    control.runtime = SimpleNamespace(installation_scope=None)
    control.port = port
    control.probes = {}
    return control


def test_private_gateway_probes_wait_for_the_listener_before_starting_the_cli(monkeypatch) -> None:
    # On Windows one OpenClaw CLI probe takes 10-23 s to start; while the
    # gateway still boots, spending that per attempt only slows its boot.
    import socket
    from daemons_companion.openclaw import gateway

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    control = _private_control(port)
    monkeypatch.setattr(gateway, "LISTEN_WAIT_SECONDS", 0)
    started = []
    monkeypatch.setattr(gateway.subprocess, "run", lambda *a, **k: started.append(a))
    assert control.gateway_ready() is False
    assert control.plugin_ready() is False
    assert started == []
    assert f"gateway health: gateway port {port} not listening yet" in control.readiness_detail()
    assert f"plugins info: gateway port {port} not listening yet" in control.readiness_detail()


def test_private_gateway_probe_reads_utf8_with_the_measured_timeout(monkeypatch) -> None:
    import socket
    import subprocess
    from daemons_companion.openclaw import gateway

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        control = _private_control(listener.getsockname()[1])
        monkeypatch.setattr(gateway, "host_environment", lambda _scope: {})
        calls = []

        def run(command, **options):
            calls.append(options)
            return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

        monkeypatch.setattr(gateway.subprocess, "run", run)
        monkeypatch.setattr(gateway, "checked_file", lambda *_a: b"{}")
        assert control.gateway_ready() is True
    # Windows decodes with the ANSI code page otherwise; the CLI writes UTF-8.
    assert calls[0]["encoding"] == "utf-8" and calls[0]["errors"] == "replace"
    # Measured on the field PC: health 21.0 s, plugin 67-86 s.
    assert calls[0]["timeout"] == gateway.HEALTH_TIMEOUT_SECONDS > 21
    control._listening = lambda: True
    monkeypatch.setattr(gateway.subprocess, "run", lambda command, **options: (calls.append(options), subprocess.CompletedProcess(
        command, 0, '{"plugin":{"id":"daemon-harness","status":"loaded"}}', ""))[1])
    assert control.plugin_ready() is True
    assert calls[1]["timeout"] == gateway.PLUGIN_TIMEOUT_SECONDS > 86
    # A loaded plugin is not probed again.
    assert control.plugin_ready() is True and len(calls) == 2
