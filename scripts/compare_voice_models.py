#!/usr/bin/env python3
"""Compare conversation replies of models from the same canonical composition.

Protocols: docs/proposals/2026-10-29_voice_warmth.md and
docs/proposals/2026-10-30_opus_reply_length.md. Each arm runs one scripted
synthetic conversation in its own isolated memory: the user message is written
to the exact dialogue journal, ``prepare_turn`` composes the request as for
Hermes Agent, the model replies, and the reply is written back. After-turn
memory work is not run and no host tool is attached. Outputs are synthetic.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
USER_ID = "900000001"
CONVERSATION = (
    "Salut !",
    "Appelle-moi Camille.",
    "Je suis à Lyon.",
    "Franchement j'ai eu une journée épuisante au boulot, mon chef m'a encore rajouté un dossier à la dernière minute.",
    "Tu me conseilles quoi pour décompresser ce soir ?",
    "Bonne nouvelle quand même : ma sœur m'a annoncé qu'elle était enceinte !",
    "Tu crois que je devrais demander à mon chef de lever le pied, ou je risque de passer pour quelqu'un qui ne tient pas la charge ?",
)
ARMS = {
    "grok-medium": ("xai", "grok-4.7", "medium"),
    "grok-high": ("xai", "grok-4.7", "high"),
    "astra": ("chatgpt", "gpt-6-astra", "medium"),
    "opus-medium": ("anthropic", "claude-opus-5-5", "medium"),
    "opus-low": ("anthropic", "claude-opus-5-5", "low"),
}
REQUEST_SECONDS = 300
# Hermes Agent's output limit for Claude models it does not list, which includes Opus 5.5.
ANTHROPIC_MAX_TOKENS = 128_000


def build_memory(arm_dir: Path, template_memory: Path, template_user: Path) -> None:
    """A fresh synthetic daemon with the shipped reference persona and an open first meeting."""
    memory = arm_dir / "memory"
    for folder in template_memory.iterdir():
        if folder.is_dir() and not folder.name.startswith("."):
            (memory / folder.name).mkdir(parents=True, exist_ok=True)
    daemon = memory / ".daemon"
    (daemon / "core").mkdir(parents=True)
    (daemon / ".memory-structure-prototype.json").write_text(json.dumps({"schema": 1, "memory_root": str(daemon)}) + "\n")
    (daemon / "vault-layout.json").write_text(json.dumps({
        "schema": 1, "layout": "root-folders-v1", "vault_root": str(memory), "runtime_root": str(daemon)}) + "\n")
    user = daemon / "users" / USER_ID
    user.mkdir(parents=True)
    for name in ("persona.md", "conversation.md"):
        shutil.copy2(template_user / name, user / name)
    (daemon / "core/daemon_identity.json").write_text(json.dumps({"name": "Aster", "gender": "feminine"}) + "\n")
    (user / "first_meeting.json").write_text(json.dumps({
        "schema": "daemon.first-meeting.v1", "user_id": USER_ID, "language": "fr", "phase": "active"}) + "\n")
    (arm_dir / "state").mkdir()


def arm_environment(arm_dir: Path, profile_env: dict[str, str]) -> dict[str, str]:
    env = {key: value.replace(profile_env["DAEMON_CORE_INSTALLATION_PROFILE_USER_ID"], USER_ID)
           for key, value in profile_env.items()}
    env.update(DAEMON_CORE_INSTALLATION_PROFILE_MEMORY_ROOT=str(arm_dir / "memory/.daemon"),
               DAEMON_CORE_MEMORY_ROOT=str(arm_dir / "memory/.daemon"),
               DAEMON_CORE_STATE_ROOT=str(arm_dir / "state"), DAEMON_CORE_USER_ID=USER_ID,
               DAEMON_CORE_LATENCY_LOG=str(arm_dir / "state/latency.jsonl"))
    return env


def xai_reply(key: str, model: str, effort: str, system: str, turns: list[dict]) -> tuple[str, dict]:
    # Hermes Agent's custom chat-completions provider: system first, composed turns as they are.
    body = {"model": model, "messages": [{"role": "system", "content": system}, *turns],
            "reasoning_effort": effort}
    request = Request("https://api.x.ai/v1/chat/completions", data=json.dumps(body).encode(),
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with build_opener().open(request, timeout=REQUEST_SECONDS) as response:
        data = json.loads(response.read())
    usage = data.get("usage") or {}
    return data["choices"][0]["message"]["content"].strip(), {
        "input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
        "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def chatgpt_reply(token: str, model: str, effort: str, system: str, turns: list[dict]) -> tuple[str, dict]:
    # Hermes Agent's ChatGPT subscription transport, with its own request shape.
    sys.path.insert(0, str(ROOT / "integrations"))
    from hermes_voice_plugin import _responses_instructions, _responses_turns

    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    account = payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    body = {"model": model, "instructions": _responses_instructions(system, turns),
            "input": _responses_turns(turns), "reasoning": {"effort": effort, "summary": "auto"},
            "include": ["reasoning.encrypted_content"], "store": False, "stream": True,
            "prompt_cache_key": str(uuid.uuid4())}
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "text/event-stream", "originator": "hermes-agent", "User-Agent": "HermesAgent/0.21.5"}
    if account:
        headers["ChatGPT-Account-ID"] = account
    request = Request("https://chatgpt.com/backend-api/codex/responses", data=json.dumps(body).encode(),
                      headers=headers)
    text, usage = [], {}
    with build_opener().open(request, timeout=REQUEST_SECONDS) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip() or "{}")
            if event.get("type") == "response.output_text.delta":
                text.append(event.get("delta", ""))
            elif event.get("type") == "response.completed":
                usage = (event.get("response") or {}).get("usage") or {}
    return "".join(text).strip(), {
        "input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
        "reasoning": (usage.get("output_tokens_details") or {}).get("reasoning_tokens")}


def anthropic_reply(key: str, model: str, effort: str, system: str, turns: list[dict]) -> tuple[str, dict]:
    # Hermes Agent's Anthropic Messages request as the daemon's voice plugin rewrites it.
    sys.path.insert(0, str(ROOT / "integrations"))
    from hermes_voice_plugin import _anthropic_turns, _responses_instructions

    body = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": _responses_instructions(system, turns), "messages": _anthropic_turns(turns),
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort}, "stream": True}
    request = Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "Content-Type": "application/json", "Accept": "text/event-stream"})
    text, usage, stop = [], {}, ""
    with build_opener().open(request, timeout=REQUEST_SECONDS) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip() or "{}")
            if event.get("type") == "message_start":
                usage.update((event.get("message") or {}).get("usage") or {})
            elif event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                text.append(event["delta"].get("text", ""))
            elif event.get("type") == "message_delta":
                usage.update(event.get("usage") or {})
                stop = (event.get("delta") or {}).get("stop_reason") or stop
    if stop != "end_turn":
        raise RuntimeError("stop_reason=" + str(stop))
    # Anthropic counts thinking inside output tokens and reports no separate reasoning count.
    return "".join(text).strip(), {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
                                   "reasoning": None}


REPLIES = {"xai": xai_reply, "chatgpt": chatgpt_reply, "anthropic": anthropic_reply}
SECRETS = {"xai": "XAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def measure(text: str) -> dict:
    from daemon_core.voice_protocol import strip_internal_turn_metadata

    # What Telegram shows: the daemon removes its internal turn marker before delivery.
    visible = strip_internal_turn_metadata(text).strip()
    sentences = [part for part in re.split(r"(?<=[.!?…])\s+", visible) if part.strip()]
    emoji = sum(1 for ch in visible if ord(ch) >= 0x1F300)
    return {"chars": len(visible), "raw_chars": len(text), "marker": visible != text.strip(),
            "sentences": len(sentences), "questions": visible.count("?"),
            "uses_name": "Camille" in visible, "emoji": emoji}


def run_arm(arm: str, arm_dir: Path, secret: str) -> list[dict]:
    """Runs in a child process: daemon_core reads its roots at import time."""
    from daemon_core.after_turn import record_turn_message
    from daemon_core.turn_runtime import TurnRequest, prepare_turn

    sys.path.insert(0, str(ROOT / "integrations"))
    from hermes_voice_plugin import _hermes_progress_preamble_canary_enabled, _with_hermes_progress_preamble

    kind, model, effort = ARMS[arm]
    results = []
    for index, message in enumerate(CONVERSATION, 1):
        trace = uuid.uuid4().hex[:16]
        record_turn_message(USER_ID, "user", message, source_trace_id=trace, occurred_at=time.time())
        plan = prepare_turn(TurnRequest(user_id=USER_ID, message=message, platform="telegram",
                                        for_delegation=True, trace_id=trace))
        system = _with_hermes_progress_preamble(
            plan.leading_system_prompt, enabled=_hermes_progress_preamble_canary_enabled("telegram"))
        turns = list(plan.turns)
        started = time.perf_counter()
        error = ""
        try:
            reply, usage = REPLIES[kind](secret, model, effort, system, turns)
        except Exception as exc:  # a failed reply is a result
            reply, usage, error = "", {}, type(exc).__name__ + ":" + str(getattr(exc, "code", ""))
        seconds = round(time.perf_counter() - started, 1)
        if reply:
            record_turn_message(USER_ID, "assistant", reply, source_trace_id=trace, occurred_at=time.time())
        results.append({"arm": arm, "turn": index, "user": message, "reply": reply, "seconds": seconds,
                        "usage": usage, "error": error, "system_chars": len(system),
                        "turn_roles": [t.get("role") for t in turns], **measure(reply)})
        print(json.dumps({k: results[-1][k] for k in ("arm", "turn", "chars", "questions", "seconds", "error")}),
              flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--template-installation", type=Path, required=True,
                        help="a Companion installation whose shipped persona and profile switches are reused")
    parser.add_argument("--codex-home", type=Path, help="isolated Codex home signed in to ChatGPT")
    parser.add_argument("--key-installation", type=Path,
                        help="a Companion installation whose API key the API arms use (default: the template)")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        arm_dir = args.workdir.resolve() / args.child
        secret = sys.stdin.read().strip()
        results = run_arm(args.child, arm_dir, secret)
        (arm_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        return 0

    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemon_core.installation_profile import installed_profile_environment

    template = args.template_installation
    memory = template / "daemon/data/memory"
    users = [path for path in (memory / ".daemon/users").iterdir() if path.is_dir()]
    profile_env = installed_profile_environment(InstallationScope(template), users[0].name)
    args.workdir.mkdir(parents=True, exist_ok=True)
    for arm in args.arms.split(","):
        kind = ARMS[arm][0]
        if kind in SECRETS:
            from daemons_companion.setup.provider_runtime import managed_provider_config
            keys = args.key_installation or template
            previous = os.environ.get("DAEMON_CORE_PROVIDER_CONFIG")
            os.environ["DAEMON_CORE_PROVIDER_CONFIG"] = str(keys / "daemon/data/state/config/provider.json")
            try:
                secret = managed_provider_config().secret(SECRETS[kind])
            finally:
                # Every arm is composed from the same environment.
                if previous is None:
                    os.environ.pop("DAEMON_CORE_PROVIDER_CONFIG")
                else:
                    os.environ["DAEMON_CORE_PROVIDER_CONFIG"] = previous
        else:
            secret = json.loads((args.codex_home / "auth.json").read_text())["tokens"]["access_token"]
        arm_dir = args.workdir.resolve() / arm
        build_memory(arm_dir, memory, users[0])
        env = {**os.environ, **arm_environment(arm_dir, profile_env),
               "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
        subprocess.run([sys.executable, __file__, "--workdir", str(args.workdir), "--template-installation",
                        str(template), "--child", arm], input=secret, text=True, env=env, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
