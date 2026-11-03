import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.relanceur import build_moment_judge_messages, run_relanceur
from daemon_core.providers.base import LLMProvider


def _setup(tmp_path, monkeypatch, dialogue: str) -> Path:
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.paths.MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_FOLLOWUPS", "on")
    l0 = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    (l0 / f"garden_{today}.md").write_text(dialogue, encoding="utf-8")
    return memory_root


class PlantingProvider(LLMProvider):
    def __init__(self, evidence: str) -> None:
        self.evidence = evidence
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return json.dumps({
            "followup": {
                "should_schedule": True,
                "topic": "Retour sur la démo de demain",
                "evidence": self.evidence,
                "angle": "encouragement",
                "fire_at": "2099-01-01T09:00",
            },
            "followups_pref": None,
            "reason": "Événement daté qui compte.",
        })


class ForbiddenProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise AssertionError("Ne doit pas être appelé quand le flag est off.")


DIALOGUE = "\n## 2026-07-05T10:00:00 — user\n\nDemain je présente ma démo, gros enjeu.\n\n## 2026-07-05T10:00:05 — assistant\n\nTu es prêt.\n"


def test_relanceur_plants_intent_with_dialogue_evidence(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, DIALOGUE)
    provider = PlantingProvider("Demain je présente ma démo, gros enjeu.")
    narrator_calls = []
    monkeypatch.setattr(
        "daemon_core.relanceur.run_narrator",
        lambda **kwargs: narrator_calls.append(kwargs["user_id"])
        or "Il prépare une démonstration importante.",
    )

    result = run_relanceur("garden", provider=provider)

    assert narrator_calls == ["garden"]
    assert result["followup"] is not None
    assert result["followup"]["angle"] == "encouragement"
    # The judge sees the full dialogue, Stance Register and expanded angles.
    assert "feedback" in provider.messages[0]["content"]
    assert "presence" in provider.messages[0]["content"]
    assert "heads-up" in provider.messages[0]["content"]
    assert "attentive friend" in provider.messages[0]["content"]
    assert "touch of presence" in provider.messages[0]["content"]
    assert "prefer the same day" in provider.messages[0]["content"]
    assert "gros enjeu" in provider.messages[1]["content"]
    assert "Il prépare une démonstration importante." in provider.messages[1]["content"]


def test_relanceur_does_not_receive_the_professional_calendar(
    tmp_path,
    monkeypatch,
):
    memory_root = _setup(tmp_path, monkeypatch, DIALOGUE)
    agenda = memory_root / "users" / "garden" / "agenda_pro.md"
    agenda.parent.mkdir(parents=True)
    agenda.write_text(
        """# Professional calendar

Last sync: 2026-06-28.
Coverage: 14 days.

## Monday
- SECRET-MEETING
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAEMON_CORE_AMBIENT_CONTEXT", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_AMBIENT_CONTEXT_USER_IDS",
        "garden",
    )
    provider = PlantingProvider(
        "Demain je présente ma démo, gros enjeu."
    )

    run_relanceur("garden", provider=provider)

    prompt = provider.messages[1]["content"]
    assert "Last sync: 2026-06-28." not in prompt
    assert "SECRET-MEETING" not in prompt


def test_relanceur_rejects_fabricated_evidence(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, DIALOGUE)
    provider = PlantingProvider("citation que personne n'a dite")

    result = run_relanceur("garden", provider=provider)

    assert result["followup"] is None


def test_relanceur_silent_when_flag_off(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, DIALOGUE)
    monkeypatch.delenv("DAEMON_CORE_FOLLOWUPS", raising=False)
    monkeypatch.setattr(
        "daemon_core.relanceur.run_narrator",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Narrator must not run when followups are disabled")
        ),
    )

    result = run_relanceur("garden", provider=ForbiddenProvider())

    assert result == {"followup": None, "followups_pref": None}


def test_relanceur_does_not_count_long_horizon_followups_as_active(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, DIALOGUE)
    from daemon_core.followup import MAX_ACTIVE_FOLLOWUPS, new_followup

    for idx in range(MAX_ACTIVE_FOLLOWUPS):
        new_followup(
            user_id="garden",
            fire_at=f"2099-01-0{idx + 1}T09:00",
            topic=f"future {idx}",
            evidence="Demain je présente ma démo, gros enjeu.",
        )
    provider = PlantingProvider("Demain je présente ma démo, gros enjeu.")

    result = run_relanceur("garden", provider=provider)

    assert result["followup"] is not None


def test_presence_followup_is_tightened_to_near_slot():
    from datetime import datetime
    from daemon_core.followup import TZ
    from daemon_core.relanceur import _tighten_presence_fire_at

    now = datetime(2026, 7, 8, 14, 10, tzinfo=TZ)

    out = _tighten_presence_fire_at("2026-07-08T18:40", "presence", now=now)

    assert out == "2026-07-08T15:10"


def test_non_presence_followup_keeps_model_time():
    from datetime import datetime
    from daemon_core.followup import TZ
    from daemon_core.relanceur import _tighten_presence_fire_at

    now = datetime(2026, 7, 8, 14, 10, tzinfo=TZ)

    assert _tighten_presence_fire_at("2026-07-08T18:40", "check-in", now=now) == "2026-07-08T18:40"


def test_moment_judge_uses_conversation_state_as_freshness_not_topic_menu():
    messages = build_moment_judge_messages(
        message="Propose-moi autre chose.",
        free_initiative_requested=True,
        recent_dialogue="user: On a déjà beaucoup parlé de création.\nassistant: Oui.",
        conversation_state="# Conversation state\n\nLa création est importante mais épuisée pour ce soir. Un peu de légèreté est demandé.",
        immediate_focus="Le fil précédent est clos.",
        memory_map="- theme/projet: création\n- theme/loisirs: cinéma, jeux",
        relevant_memory="Alex a apprécié Google Démocratie et lit Europe 2031.",
        daemon_persona="Une présence vive, attachée à la justesse.",
        conversation_manner=(
            "L'échange avant tout, par petites touches. "
            "Des sujets concrets, jamais magazine d'été."
        ),
    )
    system = messages[0]["content"]
    user = messages[1]["content"]

    assert "explicitly entrusts you with the initiative" in system
    # Initiative does not require a new topic:
    # yielding to the user or starting from their actual situation are valid.
    # Repeated refusals change the stance.
    assert "does not oblige you to hand out a topic" in system
    assert "change posture" in system
    assert "an important topic may be exhausted" in system
    assert "recurring menu" in system
    assert "not a generic question" in system
    assert "Restraint is also a move" in system
    assert "Do not manufacture liveliness with a mini-game" in system
    assert "création est importante mais épuisée" in user
    # The topic map remains present to anchor the judgment.
    # Without it, earlier runs proposed unrelated curiosities.
    # It is an anchor, never a menu; the system retains the anti-menu rule,
    # while conversation_state informs freshness.
    assert "theme/projet" in user
    assert "draw first from the person's worlds" in system
    # The former instruction to end with a question about the topic was removed:
    # it produced imposed topics followed by mechanical questions.
    # The invitation instead belongs to conversation.md,
    # supplied to the judge with the identity context.
    assert "a question about the person's relationship to this topic" not in system
    assert "Google Démocratie" in user
    # Identity consists of the persona and one conversational-style document,
    # without a second legacy manual.
    assert "Une présence vive, attachée à la justesse." in system
    assert "L'échange avant tout, par petites touches." in system
    assert "jamais magazine d'été" in system
    # In unsolicited initiative, radar memories are not a user-selected topic.
    assert "the person did NOT request this topic" in user
    assert "Memories targeted to the topic chosen" not in user


def test_moment_judge_souvenirs_are_directive_only_when_thread_closes_on_named_ground():
    messages = build_moment_judge_messages(
        message="Ok merci !",
        free_initiative_requested=False,
        recent_dialogue="user: Parlons de mes lectures.\nassistant: Volontiers.",
        relevant_memory="Alex lit Europe 2031.",
    )
    user = messages[1]["content"]
    assert "Memories targeted to the topic chosen in this scene" in user
    assert "the person did NOT request this topic" not in user
