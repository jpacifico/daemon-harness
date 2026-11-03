"""Restart the actual wizard with owned, disposable on-disk checkpoints."""
import json
import os
from types import SimpleNamespace

import pytest

from daemons_companion.setup.progress import load, save
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.desktop.host_terminal import PackagedHostSetup
from daemons_companion.desktop.terminal import Back, Quit, run_wizard
from tests.test_companion_private_setup import owned
from tests.test_companion_openclaw_first import owned_host
from tests.test_companion_terminal import FakeUI, backend


@pytest.fixture(params=["hermes", "openclaw"])
def scope(tmp_path, monkeypatch, request):
    if request.param == "hermes":
        return owned(tmp_path, monkeypatch)[1]
    return InstallationScope(owned_host(tmp_path / "isolated").attempt)


def test_checkpoint_survives_new_owner_and_has_no_secrets(scope):
    save(scope, scope.host_kind, name="Avery", gender="feminine", language="fr",
         provider_kind="openai_api", model="synthetic-model")
    restarted = InstallationScope(scope.attempt)
    assert load(restarted, scope.host_kind)["name"] == "Avery"
    before = (scope.attempt / "setup-progress.json").read_bytes()
    with pytest.raises(ValueError):
        save(restarted, scope.host_kind, api_key="synthetic-secret")
    assert (scope.attempt / "setup-progress.json").read_bytes() == before
    assert "synthetic-secret" not in before.decode()


@pytest.mark.parametrize("fault", ["foreign", "host", "link", "hardlink", "oversize", "corrupt"])
def test_checkpoint_refuses_untrusted_state(scope, tmp_path, fault):
    save(scope, scope.host_kind, name="Avery")
    path = scope.attempt / "setup-progress.json"
    data = json.loads(path.read_text())
    if fault == "foreign":
        data["installation_id"] = "0" * 32
        path.write_text(json.dumps(data))
    elif fault == "host":
        data["host"] = "another-host"
        path.write_text(json.dumps(data))
    elif fault in {"link", "hardlink"}:
        other = tmp_path / "foreign.json"
        path.rename(other)
        if fault == "link":
            path.symlink_to(other)
        else:
            os.link(other, path)
    else:
        path.write_text("x" * (9000 if fault == "oversize" else 1))
    with pytest.raises(ValueError):
        load(scope, scope.host_kind)


class UI(FakeUI):
    def first_meeting_preferences(self):
        self.output.append("language")
        self.conversation_language = "fr"


@pytest.mark.parametrize("target", ["windows", "macos"])
@pytest.mark.parametrize("legacy", [False, True])
def test_real_wizard_restarts_after_host_without_acquisition(tmp_path, monkeypatch, target, legacy):
    layout, scope = owned(tmp_path, monkeypatch, target)
    if not legacy:
        save(scope, "hermes", name="Avery", gender="feminine", language="fr",
             provider_kind="openai_api", model="synthetic-model")
    owner = PackagedHostSetup(SimpleNamespace(target_os=target, companion_home=layout.companion_home))
    owner.layout = lambda: layout
    monkeypatch.setattr("daemons_companion.desktop.host_terminal.installed_host",
                        lambda _: SimpleNamespace(config_path=str(layout.profile)))
    # Exercise the real incomplete-receipt resume path and on-disk loader.
    monkeypatch.setattr("daemons_companion.hermes.first_install.installed_host",
                        lambda _: SimpleNamespace(config_path=str(layout.profile)))
    owner.offer = lambda *a: pytest.fail("Verified host must not be offered or acquired again")
    connected, completed = [], []
    owner.connect_subscription = lambda ui, host, kind: connected.append((host, kind)) or True
    def select(ui, previous, *, resume=False):
        assert resume == (not legacy)
        assert previous == ("" if legacy else "synthetic-model")
        return "synthetic-model"
    owner.choose_model = select
    owner.complete = lambda ui, request, response: completed.append(request) or True
    ui = UI(choices=[0, 1] if legacy else [], fields=["Avery"] if legacy else [])
    assert run_wizard(ui, backend=backend(), host_setup=owner, supported_only=True) == 0
    assert connected == [("hermes", "openai_api")]
    assert completed[0]["daemon_name"] == "Avery"
    assert completed[0]["provider"]["model"] == "synthetic-model"
    assert ui.conversation_language == "fr"
    assert "1. Choose your daemon's host" not in ui.output
    assert load(scope, "hermes")["name"] == "Avery"


def test_back_from_resumed_connection_allows_editing(tmp_path, monkeypatch):
    layout, scope = owned(tmp_path, monkeypatch)
    save(scope, "hermes", name="Avery", gender="feminine", language="fr",
         provider_kind="openai_api", model="synthetic-model")
    owner = PackagedHostSetup(SimpleNamespace(target_os="macos"))
    owner._progress_scope = scope
    owner.resume = lambda ui: False
    calls = []
    def connect(*args):
        calls.append(1)
        if len(calls) == 1:
            raise Back
        return True
    owner.connect_subscription = connect
    owner.choose_model = lambda ui, previous: "replacement-model"
    owner.complete = lambda *a: True
    ui = UI(choices=[2])
    assert run_wizard(ui, backend=backend(), host_setup=owner, supported_only=True) == 0
    assert "language" in ui.output
    assert load(scope, "hermes")["model"] == "replacement-model"


@pytest.mark.parametrize("target", ["macos", "windows"])
def test_openclaw_restart_keeps_identity_and_rechecks_connection(tmp_path, monkeypatch, target):
    from daemons_companion.openclaw import first_install as module
    host = owned_host(tmp_path / "owned", target)
    scope = InstallationScope(host.attempt)
    save(scope, "openclaw", name="Avery", gender="feminine", language="fr",
         provider_kind="openai_api", model="synthetic-model")
    monkeypatch.setattr(module, "acquire", lambda *a: (host, {}))
    seen = []
    def connect(owner, ui, current, saved):
        seen.append(saved)
        raise Quit
    monkeypatch.setattr(module, "connect_api", connect)
    ui = UI()
    ui.phase = lambda phase: None
    with pytest.raises(Quit):
        module._run(SimpleNamespace(context=None), ui)
    assert seen[0]["model"] == "synthetic-model"
    assert ui.conversation_language == "fr"
    assert "language" not in ui.output


def test_openclaw_back_from_resumed_api_key_can_change_provider(tmp_path, monkeypatch):
    from daemon_core import api_providers as api
    from daemons_companion.setup import api_setup as setup
    from daemons_companion.openclaw.first_install import connect_api
    host = owned_host(tmp_path / "owned")
    scope = InstallationScope(host.attempt)
    saved = dict(provider_kind="openai_api", model="old-model")
    save(scope, "openclaw", **saved)
    keys = iter([Back(), "synthetic-valid-key"])
    class SecretUI(UI):
        def secret(self, label):
            value = next(keys)
            if isinstance(value, Exception):
                raise value
            return value
    monkeypatch.setattr(api, "model_catalog", lambda kind, key: ("new-model",))
    calls = []
    monkeypatch.setattr(setup, "verify_model", lambda *args: calls.append(args))
    owner = SimpleNamespace(selected_host="openclaw")
    assert connect_api(owner, SecretUI(choices=[2, 0]), host, saved) == "new-model"
    assert calls == [("anthropic_api", "synthetic-valid-key", "new-model")]
    assert load(scope, "openclaw")["provider_kind"] == "anthropic_api"
    assert "synthetic-valid-key" not in (scope.attempt / "setup-progress.json").read_text()


@pytest.mark.parametrize("target", ["macos", "windows"])
@pytest.mark.parametrize("from_telegram", [False, True])
def test_resumed_openclaw_back_reaches_editable_language(tmp_path, monkeypatch, target, from_telegram):
    from daemons_companion.openclaw import first_install as first
    from daemons_companion.setup import telegram
    host = owned_host(tmp_path / "owned", target)
    scope = InstallationScope(host.attempt)
    save(scope, "openclaw", name="Avery", gender="feminine", language="fr",
         provider_kind="openai_api", model="synthetic-model")
    monkeypatch.setattr(first, "acquire", lambda *a: (host, {}))
    connections = []
    def connect(owner, ui, host, saved):
        connections.append(saved.copy())
        if len(connections) == 1 and from_telegram:
            owner.api_connection = SimpleNamespace(kind="openai_api")
            return "synthetic-model"
        if len(connections) <= (2 if from_telegram else 1):
            raise Back
        assert ui.conversation_language == "en"
        raise Quit
    monkeypatch.setattr(first, "connect_api", connect)
    monkeypatch.setattr(telegram, "prepare_telegram", lambda *a, **kw: (_ for _ in ()).throw(Back()))
    class EditingUI(UI):
        def first_meeting_preferences(self):
            self.conversation_language = "en"
            self.output.append("language")
    ui = EditingUI()
    ui.phase = lambda value: None
    owner = PackagedHostSetup(SimpleNamespace(companion_home=host.companion_home, target_os=target))
    with pytest.raises(Quit):
        run_wizard(ui, backend=backend(), host_setup=owner, supported_only=True)
    assert ui.output.count("language") == 1
    assert load(scope, "openclaw")["language"] == "en"
    assert not (scope.attempt / "daemon-setup.json").exists()
