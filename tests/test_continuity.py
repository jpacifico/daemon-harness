import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.after_turn import (
    AfterTurnHooks,
    AfterTurnRequest,
    complete_turn,
    record_turn_message,
)
from daemon_core.continuity import (
    begin_continuity_turn,
    continuity_receipt_path,
    read_continuity_receipt,
    record_continuity_stage,
)
from daemon_core.turn_runtime import TurnRequest, prepare_turn


def _enable_receipts(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "shadow")
    monkeypatch.setenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        str(tmp_path),
    )
    monkeypatch.setenv("DAEMON_CORE_RUNTIME_REVISION", "test-revision")


def _voice_context():
    return SimpleNamespace(
        prompt_variant="thin",
        scene_scope="telegram__private",
        hot_memory=SimpleNamespace(souvenir_names=("theme/private",)),
    )


def _after_turn_request(trace_id, *, response="réponse privée"):
    return AfterTurnRequest(
        user_id="private-user",
        user_message="question privée",
        assistant_response=response,
        scene_scope="telegram__private",
        source_trace_id=trace_id,
        source_turn_started_at=123.0,
    )


def test_shadow_receipt_is_disabled_without_explicit_configuration(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("DAEMON_CORE_CONTINUITY_RECEIPTS", raising=False)
    monkeypatch.delenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        raising=False,
    )

    assert (
        begin_continuity_turn(
            "trace-disabled",
            user_id="private-user",
            platform="telegram",
        )
        is False
    )
    assert not continuity_receipt_path(
        "trace-disabled",
        directory=tmp_path,
    ).exists()


def test_nominal_turn_receipt_observes_without_changing_composition(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "trace-nominal"
    calls = []
    messages = [
        {"role": "system", "content": "SYSTEME EXACT"},
        {"role": "user", "content": "question privée"},
    ]

    def composer(*args, **kwargs):
        calls.append((args, kwargs))
        return messages, _voice_context()

    plan = prepare_turn(
        TurnRequest(
            user_id="private-user",
            message="question privée",
            platform="telegram",
            session_id="private-session",
            trace_id=trace_id,
        ),
        composer=composer,
    )
    writes = []
    complete_turn(
        _after_turn_request(trace_id),
        AfterTurnHooks(
            record_l0=lambda request: (
                record_turn_message(
                    request.user_id,
                    "user",
                    request.user_message,
                    writer=lambda **kwargs: writes.append(kwargs),
                    source_trace_id=request.source_trace_id,
                ),
                record_turn_message(
                    request.user_id,
                    "assistant",
                    request.assistant_response,
                    writer=lambda **kwargs: writes.append(kwargs),
                    source_trace_id=request.source_trace_id,
                ),
            ),
            update_memory=lambda request: "updated",
            revise_working_state=lambda request: None,
            revise_scene=lambda request: None,
        ),
        spawn=lambda target, name: target(),
    )

    assert list(plan.messages) == messages
    assert len(calls) == 1
    assert [item["role"] for item in writes] == ["user", "assistant"]

    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    assert receipt["status"] == "completed"
    assert receipt["stages"]["preparation"]["status"] == "completed"
    assert receipt["stages"]["l0_user"]["status"] == "completed"
    assert receipt["stages"]["l0_assistant"]["status"] == "completed"
    assert receipt["stages"]["memory"]["status"] == "completed"
    assert receipt["stages"]["scene_schedule"]["status"] == "completed"
    assert receipt["stages"]["cycle"]["status"] == "completed"
    assert receipt["anomalies"] == []

    raw = continuity_receipt_path(
        trace_id,
        directory=tmp_path,
    ).read_text(encoding="utf-8")
    assert "question privée" not in raw
    assert "réponse privée" not in raw
    assert "SYSTEME EXACT" not in raw
    assert "private-user" not in raw
    assert "private-session" not in raw
    assert "theme/private" not in raw


def test_identity_can_arrive_after_an_exact_l0_event(monkeypatch, tmp_path):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "trace-late-identity"

    record_continuity_stage(
        trace_id,
        "l0_user",
        "completed",
        reason_code="writer_completed",
    )
    begin_continuity_turn(
        trace_id,
        user_id="private-user",
        platform="openclaw",
        session_id="private-session",
    )

    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    assert receipt["stages"]["l0_user"]["status"] == "completed"
    assert receipt["stages"]["turn"]["status"] == "pending"
    assert receipt["identity"]["platform"] == "openclaw"
    assert receipt["identity"]["user_ref"]
    assert receipt["identity"]["session_ref"]


def test_receipt_writer_reopens_after_atomic_journal_replacement(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    first_trace = "trace-before-replacement"
    second_trace = "trace-after-replacement"

    assert record_continuity_stage(
        first_trace,
        "preparation",
        "completed",
        reason_code="composer_completed",
    )
    path = continuity_receipt_path(first_trace, directory=tmp_path)
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)

    assert record_continuity_stage(
        second_trace,
        "preparation",
        "completed",
        reason_code="composer_completed",
    )

    assert read_continuity_receipt(first_trace, directory=tmp_path)
    second = read_continuity_receipt(second_trace, directory=tmp_path)
    assert second["stages"]["preparation"]["status"] == "completed"


def test_preparation_receipt_keeps_the_technical_situation_source(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)

    assert record_continuity_stage(
        "trace-seed-recovery",
        "preparation",
        "completed",
        reason_code="composer_completed",
        situation_source="seed_recovery:unprovable_delta",
    )

    receipt = read_continuity_receipt(
        "trace-seed-recovery",
        directory=tmp_path,
    )
    assert receipt["stages"]["preparation"]["details"] == {
        "situation_source": "seed_recovery:unprovable_delta",
    }


def test_memory_mutation_receipt_keeps_bounded_paths_and_l0_references(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "trace-memory-provenance"
    reference = {
        "path": "levels/L0_ground_zero/conversations/u_2026-08-01.md",
        "offset": 12,
        "length": 34,
        "sha256": "a" * 64,
        "role": "user",
        "turn_id": trace_id,
    }

    record_continuity_stage(
        trace_id,
        "memory_mutation",
        "completed",
        reason_code="memory_files_changed",
        user_ref="technical-user-ref",
        source_turn_started_at=123.456,
        changed_paths=["users/u/agenda.md", "capsules/theme/work.md"],
        context_paths=["capsules/theme/family.md"],
        unchanged_paths=["users/u/context.md"],
        rejected_paths=["../outside.md", "/absolute.md"],
        user_l0_ref=reference,
        assistant_l0_ref={**reference, "role": "assistant", "offset": 46},
        ignored_payload={"content": "private"},
    )

    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    details = receipt["stages"]["memory_mutation"]["details"]
    assert details["changed_paths"] == [
        "users/u/agenda.md",
        "capsules/theme/work.md",
    ]
    assert details["unchanged_paths"] == ["users/u/context.md"]
    assert details["context_paths"] == ["capsules/theme/family.md"]
    assert "rejected_paths" not in details
    assert details["user_l0_ref"] == reference
    assert details["assistant_l0_ref"]["role"] == "assistant"
    assert "ignored_payload" not in details


def test_memory_agent_failure_is_visible_and_prevents_false_success(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "trace-memory-failure"
    begin_continuity_turn(
        trace_id,
        user_id="private-user",
        platform="openclaw",
    )

    def fail_memory(request):
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        complete_turn(
            _after_turn_request(trace_id, response="réponse déjà visible"),
            AfterTurnHooks(
                update_memory=fail_memory,
                revise_working_state=lambda request: None,
                revise_scene=lambda request: None,
            ),
            spawn=lambda target, name: target(),
        )

    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["stages"]["memory"]["status"] == "failed"
    assert (
        receipt["stages"]["memory"]["details"]["error_type"]
        == "RuntimeError"
    )
    assert receipt["stages"]["scene_schedule"]["status"] == "skipped"
    assert receipt["stages"]["followup"]["status"] == "skipped"
    assert receipt["stages"]["cycle"]["status"] == "failed"


def test_preparation_failure_is_recorded_and_original_error_is_preserved(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "trace-preparation-failure"
    calls = []

    def fail_composer(*args, **kwargs):
        calls.append((args, kwargs))
        raise LookupError("atlas unavailable")

    with pytest.raises(LookupError, match="atlas unavailable"):
        prepare_turn(
            TurnRequest(
                user_id="private-user",
                message="question privée",
                platform="openclaw",
                trace_id=trace_id,
            ),
            composer=fail_composer,
        )

    assert len(calls) == 1
    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["stages"]["preparation"]["status"] == "failed"
    assert (
        receipt["stages"]["preparation"]["details"]["error_type"]
        == "LookupError"
    )
    assert "atlas unavailable" not in continuity_receipt_path(
        trace_id,
        directory=tmp_path,
    ).read_text(encoding="utf-8")
