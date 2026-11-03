"""Deferred relation gardening from exact after-turn evidence.

MemoryAgent captures facts and the souvenirs it consulted. This module stores
only those identities, lets the existing Dreaming relation judge inspect a
bounded set later, and exercises the transactional publisher in dry-run mode.
It never runs on the response path and never rewrites souvenir prose itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from daemon_core.memory.graph import (
    MEMBERSHIP_RELATION,
    MemoryGraphIndex,
    build_memory_graph,
    normalize_souvenir_name,
)
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.relation_dreaming import (
    RELATION_DREAMING_SCHEMA,
    RelationDreamingDecision,
    audit_relation_batch,
    parse_relation_dreaming_output,
    validate_relation_dreaming_items,
)
from daemon_core.memory.relation_publication import (
    RelationMutation,
    RelationPublicationError,
    apply_relation_mutation_batch,
    mutation_from_audit_item,
    plan_relation_mutation_batch,
)
from daemon_core.memory.relation_shadow import (
    prepare_memory_relation_shadow_context,
)
from daemon_core.memory.writer import (
    GroundZeroReference,
    read_ground_zero_reference,
    safe_memory_user_id,
)
from daemon_core.paths import MEMORY_ROOT
from daemon_core.providers.base import LLMProvider


RELATION_GARDENING_ENV = "DAEMON_CORE_RELATION_GARDENING"
RELATION_GARDENING_USER_IDS_ENV = (
    "DAEMON_CORE_RELATION_GARDENING_USER_IDS"
)
RELATION_GARDENING_EVENT_SCHEMA = "daemon.relation-gardening-event.v1"
RELATION_GARDENING_DECISION_SCHEMA = "daemon.relation-gardening-decision.v1"
RELATION_GARDENING_RECEIPT_SCHEMA = "daemon.relation-gardening-receipt.v1"
MAX_EVENT_SOUVENIRS = 12
MAX_EVENT_ITEMS = 8
MAX_PENDING_EVENTS_PER_PASS = 8
MAX_USER_EVIDENCE_CHARS = 2_000
MAX_ASSISTANT_CONTEXT_CHARS = 2_000
MAX_SOUVENIR_STATE_CHARS = 4_000
MAX_NEIGHBORS_PER_ITEM = 12
DEFAULT_MEMBERSHIP_ROLE = "member"


@dataclass(frozen=True)
class RelationGardeningCapture:
    status: str
    reason_code: str
    event_id: str = ""


@dataclass(frozen=True)
class RelationGardeningResult:
    status: str
    reason_code: str
    event_id: str
    candidate_count: int = 0
    decision_count: int = 0
    repair_count: int = 0
    planned_count: int = 0
    unchanged_count: int = 0
    blocked_count: int = 0
    provider_calls: int = 0
    replayed: bool = False
    publication_status: str = ""
    publication_reason_code: str = ""
    published_count: int = 0
    publication_unchanged_count: int = 0


@dataclass(frozen=True)
class RelationGardeningPublicationProof:
    """Immutable bridge from a persisted Dreaming decision to publication."""

    event_id: str
    event_sha256: str
    decision_sha256: str
    dry_run_receipt_sha256: str
    graph_fingerprint: str
    mutations: tuple[RelationMutation, ...]
    candidate_count: int
    decision_count: int
    repair_count: int


@dataclass(frozen=True)
class _PreparedGardeningEvent:
    status: str
    reason_code: str
    items: tuple[dict[str, Any], ...] = ()
    source_ref: str = ""
    graph_fingerprint: str = ""
    prepared_sha256: str = ""


def relation_gardening_enabled(user_id: str) -> bool:
    """Enable deferred capture only for one explicitly named identity."""

    mode = os.getenv(RELATION_GARDENING_ENV, "off").strip().casefold()
    if mode not in {"dry-run", "active"}:
        return False
    allowed = {
        value.strip()
        for value in os.getenv(RELATION_GARDENING_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return bool(
        len(allowed) == 1
        and "*" not in allowed
        and str(user_id).strip() in allowed
    )


def relation_gardening_event_path(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    return _gardening_path(
        user_id,
        "events",
        event_id,
        memory_root=memory_root,
    )


def relation_gardening_decision_path(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    return _gardening_path(
        user_id,
        "decisions",
        event_id,
        memory_root=memory_root,
    )


def relation_gardening_receipt_path(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    return _gardening_path(
        user_id,
        "receipts",
        event_id,
        memory_root=memory_root,
    )


def record_relation_gardening_event(
    *,
    user_id: str,
    source_trace_id: str,
    source_turn_started_at: float,
    user_l0: GroundZeroReference | dict,
    assistant_l0: GroundZeroReference | dict,
    source_posture: str,
    changed_souvenirs: Iterable[str],
    context_souvenirs: Iterable[str],
    memory_root: Path | None = None,
    require_enabled: bool = True,
) -> RelationGardeningCapture:
    """Persist one exact after-turn identity without copying semantic content."""

    if require_enabled and not relation_gardening_enabled(user_id):
        return RelationGardeningCapture("skipped", "dry_run_disabled")
    trace_id = str(source_trace_id).strip()
    if not str(user_id).strip() or not trace_id:
        raise ValueError("relation gardening requires user and turn identity")
    event_id = _event_id(user_id, trace_id)
    path = relation_gardening_event_path(
        user_id,
        event_id,
        memory_root=memory_root,
    )
    with memory_file_lock(path):
        existing = _read_json(path)
        if existing is not None:
            _validate_event(
                existing,
                user_id=user_id,
                expected_event_id=event_id,
            )
            path.chmod(0o600)
            return RelationGardeningCapture(
                "duplicate",
                "event_already_recorded",
                event_id,
            )

        user_ref = _reference(user_l0, role="user", trace_id=trace_id)
        assistant_ref = _reference(
            assistant_l0,
            role="assistant",
            trace_id=trace_id,
        )
        posture = str(source_posture).strip() or "none"
        if posture not in {"direct_fact", "reported_memory_claim", "none"}:
            raise ValueError("invalid relation gardening source posture")
        changed = _souvenir_names(changed_souvenirs)
        context = _souvenir_names((*changed, *tuple(context_souvenirs)))
        if not context:
            return RelationGardeningCapture("skipped", "no_context_capsules")
        if len(context) > MAX_EVENT_SOUVENIRS:
            return RelationGardeningCapture(
                "skipped",
                "context_capsule_overflow",
            )
        payload = {
            "schema": RELATION_GARDENING_EVENT_SCHEMA,
            "event_id": event_id,
            "source_trace_id": trace_id,
            "source_turn_started_at": max(
                0.0,
                float(source_turn_started_at),
            ),
            "user_l0": user_ref.as_dict(),
            "assistant_l0": assistant_ref.as_dict(),
            "source_posture": posture,
            "changed_capsules": list(changed),
            "context_capsules": list(context),
        }
        atomic_write_text(path, _json_line(payload))
        path.chmod(0o600)
    return RelationGardeningCapture("created", "event_recorded", event_id)


def read_relation_gardening_event(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> dict[str, Any]:
    path = relation_gardening_event_path(
        user_id,
        event_id,
        memory_root=memory_root,
    )
    payload = _read_json(path)
    if payload is None:
        raise FileNotFoundError(path)
    _validate_event(
        payload,
        user_id=user_id,
        expected_event_id=event_id,
    )
    return payload


def pending_relation_gardening_event_ids(
    user_id: str,
    *,
    memory_root: Path | None = None,
    limit: int = MAX_PENDING_EVENTS_PER_PASS,
) -> tuple[str, ...]:
    bounded_limit = max(0, int(limit))
    if bounded_limit == 0:
        return ()
    root = _root(memory_root)
    base = (
        root
        / "levels"
        / "relation_gardening"
        / safe_memory_user_id(user_id)
    )
    event_root = base / "events"
    receipt_root = base / "receipts"
    pending = []
    for path in sorted(event_root.glob("*.json")):
        event_id = path.stem
        if not (receipt_root / path.name).is_file():
            pending.append(event_id)
        if len(pending) >= bounded_limit:
            break
    return tuple(pending)


def run_pending_relation_gardening_dry_run(
    *,
    user_id: str,
    provider: LLMProvider,
    memory_root: Path | None = None,
    limit: int = MAX_PENDING_EVENTS_PER_PASS,
    require_enabled: bool = True,
) -> tuple[RelationGardeningResult, ...]:
    if require_enabled and not relation_gardening_enabled(user_id):
        return ()
    return tuple(
        run_relation_gardening_event_dry_run(
            user_id=user_id,
            event_id=event_id,
            provider=provider,
            memory_root=memory_root,
        )
        for event_id in pending_relation_gardening_event_ids(
            user_id,
            memory_root=memory_root,
            limit=limit,
        )
    )


def run_relation_gardening_event_dry_run(
    *,
    user_id: str,
    event_id: str,
    provider: LLMProvider,
    memory_root: Path | None = None,
) -> RelationGardeningResult:
    """Judge one complete event and persist an idempotent dry-run receipt."""

    root = _root(memory_root)
    event = read_relation_gardening_event(
        user_id,
        event_id,
        memory_root=root,
    )
    receipt_path = relation_gardening_receipt_path(
        user_id,
        event_id,
        memory_root=root,
    )
    with memory_file_lock(receipt_path):
        existing = _read_json(receipt_path)
        if existing is not None:
            _validate_receipt(existing, event=event)
            receipt_path.chmod(0o600)
            return _result_from_receipt(
                existing,
                provider_calls=0,
                replayed=True,
            )

        prepared = _prepare_event(event, memory_root=root)
        if not prepared.items:
            payload = _receipt_payload(
                event=event,
                status=prepared.status,
                reason_code=prepared.reason_code,
                graph_fingerprint=prepared.graph_fingerprint,
            )
            atomic_write_text(receipt_path, _json_line(payload))
            receipt_path.chmod(0o600)
            return _result_from_receipt(
                payload,
                provider_calls=0,
                replayed=False,
            )

        decision_path = relation_gardening_decision_path(
            user_id,
            event_id,
            memory_root=root,
        )
        decision_payload = _read_json(decision_path)
        provider_calls = 0
        if decision_payload is None:
            audit = audit_relation_batch(prepared.items, provider=provider)
            decisions = audit.decisions
            provider_calls = 1
            decision_payload = _decision_payload(
                event=event,
                prepared=prepared,
                decisions=decisions,
            )
            with memory_file_lock(decision_path):
                prior = _read_json(decision_path)
                if prior is not None:
                    _validate_decision(
                        prior,
                        event=event,
                        prepared=prepared,
                    )
                    if prior != decision_payload:
                        raise ValueError(
                            "relation gardening decision identity conflict"
                        )
                else:
                    atomic_write_text(decision_path, _json_line(decision_payload))
                decision_path.chmod(0o600)
        else:
            decisions = _validate_decision(
                decision_payload,
                event=event,
                prepared=prepared,
            )

        latest_fingerprint = build_memory_graph(
            root / "capsules",
            memory_root=root,
        ).index.library_fingerprint
        if latest_fingerprint != prepared.graph_fingerprint:
            payload = _receipt_payload(
                event=event,
                status="blocked",
                reason_code="graph_changed_after_decision",
                graph_fingerprint=latest_fingerprint,
                decisions=decisions,
                outcomes=[],
            )
        else:
            payload = _plan_decisions(
                event=event,
                prepared=prepared,
                decisions=decisions,
                memory_root=root,
            )
        atomic_write_text(receipt_path, _json_line(payload))
        receipt_path.chmod(0o600)
    return _result_from_receipt(
        payload,
        provider_calls=provider_calls,
        replayed=False,
    )


def prepare_relation_gardening_publication(
    *,
    user_id: str,
    event_id: str,
    memory_root: Path | None = None,
) -> RelationGardeningPublicationProof:
    """Bind one persisted dry-run decision to exact publication mutations."""

    root = _root(memory_root)
    try:
        event = read_relation_gardening_event(
            user_id,
            event_id,
            memory_root=root,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise RelationPublicationError(
            "The relation gardening event is unavailable or invalid.",
            reason_code="gardening_event_invalid",
        ) from exc
    receipt = _read_json(
        relation_gardening_receipt_path(
            user_id,
            event_id,
            memory_root=root,
        )
    )
    if receipt is None:
        raise RelationPublicationError(
            "The relation gardening dry-run receipt is missing.",
            reason_code="dry_run_receipt_missing",
        )
    try:
        _validate_receipt(receipt, event=event)
    except ValueError as exc:
        raise RelationPublicationError(
            "The relation gardening dry-run receipt is invalid.",
            reason_code="dry_run_receipt_invalid",
        ) from exc

    prepared = _prepare_event(
        event,
        memory_root=root,
        expected_graph_fingerprint=str(receipt.get("graph_fingerprint", "")),
    )
    if prepared.status != "ready" or not prepared.items:
        raise RelationPublicationError(
            "The relation gardening event is no longer publishable.",
            reason_code=prepared.reason_code or "gardening_event_not_ready",
        )
    decision = _read_json(
        relation_gardening_decision_path(
            user_id,
            event_id,
            memory_root=root,
        )
    )
    if decision is None:
        raise RelationPublicationError(
            "The persisted relation gardening decision is missing.",
            reason_code="gardening_decision_missing",
        )
    try:
        decisions = _validate_decision(
            decision,
            event=event,
            prepared=prepared,
        )
    except ValueError as exc:
        raise RelationPublicationError(
            "The persisted relation gardening decision is invalid.",
            reason_code="gardening_decision_invalid",
        ) from exc

    latest_fingerprint = build_memory_graph(
        root / "capsules",
        memory_root=root,
    ).index.library_fingerprint
    if latest_fingerprint != prepared.graph_fingerprint:
        raise RelationPublicationError(
            "The memory graph changed after the gardening decision.",
            reason_code="graph_changed_after_decision",
        )

    by_id = {str(item["id"]): item for item in prepared.items}
    outcomes = receipt.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != len(decisions):
        raise RelationPublicationError(
            "The dry-run outcomes do not cover every gardening decision.",
            reason_code="dry_run_outcome_mismatch",
        )
    outcomes_by_id = {
        str(item.get("item_id", "")): item
        for item in outcomes
        if isinstance(item, dict)
    }
    if len(outcomes_by_id) != len(outcomes):
        raise RelationPublicationError(
            "The dry-run outcomes have invalid identities.",
            reason_code="dry_run_outcome_mismatch",
        )

    mutations = []
    repair_count = 0
    for item_decision in decisions:
        item = by_id[item_decision.item_id]
        outcome = outcomes_by_id.get(item_decision.item_id)
        if outcome is None or any(
            (
                outcome.get("disposition") != item_decision.disposition,
                outcome.get("action") != item_decision.action,
                outcome.get("source_ref") != _technical_ref(str(item["source"])),
                outcome.get("target_ref") != _technical_ref(str(item["target"])),
            )
        ):
            raise RelationPublicationError(
                "A dry-run outcome differs from its persisted decision.",
                reason_code="dry_run_outcome_mismatch",
            )
        mutation = mutation_from_audit_item(
            item,
            item_decision.as_dict(),
            source_ref=prepared.source_ref,
        )
        if mutation is None:
            if outcome.get("status") != "observed":
                raise RelationPublicationError(
                    "A non-mutating decision has a publishing outcome.",
                    reason_code="dry_run_outcome_mismatch",
                )
            continue
        if outcome.get("status") not in {"planned", "unchanged"}:
            raise RelationPublicationError(
                "A mutation was not accepted by the dry-run publisher.",
                reason_code="dry_run_mutation_not_accepted",
            )
        mutations.append(mutation)
        repair_count += int(item_decision.disposition == "repair")

    planned_count = sum(outcome.get("status") == "planned" for outcome in outcomes)
    unchanged_count = sum(outcome.get("status") == "unchanged" for outcome in outcomes)
    expected_counts = {
        "candidate_count": len(prepared.items),
        "decision_count": len(decisions),
        "repair_count": repair_count,
        "planned_count": planned_count,
        "unchanged_count": unchanged_count,
        "blocked_count": 0,
    }
    try:
        summary_matches = all(
            int(receipt.get(name, -1)) == expected
            for name, expected in expected_counts.items()
        )
    except (TypeError, ValueError):
        summary_matches = False
    if (
        receipt.get("status") not in {"planned", "unchanged"}
        or receipt.get("graph_fingerprint") != prepared.graph_fingerprint
        or not summary_matches
    ):
        raise RelationPublicationError(
            "The dry-run receipt summary differs from its decisions.",
            reason_code="dry_run_summary_mismatch",
        )
    return RelationGardeningPublicationProof(
        event_id=str(event["event_id"]),
        event_sha256=_payload_sha256(event),
        decision_sha256=_payload_sha256(decision),
        dry_run_receipt_sha256=_payload_sha256(receipt),
        graph_fingerprint=prepared.graph_fingerprint,
        mutations=tuple(mutations),
        candidate_count=len(prepared.items),
        decision_count=len(decisions),
        repair_count=repair_count,
    )


def _prepare_event(
    event: dict[str, Any],
    *,
    memory_root: Path,
    expected_graph_fingerprint: str = "",
) -> _PreparedGardeningEvent:
    try:
        user_ref = GroundZeroReference.from_dict(event["user_l0"])
        assistant_ref = GroundZeroReference.from_dict(event["assistant_l0"])
        user_evidence = read_ground_zero_reference(
            user_ref,
            memory_root=memory_root,
        )
        assistant_context = read_ground_zero_reference(
            assistant_ref,
            memory_root=memory_root,
        )
        context = prepare_memory_relation_shadow_context(
            current_user_l0=user_ref,
            user_message=user_evidence,
            memory_root=memory_root,
        )
    except (FileNotFoundError, OSError, TypeError, UnicodeDecodeError, ValueError):
        return _PreparedGardeningEvent(
            status="blocked",
            reason_code="l0_provenance_invalid",
        )
    if context is None or context.source_trace_id != event["source_trace_id"]:
        return _PreparedGardeningEvent(
            status="blocked",
            reason_code="l0_provenance_invalid",
        )
    if event["source_posture"] != "direct_fact":
        return _PreparedGardeningEvent(
            status="skipped",
            reason_code="source_not_direct_fact",
            source_ref=context.source_ref,
        )
    if not user_evidence.strip() or len(user_evidence) > MAX_USER_EVIDENCE_CHARS:
        return _PreparedGardeningEvent(
            status="skipped",
            reason_code="user_evidence_out_of_bounds",
            source_ref=context.source_ref,
        )

    graph = build_memory_graph(
        memory_root / "capsules",
        memory_root=memory_root,
    ).index
    if (
        expected_graph_fingerprint
        and graph.library_fingerprint != expected_graph_fingerprint
    ):
        return _PreparedGardeningEvent(
            status="blocked",
            reason_code="graph_changed_after_decision",
            source_ref=context.source_ref,
            graph_fingerprint=graph.library_fingerprint,
        )
    names = tuple(event["context_capsules"])
    contents: dict[str, str] = {}
    for name in names:
        if name not in graph.souvenirs:
            return _PreparedGardeningEvent(
                status="blocked",
                reason_code="context_capsule_missing",
                source_ref=context.source_ref,
                graph_fingerprint=graph.library_fingerprint,
            )
        try:
            content = _read_souvenir(memory_root / "capsules", name)
        except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
            return _PreparedGardeningEvent(
                status="blocked",
                reason_code="context_capsule_unreadable",
                source_ref=context.source_ref,
                graph_fingerprint=graph.library_fingerprint,
            )
        state = _render_souvenir_state(name, content, graph=graph)
        if len(state) > MAX_SOUVENIR_STATE_CHARS:
            return _PreparedGardeningEvent(
                status="blocked",
                reason_code="capsule_state_out_of_bounds",
                source_ref=context.source_ref,
                graph_fingerprint=graph.library_fingerprint,
            )
        contents[name] = content

    centers = tuple(
        name
        for name in names
        if graph.souvenirs[name].collection is not None
        and graph.souvenirs[name].collection.coverage == "open"
    )
    if not centers:
        return _PreparedGardeningEvent(
            status="skipped",
            reason_code="no_open_collection_in_context",
            source_ref=context.source_ref,
            graph_fingerprint=graph.library_fingerprint,
        )
    pairs = tuple(
        (source, target)
        for target in centers
        for source in names
        if source != target
    )
    if not pairs:
        return _PreparedGardeningEvent(
            status="skipped",
            reason_code="no_relation_candidate",
            source_ref=context.source_ref,
            graph_fingerprint=graph.library_fingerprint,
        )
    if len(pairs) > MAX_EVENT_ITEMS:
        return _PreparedGardeningEvent(
            status="blocked",
            reason_code="relation_candidate_overflow",
            source_ref=context.source_ref,
            graph_fingerprint=graph.library_fingerprint,
        )

    items = []
    for source, target in pairs:
        record = graph.souvenirs[source]
        membership = next(
            (
                relation
                for relation in record.relations
                if relation.kind == MEMBERSHIP_RELATION
                and relation.target == target
            ),
            None,
        )
        item_id = hashlib.sha256(
            f"{event['event_id']}\0{source}\0{target}".encode("utf-8")
        ).hexdigest()[:24]
        items.append(
            {
                "id": item_id,
                "mode": "maintenance",
                "source_posture": "direct_fact",
                "proposed_action": "none",
                "existing_state": "present" if membership else "absent",
                "source": source,
                "target": target,
                "role": (
                    membership.description
                    if membership and membership.description
                    else DEFAULT_MEMBERSHIP_ROLE
                ),
                "user_evidence": user_evidence,
                "assistant_context": _bounded_text(
                    assistant_context,
                    MAX_ASSISTANT_CONTEXT_CHARS,
                ),
                "source_state": _render_souvenir_state(
                    source,
                    contents[source],
                    graph=graph,
                ),
                "target_state": _render_souvenir_state(
                    target,
                    contents[target],
                    graph=graph,
                ),
                "existing_relations": _existing_relations(graph, source),
                "neighbors": _one_hop_neighbors(
                    graph,
                    source=source,
                    target=target,
                ),
            }
        )
    bounded = validate_relation_dreaming_items(items)
    prepared_sha256 = _payload_sha256(
        {
            "source_ref": context.source_ref,
            "graph_fingerprint": graph.library_fingerprint,
            "items": bounded,
        }
    )
    return _PreparedGardeningEvent(
        status="ready",
        reason_code="bounded_event_ready",
        items=bounded,
        source_ref=context.source_ref,
        graph_fingerprint=graph.library_fingerprint,
        prepared_sha256=prepared_sha256,
    )


def _plan_decisions(
    *,
    event: dict[str, Any],
    prepared: _PreparedGardeningEvent,
    decisions: tuple[RelationDreamingDecision, ...],
    memory_root: Path,
) -> dict[str, Any]:
    by_id = {str(item["id"]): item for item in prepared.items}
    outcomes_by_id: dict[str, dict[str, Any]] = {}
    pending = []
    preflight_blocked = False
    for decision in decisions:
        item = by_id[decision.item_id]
        outcome = {
            "item_id": decision.item_id,
            "disposition": decision.disposition,
            "action": decision.action,
            "source_ref": _technical_ref(str(item["source"])),
            "target_ref": _technical_ref(str(item["target"])),
        }
        try:
            mutation = mutation_from_audit_item(
                item,
                decision.as_dict(),
                source_ref=prepared.source_ref,
            )
            if mutation is None:
                outcomes_by_id[decision.item_id] = {
                    **outcome,
                    "status": "observed",
                    "reason_code": "no_mutation_requested",
                }
                continue
            pending.append((decision.item_id, outcome, mutation))
        except RelationPublicationError as exc:
            outcomes_by_id[decision.item_id] = {
                **outcome,
                "status": "blocked",
                "reason_code": exc.reason_code,
            }
            preflight_blocked = True

    if pending and preflight_blocked:
        for item_id, outcome, _mutation in pending:
            outcomes_by_id[item_id] = {
                **outcome,
                "status": "blocked",
                "reason_code": "relation_batch_preflight_blocked",
            }
    elif pending:
        try:
            plan = plan_relation_mutation_batch(
                souvenirs_root=memory_root / "capsules",
                memory_root=memory_root,
                mutations=tuple(
                    mutation for _item_id, _outcome, mutation in pending
                ),
            )
            publication = apply_relation_mutation_batch(
                plan,
                souvenirs_root=memory_root / "capsules",
                memory_root=memory_root,
                index_path=memory_root / "levels" / "memory_graph.json",
                dry_run=True,
            )
        except RelationPublicationError as exc:
            for item_id, outcome, _mutation in pending:
                outcomes_by_id[item_id] = {
                    **outcome,
                    "status": "blocked",
                    "reason_code": exc.reason_code,
                }
        else:
            for (item_id, outcome, _mutation), result in zip(
                pending,
                publication.items,
                strict=True,
            ):
                outcomes_by_id[item_id] = {
                    **outcome,
                    "status": result.status,
                    "reason_code": "",
                }

    outcomes = [outcomes_by_id[decision.item_id] for decision in decisions]

    blocked = [item for item in outcomes if item["status"] == "blocked"]
    planned = [item for item in outcomes if item["status"] == "planned"]
    unchanged = [item for item in outcomes if item["status"] == "unchanged"]
    repairs = [
        item
        for item in outcomes
        if item["disposition"] == "repair"
        and item["action"] in {"upsert", "remove"}
    ]
    if blocked:
        status = "blocked"
        reason_code = "relation_plan_blocked"
    elif planned:
        status = "planned"
        reason_code = "dry_run_planned"
    elif unchanged:
        status = "unchanged"
        reason_code = "relations_already_satisfied"
    else:
        status = "observed"
        reason_code = "no_relation_repair"
    return _receipt_payload(
        event=event,
        status=status,
        reason_code=reason_code,
        graph_fingerprint=prepared.graph_fingerprint,
        decisions=decisions,
        outcomes=outcomes,
        repair_count=len(repairs),
    )


def _decision_payload(
    *,
    event: dict[str, Any],
    prepared: _PreparedGardeningEvent,
    decisions: tuple[RelationDreamingDecision, ...],
) -> dict[str, Any]:
    return {
        "schema": RELATION_GARDENING_DECISION_SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_id": str(event["event_id"]),
        "event_sha256": _payload_sha256(event),
        "prepared_sha256": prepared.prepared_sha256,
        "graph_fingerprint": prepared.graph_fingerprint,
        "source_ref": prepared.source_ref,
        "decisions": [decision.as_dict() for decision in decisions],
    }


def _validate_decision(
    payload: dict[str, Any],
    *,
    event: dict[str, Any],
    prepared: _PreparedGardeningEvent,
) -> tuple[RelationDreamingDecision, ...]:
    if payload.get("schema") != RELATION_GARDENING_DECISION_SCHEMA:
        raise ValueError("unsupported relation gardening decision schema")
    if payload.get("event_id") != event.get("event_id"):
        raise ValueError("relation gardening decision event mismatch")
    if payload.get("event_sha256") != _payload_sha256(event):
        raise ValueError("relation gardening decision event changed")
    if payload.get("prepared_sha256") != prepared.prepared_sha256:
        raise ValueError("relation gardening decision context changed")
    if payload.get("graph_fingerprint") != prepared.graph_fingerprint:
        raise ValueError("relation gardening decision graph changed")
    if payload.get("source_ref") != prepared.source_ref:
        raise ValueError("relation gardening decision provenance changed")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("relation gardening decision list is missing")
    return parse_relation_dreaming_output(
        json.dumps(
            {
                "schema": RELATION_DREAMING_SCHEMA,
                "decisions": decisions,
            }
        ),
        items=prepared.items,
    )


def _receipt_payload(
    *,
    event: dict[str, Any],
    status: str,
    reason_code: str,
    graph_fingerprint: str = "",
    decisions: tuple[RelationDreamingDecision, ...] = (),
    outcomes: list[dict[str, Any]] | None = None,
    repair_count: int = 0,
) -> dict[str, Any]:
    values = outcomes or []
    return {
        "schema": RELATION_GARDENING_RECEIPT_SCHEMA,
        "mode": "dry-run",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_id": str(event["event_id"]),
        "event_sha256": _payload_sha256(event),
        "source_trace_ref": _technical_ref(str(event["source_trace_id"])),
        "status": str(status),
        "reason_code": str(reason_code)[:160],
        "graph_fingerprint": str(graph_fingerprint)[:128],
        "candidate_count": len(decisions) if decisions else 0,
        "decision_count": len(decisions),
        "repair_count": max(0, int(repair_count)),
        "planned_count": sum(item.get("status") == "planned" for item in values),
        "unchanged_count": sum(
            item.get("status") == "unchanged" for item in values
        ),
        "blocked_count": sum(item.get("status") == "blocked" for item in values),
        "outcomes": values,
    }


def _validate_receipt(
    payload: dict[str, Any],
    *,
    event: dict[str, Any],
) -> None:
    if payload.get("schema") != RELATION_GARDENING_RECEIPT_SCHEMA:
        raise ValueError("unsupported relation gardening receipt schema")
    if payload.get("mode") != "dry-run":
        raise ValueError("relation gardening receipt is not dry-run")
    if payload.get("event_id") != event.get("event_id"):
        raise ValueError("relation gardening receipt event mismatch")
    if payload.get("event_sha256") != _payload_sha256(event):
        raise ValueError("relation gardening receipt event changed")


def _result_from_receipt(
    payload: dict[str, Any],
    *,
    provider_calls: int,
    replayed: bool,
) -> RelationGardeningResult:
    return RelationGardeningResult(
        status=str(payload.get("status", "")),
        reason_code=str(payload.get("reason_code", "")),
        event_id=str(payload.get("event_id", "")),
        candidate_count=max(0, int(payload.get("candidate_count", 0))),
        decision_count=max(0, int(payload.get("decision_count", 0))),
        repair_count=max(0, int(payload.get("repair_count", 0))),
        planned_count=max(0, int(payload.get("planned_count", 0))),
        unchanged_count=max(0, int(payload.get("unchanged_count", 0))),
        blocked_count=max(0, int(payload.get("blocked_count", 0))),
        provider_calls=max(0, int(provider_calls)),
        replayed=replayed,
    )


def _render_souvenir_state(
    name: str,
    content: str,
    *,
    graph: MemoryGraphIndex,
) -> str:
    lines = [f"Souvenir: {name}"]
    record = graph.souvenirs[name]
    if record.collection is not None:
        lines.append(f"Collection: {record.collection.coverage}")
    in_facts = False
    for raw in str(content).splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            in_facts = stripped.casefold() == "## faits"
            continue
        if stripped.startswith(
            ("Sujet:", "Portee:", "Portée:", "Hors portee:", "Hors portée:")
        ):
            lines.append(stripped)
        elif in_facts and stripped.startswith("- "):
            lines.append(stripped)
    return "\n".join(lines)


def _existing_relations(graph: MemoryGraphIndex, source: str) -> list[str]:
    return [
        f"{relation.kind} {relation.target}: {relation.description}".strip()
        for relation in graph.souvenirs[source].relations
    ]


def _one_hop_neighbors(
    graph: MemoryGraphIndex,
    *,
    source: str,
    target: str,
) -> list[dict[str, str]]:
    summaries: dict[str, list[str]] = {}
    for owner, record in graph.souvenirs.items():
        for relation in record.relations:
            if relation.source in {source, target}:
                summaries.setdefault(relation.target, []).append(
                    f"{relation.kind}: {relation.description or 'relation existante'}"
                )
            if relation.target in {source, target}:
                summaries.setdefault(owner, []).append(
                    f"{relation.kind}: {relation.description or 'relation existante'}"
                )
    summaries.pop(source, None)
    summaries.pop(target, None)
    return [
        {
            "name": name,
            "summary": "; ".join(dict.fromkeys(summaries[name]))[:1_500],
        }
        for name in sorted(summaries)[:MAX_NEIGHBORS_PER_ITEM]
    ]


def _read_souvenir(souvenirs_root: Path, name: str) -> str:
    from daemon_core.memory.library_layout import vault_root, resolve_note
    root = souvenirs_root.resolve()
    if vault_root(root.parent) is not None:
        return resolve_note(root.parent, name).read_text(encoding="utf-8")
    path = (root / f"{name}.md").resolve()
    path.relative_to(root)
    return path.read_text(encoding="utf-8")


def _gardening_path(
    user_id: str,
    bucket: str,
    event_id: str,
    *,
    memory_root: Path | None,
) -> Path:
    return (
        _root(memory_root)
        / "levels"
        / "relation_gardening"
        / safe_memory_user_id(user_id)
        / bucket
        / f"{_event_filename(event_id)}.json"
    )


def _root(memory_root: Path | None) -> Path:
    return (MEMORY_ROOT if memory_root is None else Path(memory_root)).resolve()


def _event_id(user_id: str, trace_id: str) -> str:
    return hashlib.sha256(
        f"{user_id}\0{trace_id}\0{RELATION_GARDENING_EVENT_SCHEMA}".encode(
            "utf-8"
        )
    ).hexdigest()


def _event_filename(event_id: str) -> str:
    value = str(event_id).strip().casefold()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("invalid relation gardening event id")
    return value


def _souvenir_names(values: Iterable[str]) -> tuple[str, ...]:
    names = []
    for raw in values:
        value = str(raw).strip().replace("\\", "/")
        normalized = normalize_souvenir_name(value)
        if normalized is None or normalized != value:
            raise ValueError("invalid relation gardening souvenir name")
        if normalized not in names:
            names.append(normalized)
    return tuple(names)


def _reference(
    value: GroundZeroReference | dict,
    *,
    role: str,
    trace_id: str,
) -> GroundZeroReference:
    reference = (
        value
        if isinstance(value, GroundZeroReference)
        else GroundZeroReference.from_dict(value)
    )
    if reference.role != role:
        raise ValueError("relation gardening L0 reference has wrong role")
    if reference.turn_id and reference.turn_id != trace_id:
        raise ValueError("relation gardening L0 reference belongs to another turn")
    if (
        not reference.path
        or reference.offset < 0
        or reference.length <= 0
        or not reference.sha256
    ):
        raise ValueError("relation gardening L0 reference is incomplete")
    return reference


def _validate_event(
    payload: dict[str, Any],
    *,
    user_id: str,
    expected_event_id: str,
) -> None:
    if payload.get("schema") != RELATION_GARDENING_EVENT_SCHEMA:
        raise ValueError("unsupported relation gardening event schema")
    trace_id = str(payload.get("source_trace_id", "")).strip()
    event_id = str(payload.get("event_id", "")).strip()
    if not trace_id or event_id != _event_id(user_id, trace_id):
        raise ValueError("invalid relation gardening event identity")
    if event_id != _event_filename(expected_event_id):
        raise ValueError("relation gardening event filename mismatch")
    _reference(payload.get("user_l0", {}), role="user", trace_id=trace_id)
    _reference(payload.get("assistant_l0", {}), role="assistant", trace_id=trace_id)
    if payload.get("source_posture") not in {
        "direct_fact",
        "reported_memory_claim",
        "none",
    }:
        raise ValueError("invalid relation gardening source posture")
    changed = _souvenir_names(payload.get("changed_capsules") or ())
    context = _souvenir_names(payload.get("context_capsules") or ())
    if list(changed) != payload.get("changed_capsules"):
        raise ValueError("invalid relation gardening changed souvenirs")
    if list(context) != payload.get("context_capsules") or not context:
        raise ValueError("invalid relation gardening context souvenirs")
    if len(context) > MAX_EVENT_SOUVENIRS or any(
        name not in context for name in changed
    ):
        raise ValueError("invalid relation gardening event bounds")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError("relation gardening payload must be an object")
    return payload


def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _technical_ref(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _bounded_text(value: object, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) <= maximum:
        return text
    return text[: maximum - 3].rstrip() + "..."


__all__ = [
    "RELATION_GARDENING_DECISION_SCHEMA",
    "RELATION_GARDENING_ENV",
    "RELATION_GARDENING_EVENT_SCHEMA",
    "RELATION_GARDENING_RECEIPT_SCHEMA",
    "RELATION_GARDENING_USER_IDS_ENV",
    "RelationGardeningCapture",
    "RelationGardeningPublicationProof",
    "RelationGardeningResult",
    "pending_relation_gardening_event_ids",
    "prepare_relation_gardening_publication",
    "read_relation_gardening_event",
    "record_relation_gardening_event",
    "relation_gardening_decision_path",
    "relation_gardening_enabled",
    "relation_gardening_event_path",
    "relation_gardening_receipt_path",
    "run_pending_relation_gardening_dry_run",
    "run_relation_gardening_event_dry_run",
]
