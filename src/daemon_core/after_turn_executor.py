"""Execute durable after-turn jobs through the host-neutral contract."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from daemon_core.after_turn import (
    AfterTurnExecution,
    AfterTurnHooks,
    AfterTurnRequest,
    complete_turn_and_wait,
)
from daemon_core.after_turn_spool import (
    AfterTurnJob,
    SceneProjectionJob,
)
from daemon_core.continuity import record_continuity_stage, technical_ref
from daemon_core.memory.writer import read_ground_zero_reference
from daemon_core.maintenance_errors import require_memory_update_success


AfterMemoryHook = Callable[[Any], None]
ErrorHook = Callable[[str, Exception], None]
MAX_SCENE_RECOVERY_BATCHES = 128


@dataclass(frozen=True)
class DurableAfterTurnExecution:
    job: AfterTurnJob
    execution: AfterTurnExecution
    scene_projection: SceneProjectionJob | None = None

    @property
    def memory_result(self):
        return self.execution.memory_result

    @property
    def working_state_started(self) -> bool:
        return self.execution.working_state_started

    @property
    def scene_started(self) -> bool:
        return self.execution.scene_started


def execute_after_turn_job(
    job: AfterTurnJob,
    *,
    harness,
    after_memory: AfterMemoryHook | None = None,
    on_error: ErrorHook | None = None,
    logger: logging.Logger | None = None,
    defer_scene: bool = False,
    defer_replaceable_projections: bool = False,
) -> DurableAfterTurnExecution:
    """Reconstruct one exact turn and finish every enabled durable stage."""

    user_message = read_ground_zero_reference(job.user_l0)
    assistant_response = read_ground_zero_reference(job.assistant_l0)
    request = AfterTurnRequest(
        user_id=job.user_id,
        user_message=user_message,
        assistant_response=assistant_response,
        scene_scope=job.scene_scope,
        source_trace_id=job.turn_id,
        source_turn_started_at=job.source_turn_started_at,
        route=None,
        selected_souvenirs=job.selected_souvenirs,
        read_souvenirs=job.read_souvenirs,
        user_l0_recorded=True,
        assistant_l0_recorded=True,
        library_active=job.library_active,
    )
    provider = harness.memory_provider

    scene = _scene_hook(
        job,
        provider=provider,
        logger=logger,
    )
    scene_is_deferred = bool(defer_scene and scene is not None)

    def _update_memory(turn: AfterTurnRequest):
        with require_memory_update_success():
            return _update_memory_required(turn)

    def _update_memory_required(turn: AfterTurnRequest):
        result = harness.update_memory_after_response(
            user_id=turn.user_id,
            message=turn.user_message,
            response=turn.assistant_response,
            route=None,
            selected_souvenirs=(
                list(turn.memory_souvenirs)
                if turn.memory_souvenirs is not None
                else None
            ),
            source_trace_id=turn.source_trace_id,
            source_turn_started_at=turn.source_turn_started_at,
            current_user_l0=job.user_l0,
            current_assistant_l0=job.assistant_l0,
            defer_replaceable_projections=defer_replaceable_projections,
        )
        _capture_relation_gardening_event(
            job,
            result,
            logger=logger,
        )
        _run_relation_publication(
            job,
            result,
            logger=logger,
        )
        return result

    execution = complete_turn_and_wait(
        request,
        AfterTurnHooks(
            update_memory=_update_memory,
            revise_working_state=None,
            revise_scene=None if scene_is_deferred else scene,
            after_memory=after_memory,
            on_error=on_error,
        ),
        defer_scene=scene_is_deferred,
    )
    _record_memory_provenance(job, execution.memory_result)
    return DurableAfterTurnExecution(
        job=job,
        execution=execution,
        scene_projection=(
            SceneProjectionJob.from_turn(job)
            if scene_is_deferred
            else None
        ),
    )


def execute_after_turn_projection_batch(
    executions: tuple[Any, ...],
    *,
    harness,
    logger: logging.Logger | None = None,
):
    """Refresh replaceable memory views once after an exact FIFO burst.

    Fact capture is deliberately not coalesced: every source turn has already
    passed MemoryAgent before this function is called.  Only conversation
    texture, trajectory/follow-up judgment and other latest-state consumers
    are refreshed from the final verified L0 boundary.
    """

    completed = [
        item
        for item in executions
        if isinstance(item, DurableAfterTurnExecution)
    ]
    if not completed:
        return None
    latest = max(
        completed,
        key=lambda item: (
            item.job.source_turn_started_at,
            item.job.created_at,
            item.job.turn_id,
        ),
    )
    scene_projection_expected = any(
        item.scene_projection is not None for item in completed
    )
    run_followup = any(item.memory_result is not None for item in completed)
    result = harness.refresh_after_turn_projections(
        user_id=latest.job.user_id,
        current_assistant_l0=latest.job.assistant_l0,
        run_followup=run_followup,
        scene_projection_expected=scene_projection_expected,
    )
    if logger is not None:
        logger.info(
            "Projections après-tour coalescées | source=%s | covered=%d | followup=%s",
            latest.job.turn_id,
            len(completed),
            bool(getattr(result, "followup", None)),
        )
    return result


def _capture_relation_gardening_event(
    job: AfterTurnJob,
    result: Any,
    *,
    logger: logging.Logger | None,
) -> None:
    from daemon_core.memory.relation_gardening import (
        record_relation_gardening_event,
        relation_gardening_enabled,
    )

    if not relation_gardening_enabled(job.user_id):
        return
    changed = list(getattr(result, "updated_souvenirs", ()) or ())
    context = []
    for group in (
        job.selected_souvenirs or (),
        job.read_souvenirs,
        getattr(result, "consulted_souvenirs", ()) or (),
        changed,
    ):
        for raw in group:
            name = str(raw).strip().replace("\\", "/")
            if name and name not in context:
                context.append(name)
    try:
        capture = record_relation_gardening_event(
            user_id=job.user_id,
            source_trace_id=job.turn_id,
            source_turn_started_at=job.source_turn_started_at,
            user_l0=job.user_l0,
            assistant_l0=job.assistant_l0,
            source_posture=str(
                getattr(result, "source_posture", "") or "none"
            ),
            changed_souvenirs=changed,
            context_souvenirs=context,
        )
    except Exception as exc:
        record_continuity_stage(
            job.turn_id,
            "memory_relation_gardening_capture",
            "failed",
            reason_code="capture_material_failure",
            error_type=exc.__class__.__name__,
        )
        if logger is not None:
            logger.warning(
                "Relation gardening capture failed | source=%s | error=%s",
                job.turn_id,
                exc,
            )
        raise

    stage_status = (
        "completed"
        if capture.status in {"created", "duplicate"}
        else "skipped"
    )
    record_continuity_stage(
        job.turn_id,
        "memory_relation_gardening_capture",
        stage_status,
        reason_code=capture.reason_code,
        gardening_event_ref=technical_ref(capture.event_id),
        gardening_changed_count=len(changed),
        gardening_context_count=len(context),
    )
    if logger is not None:
        logger.info(
            "Relation gardening capture | source=%s | status=%s | changed=%d | context=%d",
            job.turn_id,
            capture.status,
            len(changed),
            len(context),
        )


def execute_scene_projection(
    job: SceneProjectionJob,
    *,
    harness,
    logger: logging.Logger | None = None,
):
    """Materialize the latest scene after all covered turn journals are durable."""

    from daemon_core.scene_model import (
        capture_scene_model_context,
        run_scene_model_revision,
        scene_model_shadow_enabled,
    )

    if not job.library_active and not scene_model_shadow_enabled(job.user_id):
        return None
    scene = None
    batches = 1
    if not job.dialogue_turn_limit:
        scene = run_scene_model_revision(
            user_id=job.user_id,
            scene_scope=job.scene_scope,
            provider=harness.memory_provider,
            source_trace_id=job.source_trace_id,
            source_turn_started_at=job.source_turn_started_at,
            read_souvenirs=list(job.read_souvenirs),
            strict=True,
        )
    else:
        batches = 0
        for batch_index in range(MAX_SCENE_RECOVERY_BATCHES):
            context = capture_scene_model_context(
                job.user_id,
                job.scene_scope,
                list(job.read_souvenirs),
                provider=harness.memory_provider,
                dialogue_turn_limit=job.dialogue_turn_limit,
            )
            dialogue_turn_count = int(
                context.get("dialogue_turn_count", 0)
            )
            if dialogue_turn_count == 0:
                scene = context.get("previous")
                break
            scene = run_scene_model_revision(
                user_id=job.user_id,
                scene_scope=job.scene_scope,
                provider=harness.memory_provider,
                source_trace_id=job.source_trace_id,
                source_turn_started_at=job.source_turn_started_at,
                read_souvenirs=list(job.read_souvenirs),
                context=context,
                allow_same_source_revision=True,
                strict=True,
            )
            if scene is None:
                raise RuntimeError("scene recovery made no progress")
            batches += 1
            if not bool(context.get("dialogue_truncated")):
                break
        else:
            raise RuntimeError("scene recovery exceeded its batch bound")
    if logger is not None:
        logger.info(
            "Durable coalesced scene | source=%s | covered=%s | revision=%s | batches=%s | turn_limit=%s",
            job.source_trace_id,
            len(job.covered_turn_ids),
            scene.revision if scene else None,
            batches,
            job.dialogue_turn_limit,
        )
    if scene is not None:
        from daemon_core.conversation_state import (
            project_scene_conversation_state,
        )

        project_scene_conversation_state(
            job.user_id,
            reading=str(getattr(scene, "reading", "") or ""),
            conversation=str(getattr(scene, "conversation", "") or ""),
        )
    return scene


def _scene_hook(
    job: AfterTurnJob,
    *,
    provider,
    logger: logging.Logger | None,
):
    if not _has_provenance(job):
        return None

    from daemon_core.scene_model import (
        run_scene_model_revision,
        scene_model_shadow_enabled,
    )

    if not job.library_active and not scene_model_shadow_enabled(job.user_id):
        return None

    def _revise(turn: AfterTurnRequest) -> None:
        scene = run_scene_model_revision(
            user_id=turn.user_id,
            scene_scope=turn.scene_scope,
            provider=provider,
            source_trace_id=turn.source_trace_id,
            source_turn_started_at=turn.source_turn_started_at,
            read_souvenirs=list(turn.read_souvenirs),
            strict=True,
        )
        if logger is not None:
            logger.info(
                "Durable scene | source=%s | revision=%s",
                turn.source_trace_id,
                scene.revision if scene else None,
            )

    return _revise


def _has_provenance(job: AfterTurnJob) -> bool:
    return bool(
        job.turn_id
        and job.scene_scope
        and job.source_turn_started_at > 0
    )


def _record_memory_provenance(job: AfterTurnJob, result: Any) -> None:
    changed = list(getattr(result, "changed_paths", ()) or ())
    unchanged = list(getattr(result, "unchanged_paths", ()) or ())
    rejected = list(getattr(result, "rejected_paths", ()) or ())
    source_posture = str(
        getattr(result, "source_posture", "") or ""
    ).strip()
    unresolved_direct_fact = bool(
        rejected and source_posture == "direct_fact"
    )
    status = (
        "failed"
        if unresolved_direct_fact
        else "completed"
        if changed
        else "skipped"
    )
    reason = (
        "direct_fact_write_rejected"
        if unresolved_direct_fact
        else "memory_files_changed"
        if changed
        else "memory_write_rejected"
        if rejected
        else "memory_files_unchanged"
    )
    record_continuity_stage(
        job.turn_id,
        "memory_mutation",
        status,
        reason_code=reason,
        user_ref=technical_ref(job.user_id),
        source_turn_started_at=job.source_turn_started_at,
        changed_paths=changed,
        context_paths=_memory_context_paths(job, result),
        unchanged_paths=unchanged,
        rejected_paths=rejected,
        source_posture=source_posture,
        user_l0_ref=job.user_l0.as_dict(),
        assistant_l0_ref=job.assistant_l0.as_dict(),
    )


def _run_relation_publication(
    job: AfterTurnJob,
    result: Any,
    *,
    logger: logging.Logger | None,
) -> None:
    from daemon_core.memory.relation_publication_canary import (
        memory_relation_publication_canary_enabled,
        run_relation_publication_canary,
    )
    from daemon_core.memory.relation_outbox import (
        memory_relation_publication_dry_run_enabled,
        run_relation_publication_dry_run,
    )

    event_id = str(getattr(result, "relation_shadow_event_id", "") or "")
    if memory_relation_publication_canary_enabled(job.user_id, event_id):
        try:
            receipt = run_relation_publication_canary(
                user_id=job.user_id,
                event_id=event_id,
            )
        except Exception as exc:
            record_continuity_stage(
                job.turn_id,
                "memory_relations",
                "failed",
                reason_code="active_publication_material_failure",
                error_type=exc.__class__.__name__,
                relation_event_ref=technical_ref(event_id),
            )
            raise

        stage_status = (
            "completed"
            if receipt.status in {"published", "unchanged"}
            else "skipped"
        )
        record_continuity_stage(
            job.turn_id,
            "memory_relations",
            stage_status,
            reason_code=receipt.reason_code or f"canary_{receipt.status}",
            relation_event_ref=technical_ref(receipt.event_id),
            relation_published_count=receipt.published_count,
            relation_unchanged_count=receipt.unchanged_count,
            relation_blocked_count=receipt.blocked_count,
            relation_replayed=receipt.replayed,
        )
        if logger is not None:
            logger.info(
                "Memory topology canary | source=%s | status=%s | "
                "relations=%d/%d/%d | replayed=%s",
                job.turn_id,
                receipt.status,
                receipt.published_count,
                receipt.unchanged_count,
                receipt.blocked_count,
                receipt.replayed,
            )
        return

    if not memory_relation_publication_dry_run_enabled(job.user_id):
        return
    try:
        receipt = run_relation_publication_dry_run(
            user_id=job.user_id,
            event_id=event_id,
        )
    except Exception as exc:
        record_continuity_stage(
            job.turn_id,
            "memory_relations",
            "failed",
            reason_code="dry_run_material_failure",
            error_type=exc.__class__.__name__,
            relation_event_ref=technical_ref(event_id),
        )
        raise

    stage_status = (
        "completed"
        if receipt.status in {"planned", "unchanged", "partial"}
        else "skipped"
    )
    record_continuity_stage(
        job.turn_id,
        "memory_relations",
        stage_status,
        reason_code=receipt.reason_code or f"dry_run_{receipt.status}",
        relation_event_ref=technical_ref(receipt.event_id),
        relation_proposal_count=receipt.proposal_count,
        relation_planned_count=receipt.planned_count,
        relation_unchanged_count=receipt.unchanged_count,
        relation_blocked_count=receipt.blocked_count,
        relation_replayed=receipt.replayed,
        collection_proposal_count=receipt.collection_proposal_count,
        collection_planned_count=receipt.collection_planned_count,
        collection_unchanged_count=receipt.collection_unchanged_count,
        collection_blocked_count=receipt.collection_blocked_count,
    )
    if logger is not None:
        logger.info(
            "Memory topology dry-run | source=%s | status=%s | relations=%d/%d/%d | collections=%d/%d/%d | replayed=%s",
            job.turn_id,
            receipt.status,
            receipt.planned_count,
            receipt.unchanged_count,
            receipt.blocked_count,
            receipt.collection_planned_count,
            receipt.collection_unchanged_count,
            receipt.collection_blocked_count,
            receipt.replayed,
        )


def _memory_context_paths(job: AfterTurnJob, result: Any = None) -> list[str]:
    paths = []
    groups = (
        job.selected_souvenirs or (),
        job.read_souvenirs,
        getattr(result, "consulted_souvenirs", ()) or (),
    )
    for group in groups:
        for name in group:
            normalized = str(name).strip().replace("\\", "/").removeprefix("capsules/")
            candidate = Path(normalized)
            if (
                not normalized
                or candidate.is_absolute()
                or ".." in candidate.parts
            ):
                continue
            suffix = "" if candidate.suffix == ".md" else ".md"
            path = f"capsules/{candidate.as_posix()}{suffix}"
            if path not in paths:
                paths.append(path)
    return paths


__all__ = [
    "DurableAfterTurnExecution",
    "execute_after_turn_job",
    "execute_after_turn_projection_batch",
    "execute_scene_projection",
]
