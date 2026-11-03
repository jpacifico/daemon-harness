"""Legacy verdict handoff and shared conversation-only contract.

The autonomous response engine is retired. Native host loops own responses;
canonical composition lives in canonical_voice. Verdict persistence remains
for the Hermes compatibility path when canonical voice is disabled.
"""

import json
import logging
import time
from dataclasses import asdict

from daemon_core.context_router import ContextRoute
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT

logger = logging.getLogger("daemon_core.sovereign")

VERDICT_MAX_AGE_SECONDS = 120

# Shared by the non-delegating thin composer; not a pre-turn radar.
YIELD_SENTINEL = "[OUTIL_HOTE]"
YIELD_SECTION = f"""

# Limits of this turn (firm rule)
In this turn, you may take NO action beyond conversation: no web search or
fresh information from the world, no reminder, alarm or scheduled task,
no reading or writing an external service (calendar, files), no execution.
Your memory supplied above, however, is reliable and usable. If correctly
answering the request REQUIRES one of those actions, respond EXACTLY
"{YIELD_SENTINEL}", alone, without any other word: another arm will handle it
immediately afterward. Never promise an action you cannot perform in this turn."""



def _verdict_path(user_id: str):
    return MEMORY_ROOT / "levels" / f"scene_verdict_{safe_memory_user_id(user_id)}.json"


def write_scene_verdict(
    user_id: str,
    message: str,
    route: ContextRoute,
    selected: list[str],
    *,
    trace_id: str = "",
    prefetch_started_at: float | None = None,
) -> None:
    """Store the prefetch scene judgment for the sovereign voice.

    Closure hysteresis and its consecutive_closures counter were removed.
    fil_clos remains a scene judgment passed to the ordinary voice;
    only explicitly delegated initiative invokes the moment judge."""
    try:
        path = _verdict_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "message": message,
                    "route": asdict(route),
                    "selected": list(selected or []),
                    "trace_id": trace_id,
                    "prefetch_started_at": prefetch_started_at,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.info("Verdict de scène non déposé | error=%s", exc.__class__.__name__)


def _read_scene_verdict_payload(user_id: str, prompt_user_text: str) -> dict | None:
    """Return a validated handoff payload for the current user turn."""
    try:
        path = _verdict_path(user_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts", 0)) > VERDICT_MAX_AGE_SECONDS:
            return None
        message = str(data.get("message", ""))
        if not message or message not in prompt_user_text:
            return None
        data["message"] = message
        data["route"] = ContextRoute(**data.get("route", {}))
        data["selected"] = [s for s in data.get("selected", []) if isinstance(s, str)]
        return data
    except Exception as exc:
        logger.info("Verdict de scène illisible | error=%s", exc.__class__.__name__)
        return None


def read_scene_verdict(user_id: str, prompt_user_text: str) -> tuple[str, ContextRoute, list[str]] | None:
    """Read the prefetch judgment when it matches the current turn.

    prompt_user_text may contain injected context around the exact user text,
    so match by containment rather than equality.
    Return (user_message, route, selected_memories), or None."""
    data = _read_scene_verdict_payload(user_id, prompt_user_text)
    if data is None:
        return None
    return data["message"], data["route"], data["selected"]


def read_scene_verdict_with_trace(
    user_id: str, prompt_user_text: str
) -> tuple[str, ContextRoute, list[str], str, float | None] | None:
    """Read the same verdict plus its latency correlation metadata."""
    data = _read_scene_verdict_payload(user_id, prompt_user_text)
    if data is None:
        return None
    started_at = data.get("prefetch_started_at")
    try:
        started_at = float(started_at) if started_at is not None else None
    except (TypeError, ValueError):
        started_at = None
    return (
        data["message"],
        data["route"],
        data["selected"],
        str(data.get("trace_id", "")),
        started_at,
    )
