"""Subscription setup must not masquerade as API access or change the source."""

import json
from types import SimpleNamespace
import pytest
from test_companion_host_creation import setup  # noqa: F401,F811
from daemons_companion.hosts import creation
from daemons_companion.openclaw import subscription
from daemons_companion.setup.plan import (
    ProviderKind,
    ProviderSelection,
    SetupRequest,
    HostKind,
    HostProbe,
    build_setup_plan,
)


def test_subscription_configuration_keeps_native_host_runtime():
    cfg = creation.native_config("codex_cli", "gpt-6-astra", "777001", 18901)
    assert cfg["agents"]["defaults"]["model"]["primary"] == "openai/gpt-6-astra"
    provider = cfg["models"]["providers"]["openai"]
    assert provider["auth"] == "oauth"
    assert provider["agentRuntime"] == {"id": "openclaw"}
    assert "API_KEY" not in json.dumps(cfg)
    selection = ProviderSelection(ProviderKind.CODEX_CLI, "gpt-6-astra")
    probe = HostProbe(
        HostKind.OPENCLAW,
        True,
        True,
        "/synthetic/bin",
        "/synthetic/profile",
        "2026.9.1-beta.1",
        "verified",
    )
    build_setup_plan(
        SetupRequest(HostKind.OPENCLAW, "Aster", "feminine", selection),
        (probe,),
        codex_available=True,
    )


def test_added_host_subscription_preserves_source_and_has_no_api_key(
    setup,  # noqa: F811
    monkeypatch,
):  # noqa: F811
    host, source, binding, store, factory, memory = setup
    original = memory.read_bytes(), (source / ".env").read_bytes()
    calls = []

    def connect(layout, emit=lambda _: None, **kwargs):
        assert layout == host
        calls.append(kwargs)
        emit({"code": "TEST-0000"})
        emit(
            {
                "catalog": {
                    "status": "ready",
                    "raw_catalog_observed": False,
                    "native_count": 2,
                    "excluded_count": 0,
                    "models": [
                        {
                            "id": m.identifier,
                            "status": "absent-native" if i == 1 else "offered",
                        }
                        for i, m in enumerate(subscription.SUPPORTED_MODELS)
                    ],
                }
            }
        )
        return ("gpt-6-astra", "gpt-6.1-sol")

    monkeypatch.setattr(subscription, "connect", connect)
    revision = factory().start(18901)["revision"]
    factory().perform("download", revision, {})
    result = factory().perform("key", revision, {"kind": "codex_cli"})
    assert result["phase"] == "model" and "key" not in result
    assert result["catalog"]["models"][1]["status"] == "absent-native"
    assert factory().current()["catalog"] == result["catalog"]
    assert "key" not in json.loads(next(iter(store.values.values())))
    refreshed = factory().perform("key", revision, {"kind": "codex_cli"})
    assert refreshed["catalog"] == result["catalog"]
    assert refreshed["phase"] == "model"
    factory().perform("model", revision, {"model": "gpt-6-astra"})
    result = factory().perform("telegram", revision, {"token": "987654:" + "b" * 35})
    assert result["phase"] == "ready"
    assert set(creation.credential_environment(host, store=store)) == {
        "TELEGRAM_BOT_TOKEN",
        "DAEMONS_GATEWAY_TOKEN",
    }
    assert calls == [{}, {}, {"catalog_only": True}]
    assert (memory.read_bytes(), (source / ".env").read_bytes()) == original


def test_failed_subscription_does_not_advance_and_api_retry_works(setup, monkeypatch):  # noqa: F811
    host, source, binding, store, factory, memory = setup

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret must not be recorded")

    monkeypatch.setattr(subscription, "connect", fail)
    revision = factory().start(18901)["revision"]
    factory().perform("download", revision, {})
    with pytest.raises(RuntimeError):
        factory().perform("key", revision, {"kind": "codex_cli"})
    assert factory().current()["phase"] == "key"
    assert "synthetic-secret" not in creation.record_path(host).read_text()
    assert not store.values
    result = factory().perform(
        "key", revision, {"kind": "mistral_api", "key": "synthetic-key"}
    )
    assert result["phase"] == "model"


def test_both_hosts_recommend_the_chatgpt_subscription_first():
    from daemons_companion.setup.model_access import options

    kinds, labels = options()
    assert kinds == ("codex_cli", "openai_api", "anthropic_api")
    assert "(recommended)" in labels[0]
    assert not any("recommended" in label for label in labels[1:])


def test_subscription_path_sets_no_api_secret(tmp_path, monkeypatch):
    from test_companion_openclaw_first import owned_host
    from daemons_companion.openclaw import first_install as first
    from daemons_companion.runtime.installation_scope import InstallationScope

    host = owned_host(tmp_path / "owned")
    InstallationScope(host.attempt).initialize()
    owner = SimpleNamespace(api_connection=None)

    class UI:
        def say(self, text):
            pass

        def choose(self, title, options, **kwargs):
            return options.index("ChatGPT subscription (recommended) — sign in with your account")

    monkeypatch.setattr(
        subscription,
        "first_connection",
        lambda *args: (host.attempt / "codex", "gpt-6-astra"),
    )
    assert first.connect_api(owner, UI(), host) == "gpt-6-astra"
    assert owner.api_connection.kind == "codex_cli" and not owner.api_connection.key
    assert owner.openclaw_codex == host.attempt / "codex"


def test_catalog_evidence_has_fixed_ids_and_no_private_metadata():
    from copy import deepcopy

    valid = {
        "status": "ready",
        "raw_catalog_observed": False,
        "native_count": 2,
        "excluded_count": 1,
        "models": [
            {"id": m.identifier, "status": "offered" if i == 1 else "absent-native"}
            for i, m in enumerate(subscription.SUPPORTED_MODELS)
        ],
    }
    assert subscription.valid_catalog(valid)
    for key, value in (
        ("native_count", True),
        ("excluded_count", -1),
        ("raw_catalog_observed", True),
        ("status", "secret"),
        ("models", []),
    ):
        altered = deepcopy(valid)
        altered[key] = value
        assert not subscription.valid_catalog(altered)
    altered = deepcopy(valid)
    altered["models"][0]["token"] = "secret"
    assert not subscription.valid_catalog(altered)
    altered = deepcopy(valid)
    altered["models"][0]["id"] = "secret"
    assert not subscription.valid_catalog(altered)


@pytest.mark.parametrize("retry_choice", [0, 1, 2])
def test_first_retry_stays_explicit_and_preserves_provider(tmp_path, monkeypatch, retry_choice):
    from test_companion_openclaw_first import owned_host
    from daemons_companion.openclaw import first_install as first
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.desktop.terminal import Quit
    host = owned_host(tmp_path / "owned")
    InstallationScope(host.attempt).initialize()
    owner = SimpleNamespace(api_connection=None)
    prompts, messages, attempts = [], [], []

    def connection(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise subscription.SubscriptionFailure("catalog")
        return host.attempt / "codex", "gpt-6.1-sol"

    class UI:
        def choose(self, title, options, **kwargs):
            prompts.append(title)
            if title == "ChatGPT setup needs attention":
                return retry_choice
            return 0
        def say(self, text):
            messages.append(text)

    monkeypatch.setattr(subscription, "first_connection", connection)
    if retry_choice == 2:
        with pytest.raises(Quit):
            first.connect_api(owner, UI(), host)
        assert owner.api_connection is None
    else:
        assert first.connect_api(owner, UI(), host) == "gpt-6.1-sol"
        assert prompts.count("3. Model access to prepare") == (2 if retry_choice else 1)
    assert any("ChatGPT is connected" in text for text in messages)
    assert not any("sign-in could not be completed" in text for text in messages)


def test_first_model_refresh_reloads_native_catalog(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from test_companion_openclaw_first import owned_host
    from daemons_companion.setup import codex_install
    host = owned_host(tmp_path / "owned")
    calls, prompts = [], []
    monkeypatch.setattr(codex_install, "install_private_codex", lambda *a: host.attempt / "codex")
    monkeypatch.setattr(subscription, "environment", lambda h: {})

    def connect(h, emit, **kwargs):
        calls.append(kwargs)
        return ("gpt-6.1-sol",) if len(calls) == 1 else ("gpt-6-astra",)

    class UI:
        cancelled = False
        def subscription_runner(self, *args, **kwargs):
            return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        def phase(self, name):
            assert name == "model"
        def emit(self, *args, **kwargs):
            pass
        def say(self, text):
            pass
        @contextmanager
        def activity(self, text):
            yield lambda _: None
        def choose(self, title, options, **kwargs):
            prompts.append(options)
            return 1 if len(prompts) == 1 else 0

    monkeypatch.setattr(subscription, "connect", connect)
    assert subscription.first_connection(host, UI(), {})[1] == "gpt-6-astra"
    assert len(calls) == 2 and calls[1]["catalog_only"] is True
    assert prompts[0][0].startswith("GPT-6.1-Sol")
    assert prompts[1][0].startswith("Astra")


@pytest.mark.parametrize("complete", [False, True])
def test_bridge_completion_distinguishes_login_from_result(monkeypatch, complete):
    import io
    frames = [{"stage": "authenticated"}, {"stage": "catalog"}]
    if complete:
        frames.append({"models": ["gpt-6.1-sol"]})
    process = SimpleNamespace(
        stdin=io.BytesIO(), stdout=io.BytesIO(b"".join(json.dumps(f).encode()+b"\n" for f in frames)),
        returncode=0, poll=lambda: 0,
    )
    monkeypatch.setattr(subscription, "environment", lambda h: {})
    monkeypatch.setattr(subscription.subprocess, "Popen", lambda *a, **kw: process)
    host = SimpleNamespace(node="synthetic-node", entry=__import__('pathlib').Path('/synthetic/host/entry'), profile='/synthetic/profile', attempt='/synthetic')
    if complete:
        assert subscription.connect(host) == ("gpt-6.1-sol",)
    else:
        with pytest.raises(subscription.SubscriptionFailure) as raised:
            subscription.connect(host)
        assert raised.value.stage == 'catalog'
        assert str(raised.value).startswith('ChatGPT is connected')


@pytest.mark.parametrize("kind", ["codex_cli", "openai_api", "mistral_api", "anthropic_api"])
def test_new_host_receives_reference_conversation_settings(tmp_path, kind):
    from daemons_companion.openclaw.profile import _load_config, _render_config

    model = "gpt-6-astra" if kind in {"codex_cli", "openai_api"} else "model-x"
    cfg = creation.native_config(kind, model, "777001", 18901)
    projected = _load_config(
        _render_config(
            json.dumps(cfg).encode(),
            plugin_source=tmp_path / "plugin",
            workspace=tmp_path / "workspace",
            user_id="777001",
            daemon_url="http://127.0.0.1:18902",
            runtime_health_path=tmp_path / "health.json",
        )
    )
    for config in (cfg, projected):
        assert config["agents"]["defaults"]["heartbeat"] == {"every": "0m"}
        assert config["messages"] == {
            "visibleReplies": "automatic",
            "ackReactionScope": "off",
            "statusReactions": {"enabled": False},
        }
        assert config["session"] == {"dmScope": "per-channel-peer"}
        assert config["tools"]["profile"] == "coding"
        # Host commands wait for the owner's approval in the Telegram DM.
        assert config["tools"]["exec"] == {"mode": "ask"}
        assert config["channels"]["telegram"]["execApprovals"] == {
            "enabled": True,
            "approvers": ["777001"],
            "target": "dm",
        }
        assert config["channels"]["telegram"]["streaming"] == {
            "mode": "progress",
            "preview": {"toolProgress": True, "commandText": "status"},
            "progress": {"commentary": True, "label": False},
        }
    assert projected["tools"]["alsoAllow"] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
        "browser",
    ]
    assert "actions" not in projected["channels"]["telegram"]
    hosted = {"enabled": True, "openaiCodex": {"enabled": True, "mode": "live"}}
    assert projected["tools"].get("web", {}).get("search") == (
        hosted if kind == "codex_cli" else None
    )
