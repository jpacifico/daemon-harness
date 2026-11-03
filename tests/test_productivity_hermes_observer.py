from __future__ import annotations

import contextvars
import json
from pathlib import Path
import sys
import threading
import types

from benchmarks.productivity_v1.runner import hermes_observer


def test_native_background_probe_marks_usage_and_waits_for_completion(
    monkeypatch,
    tmp_path: Path,
):
    trace = tmp_path / "trace.jsonl"
    background_review = types.ModuleType("agent.background_review")

    def spawn_background_review_thread(*_args, **_kwargs):
        def target():
            context = contextvars.copy_context()
            hook_worker = threading.Thread(
                target=lambda: context.run(
                    hermes_observer._on_post_api_request,
                    session_id="background-session",
                    usage={"input_tokens": 10, "total_tokens": 12},
                )
            )
            hook_worker.start()
            hook_worker.join(timeout=1)

        return target, "prompt"

    background_review.spawn_background_review_thread = spawn_background_review_thread
    agent = types.ModuleType("agent")
    agent.background_review = background_review
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.background_review", background_review)
    monkeypatch.setenv("PRODUCTIVITY_BENCH_TRACE_FILE", str(trace))
    monkeypatch.setenv("PRODUCTIVITY_BENCH_WAIT_NATIVE_BACKGROUND", "1")
    monkeypatch.setenv("PRODUCTIVITY_BENCH_NATIVE_BACKGROUND_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(hermes_observer, "_BACKGROUND_PENDING", 0)
    monkeypatch.setattr(hermes_observer, "_BACKGROUND_SCHEDULED", 0)
    monkeypatch.setattr(hermes_observer, "_BACKGROUND_COMPLETED", 0)
    monkeypatch.setattr(hermes_observer, "_BACKGROUND_FAILED", 0)

    hermes_observer._install_background_probe()
    target, _ = background_review.spawn_background_review_thread(None, [])
    worker = threading.Thread(target=target)
    worker.start()
    hermes_observer._on_session_end(session_id="foreground-session")
    worker.join(timeout=1)

    records = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    usage = next(
        record for record in records if record["event"] == "api_request_complete"
    )
    barrier = next(
        record for record in records if record["event"] == "native_background_barrier"
    )

    assert usage["payload"]["phase"] == "native_background"
    assert barrier["payload"]["complete"] is True
    assert barrier["payload"]["scheduled"] == 1
    assert barrier["payload"]["completed"] == 1
    assert barrier["payload"]["pending"] == 0
