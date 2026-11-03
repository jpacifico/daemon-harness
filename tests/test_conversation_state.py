import sys
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.conversation_state import (
    build_conversation_state_messages,
    clean_conversation_state,
    project_scene_conversation_state,
    refresh_conversation_state,
)
from daemon_core.memory.reader import load_conversation_state
from daemon_core.providers.base import LLMProvider


class StateProvider(LLMProvider):
    def __init__(self) -> None:
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return "# Conversation state\n\nLe cinéma vient d'être quitté. La personne demande quelque chose de plus léger et neuf."


def _patch_memory_root(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    return memory_root


def test_conversation_state_is_written_as_temporary_reading(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    l0 = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    (l0 / f"garden_{today}.md").write_text(
        "\n## 2026-07-09T21:00:00 — user\n\nOn sort du cinéma.\n"
        "\n## 2026-07-09T21:00:01 — assistant\n\nD'accord.\n",
        encoding="utf-8",
    )
    provider = StateProvider()

    state = refresh_conversation_state("garden", provider)

    assert state == load_conversation_state("garden").strip()
    assert "plus léger et neuf" in state
    assert "importance from present conversational freshness" in provider.messages[0]["content"]


def test_conversation_state_prompt_separates_importance_from_freshness():
    messages = build_conversation_state_messages(
        recent_dialogue="user: Encore le même sujet ?",
        previous="# Conversation state\n\nLe sujet est important.",
    )

    assert "Distinguish importance from present conversational freshness" in messages[0]["content"]
    assert "not a biography" in messages[0]["content"]
    assert "an opening with no subsequent user response is untested" in messages[0]["content"]
    assert "an explicit user rejection outweighs" in messages[0]["content"].lower()


def test_conversation_state_prompt_does_not_close_a_live_subject_after_one_answer():
    messages = build_conversation_state_messages(
        recent_dialogue=(
            "assistant: Vous l'avez vu ensemble ce soir ?\n"
            "user: Oui. On l'a déjà vu plusieurs fois, c'est un de nos classiques.\n"
            "user: Il y a pas mal d'action dans celui-ci."
        ),
        previous="# Conversation state\n\nLe film vient d'être évoqué.",
    )

    system = messages[0]["content"]
    assert "one question can have been answered while the broader subject remains alive" in system
    assert "Never call a subject closed merely because one answer was received" in system
    assert "A fresh detail volunteered by the person" in system
    assert "repeated acknowledgements bring no new material" in system


def test_conversation_state_cleaner_adds_heading_and_bounds_output():
    cleaned = clean_conversation_state("Un fil vivant.\n" + ("x" * 3000))

    assert cleaned.startswith("# Conversation state")
    assert len(cleaned) <= 1400


def test_scene_texture_projects_without_another_provider_call(
    tmp_path,
    monkeypatch,
):
    _patch_memory_root(tmp_path, monkeypatch)

    state = project_scene_conversation_state(
        "garden",
        reading="Le jardin est le sujet concret encore vivant.",
        conversation="La dernière ouverture a été refusée.",
    )

    assert state == load_conversation_state("garden").strip()
    assert state.startswith("# Conversation state")
    assert "jardin est le sujet concret" in state
    assert "dernière ouverture a été refusée" in state


def test_empty_scene_texture_clears_the_compatibility_projection(
    tmp_path,
    monkeypatch,
):
    _patch_memory_root(tmp_path, monkeypatch)
    project_scene_conversation_state(
        "garden",
        reading="Ancien fil vivant.",
        conversation="",
    )

    state = project_scene_conversation_state(
        "garden",
        reading="",
        conversation="",
    )

    assert state == ""
    assert load_conversation_state("garden") == "\n"


def test_scene_backed_batch_skips_standalone_state_judge(monkeypatch):
    from daemon_core.loop import DaemonCore
    import daemon_core.loop as loop_module

    refresh = Mock(return_value="unused")
    monkeypatch.setattr(loop_module, "refresh_conversation_state", refresh)
    monkeypatch.setattr(
        loop_module,
        "load_recent_dialogue_through",
        lambda *_args, **_kwargs: "Dialogue exact.",
    )
    monkeypatch.setattr(
        loop_module,
        "run_relanceur",
        lambda **_kwargs: {"followup": None, "followups_pref": None},
    )
    harness = DaemonCore.__new__(DaemonCore)
    harness.memory_provider = object()

    harness.refresh_after_turn_projections(
        user_id="garden",
        current_assistant_l0={},
        run_followup=False,
        scene_projection_expected=True,
    )

    refresh.assert_not_called()


def test_batch_without_scene_keeps_standalone_state_judge(monkeypatch):
    from daemon_core.loop import DaemonCore
    import daemon_core.loop as loop_module

    refresh = Mock(return_value="fallback")
    monkeypatch.setattr(loop_module, "refresh_conversation_state", refresh)
    monkeypatch.setattr(
        loop_module,
        "load_recent_dialogue_through",
        lambda *_args, **_kwargs: "Dialogue exact.",
    )
    monkeypatch.setattr(
        loop_module,
        "run_relanceur",
        lambda **_kwargs: {"followup": None, "followups_pref": None},
    )
    harness = DaemonCore.__new__(DaemonCore)
    harness.memory_provider = object()

    harness.refresh_after_turn_projections(
        user_id="garden",
        current_assistant_l0={},
        run_followup=False,
        scene_projection_expected=False,
    )

    refresh.assert_called_once_with(
        user_id="garden",
        provider=harness.memory_provider,
        recent_dialogue="Dialogue exact.",
    )


def test_recent_dialogue_crosses_midnight_without_merging_raw_l0(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    l0 = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True)
    (l0 / "garden_2026-07-09.md").write_text(
        "\n## 2026-07-09T23:59:00 — user\n\nLe fil de la veille.\n",
        encoding="utf-8",
    )
    (l0 / "garden_2026-07-10.md").write_text(
        "\n## 2026-07-10T00:01:00 — assistant\n\nLa suite après minuit.\n",
        encoding="utf-8",
    )
    from daemon_core.memory.reader import load_recent_dialogue

    dialogue = load_recent_dialogue("garden", max_turns=2)

    assert "fil de la veille" in dialogue
    assert "suite après minuit" in dialogue
    assert len(list(l0.glob("garden_*.md"))) == 2


def test_recovered_history_is_read_chronologically_without_rewriting_l0(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    from daemon_core.memory.writer import append_ground_zero, reconcile_ground_zero_messages
    from daemon_core.memory.reader import load_recent_dialogue

    later = 1788775500.0
    append_ground_zero("garden", "assistant", "Later answer.", occurred_at=later, idempotency_key="later")
    path = next((memory_root / "levels/L0_ground_zero/conversations").glob("garden_*.md"))
    before = path.read_bytes()
    sources = [
        {"event_id": "1", "role": "user", "content": "Earlier question.", "occurred_at": later - 20},
        {"event_id": "2", "role": "assistant", "content": "Earlier answer.", "occurred_at": later - 10},
    ]
    assert reconcile_ground_zero_messages("garden", sources, source="hermes", session_id="test").restored == 2
    repaired = path.read_bytes()
    assert repaired.startswith(before)
    assert reconcile_ground_zero_messages("garden", sources, source="hermes", session_id="test").restored == 0
    assert path.read_bytes() == repaired
    dialogue = load_recent_dialogue("garden", max_turns=3)
    assert dialogue.index("Earlier question.") < dialogue.index("Earlier answer.") < dialogue.index("Later answer.")
    assert "Earlier question." not in load_recent_dialogue("garden", max_turns=2)


def test_dialogue_projection_preserves_equal_dates_and_malformed_legacy_order():
    from daemon_core.memory.reader import _dialogue_chunks

    first = "## 2026-09-26T08:00:00+02:00 — user\n\nQuestion."
    second = "## 2026-09-26T08:00:00+02:00 — assistant\n\nAnswer."
    assert _dialogue_chunks(first + "\n\n" + second) == [first, second]
    legacy = "## unknown — user\n\nLegacy entry."
    assert _dialogue_chunks(second + "\n\n" + legacy + "\n\n" + first) == [second, legacy, first]


def test_dialogue_projection_compares_instants_not_timezone_strings():
    from daemon_core.memory.reader import _dialogue_chunks

    later = "## 2026-09-26T07:00:00+00:00 — assistant\n\nLater."
    earlier = "## 2026-09-26T08:00:00+02:00 — user\n\nEarlier."
    assert _dialogue_chunks(later + "\n\n" + earlier) == [earlier, later]


def test_prior_dialogue_keeps_bounded_exact_tails_from_two_previous_days(
    tmp_path,
    monkeypatch,
):
    from datetime import datetime, timedelta

    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    l0 = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True)
    today = datetime.now().date()
    oldest = today - timedelta(days=2)
    previous = today - timedelta(days=1)
    (l0 / f"garden_{oldest.isoformat()}.md").write_text(
        "## 2026-07-08T20:00:00 — user\n\n" + ("ancien " * 30) + "\n\n"
        "## 2026-07-08T23:00:00 — assistant\n\nFin exacte J-2.\n",
        encoding="utf-8",
    )
    (l0 / f"garden_{previous.isoformat()}.md").write_text(
        "## 2026-07-09T18:00:00 — user\n\n" + ("écarté " * 30) + "\n\n"
        "## 2026-07-09T22:00:00 — assistant\n\nFin exacte J-1.\n",
        encoding="utf-8",
    )
    (l0 / f"garden_{today.isoformat()}.md").write_text(
        "## 2026-07-10T08:00:00 — user\n\nAujourd'hui.\n",
        encoding="utf-8",
    )
    from daemon_core.memory.reader import load_prior_dialogue

    dialogue = load_prior_dialogue("garden", max_chars=240, max_files=2)

    assert "Fin exacte J-2." in dialogue
    assert "Fin exacte J-1." in dialogue
    assert "ancien" not in dialogue
    assert "écarté" not in dialogue
    assert "Aujourd'hui" not in dialogue
    assert len(dialogue) <= 240


def test_trailing_current_user_can_be_removed_from_prompt_projection():
    from daemon_core.memory.reader import without_trailing_user_message

    dialogue = (
        "## 2026-07-10T01:00:00 — assistant\n\nUne ouverture.\n\n"
        "## 2026-07-10T01:00:05 — user\n\nAutre sujet please"
    )

    projected = without_trailing_user_message(dialogue, "Autre sujet please")

    assert "Une ouverture" in projected
    assert "Autre sujet please" not in projected


def test_state_prompt_does_not_treat_clarification_as_engagement():
    from daemon_core.conversation_state import build_conversation_state_messages

    system = build_conversation_state_messages(
        "user: Dans quel contexte ?", previous=""
    )[0]["content"]

    assert "asking what a question means" in system
    assert "only a substantive contribution confirms" in system
