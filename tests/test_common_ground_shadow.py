import json
import re
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core import common_ground_shadow
from daemon_core.common_ground_shadow import (
    advance_common_ground_cursor,
    common_ground_shadow_enabled,
    common_ground_shadow_register_path,
    prepare_common_ground_observation,
    read_common_ground_shadow_events,
    read_common_ground_shadow_register,
    record_common_ground_shadow_result,
    validate_common_ground_update,
)
from daemon_core.context_router import ContextRoute
from daemon_core.loop import DaemonCore
from daemon_core.memory import writer
from daemon_core.memory.writer import (
    GroundZeroReference,
    append_ground_zero,
)
from daemon_core.providers.base import LLMProvider


def _memory_root(monkeypatch, tmp_path):
    root = tmp_path / "memory"
    monkeypatch.setattr(writer, "MEMORY_ROOT", root)
    monkeypatch.setattr(common_ground_shadow, "MEMORY_ROOT", root)
    return root


def _reference(user_id, role, content, turn_id):
    reference = append_ground_zero(
        user_id,
        role,
        content,
        turn_id=turn_id,
        return_reference=True,
    )
    assert isinstance(reference, GroundZeroReference)
    return reference


def _observation(monkeypatch, tmp_path):
    _memory_root(monkeypatch, tmp_path)
    prior = "On pourrait appeler ça notre cathédrale sonore."
    prior_ref = _reference("garden", "assistant", prior, "turn-1")
    advanced = advance_common_ground_cursor(
        user_id="garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        current_assistant_l0=prior_ref,
        current_assistant=prior,
    )
    assert advanced.status == "cursor_advanced"

    current = "Oui, cette cathédrale sonore dit exactement ce que j'entends."
    current_ref = _reference("garden", "user", current, "turn-2")
    observation = prepare_common_ground_observation(
        user_id="garden",
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        current_user_l0=current_ref,
        current_user=current,
    )
    assert observation is not None
    return observation


def _shared_language_candidate():
    return {
        "kind": "shared_language",
        "prior_assistant_evidence": (
            "On pourrait appeler ça notre cathédrale sonore."
        ),
        "current_user_evidence": (
            "cette cathédrale sonore dit exactement ce que j'entends"
        ),
        "before": "",
        "movement": (
            "L'assistant a proposé une expression pour une expérience "
            "musicale déjà décrite."
        ),
        "observed_effect": (
            "L'utilisateur a repris l'expression et confirmé qu'elle "
            "désignait justement son expérience."
        ),
        "shared_expression": "cathédrale sonore",
        "shared_meaning": (
            "Une musique monumentale et enveloppante telle qu'ils "
            "l'identifient ensemble."
        ),
        "limits": (
            "Cette expression n'autorise aucune inférence sur l'humeur "
            "présente ni sur toute musique."
        ),
        "reason": "Réemploi explicite avec confirmation de sens.",
    }


def _later_observation(
    monkeypatch,
    tmp_path,
    current=(
        "On pourra reprendre ça demain dans notre cathédrale sonore."
    ),
):
    birth_observation = _observation(monkeypatch, tmp_path)
    birth = record_common_ground_shadow_result(
        user_id="garden",
        observation=birth_observation,
        raw_candidate=_shared_language_candidate(),
    )
    register = read_common_ground_shadow_register("garden")
    assert len(register) == 1

    assistant = "Oui, gardons cette image sur la table."
    assistant_ref = _reference(
        "garden",
        "assistant",
        assistant,
        "turn-2",
    )
    advanced = advance_common_ground_cursor(
        user_id="garden",
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        current_assistant_l0=assistant_ref,
        current_assistant=assistant,
    )
    assert advanced.status == "cursor_advanced"
    current_ref = _reference("garden", "user", current, "turn-3")
    observation = prepare_common_ground_observation(
        user_id="garden",
        source_trace_id="turn-3",
        source_turn_started_at=3.0,
        current_user_l0=current_ref,
        current_user=current,
    )
    assert observation is not None
    return observation, register, birth.candidate_id


def _register_update(candidate_id, evidence, action="reinforce"):
    return {
        "action": action,
        "target_candidate_id": candidate_id,
        "current_user_evidence": evidence,
        "shared_expression": "",
        "shared_meaning": "",
        "observed_use": (
            "L'utilisateur réemploie volontairement l'expression pour "
            "désigner le même espace musical commun."
        ),
        "limits": (
            "Ce réemploi ne décrit ni toute musique ni l'humeur présente."
        ),
        "reason": "Réemploi différé dans le même sens partagé.",
    }


def test_shadow_requires_mode_and_explicit_user_allowlist(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_COMMON_GROUND_SHADOW", "shadow")
    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_SHADOW_USER_IDS",
        "garden,other",
    )

    assert common_ground_shadow_enabled("garden") is True
    assert common_ground_shadow_enabled("unknown") is False

    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_SHADOW_USER_IDS",
        "",
    )
    assert common_ground_shadow_enabled("garden") is False

    monkeypatch.setenv("DAEMON_CORE_COMMON_GROUND_SHADOW", "off")
    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_SHADOW_USER_IDS",
        "*",
    )
    assert common_ground_shadow_enabled("garden") is False


def test_cursor_builds_exact_t_minus_one_to_t_observation(
    monkeypatch,
    tmp_path,
):
    observation = _observation(monkeypatch, tmp_path)

    assert observation.prior_source_trace_id == "turn-1"
    assert observation.source_trace_id == "turn-2"
    assert observation.prior_assistant.startswith("On pourrait appeler")
    assert observation.current_user.startswith("Oui, cette")


def test_cursor_rejects_content_that_does_not_match_exact_l0(
    monkeypatch,
    tmp_path,
):
    _memory_root(monkeypatch, tmp_path)
    assistant_ref = _reference(
        "garden",
        "assistant",
        "Réponse exacte.",
        "turn-1",
    )

    receipt = advance_common_ground_cursor(
        user_id="garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        current_assistant_l0=assistant_ref,
        current_assistant="Réponse modifiée.",
    )

    assert receipt.status == "cursor_rejected"


def test_valid_shared_language_candidate_is_append_only_and_idempotent(
    monkeypatch,
    tmp_path,
):
    observation = _observation(monkeypatch, tmp_path)

    first = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=_shared_language_candidate(),
    )
    second = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=_shared_language_candidate(),
    )

    assert first.status == "recorded"
    assert first.kind == "shared_language"
    assert first.candidate_id
    assert second.status == "duplicate"
    events = read_common_ground_shadow_events("garden")
    assert len(events) == 1
    candidate = events[0]["candidate"]
    assert candidate["shared_expression"] == "cathédrale sonore"
    assert candidate["provenance"]["prior_assistant_l0"]["sha256"]
    assert candidate["provenance"]["current_user_l0"]["sha256"]


def test_shared_language_birth_projects_a_bounded_sourced_register(
    monkeypatch,
    tmp_path,
):
    observation = _observation(monkeypatch, tmp_path)
    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=_shared_language_candidate(),
    )

    assert receipt.status == "recorded"
    register = read_common_ground_shadow_register("garden")
    assert len(register) == 1
    assert register[0]["candidate_id"] == receipt.candidate_id
    assert register[0]["state"] == "candidate"
    assert register[0]["origin"]["provenance"]["prior_assistant_l0"]["sha256"]
    assert register[0]["origin"]["provenance"]["current_user_l0"]["sha256"]
    projection = json.loads(
        common_ground_shadow_register_path("garden").read_text(
            encoding="utf-8"
        )
    )
    assert projection["schema"] == "daemon.common-ground-register-shadow.v1"
    assert projection["source_event_count"] == 1


def test_delayed_reuse_reinforces_candidate_without_new_llm_call(
    monkeypatch,
    tmp_path,
):
    observation, register, candidate_id = _later_observation(
        monkeypatch,
        tmp_path,
    )
    update = _register_update(
        candidate_id,
        "notre cathédrale sonore",
    )

    first = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=None,
        raw_update=update,
        register=register,
    )
    second = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=None,
        raw_update=update,
        register=register,
    )

    assert first.status == "consolidated"
    assert first.action == "reinforce"
    assert first.candidate_id == candidate_id
    assert second.status == "duplicate"
    projected = read_common_ground_shadow_register("garden")
    assert projected[0]["state"] == "reinforced"
    assert len(projected[0]["evidence_event_ids"]) == 2
    assert len(read_common_ground_shadow_events("garden")) == 2


def test_revision_preserves_origin_and_changes_projected_language(
    monkeypatch,
    tmp_path,
):
    current = (
        "Je préfère finalement qu'on appelle ça notre nef électrique."
    )
    observation, register, candidate_id = _later_observation(
        monkeypatch,
        tmp_path,
        current=current,
    )
    update = _register_update(
        candidate_id,
        "notre nef électrique",
        action="revise",
    )
    update["shared_expression"] = "notre nef électrique"
    update["shared_meaning"] = (
        "Une musique monumentale traversée par une énergie électrique."
    )
    original_origin = register[0]["origin"]

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=None,
        raw_update=update,
        register=register,
    )

    assert receipt.status == "consolidated"
    projected = read_common_ground_shadow_register("garden")
    assert projected[0]["state"] == "revised"
    assert projected[0]["shared_expression"] == "notre nef électrique"
    assert projected[0]["origin"] == original_origin


def test_explicit_disavowal_retires_candidate_from_hot_projection(
    monkeypatch,
    tmp_path,
):
    current = (
        "Finalement, je ne veux plus qu'on parle de cathédrale sonore."
    )
    observation, register, candidate_id = _later_observation(
        monkeypatch,
        tmp_path,
        current=current,
    )
    update = _register_update(
        candidate_id,
        "je ne veux plus qu'on parle de cathédrale sonore",
        action="retire",
    )
    update["observed_use"] = (
        "L'utilisateur désavoue explicitement l'expression comme langage commun."
    )
    update["reason"] = "Désaveu explicite."

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=None,
        raw_update=update,
        register=register,
    )

    assert receipt.status == "consolidated"
    assert receipt.action == "retire"
    assert read_common_ground_shadow_register("garden") == []
    assert len(read_common_ground_shadow_events("garden")) == 2


def test_update_rejects_unknown_target_and_invented_evidence(
    monkeypatch,
    tmp_path,
):
    observation, register, candidate_id = _later_observation(
        monkeypatch,
        tmp_path,
    )
    unknown = _register_update(
        "missing",
        "notre cathédrale sonore",
    )
    invented = _register_update(
        candidate_id,
        "citation absente",
    )

    assert validate_common_ground_update(
        observation=observation,
        raw_update=unknown,
        register=register,
    )[1] == "unknown_target_candidate"
    assert validate_common_ground_update(
        observation=observation,
        raw_update=invented,
        register=register,
    )[1] == "update_user_evidence_not_exact"


def test_birth_and_consolidation_cannot_be_recorded_on_the_same_turn(
    monkeypatch,
    tmp_path,
):
    observation, register, candidate_id = _later_observation(
        monkeypatch,
        tmp_path,
    )

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=_shared_language_candidate(),
        raw_update=_register_update(
            candidate_id,
            "notre cathédrale sonore",
        ),
        register=register,
    )

    assert receipt.status == "rejected"
    assert receipt.reason_code == "multiple_common_ground_actions"
    assert len(read_common_ground_shadow_events("garden")) == 2


def test_false_shared_language_is_rejected_without_becoming_memory(
    monkeypatch,
    tmp_path,
):
    observation = _observation(monkeypatch, tmp_path)
    candidate = _shared_language_candidate()
    candidate["shared_expression"] = "mot qui n'a jamais été partagé"

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=candidate,
    )

    assert receipt.status == "rejected"
    assert receipt.reason_code == "expression_missing_from_current_user"
    event = read_common_ground_shadow_events("garden")[0]
    assert "candidate" not in event


def test_invented_user_effect_is_rejected_by_exact_evidence(
    monkeypatch,
    tmp_path,
):
    observation = _observation(monkeypatch, tmp_path)
    candidate = _shared_language_candidate()
    candidate["current_user_evidence"] = (
        "Cette citation n'existe pas dans la réaction."
    )

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=candidate,
    )

    assert receipt.status == "rejected"
    assert receipt.reason_code == "current_user_evidence_not_exact"


def test_user_can_complete_an_assistant_seed_into_shared_language(
    monkeypatch,
    tmp_path,
):
    observation = replace(
        _observation(monkeypatch, tmp_path),
        prior_assistant=(
            "On dirait une cathédrale construite avec des amplis."
        ),
    )
    candidate = _shared_language_candidate()
    candidate["prior_assistant_evidence"] = (
        "une cathédrale construite avec des amplis"
    )
    candidate["shared_expression"] = "cette cathédrale sonore"

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=candidate,
    )

    assert receipt.status == "recorded"
    event = read_common_ground_shadow_events("garden")[0]
    assert event["candidate"]["shared_expression"] == (
        "cette cathédrale sonore"
    )


def test_judge_abstention_is_observable_but_creates_no_candidate(
    monkeypatch,
    tmp_path,
):
    observation = _observation(monkeypatch, tmp_path)

    receipt = record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=None,
    )

    assert receipt.status == "none"
    event = read_common_ground_shadow_events("garden")[0]
    assert event["reason_code"] == "judge_abstained"
    assert "candidate" not in event


def test_trace_is_valid_jsonl(monkeypatch, tmp_path):
    observation = _observation(monkeypatch, tmp_path)
    record_common_ground_shadow_result(
        user_id="garden",
        observation=observation,
        raw_candidate=_shared_language_candidate(),
    )

    path = common_ground_shadow.common_ground_shadow_trace_path("garden")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["schema"] == (
        "daemon.common-ground-shadow.v1"
    )


def test_contextual_after_turn_primes_then_extracts_without_extra_call(
    monkeypatch,
    tmp_path,
):
    _memory_root(monkeypatch, tmp_path)
    monkeypatch.setenv("DAEMON_CORE_COMMON_GROUND_SHADOW", "shadow")
    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_SHADOW_USER_IDS",
        "garden",
    )
    monkeypatch.setattr(
        "daemon_core.loop.refresh_conversation_state",
        lambda **kwargs: "",
    )
    monkeypatch.setattr(
        "daemon_core.loop.run_relanceur",
        lambda **kwargs: {"followup": None, "followups_pref": None},
    )
    monkeypatch.setattr(
        "daemon_core.loop.temporal_curator_enabled",
        lambda user_id: False,
    )

    class Provider(LLMProvider):
        def __init__(self):
            self.calls = 0

        def complete(
            self,
            messages: list[dict],
            temperature: float = 0.4,
        ) -> str:
            self.calls += 1
            content = messages[-1]["content"]
            candidate = None
            update = None
            if '# Common Ground candidate register' in content:
                match = re.search(
                    r'"candidate_id":\s*"([a-f0-9]{64})"',
                    content,
                )
                assert match is not None
                update = _register_update(
                    match.group(1),
                    "notre cathédrale sonore",
                )
            elif 'Common Ground observation window' in content:
                candidate = _shared_language_candidate()
            return json.dumps(
                {
                    "reason": "Ombre seulement.",
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
                    "common_ground_candidate": candidate,
                    "common_ground_update": update,
                },
                ensure_ascii=False,
            )

    provider = Provider()
    harness = DaemonCore(
        provider=provider,
        routing_provider=provider,
        memory_provider=provider,
    )
    route = ContextRoute(mode="contextual", reason="test")

    first_user = _reference("garden", "user", "Comment nommer ça ?", "turn-1")
    first_response = (
        "On pourrait appeler ça notre cathédrale sonore."
    )
    first_assistant = _reference(
        "garden",
        "assistant",
        first_response,
        "turn-1",
    )
    first = harness.update_memory_after_response(
        user_id="garden",
        message="Comment nommer ça ?",
        response=first_response,
        route=route,
        selected_souvenirs=[],
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        current_user_l0=first_user,
        current_assistant_l0=first_assistant,
    )

    second_message = (
        "Oui, cette cathédrale sonore dit exactement ce que j'entends."
    )
    second_user = _reference(
        "garden",
        "user",
        second_message,
        "turn-2",
    )
    second_assistant = _reference(
        "garden",
        "assistant",
        "Elle nous donne désormais un mot commun.",
        "turn-2",
    )
    second = harness.update_memory_after_response(
        user_id="garden",
        message=second_message,
        response="Elle nous donne désormais un mot commun.",
        route=route,
        selected_souvenirs=[],
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        current_user_l0=second_user,
        current_assistant_l0=second_assistant,
    )

    third_message = (
        "On y revient demain dans notre cathédrale sonore."
    )
    third_user = _reference(
        "garden",
        "user",
        third_message,
        "turn-3",
    )
    third_response = "Oui, l'image reste disponible."
    third_assistant = _reference(
        "garden",
        "assistant",
        third_response,
        "turn-3",
    )
    third = harness.update_memory_after_response(
        user_id="garden",
        message=third_message,
        response=third_response,
        route=route,
        selected_souvenirs=[],
        source_trace_id="turn-3",
        source_turn_started_at=3.0,
        current_user_l0=third_user,
        current_assistant_l0=third_assistant,
    )

    assert first is not None
    assert second is not None
    assert third is not None
    assert provider.calls == 3
    assert second.common_ground_shadow_status == "recorded"
    assert third.common_ground_shadow_status == "consolidated"
    assert third.common_ground_shadow_action == "reinforce"
    events = read_common_ground_shadow_events("garden")
    assert [event["status"] for event in events] == [
        "primed",
        "recorded",
        "consolidated",
    ]
    assert read_common_ground_shadow_register("garden")[0]["state"] == (
        "reinforced"
    )
