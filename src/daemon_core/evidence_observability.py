"""Passive, content-free observability for one turn's evidence path."""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from daemon_core.continuity import record_continuity_stage, technical_ref
from daemon_core.evidence_result import project_evidence_tool_result


if TYPE_CHECKING:
    from daemon_core.voice_brief import VoiceBrief


def observe_prepared_evidence(
    trace_id: str,
    brief: VoiceBrief,
    *,
    prompt_variant: str,
) -> bool:
    """Record the prepared evidence topology without recording its content."""

    if not str(trace_id).strip():
        return False
    try:
        blocks = (*brief.leading_blocks, *brief.near_turn_blocks)
        function_chars: dict[str, int] = {}
        for block in blocks:
            function_chars[block.function] = function_chars.get(
                block.function,
                0,
            ) + len(block.raw_span)

        scene = brief.scene
        dossier = brief.turn_evidence
        coverage = brief.dialogue_coverage
        timeline = brief.near_term_timeline
        authoritative = brief.evidence.authoritative_sources
        current_context = getattr(
            brief.evidence,
            "current_context_sources",
            (),
        )
        opened = brief.evidence.opened_souvenirs
        atlas = brief.resources.atlas
        dialogue_chars = sum(len(message.content) for message in brief.recent_dialogue)
        return record_continuity_stage(
            trace_id,
            "evidence_preparation",
            "completed",
            reason_code="snapshot_compiled",
            prompt_variant=prompt_variant,
            scene_revision=scene.revision,
            scene_claim_count=len(scene.claims),
            scene_dependency_count=len(scene.dependencies),
            scene_source_count=len(scene.sources),
            scene_unresolved_source_count=len(scene.unresolved_source_ids),
            scene_watermark_ref=(
                technical_ref(scene.dialogue_watermark_source_id)
                if scene.dialogue_watermark_source_id
                else ""
            ),
            evidence_schema=dossier.schema_version,
            evidence_present_claim_count=len(dossier.present_claims),
            evidence_dependency_count=len(dossier.open_dependencies),
            evidence_unknown_count=len(dossier.material_unknowns),
            evidence_unabsorbed_turn_count=len(dossier.unabsorbed_dialogue),
            evidence_opened_document_count=len(dossier.opened_evidence),
            evidence_available_source_count=len(dossier.available_sources),
            evidence_capability_count=len(dossier.capability_handles),
            evidence_failure_count=len(dossier.material_failures),
            evidence_coverage_unit_count=dossier.source_unit_count,
            evidence_coverage_complete=dossier.coverage_complete,
            evidence_renderer_ready=dossier.renderer_ready,
            dialogue_coverage_status=coverage.status,
            dialogue_recent_turns=len(coverage.recent_turn_ids),
            dialogue_delta_turns=len(coverage.scene_delta_turn_ids),
            dialogue_visible_turns=len(coverage.visible_turn_ids),
            dialogue_expected_turns=len(coverage.expected_turn_ids),
            dialogue_missing_turns=len(coverage.missing_turn_ids),
            hot_capsule_count=len(opened),
            context_paths=[document.name for document in opened],
            atlas_capsule_count=len(atlas.souvenir_names),
            atlas_chars=atlas.rendered_chars,
            authoritative_source_count=len(authoritative),
            authoritative_source_chars=sum(
                len(document.text) for document in authoritative
            ),
            current_context_source_count=len(current_context),
            current_context_source_chars=sum(
                len(document.text) for document in current_context
            ),
            opened_capsule_count=len(opened),
            opened_capsule_chars=sum(len(document.text) for document in opened),
            near_term_entry_count=(len(timeline.entries) if timeline else 0),
            near_term_source_count=(len(timeline.sources) if timeline else 0),
            near_term_complete=(timeline.complete if timeline else True),
            identity_chars=function_chars.get("identity", 0),
            relationship_chars=function_chars.get("relationship", 0),
            temporal_chars=len(brief.temporal_frame.raw),
            situation_chars=function_chars.get("situation", 0),
            procedure_chars=function_chars.get("procedure", 0),
            evidence_chars=function_chars.get("evidence", 0),
            resource_chars=function_chars.get("resource", 0),
            dialogue_chars=dialogue_chars,
            near_turn_chars=sum(
                len(block.raw_span) for block in brief.near_turn_blocks
            ),
            current_message_chars=len(brief.current_message.content),
        )
    except Exception:
        return False


def observe_evidence_tool_result(
    trace_id: str,
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None,
    started_at: float | None = None,
) -> bool:
    """Record one exact-source tool result without persisting its evidence."""

    if not str(trace_id).strip():
        return False
    try:
        payload = result if isinstance(result, dict) else {}
        args = arguments if isinstance(arguments, dict) else {}
        tool_status = str(payload.get("status") or "error").strip().lower()
        canonical = project_evidence_tool_result(
            tool_name,
            arguments=args,
            result=payload,
        )
        event_status = "failed" if canonical.status == "error" else "completed"
        requested_paths = _string_list(args.get("paths"))
        souvenirs = payload.get("capsules")
        read_paths = []
        if isinstance(souvenirs, list):
            read_paths = [
                str(item.get("path") or "")
                for item in souvenirs
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            ]
        rejected_paths = _string_list(payload.get("rejected_paths"))
        source_identity = (
            payload.get("source_file")
            or payload.get("source")
            or payload.get("locator")
            or ""
        )
        result_chars = _non_negative_int(
            payload.get("total_chars"),
            default=(len(str(payload.get("evidence") or ""))),
        )
        duration_ms = None
        if started_at is not None:
            duration_ms = max(0.0, (time.perf_counter() - started_at) * 1000)
        return record_continuity_stage(
            trace_id,
            "evidence_tool",
            event_status,
            reason_code=f"{tool_name}_{tool_status}",
            tool_name=tool_name,
            tool_status=tool_status,
            requested_path_count=len(requested_paths),
            opened_path_count=len(read_paths),
            rejected_path_count=len(rejected_paths),
            requested_paths=requested_paths,
            read_paths=read_paths,
            rejected_paths=rejected_paths,
            result_chars=result_chars,
            evidence_present=bool(payload.get("evidence") or read_paths),
            response_constraint_present=bool(payload.get("response_constraint")),
            evidence_kind=str(payload.get("evidence_kind") or ""),
            error_code=str(payload.get("error_code") or ""),
            source_ref=(technical_ref(str(source_identity)) if source_identity else ""),
            canonical_schema=canonical.schema_version,
            canonical_status=canonical.status,
            canonical_evidence_count=len(canonical.evidence),
            canonical_source_ref_count=len(canonical.source_refs),
            canonical_unresolved_count=len(canonical.unresolved),
            canonical_response_constraint_present=bool(
                canonical.response_constraint
            ),
            canonical_resolution_status=canonical.resolution.relation_status,
            canonical_error_code=canonical.error_code,
            duration_ms=duration_ms,
        )
    except Exception:
        return False


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


__all__ = ["observe_evidence_tool_result", "observe_prepared_evidence"]
