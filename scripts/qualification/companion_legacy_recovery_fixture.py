"""Produce the old failure using unmodified baseline adapters on disposable CI."""

import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("source", "attempt", "bundle", "uv"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--uv-sha256", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve() / "src"))
    from daemons_companion.setup.plan import (
        HostKind,
        HostProbe,
        ProviderKind,
        ProviderSelection,
        SetupRequest,
        build_setup_plan,
    )
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.runtime.release import CompanionRuntimeLayout
    from daemons_companion.hermes.composition import HermesCompositionLayout
    from daemons_companion.hermes.ready_composition import (
        HermesReadyCompositionAdapter,
        build_dormant_hermes_ready_plan,
    )
    from daemons_companion.setup.transaction import (
        TransactionEngine,
        TransactionError,
        public_plan_digest,
    )

    scope = InstallationScope(args.attempt.resolve())
    scope.validate_home(scope.home)
    selection = ProviderSelection(ProviderKind.CODEX_CLI, "synthetic-model")
    host = HostProbe(
        HostKind.HERMES,
        True,
        True,
        executable="unused-synthetic",
        config_path=str(scope.profile),
    )
    reviewed = build_setup_plan(
        SetupRequest(HostKind.HERMES, "Aster", "feminine", selection),
        (host,),
        codex_available=True,
    )
    layout = HermesCompositionLayout(
        CompanionRuntimeLayout(scope.home, args.bundle.resolve(), scope),
        scope.profile,
        args.uv.resolve(),
        args.uv_sha256,
        "windows",
        scope.installation_id,
    )
    plan = build_dormant_hermes_ready_plan(reviewed, layout)

    adapter = HermesReadyCompositionAdapter(
        layout,
        plan=plan,
        provider_selection=selection,
        credential_store=None,
        credential_value=None,
        user_id="12345",
        codex_available=True,
        scheduler=SimpleNamespace(target_os="windows"),
        control=None,
    )
    # Inject at the existing baseline adapter's Python boundary, not the outer
    # composition hook (which only sees completed top-level steps). This models
    # the user's unpublished build failure with real uv files and real rollback.
    from daemons_companion.runtime.environment import (
        CompanionRuntimeEnvironmentAdapter,
    )

    def fail_after_python(boundary):
        if boundary == "python":
            raise RuntimeError(
                "Injected pre-publication failure after real managed Python acquisition"
            )

    adapter._base._delegates["runtime-environment"] = (
        CompanionRuntimeEnvironmentAdapter(
            layout.runtime_environment, mutation_hook=fail_after_python
        )
    )
    engine = TransactionEngine(scope.home / "transactions")
    failure = []
    try:
        engine.execute(
            plan,
            adapter,
            transaction_id="legacy-python",
            confirmed_plan_digest=public_plan_digest(plan),
        )
    except TransactionError as error:
        current = error
        while current is not None:
            failure.append(
                {
                    "type": type(current).__name__,
                    "message": str(current),
                    "stderr": getattr(current, "stderr", "")[-4000:],
                }
            )
            current = current.__cause__
    result = engine.inspect("legacy-python")
    print(
        json.dumps(
            {
                "status": result.status,
                "completed": result.completed_steps,
                "rolled_back": result.rolled_back_steps,
                "failed": result.rollback_failed_steps,
                "causes": failure,
            }
        ),
        flush=True,
    )
    assert result.status == "rollback_failed" and result.rollback_failed_steps == (
        "runtime-environment",
    )
    assert result.rolled_back_steps == ("runtime-layout",)


if __name__ == "__main__":
    main()
