"""Exact, read-only library projection for consolidated Common Ground."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import yaml

from daemon_core.common_ground_shadow import (
    MAX_REGISTER_ENTRIES,
    read_common_ground_shadow_register,
)
from daemon_core.memory.writer import (
    GroundZeroRecord,
    GroundZeroReference,
    read_ground_zero_record,
    safe_memory_user_id,
)


COMMON_GROUND_LIBRARY_ENTRY_SCHEMA = "daemon.common-ground-library-entry.v1"
COMMON_GROUND_LIBRARY_ENV = "DAEMON_CORE_COMMON_GROUND_LIBRARY"
COMMON_GROUND_LIBRARY_USER_IDS_ENV = (
    "DAEMON_CORE_COMMON_GROUND_LIBRARY_USER_IDS"
)
COMMON_GROUND_LIBRARY_PLATFORMS_ENV = (
    "DAEMON_CORE_COMMON_GROUND_LIBRARY_PLATFORMS"
)
ELIGIBLE_STATES = frozenset({"reinforced", "revised"})
MAX_SOURCE_BODY_CHARS = 12_000
MAX_LIBRARY_ENTRY_CHARS = 40_000
_CANDIDATE_ID = re.compile(r"[0-9a-f]{64}")

COMMON_GROUND_CONSUMPTION_CONTRACT = """The catalogue may contain
`common_ground` episodes. They serve to recognize what a shared expression or
allusion does in the present moment, not to decorate the response. The current
message retains authority over its referent: if it clearly attributes the
expression to a distinct object, place, person or event, do not read the episode
or invoke the private language, unless the user themselves asks for a connection.
Otherwise, before settling on a solely literal or local reading of a brief,
elliptical, metaphorical or rephrased message, judge whether an episode in the
catalogue could materially change its relational meaning; if so, read that
episode before responding. The fact that the message is intelligible without
reading does not prove that this local reading exhausts its meaning. Also read
before any claim about the origin, earlier meaning, chronology or historical
associations of shared language. Do not read when the episode would not change
the response. After reading, respond to the present moment without reciting
the memory or exposing its mechanism.""".strip()


@dataclass(frozen=True)
class SourcedCommonGroundEntry:
    """One consolidated lineage paired with its verified L0 evidence."""

    candidate_id: str
    state: str
    shared_expression: str
    shared_meaning: str
    origin_assistant: GroundZeroRecord
    origin_user: GroundZeroRecord
    latest_user_evidence: GroundZeroRecord
    origin_assistant_reference: GroundZeroReference
    origin_user_reference: GroundZeroReference
    latest_user_reference: GroundZeroReference

    @property
    def library_name(self) -> str:
        return f"common_ground/shared/{self.candidate_id}"

    @property
    def content(self) -> str:
        metadata = {
            "schema": COMMON_GROUND_LIBRARY_ENTRY_SCHEMA,
            "type": "common_ground",
            "subjects": [self.shared_expression],
            "facets": ["shared-language", "dyadic-memory"],
            "state": self.state,
            "candidate_id": self.candidate_id,
            "origin_turn_ids": [
                self.origin_assistant_reference.turn_id,
                self.origin_user_reference.turn_id,
            ],
            "latest_evidence_turn_id": self.latest_user_reference.turn_id,
        }
        frontmatter = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        return f"""---
{frontmatter}
---

# Shared Language: {self.shared_expression}

## Revisable Relational Projection

Current expression: {self.shared_expression}

Judged shared meaning: {self.shared_meaning}

This projection helps retrieve the lineage. It is not, on its own, evidence of what was said.

## Exact Original Episode

### Assistant — {self.origin_assistant.timestamp}

{self.origin_assistant.body}

### User — {self.origin_user.timestamp}

{self.origin_user.body}

## Exact Subsequent Evidence

### User — {self.latest_user_evidence.timestamp}

{self.latest_user_evidence.body}

## Epistemic Boundary

The statements and dates above are the exact sources. They prove only what they contain. Any relationship with a song, period, theme or other fact requires an independent source for each element.
""".strip()


@dataclass(frozen=True)
class CommonGroundLibraryRejection:
    candidate_id: str
    reason_code: str


@dataclass(frozen=True)
class CommonGroundLibraryResolution:
    entries: tuple[SourcedCommonGroundEntry, ...]
    rejections: tuple[CommonGroundLibraryRejection, ...]
    ignored_entries: int = 0

    @property
    def library_entries(self) -> tuple[tuple[str, str], ...]:
        return tuple((entry.library_name, entry.content) for entry in self.entries)


RecordReader = Callable[[GroundZeroReference | dict], GroundZeroRecord]


def common_ground_library_enabled(
    user_id: str,
    platform: str | None = None,
) -> bool:
    """Gate read exposure by mode, identity, and optionally host platform."""

    if os.getenv(COMMON_GROUND_LIBRARY_ENV, "").strip().casefold() not in {
        "1",
        "canary",
        "on",
        "true",
    }:
        return False
    allowed_users = {
        value.strip()
        for value in os.getenv(
            COMMON_GROUND_LIBRARY_USER_IDS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    if "*" not in allowed_users and str(user_id).strip() not in allowed_users:
        return False
    allowed_platforms = {
        value.strip().casefold()
        for value in os.getenv(
            COMMON_GROUND_LIBRARY_PLATFORMS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    normalized_platform = str(platform or "").strip().casefold()
    return (
        not allowed_platforms
        or "*" in allowed_platforms
        or normalized_platform in allowed_platforms
    )


def resolve_common_ground_library(
    *,
    user_id: str,
    register: Sequence[dict[str, Any]] | None = None,
    read_record: RecordReader = read_ground_zero_record,
    limit: int = MAX_REGISTER_ENTRIES,
) -> CommonGroundLibraryResolution:
    """Resolve eligible lineages or reject them without summary-only fallback."""

    source = (
        list(register)
        if register is not None
        else read_common_ground_shadow_register(user_id)
    )
    entries: list[SourcedCommonGroundEntry] = []
    rejections: list[CommonGroundLibraryRejection] = []
    ignored = 0
    bounded_limit = max(0, min(int(limit), MAX_REGISTER_ENTRIES))
    if bounded_limit == 0:
        return CommonGroundLibraryResolution(
            entries=(),
            rejections=(),
            ignored_entries=len(source),
        )
    for raw_entry in source:
        state = str(raw_entry.get("state", "")).strip()
        if state not in ELIGIBLE_STATES:
            ignored += 1
            continue
        candidate_id = str(raw_entry.get("candidate_id", "")).strip()
        try:
            entry = _resolve_entry(
                user_id=user_id,
                raw_entry=raw_entry,
                read_record=read_record,
            )
        except ValueError as exc:
            rejections.append(
                CommonGroundLibraryRejection(
                    candidate_id=candidate_id,
                    reason_code=str(exc) or "invalid_entry",
                )
            )
            continue
        entries.append(entry)
        if len(entries) >= bounded_limit:
            break
    return CommonGroundLibraryResolution(
        entries=tuple(entries),
        rejections=tuple(rejections),
        ignored_entries=ignored,
    )


def _resolve_entry(
    *,
    user_id: str,
    raw_entry: dict[str, Any],
    read_record: RecordReader,
) -> SourcedCommonGroundEntry:
    candidate_id = str(raw_entry.get("candidate_id", "")).strip()
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise ValueError("invalid_candidate_id")
    state = str(raw_entry.get("state", "")).strip()
    expected_action = {
        "reinforced": "reinforce",
        "revised": "revise",
    }.get(state)
    if expected_action is None:
        raise ValueError("ineligible_state")

    expression = str(raw_entry.get("shared_expression", "")).strip()
    meaning = str(raw_entry.get("shared_meaning", "")).strip()
    if not expression or not meaning:
        raise ValueError("missing_projection")

    evidence_ids = raw_entry.get("evidence_event_ids")
    if not isinstance(evidence_ids, list) or len(evidence_ids) < 2:
        raise ValueError("insufficient_evidence_events")
    origin = _mapping(raw_entry.get("origin"), "missing_origin")
    latest = _mapping(
        raw_entry.get("latest_evidence"),
        "missing_latest_evidence",
    )
    if str(latest.get("action", "")).strip() != expected_action:
        raise ValueError("state_action_mismatch")
    if str(origin.get("event_id", "")).strip() == str(
        latest.get("event_id", "")
    ).strip():
        raise ValueError("latest_evidence_is_origin")

    origin_provenance = _mapping(
        origin.get("provenance"),
        "missing_origin_provenance",
    )
    latest_provenance = _mapping(
        latest.get("provenance"),
        "missing_latest_provenance",
    )
    origin_assistant_ref = _reference(
        origin_provenance.get("prior_assistant_l0"),
        user_id=user_id,
        role="assistant",
    )
    origin_user_ref = _reference(
        origin_provenance.get("current_user_l0"),
        user_id=user_id,
        role="user",
    )
    latest_user_ref = _reference(
        latest_provenance.get("current_user_l0"),
        user_id=user_id,
        role="user",
    )
    if _same_reference(origin_user_ref, latest_user_ref):
        raise ValueError("latest_evidence_is_origin")

    try:
        origin_assistant = read_record(origin_assistant_ref)
        origin_user = read_record(origin_user_ref)
        latest_user = read_record(latest_user_ref)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("unresolvable_l0_reference") from None
    if (
        origin_assistant.role != "assistant"
        or origin_user.role != "user"
        or latest_user.role != "user"
    ):
        raise ValueError("resolved_role_mismatch")
    if any(
        len(record.body) > MAX_SOURCE_BODY_CHARS
        for record in (origin_assistant, origin_user, latest_user)
    ):
        raise ValueError("source_body_too_large")

    resolved = SourcedCommonGroundEntry(
        candidate_id=candidate_id,
        state=state,
        shared_expression=expression,
        shared_meaning=meaning,
        origin_assistant=origin_assistant,
        origin_user=origin_user,
        latest_user_evidence=latest_user,
        origin_assistant_reference=origin_assistant_ref,
        origin_user_reference=origin_user_ref,
        latest_user_reference=latest_user_ref,
    )
    if len(resolved.content) > MAX_LIBRARY_ENTRY_CHARS:
        raise ValueError("library_entry_too_large")
    return resolved


def _mapping(value: Any, reason_code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(reason_code)
    return value


def _reference(
    value: Any,
    *,
    user_id: str,
    role: str,
) -> GroundZeroReference:
    try:
        reference = GroundZeroReference.from_dict(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_l0_reference") from None
    if reference.role != role:
        raise ValueError("reference_role_mismatch")
    expected_prefix = (
        "levels/L0_ground_zero/conversations/"
        f"{safe_memory_user_id(user_id)}_"
    )
    if (
        not reference.path.startswith(expected_prefix)
        or not reference.path.endswith(".md")
    ):
        raise ValueError("cross_user_l0_reference")
    return reference


def _same_reference(
    left: GroundZeroReference,
    right: GroundZeroReference,
) -> bool:
    return (
        left.path,
        left.offset,
        left.length,
        left.sha256,
    ) == (
        right.path,
        right.offset,
        right.length,
        right.sha256,
    )


__all__ = [
    "COMMON_GROUND_CONSUMPTION_CONTRACT",
    "COMMON_GROUND_LIBRARY_ENV",
    "COMMON_GROUND_LIBRARY_ENTRY_SCHEMA",
    "COMMON_GROUND_LIBRARY_PLATFORMS_ENV",
    "COMMON_GROUND_LIBRARY_USER_IDS_ENV",
    "CommonGroundLibraryRejection",
    "CommonGroundLibraryResolution",
    "SourcedCommonGroundEntry",
    "common_ground_library_enabled",
    "resolve_common_ground_library",
]
