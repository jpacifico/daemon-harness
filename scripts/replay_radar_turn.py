#!/usr/bin/env python3
"""Replay one after-turn memory radar call on a copy of a daemon's memory.

Protocol: docs/proposals/2026-10-30_radar_temporal_facts.md (field replay).
The radar is rebuilt as the after-turn loop builds it: the installation's
continuity profile with its memory root moved to the copy, the daemon's routing
provider, the memory loaders, and the dialogue up to the daemon's reply. The
installation itself is only read. The copy and the output hold personal data:
keep both outside Git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = re.compile(r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (user|assistant)$", re.MULTILINE)


def turn_through(journal: str, reply_at: str) -> tuple[str, str]:
    """The journal up to the daemon's reply at ``reply_at`` and the user message it answered."""
    entries = list(ENTRY.finditer(journal))
    for index, entry in enumerate(entries):
        if entry.group(1) == reply_at and entry.group(2) == "assistant":
            end = entries[index + 1].start() if index + 1 < len(entries) else len(journal)
            previous = entries[index - 1] if index else None
            if previous is None or previous.group(2) != "user":
                raise ValueError("The reply does not follow a user message.")
            return journal[:end].rstrip() + "\n", journal[previous.end():entry.start()].strip()
    raise ValueError("No daemon reply at " + reply_at)


def environment(installation: Path, memory_copy: Path, state: Path, user_id: str) -> dict[str, str]:
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemon_core.installation_profile import installed_profile_environment

    data = installation / "daemon/data"
    profile = installed_profile_environment(InstallationScope(installation), user_id)
    env = {key: value.replace(str(data / "memory"), str(memory_copy)).replace(str(data / "state"), str(state))
           for key, value in profile.items()}
    env.update(DAEMON_CORE_MEMORY_ROOT=str(memory_copy / ".daemon"),
               DAEMON_CORE_INSTALLATION_PROFILE_MEMORY_ROOT=str(memory_copy / ".daemon"),
               DAEMON_CORE_STATE_ROOT=str(state), DAEMON_CORE_USER_ID=user_id)
    if any(str(data) in value for value in env.values()):
        raise ValueError("A path still points into the daemon's own data.")
    # Read only: the routing provider's settings and key reference.
    env["DAEMON_CORE_PROVIDER_CONFIG"] = str(data / "state/config/provider.json")
    return env


def routing_provider(codex_model: str = ""):
    """The daemon's routing provider, or a subscription model through the daemon's Codex provider."""
    if codex_model:
        from daemon_core.providers.codex_cli_provider import CodexCliProvider
        from daemon_core.providers.factory import DEFAULT_ROUTING_REASONING_EFFORT
        return CodexCliProvider(model=codex_model, reasoning_effort=DEFAULT_ROUTING_REASONING_EFFORT)
    from daemon_core.providers.factory import build_routing_provider
    return build_routing_provider()


def replay(user_id: str, reply_at: str, repetitions: int, *, before_turn: bool = False,
           codex_model: str = "") -> list[dict]:
    from daemon_core.context_router import route_context_depth
    from daemon_core.loop import ROUTER_HISTORY_TURNS
    from daemon_core.memory import reader

    journal = reader.MEMORY_ROOT / "levels/L0_ground_zero/conversations" / f"{user_id}_{reply_at[:10]}.md"
    window, message = turn_through(journal.read_text(encoding="utf-8"), reply_at)
    if before_turn:
        # Diagnostic variant: the history stops before the latest exchange.
        entries = list(ENTRY.finditer(window))
        window = window[:entries[-2].start()].rstrip() + "\n"
    # The after-turn loop reads up to 40 turns through the reply, then gives the radar 14.
    dialogue = reader.limit_recent_dialogue(reader.limit_recent_dialogue(window, 40), ROUTER_HISTORY_TURNS)
    provider = routing_provider(codex_model)
    rows = []
    for repetition in range(repetitions):
        started = time.perf_counter()
        route = route_context_depth(
            message=message, provider=provider, recent_dialogue=dialogue,
            user_context=reader.load_user_context(user_id), user_agenda=reader.load_user_agenda(user_id),
            moment_memory=reader.load_active_context(user_id),
            current_discussion=reader.load_conversation_state(user_id) or reader.load_immediate_focus(user_id))
        rows.append({"reply_at": reply_at, "model": codex_model or "routing", "before_turn": before_turn,
                     "repetition": repetition + 1,
                     "flag": route.needs_temporal_memory,
                     "mode": route.mode, "depth": route.context_depth, "confidence": route.confidence,
                     "reason": route.reason, "active_request": route.active_request,
                     "fallback": route.reason.startswith("Memory radar unavailable"),
                     "seconds": round(time.perf_counter() - started, 1)})
        print(json.dumps({k: rows[-1][k] for k in ("reply_at", "repetition", "flag", "mode", "fallback", "seconds")}),
              flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True, help="the daemon's installation, only read")
    parser.add_argument("--memory-copy", type=Path, required=True, help="a disposable copy of its daemon/data/memory")
    parser.add_argument("--state", type=Path, required=True, help="a disposable state root")
    parser.add_argument("--user", required=True)
    parser.add_argument("--reply-at", action="append", required=True, help="the daemon reply's journal time")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--history-before-turn", action="store_true",
                        help="diagnostic: cut the history before the latest user message")
    parser.add_argument("--codex-model", default="", help="replace the routing provider by this subscription model")
    parser.add_argument("--codex-home", type=Path, help="isolated Codex home signed in to ChatGPT")
    parser.add_argument("--codex-bin", type=Path, help="pinned Codex CLI")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.child:
        # The memory modules read their roots at import time: replay in a child
        # process that starts with the copy's environment.
        args.state.mkdir(parents=True, exist_ok=True)
        env = environment(args.installation, args.memory_copy.resolve(), args.state.resolve(), args.user)
        if args.codex_model:
            env.update(CODEX_HOME=str(args.codex_home), CODEX_CLI_BIN=str(args.codex_bin))
        return subprocess.run([sys.executable, __file__, *sys.argv[1:], "--child"],
                              env={**os.environ, **env, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}).returncode
    rows = [row for reply_at in args.reply_at
            for row in replay(args.user, reply_at, args.repetitions, before_turn=args.history_before_turn,
                              codex_model=args.codex_model)]
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
