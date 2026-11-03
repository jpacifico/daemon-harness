"""Common follow-up lifecycle for host-owned after-turn adapters."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from daemon_core import followup as fu


ScheduleFollowup = Callable[[fu.FollowUp], str | None]
CancelFollowup = Callable[[fu.FollowUp], bool]
OwnsOrigin = Callable[[dict[str, Any]], bool]


class FollowupAfterTurnObserver:
    """Persist one neutral intent, then delegate scheduling to its host."""

    def __init__(
        self,
        *,
        schedule_followup: ScheduleFollowup,
        cancel_followup: CancelFollowup,
        owns_origin: OwnsOrigin,
        logger: logging.Logger,
        user_id: str = "",
    ) -> None:
        self.schedule_followup = schedule_followup
        self.cancel_followup = cancel_followup
        self.owns_origin = owns_origin
        self.logger = logger
        self.user_id = user_id

    def __call__(self, result: Any, executions: tuple[Any, ...]) -> None:
        if result is None or not fu.followups_enabled():
            return
        latest = max(
            executions,
            key=lambda item: (
                item.job.source_turn_started_at,
                item.job.created_at,
                item.job.turn_id,
            ),
        )
        user_id = self.user_id or latest.job.user_id
        expired = fu.expire_overdue_pending_intents(user_id)
        if expired:
            self.logger.info(
                "Relances expirées avant programmation | count=%d",
                len(expired),
            )
        # A daemon restored by a move schedules its reminders here, on the
        # channel of this host's first completed turn.
        moved = ([] if fu.is_disabled(user_id) else
                 fu.adopt_moved(user_id, latest.job.origin or {}, self.schedule_followup))
        if moved:
            self.logger.info("Relances déménagées programmées | count=%d", len(moved))

        pref = getattr(result, "followups_pref", None)
        if pref and pref.get("action") == "disable":
            fu.set_disabled(user_id, True)
            for old in fu.list_intents(user_id, state="pending"):
                if self.owns_origin(old.origin or {}):
                    self.cancel_followup(old)
                else:
                    # The foreign host job may still wake once, but the common
                    # pending state makes that wake silent and non-repeatable.
                    fu.cancel(old.id)
            self.logger.info("Relances coupées")
            return
        if pref and pref.get("action") == "enable":
            fu.set_disabled(user_id, False)
            self.logger.info("Relances réactivées")

        intent = getattr(result, "followup", None)
        if not intent or fu.is_disabled(user_id):
            return
        fire_at = fu.clamp_to_waking(intent["fire_at"])
        if fu.followups_dry_run():
            self.logger.info("Relance dry-run | fire_at=%s", fire_at)
            return
        if len(fu.active_pending_intents(user_id)) >= fu.MAX_ACTIVE_FOLLOWUPS:
            return
        if fu.delivered_today_count(user_id) >= fu.MAX_DELIVERED_PER_DAY:
            return
        new = fu.new_followup(
            user_id,
            fire_at,
            intent["topic"],
            intent["evidence"],
            intent.get("angle", "check-in"),
            latest.job.origin or {},
        )
        job_id = self.schedule_followup(new)
        self.logger.info(
            "Relance %s | followup=%s | job=%s | fire_at=%s",
            "programmée" if job_id else "non programmée",
            new.id,
            job_id or "absent",
            fire_at,
        )


__all__ = ["FollowupAfterTurnObserver"]
