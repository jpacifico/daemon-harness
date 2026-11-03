import json
import os
import subprocess
import tempfile
from pathlib import Path

from .base import LLMProvider
from .telemetry import record_provider_usage


class CodexCliProvider(LLMProvider):
    """Experimental provider using a local Codex CLI ChatGPT/Codex login.

    This is intended for async memory work, not user-facing low-latency replies.
    It delegates one completion to `codex exec` and reads the final answer from
    `--output-last-message`.
    """

    def __init__(self, model: str | None = None, reasoning_effort: str | None = None) -> None:
        self.binary = os.getenv("CODEX_CLI_BIN", "codex")
        self.model = model or os.getenv("CODEX_CLI_MODEL", "gpt-5.5")
        self.profile = os.getenv("CODEX_CLI_PROFILE", "").strip()
        self.timeout_seconds = float(os.getenv("CODEX_CLI_TIMEOUT_SECONDS", "300"))
        self.reasoning_effort = reasoning_effort

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        with tempfile.TemporaryDirectory(prefix="daemon-codex-cli-") as tmp:
            workdir = Path(os.getenv("CODEX_CLI_WORKDIR", tmp))
            output_path = Path(tmp) / "last_message.txt"
            cmd = self._command(output_path=output_path, workdir=workdir)
            prompt = _format_codex_prompt(messages)
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"Codex CLI indisponible: {stderr[:500]}")
            usage = _extract_codex_usage(result.stdout)
            if usage:
                record_provider_usage(usage)
            if output_path.exists():
                output = output_path.read_text(encoding="utf-8").strip()
            else:
                output = result.stdout.strip()
            if not output:
                raise RuntimeError("Codex CLI n'a retourné aucune réponse.")
            return output

    def _command(self, output_path: Path, workdir: Path) -> list[str]:
        cmd = [
            self.binary,
            "-a",
            "never",
            "-s",
            "read-only",
            "exec",
            "--model",
            self.model,
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "--json",
            "--output-last-message",
            str(output_path),
            "--cd",
            str(workdir),
        ]
        if self.reasoning_effort:
            # Quote the TOML value; otherwise Codex rejects the -c argument.
            cmd.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
        if self.profile:
            cmd.extend(["--profile", self.profile])
        cmd.append("-")
        return cmd


def _extract_codex_usage(stdout: str) -> dict[str, int] | None:
    """Read provider-native usage from Codex JSONL without retaining content."""

    usage: dict[str, int] | None = None
    for raw_line in str(stdout or "").splitlines():
        try:
            event = json.loads(raw_line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue

        candidate = None
        if event.get("type") == "turn.completed":
            candidate = event.get("usage")
        elif event.get("type") == "event_msg":
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    candidate = info.get("total_token_usage")
        normalized = _numeric_codex_usage(candidate)
        if normalized:
            usage = normalized
    return usage


def _numeric_codex_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        result[str(key)] = max(0, int(raw))
    input_tokens = result.get("input_tokens")
    output_tokens = result.get("output_tokens")
    if input_tokens is not None:
        result["input_uncached_tokens"] = max(
            0,
            input_tokens
            - result.get("cached_input_tokens", 0)
            - result.get("cache_write_input_tokens", 0),
        )
    if input_tokens is not None and output_tokens is not None:
        result.setdefault("total_tokens", input_tokens + output_tokens)
    return result or None


def _format_codex_prompt(messages: list[dict]) -> str:
    parts = [
        'You are used as a pure LLM provider for Daemon Harness.',
        'Do not read the filesystem, execute commands or modify any file.',
        'Do not mention Codex, the CLI or this technical wrapper.',
        'Respond only with the final content expected by the last message.',
        "",
        "# Messages",
    ]
    for message in messages:
        role = str(message.get("role", "user")).strip() or "user"
        content = str(message.get("content", "")).strip()
        parts.append(f"\n## {role}\n{content}")
    return "\n".join(parts).strip() + "\n"
