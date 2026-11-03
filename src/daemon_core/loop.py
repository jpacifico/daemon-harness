import logging
import os
from daemon_core.after_turn import unique_souvenir_names
from daemon_core.common_ground_shadow import (
    CommonGroundShadowTurn,
)
from daemon_core.providers.base import LLMProvider
from daemon_core.providers.factory import build_conversation_provider, build_memory_provider, build_routing_provider
from daemon_core.memory.reader import limit_recent_dialogue, load_active_context, load_conversation_state, load_immediate_focus, load_recent_dialogue, load_recent_dialogue_through, load_user_agenda, load_user_context
from daemon_core.memory.writer import GroundZeroReference, record_souvenir_recalls
from daemon_core.souvenir_selector import SouvenirSelection, expand_domain_selection, select_souvenirs, select_souvenirs_for_memory_plan
from daemon_core.context_router import RADAR_FINAL_SELECTION_ENV, ContextRoute, route_context_depth
from daemon_core.memory_agent import MemoryAgentResult, run_memory_agent
from daemon_core.conversation_state import refresh_conversation_state
from daemon_core.relanceur import run_relanceur
from daemon_core.temporal_memory import run_temporal_curator, temporal_curator_enabled

logger = logging.getLogger("daemon_core.loop")

DEEP_HISTORY_TURNS = 14
DEFAULT_HISTORY_TURNS = 8
ROUTER_HISTORY_TURNS = 14


def _memory_result_with_temporal(result, temporal):
    if temporal is None:
        return result
    changed = list(getattr(temporal, "changed_paths", ()) or ())
    unchanged = list(getattr(temporal, "unchanged_paths", ()) or ())
    rejected = list(getattr(temporal, "rejected_paths", ()) or ())
    if result is None and not (
        getattr(temporal, "updated", False) or changed or unchanged or rejected
    ):
        return None
    result = result or MemoryAgentResult()
    if not result.source_posture:
        result.source_posture = str(
            getattr(temporal, "source_posture", "") or ""
        )
    result.updated_agenda = bool(
        result.updated_agenda or getattr(temporal, "updated", False)
    )
    for target, additions in (
        (result.changed_paths, changed),
        (result.unchanged_paths, unchanged),
        (result.rejected_paths, rejected),
    ):
        for path in additions:
            if path not in target:
                target.append(path)
    temporal_reason = str(getattr(temporal, "reason", "") or "").strip()
    if temporal_reason and temporal_reason not in result.reason:
        result.reason = " ".join(part for part in (result.reason, temporal_reason) if part)
    return result


def gate_scene_memories(route, trajectory, stance_register, active_context, immediate_focus, user_agenda):
    """Gate legacy scene fragments before response composition.

    The Stance Register is suspended from the canonical runtime: its historical
    parameter remains for compatibility, but its content never reaches the
    voice.
    """
    if not getattr(route, "needs_trajectory", False):
        trajectory = ""
    stance_register = ""
    # The minimal depth was removed on 2026-07-09. Omitting the current
    # conversation and agenda on short turns caused context loss
    # in observed conversations. Focus and agenda now accompany
    # every turn as orientation,
    # without turning personal memory into a scripted role.
    return trajectory, stance_register, active_context, immediate_focus, user_agenda


class DaemonCore:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        memory_provider: LLMProvider | None = None,
        routing_provider: LLMProvider | None = None,
    ) -> None:
        self.provider = provider or build_conversation_provider()
        self.routing_provider = routing_provider or build_routing_provider()
        self.memory_provider = memory_provider or build_memory_provider()

    def update_memory_after_response(
        self,
        user_id: str,
        message: str,
        response: str,
        route: ContextRoute | None = None,
        selected_souvenirs: list[str] | None = None,
        source_trace_id: str = "",
        source_turn_started_at: float = 0.0,
        current_user_l0: GroundZeroReference | dict | None = None,
        current_assistant_l0: GroundZeroReference | dict | None = None,
        defer_replaceable_projections: bool = False,
    ) -> "MemoryAgentResult | None":
        source_dialogue = None
        if current_assistant_l0 is not None:
            source_dialogue = load_recent_dialogue_through(
                user_id,
                current_assistant_l0,
                max_turns=40,
            )
        common_ground_turn = CommonGroundShadowTurn.begin(
            user_id=user_id,
            source_trace_id=source_trace_id,
            source_turn_started_at=source_turn_started_at,
            current_user_l0=current_user_l0,
            current_assistant_l0=current_assistant_l0,
            current_user=message,
            current_assistant=response,
        )

        # Conversational texture applies to all turns, including light turns.
        # It remains outside personal memory and is not used
        # except to inform future initiative.
        conversation_state = (
            load_conversation_state(user_id)
            if defer_replaceable_projections
            else refresh_conversation_state(
                user_id=user_id,
                provider=self.memory_provider,
                recent_dialogue=source_dialogue,
            )
        )
        if route is None:
            user_context = load_user_context(user_id)
            user_preferences = ""  # legacy API slot; preferences.md is archival
            user_agenda = load_user_agenda(user_id)
            active_context = load_active_context(user_id)
            immediate_focus = load_immediate_focus(user_id)
            recent_dialogue = (
                limit_recent_dialogue(
                    source_dialogue,
                    max_turns=ROUTER_HISTORY_TURNS,
                )
                if source_dialogue is not None
                else load_recent_dialogue(
                    user_id,
                    max_turns=ROUTER_HISTORY_TURNS,
                )
            )
            route = route_context_depth(
                message=message,
                provider=self.routing_provider,
                recent_dialogue=recent_dialogue,
                user_context=user_context,
                user_preferences=user_preferences,
                user_agenda=user_agenda,
                moment_memory=active_context,
                current_discussion=conversation_state or immediate_focus,
            )
            logger.info(
                "Radar mémoire async | user_id=%s | mode=%s | depth=%s | interaction=%s | register=%s | candidates=%s | active_request=%r | ambiguity=%s | confidence=%.2f | reason=%s",
                user_id,
                route.mode,
                route.context_depth,
                route.interaction_mode,
                route.scene_register,
                route.candidate_souvenirs,
                route.active_request,
                route.ambiguity,
                route.confidence,
                route.reason,
            )
        else:
            logger.info(
                "Radar mémoire async réutilisé | user_id=%s | mode=%s | depth=%s | selected_souvenirs=%s",
                user_id,
                route.mode,
                route.context_depth,
                selected_souvenirs,
            )
        route_candidates = list(
            getattr(route, "candidate_souvenirs", ()) or ()
        )
        if selected_souvenirs is None:
            selected_souvenirs = route_candidates or None
        else:
            selected_souvenirs = list(
                unique_souvenir_names(
                    selected_souvenirs,
                    route_candidates,
                )
            )
        temporal = None
        temporal_enabled = temporal_curator_enabled(user_id)
        if route.needs_temporal_memory and temporal_enabled:
            temporal = run_temporal_curator(
                user_id=user_id,
                user_message=message,
                provider=self.memory_provider,
                recent_dialogue=(
                    limit_recent_dialogue(source_dialogue, max_turns=24)
                    if source_dialogue is not None
                    else None
                ),
            )
            logger.info(
                "Curation temporelle async | user_id=%s | updated=%s | reason=%s",
                user_id,
                temporal.updated,
                temporal.reason,
            )
        if getattr(temporal, "source_posture", "") == "reported_memory_claim":
            logger.info(
                "Curation temporelle en lecture seule | user_id=%s | "
                "MemoryAgent juge indépendamment la portée factuelle",
                user_id,
            )
        from daemon_core import first_meeting
        from daemon_core.memory import writer

        welcome = first_meeting.welcome_exchange(writer.MEMORY_ROOT, user_id, source_dialogue)
        if welcome and route.mode != "contextual":
            logger.info("MemoryAgent async pendant l'accueil | user_id=%s | route_mode=%s", user_id, route.mode)
        if route.mode != "contextual" and not welcome:
            logger.info("MemoryAgent async ignoré | user_id=%s | route_mode=%s", user_id, route.mode)
            common_ground_turn.complete(
                missing_status="skipped",
                missing_reason=(
                    "non_contextual_route"
                    if common_ground_turn.observation is not None
                    else "observation_unavailable"
                ),
                record_observed_skip=True,
            )
            return _memory_result_with_temporal(None, temporal)
        result = run_memory_agent(
            user_id=user_id,
            provider=self.memory_provider,
            user_message=message,
            assistant_response=response,
            selected_souvenirs=selected_souvenirs,
            followups_enabled=False,
            agenda_updates_enabled=False,
            recent_dialogue=(
                limit_recent_dialogue(source_dialogue, max_turns=8)
                if source_dialogue is not None
                else None
            ),
            common_ground_observation=common_ground_turn.observation,
            common_ground_register=common_ground_turn.register,
            current_user_l0=current_user_l0,
        )
        result = _memory_result_with_temporal(result, temporal)
        common_ground_turn.complete(
            missing_status="primed",
            missing_reason="previous_assistant_cursor_unavailable",
        )
        logger.info("MemoryAgent async | user_id=%s | result=%s", user_id, result)
        if not defer_replaceable_projections:
            relances = run_relanceur(
                user_id=user_id,
                provider=self.memory_provider,
                recent_dialogue=(
                    limit_recent_dialogue(source_dialogue, max_turns=12)
                    if source_dialogue is not None
                    else None
                ),
            )
            result.followup = relances["followup"]
            result.followups_pref = relances["followups_pref"]
        return result

    def refresh_after_turn_projections(
        self,
        *,
        user_id: str,
        current_assistant_l0: GroundZeroReference | dict,
        run_followup: bool,
        scene_projection_expected: bool = False,
    ) -> MemoryAgentResult:
        """Refresh replaceable after-turn views once for a drained burst."""

        source_dialogue = load_recent_dialogue_through(
            user_id,
            current_assistant_l0,
            max_turns=40,
        )
        if not scene_projection_expected:
            refresh_conversation_state(
                user_id=user_id,
                provider=self.memory_provider,
                recent_dialogue=source_dialogue,
            )
        result = MemoryAgentResult(
            reason="Projections après-tour rafraîchies après la rafale."
        )
        if run_followup:
            relances = run_relanceur(
                user_id=user_id,
                provider=self.memory_provider,
                recent_dialogue=limit_recent_dialogue(
                    source_dialogue,
                    max_turns=12,
                ),
            )
            result.followup = relances["followup"]
            result.followups_pref = relances["followups_pref"]
        return result


def select_souvenirs_for_route(
    route: ContextRoute,
    message: str,
    provider: LLMProvider,
    recent_dialogue: str = "",
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
) -> SouvenirSelection | None:
    if route.mode != "contextual":
        return None
    # With DAEMON_CORE_RADAR_FINAL_SELECTION=on, a sufficient radar selection
    # skips expansion. The radar judges sufficiency,
    # rather than a numerical threshold. An empty proposal still goes
    # through the selector even when marked complete: a low-cost fallback
    # for a rare case until shadow evidence establishes otherwise.
    final_mode = os.environ.get(RADAR_FINAL_SELECTION_ENV, "off").strip().lower()
    if final_mode == "on" and route.radar_final_complete and route.radar_final_souvenirs:
        selected = list(route.radar_final_souvenirs)
        record_souvenir_recalls(selected)
        logger.info(
            "P1c on | sélection finale du radar, zoom évité | souvenirs=%s | reason=%s",
            selected,
            route.radar_final_reason,
        )
        return SouvenirSelection(
            selected=selected,
            reason=route.radar_final_reason or "Final selection judged sufficient by the radar.",
            confidence=route.confidence,
            source="radar_final",
            ambiguity=route.ambiguity,
        )
    max_souvenirs = 6 if route.context_depth == "deep" else 4
    selection = select_souvenirs_for_memory_plan(
        plan=route.memory_plan,
        seed_candidates=route.candidate_souvenirs,
        message=message,
        provider=provider,
        recent_dialogue=recent_dialogue,
        user_context=user_context,
        user_preferences=user_preferences,
        user_agenda=user_agenda,
        moment_memory=moment_memory,
        current_discussion=current_discussion,
        max_souvenirs=max_souvenirs,
    )
    if selection is not None:
        logger.info(
            "Souvenirs sélectionnées par MemoryPlan | selected=%s | subjects=%s | facets=%s | confidence=%.2f",
            selection.selected,
            route.memory_plan.subjects,
            route.memory_plan.facets,
            selection.confidence,
        )
    elif route.candidate_souvenirs:
        selected = route.candidate_souvenirs[:max_souvenirs]
        logger.info(
            "Souvenirs sélectionnées par radar | selected=%s | confidence=%.2f | reason=%s",
            selected,
            route.confidence,
            route.reason,
        )
        selection = SouvenirSelection(
            selected=selected,
            reason=f"Candidate souvenirs proposed by the radar: {route.reason}",
            confidence=route.confidence,
            source="radar",
            ambiguity=route.ambiguity,
        )
        selection = expand_domain_selection(
            selection,
            message=message,
            provider=provider,
            recent_dialogue=recent_dialogue,
            user_context=user_context,
            user_preferences=user_preferences,
            user_agenda=user_agenda,
            moment_memory=moment_memory,
            current_discussion=current_discussion,
            max_souvenirs=max_souvenirs,
        )
    else:
        selection = select_souvenirs(
            message=message,
            provider=provider,
            recent_dialogue=recent_dialogue,
            user_context=user_context,
            user_preferences=user_preferences,
            user_agenda=user_agenda,
            moment_memory=moment_memory,
            current_discussion=current_discussion,
            max_souvenirs=max_souvenirs,
        )
    if selection.selected:
        record_souvenir_recalls(selection.selected)
    radar_final = list(getattr(route, "radar_final_souvenirs", []) or [])
    if radar_final:
        selected_set = set(selection.selected)
        radar_set = set(radar_final)
        logger.info(
            "P1b shadow | radar_final=%s | complete=%s | complete_reason=%s | selector=%s | exact=%s | overlap=%s/%s",
            radar_final,
            route.radar_final_complete,
            route.radar_final_reason,
            selection.selected,
            radar_set == selected_set,
            len(radar_set & selected_set),
            len(radar_set | selected_set),
        )
    return selection
