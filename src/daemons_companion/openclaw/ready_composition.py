"""Eight-owner disposable OpenClaw installation transaction."""

from __future__ import annotations

from dataclasses import dataclass
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
from daemons_companion.desktop.identity import (
    IDENTITY_STEP,
    CompanionIdentityAdapter,
    CompanionIdentityLayout,
)
from daemons_companion.openclaw.activation import (
    OPENCLAW_ACTIVATION_STEP,
    ApiProbe,
    OpenClawActivationAdapter,
    OpenClawActivationLayout,
    OpenClawControlBackend,
    ReadinessProbe,
    openclaw_activation_step,
)
from daemons_companion.openclaw.api_service import (
    OPENCLAW_API_SERVICE_STEP,
    CompanionOpenClawApiServiceAdapter,
    OpenClawApiServiceLayout,
    openclaw_api_service_step,
)
from daemons_companion.openclaw.profile import (
    OPENCLAW_PROFILE_STEP,
    OpenClawProfileAdapter,
    OpenClawProfileLayout,
)
from daemons_companion.setup.provider import (
    PROVIDER_STATE_STEP,
    CompanionProviderAdapter,
    CompanionProviderLayout,
)
from daemons_companion.runtime.release import (
    RUNTIME_LAYOUT_STEP,
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    runtime_bundle_digest,
)
from daemons_companion.runtime.environment import (
    RUNTIME_ENVIRONMENT_STEP,
    CommandRunner,
    CompanionRuntimeEnvironmentAdapter,
    CompanionRuntimeEnvironmentLayout,
)


_PUBLIC_OPENCLAW_STEP_CODES = (
    "review",
    "runtime",
    "identity",
    "provider",
    "openclaw-plugin",
    "verify",
)
OPENCLAW_READY_STEP_CODES = (
    RUNTIME_LAYOUT_STEP,
    RUNTIME_ENVIRONMENT_STEP,
    IDENTITY_STEP,
    PROVIDER_STATE_STEP,
    OPENCLAW_PROFILE_STEP,
    BACKGROUND_SERVICES_STEP,
    OPENCLAW_API_SERVICE_STEP,
    OPENCLAW_ACTIVATION_STEP,
)


@dataclass(frozen=True)
class OpenClawReadyCompositionLayout:
    runtime: CompanionRuntimeLayout
    openclaw_state: Path
    uv_executable: Path
    uv_sha256: str
    target_os: str
    api_port: int = 8000
    gateway_port: int = 18789
    job_namespace: str | None = None

    @property
    def runtime_environment(self) -> CompanionRuntimeEnvironmentLayout:
        return CompanionRuntimeEnvironmentLayout(
            runtime=self.runtime,
            uv_executable=self.uv_executable,
            uv_sha256=self.uv_sha256,
            target_os=self.target_os,
        )


def build_dormant_openclaw_ready_plan(
    reviewed: SetupPlan,
    layout: OpenClawReadyCompositionLayout,
) -> SetupPlan:
    """Expand the exact public plan into its eight technical owners."""

    if reviewed.selected_host.host != HostKind.OPENCLAW:
        raise ValueError("OpenClaw composition requires a reviewed OpenClaw plan.")
    if tuple(step.code for step in reviewed.steps) != _PUBLIC_OPENCLAW_STEP_CODES:
        raise ValueError("Reviewed OpenClaw plan does not match the canonical public plan.")
    if reviewed.selected_host.config_path != str(layout.openclaw_state):
        raise ValueError("Reviewed OpenClaw profile differs from the composition target.")
    return SetupPlan(
        selected_host=reviewed.selected_host,
        daemon_name=reviewed.daemon_name,
        daemon_gender=reviewed.daemon_gender,
        user_preferences=reviewed.user_preferences,
        provider=reviewed.provider,
        steps=_composition_steps(layout),
        installs_host=False,
        requires_confirmation=True,
    )


class OpenClawReadyCompositionAdapter:
    """Route one exact OpenClaw plan and coordinate its rollback finalizer."""

    def __init__(
        self,
        layout: OpenClawReadyCompositionLayout,
        *,
        plan: SetupPlan,
        provider_selection: ProviderSelection,
        credential_store: CredentialStore | None,
        credential_value: str | None,
        user_id: str,
        scheduler: SchedulerBackend,
        control: OpenClawControlBackend,
        readiness_probe: ReadinessProbe | None = None,
        api_probe: ApiProbe | None = None,
        codex_available: bool = False,
        runner: CommandRunner | None = None,
        recovery_only: bool = False,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        _validate_composition_plan(
            plan,
            layout,
            provider_selection,
            codex_available,
        )
        if scheduler.target_os != layout.target_os:
            raise ValueError("OpenClaw scheduler differs from the confirmed target OS.")
        if control.openclaw_state != layout.openclaw_state:
            raise ValueError("OpenClaw control backend targets another profile.")
        self.layout = layout
        self.plan = plan
        self.provider_selection = provider_selection
        self.credential_store = credential_store
        self.credential_value = credential_value
        self.user_id = user_id
        self.scheduler = scheduler
        self.control = control
        self.readiness_probe = readiness_probe
        self.api_probe = api_probe
        self.codex_available = codex_available
        self.runner = runner
        self.recovery_only = recovery_only
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._delegates: dict[str, object] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate()
        self._step(step).snapshot(step, snapshot_dir)

    def apply(self, step: PlanStep) -> None:
        self._step(step).apply(step)
        if step.code in {
            RUNTIME_LAYOUT_STEP,
            RUNTIME_ENVIRONMENT_STEP,
            IDENTITY_STEP,
            PROVIDER_STATE_STEP,
            OPENCLAW_PROFILE_STEP,
        }:
            self._mutation_hook(step.code)

    def verify(self, step: PlanStep) -> None:
        self._step(step).verify(step)

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._step(step).rollback(step, snapshot_dir)

    def finalize_rollback(self, plan: SetupPlan, snapshots_root: Path) -> None:
        if plan != self.plan:
            raise ValueError("OpenClaw rollback finalizer received another plan.")
        snapshot = snapshots_root / OPENCLAW_ACTIVATION_STEP
        if not snapshot.joinpath("manifest.json").is_file():
            return
        self._activation_delegate().restore_gateway_after_rollback(snapshot)

    def _validate(self) -> None:
        _validate_composition_plan(
            self.plan,
            self.layout,
            self.provider_selection,
            self.codex_available,
        )

    def _step(self, step: PlanStep):
        if step.code not in OPENCLAW_READY_STEP_CODES:
            raise ValueError("OpenClaw composition received an unsupported step.")
        existing = self._delegates.get(step.code)
        if existing is not None:
            return existing
        delegate = self._build_delegate(step.code)
        self._delegates[step.code] = delegate
        return delegate

    def _build_delegate(self, code: str):
        if code == RUNTIME_LAYOUT_STEP:
            return CompanionRuntimeAdapter(self.layout.runtime)
        if code == RUNTIME_ENVIRONMENT_STEP:
            return CompanionRuntimeEnvironmentAdapter(
                self.layout.runtime_environment,
                runner=self.runner,
            )
        if code == IDENTITY_STEP:
            return CompanionIdentityAdapter(
                CompanionIdentityLayout(self.layout.runtime.companion_home, self.layout.runtime.installation_scope,
                                        root_folder_memory=self.layout.runtime.root_folder_memory),
                name=self.plan.daemon_name,
                gender=self.plan.daemon_gender,
                user=self.plan.user_preferences.for_user(self.user_id, "openclaw") if self.plan.user_preferences else None,
            )
        if code == PROVIDER_STATE_STEP:
            provider_layout = CompanionProviderLayout(
                self.layout.runtime.companion_home, self.layout.runtime.installation_scope
            )
            if self.recovery_only and _selection_requires_store(
                self.provider_selection
            ):
                if self.credential_store is None:
                    raise ValueError(
                        "Provider recovery requires its credential store."
                    )
                return CompanionProviderAdapter.for_recovery(
                    provider_layout,
                    selection=self.provider_selection,
                    credential_store=self.credential_store,
                    codex_available=self.codex_available,
                )
            return CompanionProviderAdapter(
                provider_layout,
                selection=self.provider_selection,
                credential_store=self.credential_store,
                credential_value=self.credential_value,
                codex_available=self.codex_available,
            )
        if code == OPENCLAW_PROFILE_STEP:
            adapter = OpenClawProfileAdapter
            if self.layout.runtime.installation_scope is not None:
                from daemons_companion.openclaw.first_profile import FirstProfileAdapter
                adapter = FirstProfileAdapter
            return adapter(
                OpenClawProfileLayout(
                    self.layout.runtime,
                    self.layout.openclaw_state,
                ),
                selected_host=HostKind.OPENCLAW,
                user_id=self.user_id,
                daemon_url=f"http://127.0.0.1:{self.layout.api_port}",
            )
        if code == BACKGROUND_SERVICES_STEP:
            return CompanionBackgroundServicesAdapter(
                _services_layout(self.layout, self.user_id),
                backend=self.scheduler,
                mutation_hook=self._mutation_hook,
            )
        if code == OPENCLAW_API_SERVICE_STEP:
            return CompanionOpenClawApiServiceAdapter(
                _api_layout(self.layout, self.user_id),
                backend=self.scheduler,
                mutation_hook=self._mutation_hook,
            )
        if code == OPENCLAW_ACTIVATION_STEP:
            return self._activation_delegate()
        raise ValueError("OpenClaw composition received an unsupported step.")

    def _activation_delegate(self) -> OpenClawActivationAdapter:
        existing = self._delegates.get(OPENCLAW_ACTIVATION_STEP)
        if existing is not None:
            return existing  # type: ignore[return-value]
        probe = self.readiness_probe
        api_probe = self.api_probe
        scope = self.layout.runtime.installation_scope
        if scope is not None:
            from functools import partial
            from daemons_companion.hermes.activation import _readiness_probe
            from daemons_companion.runtime.private_environment import private_process_environment
            from daemon_core.installation_profile import profile_status
            expected = profile_status(private_process_environment(scope), user_ids=(self.user_id,), memory_root=self.layout.runtime.memory_root)
            if self.layout.runtime.root_folder_memory and (expected is None or not expected['ok']):
                raise ValueError('The PARGA installation requires its complete continuity profile.')
            probe = probe or partial(_readiness_probe, expected_profile=expected)
            if api_probe is None:
                from daemons_companion.openclaw.gateway import api_ready
                api_probe = partial(api_ready, runtime=self.layout.runtime)
        delegate = OpenClawActivationAdapter(
            _activation_layout(self.layout),
            control=self.control,
            readiness_probe=probe,
            api_probe=api_probe,
            mutation_hook=self._mutation_hook,
            readiness_attempts=120 if self.layout.runtime.installation_scope else 40,
        )
        self._delegates[OPENCLAW_ACTIVATION_STEP] = delegate
        return delegate


def _composition_steps(
    layout: OpenClawReadyCompositionLayout,
) -> tuple[PlanStep, ...]:
    return (
        PlanStep(
            RUNTIME_LAYOUT_STEP,
            "Materialize the immutable private Daemon Harness release.",
            (
                f"Companion home: {layout.runtime.companion_home}",
                f"Bundle SHA-256: {runtime_bundle_digest(layout.runtime.bundle_root)}",
                "Shared memory and state",
            ),
        ),
        PlanStep(
            RUNTIME_ENVIRONMENT_STEP,
            "Build the release-owned managed Python environment.",
            (
                f"Target OS: {layout.target_os}",
                f"uv SHA-256: {layout.uv_sha256}",
                "Managed Python 3.12 and hash-locked dependencies",
            ),
        ),
        PlanStep(
            IDENTITY_STEP,
            "Store name and grammatical agreement outside the canonical voice.",
            ("daemon_identity.json", "No persona change", "No conversation change"),
        ),
        PlanStep(
            PROVIDER_STATE_STEP,
            "Store non-secret provider settings and a protected credential reference.",
            ("One model", "Protected credential", "No secret in environment"),
        ),
        PlanStep(
            OPENCLAW_PROFILE_STEP,
            "Point the disposable OpenClaw profile at the private runtime.",
            (
                f"OpenClaw profile: {layout.openclaw_state}",
                f"Daemon API: 127.0.0.1:{layout.api_port}",
                "Context engine, minimal workspace and memory tools",
            ),
        ),
        background_services_step(_services_layout(layout, "validated-at-run")),
        openclaw_api_service_step(_api_layout(layout, "validated-at-run")),
        openclaw_activation_step(_activation_layout(layout)),
    )


def _services_layout(
    layout: OpenClawReadyCompositionLayout,
    user_id: str,
) -> BackgroundServicesLayout:
    return BackgroundServicesLayout(
        layout.runtime,
        layout.runtime_environment,
        layout.target_os,
        user_id,
        job_namespace=layout.job_namespace,
    )


def _api_layout(
    layout: OpenClawReadyCompositionLayout,
    user_id: str,
) -> OpenClawApiServiceLayout:
    return OpenClawApiServiceLayout(
        layout.runtime,
        layout.runtime_environment,
        layout.openclaw_state,
        layout.target_os,
        user_id,
        layout.api_port,
        job_namespace=layout.job_namespace,
    )


def _activation_layout(
    layout: OpenClawReadyCompositionLayout,
) -> OpenClawActivationLayout:
    return OpenClawActivationLayout(
        layout.runtime,
        layout.openclaw_state,
        layout.api_port,
        layout.gateway_port,
    )


def _validate_composition_plan(
    plan: SetupPlan,
    layout: OpenClawReadyCompositionLayout,
    selection: ProviderSelection,
    codex_available: bool,
) -> None:
    if plan.selected_host.host != HostKind.OPENCLAW:
        raise ValueError("OpenClaw composition requires the OpenClaw host.")
    if plan.steps != _composition_steps(layout):
        raise ValueError("OpenClaw composition plan is incomplete or out of order.")
    if not plan.requires_confirmation or plan.installs_host:
        raise ValueError(
            "OpenClaw composition plan must be confirmed and host-preserving."
        )
    if plan.selected_host.config_path != str(layout.openclaw_state):
        raise ValueError(
            "OpenClaw composition target differs from the confirmed profile."
        )
    if (
        selection.validated(codex_available=codex_available).public_configuration()
        != plan.provider
    ):
        raise ValueError(
            "OpenClaw composition provider differs from the confirmed plan."
        )


def _selection_requires_store(selection: ProviderSelection) -> bool:
    return bool(selection.public_configuration()["required_secret_names"])


__all__ = [
    "OPENCLAW_READY_STEP_CODES",
    "OpenClawReadyCompositionAdapter",
    "OpenClawReadyCompositionLayout",
    "build_dormant_openclaw_ready_plan",
]
