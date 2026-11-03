import base64
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from daemons_companion.runtime.services import BackgroundJob, HERMES_GATEWAY_JOB, scoped_job_code, render_job_definition
from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.setup.transaction import TransactionEngine, public_plan_digest, TransactionApplyError
from tests.test_companion_private_setup import owned


def capture(definition, running=True):
    return json.dumps({"schema": "daemon.windows-task-capture.v1", "running": running,
                       "definition": base64.b64encode(definition).decode()}).encode()


def fixture(tmp_path, monkeypatch):
    import daemons_companion.runtime.gateway_repair as module
    layout, scope = owned(tmp_path, monkeypatch, "windows")
    (layout.profile / "bin/uv.exe").unlink()
    (layout.profile / "bin").rmdir()
    monkeypatch.setattr(type(layout), "launch_python", property(lambda s: str(s.python) + "-extended"))
    job = BackgroundJob(scoped_job_code(HERMES_GATEWAY_JOB, scope.installation_id), layout.launch_python,
                        ("-B", "-c", "pass"), str(layout.attempt), str(scope.home / "gateway.log"), True)
    monkeypatch.setattr(module, "private_gateway_job", lambda *a, **k: replace(job, executable=str(layout.python)) if k.get("legacy") else job)
    old = render_job_definition(replace(job, executable=str(layout.python)), "windows")
    new = render_job_definition(job, "windows")

    class Scheduler:
        job_namespace = scope.installation_id
        current = capture(old)
        operations = []
        failure = None

        def capture(self, code):
            assert code == job.code
            return self.current

        def install(self, target, definition):
            assert target == job
            self.operations.append("install")
            self.current = capture(definition)
            if self.failure:
                raise self.failure

        def restore(self, code, saved):
            assert code == job.code
            self.operations.append("restore" if saved else "remove")
            self.current = saved

        def matches(self, target, definition):
            return self.current == capture(definition)

    def simple_repair(*args):
        scheduler = args[2]
        result = module._LaunchRepair(job, scheduler, old, new)
        def needed():
            if module._signature(scheduler.current) not in (result.old, result.new):
                raise RuntimeError("Private gateway differs; preserved.")
            return module._signature(scheduler.current) != result.new
        result.needs_repair = needed
        return result
    monkeypatch.setattr(module, "service_path_repair", simple_repair)
    runtime = SimpleNamespace(installation_scope=scope, companion_home=scope.home,
                              current_pointer=scope.home / "data/runtime/current.json")
    class Noop:
        def snapshot(self, *a): pass
        def apply(self, *a): pass
        def verify(self, *a): pass
        def rollback(self, *a): pass
    plan = SetupPlan(HostProbe(HostKind.HERMES, True, True), "", "", {}, (PlanStep("original", "Original", ()),))
    TransactionEngine(scope.home / "transactions").execute(plan, Noop(), transaction_id="b" * 32,
                                                          confirmed_plan_digest=public_plan_digest(plan))
    (scope.attempt / "daemon-setup.json").write_text(json.dumps({"schema":"daemon.private-setup.v1", "transaction_id":"b"*32}))
    return module, layout, scope, runtime, Scheduler(), old, new


def test_repair_existing_gateway_only_and_is_idempotent(tmp_path, monkeypatch):
    module, layout, scope, runtime, scheduler, old, new = fixture(tmp_path, monkeypatch)
    memory = scope.home / "user-memory.md"
    memory.write_text("synthetic user memory")
    assert module.repair_gateway_launch(layout, runtime, scheduler)
    assert scheduler.current == capture(new)
    assert scheduler.operations == ["remove", "install"]
    assert not module.repair_gateway_launch(layout, runtime, scheduler)
    assert scheduler.operations == ["remove", "install"]
    assert memory.read_text() == "synthetic user memory"
    assert (layout.profile / "bin/uv.exe").read_bytes() == layout.uv_executable.read_bytes()
    assert json.loads(next((scope.attempt / "gateway-repairs").glob("*/journal.json")).read_text())["status"] == "committed"


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_changed_task_is_never_overwritten(tmp_path, monkeypatch, missing):
    module, layout, scope, runtime, scheduler, old, new = fixture(tmp_path, monkeypatch)
    scheduler.current = None if missing else capture(old.replace(b"pass", b"else"))
    # UTF-16 XML: replace a real semantic field for the concurrent variant.
    if not missing:
        scheduler.current = capture(old.decode("utf-16").replace("pass", "changed").encode("utf-16"))
    before = scheduler.current
    with pytest.raises(RuntimeError, match="differs"):
        module.repair_gateway_launch(layout, runtime, scheduler)
    assert scheduler.current == before and not scheduler.operations


def test_failed_start_restores_previous_definition_and_running_state(tmp_path, monkeypatch):
    module, layout, scope, runtime, scheduler, old, new = fixture(tmp_path, monkeypatch)
    scheduler.current = capture(old, False)
    scheduler.failure = RuntimeError("synthetic start failure")
    with pytest.raises(TransactionApplyError):
        module.repair_gateway_launch(layout, runtime, scheduler)
    assert scheduler.current == capture(old, False)
    assert scheduler.operations == ["remove", "install", "restore"]
    assert not (layout.profile / "bin/uv.exe").exists()


def test_interrupted_repair_restores_before_retry(tmp_path, monkeypatch):
    module, layout, scope, runtime, scheduler, old, new = fixture(tmp_path, monkeypatch)
    scheduler.failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        module.repair_gateway_launch(layout, runtime, scheduler)
    scheduler.failure = None
    assert module.repair_gateway_launch(layout, runtime, scheduler)
    assert scheduler.operations == ["remove", "install", "restore", "remove", "install"]
    assert scheduler.current == capture(new)


def test_uncommitted_installation_never_repaired(tmp_path, monkeypatch):
    module, layout, scope, runtime, scheduler, old, new = fixture(tmp_path, monkeypatch)
    journal = scope.home / ("transactions/" + "b" * 32 + "/journal.json")
    data = json.loads(journal.read_text())
    data["status"] = "rolled_back"
    journal.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="committed"):
        module.repair_gateway_launch(layout, runtime, scheduler)
    assert not scheduler.operations


def test_different_managed_uv_is_preserved_without_stopping_gateway(tmp_path, monkeypatch):
    module, layout, scope, runtime, scheduler, old, new = fixture(tmp_path, monkeypatch)
    uv = layout.profile / "bin/uv.exe"
    uv.parent.mkdir()
    uv.write_bytes(b"concurrent native installer")
    with pytest.raises(TransactionApplyError):
        module.repair_gateway_launch(layout, runtime, scheduler)
    assert uv.read_bytes() == b"concurrent native installer"
    assert scheduler.current == capture(old) and not scheduler.operations


@pytest.mark.parametrize("extended", [False, True])
def test_gateway_uses_extended_source_after_validating_legacy_or_new_declaration(tmp_path, monkeypatch, extended):
    import daemons_companion.hermes.gateway as module
    import daemons_companion.runtime.bootstrap as bootstrap
    layout, scope = owned(tmp_path, monkeypatch, "windows")
    release = scope.home / "data/runtime/releases" / ("c" * 64)
    monkeypatch.setattr(module, "verify_current_release", lambda runtime: release)
    logical = release / "src"
    mapped = tmp_path / "synthetic-extended-source"
    monkeypatch.setattr(bootstrap, "_source_io_path", lambda path: mapped if path == logical else path)
    declared = mapped if extended else logical
    env = layout.profile / ".env"
    env.write_text("DAEMON_CORE_SRC=" + str(declared) + "\nTELEGRAM_BOT_TOKEN=synthetic\n")
    before = env.read_bytes()
    runtime = SimpleNamespace(installation_scope=scope, companion_home=scope.home, state_root=scope.home / "data/state")
    job = module.private_gateway_job(layout, runtime, scope.installation_id)
    assert repr(str(mapped)) in job.arguments[2]
    assert env.read_bytes() == before
    old = module.private_gateway_job(layout, runtime, scope.installation_id, legacy=True)
    assert repr(str(logical)) in old.arguments[2]
    env.write_text("DAEMON_CORE_SRC=" + str(tmp_path / "foreign-runtime"))
    with pytest.raises(ValueError, match="differs"):
        module.private_gateway_job(layout, runtime, scope.installation_id)


def test_generated_gateway_bootstrap_preserves_system_environment_before_filtering(tmp_path, monkeypatch):
    import os
    import sys
    from unittest.mock import patch
    import daemons_companion.hermes.gateway as module
    from tests.test_companion_private_setup import connection
    from daemon_core import scene_model

    layout, scope = owned(tmp_path, monkeypatch, 'windows')
    connection(scope, tmp_path)
    release = scope.home / 'data/runtime/releases' / ('c' * 64)
    monkeypatch.setattr(module, 'verify_current_release', lambda _: release)
    (scope.profile / '.env').write_text('DAEMON_CORE_SRC=' + str(release / 'src') + '\nTELEGRAM_BOT_TOKEN=synthetic-owned\n')
    runtime = SimpleNamespace(installation_scope=scope, companion_home=scope.home, state_root=scope.home / 'data/state')
    job = module.private_gateway_job(layout, runtime, scope.installation_id)
    captured = {}
    monkeypatch.setattr(module, 'run_gateway', lambda *args: captured.update(os.environ))
    monkeypatch.setattr(scene_model, 'SHADOW_LOG', tmp_path / 'previous-log')
    monkeypatch.setattr(sys, 'argv', ['-c', str(scope.attempt), str(layout.executable)])
    monkeypatch.setattr(sys, 'path', list(sys.path))
    with patch.dict(os.environ, {'SYSTEMROOT': 'C:\\Windows', 'PATH': 'system-path',
                                 'OPENAI_API_KEY': 'personal-secret'}, clear=True):
        exec(job.arguments[2], {})
    assert captured['SYSTEMROOT'] == 'C:\\Windows'
    assert captured['PATH'].endswith(os.pathsep + 'system-path')
    assert captured['TELEGRAM_BOT_TOKEN'] == 'synthetic-owned'
    assert 'OPENAI_API_KEY' not in captured


def test_private_windows_cli_control_uses_the_same_extended_interpreter(tmp_path, monkeypatch):
    import daemons_companion.hermes.gateway as module
    layout, scope = owned(tmp_path, monkeypatch, 'windows')
    control = object.__new__(module.PrivateHermesControl)
    control.host_layout, control.scope, control.runtime = layout, scope, object()
    release = scope.home / 'data/runtime/releases' / ('c' * 64)
    monkeypatch.setattr(module, 'verify_current_release', lambda _: release)
    monkeypatch.setattr(module, 'private_process_environment', lambda *a, **k: {})
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    control._native((str(layout.executable), 'plugins', 'list'), {})
    from daemons_companion.runtime.bootstrap import _source_io_path
    bridge = _source_io_path(release / 'integrations/hermes_plugin/companion_control.py')
    assert commands == [(layout.launch_python, '-B', str(bridge), 'plugins', 'list')]
