"""Restartable entry for an approved host association, never a new daemon."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from daemons_companion.hosts.attachment import digest, decode, host_reference
from daemons_companion.openclaw.host_install import checked_file, direct

PLAN_KEYS = {
    "schema",
    "binding",
    "profile",
    "host",
    "host_version",
    "api_port",
    "record",
    "workspace_existed",
    "before",
    "expected",
}


def load_record(path):
    path = direct(Path(path))
    record = json.loads(checked_file(path, 12 * 1024 * 1024))
    if (
        set(record) != PLAN_KEYS | {"revision", "status", "capture", "written"}
        or record["schema"] != "daemon.host-attachment.v1"
        or record["host"] != "openclaw"
        or record["record"] != str(path)
        or record["status"] not in {"prepared", "active"}
        or digest(
            json.dumps({k: record[k] for k in PLAN_KEYS}, sort_keys=True).encode()
        )
        != record["revision"]
    ):
        raise ValueError("The host association is not approved for activation.")
    b = record["binding"]
    state = direct(Path(b["state"]))
    profile = direct(Path(record["profile"]))
    from daemon_core.host_bindings import resolve_host_execution

    execution = resolve_host_execution(
        {"host_ref": host_reference(profile)},
        host="openclaw",
        user_id=b["user_id"],
        memory=Path(b["memory"]),
        environment={"DAEMON_CORE_STATE_ROOT": str(state)},
    )
    if execution.profile != profile:
        raise ValueError("The registered target changed.")
    for name, file in {
        "config": profile / "openclaw.json",
        "environment": profile / ".env",
        "agents": profile / "workspace-daemon-harness/AGENTS.md",
        "host-binding": state / "host-bindings" / (execution.host_ref + ".json"),
    }.items():
        if checked_file(file, 2 * 1024 * 1024) != decode(record["expected"][name]):
            raise ValueError(
                "The associated host configuration changed before restart."
            )
    source = direct(Path(b["source"]))
    if (
        digest(
            checked_file(
                source / "integrations/openclaw_plugin/index.js", 2 * 1024 * 1024
            )
        )
        != b["plugin_sha256"]
        or digest(
            checked_file(source / "src/daemon_core/host_bindings.py", 2 * 1024 * 1024)
        )
        != b["routing_sha256"]
    ):
        raise ValueError("The associated runtime changed before restart.")
    from daemons_companion.runtime.bootstrap import same_interpreter

    if not same_interpreter(Path(sys.executable), Path(b["python_target"])):
        raise ValueError("The associated interpreter changed.")
    return record, execution


def host_environment(profile):
    from daemons_companion.runtime.bootstrap import (
        clean_environment,
        private_home_environment,
    )

    profile = direct(Path(profile))
    home = direct(profile.parent / "os-home")
    if not home.is_dir():
        raise ValueError("The private host home is unavailable.")
    env = clean_environment(profile.parent)
    env.update(private_home_environment(home))
    env.update(
        OPENCLAW_STATE_DIR=str(profile),
        OPENCLAW_CONFIG_PATH=str(profile / "openclaw.json"),
    )
    from daemons_companion.hosts.creation import credential_environment_for_profile

    env.update(credential_environment_for_profile(profile))
    # The host reads its own model/account settings. Never import the primary
    # host's OAuth files, API environment or Telegram token into this process.
    return {k: v for k, v in env.items() if not k.startswith("HERMES_")}


def api_environment(record):
    from daemons_companion.runtime import service_entry as background_entry
    from daemons_companion.runtime.installation_scope import InstallationScope

    b = record["binding"]
    state = Path(b["state"])
    source = Path(b["source"])
    scope = InstallationScope(state.parents[2])
    scope.validate_home(state.parents[1])
    from daemons_companion.runtime.release import (
        CompanionRuntimeLayout,
        verify_current_release,
    )

    runtime = CompanionRuntimeLayout(
        scope.home, source, scope, Path(b["memory"]).name == ".daemon"
    )
    if verify_current_release(runtime) != source or runtime.memory_root != Path(
        b["memory"]
    ):
        raise ValueError("The shared runtime is not the verified active release.")
    args = SimpleNamespace(
        release=str(source),
        python=b["python"],
        memory_root=b["memory"],
        state_root=b["state"],
        provider_config=str(state / "config/provider.json"),
        runtime_revision=source.name,
        user_id=b["user_id"],
        installation_attempt=str(scope.attempt),
    )
    background_entry._configure(args)
    ref = host_reference(Path(record["profile"]))
    from daemon_core.installation_profile import (
        CONTINUITY_RECEIPTS, DURABLE_AFTER_TURN, HOST_VOICE,
    )
    env = dict(os.environ)
    env.update(
        {
            "DAEMON_CORE_HOST_REF": ref,
            "DAEMON_CORE_HOST_BINDINGS_DIR": str(state / "host-bindings"),
            **DURABLE_AFTER_TURN,
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": b["spool"],
            **CONTINUITY_RECEIPTS,
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": b["receipts"],
            "DAEMON_CORE_OPENCLAW_AGENT_DB": str(
                Path(record["profile"]) / "agents/main/agent/openclaw-agent.sqlite"
            ),
            "DAEMON_CORE_OPENCLAW_HEALTH_FILE": str(
                state / ("openclaw-" + ref + "-health.json")
            ),
            **HOST_VOICE,
            "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS": env.get(
                "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS", "openclaw"
            ),
        }
    )
    from daemons_companion.runtime.bootstrap import private_runtime_path_environment

    environment = private_runtime_path_environment(env)
    from daemon_core.installation_profile import require_profile

    require_profile(
        environment, user_ids=(b["user_id"],), memory_root=Path(b["memory"])
    )
    return environment


def run_gateway(argv, environment, profile):
    """Keep the scheduled parent alive and bind its Windows children to its life."""
    if os.name != "nt":
        os.execve(argv[0], argv, environment)
        return
    import subprocess
    from daemons_companion.runtime.windows_job import OwnedProcessJob
    from daemons_companion.runtime.bootstrap import stop_process_tree

    log_path = profile.parent / "logs/gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log, OwnedProcessJob() as owned:
        child = subprocess.Popen(
            argv,
            env=environment,
            cwd=profile.parent,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            owned.assign(child)
            code = child.wait()
        finally:
            if child.poll() is None:
                stop_process_tree(child, environment)
    if code:
        raise SystemExit(code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("api", "gateway"))
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    record, execution = load_record(args.record)
    if args.service == "api":
        env = api_environment(record)
        os.environ.clear()
        os.environ.update(env)
        from daemons_companion.runtime.service_entry import _run_openclaw_api

        _run_openclaw_api("127.0.0.1", record["api_port"])
    else:
        config = json.loads(
            checked_file(execution.profile / "openclaw.json", 2 * 1024 * 1024)
        )
        port = config.get("gateway", {}).get("port")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Invalid gateway port.")
        env = host_environment(execution.profile)
        env["PATH"] = (
            str(Path(execution.argv[0]).parent)
            + os.pathsep
            + env.get("PATH", os.defpath)
        )
        run_gateway(
            [
                *execution.argv,
                "gateway",
                "run",
                "--port",
                str(port),
                "--bind",
                "loopback",
            ],
            env,
            execution.profile,
        )


if __name__ == "__main__":
    main()
