"""Seven-owner dormant Hermes installation transaction for the companion app."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from daemons_companion.runtime.services import (
    BACKGROUND_SERVICES_STEP,
    BackgroundServicesLayout,
    CompanionBackgroundServicesAdapter,
    SchedulerBackend,
    background_services_step,
)
from daemons_companion.setup.plan import (
    HostKind,
    PlanStep,
    ProviderSelection,
    SetupPlan,
)
from daemons_companion.setup.credentials import CredentialStore
from daemons_companion.hermes.activation import (
    HERMES_ACTIVATION_STEP,
    HermesActivationAdapter,
    HermesActivationLayout,
    HermesControlBackend,
    ReadinessProbe,
    hermes_activation_step,
)
from daemons_companion.hermes.composition import (
    HERMES_COMPOSITION_STEP_CODES,
    HermesCompositionAdapter,
    HermesCompositionLayout,
    build_dormant_hermes_composition_plan,
)
from daemons_companion.hermes.profile import (
    HERMES_PROFILE_STEP,
    HermesProfileAdapter,
    HermesProfileLayout,
)
from daemons_companion.runtime.release import verify_current_release
from daemon_core.installation_profile import DURABLE_AFTER_TURN
from daemons_companion.runtime.environment import (
    CommandRunner,
    release_environment_interpreter,
    resolve_release_environment,
)


HERMES_READY_STEP_CODES = (
    *HERMES_COMPOSITION_STEP_CODES,
    BACKGROUND_SERVICES_STEP,
    HERMES_ACTIVATION_STEP,
)


def build_dormant_hermes_ready_plan(
    reviewed: SetupPlan,
    layout: HermesCompositionLayout,
) -> SetupPlan:
    """Extend the reviewed five-owner composition with services and readiness."""

    base = build_dormant_hermes_composition_plan(reviewed, layout)
    services = background_services_step(_services_layout(layout, "validated-at-run"))
    activation = hermes_activation_step(
        HermesActivationLayout(layout.runtime, layout.hermes_home)
    )
    return replace(base, steps=(*base.steps, services, activation))


class HermesReadyCompositionAdapter:
    """Route one exact seven-owner plan and coordinate its rollback finalizer."""

    def __init__(
        self,
        layout: HermesCompositionLayout,
        *,
        plan: SetupPlan,
        provider_selection: ProviderSelection,
        credential_store: CredentialStore | None,
        credential_value: str | None,
        user_id: str,
        scheduler: SchedulerBackend,
        control: HermesControlBackend,
        readiness_probe: ReadinessProbe | None = None,
        codex_available: bool = False,
        runner: CommandRunner | None = None,
        recovery_only: bool = False,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        _validate_ready_plan(plan, layout, provider_selection, codex_available)
        if scheduler.target_os != layout.target_os:
            raise ValueError("Hermes scheduler differs from the confirmed target OS.")
        self.layout = layout
        self.plan = plan
        self.user_id = user_id
        self.scheduler = scheduler
        self.control = control
        self.readiness_probe = readiness_probe
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._delegates: dict[str, object] = {}

        base_plan = replace(plan, steps=plan.steps[: len(HERMES_COMPOSITION_STEP_CODES)])
        self._base = HermesCompositionAdapter(
            layout,
            plan=base_plan,
            provider_selection=provider_selection,
            credential_store=credential_store,
            credential_value=credential_value,
            user_id=user_id,
            codex_available=codex_available,
            runner=runner,
            recovery_only=recovery_only,
            mutation_hook=self._mutation_hook,
        )

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate()
        self._step(step).snapshot(step, snapshot_dir)

    def apply(self, step: PlanStep) -> None:
        delegate = self._step(step)
        delegate.apply(step)
        if step.code == HERMES_PROFILE_STEP:
            self._mutation_hook(step.code)

    def verify(self, step: PlanStep) -> None:
        delegate = self._step(step)
        if step.code == HERMES_ACTIVATION_STEP:
            delegate.progress = getattr(self, "progress", lambda _message: None)
        delegate.verify(step)

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._step(step).rollback(step, snapshot_dir)

    def finalize_rollback(self, plan: SetupPlan, snapshots_root: Path) -> None:
        if plan != self.plan:
            raise ValueError("Hermes rollback finalizer received another plan.")
        snapshot = snapshots_root / HERMES_ACTIVATION_STEP
        if not snapshot.joinpath("manifest.json").is_file():
            return
        activation = self._activation_delegate()
        activation.restore_gateway_after_rollback(snapshot)

    def _validate(self) -> None:
        _validate_ready_plan(
            self.plan,
            self.layout,
            self._base.provider_selection,
            self._base.codex_available,
        )

    def _step(self, step: PlanStep):
        if step.code not in HERMES_READY_STEP_CODES:
            raise ValueError("Hermes ready composition received an unsupported step.")
        if step.code in HERMES_COMPOSITION_STEP_CODES and step.code != HERMES_PROFILE_STEP:
            return self._base
        existing = self._delegates.get(step.code)
        if existing is not None:
            return existing
        if step.code == HERMES_PROFILE_STEP:
            delegate = self._profile_delegate()
        elif step.code == BACKGROUND_SERVICES_STEP:
            delegate = CompanionBackgroundServicesAdapter(
                _services_layout(self.layout, self.user_id),
                backend=self.scheduler,
                mutation_hook=self._mutation_hook,
            )
        elif step.code == HERMES_ACTIVATION_STEP:
            delegate = self._activation_delegate()
        else:  # pragma: no cover - step-code guard protects this branch.
            raise ValueError("Hermes ready composition received an unsupported step.")
        self._delegates[step.code] = delegate
        return delegate

    def _profile_delegate(self) -> HermesProfileAdapter:
        release = verify_current_release(self.layout.runtime)
        environment = resolve_release_environment(self.layout.runtime_environment)
        interpreter = release_environment_interpreter(environment, self.layout.target_os)
        state = self.layout.runtime.state_root
        return HermesProfileAdapter(
            HermesProfileLayout(self.layout.hermes_home, release, self.layout.runtime.installation_scope,
                                plugin_copies=self.layout.target_os == "windows"),
            selected_host=HostKind.HERMES,
            user_id=self.user_id,
            runtime_environment={
                "DAEMON_CORE_MEMORY_ROOT": str(self.layout.runtime.memory_root),
                "DAEMON_CORE_STATE_ROOT": str(state),
                "DAEMON_CORE_PROVIDER_CONFIG": str(
                    state / "config" / "provider.json"
                ),
                "DAEMON_CORE_PYTHON": str(interpreter),
                **DURABLE_AFTER_TURN,
                "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(
                    state / "after-turn-spool"
                ),
                "DAEMON_CORE_LATENCY_LOG": str(
                    state / "latency" / "hermes.jsonl"
                ),
            },
        )

    def _activation_delegate(self) -> HermesActivationAdapter:
        existing = self._delegates.get(HERMES_ACTIVATION_STEP)
        if existing is not None:
            return existing  # type: ignore[return-value]
        probe = self.readiness_probe
        scope = self.layout.runtime.installation_scope
        if probe is None and scope is not None:
            from functools import partial
            from daemons_companion.hermes.activation import _readiness_probe
            from daemons_companion.runtime.private_environment import private_process_environment
            from daemon_core.installation_profile import profile_status
            expected = profile_status(private_process_environment(scope), user_ids=(self.user_id,),
                                      memory_root=self.layout.runtime.memory_root)
            if self.layout.runtime.root_folder_memory and (expected is None or not expected["ok"]):
                raise ValueError("The new PARGA installation requires its complete continuity profile.")
            probe = partial(_readiness_probe, expected_profile=expected)
        delegate = HermesActivationAdapter(
            HermesActivationLayout(self.layout.runtime, self.layout.hermes_home),
            control=self.control,
            readiness_probe=probe,
            readiness_attempts=480 if self.layout.runtime.installation_scope else 40,
            mutation_hook=self._mutation_hook,
        )
        self._delegates[HERMES_ACTIVATION_STEP] = delegate
        return delegate


def _services_layout(
    layout: HermesCompositionLayout,
    user_id: str,
) -> BackgroundServicesLayout:
    return BackgroundServicesLayout(
        layout.runtime,
        layout.runtime_environment,
        layout.target_os,
        user_id,
        job_namespace=layout.job_namespace,
    )


def _validate_ready_plan(
    plan: SetupPlan,
    layout: HermesCompositionLayout,
    selection: ProviderSelection,
    codex_available: bool,
) -> None:
    if plan.selected_host.host != HostKind.HERMES:
        raise ValueError("Hermes ready composition requires the Hermes host.")
    if tuple(step.code for step in plan.steps) != HERMES_READY_STEP_CODES:
        raise ValueError("Hermes ready composition is incomplete or out of order.")
    if plan.steps[-2] != background_services_step(
        _services_layout(layout, "validated-at-run")
    ):
        raise ValueError("Hermes background-service step differs from the canonical plan.")
    if plan.steps[-1] != hermes_activation_step(
        HermesActivationLayout(layout.runtime, layout.hermes_home)
    ):
        raise ValueError("Hermes activation step differs from the canonical plan.")
    base_plan = replace(plan, steps=plan.steps[: len(HERMES_COMPOSITION_STEP_CODES)])
    HermesCompositionAdapter(
        layout,
        plan=base_plan,
        provider_selection=selection,
        credential_store=None,
        credential_value=None,
        user_id="validation",
        codex_available=codex_available,
        recovery_only=True,
    )


__all__ = [
    "HERMES_READY_STEP_CODES",
    "HermesReadyCompositionAdapter",
    "build_dormant_hermes_ready_plan",
]
