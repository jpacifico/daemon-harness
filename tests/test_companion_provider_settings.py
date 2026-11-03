import json

import pytest
import yaml

import daemons_companion.desktop.provider_settings as module
from daemon_core.api_providers import ApiConnectionError, hermes_model_config, hermes_provider_entries
from daemons_companion.hosts.creation import native_config
from daemons_companion.setup.provider import _credential_payload, _json_bytes

OLD_KEY, NEW_KEY = "old-key-123", "new-key-456"
PROVIDER_REF = "provider-" + "1" * 8 + "-1111-1111-1111-" + "1" * 12
CONNECTION_REF = "provider-" + "2" * 8 + "-2222-2222-2222-" + "2" * 12


class Store:
    def __init__(self):
        self.values = {}

    def get(self, reference):
        return self.values.get(reference)

    def put(self, reference, value):
        self.values[reference] = value

    def delete(self, reference):
        self.values.pop(reference)


class Scope:
    def __init__(self, attempt):
        self.attempt = attempt

    installation_id = property(lambda self: self.attempt.name)
    home = property(lambda self: self.attempt / "daemon")
    profile = property(lambda self: self.attempt / "profile")
    host_kind = property(lambda self: self.attempt.parent.parent.name)

    def validate_profile(self, profile):
        assert profile == self.profile
        return profile


def provider_config(kind="mistral_api", model="mistral-large-4"):
    return {
        "schema": "daemon.companion-provider.v1", "kind": kind, "model": model, "base_url": "",
        "reasoning": {"routing": "medium", "conversation": "medium", "memory": "high"},
        "credential": None if kind == "codex_cli" else {"name": "MISTRAL_API_KEY", "reference": PROVIDER_REF},
    }


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "InstallationScope", Scope)
    monkeypatch.setattr(module, "model_catalog", lambda kind, key: ("mistral-large-4", "mistral-medium-3"))
    calls = []
    monkeypatch.setattr(module, "verify_model", lambda kind, key, model: calls.append((key, model)) or True)

    def build(host="openclaw", kind="mistral_api"):
        attempt = tmp_path / "hosts" / host / "installations" / ("a" * 32)
        (attempt / "profile").mkdir(parents=True)
        (attempt / "daemon/data/state/config").mkdir(parents=True)
        store = Store()
        (attempt / "connection.json").write_text(json.dumps(
            {"schema": "daemon.private-connection.v2", "provider_kind": kind, "user_id": "12345"}) + "\n")
        (attempt / "daemon/data/state/config/provider.json").write_bytes(_json_bytes(provider_config(kind)))
        store.put(PROVIDER_REF, _credential_payload("owner", OLD_KEY))
        if kind == "codex_cli":
            (attempt / "profile/openclaw.json").write_text("{}\n")
            (attempt / "openclaw-connection.json").write_text(
                json.dumps({"schema": "daemon.openclaw-connection.v1", "credential": CONNECTION_REF, "kind": kind}) + "\n")
        elif host == "openclaw":
            config = native_config(kind, "mistral-large-4", "123", 18789)
            config["meta"] = {"lastTouchedVersion": "host-owned"}
            (attempt / "profile/openclaw.json").write_text(json.dumps(config, sort_keys=True) + "\n")
            (attempt / "openclaw-connection.json").write_text(
                json.dumps({"schema": "daemon.openclaw-connection.v1", "credential": CONNECTION_REF, "kind": kind}) + "\n")
            store.put(CONNECTION_REF, json.dumps(
                {"installation_id": "a" * 32, "key": OLD_KEY, "token": "bot", "gateway": "gw"}))
        else:
            config = {"model": hermes_model_config(kind, "mistral-large-4"),
                      "providers": hermes_provider_entries(kind, "mistral-large-4"),
                      "platforms": {"telegram": {"enabled": True}}}
            (attempt / "profile/config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        owned = module.OwnedDaemon(attempt / "profile", store)
        owned.calls = calls
        return owned

    return build


def snapshot(owned):
    return {name: path.read_bytes() for name, path in owned.paths().items()}


def test_describe_reports_provider_model_and_key(daemon):
    owned = daemon()
    described = owned.describe()
    assert {k: described[k] for k in ("kind", "model", "editable", "key", "pending")} == {
        "kind": "mistral_api", "model": "mistral-large-4", "editable": True, "key": True, "pending": False}


def test_subscription_daemon_is_shown_but_not_editable(daemon):
    owned = daemon(kind="codex_cli")
    assert owned.describe()["editable"] is False
    assert owned.describe()["key"] is None


def test_changing_the_model_updates_daemon_and_host_together(daemon):
    owned = daemon()
    before = owned.revision()
    assert owned.change(before, "mistral-medium-3", None) == {"status": "applied"}
    config = json.loads(owned.host_config.read_text())
    assert config["agents"]["defaults"]["model"]["primary"] == "daemons-mistral/mistral-medium-3"
    assert config["meta"] == {"lastTouchedVersion": "host-owned"}
    assert json.loads(owned.provider_file.read_text())["model"] == "mistral-medium-3"
    assert owned.calls == [(OLD_KEY, "mistral-medium-3")]
    assert owned.commit() == {"status": "committed"}
    assert not owned.change_dir.exists() and owned.revision() != before
    assert set(owned.store.values) == {PROVIDER_REF, CONNECTION_REF}


def test_changing_the_key_replaces_both_credentials_after_commit(daemon):
    owned = daemon()
    owned.change(owned.revision(), "mistral-large-4", " " + NEW_KEY + " ")
    provider_ref = json.loads(owned.provider_file.read_text())["credential"]["reference"]
    connection_ref = json.loads(owned.connection.read_text())["credential"]
    assert provider_ref != PROVIDER_REF and connection_ref != CONNECTION_REF
    assert json.loads(owned.store.get(provider_ref))["value"] == NEW_KEY
    secret = json.loads(owned.store.get(connection_ref))
    assert secret == {"installation_id": "a" * 32, "key": NEW_KEY, "token": "bot", "gateway": "gw"}
    # The previous credentials stay until the restarted daemon is confirmed.
    assert PROVIDER_REF in owned.store.values and CONNECTION_REF in owned.store.values
    owned.commit()
    assert set(owned.store.values) == {provider_ref, connection_ref}


def test_revert_restores_exact_files_and_removes_new_credentials(daemon):
    owned = daemon()
    before, credentials = snapshot(owned), dict(owned.store.values)
    owned.change(owned.revision(), "mistral-medium-3", NEW_KEY)
    assert owned.describe()["pending"] is True
    assert owned.revert() == {"status": "reverted"}
    assert snapshot(owned) == before and owned.store.values == credentials
    assert not owned.change_dir.exists()


def test_a_failure_during_the_change_leaves_nothing_behind(daemon):
    owned = daemon()
    before, credentials = snapshot(owned), dict(owned.store.values)
    put = owned.store.put

    def failing(reference, value):
        if len(owned.store.values) == 3:
            raise OSError("credential store refused")
        put(reference, value)

    owned.store.put = failing
    with pytest.raises(OSError):
        owned.change(owned.revision(), "mistral-medium-3", NEW_KEY)
    assert snapshot(owned) == before and owned.store.values == credentials
    assert not owned.change_dir.exists()


def test_an_unfinished_change_blocks_another_until_reverted(daemon):
    owned = daemon()
    owned.change(owned.revision(), "mistral-medium-3", None)
    with pytest.raises(module.ProviderSettingsError, match="provider.pending"):
        owned.change(owned.revision(), "mistral-large-4", None)
    owned.revert()
    assert owned.change(owned.revision(), "mistral-medium-3", None)["status"] == "applied"


def test_refusals_change_nothing(daemon, monkeypatch):
    owned = daemon()
    before = snapshot(owned)
    with pytest.raises(module.ProviderSettingsError, match="provider.changed"):
        owned.change("0" * 64, "mistral-medium-3", None)
    with pytest.raises(module.ProviderSettingsError, match="provider.model-unavailable"):
        owned.change(owned.revision(), "mistral-tiny", None)

    def rejected(kind, key):
        raise ApiConnectionError("The provider rejected this API key or its permissions. Check the key and try again.")

    monkeypatch.setattr(module, "model_catalog", rejected)
    with pytest.raises(ApiConnectionError):
        owned.change(owned.revision(), "mistral-medium-3", NEW_KEY)
    assert snapshot(owned) == before and not owned.change_dir.exists()


def test_same_key_and_model_is_unchanged(daemon):
    owned = daemon()
    assert owned.change(owned.revision(), "mistral-large-4", OLD_KEY) == {"status": "unchanged"}
    assert not owned.change_dir.exists()


def test_a_host_configuration_edited_elsewhere_is_not_editable(daemon):
    owned = daemon()
    config = json.loads(owned.host_config.read_text())
    config["agents"]["defaults"]["model"]["primary"] = "elsewhere/other"
    owned.host_config.write_text(json.dumps(config, sort_keys=True) + "\n")
    assert owned.describe()["editable"] is False
    with pytest.raises(module.ProviderSettingsError, match="provider.unsupported"):
        owned.change(owned.revision(), "mistral-medium-3", None)


def test_hermes_model_change_keeps_other_settings(daemon):
    owned = daemon(host="hermes")
    assert owned.describe()["editable"] is True
    owned.change(owned.revision(), "mistral-medium-3", NEW_KEY)
    config = yaml.safe_load(owned.host_config.read_text())
    assert config["model"] == hermes_model_config("mistral_api", "mistral-medium-3")
    assert config["providers"] == hermes_provider_entries("mistral_api", "mistral-medium-3")
    assert config["platforms"] == {"telegram": {"enabled": True}}
    owned.commit()
    reference = json.loads(owned.provider_file.read_text())["credential"]["reference"]
    assert set(owned.store.values) == {reference}


def test_check_uses_the_saved_key_or_a_new_one(daemon, monkeypatch):
    owned = daemon()
    seen = []
    monkeypatch.setattr(module, "model_catalog", lambda kind, key: seen.append(key) or ("mistral-large-4",))
    assert owned.check(None) == {"models": ["mistral-large-4"], "available": True}
    owned.check(NEW_KEY)
    assert seen == [OLD_KEY, NEW_KEY]


def test_requests_accept_only_known_fields(tmp_path):
    for request in ({"action": "describe"}, {"action": "describe", "profile": str(tmp_path), "key": "x"},
                    {"action": "delete", "profile": str(tmp_path)}):
        with pytest.raises(module.ProviderSettingsError, match="provider.invalid-request"):
            module.handle_request(request, store=Store())


def test_user_installed_profiles_are_refused(tmp_path):
    profile = tmp_path / "hermes-home"
    profile.mkdir()
    with pytest.raises(ValueError):
        module.handle_request({"action": "describe", "profile": str(profile)}, store=Store())


def test_main_reports_codes_without_secrets(monkeypatch, capsys):
    import io

    request = {"action": "check", "profile": "/nonexistent/profile", "key": NEW_KEY}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    module.main()
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False and NEW_KEY not in output and "nonexistent" not in output


class Scheduler:
    """Services as the removal captures them; records what was stopped and restored."""

    def __init__(self, services):
        self.services, self.calls = dict(services), []

    def capture(self, code):
        return self.services.get(code)

    def restore(self, code, capture):
        self.calls.append((code, capture))
        self.services[code] = capture


SERVICES = {"gateway": b"<gateway/>", "worker": b"<worker/>", "nightly": None}


def catalogs(monkeypatch):
    monkeypatch.setattr(module, "model_catalog", lambda kind, key: {
        "openai_api": ("gpt-6-astra", "gpt-6.1-sol"), "anthropic_api": ("claude-opus-5-5",),
        "mistral_api": ("mistral-large-4",)}[kind])


def codex_binary(owned):
    binary = owned.scope.attempt / "codex/0.159.2/codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic codex")
    return binary


def test_describe_offers_the_same_providers_on_both_hosts(daemon):
    hermes, openclaw = daemon(host="hermes"), daemon(host="openclaw")
    assert hermes.describe()["offers"] == ["codex_cli", "openai_api", "anthropic_api"]
    assert openclaw.describe()["offers"] == ["codex_cli", "openai_api", "anthropic_api"]
    assert hermes.describe()["switchable"] and openclaw.describe()["switchable"]


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_switching_to_another_api_provider_rewrites_everything_it_reads(daemon, monkeypatch, host):
    catalogs(monkeypatch)
    owned = daemon(host=host)
    before = snapshot(owned)
    assert owned.switch(owned.revision(), "anthropic_api", "claude-opus-5-5", " " + NEW_KEY + " ", None,
                        SERVICES) == {"status": "applied"}
    provider = json.loads(owned.provider_file.read_text())
    assert provider["kind"] == "anthropic_api" and provider["model"] == "claude-opus-5-5"
    assert provider["credential"]["name"] == "ANTHROPIC_API_KEY"
    assert json.loads(owned.store.get(provider["credential"]["reference"]))["value"] == NEW_KEY
    assert json.loads(owned.private_connection.read_text()) == {
        "schema": "daemon.private-connection.v2", "provider_kind": "anthropic_api", "user_id": "12345"}
    assert owned.host_matches("anthropic_api", "claude-opus-5-5")
    assert owned.calls == [(NEW_KEY, "claude-opus-5-5")]
    if host == "hermes":
        config = yaml.safe_load(owned.host_config.read_text())
        assert config["model"] == hermes_model_config("anthropic_api", "claude-opus-5-5")
        assert config["providers"] == {} and config["platforms"] == {"telegram": {"enabled": True}}
    else:
        config = json.loads(owned.host_config.read_text())
        assert list(config["models"]["providers"]) == ["daemons-anthropic"]
        assert config["meta"] == {"lastTouchedVersion": "host-owned"}
        record = json.loads(owned.connection.read_text())
        assert record["kind"] == "anthropic_api" and record["credential"] != CONNECTION_REF
        assert json.loads(owned.store.get(record["credential"])) == {
            "installation_id": "a" * 32, "key": NEW_KEY, "token": "bot", "gateway": "gw"}
    # Nothing old is removed before the person keeps the change.
    assert PROVIDER_REF in owned.store.values
    owned.commit()
    assert PROVIDER_REF not in owned.store.values and CONNECTION_REF not in owned.store.values
    assert snapshot(owned) != before


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_switching_to_chatgpt_records_the_private_codex_and_no_key(daemon, monkeypatch, host):
    owned = daemon(host=host)
    binary = codex_binary(owned)
    owned.switch(owned.revision(), "codex_cli", "gpt-6-astra", None, binary, SERVICES)
    provider = json.loads(owned.provider_file.read_text())
    assert provider["kind"] == "codex_cli" and provider["credential"] is None
    connection = json.loads(owned.private_connection.read_text())
    assert connection["schema"] == "daemon.private-connection.v1" and connection["codex"] == str(binary)
    assert connection["user_id"] == "12345"
    assert owned.host_matches("codex_cli", "gpt-6-astra")
    if host == "hermes":
        config = yaml.safe_load(owned.host_config.read_text())
        # The custom entry written for the previous provider goes with it.
        assert config["model"] == {"provider": "openai-codex", "default": "gpt-6-astra"}
        assert config["providers"] == {}
    else:
        config = json.loads(owned.host_config.read_text())
        assert list(config["models"]["providers"]) == ["openai"]
        assert config["tools"]["web"] == native_config("codex_cli", "gpt-6-astra", "1", 1)["tools"]["web"]
        record = json.loads(owned.connection.read_text())
        assert record["kind"] == "codex_cli" and json.loads(owned.store.get(record["credential"]))["key"] == ""
    owned.commit()
    assert PROVIDER_REF not in owned.store.values


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_going_back_restores_files_credentials_and_services_exactly(daemon, monkeypatch, host):
    catalogs(monkeypatch)
    owned = daemon(host=host)
    before, credentials = snapshot(owned), dict(owned.store.values)
    scheduler = Scheduler(SERVICES)
    owned.switch(owned.revision(), "openai_api", "gpt-6-astra", NEW_KEY, None, SERVICES)
    with pytest.raises(module.ProviderSettingsError, match="recovery"):
        owned.revert()  # Services were captured: a revert must restart them.
    assert owned.revert(scheduler) == {"status": "reverted"}
    assert snapshot(owned) == before and owned.store.values == credentials
    assert not owned.change_dir.exists()
    # Stopped gateway first, then restarted in reverse order with the captured definitions.
    assert scheduler.calls == [("gateway", None), ("worker", None), ("nightly", None),
                               ("nightly", None), ("worker", b"<worker/>"), ("gateway", b"<gateway/>")]


def test_a_switch_failure_restores_files_and_leaves_services_to_the_caller(daemon, monkeypatch):
    catalogs(monkeypatch)
    owned = daemon(host="openclaw")
    before, credentials = snapshot(owned), dict(owned.store.values)

    def fail(kind, model, user, port):
        raise RuntimeError("synthetic")

    real = module.native_config
    monkeypatch.setattr(module, "native_config", lambda *a: fail(*a) if a[0] == "openai_api" else real(*a))
    with pytest.raises(RuntimeError):
        owned.switch(owned.revision(), "openai_api", "gpt-6-astra", NEW_KEY, None, SERVICES)
    assert snapshot(owned) == before and owned.store.values == credentials and not owned.change_dir.exists()


def test_switch_refusals_change_nothing(daemon, monkeypatch, tmp_path):
    catalogs(monkeypatch)
    owned = daemon(host="hermes")
    before = snapshot(owned)
    revision = owned.revision()
    outside = tmp_path / "codex"
    outside.write_bytes(b"x")
    attempts = [
        ("mistral_api", "mistral-large-4", NEW_KEY, None),     # its own provider: a model change
        ("xai_api", "grok-4.7", NEW_KEY, None),               # not offered to new daemons
        ("openai_api", "gpt-5.6-terra", NEW_KEY, None),       # not in the approved catalog
        ("codex_cli", "gpt-6-astra", None, outside),          # Codex outside the installation
        ("codex_cli", "gpt-5.6-terra", None, codex_binary(owned)),  # unapproved subscription model
    ]
    for kind, model, key, codex in attempts:
        with pytest.raises((module.ProviderSettingsError, ValueError)):
            owned.switch(revision, kind, model, key, codex, SERVICES)
        assert snapshot(owned) == before and not owned.change_dir.exists()
    with pytest.raises(module.ProviderSettingsError, match="changed"):
        owned.switch("0" * 64, "openai_api", "gpt-6-astra", NEW_KEY, None, SERVICES)


def test_revert_request_needs_the_base_after_a_switch(daemon, monkeypatch):
    catalogs(monkeypatch)
    owned = daemon(host="hermes")
    owned.switch(owned.revision(), "openai_api", "gpt-6-astra", NEW_KEY, None, SERVICES)
    monkeypatch.setattr(module, "OwnedDaemon", lambda profile, store=None: owned)
    with pytest.raises(module.ProviderSettingsError, match="recovery"):
        module.handle_request({"action": "revert", "profile": str(owned.scope.profile)})
    scheduler = Scheduler(SERVICES)
    monkeypatch.setattr(module, "service_scheduler", lambda profile, base: type("R", (), {"scheduler": scheduler})())
    monkeypatch.setattr(module, "memory_file_try_lock", lambda path: __import__("contextlib").nullcontext(True))
    assert module.handle_request({"action": "revert", "profile": str(owned.scope.profile), "base": "/base"}) == {
        "status": "reverted"}
