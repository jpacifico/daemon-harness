"""Disposable Windows install/recovery with real Hermes, plugins and native jobs.

OAuth and remote endpoints are replaced by a loopback Bot API and fixed model.
The native Hermes Telegram adapter still runs. No real account/token is used. All three native jobs are removed even after failure.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--host-paths", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Native Windows is required")
    repository = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repository), str(repository / "src")]
    from daemons_companion.desktop.app import CompanionPreparationContext
    from daemons_companion.runtime.services import (
        WindowsTaskSchedulerBackend,
        AFTER_TURN_JOB,
        NIGHTLY_JOB,
        HERMES_GATEWAY_JOB,
        EMBEDDINGS_JOB,
        scoped_job_code,
    )
    from daemons_companion.setup.plan import (
        HostKind,
        ProviderKind,
        ProviderSelection,
        SetupRequest,
        build_setup_plan,
    )
    from daemons_companion.runtime.host_install import (
        HostInstallAdapter,
        HostInstallLayout,
        execute_host_install,
        host_install_plan,
        installed_host,
    )
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.setup.install_session import prepare_installation
    from daemons_companion.desktop.package import write_package_manifest
    from daemons_companion.hermes.first_install import (
        complete_private_setup,
        initialize_private_memory,
    )
    from daemons_companion.runtime.environment import _path_descriptor
    from daemons_companion.setup.transaction import public_plan_digest
    from scripts.qualification.companion_python_probe import causes

    started = time.monotonic()
    namespace = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix="dr-", dir=os.environ["RUNNER_TEMP"])).resolve()
    report = {
        "schema": "daemon.windows-setup-probe.v1",
        "source": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip(),
        "root": str(root),
        "telegram": False,
        "real_model": False,
        "oauth": False,
        "passed": False,
        "phases": [],
        "cleanup": False,
    }
    from daemons_companion.runtime.services import _scheduler_subprocess_runner

    report["scheduler_commands"] = []

    def scheduler_run(command, environment):
        stamp = time.monotonic()
        result = _scheduler_subprocess_runner(command, environment)
        report["scheduler_commands"].append(
            {
                "seconds": round(time.monotonic() - stamp, 3),
                "exit": result.returncode,
                "stderr": result.stderr[:2000],
            }
        )
        return result

    scheduler = WindowsTaskSchedulerBackend(
        job_namespace=namespace, runner=scheduler_run
    )
    native_matches = scheduler.matches
    def observed_matches(job, definition):
        result = native_matches(job, definition)
        if not result:
            from daemons_companion.runtime.services import _load_windows_task_capture, _windows_definition_signature
            saved = scheduler.capture(job.code)
            actual = _load_windows_task_capture(saved) if saved else None
            if actual and not actual["running"]:
                try:
                    child = subprocess.run([job.executable, *job.arguments], cwd=job.working_directory,
                                           capture_output=True, text=True, timeout=5)
                    report.setdefault("failed_job_launches", []).append({"code": job.code, "exit": child.returncode, "stdout": child.stdout[-4000:], "stderr": child.stderr[-5000:]})
                except subprocess.TimeoutExpired:
                    report.setdefault("failed_job_launches", []).append({"code": job.code, "timed_out": True})
            report.setdefault("failed_job_matches", []).append({
                "code": job.code, "running": actual["running"] if actual else None,
                "expected": _windows_definition_signature(definition),
                "actual": _windows_definition_signature(actual["definition"]) if actual else None})
        return result
    scheduler.matches = observed_matches
    native_install = scheduler.install
    def observed_install(job, definition):
        if args.host_paths and job.code.endswith("after-turn-worker") and "sys.path.insert(1," in job.arguments[2]:
            from daemons_companion.runtime.bootstrap import _source_io_path
            from daemons_companion.runtime.services import _scheduler_environment
            bootstrap = job.arguments[2].split("import daemons_companion.runtime.service_entry as entry;", 1)[0].split("runpy.run_module", 1)[0]
            root_statement = f"sys.path.insert(1,{str(_source_io_path(Path(job.working_directory)))!r});"
            report["source_root_imports"] = []
            for label, script in (("legacy", bootstrap.replace(root_statement, "")), ("extended-root", bootstrap)):
                result = subprocess.run([job.executable, "-B", "-c", script + "import integrations.after_turn_service.hermes_observer, integrations.after_turn_service.openclaw_observer; print('observers ready')"],
                                        cwd=job.working_directory, env=_scheduler_environment(), capture_output=True, text=True, timeout=45)
                report["source_root_imports"].append({"case": label, "exit": result.returncode, "stdout": result.stdout[-2000:], "stderr": result.stderr[-4000:]})
            assert report["source_root_imports"][0]["exit"] != 0
            assert report["source_root_imports"][1]["exit"] == 0
        native_install(job, definition)
    scheduler.install = observed_install
    jobs = [
        scoped_job_code(code, namespace)
        for code in (HERMES_GATEWAY_JOB, NIGHTLY_JOB, AFTER_TURN_JOB, EMBEDDINGS_JOB)
    ]
    provider = None
    telegram = None
    scope = None
    try:
        # Distinguish PowerShell startup from Task Scheduler/CIM time, without
        # exporting environment values or using any external account.
        from daemons_companion.runtime.services import _scheduler_environment

        report["powershell_startup"] = []
        for label, env in (
            ("filtered", _scheduler_environment()),
            ("ci_session", dict(os.environ)),
        ):
            stamp = time.monotonic()
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "'ready'",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            report["powershell_startup"].append(
                {
                    "environment": label,
                    "seconds": round(time.monotonic() - stamp, 3),
                    "exit": result.returncode,
                    "stdout": result.stdout[-100:],
                    "stderr": result.stderr[-2000:],
                }
            )
        assert all(scheduler.capture(code) is None for code in jobs)
        package = root / "p"
        bundle = package / "payload"
        files = (
            subprocess.check_output(["git", "ls-files", "-z"], cwd=repository)
            .decode()
            .split("\0")
        )
        for name in filter(None, files):
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / name, target)
        (package / "bin").mkdir()
        shutil.copy2(args.uv, package / "bin/uv.exe")
        digest = hashlib.sha256(args.uv.read_bytes()).hexdigest()
        write_package_manifest(package, target_os="windows")
        layout = HostInstallLayout(
            (root / ("p" * max(1, 66 - len(str(root)) - 1))) if args.host_paths else root / "c",
            package / "bin/uv.exe", digest, namespace, "windows"
        )

        def save_progress():
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

        def progress(message):
            print(message, flush=True)
            report["phases"].append(message)
            save_progress()

        execute_host_install(
            layout,
            confirmed_digest=public_plan_digest(host_install_plan(layout)),
            adapter=HostInstallAdapter(layout, progress=progress),
        )
        report["host_ready_s"] = round(time.monotonic() - started, 3)
        host = installed_host(layout)
        assert host is not None
        scope = InstallationScope.from_verified_host(layout)
        scope.initialize()
        from daemons_companion.setup.dependencies import ensure_setup_dependencies

        ensure_setup_dependencies(layout, bundle, progress)
        import scripts.qualify_companion_hermes_e2e as harness

        from scripts.qualification.companion_telegram_fixture import (
            TelegramFixture, TOKEN, model_completion,
        )
        provider = harness.FixedProvider(completion_callback=model_completion)
        provider.start()
        telegram = TelegramFixture()
        telegram.start()
        # Files are synthetic sentinels, never real credentials. The local API
        # selection below never executes this placeholder Codex binary.
        codex = layout.attempt / "synthetic-codex.exe"
        codex.write_bytes(b"never executed")
        sentinels = {}
        for name in (".codex", ".hermes-codex"):
            auth = layout.attempt / "os-home" / name
            auth.mkdir()
            file = auth / "auth.json"
            file.write_text('{"synthetic":"preserve"}')
            sentinels[str(file)] = file.read_bytes()
        initialize_private_memory(scope, bundle, "12345")
        persona = scope.home / "data/memory/core/persona.md"
        sentinels[str(persona)] = persona.read_bytes()
        connection = scope.attempt / "connection.json"
        connection.write_text(
            json.dumps(
                {
                    "schema": "daemon.private-connection.v1",
                    "codex": str(codex),
                    "codex_sha256": hashlib.sha256(codex.read_bytes()).hexdigest(),
                    "user_id": "12345",
                }
            )
            + "\n"
        )
        if not args.host_paths:
            progress("Reproducing the published legacy Python rollback failure...")
            legacy = subprocess.run(
                [
                    sys.executable,
                    "-S",
                    str(
                        repository
                        / "scripts/qualification/companion_legacy_recovery_fixture.py"
                    ),
                    "--source",
                    str(args.baseline.resolve()),
                    "--attempt",
                    str(scope.attempt),
                    "--bundle",
                    str(bundle),
                    "--uv",
                    str(package / "bin/uv.exe"),
                    "--uv-sha256",
                    digest,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            report["legacy"] = {
                "exit": legacy.returncode,
                "stdout": legacy.stdout[-8000:],
                "stderr": legacy.stderr[-8000:],
            }
            assert legacy.returncode == 0, (
                "Legacy fixture did not reach the required failure"
            )
            old_journal = scope.home / "transactions/legacy-python/journal.json"
            sentinels[str(old_journal)] = old_journal.read_bytes()
            old_snapshot = json.loads(
                (
                    old_journal.parent / "snapshots/runtime-environment/manifest.json"
                ).read_text()
            )
            old_stage = (
                scope.home / "data/runtime" / (".environment-" + old_snapshot["release_id"])
            )
            before_tree = _path_descriptor(old_stage, recursive=True)
            report["legacy_remaining_tree"] = before_tree
            assert before_tree["kind"] in {"directory", "absent"}
        context = CompanionPreparationContext(
            package,
            layout.companion_home,
            bundle,
            package / "bin/uv.exe",
            digest,
            "windows",
            installation_scope=scope,
        )
        selection = ProviderSelection(
            ProviderKind.OPENAI_COMPATIBLE_API,
            "synthetic-model",
            f"http://127.0.0.1:{provider.port}/v1",
        )
        reviewed = build_setup_plan(
            SetupRequest(HostKind.HERMES, "Aster", "feminine", selection),
            (host,),
            codex_available=True,
        )
        prepared = prepare_installation(
            reviewed, context.install_layout(HostKind.HERMES, scope.profile)
        )

        def configure(*a, **k):
            assert k["token"] is None, "Saved connection was not selected"
            import yaml
            config = yaml.safe_load(harness._profile_config(provider.port))
            config['platforms'] = {'telegram': {'enabled': True, 'extra': {
                'base_url': f'http://127.0.0.1:{telegram.port}/bot',
                'drop_pending_on_cold_boot': True,
            }}}
            (scope.profile / "config.yaml").write_text(yaml.safe_dump(config), encoding='utf-8')
            (scope.profile / ".env").write_text(
                f'TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USERS=12345\n', encoding='utf-8')
            return {"username": "synthetic_bot", "bot_id": 123456789}

        owner = SimpleNamespace(
            layout=lambda: layout,
            context=context,
            codex_executable=lambda _: codex,
            daemon_installed=False,
        )

        class UI:
            sent = False

            @contextmanager
            def activity(self, label):
                progress(label)
                yield progress

            def say(self, message):
                progress(message)
                if message.startswith("Press Start") and not self.sent:
                    self.sent = True
                    telegram.submit(harness.SYNTHETIC_MESSAGE)

            def choose(self, title, *a, **k):
                if title == "Did you receive the reply in Telegram?":
                    assert harness.EXPECTED_RESPONSE in telegram.displayed
                    report['first_exchange_and_memory_s'] = round(time.monotonic() - telegram.submitted_at, 3)
                    return 1
                return 0

            def secret(self, *a):
                raise AssertionError("Saved connection must not ask for a token")

            def field(self, *a, **k):
                raise AssertionError("Saved connection must preserve account ID")

        from daemons_companion.runtime.bootstrap import _source_io_path
        from daemon_core.after_turn_service import read_after_turn_service_status
        from daemons_companion.hermes.activation import _readiness_probe as native_readiness
        def observed_readiness(revision, memory, spool):
            old = read_after_turn_service_status(expected_revision=revision, memory_root=memory, spool_root=spool)
            new = read_after_turn_service_status(expected_revision=revision, memory_root=_source_io_path(memory), spool_root=_source_io_path(spool))
            sample = {"ordinary": old, "extended": new}
            if report.get("readiness_paths") != sample:
                report["readiness_paths"] = sample
                save_progress()
            return native_readiness(revision, memory, spool)

        from daemons_companion.hermes.gateway import PrivateHermesControl
        from daemons_companion.runtime.services import _load_windows_task_capture
        native_gateway_ready = PrivateHermesControl.gateway_ready
        gateway_wait_started = None
        def observed_gateway_ready(control):
            nonlocal gateway_wait_started
            if gateway_wait_started is None:
                gateway_wait_started = time.monotonic()
            ready = native_gateway_ready(control)
            elapsed = time.monotonic() - gateway_wait_started
            if not ready and elapsed > 60:
                captured = control.scheduler.capture(control.job_code)
                running = bool(captured and _load_windows_task_capture(captured)["running"])
                report["gateway_failure_status"] = {"running": running, "probe": control._probe()}
                if not running:
                    job = control.job()
                    try:
                        child = subprocess.run([job.executable, *job.arguments], cwd=job.working_directory,
                                               capture_output=True, text=True, timeout=10)
                        report["gateway_failure_launch"] = {"exit": child.returncode, "stdout": child.stdout[-5000:], "stderr": child.stderr[-8000:]}
                    except subprocess.TimeoutExpired as error:
                        report["gateway_failure_launch"] = {"timeout": True, "stderr": str(error.stderr)[-8000:]}
                    save_progress()
                    raise RuntimeError("Synthetic gateway exited; exact launch diagnostic captured.")
                if elapsed > 180:
                    save_progress()
                    raise RuntimeError("Synthetic gateway did not connect within three minutes; status captured.")
            return ready

        with (
            patch.object(PrivateHermesControl, "gateway_ready", observed_gateway_ready),
            patch("daemons_companion.hermes.activation._readiness_probe", observed_readiness),
            patch(
                "daemons_companion.hermes.first_install.PrivateSubscriptionSetup",
                lambda *a, **k: SimpleNamespace(
                    status=lambda: SimpleNamespace(codex=True, hermes=True)
                ),
            ),
            patch(
                "daemons_companion.hermes.first_install.ProviderSelection",
                lambda *a, **k: selection,
            ),
            patch("daemons_companion.hermes.first_install.configure_telegram", configure),
            patch(
                "daemons_companion.hermes.first_install.WindowsTaskSchedulerBackend",
                lambda *a, **k: scheduler,
            ),
        ):
            assert complete_private_setup(
                owner,
                UI(),
                {
                    "host": "hermes",
                    "daemon_name": "Aster",
                    "daemon_gender": "feminine",
                    "provider": {"kind": "codex_cli", "model": "synthetic-model"},
                },
                prepared.public_dict(),
            )
        assert harness.EXPECTED_RESPONSE in telegram.displayed
        report['reply'] = harness.EXPECTED_RESPONSE
        report['native_telegram_adapter'] = True
        assert all(
            Path(path).read_bytes() == content for path, content in sentinels.items()
        )
        if not args.host_paths:
            assert (
                _path_descriptor(
                    scope.home / "data/runtime/.retained-legacy-python", recursive=True
                )
                == before_tree
            )
            assert not old_stage.exists()
        if args.host_paths:
            # Recreate exactly the previous gateway command after a committed
            # install, then repair through the same resume UI as the download.
            from daemons_companion.hermes.gateway import private_gateway_job
            from daemons_companion.runtime.services import render_job_definition
            from daemons_companion.hermes.first_install import resume_private_setup
            runtime = context.install_layout(HostKind.HERMES, scope.profile).runtime
            job = private_gateway_job(layout, runtime, namespace)
            legacy_job = private_gateway_job(layout, runtime, namespace, legacy=True)
            report["home_length"] = len(str(layout.companion_home))
            report["launch"] = job.executable
            original_runtime = runtime.current_pointer.read_bytes()
            original_setup = (scope.attempt / "daemon-setup.json").read_bytes()
            auth_before = {p: Path(p).read_bytes() for p in sentinels}
            from daemons_companion.runtime.bootstrap import _source_io_path
            from daemons_companion.runtime.service_path_repair import service_path_repair
            from daemons_companion.hermes.profile import _patch_env
            migration = service_path_repair(layout, runtime, scheduler, legacy_job, job)
            original_environment = (layout.profile / ".env").read_bytes()
            for old, new in migration.pairs:
                scheduler.restore(new.code, None)
            legacy_values = {key: str(path) for key, path in migration.roots.items()}
            from daemons_companion.runtime.release import verify_current_release
            legacy_values["DAEMON_CORE_SRC"] = str(verify_current_release(runtime) / "src")
            (layout.profile / ".env").write_bytes(_patch_env(original_environment, legacy_values))
            # The previous distributed profile declared an ordinary source root.
            # Exercise that declaration too, not only ordinary memory roots.
            expected_environment = original_environment
            job = private_gateway_job(layout, runtime, namespace)
            migration = service_path_repair(layout, runtime, scheduler, legacy_job, job)
            (layout.profile / "bin/uv.exe").unlink()
            for old, new in reversed(migration.pairs):
                native_install(old, render_job_definition(old, "windows"))

            # Exercise the exact previously distributed background entry with the
            # repaired command/arguments, without replacing any installed code.
            old_entry = args.baseline.resolve() / "src/daemons_companion/runtime/service_entry.py"
            worker = migration.pairs[1][1]
            bootstrap = worker.arguments[2].split("import daemons_companion.runtime.service_entry as entry;", 1)[0]
            command = [worker.executable, *worker.arguments]
            command[3] = bootstrap + f"space=runpy.run_path({str(old_entry)!r});space['_configure'](space['_parser']().parse_args());import os;print(os.environ['DAEMON_CORE_MEMORY_ROOT'])"
            checked = subprocess.run(command, cwd=worker.working_directory, capture_output=True, text=True, timeout=60)
            report["legacy_background_entry"] = {"exit": checked.returncode, "stdout": checked.stdout[-2000:], "stderr": checked.stderr[-4000:]}
            assert checked.returncode == 0, checked.stderr
            assert str(_source_io_path(runtime.memory_root)) in checked.stdout
            # Previous profiles also used an ordinary source declaration. Probe
            # the gateway entry itself under both spellings in fresh processes.
            import_source = verify_current_release(runtime) / "src"
            report["gateway_source_imports"] = []
            for label, source in (("ordinary", import_source), ("extended", _source_io_path(import_source))):
                checked = subprocess.run([layout.launch_python, "-B", "-c",
                    f"import sys;sys.path.insert(0,{str(source)!r});import daemons_companion.hermes.gateway;print('gateway imports ready')"],
                    cwd=layout.attempt, capture_output=True, text=True, timeout=45)
                report["gateway_source_imports"].append({"case": label, "exit": checked.returncode, "stdout": checked.stdout[-1000:], "stderr": checked.stderr[-4000:]})
            assert report["gateway_source_imports"][0]["exit"] != 0
            assert report["gateway_source_imports"][1]["exit"] == 0

            # Establish the same missing-module failure before any repair, even
            # though Task Scheduler's long-running service can cache OS policy.
            from daemons_companion.runtime.bootstrap import clean_environment, private_home_environment
            env = clean_environment(layout.attempt)
            env.update(private_home_environment(layout.attempt / "os-home"))
            broken = subprocess.run([str(layout.python), "-B", "-c", "from openai import OpenAI"],
                                    env=env, capture_output=True, text=True, timeout=45)
            report["legacy_sdk_import"] = {"exit": broken.returncode, "stderr": broken.stderr[-4000:]}
            assert broken.returncode != 0

            class ResumeUI(UI):
                def say(self, message):
                    progress(message)
                    if message.startswith("Open https://t.me/"):
                        telegram.displayed.clear()
                        telegram.submit(harness.SYNTHETIC_MESSAGE)

            stamp = time.monotonic()
            with patch("daemons_companion.hermes.first_install.WindowsTaskSchedulerBackend", lambda **k: scheduler):
                assert resume_private_setup(owner, ResumeUI())
            assert harness.EXPECTED_RESPONSE in telegram.displayed
            assert any("reply and its completed memory work are confirmed" in phase for phase in report["phases"])
            assert runtime.current_pointer.read_bytes() == original_runtime
            assert (scope.attempt / "daemon-setup.json").read_bytes() == original_setup
            assert all(Path(p).read_bytes() == content for p, content in auth_before.items())
            assert all(scheduler.matches(new, render_job_definition(new, "windows")) for _, new in migration.pairs)
            assert (layout.profile / ".env").read_bytes() == expected_environment
            assert scheduler.matches(job, render_job_definition(job, "windows"))
            assert hashlib.sha256((layout.profile / "bin/uv.exe").read_bytes()).hexdigest() == digest
            report["gateway_resume"] = {"passed": True, "seconds": round(time.monotonic()-stamp, 3),
                                        "runtime_and_auth_unchanged": True, "all_three_paths_repaired": True, "environment_other_values_unchanged": True}
            report["repair_journals"] = [json.loads(path.read_text()) for path in (scope.attempt / "gateway-repairs").glob("*/journal.json")]
            # Exercise the real nightly entry/configuration and its child import
            # paths, without executing any maintenance judgment or writing memory.
            from daemons_companion.hermes.composition import HermesCompositionLayout
            from daemons_companion.hermes.ready_composition import _services_layout
            from daemons_companion.runtime.services import build_background_jobs
            composition = HermesCompositionLayout(runtime, layout.profile, layout.uv_executable, digest, "windows", namespace)
            nightly = build_background_jobs(_services_layout(composition, "12345"))[1]
            bootstrap, entry = nightly.arguments[2].rsplit("entry.main()", 1)
            probe = r"""
import os, subprocess
import daemon_core.nightly_maintenance as maintenance
original_cycle = maintenance.run_nightly_cycle
def import_child(command, cwd, environment):
    module = command[command.index('-m')+1]
    result = subprocess.run([command[0], '-B', '-c', 'import '+module], cwd=cwd, env=dict(environment), capture_output=True, text=True, timeout=45)
    print('nightly-child-import', module, result.returncode, flush=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return 0
maintenance.run_nightly_cycle = lambda **kwargs: original_cycle(**kwargs, runner=import_child)
"""
            command = [nightly.executable, *nightly.arguments]
            command[3] = bootstrap + probe + "\nentry.main()" + entry
            count = len(provider.requests)
            checked = subprocess.run(command, cwd=nightly.working_directory, capture_output=True, text=True, timeout=180)
            report['nightly_child_imports'] = {'exit': checked.returncode, 'stdout': checked.stdout[-4000:], 'stderr': checked.stderr[-4000:]}
            assert checked.returncode == 0, checked.stderr
            assert 'nightly-child-import daemon_core.dreaming 0' in checked.stdout
            assert 'nightly-child-import daemon_core.rem 0' in checked.stdout
            assert len(provider.requests) == count

        report.update(
            passed=True,
            retained_tree_exact=not args.host_paths,
            sentinels_unchanged=True,
            receipt_cycle_completed=True,
            provider_requests=len(provider.requests),
            native_gateway=True,
            native_worker=True,
        )
    except Exception as error:
        report["causes"] = causes(error)
        import traceback

        report["traceback"] = traceback.format_exc()
        print(json.dumps(report["causes"]), flush=True)
        if scope:
            from daemons_companion.runtime.bootstrap import _source_io_path
            report["runtime_differences"] = []
            for release in (scope.home / "data/runtime/releases").glob("*"):
                actual_root = _source_io_path(release)
                manifest = json.loads((actual_root / ".daemon-harness-release.json").read_text())
                expected = {entry["path"]: entry for entry in manifest["entries"]}
                for path in actual_root.rglob("*"):
                    relative = path.relative_to(actual_root).as_posix()
                    if relative == ".daemon-harness-release.json":
                        continue
                    entry = expected.pop(relative, None)
                    if entry is None:
                        report["runtime_differences"].append({"path": relative, "change": "added"})
                    elif entry["kind"] == "file" and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]):
                        report["runtime_differences"].append({"path": relative, "change": "modified"})
                report["runtime_differences"].extend({"path": path, "change": "missing"} for path in expected)
            report["logs"] = {}
            for path in [
                scope.attempt / "installation-diagnostic.json",
                scope.home / "data/state/gateway.log",
                *list((scope.home / "data/state").glob("*.log")),
                *list((scope.profile / "logs").glob("*.log")),
            ]:
                if path.is_file():
                    report["logs"][str(path.relative_to(scope.attempt))] = (
                        path.read_text(encoding="utf-8", errors="replace")[-14000:]
                    )
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if scope:
            report["receipts"] = {
                path.name: path.read_text(encoding="utf-8")[-30000:]
                for path in (scope.home / "data/state/receipts").glob("*.jsonl")
            }
        errors = []
        # Capture first: failed pre-service builds have no job to stop/delete.
        for code in jobs:
            try:
                if scheduler.capture(code) is not None:
                    scheduler.restore(code, None)
            except Exception as error:
                errors.extend(causes(error))
        report["cleanup"] = not errors and all(
            scheduler.capture(code) is None for code in jobs
        )
        if errors:
            report["cleanup_errors"] = errors
        if telegram:
            report['bot_api_dropped_counts'] = telegram.dropped_counts
            report['bot_api_methods'] = sorted(set(telegram.methods))
            report['bot_api_unknown_methods'] = sorted(set(telegram.unknown))
            report['synthetic_deliveries'] = telegram.sent
            report['synthetic_displayed'] = telegram.displayed
            report['delivery_seconds'] = telegram.delivery_seconds
            telegram.stop()
        if provider:
            report['provider_requests'] = len(provider.requests)
            report['provider_systems'] = [str(item.get('content', ''))[:180] for request in provider.requests for item in request.get('messages', []) if item.get('role') == 'system']
            provider.stop()
        report["elapsed_s"] = round(time.monotonic() - started, 3)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] and report["cleanup"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
