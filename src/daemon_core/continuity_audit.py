"""Read-only audit of append-only continuity receipt journals."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daemon_core.continuity import CONTINUITY_EVENT_SCHEMA


DEFAULT_ORPHAN_AFTER_SECONDS = 180.0
_TERMINAL_CYCLE_STATUSES = frozenset(
    {"completed", "failed", "skipped", "recovered"}
)


@dataclass(frozen=True)
class AuditedTurn:
    turn_id: str
    platform: str
    accepted_at: str
    updated_at: str
    status: str
    age_seconds: float
    idle_seconds: float
    cycle_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "platform": self.platform,
            "accepted_at": self.accepted_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "age_seconds": round(self.age_seconds, 3),
            "idle_seconds": round(self.idle_seconds, 3),
            "cycle_reason": self.cycle_reason,
        }


@dataclass(frozen=True)
class AuxiliaryStream:
    turn_id: str
    event_count: int
    stages: tuple[str, ...]
    threads: tuple[str, ...]
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "event_count": self.event_count,
            "stages": list(self.stages),
            "threads": list(self.threads),
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AuditIssue:
    code: str
    severity: str
    turn_id: str = ""
    line: int = 0
    details: str = ""

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
        }
        if self.turn_id:
            result["turn_id"] = self.turn_id
        if self.line:
            result["line"] = self.line
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class ContinuityAuditReport:
    journal: str
    generated_at: str
    orphan_after_seconds: float
    line_count: int
    event_count: int
    minimum_accepted_turns: int
    expected_accepted_turns: int | None
    accepted_turns: tuple[AuditedTurn, ...]
    auxiliary_streams: tuple[AuxiliaryStream, ...]
    issues: tuple[AuditIssue, ...]

    @property
    def status_counts(self) -> dict[str, int]:
        counts = Counter(turn.status for turn in self.accepted_turns)
        return {
            status: counts.get(status, 0)
            for status in (
                "completed",
                "recovered",
                "queued",
                "inflight",
                "orphaned",
                "failed",
                "skipped",
            )
        }

    @property
    def unhealthy(self) -> bool:
        counts = self.status_counts
        if counts["orphaned"] or counts["failed"] or counts["skipped"]:
            return True
        return any(issue.severity == "failure" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "daemon.continuity-audit.v1",
            "journal": self.journal,
            "generated_at": self.generated_at,
            "orphan_after_seconds": self.orphan_after_seconds,
            "healthy": not self.unhealthy,
            "line_count": self.line_count,
            "event_count": self.event_count,
            "minimum_accepted_turns": self.minimum_accepted_turns,
            "expected_accepted_turns": self.expected_accepted_turns,
            "accepted_turn_count": len(self.accepted_turns),
            "status_counts": self.status_counts,
            "auxiliary_stream_count": len(self.auxiliary_streams),
            "issue_count": len(self.issues),
            "accepted_turns": [
                turn.as_dict() for turn in self.accepted_turns
            ],
            "auxiliary_streams": [
                stream.as_dict() for stream in self.auxiliary_streams
            ],
            "issues": [issue.as_dict() for issue in self.issues],
        }


def audit_continuity_journal(
    journal: Path,
    *,
    orphan_after_seconds: float = DEFAULT_ORPHAN_AFTER_SECONDS,
    minimum_accepted_turns: int = 0,
    expected_accepted_turns: int | None = None,
    now: datetime | None = None,
) -> ContinuityAuditReport:
    """Classify accepted turns without changing the journal or runtime."""

    if orphan_after_seconds < 0:
        raise ValueError("orphan_after_seconds must be non-negative")
    if minimum_accepted_turns < 0:
        raise ValueError("minimum_accepted_turns must be non-negative")
    if expected_accepted_turns is not None and expected_accepted_turns < 0:
        raise ValueError("expected_accepted_turns must be non-negative")
    audit_now = _aware_utc(now or datetime.now(timezone.utc))
    path = Path(journal)
    lines = path.read_text(encoding="utf-8").splitlines()

    grouped: dict[str, list[dict[str, Any]]] = {}
    issues: list[AuditIssue] = []
    event_count = 0
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            issues.append(
                AuditIssue(
                    code="malformed_json",
                    severity="failure",
                    line=line_number,
                )
            )
            continue
        if (
            not isinstance(event, dict)
            or event.get("schema") != CONTINUITY_EVENT_SCHEMA
            or not str(event.get("turn_id", "")).strip()
        ):
            issues.append(
                AuditIssue(
                    code="invalid_event",
                    severity="failure",
                    line=line_number,
                )
            )
            continue
        turn_id = str(event["turn_id"])
        if _parse_event_time(event) is None:
            issues.append(
                AuditIssue(
                    code="invalid_event_timestamp",
                    severity="failure",
                    turn_id=turn_id,
                    line=line_number,
                )
            )
        grouped.setdefault(turn_id, []).append(event)
        event_count += 1

    accepted_turns: list[AuditedTurn] = []
    auxiliary_streams: list[AuxiliaryStream] = []
    for turn_id, events in grouped.items():
        acceptance_markers = [
            event
            for event in events
            if event.get("stage") == "turn"
            and event.get("status") == "pending"
            and event.get("reason_code") == "accepted"
        ]
        accepted = [
            event
            for event in acceptance_markers
            if isinstance(event.get("identity"), dict)
        ]
        if not accepted:
            if acceptance_markers:
                issues.append(
                    AuditIssue(
                        code="invalid_acceptance_identity",
                        severity="failure",
                        turn_id=turn_id,
                    )
                )
            auxiliary_streams.append(
                _auxiliary_stream(turn_id, events)
            )
            continue
        if len(accepted) > 1:
            issues.append(
                AuditIssue(
                    code="duplicate_acceptance",
                    severity="failure",
                    turn_id=turn_id,
                    details=f"count={len(accepted)}",
                )
            )

        accepted_event = accepted[0]
        accepted_at = _parse_event_time(accepted_event)
        timed_events = [
            (parsed, event)
            for event in events
            if (
                parsed := _parse_event_time(event)
            )
            is not None
        ]
        if accepted_at is None or not timed_events:
            issues.append(
                AuditIssue(
                    code="unusable_turn_timestamps",
                    severity="failure",
                    turn_id=turn_id,
                )
            )
            continue
        updated_at = max(parsed for parsed, _event in timed_events)
        cycle_events = [
            (parsed, event)
            for parsed, event in timed_events
            if event.get("stage") == "cycle"
        ]
        terminal_cycles = [
            (parsed, event)
            for parsed, event in cycle_events
            if event.get("status") in _TERMINAL_CYCLE_STATUSES
        ]
        terminal_cycle = (
            max(terminal_cycles, key=lambda item: item[0])
            if terminal_cycles
            else None
        )
        outbox_events = [
            (parsed, event)
            for parsed, event in timed_events
            if event.get("stage") == "outbox"
        ]
        latest_outbox = (
            max(outbox_events, key=lambda item: item[0])
            if outbox_events
            else None
        )
        scene_events = [
            (parsed, event)
            for parsed, event in timed_events
            if event.get("stage") == "scene_schedule"
        ]
        latest_scene = (
            max(scene_events, key=lambda item: item[0])
            if scene_events
            else None
        )
        memory_mutation_events = [
            (parsed, event)
            for parsed, event in timed_events
            if event.get("stage") == "memory_mutation"
        ]
        latest_memory_mutation = (
            max(memory_mutation_events, key=lambda item: item[0])
            if memory_mutation_events
            else None
        )
        age_seconds = max(
            0.0,
            (audit_now - accepted_at).total_seconds(),
        )
        idle_seconds = max(
            0.0,
            (audit_now - updated_at).total_seconds(),
        )
        outbox_requeues_after_cycle = (
            latest_outbox is not None
            and (
                str(latest_outbox[1].get("status", "")) == "pending"
                or (
                    str(latest_outbox[1].get("status", ""))
                    == "recovered"
                    and str(
                        latest_outbox[1].get("reason_code", "")
                    )
                    == "stale_lease_requeued"
                )
            )
            and (
                terminal_cycle is None
                or latest_outbox[0] > terminal_cycle[0]
            )
        )
        if outbox_requeues_after_cycle:
            status = "queued"
            cycle_reason = str(
                latest_outbox[1].get("reason_code", "")
            )
        elif terminal_cycle is not None:
            cycle_event = terminal_cycle[1]
            status = str(cycle_event.get("status", ""))
            cycle_reason = str(cycle_event.get("reason_code", ""))
        elif (
            latest_scene is not None
            and latest_scene[1].get("status") == "pending"
            and str(
                latest_scene[1].get("reason_code", "")
            )
            in {
                "durable_projection_deferred",
                "durable_projection_enqueued",
                "projection_claimed",
                "projection_retry_scheduled",
                "projection_stale_requeued",
            }
        ):
            status = "queued"
            cycle_reason = str(
                latest_scene[1].get("reason_code", "")
            )
        elif latest_outbox is not None:
            outbox_event = latest_outbox[1]
            outbox_status = str(outbox_event.get("status", ""))
            outbox_reason = str(outbox_event.get("reason_code", ""))
            if outbox_status == "pending" or (
                outbox_status == "recovered"
                and outbox_reason == "stale_lease_requeued"
            ):
                status = "queued"
                cycle_reason = outbox_reason
            elif outbox_status == "failed":
                status = "failed"
                cycle_reason = outbox_reason
            else:
                status = "failed"
                cycle_reason = "outbox_terminal_without_cycle"
                issues.append(
                    AuditIssue(
                        code="outbox_terminal_without_cycle",
                        severity="failure",
                        turn_id=turn_id,
                        details=(
                            f"status={outbox_status},"
                            f"reason={outbox_reason}"
                        ),
                    )
                )
        else:
            status = (
                "orphaned"
                if age_seconds > orphan_after_seconds
                else "inflight"
            )
            cycle_reason = ""
        if (
            latest_memory_mutation is not None
            and latest_memory_mutation[1].get("status") == "failed"
            and latest_memory_mutation[1].get("reason_code")
            == "direct_fact_write_rejected"
        ):
            status = "failed"
            cycle_reason = "direct_fact_write_rejected"

        identity = accepted_event.get("identity", {})
        platform = str(identity.get("platform", "")).strip() or "unknown"
        if (
            not str(identity.get("user_ref", "")).strip()
            or platform == "unknown"
        ):
            issues.append(
                AuditIssue(
                    code="incomplete_acceptance_identity",
                    severity="failure",
                    turn_id=turn_id,
                )
            )
        accepted_turns.append(
            AuditedTurn(
                turn_id=turn_id,
                platform=platform,
                accepted_at=accepted_at.isoformat(timespec="milliseconds"),
                updated_at=updated_at.isoformat(timespec="milliseconds"),
                status=status,
                age_seconds=age_seconds,
                idle_seconds=idle_seconds,
                cycle_reason=cycle_reason,
            )
        )
        issues.extend(_turn_integrity_issues(turn_id, events, status))

    accepted_turns.sort(key=lambda turn: (turn.accepted_at, turn.turn_id))
    auxiliary_streams.sort(
        key=lambda stream: (stream.updated_at, stream.turn_id)
    )
    if len(accepted_turns) < minimum_accepted_turns:
        issues.append(
            AuditIssue(
                code="insufficient_accepted_turns",
                severity="failure",
                details=(
                    f"actual={len(accepted_turns)},"
                    f"minimum={minimum_accepted_turns}"
                ),
            )
        )
    if (
        expected_accepted_turns is not None
        and len(accepted_turns) != expected_accepted_turns
    ):
        issues.append(
            AuditIssue(
                code="accepted_turn_count_mismatch",
                severity="failure",
                details=(
                    f"actual={len(accepted_turns)},"
                    f"expected={expected_accepted_turns}"
                ),
            )
        )
    return ContinuityAuditReport(
        journal=str(path),
        generated_at=audit_now.isoformat(timespec="milliseconds"),
        orphan_after_seconds=float(orphan_after_seconds),
        line_count=len(lines),
        event_count=event_count,
        minimum_accepted_turns=minimum_accepted_turns,
        expected_accepted_turns=expected_accepted_turns,
        accepted_turns=tuple(accepted_turns),
        auxiliary_streams=tuple(auxiliary_streams),
        issues=tuple(issues),
    )


def render_continuity_audit(report: ContinuityAuditReport) -> str:
    """Render a compact operator summary without personal conversation data."""

    counts = report.status_counts
    state = "UNHEALTHY" if report.unhealthy else "HEALTHY"
    lines = [
        f"Continuity audit: {state}",
        f"Journal: {report.journal}",
        (
            f"Accepted turns: {len(report.accepted_turns)} "
            f"(completed={counts['completed']}, "
            f"recovered={counts['recovered']}, "
            f"queued={counts['queued']}, "
            f"inflight={counts['inflight']}, "
            f"orphaned={counts['orphaned']}, "
            f"failed={counts['failed']}, "
            f"skipped={counts['skipped']})"
        ),
        (
            f"Events: {report.event_count}/{report.line_count} valid; "
            f"auxiliary streams={len(report.auxiliary_streams)}; "
            f"issues={len(report.issues)}"
        ),
        (
            "Orphan budget: "
            f"{report.orphan_after_seconds:g}s from accepted turn"
        ),
    ]
    for turn in report.accepted_turns:
        if turn.status not in {"completed", "recovered"}:
            lines.append(
                f"- {turn.status}: {turn.turn_id} "
                f"platform={turn.platform} "
                f"age={turn.age_seconds:.1f}s "
                f"idle={turn.idle_seconds:.1f}s"
            )
    for stream in report.auxiliary_streams:
        lines.append(
            f"- auxiliary: {stream.turn_id} "
            f"events={stream.event_count} "
            f"threads={','.join(stream.threads) or 'unknown'}"
        )
    for issue in report.issues:
        location = issue.turn_id or (
            f"line={issue.line}" if issue.line else "journal"
        )
        lines.append(
            f"- {issue.severity}: {issue.code} ({location})"
        )
    return "\n".join(lines)


def _auxiliary_stream(
    turn_id: str,
    events: list[dict[str, Any]],
) -> AuxiliaryStream:
    timestamps = [
        parsed
        for event in events
        if (
            parsed := _parse_event_time(event)
        )
        is not None
    ]
    updated_at = (
        max(timestamps).isoformat(timespec="milliseconds")
        if timestamps
        else ""
    )
    return AuxiliaryStream(
        turn_id=turn_id,
        event_count=len(events),
        stages=tuple(
            sorted(
                {
                    str(event.get("stage", ""))
                    for event in events
                    if str(event.get("stage", "")).strip()
                }
            )
        ),
        threads=tuple(
            sorted(
                {
                    str(event.get("thread", ""))
                    for event in events
                    if str(event.get("thread", "")).strip()
                }
            )
        ),
        updated_at=updated_at,
    )


def _turn_integrity_issues(
    turn_id: str,
    events: list[dict[str, Any]],
    status: str,
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    exact_counts = {
        stage: sum(
            1
            for event in events
            if event.get("stage") == stage
            and event.get("status") == "completed"
            and event.get("reason_code") == "writer_completed"
        )
        for stage in ("l0_user", "l0_assistant")
    }
    for stage, count in exact_counts.items():
        if count > 1:
            issues.append(
                AuditIssue(
                    code=f"duplicate_{stage}",
                    severity="failure",
                    turn_id=turn_id,
                    details=f"count={count}",
                )
            )

    latest_by_stage: dict[str, dict[str, Any]] = {}
    for event in events:
        latest_by_stage[str(event.get("stage", ""))] = event
    l0_event = latest_by_stage.get("l0", {})
    l0_details = (
        l0_event.get("details", {})
        if isinstance(l0_event.get("details"), dict)
        else {}
    )
    expected = {
        "l0_user": l0_details.get("user_l0_recorded") is True,
        "l0_assistant": (
            l0_details.get("assistant_l0_recorded") is True
        ),
    }
    for stage, required in expected.items():
        if required and exact_counts[stage] == 0:
            issues.append(
                AuditIssue(
                    code=f"unverified_{stage}",
                    severity="failure",
                    turn_id=turn_id,
                )
            )

    if status in _TERMINAL_CYCLE_STATUSES:
        for stage in ("l0", "memory", "scene_schedule"):
            stage_status = str(
                latest_by_stage.get(stage, {}).get("status", "")
            )
            if stage_status not in _TERMINAL_CYCLE_STATUSES:
                issues.append(
                    AuditIssue(
                        code=f"terminal_cycle_with_pending_{stage}",
                        severity="failure",
                        turn_id=turn_id,
                    )
                )
    memory_mutation = latest_by_stage.get("memory_mutation", {})
    mutation_details = (
        memory_mutation.get("details", {})
        if isinstance(memory_mutation.get("details"), dict)
        else {}
    )
    if (
        memory_mutation.get("status") == "failed"
        and memory_mutation.get("reason_code")
        == "direct_fact_write_rejected"
        and mutation_details.get("source_posture") == "direct_fact"
    ):
        rejected = mutation_details.get("rejected_paths", [])
        issues.append(
            AuditIssue(
                code="direct_fact_write_rejected",
                severity="failure",
                turn_id=turn_id,
                details=(
                    f"rejected_paths={len(rejected)}"
                    if isinstance(rejected, list)
                    else "rejected_paths=unknown"
                ),
            )
        )
    return issues


def _parse_event_time(event: dict[str, Any]) -> datetime | None:
    value = str(event.get("at", "")).strip()
    try:
        return _aware_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_ORPHAN_AFTER_SECONDS",
    "AuditedTurn",
    "AuxiliaryStream",
    "AuditIssue",
    "ContinuityAuditReport",
    "audit_continuity_journal",
    "render_continuity_audit",
]
