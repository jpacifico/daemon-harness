"""OpenClaw side effects attached to the neutral after-turn service."""

from __future__ import annotations

import importlib.util
import logging
from functools import lru_cache
from pathlib import Path

from integrations.after_turn_service.followup_observer import (
    FollowupAfterTurnObserver,
)


logger = logging.getLogger("daemon_core.openclaw_after_turn")


@lru_cache(maxsize=1)
def _followup_cron():
    path = Path(__file__).resolve().parents[1] / "openclaw_plugin" / "followup_cron.py"
    spec = importlib.util.spec_from_file_location(
        "daemon_core_openclaw_followup_cron",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("OpenClaw follow-up adapter is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenClawAfterTurnObserver(FollowupAfterTurnObserver):
    def __init__(self, user_id: str = "") -> None:
        super().__init__(
            schedule_followup=lambda followup: _followup_cron().schedule_followup(
                followup,
            ),
            cancel_followup=lambda followup: _followup_cron().cancel_followup(
                followup,
            ),
            owns_origin=lambda origin: origin.get("platform") == "openclaw",
            logger=logger,
            user_id=user_id,
        )


__all__ = ["OpenClawAfterTurnObserver"]
