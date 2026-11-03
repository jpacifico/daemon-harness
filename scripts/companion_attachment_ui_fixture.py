"""Disposable native services for the packaged Companion attachment smoke test."""

from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
import sys
import traceback
import contextlib

from scripts.qualify_companion_openclaw_e2e import (
    FixedProvider,
    _initial_openclaw_config,
    _free_port,
    _safe_environment,
    SYNTHETIC_MESSAGE,
    EXPECTED_RESPONSE,
    _wait_until,
)
from scripts import souvenir_read_fixture
from daemons_companion.setup.plan import PlanStep
from daemons_companion.runtime.acquisition import _RELEASES
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    verify_current_release,
)
from daemons_companion.runtime.environment import (
    CompanionRuntimeEnvironmentAdapter,
    CompanionRuntimeEnvironmentLayout,
    resolve_release_environment,
    release_environment_interpreter,
)
from daemons_companion.hosts.attachment_services import (
    port_closed,
)
from daemons_companion.openclaw.host_install import (
    OpenClawHostLayout,
)
from daemon_core.persona import DEFAULT_DAEMON_PERSONA
from daemons_companion.runtime.bootstrap import (
    private_home_environment,
    _source_io_path,
)
from daemons_companion.hosts.attachment_command import native_target


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n" if isinstance(value, dict) else value)


def reply(value):
    print(json.dumps(value), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()
    base, bundle, uv = args.base.resolve(), args.bundle.resolve(), args.uv.resolve()
    root = base.parent
    if (
        not root.name.startswith("daemons-companion-ui-test-")
        or (base / "Companion/private/hosts").exists()
    ):
        raise ValueError("A fresh smoke-test directory is required.")
    base.mkdir(parents=True, exist_ok=True)
    target_os = native_target()
    os.environ.update(private_home_environment(root / "home"))
    (root / "home/Library/LaunchAgents").mkdir(parents=True)
    provider = FixedProvider(exercise_souvenir_read=True)
    worker = controller = None
    layout = host = None
    gateway_port, api_port = _free_port(), _free_port()
    log = (root / "fixture.log").open("w")
    report = {
        "external_model_calls": 0,
        "real_channel_messages": 0,
        "target_os": target_os,
        "target_jobs_removed": True,
    }
    try:
        provider.start()
        attempt = (
            base / "Companion/private/hosts/hermes/installations" / uuid.uuid4().hex
        )
        attempt.mkdir(parents=True)
        (attempt / "profile").mkdir()
        (attempt / "os-home/.codex").mkdir(parents=True)
        release = _RELEASES["hermes"]
        receipt = {
            "schema": "daemon.companion-host-install.v1",
            "installation_id": attempt.name,
            "version": release.version,
            "revision": release.revision,
        }
        for file in (attempt / "owner.json", attempt.parent.parent / "current.json"):
            file.write_text(json.dumps(receipt))
        scope = InstallationScope(attempt)
        scope.initialize()
        runtime = CompanionRuntimeLayout(scope.home, bundle, scope, True)
        snapshots = root / "snapshots"
        snapshots.mkdir()

        def execute(adapter, code):
            snap = snapshots / code
            snap.mkdir()
            step = PlanStep(code, code, ())
            adapter.snapshot(step, snap)
            adapter.apply(step)
            adapter.verify(step)

        execute(CompanionRuntimeAdapter(runtime), "runtime-layout")
        environment = CompanionRuntimeEnvironmentLayout(
            runtime, uv, hashlib.sha256(uv.read_bytes()).hexdigest(), target_os
        )
        execute(CompanionRuntimeEnvironmentAdapter(environment), "runtime-environment")
        source = verify_current_release(runtime)
        python = release_environment_interpreter(
            resolve_release_environment(environment), target_os
        )
        state = runtime.state_root
        memory = runtime.memory_root
        user = "777001"
        for p in (
            state / "config",
            state / "after-turn-spool",
            state / "receipts",
            memory / "core",
            memory / "users" / user,
            root / "records",
            root / "LaunchAgents",
        ):
            p.mkdir(parents=True, exist_ok=True)
        (state / "config/provider.json").write_text(
            json.dumps(
                {
                    "schema": "daemon.companion-provider.v1",
                    "kind": "openai_compatible_api",
                    "model": "synthetic-model",
                    "base_url": f"http://127.0.0.1:{provider.port}/v1",
                    "reasoning": {
                        "routing": "medium",
                        "conversation": "medium",
                        "memory": "high",
                    },
                    "credential": None,
                }
            )
        )
        (state / "config/provider.json").chmod(0o600)
        binary = uv  # Synthetic connection fingerprint, never used for sign-in.
        (attempt / "connection.json").write_text(
            json.dumps(
                {
                    "schema": "daemon.private-connection.v1",
                    "codex": str(binary),
                    "codex_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "user_id": user,
                }
            )
        )
        (memory / "core/daemon_identity.json").write_text(
            '{"name":"Aster","gender":"feminine"}'
        )
        (memory / "core/persona.md").write_text(DEFAULT_DAEMON_PERSONA + "\n")
        shutil.copyfile(
            bundle / "memory/templates/users/conversation.md",
            memory / "users" / user / "conversation.md",
        )
        # Synthetic fixture only; the root vault and its contents are never personal.
        souvenir_read_fixture.seed(memory)
        for folder in ("Projects", "Areas", "Resources", "Garden", "Archives"):
            (memory.parent / folder).mkdir(exist_ok=True)
        from daemon_core.memory.root_migration import identify_original

        note = memory / "capsules/theme/qualification_souvenir.md"
        root_note = memory.parent / "Projects/qualification.md"
        root_note.write_text(
            identify_original(note.read_text(), souvenir_read_fixture.PATH)
        )
        note.unlink()
        write(
            memory / "vault-layout.json",
            {
                "schema": 1,
                "layout": "root-folders-v1",
                "vault_root": str(memory.parent),
                "runtime_root": str(memory),
            },
        )
        write(
            memory / ".memory-structure-prototype.json",
            {"schema": 1, "memory_root": str(memory)},
        )
        from daemon_core.installation_profile import initialize_profile

        initialize_profile(scope, user)
        report["continuity_profile"] = "parga-v0"
        workerlog = (root / "worker.log").open("wb")
        worker = subprocess.Popen(
            [
                str(_source_io_path(python)),
                "-m",
                "daemons_companion.runtime.service_entry",
                "after-turn",
                "--release",
                str(source),
                "--python",
                str(python),
                "--memory-root",
                str(memory),
                "--state-root",
                str(state),
                "--provider-config",
                str(state / "config/provider.json"),
                "--runtime-revision",
                source.name,
                "--user-id",
                user,
                "--installation-attempt",
                str(attempt),
            ],
            cwd=_source_io_path(source),
            env=_safe_environment(
                attempt / "os-home",
                PYTHONPATH=str(_source_io_path(source / "src"))
                + os.pathsep
                + str(_source_io_path(source)),
                **private_home_environment(attempt / "os-home"),
            ),
            stdout=workerlog,
            stderr=subprocess.STDOUT,
        )
        workerlog.close()
        # The source installation receipt is synthetic setup state. This test does
        # not claim to reinstall Hermes or qualify its conversational behavior.
        transaction = uuid.uuid4().hex
        write(
            scope.home / "transactions" / transaction / "journal.json",
            {"status": "committed"},
        )
        write(
            attempt / "daemon-setup.json",
            {
                "schema": "daemon.private-setup.v1",
                "username": "synthetic_fixture_bot",
                "transaction_id": transaction,
                "user_id": user,
            },
        )
        write(scope.profile / "config.yaml", "model: synthetic-model\n")
        (scope.profile / "plugins/daemon_core").mkdir(parents=True)
        write(
            scope.profile / ".env",
            f"DAEMON_CORE_SRC={source / 'src'}\nDAEMON_CORE_MEMORY_ROOT={memory}\nDAEMON_CORE_STATE_ROOT={state}\nTELEGRAM_ALLOWED_USERS={user}\nTELEGRAM_BOT_TOKEN=100001:synthetic_source_not_a_real_token\n",
        )
        from daemons_companion.openclaw.host_install import (
            plan as host_plan,
            install,
        )

        host = OpenClawHostLayout(
            base / "Companion/private",
            uuid.uuid4().hex,
            target_os,
            bundle / "apps/companion/openclaw-runtime",
        )
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            install(host, host_plan(host)["revision"])
        config = _initial_openclaw_config(provider.port, gateway_port)
        config["channels"] = {
            "telegram": {
                "enabled": True,
                "botToken": "200001:synthetic_target_not_a_real_token",
                "dmPolicy": "allowlist",
                "allowFrom": [user],
            }
        }
        write(host.profile / "openclaw.json", config)
        # Native OpenClaw reads its state-directory .env before starting channels.
        # The channel guard still sees two distinct literal synthetic bot tokens.
        write(host.profile / ".env", "OPENCLAW_SKIP_CHANNELS=1\n")
        from daemons_companion.hosts.attachment_command import layouts

        request = {
            "base": str(base),
            "source_profile": str(scope.profile),
            "target_profile": str(host.profile),
            "api_port": api_port,
            "operation": "review",
        }
        layout, controller = layouts(request)
        report["target_jobs_removed"] = False
        _wait_until(
            lambda: controller.probe("maintenance"), timeout=30, label="shared worker"
        )
        originals = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                memory / "core/daemon_identity.json",
                memory.parent / "Projects/qualification.md",
            ]
        }
        original_config = (host.profile / "openclaw.json").read_bytes()
        reply(
            {
                "phase": "ready",
                "source": str(scope.profile),
                "target": str(host.profile),
                "memory": str(memory.parent),
                "worker_pid": worker.pid,
            }
        )
        for line in sys.stdin:
            command = json.loads(line)["command"]
            if command == "stop":
                break
            if command == "cancelled":
                assert not layout.record.exists()
                assert (host.profile / "openclaw.json").read_bytes() == original_config
                assert all(
                    controller.backend.capture(j.code) is None for j in controller.jobs
                )
                reply({"phase": "cancelled", "unchanged": True})
            elif command == "verify":
                record = json.loads(layout.record.read_text())
                request["api_port"] = record["api_port"]
                api_port = record["api_port"]
                layout, controller = layouts(request)
                controller.verify(layout.binding, host.profile, api_port)
                # The entry point deliberately refuses the Companion interpreter.
                # Inspect the binding through the installed daemon interpreter.
                profile_probe = subprocess.run(
                    [str(_source_io_path(python)), "-B", "-c",
                     "import json,sys;from pathlib import Path;"
                     "from daemons_companion.hosts.attachment_entry import api_environment;"
                     "from daemon_core.installation_profile import profile_status;"
                     "r=json.load(sys.stdin);b=r['binding'];"
                     "print(json.dumps(profile_status(api_environment(r),"
                     "user_ids=(b['user_id'],),memory_root=Path(b['memory']))))"],
                    input=json.dumps(record), capture_output=True, text=True,
                    timeout=30, cwd=_source_io_path(source),
                    env=_safe_environment(
                        attempt / "os-home",
                        PYTHONPATH=str(_source_io_path(source / "src"))
                        + os.pathsep + str(_source_io_path(source)),
                        **private_home_environment(attempt / "os-home"),
                    ),
                )
                assert profile_probe.returncode == 0, profile_probe.stderr
                loaded = json.loads(profile_probe.stdout)
                assert loaded and loaded["ok"], loaded
                report["shared_continuity_profile_verified"] = True
                assert record["status"] == "active"
                assert worker.poll() is None
                assert all(
                    hashlib.sha256(Path(p).read_bytes()).hexdigest() == value
                    for p, value in originals.items()
                )
                assert len(controller.jobs) == 2
                from daemons_companion.hosts.attachment_entry import host_environment

                exchange = subprocess.run(
                    [
                        str(host.node),
                        str(host.entry),
                        "agent",
                        "--json",
                        "--session-key",
                        "agent:main:explicit:companion-openclaw-e2e",
                        "--model",
                        "openai/synthetic-model",
                        "--thinking",
                        "off",
                        "--timeout",
                        "90",
                        "--message",
                        SYNTHETIC_MESSAGE,
                    ],
                    cwd=host.profile / "workspace-daemon-harness",
                    env=host_environment(host.profile)
                    | {"OPENCLAW_SKIP_CHANNELS": "1"},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=120,
                )
                log.write(exchange.stdout + exchange.stderr)
                if exchange.returncode or EXPECTED_RESPONSE not in exchange.stdout:
                    raise RuntimeError(
                        "The native exact-memory exchange failed; see fixture.log"
                    )
                assert souvenir_read_fixture.tool_round_trip_seen(provider.requests)
                gateway_log = (host.attempt / "logs/gateway.log").read_text()
                assert "skipping channel start" in gateway_log
                report.update(
                    activation="verified",
                    exact_memory_read=True,
                    source_files_unchanged=True,
                    shared_worker_pid=worker.pid,
                    target_jobs=2,
                    channels_skipped=True,
                )
                reply({"phase": "verified", **report})
            else:
                raise ValueError("Unknown fixture command")
    except BaseException:
        traceback.print_exc(file=log)
        reply({"phase": "failed", "log": str(root / "fixture.log")})
        raise
    finally:
        try:
            if controller:
                # Activation chooses its own free API port. Reload the recorded
                # value even after a failed UI step so cleanup checks the right port.
                if layout.record.exists():
                    api_port = json.loads(layout.record.read_text())["api_port"]
                    request["api_port"] = api_port
                    layout, controller = layouts(request)
                if layout.record.exists():
                    record = json.loads(layout.record.read_text())
                    if record["status"] == "active":
                        from daemons_companion.hosts.attachment import rollback

                        rollback(layout, record["revision"], controller)
                        report["rollback_verified"] = (
                            host.profile / "openclaw.json"
                        ).read_bytes() == original_config and json.loads(
                            layout.record.read_text()
                        )["status"] == "rolled_back"
                    else:
                        controller.stop()
                else:
                    controller.stop()
                report["target_jobs_removed"] = all(
                    controller.backend.capture(j.code) is None for j in controller.jobs
                )
        finally:
            if worker:
                worker.terminate()
                try:
                    worker.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
            provider.stop()
            report["ports_closed"] = all(
                port_closed(p) for p in (api_port, gateway_port, provider.port)
            )
            report["worker_stopped"] = worker is None or worker.poll() is not None
            write(root / "fixture-result.json", report)
            log.close()
        reply({"phase": "stopped", **report})


if __name__ == "__main__":
    main()
