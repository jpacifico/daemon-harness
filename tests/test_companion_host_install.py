from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from daemons_companion.runtime.acquisition import _RELEASES
from daemons_companion.runtime.host_install import (
    HostInstallAdapter, HostInstallLayout, execute_host_install, host_install_plan, installed_host,
)
from daemons_companion.desktop.host_terminal import PackagedHostSetup
from daemons_companion.setup.transaction import TransactionApplyError, TransactionEngine, public_plan_digest
from tests.test_companion_terminal import FakeUI


class Runner:
    def __init__(self, layout, failure=None):
        self.layout = layout
        self.failure = failure
        self.calls = []

    def __call__(self, command, environment, **kwargs):
        self.calls.append((command, dict(environment)))
        action = command[1:3]
        if self.failure == action:
            raise RuntimeError("synthetic failure")
        if action == ["--version"]:
            return "uv 0.12.16" if command[0] == str(self.layout.uv_executable) else "Hermes Agent v0.21.5 (2026.9.24)\nInstall directory: /synthetic\nPython: 3.12"
        if action == ["sync", "--project"]:
            directory = self.layout.executable.parent
            directory.mkdir(parents=True)
            for path in (self.layout.executable, self.layout.python):
                path.write_text("synthetic executable")
            if self.failure == "interrupt":
                raise KeyboardInterrupt
        return "ok"


def fixture(tmp_path, target_os="macos"):
    uv = tmp_path / "uv"
    uv.write_bytes(b"synthetic uv")
    layout = HostInstallLayout(tmp_path / "companion", uv, hashlib.sha256(uv.read_bytes()).hexdigest(), "a" * 32, target_os)
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        entry = tarfile.TarInfo("hermes-agent-" + _RELEASES["hermes"].revision + "/main.py")
        body = b"# original upstream source\n"
        entry.size = len(body)
        stream.addfile(entry, io.BytesIO(body))
    runner = Runner(layout)
    adapter = HostInstallAdapter(layout, runner=runner, downloader=lambda *_: archive)
    return layout, runner, adapter


def execute(layout, adapter):
    return execute_host_install(layout, adapter=adapter, confirmed_digest=public_plan_digest(host_install_plan(layout)))


def test_real_transaction_publishes_only_checked_host_without_services(tmp_path, monkeypatch):
    layout, runner, adapter = fixture(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("HERMES_HOME", "/personal/profile")
    assert installed_host(layout) is None
    assert execute(layout, adapter).status == "committed"
    managed = installed_host(layout, runner=runner)
    assert managed.version == "0.21.5"
    assert managed.executable == str(layout.executable)
    assert managed.config_path == str(layout.profile)
    assert all("must-not-inherit" not in env.values() for _, env in runner.calls)
    assert all(Path(env["HERMES_HOME"]).is_relative_to(layout.attempt) for _, env in runner.calls)
    assert all(Path(env["HOME"]).is_relative_to(layout.attempt) for _, env in runner.calls)
    assert all("gateway" not in cmd and "setup" not in cmd and "login" not in cmd for cmd, _ in runner.calls)
    assert not (layout.profile / "config.yaml").exists()
    assert not (layout.companion_home / "data/memory").exists()


def test_unconfirmed_plan_never_creates_installation_root(tmp_path):
    layout, runner, adapter = fixture(tmp_path)
    with pytest.raises(ValueError, match="reviewed plan"):
        execute_host_install(layout, adapter=adapter, confirmed_digest="0" * 64)
    assert not layout.companion_home.exists()
    assert runner.calls == []


@pytest.mark.parametrize("failure", [["python", "install"], ["sync", "--project"], ["pip", "check"], ["-B", "-c"], "interrupt"])
def test_failure_or_cancel_never_publishes_host_and_retains_inactive_files(tmp_path, failure):
    layout, runner, adapter = fixture(tmp_path)
    runner.failure = failure
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else TransactionApplyError):
        execute(layout, adapter)
    assert not layout.pointer.exists()
    assert layout.attempt.is_dir()
    assert TransactionEngine(layout.transactions).inspect(layout.installation_id).status == "rolled_back"


def test_preserves_an_existing_managed_host(tmp_path):
    layout, runner, adapter = fixture(tmp_path)
    execute(layout, adapter)
    pointer = layout.pointer.read_bytes()
    other = replace(layout, installation_id="b" * 32)
    second = HostInstallAdapter(other, runner=runner, downloader=lambda *_: pytest.fail("download"))
    with pytest.raises(TransactionApplyError):
        execute(other, second)
    assert layout.pointer.read_bytes() == pointer
    assert not other.attempt.exists()


def test_existing_destination_and_symlink_are_rejected(tmp_path):
    layout, runner, adapter = fixture(tmp_path)
    layout.attempt.mkdir(parents=True)
    (layout.attempt / "keep.txt").write_text("user file")
    with pytest.raises(TransactionApplyError):
        execute(layout, adapter)
    assert (layout.attempt / "keep.txt").read_text() == "user file"
    assert runner.calls == []
    alias = tmp_path / "alias"
    alias.symlink_to(layout.companion_home, target_is_directory=True)
    with pytest.raises(ValueError, match="direct"):
        host_install_plan(replace(layout, companion_home=alias))


def test_unsupported_os_and_damaged_uv_rejected_before_mutation(tmp_path):
    layout, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="macOS"):
        host_install_plan(replace(layout, target_os="linux"))
    layout.uv_executable.write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        host_install_plan(layout)
    assert not layout.companion_home.exists()


def test_failure_during_final_check_removes_only_own_pointer(tmp_path):
    layout, _, adapter = fixture(tmp_path)
    class FailingVerification(HostInstallAdapter):
        def verify(self, step):
            super().verify(step)
            raise RuntimeError("verification interrupted")
    failing = FailingVerification(layout, runner=adapter.runner, downloader=adapter.downloader)
    with pytest.raises(TransactionApplyError):
        execute(layout, failing)
    assert not layout.pointer.exists()
    assert layout.executable.is_file()


def test_terminal_confirmation_is_explicit_and_not_repeated_on_cancel(tmp_path):
    layout, _, _ = fixture(tmp_path)
    calls = []
    context = SimpleNamespace(companion_home=layout.companion_home, uv_executable=layout.uv_executable,
                              uv_sha256=layout.uv_sha256, target_os="macos", validate=lambda: None)
    setup = PackagedHostSetup(context, execute=lambda *a, **k: calls.append((a, k)))
    ui = FakeUI(choices=[2, 0])
    assert setup.offer(ui, "hermes", {"action": "install_qualified"}) is False
    assert calls == []
    assert not layout.companion_home.exists()


def test_terminal_calls_existing_transaction_and_reports_host_only(tmp_path):
    layout, _, _ = fixture(tmp_path)
    calls = []
    context = SimpleNamespace(companion_home=layout.companion_home, uv_executable=layout.uv_executable,
                              uv_sha256=layout.uv_sha256, target_os="macos", validate=lambda: None)
    def run(layout, **kwargs):
        assert kwargs["confirmed_digest"] == public_plan_digest(host_install_plan(layout))
        calls.append(layout)
        kwargs["progress"]("Installing private Python")
        return SimpleNamespace(status="committed")
    setup = PackagedHostSetup(context, execute=run)
    ui = FakeUI(choices=[1])
    assert setup.offer(ui, "hermes", {"action": "install_qualified"})
    assert len(calls) == 1 and setup.changed
    assert any("Model access, the daemon and Telegram still need setup" in line for line in ui.output)
    assert not setup.offer(ui, "openclaw", {"action": "install_qualified"})
    assert len(calls) == 1


def test_invalid_host_pointer_never_selects_outside_installation(tmp_path):
    layout, _, _ = fixture(tmp_path)
    layout.root.mkdir(parents=True)
    layout.pointer.write_text(json.dumps({"schema": "daemon.companion-host-install.v1",
        "installation_id": "../outside", "version": "0.21.5", "revision": _RELEASES["hermes"].revision}))
    with pytest.raises(ValueError, match="id"):
        installed_host(layout)


def test_installed_host_rechecks_sources_instead_of_trusting_pointer(tmp_path):
    layout, runner, adapter = fixture(tmp_path)
    execute(layout, adapter)
    (layout.host / "main.py").write_text("altered upstream source")
    with pytest.raises(RuntimeError, match="sources"):
        installed_host(layout, runner=runner)


def test_windows_install_uses_native_executables_and_private_user_directories(tmp_path):
    layout, runner, adapter = fixture(tmp_path, target_os="windows")
    assert execute(layout, adapter).status == "committed"
    assert installed_host(layout, runner=runner).executable.endswith("Scripts/hermes.exe")
    assert layout.python.name == "python.exe"
    assert all(env["USERPROFILE"] == env["HOME"] for _, env in runner.calls)
    assert all(Path(env["APPDATA"]).is_relative_to(layout.attempt) for _, env in runner.calls)


def test_windows_interruption_targets_only_child_tree(monkeypatch):
    from daemons_companion.runtime.bootstrap import stop_process_tree
    from types import SimpleNamespace
    import subprocess
    calls = []
    child = SimpleNamespace(pid=4242, poll=lambda: None,
                            communicate=lambda **kwargs: calls.append(("drained", kwargs)))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(returncode=0)))
    stop_process_tree(child, {"SYSTEMROOT": "C:/Windows"}, windows=True)
    command = calls[0][0]
    assert command[1:] == ["/PID", "4242", "/T", "/F"]
    assert command[0].endswith("System32/taskkill.exe")
    assert calls[-1][0] == "drained"


def test_windows_failed_process_cleanup_is_not_reported_as_success(monkeypatch):
    from daemons_companion.runtime.bootstrap import stop_process_tree
    from types import SimpleNamespace
    import subprocess
    child = SimpleNamespace(pid=4242, poll=lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=5))
    with pytest.raises(RuntimeError, match="could not be stopped"):
        stop_process_tree(child, {"SYSTEMROOT": "C:/Windows"}, windows=True)


def test_host_failure_displays_cause_after_transaction_rollback(tmp_path):
    from daemons_companion.runtime.bootstrap import HostCommandError
    layout, _, _ = fixture(tmp_path)
    context = SimpleNamespace(companion_home=layout.companion_home, uv_executable=layout.uv_executable,
                              uv_sha256=layout.uv_sha256, target_os="windows", validate=lambda: None)
    def fail(*args, **kwargs):
        try:
            raise HostCommandError(2, "synthetic dependency could not install\x1b[31m")
        except HostCommandError as cause:
            raise TransactionApplyError("rolled back") from cause
    setup = PackagedHostSetup(context, execute=fail)
    ui = FakeUI(choices=[1])
    with pytest.raises(TransactionApplyError):
        setup.offer(ui, "hermes", {"action": "install_qualified"})
    assert any("exit 2" in line for line in ui.output)
    assert any("synthetic dependency could not install" in line for line in ui.output)
    assert not any("\x1b" in line for line in ui.output)
    assert not setup.changed


def test_command_diagnostic_is_bounded_and_not_in_public_summary():
    import sys
    from daemons_companion.runtime.bootstrap import run, HostCommandError
    with pytest.raises(HostCommandError) as caught:
        run([sys.executable, "-c", "import sys;sys.stderr.write('x'*9000+'failure detail');sys.exit(7)"], {})
    assert len(caught.value.diagnostic) == 8000
    assert caught.value.diagnostic.endswith("failure detail")
    assert "failure detail" not in str(caught.value)


def test_timeout_retains_partial_output_and_stops_process(tmp_path):
    import sys
    import time
    from daemons_companion.runtime.bootstrap import run, HostCommandTimeout
    marker = tmp_path / "must-not-exist"
    progress = []
    command = [sys.executable, "-c", "import sys,time,pathlib;print('download started',flush=True);print('partial error',file=sys.stderr,flush=True);time.sleep(1);pathlib.Path(sys.argv[1]).touch()", str(marker)]
    start = time.monotonic()
    with pytest.raises(HostCommandTimeout) as caught:
        run(command, {}, timeout=.4, progress=progress.append)
    assert time.monotonic() - start < 2
    assert "download started" in caught.value.diagnostic
    assert "partial error" in caught.value.diagnostic
    assert caught.value.timeout_seconds == .4
    assert str(marker) not in str(caught.value)
    assert progress
    time.sleep(.8)
    assert not marker.exists()


def test_dependency_command_cannot_wait_for_hidden_input():
    import sys
    from daemons_companion.runtime.bootstrap import run
    assert run([sys.executable, "-c", "import sys;print(repr(sys.stdin.read()))"], {}, timeout=2) == "''"


def test_activity_cancellation_stops_child(tmp_path):
    import sys
    import time
    from daemons_companion.runtime.bootstrap import run
    marker = tmp_path / "must-not-exist"
    def cancel(_):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run([sys.executable, "-c", "import time,pathlib,sys;print('start',flush=True);time.sleep(.8);pathlib.Path(sys.argv[1]).touch()", str(marker)], {}, progress=cancel)
    time.sleep(.9)
    assert not marker.exists()


def test_hermes_dependency_budget_and_activity_are_explicit(tmp_path):
    layout, runner, adapter = fixture(tmp_path)
    calls = []
    def record(command, environment, **kwargs):
        calls.append((command, kwargs))
        return runner(command, environment, **kwargs)
    adapter.runner = record
    execute(layout, adapter)
    command, sync = next((command, kwargs) for command, kwargs in calls if "sync" in command)
    assert sync["timeout"] == 1800
    assert callable(sync["progress"])
    # Hermes would otherwise download bedrock, edge-tts and the Anthropic SDK,
    # unlocked, during the gateway start and the first Telegram reply (14 s).
    extras = [command[index + 1] for index, arg in enumerate(command) if arg == "--extra"]
    assert extras == ["messaging", "bedrock", "edge-tts", "anthropic"] and "--frozen" in command
    check = next(command for command, _ in calls if "-c" in command)
    assert "boto3" in check[-1] and "edge_tts" in check[-1] and "anthropic" in check[-1]


def test_timeout_cleanup_failure_never_claims_child_tree_stopped(monkeypatch):
    from daemons_companion.runtime import bootstrap
    class Child:
        returncode = None
        def poll(self):
            return self.returncode
    child = Child()
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **kw: child)
    def failed_stop(*args):
        child.returncode = 1
        raise RuntimeError("Tree cleanup failed after parent exit")
    monkeypatch.setattr(bootstrap, "stop_process_tree", failed_stop)
    with pytest.raises(RuntimeError, match="Tree cleanup failed") as failure:
        bootstrap.run(["synthetic"], {}, timeout=0)
    assert not getattr(failure.value, "private_command_stopped", False)
