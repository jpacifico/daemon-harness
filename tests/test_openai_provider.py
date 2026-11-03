import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.providers.base import unsupported_optional_param
from daemon_core.providers.factory import (
    build_conversation_provider,
    build_memory_provider,
)
from daemon_core.providers.openai_provider import OpenAIProvider


class FakeMessage:
    content = "ok"


class FakeChoice:
    message = FakeMessage()


class FakeResult:
    choices = [FakeChoice()]


class FakeCompletions:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "temperature" in kwargs:
            raise FakeBadRequest("Unsupported value: 'temperature' does not support 0.2 with this model.")
        return FakeResult()


class FakeDelta:
    content = "ok"


class FakeStreamChoice:
    delta = FakeDelta()


class FakeStreamChunk:
    choices = [FakeStreamChoice()]


class FakeStreamingCompletions(FakeCompletions):
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "temperature" in kwargs:
            raise FakeBadRequest("Unsupported value: 'temperature' does not support 0.2 with this model.")
        return [FakeStreamChunk()]


class FakeChat:
    def __init__(self) -> None:
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self) -> None:
        self.chat = FakeChat()


class FakeStreamingChat:
    def __init__(self) -> None:
        self.completions = FakeStreamingCompletions()


class FakeStreamingClient:
    def __init__(self) -> None:
        self.chat = FakeStreamingChat()


class FakeBadRequest(Exception):
    pass


def _bare_provider(client, reasoning_effort=None):
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = client
    provider.model = "test-model"
    provider.reasoning_effort = reasoning_effort
    return provider


def test_openai_provider_retries_without_temperature_when_model_rejects_it(monkeypatch):
    provider = _bare_provider(FakeClient())

    result = provider.complete(messages=[{"role": "user", "content": "Salut"}], temperature=0.2)

    assert result == "ok"
    calls = provider.client.chat.completions.calls
    assert calls[0]["temperature"] == 0.2
    assert "temperature" not in calls[1]


def test_unsupported_param_detection():
    params = {"temperature": 0.2, "reasoning_effort": "low"}
    assert unsupported_optional_param(FakeBadRequest("Unsupported value: 'temperature'"), params) == "temperature"
    assert unsupported_optional_param(FakeBadRequest("Unsupported parameter: 'reasoning_effort'"), params) == "reasoning_effort"
    assert unsupported_optional_param(FakeBadRequest("Other error"), params) is None
    # A parameter absent from the request cannot be blamed for its failure.
    assert unsupported_optional_param(FakeBadRequest("Unsupported 'reasoning_effort'"), {"temperature": 0.2}) is None


def test_openai_provider_passes_reasoning_effort_and_drops_it_if_rejected():
    class EffortRejectingCompletions(FakeCompletions):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if "reasoning_effort" in kwargs:
                raise FakeBadRequest("Unsupported parameter: 'reasoning_effort' is not supported with this model.")
            return FakeResult()

    client = FakeClient()
    client.chat.completions = EffortRejectingCompletions()
    provider = _bare_provider(client, reasoning_effort="low")

    result = provider.complete(messages=[{"role": "user", "content": "Salut"}], temperature=0.2)

    assert result == "ok"
    calls = client.chat.completions.calls
    assert calls[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in calls[-1]
    assert provider.reasoning_effort is None

    provider.complete(
        messages=[{"role": "user", "content": "Encore"}],
        temperature=0.2,
    )
    assert "reasoning_effort" not in client.chat.completions.calls[-1]


def test_factory_defaults_openai_memory_reasoning_to_high(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("MEMORY_REASONING_EFFORT", raising=False)

    assert build_memory_provider().reasoning_effort == "high"


def test_factory_defaults_openai_internal_conversation_to_medium(monkeypatch):
    monkeypatch.setenv("CONVERSATION_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("CONVERSATION_REASONING_EFFORT", raising=False)

    assert build_conversation_provider().reasoning_effort == "medium"


def test_openai_provider_stream_retries_without_temperature_when_model_rejects_it():
    provider = _bare_provider(FakeStreamingClient())

    chunks = list(provider.stream_complete(messages=[{"role": "user", "content": "Salut"}], temperature=0.2))

    assert chunks == ["ok"]
    calls = provider.client.chat.completions.calls
    assert calls[0]["stream"] is True
    assert calls[0]["temperature"] == 0.2
    assert calls[1]["stream"] is True
    assert "temperature" not in calls[1]
