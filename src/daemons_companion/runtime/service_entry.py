"""Private-runtime entry points used by native per-user schedulers."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

from daemon_core.memory.path_identity import path_identity



_SAFE_INHERITED_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_REVISION = re.compile(r"^[0-9a-f]{64}$")


def _direct_absolute(path: str, label: str, *, directory: bool = False) -> Path:
    value = Path(path_identity(path))
    if not value.is_absolute() or value.is_symlink() or value.resolve() != value:
        raise ValueError(f"{label} must be an absolute direct path.")
    if directory and not value.is_dir():
        raise ValueError(f"{label} must be an existing directory.")
    return value


def _configure(args: argparse.Namespace) -> tuple[Path, Path]:
    release = _direct_absolute(args.release, "Runtime release", directory=True)
    memory = _direct_absolute(args.memory_root, "Memory root", directory=True)
    state = _direct_absolute(args.state_root, "State root", directory=True)
    provider = _direct_absolute(args.provider_config, "Provider config")
    python = Path(path_identity(args.python))
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Private interpreter must be an absolute file path.")
    data = release.parents[2]
    if release.parent != data / "runtime" / "releases":
        raise ValueError("Runtime release is outside the private runtime layout.")
    if release.name != args.runtime_revision or not _REVISION.fullmatch(release.name):
        raise ValueError("Runtime revision does not match the active release.")
    expected_memory = data / "memory"
    if memory == expected_memory / ".daemon":
        from daemon_core.memory.library_layout import vault_root

        if vault_root(memory) != expected_memory:
            raise ValueError(
                "The private root-folder vault must have its exact layout marker."
            )
    elif memory != expected_memory:
        raise ValueError("Memory and state roots must belong to the private runtime.")
    if state != data / "state":
        raise ValueError("Memory and state roots must belong to the private runtime.")
    if provider.parent != state / "config":
        raise ValueError("Provider config must belong to the private state root.")
    if provider.is_symlink() or not provider.is_file():
        raise ValueError("Provider config must be a direct regular file.")
    from daemons_companion.runtime.environment import environment_directory_name

    directory = environment_directory_name(
        release.name, "windows" if os.name == "nt" else "macos"
    )
    environment_root = data / "runtime" / "environments" / directory
    legacy_root = data / "runtime" / "environments" / release.name
    if python == legacy_root / "venv/Scripts/python.exe":
        environment_root = legacy_root
    expected_environment = environment_root / "venv"
    expected_python = expected_environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if python != expected_python or not python.resolve().is_relative_to(
        environment_root.resolve()
    ):
        raise ValueError("Private interpreter does not belong to the active release.")
    from daemons_companion.runtime.bootstrap import (
        _source_io_path,
        private_runtime_path_environment,
        same_interpreter,
    )

    if not same_interpreter(Path(sys.executable), python):
        raise ValueError(
            "Background entry must run with the declared private interpreter."
        )
    user_id = args.user_id.strip()
    if not user_id or len(user_id) > 128 or any(char in user_id for char in "\r\n\0"):
        raise ValueError("User id is invalid.")
    spool = state / "after-turn-spool"
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    from daemon_core.installation_profile import DURABLE_AFTER_TURN
    environment = {
        "DAEMON_CORE_SRC": str(release / "src"),
        "DAEMON_CORE_MEMORY_ROOT": str(memory),
        "DAEMON_CORE_STATE_ROOT": str(state),
        "DAEMON_CORE_PROVIDER_CONFIG": str(provider),
        "DAEMON_CORE_PYTHON": str(_source_io_path(python)),
        "DAEMON_CORE_USER_ID": user_id,
        "DAEMON_CORE_AFTER_TURN_USER_IDS": user_id,
        **DURABLE_AFTER_TURN,
        "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(spool),
        "DAEMON_CORE_RUNTIME_REVISION": args.runtime_revision,
        "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION": args.runtime_revision,
        "PYTHON_BIN": str(_source_io_path(python)),
        "PYTHONPATH": os.pathsep.join(
            (str(_source_io_path(release / "src")), str(_source_io_path(release)))
        ),
    }
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_INHERITED_ENVIRONMENT
    }
    attempt = getattr(args, "installation_attempt", None)
    if attempt:
        from daemons_companion.runtime.private_environment import (
            private_process_environment,
        )
        from daemons_companion.runtime.installation_scope import InstallationScope

        scope = InstallationScope(Path(path_identity(attempt)))
        scope.validate_home(state.parents[1])
        inherited = private_process_environment(scope)
    os.environ.clear()
    os.environ.update(inherited)
    os.environ.update(private_runtime_path_environment(environment))
    from daemon_core.installation_profile import require_profile

    require_profile(os.environ, user_ids=(user_id,), memory_root=memory)
    return release, state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daemon Harness background entry")
    parser.add_argument(
        "service",
        choices=("after-turn", "nightly", "openclaw-api"),
    )
    parser.add_argument("--release", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--memory-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--provider-config", required=True)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--installation-attempt")
    parser.add_argument("--openclaw-state")
    parser.add_argument("--port", type=int)
    return parser


def _configure_openclaw_api(
    args: argparse.Namespace,
    release: Path,
    state: Path,
) -> tuple[str, int]:
    if (
        args.openclaw_state is None
        or not isinstance(args.port, int)
        or isinstance(args.port, bool)
        or not 1024 <= args.port <= 65535
    ):
        raise ValueError(
            "OpenClaw API requires its disposable state and a non-privileged port."
        )
    from daemons_companion.openclaw.profile import validate_openclaw_state
    from daemons_companion.runtime.installation_scope import InstallationScope
    attempt = getattr(args, "installation_attempt", None)
    openclaw_state = validate_openclaw_state(Path(path_identity(args.openclaw_state)),
        InstallationScope(Path(path_identity(attempt))) if attempt else None)
    if not (openclaw_state / "openclaw.json").is_file():
        raise ValueError("OpenClaw API requires a configured disposable profile.")
    companion_home = state.parents[1]
    try:
        openclaw_state.relative_to(companion_home)
    except ValueError:
        pass
    else:
        raise ValueError("OpenClaw state must remain separate from companion data.")
    user_id = args.user_id.strip()
    if attempt:
        from daemons_companion.hosts.attachment import host_reference
        os.environ["DAEMON_CORE_HOST_REF"] = host_reference(openclaw_state)
        os.environ["DAEMON_CORE_HOST_BINDINGS_DIR"] = str(state / "host-bindings")
    receipts = state / "continuity" / "shared"
    health = state / "openclaw-runtime-health.json"
    latency = state / "latency" / "openclaw.jsonl"
    for directory in (receipts, health.parent, latency.parent):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    from daemon_core.installation_profile import (
        CONTINUITY_RECEIPTS, HOST_VOICE, SCENE_CONTINUITY,
    )
    os.environ.update(
        {
            "DAEMON_CORE_SRC": str(release / "src"),
            **HOST_VOICE,
            **CONTINUITY_RECEIPTS,
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(receipts),
            **SCENE_CONTINUITY,
            "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS": user_id,
            "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS": os.environ.get(
                "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS", "openclaw"
            ),
            "DAEMON_CORE_LATENCY_LOG": str(latency),
            "DAEMON_CORE_OPENCLAW_AGENT_DB": str(
                openclaw_state / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
            ),
            "DAEMON_CORE_OPENCLAW_HEALTH_FILE": str(health),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return "127.0.0.1", args.port


def _run_openclaw_api(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=host,
        port=port,
        log_level="warning",
    )


def main() -> None:
    args = _parser().parse_args()
    release, _state = _configure(args)
    if args.service == "after-turn":
        from daemon_core.after_turn_daemon import run_daemon

        run_daemon()
        return
    if args.service == "openclaw-api":
        host, port = _configure_openclaw_api(args, release, _state)
        _run_openclaw_api(host, port)
        return
    from daemon_core.nightly_maintenance import run_nightly_cycle
    from daemons_companion.runtime.bootstrap import _source_io_path

    raise SystemExit(
        run_nightly_cycle(environment=os.environ, code_root=_source_io_path(release))
    )


if __name__ == "__main__":
    main()
    ("NO_PROXY",)
    ("SSL_CERT_DIR",)
    ("SSL_CERT_FILE",)
