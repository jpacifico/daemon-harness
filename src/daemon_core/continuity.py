"""Best-effort shadow receipts for one complete daemon turn.

Each receipt is an append-only JSONL event stream containing technical
provenance and lifecycle states only. It is disabled unless explicitly
configured, and no failure here may affect the conversational runtime.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT
from daemon_core.memory.integrity import memory_file_lock
from daemon_core.platform_support import create_private_file, is_private_file


CONTINUITY_MODE_ENV = "DAEMON_CORE_CONTINUITY_RECEIPTS"
CONTINUITY_DIR_ENV = "DAEMON_CORE_CONTINUITY_RECEIPT_DIR"
CONTINUITY_SCHEMA = "daemon.continuity-receipt.v1"
CONTINUITY_EVENT_SCHEMA = "daemon.continuity-receipt.event.v1"
CONTINUITY_STATUSES = frozenset(
    {"pending", "completed", "skipped", "failed", "recovered"}
)

_ENABLED_MODES = frozenset({"1", "on", "shadow", "true"})
_CODE_RE = re.compile(r"[^a-z0-9_.-]+")
_WRITE_LOCK = threading.Lock()
_DEFERRED_FINALIZE_LOCK = threading.Lock()
_OPEN_JOURNALS: dict[tuple[int, str], int] = {}
_STARTED_LOCK = threading.Lock()
_STARTED_TURNS: dict[tuple[str, str], None] = {}
_MAX_STARTED_TURNS = 4096
_CRITICAL_STAGES = frozenset(
    {
        "preparation",
        "voice_path",
        "response",
        "l0",
        "l0_user",
        "l0_assistant",
        "memory",
        "scene_schedule",
        "cycle",
    }
)
_ALLOWED_DETAILS = frozenset(
    {
        "assistant_l0_recorded",
        "assistant_l0_ref",
        "atlas_capsule_count",
        "atlas_chars",
        "authoritative_source_chars",
        "authoritative_source_count",
        "changed_paths",
        "canonical_error_code",
        "canonical_evidence_count",
        "canonical_resolution_status",
        "canonical_response_constraint_present",
        "canonical_schema",
        "canonical_source_ref_count",
        "canonical_status",
        "canonical_unresolved_count",
        "context_paths",
        "current_message_chars",
        "dialogue_chars",
        "dialogue_coverage_status",
        "dialogue_delta_turns",
        "dialogue_expected_turns",
        "dialogue_missing_turns",
        "dialogue_recent_turns",
        "dialogue_visible_turns",
        "duration_ms",
        "error_type",
        "error_code",
        "evidence",
        "evidence_chars",
        "evidence_kind",
        "evidence_present",
        "evidence_available_source_count",
        "evidence_capability_count",
        "evidence_coverage_complete",
        "evidence_coverage_unit_count",
        "evidence_dependency_count",
        "evidence_failure_count",
        "evidence_opened_document_count",
        "evidence_present_claim_count",
        "evidence_renderer_ready",
        "evidence_schema",
        "evidence_unabsorbed_turn_count",
        "evidence_unknown_count",
        "episodic_scene_ref",
        "episodic_scene_selected",
        "for_delegation",
        "gardening_changed_count",
        "gardening_context_count",
        "gardening_event_ref",
        "hook_present",
        "hot_capsule_count",
        "historical_candidate_count",
        "historical_decision_count",
        "historical_invalid_reference_count",
        "historical_planned_count",
        "historical_provider_calls",
        "historical_receipt_ref",
        "historical_replayed",
        "historical_source_count",
        "identity_chars",
        "library_active",
        "message_count",
        "near_term_complete",
        "near_term_entry_count",
        "near_term_source_count",
        "near_turn_chars",
        "opened_capsule_chars",
        "opened_capsule_count",
        "opened_path_count",
        "procedure_chars",
        "prompt_variant",
        "projection_ref",
        "read_paths",
        "rejected_path_count",
        "relation_blocked_count",
        "relation_event_ref",
        "relation_planned_count",
        "relation_proposal_count",
        "relation_replayed",
        "relation_unchanged_count",
        "collection_blocked_count",
        "collection_planned_count",
        "collection_proposal_count",
        "collection_unchanged_count",
        "role",
        "rejected_paths",
        "relationship_chars",
        "requested_path_count",
        "requested_paths",
        "resource_chars",
        "result_chars",
        "response_constraint_present",
        "scene_claim_count",
        "scene_dependency_count",
        "scene_revision",
        "scene_scope_ref",
        "scene_source_count",
        "scene_started",
        "scene_unresolved_source_count",
        "scene_watermark_ref",
        "situation_source",
        "situation_chars",
        "source_ref",
        "source_posture",
        "covered_turn_count",
        "dialogue_turn_limit",
        "system_chars",
        "system_fingerprint",
        "source_turn_started_at",
        "temporal_chars",
        "tool_name",
        "tool_status",
        "unchanged_paths",
        "user_l0_ref",
        "user_l0_recorded",
        "user_ref",
        "voice_path",
        "working_state_started",
    }
)


class ContinuityCycleObserver:
    """Track terminal after-turn stages while remaining a complete no-op off."""

    _TERMINAL = frozenset({"completed", "skipped", "failed", "recovered"})
    _REQUIRED = frozenset({"l0", "memory", "scene_schedule"})

    def __init__(self, trace_id: str) -> None:
        self.trace_id = str(trace_id)
        self.enabled = bool(self.trace_id and continuity_receipts_enabled())
        self._lock = threading.Lock()
        self._closed = False
        self._statuses = {
            "l0": "pending",
            "working_state": "pending",
            "memory": "pending",
            "scene_schedule": "pending",
            "followup": "pending",
        }
        if self.enabled:
            record_continuity_stage(
                self.trace_id,
                "cycle",
                "pending",
                reason_code="after_turn_accepted",
            )

    def transition(
        self,
        stage: str,
        status: str,
        *,
        reason_code: str,
        **details: Any,
    ) -> None:
        if not self.enabled:
            return
        record_continuity_stage(
            self.trace_id,
            stage,
            status,
            reason_code=reason_code,
            **details,
        )
        cycle_status = ""
        with self._lock:
            self._statuses[stage] = status
            if (
                not self._closed
                and all(
                    value in self._TERMINAL
                    for value in self._statuses.values()
                )
            ):
                self._closed = True
                required_statuses = {
                    self._statuses[stage]
                    for stage in self._REQUIRED
                }
                if "failed" in required_statuses:
                    cycle_status = "failed"
                elif "skipped" in required_statuses:
                    cycle_status = "skipped"
                else:
                    cycle_status = "completed"
        if cycle_status:
            record_continuity_stage(
                self.trace_id,
                "cycle",
                cycle_status,
                reason_code=(
                    "after_turn_failed"
                    if cycle_status == "failed"
                    else "after_turn_incomplete"
                    if cycle_status == "skipped"
                    else "after_turn_completed"
                ),
            )

    def skip_remaining(self, *, reason_code: str) -> None:
        with self._lock:
            pending = [
                stage
                for stage, status in self._statuses.items()
                if status == "pending"
            ]
        for stage in pending:
            self.transition(
                stage,
                "skipped",
                reason_code=reason_code,
            )


def continuity_receipts_enabled() -> bool:
    mode = os.getenv(CONTINUITY_MODE_ENV, "").strip().lower()
    return bool(os.getenv(CONTINUITY_DIR_ENV, "").strip()) or mode in _ENABLED_MODES


def continuity_receipt_dir() -> Path:
    configured = os.getenv(CONTINUITY_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    configured_src = os.getenv("DAEMON_CORE_SRC", "").strip()
    source_root = (
        Path(configured_src).expanduser()
        if configured_src
        else PROJECT_ROOT / "src"
    )
    return source_root.parent / "exports" / "continuity_receipts"


def continuity_receipt_path(
    trace_id: str,
    *,
    directory: Path | None = None,
) -> Path:
    del trace_id
    return (directory or continuity_receipt_dir()) / "continuity_receipts.jsonl"


def begin_continuity_turn(
    trace_id: str,
    *,
    user_id: str,
    platform: str,
    session_id: str = "",
) -> bool:
    """Append the passive technical identity once per process and turn."""

    if not continuity_receipts_enabled() or not str(trace_id).strip():
        return False
    path = continuity_receipt_path(str(trace_id))
    started_key = (str(path), str(trace_id))
    with _STARTED_LOCK:
        if started_key in _STARTED_TURNS:
            return True
        _STARTED_TURNS[started_key] = None
        while len(_STARTED_TURNS) > _MAX_STARTED_TURNS:
            _STARTED_TURNS.pop(next(iter(_STARTED_TURNS)))

    event = _base_event(
        str(trace_id),
        stage="turn",
        status="pending",
        reason_code="accepted",
        details={},
    )
    event["identity"] = {
        "user_ref": _stable_ref(user_id),
        "platform": str(platform).strip().lower() or "unknown",
        "session_ref": _stable_ref(session_id) if session_id else "",
        "runtime_revision": runtime_revision(),
        "source_root_ref": _stable_ref(str(PROJECT_ROOT)),
        "memory_root_ref": _stable_ref(str(MEMORY_ROOT)),
    }
    if _append_event(path, event):
        return True
    with _STARTED_LOCK:
        _STARTED_TURNS.pop(started_key, None)
    return False


def record_continuity_stage(
    trace_id: str,
    stage: str,
    status: str,
    *,
    reason_code: str = "",
    **details: Any,
) -> bool:
    """Append one bounded technical transition without ever raising."""

    if not continuity_receipts_enabled() or not str(trace_id).strip():
        return False
    normalized_stage = _code(stage)
    normalized_status = str(status).strip().lower()
    if not normalized_stage or normalized_status not in CONTINUITY_STATUSES:
        return False
    event = _base_event(
        str(trace_id),
        stage=normalized_stage,
        status=normalized_status,
        reason_code=_code(reason_code),
        details=_safe_details(details),
    )
    return _append_event(continuity_receipt_path(str(trace_id)), event)


def finalize_deferred_scene(
    trace_id: str,
    *,
    status: str,
    reason_code: str,
    projection_ref: str = "",
    covered_turn_count: int = 0,
) -> bool:
    """Close one cycle after its durable scene projection reaches a terminal state."""

    normalized = str(status).strip().lower()
    if normalized not in {"completed", "failed"}:
        raise ValueError("deferred scene status must be completed or failed")
    if not str(trace_id).strip():
        return False
    if not continuity_receipts_enabled():
        return True

    with _DEFERRED_FINALIZE_LOCK:
        receipt = read_continuity_receipt(trace_id)
        if not isinstance(receipt, dict):
            return False
        stages = receipt.get("stages", {})
        cycle = stages.get("cycle", {}) if isinstance(stages, dict) else {}
        if isinstance(cycle, dict):
            cycle_status = cycle.get("status")
            if cycle_status in ContinuityCycleObserver._TERMINAL:
                if normalized == "completed" and cycle_status == "failed":
                    record_continuity_stage(
                        trace_id,
                        "scene_schedule",
                        "recovered",
                        reason_code=reason_code,
                        projection_ref=projection_ref,
                        covered_turn_count=covered_turn_count,
                    )
                    record_continuity_stage(
                        trace_id,
                        "cycle",
                        "recovered",
                        reason_code="after_turn_recovered",
                    )
                return True

        record_continuity_stage(
            trace_id,
            "scene_schedule",
            normalized,
            reason_code=reason_code,
            projection_ref=projection_ref,
            covered_turn_count=covered_turn_count,
        )
        receipt = read_continuity_receipt(trace_id)
        if not isinstance(receipt, dict):
            return False
        stages = receipt.get("stages", {})
        if not isinstance(stages, dict):
            return False
        stage_statuses = {
            name: str(stages.get(name, {}).get("status", ""))
            for name in (
                "l0",
                "working_state",
                "memory",
                "scene_schedule",
                "followup",
            )
            if isinstance(stages.get(name, {}), dict)
        }
        if any(
            stage_statuses.get(name, "") not in ContinuityCycleObserver._TERMINAL
            for name in (
                "l0",
                "working_state",
                "memory",
                "scene_schedule",
                "followup",
            )
        ):
            return False
        required = {
            stage_statuses.get(name, "")
            for name in ContinuityCycleObserver._REQUIRED
        }
        cycle_status = (
            "failed"
            if "failed" in required
            else "skipped"
            if "skipped" in required
            else "completed"
        )
        record_continuity_stage(
            trace_id,
            "cycle",
            cycle_status,
            reason_code=(
                "after_turn_failed"
                if cycle_status == "failed"
                else "after_turn_incomplete"
                if cycle_status == "skipped"
                else "after_turn_completed"
            ),
            projection_ref=projection_ref,
            covered_turn_count=covered_turn_count,
        )
        return True


def read_continuity_receipt(
    trace_id: str,
    *,
    directory: Path | None = None,
) -> dict[str, Any] | None:
    """Reconstruct the current receipt from its append-only technical events."""

    path = continuity_receipt_path(trace_id, directory=directory)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    receipt: dict[str, Any] | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(event, dict)
            or event.get("schema") != CONTINUITY_EVENT_SCHEMA
            or event.get("turn_id") != str(trace_id)
        ):
            continue
        if receipt is None:
            receipt = {
                "schema": CONTINUITY_SCHEMA,
                "turn_id": str(trace_id),
                "mode": "shadow",
                "created_at": event.get("at", ""),
                "updated_at": event.get("at", ""),
                "status": "pending",
                "identity": {},
                "stages": {},
                "transitions": [],
            }
        identity = event.get("identity")
        if isinstance(identity, dict):
            receipt["identity"].update(identity)
        transition = {
            "at": event.get("at", ""),
            "stage": event.get("stage", ""),
            "status": event.get("status", ""),
            "reason_code": event.get("reason_code", ""),
            "thread": event.get("thread", ""),
            "details": (
                event.get("details")
                if isinstance(event.get("details"), dict)
                else {}
            ),
        }
        stage = transition["stage"]
        receipt["stages"][stage] = {
            key: value
            for key, value in transition.items()
            if key != "stage"
        }
        receipt["transitions"].append(transition)
        receipt["updated_at"] = transition["at"]
        if stage == "cycle":
            receipt["status"] = transition["status"]
    if receipt is not None:
        critical_statuses = {
            value.get("status", "")
            for stage, value in receipt["stages"].items()
            if stage in _CRITICAL_STAGES and isinstance(value, dict)
        }
        if "failed" in critical_statuses:
            receipt["status"] = "failed"
        elif "skipped" in critical_statuses:
            receipt["status"] = "skipped"
        receipt["anomalies"] = _receipt_anomalies(receipt)
        if any(
            anomaly.get("severity") == "failure"
            for anomaly in receipt["anomalies"]
        ):
            receipt["status"] = "failed"
    return receipt


def technical_ref(value: str) -> str:
    """Expose the same non-sensitive fingerprint used by receipt metadata."""

    return _stable_ref(value)


def _receipt_anomalies(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    transitions = receipt.get("transitions", [])
    exact_counts = {
        stage: sum(
            1
            for transition in transitions
            if transition.get("stage") == stage
            and transition.get("status") == "completed"
            and transition.get("reason_code") == "writer_completed"
        )
        for stage in ("l0_user", "l0_assistant")
    }
    for stage, count in exact_counts.items():
        if count > 1:
            anomalies.append(
                {
                    "code": f"duplicate_{stage}",
                    "severity": "failure",
                    "count": count,
                }
            )

    l0_stage = receipt.get("stages", {}).get("l0", {})
    l0_details = (
        l0_stage.get("details", {})
        if isinstance(l0_stage, dict)
        else {}
    )
    expected = {
        "l0_user": l0_details.get("user_l0_recorded") is True,
        "l0_assistant": (
            l0_details.get("assistant_l0_recorded") is True
        ),
    }
    for stage, is_expected in expected.items():
        if is_expected and exact_counts[stage] == 0:
            anomalies.append(
                {
                    "code": f"unverified_{stage}",
                    "severity": "failure",
                    "count": 0,
                }
            )

    cycle = receipt.get("stages", {}).get("cycle", {})
    if (
        isinstance(cycle, dict)
        and cycle.get("status") in ContinuityCycleObserver._TERMINAL
        and not receipt.get("identity")
    ):
        anomalies.append(
            {
                "code": "missing_identity",
                "severity": "failure",
                "count": 0,
            }
        )
    memory_mutation = receipt.get("stages", {}).get(
        "memory_mutation",
        {},
    )
    mutation_details = (
        memory_mutation.get("details", {})
        if isinstance(memory_mutation, dict)
        else {}
    )
    if (
        isinstance(memory_mutation, dict)
        and memory_mutation.get("status") == "failed"
        and memory_mutation.get("reason_code")
        == "direct_fact_write_rejected"
        and mutation_details.get("source_posture") == "direct_fact"
    ):
        rejected = mutation_details.get("rejected_paths", [])
        anomalies.append(
            {
                "code": "direct_fact_write_rejected",
                "severity": "failure",
                "count": len(rejected) if isinstance(rejected, list) else 1,
            }
        )
    return anomalies


def _base_event(
    trace_id: str,
    *,
    stage: str,
    status: str,
    reason_code: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": CONTINUITY_EVENT_SCHEMA,
        "turn_id": trace_id,
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "stage": stage,
        "status": status,
        "reason_code": reason_code,
        "thread": threading.current_thread().name,
        "details": details,
    }


def _append_event(path: Path, event: dict[str, Any]) -> bool:
    try:
        encoded = (
            json.dumps(
                event,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with _WRITE_LOCK:
            if os.name == "nt":
                # Windows cannot replace a journal kept open by the CRT.
                # Serialize appends across processes and close before rotation.
                with memory_file_lock(path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        fd = create_private_file(path)
                    except FileExistsError:
                        if not is_private_file(path):
                            return False
                        fd = os.open(path, os.O_APPEND | os.O_WRONLY | os.O_BINARY)
                    try:
                        return os.write(fd, encoded) == len(encoded)
                    finally:
                        os.close(fd)
            key = (os.getpid(), str(path))
            fd = _OPEN_JOURNALS.get(key)
            if fd is not None and not _journal_descriptor_matches_path(
                fd,
                path,
            ):
                _OPEN_JOURNALS.pop(key, None)
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            if fd is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                _OPEN_JOURNALS[key] = fd
            try:
                written = os.write(fd, encoded)
            except OSError:
                _OPEN_JOURNALS.pop(key, None)
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        return written == len(encoded)
    except Exception:
        return False


def _journal_descriptor_matches_path(fd: int, path: Path) -> bool:
    try:
        descriptor = os.fstat(fd)
        current = path.stat()
    except OSError:
        return False
    return (
        descriptor.st_dev == current.st_dev
        and descriptor.st_ino == current.st_ino
    )


def _close_open_journals() -> None:
    with _WRITE_LOCK:
        descriptors = list(_OPEN_JOURNALS.values())
        _OPEN_JOURNALS.clear()
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass


atexit.register(_close_open_journals)


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in details.items():
        if key not in _ALLOWED_DETAILS or value is None:
            continue
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int):
            result[key] = value
        elif isinstance(value, float):
            result[key] = round(value, 6)
        elif isinstance(value, str):
            result[key] = value[:160]
        elif key in {
            "changed_paths",
            "context_paths",
            "read_paths",
            "requested_paths",
            "unchanged_paths",
            "rejected_paths",
        }:
            paths = _safe_path_list(value)
            if paths:
                result[key] = paths
        elif key in {"user_l0_ref", "assistant_l0_ref"}:
            reference = _safe_l0_reference(value)
            if reference:
                result[key] = reference
    return result


def _safe_path_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value[:32]:
        if not isinstance(item, str):
            continue
        path = item.strip().replace("\\", "/")[:320]
        if path and not path.startswith("/") and ".." not in Path(path).parts:
            result.append(path)
    return result


def _safe_l0_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    path = str(value.get("path", "")).strip().replace("\\", "/")[:320]
    sha256 = str(value.get("sha256", "")).strip().lower()
    role = str(value.get("role", "")).strip().lower()[:20]
    turn_id = str(value.get("turn_id", "")).strip()[:160]
    try:
        offset = int(value.get("offset", -1))
        length = int(value.get("length", 0))
    except (TypeError, ValueError):
        return {}
    if (
        not path
        or path.startswith("/")
        or ".." in Path(path).parts
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or role not in {"user", "assistant"}
        or offset < 0
        or length <= 0
    ):
        return {}
    return {
        "path": path,
        "offset": offset,
        "length": length,
        "sha256": sha256,
        "role": role,
        "turn_id": turn_id,
    }


def _code(value: str) -> str:
    return _CODE_RE.sub("_", str(value).strip().lower()).strip("_.-")[:80]


def _stable_ref(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=8)
def _git_runtime_revision(project_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        revision = result.stdout.strip()
        if revision:
            return revision[:80]
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def runtime_revision(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> str:
    """Return the configured build revision, or the current Git revision."""

    source = os.environ if environ is None else environ
    configured = source.get("DAEMON_CORE_RUNTIME_REVISION", "").strip()
    if configured:
        return configured[:80]
    return _git_runtime_revision(str(project_root or PROJECT_ROOT))


__all__ = [
    "CONTINUITY_DIR_ENV",
    "CONTINUITY_EVENT_SCHEMA",
    "CONTINUITY_MODE_ENV",
    "CONTINUITY_SCHEMA",
    "CONTINUITY_STATUSES",
    "ContinuityCycleObserver",
    "begin_continuity_turn",
    "continuity_receipt_dir",
    "continuity_receipt_path",
    "continuity_receipts_enabled",
    "finalize_deferred_scene",
    "read_continuity_receipt",
    "record_continuity_stage",
    "runtime_revision",
    "technical_ref",
]
