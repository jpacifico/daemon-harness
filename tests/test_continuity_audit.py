from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daemon_core.continuity import CONTINUITY_EVENT_SCHEMA
from daemon_core.continuity_audit import (
    audit_continuity_journal,
    render_continuity_audit,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _event(
    turn_id: str,
    *,
    at: datetime,
    stage: str,
    status: str,
    reason_code: str,
    identity: dict | None = None,
    details: dict | None = None,
    thread: str = "test",
) -> dict:
    event = {
        "schema": CONTINUITY_EVENT_SCHEMA,
        "turn_id": turn_id,
        "at": at.isoformat(timespec="milliseconds"),
        "stage": stage,
        "status": status,
        "reason_code": reason_code,
        "thread": thread,
        "details": details or {},
    }
    if identity is not None:
        event["identity"] = identity
    return event


def _accepted(turn_id: str, at: datetime) -> dict:
    return _event(
        turn_id,
        at=at,
        stage="turn",
        status="pending",
        reason_code="accepted",
        identity={
            "user_ref": "user-ref",
            "platform": "telegram",
            "session_ref": "session-ref",
        },
    )


def _write(path: Path, *events: dict, extra_lines: tuple[str, ...] = ()):
    lines = [json.dumps(event) for event in events]
    lines.extend(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_audit_closes_nominal_accepted_turn(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(seconds=50)
    _write(
        path,
        _accepted("nominal", accepted_at),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=1),
            stage="outbox",
            status="pending",
            reason_code="durable_enqueued",
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=10),
            stage="l0",
            status="completed",
            reason_code="hook_completed",
            details={
                "user_l0_recorded": True,
                "assistant_l0_recorded": True,
            },
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=11),
            stage="l0_user",
            status="completed",
            reason_code="writer_completed",
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=12),
            stage="l0_assistant",
            status="completed",
            reason_code="writer_completed",
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=20),
            stage="memory",
            status="completed",
            reason_code="memory_completed",
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=30),
            stage="scene_schedule",
            status="completed",
            reason_code="scene_scheduled",
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=31),
            stage="cycle",
            status="completed",
            reason_code="after_turn_completed",
        ),
        _event(
            "nominal",
            at=accepted_at + timedelta(seconds=32),
            stage="outbox",
            status="completed",
            reason_code="worker_completed",
        ),
    )

    report = audit_continuity_journal(path, now=NOW)

    assert report.unhealthy is False
    assert report.status_counts["completed"] == 1
    assert report.status_counts["orphaned"] == 0
    assert report.issues == ()
    assert "Continuity audit: HEALTHY" in render_continuity_audit(report)


def test_audit_exposes_a_rejected_direct_fact_as_failed(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(seconds=50)
    _write(
        path,
        _accepted("direct-fact-rejected", accepted_at),
        _event(
            "direct-fact-rejected",
            at=accepted_at + timedelta(seconds=1),
            stage="l0",
            status="completed",
            reason_code="adapter_reported",
            details={
                "user_l0_recorded": False,
                "assistant_l0_recorded": False,
            },
        ),
        _event(
            "direct-fact-rejected",
            at=accepted_at + timedelta(seconds=2),
            stage="memory",
            status="completed",
            reason_code="hook_completed",
        ),
        _event(
            "direct-fact-rejected",
            at=accepted_at + timedelta(seconds=3),
            stage="scene_schedule",
            status="skipped",
            reason_code="hook_not_configured",
        ),
        _event(
            "direct-fact-rejected",
            at=accepted_at + timedelta(seconds=4),
            stage="cycle",
            status="completed",
            reason_code="after_turn_completed",
        ),
        _event(
            "direct-fact-rejected",
            at=accepted_at + timedelta(seconds=5),
            stage="memory_mutation",
            status="failed",
            reason_code="direct_fact_write_rejected",
            details={
                "source_posture": "direct_fact",
                "rejected_paths": ["users/u/agenda.md"],
            },
        ),
    )

    report = audit_continuity_journal(path, now=NOW)

    assert report.unhealthy is True
    assert report.status_counts["failed"] == 1
    assert report.accepted_turns[0].cycle_reason == (
        "direct_fact_write_rejected"
    )
    assert [issue.code for issue in report.issues] == [
        "direct_fact_write_rejected"
    ]


def test_audit_distinguishes_inflight_from_orphan(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    _write(
        path,
        _accepted("inflight", NOW - timedelta(seconds=30)),
        _event(
            "inflight",
            at=NOW - timedelta(seconds=20),
            stage="cycle",
            status="pending",
            reason_code="after_turn_accepted",
        ),
        _accepted("orphan", NOW - timedelta(seconds=240)),
        _event(
            "orphan",
            at=NOW - timedelta(seconds=220),
            stage="cycle",
            status="pending",
            reason_code="after_turn_accepted",
        ),
    )

    report = audit_continuity_journal(
        path,
        now=NOW,
        orphan_after_seconds=180,
    )

    assert report.status_counts["inflight"] == 1
    assert report.status_counts["orphaned"] == 1
    assert report.unhealthy is True


def test_durable_pending_turn_is_queued_instead_of_orphaned(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(minutes=10)
    _write(
        path,
        _accepted("durable-pending", accepted_at),
        _event(
            "durable-pending",
            at=accepted_at + timedelta(seconds=1),
            stage="outbox",
            status="pending",
            reason_code="durable_enqueued",
        ),
        _event(
            "durable-pending",
            at=accepted_at + timedelta(minutes=2),
            stage="outbox",
            status="pending",
            reason_code="retry_scheduled",
        ),
    )

    report = audit_continuity_journal(
        path,
        now=NOW,
        orphan_after_seconds=180,
    )

    assert report.status_counts["queued"] == 1
    assert report.status_counts["orphaned"] == 0
    assert report.accepted_turns[0].cycle_reason == "retry_scheduled"
    assert report.unhealthy is False


def test_exhausted_outbox_is_a_visible_failure(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(minutes=10)
    _write(
        path,
        _accepted("durable-failed", accepted_at),
        _event(
            "durable-failed",
            at=accepted_at + timedelta(minutes=1),
            stage="outbox",
            status="failed",
            reason_code="attempts_exhausted",
        ),
    )

    report = audit_continuity_journal(path, now=NOW)

    assert report.status_counts["failed"] == 1
    assert report.accepted_turns[0].cycle_reason == "attempts_exhausted"
    assert report.unhealthy is True


def test_retry_scheduled_after_failed_cycle_is_still_queued(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(minutes=10)
    _write(
        path,
        _accepted("durable-retry", accepted_at),
        _event(
            "durable-retry",
            at=accepted_at + timedelta(minutes=1),
            stage="cycle",
            status="failed",
            reason_code="after_turn_failed",
        ),
        _event(
            "durable-retry",
            at=accepted_at + timedelta(minutes=1, seconds=1),
            stage="outbox",
            status="pending",
            reason_code="retry_scheduled",
        ),
    )

    report = audit_continuity_journal(path, now=NOW)

    assert report.status_counts["queued"] == 1
    assert report.status_counts["failed"] == 0
    assert report.accepted_turns[0].cycle_reason == "retry_scheduled"
    assert report.unhealthy is False


def test_outbox_completion_without_a_terminal_cycle_is_inconsistent(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(seconds=20)
    _write(
        path,
        _accepted("false-outbox-complete", accepted_at),
        _event(
            "false-outbox-complete",
            at=accepted_at + timedelta(seconds=10),
            stage="outbox",
            status="completed",
            reason_code="worker_completed",
        ),
    )

    report = audit_continuity_journal(path, now=NOW)

    assert report.status_counts["failed"] == 1
    assert "outbox_terminal_without_cycle" in {
        issue.code for issue in report.issues
    }
    assert report.unhealthy is True


def test_auxiliary_host_activity_is_not_a_user_orphan(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    _write(
        path,
        _event(
            "background-review",
            at=NOW - timedelta(hours=1),
            stage="voice_path",
            status="completed",
            reason_code="delegated",
            thread="bg-review",
        ),
        _event(
            "background-review",
            at=NOW - timedelta(minutes=59),
            stage="response",
            status="completed",
            reason_code="host_generation_completed",
            thread="bg-review",
        ),
    )

    report = audit_continuity_journal(
        path,
        now=NOW,
        orphan_after_seconds=1,
    )

    assert report.unhealthy is False
    assert report.accepted_turns == ()
    assert len(report.auxiliary_streams) == 1
    assert report.auxiliary_streams[0].threads == ("bg-review",)


def test_expected_turn_count_detects_a_silent_coverage_gap(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    _write(path, _accepted("only-turn", NOW - timedelta(seconds=10)))

    report = audit_continuity_journal(
        path,
        now=NOW,
        expected_accepted_turns=2,
    )

    assert report.unhealthy is True
    assert {issue.code for issue in report.issues} == {
        "accepted_turn_count_mismatch"
    }


def test_malformed_line_and_terminal_inconsistency_fail_audit(tmp_path):
    path = tmp_path / "continuity_receipts.jsonl"
    accepted_at = NOW - timedelta(seconds=10)
    _write(
        path,
        _accepted("false-complete", accepted_at),
        _event(
            "false-complete",
            at=accepted_at + timedelta(seconds=2),
            stage="cycle",
            status="completed",
            reason_code="after_turn_completed",
        ),
        extra_lines=("{not-json",),
    )

    report = audit_continuity_journal(path, now=NOW)
    codes = {issue.code for issue in report.issues}

    assert report.unhealthy is True
    assert "malformed_json" in codes
    assert "terminal_cycle_with_pending_l0" in codes
    assert "terminal_cycle_with_pending_memory" in codes
    assert "terminal_cycle_with_pending_scene_schedule" in codes


def test_abrupt_process_exit_leaves_a_detectable_orphan(tmp_path):
    journal_dir = tmp_path / "receipts"
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env.update(
        {
            "DAEMON_CORE_CONTINUITY_RECEIPTS": "shadow",
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(journal_dir),
            "PYTHONPATH": str(source_root),
        }
    )
    code = """
import os
from daemon_core.continuity import (
    ContinuityCycleObserver,
    begin_continuity_turn,
)

begin_continuity_turn(
    "crash-injected",
    user_id="test-user",
    platform="openclaw",
    session_id="test-session",
)
ContinuityCycleObserver("crash-injected")
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        check=False,
    )
    path = journal_dir / "continuity_receipts.jsonl"

    report = audit_continuity_journal(
        path,
        orphan_after_seconds=0,
        now=datetime.now(timezone.utc) + timedelta(seconds=1),
    )

    assert result.returncode == 17
    assert report.status_counts["orphaned"] == 1
    assert report.accepted_turns[0].turn_id == "crash-injected"
    assert report.unhealthy is True
