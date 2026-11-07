import hashlib
import json
import os
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import daemon_core.after_turn_spool as after_turn_spool
from daemon_core.after_turn_spool import (
    AfterTurnJob,
    AfterTurnSpool,
    AfterTurnWorker,
    DrainResult,
    MAX_COALESCED_SCENE_AGE_SECONDS,
    MAX_COALESCED_SCENE_BYTES,
    MAX_COALESCED_SCENE_TURNS,
    SceneProjectionJob,
    scene_coalescing_enabled,
)
from daemon_core.after_turn import AfterTurnStageError
from daemon_core.after_turn_executor import (
    execute_after_turn_job,
    execute_after_turn_projection_batch,
)
from daemon_core.after_turn_executor import execute_scene_projection
from daemon_core.continuity import (
    begin_continuity_turn,
    finalize_deferred_scene,
    read_continuity_receipt,
    record_continuity_stage,
)
from daemon_core.continuity_audit import audit_continuity_journal
from daemon_core.memory import writer
from daemon_core.memory import reader
from daemon_core.memory.reader import load_recent_dialogue_through
from daemon_core.memory.writer import (
    GroundZeroReference,
    append_ground_zero,
    read_ground_zero_reference,
)


def _job(monkeypatch, tmp_path, *, turn_id="turn-1"):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    user_ref = append_ground_zero(
        "u",
        "user",
        "question exacte\navec deux lignes",
        turn_id=turn_id,
        return_reference=True,
    )
    assistant_ref = append_ground_zero(
        "u",
        "assistant",
        "réponse exacte",
        turn_id=turn_id,
        return_reference=True,
    )
    assert isinstance(user_ref, GroundZeroReference)
    assert isinstance(assistant_ref, GroundZeroReference)
    return AfterTurnJob(
        turn_id=turn_id,
        host="hermes",
        user_id="u",
        source_turn_started_at=123.0,
        scene_scope="telegram__u",
        user_l0=user_ref,
        assistant_l0=assistant_ref,
        selected_souvenirs=("theme/a",),
        read_souvenirs=("theme/a", "theme/b"),
        library_active=True,
        origin={"platform": "telegram", "chat_id": "u"},
    )


def _enable_receipts(monkeypatch, tmp_path, *jobs):
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setenv(
        "DAEMON_CORE_CONTINUITY_RECEIPTS",
        "shadow",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        str(receipt_dir),
    )
    for job in jobs:
        begin_continuity_turn(
            job.turn_id,
            user_id=job.user_id,
            platform=job.host,
        )
        for stage in ("l0_user", "l0_assistant"):
            record_continuity_stage(
                job.turn_id,
                stage,
                "completed",
                reason_code="writer_completed",
            )
    return receipt_dir


def _stub_shared_faculties(monkeypatch, memory_calls):
    monkeypatch.setattr(
        "daemon_core.held_thought.record_held_thought_source",
        lambda *args, **kwargs: None,
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        "daemon_core.held_thought.capture_held_thought_context",
        lambda *args, **kwargs: {},
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        "daemon_core.held_thought.run_held_thought_revision",
        lambda **kwargs: SimpleNamespace(held_souvenirs=[]),
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda user_id: False,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("scene must be deferred")
        ),
    )

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **kwargs):
            memory_calls.append(kwargs["message"])
            return None

    return Harness()


def _record_projection_ready(job):
    record_continuity_stage(
        job.turn_id,
        "l0",
        "completed",
        reason_code="adapter_reported",
        user_l0_recorded=True,
        assistant_l0_recorded=True,
    )
    record_continuity_stage(
        job.turn_id,
        "working_state",
        "skipped",
        reason_code="hook_not_configured",
    )
    record_continuity_stage(
        job.turn_id,
        "memory",
        "completed",
        reason_code="hook_completed",
    )
    record_continuity_stage(
        job.turn_id,
        "followup",
        "skipped",
        reason_code="hook_not_configured",
    )


def test_ground_zero_reference_reads_exact_message(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)

    assert read_ground_zero_reference(job.user_l0) == (
        "question exacte\navec deux lignes"
    )
    assert read_ground_zero_reference(job.assistant_l0.as_dict()) == (
        "réponse exacte"
    )


def test_recent_dialogue_through_excludes_future_turns(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(reader, "MEMORY_ROOT", memory_root)
    append_ground_zero(
        "u",
        "user",
        "fait source",
        turn_id="turn-source",
        return_reference=True,
    )
    source = append_ground_zero(
        "u",
        "assistant",
        "réponse source",
        turn_id="turn-source",
        return_reference=True,
    )
    append_ground_zero(
        "u",
        "user",
        "fait futur qui ne doit pas être visible",
        turn_id="turn-future",
        return_reference=True,
    )

    dialogue = load_recent_dialogue_through("u", source, max_turns=8)

    assert "fait source" in dialogue
    assert "réponse source" in dialogue
    assert "fait futur" not in dialogue


def test_ground_zero_reference_detects_tampering(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    path = writer.MEMORY_ROOT / job.user_l0.path
    with path.open("r+b") as handle:
        handle.seek(job.user_l0.offset)
        handle.write(b"X")

    with pytest.raises(ValueError, match="hash mismatch"):
        read_ground_zero_reference(job.user_l0)


def test_ground_zero_idempotency_survives_completed_and_pending_receipt(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)

    first = append_ground_zero(
        "u",
        "user",
        "message unique",
        turn_id="turn-1",
        idempotency_key="openclaw-message-1:user",
        return_reference=True,
    )
    second = append_ground_zero(
        "u",
        "user",
        "message unique",
        turn_id="turn-1",
        idempotency_key="openclaw-message-1:user",
        return_reference=True,
    )
    receipt = next(
        (
            memory_root
            / "levels"
            / "L0_ground_zero"
            / "idempotency"
            / "u"
        ).glob("*.json")
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["status"] = "pending"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    third = append_ground_zero(
        "u",
        "user",
        "message unique",
        turn_id="turn-1",
        idempotency_key="openclaw-message-1:user",
        return_reference=True,
    )

    assert first == second == third
    path = memory_root / first.path
    assert path.read_text(encoding="utf-8").count("message unique") == 1
    assert json.loads(receipt.read_text(encoding="utf-8"))[
        "status"
    ] == "completed"
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_ground_zero_rejects_reused_key_with_another_payload(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(writer, "MEMORY_ROOT", tmp_path / "memory")
    append_ground_zero(
        "u",
        "user",
        "premier message",
        idempotency_key="same-key",
    )

    with pytest.raises(ValueError, match="another payload"):
        append_ground_zero(
            "u",
            "user",
            "autre message",
            idempotency_key="same-key",
        )


def test_spool_is_idempotent_and_keeps_dialogue_only_in_l0(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(tmp_path / "spool")

    first = spool.enqueue(job)
    duplicate = spool.enqueue(job)

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.state == "pending"
    assert spool.counts("hermes", "u") == {
        "pending": 1,
        "running": 0,
        "failed": 0,
    }
    raw = first.path.read_text(encoding="utf-8")
    assert "question exacte" not in raw
    assert "réponse exacte" not in raw
    assert "route" not in json.loads(raw)
    assert first.path.stat().st_mode & 0o777 == 0o600


def test_spool_rejects_a_different_memory_namespace(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    spool_root = tmp_path / "spool"
    monkeypatch.setattr(
        after_turn_spool,
        "MEMORY_ROOT",
        tmp_path / "memory-a",
    )
    first = AfterTurnSpool(spool_root)
    first.enqueue(job)
    marker = json.loads(
        (spool_root / "runtime.json").read_text(encoding="utf-8")
    )
    assert marker["schema"] == "daemon.after-turn-spool-runtime.v1"
    assert "memory-a" not in str(marker)

    monkeypatch.setattr(
        after_turn_spool,
        "MEMORY_ROOT",
        tmp_path / "memory-b",
    )
    second = AfterTurnSpool(spool_root)

    with pytest.raises(RuntimeError, match="memory namespace mismatch"):
        second.enqueue(job)
    assert first.counts("hermes", "u")["pending"] == 1


def test_spool_drains_one_job_from_verified_l0(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(job)
    observed = []

    result = spool.drain(
        "hermes",
        "u",
        lambda queued: observed.append(
            (
                read_ground_zero_reference(queued.user_l0),
                read_ground_zero_reference(queued.assistant_l0),
            )
        ),
    )

    assert result.completed == 1
    assert observed == [
        ("question exacte\navec deux lignes", "réponse exacte")
    ]
    assert spool.counts("hermes", "u") == {
        "pending": 0,
        "running": 0,
        "failed": 0,
    }


def test_spool_retries_then_quarantines_terminal_failure(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        max_attempts=2,
    )
    spool.enqueue(job)

    def fail(_job):
        raise RuntimeError("provider unavailable")

    first = spool.drain("hermes", "u", fail, now=100.0)
    second = spool.drain("hermes", "u", fail, now=106.0)

    assert first.retried == 1
    assert second.failed == 1
    assert spool.counts("hermes", "u") == {
        "pending": 0,
        "running": 0,
        "failed": 1,
    }
    failed = next(
        (tmp_path / "spool" / "failed" / "hermes" / "u").glob(
            "*.json"
        )
    )
    payload = json.loads(failed.read_text(encoding="utf-8"))
    assert payload["attempts"] == 2
    assert payload["last_error"] == "RuntimeError"
    assert "provider unavailable" not in failed.read_text(encoding="utf-8")


def test_spool_auto_resolves_unreadable_failed_head_across_hosts(
    monkeypatch,
    tmp_path,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-unreadable"),
        host="openclaw",
        source_turn_started_at=1.0,
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-valid"),
        host="hermes",
        source_turn_started_at=2.0,
    )
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, first, second)
    spool = AfterTurnSpool(tmp_path / "spool", max_attempts=1)
    spool.enqueue(first)
    spool.enqueue(second)

    exhausted = spool.drain(
        "openclaw",
        "u",
        lambda _job: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
        now=100.0,
    )
    assert exhausted.failed == 1

    l0_path = writer.MEMORY_ROOT / first.user_l0.path
    with l0_path.open("r+b") as handle:
        handle.seek(first.user_l0.offset)
        handle.write(b"X")

    observed = []
    resumed = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
        now=106.0,
    )

    assert resumed.quarantined == 1
    assert resumed.completed == 1
    assert observed == ["turn-valid"]
    assert spool.counts("openclaw", "u")["failed"] == 0
    quarantined = next(
        (
            tmp_path
            / "spool"
            / "quarantined"
            / "openclaw"
            / "u"
        ).glob("*.json")
    )
    assert json.loads(quarantined.read_text(encoding="utf-8"))[
        "turn_id"
    ] == first.turn_id
    resolution = json.loads(
        (
            tmp_path
            / "spool"
            / "quarantine_receipts"
            / "openclaw"
            / "u"
            / f"{first.turn_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert resolution["reason_code"] == "l0_reference_unrecoverable"
    assert resolution["source_state"] == "failed"
    assert resolution["job_sha256"]
    receipt = read_continuity_receipt(
        first.turn_id,
        directory=receipt_dir,
    )
    outbox = receipt["stages"]["outbox"]
    assert outbox["status"] == "skipped"
    assert outbox["reason_code"] == "l0_reference_unrecoverable"
    assert outbox["details"] == {"error_type": "ValueError"}
    duplicate = spool.enqueue(first)
    assert duplicate.created is False
    assert duplicate.state == "quarantined"


def test_spool_quarantines_unreadable_pending_and_continues_fifo(
    monkeypatch,
    tmp_path,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-unreadable"),
        source_turn_started_at=1.0,
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-valid"),
        source_turn_started_at=2.0,
    )
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(first)
    spool.enqueue(second)
    l0_path = writer.MEMORY_ROOT / first.user_l0.path
    with l0_path.open("r+b") as handle:
        handle.seek(first.user_l0.offset)
        handle.write(b"X")
    observed = []

    first_attempt = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
        now=100.0,
    )
    too_early = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
        now=104.0,
    )
    second_attempt = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
        now=106.0,
    )
    result = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
        now=116.0,
    )

    assert first_attempt.retried == 1
    assert too_early.retried == 0
    assert second_attempt.retried == 1
    assert result.quarantined == 1
    assert result.completed == 1
    assert observed == ["turn-valid"]
    assert spool.counts("hermes", "u") == {
        "pending": 0,
        "running": 0,
        "failed": 0,
    }


def test_spool_resolves_unreadable_pending_head_from_inactive_host(
    monkeypatch,
    tmp_path,
):
    orphaned = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-orphaned"),
        host="hermes",
        source_turn_started_at=1.0,
    )
    current = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-current"),
        host="openclaw",
        source_turn_started_at=2.0,
    )
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(orphaned)
    spool.enqueue(current)
    l0_path = writer.MEMORY_ROOT / orphaned.user_l0.path
    with l0_path.open("r+b") as handle:
        handle.seek(orphaned.user_l0.offset)
        handle.write(b"X")
    observed = []

    first = spool.drain("openclaw", "u", observed.append, now=100.0)
    second = spool.drain("openclaw", "u", observed.append, now=106.0)
    resolved = spool.drain("openclaw", "u", observed.append, now=116.0)

    assert first.retried == 1
    assert second.retried == 1
    assert resolved.quarantined == 1
    assert resolved.completed == 1
    assert [job.turn_id for job in observed] == ["turn-current"]
    assert spool.counts("hermes", "u")["pending"] == 0
    assert spool.counts("openclaw", "u")["pending"] == 0


def test_spool_retries_valid_terminal_failure_instead_of_quarantining(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(tmp_path / "spool", max_attempts=1)
    spool.enqueue(job)

    exhausted = spool.drain(
        "hermes",
        "u",
        lambda _job: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
        now=100.0,
    )
    assert spool.user_ids("hermes") == ("u",)
    too_early = spool.drain(
        "hermes",
        "u",
        lambda _job: None,
        now=104.0,
    )
    resumed = spool.drain(
        "hermes",
        "u",
        lambda _job: None,
        now=106.0,
    )

    assert exhausted.failed == 1
    assert too_early.completed == 0
    assert too_early.quarantined == 0
    assert resumed.recovered == 1
    assert resumed.completed == 1
    assert resumed.quarantined == 0
    assert spool.counts("hermes", "u") == {
        "pending": 0,
        "running": 0,
        "failed": 0,
    }


def test_spool_retry_prevents_later_memory_turn_from_overtaking(
    monkeypatch,
    tmp_path,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-1"),
        source_turn_started_at=1.0,
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-2"),
        source_turn_started_at=2.0,
    )
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(first)
    spool.enqueue(second)
    observed = []

    def fail_first(job):
        observed.append(job.turn_id)
        if job.turn_id == "turn-1":
            raise RuntimeError("retry")

    retried = spool.drain("hermes", "u", fail_first, now=100.0)

    assert retried.retried == 1
    assert observed == ["turn-1"]
    assert spool.counts("hermes", "u")["pending"] == 2

    observed.clear()
    completed = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
        now=106.0,
    )

    assert completed.completed == 2
    assert observed == ["turn-1", "turn-2"]


def test_unavailable_maintenance_zoom_retries_exact_l0_before_writing(monkeypatch, tmp_path):
    from daemon_core.context_router import ContextRoute
    from daemon_core.loop import DaemonCore

    job = replace(
        _job(monkeypatch, tmp_path), library_active=False,
        selected_souvenirs=("theme/project",), read_souvenirs=(),
    )
    root = tmp_path / "memory"
    for module in ("memory.reader", "memory_agent"):
        monkeypatch.setattr(f"daemon_core.{module}.MEMORY_ROOT", root)
    target = root / "capsules" / "theme" / "project" / "story.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Story\n\n## Faits\n- Original fact.\n")
    before = {path: path.read_bytes() for path in root.rglob("*.md")}
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS", "canary")
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", "u")
    monkeypatch.setattr(
        "daemon_core.loop.route_context_depth",
        lambda **kwargs: ContextRoute(mode="contextual", reason="Existing story", candidate_souvenirs=["theme/project"]),
    )
    monkeypatch.setattr("daemon_core.scene_model.scene_model_shadow_enabled", lambda _user: False)
    calls = []
    responses = iter([
        RuntimeError("offline"),
        {"selected": ["theme/project/story"]},
        {"capsule_updates": []},
    ])

    class Provider:
        def complete(self, messages, temperature=0.4):
            calls.append(messages)
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return json.dumps(value)

    provider = Provider()
    harness = DaemonCore(provider=provider, memory_provider=provider, routing_provider=provider)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(job)

    def execute(claimed):
        return execute_after_turn_job(claimed, harness=harness, defer_replaceable_projections=True)

    first = spool.drain("hermes", "u", execute, now=100.0)
    assert first.retried == 1 and first.completed == 0
    assert len(calls) == 1
    assert {path: path.read_bytes() for path in root.rglob("*.md")} == before
    assert read_ground_zero_reference(job.user_l0) == "question exacte\navec deux lignes"
    assert spool.counts("hermes", "u")["pending"] == 1

    resumed = spool.drain("hermes", "u", execute, now=106.0)
    assert resumed.completed == 1 and resumed.retried == 0
    assert len(calls) == 3
    assert "question exacte\navec deux lignes" in calls[2][1]["content"]
    assert "- Original fact." in calls[2][1]["content"]
    assert {path: path.read_bytes() for path in root.rglob("*.md")} == before
    assert spool.counts("hermes", "u") == {"pending": 0, "running": 0, "failed": 0}


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
@pytest.mark.parametrize("failed_phase", ["radar", "memory_agent"])
def test_actual_core_provider_failure_is_not_acknowledged_and_retries(monkeypatch, tmp_path, host, failed_phase):
    from daemon_core.context_router import ContextRoute
    from daemon_core.loop import DaemonCore

    job = replace(_job(monkeypatch, tmp_path), host=host, library_active=False,
                  selected_souvenirs=(), read_souvenirs=())
    root = tmp_path / "memory"
    for module in ("memory.reader", "memory_agent", "after_turn_spool"):
        monkeypatch.setattr(f"daemon_core.{module}.MEMORY_ROOT", root)
    monkeypatch.setattr("daemon_core.scene_model.scene_model_shadow_enabled", lambda _user: False)
    monkeypatch.setattr("daemon_core.after_turn_executor._scene_hook", lambda *a, **kw: lambda turn: None)
    if failed_phase == "memory_agent":
        monkeypatch.setattr("daemon_core.loop.route_context_depth",
                            lambda **kw: ContextRoute(mode="contextual", reason="synthetic"))
    _enable_receipts(monkeypatch, tmp_path, job)

    class Provider:
        calls = 0

        def complete(self, messages, temperature=0.0):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic auth unavailable")
            return json.dumps({"mode": "contextual", "reason": "synthetic", "capsule_updates": []})

    provider = Provider()
    harness = DaemonCore(provider=provider, routing_provider=provider, memory_provider=provider)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.handoff(job)
    before = {p: p.read_bytes() for p in root.rglob("*.md")}

    def execute(claimed):
        return execute_after_turn_job(claimed, harness=harness, defer_replaceable_projections=True)

    first = spool.drain(host, "u", execute, now=100.0)
    assert first.retried == 1 and first.completed == 0
    assert provider.calls == 1
    assert not after_turn_spool._receipt_cycle_completed(job.turn_id)
    assert {p: p.read_bytes() for p in root.rglob("*.md")} == before
    resumed = AfterTurnSpool(spool.root).drain(host, "u", execute, now=106.0)
    assert resumed.completed == 1 and resumed.retried == 0
    assert after_turn_spool._receipt_cycle_completed(job.turn_id)
    assert AfterTurnSpool(spool.root).drain(host, "u", execute, now=120).completed == 0


def test_handoff_survives_namespace_failure_and_process_restart(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    original_root = tmp_path / "old-memory"
    monkeypatch.setattr(after_turn_spool, "MEMORY_ROOT", original_root)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool._ensure_runtime_namespace()
    marker = spool.root / "runtime.json"
    before = marker.read_bytes()
    monkeypatch.setattr(after_turn_spool, "MEMORY_ROOT", tmp_path / "memory")
    queued = spool.handoff(job)
    assert queued.state == "handoff"
    assert marker.read_bytes() == before
    assert queued.path.is_file()
    assert spool.counts(job.host, job.user_id) == {"pending": 0, "running": 0, "failed": 0}
    # Explicit operator migration of an EMPTY synthetic queue, never automatic.
    marker.unlink()
    fresh = AfterTurnSpool(spool.root)
    completed = []
    result = fresh.drain(job.host, job.user_id, lambda j: completed.append(j.turn_id))
    assert result.completed == 1 and completed == [job.turn_id]
    assert not queued.path.exists()


def test_missing_marker_cannot_adopt_existing_queue(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(job)
    (spool.root / "runtime.json").unlink()
    with pytest.raises(RuntimeError, match="marker missing"):
        spool.drain(job.host, job.user_id, lambda j: None)


def test_spool_orders_memory_turns_across_hosts(monkeypatch, tmp_path):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-1"),
        host="openclaw",
        source_turn_started_at=1.0,
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-2"),
        host="hermes",
        source_turn_started_at=2.0,
    )
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(first)
    spool.enqueue(second)
    observed = []

    blocked = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
    )
    drained_first = spool.drain(
        "openclaw",
        "u",
        lambda job: observed.append(job.turn_id),
    )
    drained_second = spool.drain(
        "hermes",
        "u",
        lambda job: observed.append(job.turn_id),
    )

    assert blocked.completed == 0
    assert drained_first.completed == 1
    assert drained_second.completed == 1
    assert observed == ["turn-1", "turn-2"]


def test_spool_recovers_a_job_left_running_by_process_death(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        lease_seconds=1.0,
    )
    spool.enqueue(job)

    with pytest.raises(KeyboardInterrupt):
        spool.drain(
            "hermes",
            "u",
            lambda queued: (_ for _ in ()).throw(KeyboardInterrupt()),
            now=100.0,
        )

    running = next(
        (tmp_path / "spool" / "running" / "hermes" / "u").glob(
            "*.json"
        )
    )
    os.utime(running, (100.0, 100.0))
    recovered = spool.recover_stale("hermes", "u", now=102.0)

    assert recovered == 1
    assert spool.counts("hermes", "u") == {
        "pending": 1,
        "running": 0,
        "failed": 0,
    }
    assert spool.user_ids("hermes") == ("u",)


def test_spool_immediately_recovers_dead_lease_owner(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        lease_seconds=3600.0,
    )
    spool.enqueue(job)

    with pytest.raises(KeyboardInterrupt):
        spool.drain(
            "hermes",
            "u",
            lambda queued: (_ for _ in ()).throw(KeyboardInterrupt()),
            now=100.0,
        )
    running = next(
        (tmp_path / "spool" / "running" / "hermes" / "u").glob(
            "*.json"
        )
    )
    payload = json.loads(running.read_text(encoding="utf-8"))
    payload["lease_pid"] = 999_999_999
    running.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(running, (100.0, 100.0))

    assert spool.recover_stale("hermes", "u", now=101.0) == 1
    assert spool.counts("hermes", "u")["pending"] == 1


def test_executor_finishes_canonical_faculties_without_held_thought(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    order = []
    calls = {}

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **kwargs):
            calls.update(kwargs)
            order.append("memory")
            return "memory-result"

    monkeypatch.setattr(
        "daemon_core.held_thought.run_held_thought_revision",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Held Thought is suspended from the canonical runtime")
        ),
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda user_id: False,
    )

    def revise_scene(**kwargs):
        assert kwargs["strict"] is True
        order.append("scene")
        return SimpleNamespace(revision=2)

    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        revise_scene,
    )
    after = []

    execution = execute_after_turn_job(
        job,
        harness=Harness(),
        after_memory=after.append,
    )

    assert calls["message"] == "question exacte\navec deux lignes"
    assert calls["response"] == "réponse exacte"
    assert calls["route"] is None
    assert calls["selected_souvenirs"] == ["theme/a", "theme/b"]
    assert calls["source_trace_id"] == job.turn_id
    assert calls["source_turn_started_at"] == job.source_turn_started_at
    assert calls["current_user_l0"] == job.user_l0
    assert calls["current_assistant_l0"] == job.assistant_l0
    assert order.index("memory") < order.index("scene")
    assert order == ["memory", "scene"]
    assert after == ["memory-result"]
    assert execution.memory_result == "memory-result"
    assert execution.working_state_started is False


def test_executor_coalesces_only_replaceable_views_after_exact_turns(
    monkeypatch,
    tmp_path,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-1"),
        source_turn_started_at=1.0,
        library_active=False,
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-2"),
        source_turn_started_at=2.0,
        library_active=False,
    )
    memory_calls = []
    projection_calls = []

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **kwargs):
            memory_calls.append(
                (
                    kwargs["source_trace_id"],
                    kwargs["defer_replaceable_projections"],
                )
            )
            return SimpleNamespace(
                changed_paths=[],
                unchanged_paths=[],
                rejected_paths=[],
                consulted_souvenirs=[],
            )

        def refresh_after_turn_projections(self, **kwargs):
            projection_calls.append(kwargs)
            return SimpleNamespace(followup=None)

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    harness = Harness()
    executions = tuple(
        execute_after_turn_job(
            job,
            harness=harness,
            defer_replaceable_projections=True,
        )
        for job in (first, second)
    )

    execute_after_turn_projection_batch(executions, harness=harness)

    assert memory_calls == [("turn-1", True), ("turn-2", True)]
    assert projection_calls == [
        {
            "user_id": "u",
            "current_assistant_l0": second.assistant_l0,
            "run_followup": True,
            "scene_projection_expected": False,
        }
    ]


def test_executor_marks_scene_backed_batch_for_state_projection(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path, turn_id="turn-scene-backed")
    projection_calls = []

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **_kwargs):
            return SimpleNamespace(
                changed_paths=[],
                unchanged_paths=[],
                rejected_paths=[],
                consulted_souvenirs=[],
            )

        def refresh_after_turn_projections(self, **kwargs):
            projection_calls.append(kwargs)
            return SimpleNamespace(followup=None)

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    execution = execute_after_turn_job(
        job,
        harness=Harness(),
        defer_scene=True,
        defer_replaceable_projections=True,
    )

    execute_after_turn_projection_batch((execution,), harness=Harness())

    assert execution.scene_projection is not None
    assert projection_calls == [
        {
            "user_id": "u",
            "current_assistant_l0": job.assistant_l0,
            "run_followup": True,
            "scene_projection_expected": True,
        }
    ]


def test_executor_records_memory_mutation_provenance(monkeypatch, tmp_path):
    job = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-provenance"),
        library_active=False,
    )
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, job)

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **_kwargs):
            return SimpleNamespace(
                changed_paths=["users/u/agenda.md"],
                unchanged_paths=["users/u/context.md"],
                rejected_paths=[],
                consulted_souvenirs=["theme/b", "theme/c"],
            )

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )

    execute_after_turn_job(job, harness=Harness())

    receipt = read_continuity_receipt(job.turn_id, directory=receipt_dir)
    mutation = receipt["stages"]["memory_mutation"]
    assert mutation["status"] == "completed"
    assert mutation["details"]["changed_paths"] == ["users/u/agenda.md"]
    assert mutation["details"]["context_paths"] == [
        "capsules/theme/a.md",
        "capsules/theme/b.md",
        "capsules/theme/c.md",
    ]
    assert mutation["details"]["unchanged_paths"] == ["users/u/context.md"]
    assert mutation["details"]["user_l0_ref"] == job.user_l0.as_dict()
    assert mutation["details"]["assistant_l0_ref"] == job.assistant_l0.as_dict()


def test_executor_marks_a_rejected_direct_fact_as_unresolved(
    monkeypatch,
    tmp_path,
):
    job = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-direct-fact-rejected"),
        library_active=False,
    )
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, job)

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **_kwargs):
            return SimpleNamespace(
                source_posture="direct_fact",
                changed_paths=["users/u/focus_immediat.md"],
                unchanged_paths=[],
                rejected_paths=["users/u/agenda.md"],
                consulted_souvenirs=[],
            )

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )

    execute_after_turn_job(job, harness=Harness())

    receipt = read_continuity_receipt(job.turn_id, directory=receipt_dir)
    mutation = receipt["stages"]["memory_mutation"]
    assert mutation["status"] == "failed"
    assert mutation["reason_code"] == "direct_fact_write_rejected"
    assert mutation["details"]["source_posture"] == "direct_fact"
    assert mutation["details"]["rejected_paths"] == ["users/u/agenda.md"]
    assert receipt["status"] == "failed"
    assert receipt["anomalies"] == [
        {
            "code": "direct_fact_write_rejected",
            "severity": "failure",
            "count": 1,
        }
    ]


def test_executor_scene_failure_keeps_job_retryable(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)

    class Harness:
        memory_provider = object()

        def update_memory_after_response(self, **kwargs):
            return None

    monkeypatch.setattr(
        "daemon_core.held_thought.run_held_thought_revision",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Held Thought is suspended from the canonical runtime")
        ),
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda user_id: True,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    with pytest.raises(AfterTurnStageError) as raised:
        execute_after_turn_job(job, harness=Harness())

    assert raised.value.stage == "scene"


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_durable_scene_projection_coalesces_turns_after_every_memory(
    monkeypatch,
    tmp_path,
    host,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-1"),
        host=host,
        source_turn_started_at=1.0,
        read_souvenirs=("theme/one",),
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-2"),
        host=host,
        source_turn_started_at=2.0,
        read_souvenirs=("theme/two",),
    )
    receipt_dir = _enable_receipts(
        monkeypatch,
        tmp_path,
        first,
        second,
    )
    memory_calls = []
    harness = _stub_shared_faculties(monkeypatch, memory_calls)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(first)
    spool.enqueue(second)

    drained = spool.drain(
        host,
        "u",
        lambda job: execute_after_turn_job(
            job,
            harness=harness,
            defer_scene=True,
        ),
    )

    assert drained.completed == 2
    assert memory_calls == [
        "question exacte\navec deux lignes",
        "question exacte\navec deux lignes",
    ]
    assert spool.counts(host, "u")["pending"] == 0
    assert spool.projection_counts(host, "u") == {
        "scene_pending": 1,
        "scene_running": 0,
        "scene_failed": 0,
    }
    projection_path = next(
        (
            tmp_path
            / "spool"
            / "scene_pending"
            / host
            / "u"
        ).glob("*.json")
    )
    projection = SceneProjectionJob.from_dict(
        json.loads(projection_path.read_text(encoding="utf-8"))
    )
    assert projection.source_trace_id == "turn-2"
    assert projection.source_turn_started_at == 2.0
    assert projection.covered_turn_ids == ("turn-1", "turn-2")
    assert projection.read_souvenirs == ("theme/one", "theme/two")

    pending_report = audit_continuity_journal(
        receipt_dir / "continuity_receipts.jsonl",
        now=datetime.now(timezone.utc),
    )
    assert pending_report.status_counts["queued"] == 2
    assert pending_report.status_counts["failed"] == 0

    projected = []
    result = spool.drain_projections(
        host,
        "u",
        projected.append,
    )

    assert result.completed == 1
    assert len(projected) == 1
    assert projected[0].source_trace_id == "turn-2"
    assert spool.projection_counts(host, "u") == {
        "scene_pending": 0,
        "scene_running": 0,
        "scene_failed": 0,
    }
    for turn_id in ("turn-1", "turn-2"):
        receipt = read_continuity_receipt(
            turn_id,
            directory=receipt_dir,
        )
        assert receipt["status"] == "completed"
        assert receipt["stages"]["scene_schedule"]["reason_code"] == (
            "coalesced_projection_completed"
        )
        assert receipt["stages"]["cycle"]["status"] == "completed"


def test_field_burst_profile_keeps_seven_memories_and_two_scenes(
    monkeypatch,
    tmp_path,
):
    jobs = [
        replace(
            _job(
                monkeypatch,
                tmp_path,
                turn_id=f"turn-{index}",
            ),
            source_turn_started_at=float(index),
            read_souvenirs=(f"theme/{index}",),
        )
        for index in range(1, 8)
    ]
    receipt_dir = _enable_receipts(
        monkeypatch,
        tmp_path,
        *jobs,
    )
    memory_calls = []
    harness = _stub_shared_faculties(monkeypatch, memory_calls)
    spool = AfterTurnSpool(tmp_path / "spool")
    projected_batches = []

    def drain_turns(turns):
        for job in turns:
            spool.enqueue(job)
        result = spool.drain(
            "hermes",
            "u",
            lambda queued: execute_after_turn_job(
                queued,
                harness=harness,
                defer_scene=True,
            ),
        )
        assert result.completed == len(turns)
        projected = spool.drain_projections(
            "hermes",
            "u",
            lambda projection: projected_batches.append(
                projection
            ),
        )
        assert projected.completed == 1

    drain_turns(jobs[:1])
    drain_turns(jobs[1:])

    assert len(memory_calls) == 7
    assert [
        projection.covered_turn_ids
        for projection in projected_batches
    ] == [
        ("turn-1",),
        tuple(f"turn-{index}" for index in range(2, 8)),
    ]
    assert projected_batches[-1].source_trace_id == "turn-7"
    assert projected_batches[-1].read_souvenirs == tuple(
        f"theme/{index}" for index in range(2, 8)
    )
    report = audit_continuity_journal(
        receipt_dir / "continuity_receipts.jsonl",
        now=datetime.now(timezone.utc),
    )
    assert report.status_counts["completed"] == 7
    assert report.status_counts["queued"] == 0
    assert report.status_counts["failed"] == 0


def test_projection_retry_does_not_replay_completed_memory(
    monkeypatch,
    tmp_path,
):
    job = replace(
        _job(monkeypatch, tmp_path),
        source_turn_started_at=1.0,
    )
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, job)
    memory_calls = []
    harness = _stub_shared_faculties(monkeypatch, memory_calls)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        max_attempts=2,
    )
    spool.enqueue(job)
    spool.drain(
        "hermes",
        "u",
        lambda queued: execute_after_turn_job(
            queued,
            harness=harness,
            defer_scene=True,
        ),
        now=100.0,
    )

    def fail(_projection):
        raise RuntimeError("provider unavailable")

    first = spool.drain_projections(
        "hermes",
        "u",
        fail,
        now=100.0,
    )
    second = spool.drain_projections(
        "hermes",
        "u",
        fail,
        now=106.0,
    )

    assert first.retried == 1
    assert second.failed == 1
    assert memory_calls == ["question exacte\navec deux lignes"]
    assert spool.projection_counts("hermes", "u") == {
        "scene_pending": 0,
        "scene_running": 0,
        "scene_failed": 1,
    }
    receipt = read_continuity_receipt(
        job.turn_id,
        directory=receipt_dir,
    )
    assert receipt["status"] == "failed"
    assert receipt["stages"]["scene_schedule"]["reason_code"] == (
        "projection_attempts_exhausted"
    )
    assert receipt["stages"]["cycle"]["status"] == "failed"


def test_exhausted_multi_turn_projection_recovers_with_smaller_batches(
    monkeypatch,
    tmp_path,
):
    jobs = [
        replace(
            _job(monkeypatch, tmp_path, turn_id=f"turn-{index}"),
            source_turn_started_at=float(index),
        )
        for index in range(1, 5)
    ]
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, *jobs)
    for job in jobs:
        _record_projection_ready(job)
    spool = AfterTurnSpool(tmp_path / "spool", max_attempts=1)
    for job in jobs:
        spool.enqueue_projection(SceneProjectionJob.from_turn(job))

    failed = spool.drain_projections(
        "hermes",
        "u",
        lambda _projection: (_ for _ in ()).throw(
            ValueError("invalid scene patch")
        ),
        now=100.0,
    )

    assert failed.retried == 1
    assert failed.failed == 0
    pending_path = next(
        (tmp_path / "spool" / "scene_pending" / "hermes" / "u").glob(
            "*.json"
        )
    )
    pending = SceneProjectionJob.from_dict(
        json.loads(pending_path.read_text(encoding="utf-8"))
    )
    assert pending.dialogue_turn_limit == 2
    assert pending.attempts == 0

    completed = spool.drain_projections(
        "hermes",
        "u",
        lambda projection: projection.dialogue_turn_limit == 2,
        now=106.0,
    )

    assert completed.completed == 1
    assert spool.projection_counts("hermes", "u") == {
        "scene_pending": 0,
        "scene_running": 0,
        "scene_failed": 0,
    }
    for job in jobs:
        receipt = read_continuity_receipt(
            job.turn_id,
            directory=receipt_dir,
        )
        assert receipt["status"] == "completed"


def test_legacy_failed_projection_is_requeued_and_receipts_recover(
    monkeypatch,
    tmp_path,
):
    jobs = [
        replace(
            _job(monkeypatch, tmp_path, turn_id=f"turn-{index}"),
            source_turn_started_at=float(index),
        )
        for index in range(1, 5)
    ]
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, *jobs)
    for job in jobs:
        _record_projection_ready(job)
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id="turn-4",
        source_turn_started_at=4.0,
        covered_turn_ids=tuple(job.turn_id for job in jobs),
        attempts=3,
        last_error="ValueError",
    )
    for job in jobs:
        assert finalize_deferred_scene(
            job.turn_id,
            status="failed",
            reason_code="projection_attempts_exhausted",
            projection_ref=projection.projection_ref,
            covered_turn_count=4,
        )
    payload = projection.as_dict()
    payload["schema"] = "daemon.scene-projection-job.v1"
    payload.pop("dialogue_turn_limit")
    AfterTurnSpool(tmp_path / "spool")._ensure_runtime_namespace()
    failed_dir = tmp_path / "spool" / "scene_failed" / "hermes" / "u"
    failed_dir.mkdir(parents=True)
    (failed_dir / f"{projection.projection_ref}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    spool = AfterTurnSpool(tmp_path / "spool")

    result = spool.drain_projections(
        "hermes",
        "u",
        lambda recovered: recovered.dialogue_turn_limit == 2,
    )

    assert result.recovered == 1
    assert result.completed == 1
    assert spool.projection_counts("hermes", "u")["scene_failed"] == 0
    for job in jobs:
        receipt = read_continuity_receipt(
            job.turn_id,
            directory=receipt_dir,
        )
        assert receipt["status"] == "recovered"
        assert receipt["stages"]["scene_schedule"]["status"] == "recovered"
        assert receipt["stages"]["cycle"]["status"] == "recovered"


def test_terminal_projection_is_quarantined_when_newer_scene_exists(
    monkeypatch,
    tmp_path,
):
    import daemon_core.scene_model as scene_model

    job = replace(
        _job(monkeypatch, tmp_path),
        source_turn_started_at=1.0,
    )
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, job)
    _record_projection_ready(job)
    projection = replace(
        SceneProjectionJob.from_turn(job),
        attempts=3,
        dialogue_turn_limit=1,
        last_error="ValueError",
    )
    assert finalize_deferred_scene(
        job.turn_id,
        status="failed",
        reason_code="projection_attempts_exhausted",
        projection_ref=projection.projection_ref,
        covered_turn_count=1,
    )
    failed = (
        tmp_path
        / "spool"
        / "scene_failed"
        / "hermes"
        / "u"
        / f"{projection.projection_ref}.json"
    )
    AfterTurnSpool(tmp_path / "spool")._ensure_runtime_namespace()
    failed.parent.mkdir(parents=True)
    failed.write_text(json.dumps(projection.as_dict()), encoding="utf-8")
    monkeypatch.setattr(
        scene_model,
        "read_scene_model",
        lambda _user_id, _scope: SimpleNamespace(
            source_turn_started_at=2.0
        ),
    )
    spool = AfterTurnSpool(tmp_path / "spool")

    result = spool.drain_projections(
        "hermes",
        "u",
        lambda _projection: (_ for _ in ()).throw(
            AssertionError("superseded projection must not run")
        ),
    )

    assert result.recovered == 1
    assert result.completed == 0
    assert spool.projection_counts("hermes", "u")["scene_failed"] == 0
    quarantined = next(
        (
            tmp_path
            / "spool"
            / "scene_quarantined"
            / "hermes"
            / "u"
        ).glob("*.json")
    )
    resolution = json.loads(
        (
            tmp_path
            / "spool"
            / "projection_quarantine_receipts"
            / "hermes"
            / "u"
            / quarantined.name
        ).read_text(encoding="utf-8")
    )
    assert resolution["status"] == "completed"
    assert resolution["reason_code"] == (
        "projection_superseded_by_current_scene"
    )
    assert resolution["job_sha256"] == hashlib.sha256(
        quarantined.read_bytes()
    ).hexdigest()
    receipt = read_continuity_receipt(
        job.turn_id,
        directory=receipt_dir,
    )
    assert receipt["status"] == "recovered"
    assert receipt["stages"]["scene_schedule"]["reason_code"] == (
        "projection_superseded_by_current_scene"
    )


def test_projection_left_running_is_recovered_without_memory_replay(
    monkeypatch,
    tmp_path,
):
    job = replace(
        _job(monkeypatch, tmp_path),
        source_turn_started_at=1.0,
    )
    receipt_dir = _enable_receipts(monkeypatch, tmp_path, job)
    memory_calls = []
    harness = _stub_shared_faculties(monkeypatch, memory_calls)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        lease_seconds=1.0,
    )
    spool.enqueue(job)
    spool.drain(
        "hermes",
        "u",
        lambda queued: execute_after_turn_job(
            queued,
            harness=harness,
            defer_scene=True,
        ),
        now=100.0,
    )

    with pytest.raises(KeyboardInterrupt):
        spool.drain_projections(
            "hermes",
            "u",
            lambda projection: (_ for _ in ()).throw(
                KeyboardInterrupt()
            ),
            now=100.0,
        )
    running = next(
        (
            tmp_path
            / "spool"
            / "scene_running"
            / "hermes"
            / "u"
        ).glob("*.json")
    )
    payload = json.loads(running.read_text(encoding="utf-8"))
    payload["lease_pid"] = 999_999_999
    running.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(running, (100.0, 100.0))

    assert spool.recover_stale_projections(
        "hermes",
        "u",
        now=101.0,
    ) == 1
    assert spool.projection_counts("hermes", "u")["scene_pending"] == 1
    assert memory_calls == ["question exacte\navec deux lignes"]
    assert spool.user_ids("hermes") == ("u",)

    result = spool.drain_projections(
        "hermes",
        "u",
        lambda projection: None,
        now=102.0,
    )
    assert result.completed == 1
    assert read_continuity_receipt(
        job.turn_id,
        directory=receipt_dir,
    )["status"] == "completed"
    assert memory_calls == ["question exacte\navec deux lignes"]


def test_stale_projection_merges_with_turns_queued_during_its_lease(
    monkeypatch,
    tmp_path,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-1"),
        source_turn_started_at=1.0,
        read_souvenirs=("theme/one",),
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-2"),
        source_turn_started_at=2.0,
        read_souvenirs=("theme/two",),
    )
    receipt_dir = _enable_receipts(
        monkeypatch,
        tmp_path,
        first,
        second,
    )
    _record_projection_ready(first)
    _record_projection_ready(second)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        lease_seconds=1.0,
    )
    spool.enqueue_projection(SceneProjectionJob.from_turn(first))

    with pytest.raises(KeyboardInterrupt):
        spool.drain_projections(
            "hermes",
            "u",
            lambda _projection: (_ for _ in ()).throw(
                KeyboardInterrupt()
            ),
            now=100.0,
        )

    spool.enqueue_projection(SceneProjectionJob.from_turn(second))
    running = next(
        (
            tmp_path
            / "spool"
            / "scene_running"
            / "hermes"
            / "u"
        ).glob("*.json")
    )
    payload = json.loads(running.read_text(encoding="utf-8"))
    payload["lease_pid"] = 999_999_999
    running.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(running, (100.0, 100.0))

    assert spool.recover_stale_projections(
        "hermes",
        "u",
        now=101.0,
    ) == 1
    pending_path = next(
        (
            tmp_path
            / "spool"
            / "scene_pending"
            / "hermes"
            / "u"
        ).glob("*.json")
    )
    pending = SceneProjectionJob.from_dict(
        json.loads(pending_path.read_text(encoding="utf-8"))
    )
    assert pending.source_trace_id == "turn-2"
    assert pending.covered_turn_ids == ("turn-1", "turn-2")
    assert pending.read_souvenirs == ("theme/one", "theme/two")

    projected = []
    result = spool.drain_projections(
        "hermes",
        "u",
        projected.append,
        now=102.0,
    )

    assert result.completed == 1
    assert len(projected) == 1
    assert projected[0].source_trace_id == pending.source_trace_id
    assert projected[0].covered_turn_ids == pending.covered_turn_ids
    assert projected[0].read_souvenirs == pending.read_souvenirs
    for job in (first, second):
        receipt = read_continuity_receipt(
            job.turn_id,
            directory=receipt_dir,
        )
        assert receipt["status"] == "completed"
        assert receipt["stages"]["scene_schedule"]["details"][
            "covered_turn_count"
        ] == 2


@pytest.mark.parametrize("projection_was_enqueued", [False, True])
def test_stale_turn_recovers_scene_handoff_without_replaying_memory(
    monkeypatch,
    tmp_path,
    projection_was_enqueued,
):
    job = replace(
        _job(monkeypatch, tmp_path),
        source_turn_started_at=1.0,
    )
    _enable_receipts(monkeypatch, tmp_path, job)
    memory_calls = []
    harness = _stub_shared_faculties(monkeypatch, memory_calls)
    spool = AfterTurnSpool(
        tmp_path / "spool",
        lease_seconds=1.0,
    )
    spool.enqueue(job)

    def interrupt_after_memory(queued):
        execution = execute_after_turn_job(
            queued,
            harness=harness,
            defer_scene=True,
        )
        if projection_was_enqueued:
            spool.enqueue_projection(execution.scene_projection)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        spool.drain(
            "hermes",
            "u",
            interrupt_after_memory,
            now=100.0,
        )
    running = next(
        (
            tmp_path
            / "spool"
            / "running"
            / "hermes"
            / "u"
        ).glob("*.json")
    )
    payload = json.loads(running.read_text(encoding="utf-8"))
    payload["lease_pid"] = 999_999_999
    running.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(running, (100.0, 100.0))

    assert spool.recover_stale(
        "hermes",
        "u",
        now=101.0,
    ) == 1
    assert spool.counts("hermes", "u") == {
        "pending": 0,
        "running": 0,
        "failed": 0,
    }
    assert spool.projection_counts("hermes", "u")[
        "scene_pending"
    ] == 1
    assert memory_calls == ["question exacte\navec deux lignes"]


def test_turn_arriving_during_projection_forms_the_next_batch(
    monkeypatch,
    tmp_path,
):
    first = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-1"),
        source_turn_started_at=1.0,
        read_souvenirs=("theme/one",),
    )
    second = replace(
        _job(monkeypatch, tmp_path, turn_id="turn-2"),
        source_turn_started_at=2.0,
        read_souvenirs=("theme/two",),
    )
    _enable_receipts(monkeypatch, tmp_path, first, second)
    _record_projection_ready(first)
    _record_projection_ready(second)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue_projection(SceneProjectionJob.from_turn(first))
    batches = []

    def project(running):
        batches.append(running.source_trace_id)
        spool.enqueue_projection(SceneProjectionJob.from_turn(second))

    first_result = spool.drain_projections(
        "hermes",
        "u",
        project,
    )

    assert first_result.completed == 1
    assert batches == ["turn-1"]
    pending = spool.projection_counts("hermes", "u")
    assert pending["scene_pending"] == 1
    assert pending["scene_running"] == 0

    second_result = spool.drain_projections(
        "hermes",
        "u",
        lambda running: batches.append(running.source_trace_id),
    )
    assert second_result.completed == 1
    assert batches == ["turn-1", "turn-2"]


def test_slow_scene_does_not_block_factual_drain_on_another_host(
    monkeypatch,
    tmp_path,
):
    scene_turn = replace(
        _job(monkeypatch, tmp_path, turn_id="scene-turn"),
        source_turn_started_at=1.0,
    )
    factual_turn = replace(
        _job(monkeypatch, tmp_path, turn_id="factual-turn"),
        host="openclaw",
        scene_scope="openclaw__u",
        source_turn_started_at=2.0,
    )
    _enable_receipts(monkeypatch, tmp_path, scene_turn, factual_turn)
    _record_projection_ready(scene_turn)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue_projection(SceneProjectionJob.from_turn(scene_turn))
    scene_started = threading.Event()
    release_scene = threading.Event()
    factual_finished = threading.Event()
    factual_results = []

    def project(_job):
        scene_started.set()
        assert release_scene.wait(timeout=2.0)

    scene_thread = threading.Thread(
        target=lambda: spool.drain_projections(
            "hermes",
            "u",
            project,
        )
    )
    scene_thread.start()
    assert scene_started.wait(timeout=1.0)
    spool.enqueue(factual_turn)

    def drain_fact() -> None:
        factual_results.append(
            spool.drain("openclaw", "u", lambda _job: None)
        )
        factual_finished.set()

    factual_thread = threading.Thread(target=drain_fact)
    factual_thread.start()
    completed_while_scene_running = factual_finished.wait(timeout=1.0)
    release_scene.set()
    factual_thread.join(timeout=2.0)
    scene_thread.join(timeout=2.0)

    assert completed_while_scene_running is True
    assert factual_thread.is_alive() is False
    assert scene_thread.is_alive() is False
    assert factual_results[0].completed == 1


def test_scene_waits_until_all_hosts_have_drained_factual_turns(
    monkeypatch,
    tmp_path,
):
    scene_turn = replace(
        _job(monkeypatch, tmp_path, turn_id="scene-turn"),
        source_turn_started_at=2.0,
    )
    factual_turn = replace(
        _job(monkeypatch, tmp_path, turn_id="factual-turn"),
        host="openclaw",
        scene_scope="openclaw__u",
        source_turn_started_at=1.0,
    )
    _enable_receipts(monkeypatch, tmp_path, scene_turn, factual_turn)
    _record_projection_ready(scene_turn)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue_projection(SceneProjectionJob.from_turn(scene_turn))
    spool.enqueue(factual_turn)
    projected = []

    blocked = spool.drain_projections(
        "hermes",
        "u",
        projected.append,
    )
    factual = spool.drain("openclaw", "u", lambda _job: None)
    completed = spool.drain_projections(
        "hermes",
        "u",
        projected.append,
    )

    assert blocked.completed == 0
    assert factual.completed == 1
    assert completed.completed == 1
    assert [item.source_trace_id for item in projected] == ["scene-turn"]


def test_second_host_does_not_wait_inside_another_hosts_scene(
    monkeypatch,
    tmp_path,
):
    hermes_turn = replace(
        _job(monkeypatch, tmp_path, turn_id="hermes-scene"),
        source_turn_started_at=1.0,
    )
    openclaw_turn = replace(
        _job(monkeypatch, tmp_path, turn_id="openclaw-scene"),
        host="openclaw",
        scene_scope="openclaw__u",
        source_turn_started_at=2.0,
    )
    _enable_receipts(monkeypatch, tmp_path, hermes_turn, openclaw_turn)
    _record_projection_ready(hermes_turn)
    _record_projection_ready(openclaw_turn)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue_projection(SceneProjectionJob.from_turn(hermes_turn))
    spool.enqueue_projection(SceneProjectionJob.from_turn(openclaw_turn))
    first_started = threading.Event()
    release_first = threading.Event()
    second_called = []

    def first_handler(_job):
        first_started.set()
        assert release_first.wait(timeout=2.0)

    first_thread = threading.Thread(
        target=lambda: spool.drain_projections(
            "hermes",
            "u",
            first_handler,
        )
    )
    first_thread.start()
    assert first_started.wait(timeout=1.0)

    second = spool.drain_projections(
        "openclaw",
        "u",
        second_called.append,
    )
    release_first.set()
    first_thread.join(timeout=2.0)
    completed = spool.drain_projections(
        "openclaw",
        "u",
        second_called.append,
    )

    assert second.completed == 0
    assert [item.source_trace_id for item in second_called] == [
        "openclaw-scene"
    ]
    assert completed.completed == 1
    assert first_thread.is_alive() is False


def test_scene_coalescing_requires_receipts(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_SCENE_COALESCING", "canary")
    monkeypatch.delenv(
        "DAEMON_CORE_CONTINUITY_RECEIPTS",
        raising=False,
    )
    monkeypatch.delenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        raising=False,
    )
    assert scene_coalescing_enabled() is False

    monkeypatch.setenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        str(tmp_path / "receipts"),
    )
    assert scene_coalescing_enabled() is True


@pytest.mark.parametrize(
    ("covered", "dialogue_bytes"),
    (
        (MAX_COALESCED_SCENE_TURNS, 1),
        (1, MAX_COALESCED_SCENE_BYTES),
    ),
)
def test_projection_flushes_before_scene_delta_limits(
    tmp_path,
    covered,
    dialogue_bytes,
):
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id=f"turn-{covered}",
        source_turn_started_at=float(covered),
        covered_turn_ids=tuple(
            f"turn-{index}" for index in range(1, covered + 1)
        ),
        dialogue_bytes=dialogue_bytes,
    )
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue_projection(projection)

    assert spool.projection_should_flush("hermes", "u") is True


def test_projection_age_flush_uses_oldest_turn_enqueue_time(
    monkeypatch,
    tmp_path,
):
    first_created = datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc)
    spool = AfterTurnSpool(tmp_path / "spool")
    for index, offset in enumerate((0, 2), start=1):
        created = first_created + timedelta(minutes=offset)
        turn = replace(
            _job(
                monkeypatch,
                tmp_path,
                turn_id=f"turn-{index}",
            ),
            source_turn_started_at=created.timestamp(),
            created_at=created.isoformat(),
        )
        spool.enqueue_projection(
            SceneProjectionJob.from_turn(turn)
        )
    deadline = (
        first_created.timestamp() + MAX_COALESCED_SCENE_AGE_SECONDS
    )

    assert (
        spool.projection_should_flush(
            "hermes",
            "u",
            now=deadline - 0.001,
        )
        is False
    )
    assert (
        spool.projection_should_flush(
            "hermes",
            "u",
            now=deadline,
        )
        is True
    )
    assert (
        spool.projection_should_flush(
            "hermes",
            "u",
            include_age=False,
            now=deadline,
        )
        is False
    )


def test_worker_drains_ready_turns_before_scene_projection():
    calls = []
    batches = []
    ready_answers = iter((True, False))
    worker = None

    class Spool:
        root = Path("/tmp/test-scene-priority")

        def drain(self, host, user_id, handler, *, max_jobs=None):
            calls.append("turns")
            assert max_jobs == 1
            return DrainResult(
                completed=1,
                executions=(f"execution-{calls.count('turns')}",),
            )

        def has_ready_turns(self, host, user_id):
            calls.append("ready")
            return next(ready_answers)

        def projection_should_flush(
            self,
            host,
            user_id,
            *,
            include_age=True,
        ):
            calls.append("flush")
            return False

        def drain_projections(self, host, user_id, handler):
            calls.append("projection")
            worker._stop.set()

    worker = AfterTurnWorker(
        spool=Spool(),
        host="hermes",
        user_id="u",
        handler=lambda job: None,
        projection_handler=lambda job: None,
        batch_handler=lambda executions: (
            calls.append("batch"),
            batches.append(executions),
        ),
        poll_seconds=0.01,
    )

    worker._run()

    assert calls == [
        "turns",
        "ready",
        "flush",
        "turns",
        "ready",
        "batch",
        "projection",
    ]
    assert batches == [("execution-1", "execution-2")]


def test_worker_runs_batch_handler_at_idle_startup_without_turns():
    batches = []
    worker = None

    class Spool:
        root = Path("/tmp/test-batch-startup")

        def drain(self, host, user_id, handler, *, max_jobs=None):
            worker._stop.set()
            return DrainResult()

    worker = AfterTurnWorker(
        spool=Spool(),
        host="hermes",
        user_id="u",
        handler=lambda job: None,
        batch_handler=lambda executions: batches.append(executions),
        poll_seconds=0.01,
    )

    worker._run()

    assert batches == [()]


def test_worker_flushes_bounded_projection_before_more_turns():
    calls = []
    worker = None

    class Spool:
        root = Path("/tmp/test-scene-bounds")

        def drain(self, host, user_id, handler, *, max_jobs=None):
            calls.append("turns")
            return DrainResult(completed=1)

        def has_ready_turns(self, host, user_id):
            calls.append("ready")
            return True

        def projection_should_flush(
            self,
            host,
            user_id,
            *,
            include_age=True,
        ):
            calls.append("flush")
            return True

        def drain_projections(self, host, user_id, handler):
            calls.append("projection")
            worker._stop.set()

    worker = AfterTurnWorker(
        spool=Spool(),
        host="hermes",
        user_id="u",
        handler=lambda job: None,
        projection_handler=lambda job: None,
        poll_seconds=0.01,
    )

    worker._run()

    assert calls == ["turns", "ready", "flush", "projection"]


def test_worker_bounds_aged_seventeen_turn_burst_to_two_projections():
    counts = {
        "turns": 0,
        "age_checks": 0,
        "suppressed_age_checks": 0,
        "projections": 0,
    }
    ready_answers = iter((True,) * 16 + (False,))
    worker = None

    class Spool:
        root = Path("/tmp/test-scene-age-bound")

        def drain(self, host, user_id, handler, *, max_jobs=None):
            counts["turns"] += 1
            return DrainResult(completed=1)

        def has_ready_turns(self, host, user_id):
            return next(ready_answers)

        def projection_should_flush(
            self,
            host,
            user_id,
            *,
            include_age=True,
        ):
            key = (
                "age_checks"
                if include_age
                else "suppressed_age_checks"
            )
            counts[key] += 1
            return include_age

        def drain_projections(self, host, user_id, handler):
            counts["projections"] += 1
            if counts["projections"] == 2:
                worker._stop.set()
            return DrainResult(completed=1)

    worker = AfterTurnWorker(
        spool=Spool(),
        host="hermes",
        user_id="u",
        handler=lambda job: None,
        projection_handler=lambda job: None,
        poll_seconds=0.01,
    )

    worker._run()

    assert counts == {
        "turns": 17,
        "age_checks": 1,
        "suppressed_age_checks": 15,
        "projections": 2,
    }


def test_shared_projection_executor_uses_latest_turn_and_union(
    monkeypatch,
):
    calls = []

    class Harness:
        memory_provider = object()

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda user_id: False,
    )

    def revise(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(revision=9)

    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        revise,
    )
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        covered_turn_ids=("turn-1", "turn-2"),
        read_souvenirs=("theme/one", "theme/two"),
        library_active=True,
    )

    scene = execute_scene_projection(
        projection,
        harness=Harness(),
    )

    assert scene.revision == 9
    assert calls == [
        {
            "user_id": "u",
            "scene_scope": "telegram__u",
            "provider": Harness.memory_provider,
            "source_trace_id": "turn-2",
            "source_turn_started_at": 2.0,
            "read_souvenirs": ["theme/one", "theme/two"],
            "strict": True,
        }
    ]


def test_shared_projection_executor_projects_scene_texture(monkeypatch):
    projected = []

    class Harness:
        memory_provider = object()

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        lambda **_kwargs: SimpleNamespace(
            revision=3,
            reading="Le sujet concret reste le jardin.",
            conversation="Le sujet reste vivant.",
        ),
    )
    monkeypatch.setattr(
        "daemon_core.conversation_state.project_scene_conversation_state",
        lambda user_id, **values: projected.append(
            (user_id, values)
        ),
    )
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        covered_turn_ids=("turn-1",),
        library_active=True,
    )

    execute_scene_projection(projection, harness=Harness())

    assert projected == [
        (
            "u",
            {
                "reading": "Le sujet concret reste le jardin.",
                "conversation": "Le sujet reste vivant.",
            },
        )
    ]


def test_shared_projection_executor_consumes_recovery_batches_in_order(
    monkeypatch,
):
    contexts = [
        {
            "dialogue_turn_count": 2,
            "dialogue_truncated": True,
            "batch": 1,
        },
        {
            "dialogue_turn_count": 2,
            "dialogue_truncated": False,
            "batch": 2,
        },
    ]
    revisions = []

    class Harness:
        memory_provider = object()

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.capture_scene_model_context",
        lambda *_args, **kwargs: {
            **contexts.pop(0),
            "limit": kwargs["dialogue_turn_limit"],
        },
    )

    def revise(**kwargs):
        revisions.append(kwargs)
        return SimpleNamespace(revision=len(revisions))

    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        revise,
    )
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id="turn-4",
        source_turn_started_at=4.0,
        covered_turn_ids=("turn-1", "turn-2", "turn-3", "turn-4"),
        library_active=True,
        dialogue_turn_limit=2,
    )

    scene = execute_scene_projection(projection, harness=Harness())

    assert scene.revision == 2
    assert [call["context"]["batch"] for call in revisions] == [1, 2]
    assert all(call["context"]["limit"] == 2 for call in revisions)
    assert all(call["allow_same_source_revision"] for call in revisions)


def test_shared_projection_executor_stops_when_recovery_makes_no_progress(
    monkeypatch,
):
    class Harness:
        memory_provider = object()

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.capture_scene_model_context",
        lambda *_args, **_kwargs: {
            "dialogue_turn_count": 2,
            "dialogue_truncated": True,
        },
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        lambda **_kwargs: None,
    )
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        covered_turn_ids=("turn-1", "turn-2"),
        library_active=True,
        dialogue_turn_limit=1,
    )

    with pytest.raises(RuntimeError, match="made no progress"):
        execute_scene_projection(projection, harness=Harness())


def test_shared_projection_executor_completes_without_llm_when_delta_is_empty(
    monkeypatch,
):
    previous = SimpleNamespace(revision=7)

    class Harness:
        memory_provider = object()

    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.capture_scene_model_context",
        lambda *_args, **_kwargs: {
            "dialogue_turn_count": 0,
            "dialogue_truncated": False,
            "previous": previous,
        },
    )
    revise = Mock()
    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        revise,
    )
    projection = SceneProjectionJob(
        host="hermes",
        user_id="u",
        scene_scope="telegram__u",
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        covered_turn_ids=("turn-1", "turn-2"),
        library_active=True,
        dialogue_turn_limit=1,
    )

    scene = execute_scene_projection(projection, harness=Harness())

    assert scene is previous
    revise.assert_not_called()


def _drain_until_settled(spool, handler, *, start, limit=200):
    """Drain repeatedly, jumping past every scheduled delay."""
    now = start
    for _ in range(limit):
        spool.drain("hermes", "u", handler, now=now)
        counts = spool.counts("hermes", "u")
        if not any(counts.values()):
            return now
        now += 2 * 86400.0
    raise AssertionError("the queue never settled")


def test_always_failing_turn_is_set_aside_and_later_turns_proceed(
    monkeypatch,
    tmp_path,
):
    first = replace(_job(monkeypatch, tmp_path, turn_id="turn-1"), source_turn_started_at=1.0)
    second = replace(_job(monkeypatch, tmp_path, turn_id="turn-2"), source_turn_started_at=2.0)
    _enable_receipts(monkeypatch, tmp_path, first, second)
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(first)
    spool.enqueue(second)
    calls = []

    def handler(job):
        calls.append(job.turn_id)
        if job.turn_id == "turn-1":
            raise ValueError("deterministic refusal")

    _drain_until_settled(spool, handler, start=100.0)

    assert calls.count("turn-1") == spool.max_total_attempts == 9
    assert calls[-1] == "turn-2" and calls.count("turn-2") == 1
    aside = list((tmp_path / "spool" / "quarantined").rglob("*.json"))
    assert [json.loads(path.read_text())["turn_id"] for path in aside] == ["turn-1"]
    receipt = json.loads(next((tmp_path / "spool" / "quarantine_receipts").rglob("*.json")).read_text())
    assert receipt["reason_code"] == "retries_exhausted"
    assert receipt["error_type"] == "RetriesExhausted"
    assert receipt["status"] == "completed"
    stages = read_continuity_receipt("turn-1")["stages"]
    assert stages["outbox"]["reason_code"] == "retries_exhausted"


def test_retry_schedule_grows_and_never_overflows(monkeypatch, tmp_path):
    spool = AfterTurnSpool(tmp_path / "spool")
    assert [after_turn_spool._retry_delay(n) for n in (1, 2, 3)] == [5.0, 10.0, 20.0]
    assert after_turn_spool._retry_delay(10_000) == 300.0
    assert [spool._failure_delay(n) for n in range(1, 9)] == [
        5.0, 10.0, 20.0, 300.0, 1800.0, 7200.0, 21600.0, 86400.0,
    ]
    assert spool._failure_delay(10_000) == 86400.0


def test_turn_stuck_with_huge_attempt_count_is_set_aside_without_a_model_call(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(tmp_path / "spool", max_attempts=1)
    spool.enqueue(job)
    spool.drain("hermes", "u", lambda _job: (_ for _ in ()).throw(RuntimeError("x")), now=100.0)
    failed = next((tmp_path / "spool" / "failed").rglob("*.json"))
    payload = json.loads(failed.read_text(encoding="utf-8"))
    payload["attempts"] = 1553  # observed on a field daemon before this guard
    failed.write_text(json.dumps(payload), encoding="utf-8")
    handler = Mock()

    result = spool.drain("hermes", "u", handler, now=200.0)

    handler.assert_not_called()
    assert result.quarantined == 1
    assert spool.counts("hermes", "u") == {"pending": 0, "running": 0, "failed": 0}


def test_worker_dying_on_the_same_turn_at_every_restart_cannot_loop(
    monkeypatch,
    tmp_path,
):
    job = _job(monkeypatch, tmp_path)
    spool = AfterTurnSpool(tmp_path / "spool", lease_seconds=1.0)
    spool.enqueue(job)
    claims = 0
    now = 100.0

    def die(_job):
        nonlocal claims
        claims += 1
        raise KeyboardInterrupt()

    for _ in range(100):
        try:
            spool.drain("hermes", "u", die, now=now)
        except KeyboardInterrupt:
            running = next((tmp_path / "spool" / "running").rglob("*.json"))
            os.utime(running, (now, now))
        if not any(spool.counts("hermes", "u").values()):
            break
        now += 2 * 86400.0
        spool.recover_stale("hermes", "u", now=now)

    assert claims == spool.max_total_attempts
    assert len(list((tmp_path / "spool" / "quarantined").rglob("*.json"))) == 1
