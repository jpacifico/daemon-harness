"""Canonical host-neutral contract for preparing one sovereign turn."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable


TurnComposer = Callable[..., tuple[list[dict[str, Any]], Any]]


@dataclass(frozen=True)
class TurnRequest:
    user_id: str
    message: str
    platform: str
    session_id: str = ""
    for_delegation: bool = True
    message_received_at: float | None = None
    trace_id: str = ""


@dataclass(frozen=True)
class TurnPlan:
    request: TurnRequest
    messages: tuple[dict[str, Any], ...]
    voice_context: Any
    voice_path: str
    situation_source: str
    memory_source: str
    composer_name: str

    @property
    def leading_system_prompt(self) -> str:
        if not self.messages or self.messages[0].get("role") != "system":
            return ""
        return str(self.messages[0].get("content") or "")

    @property
    def near_turn_system_prompt(self) -> str:
        """System section immediately preceding the live user message."""

        if len(self.messages) < 2:
            return ""
        near_turn = self.messages[-2]
        if near_turn.get("role") != "system":
            return ""
        return str(near_turn.get("content") or "")

    @property
    def turns(self) -> list[dict[str, Any]]:
        return [
            message
            for message in self.messages[1:]
            if message.get("role") in {
                "user",
                "assistant",
                "developer",
                "system",
            }
        ]

    @property
    def developer_instruction_addition(self) -> str:
        """Native background instructions for hosts exposing a second tier."""

        return "\n\n".join(
            str(message.get("content") or "").strip()
            for message in self.messages
            if message.get("role") == "developer"
            and str(message.get("content") or "").strip()
        )

    @property
    def collapsed_system_prompt(self) -> str:
        """One system slot for hosts that cannot preserve near-turn systems."""

        return "\n\n".join(
            str(message.get("content") or "").strip()
            for message in self.messages
            if message.get("role") == "system"
            and str(message.get("content") or "").strip()
        )

    @property
    def prompt_variant(self) -> str:
        return str(getattr(self.voice_context, "prompt_variant", "current"))

    @property
    def scene_scope(self) -> str:
        return str(getattr(self.voice_context, "scene_scope", ""))

    @property
    def hot_souvenirs(self) -> tuple[str, ...]:
        hot_memory = getattr(self.voice_context, "hot_memory", None)
        return tuple(getattr(hot_memory, "souvenir_names", ()) or ())

    @property
    def atlas_collections(self) -> tuple[str, ...]:
        atlas = getattr(self.voice_context, "atlas", None)
        return tuple(getattr(atlas, "collection_names", ()) or ())

    @property
    def provenance(self) -> dict[str, str]:
        return {
            "voice_path": self.voice_path,
            "situation_source": self.situation_source,
            "memory_source": self.memory_source,
            "composer": self.composer_name,
        }


def _validate_canonical_plan(plan: TurnPlan) -> None:
    """Reject incomplete canonical plans before a host can consume them."""

    if not plan.leading_system_prompt.strip():
        raise ValueError("canonical turn plan requires a leading system prompt")
    if not plan.near_turn_system_prompt.strip():
        raise ValueError("canonical turn plan requires a near-turn system prompt")
    if not plan.collapsed_system_prompt.strip():
        raise ValueError("canonical turn plan requires a non-empty system prompt")
    if getattr(plan.voice_context, "atlas", None) is None:
        raise ValueError("canonical turn plan requires an exact-memory atlas")
    if getattr(plan.voice_context, "hot_memory", None) is None:
        raise ValueError("canonical turn plan requires an exact-memory view")
    if not plan.scene_scope.strip():
        raise ValueError("canonical turn plan requires a scene scope")
    coverage = getattr(plan.voice_context, "dialogue_coverage", None)
    if coverage is None or not bool(getattr(coverage, "exact", False)):
        raise ValueError("canonical turn plan requires exact dialogue coverage")


def prepare_turn(
    request: TurnRequest,
    *,
    composer: TurnComposer | None = None,
) -> TurnPlan:
    """Prepare a pure sovereign plan through the canonical composer."""

    from daemon_core.continuity import (
        begin_continuity_turn,
        record_continuity_stage,
        technical_ref,
    )

    begin_continuity_turn(
        request.trace_id,
        user_id=request.user_id,
        platform=request.platform,
        session_id=request.session_id,
    )
    record_continuity_stage(
        request.trace_id,
        "preparation",
        "pending",
        reason_code="composer_started",
        for_delegation=request.for_delegation,
    )

    canonical = composer is None
    if canonical:
        from daemon_core.canonical_voice import compose_canonical_voice_messages

        composer = compose_canonical_voice_messages

    try:
        messages, voice_context = composer(
            request.user_id,
            request.message,
            platform=request.platform,
            for_delegation=request.for_delegation,
            message_received_at=request.message_received_at,
        )
    except Exception as exc:
        record_continuity_stage(
            request.trace_id,
            "preparation",
            "failed",
            reason_code="composer_failed",
            error_type=exc.__class__.__name__,
        )
        raise

    plan = TurnPlan(
        request=request,
        messages=tuple(messages),
        voice_context=voice_context,
        voice_path=(
            "canonical"
            if canonical
            else str(getattr(voice_context, "voice_path", "injected"))
        ),
        situation_source=str(
            getattr(voice_context, "situation_source", "unspecified")
        ),
        memory_source=(
            "exact_library"
            if getattr(voice_context, "atlas", None) is not None
            else "unspecified"
        ),
        composer_name=(
            f"{getattr(composer, '__module__', '')}."
            f"{getattr(composer, '__name__', composer.__class__.__name__)}"
        ).strip("."),
    )
    if canonical:
        try:
            from daemon_core.voice_brief import (
                compile_voice_brief,
                render_reference_messages,
            )
            from daemon_core.voice_brief_renderer import (
                AUTHORITY_HIERARCHY_PROMPT_VARIANT,
                TEMPORAL_PROMPT_VARIANT,
                authority_hierarchy_canary_enabled,
                render_authority_hierarchy_voice_brief,
                render_temporal_voice_brief,
                temporal_voice_brief_canary_enabled,
            )

            brief = compile_voice_brief(plan)
            authority_hierarchy = authority_hierarchy_canary_enabled(
                request.user_id,
                request.platform,
            )
            temporal_hierarchy = temporal_voice_brief_canary_enabled(
                request.user_id,
                request.platform,
            )
            if authority_hierarchy and temporal_hierarchy:
                raise ValueError(
                    "authority and temporal voice-brief canaries are mutually exclusive"
                )
            if authority_hierarchy:
                rendered = render_authority_hierarchy_voice_brief(brief)
                voice_context = replace(
                    plan.voice_context,
                    prompt_variant=AUTHORITY_HIERARCHY_PROMPT_VARIANT,
                )
                plan = replace(
                    plan,
                    messages=rendered.messages,
                    voice_context=voice_context,
                )
            elif temporal_hierarchy:
                rendered = render_temporal_voice_brief(brief)
                voice_context = replace(
                    plan.voice_context,
                    prompt_variant=TEMPORAL_PROMPT_VARIANT,
                )
                plan = replace(
                    plan,
                    messages=rendered.messages,
                    voice_context=voice_context,
                )
            else:
                plan = replace(plan, messages=render_reference_messages(brief))
            _validate_canonical_plan(plan)
            try:
                from daemon_core.evidence_observability import (
                    observe_prepared_evidence,
                )

                observe_prepared_evidence(
                    request.trace_id,
                    brief,
                    prompt_variant=plan.prompt_variant,
                )
            except Exception:
                pass
        except Exception as exc:
            record_continuity_stage(
                request.trace_id,
                "preparation",
                "failed",
                reason_code="composer_invalid",
                error_type=exc.__class__.__name__,
            )
            raise
    system = plan.collapsed_system_prompt
    dialogue_coverage = getattr(plan.voice_context, "dialogue_coverage", None)
    episodic_scene_path = str(
        getattr(plan.voice_context, "episodic_scene_path", "") or ""
    )
    episodic_scene_details = {
        "episodic_scene_selected": bool(episodic_scene_path),
    }
    if episodic_scene_path:
        episodic_scene_details["episodic_scene_ref"] = technical_ref(
            episodic_scene_path
        )
    record_continuity_stage(
        request.trace_id,
        "preparation",
        "completed",
        reason_code="composer_completed",
        message_count=len(plan.messages),
        system_chars=len(system),
        system_fingerprint=technical_ref(system),
        prompt_variant=plan.prompt_variant,
        voice_path=plan.voice_path,
        situation_source=plan.situation_source,
        memory_source=plan.memory_source,
        composer=plan.composer_name,
        scene_scope_ref=(
            technical_ref(plan.scene_scope)
            if plan.scene_scope
            else ""
        ),
        hot_capsule_count=len(plan.hot_souvenirs),
        atlas_collection_count=len(plan.atlas_collections),
        dialogue_coverage_status=str(
            getattr(dialogue_coverage, "status", "unavailable")
        ),
        dialogue_visible_turns=len(
            tuple(getattr(dialogue_coverage, "visible_turn_ids", ()) or ())
        ),
        dialogue_expected_turns=len(
            tuple(getattr(dialogue_coverage, "expected_turn_ids", ()) or ())
        ),
        dialogue_missing_turns=len(
            tuple(getattr(dialogue_coverage, "missing_turn_ids", ()) or ())
        ),
        **episodic_scene_details,
    )
    return plan


__all__ = ["TurnPlan", "TurnRequest", "prepare_turn"]
