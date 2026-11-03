import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from daemon_core.memory_dates import MEMORY_DATE_RULES, normalize_memory_dates
from daemon_core.souvenir_selector import SouvenirSelection, SouvenirSelectionUnavailable, expand_domain_selection, select_souvenirs
from daemon_core.common_ground_shadow import (
    CommonGroundObservation,
    CommonGroundShadowReceipt,
    record_common_ground_shadow_result,
    record_common_ground_shadow_skip,
    render_common_ground_observation,
    render_common_ground_register,
)
from daemon_core.memory.souvenir_index import souvenir_anchor_summary as _souvenir_anchor_summary  # noqa: F401
from daemon_core.memory.souvenir_index import souvenir_index_entry as _souvenir_index_entry
from daemon_core.memory.souvenir_index import existing_souvenir_index_line
from daemon_core.memory.souvenir_index import merge_souvenir_index as _merge_souvenir_index
from daemon_core.memory.souvenir_index import validated_souvenir_index_line
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.obsidian import obsidian_format_enabled, prepare_souvenir_format
from daemon_core.memory.context_pages import (
    context_page_prompt, refresh_navigation_safely, structure_enabled,
    validate_context_page,
)
from daemon_core.memory.graph import MEMBERSHIP_RELATION, parse_souvenir_graph
from daemon_core.memory.metadata import SouvenirMetadata, parse_souvenir_frontmatter, parse_souvenir_metadata, preserve_existing_frontmatter, with_souvenir_metadata
from daemon_core.memory.reader import RESERVED_SOUVENIR_NAMES, is_system_managed_souvenir, list_available_souvenirs, load_active_context, load_souvenir, load_souvenir_facet_label_catalog, load_souvenir_structural_catalog, load_immediate_focus, load_named_souvenir_index, load_recent_dialogue, load_user_agenda, load_user_context
from daemon_core.memory.relation_shadow import (
    MemoryRelationShadowContext,
    MemoryRelationShadowReceipt,
    memory_collection_bootstrap_shadow_enabled,
    memory_performative_relation_retractions_enabled,
    memory_relation_shadow_enabled,
    memory_topology_bundle_shadow_enabled,
    prepare_memory_relation_shadow_context,
    record_memory_relation_shadow_result,
)
from daemon_core.memory.writer import (
    GroundZeroReference,
    active_context_path,
    immediate_focus_path,
    user_context_path,
    write_active_context,
    write_immediate_focus,
    write_user_context,
)
from daemon_core.memory.library_layout import (
    resolve_note, vault_root, with_identity, memory_reference,
)
from daemon_core.memory.note_relocation import (
    RelocationContext, NoteSnapshot, RULES as RELOCATION_RULES,
    capture_context, ensure_snapshot, location_prompt, publish_relocation,
    recover_relocation, relocation_enabled, validate_proposal,
)
from daemon_core.paths import MEMORY_ROOT
from daemon_core.persona import (
    load_conversation_manner,
    load_daemon_persona,
    user_daemon_persona_path,
    write_daemon_persona,
)
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider
from daemon_core.source_posture import SOURCE_POSTURE_PROMPT

logger = logging.getLogger("daemon_core.memory_agent")
prompt_logger = logging.getLogger("daemon_core.prompts")
MAX_MEMORY_AGENT_SOUVENIRS = 4
TARGET_SOUVENIR_CHARS = 1200
WARN_SOUVENIR_CHARS = 1600

_AGENDA_MEMORY_BOUNDARY = """Agenda and durable memory have different responsibilities:
- Do not copy an agenda line into a souvenir. A scheduling-only fact (an appointment, time slot or reminder) belongs to Temporal Curator, not a new souvenir bullet.
- A result, cause, decision or lasting preference established by the latest user message may enrich a souvenir even when the same event and date appear in agenda.md. Preserve useful dates and source links: a shared date or topic is not evidence of duplication.
- The agenda is read-only context, never proof that a planned event happened or a source for inventing history. Keep only independently established durable content from a mixed proposal; omit its scheduling-only additions.
- Apply this distinction to new additions. Do not remove existing facts merely because they overlap the agenda; their correction still requires explicit evidence from the latest user message.
"""


_SITUATION_NOTE_RULES = """Situation continuity (explicit canary):
- When a direct user fact advances a concrete situation across conversations, keep its story together in one ordinary thematic souvenir: what happened, what was tried, what still matters, and the latest explicitly supported outcome. Judge this by meaning, never by a domain or a keyword.
- Reuse a loaded note whose scope already covers that situation. If several loaded notes contain its separate parts and none covers the story, create one short situation note with up to three links to those exact sources. Do not duplicate an already coherent situation or reorganize unrelated notes.
- Its dated chronology may reconcile the exact loaded source bodies and the latest direct user facts. The structural catalogue and assistant guesses cannot supply facts. Keep attribution and uncertainty; never invent a missing transition.
- Distinguish the last known state, with its source date, from a new assertion about today. A topic change, silence, a past appointment or an attempted solution does not prove completion. An explicit correction, abandonment or resolution updates the same note while preserving its dated history.
- Preserve the cause, unsuccessful attempts and dependencies that explain why the situation matters. A later allusion may start from a dependency or the next step rather than the original object: make that supported starting point discoverable in subjects/facets, without copying an answer or inventing adjacent topics.
- A one-off appointment, temporary wish or standalone reminder still belongs to the existing agenda/focus owners. A situation note retains the lasting story, not a duplicate schedule; it has no expiry date merely because time has passed or the issue is resolved.
- For a newly created situation note, use an English technical path within the existing maximum depth, even when the conversation and note content use another language. Keep existing paths stable.
- Keep the ordinary souvenir schema and all source-posture, protected-merge and no-loss rules. No new call, output channel or user-facing instruction is requested."""


def memory_situation_notes_enabled(user_id: str) -> bool:
    """Opt in only named users; ordinary production maintenance stays unchanged."""
    if os.environ.get("DAEMON_CORE_MEMORY_SITUATIONS", "off").strip().lower() != "canary":
        return False
    allowed = {
        value.strip()
        for value in os.environ.get("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", "").split(",")
        if value.strip()
    }
    return bool(str(user_id).strip()) and str(user_id).strip() in allowed


_SOUVENIR_DISCOVERY_AUDIT_RULES = """A future selector will see only the souvenir's path, `subjects` and `facets`.
For each bullet in `## Faits`, provide one `discovery_checks` item, in the same order.
`other_endpoint` names another person, event or situation outside the scope described by the path, Sujet and Portée; use null if the fact stays within that scope.
A name, work or object that is only an instance of the souvenir's theme is not another endpoint: it remains an index anchor and does not become a subject or facet.
`inverse_need` formulates an allusion starting solely from the genuine other endpoint: it must neither name nor categorize the souvenir's main subject or the expected answer.
`covered_by` may cite only the current path and labels that truly represent this starting point; a label describing the answer does not cover the inverse search.
If this starting point remains invisible, provide the smallest missing `add_subject` or `add_facet`, as a brief natural-language label, never a technical identifier.
For an event or situation, the people involved alone do not distinguish what happened: prefer a discriminating facet.
A written relation remains indexable even if the general situation is listed in `Hors portée`; frontmatter opens access to the fact, it does not widen the souvenir's scope.
A merely adjacent circumstance is not enough."""

_SITUATION_DISCOVERY_AUDIT_RULES = """A future selector will see only the souvenir's path, `subjects` and `facets`.
For each bullet in `## Faits`, provide one `discovery_checks` item, in the same order, quoting the exact bullet in `fact`.
`other_endpoint` names a supported starting point from which someone could allude to this fact: a participant, event, cause, attempted solution, dependency or next step. It can belong to the souvenir's own scope; being inside that scope is not a reason to hide it. Use null when the bullet provides no distinct starting point.
An incidental name or instance of the theme is not enough. The exact bullet must establish a meaningful connection; do not infer an adjacent topic from general knowledge.
`inverse_need` formulates a plausible allusion starting from this point without naming the souvenir's main subject or the expected answer.
`covered_by` cites only current paths or labels that represent this starting point. A label for the answer does not cover the allusion.
When the starting point is invisible, provide the smallest missing `add_subject` or `add_facet`, as a brief natural-language label. Prefer a discriminating facet for an event or situation; participants alone need not distinguish it.
Audit all retained facts, including dated history, as well as the new fact. Preserve existing supported subjects/facets and genuinely supported additions from the proposal; a resolution changes the story's state, not whether its history can be found. Reject ungrounded additions. Labels grant access to exact facts, not proof of their current state or permission to widen the note's scope."""

@dataclass
class MemoryAgentResult:
    updated_user_context: bool = False
    # Compatibility field for callers reading historical receipts. The
    # canonical MemoryAgent no longer writes preferences.md.
    updated_preferences: bool = False
    # Compatibility field for historical receipts. Conversation manner is now
    # a stable, manually maintained authority and MemoryAgent never writes it.
    updated_conversation_manner: bool = False
    updated_persona: bool = False
    updated_agenda: bool = False
    updated_active_context: bool = False
    updated_immediate_focus: bool = False
    updated_souvenirs: list[str] = field(default_factory=list)
    relocated_souvenirs: list[str] = field(default_factory=list)
    skipped_souvenirs: list[str] = field(default_factory=list)
    updated_index: bool = False
    changed_paths: list[str] = field(default_factory=list)
    unchanged_paths: list[str] = field(default_factory=list)
    rejected_paths: list[str] = field(default_factory=list)
    consulted_souvenirs: list[str] = field(default_factory=list)
    source_posture: str = ""
    reason: str = ""
    followup: dict | None = None  # Proactive follow-up intent, when enabled.
    followups_pref: dict | None = None  # {"action": "disable"|"enable", "evidence": ...}
    common_ground_shadow_status: str = ""
    common_ground_shadow_candidate_id: str = ""
    common_ground_shadow_kind: str = ""
    common_ground_shadow_action: str = ""
    relation_shadow_status: str = ""
    relation_shadow_event_id: str = ""
    relation_shadow_accepted_count: int = 0
    relation_shadow_rejected_count: int = 0
    collection_shadow_accepted_count: int = 0
    collection_shadow_rejected_count: int = 0
    authorized_relation_removals: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class SouvenirMergeResult:
    content: str
    relation_removals: tuple[dict, ...] = ()
    applied_relation_retractions: tuple[dict, ...] = ()


def run_memory_agent(
    user_id: str,
    provider: LLMProvider,
    user_message: str,
    assistant_response: str,
    selected_souvenirs: list[str] | None = None,
    followups_enabled: bool | None = None,
    agenda_updates_enabled: bool = False,
    recent_dialogue: str | None = None,
    common_ground_observation: CommonGroundObservation | None = None,
    common_ground_register: (
        list[dict] | tuple[dict, ...] | None
    ) = None,
    current_user_l0: GroundZeroReference | dict | None = None,
    relation_shadow_enabled_override: bool | None = None,
    collection_bootstrap_enabled_override: bool | None = None,
    topology_bundle_enabled_override: bool | None = None,
    performative_relation_retractions_enabled_override: bool | None = None,
) -> MemoryAgentResult:
    from daemon_core import followup as _followup

    # A persisted mechanical move is recovered before any fresh source read,
    # including when the feature flag was disabled after an interruption.
    recover_relocation(MEMORY_ROOT)
    consulted_souvenirs: list[str] = []
    relation_shadow_requested = (
        memory_relation_shadow_enabled(user_id)
        if relation_shadow_enabled_override is None
        else relation_shadow_enabled_override
    )
    collection_bootstrap_requested = relation_shadow_requested and (
        memory_collection_bootstrap_shadow_enabled(user_id)
        if collection_bootstrap_enabled_override is None
        else collection_bootstrap_enabled_override
    )
    topology_bundle_requested = collection_bootstrap_requested and (
        memory_topology_bundle_shadow_enabled(user_id)
        if topology_bundle_enabled_override is None
        else topology_bundle_enabled_override
    )
    performative_relation_retractions_requested = topology_bundle_requested and (
        memory_performative_relation_retractions_enabled(user_id)
        if performative_relation_retractions_enabled_override is None
        else performative_relation_retractions_enabled_override
    )
    relation_shadow_context = (
        prepare_memory_relation_shadow_context(
            current_user_l0=current_user_l0,
            user_message=user_message,
        )
        if relation_shadow_requested
        else None
    )
    relation_shadow_known_souvenirs = (
        tuple(
            name
            for name in list_available_souvenirs()
            if not is_system_managed_souvenir(name)
        )
        if relation_shadow_context is not None
        else ()
    )
    fu_on = _followup.followups_enabled() if followups_enabled is None else followups_enabled
    pending_followups = ""
    if fu_on:
        _pending = _followup.list_intents(user_id, state="pending")
        pending_followups = "\n".join(f"- {p.fire_at} — {p.topic}" for p in _pending)
    user_context = load_user_context(user_id)
    conversation_manner = load_conversation_manner(user_id)
    daemon_persona = load_daemon_persona(user_id)
    user_agenda = load_user_agenda(user_id)
    active_context = load_active_context(user_id)
    immediate_focus = load_immediate_focus(user_id)
    if recent_dialogue is None:
        recent_dialogue = load_recent_dialogue(user_id, max_turns=8)
    situation_notes_enabled = memory_situation_notes_enabled(user_id)
    if selected_souvenirs is None:
        relevant_souvenirs = load_relevant_souvenirs_for_memory_agent(
            provider=provider,
            user_message=user_message,
            assistant_response=assistant_response,
            recent_dialogue=recent_dialogue,
            user_context=user_context,
            user_agenda=user_agenda,
            moment_memory=active_context,
            current_discussion=immediate_focus,
            require_success=situation_notes_enabled,
        )
    elif situation_notes_enabled:
        selection = expand_domain_selection(
            SouvenirSelection(selected=selected_souvenirs, reason="After-turn source candidates."),
            message=f'{user_message}\n\nAssistant response:\n{assistant_response}',
            provider=provider,
            recent_dialogue=recent_dialogue,
            user_context=user_context,
            user_agenda=user_agenda,
            moment_memory=active_context,
            current_discussion=immediate_focus,
            max_souvenirs=MAX_MEMORY_AGENT_SOUVENIRS,
            require_success=True,
        )
        # Loading precedes all MemoryAgent writes. A failed source resolution
        # reaches the existing ordered after-turn retry queue, not an empty note set.
        relevant_souvenirs = load_named_souvenirs(selection.selected, require_success=True)
    else:
        relevant_souvenirs = load_named_souvenirs(selected_souvenirs)
    consulted_souvenirs = list(relevant_souvenirs)
    loaded_souvenir_index = load_named_souvenir_index(list(relevant_souvenirs))
    messages = _build_memory_agent_messages(
        user_context=user_context,
        conversation_manner=conversation_manner,
        daemon_persona=daemon_persona,
        user_agenda=user_agenda,
        moment_memory=active_context,
        current_discussion=immediate_focus,
        souvenir_catalog="\n".join(
            line
            for line in load_souvenir_structural_catalog(
                include_subjects=False
            ).splitlines()
            if not is_system_managed_souvenir(
                line.removeprefix("- ").split(" |", 1)[0]
            )
        ),
        loaded_souvenir_index=loaded_souvenir_index,
        recent_dialogue=recent_dialogue,
        user_message=user_message,
        assistant_response=assistant_response,
        relevant_souvenirs=relevant_souvenirs,
        followups_enabled=fu_on,
        pending_followups=pending_followups,
        facet_catalog=load_souvenir_facet_label_catalog(),
        agenda_updates_enabled=agenda_updates_enabled,
        common_ground_observation=common_ground_observation,
        common_ground_register=common_ground_register,
        relation_shadow_enabled=relation_shadow_context is not None,
        collection_bootstrap_enabled=(
            relation_shadow_context is not None and collection_bootstrap_requested
        ),
        topology_bundle_enabled=(
            relation_shadow_context is not None and topology_bundle_requested
        ),
        situation_notes_enabled=situation_notes_enabled,
    )
    relocation_context = None
    if structure_enabled():
        source = prepare_memory_relation_shadow_context(
            current_user_l0=current_user_l0, user_message=user_message, memory_root=MEMORY_ROOT,
        )
        if source is not None:
            messages[-1]["content"] += (
                "\n\nExact latest user source (only supports this turn): [["
                + source.source_ref + "]]\n"
                "Preserve older source links. Attach this source to new facts where useful."
            )
            if relocation_enabled(MEMORY_ROOT):
                relocation_context = capture_context(MEMORY_ROOT, relevant_souvenirs, source.source_ref)
                messages[0]["content"] += "\n\n" + RELOCATION_RULES
                messages[-1]["content"] += location_prompt(relocation_context)
    shadow_receipt = None
    relation_shadow_receipt = None
    try:
        log_llm_prompt(prompt_logger, "MEMORY AGENT", messages)
        raw = provider.complete(messages=messages, temperature=0.0)
        data = json.loads(_strip_json_fence(raw))
        data = _guard_reported_memory_claim(data)
        if common_ground_observation is not None:
            shadow_receipt = _record_common_ground_safely(
                user_id=user_id,
                observation=common_ground_observation,
                raw_candidate=data.get("common_ground_candidate"),
                raw_update=data.get("common_ground_update"),
                register=common_ground_register or (),
            )
        result = _apply_memory_agent_result(
            user_id,
            data,
            user_message=user_message,
            provider=provider,
            agenda_updates_enabled=agenda_updates_enabled,
            derive_relation_removals=topology_bundle_requested,
            performative_relation_retractions=(
                performative_relation_retractions_requested
            ),
            relocation_context=relocation_context,
        )
        if relation_shadow_context is not None:
            relation_shadow_receipt = _record_memory_relation_safely(
                user_id=user_id,
                context=relation_shadow_context,
                user_message=user_message,
                raw_updates=_with_authorized_relation_removals(
                    _proposed_relation_updates(
                        data,
                        topology_bundle_enabled=topology_bundle_requested,
                    ),
                    result.authorized_relation_removals,
                ),
                raw_collection_updates=(
                    _proposed_collection_updates(
                        data,
                        topology_bundle_enabled=topology_bundle_requested,
                    )
                    if collection_bootstrap_requested
                    else None
                ),
                available_souvenirs=relation_shadow_known_souvenirs,
                proposed_souvenirs=_proposed_souvenir_names(data),
            )
        result.consulted_souvenirs = consulted_souvenirs
        if fu_on:
            result.followup = _extract_followup(data, user_message)
            result.followups_pref = _extract_followups_pref(data, user_message)
        _apply_common_ground_receipt(result, shadow_receipt)
        _apply_relation_shadow_receipt(
            result,
            relation_shadow_receipt,
            requested=relation_shadow_requested,
            context_available=relation_shadow_context is not None,
        )
        refresh_navigation_safely(MEMORY_ROOT)
        return result
    except Exception as exc:
        from daemon_core.maintenance_errors import reraise_memory_update_failure

        reraise_memory_update_failure(exc)
        logger.info("MemoryAgent ignoré | error=%s", exc.__class__.__name__)
        if common_ground_observation is not None and shadow_receipt is None:
            shadow_receipt = _record_common_ground_skip_safely(
                user_id=user_id,
                observation=common_ground_observation,
                status="failed",
                reason_code=f"memory_agent_{exc.__class__.__name__}",
            )
        result = MemoryAgentResult(
            consulted_souvenirs=consulted_souvenirs,
            reason=f"MemoryAgent indisponible: {exc.__class__.__name__}.",
        )
        _apply_common_ground_receipt(result, shadow_receipt)
        _apply_relation_shadow_receipt(
            result,
            relation_shadow_receipt,
            requested=relation_shadow_requested,
            context_available=relation_shadow_context is not None,
        )
        return result


def load_relevant_souvenirs_for_memory_agent(
    provider: LLMProvider,
    user_message: str,
    assistant_response: str,
    recent_dialogue: str,
    user_context: str,
    user_agenda: str,
    moment_memory: str,
    current_discussion: str,
    *,
    require_success: bool = False,
) -> dict[str, str]:
    selection = select_souvenirs(
        message=f'{user_message}\n\nAssistant response:\n{assistant_response}',
        provider=provider,
        recent_dialogue=recent_dialogue,
        user_context=user_context,
        user_preferences="",
        user_agenda=user_agenda,
        moment_memory=moment_memory,
        current_discussion=current_discussion,
        require_success=require_success,
    )
    return load_named_souvenirs(selection.selected, require_success=require_success)


def load_named_souvenirs(names: list[str], *, require_success: bool = False) -> dict[str, str]:
    souvenirs = {}
    eligible = [name for name in names if not is_system_managed_souvenir(name)]
    for name in eligible[:MAX_MEMORY_AGENT_SOUVENIRS]:
        content = load_souvenir(name)
        if require_success and not content.strip():
            raise SouvenirSelectionUnavailable("Selected souvenir body unavailable")
        if content.strip():
            souvenirs[name] = content
    return souvenirs


def _build_memory_agent_messages(
    user_context: str,
    conversation_manner: str,
    current_discussion: str,
    souvenir_catalog: str,
    recent_dialogue: str,
    user_message: str,
    assistant_response: str,
    daemon_persona: str = "",
    user_agenda: str = "",
    relevant_souvenirs: dict[str, str] | None = None,
    moment_memory: str = "",
    followups_enabled: bool = False,
    pending_followups: str = "",
    facet_catalog: str = "",
    agenda_updates_enabled: bool = False,
    common_ground_observation: CommonGroundObservation | None = None,
    common_ground_register: (
        list[dict] | tuple[dict, ...] | None
    ) = None,
    loaded_souvenir_index: str = "",
    relation_shadow_enabled: bool = False,
    collection_bootstrap_enabled: bool = False,
    topology_bundle_enabled: bool = False,
    situation_notes_enabled: bool = False,
) -> list[dict]:
    souvenir_text = _format_relevant_souvenirs(relevant_souvenirs or {})
    system = """You are the MemoryAgent of Daemon Harness.

You work in a hidden turn, after the user-facing response. You never respond to the user.
Your role is to maintain short, useful, evolving Markdown memory.

You may update:
- user_context.md: a minimal interlocutor profile, main facts, stable context.
- your persona (the daemon's own voice, specific to this person): only through `persona_update` if the user explicitly asks to change its identity, character or own sensibilities.
- contexte_actif.md: background active context, topics or questions that remain alive beyond the latest exchange.
- focus_immediat.md: the immediate thread, current request and very temporary discussion state.
- thematic Markdown souvenirs: contextual memory by theme.
- capsules/index.md: a short recall map enabling readers to find souvenirs. Its descriptions and anchors are never a factual source.

Rules:
""" + SOURCE_POSTURE_PROMPT + """
- With `reported_memory_claim`, modify no file and schedule no follow-up: the exact journal must first settle the matter.
- Never put the current conversation's objective in user_context.md. Put it in focus_immediat.md.
- Put only what would remain useful across several conversations in user_context.md.
- Put only the genuinely operative setting of the period in contexte_actif.md: an ongoing project, a near deadline, a current concern or an open question that can still change a concrete response.
- The current physical and social setting belongs to this background as long as it remains true: where the user is, whom they are staying or travelling with, who is hosting them, and which transition ends this situation. Preserve it across topic changes when it can still affect a concrete decision. Do not confuse “present in the setting” with “certain participant in every activity”.
- Put what can change quickly in focus_immediat.md: the explicitly discussed current topic, explicit request, interruption, local thread state.
- Do not duplicate the same item in contexte_actif.md and focus_immediat.md.
- If an element concerns only the last 1 to 5 exchanges, it belongs in focus_immediat.md.
- If an element remains useful after a topic change but is not lasting, it may go in contexte_actif.md only if it is still active now; otherwise leave it latent in its souvenir.
- When a perishable, imminent concern mentioned by the user (for example a nearby event, approaching holidays, a deadline) remains in a souvenir, also bring its essence — and its emotional connection if one exists — into contexte_actif.md: what is near and charged for the user must live in active context, not merely sleep in a souvenir.
- conversation.md describes how the person asks the daemon to converse. It is supplied read-only to distinguish the manner of the exchange from persona and personal facts.
- MemoryAgent never proposes or applies a change to conversation.md. This document is a stable authority, maintained manually while its automatic evolution is suspended.
- A request about style, tone, format, length, follow-ups, initiative or interaction remains attested in L0. Do not transfer it to persona.md, user_context.md, agenda.md, contexte_actif.md, focus_immediat.md or a souvenir.
- NEVER create a thematic souvenir about the daemon's expected behavior (truth pact, resistance, accuracy, continuity tests, expectations about voice or relationship). A behavioral souvenir would be recalled in every scene as a pseudo-memory and distort every response. Thematic souvenirs remember the user's world, not how to speak to them.
- Modify persona.md only if the latest user message explicitly asks to change the daemon's own identity, character, temperament, tastes or sensibility.
- Never modify persona.md based on a simple mood, a joke, a factual correction or an occasional preference.
- persona.md must remain short, coherent, noncontradictory and contain no personal fact about the user.
- persona.md is a PORTRAIT, not a manual: never lists of rules, questions to ask or named conversational mechanisms — the model recites everything that can be quoted. Character is written in qualities, not instructions.
- If you modify persona.md, use only `persona_update`.
- `persona_update.should_update` is true only if the request comes from the latest user message, not from history or the assistant response.
- `persona_update.evidence` must be an exact, continuous quotation from the latest user message justifying the change.
- If you cannot quote the latest user message exactly, set `persona_update.should_update` to false.
- `persona_update.persona_md` contains a complete revised version, not a patch.
- `agenda.md` is supplied read-only to understand the present. It belongs exclusively to Temporal Curator: always return `agenda_md: null` and do not try to correct or condense it.
""" + _AGENDA_MEMORY_BOUNDARY + """
- contexte_actif.md must fit on one screen and remain temporary: no stable identity facts, lasting tastes, complete history, interpretations of personality, a work or the relationship, topics retained because they produced an intense conversation, or details belonging in a souvenir.
- contexte_actif.md must follow this Markdown format:
  `# Contexte actif`
  `## Sujets actifs`
  `## Questions ouvertes`
  `## Points de vigilance`
  `## Dernière mise à jour`
- In the three content sections, each bullet carries a single independent condition. Two facts that can change or expire separately stay in two bullets, even if they concern the same day, person or project. Never compact several independent deadlines, events or obligations into one bullet.
- If the supplied active context already contains a bullet grouping several independent conditions, `active_context_md` cannot remain null: return the complete document with these conditions separated, without removing or inventing any.
- focus_immediat.md must remain even shorter: 3 to 5 lines maximum, only the thread now.
- On every rewrite, prune the entire active context, not only the latest item. Remove elements that are no longer operative and threads that are merely interesting but now latent. Never retain a topic because it was there before or is extensively documented.
- If the supplied active context is already too long to fit on a screen, `active_context_md` cannot remain null: return its complete, cooled-down version, even if the latest turn adds no fact.
- Write a "Transition" only if the user explicitly asks to change topics to a specific theme.
- If the user asks what the assistant remembers, responds to a memory recall, validates information or corrects an extrapolation, that is not a request for ideas or a transition to that theme.
- "Ce que l'utilisateur veut" must paraphrase the user's explicit request, with no inferred objective.
- Do not turn requests about style, tone, format, length, follow-ups or interaction into factual memory; they remain in L0 while automatic evolution of conversation.md is suspended.
- Do not create or update a souvenir dedicated to communication style.
- Thematic souvenir content must come only from what the user says, not the assistant, unless the assistant proposes information that the user explicitly agrees to retain.
- In thematic souvenirs, avoid repeating general communication preferences.
- A one-off circumstance — people present at a meal, a current wish, current location, a choice considered for today, a group's temporary composition — never becomes a lasting fact without its episode or date. If it will be useful only for the next few turns, keep it in focus_immediat.md or contexte_actif.md. If it merits a souvenir, isolate the episode, phrase the fact as dated and add `valid_until` when its end is known. Never turn an occasional wish into a stable preference or past presence into current composition.
- Judge an intention's durability separately from whether its means or outcome are decided. When the user expresses an intended outcome, an ongoing practice or an unresolved situation whose history will be useful across conversations, preserve that dated intention or situation in a suitable souvenir, creating one if needed. An undecided option, missing detail or lack of a deadline does not make the established intention merely temporary. Preserve its criteria and distinguish possibilities from decisions; do not invent commitment, progress, completion or a deadline. The immediate thread is not a substitute for this durable record.
- This does not make every wish a project: a possibility merely floated, a wish limited to the present occasion, or an assistant suggestion not adopted by the user remains in short context when useful. Judge future usefulness from the user's evidence, not from a keyword or the intensity of the assistant's response.
- In focus_immediat.md, strictly distinguish what the user wants from what the assistant proposed.
- An assistant response never becomes a memory fact without explicit user confirmation.
- Mere repetition, rephrasing or confirmation of an already written lasting fact justifies no souvenir or index-line rewrite. The date when the user mentions a fact again is not a new fact. Update a souvenir only for new independent information, an explicit correction or a qualification that materially changes what must be understood later; do not turn successive rementions into a chronicle.
- Preserve factual dependencies from recent dialogue: when a new element completes an already explicitly established situation, its souvenir and index line must retain this useful recall link instead of isolating it as a contextless fact. Judge this link semantically, without deriving it from a particular phrasing.
- Do not write assistant-response content in focus_immediat.md under a form such as "Réponse donnée", "Réponse assistant" or equivalent.
- If the response seems uncertain, disputed or unconfirmed, retain only the explicit request and a neutral state such as "unconfirmed response" or "ambiguity to resolve".
- Never turn an assistant proposal/question into a user objective, decision, next step or expectation.
- Never turn a user's factual answer to an assistant question into a request to continue on that theme.
- Write "prochaine étape", "décision en attente" or equivalent only if the user explicitly requested or accepted it.
- If the user says "nothing", "thanks", "no need", or corrects the framing, remove assistant-originated next steps.
- Write only significant elements.
- Merge, correct, simplify. Do not duplicate.
- Cultivate souvenirs like a garden: plant a souvenir when a memory merits its own space, prune an overly broad souvenir, and transplant information when it is in the wrong scope.
- Memory must remain navigable: maps orient, notes remember, nearby links connect without duplicating.
- Before creating a souvenir, inspect the catalogue and existing names. If a nearby souvenir exists, update that one.
- The structural catalogue and supplied index lines serve only to discover or maintain a path. Never copy a new name, relation or detail from them into user_context.md, agenda.md, contexte_actif.md, focus_immediat.md or a souvenir. Naturally preserve these files' existing content; only the latest user exchange, exact history and explicitly loaded souvenir bodies can ground an addition or correction.
- Do not create several souvenirs for the same subtheme.
- A souvenir's name and title define its scope.
- Every piece of information in a souvenir must clearly belong to that scope.
- New thematic souvenir names must follow the hierarchical format `theme/<domaine>/<sujet>` or `theme/<domaine>/<sous_domaine>/<sujet>`.
- Old flat names `theme/<domaine>__<sujet_precis>` remain readable as a legacy format, but avoid creating new ones.
- Maximum depth is 3 levels under `theme`: domain, optional subdomain, specific subject. Never create a deeper path.
- Use simple, stable domains: `famille`, `loisirs`, `preferences`, `quotidien`, `sante`, `style`, `travail`, `projet`.
- Technical souvenir names stay in the user's main language. Do not translate domains, subdomains or subjects according to the latest message's occasional language.
- The name must be explicit without reading the content: avoid ambiguous names such as `travail_ia` or `jeux_avec_enfant`.
- The name, Sujet and Portée describe the actual entirety of the souvenir's facts, not only their most salient theme. If no honest subtheme covers the whole, choose a more neutral parent frame or split it.
- Before returning a souvenir, check every fact against `Sujet`, `Portée` and `Hors portée`. No present fact may be declared out of scope. If new information genuinely belongs in the same note but honestly broadens its angle, rewrite `Portée` and `Hors portée`; otherwise move it to an appropriate souvenir.
- A souvenir is either a map or a note.
- A map souvenir serves navigation. It contains few or no detailed facts. It mainly uses:
  `## Résumé`
  `## Sous-capsules`
  `## Liens proches`
- A note souvenir remembers precise facts. It mainly uses:
  `## Faits`
  and optionally `## Liens proches` if a cross-cutting bridge is truly useful.
- Every thematic souvenir must follow this minimal Markdown format:
  discovery YAML frontmatter with `subjects` (people, groups, projects or other genuinely concerned subjects) and `facets` (free semantic angles, without a closed taxonomy — reuse a label from the existing-facet catalogue supplied below when the meaning matches; create a new label only for a genuinely new angle),
  `# Capsule: theme/<domaine>/<sujet>`
  `Sujet: short sentence that describes the souvenir exactly.`
  `Portée: what this souvenir is allowed to contain.`
  `Hors portée: what belongs elsewhere.`
  `valid_until: YYYY-MM-DD` (optional — only if the souvenir has a validity end date)
  then either `## Résumé` / `## Sous-capsules` / `## Liens proches`, or `## Faits`.
- Perishability: if information has a known validity end (holidays, absence, a temporary situation “until …”), put it in a dedicated souvenir with a `valid_until: YYYY-MM-DD` line (end date, resolved using today's date supplied below). Add `valid_until:` only for genuinely perishable facts, never for a lasting fact. Once the date has passed, dreaming will archive the souvenir.
- A general thematic souvenir may retain an occasion's history only if its temporal anchor is explicit in each relevant fact. Never write “will be there”, “tonight”, “today” or a present wish without a date or episode: outside its original turn, that wording would manufacture a false present.
- If you modify an old souvenir that does not follow this format, rewrite it in this format without information loss.
- `capsules/index.md` entries must follow exactly `- theme/<chemin_souvenir>: short description`, without a Markdown heading or `#`.
- `capsules/index.md` entries are not mere titles: they must contain a few concrete recall anchors when available.
- A concrete anchor can be a proper name, object, brand, work, place, relation or cultural reference useful for finding the souvenir.
- When the user uses a recurring personal word or expression for something (for example “libération” for the end of a busy period, a nickname, a favorite expression), add that word verbatim as an anchor in the relevant souvenir's index line: these are the words they will use to allude to it later.
- Example of a useful entry: `- theme/style/chaussures: Chaussures et style vestimentaire de l'utilisateur — baskets rétro, cadeau familial, référence musicale`.
- If information touches several domains, place it in the souvenir whose primary scope best matches, and leave enough anchors in the index for other paths to find it.
- Do not leave information in an out-of-scope souvenir merely because it has a secondary connection. For example, shoes linked to a musical reference may also require a `theme/style/chaussures` souvenir, not only a music souvenir.
- The `## Sous-capsules` and `## Liens proches` sections must use full souvenir names and a short description.
- A note souvenir must have 0 to 3 nearby links maximum.
- When creating a targeted note from a broader context represented by an existing, actually loaded souvenir, add a nearby link to that souvenir if it truly forms the note's natural context. Never invent a link to fill the graph, but do not leave a new note isolated when a direct, useful link is already established by the supplied facts.
- Do not copy a subsouvenir's detailed facts into its parent souvenir: the parent orients, the child remembers.
- If new or corrected information falls outside an existing souvenir's scope, place it in a more appropriate souvenir.
- If no appropriate souvenir exists, create a short, targeted souvenir.
- Do not keep information in a souvenir merely because it was there before.
- When modifying an existing souvenir supplied in the prompt, return its complete revised version.
- In an existing souvenir, preserve all already present information by default.
- You may rephrase or move information for clarity, but without changing its meaning.
- You may add new information only if it comes from the latest user exchange or explicit user validation.
- You may modify existing information only if the latest exchange explicitly corrects it.
- You may remove information only if the latest exchange explicitly contradicts it, makes it obsolete, or the user asks to forget it.
- If attribution is uncertain, mark it as "to be confirmed" instead of deleting it.
- When several people are involved, clearly distinguish: user, child/loved one, shared, assistant, to be confirmed.
- Never turn an assistant recall failure into a memory fact such as “no memory”, “no confirmed title” or equivalent.
- Never turn the absence of information in the loaded relevant souvenirs into a memory fact such as “not specified”, “unavailable”, “unknown” or equivalent.
- If information seems missing but could exist elsewhere in memory, instead write a temporary vigilance point such as "to check in the souvenirs", without asserting that the information does not exist.
- A souvenir must remain short and targeted: indicative target 1200 characters, attention threshold 1600 characters.
- Never compact a souvenir by deleting or summarizing existing information to save space.
- If a souvenir becomes too long or mixes several memory questions, create one or more more-targeted subsouvenirs and update the index.
- When splitting, every existing piece of information must be preserved without loss in an appropriate souvenir, unless explicitly contradicted or requested to be forgotten.
- Never replace the index with an impoverished version: preserve existing entries and add/adjust only what is necessary.
- For each `souvenir_updates` item, also provide `index_line`: a complete, compact line in the format `- theme/<chemin>: description — anchors`. Judge anchors against the COMPLETE souvenir, not only the latest added fact. Preserve old recall handles that are still discriminating and condense lists (names, works, places, expressions) instead of copying entire sentences. Aim for fewer than 300 characters. This line is a discovery map, never a second souvenir.
- Files must remain short and readable.
- If nothing merits retention, return only null/empty fields.
- Final check before JSON: verify lasting novelty, scope coherence for every fact, any natural link of a new souvenir and the agenda's strictly short temporal scope. If the latest turn only repeats an already written fact, leave souvenir, index and agenda unchanged; only the immediate thread may possibly evolve.
- Respond in strict JSON, without Markdown outside Markdown content.

Schema:
{
  "reason": "short reason",
  "source_posture": "direct_fact|reported_memory_claim|none",
  "user_context_md": null,
  "persona_update": {
    "should_update": false,
    "evidence": "",
    "persona_md": null
  },
  "agenda_md": null,
  "active_context_md": null,
  "immediate_focus_md": null,
  "capsule_index_md": null,
  "capsule_updates": [
    {"name":"theme/sous_theme_ou_nom_simple","content_md":"# ...","index_line":"- theme/sous_theme_ou_nom_simple: description — compact anchors"}
  ]
}
"""
    system += "\n\n" + MEMORY_DATE_RULES
    if situation_notes_enabled:
        system += "\n" + _SITUATION_NOTE_RULES + "\n"
    if followups_enabled:
        system += """
Proactive follow-up (optional, RARE):
- You may propose that the daemon return to the user later on its own (checking in, encouraging), as a caring friend would.
- Emit `followup.should_schedule=true` only if EVERYTHING is present: a precise, dated event or state that truly matters to the user, an expressed emotional stake or meaningful milestone, and a natural “after” moment when a question would make sense.
- Never for logistical/transactional matters or an already closed topic.
- IMPORTANT — SPONTANEOUS initiative only: this follow-up serves when YOU judge, on your own, that returning to the user would be worthwhile, although they asked for NOTHING. If the user themselves requests a follow-up, a check-in or a reminder (“feel free to ask me again”, “check in on me tonight”, “remind me…”), do NOT emit a followup: these explicit requests are handled elsewhere (the agent's reminder tool).
- When in doubt, do not emit a follow-up.
- `followup.evidence` must be an exact, continuous quotation from the latest user message. Without an exact quotation, set `should_schedule=false`.
- `followup.fire_at`: future timestamp "YYYY-MM-DDTHH:MM" (waking hours, after the relevant moment), resolved using today's date.
- `followup.topic`: short, grounded description. `followup.angle`: "check-in" or "encouragement".

Then add this key to the JSON:
  "followup": {"should_schedule": false, "fire_at": "", "topic": "", "angle": "check-in", "evidence": ""}

Follow-up preference (optional): judge — by meaning and tone, not specific words — whether
the user no longer wants you to return to them on your own (set `action: "disable"`), or
accepts/requests follow-ups again after declining (`action: "enable"`). Otherwise `action: null`.
`evidence` must be an exact quotation from the latest user message justifying it.
  "followups_pref": {"action": null, "evidence": ""}
"""
        if pending_followups:
            system += (
                f"\nFollow-ups already scheduled for this user (DO NOT duplicate):\n{pending_followups}\nIf your follow-up would duplicate one of them — same concern or same moment — set `should_schedule: false`.\n"
            )
    if common_ground_observation is not None:
        system += """

Common Ground observation (shadow experiment):
- This observation changes no memory visible to the voice. It serves to propose at most one auditable act.
- For a BIRTH, judge only the potentially observable effect of assistant response T-1 in user reaction T supplied in the dedicated window.
- Current assistant response T comes after that reaction: it can never prove its own effect and must influence no candidate field.
- Silence proves nothing. A merely polite response, minimal agreement, lexical repetition or pleasant turn is insufficient to establish common ground.
- `shared_language` requires either an expression genuinely reused or wording in which the user explicitly names or refines an image or distinction proposed at T-1. Do not confuse lexical proximity, repetition of words and private vocabulary.
- `shared_expression` quotes the most precise shared wording exactly as it appears in user reaction T. It may complete the seed proposed at T-1; in that case, `movement` must soberly describe this co-construction and `prior_assistant_evidence` must quote the exact seed.
- `relational_precedent` requires a prior state, a genuinely attempted move, then an effect readable in the next reaction. It describes an episode and its limits, never a conversational rule.
- Propose no technique, general preference or future instruction. When in doubt, return null.
- `prior_assistant_evidence` and `current_user_evidence` are exact, continuous, nonempty quotations from their corresponding turns.
- `movement`, `observed_effect` and `limits` are required for every candidate. `before` is also required for a relational precedent.
- If the supplied candidate register already contains the same shared ground, do not create a duplicate: instead judge consolidation.

Add these two keys to the strict JSON. At most ONE may be non-null:
  "common_ground_candidate": null,
  "common_ground_update": null

Or, for one sufficiently grounded candidate:
  "common_ground_candidate": {
    "kind": "shared_language|relational_precedent",
    "prior_assistant_evidence": "exact quotation from T-1",
    "current_user_evidence": "exact quotation from T",
    "before": "prior state, empty only for shared_language",
    "movement": "move attempted at T-1",
    "observed_effect": "observable effect at T, without extrapolation",
    "shared_expression": "exact expression, empty for relational_precedent",
    "shared_meaning": "meaning built together, empty for relational_precedent",
    "limits": "what this episode does not allow one to generalize",
    "reason": "short reason"
  }
"""
        if common_ground_register:
            system += """

Common Ground consolidation (same shadow experiment):
- Compare user reaction T with the candidates: `reinforce` for intentional reuse with the same meaning, `revise` for an explicit rephrasing of the same lineage, `retire` for explicit disavowal.
- If T clearly replaces a candidate with a new expression, `revise` takes precedence over a new birth: do not create two lineages for the same shared ground.
- A lexical occurrence, title, third-party quotation, question, ambiguous negation or different meaning consolidates nothing. Use by the assistant proves nothing either.
- Only T and the sourced candidate constitute evidence. Quote T exactly in `current_user_evidence` and use an exact `target_candidate_id`.
- `observed_use`, `limits` and `reason` are required. For `revise`, also quote the exact new expression and its meaning; otherwise leave those fields empty.
- When in doubt, return null.

Or, for one sufficiently grounded consolidation act:
  "common_ground_update": {
    "action": "reinforce|revise|retire",
    "target_candidate_id": "exact register identifier",
    "current_user_evidence": "exact quotation from T",
    "shared_expression": "exact new expression, only for revise",
    "shared_meaning": "revised meaning, only for revise",
    "observed_use": "what T actually does with the shared language",
    "limits": "what this use does not allow one to infer",
    "reason": "short reason"
  }
"""
    if relation_shadow_enabled and topology_bundle_enabled:
        system += """

Membership topology (shadow, optional):
- You may add one `topology_updates` in ONE complete `souvenir_update` preserving the grouping fact. The carrier must retain complete `name`, `content_md` and `index_line`: topology never replaces a necessary mutation.
- The carrier is not necessarily a source or target. Endpoints are explicit and already exist or appear in `souvenir_updates`.
- Choose as center the souvenir whose scope precisely describes the whole or episode, never a broader parent that is merely adjacent.
- `relations` contains at most three `member_of`, only if the LATEST message establishes membership. Each item supplies `source`, `target`, `role`, `epistemic: established` and an exact `evidence` quotation. A possibility, proximity, homonym, assistant suggestion or catalogue is insufficient. `remove` requires an explicit correction.
- `collections` contains at most three `declare_open` declarations, with `target`, `epistemic: established` and the same exact evidence. Declare nothing for an already `open|closed` center. Each declaration must be targeted by at least one `upsert member_of` from the same bundle.
- Form: `"topology_updates":{"collections":[{"action":"declare_open","target":"theme/domaine/centre","epistemic":"established","evidence":"citation exacte"}],"relations":[{"action":"upsert|remove","kind":"member_of","source":"theme/domaine/membre","target":"theme/domaine/centre","role":"role court","epistemic":"established","evidence":"citation exacte"}]}`.
- When in doubt, omit `topology_updates`. Do not simultaneously use the old nested `relation_updates` or `collection_update`.
"""
    elif relation_shadow_enabled:
        system += """

Membership relations (shadow, optional):
- Add `relation_updates` only in the member's complete `souvenir_update`; this subfield never replaces `content_md`, `index_line` or another necessary mutation.
- At most three `member_of`, only if the LATEST message establishes membership in a precise episode or collection. `epistemic` then equals `established`. A possibility, even described in `role`, remains without a relation. Proximity, homonym, assistant suggestion or catalogue is insufficient; abstention is normal.
- The carrying souvenir is implicitly `source`. `target` is the center and already exists or appears in `souvenir_updates`. `remove` requires an explicit correction.
- `role` remains short. `evidence` quotes the latest message exactly and continuously; the runtime attaches the L0 reference.
- Form: `"relation_updates": [{"action":"upsert|remove","kind":"member_of","epistemic":"established","target":"theme/domaine/centre","role":"role court","evidence":"citation exacte"}]`. Otherwise omit the subfield or return an empty list.
"""
    if collection_bootstrap_enabled and not topology_bundle_enabled:
        system += """

Declaration of a collection center (shadow, optional):
- Only if the LATEST message establishes both a precise set or episode and the certain membership of at least one souvenir in that set may you declare its center souvenir `open`.
- Add `collection_update` only in the center's complete `souvenir_update`; this subfield never replaces `content_md`, `index_line` or another necessary mutation. `relation_updates` remain in the members' complete updates.
- Declare nothing for a possibility, mere proximity, homonym, solitary fact, center already marked `collection.coverage: open|closed`, correction removing a member, or reported-memory verification request.
- Sole form: `"collection_update":{"action":"declare_open","epistemic":"established","evidence":"citation exacte"}`. `evidence` quotes the latest message exactly and continuously; the runtime attaches the L0 reference.
- The declaration is valid only if at least one `upsert member_of` in the same result targets this center. Otherwise omit the subfield.
"""
    system = context_page_prompt(system)
    user = f"""# Current state

Today's date: {_memory_agent_today()}

## user_context.md
{user_context or "Empty file."}

## conversation.md — manner of conversing
{conversation_manner or "Empty file."}

## memory/core/persona.md — daemon's own voice
{daemon_persona or "Empty file."}

## agenda.md — read-only temporal projection
{user_agenda or "Empty file."}

## contexte_actif.md — background active context
{moment_memory or "Empty file."}

## focus_immediat.md — immediate thread
{current_discussion or "Empty file."}

## Structural capsule catalogue — orientation only
{souvenir_catalog or "Empty catalogue."}

## Index lines of only the exact loaded capsules — maintenance only
{loaded_souvenir_index or "No index line loaded."}

## Existing facet catalogue (for capsule frontmatter)
{facet_catalog or "No existing facet."}

## Relevant existing capsules
{souvenir_text}

## Recent history
{recent_dialogue or "No recent history."}

# Latest exchange

User:
{user_message}

Assistant:
{assistant_response}
"""
    if common_ground_observation is not None:
        user += "\n" + render_common_ground_observation(
            common_ground_observation
        )
        if common_ground_register:
            user += "\n" + render_common_ground_register(
                common_ground_register
            )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _memory_agent_today() -> str:
    forced = os.getenv("DAEMON_CORE_EVAL_NOW", "").strip()
    if forced:
        try:
            return date.fromisoformat(forced[:10]).isoformat()
        except ValueError:
            pass
    return date.today().isoformat()


def _format_relevant_souvenirs(souvenirs: dict[str, str]) -> str:
    if not souvenirs:
        return 'No relevant existing souvenir loaded.'
    formatted = []
    for name, content in souvenirs.items():
        text = content.strip()
        if not text:
            continue
        length = len(text)
        note = f'Current length: {length} characters. Target: {TARGET_SOUVENIR_CHARS}, attention threshold: {WARN_SOUVENIR_CHARS}.'
        if length >= WARN_SOUVENIR_CHARS:
            note += ' This souvenir approaches or exceeds the threshold; if you modify it, prefer a targeted split without information loss.'
        formatted.append(f"### {name}\n{note}\n\n{text}")
    return "\n\n".join(formatted)


def _guard_reported_memory_claim(data: dict) -> dict:
    """Make a verification request read-only until exact history settles it."""

    if data.get("source_posture") != "reported_memory_claim":
        return data
    guarded = dict(data)
    guarded.update(
        {
            "user_context_md": None,
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
            "relation_updates": [],
            "collection_updates": [],
            "followup": {"should_schedule": False},
            "followups_pref": {"action": None, "evidence": ""},
            "common_ground_candidate": None,
            "common_ground_update": None,
        }
    )
    reason = str(guarded.get("reason", "") or "").strip()
    boundary = "Demande de vérification conservée dans L0 sans mutation durable."
    guarded["reason"] = " ".join(part for part in (reason, boundary) if part)
    return guarded


def _apply_memory_agent_result(
    user_id: str,
    data: dict,
    user_message: str = "",
    provider: LLMProvider | None = None,
    agenda_updates_enabled: bool = False,
    derive_relation_removals: bool = False,
    performative_relation_retractions: bool = False,
    relocation_context: RelocationContext | None = None,
) -> MemoryAgentResult:
    data = _guard_reported_memory_claim(data)
    result = MemoryAgentResult(
        source_posture=str(data.get("source_posture", "") or "").strip(),
        reason=str(data.get("reason", "") or ""),
    )
    user_context = data.get("user_context_md")
    if isinstance(user_context, str) and user_context.strip():
        result.updated_user_context = _write_markdown_if_changed(
            result=result,
            path=user_context_path(user_id),
            current=load_user_context(user_id),
            proposed=user_context,
            writer=lambda content: write_user_context(user_id, content),
        )

    persona = _persona_update_content(data.get("persona_update"), user_message)
    if persona:
        result.updated_persona = _write_markdown_if_changed(
            result=result,
            path=user_daemon_persona_path(user_id),
            current=load_daemon_persona(user_id),
            proposed=persona,
            writer=lambda content: write_daemon_persona(content, user_id),
        )

    active_context_md = data.get("active_context_md") or data.get("moment_md")
    if isinstance(active_context_md, str) and active_context_md.strip():
        result.updated_active_context = _write_markdown_if_changed(
            result=result,
            path=active_context_path(user_id),
            current=load_active_context(user_id),
            proposed=active_context_md,
            writer=lambda content: write_active_context(user_id, content),
        )

    immediate_focus_md = data.get("immediate_focus_md") or data.get("current_discussion_md")
    if isinstance(immediate_focus_md, str) and immediate_focus_md.strip():
        immediate_focus_md = _clean_immediate_focus(immediate_focus_md)
        result.updated_immediate_focus = _write_markdown_if_changed(
            result=result,
            path=immediate_focus_path(user_id),
            current=load_immediate_focus(user_id),
            proposed=immediate_focus_md,
            writer=lambda content: write_immediate_focus(user_id, content),
        )

    souvenir_index = data.get("capsule_index_md")
    if isinstance(souvenir_index, str) and souvenir_index.strip() and vault_root(MEMORY_ROOT) is None:
        path = MEMORY_ROOT / "capsules" / "index.md"
        _record_path(result.changed_paths if _merge_index_safely(path, souvenir_index) else result.unchanged_paths, path)
        result.updated_index = _memory_path(path) in result.changed_paths

    updates = data.get("capsule_updates", [])
    if isinstance(updates, list):
        for update in updates:
            if not isinstance(update, dict):
                continue
            name = _sanitize_souvenir_name(str(update.get("name", "")))
            content = update.get("content_md")
            if (
                not name
                or name in RESERVED_SOUVENIR_NAMES
                or is_system_managed_souvenir(name)
                or not isinstance(content, str)
                or not content.strip()
            ):
                continue
            try:
                relocation = None
                if update.get("relocation") is not None:
                    relocation = validate_proposal(
                        MEMORY_ROOT, name, update["relocation"], relocation_context, user_message)
                path = resolve_note(MEMORY_ROOT, name, create=True,
                                    storage_path=update.get("storage_path"))
            except (OSError, ValueError) as exc:
                if isinstance(exc, OSError):
                    from daemon_core.maintenance_errors import reraise_memory_update_failure

                    reraise_memory_update_failure(exc)
                result.skipped_souvenirs.append(name)
                result.rejected_paths.append("capsules/" + name + ".md")
                result.reason = f"{result.reason} Note {name} refused: {exc}.".strip()
                continue
            status, final_content, failure, relation_removals = _write_souvenir_update_safely(
                user_id=user_id,
                path=path,
                name=name,
                proposed_content=content,
                user_message=user_message,
                provider=provider,
                derive_relation_removals=derive_relation_removals,
                performative_relation_retractions=(
                    performative_relation_retractions
                ),
                expected_snapshot=relocation.snapshot if relocation else None,
            )
            if status == "rejected":
                result.skipped_souvenirs.append(name)
                _record_path(result.rejected_paths, path)
                if failure:
                    result.reason = f"{result.reason} Souvenir {name} refusée: {failure}.".strip()
                continue
            result.authorized_relation_removals.extend(relation_removals)
            if relocation is not None:
                expected = ((final_content + "\n").replace("\n", os.linesep).encode("utf-8") if status == "changed"
                            else relocation.snapshot.content)
                try:
                    move_status = publish_relocation(MEMORY_ROOT, relocation, expected)
                    path = resolve_note(MEMORY_ROOT, name)
                    if move_status == "applied":
                        result.relocated_souvenirs.append(name)
                        status = "changed"
                    else:
                        result.reason += f" Folder transition cancelled for {name}."
                except (OSError, ValueError) as exc:
                    if isinstance(exc, OSError):
                        from daemon_core.maintenance_errors import reraise_memory_update_failure
                        reraise_memory_update_failure(exc)
                    result.reason += f" Folder transition refused for {name}: {exc}."
            logical_path = "capsules/" + name + ".md"
            if status == "unchanged":
                if logical_path not in result.unchanged_paths:
                    result.unchanged_paths.append(logical_path)
            else:
                result.updated_souvenirs.append(name)
                if logical_path not in result.changed_paths:
                    result.changed_paths.append(logical_path)
            index_path = MEMORY_ROOT / "capsules" / "index.md"
            index_line = validated_souvenir_index_line(name, update.get("index_line"))
            existing_index = (
                index_path.read_text(encoding="utf-8") if index_path.exists() else ""
            )
            fallback = existing_souvenir_index_line(existing_index, name)
            if _merge_index_safely(
                index_path,
                index_line or fallback or _souvenir_index_entry(name, final_content),
            ):
                _record_path(result.changed_paths, index_path)
                result.updated_index = True
            else:
                _record_path(result.unchanged_paths, index_path)

    return result


def _write_souvenir_update_safely(
    user_id: str,
    path: Path,
    name: str,
    proposed_content: str,
    user_message: str,
    provider: LLMProvider | None,
    derive_relation_removals: bool = False,
    performative_relation_retractions: bool = False,
    expected_snapshot: NoteSnapshot | None = None,
) -> tuple[str, str, str, tuple[dict, ...]]:
    vault = vault_root(MEMORY_ROOT)
    if vault is not None:
        try:
            # Resolve again: a queued writer must never use a pre-rename path.
            path = resolve_note(MEMORY_ROOT, name, create=True,
                                storage_path=path.relative_to(vault).as_posix())
        except (OSError, ValueError) as exc:
            return "rejected", "", str(exc), ()
    with memory_file_lock(path):
        if expected_snapshot is not None:
            try:
                ensure_snapshot(MEMORY_ROOT, expected_snapshot)
            except (OSError, ValueError) as exc:
                return "rejected", "", str(exc), ()
        initial_bytes = path.read_bytes() if path.exists() else None
        current = initial_bytes.decode("utf-8").strip() if initial_bytes is not None else ""
        final_content = proposed_content.strip()
        if current == final_content:
            return "unchanged", current, "", ()
        if current:
            if provider is None:
                return (
                    "rejected",
                    "",
                    "fusion LLM indisponible pour une souvenir existante",
                    (),
                )
            try:
                merged = _merge_existing_souvenir(
                    provider=provider,
                    name=name,
                    current=current,
                    proposed=final_content,
                    user_message=user_message,
                    user_agenda=load_user_agenda(user_id),
                    derive_relation_removals=derive_relation_removals,
                    performative_relation_retractions=(
                        performative_relation_retractions
                    ),
                    situation_notes_enabled=memory_situation_notes_enabled(user_id),
                )
                final_content = merged.content
            except Exception as exc:
                # A rejected semantic merge remains a rejection. An unavailable
                # provider or storage must keep the durable source turn pending.
                if not isinstance(exc, ValueError):
                    from daemon_core.maintenance_errors import reraise_memory_update_failure

                    reraise_memory_update_failure(exc)
                logger.warning("Mise à jour souvenir refusée | name=%s | error=%s", name, exc)
                return "rejected", "", str(exc), ()
        else:
            merged = SouvenirMergeResult(content=final_content)
        try:
            final_content = normalize_memory_dates(final_content, current=current)
            final_content = prepare_souvenir_format(final_content, name)
            if vault is not None:
                final_content = with_identity(final_content, name, current=current)
            parse_souvenir_frontmatter(final_content)
            if structure_enabled():
                validate_context_page(final_content)
        except (ValueError, yaml.YAMLError) as exc:
            logger.warning(
                "Écriture souvenir refusée : note invalide | "
                "name=%s | error=%s",
                name,
                exc.__class__.__name__,
            )
            return (
                "rejected",
                "",
                (f"frontmatter YAML invalide ({exc.__class__.__name__})"
                 if isinstance(exc, yaml.YAMLError)
                 else f"invalid memory note ({exc.__class__.__name__}): {exc}"),
                (),
            )
        if current == final_content:
            return "unchanged", current, "", merged.relation_removals
        if vault is not None:
            try:
                resolved = resolve_note(MEMORY_ROOT, name, create=initial_bytes is None,
                                        storage_path=path.relative_to(vault).as_posix())
                if resolved != path:
                    return "rejected", "", "note moved during merge; reread latest location", ()
            except (OSError, ValueError) as exc:
                return "rejected", "", str(exc), ()
        if obsidian_format_enabled() or vault is not None:
            latest_bytes = path.read_bytes() if path.exists() else None
            if latest_bytes != initial_bytes:
                return (
                    "rejected", "", "souvenir changed externally during merge; retry from latest source", (),
                )
        atomic_write_text(path, final_content + "\n")
        return "changed", final_content, "", merged.relation_removals


def _apply_discovery_additions(content: str, raw: object) -> str:
    additions = SouvenirMetadata.from_raw(raw)
    if additions.is_empty:
        return content
    current = parse_souvenir_metadata(content)
    metadata = SouvenirMetadata(
        subjects=list(dict.fromkeys([*current.subjects, *additions.subjects])),
        facets=list(dict.fromkeys([*current.facets, *additions.facets])),
    )
    return with_souvenir_metadata(content, metadata)


def _merge_existing_souvenir(
    provider: LLMProvider,
    name: str,
    current: str,
    proposed: str,
    user_message: str,
    derive_relation_removals: bool = False,
    performative_relation_retractions: bool = False,
    situation_notes_enabled: bool = False,
    user_agenda: str = "",
) -> SouvenirMergeResult:
    current = prepare_souvenir_format(current, name)
    proposed = prepare_souvenir_format(proposed, name)
    eligible_relation_retractions = (
        _eligible_member_of_retractions(
            current=current,
            proposed=proposed,
        )
        if performative_relation_retractions and derive_relation_removals
        else ()
    )
    messages = _build_souvenir_merge_messages(
        name=name,
        current=current,
        proposed=proposed,
        user_message=user_message,
        protect_relations=derive_relation_removals,
        performative_relation_retractions=performative_relation_retractions,
        eligible_relation_retractions=eligible_relation_retractions,
        situation_notes_enabled=situation_notes_enabled,
        user_agenda=user_agenda,
    )
    log_llm_prompt(prompt_logger, "FUSION SOUVENIR SÉCURISÉE", messages)
    raw = provider.complete(messages=messages, temperature=0.0)
    data = json.loads(_strip_json_fence(raw))
    content = data.get("content_md")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("fusion vide ou invalide")
    content = preserve_existing_frontmatter(current, content)
    content = _apply_discovery_additions(
        content,
        _discovery_additions_from_checks(data.get("discovery_checks")),
    )
    content = prepare_souvenir_format(content, name)
    applied_relation_retractions = (
        _authorized_performative_relation_retractions(
            data.get("existing_relation_decisions"),
            eligible_lines=eligible_relation_retractions,
            user_message=user_message,
        )
        if performative_relation_retractions
        else {}
    )
    if applied_relation_retractions:
        content = _apply_performative_relation_removals(
            content,
            set(applied_relation_retractions),
        )
        logger.info(
            "Retraits performatifs relations | name=%s | count=%d",
            name,
            len(applied_relation_retractions),
        )

    protected = set(
        _protected_souvenir_lines(
            current,
            include_relations=derive_relation_removals,
        )
    )
    merged_lines = set(
        _protected_souvenir_lines(
            content,
            include_relations=derive_relation_removals,
        )
    )
    missing = protected - merged_lines
    ordinary_authorized = _authorized_removed_line_evidence(
        data.get("removed_existing_lines"),
        user_message,
    )
    if performative_relation_retractions:
        ordinary_authorized = {
            line: evidence
            for line, evidence in ordinary_authorized.items()
            if not line.startswith(f"- {MEMBERSHIP_RELATION} ")
        }
    authorized = {**ordinary_authorized, **applied_relation_retractions}
    unauthorized = sorted(missing - set(authorized))
    if unauthorized:
        preview = " | ".join(unauthorized[:3])
        raise ValueError(f"perte d'information non autorisée ({preview})")
    relation_removals = (
        _authorized_relation_removals(
            name=name,
            current=current,
            merged=content,
            evidence_by_line=authorized,
        )
        if derive_relation_removals
        else ()
    )
    return SouvenirMergeResult(
        content=content.strip(),
        relation_removals=relation_removals,
        applied_relation_retractions=tuple(
            {
                "line": line,
                "action": "remove",
                "evidence": evidence,
            }
            for line, evidence in applied_relation_retractions.items()
        ),
    )


def _build_souvenir_merge_messages(
    name: str,
    current: str,
    proposed: str,
    user_message: str,
    *,
    protect_relations: bool = False,
    performative_relation_retractions: bool = False,
    eligible_relation_retractions: tuple[str, ...] = (),
    situation_notes_enabled: bool = False,
    user_agenda: str = "",
) -> list[dict]:
    system = """You are the integrity guardian of personal Markdown memory.

You receive the CURRENT version of a souvenir and a PROPOSAL produced by another judge.
Return a complete merged version. The current version is the source of truth:
- preserve all its existing structured bullets verbatim;
- add only what the proposal brings that is grounded in the latest user message;
- add no rephrasing, new mention date or confirmation of an already present fact; if the proposal brings no independent fact, return the current version unchanged;
- never summarize, compact or rephrase an existing bullet;
- removal or correction is allowed only if the latest user message explicitly justifies it.
- in the final document, `Sujet`, `Portée` and `Hors portée` must honestly describe all bullets: no retained or added fact may be declared out of scope;
- preserve existing nearby links and any genuinely grounded new link connecting the note to its natural context, without creating an artificial link.
"""
    system += "\n" + _AGENDA_MEMORY_BOUNDARY
    if protect_relations:
        system += """
- bullets in `## Relations` are structural data: an explicit correction
  may remove one, but never a mere omission.
"""
    system += "\n" + (
        _SITUATION_DISCOVERY_AUDIT_RULES
        if situation_notes_enabled
        else _SOUVENIR_DISCOVERY_AUDIT_RULES
    )
    if performative_relation_retractions:
        system += """

The runtime gives you the exhaustive list of old `member_of` relations absent from the proposal. For each, return exactly one decision in `existing_relation_decisions`:
- `preserve` if the latest message does not explicitly justify its removal; `evidence` is then an empty string;
- `remove` only with an exact, continuous quotation from the latest user message correcting this membership.

This decision will be executed by the runtime. An unlisted line is never eligible for this register. Ordinary facts are never eligible: their correction continues to use `removed_existing_lines` with exact evidence.

Respond in strict JSON:
{"content_md":"complete Markdown content","discovery_checks":[{"fact":"- exact bullet","other_endpoint":null,"inverse_need":null,"covered_by":[],"add_subject":null,"add_facet":null}],"removed_existing_lines":[{"line":"- exact removed factual bullet","evidence":"exact quotation"}],"existing_relation_decisions":[{"line":"- exact supplied relation","action":"preserve|remove","evidence":"exact quotation or empty string"}],"reason":"short reason"}
"""
    else:
        system += """

For every existing bullet actually removed, fill `removed_existing_lines` with the exact line and an exact, continuous quotation from the latest user message. Without this evidence, preserve the line.

Respond in strict JSON:
{"content_md":"complete Markdown content","discovery_checks":[{"fact":"- exact bullet","other_endpoint":null,"inverse_need":null,"covered_by":[],"add_subject":null,"add_facet":null}],"removed_existing_lines":[{"line":"- exact removed line","evidence":"exact quotation"}],"reason":"short reason"}
"""
    relation_retraction_section = ""
    if performative_relation_retractions:
        relation_retraction_section = (
            '\n\n## member_of relations absent from the proposal\n'
            + json.dumps(
                list(eligible_relation_retractions),
                ensure_ascii=False,
                indent=2,
            )
        )
    system = context_page_prompt(system, merge=True) + "\n\n" + MEMORY_DATE_RULES
    user = f"""Souvenir: {name}

## Current version
{current}

## Proposal
{proposed}

## Latest user message
{user_message}

## agenda.md — read-only temporal projection
{user_agenda or "(empty)"}
{relation_retraction_section}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _discovery_additions_from_checks(raw: object) -> dict[str, list[str]]:
    subjects: list[str] = []
    facets: list[str] = []
    if not isinstance(raw, list):
        return {"subjects": subjects, "facets": facets}
    for item in raw:
        if not isinstance(item, dict):
            continue
        endpoint = item.get("other_endpoint")
        inverse_need = item.get("inverse_need")
        if not isinstance(endpoint, str) or not endpoint.strip():
            continue
        if not isinstance(inverse_need, str) or not inverse_need.strip():
            continue
        subject = item.get("add_subject")
        facet = item.get("add_facet")
        if isinstance(subject, str) and subject.strip():
            subjects.append(subject)
        if isinstance(facet, str) and facet.strip():
            facets.append(facet)
    return {"subjects": subjects, "facets": facets}


def _protected_souvenir_lines(
    content: str,
    *,
    include_relations: bool = False,
) -> list[str]:
    protected_sections = {
        "## faits",
        "## résumé",
        "## sous-capsules",
        "## liens proches",
        "## scope",
        "## last confirmed state",
        "## history",
        "## decisions",
        "## still unknown",
        "## links",
        "## sources",
    }
    if include_relations:
        protected_sections.add("## relations")
    current_section = ""
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped.casefold()
            continue
        if current_section not in protected_sections or not stripped.startswith("- "):
            continue
        if current_section == "## relations" and line != line.lstrip():
            continue
        if stripped:
            lines.append(stripped)
    return lines


def _eligible_member_of_retractions(
    *,
    current: str,
    proposed: str,
) -> tuple[str, ...]:
    relation_prefix = f"- {MEMBERSHIP_RELATION} "
    proposed_relations = {
        line
        for line in _protected_souvenir_lines(
            proposed,
            include_relations=True,
        )
        if line.startswith(relation_prefix)
    }
    return tuple(
        dict.fromkeys(
            line
            for line in _protected_souvenir_lines(
                current,
                include_relations=True,
            )
            if line.startswith(relation_prefix)
            and line not in proposed_relations
        )
    )


def _authorized_performative_relation_retractions(
    raw: object,
    *,
    eligible_lines: tuple[str, ...],
    user_message: str,
) -> dict[str, str]:
    if not isinstance(raw, list):
        return {}
    eligible = set(eligible_lines)
    decisions: dict[str, list[tuple[str, str]]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        action = item.get("action")
        evidence = item.get("evidence")
        if not isinstance(line, str) or not isinstance(action, str):
            continue
        normalized_line = line.strip()
        if normalized_line not in eligible:
            continue
        normalized_action = action.strip().lower()
        normalized_evidence = evidence.strip() if isinstance(evidence, str) else ""
        decisions.setdefault(normalized_line, []).append(
            (normalized_action, normalized_evidence)
        )

    authorized = {}
    for line, line_decisions in decisions.items():
        if len(line_decisions) != 1:
            continue
        action, evidence = line_decisions[0]
        if action != "remove":
            continue
        if evidence and evidence in user_message:
            authorized[line] = evidence
    return authorized


def _apply_performative_relation_removals(
    content: str,
    removed_lines: set[str],
) -> str:
    relation_prefix = f"- {MEMBERSHIP_RELATION} "
    exact_relations = {
        line.strip()
        for line in removed_lines
        if line.strip().startswith(relation_prefix)
    }
    if not exact_relations:
        return content
    output = []
    skip_relation_children = False
    relation_parent_indent = 0
    for line in content.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if skip_relation_children:
            if not stripped:
                continue
            if indent > relation_parent_indent:
                continue
            skip_relation_children = False
        if stripped in exact_relations:
            skip_relation_children = True
            relation_parent_indent = indent
            continue
        output.append(line)
    return "\n".join(output)


def _authorized_removed_line_evidence(
    raw: object,
    user_message: str,
) -> dict[str, str]:
    if not isinstance(raw, list):
        return {}
    authorized = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        evidence = item.get("evidence")
        if not isinstance(line, str) or not isinstance(evidence, str):
            continue
        if _quoted_evidence_appears_in_message(evidence, user_message):
            authorized[line.strip()] = evidence.strip()
    return authorized


def _authorized_removed_lines(raw: object, user_message: str) -> set[str]:
    return set(_authorized_removed_line_evidence(raw, user_message))


def _authorized_relation_removals(
    *,
    name: str,
    current: str,
    merged: str,
    evidence_by_line: dict[str, str],
) -> tuple[dict, ...]:
    before = parse_souvenir_graph(name, current)
    after = parse_souvenir_graph(name, merged)
    after_keys = {
        (relation.kind, relation.target)
        for relation in after.relations
    }
    current_lines = current.splitlines()
    removals = []
    for relation in before.relations:
        key = (relation.kind, relation.target)
        if relation.kind != MEMBERSHIP_RELATION or key in after_keys:
            continue
        line_index = relation.line - 1
        if line_index < 0 or line_index >= len(current_lines):
            raise ValueError("ligne de relation introuvable")
        relation_line = current_lines[line_index].strip()
        evidence = evidence_by_line.get(relation_line)
        if not evidence:
            raise ValueError(
                f"suppression de relation non autorisée ({relation_line})"
            )
        removals.append(
            {
                "action": "remove",
                "kind": MEMBERSHIP_RELATION,
                "epistemic": "established",
                "source": name,
                "target": relation.target,
                "role": "",
                "evidence": evidence,
            }
        )
    if len(removals) > 3:
        raise ValueError("trop de suppressions de relation dans une souvenir")
    return tuple(removals)


def _merge_index_safely(path: Path, proposed: str) -> bool:
    with memory_file_lock(path):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        safe_proposed = "\n".join(
            line
            for line in proposed.splitlines()
            if not (
                line.strip().startswith("- ")
                and is_system_managed_souvenir(
                    line.strip()[2:].split(":", 1)[0].strip()
                )
            )
        )
        merged = _merge_souvenir_index(existing, safe_proposed)
        if existing.strip() == merged.strip():
            return False
        atomic_write_text(path, merged)
        return True


def _write_markdown_if_changed(
    *,
    result: MemoryAgentResult,
    path: Path,
    current: str,
    proposed: str,
    writer,
) -> bool:
    try:
        target = normalize_memory_dates(proposed.strip(), current=current)
    except ValueError as exc:
        _record_path(result.rejected_paths, path)
        result.reason = f"{result.reason} Invalid generated date: {exc}".strip()
        return False
    with memory_file_lock(path):
        latest = (
            path.read_text(encoding="utf-8").strip()
            if path.exists()
            else current.strip()
        )
        if latest == target:
            _record_path(result.unchanged_paths, path)
            return False
        writer(target)
    _record_path(result.changed_paths, path)
    return True


def _record_path(paths: list[str], path: Path) -> None:
    relative = _memory_path(path)
    if relative not in paths:
        paths.append(relative)


def _memory_path(path: Path) -> str:
    try:
        return memory_reference(MEMORY_ROOT, path.resolve())
    except ValueError:
        return path.name


def _clean_immediate_focus(content: str) -> str:
    lines = []
    forbidden_markers = [
        "prochaine étape",
        "prochaine decision",
        "prochaine décision",
        "decision en attente",
        "décision en attente",
        "choisir entre",
        "réponse donnée",
        "reponse donnee",
        "réponse assistant",
        "reponse assistant",
        "réponse de l'assistant",
        "reponse de l'assistant",
    ]
    for line in content.splitlines():
        normalized = line.strip().lower()
        if any(marker in normalized for marker in forbidden_markers):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines).strip()
    return cleaned or "# Focus immédiat\n- No explicit immediate thread."


def _persona_update_content(raw_update: object, user_message: str) -> str:
    if not isinstance(raw_update, dict) or raw_update.get("should_update") is not True:
        return ""
    evidence = raw_update.get("evidence")
    persona = raw_update.get("persona_md")
    if not isinstance(evidence, str) or not isinstance(persona, str):
        return ""
    if not evidence.strip() or not persona.strip():
        return ""
    if not _quoted_evidence_appears_in_message(evidence, user_message):
        return ""
    return persona.strip()


def _extract_followup(data: dict, user_message: str) -> dict | None:
    """Extract a valid follow-up intent from MemoryAgent JSON, or return None.

    Like persona_update, the optional field requires a strict should_schedule
    value and an exact evidence quote from the latest message."""
    raw = data.get("followup")
    if not isinstance(raw, dict) or raw.get("should_schedule") is not True:
        return None
    evidence = str(raw.get("evidence", "")).strip()
    if not _quoted_evidence_appears_in_message(evidence, user_message):
        return None
    topic = str(raw.get("topic", "")).strip()
    fire_at = str(raw.get("fire_at", "")).strip()
    if not topic or not fire_at:
        return None
    angle = str(raw.get("angle", "check-in")).strip() or "check-in"
    return {"topic": topic, "fire_at": fire_at, "angle": angle, "evidence": evidence}


def _extract_followups_pref(data: dict, user_message: str) -> dict | None:
    """Read the LLM-judged follow-up preference, requiring an exact evidence quote."""
    raw = data.get("followups_pref")
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "")).strip().lower()
    if action not in ("disable", "enable"):
        return None
    evidence = str(raw.get("evidence", "")).strip()
    if not _quoted_evidence_appears_in_message(evidence, user_message):
        return None
    return {"action": action, "evidence": evidence}


def _quoted_evidence_appears_in_message(evidence: str, user_message: str) -> bool:
    normalized_evidence = _normalize_evidence_text(evidence)
    normalized_message = _normalize_evidence_text(user_message)
    return bool(normalized_evidence and normalized_evidence in normalized_message)


def _normalize_evidence_text(text: str) -> str:
    replacements = {
        "\u00a0": " ",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()


def _record_common_ground_safely(
    *,
    user_id: str,
    observation: CommonGroundObservation,
    raw_candidate: object,
    raw_update: object = None,
    register: list[dict] | tuple[dict, ...] = (),
) -> CommonGroundShadowReceipt | None:
    try:
        receipt = record_common_ground_shadow_result(
            user_id=user_id,
            observation=observation,
            raw_candidate=raw_candidate,
            raw_update=raw_update,
            register=register,
        )
    except Exception as exc:
        logger.info(
            "Common Ground shadow ignoré | source=%s | error=%s",
            observation.source_trace_id,
            exc.__class__.__name__,
        )
        return None
    logger.info(
        "Common Ground shadow | source=%s | status=%s | kind=%s | action=%s | candidate=%s | reason=%s",
        observation.source_trace_id,
        receipt.status,
        receipt.kind,
        receipt.action,
        receipt.candidate_id[:12],
        receipt.reason_code,
    )
    return receipt


def _record_common_ground_skip_safely(
    *,
    user_id: str,
    observation: CommonGroundObservation,
    status: str,
    reason_code: str,
) -> CommonGroundShadowReceipt | None:
    try:
        return record_common_ground_shadow_skip(
            user_id=user_id,
            source_trace_id=observation.source_trace_id,
            source_turn_started_at=observation.source_turn_started_at,
            status=status,
            reason_code=reason_code,
        )
    except Exception as exc:
        logger.info(
            "Common Ground shadow failure ignorée | source=%s | error=%s",
            observation.source_trace_id,
            exc.__class__.__name__,
        )
        return None


def _apply_common_ground_receipt(
    result: MemoryAgentResult,
    receipt: CommonGroundShadowReceipt | None,
) -> None:
    if receipt is None:
        return
    result.common_ground_shadow_status = receipt.status
    result.common_ground_shadow_candidate_id = receipt.candidate_id
    result.common_ground_shadow_kind = receipt.kind
    result.common_ground_shadow_action = receipt.action


def _record_memory_relation_safely(
    *,
    user_id: str,
    context: MemoryRelationShadowContext,
    user_message: str,
    raw_updates: object,
    raw_collection_updates: object | None,
    available_souvenirs: tuple[str, ...],
    proposed_souvenirs: tuple[str, ...],
) -> MemoryRelationShadowReceipt | None:
    try:
        receipt = record_memory_relation_shadow_result(
            user_id=user_id,
            context=context,
            user_message=user_message,
            raw_updates=raw_updates,
            raw_collection_updates=raw_collection_updates,
            available_souvenirs=available_souvenirs,
            proposed_souvenirs=proposed_souvenirs,
        )
    except Exception as exc:
        logger.info(
            "Relations mémoire ombre ignorées | source=%s | error=%s",
            context.source_trace_id,
            exc.__class__.__name__,
        )
        return None
    logger.info(
        "Relations mémoire ombre | source=%s | status=%s | accepted=%d | rejected=%d | reason=%s",
        context.source_trace_id,
        receipt.status,
        receipt.accepted_count,
        receipt.rejected_count,
        receipt.reason_code,
    )
    return receipt


def _apply_relation_shadow_receipt(
    result: MemoryAgentResult,
    receipt: MemoryRelationShadowReceipt | None,
    *,
    requested: bool,
    context_available: bool,
) -> None:
    if receipt is not None:
        result.relation_shadow_status = receipt.status
        result.relation_shadow_event_id = receipt.event_id
        result.relation_shadow_accepted_count = receipt.accepted_count
        result.relation_shadow_rejected_count = receipt.rejected_count
        result.collection_shadow_accepted_count = (
            receipt.accepted_collection_count
        )
        result.collection_shadow_rejected_count = (
            receipt.rejected_collection_count
        )
    elif requested and not context_available:
        result.relation_shadow_status = "unavailable"


def _proposed_souvenir_names(data: dict) -> tuple[str, ...]:
    updates = data.get("capsule_updates")
    if not isinstance(updates, list):
        return ()
    return tuple(
        str(update.get("name", ""))
        for update in updates
        if isinstance(update, dict)
    )


def _proposed_relation_updates(
    data: dict,
    *,
    topology_bundle_enabled: bool = False,
) -> object:
    if topology_bundle_enabled:
        bundle_present, bundle = _proposed_topology_bundle(data)
        if bundle_present:
            if not isinstance(bundle, dict):
                return bundle
            return bundle.get("relations", [])
    updates = data.get("capsule_updates")
    if not isinstance(updates, list):
        return []
    relations = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        nested = update.get("relation_updates")
        if nested is None:
            continue
        if not isinstance(nested, list):
            return nested
        source = str(update.get("name", ""))
        for relation in nested:
            if not isinstance(relation, dict):
                relations.append(relation)
                continue
            relations.append({**relation, "source": source})
    return relations


def _with_authorized_relation_removals(
    raw_updates: object,
    authorized_removals: list[dict],
) -> object:
    if not authorized_removals:
        return raw_updates
    if raw_updates is None:
        combined = []
    elif isinstance(raw_updates, list):
        combined = list(raw_updates)
    else:
        combined = [raw_updates]
    seen = {
        (
            str(update.get("action", "")),
            str(update.get("source", "")),
            str(update.get("target", "")),
        )
        for update in combined
        if isinstance(update, dict)
    }
    for removal in authorized_removals:
        key = (
            str(removal.get("action", "")),
            str(removal.get("source", "")),
            str(removal.get("target", "")),
        )
        if key not in seen:
            combined.append(removal)
            seen.add(key)
    return combined


def _proposed_collection_updates(
    data: dict,
    *,
    topology_bundle_enabled: bool = False,
) -> object:
    if topology_bundle_enabled:
        bundle_present, bundle = _proposed_topology_bundle(data)
        if bundle_present:
            if not isinstance(bundle, dict):
                return bundle
            return bundle.get("collections", [])
    updates = data.get("capsule_updates")
    if not isinstance(updates, list):
        return []
    collections = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        nested = update.get("collection_update")
        if nested is None:
            continue
        if not isinstance(nested, dict):
            collections.append(nested)
            continue
        collections.append({**nested, "target": str(update.get("name", ""))})
    return collections


def _proposed_topology_bundle(data: dict) -> tuple[bool, object]:
    updates = data.get("capsule_updates")
    if not isinstance(updates, list):
        return False, None
    bundles: list[object] = []
    for update in updates:
        if not isinstance(update, dict) or "topology_updates" not in update:
            continue
        if not _complete_souvenir_update(update):
            return True, "topology_carrier_incomplete"
        bundles.append(update.get("topology_updates"))
    if not bundles:
        return False, None
    if len(bundles) != 1:
        return True, "multiple_topology_bundles"
    return True, bundles[0]


def _complete_souvenir_update(update: dict) -> bool:
    return all(
        isinstance(update.get(field), str) and update[field].strip()
        for field in ("name", "content_md", "index_line")
    )


def _clean_current_discussion(content: str) -> str:
    return _clean_immediate_focus(content)


def _souvenir_fact_lines(content: str) -> list[str]:
    facts = []
    current_section = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped.lower()
            continue
        if current_section == "## faits" and stripped.startswith("- "):
            facts.append(stripped[2:].strip())
    return facts


def _sanitize_souvenir_name(name: str) -> str:
    clean = name.strip().replace("\\", "/").strip("/")
    if clean == "index":
        return ""
    parts = []
    for part in clean.split("/"):
        part = re.sub(r"[^a-zA-Z0-9_-]", "_", part.strip())
        if part:
            parts.append(part)
    max_parts = 4 if parts[:1] == ["theme"] else 3
    return "/".join(parts[:max_parts])


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
