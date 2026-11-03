"""Private OpenClaw gateway for a first daemon, with no Hermes dependency."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from daemons_companion.runtime.services import (
    BackgroundJob,
    OPENCLAW_GATEWAY_JOB,
    render_job_definition,
    scoped_job_code,
)
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.openclaw.activation import OpenClawControlState
from daemons_companion.openclaw.host_install import (
    OpenClawHostLayout,
    checked_file,
    verify_installed,
)
from daemons_companion.runtime.release import verify_current_release
from daemons_companion.runtime.environment import (
    release_environment_interpreter,
    resolve_release_environment,
)


# Each probe starts the whole OpenClaw CLI. On the owner's Windows PC
# (2026-10-25, 8 threads, about 77 % busy while the gateway booted) a live
# `gateway health` answered ok in 21.0 s and `plugins info --runtime` answered
# loaded in 67-86 s; the former 20 s limit could never see either. The limits
# leave room above those measurements. See
# docs/evaluations/2026-10-25_windows_python_home.md.
HEALTH_TIMEOUT_SECONDS = 90
PLUGIN_TIMEOUT_SECONDS = 150
# A closed port costs nothing to check; waiting here keeps the readiness loop's
# 40 attempts spread over the gateway's boot instead of spent in seconds.
LISTEN_WAIT_SECONDS = 5


def host_for(scope):
    proof = scope.receipt()["host"]
    return OpenClawHostLayout(
        scope.attempt.parents[3],
        scope.installation_id,
        proof["target_os"],
        scope.attempt / "host",
    )


def private_npm_environment(host, *, prepare=False):
    """Expose the pinned npm to the native host, including under launchd."""
    import shlex
    from daemons_companion.openclaw.host_install import direct
    from daemon_core.memory.integrity import atomic_write_text

    directory = direct(host.attempt / "tools")
    if host.target_os == "macos":
        launchers = {
            directory / "npm": "#!/bin/sh\nexec "
            + shlex.quote(str(host.node))
            + " "
            + shlex.quote(str(host.npm))
            + ' "$@"\n'
        }
    else:
        launchers = {
            directory / "npm.cmd": '@echo off\r\nsetlocal DisableDelayedExpansion\r\n"'
            + str(host.node).replace("%", "%%")
            + '" "'
            + str(host.npm).replace("%", "%%")
            + '" %*\r\n'
        }
    for name in ("empty.npmrc", "empty-global.npmrc"):
        launchers[host.attempt / name] = ""
    if prepare:
        directory.mkdir(mode=0o700, exist_ok=True)
    for path, content in launchers.items():
        direct(path)
        if prepare and not path.exists():
            atomic_write_text(path, content)
            if host.target_os == "macos" and path.name == "npm":
                path.chmod(0o700)
        if checked_file(path, 65536) != content.encode():
            raise ValueError("The private npm launcher or settings changed.")
    return {
        "NPM_CONFIG_USERCONFIG": str(host.attempt / "empty.npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(host.attempt / "empty-global.npmrc"),
        "NPM_CONFIG_CACHE": str(host.attempt / "npm-cache"),
        "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
    }


def host_environment(scope):
    from daemons_companion.runtime.bootstrap import (
        clean_environment,
        private_home_environment,
    )
    from daemons_companion.setup.credentials import system_credential_store
    from daemon_core.api_providers import API_PROVIDERS, validate_key

    scope.validate_home(scope.home)
    host = host_for(scope)
    verify_installed(host)
    record = json.loads(checked_file(scope.attempt / "openclaw-connection.json", 8192))
    if (
        not isinstance(record, dict)
        or set(record) != {"schema", "credential", "kind"}
        or record.get("schema") != "daemon.openclaw-connection.v1"
        or record.get("kind") not in {"codex_cli", "mistral_api", "openai_api", "anthropic_api", "xai_api"}
    ):
        raise ValueError("Invalid OpenClaw connection record.")
    secret = json.loads(system_credential_store().get(record["credential"]) or "{}")
    if secret.get("installation_id") != scope.installation_id:
        raise ValueError("OpenClaw credential belongs to another installation.")
    env = clean_environment(scope.attempt)
    env.update(private_home_environment(scope.attempt / "os-home"))
    env.update(
        OPENCLAW_STATE_DIR=str(scope.profile),
        OPENCLAW_CONFIG_PATH=str(scope.profile / "openclaw.json"),
        TELEGRAM_BOT_TOKEN=secret["token"],
        DAEMONS_GATEWAY_TOKEN=secret["gateway"],
    )
    if record["kind"] != "codex_cli":
        env[API_PROVIDERS[record["kind"]].secret_name] = validate_key(secret["key"])
    env.update(private_npm_environment(host))
    # launchd may supply no PATH; the native gateway also uses system utilities.
    fallback = (
        "/usr/bin:/bin:/usr/sbin:/sbin" if host.target_os == "macos" else os.defpath
    )
    env["PATH"] = os.pathsep.join(
        (str(host.attempt / "tools"), str(host.node.parent), env.get("PATH", fallback))
    )
    env.pop("HERMES_HOME", None)
    return env


class PrivateOpenClawControl:
    def __init__(self, host, runtime, environment, scheduler, port):
        self.host, self.runtime, self.environment, self.scheduler, self.port = (
            host,
            runtime,
            environment,
            scheduler,
            port,
        )
        self.openclaw_state = host.profile
        runtime.installation_scope.validate_namespace(scheduler.job_namespace)
        self.code = scoped_job_code(OPENCLAW_GATEWAY_JOB, host.installation_id)
        self.definition = None
        self.probes = {}
        self.plugin_loaded = False
        if scheduler.capture(self.code) is not None:
            raise ValueError("A gateway service already exists; recovery is required.")

    def job(self):
        release = verify_current_release(self.runtime)
        python = release_environment_interpreter(
            resolve_release_environment(self.environment), self.host.target_os
        )
        from daemons_companion.runtime.bootstrap import _source_io_path

        path = _source_io_path if self.host.target_os == "windows" else lambda p: p
        bootstrap = (
            "import runpy,sys;sys.dont_write_bytecode=True;"
            f"sys.path.insert(0,{str(path(release))!r});sys.path.insert(0,{str(path(release / 'src'))!r});"
            "runpy.run_module('daemons_companion.openclaw.gateway',run_name='__main__')"
        )
        return BackgroundJob(
            self.code,
            str(path(python)),
            ("-I", "-B", "-c", bootstrap, "--attempt", str(self.host.attempt)),
            str(self.host.attempt),
            str(self.host.attempt / "logs/gateway.log"),
            True,
        )

    def state(self):
        capture = self.scheduler.capture(self.code)
        if capture is not None:
            from daemons_companion.hosts.attachment_services import capture_matches

            if self.definition is None or not capture_matches(
                capture, self.definition, self.host.target_os
            ):
                raise RuntimeError("Gateway service changed.")
        # Registered-but-crashed gateways must also be removed during rollback.
        return OpenClawControlState("running" if capture is not None else "stopped")

    def start_gateway(self):
        if self.scheduler.capture(self.code) is not None:
            raise RuntimeError("Gateway service appeared after confirmation.")
        job = self.job()
        self.definition = render_job_definition(job, self.host.target_os)
        self.scheduler.install(job, self.definition)

    def stop_gateway(self):
        self.state()
        self.scheduler.restore(self.code, None)

    def restart_gateway(self):
        self.stop_gateway()
        self.start_gateway()

    def _listening(self):
        """Wait briefly for the gateway listener, without starting the CLI."""
        deadline = time.monotonic() + LISTEN_WAIT_SECONDS
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.5)

    def _probe(self, *args, timeout):
        name = " ".join(args[:2])
        # Each probe starts the full OpenClaw CLI. While the gateway still boots
        # it cannot answer, and the CLI competes with it for the machine.
        if not self._listening():
            self.probes[name] = f"{name}: gateway port {self.port} not listening yet"
            return {}
        try:
            result = subprocess.run(
                [str(self.host.node), str(self.host.entry), *args],
                env=host_environment(self.runtime.installation_scope),
                cwd=self.host.attempt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            # Probe answers only: status fields, never the gateway's chat log.
            self.probes[name] = (
                f"{name} (exit {result.returncode}): "
                + (result.stdout[-1500:] + "\n" + result.stderr[-1500:]).strip()
            )
            data = json.loads(result.stdout) if result.returncode == 0 else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            self.probes[name] = f"{name}: {type(error).__name__}"
            return {}

    def readiness_detail(self):
        return "\n".join(self.probes.values())

    def gateway_ready(self):
        data = self._probe(
            "gateway", "health", "--json", "--port", str(self.port), "--timeout", "1000",
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
        if data.get("ok") is not True:
            return False
        config = json.loads(
            checked_file(self.host.profile / "openclaw.json", 2 * 1024 * 1024)
        )
        if config.get("channels", {}).get("telegram", {}).get("enabled") is True:
            channel = data.get("channels", {}).get("telegram", {})
            return (
                channel.get("running") is True
                and channel.get("configured") is True
                and not channel.get("terminalDisconnect")
                and channel.get("lifecycle")
                not in {"starting", "recovering", "stopped", "blocked"}
                and channel.get("probe", {}).get("ok") is not False
            )
        return True

    def plugin_ready(self):
        # The plugin loads once with the gateway; a loaded answer is not probed
        # again, since each probe costs a minute on a busy Windows PC.
        if getattr(self, "plugin_loaded", False):
            return True
        data = self._probe(
            "plugins", "info", "daemon-harness", "--json", "--runtime",
            timeout=PLUGIN_TIMEOUT_SECONDS,
        )
        plugin = data.get("plugin", data)
        self.plugin_loaded = (
            isinstance(plugin, dict)
            and plugin.get("id") == "daemon-harness"
            and plugin.get("status") == "loaded"
        )
        return self.plugin_loaded


def api_ready(port, *, runtime):
    from urllib.request import urlopen
    from urllib.error import HTTPError
    from daemon_core.continuity import technical_ref
    from daemons_companion.runtime.bootstrap import _source_io_path

    try:
        try:
            response = urlopen(f"http://127.0.0.1:{port}/ready", timeout=1)
        except HTTPError as error:
            if error.code != 503:
                return False
            response = error
        with response:
            data = json.loads(response.read(65537))
        return data.get("runtime_revision") == verify_current_release(
            runtime
        ).name and data.get("refs", {}).get("memory_root") == technical_ref(
            str(_source_io_path(runtime.memory_root))
        )
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", type=Path, required=True)
    args = parser.parse_args()
    scope = InstallationScope(args.attempt)
    scope.validate_home(scope.home)
    host = host_for(scope)
    config = json.loads(checked_file(scope.profile / "openclaw.json", 2 * 1024 * 1024))
    from daemons_companion.hosts.attachment_entry import run_gateway

    run_gateway(
        [
            str(host.node),
            str(host.entry),
            "gateway",
            "run",
            "--port",
            str(config["gateway"]["port"]),
            "--bind",
            "loopback",
        ],
        host_environment(scope),
        scope.profile,
    )


if __name__ == "__main__":
    main()
