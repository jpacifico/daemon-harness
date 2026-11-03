from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.canonical_voice import CanonicalVoiceContext
from daemon_core.held_atlas_sovereign import (
    ExactSouvenirRead,
    ExactMemoryAtlas,
)
from daemon_core.scene_model import (
    SceneDependency,
    SceneItem,
    SceneSnapshot,
    SceneSource,
    SceneTemporalBoundary,
    render_scene_model_for_voice,
)
from daemon_core.thin_sovereign import build_thin_held_atlas_messages
from daemon_core.turn_runtime import TurnPlan, TurnRequest
from daemon_core.voice_brief import (
    ATOMICITY_UNVERIFIED,
    DialogueCoverageError,
    build_dialogue_coverage,
    compile_voice_brief,
    render_reference_messages,
)
from daemon_core.voice_brief_renderer import (
    authority_hierarchy_canary_enabled,
    render_authority_hierarchy_voice_brief,
    render_narrative_voice_brief,
    render_temporal_voice_brief,
    temporal_voice_brief_canary_enabled,
)


NOW = 1_785_665_400.0


def _scene(*, unresolved: bool = False) -> SceneSnapshot:
    source = SceneSource(
        source_id="src-1",
        kind="l0_turn",
        locator="2026-08-02T08:00:00+02:00 — user",
        sha256="source-sha",
        excerpt="Le départ est prévu demain.",
    )
    source_ids = ("missing",) if unresolved else (source.source_id,)
    return SceneSnapshot(
        scene_scope="telegram__u",
        source_trace_id="trace-scene",
        source_turn_started_at=NOW - 300,
        revised_at=NOW - 120,
        revision=7,
        provider={"model": "terra"},
        dialogue_watermark_source_id=source.source_id,
        dialogue_watermark_locator=source.locator,
        reading="Un départ proche organise la matinée.",
        conversation="La décision reste ouverte.",
        items=(
            SceneItem(
                item_id="item-1",
                text="Le départ est prévu demain.",
                epistemic="established",
                sources=source_ids,
                temporal_boundaries=(
                    SceneTemporalBoundary(
                        at="2026-08-03T08:00:00+02:00",
                        after="Le départ appartient au passé.",
                    ),
                ),
            ),
        ),
        dependencies=(
            SceneDependency(
                dependency_id="dep-1",
                text="Le nombre de participants reste à confirmer.",
                status="uncertain",
                horizon="demain",
                sources=(source.source_id,),
            ),
        ),
        sources=(source,),
    )


def _plan(
    *,
    scene: SceneSnapshot | None = None,
    delegation: bool = True,
    personal_agenda: str = "",
    professional_agenda: str = "",
    message_received_at: float = NOW,
) -> TurnPlan:
    scene = scene if scene is not None else _scene()
    atlas = ExactMemoryAtlas(
        text="- theme/depart: départ et participants",
        souvenir_names=("theme/depart",),
        rendered_chars=38,
        sha256="atlas-sha",
        common_ground_names=("common_ground/pacte",),
    )
    hot_memory = ExactSouvenirRead(
        souvenirs=(("theme/depart", "# Départ\n\n- Le trajet reste ouvert."),),
        rejected_paths=(),
        rendered_chars=38,
    )
    recent_dialogue = """## 2026-08-02T08:00:00+02:00 — user

On part demain.

## 2026-08-02T08:01:00+02:00 — assistant

Il reste à préciser qui vient.
"""
    authoritative_context = '## Current agenda\n- Demain : départ.'
    if personal_agenda or professional_agenda:
        combined_agenda = personal_agenda.strip()
        if professional_agenda.strip():
            combined_agenda = (
                combined_agenda
                + "\n\n# Agenda professionnel (lecture seule)\n"
                + professional_agenda.strip()
            ).strip()
        authoritative_context = '## Current agenda\n' + combined_agenda
    messages = build_thin_held_atlas_messages(
        message="Qu'est-ce qu'on prépare ?",
        atlas=atlas,
        hot_memory=hot_memory,
        scene_model_text=render_scene_model_for_voice(
            scene,
            now=datetime.fromtimestamp(message_received_at).astimezone(),
        ),
        recent_dialogue=recent_dialogue,
        daemon_persona="Une présence précise.",
        conversation_manner="Laisser de la place à l'autre.",
        user_context="La personne travaille à domicile.",
        current_context=(
            "# Contexte actif\n\n"
            "## Sujets actifs\n"
            "- La préparation Acme reste prioritaire aujourd'hui."
        ),
        authoritative_context=authoritative_context,
        connected_source_status="Calendrier observé à 08:00.",
        agent_name="Lyra",
        host_tools_available=delegation,
        message_received_at=message_received_at,
        working_state_revised_at=scene.revised_at,
    )
    messages[0]["content"] += (
        "\n\n# Common Ground reading threshold\n\nLire une entrée exacte seulement lorsqu'elle compte."
    )
    scene_delta = """## 2026-08-02T08:01:00+02:00 — assistant

Il reste à préciser qui vient.
"""
    dialogue_coverage = build_dialogue_coverage(
        scan_dialogue=recent_dialogue,
        recent_dialogue=recent_dialogue,
        scene_delta=scene_delta,
        scene_active=True,
        scene_watermark_source_id=scene.dialogue_watermark_source_id,
    )
    request = TurnRequest(
        user_id="u",
        message="Qu'est-ce qu'on prépare ?",
        platform="telegram",
        message_received_at=message_received_at,
    )
    context = CanonicalVoiceContext(
        atlas=atlas,
        hot_memory=hot_memory,
        scene_scope=scene.scene_scope,
        personal_agenda=personal_agenda,
        professional_agenda=professional_agenda,
        active_context=(
            "# Contexte actif\n\n"
            "## Sujets actifs\n"
            "- La préparation Acme reste prioritaire aujourd'hui."
        ),
        scene_model=scene,
        scene_delta=scene_delta,
        dialogue_coverage=dialogue_coverage,
        situation_source="scene",
    )
    return TurnPlan(
        request=request,
        messages=tuple(messages),
        voice_context=context,
        voice_path="canonical",
        situation_source="scene",
        memory_source="exact_library",
        composer_name="test.composer",
    )


def test_reference_renderer_reproduces_the_canonical_plan_exactly():
    plan = _plan()

    brief = compile_voice_brief(plan)

    assert list(render_reference_messages(brief)) == list(plan.messages)
    assert [block.name for block in brief.leading_blocks] == [
        "identity",
        "conversation_manner",
        "stable_user_context",
        "turn_contract",
        "temporal_frame",
        "threshold_situation",
        "current_context",
        "authoritative_sources",
        "open_exact_memories",
        "memory_catalog",
        "host_tools_contract",
        "common_ground_contract",
    ]
    assert [block.name for block in brief.near_turn_blocks] == [
        "turn_threshold",
        "external_knowledge_boundary",
        "connected_source_status",
    ]
    assert brief.leading_prefix == ""
    assert brief.near_turn_prefix == ""


def test_authority_hierarchy_moves_only_durable_background():
    brief = compile_voice_brief(_plan())

    rendered = render_authority_hierarchy_voice_brief(brief)

    assert [message["role"] for message in rendered.messages] == [
        "system",
        "developer",
        "user",
        "assistant",
        "system",
        "user",
    ]
    primary = rendered.messages[0]["content"]
    background = rendered.messages[1]["content"]
    assert '# Identity' in primary
    assert '# Turn contract' in primary
    assert '# Situation at the turn threshold' in primary
    assert brief.block("host_tools_contract").raw_span.strip() in primary
    assert '# Stable personal reference points' not in primary
    assert '# Current authoritative durable sources' not in primary
    assert '# Durable memory catalog' in primary
    assert '# Stable personal reference points' in background
    assert '# Current authoritative durable sources' in background
    assert '# Durable memory catalog' not in background
    assert '# Situation at the turn threshold' not in background
    assert brief.block("host_tools_contract").raw_span.strip() not in background
    assert len(rendered.coverage) == (
        len(brief.leading_blocks) + len(brief.near_turn_blocks)
    )
    assert len({record.block_id for record in rendered.coverage}) == len(
        rendered.coverage
    )
    assert {
        record.block_id
        for record in rendered.coverage
        if record.disposition == "moved_to_developer_background"
    } == {
        "leading_system:stable_user_context",
        "leading_system:authoritative_sources",
    }


def test_authority_hierarchy_projects_only_operational_agenda(caplog):
    caplog.set_level("INFO", logger="daemon_core.voice_brief_renderer")
    brief = compile_voice_brief(
        _plan(
            personal_agenda=(
                "# Agenda\n\n"
                "- 2026-08-03 | Alex | présenter son backlog\n"
                "- En cours | Alex et Sam | café favori | souvenir durable"
            ),
        )
    )

    rendered = render_authority_hierarchy_voice_brief(brief)
    joined = "\n".join(message["content"] for message in rendered.messages)
    background = rendered.messages[1]["content"]

    assert '## Operational agenda' in background
    assert "présenter son backlog" in background
    assert "café favori" not in joined
    assert "count=1" in caplog.text
    assert "café favori" not in caplog.text
    coverage = {
        item.block_id: item.disposition for item in rendered.coverage
    }
    assert coverage["leading_system:authoritative_sources"] == (
        "transformed_to_developer_background"
    )


def test_authority_hierarchy_canary_requires_flag_user_and_platform(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY", "on")
    monkeypatch.setenv(
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS",
        "garden",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS",
        "telegram,openclaw",
    )

    assert authority_hierarchy_canary_enabled("garden", "telegram") is True
    assert authority_hierarchy_canary_enabled("garden", "openclaw") is True
    assert authority_hierarchy_canary_enabled("other", "telegram") is False


def test_authority_hierarchy_accepts_absent_optional_background():
    brief = compile_voice_brief(_plan())
    brief = replace(
        brief,
        leading_blocks=tuple(
            block
            for block in brief.leading_blocks
            if block.name
            not in {"stable_user_context", "authoritative_sources"}
        ),
    )

    rendered = render_authority_hierarchy_voice_brief(brief)

    assert [message["role"] for message in rendered.messages] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
    ]
    assert '# Durable memory catalog' in rendered.messages[0]["content"]
    assert not {
        record.block_id
        for record in rendered.coverage
        if record.disposition == "moved_to_developer_background"
    }


def test_authority_hierarchy_keeps_operational_agenda_without_scene_or_source_block():
    brief = compile_voice_brief(
        _plan(
            personal_agenda=(
                "# Agenda\n\n"
                "- 2026-08-03 | Alex | présenter son backlog"
            ),
        )
    )
    brief = replace(
        brief,
        scene=replace(brief.scene, available=False),
        leading_blocks=tuple(
            block
            for block in brief.leading_blocks
            if block.name
            not in {"stable_user_context", "authoritative_sources"}
        ),
    )

    rendered = render_authority_hierarchy_voice_brief(brief)

    assert [message["role"] for message in rendered.messages[:2]] == [
        "system",
        "developer",
    ]
    joined = "\n".join(message["content"] for message in rendered.messages)
    assert joined.count("présenter son backlog") == 1
    assert '## Operational agenda' in rendered.messages[1]["content"]


def test_compiler_exposes_identity_relation_evidence_and_resources():
    brief = compile_voice_brief(_plan())

    assert brief.identity.agent_name == "Lyra"
    assert brief.identity.persona == "Une présence précise."
    assert brief.relationship.manner == "Laisser de la place à l'autre."
    assert brief.relationship.stable_user_context == (
        "La personne travaille à domicile."
    )
    assert brief.relationship.shared_language_resources == ("common_ground/pacte",)
    assert brief.temporal_frame.now == "2026-08-02T12:10:00+02:00"
    assert brief.temporal_frame.weekday == "Sunday"
    assert brief.temporal_frame.timezone == "Europe/Paris"
    assert brief.evidence.opened_souvenirs[0].name == "theme/depart"
    assert brief.evidence.current_context_sources[0].kind == (
        "active_context_projection"
    )
    assert "préparation Acme" in brief.evidence.current_context_sources[0].text
    assert brief.evidence.authoritative_sources[0].kind == "connected_source"
    assert brief.resources.atlas.sha256 == "atlas-sha"
    assert brief.resources.atlas.souvenir_names == ("theme/depart",)
    assert "Calendrier observé" in brief.resources.connected_source_status
    assert "capabilities" in brief.resources.host_capability_contract


def test_turn_evidence_compiles_one_sourced_dossier_without_prompt_changes():
    plan = _plan()

    brief = compile_voice_brief(plan)
    dossier = brief.turn_evidence

    assert dossier.schema_version == "daemon.turn-evidence.v1"
    assert dossier.source_prompt_sha256 == brief.source_prompt_sha256
    assert [claim.claim_id for claim in dossier.present_claims] == ["item-1"]
    assert [dependency.dependency_id for dependency in dossier.open_dependencies] == [
        "dep-1"
    ]
    assert dossier.material_unknowns == ()
    assert [
        (turn.timestamp, turn.role, turn.content)
        for turn in dossier.unabsorbed_dialogue
    ] == [
        (
            "2026-08-02T08:01:00+02:00",
            "assistant",
            "Il reste à préciser qui vient.",
        )
    ]
    assert [document.name for document in dossier.opened_evidence] == [
        "active_context",
        "authoritative_context",
        "theme/depart",
    ]
    assert {source.source_id for source in dossier.available_sources} == {
        "memory_catalog",
        "connected_source_status",
    }
    assert [capability.capability_id for capability in dossier.capability_handles] == [
        "host_tools"
    ]
    assert dossier.material_failures == ()
    assert dossier.coverage_complete is True
    assert dossier.renderer_ready is True
    assert list(render_reference_messages(brief)) == list(plan.messages)


def test_turn_evidence_coverage_accounts_for_every_source_unit_once():
    brief = compile_voice_brief(_plan())
    dossier = brief.turn_evidence

    expected = {
        block.block_id for block in (*brief.leading_blocks, *brief.near_turn_blocks)
    }
    expected.update(
        f"dialogue:{index}" for index, _message in enumerate(brief.recent_dialogue)
    )
    expected.add("current_message")
    expected.update(
        f"dialogue_coverage:{turn.turn_id}" for turn in dossier.unabsorbed_dialogue
    )
    coverage_ids = [record.unit_id for record in dossier.coverage]

    assert set(coverage_ids) == expected
    assert len(coverage_ids) == len(set(coverage_ids))
    assert dossier.source_unit_count == len(expected)
    assert {record.disposition for record in dossier.coverage} == {
        "projected",
        "retained",
    }


def test_turn_evidence_separates_uncertainty_and_unresolved_provenance():
    original = _scene(unresolved=True)
    uncertain = replace(
        original,
        items=(replace(original.items[0], epistemic="uncertain"),),
    )

    dossier = compile_voice_brief(_plan(scene=uncertain)).turn_evidence

    assert dossier.present_claims == ()
    assert [(item.unknown_id, item.status) for item in dossier.material_unknowns] == [
        ("item-1", "uncertain")
    ]
    assert [failure.failure_id for failure in dossier.material_failures] == [
        "unresolved_scene_source:missing"
    ]
    assert dossier.coverage_complete is True
    assert dossier.renderer_ready is False


def test_turn_evidence_projects_crossed_boundaries_before_a_renderer_can_use_them():
    brief = compile_voice_brief(
        _plan(message_received_at=NOW + (2 * 24 * 60 * 60))
    )
    dossier = brief.turn_evidence

    assert dossier.present_claims == ()
    assert [item.status for item in dossier.material_unknowns] == ["uncertain"]
    assert dossier.material_unknowns[0].text == "Le départ appartient au passé."
    assert brief.scene.reading == "Un départ proche organise la matinée."
    assert brief.scene.conversation == "La décision reste ouverte."
    assert dossier.interpretive_reading == ""
    assert dossier.conversation_texture == ""
    assert dossier.renderer_ready is True


def test_turn_evidence_refuses_an_unparseable_temporal_boundary():
    plan = _plan()
    scene = plan.voice_context.scene_model
    malformed_item = replace(
        scene.items[0],
        temporal_boundaries=(
            SceneTemporalBoundary(
                at="not-an-instant",
                after="Le départ appartient au passé.",
            ),
        ),
    )
    malformed_scene = replace(scene, items=(malformed_item,))
    malformed_context = replace(plan.voice_context, scene_model=malformed_scene)

    dossier = compile_voice_brief(
        replace(plan, voice_context=malformed_context)
    ).turn_evidence

    assert [failure.failure_id for failure in dossier.material_failures] == [
        "claim_boundary_unparsed:item-1"
    ]
    assert dossier.renderer_ready is False


def test_turn_evidence_is_host_neutral_for_the_same_plan():
    telegram = _plan()
    openclaw = replace(
        telegram,
        request=replace(telegram.request, platform="openclaw"),
    )

    assert (
        compile_voice_brief(telegram).turn_evidence
        == compile_voice_brief(openclaw).turn_evidence
    )


def test_compiler_preserves_scene_provenance_without_claiming_atomicity():
    brief = compile_voice_brief(_plan())

    assert brief.scene.available is True
    assert brief.scene.revision == 7
    assert brief.scene.claims[0].epistemic == "established"
    assert brief.scene.claims[0].atomicity == ATOMICITY_UNVERIFIED
    assert brief.scene.claims[0].source_refs[0].source_id == "src-1"
    assert brief.scene.claims[0].temporal_boundaries[0].after == (
        "Le départ appartient au passé."
    )
    assert brief.scene.dependencies[0].status == "uncertain"
    assert brief.scene.dependencies[0].source_refs[0].sha256 == "source-sha"
    assert brief.scene.unresolved_source_ids == ()


def test_compiler_makes_unresolved_scene_sources_visible():
    brief = compile_voice_brief(_plan(scene=_scene(unresolved=True)))

    assert brief.scene.unresolved_source_ids == ("missing",)
    assert brief.scene.claims[0].source_refs[0].kind == "unresolved"
    assert brief.scene.claims[0].source_refs[0].sha256 == ""


def test_reference_renderer_rejects_a_changed_source_span():
    brief = compile_voice_brief(_plan())
    first = replace(brief.leading_blocks[0], raw_span='# Identity\n\nAltérée')
    changed = replace(
        brief,
        leading_blocks=(first, *brief.leading_blocks[1:]),
    )

    with pytest.raises(ValueError, match="changed the source prompt"):
        render_reference_messages(changed)


def test_compiler_accepts_the_non_delegating_handoff_contract():
    plan = _plan(delegation=False)

    brief = compile_voice_brief(plan)

    assert brief.block("yield_contract") is not None
    assert brief.block("host_tools_contract") is None
    assert list(render_reference_messages(brief)) == list(plan.messages)


def test_compiler_rejects_a_plan_without_near_turn_system():
    plan = _plan()
    malformed = replace(
        plan,
        messages=tuple(
            message
            for index, message in enumerate(plan.messages)
            if index != len(plan.messages) - 2
        ),
    )

    with pytest.raises(ValueError, match="near-turn system"):
        compile_voice_brief(malformed)


def test_narrative_renderer_covers_every_source_block_once():
    brief = compile_voice_brief(_plan())

    rendered = render_narrative_voice_brief(brief)

    source_block_ids = {
        block.block_id for block in (*brief.leading_blocks, *brief.near_turn_blocks)
    }
    coverage_ids = [record.block_id for record in rendered.coverage]
    assert rendered.complete is True
    assert rendered.uncovered_block_ids == ()
    assert set(coverage_ids) == source_block_ids
    assert len(coverage_ids) == len(set(coverage_ids))
    assert rendered.source_prompt_sha256 == brief.source_prompt_sha256
    assert rendered.rendered_prompt_sha256 != brief.source_prompt_sha256


def test_narrative_renderer_preserves_dialogue_and_current_message_roles():
    brief = compile_voice_brief(_plan())

    rendered = render_narrative_voice_brief(brief)

    assert rendered.messages[1:-2] == tuple(
        message.as_dict() for message in brief.recent_dialogue
    )
    assert rendered.messages[-1] == brief.current_message.as_dict()
    assert rendered.messages[-2]["role"] == "system"
    assert [message["role"] for message in rendered.messages] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
    ]


def test_narrative_renderer_orders_the_brief_as_one_readable_interface():
    brief = compile_voice_brief(_plan())

    rendered = render_narrative_voice_brief(brief)
    leading = rendered.messages[0]["content"]
    near_turn = rendered.messages[-2]["content"]

    headings = (
        '# I am',
        '# You and us',
        '# Rules of truth and conduct',
        '# We are',
        '# Here is the situation',
        '# The coming days and current sources',
        '# Exact evidence already open',
        '# What I can consult',
    )
    assert [leading.index(heading) for heading in headings] == sorted(
        leading.index(heading) for heading in headings
    )
    assert "Une présence précise." in leading
    assert "La personne travaille à domicile." in leading
    assert "Le départ est prévu demain." in leading
    assert '# At the threshold of this message' in near_turn
    assert '## Situation to recompose' in near_turn
    assert '## Connected source freshness' in near_turn


def test_narrative_renderer_keeps_host_handoff_when_tools_are_unavailable():
    brief = compile_voice_brief(_plan(delegation=False))

    rendered = render_narrative_voice_brief(brief)

    assert rendered.complete is True
    assert '## Limits of this turn' in rendered.messages[0]["content"]
    assert "[OUTIL_HOTE]" in rendered.messages[0]["content"]


def test_temporal_renderer_replaces_raw_agendas_with_one_typed_timeline():
    personal = """# Agenda

- 2026-08-02 soir | Alex | préparer la présentation Acme | le soir | vingt slides.
- 2026-08-03 | Alex | présenter son backlog | horaire non précisé | encore incomplet.
"""
    professional = """# Agenda professionnel (lecture seule)
Dernière synchro : dimanche 2 août 2026, 12:00.
Fenêtre : 14 prochains jours. 1 événement(s).

## mardi 4 août
- 12:00–12:30 sync Acme
"""
    brief = compile_voice_brief(
        _plan(
            personal_agenda=personal,
            professional_agenda=professional,
        )
    )

    assert brief.turn_evidence.near_term_horizon == brief.near_term_timeline
    rendered = render_temporal_voice_brief(brief)
    leading = rendered.messages[0]["content"]

    assert rendered.timeline_entry_count == 3
    assert rendered.authoritative_agenda_representations == 1
    assert "## Canonical agenda timeline" in leading
    assert "Today — 2026-08-02 (Sunday)" in leading
    assert "Tomorrow — 2026-08-03 (Monday)" in leading
    assert "2026-08-04 | 12:00–12:30 sync Acme" in leading
    assert leading.count("préparer la présentation Acme") == 1
    assert leading.count("présenter son backlog") == 1
    assert leading.count("sync Acme") == 1
    assert '# Current authoritative durable sources' in leading
    assert rendered.messages[1:-2] == tuple(
        message.as_dict() for message in brief.recent_dialogue
    )
    assert rendered.messages[-1] == brief.current_message.as_dict()


def test_temporal_renderer_keeps_a_recently_crossed_agenda_authority():
    brief = compile_voice_brief(
        _plan(
            personal_agenda=(
                "- 2026-08-01 | parents d'Alex | déménagement | "
                "horaire non précisé | échéance familiale."
            ),
        )
    )

    rendered = render_temporal_voice_brief(brief)
    leading = rendered.messages[0]["content"]

    assert "## Recently crossed dated horizons" in leading
    assert "[agenda personnel; date crossed 2026-08-02]" in leading
    assert leading.count("parents d'Alex | déménagement") == 1


def test_temporal_renderer_preserves_every_non_temporal_block():
    personal = "- 2026-08-02 | Alex | finir le dossier"
    brief = compile_voice_brief(_plan(personal_agenda=personal))

    rendered = render_temporal_voice_brief(brief)
    leading = rendered.messages[0]["content"]
    dispositions = {record.block_id: record.disposition for record in rendered.coverage}

    assert dispositions["leading_system:temporal_frame"] == "preserved"
    assert dispositions["leading_system:authoritative_sources"] == "transformed"
    for block in brief.leading_blocks:
        if block.name in {"temporal_frame", "authoritative_sources"}:
            continue
        assert block.raw_span.strip() in leading
        assert dispositions[block.block_id] == "preserved"


def test_temporal_renderer_is_an_exact_noop_without_typed_agendas():
    brief = compile_voice_brief(_plan())

    rendered = render_temporal_voice_brief(brief)

    assert rendered.messages == tuple(render_reference_messages(brief))
    assert rendered.rendered_prompt_sha256 == brief.source_prompt_sha256
    assert rendered.char_delta == 0


def test_temporal_canary_requires_flag_user_and_platform_allowlists(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_TEMPORAL_VOICE_BRIEF", "on")
    monkeypatch.setenv(
        "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_USER_IDS",
        "garden",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_PLATFORMS",
        "telegram",
    )

    assert temporal_voice_brief_canary_enabled("garden", "telegram") is True
    assert temporal_voice_brief_canary_enabled("other", "telegram") is False
    assert temporal_voice_brief_canary_enabled("garden", "openclaw") is False

    monkeypatch.delenv(
        "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_USER_IDS",
    )
    assert temporal_voice_brief_canary_enabled("garden", "telegram") is False


def _dialogue_chunks(count: int) -> list[str]:
    return [
        (
            f"## 2026-08-03T12:{index // 60:02d}:{index % 60:02d}+02:00 — "
            f"{'user' if index % 2 == 0 else 'assistant'}\n\nTour {index}."
        )
        for index in range(count)
    ]


@pytest.mark.parametrize("lag", [25, 64, 81])
def test_dialogue_coverage_keeps_the_complete_scene_delta(lag):
    chunks = _dialogue_chunks(100)
    scan = "\n\n".join(chunks)
    recent = "\n\n".join(chunks[-24:])
    delta = "\n\n".join(chunks[-lag:])

    coverage = build_dialogue_coverage(
        scan_dialogue=scan,
        recent_dialogue=recent,
        scene_delta=delta,
        scene_active=True,
        scene_watermark_source_id="src-watermark",
    )

    assert coverage.exact is True
    assert coverage.status == "scene_delta_covered"
    assert len(coverage.expected_turn_ids) == lag
    assert len(coverage.visible_turn_ids) == max(lag, 24)
    assert coverage.missing_turn_ids == ()
    assert "Tour 0." not in coverage.dialogue
    assert f"Tour {100 - lag}." in coverage.dialogue


def test_dialogue_coverage_rejects_a_delta_absent_from_the_scan():
    chunks = _dialogue_chunks(30)

    with pytest.raises(DialogueCoverageError, match="absent"):
        build_dialogue_coverage(
            scan_dialogue="\n\n".join(chunks),
            recent_dialogue="\n\n".join(chunks[-24:]),
            scene_delta=(
                "## 2026-08-03T15:00:00+02:00 — assistant\n\n" "Tour introuvable."
            ),
            scene_active=True,
        )
