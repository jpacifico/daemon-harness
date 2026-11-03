"""Bounded active publication of persisted Dreaming gardening events.

Dreaming has already made and persisted the semantic decision. This module
records the exact technical intent and publishes that immutable batch through
the canonical Markdown publisher. The original canary admits one event and
pair; personal production admits post-activation monotone batches for one
explicit identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.relation_gardening import (
    RELATION_GARDENING_DECISION_SCHEMA,
    RELATION_GARDENING_ENV,
    RELATION_GARDENING_RECEIPT_SCHEMA,
    RELATION_GARDENING_USER_IDS_ENV,
    RelationGardeningResult,
    RelationGardeningPublicationProof,
    prepare_relation_gardening_publication,
    read_relation_gardening_event,
    relation_gardening_decision_path,
    relation_gardening_receipt_path,
    run_relation_gardening_event_dry_run,
)
from daemon_core.memory.relation_publication import (
    RelationMutation,
    RelationMutationBatchPlan,
    RelationPublicationError,
    apply_relation_mutation_batch,
    plan_relation_mutation_batch,
)
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT
from daemon_core.providers.base import LLMProvider


RELATION_GARDENING_ACTIVE_EVENT_IDS_ENV = (
    "DAEMON_CORE_RELATION_GARDENING_EVENT_IDS"
)
RELATION_GARDENING_ACTIVE_INTENT_SCHEMA = (
    "daemon.relation-gardening-publication-intent.v1"
)
RELATION_GARDENING_ACTIVE_RECEIPT_SCHEMA = (
    "daemon.relation-gardening-publication-canary.v1"
)
RELATION_GARDENING_PERSONAL_ACTIVATION_SCHEMA = (
    "daemon.relation-gardening-personal-activation.v1"
)
RELATION_GARDENING_PERSONAL_RECEIPT_SCHEMA = (
    "daemon.relation-gardening-publication-personal.v1"
)
MAX_PERSONAL_EVENTS_PER_PASS = 8
MAX_PERSONAL_MUTATIONS_PER_EVENT = 8


@dataclass(frozen=True)
class RelationGardeningPublicationCanaryResult:
    status: str
    reason_code: str
    event_id: str
    published_count: int = 0
    unchanged_count: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class RelationGardeningPersonalPublicationResult:
    status: str
    reason_code: str
    event_id: str
    published_count: int = 0
    unchanged_count: int = 0
    replayed: bool = False


def relation_gardening_publication_canary_enabled(
    user_id: str,
    event_id: str,
) -> bool:
    """Return true only for one explicit identity and exact event."""

    if os.getenv(RELATION_GARDENING_ENV, "off").strip().casefold() != "canary":
        return False
    allowed_user = _single_explicit_value(RELATION_GARDENING_USER_IDS_ENV)
    allowed_event = _single_explicit_value(RELATION_GARDENING_ACTIVE_EVENT_IDS_ENV)
    canonical_event = _canonical_event_id(event_id)
    return (
        allowed_user is not None
        and allowed_user == str(user_id).strip()
        and allowed_event is not None
        and allowed_event == canonical_event
    )


def relation_gardening_personal_publication_enabled(user_id: str) -> bool:
    """Return true only for the single identity named in active mode."""

    if os.getenv(RELATION_GARDENING_ENV, "off").strip().casefold() != "active":
        return False
    allowed_user = _single_explicit_value(RELATION_GARDENING_USER_IDS_ENV)
    return allowed_user is not None and allowed_user == str(user_id).strip()


def relation_gardening_personal_activation_path(
    user_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    return (
        _root(memory_root)
        / "levels"
        / "relation_gardening"
        / safe_memory_user_id(user_id)
        / "personal_activation.json"
    )


def activate_personal_relation_gardening(
    *,
    user_id: str,
    activated_at: float | None = None,
    memory_root: Path | None = None,
) -> dict[str, Any]:
    """Create the private boundary after which events may be published."""

    if not relation_gardening_personal_publication_enabled(user_id):
        raise RelationPublicationError(
            "Personal relation gardening is not enabled for this identity.",
            reason_code="personal_publication_not_enabled",
        )
    timestamp = time.time() if activated_at is None else float(activated_at)
    if timestamp <= 0:
        raise RelationPublicationError(
            "The personal activation timestamp is invalid.",
            reason_code="personal_activation_invalid",
        )
    path = relation_gardening_personal_activation_path(
        user_id,
        memory_root=memory_root,
    )
    payload = {
        "schema": RELATION_GARDENING_PERSONAL_ACTIVATION_SCHEMA,
        "mode": "active",
        "user_ref": _technical_ref(user_id),
        "activated_at": timestamp,
        "activated_at_iso": datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).isoformat(timespec="seconds"),
    }
    with memory_file_lock(path):
        existing = _read_json(path)
        if existing is not None:
            _validate_personal_activation(existing, user_id=user_id)
            path.chmod(0o600)
            return existing
        atomic_write_text(path, _json_line(payload))
        path.chmod(0o600)
    return payload


def relation_gardening_publication_intent_path(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    return _publication_path(
        user_id,
        "publication_intents",
        event_id,
        memory_root=memory_root,
    )


def relation_gardening_publication_receipt_path(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    return _publication_path(
        user_id,
        "publication_receipts",
        event_id,
        memory_root=memory_root,
    )


def run_relation_gardening_publication_canary(
    *,
    user_id: str,
    event_id: str,
    expected_source: str,
    expected_target: str,
    memory_root: Path | None = None,
) -> RelationGardeningPublicationCanaryResult:
    """Publish one pre-authorized gardening mutation without another judge."""

    canonical_event = _canonical_event_id(event_id)
    if canonical_event is None:
        return RelationGardeningPublicationCanaryResult(
            status="skipped",
            reason_code="invalid_event_id",
            event_id=str(event_id).strip(),
        )
    if not relation_gardening_publication_canary_enabled(
        user_id,
        canonical_event,
    ):
        return RelationGardeningPublicationCanaryResult(
            status="skipped",
            reason_code="canary_not_enabled",
            event_id=canonical_event,
        )

    root = _root(memory_root)
    receipt_path = relation_gardening_publication_receipt_path(
        user_id,
        canonical_event,
        memory_root=root,
    )
    intent_path = relation_gardening_publication_intent_path(
        user_id,
        canonical_event,
        memory_root=root,
    )
    with memory_file_lock(receipt_path):
        existing = _read_active_receipt(
            receipt_path,
            intent_path=intent_path,
            user_id=user_id,
            event_id=canonical_event,
            expected_source=expected_source,
            expected_target=expected_target,
        )
        if existing is not None:
            receipt_path.chmod(0o600)
            return replace(existing, replayed=True)

        try:
            intent = _read_json(intent_path)
            if intent is None:
                proof = prepare_relation_gardening_publication(
                    user_id=user_id,
                    event_id=canonical_event,
                    memory_root=root,
                )
                mutation = _sole_active_mutation(
                    proof.mutations,
                    expected_source=expected_source,
                    expected_target=expected_target,
                )
                plan = plan_relation_mutation_batch(
                    souvenirs_root=root / "capsules",
                    memory_root=root,
                    mutations=(mutation,),
                )
                if not plan.changed:
                    raise RelationPublicationError(
                        "A fresh active canary requires one missing relation.",
                        reason_code="active_canary_relation_already_present",
                    )
                if plan.baseline_index.library_fingerprint != proof.graph_fingerprint:
                    raise RelationPublicationError(
                        "The graph changed while preparing the active canary.",
                        reason_code="graph_changed_before_active_intent",
                    )
                intent = _intent_payload(
                    user_id=user_id,
                    event_id=canonical_event,
                    proof=proof,
                    plan=plan,
                )
                atomic_write_text(intent_path, _json_line(intent))
                intent_path.chmod(0o600)
            else:
                mutations, source_hashes = _validate_intent(
                    intent,
                    user_id=user_id,
                    event_id=canonical_event,
                    expected_source=expected_source,
                    expected_target=expected_target,
                    memory_root=root,
                )
                plan = plan_relation_mutation_batch(
                    souvenirs_root=root / "capsules",
                    memory_root=root,
                    mutations=mutations,
                )
                _validate_replay_plan(
                    plan,
                    source_hashes=source_hashes,
                    baseline_fingerprint=str(intent.get("graph_fingerprint", "")),
                )

            publication = apply_relation_mutation_batch(
                plan,
                souvenirs_root=root / "capsules",
                memory_root=root,
                index_path=root / "levels" / "memory_graph.json",
            )
            payload = _active_receipt_payload(
                user_id=user_id,
                event_id=canonical_event,
                expected_source=expected_source,
                expected_target=expected_target,
                intent=intent,
                status=publication.status,
                published_count=publication.published_count,
                unchanged_count=publication.unchanged_count,
                index_fingerprint=publication.index_fingerprint,
            )
            atomic_write_text(receipt_path, _json_line(payload))
            receipt_path.chmod(0o600)
        except RelationPublicationError as exc:
            return RelationGardeningPublicationCanaryResult(
                status="blocked",
                reason_code=exc.reason_code,
                event_id=canonical_event,
            )

    return _result_from_receipt(payload)


def pending_personal_relation_gardening_event_ids(
    user_id: str,
    *,
    memory_root: Path | None = None,
    limit: int = MAX_PERSONAL_EVENTS_PER_PASS,
    require_enabled: bool = True,
) -> tuple[str, ...]:
    """List post-activation events that still need judgment or publication."""

    if require_enabled and not relation_gardening_personal_publication_enabled(
        user_id
    ):
        return ()
    root = _root(memory_root)
    activation = _read_personal_activation(user_id, memory_root=root)
    if activation is None:
        return ()
    activation_sha256 = _payload_sha256(activation)
    bounded_limit = max(0, int(limit))
    if bounded_limit == 0:
        return ()
    event_root = (
        root
        / "levels"
        / "relation_gardening"
        / safe_memory_user_id(user_id)
        / "events"
    )
    candidates: list[tuple[float, str]] = []
    for path in event_root.glob("*.json"):
        event_id = path.stem
        try:
            event = read_relation_gardening_event(
                user_id,
                event_id,
                memory_root=root,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
            raise RelationPublicationError(
                "A queued relation gardening event is invalid.",
                reason_code="queued_gardening_event_invalid",
            ) from exc
        started_at = _event_started_at(event)
        if started_at < float(activation["activated_at"]):
            continue
        intent_path = relation_gardening_publication_intent_path(
            user_id,
            event_id,
            memory_root=root,
        )
        publication_receipt_path = relation_gardening_publication_receipt_path(
            user_id,
            event_id,
            memory_root=root,
        )
        existing = _read_personal_receipt(
            publication_receipt_path,
            intent_path=intent_path,
            user_id=user_id,
            event_id=event_id,
            activation_sha256=activation_sha256,
        )
        if existing is not None:
            continue
        if intent_path.is_file():
            candidates.append((started_at, event_id))
            continue
        dry_run = _read_json(
            relation_gardening_receipt_path(
                user_id,
                event_id,
                memory_root=root,
            )
        )
        if dry_run is None or dry_run.get("status") == "planned":
            candidates.append((started_at, event_id))
    return tuple(
        event_id
        for _started_at, event_id in sorted(candidates)[:bounded_limit]
    )


def run_pending_personal_relation_gardening(
    *,
    user_id: str,
    provider: LLMProvider,
    memory_root: Path | None = None,
    limit: int = MAX_PERSONAL_EVENTS_PER_PASS,
    require_enabled: bool = True,
) -> tuple[RelationGardeningResult, ...]:
    """Judge and publish post-activation events against sequential graphs."""

    results = []
    for event_id in pending_personal_relation_gardening_event_ids(
        user_id,
        memory_root=memory_root,
        limit=limit,
        require_enabled=require_enabled,
    ):
        judgment = run_relation_gardening_event_dry_run(
            user_id=user_id,
            event_id=event_id,
            provider=provider,
            memory_root=memory_root,
        )
        if judgment.status != "planned":
            results.append(
                replace(
                    judgment,
                    publication_status="not-needed",
                    publication_reason_code=judgment.reason_code,
                )
            )
            continue
        publication = run_personal_relation_gardening_publication(
            user_id=user_id,
            event_id=event_id,
            memory_root=memory_root,
            require_enabled=require_enabled,
        )
        results.append(
            replace(
                judgment,
                publication_status=publication.status,
                publication_reason_code=publication.reason_code,
                published_count=publication.published_count,
                publication_unchanged_count=publication.unchanged_count,
            )
        )
    return tuple(results)


def run_personal_relation_gardening_publication(
    *,
    user_id: str,
    event_id: str,
    memory_root: Path | None = None,
    require_enabled: bool = True,
) -> RelationGardeningPersonalPublicationResult:
    """Publish one post-activation monotone batch without another judge."""

    canonical_event = _canonical_event_id(event_id)
    if canonical_event is None:
        return RelationGardeningPersonalPublicationResult(
            status="skipped",
            reason_code="invalid_event_id",
            event_id=str(event_id).strip(),
        )
    if require_enabled and not relation_gardening_personal_publication_enabled(
        user_id
    ):
        return RelationGardeningPersonalPublicationResult(
            status="skipped",
            reason_code="personal_publication_not_enabled",
            event_id=canonical_event,
        )
    root = _root(memory_root)
    activation = _read_personal_activation(user_id, memory_root=root)
    if activation is None:
        return RelationGardeningPersonalPublicationResult(
            status="skipped",
            reason_code="personal_activation_missing",
            event_id=canonical_event,
        )
    activation_sha256 = _payload_sha256(activation)
    try:
        event = read_relation_gardening_event(
            user_id,
            canonical_event,
            memory_root=root,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return RelationGardeningPersonalPublicationResult(
            status="blocked",
            reason_code="gardening_event_invalid",
            event_id=canonical_event,
        )
    if _event_started_at(event) < float(activation["activated_at"]):
        return RelationGardeningPersonalPublicationResult(
            status="skipped",
            reason_code="event_before_personal_activation",
            event_id=canonical_event,
        )

    receipt_path = relation_gardening_publication_receipt_path(
        user_id,
        canonical_event,
        memory_root=root,
    )
    intent_path = relation_gardening_publication_intent_path(
        user_id,
        canonical_event,
        memory_root=root,
    )
    with memory_file_lock(receipt_path):
        existing = _read_personal_receipt(
            receipt_path,
            intent_path=intent_path,
            user_id=user_id,
            event_id=canonical_event,
            activation_sha256=activation_sha256,
        )
        if existing is not None:
            receipt_path.chmod(0o600)
            return replace(existing, replayed=True)

        intent = None
        try:
            intent = _read_json(intent_path)
            if intent is None:
                proof = prepare_relation_gardening_publication(
                    user_id=user_id,
                    event_id=canonical_event,
                    memory_root=root,
                )
                mutations = _personal_active_mutations(proof.mutations)
                plan = plan_relation_mutation_batch(
                    souvenirs_root=root / "capsules",
                    memory_root=root,
                    mutations=mutations,
                )
                _validate_personal_target_collections(plan)
                if not plan.changed:
                    raise RelationPublicationError(
                        "A fresh personal publication requires a missing relation.",
                        reason_code="personal_relation_already_present",
                    )
                if plan.baseline_index.library_fingerprint != proof.graph_fingerprint:
                    raise RelationPublicationError(
                        "The graph changed while preparing personal publication.",
                        reason_code="graph_changed_before_personal_intent",
                    )
                intent = _intent_payload(
                    user_id=user_id,
                    event_id=canonical_event,
                    proof=proof,
                    plan=plan,
                    mode="active",
                    activation_sha256=activation_sha256,
                )
                atomic_write_text(intent_path, _json_line(intent))
                intent_path.chmod(0o600)
            else:
                mutations, source_hashes = _validate_intent(
                    intent,
                    user_id=user_id,
                    event_id=canonical_event,
                    memory_root=root,
                    mode="active",
                    activation_sha256=activation_sha256,
                )
                plan = plan_relation_mutation_batch(
                    souvenirs_root=root / "capsules",
                    memory_root=root,
                    mutations=mutations,
                )
                _validate_replay_plan(
                    plan,
                    source_hashes=source_hashes,
                    baseline_fingerprint=str(intent.get("graph_fingerprint", "")),
                )

            publication = apply_relation_mutation_batch(
                plan,
                souvenirs_root=root / "capsules",
                memory_root=root,
                index_path=root / "levels" / "memory_graph.json",
            )
            payload = _personal_receipt_payload(
                user_id=user_id,
                event_id=canonical_event,
                activation_sha256=activation_sha256,
                intent=intent,
                status=publication.status,
                reason_code="",
                published_count=publication.published_count,
                unchanged_count=publication.unchanged_count,
                mutation_count=len(plan.mutations),
                index_fingerprint=publication.index_fingerprint,
            )
            atomic_write_text(receipt_path, _json_line(payload))
            receipt_path.chmod(0o600)
        except RelationPublicationError as exc:
            if exc.reason_code == "relation_batch_rollback_failed":
                raise
            payload = _personal_receipt_payload(
                user_id=user_id,
                event_id=canonical_event,
                activation_sha256=activation_sha256,
                intent=intent,
                status="blocked",
                reason_code=exc.reason_code,
                published_count=0,
                unchanged_count=0,
                mutation_count=0,
                index_fingerprint="",
            )
            atomic_write_text(receipt_path, _json_line(payload))
            receipt_path.chmod(0o600)

    return _personal_result_from_receipt(payload)


def _sole_active_mutation(
    mutations: tuple[RelationMutation, ...],
    *,
    expected_source: str,
    expected_target: str,
) -> RelationMutation:
    if len(mutations) != 1:
        raise RelationPublicationError(
            "The active canary requires exactly one mutation.",
            reason_code="exactly_one_active_mutation_required",
        )
    mutation = mutations[0]
    if mutation.action != "upsert":
        raise RelationPublicationError(
            "The active canary only permits monotone upserts.",
            reason_code="active_canary_upsert_only",
        )
    if (
        mutation.source != str(expected_source).strip()
        or mutation.target != str(expected_target).strip()
    ):
        raise RelationPublicationError(
            "The active canary mutation differs from the pre-registered pair.",
            reason_code="active_canary_pair_mismatch",
        )
    return mutation


def _personal_active_mutations(
    mutations: tuple[RelationMutation, ...],
) -> tuple[RelationMutation, ...]:
    if not 1 <= len(mutations) <= MAX_PERSONAL_MUTATIONS_PER_EVENT:
        raise RelationPublicationError(
            "Personal relation gardening requires one bounded mutation batch.",
            reason_code="personal_mutation_batch_out_of_bounds",
        )
    if any(mutation.action != "upsert" for mutation in mutations):
        raise RelationPublicationError(
            "Personal relation gardening only permits monotone upserts.",
            reason_code="personal_publication_upsert_only",
        )
    return mutations


def _validate_personal_target_collections(
    plan: RelationMutationBatchPlan,
) -> None:
    for mutation in plan.mutations:
        target = plan.baseline_index.souvenirs.get(mutation.target)
        if (
            target is None
            or target.collection is None
            or target.collection.coverage != "open"
        ):
            raise RelationPublicationError(
                "Personal relation gardening requires an existing open collection.",
                reason_code="personal_target_not_open_collection",
            )


def _intent_payload(
    *,
    user_id: str,
    event_id: str,
    proof: RelationGardeningPublicationProof,
    plan: RelationMutationBatchPlan,
    mode: str = "canary",
    activation_sha256: str = "",
) -> dict[str, Any]:
    payload = {
        "schema": RELATION_GARDENING_ACTIVE_INTENT_SCHEMA,
        "mode": mode,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_ref": _technical_ref(user_id),
        "event_id": event_id,
        "event_sha256": proof.event_sha256,
        "decision_sha256": proof.decision_sha256,
        "dry_run_receipt_sha256": proof.dry_run_receipt_sha256,
        "graph_fingerprint": proof.graph_fingerprint,
        "mutations": [
            {
                "action": mutation.action,
                "source": mutation.source,
                "target": mutation.target,
                "role": mutation.role,
                "source_ref": mutation.source_ref,
            }
            for mutation in plan.mutations
        ],
        "sources": [
            {
                "source": source.source,
                "current_sha256": source.current_hash,
                "next_sha256": source.next_hash,
            }
            for source in plan.source_plans
        ],
    }
    if activation_sha256:
        payload["activation_sha256"] = activation_sha256
    return payload


def _validate_intent(
    payload: dict[str, Any],
    *,
    user_id: str,
    event_id: str,
    expected_source: str = "",
    expected_target: str = "",
    memory_root: Path,
    mode: str = "canary",
    activation_sha256: str = "",
) -> tuple[tuple[RelationMutation, ...], dict[str, tuple[str, str]]]:
    if (
        payload.get("schema") != RELATION_GARDENING_ACTIVE_INTENT_SCHEMA
        or payload.get("mode") != mode
        or payload.get("user_ref") != _technical_ref(user_id)
        or payload.get("event_id") != event_id
    ):
        raise RelationPublicationError(
            "The active relation intent has the wrong identity.",
            reason_code="active_intent_identity_mismatch",
        )
    if activation_sha256 and payload.get("activation_sha256") != activation_sha256:
        raise RelationPublicationError(
            "The active relation intent belongs to another activation.",
            reason_code="active_intent_activation_mismatch",
        )
    if not _is_sha256(str(payload.get("graph_fingerprint", ""))):
        raise RelationPublicationError(
            "The active relation intent has no valid graph fingerprint.",
            reason_code="active_intent_invalid",
        )

    try:
        event = read_relation_gardening_event(
            user_id,
            event_id,
            memory_root=memory_root,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise RelationPublicationError(
            "The event behind the active intent is unavailable.",
            reason_code="active_intent_evidence_missing",
        ) from exc
    decision = _read_json(
        relation_gardening_decision_path(
            user_id,
            event_id,
            memory_root=memory_root,
        )
    )
    dry_run = _read_json(
        relation_gardening_receipt_path(
            user_id,
            event_id,
            memory_root=memory_root,
        )
    )
    if (
        decision is None
        or decision.get("schema") != RELATION_GARDENING_DECISION_SCHEMA
        or decision.get("event_id") != event_id
        or dry_run is None
        or dry_run.get("schema") != RELATION_GARDENING_RECEIPT_SCHEMA
        or dry_run.get("mode") != "dry-run"
        or dry_run.get("event_id") != event_id
    ):
        raise RelationPublicationError(
            "The evidence behind the active intent is unavailable.",
            reason_code="active_intent_evidence_missing",
        )
    if any(
        (
            payload.get("event_sha256") != _payload_sha256(event),
            payload.get("decision_sha256") != _payload_sha256(decision),
            payload.get("dry_run_receipt_sha256") != _payload_sha256(dry_run),
        )
    ):
        raise RelationPublicationError(
            "The evidence behind the active intent changed.",
            reason_code="active_intent_evidence_changed",
        )

    raw_mutations = payload.get("mutations")
    if not isinstance(raw_mutations, list):
        raise RelationPublicationError(
            "The active intent has no mutation list.",
            reason_code="active_intent_invalid",
        )
    try:
        mutations = tuple(
            RelationMutation(
                action=str(item["action"]),
                source=str(item["source"]),
                target=str(item["target"]),
                role=str(item["role"]),
                source_ref=str(item["source_ref"]),
            )
            for item in raw_mutations
            if isinstance(item, dict)
        )
    except KeyError as exc:
        raise RelationPublicationError(
            "The active intent mutation is incomplete.",
            reason_code="active_intent_invalid",
        ) from exc
    if len(mutations) != len(raw_mutations):
        raise RelationPublicationError(
            "The active intent mutation is malformed.",
            reason_code="active_intent_invalid",
        )
    if mode == "canary":
        _sole_active_mutation(
            mutations,
            expected_source=expected_source,
            expected_target=expected_target,
        )
    else:
        _personal_active_mutations(mutations)

    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise RelationPublicationError(
            "The active intent has no source fingerprints.",
            reason_code="active_intent_invalid",
        )
    source_hashes = {}
    for item in sources:
        if not isinstance(item, dict):
            raise RelationPublicationError(
                "The active intent source fingerprint is malformed.",
                reason_code="active_intent_invalid",
            )
        source = str(item.get("source", ""))
        current = str(item.get("current_sha256", ""))
        next_hash = str(item.get("next_sha256", ""))
        if (
            source in source_hashes
            or not _is_sha256(current)
            or not _is_sha256(next_hash)
        ):
            raise RelationPublicationError(
                "The active intent source fingerprint is invalid.",
                reason_code="active_intent_invalid",
            )
        source_hashes[source] = (current, next_hash)
    if set(source_hashes) != {mutation.source for mutation in mutations}:
        raise RelationPublicationError(
            "The active intent does not cover every source.",
            reason_code="active_intent_invalid",
        )
    return mutations, source_hashes


def _validate_replay_plan(
    plan: RelationMutationBatchPlan,
    *,
    source_hashes: dict[str, tuple[str, str]],
    baseline_fingerprint: str,
) -> None:
    saw_published_source = False
    for source in plan.source_plans:
        expected_current, expected_next = source_hashes[source.source]
        if source.current_hash == expected_current:
            if source.next_hash != expected_next:
                raise RelationPublicationError(
                    "The active intent no longer renders the same relation.",
                    reason_code="active_intent_render_changed",
                )
            continue
        if source.current_hash == expected_next and source.next_hash == expected_next:
            saw_published_source = True
            continue
        raise RelationPublicationError(
            "A source differs from both sides of the active intent.",
            reason_code="active_intent_source_changed",
        )
    if (
        not saw_published_source
        and plan.baseline_index.library_fingerprint != baseline_fingerprint
    ):
        raise RelationPublicationError(
            "The graph changed before the active intent was applied.",
            reason_code="active_intent_graph_changed",
        )


def _active_receipt_payload(
    *,
    user_id: str,
    event_id: str,
    expected_source: str,
    expected_target: str,
    intent: dict[str, Any],
    status: str,
    published_count: int,
    unchanged_count: int,
    index_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema": RELATION_GARDENING_ACTIVE_RECEIPT_SCHEMA,
        "mode": "canary",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_ref": _technical_ref(user_id),
        "event_id": event_id,
        "intent_sha256": _payload_sha256(intent),
        "source_ref": _technical_ref(expected_source),
        "target_ref": _technical_ref(expected_target),
        "status": status,
        "reason_code": "",
        "published_count": max(0, int(published_count)),
        "unchanged_count": max(0, int(unchanged_count)),
        "index_fingerprint": str(index_fingerprint)[:128],
    }


def _read_active_receipt(
    path: Path,
    *,
    intent_path: Path,
    user_id: str,
    event_id: str,
    expected_source: str,
    expected_target: str,
) -> RelationGardeningPublicationCanaryResult | None:
    payload = _read_json(path)
    if payload is None:
        return None
    if (
        payload.get("schema") != RELATION_GARDENING_ACTIVE_RECEIPT_SCHEMA
        or payload.get("mode") != "canary"
        or payload.get("user_ref") != _technical_ref(user_id)
        or payload.get("event_id") != event_id
        or payload.get("source_ref") != _technical_ref(expected_source)
        or payload.get("target_ref") != _technical_ref(expected_target)
    ):
        raise RelationPublicationError(
            "The active relation receipt has the wrong identity.",
            reason_code="active_receipt_identity_mismatch",
        )
    intent = _read_json(intent_path)
    if (
        intent is None
        or payload.get("intent_sha256") != _payload_sha256(intent)
        or payload.get("status") not in {"published", "unchanged"}
    ):
        raise RelationPublicationError(
            "The active relation receipt is detached from its intent.",
            reason_code="active_receipt_intent_mismatch",
        )
    try:
        published_count = int(payload.get("published_count", -1))
        unchanged_count = int(payload.get("unchanged_count", -1))
    except (TypeError, ValueError) as exc:
        raise RelationPublicationError(
            "The active relation receipt has invalid counters.",
            reason_code="active_receipt_invalid",
        ) from exc
    if (
        published_count < 0
        or unchanged_count < 0
        or published_count + unchanged_count != 1
    ):
        raise RelationPublicationError(
            "The active relation receipt has inconsistent counters.",
            reason_code="active_receipt_invalid",
        )
    return _result_from_receipt(payload)


def _result_from_receipt(
    payload: dict[str, Any],
) -> RelationGardeningPublicationCanaryResult:
    return RelationGardeningPublicationCanaryResult(
        status=str(payload.get("status", "")),
        reason_code=str(payload.get("reason_code", "")),
        event_id=str(payload.get("event_id", "")),
        published_count=max(0, int(payload.get("published_count", 0))),
        unchanged_count=max(0, int(payload.get("unchanged_count", 0))),
    )


def _read_personal_activation(
    user_id: str,
    *,
    memory_root: Path,
) -> dict[str, Any] | None:
    payload = _read_json(
        relation_gardening_personal_activation_path(
            user_id,
            memory_root=memory_root,
        )
    )
    if payload is not None:
        _validate_personal_activation(payload, user_id=user_id)
    return payload


def _validate_personal_activation(
    payload: dict[str, Any],
    *,
    user_id: str,
) -> None:
    if (
        payload.get("schema") != RELATION_GARDENING_PERSONAL_ACTIVATION_SCHEMA
        or payload.get("mode") != "active"
        or payload.get("user_ref") != _technical_ref(user_id)
    ):
        raise RelationPublicationError(
            "The personal relation activation has the wrong identity.",
            reason_code="personal_activation_identity_mismatch",
        )
    try:
        activated_at = float(payload.get("activated_at", 0))
    except (TypeError, ValueError) as exc:
        raise RelationPublicationError(
            "The personal relation activation timestamp is invalid.",
            reason_code="personal_activation_invalid",
        ) from exc
    if activated_at <= 0:
        raise RelationPublicationError(
            "The personal relation activation timestamp is invalid.",
            reason_code="personal_activation_invalid",
        )


def _event_started_at(event: dict[str, Any]) -> float:
    try:
        started_at = float(event.get("source_turn_started_at", 0))
    except (TypeError, ValueError) as exc:
        raise RelationPublicationError(
            "The relation gardening event has no valid start time.",
            reason_code="gardening_event_time_invalid",
        ) from exc
    if started_at < 0:
        raise RelationPublicationError(
            "The relation gardening event has no valid start time.",
            reason_code="gardening_event_time_invalid",
        )
    return started_at


def _personal_receipt_payload(
    *,
    user_id: str,
    event_id: str,
    activation_sha256: str,
    intent: dict[str, Any] | None,
    status: str,
    reason_code: str,
    published_count: int,
    unchanged_count: int,
    mutation_count: int,
    index_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema": RELATION_GARDENING_PERSONAL_RECEIPT_SCHEMA,
        "mode": "active",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_ref": _technical_ref(user_id),
        "event_id": event_id,
        "activation_sha256": activation_sha256,
        "intent_sha256": _payload_sha256(intent) if intent is not None else "",
        "status": status,
        "reason_code": str(reason_code)[:160],
        "published_count": max(0, int(published_count)),
        "unchanged_count": max(0, int(unchanged_count)),
        "mutation_count": max(0, int(mutation_count)),
        "index_fingerprint": str(index_fingerprint)[:128],
    }


def _read_personal_receipt(
    path: Path,
    *,
    intent_path: Path,
    user_id: str,
    event_id: str,
    activation_sha256: str,
) -> RelationGardeningPersonalPublicationResult | None:
    payload = _read_json(path)
    if payload is None:
        return None
    if (
        payload.get("schema") != RELATION_GARDENING_PERSONAL_RECEIPT_SCHEMA
        or payload.get("mode") != "active"
        or payload.get("user_ref") != _technical_ref(user_id)
        or payload.get("event_id") != event_id
        or payload.get("activation_sha256") != activation_sha256
    ):
        raise RelationPublicationError(
            "The personal relation receipt has the wrong identity.",
            reason_code="personal_receipt_identity_mismatch",
        )
    status = str(payload.get("status", ""))
    try:
        published_count = int(payload.get("published_count", -1))
        unchanged_count = int(payload.get("unchanged_count", -1))
        mutation_count = int(payload.get("mutation_count", -1))
    except (TypeError, ValueError) as exc:
        raise RelationPublicationError(
            "The personal relation receipt has invalid counters.",
            reason_code="personal_receipt_invalid",
        ) from exc
    if status == "blocked":
        if (
            published_count != 0
            or unchanged_count != 0
            or mutation_count != 0
            or not str(payload.get("reason_code", "")).strip()
        ):
            raise RelationPublicationError(
                "The blocked personal receipt is inconsistent.",
                reason_code="personal_receipt_invalid",
            )
        intent_sha256 = str(payload.get("intent_sha256", ""))
        if intent_sha256:
            intent = _read_json(intent_path)
            if intent is None or intent_sha256 != _payload_sha256(intent):
                raise RelationPublicationError(
                    "The blocked personal receipt is detached from its intent.",
                    reason_code="personal_receipt_intent_mismatch",
                )
        return _personal_result_from_receipt(payload)
    if status not in {"published", "unchanged"}:
        raise RelationPublicationError(
            "The personal relation receipt has an invalid status.",
            reason_code="personal_receipt_invalid",
        )
    intent = _read_json(intent_path)
    if (
        intent is None
        or payload.get("intent_sha256") != _payload_sha256(intent)
        or not 1 <= mutation_count <= MAX_PERSONAL_MUTATIONS_PER_EVENT
        or published_count < 0
        or unchanged_count < 0
        or published_count + unchanged_count != mutation_count
    ):
        raise RelationPublicationError(
            "The personal relation receipt is detached from its intent.",
            reason_code="personal_receipt_intent_mismatch",
        )
    return _personal_result_from_receipt(payload)


def _personal_result_from_receipt(
    payload: dict[str, Any],
) -> RelationGardeningPersonalPublicationResult:
    return RelationGardeningPersonalPublicationResult(
        status=str(payload.get("status", "")),
        reason_code=str(payload.get("reason_code", "")),
        event_id=str(payload.get("event_id", "")),
        published_count=max(0, int(payload.get("published_count", 0))),
        unchanged_count=max(0, int(payload.get("unchanged_count", 0))),
    )


def _publication_path(
    user_id: str,
    bucket: str,
    event_id: str,
    *,
    memory_root: Path | None,
) -> Path:
    root = _root(memory_root)
    canonical_event = _canonical_event_id(event_id)
    if canonical_event is None:
        raise ValueError("invalid relation gardening event id")
    return (
        root
        / "levels"
        / "relation_gardening"
        / safe_memory_user_id(user_id)
        / bucket
        / f"{canonical_event}.json"
    )


def _root(memory_root: Path | None) -> Path:
    return (MEMORY_ROOT if memory_root is None else Path(memory_root)).resolve()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RelationPublicationError(
            "A relation gardening publication artifact is invalid JSON.",
            reason_code="active_publication_artifact_invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise RelationPublicationError(
            "A relation gardening publication artifact is not an object.",
            reason_code="active_publication_artifact_invalid",
        )
    return payload


def _single_explicit_value(name: str) -> str | None:
    values = [
        value.strip() for value in os.getenv(name, "").split(",") if value.strip()
    ]
    if len(values) != 1 or values[0] == "*":
        return None
    return values[0]


def _canonical_event_id(value: str) -> str | None:
    candidate = str(value).strip()
    if not _is_sha256(candidate):
        return None
    return candidate


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _json_line(payload: dict[str, Any]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


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


__all__ = [
    "RELATION_GARDENING_ACTIVE_EVENT_IDS_ENV",
    "RELATION_GARDENING_ACTIVE_INTENT_SCHEMA",
    "RELATION_GARDENING_ACTIVE_RECEIPT_SCHEMA",
    "RELATION_GARDENING_PERSONAL_ACTIVATION_SCHEMA",
    "RELATION_GARDENING_PERSONAL_RECEIPT_SCHEMA",
    "RelationGardeningPublicationCanaryResult",
    "RelationGardeningPersonalPublicationResult",
    "activate_personal_relation_gardening",
    "pending_personal_relation_gardening_event_ids",
    "relation_gardening_publication_canary_enabled",
    "relation_gardening_publication_intent_path",
    "relation_gardening_publication_receipt_path",
    "relation_gardening_personal_activation_path",
    "relation_gardening_personal_publication_enabled",
    "run_pending_personal_relation_gardening",
    "run_personal_relation_gardening_publication",
    "run_relation_gardening_publication_canary",
]
