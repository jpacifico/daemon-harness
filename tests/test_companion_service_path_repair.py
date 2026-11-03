from dataclasses import replace
from pathlib import Path

import pytest

from daemons_companion.runtime.services import (
    BackgroundJob,
    render_job_definition,
)
from daemons_companion.runtime.service_path_repair import ServicePathRepair
from tests.test_companion_gateway_repair import capture


def fixture(tmp_path, monkeypatch):
    import daemons_companion.runtime.service_path_repair as module

    env = tmp_path / ".env"
    root = tmp_path / "memory"
    monkeypatch.setattr(
        module,
        "_source_io_path",
        lambda p: Path("/extended") / str(p).lstrip("/") if p == root else p,
    )
    env.write_text(
        f"# retained\nSECRET=synthetic-token\nDAEMON_CORE_MEMORY_ROOT={root}\n"
    )
    pairs = []
    for code, persistent in [
        ("daemon-hermes-gateway", True),
        ("daemon-after-turn", True),
        ("daemon-nightly", False),
    ]:
        old = BackgroundJob(
            code,
            "/python",
            ("-c", "old"),
            str(tmp_path),
            str(tmp_path / "log"),
            persistent,
            4 if not persistent else None,
        )
        pairs.append(
            (old, replace(old, executable="/extended/python", arguments=("-c", "new")))
        )

    class Scheduler:
        failure = False
        operations = []
        current = {
            old.code: capture(render_job_definition(old, "windows"), old.persistent)
            for old, _ in pairs
        }

        def capture(self, code):
            return self.current.get(code)

        def restore(self, code, value):
            self.operations.append(("restore", code, value is not None))
            if value is None:
                self.current.pop(code, None)
            else:
                self.current[code] = value

        def install(self, job, definition):
            assert all("/extended/" in env.read_text() for _ in [0])
            self.operations.append(("install", job.code))
            self.current[job.code] = capture(definition, job.persistent)
            if self.failure:
                raise RuntimeError("start failed")

        def matches(self, job, definition):
            return self.capture(job.code) == capture(definition, job.persistent)

    scheduler = Scheduler()
    adapter = ServicePathRepair(
        env, {"DAEMON_CORE_MEMORY_ROOT": root}, pairs, scheduler
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    return adapter, scheduler, env, snapshot, pairs


def test_switches_all_writers_before_paths_and_keeps_other_env_bytes(
    tmp_path, monkeypatch
):
    adapter, scheduler, env, snapshot, pairs = fixture(tmp_path, monkeypatch)
    assert adapter.needs_repair()
    adapter.snapshot(None, snapshot)
    adapter.apply(None)
    adapter.verify(None)
    assert not adapter.needs_repair()
    assert env.read_text().startswith("# retained\nSECRET=synthetic-token\n")
    assert scheduler.operations[:3] == [
        ("restore", new.code, False) for _, new in pairs
    ]
    assert [x[0] for x in scheduler.operations[3:]] == ["install"] * 3


def test_partial_restart_restores_all_definitions_states_and_environment(
    tmp_path, monkeypatch
):
    adapter, scheduler, env, snapshot, _ = fixture(tmp_path, monkeypatch)
    before = env.read_bytes()
    jobs = dict(scheduler.current)
    adapter.snapshot(None, snapshot)
    scheduler.failure = True
    with pytest.raises(RuntimeError, match="start failed"):
        adapter.apply(None)
    # New adapter simulates recovering from the next assistant process.
    recovered = ServicePathRepair(
        adapter.env_path, adapter.roots, adapter.pairs, scheduler
    )
    recovered.rollback(None, snapshot)
    assert env.read_bytes() == before and scheduler.current == jobs


@pytest.mark.parametrize("changed", ["environment", "task", "missing"])
def test_foreign_state_refused_before_any_stop(tmp_path, monkeypatch, changed):
    adapter, scheduler, env, snapshot, pairs = fixture(tmp_path, monkeypatch)
    if changed == "environment":
        env.write_text("DAEMON_CORE_MEMORY_ROOT=/outside\n")
    elif changed == "missing":
        scheduler.current.pop(pairs[0][0].code)
    else:
        scheduler.current[pairs[0][0].code] = capture(
            render_job_definition(
                replace(pairs[0][0], arguments=("foreign",)), "windows"
            )
        )
    with pytest.raises((ValueError, RuntimeError)):
        adapter.needs_repair()
    assert not scheduler.operations


def test_concurrent_change_blocks_rollback_without_touching_other_services(
    tmp_path, monkeypatch
):
    adapter, scheduler, env, snapshot, pairs = fixture(tmp_path, monkeypatch)
    adapter.snapshot(None, snapshot)
    adapter.apply(None)
    scheduler.current[pairs[0][0].code] = capture(
        render_job_definition(replace(pairs[0][0], arguments=("foreign",)), "windows")
    )
    before = dict(scheduler.current)
    before_env = env.read_bytes()
    with pytest.raises(RuntimeError, match="concurrently"):
        adapter.rollback(None, snapshot)
    assert scheduler.current == before and env.read_bytes() == before_env


def test_stopped_canonical_worker_needs_repair(tmp_path, monkeypatch):
    adapter, scheduler, env, snapshot, pairs = fixture(tmp_path, monkeypatch)
    adapter.snapshot(None, snapshot)
    adapter.apply(None)
    assert not adapter.needs_repair()
    _, worker = pairs[1]
    scheduler.current[worker.code] = capture(render_job_definition(worker, 'windows'), False)
    assert adapter.needs_repair()
