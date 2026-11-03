import json
import os
from types import SimpleNamespace

import pytest

from daemon_core import voice_request_capture as capture


@pytest.fixture
def private_capture_root(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_REPOSITORY_ROOT", tmp_path)
    return tmp_path / "exports" / "private"


def _request():
    return {
        "model": "gpt-test",
        "instructions": "SYSTEM privé",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Bonjour"}],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "web_search",
                "description": "Recherche synthétique",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": "auto",
        "timeout": object(),
        "extra_headers": {"Authorization": "Bearer secret"},
    }


def _metadata(**overrides):
    metadata = {
        "trace_id": "trace-1",
        "turn_id": "turn-1",
        "api_request_id": "turn-1:api:1",
        "api_call_count": 1,
        "platform": "telegram",
        "provider": "openai-codex",
        "model": "gpt-test",
        "api_mode": "codex_responses",
        "voice_path": "canonical",
        "prompt_variant": "voice_brief_reference",
    }
    metadata.update(overrides)
    return metadata


def test_capture_is_disabled_without_an_explicit_file(monkeypatch):
    monkeypatch.delenv(capture.VOICE_CAPTURE_FILE_ENV, raising=False)

    assert capture.begin_voice_request_capture(
        _request(),
        metadata=_metadata(),
    ) is None


def test_capture_refuses_a_target_outside_exports_private(
    monkeypatch,
    private_capture_root,
):
    monkeypatch.setenv(capture.VOICE_CAPTURE_FILE_ENV, "capture.json")

    with pytest.raises(ValueError, match="exports/private"):
        capture.begin_voice_request_capture(
            _request(),
            metadata=_metadata(),
        )

    assert not (private_capture_root.parent.parent / "capture.json").exists()


def test_capture_refuses_a_non_json_target(monkeypatch, private_capture_root):
    monkeypatch.setenv(
        capture.VOICE_CAPTURE_FILE_ENV,
        "exports/private/capture.txt",
    )

    with pytest.raises(ValueError, match=".json"):
        capture.begin_voice_request_capture(
            _request(),
            metadata=_metadata(),
        )


def test_capture_persists_effective_body_then_provider_usage(
    monkeypatch,
    private_capture_root,
):
    target = private_capture_root / "capture.json"
    monkeypatch.setenv(
        capture.VOICE_CAPTURE_FILE_ENV,
        "exports/private/capture.json",
    )

    handle = capture.begin_voice_request_capture(
        _request(),
        metadata=_metadata(),
    )

    assert handle is not None
    pending = json.loads(target.read_text(encoding="utf-8"))
    assert pending["status"] == "pending"
    assert pending["request"]["body"]["instructions"] == "SYSTEM privé"
    assert pending["request"]["body"]["tools"][0]["name"] == "web_search"
    assert "timeout" not in pending["request"]["body"]
    assert "extra_headers" not in pending["request"]["body"]
    assert pending["request"]["excluded_transport_keys"] == [
        "extra_headers",
        "timeout",
    ]
    assert "secret" not in target.read_text(encoding="utf-8")
    assert pending["measurements"]["instructions_chars"] == len("SYSTEM privé")
    assert pending["measurements"]["message_text_chars"] == len("Bonjour")
    assert pending["measurements"]["tool_count"] == 1
    assert pending["measurements"]["tool_names"] == ["web_search"]
    assert os.stat(target).st_mode & 0o777 == 0o600

    handle.complete(
        SimpleNamespace(
            id="response-1",
            model="gpt-test-2026-08-01",
            status="completed",
            usage=SimpleNamespace(
                input_tokens=1234,
                output_tokens=17,
                input_tokens_details=SimpleNamespace(cached_tokens=1000),
            ),
        ),
        duration_s=1.23456,
    )

    completed = json.loads(target.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert completed["outcome"]["duration_s"] == 1.2346
    assert completed["outcome"]["response"] == {
        "id": "response-1",
        "model": "gpt-test-2026-08-01",
        "status": "completed",
    }
    assert completed["outcome"]["provider_usage"] == {
        "input_tokens": 1234,
        "output_tokens": 17,
        "input_tokens_details": {"cached_tokens": 1000},
    }
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_capture_is_one_shot_and_never_overwrites(
    monkeypatch,
    private_capture_root,
):
    target = private_capture_root / "capture.json"
    monkeypatch.setenv(
        capture.VOICE_CAPTURE_FILE_ENV,
        "exports/private/capture.json",
    )
    first = capture.begin_voice_request_capture(
        _request(),
        metadata=_metadata(),
    )
    original = target.read_bytes()
    changed = _request()
    changed["instructions"] = "AUTRE SYSTÈME"

    second = capture.begin_voice_request_capture(
        changed,
        metadata=_metadata(turn_id="turn-2", api_request_id="turn-2:api:1"),
    )

    assert first is not None
    assert second is None
    assert target.read_bytes() == original


def test_capture_skips_later_tool_iterations(monkeypatch, private_capture_root):
    target = private_capture_root / "capture.json"
    monkeypatch.setenv(
        capture.VOICE_CAPTURE_FILE_ENV,
        "exports/private/capture.json",
    )

    handle = capture.begin_voice_request_capture(
        _request(),
        metadata=_metadata(api_call_count=2),
    )

    assert handle is None
    assert not target.exists()


def test_capture_failure_records_only_the_error_type(
    monkeypatch,
    private_capture_root,
):
    target = private_capture_root / "capture.json"
    monkeypatch.setenv(
        capture.VOICE_CAPTURE_FILE_ENV,
        "exports/private/capture.json",
    )
    handle = capture.begin_voice_request_capture(
        _request(),
        metadata=_metadata(),
    )
    assert handle is not None

    handle.fail(
        RuntimeError("Bearer secret-provider-error"),
        duration_s=0.5,
    )

    failed = json.loads(target.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["outcome"] == {
        "duration_s": 0.5,
        "error_type": "RuntimeError",
        "provider_usage": None,
    }
    assert "secret-provider-error" not in target.read_text(encoding="utf-8")
