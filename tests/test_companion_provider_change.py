"""The guided provider change, with a synthetic daemon, services and person."""

from contextlib import contextmanager
from pathlib import Path

import pytest

import daemons_companion.hosts.provider_change as module
from daemons_companion.desktop.terminal import Quit


class UI:
    def __init__(self, answers=(), secrets=()):
        self.answers, self.secrets = list(answers), list(secrets)
        self.said, self.prompts, self.phases = [], [], []

    def phase(self, name):
        self.phases.append(name)

    def say(self, text):
        self.said.append(text)

    def choose(self, title, options, default=0):
        self.prompts.append((title, options))
        return self.answers.pop(0)

    def secret(self, title):
        return self.secrets.pop(0)

    @contextmanager
    def activity(self, text):
        yield lambda *_: None


class Scheduler:
    def __init__(self):
        self.calls = []

    def restore(self, code, capture):
        self.calls.append((code, capture))


class Removal:
    def __init__(self):
        self.scheduler = Scheduler()

    def service_snapshot(self, memory):
        return {"gateway": b"g", "worker": b"w", "nightly": None, "embeddings": b"e"}


class Daemon:
    def __init__(self, tmp_path, host="hermes", fail=False):
        self.scope = type("Scope", (), {"home": tmp_path / "home", "profile": tmp_path / "profile",
                                        "host_kind": host, "attempt": tmp_path})()
        self.openclaw = host == "openclaw"
        self.change_dir = tmp_path / "provider-change"
        self.private_connection = tmp_path / "connection.json"
        self.fail, self.switched, self.events = fail, None, []

    def describe(self):
        return {"kind": "anthropic_api", "model": "claude-opus-5-5", "pending": False,
                "switchable": True, "revision": "r1"}

    def switch(self, revision, kind, model, key, codex, services):
        self.events.append("switch")
        if self.fail:
            raise RuntimeError("synthetic")
        self.switched = (revision, kind, model, key, codex, dict(services))
        return {"status": "applied"}

    def commit(self):
        self.events.append("commit")

    def revert(self, scheduler):
        self.events.append(("revert", scheduler))

    def pending(self):
        return {"status": "applied"} if self.switched and "commit" not in self.events else None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    removal = Removal()
    monkeypatch.setattr(module, "service_scheduler", lambda profile, base: removal)
    monkeypatch.setattr(module, "pending_work", lambda spool: 0, raising=False)
    monkeypatch.setattr("daemons_companion.backup.live.pending_work", lambda spool: 0)
    monkeypatch.setattr(module, "_answered", lambda daemon, since: True)
    monkeypatch.setattr(module, "_previous_kind", lambda daemon: "anthropic_api")
    return removal


def test_api_switch_stops_the_gateway_first_and_keeps_only_after_a_reply(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr(module, "_api", lambda ui, host, kind: module.Connection(kind, "gpt-6-astra", key="k"))
    # Offered: ChatGPT, OpenAI (the current Anthropic is left out); then confirm; then keep.
    ui = UI(answers=[1, 0, 0])
    assert module.run(ui, Path("/p"), Path("/b"), "Nova", None) is True
    assert ui.prompts[0][1] == ("ChatGPT subscription (recommended) — sign in with your account", "OpenAI API key")
    revision, kind, model, key, codex, services = daemon.switched
    assert (revision, kind, model, key, codex) == ("r1", "openai_api", "gpt-6-astra", "k", None)
    # The local search service keeps running; the others restart with their definitions.
    assert list(services) == ["gateway", "worker", "nightly"]
    assert setup.scheduler.calls == [("gateway", None), ("worker", None), ("nightly", None),
                                     ("nightly", None), ("worker", b"w"), ("gateway", b"g")]
    assert daemon.events == ["switch", "commit"]
    assert ui.phases == ["model", "installation", "conversation"]


def test_cancelling_before_the_switch_changes_nothing(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr(module, "_api", lambda ui, host, kind: module.Connection(kind, "gpt-6-astra", key="k"))
    with pytest.raises(Quit):
        module.run(UI(answers=[1, 1]), Path("/p"), Path("/b"), "Nova", None)
    assert daemon.events == [] and setup.scheduler.calls == []


def test_a_failed_switch_restarts_the_previous_services(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path, fail=True)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr(module, "_api", lambda ui, host, kind: module.Connection(kind, "gpt-6-astra", key="k"))
    with pytest.raises(RuntimeError):
        module.run(UI(answers=[1, 0]), Path("/p"), Path("/b"), "Nova", None)
    assert setup.scheduler.calls == [("gateway", None), ("worker", None), ("nightly", None),
                                     ("nightly", None), ("worker", b"w"), ("gateway", b"g")]


def test_going_back_reverts_with_the_services(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr(module, "_api", lambda ui, host, kind: module.Connection(kind, "gpt-6-astra", key="k"))
    ui = UI(answers=[1, 0, 2])
    assert module.run(ui, Path("/p"), Path("/b"), "Nova", None) is False
    assert daemon.events == ["switch", ("revert", setup.scheduler)]
    assert "Nova uses Anthropic again, exactly as before." in ui.said


def test_no_reply_offers_to_wait_or_go_back(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path)
    replies = iter([False, True])
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr(module, "_answered", lambda d, since: next(replies))
    monkeypatch.setattr(module, "_api", lambda ui, host, kind: module.Connection(kind, "gpt-6-astra", key="k"))
    ui = UI(answers=[1, 0, 0, 0])
    assert module.run(ui, Path("/p"), Path("/b"), "Nova", None) is True
    assert ui.prompts[2] == ("Nova has not answered with OpenAI yet", ("Wait again", "Go back to Anthropic"))


def test_chatgpt_on_openclaw_uses_its_native_connection(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path, host="openclaw")
    binary = tmp_path / "codex"
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr("daemons_companion.openclaw.gateway.host_for", lambda scope: "host")
    monkeypatch.setattr("daemons_companion.openclaw.subscription.first_connection",
                        lambda host, ui, saved, **_: (binary, "gpt-6.2-sol"))
    # OpenClaw offers ChatGPT, OpenAI (the current Anthropic is left out).
    ui = UI(answers=[0, 0, 0])
    assert module.run(ui, Path("/p"), Path("/b"), "Nova", None) is True
    assert daemon.switched[1:5] == ("codex_cli", "gpt-6.2-sol", None, binary)


def test_an_unfinished_session_goes_back(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path)
    daemon.switched = ("r1",)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    module.abandon(Path("/p"), Path("/b"))
    assert daemon.events == [("revert", setup.scheduler)]


class SubscriptionDaemon(Daemon):
    def __init__(self, tmp_path, host="hermes", codex="codex/0.159.2/bin/codex"):
        super().__init__(tmp_path, host)
        self.private_connection.write_text('{"codex": "%s"}' % (tmp_path / codex))

    def describe(self):
        return {"kind": "codex_cli", "model": "gpt-6-astra", "pending": False, "switchable": True,
                "model_changeable": True, "revision": "r1"}

    def provider(self):
        return {"kind": "codex_cli", "model": "gpt-6-astra"}


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_a_subscription_daemon_changes_model_with_the_current_codex_and_keeps_after_a_reply(
        tmp_path, setup, monkeypatch, host):
    daemon = SubscriptionDaemon(tmp_path, host)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    newer = tmp_path / "codex/0.170.0/bin/codex"
    seen = {}

    def subscription(ui, owned, context, *, signed_in=False):
        seen["signed_in"] = signed_in
        return module.Connection("codex_cli", "gpt-6.2-sol", codex=newer)

    monkeypatch.setattr(module, "_subscription", subscription)
    ui = UI(answers=[0, 0])  # Change to GPT-6.2 Sol, then keep it after the reply.
    assert module.run(ui, tmp_path / "profile", tmp_path, "Nova", None, model_only=True) is True
    assert seen == {"signed_in": True}
    assert daemon.switched[1:5] == ("codex_cli", "gpt-6.2-sol", None, newer)
    assert ui.prompts[0] == ("Change Nova's model to GPT-6.2 Sol?", ("Change to GPT-6.2 Sol", "Cancel"))
    assert ui.prompts[1][1][2] == "Go back to GPT-6 Astra"
    assert daemon.events == ["switch", "commit"]


def test_choosing_the_same_model_and_codex_changes_nothing(tmp_path, setup, monkeypatch):
    daemon = SubscriptionDaemon(tmp_path, codex="codex/0.170.0/bin/codex")
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    monkeypatch.setattr(module, "_subscription", lambda ui, owned, context, **_: module.Connection(
        "codex_cli", "gpt-6-astra", codex=tmp_path / "codex/0.170.0/bin/codex"))
    ui = UI()
    assert module.run(ui, tmp_path / "profile", tmp_path, "Nova", None, model_only=True) is False
    assert daemon.events == [] and "Nova already uses GPT-6 Astra. Nothing was changed." in ui.said


def test_the_same_model_with_an_older_codex_is_still_a_change(tmp_path, setup, monkeypatch):
    daemon = SubscriptionDaemon(tmp_path)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    newer = tmp_path / "codex/0.170.0/bin/codex"
    monkeypatch.setattr(module, "_subscription", lambda ui, owned, context, **_: module.Connection(
        "codex_cli", "gpt-6-astra", codex=newer))
    assert module.run(UI(answers=[0, 0]), tmp_path / "profile", tmp_path, "Nova", None, model_only=True)
    assert daemon.switched[1:5] == ("codex_cli", "gpt-6-astra", None, newer)


def test_an_api_daemon_has_no_guided_model_change(tmp_path, setup, monkeypatch):
    daemon = Daemon(tmp_path)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile: daemon)
    with pytest.raises(module.ProviderSettingsError, match="provider.unsupported"):
        module.run(UI(), tmp_path / "profile", tmp_path, "Nova", None, model_only=True)
    assert daemon.events == []


@pytest.mark.parametrize("request_value,model_only", [
    ({"profile": "/p", "base": "/b", "name": "Nova"}, False),
    ({"profile": "/p", "base": "/b", "name": "Nova", "model_only": True}, True),
])
def test_the_app_asks_for_a_model_change_with_one_explicit_flag(monkeypatch, request_value, model_only):
    from types import SimpleNamespace
    from daemons_companion.desktop import protocol

    seen = {}
    monkeypatch.setattr(module, "run", lambda ui, profile, base, name, context, **kw: seen.update(kw) or True)
    events = []
    ui = SimpleNamespace(emit=lambda *a, **k: events.append((a, k)), finished=False)
    assert protocol.serve_provider_change(ui, request_value, SimpleNamespace(context=None)) == 0
    assert seen == {"model_only": model_only} and events[-1][1]["result"] == "installed"


@pytest.mark.parametrize("request_value", [
    {"profile": "/p", "base": "/b", "name": "Nova", "model_only": "yes"},
    {"profile": "/p", "base": "/b", "name": "Nova", "model_only": False},
    {"profile": "/p", "base": "/b", "name": "Nova", "other": True},
])
def test_any_other_model_change_request_is_refused(monkeypatch, request_value):
    from types import SimpleNamespace
    from daemons_companion.desktop import protocol

    monkeypatch.setattr(module, "run", lambda *a, **k: pytest.fail("must not run"))
    events = []
    ui = SimpleNamespace(emit=lambda *a, **k: events.append((a, k)), diagnostic=lambda *a: None, finished=False)
    assert protocol.serve_provider_change(ui, request_value, SimpleNamespace(context=None)) == 1
    assert events[-1][1]["result"] == "error"
