import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.providers.local_openai_compatible_provider import (
    LocalOpenAICompatibleProvider,
)
from daemon_core.providers.factory import (
    build_memory_provider,
    build_routing_provider,
)


class FakeMessage:
    content = "ok"


class FakeChoice:
    message = FakeMessage()


class FakeResult:
    choices = [FakeChoice()]
    usage = None


class FakeBadRequest(Exception):
    pass


class FakeCompletions:
    def __init__(self, *, reject_effort: bool = False) -> None:
        self.reject_effort = reject_effort
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_effort and "reasoning_effort" in kwargs:
            raise FakeBadRequest(
                "Unsupported parameter: 'reasoning_effort'"
            )
        return FakeResult()


class FakeChat:
    def __init__(self, completions) -> None:
        self.completions = completions


class FakeClient:
    def __init__(self, completions) -> None:
        self.chat = FakeChat(completions)


def _provider(completions) -> LocalOpenAICompatibleProvider:
    provider = LocalOpenAICompatibleProvider.__new__(
        LocalOpenAICompatibleProvider
    )
    provider.client = FakeClient(completions)
    provider.model = "compatible-model"
    provider.reasoning_effort = "high"
    return provider


def test_compatible_provider_passes_reasoning_effort_when_supported():
    completions = FakeCompletions()
    provider = _provider(completions)

    assert provider.complete([{"role": "user", "content": "Salut"}]) == "ok"
    assert len(completions.calls) == 1
    assert completions.calls[0]["reasoning_effort"] == "high"


def test_compatible_provider_retries_once_without_rejected_effort():
    completions = FakeCompletions(reject_effort=True)
    provider = _provider(completions)

    assert provider.complete([{"role": "user", "content": "Salut"}]) == "ok"
    assert len(completions.calls) == 2
    assert completions.calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in completions.calls[1]
    assert provider.reasoning_effort is None

    provider.complete([{"role": "user", "content": "Encore"}])
    assert len(completions.calls) == 3
    assert "reasoning_effort" not in completions.calls[2]


def test_compatible_provider_does_not_mask_unrelated_errors():
    class FailingCompletions(FakeCompletions):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise FakeBadRequest("authentication failed")

    completions = FailingCompletions()
    provider = _provider(completions)

    with pytest.raises(FakeBadRequest, match="authentication failed"):
        provider.complete([{"role": "user", "content": "Salut"}])
    assert len(completions.calls) == 1


def test_factory_defaults_compatible_memory_reasoning_to_high(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "local")
    monkeypatch.delenv("MEMORY_REASONING_EFFORT", raising=False)

    assert build_memory_provider().reasoning_effort == "high"


def test_factory_defaults_compatible_routing_reasoning_to_medium(monkeypatch):
    monkeypatch.setenv("ROUTING_LLM_PROVIDER", "local")
    monkeypatch.delenv("ROUTING_REASONING_EFFORT", raising=False)

    assert build_routing_provider().reasoning_effort == "medium"
