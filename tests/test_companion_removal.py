from __future__ import annotations
import json
from pathlib import Path
import plistlib
import shutil

import pytest
import yaml

from daemons_companion.removal.removal import Refused, PROGRAMS


# The same synthetic setup also exercises the packaged Electron/Python boundary.
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "removal_fixture",
    Path(__file__).resolve().parents[1] / "apps/companion/desktop/removal-fixture.py",
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
fixture, tree, apply = _fixture.fixture, _fixture.tree, _fixture.apply


@pytest.mark.parametrize("action", ["detach", "uninstall"])
def test_retains_exact_memory_profile_accounts_and_sibling(tmp_path, action):
    engine, memory, _ = fixture(tmp_path)
    sibling = tmp_path / "other-installation"
    sibling.mkdir()
    (sibling / "profile").write_text("other")
    before = tree(memory), tree(sibling)
    result = apply(engine, action)
    assert result["done"] and result["memory"] == str(memory)
    assert (tree(memory), tree(sibling)) == before
    config = yaml.safe_load((engine.profile / "config.yaml").read_text())
    assert config["model"] == "native" and config["custom"] == 42
    assert config["plugins"]["enabled"] == ["unrelated"]
    assert "# Keep this comment" in (engine.profile / "config.yaml").read_text()
    assert (engine.profile / "state.db").read_bytes() == b"synthetic history"
    assert (
        engine.profile / ".env"
    ).read_text() == "TELEGRAM_BOT_TOKEN='synthetic-never-return'\n"
    assert (engine.attempt / "os-home/retained-or-program").exists()
    assert "synthetic-never-return" not in json.dumps(result)
    assert all(
        not (engine.profile / "plugins" / p).exists()
        for p in ("daemon_core", "daemon_voice")
    )
    assert all(engine.scheduler.capture(c) is None for c in engine.codes[1:])
    if action == "detach":
        assert (
            engine.scheduler.capture(engine.codes[0])
            and (engine.attempt / "source").exists()
        )
    else:
        assert not any(engine.scheduler.capture(c) for c in engine.codes)
        assert all(not (engine.attempt / p).exists() for p in PROGRAMS)


def test_detach_then_uninstall_keeps_same_memory(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    before = tree(memory)
    apply(engine, "detach")
    assert apply(engine, "uninstall")["done"]
    assert tree(memory) == before


def test_exact_name_and_stale_review_make_no_mutation(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    plan = engine.inspect("detach")
    before = tree(engine.attempt)
    with pytest.raises(Refused, match="changed"):
        engine.apply("detach", plan["revision"], "nova")
    assert tree(engine.attempt) == before
    with (engine.profile / "config.yaml").open("a") as out:
        out.write("new_setting: true\n")
    with pytest.raises(Refused, match="changed"):
        engine.apply("detach", plan["revision"], "Nova")
    assert not engine.marker.exists()


def test_shared_service_refuses_without_mutation(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    before = tree(engine.attempt)
    (engine.launch_agents / "foreign.plist").write_bytes(
        plistlib.dumps(
            {"Label": "foreign", "ProgramArguments": ["/bin/echo", str(memory)]}
        )
    )
    with pytest.raises(Refused, match="shared"):
        engine.inspect("uninstall")
    assert tree(engine.attempt) == before


@pytest.mark.parametrize("what", ["program", "memory", "plugin", "owner"])
def test_redirected_paths_are_refused(tmp_path, what):
    engine, memory, _ = fixture(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("keep")
    if what == "program":
        shutil.rmtree(engine.attempt / "source")
        (engine.attempt / "source").symlink_to(foreign)
    elif what == "memory":
        shutil.rmtree(memory)
        memory.symlink_to(foreign)
    elif what == "plugin":
        p = engine.profile / "plugins/daemon_core"
        p.unlink()
        p.symlink_to(foreign)
    else:
        (engine.attempt / "owner.json").unlink()
        (engine.attempt / "owner.json").symlink_to(foreign / "keep")
    with pytest.raises((ValueError, OSError)):
        engine.inspect("uninstall")
    assert (foreign / "keep").read_text() == "keep"


@pytest.mark.parametrize(
    "phase", ["config.yaml", ".env", "daemon_core", "gateway-restored"]
)
def test_failure_rolls_back_exact_profile_and_services(tmp_path, phase):
    engine, memory, _ = fixture(tmp_path)
    before = tree(engine.profile), tree(memory), dict(engine.scheduler.items)

    def fail(step):
        if step == phase:
            raise RuntimeError("synthetic failure")

    engine.hook = fail
    with pytest.raises(Refused, match="rolled_back"):
        apply(engine, "detach")
    assert (tree(engine.profile), tree(memory), engine.scheduler.items) == before
    assert engine.status()["status"] == "rolled_back"
    engine.hook = lambda _: None
    assert apply(engine, "detach")["done"]


def test_cleanup_failure_can_resume_without_touching_vault(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    before = tree(memory)

    def fail(step):
        if step == "program:source":
            raise RuntimeError("synthetic disk failure")

    engine.hook = fail
    with pytest.raises(Refused, match="recovery"):
        apply(engine, "uninstall")
    assert engine.status()["status"] == "cleaning"
    assert not (engine.attempt / "source").exists()
    assert tree(memory) == before
    engine.hook = lambda _: None
    assert engine.inspect("uninstall")["recovery"]
    assert apply(engine, "uninstall")["done"]
    assert tree(memory) == before


def test_changed_unowned_fields_are_retained(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    p = engine.profile / "config.yaml"
    p.write_text(
        p.read_text()
        .replace("custom: 42", "custom: 43")
        .replace('tool_progress: "all"', 'tool_progress: "none"')
    )
    p = engine.profile / ".env"
    p.write_text(p.read_text() + "NEW_HOST_KEY=keep\n")
    apply(engine, "detach")
    assert yaml.safe_load((engine.profile / "config.yaml").read_text())["custom"] == 43
    assert (
        yaml.safe_load((engine.profile / "config.yaml").read_text())["display"][
            "platforms"
        ]["telegram"]["tool_progress"]
        == "none"
    )
    assert "NEW_HOST_KEY=keep" in (engine.profile / ".env").read_text()


def test_changed_memory_pointer_and_followups_refuse_detach(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    outside = tmp_path / "external-memory"
    outside.mkdir()
    (outside / "keep.md").write_text("keep")
    p = engine.profile / ".env"
    original = p.read_text()
    p.write_text(original.replace(str(memory / ".daemon"), str(outside)))
    with pytest.raises(Refused, match="configuration"):
        engine.inspect("uninstall")
    assert (outside / "keep.md").read_text() == "keep"
    p.write_text(original)
    (engine.profile / "cron").mkdir()
    (engine.profile / "cron/jobs.json").write_text('[{"name":"followup_synthetic"}]')
    with pytest.raises(Refused, match="followups"):
        engine.inspect("detach")


def test_tampered_recovery_record_and_program_replacement_are_refused(tmp_path):
    engine, memory, _ = fixture(tmp_path)

    def fail(step):
        if step == "program:source":
            raise RuntimeError("disk")

    engine.hook = fail
    with pytest.raises(Refused):
        apply(engine, "uninstall")
    data = json.loads(engine.marker.read_text())
    data["programs"].append("../../escape")
    engine.marker.write_text(json.dumps(data))
    with pytest.raises(Refused):
        engine.inspect("uninstall")
    assert memory.is_dir()


def test_failed_uninstall_after_detach_remains_retryable(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    apply(engine, "detach")

    def fail(step):
        if step.startswith("service:"):
            raise RuntimeError("synthetic service failure")

    engine.hook = fail
    with pytest.raises(Refused, match="rolled_back"):
        apply(engine, "uninstall")
    assert engine.status()["detached_before"] is True
    engine.hook = lambda _: None
    assert apply(engine, "uninstall")["done"]
    assert (memory / "Projects/synthetic.md").exists()


def test_recovery_keeps_a_concurrent_edit_and_does_not_restore_over_it(tmp_path):
    engine, memory, _ = fixture(tmp_path)

    def conflict(step):
        if step == "config.yaml":
            (engine.profile / "config.yaml").write_text("custom: preserve-new-edit\n")
            raise RuntimeError("concurrent edit")

    engine.hook = conflict
    with pytest.raises(Refused, match="recovery"):
        apply(engine, "detach")
    assert (engine.profile / "config.yaml").read_text() == "custom: preserve-new-edit\n"
    with pytest.raises(Refused, match="recovery"):
        apply(engine, "detach")
    assert memory.is_dir()


def test_verified_private_upgrade_is_removable_but_altered_release_is_not(tmp_path):
    from tests.test_companion_runtime import _bundle
    from daemons_companion.setup.plan import PlanStep
    from daemons_companion.runtime.release import (
        CompanionRuntimeLayout,
        CompanionRuntimeAdapter,
        resolve_current_release,
        RUNTIME_LAYOUT_STEP,
    )

    engine, memory, _ = fixture(tmp_path)
    link = engine.profile / "plugins/daemon_core"
    old = link.resolve().parents[1]
    bundle = _bundle(tmp_path / "upgrade-bundle")
    layout = CompanionRuntimeLayout(engine.scope.home, bundle, engine.scope, True)
    adapter = CompanionRuntimeAdapter(layout)
    snapshot = tmp_path / "upgrade-snapshot"
    snapshot.mkdir()
    step = PlanStep(RUNTIME_LAYOUT_STEP, "upgrade synthetic", ())
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    current = resolve_current_release(layout)
    env = engine.profile / ".env"
    env.write_text(env.read_text().replace(str(old), str(current)))
    for plugin, relative in [
        ("daemon_core", "hermes_plugin"),
        ("daemon_voice", "hermes_voice_plugin"),
    ]:
        p = engine.profile / "plugins" / plugin
        p.unlink()
        p.symlink_to(current / "integrations" / relative)
    review = engine.inspect("detach")
    code = current / "src/daemon_core/__init__.py"
    original = code.read_bytes()
    code.write_bytes(b"# changed after release\n")
    with pytest.raises(Refused, match="configuration"):
        engine.inspect("detach")
    code.write_bytes(original)
    assert engine.apply("detach", review["revision"], review["name"])["done"]
    assert (memory / "Projects/synthetic.md").exists()


@pytest.mark.parametrize("action", ["detach", "uninstall"])
def test_windows_plugin_copies_restore_and_remove_without_symlinks(tmp_path, action):
    engine, memory, _ = fixture(tmp_path, plugin_copies=True)
    exact = tree(engine.profile), tree(memory)

    def fail(step):
        if step == "daemon_voice":
            raise RuntimeError("copy rollback")

    engine.hook = fail
    with pytest.raises(Refused, match="rolled_back"):
        apply(engine, action)
    assert (tree(engine.profile), tree(memory)) == exact
    engine.hook = lambda _: None
    assert apply(engine, action)["done"]
    assert tree(memory) == exact[1]
    assert not (engine.profile / "plugins/daemon_core").exists()
    if action == "detach":
        assert apply(engine, "uninstall")["done"]
        assert not engine.copy_backup("daemon_core").exists()


def test_plugin_copies_allow_only_derived_cache_additions(tmp_path):
    engine, memory, _ = fixture(tmp_path, plugin_copies=True)
    plugin = engine.profile / "plugins/daemon_core"
    cache = plugin / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"derived")
    engine.inspect("detach")
    (plugin / "unexpected.py").write_bytes(b"changed")
    with pytest.raises(Refused, match="configuration"):
        engine.inspect("detach")
    (plugin / "unexpected.py").unlink()
    (plugin / "__init__.py").write_bytes(b"changed")
    with pytest.raises(Refused, match="configuration"):
        engine.inspect("detach")


def test_windows_task_definition_requires_private_command_and_current_identity(
    tmp_path,
):
    from daemons_companion.removal.windows import validate_task
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    command = tmp_path / "worker.exe"
    command.write_bytes(b"fixture")
    xml = f"""<Task xmlns="{ns[1:-1]}"><Principals><Principal><UserId>S-1-5-21-123</UserId>
<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Actions><Exec><Command>{command}</Command><WorkingDirectory>{tmp_path}</WorkingDirectory></Exec></Actions></Task>"""
    validate_task(xml.encode(), tmp_path, "S-1-5-21-123")
    for key, value in (
        ("UserId", "S-1-5-21-999"),
        ("RunLevel", "HighestAvailable"),
        ("LogonType", "Password"),
        ("Command", "/outside/worker"),
    ):
        root = ET.fromstring(xml)
        root.find(".//" + ns + key).text = value
        with pytest.raises(Refused, match="services"):
            validate_task(ET.tostring(root), tmp_path, "S-1-5-21-123")


def test_windows_scheduler_arguments_do_not_become_powershell_code(tmp_path):
    import base64
    from daemons_companion.runtime.services import SchedulerCommandResult
    from daemons_companion.removal.windows import WindowsRemovalScheduler

    calls = []

    def runner(command, environment):
        calls.append(command)
        return SchedulerCommandResult(0, "", "")

    scheduler = WindowsRemovalScheduler(runner=runner, job_namespace="a" * 32)
    attempt = tmp_path / "space & accented é"
    scheduler.require_exclusive(attempt, attempt / "memory", ["safe.one", "safe.two"])
    payload = json.loads(base64.b64decode(calls[-1][-1]))
    assert payload["attempt"] == str(attempt)
    assert payload["codes"] == ["safe.one", "safe.two"]
    assert str(attempt) not in calls[-1][-2]
    scheduler.require_stopped(attempt)
    assert base64.b64decode(calls[-1][-1]).decode() == str(attempt)


def windows_engine(tmp_path, monkeypatch):
    """Exercise the Windows controller with injected OS boundaries on this host."""
    import base64
    from daemons_companion.runtime import services
    from daemon_core import platform_support
    from daemons_companion.removal import windows as windows_removal

    sid = "S-1-5-21-123"
    monkeypatch.setattr(platform_support, "_user_sid", lambda: sid)
    monkeypatch.setattr(services, "_windows_scheduler_user_sid", lambda: sid)
    engine, memory, jobs = fixture(tmp_path, plugin_copies=True)
    scheduler = engine.scheduler
    scheduler.target_os = "windows"
    scheduler.require_exclusive = lambda *args: None

    def stopped(root):
        # Removing only the daemon restarts the Hermes Gateway, then checks its own programs.
        if root != engine.attempt:
            assert root == engine.attempt / "embeddings"
            assert not any(v for k, v in scheduler.items.items() if k != engine.codes[0])
        else:
            assert not any(scheduler.items.values())

    scheduler.require_stopped = stopped
    for job, _ in jobs:
        raw = services.render_job_definition(job, "windows")
        scheduler.items[job.code] = json.dumps(
            {
                "schema": "daemon.windows-task-capture.v1",
                "running": True,
                "definition": base64.b64encode(raw).decode(),
            }
        ).encode()

    def remove(path, identity):
        info = path.stat()
        assert [info.st_dev, info.st_ino] == identity
        shutil.rmtree(path)

    monkeypatch.setattr(windows_removal, "remove_tree", remove)
    return engine, memory


@pytest.mark.parametrize("action", ["detach", "uninstall"])
def test_windows_controller_preserves_memory_and_scopes_tasks(
    tmp_path, monkeypatch, action
):
    engine, memory = windows_engine(tmp_path, monkeypatch)
    before = tree(memory)
    original = dict(engine.scheduler.items)
    assert apply(engine, action)["done"]
    assert tree(memory) == before
    assert not any(engine.scheduler.capture(c) for c in engine.codes[1:])
    if action == "detach":
        assert engine.scheduler.capture(engine.codes[0]) == original[engine.codes[0]]
        assert (engine.attempt / "source").is_dir()
        assert apply(engine, "uninstall")["done"]
    assert not any(engine.scheduler.items.values())


def test_windows_controller_locked_file_resumes_without_rollback_claim(
    tmp_path, monkeypatch
):
    from daemons_companion.removal import windows as windows_removal

    engine, memory = windows_engine(tmp_path, monkeypatch)
    before = tree(memory)
    remove = windows_removal.remove_tree

    def locked(*args):
        raise PermissionError("synthetic locked executable")

    monkeypatch.setattr(windows_removal, "remove_tree", locked)
    with pytest.raises(Refused, match="recovery"):
        apply(engine, "uninstall")
    assert engine.status()["status"] == "cleaning"
    assert (engine.attempt / "source").exists()
    assert tree(memory) == before
    monkeypatch.setattr(windows_removal, "remove_tree", remove)
    assert apply(engine, "uninstall")["done"]
    assert tree(memory) == before


def test_windows_stop_failure_rolls_back_without_touching_profile(
    tmp_path, monkeypatch
):
    engine, memory = windows_engine(tmp_path, monkeypatch)
    exact = tree(engine.profile), tree(memory), dict(engine.scheduler.items)
    restore = engine.scheduler.restore
    calls = []

    def fail_once(code, value):
        calls.append(code)
        if len(calls) == 2:
            raise RuntimeError("Task refused stop")
        restore(code, value)

    engine.scheduler.restore = fail_once
    with pytest.raises(Refused, match="rolled_back"):
        apply(engine, "detach")
    assert (tree(engine.profile), tree(memory), engine.scheduler.items) == exact


def test_windows_foreign_task_blocks_before_mutation(tmp_path, monkeypatch):
    engine, memory = windows_engine(tmp_path, monkeypatch)
    exact = tree(engine.attempt)

    def shared(*args):
        raise ValueError("shared")

    engine.scheduler.require_exclusive = shared
    with pytest.raises(Refused, match="shared"):
        apply(engine, "uninstall")
    assert tree(engine.attempt) == exact


def test_review_capture_failure_reports_stage_without_writes(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    before = tree(engine.attempt), tree(memory)
    def fail(code):
        raise RuntimeError("private secret path")
    engine.scheduler.capture = fail
    with pytest.raises(Refused) as caught:
        engine.inspect("uninstall")
    assert caught.value.diagnostic == {"stage": "task_capture"}
    assert (tree(engine.attempt), tree(memory)) == before
    assert not engine.marker.exists()


@pytest.mark.parametrize("exit_code, expected_code", [(1, "services"), (4, "shared")])
def test_desktop_entry_preserves_windows_refusal_details(tmp_path, monkeypatch, capsys, exit_code, expected_code):
    """Actual runpy/stdin/stdout entry; Windows COM transport alone is substituted."""
    import io
    import runpy
    import sys
    from daemons_companion.removal.windows import WindowsRemovalScheduler
    from daemons_companion.runtime.services import SchedulerCommandResult

    engine, memory, _ = fixture(tmp_path)
    before = tree(engine.attempt), tree(memory)
    monkeypatch.setattr(WindowsRemovalScheduler, "capture", lambda self, code: None)
    monkeypatch.setattr(WindowsRemovalScheduler, "_run", lambda *args:
        SchedulerCommandResult(exit_code, "", "DAEMON_REMOVAL|task_xml|-2147024891\n"))
    request = {"profile": str(engine.profile), "base": str(engine.base),
               "operation": "inspect", "action": "uninstall"}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    runpy.run_module("daemons_companion.removal.removal", run_name="__main__")
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": False, "code": expected_code,
                      "diagnostic": {"stage": "task_xml", "exit_code": exit_code, "hresult": -2147024891}}
    assert (tree(engine.attempt), tree(memory)) == before
    assert not engine.marker.exists()


def test_windows_detached_host_cleanup_reports_lock_and_resumes(tmp_path, monkeypatch):
    from daemons_companion.removal import windows as windows_removal
    engine, memory = windows_engine(tmp_path, monkeypatch)
    before = tree(memory)
    assert apply(engine, 'detach')['done']
    native_remove = windows_removal.remove_tree
    calls = []

    def locked(path, identity):
        calls.append(path)
        if len(calls) == 1:
            return native_remove(path, identity)
        error = PermissionError(13, 'SECRET private filename')
        error.winerror = 32
        raise error

    monkeypatch.setattr(windows_removal, 'remove_tree', locked)
    with pytest.raises(Refused, match='recovery') as caught:
        apply(engine, 'uninstall')
    assert caught.value.diagnostic == {'stage': 'program_delete', 'errno': 13, 'winerror': 32}
    assert 'SECRET' not in json.dumps(caught.value.diagnostic)
    assert engine.status()['status'] == 'cleaning'
    assert not (engine.attempt / 'source').exists()
    assert tree(memory) == before
    # The existing immutable record remains valid, including after app restart.
    saved = engine.marker.read_bytes()
    assert engine.inspect('uninstall')['recovery']
    assert engine.marker.read_bytes() == saved
    monkeypatch.setattr(windows_removal, 'remove_tree', native_remove)
    assert apply(engine, 'uninstall')['done']
    assert tree(memory) == before


def test_cleanup_preserves_precise_native_stage(tmp_path, monkeypatch):
    from daemons_companion.removal import windows as windows_removal
    engine, memory = windows_engine(tmp_path, monkeypatch)
    before = tree(memory)
    def fail(*args):
        raise Refused('recovery', {'stage': 'file_open', 'winerror': 5})
    monkeypatch.setattr(windows_removal, 'remove_tree', fail)
    with pytest.raises(Refused) as caught:
        apply(engine, 'uninstall')
    assert caught.value.diagnostic == {'stage': 'file_open', 'winerror': 5}
    assert engine.status()['status'] == 'cleaning'
    assert tree(memory) == before


def _added_openclaw(engine, memory, monkeypatch):
    """An OpenClaw added to this daemon: setup record, association and two services."""
    from daemons_companion.hosts import attachment, attachment_command
    from daemons_companion.runtime.services import OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB, scoped_job_code
    from daemons_companion.setup import credentials

    host_id = "e" * 32
    private = engine.base / "Companion/private"
    profile = private / "hosts/openclaw/installations" / host_id / "profile"
    profile.mkdir(parents=True)
    (profile.parent / "host").mkdir()
    (profile.parent / "host/openclaw.mjs").write_text("// synthetic")
    (private / "host-setups").mkdir(parents=True, exist_ok=True)
    (private / "host-setups" / f"{host_id}.json").write_text(json.dumps(
        {"source": str(engine.profile), "profile": str(profile), "credential": "provider-synthetic"}))
    state = engine.scope.home / "data/state"
    state.mkdir(parents=True, exist_ok=True)
    record = state / ("host-attachment-" + attachment.host_reference(profile) + ".json")
    record.write_text(json.dumps({"status": "active", "revision": "association", "api_port": 18666}))
    plists = []
    for code in (OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB):
        label = scoped_job_code(code, host_id)
        path = engine.launch_agents / (label + ".plist")
        program = str(engine.attempt / "python/bin/python3")
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": [program, str(memory)]}))
        plists.append(path)
    events = []
    # Like launchd: a registered job is its plist; removing it deletes the file.
    import base64

    scheduler, plain = engine.scheduler, engine.scheduler.restore
    for path in plists:
        scheduler.items[path.stem] = json.dumps({
            "schema": "daemon.launchd-capture.v1", "loaded": False,
            "definition": base64.b64encode(path.read_bytes()).decode(),
        }).encode()

    def restore(code, value):
        plain(code, value)
        if value is None:
            (engine.launch_agents / (code + ".plist")).unlink(missing_ok=True)

    monkeypatch.setattr(scheduler, "restore", restore)

    def rollback(layout, revision, controller):
        assert revision == "association" and layout == "layout"
        for path in plists:
            restore(path.stem, None)
        record.write_text(json.dumps({"status": "rolled_back", "revision": revision, "api_port": 18666}))
        events.append("detached")

    class Store:
        def delete(self, reference):
            events.append("credential:" + reference)

    monkeypatch.setattr(attachment, "rollback", rollback)
    monkeypatch.setattr(attachment_command, "layouts", lambda request, undo=False: ("layout", "controller"))
    monkeypatch.setattr(credentials, "system_credential_store", Store)
    return profile, events


@pytest.mark.parametrize("action", ["detach", "uninstall"])
def test_an_added_openclaw_no_longer_blocks_removal(tmp_path, monkeypatch, action):
    engine, memory, _ = fixture(tmp_path)
    before = tree(memory)
    profile, events = _added_openclaw(engine, memory, monkeypatch)
    plan = engine.inspect(action)
    assert plan["addedHosts"] == 1
    result = engine.apply(action, plan["revision"], plan["name"])
    assert result["done"] and events[0] == "detached"
    assert tree(memory) == before
    if action == "uninstall":
        assert not profile.parent.exists()
        assert "credential:provider-synthetic" in events
        assert not list((engine.base / "Companion/private/host-setups").glob("*.json"))
    else:
        assert profile.is_dir()


def test_an_association_from_an_older_version_is_stopped_by_ownership(tmp_path, monkeypatch):
    from daemons_companion.hosts import attachment

    engine, memory, _ = fixture(tmp_path)
    profile, events = _added_openclaw(engine, memory, monkeypatch)

    def mismatched(*_args):
        raise RuntimeError("A destination service changed; recovery requires review.")

    monkeypatch.setattr(attachment, "rollback", mismatched)
    plan = engine.inspect("uninstall")
    assert engine.apply("uninstall", plan["revision"], plan["name"])["done"]
    assert not list(engine.launch_agents.glob("*openclaw*")) and not profile.parent.exists()


def test_a_foreign_program_under_an_added_host_name_changes_nothing(tmp_path, monkeypatch):
    from daemons_companion.hosts import attachment

    engine, memory, _ = fixture(tmp_path)
    _added_openclaw(engine, memory, monkeypatch)
    import base64

    for path in engine.launch_agents.glob("*openclaw*"):
        value = plistlib.loads(path.read_bytes())
        path.write_bytes(plistlib.dumps({**value, "ProgramArguments": ["/bin/echo", str(memory)]}))
        engine.scheduler.items[path.stem] = json.dumps({
            "schema": "daemon.launchd-capture.v1", "loaded": False,
            "definition": base64.b64encode(path.read_bytes()).decode(),
        }).encode()

    def mismatched(*_args):
        raise RuntimeError("A destination service changed; recovery requires review.")

    monkeypatch.setattr(attachment, "rollback", mismatched)
    plan = engine.inspect("uninstall")
    before = tree(engine.attempt)
    with pytest.raises(Refused, match="attached"):
        engine.apply("uninstall", plan["revision"], plan["name"])
    assert tree(engine.attempt) == before and not engine.marker.exists()


@pytest.mark.parametrize("name, expected", [
    ("com.daemonharness.test.abc.openclaw-api", "\\DaemonHarness\\com.daemonharness.test.abc.openclaw-api"),
    ("Task <with> secrets/ C:\\\\Users", "unrecognized"),
])
def test_windows_shared_refusal_names_the_blocking_task(tmp_path, monkeypatch, capsys, name, expected):
    import io
    import runpy
    import sys
    from daemons_companion.removal.windows import WindowsRemovalScheduler
    from daemons_companion.runtime.services import SchedulerCommandResult

    engine, memory, _ = fixture(tmp_path)
    monkeypatch.setattr(WindowsRemovalScheduler, "capture", lambda self, code: None)
    monkeypatch.setattr(WindowsRemovalScheduler, "_run", lambda *args:
        SchedulerCommandResult(4, "", f"DAEMON_REMOVAL_SHARED|\\DaemonHarness|{name}\n"))
    request = {"profile": str(engine.profile), "base": str(engine.base),
               "operation": "inspect", "action": "uninstall"}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    runpy.run_module("daemons_companion.removal.removal", run_name="__main__")
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "shared"
    assert result["diagnostic"]["blocking_task"] == expected


def test_removing_only_the_daemon_also_removes_its_memory_search_model(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    before = tree(memory)
    assert (engine.attempt / "embeddings").is_dir()
    plan = engine.inspect("detach")
    assert plan["programs"] == [str(engine.attempt / "embeddings")]
    assert engine.apply("detach", plan["revision"], plan["name"])["done"]
    assert not (engine.attempt / "embeddings").exists()
    assert (engine.attempt / "source").is_dir() and (engine.attempt / "python").is_dir()
    assert tree(memory) == before


def test_an_uninstalled_daemon_memory_may_go_to_the_trash_only_once_unused(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    review = engine.inspect("uninstall")
    assert review["memoryDeletable"] and review["memoryFiles"] == 3 and review["memoryBytes"] > 0
    assert not engine.inspect("detach")["memoryDeletable"]
    # Before the uninstall has finished nothing may be deleted.
    with pytest.raises(Refused, match="changed"):
        engine.memory_check(review["revision"])
    result = engine.apply("uninstall", review["revision"], review["name"])
    assert result["memoryDeletable"]
    with pytest.raises(Refused, match="changed"):
        engine.memory_check("0" * 64)
    # A definition still naming the vault keeps it.
    foreign = engine.launch_agents / "foreign.plist"
    foreign.write_bytes(plistlib.dumps({"Label": "foreign", "ProgramArguments": ["/bin/echo", str(memory)]}))
    with pytest.raises(Refused, match="shared"):
        engine.memory_check(review["revision"])
    foreign.unlink()
    assert engine.memory_check(review["revision"]) == {"memory": str(memory), "revision": review["revision"]}
    assert (memory / "Projects/synthetic.md").exists()  # The check itself deletes nothing.


def test_a_memory_outside_the_daemon_folder_is_never_offered(tmp_path):
    engine, memory, _ = fixture(tmp_path)
    outside = tmp_path / "personal-memory"
    outside.mkdir()
    assert engine.owned_memory(memory) and not engine.owned_memory(outside)
    # A Hermes installation outside Companion's instance folders (the private
    # or older layout) may point at a personal vault: never offered there.
    engine.instance = False
    review = engine.inspect("uninstall")
    assert not review["memoryDeletable"] and "memoryFiles" not in review
    engine.apply("uninstall", review["revision"], review["name"])
    with pytest.raises(Refused, match="memory"):
        engine.memory_check(review["revision"])
    assert (memory / "Projects/synthetic.md").exists()


def test_restoring_a_config_rewritten_by_a_provider_change_keeps_valid_yaml():
    """yaml.safe_dump writes lists at their key's indentation; removal must still parse."""
    import yaml
    from daemons_companion.hermes.profile import HERMES_CONFIG_VALUES
    from daemons_companion.removal.removal import yaml_restore

    before = b"model:\n  provider: openai-codex\n  default: gpt-6-astra\n"
    installed = {"model": {"provider": "openai-codex", "default": "gpt-6.2-sol"},
                 "plugins": {"enabled": ["daemon_voice", "native-x"], "disabled": ["daemon_core"]}}
    for dotted, value in HERMES_CONFIG_VALUES:
        node = installed
        *parents, leaf = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    text = yaml.safe_dump(installed, sort_keys=False).encode()
    assert b"\n  - daemon_voice" in text  # The layout that broke the text patch.
    restored = yaml.safe_load(yaml_restore(text, before))
    assert restored["plugins"]["enabled"] == ["native-x"] and restored["plugins"]["disabled"] == []
    assert restored["model"]["default"] == "gpt-6.2-sol"
    assert "load_timeout_seconds" not in restored["plugins"]
