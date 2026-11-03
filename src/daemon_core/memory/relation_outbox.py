"""Dry-run relation publication inside the durable memory job.

The semantic proposal has already been made by MemoryAgent and validated by
the relation shadow. This module only binds that exact event to the existing
transactional publisher, writes a technical receipt under exports, and never
mutates the memory vault.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from daemon_core.memory.collection_publication import (
    CollectionDeclaration,
    CollectionPublicationError,
    plan_collection_declaration,
)
from daemon_core.memory.graph import MEMBERSHIP_RELATION
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.relation_publication import (
    RelationMutation,
    RelationPublicationError,
    apply_relation_mutation,
    plan_relation_mutation,
)
from daemon_core.memory.relation_shadow import (
    read_memory_relation_shadow_event,
)
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT


RELATION_PUBLICATION_ENV = "DAEMON_CORE_MEMORY_RELATION_PUBLICATION"
RELATION_PUBLICATION_USER_IDS_ENV = (
    "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS"
)
RELATION_PUBLICATION_RECEIPT_DIR_ENV = (
    "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_RECEIPT_DIR"
)
RELATION_PUBLICATION_DRY_RUN_SCHEMA = "daemon.memory-relation-publication-dry-run.v1"
RELATION_PUBLICATION_DRY_RUN_SCHEMA_V2 = (
    "daemon.memory-relation-publication-dry-run.v2"
)
RELATION_PUBLICATION_DRY_RUN_SCHEMAS = frozenset(
    {
        RELATION_PUBLICATION_DRY_RUN_SCHEMA,
        RELATION_PUBLICATION_DRY_RUN_SCHEMA_V2,
    }
)


@dataclass(frozen=True)
class RelationPublicationDryRunReceipt:
    status: str
    reason_code: str
    event_id: str
    proposal_count: int = 0
    planned_count: int = 0
    unchanged_count: int = 0
    blocked_count: int = 0
    collection_proposal_count: int = 0
    collection_planned_count: int = 0
    collection_unchanged_count: int = 0
    collection_blocked_count: int = 0
    replayed: bool = False


def memory_relation_publication_dry_run_enabled(user_id: str) -> bool:
    if os.getenv(RELATION_PUBLICATION_ENV, "off").strip().casefold() != "dry-run":
        return False
    allowed = {
        value.strip()
        for value in os.getenv(RELATION_PUBLICATION_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return "*" in allowed or str(user_id).strip() in allowed


def relation_publication_dry_run_receipt_path(
    user_id: str,
    event_id: str,
    *,
    receipt_root: Path | None = None,
) -> Path:
    root = _receipt_root() if receipt_root is None else receipt_root
    return root / safe_memory_user_id(user_id) / f"{_event_filename(event_id)}.json"


def run_relation_publication_dry_run(
    *,
    user_id: str,
    event_id: str,
    memory_root: Path | None = None,
    receipt_root: Path | None = None,
) -> RelationPublicationDryRunReceipt:
    """Plan every accepted relation and persist one idempotent technical receipt."""

    normalized_event_id = str(event_id).strip()
    if not normalized_event_id:
        return RelationPublicationDryRunReceipt(
            status="skipped",
            reason_code="shadow_event_unavailable",
            event_id="",
        )
    root = MEMORY_ROOT if memory_root is None else memory_root
    path = relation_publication_dry_run_receipt_path(
        user_id,
        normalized_event_id,
        receipt_root=receipt_root,
    )
    with memory_file_lock(path):
        existing = _read_receipt(path, user_id, normalized_event_id)
        if existing is not None:
            path.chmod(0o600)
            return replace(existing, replayed=True)

    event = read_memory_relation_shadow_event(
        user_id,
        normalized_event_id,
        memory_root=root,
    )
    payload = _build_receipt_payload(
        user_id=user_id,
        event_id=normalized_event_id,
        event=event,
        memory_root=root,
    )
    receipt = _receipt_from_payload(payload)

    with memory_file_lock(path):
        existing = _read_receipt(path, user_id, normalized_event_id)
        if existing is not None:
            path.chmod(0o600)
            return replace(existing, replayed=True)
        atomic_write_text(
            path,
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        path.chmod(0o600)
    return receipt


def _build_receipt_payload(
    *,
    user_id: str,
    event_id: str,
    event: dict[str, Any] | None,
    memory_root: Path,
) -> dict[str, Any]:
    if event is None:
        return _payload(
            user_id=user_id,
            event_id=event_id,
            status="blocked",
            reason_code="shadow_event_missing",
            outcomes=[],
        )

    accepted = event.get("accepted")
    accepted_collections = event.get("accepted_collections")
    relations = accepted if isinstance(accepted, list) else []
    collections = (
        accepted_collections if isinstance(accepted_collections, list) else []
    )
    if not relations and not collections:
        return _payload(
            user_id=user_id,
            event_id=event_id,
            source_trace_id=str(event.get("source_trace_id", "")),
            status="skipped",
            reason_code="no_accepted_relations",
            outcomes=[],
            schema=_receipt_schema(event),
        )

    outcomes: list[dict[str, Any]] = []
    staged_collections = {}
    blocked_collections: dict[str, str] = {}
    for position, proposal in enumerate(collections):
        target = proposal.get("target", "") if isinstance(proposal, dict) else ""
        outcome = {
            "kind": "collection",
            "position": position,
            "target_ref": _technical_ref(str(target)),
        }
        try:
            declaration = _declaration_from_shadow_proposal(proposal)
            plan = plan_collection_declaration(
                souvenirs_root=memory_root / "capsules",
                memory_root=memory_root,
                declaration=declaration,
            )
        except CollectionPublicationError as exc:
            outcomes.append(
                {
                    **outcome,
                    "status": "blocked",
                    "reason_code": exc.reason_code,
                }
            )
            blocked_collections[str(target)] = exc.reason_code
            continue
        staged_collections[declaration.target] = plan.collection
        outcomes.append(
            {
                **outcome,
                "status": "planned" if plan.changed else "unchanged",
                "reason_code": "",
                "index_fingerprint": plan.baseline_index.library_fingerprint,
            }
        )

    for position, proposal in enumerate(relations):
        source = proposal.get("source", "") if isinstance(proposal, dict) else ""
        target = proposal.get("target", "") if isinstance(proposal, dict) else ""
        outcome = {
            "kind": "relation",
            "position": position,
            "source_ref": _technical_ref(str(source)),
            "target_ref": _technical_ref(str(target)),
        }
        if str(target) in blocked_collections:
            outcomes.append(
                {
                    **outcome,
                    "status": "blocked",
                    "reason_code": "collection_declaration_blocked",
                }
            )
            continue
        try:
            mutation = relation_mutation_from_shadow_proposal(proposal)
            plan = plan_relation_mutation(
                souvenirs_root=memory_root / "capsules",
                memory_root=memory_root,
                mutation=mutation,
                staged_collections=(
                    {mutation.target: staged_collections[mutation.target]}
                    if mutation.target in staged_collections
                    else None
                ),
            )
            result = apply_relation_mutation(
                plan,
                souvenirs_root=memory_root / "capsules",
                memory_root=memory_root,
                index_path=memory_root / "levels" / "memory_graph.json",
                dry_run=True,
            )
        except RelationPublicationError as exc:
            outcomes.append(
                {
                    **outcome,
                    "status": "blocked",
                    "reason_code": exc.reason_code,
                }
            )
            continue
        outcomes.append(
            {
                **outcome,
                "status": result.status,
                "reason_code": "",
                "index_fingerprint": result.index_fingerprint,
            }
        )

    status, reason_code = _aggregate_status(outcomes)
    return _payload(
        user_id=user_id,
        event_id=event_id,
        source_trace_id=str(event.get("source_trace_id", "")),
        status=status,
        reason_code=reason_code,
        outcomes=outcomes,
        schema=_receipt_schema(event),
    )


def _declaration_from_shadow_proposal(
    proposal: object,
) -> CollectionDeclaration:
    if not isinstance(proposal, dict):
        raise CollectionPublicationError(
            "Shadow collection proposal is not an object.",
            reason_code="invalid_shadow_collection_proposal",
        )
    source_refs = proposal.get("source_refs")
    if (
        proposal.get("action") != "declare_open"
        or not isinstance(source_refs, list)
        or len(source_refs) != 1
        or not isinstance(source_refs[0], str)
    ):
        raise CollectionPublicationError(
            "Shadow collection proposal is incomplete.",
            reason_code="invalid_shadow_collection_source_ref",
        )
    return CollectionDeclaration(
        action="declare_open",
        target=str(proposal.get("target", "")),
        source_ref=source_refs[0],
    )


def relation_mutation_from_shadow_proposal(proposal: object) -> RelationMutation:
    if not isinstance(proposal, dict):
        raise RelationPublicationError(
            "Shadow proposal is not an object.",
            reason_code="invalid_shadow_proposal",
        )
    if proposal.get("kind") != MEMBERSHIP_RELATION:
        raise RelationPublicationError(
            "Shadow proposal has an unsupported relation kind.",
            reason_code="invalid_shadow_relation_kind",
        )
    source_refs = proposal.get("source_refs")
    if (
        not isinstance(source_refs, list)
        or len(source_refs) != 1
        or not isinstance(source_refs[0], str)
    ):
        raise RelationPublicationError(
            "Shadow proposal must carry one exact L0 source.",
            reason_code="invalid_shadow_source_ref",
        )
    return RelationMutation(
        action=str(proposal.get("action", "")),
        source=str(proposal.get("source", "")),
        target=str(proposal.get("target", "")),
        role=str(proposal.get("role", "")),
        source_ref=source_refs[0],
    )


def _aggregate_status(outcomes: list[dict[str, Any]]) -> tuple[str, str]:
    blocked = [item for item in outcomes if item.get("status") == "blocked"]
    planned = [item for item in outcomes if item.get("status") == "planned"]
    unchanged = [item for item in outcomes if item.get("status") == "unchanged"]
    if blocked and (planned or unchanged):
        return "partial", "some_relations_blocked"
    if blocked:
        codes = {str(item.get("reason_code", "")) for item in blocked}
        return "blocked", codes.pop() if len(codes) == 1 else "relations_blocked"
    if planned:
        return "planned", ""
    if unchanged:
        return "unchanged", ""
    return "skipped", "no_publishable_relations"


def _payload(
    *,
    user_id: str,
    event_id: str,
    status: str,
    reason_code: str,
    outcomes: list[dict[str, Any]],
    source_trace_id: str = "",
    schema: str = RELATION_PUBLICATION_DRY_RUN_SCHEMA,
) -> dict[str, Any]:
    relation_outcomes = [
        item for item in outcomes if item.get("kind") != "collection"
    ]
    collection_outcomes = [
        item for item in outcomes if item.get("kind") == "collection"
    ]
    return {
        "schema": schema,
        "mode": "dry-run",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_ref": _technical_ref(user_id),
        "event_id": event_id,
        "source_trace_ref": _technical_ref(source_trace_id),
        "status": status,
        "reason_code": reason_code,
        "proposal_count": len(relation_outcomes),
        "planned_count": sum(
            item.get("status") == "planned" for item in relation_outcomes
        ),
        "unchanged_count": sum(
            item.get("status") == "unchanged" for item in relation_outcomes
        ),
        "blocked_count": sum(
            item.get("status") == "blocked" for item in relation_outcomes
        ),
        "collection_proposal_count": len(collection_outcomes),
        "collection_planned_count": sum(
            item.get("status") == "planned" for item in collection_outcomes
        ),
        "collection_unchanged_count": sum(
            item.get("status") == "unchanged" for item in collection_outcomes
        ),
        "collection_blocked_count": sum(
            item.get("status") == "blocked" for item in collection_outcomes
        ),
        "outcomes": outcomes,
    }


def _read_receipt(
    path: Path,
    user_id: str,
    event_id: str,
) -> RelationPublicationDryRunReceipt | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError("relation_dry_run_receipt_invalid") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema") not in RELATION_PUBLICATION_DRY_RUN_SCHEMAS
        or raw.get("user_ref") != _technical_ref(user_id)
        or raw.get("event_id") != event_id
    ):
        raise RuntimeError("relation_dry_run_receipt_identity_mismatch")
    return _receipt_from_payload(raw)


def _receipt_from_payload(
    payload: dict[str, Any],
) -> RelationPublicationDryRunReceipt:
    return RelationPublicationDryRunReceipt(
        status=str(payload.get("status", "")),
        reason_code=str(payload.get("reason_code", "")),
        event_id=str(payload.get("event_id", "")),
        proposal_count=int(payload.get("proposal_count", 0)),
        planned_count=int(payload.get("planned_count", 0)),
        unchanged_count=int(payload.get("unchanged_count", 0)),
        blocked_count=int(payload.get("blocked_count", 0)),
        collection_proposal_count=int(
            payload.get("collection_proposal_count", 0)
        ),
        collection_planned_count=int(
            payload.get("collection_planned_count", 0)
        ),
        collection_unchanged_count=int(
            payload.get("collection_unchanged_count", 0)
        ),
        collection_blocked_count=int(
            payload.get("collection_blocked_count", 0)
        ),
    )


def _receipt_schema(event: dict[str, Any]) -> str:
    return (
        RELATION_PUBLICATION_DRY_RUN_SCHEMA_V2
        if "accepted_collections" in event
        else RELATION_PUBLICATION_DRY_RUN_SCHEMA
    )


def _receipt_root() -> Path:
    configured = os.getenv(RELATION_PUBLICATION_RECEIPT_DIR_ENV, "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else PROJECT_ROOT / "exports" / "memory_relation_publication_dry_run"
    )
    return root.resolve()


def _event_filename(event_id: str) -> str:
    cleaned = str(event_id).strip().casefold()
    if len(cleaned) == 64 and all(
        character in "0123456789abcdef" for character in cleaned
    ):
        return cleaned
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _technical_ref(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "RELATION_PUBLICATION_DRY_RUN_SCHEMA",
    "RELATION_PUBLICATION_DRY_RUN_SCHEMA_V2",
    "RELATION_PUBLICATION_ENV",
    "RELATION_PUBLICATION_RECEIPT_DIR_ENV",
    "RELATION_PUBLICATION_USER_IDS_ENV",
    "RelationPublicationDryRunReceipt",
    "memory_relation_publication_dry_run_enabled",
    "relation_mutation_from_shadow_proposal",
    "relation_publication_dry_run_receipt_path",
    "run_relation_publication_dry_run",
]
