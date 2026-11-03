from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import daemon_core.providers.factory as factory
from daemons_companion.setup.provider_runtime import (
    ManagedProviderConfig,
    ManagedProviderLoader,
    PROVIDER_CONFIG_ENV,
)


class MemoryStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.get_calls = 0

    def get(self, reference: str) -> str | None:
        self.get_calls += 1
        return self.values.get(reference)


def _envelope(secret: str) -> str:
    return json.dumps(
        {
            "schema": "daemon.companion-credential.v1",
            "owner_token": "owned-token",
            "value": secret,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _write_config(
    path: Path,
    *,
    kind: str = "openai_api",
    model: str = "gpt-test",
    base_url: str = "",
    credential: dict[str, str] | None | object = ...,
) -> Path:
    if credential is ...:
        credential = {
            "name": "OPENAI_API_KEY",
            "reference": "provider-00000000-0000-0000-0000-000000000000",
        }
    payload = {
        "schema": "daemon.companion-provider.v1",
        "kind": kind,
        "model": model,
        "base_url": base_url,
        "reasoning": {
            "routing": "medium",
            "conversation": "medium",
            "memory": "high",
        },
        "credential": credential,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path.absolute()


def test_openai_loader_resolves_secret_once_without_exporting_it(tmp_path: Path) -> None:
    reference = "provider-00000000-0000-0000-0000-000000000000"
    secret = "sk-protected"
    store = MemoryStore({reference: _envelope(secret)})
    config_path = _write_config(tmp_path / "provider.json")
    loader = ManagedProviderLoader(store_factory=lambda: store)
    environ = {PROVIDER_CONFIG_ENV: str(config_path)}

    first = loader.load(environ)
    second = loader.load(environ)

    assert first is second
    assert first is not None
    assert first.get("LLM_PROVIDER") == "openai"
    assert first.get("OPENAI_MODEL") == "gpt-test"
    assert first.get("ROUTING_OPENAI_MODEL") == "gpt-test"
    assert first.get("CONVERSATION_OPENAI_MODEL") == "gpt-test"
    assert first.get("MEMORY_OPENAI_MODEL") == "gpt-test"
    assert first.secret("OPENAI_API_KEY") == secret
    assert store.get_calls == 1
    assert "OPENAI_API_KEY" not in environ
    assert secret not in json.dumps(dict(first.values))


def test_changed_file_revision_invalidates_cache(tmp_path: Path) -> None:
    reference = "provider-00000000-0000-0000-0000-000000000000"
    store = MemoryStore({reference: _envelope("secret")})
    path = _write_config(tmp_path / "provider.json", model="first-model")
    loader = ManagedProviderLoader(store_factory=lambda: store)
    environ = {PROVIDER_CONFIG_ENV: str(path)}
    assert loader.load(environ).get("OPENAI_MODEL") == "first-model"

    _write_config(path, model="second-model-longer")

    assert loader.load(environ).get("OPENAI_MODEL") == "second-model-longer"
    assert store.get_calls == 2


@pytest.mark.parametrize(
    "kind,base_url,credential,expected_provider,model_key",
    [
        (
            "openai_compatible_api",
            "https://provider.example/v1",
            {
                "name": "LOCAL_LLM_API_KEY",
                "reference": "provider-00000000-0000-0000-0000-000000000000",
            },
            "local",
            "LOCAL_LLM_MODEL",
        ),
        (
            "openai_compatible_api",
            "http://127.0.0.1:1234/v1",
            None,
            "local",
            "LOCAL_LLM_MODEL",
        ),
        ("codex_cli", "", None, "codex_cli", "CODEX_CLI_MODEL"),
    ],
)
def test_other_provider_modes_map_one_model(
    tmp_path: Path,
    kind: str,
    base_url: str,
    credential: dict[str, str] | None,
    expected_provider: str,
    model_key: str,
) -> None:
    reference = "provider-00000000-0000-0000-0000-000000000000"
    store = MemoryStore({reference: _envelope("secret")})
    path = _write_config(
        tmp_path / "provider.json",
        kind=kind,
        base_url=base_url,
        credential=credential,
    )
    loaded = ManagedProviderLoader(store_factory=lambda: store).load(
        {PROVIDER_CONFIG_ENV: str(path)}
    )
    assert loaded is not None
    for key in (
        "LLM_PROVIDER",
        "ROUTING_LLM_PROVIDER",
        "CONVERSATION_LLM_PROVIDER",
        "MEMORY_LLM_PROVIDER",
    ):
        assert loaded.get(key) == expected_provider
    assert loaded.get(model_key) == "gpt-test"


class FakeOpenAIClient:
    created: list[dict[str, str]] = []

    def __init__(self, **kwargs) -> None:
        self.created.append(kwargs)
        self.chat = SimpleNamespace(completions=SimpleNamespace())


def test_factory_passes_secret_directly_to_every_openai_faculty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-direct-only"
    managed = ManagedProviderConfig(
        {
            "LLM_PROVIDER": "openai",
            "ROUTING_LLM_PROVIDER": "openai",
            "CONVERSATION_LLM_PROVIDER": "openai",
            "MEMORY_LLM_PROVIDER": "openai",
            "OPENAI_MODEL": "one-model",
            "ROUTING_OPENAI_MODEL": "one-model",
            "CONVERSATION_OPENAI_MODEL": "one-model",
            "MEMORY_OPENAI_MODEL": "one-model",
            "ROUTING_REASONING_EFFORT": "medium",
            "CONVERSATION_REASONING_EFFORT": "medium",
            "MEMORY_REASONING_EFFORT": "high",
        },
        "OPENAI_API_KEY",
        secret,
    )
    FakeOpenAIClient.created = []
    monkeypatch.setattr(factory, "managed_provider_config", lambda: managed)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    providers = [
        factory.build_provider(),
        factory.build_routing_provider(),
        factory.build_conversation_provider(),
        factory.build_memory_provider(),
    ]

    assert [provider.model for provider in providers] == ["one-model"] * 4
    assert [provider.reasoning_effort for provider in providers] == [
        None,
        "medium",
        "medium",
        "high",
    ]
    assert FakeOpenAIClient.created == [{"api_key": secret}] * 4
    assert "OPENAI_API_KEY" not in os.environ


def test_inactive_loader_keeps_existing_environment_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROVIDER_CONFIG_ENV, raising=False)
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "manual-key")
    monkeypatch.setenv("OPENAI_MODEL", "manual-model")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))
    assert ManagedProviderLoader(store_factory=lambda: pytest.fail()).load() is None

    provider = factory.build_memory_provider()

    assert provider.model == "manual-model"
    assert provider.reasoning_effort == "high"


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda payload: payload.update(schema="wrong"), "unsupported"),
        (lambda payload: payload.update(model=""), "model"),
        (lambda payload: payload.update(reasoning={"memory": "low"}), "reasoning"),
        (lambda payload: payload.update(base_url="http://remote.example/v1"), "base URL"),
        (lambda payload: payload.update(credential=None), "selection"),
    ],
)
def test_invalid_managed_state_fails_closed(
    tmp_path: Path,
    mutate,
    match: str,
) -> None:
    path = _write_config(tmp_path / "provider.json")
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    loader = ManagedProviderLoader(store_factory=lambda: MemoryStore())
    with pytest.raises(RuntimeError, match=match):
        loader.load({PROVIDER_CONFIG_ENV: str(path)})


def test_missing_or_malformed_credential_fails_without_secret_in_error(tmp_path: Path) -> None:
    reference = "provider-00000000-0000-0000-0000-000000000000"
    path = _write_config(tmp_path / "provider.json")
    for protected in (None, "not-json", json.dumps({"value": "sk-never-leak"})):
        store = MemoryStore({} if protected is None else {reference: protected})
        loader = ManagedProviderLoader(store_factory=lambda store=store: store)
        with pytest.raises(RuntimeError) as captured:
            loader.load({PROVIDER_CONFIG_ENV: str(path)})
        assert "sk-never-leak" not in str(captured.value)


def test_relative_symlinked_and_world_readable_config_are_refused(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "provider.json")
    loader = ManagedProviderLoader(store_factory=lambda: MemoryStore())
    with pytest.raises(RuntimeError, match="absolute"):
        loader.load({PROVIDER_CONFIG_ENV: "provider.json"})

    link = tmp_path / "provider-link.json"
    link.symlink_to(path)
    with pytest.raises(RuntimeError, match="regular"):
        loader.load({PROVIDER_CONFIG_ENV: str(link.absolute())})

    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(RuntimeError, match="private"):
            loader.load({PROVIDER_CONFIG_ENV: str(path)})
