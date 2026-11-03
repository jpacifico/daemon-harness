import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.providers.codex_cli_provider import (
    CodexCliProvider,
    _extract_codex_usage,
    _format_codex_prompt,
)
from daemon_core.providers.factory import (
    build_conversation_provider,
    build_memory_provider,
    build_routing_provider,
)


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_codex_prompt_wraps_messages_without_losing_roles():
    prompt = _format_codex_prompt(
        [
            {"role": "system", "content": "Réponds en JSON strict."},
            {"role": "user", "content": "Bonjour"},
        ]
    )

    assert 'pure LLM provider' in prompt
    assert "## system" in prompt
    assert "Réponds en JSON strict." in prompt
    assert "## user" in prompt
    assert "Bonjour" in prompt


def test_codex_cli_provider_reads_output_last_message(monkeypatch):
    calls = {}

    def fake_run(cmd, input, text, capture_output, timeout, check, encoding):
        calls["cmd"] = cmd
        calls["input"] = input
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text('{"reason":"ok"}', encoding="utf-8")
        return FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CODEX_CLI_BIN", "codex-test")
    provider = CodexCliProvider(model="gpt-test")

    result = provider.complete([{"role": "user", "content": "Salut"}])

    assert result == '{"reason":"ok"}'
    assert calls["cmd"][:6] == ["codex-test", "-a", "never", "-s", "read-only", "exec"]
    assert "--model" in calls["cmd"]
    assert "gpt-test" in calls["cmd"]
    assert "-s" in calls["cmd"]
    assert "read-only" in calls["cmd"]
    assert "--json" in calls["cmd"]
    assert calls["cmd"][-1] == "-"
    assert "Salut" in calls["input"]


def test_codex_cli_provider_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, input, text, capture_output, timeout, check, encoding):
        return FakeCompletedProcess(returncode=1, stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = CodexCliProvider(model="gpt-test")

    try:
        provider.complete([{"role": "user", "content": "Salut"}])
    except RuntimeError as exc:
        assert "not logged in" in str(exc)
    else:
        raise AssertionError("RuntimeError attendu")


def test_factory_can_build_memory_codex_cli_provider(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "gpt-memory")

    provider = build_memory_provider()

    assert isinstance(provider, CodexCliProvider)
    assert provider.model == "gpt-memory"
    assert provider.reasoning_effort == "high"


def test_codex_cli_command_includes_reasoning_effort_when_set(monkeypatch):
    calls = {}

    def fake_run(cmd, input, text, capture_output, timeout, check, encoding):
        calls["cmd"] = cmd
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = CodexCliProvider(model="gpt-test", reasoning_effort="low")

    provider.complete([{"role": "user", "content": "Salut"}])

    idx = calls["cmd"].index("-c")
    assert calls["cmd"][idx + 1] == 'model_reasoning_effort="low"'


def test_codex_cli_provider_records_native_usage(monkeypatch):
    captured = []

    def fake_run(cmd, input, text, capture_output, timeout, check, encoding):
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("ok", encoding="utf-8")
        stdout = (
            '{"type":"thread.started","thread_id":"test"}\n'
            '{"type":"turn.completed","usage":'
            '{"input_tokens":120,"cached_input_tokens":30,"output_tokens":8}}\n'
        )
        return FakeCompletedProcess(stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "daemon_core.providers.codex_cli_provider.record_provider_usage",
        captured.append,
    )

    result = CodexCliProvider(model="gpt-test").complete(
        [{"role": "user", "content": "Salut"}]
    )

    assert result == "ok"
    assert captured == [
        {
            "input_tokens": 120,
            "cached_input_tokens": 30,
            "output_tokens": 8,
            "input_uncached_tokens": 90,
            "total_tokens": 128,
        }
    ]


def test_extract_codex_usage_accepts_rollout_token_count_events():
    stdout = (
        '{"type":"event_msg","payload":{"type":"token_count","info":'
        '{"total_token_usage":{"input_tokens":250,"output_tokens":12,'
        '"total_tokens":262}}}}\n'
    )

    assert _extract_codex_usage(stdout) == {
        "input_tokens": 250,
        "output_tokens": 12,
        "total_tokens": 262,
        "input_uncached_tokens": 250,
    }


def test_factory_reads_reasoning_effort_env_and_rejects_invalid(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("MEMORY_REASONING_EFFORT", "low")
    assert build_memory_provider().reasoning_effort == "low"

    monkeypatch.setenv("MEMORY_REASONING_EFFORT", "medium")
    assert build_memory_provider().reasoning_effort == "medium"

    # Minimal reasoning once failed silently; retain the safe default.
    monkeypatch.setenv("MEMORY_REASONING_EFFORT", "minimal")
    assert build_memory_provider().reasoning_effort == "high"


def test_factory_defaults_internal_routing_and_conversation_to_medium(monkeypatch):
    monkeypatch.setenv("ROUTING_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("CONVERSATION_LLM_PROVIDER", "codex_cli")
    monkeypatch.delenv("ROUTING_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("CONVERSATION_REASONING_EFFORT", raising=False)

    assert build_routing_provider().reasoning_effort == "medium"
    assert build_conversation_provider().reasoning_effort == "medium"


def test_factory_respects_internal_reasoning_overrides(monkeypatch):
    monkeypatch.setenv("ROUTING_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("CONVERSATION_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("ROUTING_REASONING_EFFORT", "low")
    monkeypatch.setenv("CONVERSATION_REASONING_EFFORT", "high")

    assert build_routing_provider().reasoning_effort == "low"
    assert build_conversation_provider().reasoning_effort == "high"


def test_codex_cli_real_pipe_preserves_unicode_under_windows_legacy_encoding(monkeypatch):
    # A real child, no Codex credentials: emulate its UTF-8 stdin/JSONL/file
    # contract while forcing Python's Windows-compatible default pipe codec.
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp1252')
    expected = 'Café, déjà — 東京 🔷'
    script = (
        "import json,sys;from pathlib import Path;"
        "prompt=sys.stdin.buffer.read().decode('utf-8');"
        f"assert {expected!r} in prompt;"
        "Path(sys.argv[1]).write_text(prompt,encoding='utf-8');"
        "sys.stdout.buffer.write(json.dumps({'type':'note','text':'café 🔷'},ensure_ascii=False).encode('utf-8'))"
    )
    provider = CodexCliProvider(model='synthetic')
    monkeypatch.setattr(provider, '_command', lambda *, output_path, workdir:
        [sys.executable, '-B', '-c', script, str(output_path)])
    assert expected in provider.complete([{'role': 'user', 'content': expected}])
