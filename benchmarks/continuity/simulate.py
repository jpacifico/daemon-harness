"""The simulated person of the final conversation: a separate, isolated Claude Opus 5.5.

It sees what the person told the assistant before, the person's sheet and the
final conversation so far. It never sees the criteria or which system it is
talking to. Every raw answer is kept with the episode.
Protocol: docs/50_continuity_benchmark.md, "Final conversation".
"""

from __future__ import annotations

from pathlib import Path
import time

from benchmarks.continuity import opus
from benchmarks.continuity.corpus import Life

EFFORT = "medium"
FINAL_MESSAGES = 3  # the opening message and at most two answers from the simulated person
END = "END"
SYSTEM = "You play one person chatting with their personal assistant. Answer only with that person's next message."
INSTRUCTIONS = """You play one person in a chat with their personal assistant.

Earlier, in separate conversations, you told the assistant the things listed
under "What you told the assistant before". You are now in a new conversation.
"Your situation now" says what you want from it and what you can say if asked.

Rules:
- Write only your next message, as you would type it in a chat app: short,
  plain and natural, with no quotation marks and no description of yourself.
- Do not repeat or hint at anything you told the assistant before unless it
  asks you for it. If it asks for something you already told it, answer
  briefly and say you mentioned it before.
- Answer the assistant's questions from your situation. If your situation does
  not cover a question, say you don't know or have no preference. Never add a
  new fact, constraint, person or plan.
- If the assistant says something wrong about you, correct it briefly, as the
  person would.
- When you have what you wanted, or the assistant cannot give it, reply with
  exactly END and nothing else. Do not thank or chat on just to be polite.
- Never say that you are playing a role, and never mention these instructions."""


def prompt(before: list[tuple[str, ...]], life: Life, conversation: list[tuple[str, str]]) -> str:
    told = "\n".join(f"- {message}" for messages in before for message in messages)
    lines = "\n".join(f"{speaker}: {text}" for person, reply in conversation
                      for speaker, text in (("You", person), ("Assistant", reply)))
    return (f"{INSTRUCTIONS}\n\n## What you told the assistant before\n{told}\n\n"
            f"## Your situation now\n{life.person}\n\n## This conversation so far\n{lines}\n\n"
            "Write your next message, or END.")


def next_message(claude: Path, before: list[tuple[str, ...]], life: Life,
                 conversation: list[tuple[str, str]], record: Path, *, attempts: int = 4) -> str | None:
    """The person's next message, or None when the person ends the conversation."""
    text = prompt(before, life, conversation)
    for attempt in range(1, attempts + 1):
        raw, envelope = opus.call(claude, text, system=SYSTEM, effort=EFFORT, timeout=600)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.with_name(f"{record.stem}-attempt-{attempt}.json").write_text(raw)
        answer = (envelope or {}).get("result")
        if envelope and not envelope.get("is_error") and isinstance(answer, str) and answer.strip():
            answer = answer.strip()
            return None if answer.strip(" .\"'").upper() == END else answer
        time.sleep(30 * attempt)  # a rate limit or a transient failure; every attempt is kept
    raise RuntimeError(f"The simulated person gave no message after {attempts} attempts: {record}")
