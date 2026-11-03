import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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
from server.routes import openclaw


def _enable_receipts(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "shadow")
    monkeypatch.setenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        str(tmp_path),
    )
    monkeypatch.setenv("DAEMON_CORE_RUNTIME_REVISION", "multiturn")


def _run_nominal_turn(trace_id, platform, writes, writes_lock):
    question = f"SECRET-QUESTION-{trace_id}"
    response = f"SECRET-ANSWER-{trace_id}"
    context = SimpleNamespace(
        prompt_variant="thin",
        scene_scope=f"{platform}__shared",
        hot_memory=SimpleNamespace(souvenir_names=("theme/private",)),
    )
    messages = [
        {"role": "system", "content": f"SECRET-SYSTEM-{trace_id}"},
        {"role": "user", "content": question},
    ]

    plan = prepare_turn(
        TurnRequest(
            user_id="shared-user",
            message=question,
            platform=platform,
            session_id=f"{platform}-session",
            trace_id=trace_id,
        ),
        composer=lambda *args, **kwargs: (messages, context),
    )
    record_continuity_stage(
        trace_id,
        "voice_path",
        "completed",
        reason_code="delegated",
        voice_path="sovereign_library",
    )
    record_continuity_stage(
        trace_id,
        "response",
        "completed",
        reason_code="host_generation_completed",
        voice_path="sovereign_library",
    )

    def writer(**kwargs):
        with writes_lock:
            writes.append((trace_id, kwargs["role"], kwargs["content"]))

    def record_l0(request):
        record_turn_message(
            request.user_id,
            "user",
            request.user_message,
            writer=writer,
            source_trace_id=request.source_trace_id,
        )
        record_turn_message(
            request.user_id,
            "assistant",
            request.assistant_response,
            writer=writer,
            source_trace_id=request.source_trace_id,
        )

    complete_turn(
        AfterTurnRequest(
            user_id="shared-user",
            user_message=question,
            assistant_response=response,
            scene_scope=context.scene_scope,
            source_trace_id=trace_id,
            source_turn_started_at=123.0,
        ),
        AfterTurnHooks(
            record_l0=record_l0,
            update_memory=lambda request: "updated",
            revise_working_state=lambda request: None,
            revise_scene=lambda request: None,
            after_memory=lambda result: None,
        ),
        spawn=lambda target, name: target(),
    )
    assert list(plan.messages) == messages


def test_shared_journal_isolates_sequential_and_concurrent_turns(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    writes = []
    writes_lock = threading.Lock()
    sequential = [
        (f"seq-{index}", "telegram" if index % 2 == 0 else "openclaw")
        for index in range(16)
    ]
    concurrent = [
        (f"parallel-{index}", "telegram" if index % 2 == 0 else "openclaw")
        for index in range(24)
    ]

    for trace_id, platform in sequential:
        _run_nominal_turn(
            trace_id,
            platform,
            writes,
            writes_lock,
        )
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                _run_nominal_turn,
                trace_id,
                platform,
                writes,
                writes_lock,
            )
            for trace_id, platform in concurrent
        ]
        for future in futures:
            future.result()

    all_turns = sequential + concurrent
    for trace_id, platform in all_turns:
        receipt = read_continuity_receipt(
            trace_id,
            directory=tmp_path,
        )
        assert receipt["status"] == "completed"
        assert receipt["identity"]["platform"] == platform
        assert receipt["stages"]["preparation"]["status"] == "completed"
        assert receipt["stages"]["l0_user"]["status"] == "completed"
        assert receipt["stages"]["l0_assistant"]["status"] == "completed"
        assert receipt["stages"]["memory"]["status"] == "completed"
        assert receipt["stages"]["scene_schedule"]["status"] == "completed"
        assert receipt["stages"]["cycle"]["status"] == "completed"
        assert receipt["anomalies"] == []

    assert len(writes) == len(all_turns) * 2
    journal = continuity_receipt_path(
        "any-trace",
        directory=tmp_path,
    )
    records = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
    ]
    assert records
    assert {record["turn_id"] for record in records} == {
        trace_id for trace_id, _ in all_turns
    }
    raw = journal.read_text(encoding="utf-8")
    assert "SECRET-" not in raw
    assert "shared-user" not in raw
    assert "theme/private" not in raw


def test_delayed_workers_cannot_create_a_premature_success(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "delayed-turn"
    begin_continuity_turn(
        trace_id,
        user_id="u",
        platform="telegram",
    )
    targets = {}

    def capture_spawn(target, name):
        targets[name] = target

    def record_l0(request):
        for role, content in (
            ("user", request.user_message),
            ("assistant", request.assistant_response),
        ):
            record_turn_message(
                request.user_id,
                role,
                content,
                writer=lambda **kwargs: None,
                source_trace_id=request.source_trace_id,
            )

    complete_turn(
        AfterTurnRequest(
            user_id="u",
            user_message="question",
            assistant_response="response",
            scene_scope="telegram__u",
            source_trace_id=trace_id,
            source_turn_started_at=1.0,
        ),
        AfterTurnHooks(
            record_l0=record_l0,
            update_memory=lambda request: None,
            revise_working_state=lambda request: None,
            revise_scene=lambda request: None,
        ),
        spawn=capture_spawn,
    )

    pending = read_continuity_receipt(trace_id, directory=tmp_path)
    assert pending["status"] == "pending"
    assert pending["stages"]["working_state"]["status"] == "pending"
    assert pending["stages"]["scene_schedule"]["status"] == "pending"

    working_name = next(
        name for name in targets if "working-state" in name
    )
    scene_name = next(name for name in targets if "scene" in name)
    targets[working_name]()
    still_pending = read_continuity_receipt(trace_id, directory=tmp_path)
    assert still_pending["status"] == "pending"
    targets[scene_name]()

    completed = read_continuity_receipt(trace_id, directory=tmp_path)
    assert completed["status"] == "completed"
    assert completed["anomalies"] == []


def test_duplicate_exact_l0_write_is_reported_as_integrity_failure(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "duplicate-l0"
    begin_continuity_turn(
        trace_id,
        user_id="u",
        platform="telegram",
    )
    for _ in range(2):
        record_turn_message(
            "u",
            "user",
            "same message",
            writer=lambda **kwargs: None,
            source_trace_id=trace_id,
        )

    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["anomalies"] == [
        {
            "code": "duplicate_l0_user",
            "severity": "failure",
            "count": 2,
        }
    ]


def test_openclaw_busy_memory_slot_is_explicitly_non_nominal(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)
    trace_id = "openclaw-slot-busy"
    begin_continuity_turn(
        trace_id,
        user_id="u",
        platform="openclaw",
    )

    class BusySlot:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

    monkeypatch.setattr(openclaw, "_memory_slot", BusySlot())
    barrier = openclaw._schedule_sovereign_after_turn(
        AfterTurnRequest(
            user_id="u",
            user_message="question",
            assistant_response="response",
            scene_scope="",
            source_trace_id=trace_id,
            source_turn_started_at=1.0,
            user_l0_recorded=True,
            assistant_l0_recorded=True,
            library_active=True,
        )
    )

    assert barrier is None
    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    assert receipt["status"] == "skipped"
    assert receipt["stages"]["cycle"]["reason_code"] == "memory_slot_busy"
