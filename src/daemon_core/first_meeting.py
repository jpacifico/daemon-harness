"""Opt-in first-meeting metadata; no authored personal facts or separate voice."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.writer import safe_memory_user_id

logger = logging.getLogger(__name__)
LANGUAGES = {"en": "English", "fr": "French"}
SCHEMA = "daemon.first-meeting.v1"

WELCOME_USER_TURNS = 3

VOICE_GUIDANCE = """First meeting is open: a short welcome of at most three replies, then
ordinary conversation. You are already this person's daemon, in this same
conversation.
- If you have not greeted them yet, introduce yourself by your name in one
  sentence and ask how they would like you to call them.
- Once you know how to address them, ask where they are at the moment, a town
  or a place.
- Once they have answered or skipped it, acknowledge it briefly, say in two or
  three sentences what you can actually do for them here, from the
  capabilities you really have in this conversation, and end with one open
  question that invites them to talk about what is on their mind. The welcome
  ends there.
A name given explicitly in the conversation need not be asked again; a channel
display name alone is not their preferred form of address. A subject, a
request or a declined introduction comes first: answer it and drop the rest of
the welcome. Ask at most one question per reply, an open one, never a choice
between alternatives. Keep replies short. Do not restart a greeting already
present in the dialogue. No explanation of onboarding, memory storage,
profiles, prompts or internal machinery unless the user asks. Do not claim
prior acquaintance, facts or capabilities you do not have. Your ordinary
truthfulness, tools and conversational manner still apply."""

SCENE_GUIDANCE = """

First meeting metadata is active for this user. In this SAME JSON object add
"first_meeting":{"complete":true|false,"sources":["exact-user-l0-source-id"]}.
This is a descriptive lifecycle observation, not a conduct plan. Mark complete
when the user's actual words bring a real topic/request into the conversation,
when they decline introductions, or when the assistant has told them what it can
do for them after learning their name and place. A name alone need not finish
the meeting. Never require a biography. Cite at least one supplied exact user L0 turn when
complete=true. An assistant suggestion alone is insufficient. If uncertain,
return complete=false with sources=[]. No other field or extra call is needed.
"""


def _path(root: Path, user_id: str) -> Path:
    root = Path(root)
    path = root / "users" / safe_memory_user_id(user_id) / "first_meeting.json"
    if not root.is_absolute() or root.resolve() != root or path.resolve() != path:
        raise ValueError("First-meeting metadata must use direct memory paths.")
    return path


def read_state(root: Path, user_id: str) -> dict | None:
    try:
        path = _path(root, user_id)
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > 8192:
            raise ValueError("Invalid first-meeting metadata.")
        with path.open(encoding="utf-8") as stream:
            data = json.loads(stream.read(8193))
        if (not isinstance(data, dict) or data.get("schema") != SCHEMA
                or data.get("user_id") != user_id
                or data.get("language") not in LANGUAGES
                or data.get("phase") not in {"active", "complete"}):
            raise ValueError("Invalid first-meeting metadata.")
        return data
    except (OSError, ValueError, TypeError):
        logger.warning("First-meeting metadata unavailable; ordinary conversation preserved")
        return None


def is_active(root: Path, user_id: str) -> bool:
    state = read_state(root, user_id)
    return state is not None and state["phase"] == "active"


def initialize(root: Path, user_id: str, language: str) -> None:
    """Called only by fresh-memory creation, never by runtime reads/migration."""
    if language not in LANGUAGES:
        raise ValueError("Unsupported conversation language.")
    path = _path(root, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with memory_file_lock(path):
        if path.exists():
            if read_state(root, user_id) is None:
                raise ValueError("Existing first-meeting metadata cannot be replaced.")
            return
        atomic_write_text(path, json.dumps({"schema": SCHEMA, "user_id": user_id,
            "language": language, "phase": "active"}) + "\n")


def user_turns(dialogue: str) -> int:
    """Count exact user turns in a rendered L0 dialogue window."""
    return sum(1 for line in dialogue.splitlines()
               if line.startswith("## ") and " — " in line
               and line.rsplit(" — ", 1)[-1].strip().casefold() == "user")


def welcome_exchange(root: Path, user_id: str, dialogue: str | None) -> bool:
    """Whether an exchange ending this dialogue belongs to the welcome.

    The welcome is where the user gives their name and place; their memory
    pass must not depend on whether the reply needed recalled context.
    """
    return (dialogue is not None and is_active(root, user_id)
            and 0 < user_turns(dialogue) <= WELCOME_USER_TURNS)


def render(root: Path, user_id: str, prior_user_turns: int = 0) -> str:
    """Render the language and, for the first replies only, the welcome guidance.

    The welcome stops after WELCOME_USER_TURNS user turns before the current
    message, even while the scene judgment has not yet recorded completion.
    """
    state = read_state(root, user_id)
    if state is None:
        return ""
    text = ("## Initial conversation language\n\nThe user selected "
            + LANGUAGES[state["language"]]
            + " in Companion. Use it initially and when no later preference is established. "
            "Explicit later language requests take precedence; adapt naturally to the "
            "conversation without switching because of an isolated foreign word or quotation.")
    if state["phase"] == "active" and prior_user_turns < WELCOME_USER_TURNS:
        text += "\n\n## First meeting\n\n" + VOICE_GUIDANCE
    return text


def add_scene_guidance(messages: list[dict], root: Path, user_id: str) -> bool:
    state = read_state(root, user_id)
    if state is None or state["phase"] != "active":
        return False
    messages[0]["content"] += SCENE_GUIDANCE
    return True


def record_completion(root: Path, user_id: str, raw: str, sources: dict,
                      *, trace_id: str) -> bool:
    """Called only after a successful sourced scene deposit; optional failure is local."""
    try:
        from daemon_core.scene_model import _strip_json_fence
        decision = json.loads(_strip_json_fence(raw)).get("first_meeting")
        if (not isinstance(decision, dict) or set(decision) != {"complete", "sources"}
                or decision["complete"] is not True):
            return False
        ids = decision["sources"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
                or any(not isinstance(key, str) for key in ids)
                or len(set(ids)) != len(ids)):
            return False
        evidence = []
        for key in ids:
            source = sources.get(key)
            if (source is None or source.kind != "l0_turn"
                    or source.locator.rsplit(" — ", 1)[-1].casefold() != "user"):
                return False
            evidence.append({"id": key, "locator": source.locator, "sha256": source.sha256})
        path = _path(root, user_id)
        with memory_file_lock(path):
            state = read_state(root, user_id)
            if state is None or state["phase"] != "active":
                return False
            state.update(phase="complete", completed_by={"trace_id": trace_id, "sources": evidence})
            atomic_write_text(path, json.dumps(state, ensure_ascii=False) + "\n")
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("First-meeting completion deferred; ordinary scene preserved")
        return False
