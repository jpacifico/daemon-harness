"""Moving a daemon to its other host, on synthetic installations."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import uuid

import pytest

import daemons_companion.desktop.provider_settings as provider_settings
import daemons_companion.hosts.change as change_module
import daemons_companion.removal.removal as removal
from daemons_companion.hosts.change import ChangeError, HostChange, write_request
from daemons_companion.removal.removal import OPENCLAW_PROGRAMS, Removal
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.services import BackgroundJob, render_job_definition
from daemons_companion.setup.provider import _credential_payload, _json_bytes

_spec = importlib.util.spec_from_file_location(
    "removal_fixture", Path(__file__).resolve().parents[1] / "apps/companion/desktop/removal-fixture.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="launchd definitions")

OWNER = "12345"
KEY = "synthetic-key-123"
TOKEN = "123456:" + "A" * 30
PROVIDER_REF = "provider-" + str(uuid.UUID(int=1))
CONNECTION_REF = "provider-" + str(uuid.UUID(int=2))


class Store:
    def __init__(self):
        self.values = {}

    def get(self, reference):
        return self.values.get(reference)

    def put(self, reference, value):
        self.values[reference] = value

    def delete(self, reference):
        self.values.pop(reference)


class Scheduler:
    """Records every call, so tests can check the order of the switch."""

    def __init__(self, items=None):
        self.items, self.calls = dict(items or {}), []

    def capture(self, code):
        return self.items.get(code)

    def restore(self, code, value):
        self.calls.append((code, value is not None))
        self.items[code] = value


class Scope(InstallationScope):
    # The real receipt verifies a downloaded OpenClaw; this one is synthetic.
    def receipt(self):
        return {"schema": "daemon.companion-installation-scope.v1", "host": {"synthetic": True}}


def memory_files(memory):
    from daemon_core.memory.library_layout import MARKER

    (memory / ".daemon" / MARKER).write_text(json.dumps({
        "schema": 1, "layout": "root-folders-v1", "vault_root": str(memory),
        "runtime_root": str(memory / ".daemon")}))
    (memory / ".daemon/core/daemon_identity.json").write_text(json.dumps({"name": "Nova", "gender": "feminine"}))
    users = memory / ".daemon/users" / OWNER
    users.mkdir(parents=True)
    (users / "persona.md").write_text("# Persona\n")
    (users / "first_meeting.json").write_text(json.dumps({"language": "fr"}))


def provider(scope, store):
    config = scope.home / "data/state/config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "provider.json").write_bytes(_json_bytes({
        "schema": "daemon.companion-provider.v1", "kind": "mistral_api", "model": "mistral-large-4",
        "base_url": "", "reasoning": {"routing": "medium", "conversation": "medium", "memory": "high"},
        "credential": {"name": "MISTRAL_API_KEY", "reference": PROVIDER_REF}}))
    store.put(PROVIDER_REF, _credential_payload("owner", KEY))


def hermes_source(tmp_path, store):
    engine, memory, _ = _fixture.fixture(tmp_path)
    memory_files(memory)
    (engine.attempt / "connection.json").write_text(json.dumps(
        {"schema": "daemon.private-connection.v2", "provider_kind": "mistral_api", "user_id": OWNER}))
    provider(engine.scope, store)
    engine.scheduler = Scheduler(engine.scheduler.items)
    return engine, memory


def openclaw_source(tmp_path, store):
    base = tmp_path.resolve() / "DaemonHarness"
    attempt = base / "Companion/instance-ABC123/hosts/openclaw/installations" / uuid.uuid4().hex
    profile = attempt / "profile"
    profile.mkdir(parents=True)
    scope = Scope(attempt)
    scope.initialize()
    memory = scope.home / "data/memory"
    (memory / ".daemon/core").mkdir(parents=True)
    memory_files(memory)
    (memory / "Projects").mkdir()
    (memory / "Projects/synthetic.md").write_text("# Keep every byte\n")
    (profile / "openclaw.json").write_text('{"gateway": {"port": 18789}}\n')
    (attempt / "openclaw-connection.json").write_text(json.dumps(
        {"schema": "daemon.openclaw-connection.v1", "credential": CONNECTION_REF, "kind": "mistral_api"}))
    store.put(CONNECTION_REF, json.dumps({"installation_id": attempt.name, "key": KEY, "token": TOKEN,
                                          "gateway": "gw"}))
    (attempt / "connection.json").write_text(json.dumps(
        {"schema": "daemon.private-connection.v2", "provider_kind": "mistral_api", "user_id": OWNER}))
    provider(scope, store)
    for relative in OPENCLAW_PROGRAMS:
        (attempt / relative).mkdir(parents=True, exist_ok=True)
        (attempt / relative / "program").write_text(relative)
    (attempt / "logs").mkdir()
    executable = attempt / "node/worker"
    shutil.copyfile("/bin/sleep", executable)
    executable.chmod(0o700)
    tx = scope.home / "transactions" / uuid.uuid4().hex
    tx.mkdir(parents=True)
    (tx / "journal.json").write_text(json.dumps({"status": "committed"}))
    (attempt / "daemon-setup.json").write_text(json.dumps(
        {"schema": "daemon.private-setup.v1", "transaction_id": tx.name}))
    agents = tmp_path / "home/Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    scheduler = Scheduler()
    engine = Removal(profile, base, agents, scheduler=scheduler)
    for code in engine.codes:
        job = BackgroundJob(code, str(executable), ("60",), str(attempt), str(attempt / "logs" / (code + ".log")),
                            code != engine.codes[1], 4 if code == engine.codes[1] else None)
        raw = render_job_definition(job, "macos")
        (agents / (code + ".plist")).write_bytes(raw)
        scheduler.items[code] = json.dumps({"schema": "daemon.launchd-capture.v1", "loaded": True,
                                            "definition": base64.b64encode(raw).decode()}).encode()
    return engine, memory


class Target:
    """The new host's scope: only its home and attempt are used by the switch."""

    def __init__(self, root):
        self.attempt = root / "target-attempt"
        self.home = self.attempt / "daemon"
        self.home.mkdir(parents=True)


class TargetRemoval:
    def __init__(self):
        self.applied = []

    def __call__(self, profile):
        self.profile = profile
        return self

    def inspect(self, action):
        return {"revision": "r", "name": "Nova"}

    def apply(self, action, revision, name):
        self.applied.append((str(self.profile), action))


@pytest.fixture(params=["hermes", "openclaw"])
def change(request, tmp_path, monkeypatch):
    if request.param == "openclaw":
        monkeypatch.setattr(removal, "InstallationScope", Scope)
        monkeypatch.setattr(provider_settings, "InstallationScope", Scope)
    import daemons_companion.setup.credentials as credentials

    store = Store()
    monkeypatch.setattr(credentials, "system_credential_store", lambda: store)
    engine, memory = (hermes_source if request.param == "hermes" else openclaw_source)(tmp_path, store)
    engine.store = store
    path = write_request(tmp_path / "setup/host-change", engine.profile, engine.base)
    sleeps = []
    value = HostChange.from_request(path, removal=engine, target_removal=TargetRemoval(), store=store,
                       sleep=sleeps.append, queue_wait=20)
    value.sleeps, value.memory, value.target_root = sleeps, memory, tmp_path
    return value


def test_inspect_reads_the_identity_provider_and_owner_it_reuses(change):
    review = change.inspect()
    assert review == {"name": "Nova", "source": change.source, "kind": "mistral_api",
                      "target": "openclaw" if change.source == "hermes" else "hermes"}
    assert {k: change.identity[k] for k in ("name", "gender", "language", "owner", "model")} == {
        "name": "Nova", "gender": "feminine", "language": "fr", "owner": OWNER, "model": "mistral-large-4"}
    assert change.key() == KEY


def test_the_bot_token_comes_from_the_previous_host(change):
    if change.source == "hermes":
        (change.source_profile / ".env").write_text(f"TELEGRAM_BOT_TOKEN='{TOKEN}'\n")
    assert change.token() == TOKEN
    # A malformed value is never reused: the form asks for a token instead.
    if change.source == "hermes":
        (change.source_profile / ".env").write_text("TELEGRAM_BOT_TOKEN=not-a-token\n")
        assert change.token() == ""


def test_switch_stops_the_gateway_first_waits_for_the_queue_and_moves_the_memory(change, monkeypatch):
    scheduler, codes = change.removal.scheduler, change.removal.codes
    before = _fixture.tree(change.memory)
    pending = change.spool() / "pending/x"
    pending.mkdir(parents=True)
    (pending / "turn.json").write_text("{}")
    order = []

    def sleep(seconds):
        # The queue settles while only the gateway is stopped.
        order.append(list(scheduler.calls))
        (pending / "turn.json").unlink(missing_ok=True)

    change.sleep = sleep
    restored = change.switch(Target(change.target_root))
    assert order == [[(codes[0], False)]]
    assert [c for c, _ in scheduler.calls] == codes and not any(v for _, v in scheduler.calls)
    assert restored.move and restored.owner == OWNER and restored.name == "Nova"
    assert change.state == "switched"
    # The previous memory is untouched: it stays as an archive.
    assert _fixture.tree(change.memory) == before


def test_a_busy_queue_restarts_every_service_unchanged(change):
    services = dict(change.removal.scheduler.items)
    pending = change.spool() / "running/x"
    pending.mkdir(parents=True)
    (pending / "turn.json").write_text("{}")
    with pytest.raises(ChangeError, match="daemon-busy"):
        change.switch(Target(change.target_root))
    assert change.removal.scheduler.items == services
    assert change.state == "prepared" and len(change.sleeps) == 4


def test_a_failed_archive_restarts_every_service_unchanged(change, monkeypatch):
    services = dict(change.removal.scheduler.items)
    before = _fixture.tree(change.memory)

    def fail(*_):
        raise OSError("synthetic")

    monkeypatch.setattr(change_module, "create_archive", fail)
    target = Target(change.target_root)
    with pytest.raises(ChangeError, match="switch-failed"):
        change.switch(target)
    assert change.removal.scheduler.items == services
    assert _fixture.tree(change.memory) == before
    assert not (target.home / "restore" / change_module.ARCHIVE).exists()
    assert change.state == "prepared"


def test_an_unfinished_new_host_gives_the_daemon_back(change):
    services = dict(change.removal.scheduler.items)
    change.switch(Target(change.target_root))
    change.abandon()
    assert change.removal.scheduler.items == services and change.state == "reverted"


def test_an_interrupted_stop_is_undone_before_switching_again(change):
    services = dict(change.removal.scheduler.items)
    target = Target(change.target_root)
    data = change.record()
    data.update(state="switching", target_attempt=str(target.attempt), switched_at=0,
                services={code: base64.b64encode(value).decode() for code, value in services.items()})
    change.save(data)
    change.removal.scheduler.restore(change.removal.codes[0], None)
    assert change.switch(target).move
    assert change.state == "switched"


def test_yes_uninstalls_only_the_previous_host(change):
    target = Target(change.target_root)
    change.switch(target)
    (target.attempt / "daemon-setup.json").write_text("{}")
    change.resume()
    assert change.state == "installed"
    before = _fixture.tree(change.memory)
    change.finish()
    assert change.state == "finished"
    assert change.target_removal.applied == []
    assert _fixture.tree(change.memory) == before
    assert not any(change.removal.scheduler.capture(code) for code in change.removal.codes)
    programs = OPENCLAW_PROGRAMS if change.source == "openclaw" else ("source", "python", "codex", "embeddings")
    assert not any((change.removal.attempt / p).exists() for p in programs)


def test_going_back_uninstalls_the_new_host_and_restarts_the_previous_one(change):
    services = dict(change.removal.scheduler.items)
    target = Target(change.target_root)
    change.switch(target)
    change.installed(target.attempt / "profile")
    change.revert()
    assert change.target_removal.applied == [(str(target.attempt / "profile"), "uninstall")]
    assert change.removal.scheduler.items == services and change.state == "reverted"


class UI:
    def __init__(self, answers):
        self.answers, self.said, self.questions = list(answers), [], []

    def say(self, value):
        self.said.append(value)

    def choose(self, title, options, **_):
        self.questions.append((title, options))
        return self.answers.pop(0)

    def phase(self, _):
        pass

    def activity(self, _):
        from contextlib import nullcontext

        return nullcontext()


def test_conclusion_waits_for_a_real_reply_before_offering_the_uninstall(change, monkeypatch):
    target = Target(change.target_root)
    change.switch(target)
    change.installed(target.attempt / "profile")
    replies = iter([False, True])
    monkeypatch.setattr(change, "answered", lambda data: next(replies))
    finished = []
    monkeypatch.setattr(change, "finish", lambda: finished.append(True))
    ui = UI([0, 0])
    assert change.conclude(ui) is True and finished == [True]
    # Without a confirmed reply the uninstall is not offered.
    first, second = ui.questions
    assert not any("uninstall" in option for option in first[1])
    assert second[1][0].startswith("Yes — uninstall")


def test_a_daemon_on_both_hosts_is_refused(change, monkeypatch):
    review = change.removal.inspect("uninstall")
    monkeypatch.setattr(change.removal, "inspect", lambda action: {**review, "addedHosts": 1})
    with pytest.raises(ChangeError, match="added-host"):
        change.inspect()


def test_a_profile_outside_the_app_is_refused(tmp_path):
    profile = tmp_path / "elsewhere/profile"
    profile.mkdir(parents=True)
    path = write_request(tmp_path / "setup", profile, tmp_path)
    with pytest.raises(ChangeError, match="not-owned"):
        HostChange.from_request(path, store=Store())


def test_the_api_key_is_reused_once_then_asked(change, monkeypatch):
    from daemons_companion.setup import api_setup

    monkeypatch.setattr(change_module, "pending_change", lambda: change)
    ui = UI([])
    assert api_setup.reused_key(ui, "mistral_api") == KEY
    assert api_setup.reused_key(ui, "openai_api") == ""
    name, gender, language, _ = change_module.inherited_identity()
    assert (name, gender, language) == ("Nova", "feminine", "fr")


@pytest.fixture
def bundle(tmp_path):
    template = tmp_path / "bundle/memory/templates/users/conversation.md"
    template.parent.mkdir(parents=True)
    template.write_text("Template manner")
    template.with_name("persona.md").write_text("Template persona")
    return tmp_path / "bundle"


def test_the_new_memory_is_the_previous_one_and_a_resume_keeps_it(change, bundle, monkeypatch):
    from types import SimpleNamespace

    from daemons_companion.hermes.first_install import initialize_private_memory

    monkeypatch.setattr(change_module, "pending_change", lambda: change)
    target = Target(change.target_root)
    scope = SimpleNamespace(home=target.home, attempt=target.attempt, validate_home=lambda _: None)
    initialize_private_memory(scope, bundle, OWNER, root_folder_memory=True, conversation_language="en")
    restored = target.home / "data/memory"
    assert (restored / ".daemon/users" / OWNER / "persona.md").read_text() == "# Persona\n"
    assert (restored / "Projects/synthetic.md").exists()
    assert change.state == "switched"
    assert not (target.home / "restore" / change_module.ARCHIVE).exists()
    # A resumed setup neither stops anything again nor replaces the memory.
    (restored / "Projects/synthetic.md").write_text("A new turn.\n")
    calls = list(change.removal.scheduler.calls)
    initialize_private_memory(scope, bundle, OWNER, root_folder_memory=True, conversation_language="en")
    assert (restored / "Projects/synthetic.md").read_text() == "A new turn.\n"
    assert change.removal.scheduler.calls == calls


class Session(UI):
    def __init__(self, answers):
        super().__init__(answers)
        self.frames = []

    def emit(self, kind, **values):
        self.frames.append((kind, values))


def test_a_session_that_stops_before_the_new_host_leaves_the_daemon_where_it_was(change, monkeypatch):
    import daemons_companion.desktop.protocol as protocol

    services = dict(change.removal.scheduler.items)

    def wizard(*_, **__):
        change.switch(Target(change.target_root))
        return 1

    monkeypatch.setattr(protocol, "run_wizard", wizard)
    ui = Session([])
    assert protocol.serve_change(ui, SimpleOwner(), change) == 1
    assert change.removal.scheduler.items == services and change.state == "reverted"
    assert ui.frames[-1][1]["message"].startswith("Nova keeps running on")


def test_a_session_resumed_after_the_install_goes_straight_to_the_check(change, monkeypatch):
    import daemons_companion.desktop.protocol as protocol

    target = Target(change.target_root)
    change.switch(target)
    (target.attempt / "daemon-setup.json").write_text("{}")
    monkeypatch.setattr(protocol, "run_wizard", lambda *_, **__: pytest.fail("no second install"))
    monkeypatch.setattr(change, "answered", lambda data: True)
    monkeypatch.setattr(change, "finish", lambda: None)
    ui = Session([0])
    assert protocol.serve_change(ui, SimpleOwner(), change) == 0
    assert ui.frames[-1][0] == "finished" and ui.frames[-1][1]["result"] == "installed"


class SimpleOwner:
    backend = None


def catalog_patterns():
    import re as regex

    catalog = json.loads((Path(__file__).resolve().parents[1] / "apps/companion/desktop/locales/messages.json").read_text())
    return [regex.compile("^" + regex.sub(r"\\\{[a-zA-Z_]+\\\}", ".+", regex.escape(entry["en"])) + "$", regex.S)
            for entry in catalog.values()]


def test_every_message_of_the_change_is_translated(change, monkeypatch):
    import daemons_companion.desktop.protocol as protocol
    from daemons_companion.desktop.terminal import Quit

    shown = []
    for answers, reply in (([0, 0], [False, True]), ([1, 2], [True, True])):
        target = Target(change.target_root)
        change.save({"schema": change_module.SCHEMA, "state": "prepared", "source_profile": str(change.source_profile)})
        change.removal.scheduler.items = {k: v for k, v in change.removal.scheduler.items.items()}
        ui = Session([0, *answers])
        change_module.confirm(ui, change)
        change.switch(target)
        change.installed(target.attempt / "profile")
        replies = iter(reply)
        monkeypatch.setattr(change, "answered", lambda data: next(replies))
        monkeypatch.setattr(change, "finish", lambda: None)
        monkeypatch.setattr(change, "revert", lambda: None)
        change.conclude(ui)
        shown += ui.said + [t for title, options in ui.questions for t in (title, *options)]
        shutil.rmtree(target.attempt)
    ui = Session([1])
    with pytest.raises(Quit):
        change_module.confirm(ui, change)
    change.save({"schema": change_module.SCHEMA, "state": "prepared", "source_profile": str(change.source_profile)})
    monkeypatch.setattr(protocol, "run_wizard", lambda *_, **__: 1)
    protocol.serve_change(ui, SimpleOwner(), change)
    protocol.stopped_change(ui, change)
    shown += [values["message"] for _, values in ui.frames]
    shown += [change_module.message(ChangeError(code)) for code in (*change_module.MESSAGES, "change.uninstall-x", "x")]
    monkeypatch.setattr(change_module, "pending_change", lambda: change)
    from daemons_companion.setup.api_setup import reused_key

    ui = Session([])
    reused_key(ui, "mistral_api")
    shown += [change_module.inherited_identity()[3], *ui.said, "Telegram verified @synthetic_bot, the same bot as before."]
    patterns = catalog_patterns()
    assert [text for text in shown if not any(p.match(text) for p in patterns)] == []


def test_the_apps_check_reads_the_daemon_without_a_temporary_file(change, monkeypatch, capsys, tmp_path):
    import io
    import sys

    monkeypatch.setattr(change_module, "Removal", lambda profile, base, agents: change.removal)
    monkeypatch.setattr(sys, "platform", "darwin")
    # A temporary folder reached through a symbolic link, as on macOS, is never used.
    monkeypatch.setattr("tempfile.gettempdir", lambda: pytest.fail("no temporary file"))
    request = {"action": "inspect", "profile": str(change.source_profile), "base": str(change.base)}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    change_module.main()
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "result": {"name": "Nova", "source": change.source, "kind": "mistral_api",
                                             "target": change.target}}
