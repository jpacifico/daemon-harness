from contextlib import contextmanager
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from daemon_core import api_providers as api
from daemons_companion.setup.api_setup import ApiConnection, choose_api_model
from daemons_companion.setup.plan import ProviderKind, ProviderSelection
from daemons_companion.setup.provider import CompanionProviderAdapter
from daemons_companion.setup.provider_runtime import (
    ManagedProviderLoader,
    PROVIDER_CONFIG_ENV,
)
from daemons_companion.setup.transaction import TransactionApplyError
from tests.test_companion_provider import MemoryCredentialStore, _layout, _execute


MODELS = {
    "mistral_api": "mistral-large-4",
    "openai_api": "gpt-6-astra",
    "anthropic_api": "claude-opus-5-5",
    "xai_api": "grok-4.7",
}


@pytest.mark.parametrize("kind", MODELS)
def test_account_catalog_and_generation_probe(kind):
    calls = []

    def request(provider, key, path, payload=None):
        assert key == "synthetic-key"
        calls.append((path, payload))
        if path == "/models":
            return {
                "data": [
                    {
                        "id": MODELS[kind],
                        "capabilities": {
                            "completion_chat": True,
                            "function_calling": True,
                        },
                    },
                    {
                        "id": "embedding-model",
                        "capabilities": {"completion_chat": False},
                    },
                ]
            }
        if kind == "anthropic_api":
            return {
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "OK"},
                ],
                "stop_reason": "end_turn",
            }
        return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    assert api.model_catalog(kind, "synthetic-key", request=request) == (MODELS[kind],)
    assert api.verify_model(kind, "synthetic-key", MODELS[kind], request=request)
    messages = calls[-1][1]["messages"]
    assert messages[-1] == {"role": "user", "content": "Reply with OK."}
    assert [m["role"] for m in messages] == (
        ["system", "system", "user"] if kind == "mistral_api" else ["user"]
    )
    assert "synthetic-key" not in json.dumps(calls)
    assert calls[-1][0] == (
        "/messages" if kind == "anthropic_api" else "/chat/completions"
    )


def test_mistral_recommendation_uses_only_catalog_alias():
    def request(*args):
        return {
            "data": [
                {
                    "id": "mistral-large-2610",
                    "aliases": ["mistral-large-4"],
                    "capabilities": {"completion_chat": True, "function_calling": True},
                }
            ]
        }

    assert (
        api.model_catalog("mistral_api", "synthetic", request=request)[0]
        == "mistral-large-4"
    )
    with pytest.raises(api.ApiConnectionError, match="No supported"):
        api.model_catalog("openai_api", "synthetic", request=request)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 422, 429, 500])
def test_http_errors_never_echo_provider_body_or_key(status):
    class Opener:
        def open(self, request, **kwargs):
            assert request.full_url == "https://api.mistral.ai/v1/models"
            assert request.get_header("Authorization") == "Bearer synthetic-secret"
            raise HTTPError(
                request.full_url,
                status,
                "synthetic-secret",
                {},
                io.BytesIO(b"synthetic-secret"),
            )

    with pytest.raises(api.ApiConnectionError) as failure:
        api.api_request("mistral_api", "synthetic-secret", "/models", opener=Opener())
    assert "synthetic-secret" not in str(failure.value)
    assert failure.value.__suppress_context__
    assert (
        api._NoRedirects().redirect_request(
            None, None, 302, None, None, "https://evil.invalid"
        )
        is None
    )


@pytest.mark.parametrize(
    "key", ["", "a\nb", "contains spaces", "é", "x" * 1501, "\x7f"]
)
def test_key_validation_is_safe(key):
    with pytest.raises(api.ApiConnectionError):
        api.validate_key(key)


def test_anthropic_pagination_and_loop_rejection():
    calls = []

    def request(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            return {"data": [], "has_more": True, "last_id": "cursor"}
        return {"data": [{"id": "claude-opus-5-5"}], "has_more": False}

    assert api.model_catalog("anthropic_api", "key", request=request) == (
        "claude-opus-5-5",
    )
    assert calls == ["/models", "/models?limit=100&after_id=cursor"]
    with pytest.raises(api.ApiConnectionError):
        api.model_catalog(
            "anthropic_api",
            "key",
            request=lambda *args: {"data": [], "has_more": True, "last_id": "same"},
        )


@pytest.mark.parametrize("kind", MODELS)
def test_all_faculties_use_transaction_secret_and_selected_model(
    tmp_path, monkeypatch, kind
):
    from daemon_core.providers import factory

    layout, store = _layout(tmp_path), MemoryCredentialStore()
    selection = ProviderSelection(
        ProviderKind(kind), MODELS[kind], credential_available=True
    )
    _execute(
        tmp_path,
        CompanionProviderAdapter(
            layout,
            selection=selection,
            credential_store=store,
            credential_value="synthetic-protected",
        ),
    )
    config = ManagedProviderLoader(store_factory=lambda: store).load(
        {PROVIDER_CONFIG_ENV: str(layout.provider_file)}
    )
    monkeypatch.setattr(factory, "managed_provider_config", lambda: config)
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-inherited-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-inherited-key")
    monkeypatch.setenv("MISTRAL_API_KEY", "wrong-inherited-key")
    monkeypatch.setenv("XAI_API_KEY", "wrong-inherited-key")
    built = [
        builder()
        for builder in (
            factory.build_provider,
            factory.build_routing_provider,
            factory.build_conversation_provider,
            factory.build_memory_provider,
        )
    ]
    assert [p.model for p in built] == [MODELS[kind]] * 4
    assert [p.reasoning_effort for p in built] == [None, "medium", "medium", "high"]
    assert config.secret(api.API_PROVIDERS[kind].secret_name) == "synthetic-protected"
    assert "synthetic-protected" not in layout.provider_file.read_text()
    assert all(
        "synthetic-protected" not in p.read_text()
        for p in (tmp_path / "transactions").rglob("*.json")
    )


@pytest.mark.parametrize("kind", MODELS)
@pytest.mark.parametrize("boundary", ["credential", "config"])
def test_failed_api_installation_removes_only_its_owned_key(tmp_path, kind, boundary):
    layout, store = _layout(tmp_path), MemoryCredentialStore()
    store.values["unrelated"] = "preserve"

    def fail(where):
        if where == boundary:
            raise RuntimeError("synthetic failure")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionProviderAdapter(
                layout,
                selection=ProviderSelection(
                    ProviderKind(kind), MODELS[kind], credential_available=True
                ),
                credential_store=store,
                credential_value="synthetic-protected",
                mutation_hook=fail,
            ),
        )
    assert store.values == {"unrelated": "preserve"}
    assert not layout.provider_file.exists()


def test_anthropic_preserves_exact_instruction_and_dialogue_text(monkeypatch):
    from daemon_core.providers.anthropic_provider import AnthropicProvider

    seen = []

    def request(kind, key, path, payload):
        seen.append(payload)
        return {
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "result"},
            ],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr("daemon_core.providers.anthropic_provider.api_request", request)
    messages = [
        {"role": "system", "content": "exact system"},
        {"role": "developer", "content": "exact developer"},
        {"role": "user", "content": "exact user"},
    ]
    provider = AnthropicProvider("claude-opus-5-5", "high", "synthetic")
    assert provider.complete(messages) == "result"
    assert seen[0]["system"] == [
        {"type": "text", "text": "exact system"},
        {"type": "text", "text": "exact developer"},
    ]
    assert seen[0]["messages"] == messages[-1:]
    assert seen[0]["output_config"] == {"effort": "high"}
    assert "temperature" not in seen[0]
    with pytest.raises(api.ApiConnectionError):
        api.completion_text(
            "anthropic_api",
            {
                "content": [{"type": "text", "text": "truncated"}],
                "stop_reason": "max_tokens",
            },
        )


@pytest.mark.parametrize(
    "kind,data",
    [
        ("anthropic_api", {"stop_reason": "end_turn", "content": [None]}),
        ("mistral_api", {"choices": [None]}),
        ("openai_api", {"choices": [{"finish_reason": "stop", "message": None}]}),
    ],
)
def test_malformed_completion_stays_in_safe_retry_boundary(kind, data):
    with pytest.raises(api.ApiConnectionError, match="complete text response"):
        api.completion_text(kind, data)


class UI:
    def __init__(self, choices, secrets=()):
        self.choices, self.secrets = iter(choices), iter(secrets)
        self.output = []

    def say(self, text):
        self.output.append(text)

    def secret(self, prompt):
        self.output.append(prompt)
        return next(self.secrets)

    def choose(self, title, options, default=0):
        self.output.append(title)
        result = next(self.choices)
        if isinstance(result, BaseException):
            raise result
        return result

    @contextmanager
    def activity(self, label):
        self.output.append(label)
        yield self.say


def test_invalid_key_can_be_reentered_without_restarting_setup(tmp_path, monkeypatch):
    from daemons_companion.setup.api_setup import connect_api
    from tests.test_companion_private_setup import owned

    layout, _ = owned(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "daemons_companion.setup.api_setup.installed_host",
        lambda _: SimpleNamespace(config_path=str(layout.profile / "config.yaml")),
    )
    checks = []

    def catalog(kind, key):
        checks.append(key)
        if key == "wrong-secret":
            raise api.ApiConnectionError("Rejected key.")
        return ("mistral-large-4",)

    monkeypatch.setattr("daemons_companion.setup.api_setup.model_catalog", catalog)
    owner = SimpleNamespace(layout=lambda: layout)
    ui = UI([0], ["wrong-secret", "accepted-secret"])
    assert connect_api(owner, ui, "hermes", "mistral_api")
    assert checks == ["wrong-secret", "accepted-secret"]
    assert owner.api_connection.verified_model == ""
    assert "secret" not in repr(owner.api_connection)
    assert all(
        "wrong-secret" not in text and "accepted-secret" not in text
        for text in ui.output
    )


def test_failed_model_probe_does_not_mark_connection_ready(monkeypatch):
    from daemons_companion.desktop.terminal import Quit

    owner = SimpleNamespace(
        api_connection=ApiConnection(
            "mistral_api", "/synthetic", "key", ("mistral-large-4",)
        )
    )

    def fail(*args):
        raise api.ApiConnectionError("Quota exceeded.")

    monkeypatch.setattr("daemons_companion.setup.api_setup.verify_model", fail)
    with pytest.raises(Quit):
        choose_api_model(owner, UI([0, 3]))
    assert not owner.api_connection.verified_model


@pytest.mark.parametrize("kind", MODELS)
def test_private_service_environment_needs_no_codex_and_never_inherits_key(
    tmp_path, monkeypatch, kind
):
    import yaml
    from tests.test_companion_private_setup import owned
    from daemons_companion.runtime.private_environment import private_process_environment
    from daemons_companion.setup.provider_runtime import ManagedProviderConfig

    layout, scope = owned(tmp_path, monkeypatch)
    model, spec = MODELS[kind], api.API_PROVIDERS[kind]
    (scope.attempt / "connection.json").write_text(
        json.dumps(
            {
                "schema": "daemon.private-connection.v2",
                "provider_kind": kind,
                "user_id": "12345",
            }
        )
    )
    config = {
        "model": api.hermes_model_config(kind, model),
        "providers": api.hermes_provider_entries(kind, model),
    }
    (scope.profile / "config.yaml").write_text(yaml.safe_dump(config))
    managed = ManagedProviderConfig(
        {kind.removesuffix("_api").upper() + "_MODEL": model},
        spec.secret_name,
        "owned-synthetic-key",
    )
    monkeypatch.setattr(
        "daemons_companion.setup.provider_runtime.managed_provider_config",
        lambda _: managed,
    )
    monkeypatch.setenv(spec.secret_name, "personal-key-not-used")
    worker = private_process_environment(scope)
    host = private_process_environment(scope, hermes=True)
    assert worker[PROVIDER_CONFIG_ENV].endswith("/config/provider.json")
    assert "CODEX_CLI_BIN" not in worker and "CODEX_HOME" not in host
    assert spec.secret_name not in worker
    assert host[spec.secret_name] == "owned-synthetic-key"
    config["model"]["base_url"] = "https://wrong-endpoint.invalid"
    (scope.profile / "config.yaml").write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="differs"):
        private_process_environment(scope, hermes=True)


@pytest.mark.parametrize(
    "kind_index,kind", [(1, "openai_api"), (2, "anthropic_api")]
)
def test_desktop_wizard_reaches_transaction_only_after_provider_validation(
    kind_index, kind
):
    from tests.test_companion_terminal import FakeUI, backend
    from daemons_companion.desktop.terminal import run_wizard

    calls = []

    def connect(ui, host, provider):
        calls.append(("connected", provider))
        return True

    def complete(ui, request, response):
        assert request["provider"]["kind"] == kind
        assert request["provider"]["credential_available"] is True
        assert request["provider"]["model"] == MODELS[kind]
        calls.append(("installed", kind))
        return True

    owner = SimpleNamespace(
        context=SimpleNamespace(target_os="macos"),
        offer=lambda *args: False,
        connect_subscription=connect,
        choose_model=lambda *args: MODELS[kind],
        complete=complete,
    )
    ui = FakeUI(choices=[0, 0, kind_index, 0, 0], fields=["Aster"])
    assert run_wizard(ui, backend=backend(), host_setup=owner, supported_only=True) == 0
    assert calls == [("connected", kind), ("installed", kind)]


@pytest.mark.parametrize("kind", MODELS)
def test_real_http_transport_checks_headers_body_and_refuses_redirects(
    monkeypatch, kind
):
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/v1/models":
                requests.append((self.path, dict(self.headers), None))
                self.send_response(302)
                self.send_header("Location", "/should-not-receive-a-key")
                self.end_headers()
            else:
                requests.append(("FORWARDED", {}, None))
                self.send_response(500)
                self.end_headers()

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, dict(self.headers), data))
            body = (
                {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"}
                if kind == "anthropic_api"
                else {
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]
                }
            )
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setitem(
        api.API_PROVIDERS,
        kind,
        replace(
            api.API_PROVIDERS[kind],
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        ),
    )
    try:
        assert api.verify_model(kind, "synthetic-key", MODELS[kind])
        assert requests[0][1]["Authorization"] == "Bearer synthetic-key"
        assert requests[0][2]["model"] == MODELS[kind]
        if kind == "anthropic_api":
            assert requests[0][1]["Anthropic-Version"] == "2023-06-01"
        with pytest.raises(api.ApiConnectionError):
            api.model_catalog(kind, "synthetic-key")
        assert not any(r[0] == "FORWARDED" for r in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "kind_index,kind", [(1, "openai_api"), (2, "anthropic_api")]
)
@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_packaged_api_wizard_prepares_real_review_before_telegram(
    tmp_path, kind_index, kind, target_os
):
    from functools import partial
    from tests.test_companion_app import _preparation, _probes
    from tests.test_companion_terminal import FakeUI
    from daemons_companion.desktop.app import handle_request
    from daemons_companion.desktop.terminal import run_wizard

    reached = []

    def complete(ui, request, response):
        assert response["action"] == "prepare"
        assert MODELS[kind] in response["review_text"]
        assert api.API_PROVIDERS[kind].label in response["review_text"]
        assert response["plan"]["steps"]
        reached.append("telegram boundary")
        return True

    owner = SimpleNamespace(
        context=SimpleNamespace(target_os=target_os),
        offer=lambda *args: False,
        connect_subscription=lambda *args: True,
        choose_model=lambda *args: MODELS[kind],
        complete=complete,
    )
    backend = partial(
        handle_request,
        probes=_probes(openclaw=False),
        finder=lambda _: None,
        preparation_context=_preparation(tmp_path, target_os=target_os),
        target_os=target_os,
    )
    ui = FakeUI(choices=[0, 0, kind_index, 0, 0], fields=["Aster"])
    assert (
        run_wizard(
            ui, backend=backend, host_setup=owner, supported_only=True, packaged=True
        )
        == 0
    )
    assert reached == ["telegram boundary"]
    assert (
        "Review details" not in ui.output and "Continue installation" not in ui.output
    )
    assert not any("4. Review:" in text for text in ui.output)


@pytest.mark.parametrize(
    "content",
    [
        "OK",
        [{"type": "text", "text": "OK"}],
        [
            {
                "type": "thinking",
                "thinking": [{"type": "text", "text": "private reasoning"}],
            },
            {"type": "text", "text": "O"},
            {"type": "text", "text": "K"},
        ],
    ],
)
def test_mistral_final_text_blocks_work_in_probe_and_maintenance(monkeypatch, content):
    from daemon_core.providers.mistral_provider import MistralProvider

    result = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}

    def request(*args):
        return result

    assert api.verify_model(
        "mistral_api", "synthetic", "mistral-large-4", request=request
    )
    monkeypatch.setattr("daemon_core.providers.mistral_provider.api_request", request)
    assert (
        MistralProvider("mistral-large-4", api_key="synthetic").complete(
            [{"role": "user", "content": "Synthetic maintenance request"}]
        )
        == "OK"
    )


@pytest.mark.parametrize(
    "content,finish",
    [
        (
            [
                {
                    "type": "thinking",
                    "thinking": [{"type": "text", "text": "not a final answer"}],
                }
            ],
            "stop",
        ),
        ([{"type": "text", "text": "partial"}], "length"),
        ([None], "stop"),
        ([{"type": "text", "text": 123}], "stop"),
        ([], "stop"),
    ],
)
def test_mistral_thoughts_and_incomplete_chunks_do_not_verify(content, finish):
    with pytest.raises(api.ApiConnectionError):
        api.completion_text(
            "mistral_api",
            {"choices": [{"message": {"content": content}, "finish_reason": finish}]},
        )


def test_model_retry_and_switch_keep_key_and_never_advance_on_failure(monkeypatch):
    owner = SimpleNamespace(
        api_connection=ApiConnection(
            "mistral_api", "/synthetic", "synthetic-key", ("mistral-large-4", "glm-5-2")
        )
    )
    calls = []

    def verify(kind, key, model):
        calls.append((key, model))
        assert owner.api_connection.verified_model == ""
        if model == "mistral-large-4":
            raise api.ApiConnectionError("Cannot reach the provider.")
        return True

    monkeypatch.setattr("daemons_companion.setup.api_setup.verify_model", verify)
    ui = UI([0, 0, 1, 1])  # Large 4; retry; choose another; GLM.
    assert choose_api_model(owner, ui) == "glm-5-2"
    assert calls == [("synthetic-key", "mistral-large-4")] * 2 + [
        ("synthetic-key", "glm-5-2")
    ]
    assert owner.api_connection.verified_model == "glm-5-2"
    assert "synthetic-key" not in str(ui.output)


def test_mistral_maintenance_uses_same_adapter(monkeypatch):
    from daemon_core.providers import mistral_provider

    seen = []
    monkeypatch.setattr(
        mistral_provider,
        "api_request",
        lambda kind, key, path, body: (
            seen.append(body)
            or {"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}
        ),
    )
    messages = [
        {"role": "developer", "content": "Background"},
        {"role": "user", "content": "Question"},
    ]
    provider = mistral_provider.MistralProvider(
        "synthetic-model", api_key="synthetic-key"
    )
    assert provider.complete(messages) == "OK"
    assert seen[0]["messages"][0] == {"role": "system", "content": "Background"}
    assert messages[0]["role"] == "developer"


def test_mistral_setup_probe_covers_instruction_roles():
    seen = []

    def request(kind, key, path, payload):
        seen.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}

    assert api.verify_model(
        "mistral_api", "synthetic-key", "synthetic-model", request=request
    )
    assert [m["role"] for m in seen[0]["messages"]] == ["system", "system", "user"]


def test_resumed_model_still_requires_provider_verification(monkeypatch):
    from types import SimpleNamespace
    from daemons_companion.setup import api_setup as setup
    from tests.test_companion_terminal import FakeUI, Quit
    owner = SimpleNamespace(api_connection=setup.ApiConnection(
        "openai_api", "/synthetic", "synthetic-key", ("synthetic-model",)))
    calls = []
    monkeypatch.setattr(setup, "verify_model", lambda *args: calls.append(args))
    assert setup.choose_api_model(owner, FakeUI(), "synthetic-model", resume=True) == "synthetic-model"
    assert calls == [("openai_api", "synthetic-key", "synthetic-model")]
    def fail(*args):
        raise setup.ApiConnectionError("synthetic provider failure")
    monkeypatch.setattr(setup, "verify_model", fail)
    with pytest.raises(Quit):
        setup.choose_api_model(owner, FakeUI(choices=[3]), "synthetic-model", resume=True)
    assert owner.api_connection.verified_model == ""


@pytest.mark.parametrize("temperature,top_p", [(0.0, 1), (0, 1), (0.4, None)])
def test_mistral_greedy_sampling_sends_top_p_one(monkeypatch, temperature, top_p):
    # Mistral answers 400 "top_p must be 1 when using greedy sampling" (code 3054)
    # to temperature 0 without top_p; every maintenance call at 0 then failed.
    from daemon_core.providers import mistral_provider

    seen = []

    def request(kind, key, path, payload):
        seen.append(payload)
        return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(mistral_provider, "api_request", request)
    provider = mistral_provider.MistralProvider("mistral-large-4", api_key="synthetic")
    assert provider.complete([{"role": "user", "content": "Synthetic"}], temperature=temperature) == "OK"
    assert seen[0].get("top_p") == top_p


def test_setup_offers_only_qualified_providers():
    # Mistral and SpaceXSI were withdrawn on 2026-10-29 (docs/model_providers.md);
    # daemons already using them keep their runtime support.
    from daemons_companion.setup.model_access import options

    kinds, labels = options()
    assert set(kinds) == {"codex_cli", "openai_api", "anthropic_api"}
    assert not any("Mistral" in label for label in labels)
    assert "gpt-5.6-terra" not in api.OPENAI_MODELS


def test_xai_faculties_send_their_reasoning_effort(monkeypatch):
    from daemon_core.providers import xai_provider

    seen = []

    def request(kind, key, path, payload):
        seen.append((kind, path, payload))
        return {"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}

    monkeypatch.setattr(xai_provider, "api_request", request)
    provider = xai_provider.XaiProvider("grok-4.7", reasoning_effort="high", api_key="synthetic")
    messages = [{"role": "developer", "content": "Synthetic"}, {"role": "user", "content": "Synthetic"}]
    assert provider.complete(messages, temperature=0.0) == "OK"
    kind, path, payload = seen[0]
    assert (kind, path) == ("xai_api", "/chat/completions")
    assert payload["reasoning_effort"] == "high" and payload["model"] == "grok-4.7"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


def test_xai_reaches_both_hosts_through_chat_completions():
    from daemons_companion.hosts.creation import native_config

    assert api.hermes_model_config("xai_api", "grok-4.7") == {
        "provider": "custom:daemon-xai", "default": "grok-4.7", "base_url": "https://api.x.ai/v1",
        "api_mode": "chat_completions", "key_env": "XAI_API_KEY"}
    assert api.hermes_provider_entries("xai_api", "grok-4.7") == {"daemon-xai": {
        "name": "SpaceXSI", "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY",
        "default_model": "grok-4.7", "transport": "chat_completions"}}
    assert api.hermes_provider_entries("mistral_api", "m") == {"daemon-mistral": {
        "name": "Mistral AI", "base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY",
        "default_model": "m", "transport": "chat_completions"}}
    provider = native_config("xai_api", "grok-4.7", "777001", 18901)["models"]["providers"]["daemons-xai"]
    assert provider["baseUrl"] == "https://api.x.ai/v1" and provider["api"] == "openai-completions"
    assert provider["apiKey"] == "${XAI_API_KEY}"
