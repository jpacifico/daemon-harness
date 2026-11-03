"""Ephemeral reading of the conversation's current texture.

This is not personal memory and does not replace L0. It is a compact, revisable
LLM judgment used as the live thread for both ordinary voice and initiative.
"""

import logging

from daemon_core.memory.reader import load_conversation_state, load_recent_dialogue
from daemon_core.memory.writer import write_conversation_state
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider

logger = logging.getLogger("daemon_core.conversation_state")

MAX_CONVERSATION_STATE_CHARS = 1400
CONVERSATION_STATE_TURNS = 40


def refresh_conversation_state(
    user_id: str,
    provider: LLMProvider,
    recent_dialogue: str | None = None,
) -> str:
    """Refresh the temporary conversational reading after a completed turn."""
    try:
        if recent_dialogue is None:
            recent_dialogue = load_recent_dialogue(
                user_id,
                max_turns=CONVERSATION_STATE_TURNS,
            )
        if not recent_dialogue.strip():
            return ""
        messages = build_conversation_state_messages(
            recent_dialogue=recent_dialogue,
            previous=load_conversation_state(user_id),
        )
        log_llm_prompt(logger, "CONVERSATION STATE", messages)
        raw = provider.complete(messages=messages, temperature=0.2)
        state = clean_conversation_state(raw)
        if state:
            write_conversation_state(user_id, state)
            logger.info("Conversation state refreshed | user_id=%s | chars=%s", user_id, len(state))
        return state
    except Exception as exc:
        logger.info("Conversation state ignored | user_id=%s | error=%s", user_id, exc.__class__.__name__)
        return ""


def project_scene_conversation_state(
    user_id: str,
    *,
    reading: str,
    conversation: str,
) -> str:
    """Expose the Scene Model reading through the legacy compatibility view.

    The canonical voice already consumes the scene directly.  Keeping this
    projection preserves older and fallback readers without paying a second
    semantic judgment after a successful scene revision.  The general reading
    carries the concrete live matter; the conversational texture carries its
    current manner and freshness.
    """

    try:
        body = "\n\n".join(
            value.strip()
            for value in (reading, conversation)
            if value.strip()
        )
        state = clean_conversation_state(body)
        write_conversation_state(user_id, state)
        logger.info(
            "Conversation state projected from scene | user_id=%s | chars=%s",
            user_id,
            len(state),
        )
        return state
    except Exception as exc:
        logger.info(
            "Conversation state scene projection ignored | user_id=%s | error=%s",
            user_id,
            exc.__class__.__name__,
        )
        return ""


def build_conversation_state_messages(recent_dialogue: str, previous: str = "") -> list[dict]:
    system = """You read the current texture of an ongoing conversation for another conversational faculty.

Make one compact, revisable judgment: what is still alive in the exchange, what has just become tired or was declined, which openings have already been tried without taking, and what degree of lightness, depth, continuity or novelty the person currently seems to invite.

This is not a biography, a summary of everything known, a list of rules, or a plan for the next reply. Do not turn an important recurring subject into a live subject merely because it matters to the person. Distinguish importance from present conversational freshness. Distinguish strictly what the assistant attempted from what the person actually engaged with: an opening with no subsequent user response is untested, never successful or alive by itself; asking what a question means, politely acknowledging it, or merely not rejecting it is not engagement with its subject; only a substantive contribution confirms that the opening took. An explicit user rejection outweighs the assistant's framing. A user's claim about what they previously said is a request or correction to verify, not proof of the reported fact; do not settle that fact from the claim or from the assistant's reply. Distinguish a completed conversational move from a closed subject: a joke can have landed and one question can have been answered while the broader subject remains alive. Never call a subject closed merely because one answer was received. A fresh detail volunteered by the person, a correction, or a substantive continuation keeps that subject alive. Call it closed only when the person ends or declines it, clearly moves elsewhere, or repeated acknowledgements bring no new material. Do not prolong an accomplished moment artificially. Base every observation on the dialogue. Write short natural prose under the heading `# Conversation state`. If no state is clear, say so plainly. Never address the person."""
    user = f"""Previous reading (revisable, never authoritative):
{previous.strip() or "None."}

Recent dialogue:
{recent_dialogue.strip()[-12000:]}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def clean_conversation_state(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()
    if not text:
        return ""
    if not text.startswith("# Conversation state"):
        text = "# Conversation state\n\n" + text
    if len(text) > MAX_CONVERSATION_STATE_CHARS:
        text = text[:MAX_CONVERSATION_STATE_CHARS].rsplit("\n", 1)[0].rstrip()
    return text
