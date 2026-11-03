"""Shadow-only extraction of dyadic Common Ground.

The LLM judges whether one relational observation is worth proposing. This
module owns only deterministic safeguards: exact L0 provenance, bounded fields,
idempotent append-only traces, and a cursor over the previous assistant turn.
Nothing here is exposed to the response voice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.writer import (
    GroundZeroReference,
    read_ground_zero_reference,
    safe_memory_user_id,
)
from daemon_core.paths import MEMORY_ROOT


logger = logging.getLogger("daemon_core.common_ground_shadow")

COMMON_GROUND_SHADOW_ENV = "DAEMON_CORE_COMMON_GROUND_SHADOW"
COMMON_GROUND_SHADOW_USER_IDS_ENV = (
    "DAEMON_CORE_COMMON_GROUND_SHADOW_USER_IDS"
)
COMMON_GROUND_SHADOW_SCHEMA = "daemon.common-ground-shadow.v1"
COMMON_GROUND_CURSOR_SCHEMA = "daemon.common-ground-cursor.v1"
COMMON_GROUND_REGISTER_SCHEMA = "daemon.common-ground-register-shadow.v1"
ALLOWED_KINDS = frozenset({"shared_language", "relational_precedent"})
ALLOWED_UPDATE_ACTIONS = frozenset({"reinforce", "revise", "retire"})
MAX_REGISTER_ENTRIES = 5
MAX_REGISTER_EVIDENCE_EVENTS = 12
MAX_EVIDENCE_CHARS = 600
MAX_EXPRESSION_CHARS = 180
MAX_FIELD_CHARS = 900
MAX_CANDIDATE_CHARS = 5_000
MAX_UPDATE_CHARS = 4_000


@dataclass(frozen=True)
class CommonGroundObservation:
    """Exact T-1 assistant response and T user reaction."""

    source_trace_id: str
    source_turn_started_at: float
    prior_source_trace_id: str
    prior_source_turn_started_at: float
    prior_assistant_l0: GroundZeroReference
    current_user_l0: GroundZeroReference
    prior_assistant: str
    current_user: str


@dataclass(frozen=True)
class CommonGroundShadowReceipt:
    status: str
    reason_code: str = ""
    event_id: str = ""
    candidate_id: str = ""
    kind: str = ""
    action: str = ""


@dataclass(frozen=True)
class CommonGroundShadowTurn:
    """Fail-open lifecycle wrapper for one durable after-turn cycle."""

    enabled: bool
    user_id: str
    source_trace_id: str
    source_turn_started_at: float
    current_assistant_l0: GroundZeroReference | dict | None
    current_assistant: str
    observation: CommonGroundObservation | None = None
    register: tuple[dict[str, Any], ...] = ()

    @classmethod
    def begin(
        cls,
        *,
        user_id: str,
        source_trace_id: str,
        source_turn_started_at: float,
        current_user_l0: GroundZeroReference | dict | None,
        current_assistant_l0: GroundZeroReference | dict | None,
        current_user: str,
        current_assistant: str,
    ) -> "CommonGroundShadowTurn":
        enabled = common_ground_shadow_enabled(user_id)
        observation = None
        register: tuple[dict[str, Any], ...] = ()
        if enabled and source_trace_id and current_user_l0 is not None:
            observation = prepare_common_ground_observation(
                user_id=user_id,
                source_trace_id=source_trace_id,
                source_turn_started_at=source_turn_started_at,
                current_user_l0=current_user_l0,
                current_user=current_user,
            )
            try:
                register = tuple(read_common_ground_shadow_register(user_id))
            except Exception as exc:
                logger.info(
                    "Common Ground shadow register ignoré | user=%s | error=%s",
                    _technical_ref(user_id),
                    exc.__class__.__name__,
                )
        return cls(
            enabled=enabled,
            user_id=user_id,
            source_trace_id=source_trace_id,
            source_turn_started_at=source_turn_started_at,
            current_assistant_l0=current_assistant_l0,
            current_assistant=current_assistant,
            observation=observation,
            register=register,
        )

    def complete(
        self,
        *,
        missing_status: str,
        missing_reason: str,
        record_observed_skip: bool = False,
    ) -> None:
        if not self.enabled:
            return
        if (
            self.source_trace_id
            and (self.observation is None or record_observed_skip)
        ):
            try:
                record_common_ground_shadow_skip(
                    user_id=self.user_id,
                    source_trace_id=self.source_trace_id,
                    source_turn_started_at=self.source_turn_started_at,
                    status=missing_status,
                    reason_code=missing_reason,
                )
            except Exception as exc:
                logger.info(
                    "Common Ground shadow trace ignorée | source=%s | error=%s",
                    self.source_trace_id,
                    exc.__class__.__name__,
                )
        if (
            not self.source_trace_id
            or self.current_assistant_l0 is None
        ):
            return
        try:
            receipt = advance_common_ground_cursor(
                user_id=self.user_id,
                source_trace_id=self.source_trace_id,
                source_turn_started_at=self.source_turn_started_at,
                current_assistant_l0=self.current_assistant_l0,
                current_assistant=self.current_assistant,
            )
            logger.info(
                "Common Ground shadow cursor | source=%s | status=%s | reason=%s",
                self.source_trace_id,
                receipt.status,
                receipt.reason_code,
            )
        except Exception as exc:
            logger.info(
                "Common Ground shadow cursor ignoré | source=%s | error=%s",
                self.source_trace_id,
                exc.__class__.__name__,
            )


def common_ground_shadow_enabled(user_id: str) -> bool:
    if os.getenv(COMMON_GROUND_SHADOW_ENV, "off").strip().casefold() != "shadow":
        return False
    allowed = {
        value.strip()
        for value in os.getenv(
            COMMON_GROUND_SHADOW_USER_IDS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    return "*" in allowed or str(user_id).strip() in allowed


def common_ground_shadow_dir(user_id: str) -> Path:
    return (
        MEMORY_ROOT
        / "levels"
        / "common_ground_shadow"
        / safe_memory_user_id(user_id)
    )


def common_ground_shadow_trace_path(user_id: str) -> Path:
    return common_ground_shadow_dir(user_id) / "events.jsonl"


def common_ground_shadow_cursor_path(user_id: str) -> Path:
    return common_ground_shadow_dir(user_id) / "cursor.json"


def common_ground_shadow_register_path(user_id: str) -> Path:
    return common_ground_shadow_dir(user_id) / "register.json"


def prepare_common_ground_observation(
    *,
    user_id: str,
    source_trace_id: str,
    source_turn_started_at: float,
    current_user_l0: GroundZeroReference | dict,
    current_user: str,
) -> CommonGroundObservation | None:
    """Build an exact observation window from the previous durable cursor."""

    if not source_trace_id.strip():
        return None
    try:
        user_ref = _ground_zero_reference(current_user_l0)
        if user_ref.role != "user":
            return None
        if user_ref.turn_id and user_ref.turn_id != source_trace_id:
            return None
        if read_ground_zero_reference(user_ref) != current_user:
            return None
        cursor = _read_cursor(user_id)
        if cursor is None:
            return None
        prior_trace_id = str(cursor.get("source_trace_id", "")).strip()
        if not prior_trace_id or prior_trace_id == source_trace_id:
            return None
        prior_started_at = float(cursor.get("source_turn_started_at", 0.0))
        if (
            prior_started_at > 0
            and source_turn_started_at > 0
            and prior_started_at > source_turn_started_at
        ):
            return None
        assistant_ref = _ground_zero_reference(cursor.get("assistant_l0"))
        if assistant_ref.role != "assistant":
            return None
        if assistant_ref.turn_id and assistant_ref.turn_id != prior_trace_id:
            return None
        prior_assistant = read_ground_zero_reference(assistant_ref)
    except (FileNotFoundError, TypeError, ValueError):
        return None
    return CommonGroundObservation(
        source_trace_id=source_trace_id,
        source_turn_started_at=max(0.0, float(source_turn_started_at)),
        prior_source_trace_id=prior_trace_id,
        prior_source_turn_started_at=max(0.0, prior_started_at),
        prior_assistant_l0=assistant_ref,
        current_user_l0=user_ref,
        prior_assistant=prior_assistant,
        current_user=current_user,
    )


def advance_common_ground_cursor(
    *,
    user_id: str,
    source_trace_id: str,
    source_turn_started_at: float,
    current_assistant_l0: GroundZeroReference | dict,
    current_assistant: str,
) -> CommonGroundShadowReceipt:
    """Advance the exact previous-assistant cursor after one completed turn."""

    try:
        assistant_ref = _ground_zero_reference(current_assistant_l0)
        if assistant_ref.role != "assistant":
            raise ValueError("assistant_role_mismatch")
        if assistant_ref.turn_id and assistant_ref.turn_id != source_trace_id:
            raise ValueError("assistant_turn_mismatch")
        if read_ground_zero_reference(assistant_ref) != current_assistant:
            raise ValueError("assistant_content_mismatch")
    except (FileNotFoundError, TypeError, ValueError) as exc:
        return CommonGroundShadowReceipt(
            status="cursor_rejected",
            reason_code=str(exc)[:120] or exc.__class__.__name__,
        )

    path = common_ground_shadow_cursor_path(user_id)
    with memory_file_lock(path):
        current = _read_json_object(path)
        current_trace = str(current.get("source_trace_id", "")).strip()
        if current_trace == source_trace_id:
            return CommonGroundShadowReceipt(
                status="cursor_unchanged",
                reason_code="same_turn",
            )
        current_started_at = float(current.get("source_turn_started_at", 0.0))
        if (
            current_started_at > 0
            and source_turn_started_at > 0
            and current_started_at > source_turn_started_at
        ):
            return CommonGroundShadowReceipt(
                status="cursor_rejected",
                reason_code="stale_turn",
            )
        payload = {
            "schema": COMMON_GROUND_CURSOR_SCHEMA,
            "source_trace_id": source_trace_id,
            "source_turn_started_at": max(
                0.0,
                float(source_turn_started_at),
            ),
            "assistant_l0": assistant_ref.as_dict(),
            "updated_at": _now_iso(),
        }
        atomic_write_text(
            path,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        path.chmod(0o600)
    return CommonGroundShadowReceipt(
        status="cursor_advanced",
        reason_code="exact_assistant_l0",
    )


def record_common_ground_shadow_result(
    *,
    user_id: str,
    observation: CommonGroundObservation,
    raw_candidate: object,
    raw_update: object = None,
    register: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> CommonGroundShadowReceipt:
    """Validate and journal one LLM birth or consolidation act."""

    event_id = _event_id(user_id, observation.source_trace_id)
    if raw_candidate is not None and raw_update is not None:
        return _append_event(
            user_id=user_id,
            event={
                **_event_base(user_id, observation, event_id),
                "status": "rejected",
                "reason_code": "multiple_common_ground_actions",
            },
        )
    if raw_update is not None:
        current_register = (
            list(register)
            if register is not None
            else read_common_ground_shadow_register(user_id)
        )
        update, reason_code = _validated_update(
            observation=observation,
            raw=raw_update,
            register=current_register,
        )
        if update is None:
            return _append_event(
                user_id=user_id,
                event={
                    **_event_base(user_id, observation, event_id),
                    "status": "rejected",
                    "reason_code": reason_code,
                },
            )
        receipt = _append_event(
            user_id=user_id,
            event={
                **_event_base(user_id, observation, event_id),
                "status": "consolidated",
                "reason_code": f"validated_{update['action']}_evidence",
                "update": update,
            },
        )
        _refresh_register_safely(user_id)
        return receipt
    if raw_candidate is None:
        return _append_event(
            user_id=user_id,
            event={
                **_event_base(user_id, observation, event_id),
                "status": "none",
                "reason_code": "judge_abstained",
            },
        )
    candidate, reason_code = _validated_candidate(
        user_id=user_id,
        observation=observation,
        raw=raw_candidate,
    )
    if candidate is None:
        return _append_event(
            user_id=user_id,
            event={
                **_event_base(user_id, observation, event_id),
                "status": "rejected",
                "reason_code": reason_code,
            },
        )
    receipt = _append_event(
        user_id=user_id,
        event={
            **_event_base(user_id, observation, event_id),
            "status": "recorded",
            "reason_code": "validated_exact_evidence",
            "candidate": candidate,
        },
    )
    _refresh_register_safely(user_id)
    return receipt


def validate_common_ground_candidate(
    *,
    user_id: str,
    observation: CommonGroundObservation,
    raw_candidate: object,
) -> tuple[dict[str, Any] | None, str]:
    """Pure validation entry point for isolated evaluations."""

    if raw_candidate is None:
        return None, "judge_abstained"
    return _validated_candidate(
        user_id=user_id,
        observation=observation,
        raw=raw_candidate,
    )


def validate_common_ground_update(
    *,
    observation: CommonGroundObservation,
    raw_update: object,
    register: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any] | None, str]:
    """Pure consolidation validation entry point for isolated evaluations."""

    if raw_update is None:
        return None, "judge_abstained"
    return _validated_update(
        observation=observation,
        raw=raw_update,
        register=list(register),
    )


def record_common_ground_shadow_skip(
    *,
    user_id: str,
    source_trace_id: str,
    source_turn_started_at: float,
    status: str,
    reason_code: str,
) -> CommonGroundShadowReceipt:
    """Journal why no extraction judgment was available for one turn."""

    event_id = _event_id(user_id, source_trace_id)
    return _append_event(
        user_id=user_id,
        event={
            "schema": COMMON_GROUND_SHADOW_SCHEMA,
            "event_id": event_id,
            "observed_at": _now_iso(),
            "user_ref": _technical_ref(user_id),
            "source_trace_id": source_trace_id,
            "source_turn_started_at": max(
                0.0,
                float(source_turn_started_at),
            ),
            "status": _bounded_text(status, 80) or "skipped",
            "reason_code": _bounded_text(reason_code, 160) or "unspecified",
        },
    )


def read_common_ground_shadow_events(user_id: str) -> list[dict[str, Any]]:
    path = common_ground_shadow_trace_path(user_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def read_common_ground_shadow_register(
    user_id: str,
    *,
    limit: int = MAX_REGISTER_ENTRIES,
) -> list[dict[str, Any]]:
    """Return the bounded active projection, rebuilding it when stale."""

    events = read_common_ground_shadow_events(user_id)
    projection = _build_register_projection(events)
    path = common_ground_shadow_register_path(user_id)
    current = _read_json_object(path)
    stable_fields = (
        "schema",
        "source_event_count",
        "source_last_event_id",
        "entries",
    )
    if any(current.get(key) != projection.get(key) for key in stable_fields):
        payload = {
            **projection,
            "projected_at": _now_iso(),
        }
        with memory_file_lock(path):
            atomic_write_text(
                path,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )
            path.chmod(0o600)
        current = payload
    entries = current.get("entries", [])
    if not isinstance(entries, list):
        return []
    bounded = max(0, min(int(limit), MAX_REGISTER_ENTRIES))
    return [
        dict(entry)
        for entry in entries[:bounded]
        if isinstance(entry, dict)
    ]


def render_common_ground_observation(
    observation: CommonGroundObservation,
) -> str:
    return f"""# Common Ground observation window

The only cause-and-effect relationship you can judge here is:

## Previous assistant response (T-1)
{observation.prior_assistant}

## Following user reaction (T)
{observation.current_user}

The assistant response in the latest complete exchange comes after this reaction. It therefore proves no effect and must never serve as evidence for the candidate.
"""


def render_common_ground_register(
    register: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> str:
    """Render only the bounded candidate projection supplied to the judge."""

    visible = []
    for entry in list(register)[:MAX_REGISTER_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        visible.append(
            {
                "candidate_id": entry.get("candidate_id", ""),
                "state": entry.get("state", "candidate"),
                "shared_expression": entry.get("shared_expression", ""),
                "shared_meaning": entry.get("shared_meaning", ""),
            }
        )
    return (
        "# Common Ground candidate register\n\n"
        + json.dumps(
            visible,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _validated_candidate(
    *,
    user_id: str,
    observation: CommonGroundObservation,
    raw: object,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(raw, dict):
        return None, "candidate_not_object"
    kind = _bounded_text(raw.get("kind"), 80)
    if kind not in ALLOWED_KINDS:
        return None, "unsupported_kind"

    values = {
        "prior_assistant_evidence": _bounded_text(
            raw.get("prior_assistant_evidence"),
            MAX_EVIDENCE_CHARS,
        ),
        "current_user_evidence": _bounded_text(
            raw.get("current_user_evidence"),
            MAX_EVIDENCE_CHARS,
        ),
        "before": _bounded_text(raw.get("before"), MAX_FIELD_CHARS),
        "movement": _bounded_text(raw.get("movement"), MAX_FIELD_CHARS),
        "observed_effect": _bounded_text(
            raw.get("observed_effect"),
            MAX_FIELD_CHARS,
        ),
        "shared_expression": _bounded_text(
            raw.get("shared_expression"),
            MAX_EXPRESSION_CHARS,
        ),
        "shared_meaning": _bounded_text(
            raw.get("shared_meaning"),
            MAX_FIELD_CHARS,
        ),
        "limits": _bounded_text(raw.get("limits"), MAX_FIELD_CHARS),
        "reason": _bounded_text(raw.get("reason"), MAX_FIELD_CHARS),
    }
    if any(value is None for value in values.values()):
        return None, "field_too_long_or_invalid"
    prior_evidence = values["prior_assistant_evidence"] or ""
    user_evidence = values["current_user_evidence"] or ""
    if not _quoted_evidence_appears_in(prior_evidence, observation.prior_assistant):
        return None, "prior_assistant_evidence_not_exact"
    if not _quoted_evidence_appears_in(user_evidence, observation.current_user):
        return None, "current_user_evidence_not_exact"
    if not values["movement"] or not values["observed_effect"] or not values["limits"]:
        return None, "missing_relational_account"
    if kind == "shared_language":
        expression = values["shared_expression"] or ""
        if not expression or not values["shared_meaning"]:
            return None, "missing_shared_language"
        if not _quoted_evidence_appears_in(expression, observation.current_user):
            return None, "expression_missing_from_current_user"
    elif not values["before"]:
        return None, "missing_prior_state"

    candidate_core = {
        "kind": kind,
        "status": "candidate",
        "before": values["before"] or "",
        "movement": values["movement"] or "",
        "observed_effect": values["observed_effect"] or "",
        "shared_expression": values["shared_expression"] or "",
        "shared_meaning": values["shared_meaning"] or "",
        "limits": values["limits"] or "",
        "reason": values["reason"] or "",
        "evidence": {
            "prior_assistant": prior_evidence,
            "current_user": user_evidence,
        },
        "provenance": {
            "prior_source_trace_id": observation.prior_source_trace_id,
            "prior_assistant_l0": observation.prior_assistant_l0.as_dict(),
            "source_trace_id": observation.source_trace_id,
            "current_user_l0": observation.current_user_l0.as_dict(),
        },
    }
    encoded = json.dumps(
        candidate_core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > MAX_CANDIDATE_CHARS:
        return None, "candidate_too_large"
    candidate_id = hashlib.sha256(
        (
            COMMON_GROUND_SHADOW_SCHEMA
            + "\0"
            + user_id
            + "\0"
            + encoded
        ).encode("utf-8")
    ).hexdigest()
    return {
        "candidate_id": candidate_id,
        **candidate_core,
    }, ""


def _validated_update(
    *,
    observation: CommonGroundObservation,
    raw: object,
    register: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(raw, dict):
        return None, "update_not_object"
    action = _bounded_text(raw.get("action"), 80)
    if action not in ALLOWED_UPDATE_ACTIONS:
        return None, "unsupported_update_action"
    target_id = _bounded_text(raw.get("target_candidate_id"), 128)
    candidates = {
        str(entry.get("candidate_id", "")): entry
        for entry in register
        if isinstance(entry, dict)
    }
    target = candidates.get(target_id or "")
    if target is None:
        return None, "unknown_target_candidate"

    values = {
        "current_user_evidence": _bounded_text(
            raw.get("current_user_evidence"),
            MAX_EVIDENCE_CHARS,
        ),
        "shared_expression": _bounded_text(
            raw.get("shared_expression"),
            MAX_EXPRESSION_CHARS,
        ),
        "shared_meaning": _bounded_text(
            raw.get("shared_meaning"),
            MAX_FIELD_CHARS,
        ),
        "observed_use": _bounded_text(
            raw.get("observed_use"),
            MAX_FIELD_CHARS,
        ),
        "limits": _bounded_text(raw.get("limits"), MAX_FIELD_CHARS),
        "reason": _bounded_text(raw.get("reason"), MAX_FIELD_CHARS),
    }
    if any(value is None for value in values.values()):
        return None, "update_field_too_long_or_invalid"
    evidence = values["current_user_evidence"] or ""
    if not _quoted_evidence_appears_in(evidence, observation.current_user):
        return None, "update_user_evidence_not_exact"
    if (
        not values["observed_use"]
        or not values["limits"]
        or not values["reason"]
    ):
        return None, "missing_consolidation_account"
    if action == "revise":
        expression = values["shared_expression"] or ""
        if not expression or not values["shared_meaning"]:
            return None, "missing_revised_language"
        if not _quoted_evidence_appears_in(expression, observation.current_user):
            return None, "revised_expression_missing_from_user"

    update_core = {
        "kind": "shared_language",
        "action": action,
        "target_candidate_id": target_id,
        "current_user_evidence": evidence,
        "shared_expression": values["shared_expression"] or "",
        "shared_meaning": values["shared_meaning"] or "",
        "observed_use": values["observed_use"] or "",
        "limits": values["limits"] or "",
        "reason": values["reason"] or "",
        "candidate_before": {
            "state": str(target.get("state", "candidate")),
            "shared_expression": str(target.get("shared_expression", "")),
            "shared_meaning": str(target.get("shared_meaning", "")),
        },
        "provenance": {
            "source_trace_id": observation.source_trace_id,
            "current_user_l0": observation.current_user_l0.as_dict(),
        },
    }
    encoded = json.dumps(
        update_core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > MAX_UPDATE_CHARS:
        return None, "update_too_large"
    update_id = hashlib.sha256(
        (
            COMMON_GROUND_REGISTER_SCHEMA
            + "\0"
            + encoded
        ).encode("utf-8")
    ).hexdigest()
    return {
        "update_id": update_id,
        **update_core,
    }, ""


def _build_register_projection(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    relevant_events: list[dict[str, Any]] = []
    for position, event in enumerate(events):
        candidate = event.get("candidate")
        if (
            event.get("status") == "recorded"
            and isinstance(candidate, dict)
            and candidate.get("kind") == "shared_language"
        ):
            candidate_id = str(candidate.get("candidate_id", "")).strip()
            if not candidate_id:
                continue
            relevant_events.append(event)
            if candidate_id in entries:
                continue
            entries[candidate_id] = {
                "candidate_id": candidate_id,
                "state": "candidate",
                "shared_expression": str(
                    candidate.get("shared_expression", "")
                ),
                "shared_meaning": str(candidate.get("shared_meaning", "")),
                "origin": {
                    "event_id": str(event.get("event_id", "")),
                    "observed_at": str(event.get("observed_at", "")),
                    "provenance": candidate.get("provenance", {}),
                },
                "latest_evidence": {
                    "event_id": str(event.get("event_id", "")),
                    "observed_at": str(event.get("observed_at", "")),
                    "provenance": candidate.get("provenance", {}),
                },
                "evidence_event_ids": [str(event.get("event_id", ""))],
                "_position": position,
            }
            continue

        update = event.get("update")
        if (
            event.get("status") != "consolidated"
            or not isinstance(update, dict)
        ):
            continue
        candidate_id = str(update.get("target_candidate_id", "")).strip()
        entry = entries.get(candidate_id)
        if entry is None:
            continue
        action = str(update.get("action", "")).strip()
        if action not in ALLOWED_UPDATE_ACTIONS:
            continue
        relevant_events.append(event)
        entry["state"] = {
            "reinforce": "reinforced",
            "revise": "revised",
            "retire": "retired",
        }[action]
        if action == "revise":
            entry["shared_expression"] = str(
                update.get("shared_expression", "")
            )
            entry["shared_meaning"] = str(update.get("shared_meaning", ""))
        entry["latest_evidence"] = {
            "event_id": str(event.get("event_id", "")),
            "observed_at": str(event.get("observed_at", "")),
            "provenance": update.get("provenance", {}),
            "action": action,
        }
        evidence_ids = list(entry.get("evidence_event_ids", []))
        evidence_ids.append(str(event.get("event_id", "")))
        entry["evidence_event_ids"] = evidence_ids[
            -MAX_REGISTER_EVIDENCE_EVENTS:
        ]
        entry["_position"] = position

    active = [
        entry for entry in entries.values() if entry.get("state") != "retired"
    ]
    active.sort(key=lambda item: int(item.get("_position", 0)), reverse=True)
    projected = []
    for entry in active[:MAX_REGISTER_ENTRIES]:
        clean = dict(entry)
        clean.pop("_position", None)
        projected.append(clean)
    return {
        "schema": COMMON_GROUND_REGISTER_SCHEMA,
        "source_event_count": len(relevant_events),
        "source_last_event_id": (
            str(relevant_events[-1].get("event_id", ""))
            if relevant_events
            else ""
        ),
        "entries": projected,
    }


def _refresh_register_safely(user_id: str) -> None:
    try:
        read_common_ground_shadow_register(user_id)
    except Exception as exc:
        logger.info(
            "Common Ground shadow projection ignorée | user=%s | error=%s",
            _technical_ref(user_id),
            exc.__class__.__name__,
        )


def _append_event(
    *,
    user_id: str,
    event: dict[str, Any],
) -> CommonGroundShadowReceipt:
    path = common_ground_shadow_trace_path(user_id)
    event_id = str(event.get("event_id", ""))
    candidate = event.get("candidate")
    update = event.get("update")
    candidate_id = (
        str(candidate.get("candidate_id", ""))
        if isinstance(candidate, dict)
        else (
            str(update.get("target_candidate_id", ""))
            if isinstance(update, dict)
            else ""
        )
    )
    kind = (
        str(candidate.get("kind", ""))
        if isinstance(candidate, dict)
        else (
            str(update.get("kind", ""))
            if isinstance(update, dict)
            else ""
        )
    )
    action = (
        str(update.get("action", ""))
        if isinstance(update, dict)
        else ""
    )
    with memory_file_lock(path):
        if _event_exists(path, event_id):
            return CommonGroundShadowReceipt(
                status="duplicate",
                reason_code="event_already_recorded",
                event_id=event_id,
                candidate_id=candidate_id,
                kind=kind,
                action=action,
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
    return CommonGroundShadowReceipt(
        status=str(event.get("status", "")),
        reason_code=str(event.get("reason_code", "")),
        event_id=event_id,
        candidate_id=candidate_id,
        kind=kind,
        action=action,
    )


def _event_base(
    user_id: str,
    observation: CommonGroundObservation,
    event_id: str,
) -> dict[str, Any]:
    return {
        "schema": COMMON_GROUND_SHADOW_SCHEMA,
        "event_id": event_id,
        "observed_at": _now_iso(),
        "user_ref": _technical_ref(user_id),
        "source_trace_id": observation.source_trace_id,
        "source_turn_started_at": observation.source_turn_started_at,
        "prior_source_trace_id": observation.prior_source_trace_id,
    }


def _event_exists(path: Path, event_id: str) -> bool:
    if not event_id or not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event_id") == event_id:
            return True
    return False


def _read_cursor(user_id: str) -> dict[str, Any] | None:
    value = _read_json_object(common_ground_shadow_cursor_path(user_id))
    if value.get("schema") != COMMON_GROUND_CURSOR_SCHEMA:
        return None
    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _ground_zero_reference(
    value: GroundZeroReference | dict | None,
) -> GroundZeroReference:
    if isinstance(value, GroundZeroReference):
        return value
    if not isinstance(value, dict):
        raise TypeError("ground_zero_reference_missing")
    return GroundZeroReference.from_dict(value)


def _bounded_text(value: object, limit: int) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if len(cleaned) <= limit else None


def _quoted_evidence_appears_in(evidence: str, source: str) -> bool:
    normalized_evidence = _normalize_text(evidence)
    normalized_source = _normalize_text(source)
    return bool(
        normalized_evidence
        and normalized_evidence in normalized_source
    )


def _normalize_text(text: str) -> str:
    replacements = {
        "\u00a0": " ",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _event_id(user_id: str, source_trace_id: str) -> str:
    return hashlib.sha256(
        (
            COMMON_GROUND_SHADOW_SCHEMA
            + "\0"
            + user_id
            + "\0"
            + source_trace_id
        ).encode("utf-8")
    ).hexdigest()


def _technical_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "COMMON_GROUND_SHADOW_ENV",
    "COMMON_GROUND_SHADOW_USER_IDS_ENV",
    "COMMON_GROUND_REGISTER_SCHEMA",
    "CommonGroundObservation",
    "CommonGroundShadowReceipt",
    "CommonGroundShadowTurn",
    "advance_common_ground_cursor",
    "common_ground_shadow_cursor_path",
    "common_ground_shadow_enabled",
    "common_ground_shadow_register_path",
    "common_ground_shadow_trace_path",
    "prepare_common_ground_observation",
    "read_common_ground_shadow_events",
    "read_common_ground_shadow_register",
    "record_common_ground_shadow_result",
    "record_common_ground_shadow_skip",
    "render_common_ground_observation",
    "render_common_ground_register",
    "validate_common_ground_candidate",
    "validate_common_ground_update",
]
