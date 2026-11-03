"""Bind a reviewed companion plan to its exact executable transaction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from daemons_companion.setup.plan import HostKind, PlanStep, SetupPlan
from daemons_companion.hermes.composition import HermesCompositionLayout
from daemons_companion.hermes.ready_composition import (
    build_dormant_hermes_ready_plan,
)
from daemons_companion.openclaw.ready_composition import (
    OpenClawReadyCompositionLayout,
    build_dormant_openclaw_ready_plan,
)
from daemons_companion.runtime.release import CompanionRuntimeLayout
from daemons_companion.setup.transaction import (
    TransactionEngine,
    TransactionResult,
    public_plan_digest,
)


PREPARED_INSTALLATION_SCHEMA = "daemon.companion-prepared-installation.v1"


@dataclass(frozen=True)
class CompanionInstallLayout:
    runtime: CompanionRuntimeLayout
    uv_executable: Path
    uv_sha256: str
    target_os: str
    hermes_home: Path | None = None
    openclaw_state: Path | None = None
    openclaw_api_port: int = 8000
    openclaw_gateway_port: int = 18789
    job_namespace: str | None = None


@dataclass(frozen=True)
class PreparedInstallation:
    plan: SetupPlan
    confirmation_digest: str

    def public_dict(self) -> dict[str, object]:
        return {
            "schema": PREPARED_INSTALLATION_SCHEMA,
            "plan": self.plan.to_public_dict(),
            "confirmation_digest": self.confirmation_digest,
        }


class InstallationAdapter(Protocol):
    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None: ...

    def apply(self, step: PlanStep) -> None: ...

    def verify(self, step: PlanStep) -> None: ...

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None: ...


def prepare_installation(
    reviewed: SetupPlan,
    layout: CompanionInstallLayout,
) -> PreparedInstallation:
    """Expand one reviewed plan into the exact existing host composition."""

    if reviewed.selected_host.host == HostKind.HERMES:
        if layout.hermes_home is None or layout.openclaw_state is not None:
            raise ValueError("Hermes installation layout is ambiguous.")
        technical = build_dormant_hermes_ready_plan(
            reviewed,
            HermesCompositionLayout(
                layout.runtime,
                layout.hermes_home,
                layout.uv_executable,
                layout.uv_sha256,
                layout.target_os,
                job_namespace=layout.job_namespace,
            ),
        )
    elif reviewed.selected_host.host == HostKind.OPENCLAW:
        if layout.openclaw_state is None or layout.hermes_home is not None:
            raise ValueError("OpenClaw installation layout is ambiguous.")
        technical = build_dormant_openclaw_ready_plan(
            reviewed,
            OpenClawReadyCompositionLayout(
                layout.runtime,
                layout.openclaw_state,
                layout.uv_executable,
                layout.uv_sha256,
                layout.target_os,
                layout.openclaw_api_port,
                layout.openclaw_gateway_port,
                job_namespace=layout.job_namespace,
            ),
        )
    else:  # pragma: no cover - HostKind protects construction.
        raise ValueError("Unsupported companion host.")
    return PreparedInstallation(technical, public_plan_digest(technical))


def execute_prepared_installation(
    prepared: PreparedInstallation,
    *,
    adapter: InstallationAdapter,
    transactions_root: Path,
    transaction_id: str,
    confirmed_digest: str,
) -> TransactionResult:
    """Execute only the exact technical plan that the facade confirmed."""

    if confirmed_digest != prepared.confirmation_digest:
        raise ValueError("Confirmed installation plan digest differs from the prepared plan.")
    if public_plan_digest(prepared.plan) != prepared.confirmation_digest:
        raise ValueError("Prepared installation plan changed after review.")
    return TransactionEngine(transactions_root).execute(
        prepared.plan,
        adapter,
        transaction_id=transaction_id,
        confirmed_plan_digest=confirmed_digest,
    )


__all__ = [
    "CompanionInstallLayout",
    "InstallationAdapter",
    "PREPARED_INSTALLATION_SCHEMA",
    "PreparedInstallation",
    "execute_prepared_installation",
    "prepare_installation",
]
