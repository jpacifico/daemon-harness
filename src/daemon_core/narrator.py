import logging

from daemon_core.memory.reader import load_active_context, load_recent_dialogue, load_trajectory
from daemon_core.memory.writer import write_trajectory
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider

logger = logging.getLogger("daemon_core.narrator")

MAX_TRAJECTORY_CHARS = 700


def run_narrator(
    user_id: str,
    provider: LLMProvider,
    *,
    recent_dialogue: str | None = None,
) -> str:
    """Refresh the trajectory immediately before a remaining maintenance consumer.

    The canonical thin voice does not read this projection. Relanceur and REM
    request it on demand; the factual capture remains MemoryAgent's job.
    """
    try:
        previous = load_trajectory(user_id)
        messages = _build_narrator_messages(
            previous=previous,
            active_context=load_active_context(user_id),
            recent_dialogue=(
                load_recent_dialogue(user_id, max_turns=10)
                if recent_dialogue is None
                else recent_dialogue
            ),
        )
        log_llm_prompt(logger, "NARRATEUR", messages)
        raw = provider.complete(messages=messages, temperature=0.2)
        trajectory = _clean_trajectory(raw)
        if not trajectory:
            return previous
        if trajectory != previous.strip():
            write_trajectory(user_id, trajectory)
            logger.info("Trajectoire mise à jour | user_id=%s | chars=%s", user_id, len(trajectory))
        return trajectory
    except Exception as exc:
        logger.info("Narrateur ignoré | error=%s", exc.__class__.__name__)
        return ""


def _build_narrator_messages(previous: str, active_context: str, recent_dialogue: str) -> list[dict]:
    system = """You are the narrator of Daemon Harness.

You maintain the trajectory: the user's current arc, in two or three present-tense sentences written as a narrative — what they are coming out of, what they are in, where they are heading.

Rules:
- This is not a list of facts: it is a montage. Connect the moments; the contrast between before and now often carries the meaning.
- Stay factual about events, but allow yourself a reading of their state (fatigue, momentum, decompression, tension) grounded in actual textual cues.
- Invent nothing. No advice, no plans on their behalf, no value judgments.
- Two or three sentences, never more. No heading, no list, no bold.
- If the arc has not changed since the previous trajectory, return it verbatim.

Respond only with the trajectory text, without commentary."""
    user = f"""Current time:
{current_time_context()}

Previous trajectory:
{previous.strip() or "No trajectory written yet."}

Active context:
{active_context.strip() or "No active context."}

Recent dialogue:
{recent_dialogue.strip()[-3000:] or "No recent dialogue."}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _clean_trajectory(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()
    return text[:MAX_TRAJECTORY_CHARS].strip()
