import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core import followup as fu_mod
from daemon_core.followup import (
    TZ,
    _compose_messages,
    active_pending_intents,
    cancel,
    clamp_to_waking,
    delivered_today_count,
    expire_overdue_pending_intents,
    fire,
    format_status,
    is_disabled,
    list_intents,
    load_intent,
    new_followup,
    set_disabled,
)
from daemon_core.memory.reader import load_dialogue_since
from datetime import datetime as _dt
from daemon_core.providers.base import LLMProvider


class MessageProvider(LLMProvider):
    def __init__(
        self,
        text: str = "",
        *,
        action: str = "deliver",
        fire_at: str = "",
        reason: str = "test",
    ) -> None:
        self.text = text
        self.action = action
        self.fire_at = fire_at
        self.reason = reason
        self.messages = None

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return json.dumps(
            {
                "action": self.action,
                "message": self.text,
                "fire_at": self.fire_at,
                "reason": self.reason,
            }
        )


class RawProvider(LLMProvider):
    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return self.text


class RaisingProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise RuntimeError("modèle indisponible")


@pytest.fixture(autouse=True)
def _tmp_memory(monkeypatch, tmp_path):
    root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.paths.MEMORY_ROOT", root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", root)
    monkeypatch.setattr("daemon_core.persona.MEMORY_ROOT", root)


def _at(hour: int = 18, minute: int = 0) -> datetime:
    return datetime(2026, 6, 30, hour, minute, tzinfo=TZ)


def _make(**kw) -> str:
    defaults = dict(
        user_id="u1",
        fire_at="2026-06-30T18:00",
        topic="restitution POC IA d'aujourd'hui",
        evidence="heureusement vendredi soir libération",
        angle="check-in",
    )
    defaults.update(kw)
    return new_followup(**defaults).id


# --- store -------------------------------------------------------------------
def test_store_roundtrip_and_list_and_cancel():
    fid = _make()
    fu = load_intent(fid)
    assert fu is not None and fu.state == "pending" and fu.user_id == "u1"
    assert fu.topic == "restitution POC IA d'aujourd'hui"

    assert [x.id for x in list_intents("u1", state="pending")] == [fid]
    assert list_intents("autre") == []

    assert cancel(fid) is True
    assert load_intent(fid).state == "cancelled"
    assert cancel(fid) is False  # No longer pending.


def test_followup_uses_conversation_manner_not_archived_preferences(tmp_path):
    user_root = tmp_path / "memory" / "users" / "u1"
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "conversation.md").write_text(
        "# Manière\n\n- Une présence brève et naturelle.\n",
        encoding="utf-8",
    )
    (user_root / "preferences.md").write_text(
        "# Manuel historique\n\n- FORMULATION_OBSOLETE.\n",
        encoding="utf-8",
    )
    followup = load_intent(_make())

    messages = _compose_messages(followup, now=_at())
    prompt = "\n".join(message["content"] for message in messages)

    assert "Une présence brève et naturelle" in prompt
    assert "FORMULATION_OBSOLETE" not in prompt


# --- tir ---------------------------------------------------------------------
def test_fire_delivers_and_records():
    fid = _make()
    msg = fire(fid, provider=MessageProvider("Coucou, alors cette restitution ?"), now=_at())
    assert msg == "Coucou, alors cette restitution ?"
    fu = load_intent(fid)
    assert fu.state == "delivered"
    assert fu.outcome_reason == "llm_deliver:test"
    assert fu.delivered_at is not None
    assert delivered_today_count("u1", now=_at()) == 1


def test_fire_cancel_no_delivery():
    fid = _make()
    msg = fire(
        fid,
        provider=MessageProvider(action="cancel", reason="moment passé"),
        now=_at(),
    )
    assert msg == ""
    fu = load_intent(fid)
    assert fu.state == "cancelled"
    assert fu.outcome_reason == "llm_cancel:moment passé"
    assert delivered_today_count("u1", now=_at()) == 0


def test_fire_empty_output_is_silent():
    fid = _make()
    assert fire(fid, provider=RawProvider("   "), now=_at()) == ""
    assert load_intent(fid).outcome_reason == "invalid_verdict"


def test_fire_blocked_when_disabled():
    set_disabled("u1", True)
    assert is_disabled("u1") is True
    fid = _make()
    assert fire(fid, provider=MessageProvider("coucou"), now=_at()) == ""
    assert load_intent(fid).state == "silent"
    assert load_intent(fid).outcome_reason == "off-switch actif"
    assert delivered_today_count("u1", now=_at()) == 0


def test_fire_blocked_outside_waking_hours():
    fid = _make()
    assert fire(fid, provider=MessageProvider("coucou"), now=_at(hour=3)) == ""


def test_fire_blocked_when_window_missed():
    fid = _make(fire_at="2026-06-30T06:00")
    # Ten hours late exceeds the six-hour grace window.
    assert fire(fid, provider=MessageProvider("coucou"), now=_at(hour=16)) == ""


def test_fire_dry_run_never_delivers():
    fid = _make()
    msg = fire(fid, provider=MessageProvider("coucou"), now=_at(), dry_run=True)
    assert msg == ""
    assert load_intent(fid).state == "silent"
    assert load_intent(fid).outcome_reason == "dry_run"
    assert delivered_today_count("u1", now=_at()) == 0


def test_fire_is_idempotent():
    fid = _make()
    fire(fid, provider=MessageProvider("coucou"), now=_at())
    # Second attempt: already fired, so remain silent.
    assert fire(fid, provider=MessageProvider("re-coucou"), now=_at()) == ""


def test_fire_lock_blocks_concurrent_second_tir():
    fid = _make()

    assert fu_mod._acquire_fire_lock(fid, _at()) is True

    class Forbidden(LLMProvider):
        def complete(self, messages, temperature=0.4):
            raise AssertionError("le doublon ne doit pas appeler le modèle")

    try:
        assert fire(fid, provider=Forbidden(), now=_at()) == ""
        assert load_intent(fid).state == "pending"
        assert delivered_today_count("u1", now=_at()) == 0
    finally:
        fu_mod._release_fire_lock(fid)


def test_fire_provider_failure_is_silent_not_crash():
    fid = _make()
    assert fire(fid, provider=RaisingProvider(), now=_at()) == ""
    assert load_intent(fid).state == "silent"
    assert load_intent(fid).outcome_reason == "provider_failure:RuntimeError"


def test_fire_unknown_id_is_silent():
    assert fire("doesnotexist", provider=MessageProvider("coucou"), now=_at()) == ""


# --- clamp_to_waking ---------------------------------------------------------
def test_clamp_keeps_valid_future_waking_time():
    now = _dt(2026, 6, 30, 10, 0, tzinfo=TZ)
    assert clamp_to_waking("2026-06-30T18:00", now=now) == "2026-06-30T18:00"


def test_clamp_pushes_early_morning_into_waking():
    now = _dt(2026, 6, 30, 10, 0, tzinfo=TZ)
    out = clamp_to_waking("2026-07-01T05:00", now=now)
    assert out == "2026-07-01T08:00"


def test_clamp_pulls_late_night_into_waking():
    now = _dt(2026, 6, 30, 10, 0, tzinfo=TZ)
    out = clamp_to_waking("2026-07-01T23:30", now=now)
    assert out == "2026-07-01T22:00"


def test_clamp_keeps_late_evening_same_night():
    # An evening request at 22:30 must stay on that evening, not tomorrow morning.
    now = _dt(2026, 6, 30, 21, 50, tzinfo=TZ)
    out = clamp_to_waking("2026-06-30T22:30", now=now)
    assert out == "2026-06-30T22:30"


def test_clamp_past_time_is_pushed_to_future():
    now = _dt(2026, 6, 30, 12, 0, tzinfo=TZ)
    out = clamp_to_waking("2026-06-30T09:00", now=now)  # Already in the past.
    assert _dt.fromisoformat(out) > now.replace(tzinfo=None)


def _patch_l0(monkeypatch, tmp_path):
    # The autouse _tmp_memory fixture already isolates memory.
    return tmp_path / "memory"


def test_fire_prompt_composes_from_present(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    fid = _make(topic="prendre des nouvelles du dimanche")
    from daemon_core.memory.reader import ground_zero_conversation_path
    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    l0_path.write_text(
        "\n## 2026-06-30T17:00:00 — user\n\nÀ moi de choisir la deuxième chanson non ?\n",
        encoding="utf-8",
    )
    provider = MessageProvider(action="cancel")

    fire(fid, provider=provider, now=_at(18, 0))

    system = provider.messages[0]["content"]
    # The present scene can change the intent without creating a daemon obligation.
    assert 'genuine thread left pending' in system
    assert 'the intention is only an impulse' in system
    # Supply the exact absence interval to the judge: 17:00 to 18:00, about one hour.
    assert 'approximately 1 hour' in system
    assert "deliver|reschedule|cancel" in system


def test_fire_prompt_keeps_resolution_after_intent_creation(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    fid = _make(
        fire_at="2026-06-30T18:00",
        topic="prendre des nouvelles de sa fatigue après la nuit",
        evidence="Je vais me coucher",
    )
    intent = load_intent(fid)
    intent.created_at = "2026-06-30T06:22+02:00"
    fu_mod.save_intent(intent)
    from daemon_core.memory.reader import ground_zero_conversation_path

    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        ("06:22:19", "assistant", "Bien dormi, cette fois ?"),
        ("06:23:01", "user", "Un peu mieux, malgré un réveil très tôt."),
    ]
    entries.extend(
        (f"06:{minute:02d}:00", "user", f"Message récent {minute}")
        for minute in range(24, 35)
    )
    l0_path.write_text(
        "".join(
            f"\n## 2026-06-30T{stamp} — {role}\n\n{body}\n"
            for stamp, role, body in entries
        ),
        encoding="utf-8",
    )
    provider = MessageProvider(action="cancel", reason="déjà répondu")

    assert fire(fid, provider=provider, now=_at(18, 0)) == ""

    prompt = provider.messages[1]["content"]
    assert "Bien dormi, cette fois ?" in prompt
    assert "Un peu mieux, malgré un réveil très tôt." in prompt
    assert "Message récent 34" in prompt


def test_dialogue_since_keeps_both_edges_when_bounded(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    from daemon_core.memory.reader import ground_zero_conversation_path

    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    l0_path.write_text(
        "".join(
            f"\n## 2026-06-30T10:{minute:02d}:00 — user\n\nMessage {minute}\n"
            for minute in range(10)
        ),
        encoding="utf-8",
    )

    dialogue = load_dialogue_since(
        "u1",
        "2026-06-30T10:00+02:00",
        max_turns=4,
    )

    assert "Message 0" in dialogue
    assert "Message 1" in dialogue
    assert "Message 8" in dialogue
    assert "Message 9" in dialogue
    assert "6 intermediate exact message(s) omitted" in dialogue
    assert "Message 4" not in dialogue


def test_dialogue_since_invalid_boundary_falls_back_cleanly():
    assert load_dialogue_since("u1", "") == ""


def test_presence_prompt_prioritizes_situated_intention(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    fid = _make(
        angle="presence",
        topic="Micro-présence aux Terrasses du Port",
        evidence="Nous sommes au frais",
    )
    from daemon_core.memory.reader import ground_zero_conversation_path
    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    l0_path.write_text(
        "\n## 2026-06-30T17:00:00 — user\n\nDemain à la cool on a dit\n",
        encoding="utf-8",
    )
    provider = MessageProvider(action="cancel")

    fire(fid, provider=provider, now=_at(18, 0))

    system = provider.messages[0]["content"]
    assert 'not to automatically replace it with the latest topic' in system
    assert '3 to 12 words' in system
    assert 'Do not claim to be physically' in system
    assert 'genuine thread left pending' not in system


def test_fire_delivery_lands_in_l0(tmp_path, monkeypatch):
    memory_root = _patch_l0(monkeypatch, tmp_path)
    fid = _make(angle="reaction")
    provider = MessageProvider("Alors, tu l'as trouvée, ta pierre nocturne ?")

    message = fire(fid, provider=provider, now=_at(18, 0))

    assert "pierre nocturne" in message
    l0_files = list((memory_root / "levels" / "L0_ground_zero" / "conversations").glob("u1_*.md"))
    assert l0_files, "la relance livrée doit s'écrire au L0"
    content = l0_files[0].read_text(encoding="utf-8")
    assert "pierre nocturne" in content
    assert "— assistant" in content


def test_fire_can_reschedule_when_conversation_active(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    fid = _make()
    from daemon_core.memory.reader import ground_zero_conversation_path
    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    # Last exchange at 17:50, delivery attempted at 18:00: 10 minutes is below 45.
    l0_path.write_text("\n## 2026-06-30T17:50:00 — user\n\nÀ tout à l'heure\n", encoding="utf-8")

    provider = MessageProvider(
        action="reschedule",
        fire_at="2026-06-30T19:15",
        reason="laisser retomber la conversation",
    )
    message = fire(fid, provider=provider, now=_at(18, 0))

    assert message == ""
    updated = load_intent(fid)
    assert updated.state == "pending"
    assert updated.fire_at == "2026-06-30T19:15"
    assert updated.job_id is None
    assert updated.reschedule_count == 1
    assert updated.outcome_reason == "rescheduled:laisser retomber la conversation"
    assert '10 minutes ago' in provider.messages[0]["content"]


def test_fire_can_deliver_naturally_during_recent_conversation(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    fid = _make(angle="reaction")
    from daemon_core.memory.reader import ground_zero_conversation_path

    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    l0_path.write_text(
        "\n## 2026-06-30T17:58:00 — user\n\nCette idée me travaille encore.\n",
        encoding="utf-8",
    )
    provider = MessageProvider("Je crois qu'elle touche à quelque chose de plus vaste.")

    message = fire(fid, provider=provider, now=_at(18, 0))

    assert "quelque chose de plus vaste" in message
    assert load_intent(fid).state == "delivered"


def test_fire_proceeds_after_real_absence(tmp_path, monkeypatch):
    _patch_l0(monkeypatch, tmp_path)
    fid = _make()
    from daemon_core.memory.reader import ground_zero_conversation_path
    l0_path = ground_zero_conversation_path("u1")
    l0_path.parent.mkdir(parents=True, exist_ok=True)
    # Last exchange at 16:00, delivery at 18:00: 120 minutes exceeds 45.
    l0_path.write_text("\n## 2026-06-30T16:00:00 — user\n\nÀ ce soir\n", encoding="utf-8")
    provider = MessageProvider("Alors, cette journée ?")

    message = fire(fid, provider=provider, now=_at(18, 0))
    assert "cette journée" in message
    system = provider.messages[0]["content"]
    assert 'Choose ONE action' in system
    assert 'A recent conversation does not on its own dictate any action' in system


def test_delivered_today_deduplicates_identical_legacy_lines():
    log = fu_mod._delivered_log("u1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "2026-06-30T18:00+02:00\n"
        "2026-06-30T18:00+02:00\n"
        "2026-06-30T19:00+02:00 abc123\n",
        encoding="utf-8",
    )

    assert delivered_today_count("u1", now=_at()) == 2


def test_active_pending_ignores_long_horizon_intents():
    near = _make(fire_at="2026-06-30T20:00")
    far = _make(fire_at="2026-07-10T18:00")

    active = active_pending_intents("u1", now=_at(18, 0))

    assert [fu.id for fu in active] == [near]
    assert far in [fu.id for fu in list_intents("u1", state="pending")]


def test_overdue_pending_intents_do_not_block_and_are_expired():
    stale = _make(fire_at="2026-06-29T15:30")
    future = _make(fire_at="2026-07-02T20:15")
    now = _at(17, 0)

    assert [fu.id for fu in active_pending_intents("u1", now=now)] == [future]

    expired = expire_overdue_pending_intents("u1", now=now)

    assert expired == [stale]
    assert load_intent(stale).state == "expired"
    assert load_intent(stale).outcome_reason == "window_missed_before_fire"
    assert [fu.id for fu in list_intents("u1", state="pending")] == [future]


def test_status_exposes_pending_scopes_and_outcomes(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_FOLLOWUPS", "on")
    _make(fire_at="2026-06-30T20:00", topic="relance proche")
    _make(fire_at="2026-07-10T18:00", topic="relance lointaine")
    silent_id = _make(topic="relance silencieuse")
    fire(silent_id, provider=RawProvider("pas du JSON"), now=_at())

    status = format_status("u1", now=_at())

    assert "followups_enabled: yes" in status
    assert "active_pending: 1/3" in status
    assert "[active]" in status
    assert "[long]" in status
    assert "silent" in status
    assert "reason=invalid_verdict" in status
