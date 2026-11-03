"""Dormant composition of qualified companion operations for Hermes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from daemons_companion.setup.plan import (
    HostKind,
    PlanStep,
    ProviderSelection,
    SetupPlan,
)
from daemons_companion.setup.credentials import CredentialStore
from daemons_companion.hermes.profile import (
    HERMES_PROFILE_STEP,
    HermesProfileAdapter,
    HermesProfileLayout,
)
from daemons_companion.desktop.identity import (
    IDENTITY_STEP,
    CompanionIdentityAdapter,
    CompanionIdentityLayout,
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
    verify_current_release,
)
from daemons_companion.runtime.environment import (
    RUNTIME_ENVIRONMENT_STEP,
    CommandRunner,
    CompanionRuntimeEnvironmentAdapter,
    CompanionRuntimeEnvironmentLayout,
    release_environment_interpreter,
    resolve_release_environment,
)


_PUBLIC_HERMES_STEP_CODES = (
    "review",
    "runtime",
    "identity",
    "provider",
    "hermes-plugin",
    "verify",
)
HERMES_COMPOSITION_STEP_CODES = (
    RUNTIME_LAYOUT_STEP,
    RUNTIME_ENVIRONMENT_STEP,
    IDENTITY_STEP,
    PROVIDER_STATE_STEP,
    HERMES_PROFILE_STEP,
)
@dataclass(frozen=True)
class HermesCompositionLayout:
    runtime: CompanionRuntimeLayout
    hermes_home: Path
    uv_executable: Path
    uv_sha256: str
    target_os: str
    job_namespace: str | None = None

    @property
    def runtime_environment(self) -> CompanionRuntimeEnvironmentLayout:
        return CompanionRuntimeEnvironmentLayout(
            runtime=self.runtime,
            uv_executable=self.uv_executable,
            uv_sha256=self.uv_sha256,
            target_os=self.target_os,
        )


def build_dormant_hermes_composition_plan(
    reviewed: SetupPlan,
    layout: HermesCompositionLayout,
) -> SetupPlan:
    """Expand an exact reviewed Hermes plan without claiming service readiness."""

    if reviewed.selected_host.host != HostKind.HERMES:
        raise ValueError("Hermes composition requires a reviewed Hermes plan.")
    if tuple(step.code for step in reviewed.steps) != _PUBLIC_HERMES_STEP_CODES:
        raise ValueError("Reviewed Hermes plan does not match the canonical public plan.")
    if reviewed.selected_host.config_path != str(layout.hermes_home):
        raise ValueError("Reviewed Hermes profile differs from the composition target.")
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


class HermesCompositionAdapter:
    """Route an exact five-step plan to the existing qualified adapters."""

    def __init__(
        self,
        layout: HermesCompositionLayout,
        *,
        plan: SetupPlan,
        provider_selection: ProviderSelection,
        credential_store: CredentialStore | None,
        credential_value: str | None,
        user_id: str,
        codex_available: bool = False,
        runner: CommandRunner | None = None,
        recovery_only: bool = False,
        mutation_hook: Callable[[str], None] | None = None,
    ) -> None:
        _validate_composition_plan(plan, layout, provider_selection, codex_available)
        self.layout = layout
        self.plan = plan
        self.provider_selection = provider_selection
        self.credential_store = credential_store
        self.credential_value = credential_value
        self.user_id = user_id
        self.codex_available = codex_available
        self.runner = runner
        self.recovery_only = recovery_only
        self._mutation_hook = mutation_hook or (lambda _step: None)
        self._delegates: dict[str, object] = {}

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        _validate_composition_plan(
            self.plan,
            self.layout,
            self.provider_selection,
            self.codex_available,
        )
        self._step(step).snapshot(step, snapshot_dir)

    def apply(self, step: PlanStep) -> None:
        self._step(step).apply(step)
        self._mutation_hook(step.code)

    def verify(self, step: PlanStep) -> None:
        self._step(step).verify(step)

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._step(step).rollback(step, snapshot_dir)

    def _step(self, step: PlanStep):
        if step.code not in HERMES_COMPOSITION_STEP_CODES:
            raise ValueError("Hermes composition received an unsupported step.")
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
                                        self.layout.runtime.root_folder_memory),
                name=self.plan.daemon_name,
                gender=self.plan.daemon_gender,
                user=self.plan.user_preferences.for_user(self.user_id) if self.plan.user_preferences else None,
            )
        if code == PROVIDER_STATE_STEP:
            provider_layout = CompanionProviderLayout(
                self.layout.runtime.companion_home, self.layout.runtime.installation_scope
            )
            if self.recovery_only and _selection_requires_store(
                self.provider_selection
            ):
                if self.credential_store is None:
                    raise ValueError("Provider recovery requires its credential store.")
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
        if code == HERMES_PROFILE_STEP:
            release = verify_current_release(self.layout.runtime)
            environment = resolve_release_environment(
                self.layout.runtime_environment
            )
            interpreter = release_environment_interpreter(
                environment,
                self.layout.target_os,
            )
            state_root = self.layout.runtime.state_root
            return HermesProfileAdapter(
                HermesProfileLayout(self.layout.hermes_home, release, self.layout.runtime.installation_scope,
                                plugin_copies=self.layout.target_os == "windows"),
                selected_host=HostKind.HERMES,
                user_id=self.user_id,
                runtime_environment={
                    "DAEMON_CORE_MEMORY_ROOT": str(
                        self.layout.runtime.memory_root
                    ),
                    "DAEMON_CORE_STATE_ROOT": str(state_root),
                    "DAEMON_CORE_PROVIDER_CONFIG": str(
                        state_root / "config" / "provider.json"
                    ),
                    "DAEMON_CORE_PYTHON": str(interpreter),
                    "DAEMON_CORE_LATENCY_LOG": str(
                        state_root / "latency" / "hermes.jsonl"
                    ),
                },
            )
        raise ValueError("Hermes composition received an unsupported step.")


def _composition_steps(layout: HermesCompositionLayout) -> tuple[PlanStep, ...]:
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
            ("One model", "Protected credential", "No secret in persisted settings"),
        ),
        PlanStep(
            HERMES_PROFILE_STEP,
            "Connect the authorized Hermes profile to the private runtime.",
            (
                f"Hermes profile: {layout.hermes_home}",
                "Bounded config and environment",
                "Two verified plugin copies" if layout.target_os == "windows" else "Two plugin links",
            ),
        ),
    )


def _validate_composition_plan(
    plan: SetupPlan,
    layout: HermesCompositionLayout,
    selection: ProviderSelection,
    codex_available: bool,
) -> None:
    if plan.selected_host.host != HostKind.HERMES:
        raise ValueError("Hermes composition requires the Hermes host.")
    if plan.steps != _composition_steps(layout):
        raise ValueError("Hermes composition plan is incomplete or out of order.")
    if not plan.requires_confirmation or plan.installs_host:
        raise ValueError("Hermes composition plan must be confirmed and host-preserving.")
    if plan.selected_host.config_path != str(layout.hermes_home):
        raise ValueError("Hermes composition target differs from the confirmed profile.")
    if selection.validated(codex_available=codex_available).public_configuration() != plan.provider:
        raise ValueError("Hermes composition provider differs from the confirmed plan.")


def _selection_requires_store(selection: ProviderSelection) -> bool:
    return bool(selection.public_configuration()["required_secret_names"])


__all__ = [
    "HERMES_COMPOSITION_STEP_CODES",
    "HermesCompositionAdapter",
    "HermesCompositionLayout",
    "build_dormant_hermes_composition_plan",
]
