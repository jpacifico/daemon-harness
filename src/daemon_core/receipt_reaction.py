"""Delayed receipt reaction while memory preparation is still running.

After about three seconds, a Telegram reaction acknowledges receipt without
adding a waiting message to the conversation or its journal. It makes no
claim about the message's content. Fast turns produce no reaction.

This is a delivery mechanism, not a semantic judgment.
Set DAEMON_CORE_RECEIPT_REACTION=off to disable it, or to a supported
Telegram emoji to change it. Failures remain silent."""

from __future__ import annotations

import json
import logging
import os
import threading
from urllib import request as _urllib_request

logger = logging.getLogger(__name__)

RECEIPT_REACTION_ENV = "DAEMON_CORE_RECEIPT_REACTION"
DEFAULT_EMOJI = "👀"
DEFAULT_DELAY_SECONDS = 3.0


def receipt_reaction_emoji() -> str | None:
    value = os.getenv(RECEIPT_REACTION_ENV, DEFAULT_EMOJI).strip()
    if value.lower() in {"off", "0", "false", ""}:
        return None
    return value


def schedule_receipt_reaction(
    origin: dict | None,
    message_id: str | None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
) -> threading.Event | None:
    """Schedule the delayed reaction and return its cancellation event.

    The caller sets the event when preparation finishes.
    Completion before the delay suppresses the reaction."""
    emoji = receipt_reaction_emoji()
    if emoji is None:
        return None
    origin = origin or {}
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if (
        not token
        or origin.get("platform") != "telegram"
        or not origin.get("chat_id")
        or not message_id
    ):
        return None
    cancel = threading.Event()
    url = f"https://api.telegram.org/bot{token}/setMessageReaction"
    payload = json.dumps(
        {
            "chat_id": origin["chat_id"],
            "message_id": int(message_id),
            "reaction": [{"type": "emoji", "emoji": emoji}],
        }
    ).encode("utf-8")

    def _fire() -> None:
        if cancel.wait(delay_seconds):
            return  # Preparation completed before the delay: no reaction needed.
        try:
            req = _urllib_request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            _urllib_request.urlopen(req, timeout=3)
        except Exception as exc:
            logger.debug("réaction de réception ratée | %s", exc.__class__.__name__)

    threading.Thread(target=_fire, name="daemon-receipt-reaction", daemon=True).start()
    return cancel
