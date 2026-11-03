import copy
import importlib.util
from pathlib import Path

import pytest

from daemon_core.provider_messages import is_mistral_endpoint, mistral_messages

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mistral_voice_test", ROOT / "integrations/hermes_voice_plugin/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


@pytest.mark.parametrize("url,expected", [
    ("https://api.mistral.ai/v1", True),
    ("https://api.mistral.ai/v1/", True),
    ("https://api.openai.com/v1", False),
    ("https://api.mistral.ai.example.org/v1", False),
    ("http://localhost:1234/v1", False),
    ("", False), ("https://[broken", False),
])
def test_endpoint_identification(url, expected):
    assert is_mistral_endpoint(url) is expected


def test_role_mapping_preserves_exact_payload_and_does_not_mutate():
    messages = [
        {"role": "system", "content": "Core instructions"},
        {"role": "developer", "content": [{"type": "text", "text": "Exact background"}]},
        {"role": "user", "content": "Current"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "abc123456", "type": "function", "function": {"name": "read_fixture", "arguments": "{}"}}]},
        {"role": "tool", "content": "Source text", "tool_call_id": "abc123456"},
    ]
    before = copy.deepcopy(messages)
    adapted = mistral_messages(messages)
    assert messages == before
    assert adapted[1] == {**before[1], "role": "system"}
    assert adapted[:1] + adapted[2:] == before[:1] + before[2:]
    assert mistral_messages(adapted) == adapted


@pytest.mark.parametrize("endpoint,api_mode,expected", [
    ("https://api.mistral.ai/v1", "chat_completions", "system"),
    ("https://api.openai.com/v1", "chat_completions", "developer"),
    ("https://custom.example/v1", "chat_completions", "developer"),
])
@pytest.mark.parametrize("iteration", [0, 1])
def test_real_plugin_adapts_initial_and_tool_requests(monkeypatch, endpoint, api_mode, expected, iteration):
    turns = [{"role": "developer", "content": "Background"},
             {"role": "user", "content": "Earlier"},
             {"role": "assistant", "content": "Earlier reply"},
             {"role": "system", "content": "Present state"},
             {"role": "user", "content": "Current"}]
    tool_tail = [{"role": "assistant", "tool_calls": [{"id": "abc123456", "type": "function", "function": {"name": "read_fixture", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "abc123456", "content": "Exact result"}]
    request = {"messages": turns[-1:] + (tool_tail if iteration else []),
               "model": "synthetic-model", "stream": False,
               "tools": [{"type": "function", "function": {"name": "read_fixture", "parameters": {"type": "object"}}}]}
    original = copy.deepcopy(request)
    monkeypatch.setattr(plugin, "_install_visible_stream_tracking", lambda: False)
    monkeypatch.setattr(plugin, "_append_voice_event", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_observe_continuity", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_delegated_request_or_none", lambda req, **kw: plugin._rewrite_request(
        req, "Sovereign", turns if not iteration else None, api_mode,
        tool_loop_prefix=turns[:-1] if iteration else None))
    seen = []
    def next_call(payload):
        seen.append(payload)
        return {"choices": [{"message": {"content": "OK"}}]}
    plugin._sovereign_middleware(request, next_call, base_url=endpoint, provider="custom",
                                api_mode=api_mode, turn_id="role-test", api_call_count=iteration)
    assert len(seen) == 1
    assert request == original
    sent = seen[0]
    assert sent["messages"][1] == {"role": expected, "content": "Background"}
    assert sent["messages"][2:6] == turns[1:]
    assert sent["messages"][6:] == (tool_tail if iteration else [])
    assert sent["tools"] == original["tools"]
    assert sent["model"] == original["model"]

