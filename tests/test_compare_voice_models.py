"""The voice comparison bench: request shapes and measures, without any network call."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/compare_voice_models.py"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("compare_voice_models", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Opener:
    def __init__(self, events):
        self.events, self.requests = events, []

    def open(self, request, timeout):
        self.requests.append(request)
        lines = [f"data: {json.dumps(event)}\n".encode() for event in self.events]
        return io.BufferedReader(io.BytesIO(b"".join(lines)))


def stream(stop="end_turn"):
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 4100, "output_tokens": 1}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Salut "}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Camille."}},
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 240}},
    ]


def test_opus_gets_hermes_request_shape_and_only_its_visible_text(bench, monkeypatch):
    opener = Opener(stream())
    monkeypatch.setattr(bench, "build_opener", lambda: opener)
    turns = [{"role": "system", "content": "scene"}, {"role": "assistant", "content": "Bonjour."},
             {"role": "user", "content": "Salut !"}]
    reply, usage = bench.anthropic_reply("k", "claude-opus-5-5", "low", "voice", turns)
    assert reply == "Salut Camille."
    assert usage == {"input": 4100, "output": 240, "reasoning": None}
    request = opener.requests[0]
    body = json.loads(request.data)
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == "k"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "low"} and body["max_tokens"] == 128_000 and body["stream"]
    # Instruction roles go to system; the dialogue starts with a user turn.
    assert "voice" in body["system"] and "scene" in body["system"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]


def test_an_unfinished_opus_reply_is_an_error(bench, monkeypatch):
    monkeypatch.setattr(bench, "build_opener", lambda: Opener(stream("max_tokens")))
    with pytest.raises(RuntimeError, match="max_tokens"):
        bench.anthropic_reply("k", "claude-opus-5-5", "medium", "voice", [{"role": "user", "content": "Salut"}])


def test_measures_count_what_telegram_shows(bench):
    measured = bench.measure("[daemon-turn: exchange]\nSalut Camille ! Ça va ?")
    assert measured["marker"] is True
    assert measured["chars"] == len("Salut Camille ! Ça va ?")
    assert measured["raw_chars"] > measured["chars"]
    assert measured["questions"] == 1 and measured["uses_name"]
    assert bench.measure("Bonsoir.")["marker"] is False
