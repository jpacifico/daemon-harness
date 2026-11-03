"""Shadow-only MemoryAgent proposals for sourced collection membership.

The model judges semantic membership inside the existing MemoryAgent call.
This module only checks structure, exact user evidence and exact L0 provenance,
then appends an auditable receipt. It never mutates souvenirs or graph indexes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from daemon_core.memory.graph import MEMBERSHIP_RELATION, normalize_souvenir_name
from daemon_core.memory.integrity import memory_file_lock
from daemon_core.memory.writer import (
    GroundZeroReference,
    read_ground_zero_record,
    safe_memory_user_id,
)
from daemon_core.paths import MEMORY_ROOT


RELATION_SHADOW_ENV = "DAEMON_CORE_MEMORY_RELATION_SHADOW"
RELATION_SHADOW_USER_IDS_ENV = "DAEMON_CORE_MEMORY_RELATION_SHADOW_USER_IDS"
RELATION_SHADOW_SCHEMA = "daemon.memory-relation-shadow.v1"
RELATION_SHADOW_SCHEMA_V2 = "daemon.memory-relation-shadow.v2"
RELATION_SHADOW_SCHEMAS = frozenset(
    {RELATION_SHADOW_SCHEMA, RELATION_SHADOW_SCHEMA_V2}
)
COLLECTION_BOOTSTRAP_SHADOW_ENV = (
    "DAEMON_CORE_MEMORY_COLLECTION_BOOTSTRAP_SHADOW"
)
COLLECTION_BOOTSTRAP_SHADOW_USER_IDS_ENV = (
    "DAEMON_CORE_MEMORY_COLLECTION_BOOTSTRAP_SHADOW_USER_IDS"
)
TOPOLOGY_BUNDLE_SHADOW_ENV = "DAEMON_CORE_MEMORY_TOPOLOGY_BUNDLE_SHADOW"
TOPOLOGY_BUNDLE_SHADOW_USER_IDS_ENV = (
    "DAEMON_CORE_MEMORY_TOPOLOGY_BUNDLE_SHADOW_USER_IDS"
)
PERFORMATIVE_RELATION_RETRACTIONS_ENV = (
    "DAEMON_CORE_MEMORY_PERFORMATIVE_RELATION_RETRACTIONS"
)
PERFORMATIVE_RELATION_RETRACTIONS_USER_IDS_ENV = (
    "DAEMON_CORE_MEMORY_PERFORMATIVE_RELATION_RETRACTIONS_USER_IDS"
)
MAX_PROPOSALS = 3
MAX_COLLECTION_PROPOSALS = 3
MAX_ROLE_CHARS = 240
MAX_EVIDENCE_CHARS = 600
ALLOWED_ACTIONS = frozenset({"upsert", "remove"})
ALLOWED_COLLECTION_ACTIONS = frozenset({"declare_open"})


@dataclass(frozen=True)
class MemoryRelationShadowContext:
    current_user_l0: GroundZeroReference
    source_ref: str
    source_trace_id: str


@dataclass(frozen=True)
class MemoryRelationShadowReceipt:
    status: str
    reason_code: str = ""
    event_id: str = ""
    accepted_count: int = 0
    rejected_count: int = 0
    accepted_collection_count: int = 0
    rejected_collection_count: int = 0


@dataclass(frozen=True)
class MemoryRelationValidation:
    accepted: tuple[dict[str, Any], ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class MemoryCollectionValidation:
    accepted: tuple[dict[str, Any], ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()


def memory_relation_shadow_enabled(user_id: str) -> bool:
    if os.getenv(RELATION_SHADOW_ENV, "off").strip().casefold() != "shadow":
        return False
    allowed = {
        value.strip()
        for value in os.getenv(RELATION_SHADOW_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return "*" in allowed or str(user_id).strip() in allowed


def memory_collection_bootstrap_shadow_enabled(user_id: str) -> bool:
    if (
        os.getenv(COLLECTION_BOOTSTRAP_SHADOW_ENV, "off").strip().casefold()
        != "shadow"
    ):
        return False
    allowed = {
        value.strip()
        for value in os.getenv(
            COLLECTION_BOOTSTRAP_SHADOW_USER_IDS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    return "*" in allowed or str(user_id).strip() in allowed


def memory_topology_bundle_shadow_enabled(user_id: str) -> bool:
    if (
        os.getenv(TOPOLOGY_BUNDLE_SHADOW_ENV, "off").strip().casefold()
        != "shadow"
    ):
        return False
    allowed = {
        value.strip()
        for value in os.getenv(
            TOPOLOGY_BUNDLE_SHADOW_USER_IDS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    return "*" in allowed or str(user_id).strip() in allowed


def memory_performative_relation_retractions_enabled(user_id: str) -> bool:
    """Enable relation-only mutations for explicitly named canary users."""

    if (
        os.getenv(
            PERFORMATIVE_RELATION_RETRACTIONS_ENV,
            "off",
        ).strip().casefold()
        != "canary"
    ):
        return False
    allowed = {
        value.strip()
        for value in os.getenv(
            PERFORMATIVE_RELATION_RETRACTIONS_USER_IDS_ENV,
            "",
        ).split(",")
        if value.strip() and value.strip() != "*"
    }
    return str(user_id).strip() in allowed


def memory_relation_shadow_trace_path(
    user_id: str,
    *,
    memory_root: Path | None = None,
) -> Path:
    root = MEMORY_ROOT if memory_root is None else memory_root
    return (
        root
        / "levels"
        / "memory_relation_shadow"
        / safe_memory_user_id(user_id)
        / "events.jsonl"
    )


def prepare_memory_relation_shadow_context(
    *,
    current_user_l0: GroundZeroReference | dict | None,
    user_message: str,
    memory_root: Path | None = None,
) -> MemoryRelationShadowContext | None:
    """Verify the current user turn and derive its exact Obsidian source link."""

    try:
        reference = _ground_zero_reference(current_user_l0)
        record = read_ground_zero_record(reference, memory_root=memory_root)
    except (FileNotFoundError, OSError, TypeError, UnicodeDecodeError, ValueError):
        return None
    if reference.role != "user" or record.role != "user":
        return None
    if record.body != user_message:
        return None
    source_ref = _obsidian_source_ref(reference, record.timestamp, record.role)
    if source_ref is None:
        return None
    return MemoryRelationShadowContext(
        current_user_l0=reference,
        source_ref=source_ref,
        source_trace_id=reference.turn_id,
    )


def record_memory_relation_shadow_result(
    *,
    user_id: str,
    context: MemoryRelationShadowContext,
    user_message: str,
    raw_updates: object,
    available_souvenirs: Iterable[str],
    proposed_souvenirs: Iterable[str] = (),
    raw_collection_updates: object | None = None,
) -> MemoryRelationShadowReceipt:
    """Validate and append one shadow receipt without applying any relation."""

    schema = (
        RELATION_SHADOW_SCHEMA_V2
        if raw_collection_updates is not None
        else RELATION_SHADOW_SCHEMA
    )
    event_id = _event_id(user_id, context.current_user_l0, schema=schema)
    base = {
        "schema": schema,
        "event_id": event_id,
        "observed_at": _now_iso(),
        "user_ref": _technical_ref(user_id),
        "source_trace_id": context.source_trace_id,
        "provenance": {
            "current_user_l0": context.current_user_l0.as_dict(),
            "source_ref": context.source_ref,
        },
    }
    verified = prepare_memory_relation_shadow_context(
        current_user_l0=context.current_user_l0,
        user_message=user_message,
    )
    if verified is None or verified.source_ref != context.source_ref:
        return _append_event(
            user_id=user_id,
            event={
                **base,
                "status": "rejected",
                "reason_code": "l0_provenance_mismatch",
                "accepted": [],
                "rejected": [],
            },
        )

    validation = validate_memory_relation_updates(
        raw_updates,
        user_message=user_message,
        available_souvenirs=available_souvenirs,
        proposed_souvenirs=proposed_souvenirs,
        source_ref=context.source_ref,
    )
    accepted = list(validation.accepted)
    rejected = list(validation.rejected)

    collection_validation = MemoryCollectionValidation()
    if raw_collection_updates is not None:
        collection_validation = validate_memory_collection_updates(
            raw_collection_updates,
            user_message=user_message,
            available_souvenirs=available_souvenirs,
            proposed_souvenirs=proposed_souvenirs,
            source_ref=context.source_ref,
            accepted_relations=accepted,
        )
    accepted_collections = list(collection_validation.accepted)
    rejected_collections = list(collection_validation.rejected)

    accepted_total = len(accepted) + len(accepted_collections)
    rejected_total = len(rejected) + len(rejected_collections)
    if accepted_total and rejected_total:
        status = "partial"
        reason_code = "some_proposals_rejected"
    elif accepted_total:
        status = "recorded"
        reason_code = ""
    elif rejected_total:
        status = "rejected"
        first_rejected = rejected or rejected_collections
        reason_code = first_rejected[0]["reason_code"]
    else:
        status = "abstained"
        reason_code = "judge_abstained"
    event = {
        **base,
        "status": status,
        "reason_code": reason_code,
        "accepted": accepted,
        "rejected": rejected,
    }
    if raw_collection_updates is not None:
        event.update(
            {
                "accepted_collections": accepted_collections,
                "rejected_collections": rejected_collections,
            }
        )
    return _append_event(
        user_id=user_id,
        event=event,
    )


def validate_memory_relation_updates(
    raw_updates: object,
    *,
    user_message: str,
    available_souvenirs: Iterable[str],
    source_ref: str,
    proposed_souvenirs: Iterable[str] = (),
) -> MemoryRelationValidation:
    """Apply only structural checks to a model-authored semantic judgment."""

    if raw_updates is None:
        raw_updates = []
    if not isinstance(raw_updates, list):
        return MemoryRelationValidation(
            rejected=(
                {"position": 0, "reason_code": "relation_updates_not_list"},
            )
        )
    if len(raw_updates) > MAX_PROPOSALS:
        return MemoryRelationValidation(
            rejected=(
                {"position": 0, "reason_code": "too_many_relation_updates"},
            )
        )

    known = _normalized_names((*available_souvenirs, *proposed_souvenirs))
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for position, raw in enumerate(raw_updates):
        proposal, reason = _validated_proposal(
            raw,
            user_message=user_message,
            known_souvenirs=known,
            source_ref=source_ref,
        )
        if proposal is None:
            rejected.append({"position": position, "reason_code": reason})
            continue
        key = (proposal["action"], proposal["source"], proposal["target"])
        if key in seen:
            rejected.append({"position": position, "reason_code": "duplicate_relation"})
            continue
        seen.add(key)
        accepted.append(proposal)
    return MemoryRelationValidation(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def validate_memory_collection_updates(
    raw_updates: object,
    *,
    user_message: str,
    available_souvenirs: Iterable[str],
    source_ref: str,
    accepted_relations: Iterable[dict[str, Any]],
    proposed_souvenirs: Iterable[str] = (),
) -> MemoryCollectionValidation:
    """Validate structure and provenance around a model-authored declaration."""

    if raw_updates is None:
        raw_updates = []
    if not isinstance(raw_updates, list):
        return MemoryCollectionValidation(
            rejected=(
                {"position": 0, "reason_code": "collection_updates_not_list"},
            )
        )
    if len(raw_updates) > MAX_COLLECTION_PROPOSALS:
        return MemoryCollectionValidation(
            rejected=(
                {"position": 0, "reason_code": "too_many_collection_updates"},
            )
        )

    known = _normalized_names((*available_souvenirs, *proposed_souvenirs))
    member_targets = {
        str(relation.get("target", ""))
        for relation in accepted_relations
        if isinstance(relation, dict)
        and relation.get("action") == "upsert"
        and relation.get("kind") == MEMBERSHIP_RELATION
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_updates):
        proposal, reason = _validated_collection_proposal(
            raw,
            user_message=user_message,
            known_souvenirs=known,
            source_ref=source_ref,
            member_targets=member_targets,
        )
        if proposal is None:
            rejected.append({"position": position, "reason_code": reason})
            continue
        target = proposal["target"]
        if target in seen:
            rejected.append(
                {"position": position, "reason_code": "duplicate_collection_update"}
            )
            continue
        seen.add(target)
        accepted.append(proposal)
    return MemoryCollectionValidation(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def read_memory_relation_shadow_events(
    user_id: str,
    *,
    memory_root: Path | None = None,
) -> list[dict[str, Any]]:
    path = memory_relation_shadow_trace_path(user_id, memory_root=memory_root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def read_memory_relation_shadow_event(
    user_id: str,
    event_id: str,
    *,
    memory_root: Path | None = None,
) -> dict[str, Any] | None:
    """Read one exact validated shadow envelope by its stable identifier."""

    expected_id = str(event_id).strip()
    if not expected_id:
        return None
    expected_user_ref = _technical_ref(user_id)
    for event in read_memory_relation_shadow_events(
        user_id,
        memory_root=memory_root,
    ):
        if (
            event.get("schema") in RELATION_SHADOW_SCHEMAS
            and event.get("event_id") == expected_id
            and event.get("user_ref") == expected_user_ref
        ):
            return event
    return None


def _validated_proposal(
    raw: object,
    *,
    user_message: str,
    known_souvenirs: set[str],
    source_ref: str,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(raw, dict):
        return None, "proposal_not_object"
    action = _bounded_text(raw.get("action"), 20)
    kind = _bounded_text(raw.get("kind"), 40)
    epistemic = _bounded_text(raw.get("epistemic"), 20)
    source = normalize_souvenir_name(str(raw.get("source", "")))
    target = normalize_souvenir_name(str(raw.get("target", "")))
    role = _bounded_text(raw.get("role"), MAX_ROLE_CHARS)
    evidence = _bounded_text(raw.get("evidence"), MAX_EVIDENCE_CHARS)
    if action not in ALLOWED_ACTIONS:
        return None, "unsupported_action"
    if kind != MEMBERSHIP_RELATION:
        return None, "unsupported_relation_kind"
    if epistemic != "established":
        return None, "membership_not_established"
    if source is None or target is None:
        return None, "invalid_capsule_path"
    if source == target:
        return None, "self_relation"
    if source not in known_souvenirs:
        return None, "unknown_source_capsule"
    if target not in known_souvenirs:
        return None, "unknown_target_capsule"
    if evidence is None or not evidence or evidence not in user_message:
        return None, "evidence_not_exact"
    if action == "upsert" and (role is None or not role):
        return None, "missing_relation_role"
    if action == "remove" and role is None:
        return None, "invalid_relation_role"
    return (
        {
            "action": action,
            "kind": kind,
            "epistemic": epistemic,
            "source": source,
            "target": target,
            "role": role or "",
            "evidence": evidence,
            "source_refs": [source_ref],
        },
        "",
    )


def _validated_collection_proposal(
    raw: object,
    *,
    user_message: str,
    known_souvenirs: set[str],
    source_ref: str,
    member_targets: set[str],
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(raw, dict):
        return None, "collection_proposal_not_object"
    action = _bounded_text(raw.get("action"), 40)
    epistemic = _bounded_text(raw.get("epistemic"), 20)
    target = normalize_souvenir_name(str(raw.get("target", "")))
    evidence = _bounded_text(raw.get("evidence"), MAX_EVIDENCE_CHARS)
    if action not in ALLOWED_COLLECTION_ACTIONS:
        return None, "unsupported_collection_action"
    if epistemic != "established":
        return None, "collection_not_established"
    if target is None:
        return None, "invalid_collection_path"
    if target not in known_souvenirs:
        return None, "unknown_collection_capsule"
    if evidence is None or not evidence or evidence not in user_message:
        return None, "collection_evidence_not_exact"
    if target not in member_targets:
        return None, "collection_without_accepted_member"
    return (
        {
            "action": action,
            "epistemic": epistemic,
            "target": target,
            "evidence": evidence,
            "source_refs": [source_ref],
        },
        "",
    )


def _append_event(
    *,
    user_id: str,
    event: dict[str, Any],
) -> MemoryRelationShadowReceipt:
    path = memory_relation_shadow_trace_path(user_id)
    event_id = str(event.get("event_id", ""))
    accepted = event.get("accepted", [])
    rejected = event.get("rejected", [])
    accepted_collections = event.get("accepted_collections", [])
    rejected_collections = event.get("rejected_collections", [])
    with memory_file_lock(path):
        for existing in read_memory_relation_shadow_events(user_id):
            if existing.get("event_id") == event_id:
                return MemoryRelationShadowReceipt(
                    status="duplicate",
                    reason_code="event_already_recorded",
                    event_id=event_id,
                    accepted_count=len(existing.get("accepted", [])),
                    rejected_count=len(existing.get("rejected", [])),
                    accepted_collection_count=len(
                        existing.get("accepted_collections", [])
                    ),
                    rejected_collection_count=len(
                        existing.get("rejected_collections", [])
                    ),
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    return MemoryRelationShadowReceipt(
        status=str(event.get("status", "")),
        reason_code=str(event.get("reason_code", "")),
        event_id=event_id,
        accepted_count=len(accepted) if isinstance(accepted, list) else 0,
        rejected_count=len(rejected) if isinstance(rejected, list) else 0,
        accepted_collection_count=(
            len(accepted_collections)
            if isinstance(accepted_collections, list)
            else 0
        ),
        rejected_collection_count=(
            len(rejected_collections)
            if isinstance(rejected_collections, list)
            else 0
        ),
    )


def _obsidian_source_ref(
    reference: GroundZeroReference,
    timestamp: str,
    role: str,
) -> str | None:
    raw_path = reference.path.strip().replace("\\", "/").lstrip("/")
    if raw_path.endswith(".md"):
        raw_path = raw_path[:-3]
    path = PurePosixPath(raw_path)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or not path.as_posix().startswith("levels/L0_ground_zero/")
        or not timestamp.strip()
        or role != "user"
    ):
        return None
    return f"{path.as_posix()}#{timestamp.strip()} — {role}"


def _ground_zero_reference(
    value: GroundZeroReference | dict | None,
) -> GroundZeroReference:
    if isinstance(value, GroundZeroReference):
        return value
    if not isinstance(value, dict):
        raise TypeError("ground_zero_reference_missing")
    return GroundZeroReference.from_dict(value)


def _normalized_names(values: Iterable[str]) -> set[str]:
    result = set()
    for value in values:
        name = normalize_souvenir_name(str(value))
        if name is not None:
            result.add(name)
    return result


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if len(cleaned) <= limit else None


def _event_id(
    user_id: str,
    reference: GroundZeroReference,
    *,
    schema: str = RELATION_SHADOW_SCHEMA,
) -> str:
    return hashlib.sha256(
        (
            schema
            + "\0"
            + str(user_id)
            + "\0"
            + reference.sha256
            + "\0"
            + reference.turn_id
        ).encode("utf-8")
    ).hexdigest()


def _technical_ref(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
