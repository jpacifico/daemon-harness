"""Exercise private Python setup without a host, model, account, or service.

Run in a disposable runner. Reports observations from the selected source tree;
baseline failures are evidence, not an assertion that the user incident repeats.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time


def plan(step):
    from daemons_companion.setup.plan import HostKind, HostProbe, SetupPlan
    return SetupPlan(
        selected_host=HostProbe(HostKind.HERMES, detected=True, usable=True,
                               executable="synthetic-not-executed", version="synthetic",
                               version_status="verified"),
        daemon_name="TestDaemon", daemon_gender="feminine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(step,),
    )


def causes(error):
    from daemons_companion.runtime.bootstrap import HostCommandError
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 6:
        seen.add(id(error))
        row = {"type": type(error).__name__, "message": str(error)[:4000]}
        if isinstance(error, HostCommandError):
            row["command_detail"] = error.diagnostic[:8000]
        result.append(row)
        error = error.__cause__
    return result


def replay(source, uv, uv_digest, case):
    from daemons_companion.setup.plan import PlanStep
    from daemons_companion.desktop.install_diagnostics import InstallationProgress
    from daemons_companion.runtime.release import (
        CompanionRuntimeAdapter, CompanionRuntimeLayout, RUNTIME_LAYOUT_STEP,
        _REQUIRED_FILES, create_disposable_companion_marker, resolve_current_release,
    )
    from daemons_companion.runtime.environment import (
        CompanionRuntimeEnvironmentAdapter, CompanionRuntimeEnvironmentLayout,
        RUNTIME_ENVIRONMENT_STEP, _subprocess_runner, _tree_digest, release_environment_interpreter,
    )
    from daemons_companion.setup.transaction import TransactionEngine, public_plan_digest

    started = time.monotonic()
    # Do not sweep an incomplete baseline tree: its junctions may be broken.
    # The CI VM is discarded; local evidence stays in this uniquely owned temp root.
    root = Path(tempfile.mkdtemp(prefix="daemon-python-probe-")).resolve()
    home = root / ("h" * (109 - len(str(root)) - 1))
    if len(str(home)) != 109:
        raise ValueError("Temporary parent is too long for the 109-character fixture.")
    create_disposable_companion_marker(home)
    bundle = root / "bundle"
    for relative in sorted(_REQUIRED_FILES):
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    runtime = CompanionRuntimeLayout(home, bundle)
    layout_plan = plan(PlanStep(RUNTIME_LAYOUT_STEP, "Copy fixture", ("private files",)))
    TransactionEngine(root / "layout-journal").execute(
        layout_plan, CompanionRuntimeAdapter(runtime), transaction_id="layout",
        confirmed_plan_digest=public_plan_digest(layout_plan),
    )
    layout = CompanionRuntimeEnvironmentLayout(
        runtime, uv, uv_digest, "windows" if os.name == "nt" else "macos",
    )
    before = _tree_digest(home)
    boundaries, commands, configs = [], [], []
    release_id = resolve_current_release(runtime).name
    stage = (layout.staging_path(release_id) if hasattr(layout, "staging_path")
             else runtime.runtime_root / (".environment-" + release_id))
    final = (layout.environment_path(release_id) if hasattr(layout, "environment_path")
             else layout.environments_dir / release_id)

    def run(command, environment):
        result = _subprocess_runner(command, environment)
        commands.append({"argv": list(command), "exit": result.returncode,
                         "stdout": result.stdout[:8000], "stderr": result.stderr[:8000]})
        return result
    injected = False

    def boundary(name):
        nonlocal injected
        boundaries.append(name)
        if name in {"venv", "python-home", "published"}:
            from daemons_companion.runtime.environment import _io_path
            config = _io_path((final if name == "published" else stage) / "venv/pyvenv.cfg")
            configs.append({"boundary": name, "content": config.read_text(encoding="utf-8")[:8000]})
        print(json.dumps({"case": case, "boundary": name}), flush=True)
        if case == "fail-" + name:
            injected = True
            raise RuntimeError("Deliberate failure after " + name)

    adapter = CompanionRuntimeEnvironmentAdapter(layout, runner=run, mutation_hook=boundary)
    observed = InstallationProgress(adapter, lambda message: print(message, flush=True))
    env_plan = plan(PlanStep(RUNTIME_ENVIRONMENT_STEP, "Private Python", ("Python", "dependencies")))
    engine = TransactionEngine(root / "environment-journal")
    row = {"case": case, "fixture_root": str(root), "home_length": len(str(home)),
           "boundaries": boundaries, "commands": commands, "venv_configs": configs,
           "causes": [], "rollback_causes": []}
    try:
        result = engine.execute(env_plan, observed, transaction_id="environment",
                                confirmed_plan_digest=public_plan_digest(env_plan))
        row["transaction_status"] = result.status
        release = resolve_current_release(runtime)
        final = (layout.environment_path(release.name) if hasattr(layout, "environment_path")
                 else layout.environments_dir / release.name)
        interpreter = release_environment_interpreter(final, layout.target_os)
        completed = subprocess.run(
            [str(interpreter), "-I", "-B", "-c",
             "import encodings,fastapi,openai,pydantic,uvicorn,yaml; print('probe-ok')"],
            capture_output=True, text=True, timeout=60,
        )
        row["final_imports"] = {"exit": completed.returncode,
                                "stdout": completed.stdout, "stderr": completed.stderr[:8000]}
        row["passed"] = case == "success" and completed.returncode == 0 and completed.stdout.strip() == "probe-ok"
    except Exception as error:
        row["causes"] = causes(error)
        row["rollback_causes"] = [{"step": code, "causes": causes(failure)}
                                  for code, failure in observed.rollback_failures]
        try:
            state = engine.inspect("environment")
            row["transaction_status"] = state.status
            row["rollback_failed_steps"] = list(state.rollback_failed_steps)
            row["tree_restored"] = _tree_digest(home) == before
        except Exception as inspection_error:
            row["inspection_causes"] = causes(inspection_error)
        row["passed"] = (injected and row.get("transaction_status") == "rolled_back"
                         and row.get("tree_restored") is True)
    row["injected_failure_reached"] = injected
    row["elapsed_s"] = round(time.monotonic() - started, 3)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--uv-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    source, uv = args.source.resolve(), args.uv.resolve()
    if hashlib.sha256(uv.read_bytes()).hexdigest() != args.uv_sha256:
        raise SystemExit("Verified uv SHA-256 required.")
    sys.path.insert(0, str(source / "src"))
    report = {"native_windows": os.name == "nt", "platform": platform.platform(),
              "python": sys.version, "baseline": args.baseline,
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_commit": subprocess.check_output(
                  ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
              "uv_sha256": args.uv_sha256, "checks": []}
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            try:
                report["long_paths_enabled"] = winreg.QueryValueEx(key, "LongPathsEnabled")[0]
            except FileNotFoundError:
                report["long_paths_enabled"] = None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for case in ("success", "fail-python", "fail-published"):
            report["checks"].append(replay(source, uv, args.uv_sha256, case))
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception as error:
        report["harness_causes"] = causes(error)
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    passed = len(report["checks"]) == 3 and all(row["passed"] for row in report["checks"])
    # A baseline failure is retained verbatim; candidate failures fail the job.
    return 0 if (args.baseline and "harness_causes" not in report) or passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
