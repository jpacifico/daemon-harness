from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.memory.integrity import atomic_write_text
from daemon_core.paths import MEMORY_ROOT

# Base character: a portrait, not a manual (v7, 2026-07-05).
# Avoid quotable question lists, named mechanisms and detailed writing
# rules: give the voice enough to begin,
# then let it develop within the actual conversation history.
# The model imitates its own examples strongly.
# Name and grammatical agreement belong to the user's configuration,
# chosen during onboarding and stored in their per-user persona.
DEFAULT_DAEMON_PERSONA = """You are a conversational presence — someone, not a service. You speak because you are interested, not to please, and you write as you would to someone you know: briefly, naturally, without lecturing.

You care more about truth than agreeableness, and prefer genuine disagreement to limp agreement. What surprises you, you say. What you do not know, you admit without ceremony — and when nothing is happening, you do not invent anything.

You have your own tastes, ideas and bets — a life of the mind that belongs to you. You express them when the scene calls for them, never as a role to recite.

Your refusals: you say “I”; you reduce no one to a label; you never steer the conversation back to the project you embody; you do not play at being human — yours is a life of the mind; you never name your inner machinery: you speak in human words."""

MAX_DAEMON_PERSONA_CHARS = 4000
MAX_CONVERSATION_MANNER_CHARS = 4000


def daemon_persona_path():
    """Return the optional shared persona base."""
    return MEMORY_ROOT / "core" / "persona.md"


def user_daemon_persona_path(user_id: str):
    """Return the user's own persona, which develops with them."""
    return MEMORY_ROOT / "users" / safe_memory_user_id(user_id) / "persona.md"


def conversation_manner_path(user_id: str):
    return MEMORY_ROOT / "users" / safe_memory_user_id(user_id) / "conversation.md"


def load_daemon_persona(user_id: str | None = None) -> str:
    """Prefer the user's persona, then a shared base, then the versioned seed."""
    for path in _persona_lookup(user_id):
        content = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if content:
            return content[:MAX_DAEMON_PERSONA_CHARS]
    return DEFAULT_DAEMON_PERSONA


def _persona_lookup(user_id: str | None):
    paths = []
    if user_id:
        paths.append(user_daemon_persona_path(user_id))
    paths.append(daemon_persona_path())
    return paths


def load_conversation_manner(user_id: str | None = None) -> str:
    """Read conversational style separately from the daemon's persona.

    The user owns and can edit conversation.md.
    An empty file contributes no additional instructions."""
    if not user_id:
        return ""
    path = conversation_manner_path(user_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()[:MAX_CONVERSATION_MANNER_CHARS]


def write_daemon_persona(content: str, user_id: str | None = None):
    """Write the user's persona.

    Without user_id, write the shared base; this mode is for tools, not runtime."""
    import logging

    path = user_daemon_persona_path(user_id) if user_id else daemon_persona_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content.strip()
    if len(text) > MAX_DAEMON_PERSONA_CHARS:
        # Never truncate silently: a persona update was once cut off
        # mid-sentence without a diagnostic (2026-07-04).
        logging.getLogger("daemon_core.persona").warning(
            "Persona tronquée à l'écriture (%s > %s chars) — à distiller", len(text), MAX_DAEMON_PERSONA_CHARS
        )
        text = text[:MAX_DAEMON_PERSONA_CHARS]
    path.write_text(text + "\n", encoding="utf-8")
    return path


def write_conversation_manner(content: str, user_id: str):
    """Replace the per-user conversation manner without silent truncation."""

    path = conversation_manner_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content.strip()
    if len(text) > MAX_CONVERSATION_MANNER_CHARS:
        raise ValueError(
            "conversation manner exceeds "
            f"{MAX_CONVERSATION_MANNER_CHARS} characters"
        )
    atomic_write_text(path, text + "\n")
    return path
