import sys
import json
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.memory.reader import load_active_context, load_souvenir_facet_label_catalog, load_souvenir_structural_catalog, load_immediate_focus, load_user_agenda, load_user_context, load_user_preferences
from daemon_core.memory.writer import reset_active_context, reset_immediate_focus, reset_user_agenda, reset_user_context, reset_user_preferences, write_user_preferences
from daemon_core.memory_agent import _apply_memory_agent_result, _build_souvenir_merge_messages, _build_memory_agent_messages, _souvenir_anchor_summary, _souvenir_index_entry, _clean_immediate_focus, _discovery_additions_from_checks, _extract_followup, _extract_followups_pref, _merge_souvenir_index, _persona_update_content, _sanitize_souvenir_name, load_named_souvenirs, run_memory_agent
from daemon_core.common_ground_shadow import CommonGroundObservation
from daemon_core.common_ground_shadow import CommonGroundShadowReceipt
from daemon_core.memory.writer import GroundZeroReference
from daemon_core import persona
from daemon_core.providers.base import LLMProvider


class MemoryAgentProvider(LLMProvider):
    def __init__(self):
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return '{\n  "reason": "Nouvelle identité et demande explicite de brièveté.",\n  "user_context_md": "# User Context\\n\\n- Prénom : Test",\n  "preferences_md": "# Ancien manuel\\n\\n- Cette sortie doit être ignorée.",\n  "conversation_update": {"should_update": true, "evidence": "Fais court.", "conversation_md": "# Conversational manner\\n\\n- Réponses courtes et naturelles."},\n  "persona_update": {"should_update": false, "evidence": "", "persona_md": null},\n  "agenda_md": "# Agenda\\n\\n- Vendredi | Sam | dessin | 18h15-19h45 | récupération après activité vers 19h45.",\n  "active_context_md": "# Contexte actif\\n\\n## Sujets actifs\\n- Test de mémoire\\n\\n## Questions ouvertes\\n- Aucune\\n\\n## Points de vigilance\\n- Garder simple\\n\\n## Dernière mise à jour\\n2026-05-01",\n  "immediate_focus_md": "# Focus immédiat\\n\\n- Objectif : faire connaissance",\n  "capsule_index_md": null,\n  "capsule_updates": []\n}'


def test_memory_agent_keeps_conversation_manner_read_only_without_touching_preferences():
    user_id = "test-memory-agent"
    manner_path = persona.conversation_manner_path(user_id)
    manner_path.unlink(missing_ok=True)
    persona.write_conversation_manner(
        '# Conversational manner\n\n- Socle stable.', user_id
    )
    original_manner = manner_path.read_bytes()
    original_mtime_ns = manner_path.stat().st_mtime_ns
    reset_user_context(user_id)
    reset_user_preferences(user_id)
    write_user_preferences(user_id, "# Manuel historique\n\n- Contenu à préserver.")
    reset_user_agenda(user_id)
    reset_active_context(user_id)
    reset_immediate_focus(user_id)

    try:
        provider = MemoryAgentProvider()
        result = run_memory_agent(
            user_id=user_id,
            provider=provider,
            user_message="Je m'appelle Test. Fais court.",
            assistant_response="Enchanté.",
        )

        assert result.updated_user_context is True
        assert result.updated_preferences is False
        assert result.updated_conversation_manner is False
        assert result.updated_agenda is False
        assert result.updated_active_context is True
        assert result.updated_immediate_focus is True
        assert "Test" in load_user_context(user_id)
        assert "Contenu à préserver" in load_user_preferences(user_id)
        assert "Cette sortie doit être ignorée" not in load_user_preferences(user_id)
        assert manner_path.read_bytes() == original_manner
        assert manner_path.stat().st_mtime_ns == original_mtime_ns
        assert str(manner_path) not in result.changed_paths
        assert "18h15-19h45" not in load_user_agenda(user_id)
        assert "Test de mémoire" in load_active_context(user_id)
        assert "faire connaissance" in load_immediate_focus(user_id)
        prompt = "\n".join(message["content"] for message in provider.messages)
        assert "Contenu à préserver" not in prompt
        assert '## conversation.md — manner of conversing' in prompt
        assert 'belongs exclusively to Temporal Curator' in prompt
    finally:
        reset_user_context(user_id)
        reset_user_preferences(user_id)
        manner_path.unlink(missing_ok=True)
        reset_user_agenda(user_id)
        reset_active_context(user_id)
        reset_immediate_focus(user_id)


def test_memory_agent_reports_souvenirs_it_actually_consulted(monkeypatch):
    class NoopProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return '{"reason":"Aucun changement.","capsule_updates":[]}'

    monkeypatch.setattr(
        "daemon_core.memory_agent.load_named_souvenirs",
        lambda names: {
            name: f"# {name}\n\n- Fait exact."
            for name in names
            if name != "theme/missing"
        },
    )

    result = run_memory_agent(
        user_id="memory-agent-provenance",
        provider=NoopProvider(),
        user_message="Un changement.",
        assistant_response="Je le note.",
        selected_souvenirs=["theme/loaded", "theme/missing"],
        followups_enabled=False,
    )

    assert result.consulted_souvenirs == ["theme/loaded"]


def test_memory_agent_cannot_read_or_rewrite_rem_episodes(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    episode = (
        memory_root
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "scene.md"
    )
    regular = memory_root / "capsules" / "theme" / "travail" / "projet.md"
    episode.parent.mkdir(parents=True)
    regular.parent.mkdir(parents=True)
    episode.write_text("# Scène REM\n\n- Épisode sourcé.\n", encoding="utf-8")
    regular.write_text("# Projet\n\n## Faits\n- Fait durable.\n", encoding="utf-8")
    index = memory_root / "capsules" / "index.md"
    index.write_text(
        "- theme/scenes_episodiques/2026/scene: Scène REM intacte\n"
        "- theme/travail/projet: Projet\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    loaded = load_named_souvenirs(
        ["theme/scenes_episodiques/2026/scene", "theme/travail/projet"]
    )
    result = _apply_memory_agent_result(
        "garden",
        {
            "capsule_index_md": (
                "- theme/scenes_episodiques/2026/scene: Ligne falsifiée\n"
                "- theme/travail/projet: Projet enrichi\n"
            ),
            "capsule_updates": [
                {
                    "name": "theme/scenes_episodiques/2026/scene",
                    "content_md": "# Scène REM falsifiée",
                    "index_line": "- theme/scenes_episodiques/2026/scene: falsifiée",
                }
            ],
        },
    )

    assert list(loaded) == ["theme/travail/projet"]
    assert episode.read_text(encoding="utf-8") == "# Scène REM\n\n- Épisode sourcé.\n"
    final_index = index.read_text(encoding="utf-8")
    assert "Scène REM intacte" in final_index
    assert "Ligne falsifiée" not in final_index
    assert result.updated_souvenirs == []


def test_memory_agent_prompt_keeps_one_off_situations_out_of_timeless_souvenirs():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="Mes parents seront là aussi pour déjeuner.",
        assistant_response="D'accord.",
    )
    system = messages[0]["content"]

    assert 'never becomes a lasting fact without its episode or date' in system
    assert 'past presence into current composition' in system
    assert 'would manufacture a false present' in system
    assert 'The current physical and social setting' in system
    assert 'certain participant in every activity' in system


def test_memory_agent_never_writes_agenda_even_with_legacy_flag_enabled():
    user_id = "test-memory-agent-agenda-governance"
    reset_user_agenda(user_id)
    try:
        result = _apply_memory_agent_result(
            user_id,
            {
                "reason": "Mauvais classement d'un souvenir durable.",
                "source_posture": "direct_fact",
                "agenda_md": (
                    "# Agenda\n\n"
                    "- En cours | Alex et Sam | brûlerie | chocolat viennois"
                ),
                "capsule_updates": [],
            },
            user_message="Sam aime leur chocolat viennois.",
            agenda_updates_enabled=True,
        )

        assert result.updated_agenda is False
        assert result.rejected_paths == []
        assert "brûlerie" not in load_user_agenda(user_id)
    finally:
        reset_user_agenda(user_id)


def test_memory_agent_structural_catalog_excludes_descriptive_index_anchors(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    souvenir = memory_root / "capsules" / "theme" / "famille" / "famille_elargie.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text(
        """---
subjects:
- sœur de Alex
- Olivier
- Andréa
- Timothé
facets:
- famille élargie
---
# Capsule: theme/famille/famille_elargie

## Faits
- La composition exacte de la famille vit dans ce corps.
""",
        encoding="utf-8",
    )
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/famille/famille_elargie: Sœur avec Olivier, Andréa et Timothé\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    catalog = load_souvenir_structural_catalog(include_subjects=False)
    facets = load_souvenir_facet_label_catalog()

    assert "theme/famille/famille_elargie" in catalog
    assert "facets: famille élargie" in catalog
    assert "subjects:" not in catalog
    assert "Olivier" not in catalog
    assert "Andréa" not in catalog
    assert "Timothé" not in catalog
    assert "famille élargie" in facets
    assert "Olivier" not in facets
    assert "Andréa" not in facets
    assert "Timothé" not in facets


def _common_ground_observation():
    return CommonGroundObservation(
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        prior_source_trace_id="turn-1",
        prior_source_turn_started_at=1.0,
        prior_assistant_l0=GroundZeroReference(
            path="l0.md",
            offset=0,
            length=10,
            sha256="a" * 64,
            role="assistant",
            turn_id="turn-1",
        ),
        current_user_l0=GroundZeroReference(
            path="l0.md",
            offset=10,
            length=10,
            sha256="b" * 64,
            role="user",
            turn_id="turn-2",
        ),
        prior_assistant=(
            "On pourrait appeler ça notre cathédrale sonore."
        ),
        current_user=(
            "Oui, cette cathédrale sonore dit exactement ce que j'entends."
        ),
    )


def _common_ground_register():
    return [
        {
            "candidate_id": "c" * 64,
            "state": "candidate",
            "shared_expression": "cathédrale sonore",
            "shared_meaning": "Une musique monumentale et enveloppante.",
            "origin": {
                "event_id": "origin-event",
                "observed_at": "2026-07-28T10:00:00+00:00",
                "provenance": {
                    "prior_source_trace_id": "turn-origin-1",
                    "source_trace_id": "turn-origin-2",
                    "prior_assistant_l0": {"sha256": "a" * 64},
                    "current_user_l0": {"sha256": "b" * 64},
                },
            },
            "latest_evidence": {
                "event_id": "origin-event",
                "observed_at": "2026-07-28T10:00:00+00:00",
            },
        }
    ]


def test_common_ground_shadow_prompt_is_absent_by_default():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="Oui.",
        assistant_response="Très bien.",
    )

    assert "Common Ground" not in messages[0]["content"]
    assert "common_ground_candidate" not in messages[0]["content"]
    assert "Fenêtre d'observation" not in messages[1]["content"]


def test_common_ground_shadow_extends_the_existing_judgment_only():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="On continue.",
        assistant_response="D'accord.",
        common_ground_observation=_common_ground_observation(),
    )
    system = messages[0]["content"]
    user = messages[1]["content"]

    assert '"common_ground_candidate": null' in system
    assert '"common_ground_update": null' in system
    assert 'Silence proves nothing' in system
    assert 'lexical repetition' in system
    assert 'never a conversational rule' in system
    assert 'Previous assistant response (T-1)' in user
    assert 'Following user reaction (T)' in user
    assert "cathédrale sonore" in user


def test_common_ground_register_is_bounded_context_for_same_judgment():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="On y revient dans notre cathédrale sonore.",
        assistant_response="Oui.",
        common_ground_observation=_common_ground_observation(),
        common_ground_register=_common_ground_register(),
    )
    system = messages[0]["content"]
    user = messages[1]["content"]

    assert 'Common Ground consolidation' in system
    assert "reinforce|revise|retire" in system
    assert 'Only T and the sourced candidate constitute evidence' in system
    assert '`revise` takes precedence over a new birth' in system
    assert '# Common Ground candidate register' in user
    assert "c" * 64 in user
    assert "turn-origin-1" not in user


def test_common_ground_shadow_reuses_one_memory_agent_call(
    monkeypatch,
):
    class Provider(LLMProvider):
        def __init__(self):
            self.calls = 0

        def complete(
            self,
            messages: list[dict],
            temperature: float = 0.4,
        ) -> str:
            self.calls += 1
            return json.dumps(
                {
                    "reason": "Rien d'autre à mémoriser.",
                    "user_context_md": None,
                    "preferences_md": None,
                    "persona_update": {
                        "should_update": False,
                        "evidence": "",
                        "persona_md": None,
                    },
                    "agenda_md": None,
                    "active_context_md": None,
                    "immediate_focus_md": None,
                    "capsule_index_md": None,
                    "capsule_updates": [],
                    "common_ground_candidate": None,
                    "common_ground_update": None,
                }
            )

    captured = {}

    def record(**kwargs):
        captured.update(kwargs)
        return CommonGroundShadowReceipt(
            status="none",
            reason_code="judge_abstained",
            event_id="event",
        )

    monkeypatch.setattr(
        "daemon_core.memory_agent._record_common_ground_safely",
        record,
    )
    provider = Provider()

    result = run_memory_agent(
        user_id="common-ground-one-call",
        provider=provider,
        user_message="On continue.",
        assistant_response="D'accord.",
        selected_souvenirs=[],
        followups_enabled=False,
        common_ground_observation=_common_ground_observation(),
        common_ground_register=_common_ground_register(),
    )

    assert provider.calls == 1
    assert captured["raw_candidate"] is None
    assert captured["raw_update"] is None
    assert captured["register"] == _common_ground_register()
    assert result.common_ground_shadow_status == "none"


def test_souvenir_merge_prompt_audits_only_cross_scope_discovery_routes():
    messages = _build_souvenir_merge_messages(
        name="theme/loisirs/photographie",
        current="# Capsule\n\n## Faits\n- Un appareil a été sauvé pendant un tri.",
        proposed="# Capsule\n\n## Faits\n- Le retrouver serait émouvant.",
        user_message="Le retrouver serait émouvant.",
    )

    system = messages[0]["content"]
    assert 'outside the scope' in system
    assert "instance of the souvenir's theme" in system
    assert 'merely adjacent circumstance' in system
    assert "discovery_checks" in system


def test_discovery_additions_require_an_explicit_inverse_route():
    additions = _discovery_additions_from_checks([
        {
            "other_endpoint": None,
            "inverse_need": None,
            "add_facet": "étiquette orpheline",
        },
        {
            "other_endpoint": "tri familial",
            "inverse_need": "Qu'avais-je gardé pendant ce tri ?",
            "add_facet": "objets sauvés",
        },
    ])

    assert additions == {"subjects": [], "facets": ["objets sauvés"]}


def test_memory_agent_can_leave_agenda_to_dedicated_temporal_owner(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "# Agenda\n\n- Dimanche | retour à Lyon | soir\n"
    agenda.write_text(original, encoding="utf-8")

    result = _apply_memory_agent_result(
        "garden",
        {"agenda_md": "# Agenda\n\n- Vendredi | retour"},
        agenda_updates_enabled=False,
    )

    assert result.updated_agenda is False
    assert agenda.read_text(encoding="utf-8") == original


def test_memory_agent_keeps_a_reported_memory_claim_out_of_durable_files(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    agenda = user_root / "agenda.md"
    agenda.write_text(
        "# Agenda\n\n- 2026-08-05 | présenter le backlog | horaire non précisé\n",
        encoding="utf-8",
    )

    result = _apply_memory_agent_result(
        "garden",
        {
            "source_posture": "reported_memory_claim",
            "reason": "Le message demande de revérifier une parole passée.",
            "agenda_md": "# Agenda\n\n- 2026-08-05 matin | présenter le backlog",
            "active_context_md": "# Contexte actif\n\n- Présentation le matin.",
            "immediate_focus_md": "# Focus immédiat\n\n- Présentation le matin.",
            "capsule_index_md": "- theme/travail/backlog: présentation le matin",
            "capsule_updates": [
                {
                    "name": "theme/travail/backlog",
                    "content_md": "# Backlog\n\n- Présentation le matin.",
                }
            ],
        },
    )

    assert result.changed_paths == []
    assert result.updated_agenda is False
    assert "horaire non précisé" in agenda.read_text(encoding="utf-8")
    assert not (memory_root / "capsules" / "theme" / "travail" / "backlog.md").exists()
    assert "sans mutation durable" in result.reason


def test_memory_agent_leaves_direct_temporal_correction_to_temporal_curator(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    agenda.write_text(
        "# Agenda\n\n- 2026-08-05 | réunion | horaire non précisé\n",
        encoding="utf-8",
    )

    result = _apply_memory_agent_result(
        "garden",
        {
            "source_posture": "direct_fact",
            "agenda_md": "# Agenda\n\n- 2026-08-05 | réunion | 09:00",
        },
    )

    assert result.updated_agenda is False
    assert "09:00" not in agenda.read_text(encoding="utf-8")
    assert "horaire non précisé" in agenda.read_text(encoding="utf-8")


def test_existing_souvenir_merge_receives_read_only_agenda_and_can_omit_schedule(
    tmp_path, monkeypatch,
):
    memory_root = tmp_path / "memory"
    for module in ("daemon_core.memory_agent", "daemon_core.memory.reader", "daemon_core.memory.writer"):
        monkeypatch.setattr(module + ".MEMORY_ROOT", memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    agenda_text = "# Agenda\n\n- 2026-10-12 | Alex | atelier | 15h00\n"
    agenda.write_text(agenda_text, encoding="utf-8")
    target = memory_root / "capsules/theme/workshop.md"
    target.parent.mkdir(parents=True)
    current = "# Workshop\n\n## Faits\n- Alex répare une étagère."
    target.write_text(current + "\n", encoding="utf-8")

    class MergeProvider(LLMProvider):
        calls = 0

        def complete(self, messages, temperature=0.4):
            self.calls += 1
            assert agenda_text in messages[-1]["content"]
            assert "scheduling-only" in messages[0]["content"]
            return json.dumps({"content_md": current})

    provider = MergeProvider()
    result = _apply_memory_agent_result(
        "garden", {"source_posture": "direct_fact", "capsule_updates": [{
            "name": "theme/workshop",
            "content_md": current + "\n- Rendez-vous à l’atelier le 12 octobre 2026 à 15 h.",
        }]}, provider=provider, user_message="Le rendez-vous est le 12 octobre à 15 h.",
    )
    assert provider.calls == 1
    assert result.updated_souvenirs == []
    assert result.rejected_paths == []
    assert target.read_text() == current + "\n"
    assert agenda.read_text() == agenda_text


def test_existing_souvenir_keeps_dated_outcome_near_agenda(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    for module in ("daemon_core.memory_agent", "daemon_core.memory.reader", "daemon_core.memory.writer"):
        monkeypatch.setattr(module + ".MEMORY_ROOT", memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    agenda_text = "# Agenda\n\n- 2026-10-12 | Alex | atelier | 15h00\n"
    agenda.write_text(agenda_text, encoding="utf-8")
    target = memory_root / "capsules/theme/workshop.md"
    target.parent.mkdir(parents=True)
    current = "# Workshop\n\n## Faits\n- Alex répare une étagère."
    target.write_text(current + "\n", encoding="utf-8")
    proposed = current + "\n- Le 12 octobre 2026, l’atelier a montré à Alex une teinte chêne qui lui plaît."

    class MergeProvider(LLMProvider):
        calls = 0

        def complete(self, messages, temperature=0.4):
            self.calls += 1
            assert agenda_text in messages[-1]["content"]
            return json.dumps({"content_md": proposed})

    provider = MergeProvider()
    result = _apply_memory_agent_result(
        "garden", {"source_posture": "direct_fact", "capsule_updates": [{
            "name": "theme/workshop", "content_md": proposed,
        }]}, provider=provider,
        user_message="L’atelier m’a montré le 12 octobre une teinte chêne qui me plaît.",
    )
    assert provider.calls == 1
    assert result.updated_souvenirs == ["theme/workshop"]
    assert result.rejected_paths == []
    assert target.read_text() == proposed.replace("12 octobre 2026", "2026-10-12") + "\n"
    assert agenda.read_text() == agenda_text


def test_souvenir_write_boundary_allows_durable_fact_near_agenda_topic(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    agenda.write_text(
        "# Agenda\n\n"
        "- 2026-08-24 au 2026-08-28 | Sam | stage de ping-pong\n",
        encoding="utf-8",
    )

    result = _apply_memory_agent_result(
        "garden",
        {
            "source_posture": "direct_fact",
            "capsule_updates": [{
                "name": "theme/famille/sam",
                "content_md": (
                    "# Capsule\n\n## Faits\n"
                    "- Le 24 août 2026, Sam dit aimer beaucoup le ping-pong."
                ),
            }],
        },
        user_message="Sam aime beaucoup le ping-pong.",
    )

    assert result.updated_souvenirs == ["theme/famille/sam"]
    assert result.skipped_souvenirs == []


def test_souvenir_write_boundary_rejects_invalid_yaml_before_creation(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)

    result = _apply_memory_agent_result(
        "garden",
        {
            "source_posture": "direct_fact",
            "capsule_updates": [{
                "name": "theme/loisirs/sculpture",
                "content_md": (
                    "---\nsubjects:\n  - utilisateur\n"
                    "  - @claycreations\nfacets:\n  - sculpture\n---\n"
                    "# Capsule\n\n## Faits\n"
                    "- L'utilisateur suit ce compte de sculpture."
                ),
            }],
        },
        user_message="Je suis le compte @claycreations.",
    )

    target = (
        memory_root / "capsules" / "theme" / "loisirs" / "sculpture.md"
    )
    assert result.updated_souvenirs == []
    assert result.skipped_souvenirs == ["theme/loisirs/sculpture"]
    assert result.rejected_paths == ["capsules/theme/loisirs/sculpture.md"]
    assert "frontmatter YAML invalide (ScannerError)" in result.reason
    assert not target.exists()


def test_souvenir_write_boundary_does_not_merge_distinct_events_on_same_day(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    agenda.write_text(
        "# Agenda\n\n"
        "- 2026-09-03 | Alex | rendez-vous chez un ophtalmologue | 17h00\n",
        encoding="utf-8",
    )

    result = _apply_memory_agent_result(
        "garden",
        {
            "source_posture": "direct_fact",
            "capsule_updates": [{
                "name": "theme/sante/dentaire",
                "content_md": (
                    "# Capsule\n\n## Faits\n"
                    "- Le contrôle chez le dentiste est prévu le "
                    "3 septembre 2026 à 10 h."
                ),
            }],
        },
        user_message=(
            "Le contrôle chez le dentiste est prévu le 3 septembre à 10 h."
        ),
    )

    assert result.updated_souvenirs == ["theme/sante/dentaire"]
    assert result.skipped_souvenirs == []


def test_memory_agent_does_not_rewrite_identical_user_document(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    context = memory_root / "users" / "garden" / "context.md"
    context.parent.mkdir(parents=True)
    content = "# Contexte\n\n- Travail à distance."
    context.write_text(content + "\n", encoding="utf-8")
    before = context.stat().st_mtime_ns

    result = _apply_memory_agent_result(
        "garden",
        {"user_context_md": content},
    )

    assert result.updated_user_context is False
    assert result.changed_paths == []
    assert result.unchanged_paths == ["users/garden/context.md"]
    assert context.stat().st_mtime_ns == before


def test_memory_agent_does_not_merge_or_rewrite_identical_souvenir(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    souvenir = memory_root / "capsules" / "theme" / "work.md"
    souvenir.parent.mkdir(parents=True)
    content = '# Travail\n\n## Faits\n- Rythme confirmé.'
    souvenir.write_text(content + "\n", encoding="utf-8")
    index = memory_root / "capsules" / "index.md"
    index.write_text("- theme/work: Travail\n", encoding="utf-8")
    before = souvenir.stat().st_mtime_ns

    result = _apply_memory_agent_result(
        "garden",
        {
            "capsule_updates": [
                {"name": "theme/work", "content_md": content}
            ]
        },
        provider=type(
            "FailingProvider",
            (),
            {"complete": lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError)},
        )(),
    )

    assert result.updated_souvenirs == []
    assert "capsules/theme/work.md" in result.unchanged_paths
    assert souvenir.stat().st_mtime_ns == before


def test_souvenir_index_merge_preserves_existing_entries_without_style_souvenirs():
    existing = "- theme/communication_style\n- delegation_et_validation\n"
    proposed = "- theme/loisirs/jeux/avec_enfant_general\n- style_de_communication\n"

    merged = _merge_souvenir_index(existing, proposed)

    assert "style_de_communication" not in merged
    assert "theme/communication_style" not in merged
    assert "- delegation_et_validation" in merged
    assert "- theme/loisirs/jeux/avec_enfant_general" in merged


def test_souvenir_index_merge_replaces_existing_entry_by_souvenir_name():
    existing = "- theme/jeux_video: # Jeux vidéo avec enfant (Jeu Alpha, Jeu Beta)\n- theme/cinema: # Cinéma\n"
    proposed = "- theme/jeux_video: # Jeux vidéo avec enfant (Jeu Beta)\n"

    merged = _merge_souvenir_index(existing, proposed)

    assert "- theme/jeux_video: # Jeux vidéo avec enfant (Jeu Beta)" in merged
    assert "Jeu Alpha" not in merged
    assert "- theme/cinema: # Cinéma" in merged


def test_souvenir_update_refreshes_index_entry(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules"
    souvenir_root.mkdir(parents=True)
    index = souvenir_root / "index.md"
    index.write_text("", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "reason": "Test",
            "capsule_updates": [
                {"name": "theme/jeux_video", "content_md": "# Jeux vidéo utilisateur\n- Jeu Alpha : favori."}
            ],
        },
    )

    assert result.updated_souvenirs == ["theme/jeux_video"]
    assert result.updated_index is True
    assert index.read_text(encoding="utf-8") == "- theme/jeux_video: Jeux vidéo utilisateur\n"


def test_souvenir_update_prefers_llm_authored_compact_index_line(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules"
    souvenir_root.mkdir(parents=True)
    index = souvenir_root / "index.md"
    index.write_text(
        "- theme/loisirs/musique/playlist: Playlist — Street Spirit\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "reason": "Ajout de morceaux.",
            "capsule_updates": [{
                "name": "theme/loisirs/musique/playlist",
                "content_md": (
                    "# Playlist\n\n## Faits\n- Street Spirit.\n"
                    "- High Hopes.\n- Gas Panic!.\n"
                ),
                "index_line": (
                    "- theme/loisirs/musique/playlist: Playlist commune — "
                    "Street Spirit, High Hopes, Gas Panic!"
                ),
            }],
        },
    )

    assert result.updated_index is True
    assert index.read_text(encoding="utf-8") == (
        "- theme/loisirs/musique/playlist: Playlist commune — "
        "Street Spirit, High Hopes, Gas Panic!\n"
    )


def test_souvenir_update_rejects_index_line_for_another_souvenir(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules"
    souvenir_root.mkdir(parents=True)
    index = souvenir_root / "index.md"
    index.write_text("", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    _apply_memory_agent_result(
        user_id="test-user",
        data={"capsule_updates": [{
            "name": "theme/loisirs/musique/playlist",
            "content_md": "# Playlist\n\n## Faits\n- Street Spirit.",
            "index_line": "- theme/famille: Mauvaise attribution — Street Spirit",
        }]},
    )

    assert "theme/famille" not in index.read_text(encoding="utf-8")
    assert "theme/loisirs/musique/playlist" in index.read_text(encoding="utf-8")


def test_souvenir_update_preserves_existing_index_when_llm_line_is_missing(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules"
    souvenir_root.mkdir(parents=True)
    index = souvenir_root / "index.md"
    original = "- theme/loisirs/musique/playlist: Playlist — Street Spirit\n"
    index.write_text(original, encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    _apply_memory_agent_result(
        user_id="test-user",
        data={"capsule_updates": [{
            "name": "theme/loisirs/musique/playlist",
            "content_md": "# Playlist\n\n## Faits\n- High Hopes.",
        }]},
    )

    assert index.read_text(encoding="utf-8") == original


def test_existing_souvenir_update_is_merged_without_losing_facts(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules"
    target = souvenir_root / "theme" / "famille" / "region_voisine.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# Capsule: theme/famille/region_voisine\n\n## Faits\n"
        "- Camille est une cousine de Alex.\n"
        "- Eyvan et Coraly sont les enfants de Camille.\n"
        "- Coraly est adulte.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    class MergeProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            assert 'Current version' in messages[1]["content"]
            return json.dumps({
                "content_md": (
                    "# Capsule: theme/famille/region_voisine\n\n## Faits\n"
                    "- Camille est une cousine de Alex.\n"
                    "- Eyvan et Coraly sont les enfants de Camille.\n"
                    "- Coraly est adulte.\n"
                    "- Alex est le parrain de Léa."
                ),
                "removed_existing_lines": [],
                "reason": "Ajout sans perte.",
            }, ensure_ascii=False)

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "reason": "Ajout familial.",
            "capsule_updates": [{
                "name": "theme/famille/region_voisine",
                "content_md": "# Capsule\n\n## Faits\n- Alex est le parrain de Léa.",
            }],
        },
        user_message="Je suis le parrain de Léa.",
        provider=MergeProvider(),
    )

    content = target.read_text(encoding="utf-8")
    assert result.updated_souvenirs == ["theme/famille/region_voisine"]
    assert result.skipped_souvenirs == []
    assert "Eyvan et Coraly" in content
    assert "parrain de Léa" in content


def test_existing_souvenir_update_is_rejected_when_merge_drops_information(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    target = memory_root / "capsules" / "theme" / "famille" / "region_voisine.md"
    target.parent.mkdir(parents=True)
    original = "# Capsule\n\n## Faits\n- Eyvan et Coraly sont les enfants de Camille.\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    class LossyProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps({
                "content_md": "# Capsule\n\n## Faits\n- Alex est le parrain de Léa.",
                "removed_existing_lines": [],
                "reason": "Réécriture partielle.",
            }, ensure_ascii=False)

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "capsule_updates": [{
                "name": "theme/famille/region_voisine",
                "content_md": "# Capsule\n\n## Faits\n- Alex est le parrain de Léa.",
            }],
        },
        user_message="Je suis le parrain de Léa.",
        provider=LossyProvider(),
    )

    assert result.updated_souvenirs == []
    assert result.skipped_souvenirs == ["theme/famille/region_voisine"]
    assert target.read_text(encoding="utf-8") == original


def test_existing_souvenir_frontmatter_survives_llm_merge(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    target = memory_root / "capsules" / "theme" / "famille" / "sam.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\nsubjects: [Sam]\nfacets: [goûts alimentaires]\n"
        "truth_scope: durable\n---\n"
        "# Capsule\n\n## Faits\n- Sam aime les pâtes.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    class MetadataDroppingProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps(
                {
                    "content_md": (
                        "# Capsule\n\n## Faits\n"
                        "- Sam aime les pâtes.\n"
                        "- Sam aime les sushis."
                    ),
                    "removed_existing_lines": [],
                    "reason": "Ajout sans frontmatter dans la proposition.",
                },
                ensure_ascii=False,
            )

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "capsule_updates": [
                {
                    "name": "theme/famille/sam",
                    "content_md": "# Capsule\n\n## Faits\n- Sam aime les sushis.",
                }
            ]
        },
        user_message="Sam aime aussi les sushis.",
        provider=MetadataDroppingProvider(),
    )

    content = target.read_text(encoding="utf-8")
    assert result.updated_souvenirs == ["theme/famille/sam"]
    assert "subjects:\n- Sam" in content
    assert "facets:\n- goûts alimentaires" in content
    assert "truth_scope: durable" in content
    assert "Sam aime les pâtes" in content
    assert "Sam aime les sushis" in content


def test_existing_souvenir_merge_can_complete_discovery_metadata(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    target = memory_root / "capsules" / "theme" / "loisirs" / "photographie.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\nsubjects: [Alex]\nfacets: [photographie]\n---\n"
        "# Capsule\n\n## Faits\n- Un appareil a été sauvé pendant un tri familial.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    class DiscoveryMergeProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps(
                {
                    "content_md": (
                        "# Capsule\n\n## Faits\n"
                        "- Un appareil a été sauvé pendant un tri familial.\n"
                        "- Le retrouver serait émouvant."
                    ),
                    "discovery_checks": [{
                        "fact": "- Un appareil a été sauvé pendant un tri familial.",
                        "other_endpoint": "tri familial",
                        "inverse_need": "Quels objets ont été sauvés pendant le tri ?",
                        "covered_by": [],
                        "add_subject": "famille de Alex",
                        "add_facet": "objets sauvés",
                    }],
                    "removed_existing_lines": [],
                    "reason": "Ajout sans perte.",
                },
                ensure_ascii=False,
            )

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "capsule_updates": [{
                "name": "theme/loisirs/photographie",
                "content_md": "# Capsule\n\n## Faits\n- Le retrouver serait émouvant.",
            }]
        },
        user_message="Le retrouver serait émouvant.",
        provider=DiscoveryMergeProvider(),
    )

    content = target.read_text(encoding="utf-8")
    assert result.updated_souvenirs == ["theme/loisirs/photographie"]
    assert "subjects:\n- Alex\n- famille de Alex" in content
    assert "facets:\n- photographie\n- objets sauvés" in content


def test_existing_fact_can_be_replaced_with_exact_user_evidence(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    target = memory_root / "capsules" / "theme" / "famille" / "region_voisine.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Capsule\n\n## Faits\n- Eyvan a 17 ans.\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    class CorrectionProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps({
                "content_md": "# Capsule\n\n## Faits\n- Eyvan a 18 ans.",
                "removed_existing_lines": [{
                    "line": "- Eyvan a 17 ans.",
                    "evidence": "Non, Eyvan a 18 ans",
                }],
                "reason": "Correction explicite.",
            }, ensure_ascii=False)

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={"capsule_updates": [{
            "name": "theme/famille/region_voisine",
            "content_md": "# Capsule\n\n## Faits\n- Eyvan a 18 ans.",
        }]},
        user_message="Non, Eyvan a 18 ans maintenant.",
        provider=CorrectionProvider(),
    )

    assert result.updated_souvenirs == ["theme/famille/region_voisine"]
    assert "18 ans" in target.read_text(encoding="utf-8")
    assert "17 ans" not in target.read_text(encoding="utf-8")


def test_concurrent_souvenir_updates_merge_against_latest_version(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    target = memory_root / "capsules" / "theme" / "famille" / "region_voisine.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Capsule\n\n## Faits\n- Fait initial.\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)

    class AccumulatingProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            text = messages[1]["content"]
            current = text.split('## Current version\n', 1)[1].split('\n\n## Proposal', 1)[0]
            proposal = text.split('## Proposal\n', 1)[1].split('\n\n## Latest user message', 1)[0]
            additions = [line for line in proposal.splitlines() if line.startswith("- ") and line not in current]
            merged = current.rstrip() + "\n" + "\n".join(additions)
            time.sleep(0.05)
            return json.dumps({"content_md": merged, "removed_existing_lines": [], "reason": "Fusion."})

    provider = AccumulatingProvider()
    barrier = threading.Barrier(3)

    def update(fact: str) -> None:
        barrier.wait()
        _apply_memory_agent_result(
            user_id="test-user",
            data={"capsule_updates": [{
                "name": "theme/famille/region_voisine",
                "content_md": f"# Capsule\n\n## Faits\n- {fact}.",
            }]},
            user_message=f"{fact}.",
            provider=provider,
        )

    threads = [
        threading.Thread(target=update, args=("Deuxième fait",)),
        threading.Thread(target=update, args=("Troisième fait",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    content = target.read_text(encoding="utf-8")
    assert "Fait initial" in content
    assert "Deuxième fait" in content
    assert "Troisième fait" in content


def test_persona_update_requires_memory_agent_evidence(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)

    ignored = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "reason": "Preuve absente.",
            "persona_update": {
                "should_update": True,
                "evidence": "ta personnalité soit plus sarcastique",
                "persona_md": "Tu es plus sarcastique.",
            },
        },
        user_message="J'aime bien quand les réponses ont un peu de relief.",
    )

    assert ignored.updated_persona is False
    assert not (memory_root / "users" / "test-user" / "persona.md").exists()

    applied = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "reason": "Demande explicite avec preuve.",
            "persona_update": {
                "should_update": True,
                "evidence": "J’aimerais que tu aies un certain goût pour les arts",
                "persona_md": "Tu as un goût pour les arts, la peinture, la littérature et l'audace.",
            },
        },
        user_message="J’aimerais que tu aies un certain goût pour les arts, la peinture, la littérature, de l’audace",
    )

    assert applied.updated_persona is True
    assert "goût pour les arts" in (memory_root / "users" / "test-user" / "persona.md").read_text(encoding="utf-8")


def test_persona_update_accepts_only_evidence_from_current_user_message():
    assert (
        _persona_update_content(
            {
                "should_update": True,
                "evidence": "Sois plus féminine et piquante",
                "persona_md": "Tu es plus féminine et piquante.",
            },
            "Sois plus féminine et piquante.",
        )
        == "Tu es plus féminine et piquante."
    )
    assert (
        _persona_update_content(
            {
                "should_update": True,
                "evidence": "Sois plus féminine et piquante",
                "persona_md": "Tu es plus féminine et piquante.",
            },
            "Tu t'es trompée sur l'horaire.",
        )
        == ""
    )
    assert (
        _persona_update_content(
            {
                "should_update": False,
                "evidence": "Sois plus féminine et piquante",
                "persona_md": "Tu es plus féminine et piquante.",
            },
            "Sois plus féminine et piquante.",
        )
        == ""
    )


def test_legacy_conversation_update_is_ignored_even_with_current_message_evidence(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)
    legacy = memory_root / "users" / "test-user" / "preferences.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Manuel historique\n\n- Ne pas modifier.\n", encoding="utf-8")

    manner = persona.conversation_manner_path("test-user")
    manner.write_text("# Manière\n\n- Socle stable.\n", encoding="utf-8")
    original = manner.read_bytes()

    ignored = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "conversation_update": {
                "should_update": True,
                "evidence": "Fais des réponses plus courtes",
                "conversation_md": "# Manière\n\n- Réponses plus courtes.",
            },
        },
        user_message="Fais des réponses plus courtes, s'il te plaît.",
    )

    assert ignored.updated_conversation_manner is False
    assert manner.read_bytes() == original
    assert legacy.read_text(encoding="utf-8") == (
        "# Manuel historique\n\n- Ne pas modifier.\n"
    )


def test_legacy_conversation_update_does_not_create_missing_manner(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)
    path = persona.conversation_manner_path("test-user")
    evidence = "Réponds plus brièvement."

    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "conversation_update": {
                "should_update": True,
                "evidence": evidence,
                "conversation_md": "# Manière\n\n- Plus bref.",
            }
        },
        user_message=evidence,
    )

    assert result.updated_conversation_manner is False
    assert not path.exists()
    assert not result.changed_paths
    assert not result.rejected_paths


def test_souvenir_index_entry_uses_subject_and_concrete_anchors():
    entry = _souvenir_index_entry(
        "theme/loisirs/jeux_video/avec_enfant",
        "# Capsule: theme/loisirs/jeux_video/avec_enfant\n"
        "Sujet: Jeux vidéo auxquels l'utilisateur joue avec son enfant.\n"
        "Portée: jeux vidéo partagés.\n"
        "Hors portée: jeux favoris individuels.\n"
        "\n## Faits\n- Jeu Kart familial.",
    )

    assert entry == "- theme/loisirs/jeux_video/avec_enfant: Jeux vidéo auxquels l'utilisateur joue avec son enfant. — Jeu Kart familial\n"


def test_souvenir_anchor_summary_keeps_concrete_recall_hooks():
    anchors = _souvenir_anchor_summary(
        "# Capsule: theme/style/chaussures\n"
        "Sujet: Chaussures et style vestimentaire de l'utilisateur.\n"
        "Portée: chaussures.\n"
        "Hors portée: musique seule.\n"
        "\n## Faits\n"
        "- Baskets rétro offertes à son enfant.\n"
        "- Référence musicale familiale.\n"
    )

    assert "Baskets rétro" in anchors
    assert "enfant" in anchors
    assert "Référence musicale familiale" in anchors


def test_souvenir_anchor_summary_reads_card_sections():
    anchors = _souvenir_anchor_summary(
        "# Capsule: theme/style\n"
        "Sujet: Style vestimentaire de l'utilisateur.\n"
        "Portée: carte locale.\n"
        "Hors portée: faits précis.\n"
        "\n## Résumé\n"
        "- Style vestimentaire, chaussures, références visuelles.\n"
        "\n## Sous-capsules\n"
        "- theme/style/chaussures : baskets rétro, enfant, référence musicale.\n"
        "\n## Liens proches\n"
        "- theme/loisirs/musique : référence musicale associée.\n"
    )

    assert "theme/style/chaussures" in anchors
    assert "baskets rétro" in anchors


def test_sanitize_souvenir_name_allows_three_theme_levels():
    assert _sanitize_souvenir_name("theme/loisirs/jeux_video/avec_enfant") == "theme/loisirs/jeux_video/avec_enfant"
    assert _sanitize_souvenir_name("theme/loisirs/jeux_video/avec_enfant/switch") == "theme/loisirs/jeux_video/avec_enfant"


def test_immediate_focus_cleaner_removes_assistant_suggested_next_step():
    content = """# Statut
- Sujet courant : habitudes / organisation en remote
- Prochaine étape : choisir entre plan par blocs (matin/aprem/soir) ou checklist simple
- Fait récent : demain = jour de déplacement
"""

    cleaned = _clean_immediate_focus(content)

    assert "Prochaine étape" not in cleaned
    assert "plan par blocs" not in cleaned
    assert "demain = jour de déplacement" in cleaned


def test_immediate_focus_cleaner_removes_unconfirmed_assistant_answer():
    content = "# Focus immédiat\n- Demande explicite : connaître l'horaire de récupération de Sam.\n- Réponse donnée : le vendredi en début d'après-midi.\n- État : réponse non confirmée.\n"

    cleaned = _clean_immediate_focus(content)

    assert "Demande explicite" in cleaned
    assert "Réponse donnée" not in cleaned
    assert "le vendredi en début d'après-midi" not in cleaned
    assert 'réponse non confirmée' in cleaned


def test_memory_agent_prompt_forbids_inferred_transitions():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="J'aime jouer à un jeu de cartes avec mon enfant",
        assistant_response="Tu veux des idées de jeux avec lui ?",
    )
    system = messages[0]["content"]

    assert 'Write a "Transition" only if the user explicitly asks' in system
    assert 'that is not a request for ideas or a transition' in system
    assert 'with no inferred objective' in system
    assert "contexte_actif.md" in system
    assert "# Contexte actif" in system
    assert 'a single independent condition' in system
    assert 'can change or expire separately' in system
    assert 'grouping several independent conditions' in system
    assert 'conversation.md describes how the person asks' in system
    assert 'MemoryAgent never proposes or applies' in system
    assert 'Do not transfer it to persona.md' in system
    assert "conversation_update" not in system


def test_memory_agent_prompt_includes_relevant_souvenir_content_and_conservative_rules():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="- theme/jeux_video | facets: jeu vidéo",
        loaded_souvenir_index="- theme/jeux_video: # Jeux vidéo",
        recent_dialogue="",
        user_message="Jeu Beta, c'est plutôt mon enfant.",
        assistant_response="Bien noté.",
        relevant_souvenirs={
            "theme/jeux_video": "# Jeux vidéo\n- Jeu Alpha : cité comme apprécié.\n- Jeu Beta : attribution à clarifier."
        },
    )
    system = messages[0]["content"]
    user = messages[1]["content"]

    assert 'preserve all already present information by default' in system
    assert 'explicitly contradicts' in system
    assert 'to be confirmed' in system
    assert 'Never turn an assistant recall failure into a memory fact' in system
    assert 'An assistant response never becomes a memory fact' in system
    assert 'Do not write assistant-response content in focus_immediat.md' in system
    assert 'absence of information in the loaded relevant souvenirs' in system
    assert 'Never compact' in system
    assert 'create one or more more-targeted subsouvenirs' in system
    assert 'without information loss' in system
    assert 'concrete recall anchors' in system
    assert 'A souvenir is either a map or a note' in system
    assert 'Memory must remain navigable' in system
    assert "theme/<domaine>/<sujet>" in system
    assert "theme/<domaine>/<sous_domaine>/<sujet>" in system
    assert 'Maximum depth' in system
    assert "user's main language" in system
    assert 'Do not translate domains' in system
    assert "## Sous-capsules" in system
    assert "## Liens proches" in system
    assert "`style`" in system
    assert "A souvenir's name and title define its scope" in system
    assert 'theme/<domaine>__<sujet_precis>` remain readable as a legacy format' in system
    assert "# Capsule: theme/<domaine>/<sujet>" in system
    assert 'discovery YAML frontmatter' in system
    assert "`subjects`" in system
    assert "`facets`" in system
    assert "Sujet: short sentence" in system
    assert "Hors portée:" in system
    assert '## contexte_actif.md — background active context' in user
    assert '## focus_immediat.md — immediate thread' in user
    assert '## conversation.md — manner of conversing' in user
    assert "preferences.md" not in user
    assert "## memory/core/persona.md — daemon's own voice" in user
    assert '## agenda.md — read-only temporal projection' in user
    assert 'Do not keep information in a souvenir merely because it was there before' in system
    assert 'active_context_md` cannot remain null' in system
    assert '## Relevant existing capsules' in user
    assert "### theme/jeux_video" in user
    assert 'Current length' in user
    assert "Jeu Alpha : cité comme apprécié" in user


def test_memory_agent_prompt_enforces_general_curation_boundaries():
    messages = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="- theme/famille/proches | facets: famille élargie",
        loaded_souvenir_index="- theme/famille/proches: Famille élargie",
        recent_dialogue="",
        user_message="Je les vois à chaque séjour.",
        assistant_response="Je comprends.",
        relevant_souvenirs={
            "theme/famille/proches": (
                "# Capsule: theme/famille/proches\n"
                "Sujet: proches.\n"
                "Portée: liens familiaux.\n"
                "Hors portée: horaires.\n\n"
                "## Faits\n- Ce sont des proches."
            )
        },
    )
    system = messages[0]["content"]

    assert 'The date when the user mentions a fact again is not a new fact' in system
    assert 'new independent information' in system
    assert 'agenda.md` is supplied read-only' in system
    assert 'Do not copy an agenda line into a souvenir' in system
    assert 'independently established durable content' in system
    assert 'No present fact may be declared out of scope' in system
    assert 'do not leave a new note isolated' in system
    assert 'Final check before JSON' in system


def test_souvenir_merge_prompt_rejects_remention_and_scope_contradiction():
    system = _build_souvenir_merge_messages(
        name="theme/quotidien/rituel",
        current=(
            "# Capsule: theme/quotidien/rituel\n"
            "Sujet: rituel.\n"
            "Portée: habitudes.\n"
            "Hors portée: événements ponctuels.\n\n"
            "## Faits\n- Le rituel compte beaucoup."
        ),
        proposed=(
            "# Capsule: theme/quotidien/rituel\n"
            "Sujet: rituel.\n"
            "Portée: habitudes.\n"
            "Hors portée: événements ponctuels.\n\n"
            "## Faits\n- Le rituel compte beaucoup.\n"
            "- Aujourd'hui, le rituel lui fera du bien."
        ),
        user_message="Ce rituel va me faire du bien.",
    )[0]["content"]

    assert 'new mention date or confirmation of an already present fact' in system
    assert 'no independent fact' in system
    assert 'no retained or added fact may be declared out of scope' in system
    assert 'without creating an artificial link' in system


# --- relances proactives (followup) -----------------------------------------
def _base_msgs_kwargs():
    return dict(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="heureusement vendredi soir libération",
        assistant_response="Courage.",
    )


def test_followup_block_absent_by_default():
    system = _build_memory_agent_messages(**_base_msgs_kwargs())[0]["content"]
    assert 'Proactive follow-up' not in system  # With the flag off, the prompt is unchanged.


def test_memory_agent_uses_the_shared_source_posture_boundary():
    system = _build_memory_agent_messages(**_base_msgs_kwargs())[0]["content"]

    assert "I already told you: A is B" in system
    assert "Judge the asserted content, not its preamble" in system
    assert 'With `reported_memory_claim`, modify no file' in system


def test_followup_block_present_when_enabled():
    system = _build_memory_agent_messages(**_base_msgs_kwargs(), followups_enabled=True)[0]["content"]
    assert 'Proactive follow-up' in system
    assert '"followup"' in system
    assert 'exact quotation' in system


def test_pending_followups_injected_for_dedup():
    system = _build_memory_agent_messages(
        **_base_msgs_kwargs(), followups_enabled=True,
        pending_followups="- 2026-07-03T19:30 — vendredi libération",
    )[0]["content"]
    assert 'DO NOT duplicate' in system
    assert "vendredi libération" in system
    assert "should_schedule: false" in system


def test_pending_followups_absent_when_none():
    system = _build_memory_agent_messages(**_base_msgs_kwargs(), followups_enabled=True)[0]["content"]
    assert 'DO NOT duplicate' not in system


def test_extract_followup_valid():
    data = {"followup": {"should_schedule": True, "fire_at": "2026-07-01T18:00",
                          "topic": "restitution", "angle": "check-in",
                          "evidence": "vendredi soir libération"}}
    fu = _extract_followup(data, "heureusement vendredi soir libération")
    assert fu == {"topic": "restitution", "fire_at": "2026-07-01T18:00",
                  "angle": "check-in", "evidence": "vendredi soir libération"}


def test_extract_followup_rejected_cases():
    base = {"should_schedule": True, "fire_at": "2026-07-01T18:00", "topic": "t",
            "angle": "check-in", "evidence": "vendredi soir libération"}
    msg = "heureusement vendredi soir libération"
    assert _extract_followup({}, msg) is None
    assert _extract_followup({"followup": {**base, "should_schedule": False}}, msg) is None
    assert _extract_followup({"followup": {**base, "evidence": "jamais dit ça"}}, msg) is None
    assert _extract_followup({"followup": {**base, "topic": ""}}, msg) is None
    assert _extract_followup({"followup": {**base, "fire_at": ""}}, msg) is None


class _FollowupProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return ('{"reason":"r","user_context_md":null,"preferences_md":null,'
                '"persona_update":{"should_update":false,"evidence":"","persona_md":null},'
                '"agenda_md":null,"active_context_md":null,"immediate_focus_md":null,'
                '"capsule_index_md":null,"capsule_updates":[],'
                '"followup":{"should_schedule":true,"fire_at":"2026-07-01T18:00",'
                '"topic":"restitution","angle":"check-in","evidence":"vendredi soir libération"}}')


def test_run_memory_agent_emits_followup_when_enabled():
    result = run_memory_agent(
        user_id="test-followup-emit", provider=_FollowupProvider(),
        user_message="heureusement vendredi soir libération",
        assistant_response="Courage.", selected_souvenirs=[], followups_enabled=True,
    )
    assert result.followup is not None
    assert result.followup["topic"] == "restitution"
    assert result.followup["fire_at"] == "2026-07-01T18:00"


def test_run_memory_agent_ignores_followup_when_disabled():
    result = run_memory_agent(
        user_id="test-followup-off", provider=_FollowupProvider(),
        user_message="heureusement vendredi soir libération",
        assistant_response="Courage.", selected_souvenirs=[], followups_enabled=False,
    )
    assert result.followup is None


def test_extract_followups_pref_disable_and_enable():
    msg = "franchement arrête de m'écrire sans raison"
    data = {"followups_pref": {"action": "disable", "evidence": "arrête de m'écrire sans raison"}}
    assert _extract_followups_pref(data, msg) == {"action": "disable", "evidence": "arrête de m'écrire sans raison"}
    msg2 = "tu peux recommencer à me relancer si tu veux"
    data2 = {"followups_pref": {"action": "enable", "evidence": "recommencer à me relancer"}}
    assert _extract_followups_pref(data2, msg2) == {"action": "enable", "evidence": "recommencer à me relancer"}


def test_extract_followups_pref_rejected_cases():
    msg = "arrête de m'écrire sans raison"
    assert _extract_followups_pref({}, msg) is None
    assert _extract_followups_pref({"followups_pref": {"action": None, "evidence": ""}}, msg) is None
    assert _extract_followups_pref({"followups_pref": {"action": "disable", "evidence": "jamais dit"}}, msg) is None
    assert _extract_followups_pref({"followups_pref": {"action": "bidon", "evidence": "arrête de m'écrire"}}, msg) is None
