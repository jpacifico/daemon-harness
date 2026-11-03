"""Native service boundaries for one existing daemon and a new private host."""

import base64
from dataclasses import replace
import json
from types import SimpleNamespace
import pytest

import test_companion_host_attachment as attachment_fixture
from daemons_companion.hosts.attachment import inspect, apply, rollback
from daemons_companion.hosts import attachment_services as services
from daemons_companion.hosts.attachment_entry import load_record, host_environment


class Scheduler:
    target_os = "macos"
    job_namespace = "d" * 32

    def __init__(self):
        self.definitions = {}
        self.calls = []
        self.fail = None

    def capture(self, code):
        if code not in self.definitions:
            return None
        return json.dumps(
            {
                "schema": "daemon.launchd-capture.v1",
                "loaded": True,
                "definition": base64.b64encode(self.definitions[code]).decode(),
            }
        ).encode()

    def install(self, job, definition):
        self.definitions[job.code] = definition
        self.calls.append(("install", job.code))
        if self.fail and self.fail in job.code:
            raise RuntimeError("Injected registration failure")

    def matches(self, job, definition):
        return self.definitions.get(job.code) == definition

    def restore(self, code, before):
        assert before is None
        self.calls.append(("remove", code))
        self.definitions.pop(code, None)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    layout = attachment_fixture.layout.__wrapped__(tmp_path)
    entry = layout.profile.parent / "openclaw.mjs"
    entry.write_text("// synthetic native host")
    layout = replace(layout, executables=(*layout.executables, entry))
    target = layout.binding.source / "src/daemons_companion/hosts/attachment_entry.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# synthetic entry")
    config = json.loads((layout.profile / "openclaw.json").read_text())
    config["gateway"] = {"port": 18666}
    (layout.profile / "openclaw.json").write_text(json.dumps(config))
    (layout.profile.parent / "os-home").mkdir()
    host = SimpleNamespace(
        profile=layout.profile,
        node=layout.executables[0],
        entry=entry,
        target_os="macos",
        installation_id="d" * 32,
        attempt=layout.profile.parent,
    )
    monkeypatch.setattr(services, "verify_installed", lambda h: {"verified": True})
    monkeypatch.setattr(services, "port_closed", lambda p: True)
    scheduler = Scheduler()

    def controller(probe=None):
        return services.PrivateAttachmentServices(
            layout, host, scheduler, probe=probe or (lambda _: True), wait_seconds=0
        )

    return layout, scheduler, controller


def test_registration_and_restart_recovery_only_own_two_target_services(prepared):
    layout, scheduler, controller = prepared
    plan = inspect(layout)
    apply(layout, plan["revision"], controller())
    assert len(scheduler.definitions) == 2
    assert {code.rsplit(".", 1)[-1] for code in scheduler.definitions} == {
        "openclaw-api",
        "openclaw-gateway",
    }
    record, execution = load_record(layout.record)
    assert execution.profile == layout.profile and record["binding"]["spool"] == str(
        layout.binding.spool
    )
    rollback(layout, plan["revision"], controller())
    assert not scheduler.definitions
    assert json.loads(layout.record.read_text())["status"] == "rolled_back"


@pytest.mark.parametrize("failure", ["openclaw-api", "openclaw-gateway"])
def test_failure_after_native_write_restores_definitions_and_profile(prepared, failure):
    layout, scheduler, controller = prepared
    before = (layout.profile / "openclaw.json").read_bytes()
    scheduler.fail = failure
    plan = inspect(layout)
    with pytest.raises(RuntimeError, match="Injected"):
        apply(layout, plan["revision"], controller())
    assert not scheduler.definitions
    assert (layout.profile / "openclaw.json").read_bytes() == before


def test_missing_shared_maintenance_rolls_back_without_creating_worker(prepared):
    layout, scheduler, controller = prepared
    plan = inspect(layout)
    with pytest.raises(RuntimeError, match="maintenance"):
        apply(layout, plan["revision"], controller(lambda kind: kind != "maintenance"))
    assert not scheduler.definitions
    assert all(
        "worker" not in code and "dreaming" not in code for _, code in scheduler.calls
    )


def test_preexisting_and_concurrently_changed_jobs_are_preserved(prepared):
    layout, scheduler, controller = prepared
    c = controller()
    scheduler.definitions[c.jobs[0].code] = b"unrelated"
    plan = inspect(layout)
    with pytest.raises(RuntimeError, match="already"):
        apply(layout, plan["revision"], c)
    assert not layout.record.exists() and not scheduler.calls
    scheduler.definitions.clear()
    apply(layout, plan["revision"], controller())
    scheduler.definitions[c.jobs[0].code] = b"concurrent user edit"
    with pytest.raises(RuntimeError, match="changed"):
        rollback(layout, plan["revision"], controller())
    assert len(scheduler.definitions) == 2
    assert json.loads(layout.record.read_text())["status"] == "recovery_required"


def test_changed_config_or_closed_record_refuses_service_restart(prepared):
    layout, _, controller = prepared
    plan = inspect(layout)
    apply(layout, plan["revision"], controller())
    (layout.profile / ".env").write_text("CHANGED=1\n")
    with pytest.raises(ValueError, match="configuration changed"):
        load_record(layout.record)
    record = json.loads(layout.record.read_text())
    record["status"] = "rolling_back"
    layout.record.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="not approved"):
        load_record(layout.record)


def test_gateway_environment_does_not_import_primary_auth(prepared, monkeypatch):
    layout, _, _ = prepared
    for key in (
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "CODEX_HOME",
        "OPENCLAW_STATE_DIR",
        "NODE_OPTIONS",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    env = host_environment(layout.profile)
    assert "must-not-inherit" not in env.values()
    assert env["HOME"] == str(layout.profile.parent / "os-home")
    assert env["OPENCLAW_STATE_DIR"] == str(layout.profile)


@pytest.mark.parametrize(
    "payload,expected", [({}, False), ({"ok": False}, False), ({"ok": True}, True)]
)
def test_native_health_requires_explicit_success(prepared, monkeypatch, payload, expected):
    _, _, factory = prepared
    controller = factory()
    monkeypatch.setattr(services, "port_closed", lambda _port: False)
    controller.command = lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload)
    )
    assert controller._probe("gateway") is expected


def test_native_health_waits_for_the_listener_before_starting_the_cli(prepared, monkeypatch):
    _, _, factory = prepared
    controller = factory()
    monkeypatch.setattr(services, "port_closed", lambda _port: True)
    started = []
    controller.command = lambda *args, **kwargs: started.append(args)
    assert controller._probe("gateway") is False
    assert started == []


@pytest.mark.parametrize("failure", [None, "openclaw-api", "openclaw-gateway"])
def test_windows_definitions_and_rollback(prepared, monkeypatch, failure):
    layout, scheduler, _ = prepared
    scheduler.target_os = "windows"
    scheduler.fail = failure

    def capture(code):
        if code not in scheduler.definitions:
            return None
        return json.dumps(
            {
                "schema": "daemon.windows-task-capture.v1",
                "running": True,
                "definition": base64.b64encode(scheduler.definitions[code]).decode(),
            }
        ).encode()

    scheduler.capture = capture
    host = SimpleNamespace(
        profile=layout.profile,
        node=layout.executables[0],
        entry=layout.executables[1],
        target_os="windows",
        installation_id="d" * 32,
        attempt=layout.profile.parent,
    )
    monkeypatch.setattr(
        "daemons_companion.runtime.services._windows_scheduler_user_sid",
        lambda: "S-1-5-21-123",
    )
    c = services.PrivateAttachmentServices(
        layout, host, scheduler, probe=lambda _: True, wait_seconds=0
    )
    assert all(job.working_directory == str(host.attempt) for job in c.jobs)
    for definition in c.definitions:
        xml = definition.decode("utf-16")
        assert "LeastPrivilege" in xml and "InteractiveToken" in xml
        assert xml.count("S-1-5-21-123") == 2
    before = (layout.profile / "openclaw.json").read_bytes()
    plan = inspect(layout)
    if failure:
        with pytest.raises(RuntimeError, match="Injected"):
            apply(layout, plan["revision"], c)
    else:
        apply(layout, plan["revision"], c)
        assert len(scheduler.definitions) == 2
        rollback(layout, plan["revision"], c)
    assert not scheduler.definitions
    assert (layout.profile / "openclaw.json").read_bytes() == before
    assert json.loads(layout.record.read_text())["status"] == "rolled_back"


@pytest.mark.parametrize("failure", [False, True])
def test_windows_gateway_waits_and_closes_owned_children(
    tmp_path, monkeypatch, failure
):
    from daemons_companion.hosts import attachment_entry as entry
    from daemons_companion.runtime import windows_job
    from daemons_companion.runtime import bootstrap as host_bootstrap
    import subprocess

    calls = []

    class Job:
        def __enter__(self):
            calls.append("enter")
            return self

        def assign(self, child):
            calls.append("assign")
            if failure:
                raise RuntimeError("assignment refused")

        def __exit__(self, *args):
            calls.append("close")

    class Child:
        done = False

        def wait(self):
            calls.append("wait")
            self.done = True
            return 0

        def poll(self):
            return 0 if self.done else None

    child = Child()
    monkeypatch.setattr(entry.os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(windows_job, "OwnedProcessJob", Job)
    monkeypatch.setattr(
        host_bootstrap,
        "stop_process_tree",
        lambda *args: calls.append("stop"),
    )
    if failure:
        with pytest.raises(RuntimeError, match="assignment refused"):
            entry.run_gateway(["node.exe", "openclaw.mjs"], {}, tmp_path / "profile")
        assert calls == ["enter", "assign", "stop", "close"]
    else:
        entry.run_gateway(["node.exe", "openclaw.mjs"], {}, tmp_path / "profile")
        assert calls == ["enter", "assign", "wait", "close"]


@pytest.mark.parametrize(
    "missing,message",
    [
        ("gateway", "The associated host did not become ready."),
        ("maintenance", "The shared memory maintenance did not become ready."),
    ],
)
def test_activation_names_the_part_that_never_got_ready(prepared, missing, message):
    layout, scheduler, controller = prepared
    plan = inspect(layout)
    with pytest.raises(RuntimeError, match=message):
        apply(layout, plan["revision"], controller(lambda kind: kind != missing))
    assert not scheduler.definitions
    assert json.loads(layout.record.read_text())["status"] == "rolled_back"


def test_activation_waits_as_long_as_a_first_openclaw_installation():
    import inspect as source

    assert source.signature(services.PrivateAttachmentServices).parameters["wait_seconds"].default == 420
