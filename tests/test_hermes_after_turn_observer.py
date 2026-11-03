from __future__ import annotations

from types import SimpleNamespace

from integrations.after_turn_service import followup_observer as common
from integrations.after_turn_service import hermes_observer as observer_module


def test_hermes_observer_preserves_followup_origin(monkeypatch):
    scheduled = []
    monkeypatch.setattr(common.fu, "followups_enabled", lambda: True)
    monkeypatch.setattr(
        common.fu,
        "expire_overdue_pending_intents",
        lambda _user_id: (),
    )
    monkeypatch.setattr(common.fu, "is_disabled", lambda _user_id: False)
    monkeypatch.setattr(common.fu, "followups_dry_run", lambda: False)
    monkeypatch.setattr(
        common.fu,
        "active_pending_intents",
        lambda _user_id: (),
    )
    monkeypatch.setattr(
        common.fu,
        "delivered_today_count",
        lambda _user_id: 0,
    )
    monkeypatch.setattr(common.fu, "clamp_to_waking", lambda value: value)

    def new_followup(user_id, fire_at, topic, evidence, angle, origin):
        return SimpleNamespace(
            id="followup-1",
            user_id=user_id,
            fire_at=fire_at,
            topic=topic,
            evidence=evidence,
            angle=angle,
            origin=origin,
        )

    monkeypatch.setattr(common.fu, "new_followup", new_followup)
    cron = SimpleNamespace(
        schedule_followup=(
            lambda followup, **kwargs: scheduled.append((followup, kwargs))
            or "job-1"
        )
    )
    monkeypatch.setattr(observer_module, "_followup_cron", lambda: cron)
    result = SimpleNamespace(
        followups_pref=None,
        followup={
            "fire_at": "2026-08-29T09:00:00+02:00",
            "topic": "reprendre le sujet",
            "evidence": "demande explicite",
            "angle": "check-in",
        },
    )
    execution = SimpleNamespace(
        job=SimpleNamespace(
            user_id="personal",
            source_turn_started_at=1.0,
            created_at="2026-08-28T20:00:00Z",
            turn_id="turn-1",
            origin={"platform": "telegram", "chat_id": "private"},
        )
    )

    observer_module.HermesAfterTurnObserver(
        hermes_home="/tmp/hermes-test",
    )(result, (execution,))

    followup, kwargs = scheduled[0]
    assert followup.user_id == "personal"
    assert followup.origin == {"platform": "telegram", "chat_id": "private"}
    assert kwargs == {"hermes_home": "/tmp/hermes-test"}
