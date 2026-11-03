"""Transactional upgrade of an owned, committed Windows gateway launch only."""
from __future__ import annotations

import uuid

from daemons_companion.runtime.uv import PrivateUvRepair, private_uv_path
from daemons_companion.runtime.service_path_repair import service_path_repair

from daemons_companion.runtime.services import (
    _load_windows_task_capture, _windows_definition_signature, render_job_definition,
)
from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.runtime.installation_scope import _read
from daemons_companion.hermes.gateway import private_gateway_job
from daemons_companion.setup.transaction import TransactionEngine, public_plan_digest


def _signature(capture):
    return _windows_definition_signature(_load_windows_task_capture(capture)["definition"]) if capture else None


class _LaunchRepair:
    def __init__(self, job, scheduler, old_definition, new_definition):
        self.job, self.scheduler = job, scheduler
        self.old = _windows_definition_signature(old_definition)
        self.new = _windows_definition_signature(new_definition)
        self.definition = new_definition

    def snapshot(self, step, directory):
        before = self.scheduler.capture(self.job.code)
        if _signature(before) != self.old:
            raise RuntimeError("Private gateway changed before repair; preserved.")
        (directory / "gateway.before").write_bytes(before)

    def apply(self, step):
        if _signature(self.scheduler.capture(self.job.code)) != self.old:
            raise RuntimeError("Private gateway changed concurrently; preserved.")
        # Stop the old process before replacing its action. Task registration by
        # itself does not stop an already running interpreter.
        self.scheduler.restore(self.job.code, None)
        self.scheduler.install(self.job, self.definition)

    def verify(self, step):
        if not self.scheduler.matches(self.job, self.definition):
            raise RuntimeError("The repaired private gateway did not start.")

    def rollback(self, step, directory):
        saved = directory / "gateway.before"
        if saved.is_symlink() or saved.resolve() != saved or saved.stat().st_size > 131072:
            raise ValueError("Gateway repair snapshot is invalid.")
        before = saved.read_bytes()
        if _signature(before) != self.old:
            raise ValueError("Gateway repair snapshot differs from the previous owned job.")
        current = self.scheduler.capture(self.job.code)
        if _signature(current) not in (None, self.old, self.new):
            raise RuntimeError("Private gateway changed concurrently; recovery refused.")
        self.scheduler.restore(self.job.code, before)
        restored = self.scheduler.capture(self.job.code)
        if _signature(restored) != self.old or (
            _load_windows_task_capture(before)["running"]
            and not _load_windows_task_capture(restored)["running"]
        ):
            raise RuntimeError("Previous gateway could not be restored.")


class _RepairAdapter:
    def __init__(self, launch, uv):
        self.launch, self.uv = launch, uv

    def _owner(self, step):
        return self.uv if step.code == "host-dependency-installer" else self.launch

    def snapshot(self, step, directory):
        self._owner(step).snapshot(step, directory)

    def apply(self, step):
        self._owner(step).apply(step)

    def verify(self, step):
        self._owner(step).verify(step)

    def rollback(self, step, directory):
        self._owner(step).rollback(step, directory)


def repair_gateway_launch(layout, runtime, scheduler):
    """Caller holds setup-owner lock. No runtime, profile or memory file is edited."""
    if layout.target_os != "windows":
        return False
    scope = runtime.installation_scope
    scope.validate_home(runtime.companion_home)
    scope.validate_namespace(scheduler.job_namespace)
    if layout.attempt != scope.attempt or layout.profile != scope.profile:
        raise ValueError("Gateway repair must stay in its owned installation.")
    receipt = _read(scope.attempt / "daemon-setup.json")
    transaction_id = receipt.get("transaction_id", "")
    if (receipt.get("schema") != "daemon.private-setup.v1" or not isinstance(transaction_id, str)
            or len(transaction_id) != 32 or any(c not in "0123456789abcdef" for c in transaction_id)):
        raise ValueError("A committed private setup receipt is required for repair.")
    if TransactionEngine(scope.home / "transactions").inspect(transaction_id).status != "committed":
        raise ValueError("Only a committed private setup can be repaired.")
    job = private_gateway_job(layout, runtime, scheduler.job_namespace)
    old = render_job_definition(private_gateway_job(layout, runtime, scheduler.job_namespace, legacy=True), "windows")
    new = render_job_definition(job, "windows")
    if old == new:
        return False
    launch = service_path_repair(layout, runtime, scheduler,
        private_gateway_job(layout, runtime, scheduler.job_namespace, legacy=True), job)
    adapter = _RepairAdapter(launch, PrivateUvRepair(layout))
    plan = SetupPlan(HostProbe(HostKind.HERMES, True, True, str(layout.executable), str(layout.profile)),
                     "", "", {"kind": "unchanged"},
                     (PlanStep("host-dependency-installer", "Restore Hermes native dependency installer.",
                               (str(layout.profile / "bin"), layout.uv_sha256)),
                      PlanStep("gateway-launch-path", "Repair private service paths together.",
                               (str(scope.attempt), str(layout.profile / ".env"), "Owned gateway, worker and nightly tasks", job.executable)),))
    root = scope.attempt / "gateway-repairs"
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Gateway repair journal must remain private and direct.")
    engine = TransactionEngine(root)
    digest = public_plan_digest(plan)
    # An interrupted replacement is restored from its exact snapshot before a
    # fresh attempt. An unrelated/concurrently modified task is never replaced.
    for journal in sorted(root.glob("*/journal.json")):
        state = engine.inspect(journal.parent.name)
        if state.status not in {"committed", "rolled_back"}:
            engine.recover(plan, adapter, transaction_id=state.transaction_id, confirmed_plan_digest=digest)
    if not launch.needs_repair() and private_uv_path(layout).is_file():
        return False
    engine.execute(plan, adapter, transaction_id=uuid.uuid4().hex, confirmed_plan_digest=digest)
    return True
