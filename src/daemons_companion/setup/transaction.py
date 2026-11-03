"""Transactional owner for companion operations, independent of host policy."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator, Protocol

from daemons_companion.setup.plan import PlanStep, SetupPlan
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock


TRANSACTION_SCHEMA = "daemon.companion-transaction.v1"
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STEP_CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ACTIVE_STATUSES = frozenset({"prepared", "applying", "rolling_back"})
_FINAL_STATUSES = frozenset({"committed", "rolled_back", "rollback_failed"})
_JOURNAL_KEYS = frozenset({
    "schema",
    "transaction_id",
    "plan_digest",
    "host",
    "status",
    "step_codes",
    "snapshotted_steps",
    "completed_steps",
    "rolled_back_steps",
    "active_step",
    "active_phase",
    "failure_type",
    "rollback_failed_steps",
})

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class TransactionAdapter(Protocol):
    """Host-owned operations. Methods must be safe to retry on the same state."""

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None: ...

    def apply(self, step: PlanStep) -> None: ...

    def verify(self, step: PlanStep) -> None: ...

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None: ...


class TransactionError(RuntimeError):
    pass


class PlanConfirmationError(TransactionError):
    pass


class TransactionApplyError(TransactionError):
    pass


class TransactionRecoveryError(TransactionError):
    pass


@dataclass(frozen=True)
class TransactionResult:
    transaction_id: str
    plan_digest: str
    status: str
    completed_steps: tuple[str, ...]
    rolled_back_steps: tuple[str, ...]
    rollback_failed_steps: tuple[str, ...]


def public_plan_digest(plan: SetupPlan) -> str:
    payload = json.dumps(
        plan.to_public_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TransactionEngine:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    def execute(
        self,
        plan: SetupPlan,
        adapter: TransactionAdapter,
        *,
        transaction_id: str,
        confirmed_plan_digest: str,
    ) -> TransactionResult:
        digest = self._validate_confirmation(
            plan,
            transaction_id=transaction_id,
            confirmed_plan_digest=confirmed_plan_digest,
        )
        paths = self._paths(transaction_id)
        with _transaction_lock(paths.journal):
            self._prepare_paths(paths)
            if paths.journal.exists():
                journal = self._load_journal(paths.journal)
                self._match_existing(journal, plan, digest)
                if journal["status"] == "committed":
                    return _result(journal)
                if journal["status"] in _ACTIVE_STATUSES:
                    self._rollback(plan, adapter, paths, journal)
                    raise TransactionRecoveryError(
                        "An interrupted transaction was rolled back; review and confirm a new transaction."
                    )
                raise TransactionError(
                    f"Transaction is already final with status {journal['status']}."
                )

            journal = _new_journal(plan, transaction_id, digest)
            self._write_journal(paths.journal, journal)
            journal["status"] = "applying"
            self._write_journal(paths.journal, journal)
            step_by_code = {step.code: step for step in plan.steps}
            try:
                for step in plan.steps:
                    self._validate_step(step)
                    journal["active_step"] = step.code
                    journal["active_phase"] = "snapshot"
                    self._write_journal(paths.journal, journal)
                    snapshot_dir = paths.snapshots / step.code
                    self._prepare_private_dir(snapshot_dir)
                    adapter.snapshot(step, _snapshot_io_path(snapshot_dir))
                    _protect_snapshot_tree(snapshot_dir)
                    journal["snapshotted_steps"].append(step.code)
                    journal["active_phase"] = "apply"
                    self._write_journal(paths.journal, journal)
                    adapter.apply(step)
                    journal["active_phase"] = "verify"
                    self._write_journal(paths.journal, journal)
                    adapter.verify(step)
                    journal["completed_steps"].append(step.code)
                    journal["active_step"] = ""
                    journal["active_phase"] = ""
                    self._write_journal(paths.journal, journal)
            except Exception as error:
                journal["failure_type"] = error.__class__.__name__[:120]
                self._write_journal(paths.journal, journal)
                self._rollback(plan, adapter, paths, journal)
                if journal["status"] == "rollback_failed":
                    raise TransactionRecoveryError(
                        "Installation failed and rollback needs manual recovery."
                    ) from error
                raise TransactionApplyError(
                    "Installation failed and the disposable profile was rolled back."
                ) from error

            # Reject adapters that somehow changed the declared step set.
            if set(journal["completed_steps"]) != set(step_by_code):
                raise TransactionApplyError("Transaction did not complete its declared steps.")
            journal["status"] = "committed"
            self._write_journal(paths.journal, journal)
            return _result(journal)

    def recover(
        self,
        plan: SetupPlan,
        adapter: TransactionAdapter,
        *,
        transaction_id: str,
        confirmed_plan_digest: str,
    ) -> TransactionResult:
        digest = self._validate_confirmation(
            plan,
            transaction_id=transaction_id,
            confirmed_plan_digest=confirmed_plan_digest,
        )
        paths = self._paths(transaction_id)
        with _transaction_lock(paths.journal):
            self._validate_existing_paths(paths)
            if not paths.journal.exists():
                raise TransactionError("Transaction journal does not exist.")
            journal = self._load_journal(paths.journal)
            self._match_existing(journal, plan, digest)
            if journal["status"] in _FINAL_STATUSES:
                return _result(journal)
            self._rollback(plan, adapter, paths, journal)
            return _result(journal)

    def inspect(self, transaction_id: str) -> TransactionResult:
        paths = self._paths(transaction_id)
        with _transaction_lock(paths.journal):
            self._validate_existing_paths(paths)
            if not paths.journal.exists():
                raise TransactionError("Transaction journal does not exist.")
            return _result(self._load_journal(paths.journal))

    def _rollback(
        self,
        plan: SetupPlan,
        adapter: TransactionAdapter,
        paths: "_TransactionPaths",
        journal: dict[str, object],
    ) -> None:
        journal["status"] = "rolling_back"
        self._write_journal(paths.journal, journal)
        by_code = {step.code: step for step in plan.steps}
        already = set(journal["rolled_back_steps"])
        pending = [
            code for code in reversed(journal["snapshotted_steps"])
            if code not in already
        ]
        failures: list[str] = list(journal["rollback_failed_steps"])
        for code in pending:
            try:
                adapter.rollback(by_code[code], _snapshot_io_path(paths.snapshots / code))
                if code not in journal["rolled_back_steps"]:
                    journal["rolled_back_steps"].append(code)
            except Exception:
                if code not in failures:
                    failures.append(code)
            journal["rollback_failed_steps"] = failures
            self._write_journal(paths.journal, journal)
        finalizer = getattr(adapter, "finalize_rollback", None)
        if callable(finalizer):
            try:
                finalizer(plan, paths.snapshots)
            except Exception:
                owner = plan.steps[-1].code
                if owner not in failures:
                    failures.append(owner)
                journal["rollback_failed_steps"] = failures
                self._write_journal(paths.journal, journal)
        journal["active_step"] = ""
        journal["active_phase"] = ""
        journal["status"] = "rollback_failed" if failures else "rolled_back"
        self._write_journal(paths.journal, journal)

    def _validate_confirmation(
        self,
        plan: SetupPlan,
        *,
        transaction_id: str,
        confirmed_plan_digest: str,
    ) -> str:
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise ValueError("Transaction id is invalid.")
        digest = public_plan_digest(plan)
        if confirmed_plan_digest != digest:
            raise PlanConfirmationError("The confirmed plan does not match this plan.")
        if not plan.requires_confirmation:
            raise PlanConfirmationError("The plan must require explicit confirmation.")
        step_codes = [step.code for step in plan.steps]
        if len(step_codes) != len(set(step_codes)):
            raise ValueError("Plan step codes must be unique.")
        for step in plan.steps:
            self._validate_step(step)
        return digest

    def _match_existing(
        self,
        journal: dict[str, object],
        plan: SetupPlan,
        digest: str,
    ) -> None:
        if journal["plan_digest"] != digest:
            raise PlanConfirmationError("Transaction belongs to a different plan.")
        if journal["host"] != plan.selected_host.host.value:
            raise PlanConfirmationError("Transaction belongs to a different host.")
        if journal["step_codes"] != [step.code for step in plan.steps]:
            raise PlanConfirmationError("Transaction step set differs from the plan.")

    def _paths(self, transaction_id: str) -> "_TransactionPaths":
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise ValueError("Transaction id is invalid.")
        transaction_dir = self.root / transaction_id
        return _TransactionPaths(
            transaction_dir=transaction_dir,
            journal=transaction_dir / "journal.json",
            snapshots=transaction_dir / "snapshots",
        )

    def _prepare_paths(self, paths: "_TransactionPaths") -> None:
        self._prepare_private_dir(self.root)
        self._prepare_private_dir(paths.transaction_dir)
        self._prepare_private_dir(paths.snapshots)
        if paths.journal.is_symlink():
            raise ValueError("Transaction journal must not be a symbolic link.")

    def _validate_existing_paths(self, paths: "_TransactionPaths") -> None:
        for path in (self.root, paths.transaction_dir, paths.snapshots):
            if path.is_symlink():
                raise ValueError("Transaction directories must not be symbolic links.")
        if paths.journal.is_symlink():
            raise ValueError("Transaction journal must not be a symbolic link.")

    @staticmethod
    def _prepare_private_dir(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("Transaction directories must not be symbolic links.")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise PermissionError("Transaction directory is not private.")

    @staticmethod
    def _validate_step(step: PlanStep) -> None:
        if not _STEP_CODE.fullmatch(step.code):
            raise ValueError(f"Invalid transaction step code: {step.code!r}")

    @staticmethod
    def _write_journal(path: Path, journal: dict[str, object]) -> None:
        _validate_journal(journal)
        atomic_write_text(
            path,
            json.dumps(journal, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n",
        )
        path.chmod(0o600)
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise PermissionError("Transaction journal is not private.")

    @staticmethod
    def _load_journal(path: Path) -> dict[str, object]:
        if path.is_symlink():
            raise ValueError("Transaction journal must not be a symbolic link.")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        _validate_journal(loaded)
        return loaded


@dataclass(frozen=True)
class _TransactionPaths:
    transaction_dir: Path
    journal: Path
    snapshots: Path


def _new_journal(plan: SetupPlan, transaction_id: str, digest: str) -> dict[str, object]:
    return {
        "schema": TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "plan_digest": digest,
        "host": plan.selected_host.host.value,
        "status": "prepared",
        "step_codes": [step.code for step in plan.steps],
        "snapshotted_steps": [],
        "completed_steps": [],
        "rolled_back_steps": [],
        "active_step": "",
        "active_phase": "",
        "failure_type": "",
        "rollback_failed_steps": [],
    }


def _validate_journal(journal: object) -> None:
    if not isinstance(journal, dict) or set(journal) != _JOURNAL_KEYS:
        raise ValueError("Transaction journal has an unknown schema.")
    if journal.get("schema") != TRANSACTION_SCHEMA:
        raise ValueError("Transaction journal version is unsupported.")
    if not _TRANSACTION_ID.fullmatch(str(journal.get("transaction_id", ""))):
        raise ValueError("Transaction journal id is invalid.")
    digest = journal.get("plan_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("Transaction journal plan digest is invalid.")
    if journal.get("host") not in {"hermes", "openclaw"}:
        raise ValueError("Transaction journal host is invalid.")
    if journal.get("status") not in _ACTIVE_STATUSES | _FINAL_STATUSES:
        raise ValueError("Transaction journal status is invalid.")
    if journal.get("active_phase") not in {"", "snapshot", "apply", "verify"}:
        raise ValueError("Transaction journal phase is invalid.")
    for key in (
        "step_codes",
        "snapshotted_steps",
        "completed_steps",
        "rolled_back_steps",
        "rollback_failed_steps",
    ):
        value = journal.get(key)
        if not isinstance(value, list) or any(
            not isinstance(code, str) or not _STEP_CODE.fullmatch(code)
            for code in value
        ):
            raise ValueError(f"Transaction journal field {key} is invalid.")
        if len(value) != len(set(value)):
            raise ValueError(f"Transaction journal field {key} contains duplicates.")
    declared = journal["step_codes"]
    if len(declared) != len(set(declared)):
        raise ValueError("Transaction journal contains duplicate steps.")
    for key in (
        "snapshotted_steps",
        "completed_steps",
        "rolled_back_steps",
        "rollback_failed_steps",
    ):
        if not set(journal[key]).issubset(set(declared)):
            raise ValueError(f"Transaction journal field {key} contains an unknown step.")
    active = journal.get("active_step")
    if not isinstance(active, str) or (active and active not in declared):
        raise ValueError("Transaction journal active step is invalid.")
    if not isinstance(journal.get("failure_type"), str):
        raise ValueError("Transaction journal failure type is invalid.")
    if len(journal["failure_type"]) > 120:
        raise ValueError("Transaction journal failure type is too long.")
    if journal["active_phase"] and not active:
        raise ValueError("Transaction journal phase has no active step.")
    if journal["status"] in _FINAL_STATUSES and (active or journal["active_phase"]):
        raise ValueError("Final transaction journal still has an active step.")
    if not set(journal["completed_steps"]).issubset(set(journal["snapshotted_steps"])):
        raise ValueError("Completed transaction steps have no snapshot.")
    if not set(journal["rolled_back_steps"]).issubset(set(journal["snapshotted_steps"])):
        raise ValueError("Rolled-back transaction steps have no snapshot.")


def _snapshot_io_path(path: Path) -> Path:
    """Snapshot folders sit deep in an installation; on Windows every step
    writes its records there through the extended namespace, beyond MAX_PATH."""
    from daemons_companion.runtime.bootstrap import _source_io_path
    return _source_io_path(path)


def _protect_snapshot_tree(root: Path) -> None:
    from daemons_companion.runtime.bootstrap import _source_io_path
    root = _source_io_path(root)
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Transaction snapshots must not contain symbolic links.")
        path.chmod(0o700 if path.is_dir() else 0o600)


def _result(journal: dict[str, object]) -> TransactionResult:
    return TransactionResult(
        transaction_id=str(journal["transaction_id"]),
        plan_digest=str(journal["plan_digest"]),
        status=str(journal["status"]),
        completed_steps=tuple(journal["completed_steps"]),
        rolled_back_steps=tuple(journal["rolled_back_steps"]),
        rollback_failed_steps=tuple(journal["rollback_failed_steps"]),
    )


@contextmanager
def _transaction_lock(journal_path: Path) -> Iterator[None]:
    key = str(journal_path.resolve())
    with _LOCAL_LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    with local:
        with memory_file_lock(journal_path):
            yield


__all__ = [
    "PlanConfirmationError",
    "TransactionAdapter",
    "TransactionApplyError",
    "TransactionEngine",
    "TransactionError",
    "TransactionRecoveryError",
    "TransactionResult",
    "public_plan_digest",
]
