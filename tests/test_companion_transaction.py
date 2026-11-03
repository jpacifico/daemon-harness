from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import threading
import time

import pytest

from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.setup.transaction import (
    PlanConfirmationError,
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


def _plan(host: HostKind = HostKind.HERMES):
    probe = HostProbe(
        host=host,
        detected=True,
        usable=True,
        executable=f"/synthetic/{host.value}",
        version="synthetic-1",
        version_status="verified",
    )
    return build_setup_plan(
        SetupRequest(
            host=host,
            daemon_name="Lyra",
            daemon_gender="feminine",
            provider=ProviderSelection(
                ProviderKind.OPENAI_API,
                model="synthetic-model",
                credential_available=True,
            ),
        ),
        (probe,),
    )


class DisposableProfileAdapter:
    def __init__(
        self,
        profile: Path,
        *,
        fail_apply: str = "",
        fail_verify: str = "",
        fail_rollback: str = "",
        interrupt_snapshot: str = "",
        interrupt_apply: str = "",
        delay: float = 0.0,
    ) -> None:
        self.profile = profile
        self.fail_apply = fail_apply
        self.fail_verify = fail_verify
        self.fail_rollback = fail_rollback
        self.interrupt_snapshot = interrupt_snapshot
        self.interrupt_apply = interrupt_apply
        self.delay = delay
        self.events: list[tuple[str, str]] = []
        self._guard = threading.Lock()

    def _event(self, action: str, code: str) -> None:
        with self._guard:
            self.events.append((action, code))

    def snapshot(self, step, snapshot_dir: Path) -> None:
        self._event("snapshot", step.code)
        state = snapshot_dir / "profile.bin"
        marker = snapshot_dir / "exists"
        if self.profile.exists():
            shutil.copyfile(self.profile, state)
            marker.write_text("1", encoding="ascii")
        else:
            marker.write_text("0", encoding="ascii")
        if step.code == self.interrupt_snapshot:
            raise KeyboardInterrupt("synthetic process interruption")

    def apply(self, step) -> None:
        self._event("apply", step.code)
        previous = self.profile.read_bytes() if self.profile.exists() else b""
        self.profile.parent.mkdir(parents=True, exist_ok=True)
        self.profile.write_bytes(previous + f"|{step.code}".encode())
        if self.delay:
            time.sleep(self.delay)
        if step.code == self.interrupt_apply:
            raise KeyboardInterrupt("synthetic process interruption")
        if step.code == self.fail_apply:
            raise RuntimeError("synthetic failure containing secret-value")

    def verify(self, step) -> None:
        self._event("verify", step.code)
        if step.code == self.fail_verify:
            raise ValueError("synthetic verification failure")
        assert self.profile.read_bytes().endswith(f"|{step.code}".encode())

    def rollback(self, step, snapshot_dir: Path) -> None:
        self._event("rollback", step.code)
        if step.code == self.fail_rollback:
            raise OSError("synthetic rollback failure")
        if (snapshot_dir / "exists").read_text(encoding="ascii") == "1":
            shutil.copyfile(snapshot_dir / "profile.bin", self.profile)
        else:
            self.profile.unlink(missing_ok=True)


class FinalizingProfileAdapter(DisposableProfileAdapter):
    def __init__(self, *args, fail_finalize: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_finalize = fail_finalize
        self.finalized = 0

    def finalize_rollback(self, plan, snapshots_root: Path) -> None:
        assert snapshots_root.is_dir()
        assert tuple(step.code for step in plan.steps)
        self.finalized += 1
        self._event("finalize", plan.steps[-1].code)
        if self.fail_finalize:
            raise RuntimeError("synthetic finalizer failure")


def _execute(engine: TransactionEngine, plan, adapter, transaction_id="test-1"):
    return engine.execute(
        plan,
        adapter,
        transaction_id=transaction_id,
        confirmed_plan_digest=public_plan_digest(plan),
    )


def _process_execute_transaction(
    root_value: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    plan = _plan()
    adapter = DisposableProfileAdapter(root / "profile.bin", delay=0.01)
    try:
        if not start.wait(timeout=5):
            raise TimeoutError("synthetic process start timeout")
        result = _execute(
            TransactionEngine(root / "transactions"),
            plan,
            adapter,
            transaction_id="shared-process",
        )
        results.put(("ok", result.status))
    except BaseException as error:  # pragma: no cover - reported in parent.
        results.put(("error", error.__class__.__name__))


def test_success_commits_and_second_execution_is_an_idempotent_read(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile" / "config.bin"
    profile.parent.mkdir()
    profile.write_bytes(b"original")
    adapter = DisposableProfileAdapter(profile)
    engine = TransactionEngine(tmp_path / "transactions")

    first = _execute(engine, plan, adapter)
    first_events = tuple(adapter.events)
    second = _execute(engine, plan, adapter)

    assert first.status == second.status == "committed"
    assert first.completed_steps == tuple(step.code for step in plan.steps)
    assert tuple(adapter.events) == first_events
    assert profile.read_bytes().startswith(b"original|review|runtime")


@pytest.mark.parametrize("failure_kind", ["apply", "verify"])
def test_failure_restores_disposable_profile_byte_for_byte(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    plan = _plan(HostKind.OPENCLAW)
    profile = tmp_path / "profile.bin"
    original = b"synthetic profile before install\x00\xff"
    profile.write_bytes(original)
    failing_step = "provider"
    adapter = DisposableProfileAdapter(
        profile,
        fail_apply=failing_step if failure_kind == "apply" else "",
        fail_verify=failing_step if failure_kind == "verify" else "",
    )
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionApplyError, match="rolled back"):
        _execute(engine, plan, adapter)

    result = engine.inspect("test-1")
    assert result.status == "rolled_back"
    assert profile.read_bytes() == original
    applied = [code for action, code in adapter.events if action == "apply"]
    rolled_back = [code for action, code in adapter.events if action == "rollback"]
    assert rolled_back == list(reversed(applied))
    journal = (tmp_path / "transactions" / "test-1" / "journal.json").read_text()
    assert "secret-value" not in journal
    assert '"failure_type":"RuntimeError"' in journal or '"failure_type":"ValueError"' in journal


def test_rollback_failure_is_explicit_and_never_success(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    adapter = DisposableProfileAdapter(
        profile,
        fail_verify="provider",
        fail_rollback="runtime",
    )
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionRecoveryError, match="manual recovery"):
        _execute(engine, plan, adapter)

    result = engine.inspect("test-1")
    assert result.status == "rollback_failed"
    assert result.rollback_failed_steps == ("runtime",)
    assert result.status != "committed"


def test_rollback_finalizer_runs_after_every_step_is_restored(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    adapter = FinalizingProfileAdapter(profile, fail_verify="provider")
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionApplyError):
        _execute(engine, plan, adapter)

    assert profile.read_bytes() == b"original"
    assert adapter.finalized == 1
    assert adapter.events[-1] == ("finalize", plan.steps[-1].code)


def test_rollback_finalizer_failure_is_explicit(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    adapter = FinalizingProfileAdapter(
        profile,
        fail_verify="provider",
        fail_finalize=True,
    )
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionRecoveryError):
        _execute(engine, plan, adapter)

    result = engine.inspect("test-1")
    assert result.status == "rollback_failed"
    assert result.rollback_failed_steps == (plan.steps[-1].code,)


@pytest.mark.parametrize("phase", ["snapshot", "apply"])
@pytest.mark.parametrize("step_index", range(len(_plan().steps)))
def test_interruption_at_each_boundary_recovers_only_by_reverse_rollback(
    tmp_path: Path,
    phase: str,
    step_index: int,
) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    original = b"original"
    profile.write_bytes(original)
    interrupted_step = plan.steps[step_index].code
    adapter = DisposableProfileAdapter(
        profile,
        interrupt_snapshot=interrupted_step if phase == "snapshot" else "",
        interrupt_apply=interrupted_step if phase == "apply" else "",
    )
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(KeyboardInterrupt):
        _execute(engine, plan, adapter)
    before_recovery = tuple(adapter.events)

    result = engine.recover(
        plan,
        adapter,
        transaction_id="test-1",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert profile.read_bytes() == original
    recovery_events = adapter.events[len(before_recovery):]
    if phase == "snapshot" and step_index == 0:
        assert recovery_events == []
    else:
        assert recovery_events
    assert all(action == "rollback" for action, _code in recovery_events)


def test_unconfirmed_or_changed_plan_has_no_side_effect(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    adapter = DisposableProfileAdapter(profile)
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(PlanConfirmationError):
        engine.execute(
            plan,
            adapter,
            transaction_id="test-1",
            confirmed_plan_digest="0" * 64,
        )
    assert adapter.events == []
    assert profile.read_bytes() == b"original"
    assert not (tmp_path / "transactions").exists()


def test_private_allowlisted_journal_and_snapshots(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    engine = TransactionEngine(tmp_path / "transactions")
    _execute(engine, plan, DisposableProfileAdapter(profile))

    transaction = tmp_path / "transactions" / "test-1"
    journal_path = transaction / "journal.json"
    journal = json.loads(journal_path.read_text())
    assert set(journal) == {
        "schema", "transaction_id", "plan_digest", "host", "status",
        "step_codes", "snapshotted_steps", "completed_steps",
        "rolled_back_steps", "active_step", "active_phase", "failure_type",
        "rollback_failed_steps",
    }
    assert "synthetic-model" not in journal_path.read_text()
    if os.name != "nt":
        assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
        for path in (transaction / "snapshots").rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            assert stat.S_IMODE(path.stat().st_mode) == expected


def test_symlink_transaction_root_is_refused_before_adapter_action(tmp_path: Path) -> None:
    plan = _plan()
    external = tmp_path / "external"
    external.mkdir()
    transaction_root = tmp_path / "transactions"
    transaction_root.symlink_to(external, target_is_directory=True)
    adapter = DisposableProfileAdapter(tmp_path / "profile.bin")

    with pytest.raises(ValueError, match="symbolic link"):
        _execute(TransactionEngine(transaction_root), plan, adapter)
    assert adapter.events == []
    assert not list(external.iterdir())


def test_two_owners_do_not_apply_the_same_transaction_twice(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    adapter = DisposableProfileAdapter(profile, delay=0.002)
    engine = TransactionEngine(tmp_path / "transactions")
    results = []
    errors = []

    def run() -> None:
        try:
            results.append(_execute(engine, plan, adapter, transaction_id="shared"))
        except Exception as error:  # pragma: no cover - assertion reports it.
            errors.append(error)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert [result.status for result in results] == ["committed", "committed"]
    applied = [event for event in adapter.events if event[0] == "apply"]
    assert len(applied) == len(plan.steps)


def test_two_processes_do_not_apply_the_same_transaction_twice(tmp_path: Path) -> None:
    plan = _plan()
    profile = tmp_path / "profile.bin"
    profile.write_bytes(b"original")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_execute_transaction,
            args=(str(tmp_path), start, results),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)

    assert outcomes == [("ok", "committed"), ("ok", "committed")]
    assert all(process.exitcode == 0 for process in processes)
    profile_text = profile.read_text(encoding="utf-8")
    for step in plan.steps:
        assert profile_text.count(f"|{step.code}") == 1


def test_malformed_journal_fails_closed(tmp_path: Path) -> None:
    plan = _plan()
    transaction = tmp_path / "transactions" / "test-1"
    transaction.mkdir(parents=True)
    (transaction / "journal.json").write_text('{"schema":"unexpected"}\n')
    adapter = DisposableProfileAdapter(tmp_path / "profile.bin")

    with pytest.raises(ValueError, match="unknown schema"):
        _execute(TransactionEngine(tmp_path / "transactions"), plan, adapter)
    assert adapter.events == []


@pytest.mark.skipif(os.name == 'nt', reason='Explicit POSIX simulation of the Win32 path limit')
def test_protect_snapshot_tree_keeps_permissions_and_link_refusal_beyond_max_path(tmp_path, monkeypatch):
    from daemons_companion.setup import transaction as module
    from daemons_companion.runtime import bootstrap
    from tests.companion_windows_io import WindowsPathGuard
    # The padding keeps the path beyond MAX_PATH however short the temporary root is.
    root = tmp_path / ('p' * 80) / 'snapshots'
    deep = root / ('a' * 80) / ('b' * 80) / 'gateway-worker.before'
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b'synthetic-private-snapshot')
    assert len(str(deep)) >= 260
    with monkeypatch.context() as context:
        guard = WindowsPathGuard(context, tmp_path)
        context.setattr(bootstrap, '_source_io_path', lambda p: p)
        with pytest.raises(FileNotFoundError):
            module._protect_snapshot_tree(root)
        guard.denied.clear()
        context.setattr(bootstrap, '_source_io_path', guard.io_path)
        module._protect_snapshot_tree(root)
        assert guard.extended and not guard.denied
    assert deep.read_bytes() == b'synthetic-private-snapshot'
    assert stat.S_IMODE(deep.stat().st_mode) == 0o600
    (root / 'foreign').symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='symbolic links'):
        module._protect_snapshot_tree(root)
