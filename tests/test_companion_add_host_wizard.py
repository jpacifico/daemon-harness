"""Guided Add a host: order of steps, resumption and plain refusals, without native hosts."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from daemons_companion.hosts import wizard


class UI:
    def __init__(self, choices=(), secrets=(), fields=()):
        self.choices, self.secrets, self.fields = list(choices), list(secrets), list(fields)
        self.phases, self.messages, self.activities, self.prompts = [], [], [], []

    def phase(self, name):
        self.phases.append(name)

    def say(self, message):
        self.messages.append(message)

    def choose(self, title, options, default=0):
        self.prompts.append(title)
        return self.choices.pop(0)

    def secret(self, title):
        self.prompts.append(title)
        return self.secrets.pop(0)

    def field(self, title, previous="", maximum=160):
        self.prompts.append(title)
        return self.fields.pop(0)

    def open_link(self, url):
        return True

    @contextmanager
    def activity(self, label):
        self.activities.append(label)
        yield lambda _message: None


class Creation:
    """Records operations like the durable creation record, without downloads."""

    def __init__(self, host, source, binding, **kwargs):
        self.host, self.operations = host, []
        self.data = {"revision": "r", "phase": "download", "installation_id": host.installation_id}
        self.progress = self.events = None
        Creation.last = self

    def start(self, port):
        return dict(self.data)

    def current(self):
        return dict(self.data)

    def perform(self, operation, revision, values):
        self.operations.append((operation, dict(values)))
        if operation == "download":
            self.data["phase"] = "key"
        elif operation == "key":
            self.data.update(phase="model", kind=values["kind"], models=["mistral-large-4"])
        elif operation == "model":
            self.data.update(phase="telegram", model=values["model"])
        elif operation == "telegram":
            if values["token"] == "hermes-bot":
                raise ValueError("Use a separate bot; same-bot transfer is not enabled.")
            self.data.update(phase="ready", bot="Aster_openclaw_bot")
        return dict(self.data)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    base = tmp_path / "base"
    profile = base / "hosts/hermes/installations/a/profile"
    profile.mkdir(parents=True)
    (base / "bundle").mkdir()
    monkeypatch.setenv("DAEMON_HARNESS_COMPANION_BUNDLE_ROOT", str(base / "bundle"))
    binding = SimpleNamespace(state=tmp_path / "state", user_id="777001")
    calls = SimpleNamespace(attached=0, status="current", reviewed=None, run=None)
    Creation.last = None

    class Upgrade:
        def review(self):
            if calls.reviewed == "refused":
                from daemons_companion.runtime.upgrade import UpgradeRefused
                raise UpgradeRefused(calls.refusal, "synthetic refusal")
            return {"revision": "u", "status": "review"}

        def run(self, revision, recover=False):
            calls.run = revision
            return {"status": calls.reviewed or "committed"}

    import daemons_companion.runtime.upgrade as upgrade
    import daemons_companion.hosts.attachment_command as command
    import daemons_companion.hermes.first_install as first_install
    import daemons_companion.setup.api_setup as api_setup
    from daemon_core import api_providers

    monkeypatch.setattr(upgrade, "from_request", lambda request: Upgrade())
    monkeypatch.setattr(upgrade, "current_status", lambda u: {"revision": "s", "status": calls.status})
    monkeypatch.setattr(command, "source_binding", lambda source: (None, None, binding))
    monkeypatch.setattr(command, "native_target", lambda: "macos")
    monkeypatch.setattr(wizard.OpenClawHostLayout, "validate", lambda self: None)
    monkeypatch.setattr(wizard.host_creation, "Creation", Creation)
    monkeypatch.setattr(wizard.host_creation, "record_path", lambda host: tmp_path / "missing.json")
    monkeypatch.setattr(wizard, "_attach", lambda *a: setattr(calls, "attached", calls.attached + 1))
    monkeypatch.setattr(first_install, "first_exchange", lambda *a, **k: True)
    monkeypatch.setattr(api_providers, "validate_key", lambda key: key)
    monkeypatch.setattr(api_setup, "verify_model", lambda *a: None)
    return profile, base, calls


def test_a_new_host_follows_the_first_installation_steps(flow):
    profile, base, calls = flow
    # Continue, OpenAI API key (after the recommended ChatGPT), its model, bot created.
    ui = UI(choices=[0, 1, 0, 0], secrets=["synthetic-key", "987654:" + "b" * 35])
    assert wizard.run(ui, profile, base, "Aster")
    assert ui.phases == ["host", "model", "telegram", "installation", "conversation"]
    assert [op for op, _ in Creation.last.operations] == ["download", "key", "model", "telegram"]
    assert calls.attached == 1
    assert "OpenClaw is ready. Open https://t.me/Aster_openclaw_bot" in ui.messages
    assert "Aster now answers you from both bots. Use whichever you prefer." in ui.messages
    assert "Downloading and verifying OpenClaw..." in ui.activities


def test_a_daemon_too_old_to_update_stops_with_a_plain_explanation(flow):
    profile, base, calls = flow
    calls.status, calls.reviewed, calls.refusal = "required", "refused", "protected-runtime"
    ui = UI(choices=[0, 0])
    with pytest.raises(wizard.HostAdditionStopped) as stopped:
        wizard.run(ui, profile, base, "Aster")
    assert any("older version of the app" in m for m in ui.messages)
    assert stopped.value.code == "protected-runtime"
    assert Creation.last is None and calls.attached == 0


def test_stopped_services_are_not_reported_as_an_old_daemon(flow):
    profile, base, calls = flow
    calls.status, calls.reviewed, calls.refusal = "required", "refused", "services-changed"
    ui = UI(choices=[0, 0])
    with pytest.raises(wizard.HostAdditionStopped) as stopped:
        wizard.run(ui, profile, base, "Aster")
    assert stopped.value.code == "services-changed"
    assert any("background services" in m for m in ui.messages)
    assert not any("older version" in m for m in ui.messages)


def test_a_required_update_runs_before_creation_and_a_rollback_stops(flow):
    profile, base, calls = flow
    calls.status = "required"
    # Continue, update, OpenAI API key, its model, bot created.
    ui = UI(choices=[0, 0, 1, 0, 0], secrets=["synthetic-key", "987654:" + "b" * 35])
    assert wizard.run(ui, profile, base, "Aster")
    assert calls.run == "u" and "Aster is up to date." in ui.messages
    calls.reviewed = "rolled_back"
    ui = UI(choices=[0, 0])
    with pytest.raises(wizard.HostAdditionStopped):
        wizard.run(ui, profile, base, "Aster")
    assert any("restored to its previous version" in m for m in ui.messages)


def test_the_hermes_bot_is_refused_with_a_clear_retry(flow):
    profile, base, calls = flow
    # Continue, OpenAI API key, model, bot created, then "Enter the token again".
    ui = UI(choices=[0, 1, 0, 0, 0], secrets=["synthetic-key", "hermes-bot", "987654:" + "b" * 35])
    assert wizard.run(ui, profile, base, "Aster")
    assert "This is your Hermes Agent bot. Create a new bot with BotFather for OpenClaw." in ui.messages


def test_a_prepared_but_unattached_host_is_resumed(tmp_path, flow):
    profile, base, calls = flow
    setups = base / "Companion/private/host-setups"
    setups.mkdir(parents=True)
    host_id = "c" * 32
    (setups / f"{host_id}.json").write_text(json.dumps({
        "source": str(profile), "phase": "ready",
        "profile": str(base / f"Companion/private/hosts/openclaw/installations/{host_id}/profile"),
    }))
    (tmp_path / "state").mkdir()
    assert wizard._resumable_creation(base, profile, tmp_path / "state") == host_id
    from daemons_companion.hosts.attachment import host_reference

    record = tmp_path / "state" / ("host-attachment-" + host_reference(
        base / f"Companion/private/hosts/openclaw/installations/{host_id}/profile") + ".json")
    record.write_text(json.dumps({"status": "active"}))
    assert wizard._resumable_creation(base, profile, tmp_path / "state") is None


def test_cancel_at_the_introduction_changes_nothing(flow):
    profile, base, calls = flow
    with pytest.raises(wizard.Quit):
        wizard.run(UI(choices=[1]), profile, base, "Aster")
    assert calls.attached == 0
