from __future__ import annotations

from types import SimpleNamespace

from integrations.after_turn_service import openclaw_observer as observer_module


def test_openclaw_observer_preserves_native_delivery_origin(monkeypatch):
    scheduled = []
    from integrations.after_turn_service import followup_observer as common

    monkeypatch.setattr(common.fu, "followups_enabled", lambda: True)
    monkeypatch.setattr(common.fu, "expire_overdue_pending_intents", lambda _user: ())
    monkeypatch.setattr(common.fu, "is_disabled", lambda _user: False)
    monkeypatch.setattr(common.fu, "followups_dry_run", lambda: False)
    monkeypatch.setattr(common.fu, "active_pending_intents", lambda _user: ())
    monkeypatch.setattr(common.fu, "delivered_today_count", lambda _user: 0)
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
    monkeypatch.setattr(
        observer_module,
        "_followup_cron",
        lambda: SimpleNamespace(
            schedule_followup=lambda followup: scheduled.append(followup) or "job-1",
            cancel_followup=lambda _followup: True,
        ),
    )
    origin = {
        "platform": "openclaw",
        "session_id": "session-1",
        "channel": "telegram",
        "to": "telegram:12345",
    }
    result = SimpleNamespace(
        followups_pref=None,
        followup={
            "fire_at": "2026-08-31T18:30",
            "topic": "reprendre le fil",
            "evidence": "on se reparle",
            "angle": "check-in",
        },
    )
    execution = SimpleNamespace(
        job=SimpleNamespace(
            user_id="personal",
            source_turn_started_at=1.0,
            created_at="2026-08-31T17:00:00Z",
            turn_id="turn-1",
            origin=origin,
        )
    )

    observer_module.OpenClawAfterTurnObserver()(result, (execution,))

    assert scheduled[0].origin == origin


def test_moved_reminders_are_adopted_on_this_hosts_turn_unless_disabled(monkeypatch):
    from integrations.after_turn_service import followup_observer as common

    monkeypatch.setattr(common.fu, "followups_enabled", lambda: True)
    monkeypatch.setattr(common.fu, "expire_overdue_pending_intents", lambda _user: ())
    adopted = []
    monkeypatch.setattr(common.fu, "adopt_moved",
                        lambda user, origin, schedule: adopted.append((user, origin)) or ["moved-1"])
    monkeypatch.setattr(observer_module, "_followup_cron", lambda: SimpleNamespace(
        schedule_followup=lambda followup: "job", cancel_followup=lambda _followup: True))
    origin = {"platform": "openclaw", "channel": "telegram", "to": "telegram:12345"}
    execution = SimpleNamespace(job=SimpleNamespace(user_id="12345", source_turn_started_at=1.0,
                                                    created_at="2026-10-27T10:00:00Z", turn_id="t", origin=origin))
    result = SimpleNamespace(followups_pref=None, followup=None)
    for disabled, expected in ((True, []), (False, [("12345", origin)])):
        adopted.clear()
        monkeypatch.setattr(common.fu, "is_disabled", lambda _user, value=disabled: value)
        observer_module.OpenClawAfterTurnObserver()(result, (execution,))
        assert adopted == expected
