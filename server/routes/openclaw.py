"""Local HTTP bridge for the OpenClaw context engine.

OpenClaw runs in TS/Node and calls these endpoints across the process boundary.
Assembly reads context; ingestion writes the exact conversation journal and
schedules after-turn maintenance through the same runtime as Hermes.

    assemble() -> POST /openclaw/assemble -> prepare_turn
    ingest()   -> POST /openclaw/ingest   -> journal and after-turn cycle

The daemon composes context; OpenClaw retains its model, streaming and tools."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("openclaw_bridge")
router = APIRouter(prefix="/openclaw")


def _observe_continuity(
    trace_id: str,
    stage: str,
    status: str,
    *,
    reason_code: str,
    **details: Any,
) -> None:
    try:
        from daemon_core.continuity import record_continuity_stage

        record_continuity_stage(
            trace_id,
            stage,
            status,
            reason_code=reason_code,
            **details,
        )
    except Exception:
        pass


class AssembleRequest(BaseModel):
    user_id: str
    message: str
    session_id: str = ""
    trace_id: str = ""


class AssembleResponse(BaseModel):
    system_prompt_addition: str
    sovereign_system_prompt: str = ""
    primary_system_prompt: str = ""
    near_turn_system_prompt: str = ""
    canonical_recent_dialogue: list[dict[str, str]] = Field(default_factory=list)
    souvenirs: list[str]
    selection_source: str | None = None
    selection_reused: bool = False
    depth: str
    scene_register: str
    route: dict = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    voice_path: str = "canonical"
    canary_trace_id: str = ""
    source_turn_started_at: float = 0.0
    scene_scope: str = ""
    hot_souvenirs: list[str] = Field(default_factory=list)
    prompt_variant: str = "current"
    degraded: bool = False
    degradation_reason: str = ""


class IngestRequest(BaseModel):
    user_id: str
    role: str  # "user" | "assistant"
    content: str
    last_user_message: str = ""  # Required on assistant turns by MemoryAgent.
    selected_souvenirs: list[str] = Field(default_factory=list)
    route: dict = Field(default_factory=dict)
    voice_path: str = ""
    canary_trace_id: str = ""
    source_turn_started_at: float = 0.0
    scene_scope: str = ""
    read_souvenirs: list[str] = Field(default_factory=list)
    session_id: str = ""
    idempotency_key: str = ""
    user_l0_ref: dict = Field(default_factory=dict)
    occurred_at: float = 0.0
    historical_recovery: bool = False


class TranscriptReconciliationMessage(BaseModel):
    event_id: str = Field(min_length=1, max_length=256)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000)
    occurred_at: float = Field(gt=0)


class TranscriptReconciliationRequest(BaseModel):
    user_id: str
    session_id: str = Field(min_length=1, max_length=256)
    messages: list[TranscriptReconciliationMessage] = Field(
        default_factory=list,
        max_length=128,
    )


class TranscriptReconciliationResponse(BaseModel):
    scanned: int
    already_recorded: int
    restored: int


class ToolRequest(BaseModel):
    user_id: str
    session_id: str = ""
    trace_id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    read_souvenirs: list[str] = Field(default_factory=list)
    library_preflight_calls: int = 0


class ToolResponse(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)
    read_souvenirs: list[str] = Field(default_factory=list)
    library_preflight_consumed: bool = False


class IngestResponse(BaseModel):
    ingested: bool
    memory_updated: bool = False
    reason: str = ""
    l0_ref: dict = Field(default_factory=dict)


# Cache providers when loading the module, as the chat router harness does.
_routing_provider = None
_selection_provider = None
_memory_provider = None

def _providers():
    global _routing_provider, _selection_provider, _memory_provider
    if _routing_provider is None:
        from daemon_core.providers.factory import (
            build_conversation_provider,
            build_memory_provider,
            build_routing_provider,
        )
        _routing_provider = build_routing_provider()
        _selection_provider = build_conversation_provider()
        _memory_provider = build_memory_provider()
    return _routing_provider, _selection_provider, _memory_provider


@router.post("/assemble", response_model=AssembleResponse)
def assemble(request: AssembleRequest) -> AssembleResponse:
    """Compose un tour canonique ; `ingest` reste seul responsable de L0."""
    canonical_started_at = time.perf_counter()
    try:
        return _assemble_canonical(request)
    except Exception as exc:
        logger.warning(
            "assemble canonique échoué, repli hôte signalé | %s",
            exc.__class__.__name__,
        )
        return _assemble_degraded(
            request,
            exc,
            elapsed_ms=round(
                (time.perf_counter() - canonical_started_at) * 1000
            ),
        )


def _assemble_canonical(request: AssembleRequest) -> AssembleResponse:
    """Compose the same canonical bounded voice used by Hermes."""
    from daemon_core.latency_probe import new_trace_id
    from daemon_core.thin_sovereign import (
        adapt_turn_commitment_for_unfiltered_streaming,
    )
    from daemon_core.turn_runtime import TurnRequest, prepare_turn

    started_at = time.perf_counter()
    source_turn_started_at = time.time()
    trace_id = request.trace_id.strip() or new_trace_id()
    plan = prepare_turn(
        TurnRequest(
            user_id=request.user_id,
            message=request.message,
            platform="openclaw",
            session_id=request.session_id,
            for_delegation=True,
            message_received_at=source_turn_started_at,
            trace_id=trace_id,
        )
    )
    candidate = plan.voice_context
    if not plan.leading_system_prompt:
        raise ValueError("missing canonical system")
    if not plan.near_turn_system_prompt:
        raise ValueError("missing canonical near-turn system")
    # Hermes keeps a second system message immediately before the live turn.
    # OpenClaw exposes one sovereign system slot, so preserve every system
    # section in its original order instead of silently dropping the threshold.
    # Codex app-server publishes its assistant deltas before OpenClaw exposes
    # any plugin output transform. Keep the same private turn judgment, but ask
    # for it silently at this transport boundary so its technical marker can
    # never flash inside Telegram's native answer preview. Hermes and the
    # canonical composer keep their stream-filtered contract unchanged.
    primary_system = adapt_turn_commitment_for_unfiltered_streaming(
        plan.leading_system_prompt
    )
    near_turn_system = adapt_turn_commitment_for_unfiltered_streaming(
        plan.near_turn_system_prompt
    )
    system = "\n\n".join((primary_system, near_turn_system))
    if not system:
        raise ValueError("empty canonical system")
    # Hermes already consumes these exact recent user/assistant turns through
    # ``plan.turns``.  OpenClaw has its own native history, so expose only the
    # bounded canonical dialogue and let its bridge add turns that are absent.
    # The final user item is the live request and must remain owned by the host.
    canonical_recent_dialogue = [
        {
            "role": str(message["role"]),
            "content": str(message.get("content") or ""),
        }
        for message in plan.messages[1:-1]
        if message.get("role") in {"user", "assistant"}
        and str(message.get("content") or "").strip()
    ]
    elapsed = round((time.perf_counter() - started_at) * 1000)
    hot_souvenirs = list(candidate.hot_memory.souvenir_names)
    scene_model = getattr(candidate, "scene_model", None)
    working_state = (
        f"scene:r{scene_model.revision}"
        if scene_model is not None
        else plan.situation_source
    )
    logger.info(
        "assemble canonique | user_id=%s | session_id=%s | "
        "prompt_variant=%s | atlas_souvenirs=%s | atlas_collections=%s | "
        "atlas_chars=%s | working_state=%s | "
        "scene_delta_chars=%s | hot_souvenirs=%s | total_ms=%s",
        request.user_id,
        request.session_id,
        getattr(candidate, "prompt_variant", "current"),
        len(candidate.atlas.souvenir_names),
        len(getattr(candidate.atlas, "collection_names", ())),
        candidate.atlas.rendered_chars,
        working_state,
        len(getattr(candidate, "scene_delta", "")),
        hot_souvenirs,
        elapsed,
    )
    _observe_continuity(
        trace_id,
        "voice_path",
        "completed",
        reason_code="canonical",
        voice_path=plan.voice_path,
    )
    return AssembleResponse(
        system_prompt_addition=plan.developer_instruction_addition,
        sovereign_system_prompt=system,
        primary_system_prompt=primary_system,
        near_turn_system_prompt=near_turn_system,
        canonical_recent_dialogue=canonical_recent_dialogue,
        souvenirs=hot_souvenirs,
        selection_source="canonical",
        depth="contextual",
        scene_register="pratique",
        route={
            "mode": "canonical",
            "reason": "Voix souveraine canonique avec bibliothèque consultable.",
        },
        timings_ms={
            "load": elapsed,
            "radar": 0,
            "selection": 0,
            "addition": 0,
            "sovereign": elapsed,
            "total": elapsed,
        },
        voice_path=plan.voice_path,
        canary_trace_id=trace_id,
        source_turn_started_at=source_turn_started_at,
        scene_scope=candidate.scene_scope,
        hot_souvenirs=hot_souvenirs,
        prompt_variant=getattr(candidate, "prompt_variant", "current"),
    )


def _assemble_degraded(
    request: AssembleRequest,
    error: Exception,
    *,
    elapsed_ms: int,
) -> AssembleResponse:
    """Return one observable native-host fallback without composing P1c."""

    from daemon_core.latency_probe import new_trace_id
    from daemon_core.runtime_degradation import (
        CANONICAL_PREPARE_FAILED,
        DEGRADED_HOST_CONTRACT,
    )

    trace_id = request.trace_id.strip() or new_trace_id()
    started_at = time.time()
    _observe_continuity(
        trace_id,
        "voice_path",
        "failed",
        reason_code=CANONICAL_PREPARE_FAILED,
        voice_path="host_fallback",
        error_type=error.__class__.__name__,
    )
    return AssembleResponse(
        system_prompt_addition=DEGRADED_HOST_CONTRACT,
        sovereign_system_prompt="",
        souvenirs=[],
        selection_source="degraded",
        depth="light",
        scene_register="pratique",
        route={
            "mode": "degraded",
            "reason": "Canonical daemon memory unavailable for this turn.",
        },
        timings_ms={
            "load": elapsed_ms,
            "radar": 0,
            "selection": 0,
            "addition": 0,
            "sovereign": elapsed_ms,
            "total": elapsed_ms,
        },
        voice_path="host_fallback",
        canary_trace_id=trace_id,
        source_turn_started_at=started_at,
        degraded=True,
        degradation_reason=CANONICAL_PREPARE_FAILED,
    )


@router.post("/tool", response_model=ToolResponse)
def execute_tool(request: ToolRequest) -> ToolResponse:
    """Execute bounded sovereign-memory tools for OpenClaw's native tool loop."""
    started_at = time.perf_counter()
    if request.name == "daemon_read_souvenirs":
        response = _read_souvenirs_tool(request)
    elif request.name == "daemon_recall_l0":
        response = _recall_l0_tool(request)
    else:
        response = ToolResponse(
            result={"status": "error", "reason": "Unknown tool."}
        )
    try:
        from daemon_core.evidence_observability import (
            observe_evidence_tool_result,
        )

        observe_evidence_tool_result(
            request.trace_id,
            tool_name=request.name,
            arguments=request.arguments,
            result=response.result,
            started_at=started_at,
        )
    except Exception:
        pass
    return response


def _read_souvenirs_tool(request: ToolRequest) -> ToolResponse:
    from daemon_core.held_atlas_sovereign import (
        collection_coverage_checkpoint,
        parse_scene_coverage,
        read_exact_active_souvenirs,
        scene_coverage_checkpoint,
    )

    paths = request.arguments.get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(path, str) or not path.strip() for path in paths)
    ):
        return ToolResponse(
            result={"status": "error", "reason": "A list of non-empty exact paths is required."},
            read_souvenirs=request.read_souvenirs,
        )
    try:
        coverage = parse_scene_coverage(request.arguments)
        exact = read_exact_active_souvenirs(
            paths,
            user_id=request.user_id,
            platform="openclaw",
            collection_path=request.arguments.get("collection_path", ""),
            collection_offset=request.arguments.get("collection_offset", 0),
        )
    except ValueError as exc:
        return ToolResponse(
            result={"status": "error", "reason": str(exc)},
            read_souvenirs=request.read_souvenirs,
        )
    read = _unique_strings([*request.read_souvenirs, *exact.souvenir_names])
    status = (
        "ok"
        if not exact.rejected_paths
        and (
            exact.collection_page is None
            or exact.collection_page.next_offset is None
        )
        else "partial"
        if exact.souvenirs
        else "not_found"
    )
    return ToolResponse(
        result={
            "status": status,
            "capsules": [
                {"path": name, "content": content}
                for name, content in exact.souvenirs
            ],
            "rejected_paths": list(exact.rejected_paths),
            "total_chars": exact.rendered_chars,
            "collection": (
                exact.collection_page.as_dict()
                if exact.collection_page is not None
                else None
            ),
            "scene_assessment": coverage.as_dict(),
            "coverage_checkpoint": " ".join(
                part
                for part in (
                    scene_coverage_checkpoint(coverage),
                    collection_coverage_checkpoint(exact.collection_page),
                )
                if part
            ),
        },
        read_souvenirs=read,
    )


def _recall_l0_tool(request: ToolRequest) -> ToolResponse:
    from daemon_core.held_atlas_sovereign import (
        build_exact_memory_atlas,
        judge_unread_souvenirs_before_l0,
        read_exact_active_souvenirs,
        verify_l0_evidence_coverage,
    )
    from daemon_core.memory.coverage import RecoveryNeed
    from daemon_core.memory.forensics import (
        attempt_l0_forensic_recovery,
        chronology_tool_payload,
        failed_recall_tool_payload,
        personal_fact_tool_payload,
    )
    from daemon_core.memory.reader import load_recent_dialogue

    need = request.arguments.get("need")
    lookup_kind = request.arguments.get("lookup_kind", "personal_fact")
    if not isinstance(need, str) or not need.strip():
        return ToolResponse(
            result={"status": "error", "reason": "The exact personal recall need is required."},
            read_souvenirs=request.read_souvenirs,
        )
    if lookup_kind not in {"personal_fact", "conversation_chronology"}:
        return ToolResponse(
            result={"status": "error", "reason": "The L0 recall kind is invalid."},
            read_souvenirs=request.read_souvenirs,
        )
    routing_provider, _, _ = _providers()
    recent_dialogue = load_recent_dialogue(request.user_id, max_turns=14)

    if lookup_kind == "personal_fact" and request.library_preflight_calls < 2:
        try:
            preflight = judge_unread_souvenirs_before_l0(
                need.strip(),
                build_exact_memory_atlas(
                    user_id=request.user_id,
                    platform="openclaw",
                ),
                request.read_souvenirs,
                routing_provider,
                recent_dialogue=recent_dialogue,
            )
            if preflight.paths:
                exact = read_exact_active_souvenirs(
                    list(preflight.paths),
                    user_id=request.user_id,
                    platform="openclaw",
                )
                if exact.souvenirs:
                    read = _unique_strings(
                        [*request.read_souvenirs, *exact.souvenir_names]
                    )
                    return ToolResponse(
                        result={
                            "status": "capsule_sources",
                            "l0_searched": False,
                            "reason": (
                                "Plausible durable sources remained unread "
                                "before the L0 fallback. Reassess their exact coverage."
                            ),
                            "selection_reason": preflight.reason,
                            "capsules": [
                                {"path": name, "content": content}
                                for name, content in exact.souvenirs
                            ],
                            "rejected_paths": list(exact.rejected_paths),
                            "total_chars": exact.rendered_chars,
                        },
                        read_souvenirs=read,
                        library_preflight_consumed=True,
                    )
        except Exception as exc:
            logger.info(
                "Pré-vol bibliothèque OpenClaw ignoré | user_id=%s | error=%s",
                request.user_id,
                exc.__class__.__name__,
            )

    try:
        recall_message = f"Recall need: {need.strip()}"
        if lookup_kind == "conversation_chronology":
            from daemon_core.memory.forensics import (
                attempt_l0_chronology_recovery,
            )

            recovery = attempt_l0_chronology_recovery(
                user_id=request.user_id,
                message=recall_message,
                recent_dialogue=recent_dialogue,
                provider=routing_provider,
            )
        else:
            recovery = attempt_l0_forensic_recovery(
                user_id=request.user_id,
                message=recall_message,
                recent_dialogue=recent_dialogue,
                recovery_needs=[RecoveryNeed(description=need.strip())],
                provider=routing_provider,
            )
        status = recovery.status
        confidence = recovery.confidence
        if (
            status == "found"
            and lookup_kind == "personal_fact"
            and recovery.relation_status == "established"
        ):
            complete, coverage_reason = verify_l0_evidence_coverage(
                need=need.strip(),
                evidence=recovery.evidence,
                evidence_context=getattr(recovery, "evidence_context", ""),
                provider=routing_provider,
            )
            if not complete:
                status = "partial"
                logger.info(
                    "Rappel L0 OpenClaw partiel | user_id=%s | reason=%s",
                    request.user_id,
                    coverage_reason
                    or "La citation exacte ne couvre pas toute la pièce demandée.",
                )
                confidence = 0.0
        if lookup_kind == "conversation_chronology":
            result = chronology_tool_payload(recovery)
        else:
            result = personal_fact_tool_payload(
                recovery,
                status=status,
                confidence=confidence,
            )
    except Exception as exc:
        logger.warning(
            "Rappel L0 OpenClaw échoué | user_id=%s | error=%s",
            request.user_id,
            exc.__class__.__name__,
        )
        result = failed_recall_tool_payload(evidence_kind=lookup_kind)
    return ToolResponse(
        result=result,
        read_souvenirs=request.read_souvenirs,
        library_preflight_consumed=(
            lookup_kind == "personal_fact"
            and request.library_preflight_calls < 2
        ),
    )


@router.post("/ingest", response_model=IngestResponse)
def ingest(request: IngestRequest) -> IngestResponse:
    """Persist a message in the exact conversation journal.

    An assistant turn schedules MemoryAgent after-turn consolidation,
    equivalent to Hermes sync_turn."""
    from daemon_core.after_turn import record_turn_message
    from daemon_core.runtime_degradation import (
        strip_degraded_response_notice,
    )
    from daemon_core.voice_protocol import strip_internal_turn_metadata

    role = "assistant" if request.role.strip().lower() == "assistant" else "user"
    content = (
        strip_degraded_response_notice(
            strip_internal_turn_metadata(request.content)
        )
        if role == "assistant"
        else request.content
    )
    l0_written, l0_reference = _safe_append(
        record_turn_message,
        request.user_id,
        role,
        content,
        source_trace_id=request.canary_trace_id,
        idempotency_key=(
            f"openclaw:{request.idempotency_key}:{role}"
            if request.idempotency_key.strip()
            else ""
        ),
        occurred_at=request.occurred_at,
    )
    serialized_l0_reference = (
        l0_reference.as_dict()
        if hasattr(l0_reference, "as_dict")
        else {}
    )

    if role != "assistant":
        return IngestResponse(
            ingested=l0_written,
            l0_ref=serialized_l0_reference,
        )

    if request.historical_recovery:
        _observe_continuity(
            request.canary_trace_id,
            "cycle",
            "skipped",
            reason_code="historical_turn_restored_without_reprojection",
        )
        return IngestResponse(
            ingested=l0_written,
            memory_updated=False,
            reason=(
                "Échange historique restauré à sa date d'origine ; "
                "état courant inchangé."
            ),
            l0_ref=serialized_l0_reference,
        )

    reported_voice_path = request.voice_path.strip()
    canonical_turn = reported_voice_path == "canonical"
    effective_voice_path = reported_voice_path or "host_fallback"
    _observe_continuity(
        request.canary_trace_id,
        "response",
        "completed",
        reason_code="host_delivery_observed",
        voice_path=effective_voice_path,
    )

    if not request.last_user_message.strip():
        logger.warning("Cycle mémoire ignoré | reason=message utilisateur du tour absent")
        _observe_continuity(
            request.canary_trace_id,
            "cycle",
            "failed",
            reason_code="missing_user_message",
        )
        return IngestResponse(
            ingested=True,
            memory_updated=False,
            reason="Paire utilisateur/assistant incomplète ; récupération nocturne depuis L0.",
            l0_ref=serialized_l0_reference,
        )

    # MemoryAgent runs AFTER the turn and must never delay the HTTP response.
    # Synchronous consolidation once took 30-60 seconds and timed out the adapter;
    # the OpenClaw Telegram turn then encountered a session conflict.
    # As in Hermes, run it in the background, outside the critical path.
    barrier = None
    if canonical_turn:
        durable_queued = False
        durable_requested = False
        try:
            from daemon_core.after_turn_spool import (
                AfterTurnJob,
                after_turn_spool_enabled,
            )
            from daemon_core.memory.writer import GroundZeroReference

            durable_requested = after_turn_spool_enabled()
            if durable_requested:
                user_reference = GroundZeroReference.from_dict(
                    request.user_l0_ref
                )
                if not isinstance(
                    l0_reference,
                    GroundZeroReference,
                ):
                    raise ValueError("assistant L0 reference missing")
                reference_turns = {
                    value
                    for value in (
                        user_reference.turn_id,
                        l0_reference.turn_id,
                    )
                    if value
                }
                if (
                    request.canary_trace_id
                    and reference_turns
                    and reference_turns != {
                        request.canary_trace_id
                    }
                ):
                    logger.info(
                        "Ingest OpenClaw dupliqué ignoré | key=%s",
                        request.idempotency_key or "trace",
                    )
                    return IngestResponse(
                        ingested=True,
                        memory_updated=False,
                        reason="Message déjà ingéré.",
                        l0_ref=serialized_l0_reference,
                    )
                from daemon_core.after_turn_spool import AfterTurnSpool
                from integrations.openclaw_plugin.origin import (
                    resolve_delivery_origin,
                )

                origin = {
                    "platform": "openclaw",
                    "session_id": request.session_id,
                    "voice_path": effective_voice_path,
                }
                origin.update(resolve_delivery_origin(request.session_id))

                queued = AfterTurnSpool().handoff(
                    AfterTurnJob(
                        turn_id=request.canary_trace_id,
                        host="openclaw",
                        user_id=request.user_id,
                        source_turn_started_at=(
                            request.source_turn_started_at
                        ),
                        scene_scope=request.scene_scope,
                        user_l0=user_reference,
                        assistant_l0=l0_reference,
                        selected_souvenirs=tuple(
                            request.selected_souvenirs
                        ),
                        read_souvenirs=tuple(request.read_souvenirs),
                        tool_read_souvenirs=tuple(request.read_souvenirs),
                        library_active=True,
                        origin=origin,
                    )
                )
                durable_queued = True
                logger.info(
                    "Après-tour OpenClaw durable confié | source=%s | state=%s | created=%s",
                    request.canary_trace_id,
                    queued.state,
                    queued.created,
                )
        except Exception as exc:
            if durable_requested:
                # Preserve the native host's durable outbox obligation. L0 is
                # idempotent; HTTP success would wrongly acknowledge delivery.
                logger.error("Après-tour durable indisponible | error=%s", exc.__class__.__name__)
                raise HTTPException(status_code=503, detail="after_turn_handoff_unavailable") from exc
            logger.warning(
                "Après-tour OpenClaw durable non confié, repli historique | %s",
                exc.__class__.__name__,
            )
        if not durable_queued:
            from daemon_core.after_turn import AfterTurnRequest

            barrier = _schedule_sovereign_after_turn(
                AfterTurnRequest(
                    user_id=request.user_id,
                    user_message=request.last_user_message,
                    assistant_response=content,
                    scene_scope=request.scene_scope,
                    source_trace_id=request.canary_trace_id,
                    source_turn_started_at=request.source_turn_started_at,
                    route=None,
                    selected_souvenirs=tuple(
                        request.selected_souvenirs
                    ),
                    read_souvenirs=tuple(request.read_souvenirs),
                    user_l0_recorded=True,
                    assistant_l0_recorded=l0_written,
                    library_active=True,
                )
            )
    else:
        barrier = _schedule_memory_agent(
            user_id=request.user_id,
            user_message=request.last_user_message,
            assistant_response=content,
            route_data=request.route,
            selected_souvenirs=request.selected_souvenirs,
        )
    _schedule_host_capability_lifecycle_monitor(barrier)
    return IngestResponse(
        ingested=True,
        memory_updated=False,
        reason=(
            "Cycle après-tour confié à la file durable."
            if canonical_turn and durable_queued
            else "MemoryAgent planifié en tâche de fond."
        ),
        l0_ref=serialized_l0_reference,
    )


@router.post(
    "/reconcile-transcript",
    response_model=TranscriptReconciliationResponse,
)
def reconcile_transcript(
    request: TranscriptReconciliationRequest,
) -> TranscriptReconciliationResponse:
    """Restore a bounded recent native transcript gap into exact shared L0."""

    now = time.time()
    oldest = now - 7 * 24 * 60 * 60
    for message in request.messages:
        if message.occurred_at < oldest or message.occurred_at > now + 300:
            raise HTTPException(
                status_code=422,
                detail="Transcript reconciliation is limited to recent messages.",
            )

    from daemon_core.memory.writer import reconcile_ground_zero_messages

    result = reconcile_ground_zero_messages(
        request.user_id,
        (message.model_dump() for message in request.messages),
        source="openclaw",
        session_id=request.session_id,
    )
    logger.info(
        "Rattrapage transcript OpenClaw | scanned=%s | existing=%s | restored=%s",
        result.scanned,
        result.already_recorded,
        result.restored,
    )
    return TranscriptReconciliationResponse(
        scanned=result.scanned,
        already_recorded=result.already_recorded,
        restored=result.restored,
    )


_memory_slot = threading.BoundedSemaphore(value=1)


class _MemoryCycleBarrier:
    def __init__(self) -> None:
        self._done = threading.Event()
        self.succeeded = False

    def finish(self, *, succeeded: bool) -> None:
        self.succeeded = succeeded
        self._done.set()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._done.wait(timeout=timeout_seconds)


def _schedule_host_capability_lifecycle_monitor(
    barrier: _MemoryCycleBarrier | None,
) -> None:
    """Record a complete evaluation lifecycle without delaying OpenClaw."""

    raw_timeout = os.getenv(
        "HOST_CAPABILITY_AFTER_TURN_WAIT_SECONDS",
        "",
    ).strip()
    if not raw_timeout or not os.getenv(
        "DAEMON_CORE_LLM_TELEMETRY_FILE",
        "",
    ).strip():
        return
    try:
        timeout = min(420.0, max(0.0, float(raw_timeout)))
    except ValueError:
        return

    def _monitor() -> None:
        from daemon_core.providers.telemetry import (
            record_lifecycle_marker,
            wait_for_llm_quiescence,
        )

        started = time.monotonic()
        completed = bool(
            barrier is not None and barrier.wait(timeout_seconds=timeout)
        )
        remaining = max(0.0, timeout - (time.monotonic() - started))
        quiescent = completed and wait_for_llm_quiescence(
            timeout_seconds=remaining,
            idle_seconds=1.0,
        )
        record_lifecycle_marker(
            quiescent=quiescent,
            timeout_seconds=timeout,
        )

    threading.Thread(
        target=_monitor,
        daemon=True,
        name="openclaw-host-capability-lifecycle",
    ).start()


def _run_memory_update(
    *,
    user_id: str,
    user_message: str,
    assistant_response: str,
    route_data: dict | None = None,
    selected_souvenirs: list[str] | tuple[str, ...] | None = None,
):
    from daemon_core.context_router import ContextRoute
    from daemon_core.loop import DaemonCore

    routing_provider, selection_provider, memory_provider = _providers()
    route = ContextRoute(**route_data) if route_data else None
    harness = DaemonCore(
        provider=selection_provider,
        routing_provider=routing_provider,
        memory_provider=memory_provider,
    )
    result = harness.update_memory_after_response(
        user_id=user_id,
        message=user_message,
        response=assistant_response,
        route=route,
        selected_souvenirs=(
            list(selected_souvenirs)
            if selected_souvenirs is not None
            else None
        ),
    )
    logger.info(
        "Cycle mémoire (fond) | souvenirs=%s | reason=%s",
        result.updated_souvenirs if result else [],
        result.reason if result else "route non contextuelle",
    )
    return result


def _schedule_memory_agent(
    user_id: str,
    user_message: str,
    assistant_response: str,
    route_data: dict | None = None,
    selected_souvenirs: list[str] | None = None,
) -> _MemoryCycleBarrier | None:
    """Run background memory maintenance, with only one cycle at a time."""
    if not _memory_slot.acquire(blocking=False):
        logger.info("MemoryAgent ignoré | reason=consolidation déjà en cours")
        return None
    completed = _MemoryCycleBarrier()

    def _run() -> None:
        from daemon_core.providers.telemetry import llm_telemetry_phase

        succeeded = False
        try:
            with llm_telemetry_phase("after_turn.memory"):
                _run_memory_update(
                    user_id=user_id,
                    user_message=user_message,
                    assistant_response=assistant_response,
                    route_data=route_data,
                    selected_souvenirs=selected_souvenirs,
                )
            succeeded = True
        except Exception as exc:
            logger.warning("Cycle mémoire (fond) échoué | %s", exc.__class__.__name__)
        finally:
            completed.finish(succeeded=succeeded)
            _memory_slot.release()

    try:
        threading.Thread(target=_run, daemon=True, name="openclaw-memory-agent").start()
    except Exception:
        completed.finish(succeeded=False)
        _memory_slot.release()
        raise
    return completed


def _after_turn_trace_id(request) -> str:
    return str(
        getattr(request, "source_trace_id", "")
        or getattr(request, "canary_trace_id", "")
    )


def _has_after_turn_provenance(request) -> bool:
    return bool(
        _after_turn_trace_id(request)
        and getattr(request, "scene_scope", "")
        and float(getattr(request, "source_turn_started_at", 0.0)) > 0
    )


def _revise_scene_model(request, memory_provider=None) -> None:
    from daemon_core.scene_model import (
        schedule_scene_model_revision,
        scene_model_shadow_enabled,
    )

    if (
        not request.library_active
        and not scene_model_shadow_enabled(request.user_id)
    ):
        return
    if memory_provider is None:
        _, _, memory_provider = _providers()
    trace_id = _after_turn_trace_id(request)
    scheduled = schedule_scene_model_revision(
        user_id=request.user_id,
        scene_scope=request.scene_scope,
        provider=memory_provider,
        source_trace_id=trace_id,
        source_turn_started_at=request.source_turn_started_at,
        read_souvenirs=list(getattr(request, "read_souvenirs", ()) or ()),
    )
    logger.info(
        "Scene model OpenClaw planifié | source=%s | scope=%s | accepted=%s",
        trace_id,
        request.scene_scope,
        scheduled,
    )


def _schedule_sovereign_after_turn(request):
    """Shared sovereign ordering: memory, then Scene Model."""

    from daemon_core.after_turn import AfterTurnHooks, complete_turn

    def _spawn(target, name: str) -> None:
        threading.Thread(target=target, daemon=True, name=name).start()

    if not _memory_slot.acquire(blocking=False):
        logger.info("MemoryAgent ignoré | reason=consolidation déjà en cours")
        _observe_continuity(
            _after_turn_trace_id(request),
            "cycle",
            "skipped",
            reason_code="memory_slot_busy",
        )
        return None

    completed = _MemoryCycleBarrier()

    def _run() -> None:
        succeeded = False
        try:
            def _on_error(stage: str, exc: Exception) -> None:
                if stage.startswith("scene"):
                    logger.info(
                        "Scene model OpenClaw ignoré | error=%s",
                        exc.__class__.__name__,
                    )

            def _after_memory(result) -> None:
                try:
                    from daemon_core.paths import MEMORY_ROOT
                    from daemon_core.semantic_atlas import (
                        coalesced_semantic_atlas_prewarmer,
                    )

                    atlas_prewarmer = coalesced_semantic_atlas_prewarmer(
                        request.user_id,
                        MEMORY_ROOT,
                    )
                    if atlas_prewarmer is not None:
                        atlas_prewarmer.process_batch((result,))
                except Exception as exc:
                    logger.warning(
                        "Préchauffage Atlas OpenClaw différé | %s",
                        exc.__class__.__name__,
                    )

            complete_turn(
                request,
                AfterTurnHooks(
                    update_memory=lambda turn: _run_memory_update(
                        user_id=turn.user_id,
                        user_message=turn.user_message,
                        assistant_response=turn.assistant_response,
                        route_data=None,
                        selected_souvenirs=turn.memory_souvenirs,
                    ),
                    revise_working_state=None,
                    revise_scene=(
                        (lambda turn: _revise_scene_model(turn))
                        if _has_after_turn_provenance(request)
                        else None
                    ),
                    after_memory=_after_memory,
                    on_error=_on_error,
                ),
                spawn=_spawn,
            )
            succeeded = True
        except Exception as exc:
            _observe_continuity(
                _after_turn_trace_id(request),
                "cycle",
                "failed",
                reason_code="after_turn_worker_failed",
                error_type=exc.__class__.__name__,
            )
            logger.warning(
                "Cycle mémoire souverain (fond) échoué | %s",
                exc.__class__.__name__,
            )
        finally:
            completed.finish(succeeded=succeeded)
            _memory_slot.release()

    try:
        threading.Thread(
            target=_run,
            daemon=True,
            name="openclaw-sovereign-after-turn",
        ).start()
    except Exception as exc:
        _observe_continuity(
            _after_turn_trace_id(request),
            "cycle",
            "failed",
            reason_code="after_turn_thread_start_failed",
            error_type=exc.__class__.__name__,
        )
        completed.finish(succeeded=False)
        _memory_slot.release()
        raise
    return completed


def _unique_strings(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _safe_append(
    fn,
    user_id: str,
    role: str,
    content: str,
    *,
    source_trace_id: str = "",
    idempotency_key: str = "",
    occurred_at: float = 0.0,
) -> tuple[bool, Any]:
    try:
        result = fn(
            user_id=user_id,
            role=role,
            content=content,
            source_trace_id=source_trace_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )
    except Exception as exc:
        logger.warning("écriture L0 %s échouée | %s", role, exc.__class__.__name__)
        return False, None
    return True, result
