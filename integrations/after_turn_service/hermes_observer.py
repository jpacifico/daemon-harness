"""Hermes-only side effects attached to the neutral after-turn service."""

from __future__ import annotations

import importlib.util
import logging
import os
from functools import lru_cache
from pathlib import Path

from integrations.after_turn_service.followup_observer import (
    FollowupAfterTurnObserver,
)


logger = logging.getLogger("daemon_core.hermes_after_turn")


@lru_cache(maxsize=1)
def _followup_cron():
    path = Path(__file__).resolve().parents[1] / "hermes_plugin" / "followup_cron.py"
    spec = importlib.util.spec_from_file_location(
        "daemon_core_followup_cron",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Hermes follow-up adapter is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesAfterTurnObserver(FollowupAfterTurnObserver):
    def __init__(self, user_id: str = "", *, hermes_home: str = "") -> None:
        self.hermes_home = (
            hermes_home
            or os.getenv("HERMES_HOME", "").strip()
            or str(os.path.expanduser("~/.hermes"))
        )
        super().__init__(
            schedule_followup=lambda followup: _followup_cron().schedule_followup(
                followup,
                hermes_home=self.hermes_home,
            ),
            cancel_followup=lambda followup: _followup_cron().cancel_followup(
                followup,
                hermes_home=self.hermes_home,
            ),
            owns_origin=lambda origin: origin.get("platform") != "openclaw",
            logger=logger,
            user_id=user_id,
        )


__all__ = ["HermesAfterTurnObserver"]
