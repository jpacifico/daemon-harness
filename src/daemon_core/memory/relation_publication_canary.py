"""Strictly bounded active publication for one accepted relation event.

The semantic decision remains the immutable relation-shadow event. This module
only authorizes one explicit user and event, then delegates the actual write to
the transactional Markdown publisher.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from daemon_core.memory.graph import MEMBERSHIP_RELATION
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.relation_outbox import (
    RELATION_PUBLICATION_ENV,
    RELATION_PUBLICATION_RECEIPT_DIR_ENV,
    RELATION_PUBLICATION_USER_IDS_ENV,
    relation_mutation_from_shadow_proposal,
)
from daemon_core.memory.relation_publication import (
    RelationPublicationError,
    apply_relation_mutation,
    plan_relation_mutation,
)
from daemon_core.memory.relation_shadow import read_memory_relation_shadow_event
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT


RELATION_PUBLICATION_CANARY_EVENT_IDS_ENV = (
    "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_EVENT_IDS"
)
RELATION_PUBLICATION_CANARY_SCHEMA = (
    "daemon.memory-relation-publication-canary.v1"
)


@dataclass(frozen=True)
class RelationPublicationCanaryReceipt:
    status: str
    reason_code: str
    event_id: str
    published_count: int = 0
    unchanged_count: int = 0
    blocked_count: int = 0
    replayed: bool = False


def memory_relation_publication_canary_enabled(
    user_id: str,
    event_id: str,
) -> bool:
    """Return true only for one explicit identity and one exact event."""

    if os.getenv(RELATION_PUBLICATION_ENV, "off").strip().casefold() != "canary":
        return False
    allowed_user = _single_explicit_value(RELATION_PUBLICATION_USER_IDS_ENV)
    allowed_event = _single_explicit_value(
        RELATION_PUBLICATION_CANARY_EVENT_IDS_ENV
    )
    canonical_event = _canonical_event_id(event_id)
    return (
        allowed_user is not None
        and allowed_user == str(user_id).strip()
        and allowed_event is not None
        and allowed_event == canonical_event
    )


def relation_publication_canary_receipt_path(
    user_id: str,
    event_id: str,
    *,
    receipt_root: Path | None = None,
) -> Path:
    root = _receipt_root() if receipt_root is None else receipt_root
    canonical_event = _canonical_event_id(event_id)
    filename = canonical_event or hashlib.sha256(
        str(event_id).strip().encode("utf-8")
    ).hexdigest()
    return root / "active" / safe_memory_user_id(user_id) / f"{filename}.json"


def run_relation_publication_canary(
    *,
    user_id: str,
    event_id: str,
    memory_root: Path | None = None,
    receipt_root: Path | None = None,
) -> RelationPublicationCanaryReceipt:
    """Publish the sole accepted relation from one explicitly allowed event."""

    canonical_event = _canonical_event_id(event_id)
    if canonical_event is None:
        return RelationPublicationCanaryReceipt(
            status="skipped",
            reason_code="invalid_event_id",
            event_id=str(event_id).strip(),
        )
    if not memory_relation_publication_canary_enabled(
        user_id,
        canonical_event,
    ):
        return RelationPublicationCanaryReceipt(
            status="skipped",
            reason_code="canary_not_enabled",
            event_id=canonical_event,
        )

    root = MEMORY_ROOT if memory_root is None else memory_root
    path = relation_publication_canary_receipt_path(
        user_id,
        canonical_event,
        receipt_root=receipt_root,
    )
    with memory_file_lock(path):
        existing = _read_receipt(path, user_id, canonical_event)
        if existing is not None:
            path.chmod(0o600)
            return replace(existing, replayed=True)

        event = read_memory_relation_shadow_event(
            user_id,
            canonical_event,
            memory_root=root,
        )
        proposal, reason_code = _sole_active_proposal(event)
        if proposal is None:
            payload = _receipt_payload(
                user_id=user_id,
                event_id=canonical_event,
                status="blocked",
                reason_code=reason_code,
            )
        else:
            try:
                mutation = relation_mutation_from_shadow_proposal(proposal)
                plan = plan_relation_mutation(
                    souvenirs_root=root / "capsules",
                    memory_root=root,
                    mutation=mutation,
                )
                result = apply_relation_mutation(
                    plan,
                    souvenirs_root=root / "capsules",
                    memory_root=root,
                    index_path=root / "levels" / "memory_graph.json",
                )
            except RelationPublicationError as exc:
                payload = _receipt_payload(
                    user_id=user_id,
                    event_id=canonical_event,
                    status="blocked",
                    reason_code=exc.reason_code,
                    proposal=proposal,
                )
            else:
                payload = _receipt_payload(
                    user_id=user_id,
                    event_id=canonical_event,
                    status=result.status,
                    reason_code="",
                    proposal=proposal,
                    index_fingerprint=result.index_fingerprint,
                )

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
        return _receipt_from_payload(payload)


def _sole_active_proposal(
    event: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    if event is None:
        return None, "shadow_event_missing"
    if event.get("status") != "recorded":
        return None, "shadow_event_not_recorded"
    collections = event.get("accepted_collections", [])
    if not isinstance(collections, list) or collections:
        return None, "collection_publication_not_allowed"
    accepted = event.get("accepted")
    if not isinstance(accepted, list) or len(accepted) != 1:
        return None, "exactly_one_relation_required"
    proposal = accepted[0]
    if not isinstance(proposal, dict):
        return None, "invalid_shadow_proposal"
    if proposal.get("action") != "upsert":
        return None, "active_canary_upsert_only"
    if proposal.get("kind") != MEMBERSHIP_RELATION:
        return None, "active_canary_member_of_only"
    if proposal.get("epistemic") != "established":
        return None, "active_canary_established_only"
    source_refs = proposal.get("source_refs")
    if (
        not isinstance(source_refs, list)
        or len(source_refs) != 1
        or not isinstance(source_refs[0], str)
    ):
        return None, "active_canary_exact_source_required"
    return proposal, ""


def _receipt_payload(
    *,
    user_id: str,
    event_id: str,
    status: str,
    reason_code: str,
    proposal: dict[str, Any] | None = None,
    index_fingerprint: str = "",
) -> dict[str, Any]:
    return {
        "schema": RELATION_PUBLICATION_CANARY_SCHEMA,
        "mode": "canary",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_ref": _technical_ref(user_id),
        "event_id": event_id,
        "status": status,
        "reason_code": reason_code,
        "published_count": int(status == "published"),
        "unchanged_count": int(status == "unchanged"),
        "blocked_count": int(status == "blocked"),
        "source_ref": _technical_ref(
            str(proposal.get("source", "")) if proposal else ""
        ),
        "target_ref": _technical_ref(
            str(proposal.get("target", "")) if proposal else ""
        ),
        "index_fingerprint": index_fingerprint,
    }


def _read_receipt(
    path: Path,
    user_id: str,
    event_id: str,
) -> RelationPublicationCanaryReceipt | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError("relation_canary_receipt_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != RELATION_PUBLICATION_CANARY_SCHEMA
        or payload.get("user_ref") != _technical_ref(user_id)
        or payload.get("event_id") != event_id
    ):
        raise RuntimeError("relation_canary_receipt_identity_mismatch")
    return _receipt_from_payload(payload)


def _receipt_from_payload(
    payload: dict[str, Any],
) -> RelationPublicationCanaryReceipt:
    return RelationPublicationCanaryReceipt(
        status=str(payload.get("status", "")),
        reason_code=str(payload.get("reason_code", "")),
        event_id=str(payload.get("event_id", "")),
        published_count=int(payload.get("published_count", 0)),
        unchanged_count=int(payload.get("unchanged_count", 0)),
        blocked_count=int(payload.get("blocked_count", 0)),
    )


def _single_explicit_value(name: str) -> str | None:
    values = [
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    ]
    if len(values) != 1 or values[0] == "*":
        return None
    return values[0]


def _canonical_event_id(value: str) -> str | None:
    candidate = str(value).strip()
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        return None
    return candidate


def _receipt_root() -> Path:
    configured = os.getenv(RELATION_PUBLICATION_RECEIPT_DIR_ENV, "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else PROJECT_ROOT / "exports" / "memory_relation_publication"
    )
    return root.resolve()


def _technical_ref(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "RELATION_PUBLICATION_CANARY_EVENT_IDS_ENV",
    "RELATION_PUBLICATION_CANARY_SCHEMA",
    "RelationPublicationCanaryReceipt",
    "memory_relation_publication_canary_enabled",
    "relation_publication_canary_receipt_path",
    "run_relation_publication_canary",
]
