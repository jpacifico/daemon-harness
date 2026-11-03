import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location(
    "daemon_voice_test", ROOT / "integrations" / "hermes_voice_plugin" / "__init__.py"
)
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


def _stub_canonical(monkeypatch, messages):
    import daemon_core.canonical_voice as canonical_voice
    import daemon_core.voice_brief as voice_brief

    context = type(
        "CanonicalContext",
        (),
        {
            "atlas": type("Atlas", (), {"souvenir_names": (), "rendered_chars": 0})(),
            "hot_memory": type("Hot", (), {"souvenir_names": (), "rendered_chars": 0})(),
            "scene_model": None,
            "scene_delta": "",
            "scene_scope": "telegram__garden",
            "prompt_variant": "voice_brief_reference",
            "situation_source": "seed",
            "dialogue_coverage": voice_brief.DialogueCoverage(
                status="recent_only",
                dialogue="",
            ),
        },
    )()
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (messages, context),
    )
    # Transport tests intentionally use tiny synthetic prompts. The compiler's
    # loss barrier has its own suite; here we preserve the supplied messages.
    monkeypatch.setattr(voice_brief, "compile_voice_brief", lambda plan: plan)
    monkeypatch.setattr(
        voice_brief,
        "render_reference_messages",
        lambda plan: plan.messages,
    )


@pytest.fixture(autouse=True)
def isolated_latency_log(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_LATENCY_LOG", str(tmp_path / "latency.jsonl"))
    voice._turn_latency.clear()
    voice._latency_local.current = None
    voice._active_latency.clear()


def test_rewrite_codex_first_call_replaces_prompt_keeps_tools():
    request = {
        "model": "gpt-5.5", "instructions": "You are Hermes Agent...",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Salut"}]}],
        "tools": [{"type": "function", "name": "web_search"}], "tool_choice": "auto",
    }
    # Chat structure: turns remain separate, alternating messages.
    turns = [
        {"role": "user", "content": "Il reste du temps avant le match"},
        {"role": "assistant", "content": "Oui, encore une heure tranquille."},
        {"role": "user", "content": "Salut"},
    ]
    out = voice._rewrite_request(request, "Tu es Écho.", turns, "codex_responses")
    assert out["instructions"] == "Tu es Écho."
    assert out["input"][0]["content"][0]["type"] == "input_text"
    assert out["input"][1]["content"][0]["type"] == "output_text"  # tour assistant
    assert out["input"][2]["content"][0]["text"] == "Salut"  # The current user message is separate.
    assert out["tools"] == request["tools"]  # Existing request fields remain.
    assert request["instructions"].startswith("You are Hermes")  # original intact


def test_rewrite_codex_tool_loop_swaps_system_only():
    request = {
        "model": "gpt-5.5", "instructions": "You are Hermes Agent...",
        "input": [{"role": "user", "content": []}, {"type": "function_call_output", "output": "résultat"}],
        "tools": [{"type": "function", "name": "web_search"}],
    }
    out = voice._rewrite_request(request, "Tu es Écho.", None, "codex_responses")
    assert out["instructions"] == "Tu es Écho."
    assert out["input"] == request["input"]  # Tool history is preserved.


def test_rewrite_chat_format():
    request = {"model": "m", "messages": [
        {"role": "system", "content": "hermes"}, {"role": "user", "content": "salut"}],
        "tools": [{"type": "function"}]}
    out = voice._rewrite_request(request, "Tu es Écho.", [{"role": "user", "content": "USER"}], "chat_completions")
    assert out["messages"][0] == {"role": "system", "content": "Tu es Écho."}
    assert out["messages"][1]["content"] == "USER"
    out2 = voice._rewrite_request(request, "Tu es Écho.", None, "chat_completions")
    assert out2["messages"][0]["content"] == "Tu es Écho."
    assert out2["messages"][1]["content"] == "salut"


def test_middleware_delegate_calls_next_with_rewritten(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_MODE", "delegate")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "garden")
    monkeypatch.setenv("DAEMON_CORE_SRC", str(ROOT / "src"))
    _stub_canonical(
        monkeypatch,
        [
            {"role": "system", "content": "Tu es Écho."},
            {"role": "user", "content": "USER"},
        ],
    )

    seen = {}
    def next_call(req):
        seen["request"] = req
        return "STREAMED_BY_HERMES"

    request = {"model": "gpt-5.5", "instructions": "hermes", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "Salut"}]}], "tools": [{"name": "web_search"}]}
    out = voice._sovereign_middleware(
        request=request, next_call=next_call,
        platform="telegram", api_call_count=1, api_mode="codex_responses", turn_id="t1",
    )

    assert out == "STREAMED_BY_HERMES"  # Hermes produces the response.
    assert seen["request"]["instructions"] == "Tu es Écho."
    assert seen["request"]["tools"] == request["tools"]
    # The sovereign system context survives the next tool-loop iteration.
    request2 = {"model": "gpt-5.5", "instructions": "hermes-rebuilt", "input": ["history"], "tools": []}
    voice._sovereign_middleware(
        request=request2, next_call=lambda r: seen.update(second=r) or "ok",
        platform="telegram", api_call_count=2, api_mode="codex_responses", turn_id="t1",
    )
    assert seen["second"]["instructions"] == "Tu es Écho."
    assert seen["second"]["input"] == ["history"]


def test_middleware_captures_only_the_effective_rewritten_request(
    monkeypatch,
    tmp_path,
):
    from types import SimpleNamespace

    from daemon_core import voice_request_capture

    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "garden")
    monkeypatch.setenv("DAEMON_CORE_SRC", str(ROOT / "src"))
    monkeypatch.setenv(
        voice_request_capture.VOICE_CAPTURE_FILE_ENV,
        "exports/private/effective.json",
    )
    monkeypatch.setattr(voice_request_capture, "_REPOSITORY_ROOT", tmp_path)
    _stub_canonical(
        monkeypatch,
        [
            {"role": "system", "content": "SYSTÈME SOUVERAIN"},
            {"role": "user", "content": "Bonjour"},
        ],
    )
    request = {
        "model": "gpt-test",
        "instructions": "SYSTÈME HERMES À REMPLACER",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Bonjour"}],
            }
        ],
        "tools": [{"type": "function", "name": "web_search"}],
    }

    response = voice._sovereign_middleware(
        request=request,
        next_call=lambda _request: SimpleNamespace(
            output_text="Réponse",
            output=[],
            usage=SimpleNamespace(input_tokens=321, output_tokens=9),
        ),
        platform="telegram",
        provider="openai-codex",
        model="gpt-test",
        api_call_count=1,
        api_mode="codex_responses",
        turn_id="turn-capture",
        api_request_id="turn-capture:api:1",
    )

    document = json.loads(
        (
            tmp_path / "exports" / "private" / "effective.json"
        ).read_text(encoding="utf-8")
    )
    assert response.output_text == "Réponse"
    assert document["status"] == "completed"
    assert document["request"]["body"]["instructions"] == "SYSTÈME SOUVERAIN"
    assert "SYSTÈME HERMES À REMPLACER" not in json.dumps(
        document,
        ensure_ascii=False,
    )
    assert document["request"]["body"]["tools"] == request["tools"]
    assert document["metadata"]["api_request_id"] == "turn-capture:api:1"
    assert document["outcome"]["provider_usage"] == {
        "input_tokens": 321,
        "output_tokens": 9,
    }


def test_capture_failure_never_blocks_the_provider_call(monkeypatch):
    from daemon_core import voice_request_capture

    monkeypatch.setattr(
        voice,
        "_delegated_request_or_none",
        lambda request, **_kwargs: {**request, "instructions": "souverain"},
    )
    monkeypatch.setattr(
        voice_request_capture,
        "begin_voice_request_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    request = {"instructions": "hôte", "input": []}

    response = voice._sovereign_middleware(
        request=request,
        next_call=lambda effective: ("ok", effective),
        platform="telegram",
        provider="openai-codex",
        model="gpt-test",
        api_call_count=1,
        api_mode="codex_responses",
        turn_id="turn-capture-failure",
    )

    assert response == ("ok", {"instructions": "souverain", "input": []})


def test_middleware_delegate_passthrough_on_other_platform(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_MODE", "delegate")
    request = {"model": "m", "instructions": "hermes", "input": []}
    out = voice._sovereign_middleware(
        request=request, next_call=lambda r: ("PASS", r),
        platform="cron", api_call_count=1, api_mode="codex_responses", turn_id="t2",
    )
    assert out == ("PASS", request)  # The original request is unchanged.


def test_middleware_latency_events_share_prefetch_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_MODE", "delegate")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "garden")
    monkeypatch.setenv("DAEMON_CORE_SRC", str(ROOT / "src"))
    import daemon_core.turn_handoff as turn_handoff

    monkeypatch.setattr(
        turn_handoff,
        "read_turn_handoff",
        lambda *a, **k: ("Salut", "prefetch-trace", time.time() - 2),
    )
    _stub_canonical(
        monkeypatch,
        [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Salut"},
        ],
    )

    request = {
        "model": "m",
        "instructions": "hermes",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Salut"}]}],
    }
    voice._sovereign_middleware(
        request=request,
        next_call=lambda req: "done",
        platform="telegram",
        api_call_count=1,
        api_mode="codex_responses",
        turn_id="turn-7",
        api_request_id="turn-7:api:1",
    )

    path = Path(os.environ["DAEMON_CORE_LATENCY_LOG"])
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event"] for event in events] == ["voice_start", "voice_complete"]
    assert {event["trace_id"] for event in events} == {"prefetch-trace"}
    assert {event["turn_id"] for event in events} == {"turn-7"}
    assert {event["api_request_id"] for event in events} == {"turn-7:api:1"}


def test_stream_probe_correlates_worker_delta_and_first_successful_delivery(monkeypatch):
    class FakeConsumer:
        def __init__(self):
            self._already_sent = False
            self._last_sent_text = ""
            self.received = []

        def on_delta(self, text):
            self.received.append(text)

        async def _send_or_edit(self, text, *, finalize=False, is_turn_final=True):
            self._already_sent = True
            self._last_sent_text = text
            return True

    assert voice._install_stream_probe(FakeConsumer) is True
    record = {
        "trace_id": "stream-trace",
        "turn_id": "turn-stream",
        "api_request_id": "turn-stream:api:1",
        "api_call_count": 1,
        "platform": "telegram",
        "prefetch_started_at": time.time() - 1,
        "voice_started_at": time.time() - 0.5,
        "first_delta_logged": False,
    }
    voice._register_active_latency(record)
    consumer = FakeConsumer()
    worker = threading.Thread(target=consumer.on_delta, args=("Bonjour",))
    worker.start()
    worker.join()
    voice._unregister_active_latency(record)
    assert asyncio.run(consumer._send_or_edit("Bonjour")) is True

    path = Path(os.environ["DAEMON_CORE_LATENCY_LOG"])
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "voice_first_delta",
        "voice_first_visible",
    ]
    assert {event["trace_id"] for event in events} == {"stream-trace"}


def test_stream_probe_does_not_guess_between_concurrent_requests():
    class FakeConsumer:
        def __init__(self):
            self._already_sent = False
            self._last_sent_text = ""

        def on_delta(self, text):
            return None

        async def _send_or_edit(self, text, *, finalize=False, is_turn_final=True):
            return True

    assert voice._install_stream_probe(FakeConsumer) is True
    first = {"trace_id": "first", "api_request_id": "api-1"}
    second = {"trace_id": "second", "api_request_id": "api-2"}
    voice._register_active_latency(first)
    voice._register_active_latency(second)

    consumer = FakeConsumer()
    consumer.on_delta("Bonjour")

    assert not hasattr(consumer, "_daemon_latency_record")
    path = Path(os.environ["DAEMON_CORE_LATENCY_LOG"])
    assert not path.exists()
