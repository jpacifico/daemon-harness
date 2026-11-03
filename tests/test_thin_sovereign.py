from datetime import datetime
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.held_atlas_sovereign import ExactSouvenirRead, ExactMemoryAtlas
from daemon_core.sovereign_library import ActiveMemoryLibrary, ExactHotMemory
from daemon_core.thin_sovereign import (
    EMITTED_TURN_COMMITMENT_CONTRACT,
    EXTERNAL_KNOWLEDGE_CONTRACT,
    SILENT_TURN_COMMITMENT_CONTRACT,
    THIN_TURN_THRESHOLD,
    THIN_VOICE_CONTRACT,
    adapt_turn_commitment_for_unfiltered_streaming,
    build_compact_scene_context,
    build_thin_held_atlas_messages,
    build_thin_library_messages,
    build_thin_sovereign_messages,
)


def test_unfiltered_streaming_keeps_judgment_without_emitting_private_marker():
    assert THIN_VOICE_CONTRACT.endswith(EMITTED_TURN_COMMITMENT_CONTRACT)

    adapted = adapt_turn_commitment_for_unfiltered_streaming(
        THIN_VOICE_CONTRACT
    )
    normalized = " ".join(adapted.split())

    assert adapted.endswith(SILENT_TURN_COMMITMENT_CONTRACT)
    assert "[daemon-turn" not in adapted
    assert "entirely internal" in normalized
    assert "Choose exactly one mode" in normalized
    assert all(
        f"`{mode}`" in adapted
        for mode in ("exchange", "direct", "clarify", "condition")
    )
    assert "the single natural question" in normalized
    assert "open the response with the exact natural condition" in normalized


def test_streaming_transport_adaptation_is_exact_and_leaves_other_prompts_alone():
    prompt = "Système sans contrat privé."

    assert adapt_turn_commitment_for_unfiltered_streaming(prompt) == prompt
    assert "[daemon-turn: mode]" in THIN_VOICE_CONTRACT


@pytest.mark.parametrize(
    "message,personal_text",
    [
        ("Et notre atelier, tu t'en souviens ?", "L'atelier était une image, pas un lieu."),
        ("Do you remember our workshop?", "The workshop was an image, not a place."),
    ],
)
def test_english_contracts_do_not_rewrite_supplied_language_or_exact_sources(
    message, personal_text,
):
    payloads = {
        "daemon_persona": "# Persona personnelle\nTu es Écho, sans rôle à réciter.",
        "conversation_manner": "# Manière personnelle\nParle-moi simplement.",
        "user_context": "# Profil exact\n" + personal_text,
        "scene_model_text": "# Lecture révisable\n" + personal_text,
        "exact_memory_text": "# Capsule exacte\n" + personal_text,
        "memory_catalog": "- theme/atelier | subjects: Écho | facets: métaphore",
        "current_context": "# Contexte exact\n" + personal_text,
        "authoritative_context": "# Agenda exact\nRendez-vous conditionnel.",
        "episodic_scene_text": (
            "## Établi\n" + personal_text
            + "\n## Lectures ouvertes de Iris\nUne lecture, pas un fait."
        ),
        "prior_dialogue": "## Extrait antérieur\nCe que j'avais dit exactement.",
    }
    messages = build_thin_sovereign_messages(
        message=message,
        recent_dialogue="## 2026-09-06T10:00:00+02:00 — user\n\n" + message,
        host_tools_available=True,
        message_received_at=0,
        **payloads,
    )

    assert [item["role"] for item in messages] == ["system", "system", "user"]
    assert messages[-1] == {"role": "user", "content": message}
    assert sum(item["content"].count(message) for item in messages) == 1
    for text in payloads.values():
        assert messages[0]["content"].count(text) == 1
    adapted = adapt_turn_commitment_for_unfiltered_streaming(messages[0]["content"])
    for text in payloads.values():
        assert adapted.count(text) == 1
    assert "[daemon-turn" not in adapted


def test_thin_atlas_voice_keeps_exact_dialogue_and_bounded_context():
    messages = build_thin_held_atlas_messages(
        message="Et maintenant ?",
        atlas=ExactMemoryAtlas("- theme/famille", ("theme/famille",), 18, "hash"),
        hot_memory=ExactSouvenirRead(
            (("theme/famille", "Fait exact."),),
            (),
            11,
        ),
        scene_model_text="- [uncertain] La suite reste ouverte.",
        recent_dialogue=(
            "## 2026-07-21T06:44:23 — user\n\nUne idée.\n\n"
            "## 2026-07-21T06:44:29 — assistant\n\nMa réponse."
        ),
        daemon_persona="Persona.",
        conversation_manner="Manière.",
        user_context=(
            "# Fiche interlocuteur\n"
            "- Travaille généralement à distance en semaine."
        ),
        episodic_scene_text=(
            "# Un ancien atelier\n\n"
            "## Établi\n\n- Un objet commun avait été nommé.\n\n"
            "## Lectures ouvertes de Iris\n\n"
            "- Ce pouvait être un lieu où revenir."
        ),
        connected_source_status=(
            "### professional_calendar\n"
            "Last sync: 2026-06-28.\n"
            "Coverage: through 2026-07-12."
        ),
        host_tools_available=True,
        message_received_at=datetime(
            2026, 7, 25, 8, 27, 40, tzinfo=ZoneInfo("Europe/Paris")
        ),
        working_state_revised_at=datetime(
            2026, 7, 21, 6, 44, 35, tzinfo=ZoneInfo("Europe/Paris")
        ),
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
    ]
    system = messages[0]["content"]
    assert "Persona." in system
    assert "Manière." in system
    assert '# Stable personal reference points' in system
    assert "Travaille généralement à distance en semaine." in system
    assert "Fait exact." in system
    assert "theme/famille" in system
    assert "La suite reste ouverte" in system
    assert system.count('# Potentially relevant past scene') == 1
    assert "Un ancien atelier" in system
    episodic_contract = " ".join(system.split())
    assert "never describes the present state" in episodic_contract
    # Scenes written before 1 November 2026 still carry "Lectures ouvertes de Iris".
    assert "the `Lectures ouvertes` remain readings" in episodic_contract
    assert 'Host tools' in system
    assert "Préférences de format de discussion prioritaires" not in system
    assert "Any relative expression" in system
    normalized_system = " ".join(system.split())
    assert "ne prononce pas son sens à sa place" not in normalized_system
    assert "une lecture brève comme hypothèse révisable" not in normalized_system
    assert '# Temporal frame' in system
    assert '4 days, 1 hour, 43 minutes, 11 seconds' in system
    assert (
        'Working snapshot revised : 2026-07-21T06:44:35+02:00 (weekday=Tuesday, timezone=Europe/Paris)'
    ) in system
    assert messages[1]["content"].startswith(
        "[heure locale du tour : 2026-07-21T06:44:23]"
    )
    assert messages[2]["content"].startswith(
        "[heure locale du tour : 2026-07-21T06:44:29]"
    )
    assert messages[-2]["content"].startswith('# Turn threshold')
    assert THIN_TURN_THRESHOLD in messages[-2]["content"]
    assert "Last sync: 2026-06-28." in messages[-2]["content"]
    assert "Last sync: 2026-06-28." not in system


def test_thin_chat_deduplicates_current_turn_and_keeps_temporal_provenance():
    messages = build_thin_held_atlas_messages(
        message="Salut toi",
        atlas=ExactMemoryAtlas("", (), 0, "atlas"),
        hot_memory=ExactSouvenirRead((), (), 0),
        scene_model_text="",
        recent_dialogue=(
            "## 2026-07-21T06:44:29 — assistant\n\nBon voyage.\n\n"
            "## 2026-07-25T08:27:40 — user\n\nSalut toi"
        ),
        daemon_persona="Persona.",
        conversation_manner="Manière.",
        host_tools_available=True,
        message_received_at=datetime(
            2026, 7, 25, 8, 27, 40, tzinfo=ZoneInfo("Europe/Paris")
        ),
    )
    assert [m["content"] for m in messages if m["role"] == "user"] == ["Salut toi"]
    assert "[heure locale du tour : 2026-07-21T06:44:29]\nBon voyage." in [
        m["content"] for m in messages
    ]
    assert "4 days, 1 hour, 43 minutes, 11 seconds" in messages[0]["content"]


def test_compact_scene_keeps_only_supplied_working_sources():
    scene = build_compact_scene_context(
        user_agenda="Vendredi : dessin.",
        active_context="Séjour en cours.",
        conversation_state="Le fil reste ouvert.",
    )

    assert "Séjour en cours." in scene
    assert "Vendredi : dessin." in scene
    assert "Le fil reste ouvert." in scene


def test_external_verification_is_scoped_to_claims_required_now():
    normalized = " ".join(EXTERNAL_KNOWLEDGE_CONTRACT.split())

    assert "exact result" in normalized
    assert "never proves that you already knew" in normalized
    assert "A request for an opinion does not suspend this boundary" in normalized
    assert "first person as proof of a perception" in normalized


def test_voice_carries_defensible_referents_without_forcing_repetition():
    normalized = " ".join(THIN_VOICE_CONTRACT.split())

    assert "a pronoun, an ellipsis or an incomplete name" in normalized
    assert "nearest coherent referent" in normalized
    assert "Do not ask the interlocutor to rename" in normalized
    assert "several genuinely possible referents" in normalized


def test_material_uncertainty_precedes_dependent_choice_without_overquestioning():
    normalized = " ".join(THIN_TURN_THRESHOLD.split())
    leading = " ".join(THIN_VOICE_CONTRACT.split())

    assert "resolving it constitutes the entire next move" in normalized
    assert "the visible capabilities can establish it" in normalized
    assert "proceed explicitly conditionally" in normalized
    assert "the interlocutor must supply the information" in normalized
    assert "wait for their answer without composing the choices that depend on it" in normalized
    assert "without resolvable external uncertainty" in leading
    assert "a factual answer, research" in leading
    assert "only the interlocutor can provide" in leading
    assert "must be supplied by the interlocutor" in leading
    assert "a caveat after the choice" in normalized
    assert "without asking for confirmation as a precaution" in normalized
    assert "resolving it constitutes the entire next move" not in leading


def test_thin_library_voice_exposes_exact_read_protocol(tmp_path):
    library_root = tmp_path / "capsules"
    library_root.mkdir()
    library = ActiveMemoryLibrary(
        workspace_root=tmp_path,
        root=library_root,
        souvenir_names=("theme/famille",),
        source_chars=10,
        sha256="hash",
        routing_metadata=(("theme/famille", ("Sam",), ("famille",)),),
    )

    messages = build_thin_library_messages(
        message="Qui sera là ?",
        library=library,
        user_agenda="Demain : visite.",
        active_context="Chez les parents.",
        conversation_state="Question familiale.",
        recent_dialogue="",
        daemon_persona="Persona.",
        conversation_manner="Manière.",
        user_context="- Travaille généralement à distance en semaine.",
        hot_memory=ExactHotMemory((("theme/famille", "Corps exact."),)),
    )

    system = messages[0]["content"]
    threshold = messages[-2]["content"]
    compact_threshold = " ".join(threshold.split())
    assert "subjects: Sam" in system
    assert "Travaille généralement à distance en semaine." in system
    assert "Corps exact." in system
    assert "--handling-mode clarify|condition|direct|offer" in system
    assert "without composing the choices" in threshold
    assert "responsibilities, commitments, continuities and exclusions" in compact_threshold
    assert "an unknown about a property neither" in compact_threshold
    assert "downgrades nor erases the fact" in compact_threshold
    assert 'before any proposal' in system
    assert 'An unknown detail neither downgrades nor erases' in system
    assert 'The only authorized executions' in system
    assert messages[-1] == {"role": "user", "content": "Qui sera là ?"}


@pytest.mark.parametrize("host_tools_available", [False, True])
def test_context_use_candidate_preserves_all_other_content(host_tools_available):
    from daemon_core.thin_sovereign import PERSONAL_CONTEXT_USE_CLARIFICATION

    arguments = dict(
        message="Et ce projet ?",
        daemon_persona="Persona intacte.",
        conversation_manner="Manière intacte.",
        recent_dialogue="",
        exact_memory_text="Une source exacte dont les mots ne changent pas.",
        memory_catalog="- theme/projet | subjects: Projet",
        host_tools_available=host_tools_available,
        message_received_at=0,
    )
    reference = build_thin_sovereign_messages(**arguments)
    disabled = build_thin_sovereign_messages(
        **arguments, clarify_personal_context_use=False
    )
    candidate = build_thin_sovereign_messages(
        **arguments, clarify_personal_context_use=True
    )
    assert disabled == reference
    assert sum(row["content"].count(PERSONAL_CONTEXT_USE_CLARIFICATION)
               for row in candidate) == 1
    restored = [
        {**row, "content": row["content"].replace(
            "\n\n" + PERSONAL_CONTEXT_USE_CLARIFICATION, "", 1
        )}
        for row in candidate
    ]
    assert restored == reference
    streamed = adapt_turn_commitment_for_unfiltered_streaming(candidate[0]["content"])
    assert PERSONAL_CONTEXT_USE_CLARIFICATION in streamed
    assert "[daemon-turn" not in streamed
