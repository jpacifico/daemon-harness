"""Hermes gateway control through explicitly scoped companion scheduler jobs."""
from __future__ import annotations

import json
import base64
import os
from pathlib import Path
import subprocess

from daemons_companion.runtime.services import (
    BackgroundJob, HERMES_GATEWAY_JOB, render_job_definition, scoped_job_code,
)
from daemons_companion.hermes.activation import HermesCliBackend, HermesCommandResult, HermesControlState
from daemons_companion.runtime.private_environment import private_process_environment
from daemons_companion.runtime.release import verify_current_release


_GATEWAY_STATUS_SCRIPT = (
    "import json; from gateway.status import get_running_pid, read_runtime_status; "
    "print(json.dumps({'pid':get_running_pid(), 'status':read_runtime_status()}))"
)


def _gateway_ready(probe):
    status = probe.get("status") or {}
    return bool(probe.get("pid") and status.get("gateway_state") == "running"
                and status.get("platforms", {}).get("telegram", {}).get("state") == "connected"
                and probe.get("continuity_profile_ok", True))


def _profile_ready(scope, pid):
    from daemon_core.installation_profile import profile_status
    expected = profile_status(private_process_environment(scope, hermes=True))
    if expected is None:
        return True
    path = scope.attempt / "gateway-continuity.json"
    try:
        if path.is_symlink() or path.resolve() != path or path.stat().st_size > 8192:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        return bool(expected["ok"] and pid and value == {"pid": pid, "profile": expected})
    except (OSError, ValueError):
        return False


def private_gateway_status(layout):
    from daemons_companion.runtime.installation_scope import InstallationScope
    scope = InstallationScope(layout.attempt)
    environment = private_process_environment(scope, hermes=True, credentials=False)
    result = subprocess.run([layout.launch_python, "-B", "-c", _GATEWAY_STATUS_SCRIPT],
                            cwd=layout.attempt, env=environment, capture_output=True,
                            text=True, timeout=15, check=False)
    try:
        probe = json.loads(result.stdout) if not result.returncode else {}
        probe["continuity_profile_ok"] = _profile_ready(scope, probe.get("pid"))
        return probe
    except ValueError:
        return {}


def wait_private_gateway_ready(layout, *, timeout=120):
    import time
    deadline = time.monotonic() + timeout
    while True:
        try:
            if _gateway_ready(private_gateway_status(layout)):
                return
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Private services are retained, but Telegram has not reconnected yet. Reopen the assistant to retry.")
        time.sleep(1)


class PrivateHermesControl(HermesCliBackend):
    def __init__(self, layout, runtime, scheduler, *, runner=None):
        self.host_layout = layout
        self.runtime = runtime
        self.scope = runtime.installation_scope
        self.scope.validate_namespace(scheduler.job_namespace)
        self.scheduler = scheduler
        self.job_code = scoped_job_code(HERMES_GATEWAY_JOB, scheduler.job_namespace)
        self.before = scheduler.capture(self.job_code)
        self.expected_definition = None
        self._config_snapshot = None
        if self.before is not None:
            raise ValueError("A private gateway job already exists. Resume it; do not overwrite it.")
        super().__init__(layout.executable, layout.profile, runner=runner or self._native)

    def _native(self, command, _environment):
        environment = private_process_environment(self.scope, hermes=True, credentials=False)
        self._check_config_snapshot()
        if str(command[0]) == str(self.host_layout.executable):
            from daemons_companion.runtime.bootstrap import _source_io_path
            release = verify_current_release(self.runtime)
            bridge = _source_io_path(release / "integrations/hermes_plugin/companion_control.py")
            command = (self.host_layout.launch_python, "-B", str(bridge), *command[1:])
        result = subprocess.run(command, env=environment, cwd=self.host_layout.attempt,
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=45, check=False)
        self._record_config_snapshot()
        return HermesCommandResult(result.returncode, result.stdout, result.stderr)

    def _config_bytes(self):
        path = self.host_layout.profile / "config.yaml"
        if (path.is_symlink() or path.resolve() != path or not path.is_file()
                or path.stat().st_size > 1024 * 1024):
            raise RuntimeError("Private host configuration is unavailable.")
        return path.read_bytes()

    def track_config(self, directory, *, create=False):
        self.scope.validate_profile(self.host_layout.profile)
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError("Native configuration snapshot must be direct.")
        from daemons_companion.runtime.bootstrap import _source_io_path
        directory = _source_io_path(directory)
        before = directory / "native-config.before"
        expected = directory / "native-config.sha256"
        if create:
            if before.exists() or expected.exists():
                raise RuntimeError("Native configuration snapshot already exists.")
            from daemons_companion.hermes.profile import _write_private_bytes
            _write_private_bytes(before, self._config_bytes(), 0o600)
        elif not before.exists():
            return  # Historical snapshots remain readable, without new authority.
        if before.is_symlink() or not before.is_file() or before.stat().st_size > 1024 * 1024:
            raise RuntimeError("Native configuration snapshot is invalid.")
        self._config_snapshot = directory
        if create:
            self._record_config_snapshot()
        else:
            self._check_config_snapshot()

    def _check_config_snapshot(self):
        import hashlib
        if getattr(self, "_config_snapshot", None) is None:
            return
        path = self._config_snapshot / "native-config.sha256"
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != 64
                or path.read_text() != hashlib.sha256(self._config_bytes()).hexdigest()):
            raise RuntimeError("Native host configuration changed concurrently; it is preserved.")

    def _record_config_snapshot(self):
        import hashlib
        from daemon_core.memory.integrity import atomic_write_text
        if getattr(self, "_config_snapshot", None) is not None:
            atomic_write_text(self._config_snapshot / "native-config.sha256",
                              hashlib.sha256(self._config_bytes()).hexdigest())

    def restore_config(self):
        if getattr(self, "_config_snapshot", None) is not None:
            self._check_config_snapshot()
            before = self._config_snapshot / "native-config.before"
            from daemons_companion.hermes.profile import _write_private_bytes
            import stat
            target = self.host_layout.profile / "config.yaml"
            _write_private_bytes(target, before.read_bytes(), stat.S_IMODE(target.stat().st_mode))
            self._config_snapshot = None

    def _probe(self):
        # Native Hermes checks the PID identity without kill(pid, 0) on Windows.
        result = self._runner((self.host_layout.launch_python, "-c", _GATEWAY_STATUS_SCRIPT), {})
        if result.returncode:
            return {}
        try:
            probe = json.loads(result.stdout)
            probe["continuity_profile_ok"] = _profile_ready(self.scope, probe.get("pid"))
            return probe
        except ValueError:
            return {}

    def state(self):
        inventory = super().state()
        # Registered includes starting/failed processes so rollback always removes
        # a job created by this transaction; gateway_ready separately proves life.
        registered = self.scheduler.capture(self.job_code) is not None
        return HermesControlState(inventory.plugins, "running" if registered else "stopped")

    def job(self):
        return private_gateway_job(self.host_layout, self.runtime, self.scheduler.job_namespace)

    def start_gateway(self):
        self.scope.validate_home(self.runtime.companion_home)
        if self.scheduler.capture(self.job_code) is not None or self._probe().get("pid"):
            raise RuntimeError("A private gateway already exists; it will not be replaced.")
        job = self.job()
        self.expected_definition = render_job_definition(job, self.host_layout.target_os)
        self.scheduler.install(job, self.expected_definition)

    def stop_gateway(self):
        self.scope.validate_home(self.runtime.companion_home)
        current = self.scheduler.capture(self.job_code)
        if current is not None:
            if self.expected_definition is None or not self._same_definition(current):
                raise RuntimeError("Gateway job changed concurrently; removal refused.")
            self.scheduler.restore(self.job_code, None)

    def _same_definition(self, capture):
        if self.host_layout.target_os == "windows":
            from daemons_companion.runtime.services import _load_windows_task_capture, _windows_definition_signature
            actual = _load_windows_task_capture(capture)["definition"]
            return _windows_definition_signature(actual) == _windows_definition_signature(self.expected_definition)
        payload = json.loads(capture)
        return base64.b64decode(payload["definition"]) == self.expected_definition

    def restart_gateway(self):
        self.stop_gateway()
        self.start_gateway()

    def gateway_ready(self):
        return _gateway_ready(self._probe())


def private_gateway_job(layout, runtime, namespace, *, legacy=False):
    scope = runtime.installation_scope
    scope.validate_namespace(namespace)
    scope.validate_home(runtime.companion_home)
    release = verify_current_release(runtime)
    from daemons_companion.runtime.bootstrap import _source_io_path
    source = release / "src"
    if layout.target_os == "windows" and not legacy:
        from daemons_companion.hermes.profile import _read_env_values
        environment_file = layout.profile / ".env"
        if (environment_file.is_symlink() or environment_file.resolve() != environment_file
                or not environment_file.is_file() or environment_file.stat().st_size > 1048576):
            raise ValueError("Private Hermes source declaration is unavailable.")
        declared = _read_env_values(environment_file).get("DAEMON_CORE_SRC", "")
        if declared not in {str(source), str(_source_io_path(source))}:
            raise ValueError("Private Hermes source differs from the owned runtime.")
        # Source is one of the owned roots repaired together in the profile.
        # Use its extended spelling for imports and the native provenance check;
        # the release files and plugin copies remain byte-for-byte unchanged.
        source = _source_io_path(source)
    script = f"import sys;sys.path.insert(0,{str(source)!r});"
    if not legacy:
        from daemons_companion.runtime.process_settings import private_diagnostic_setup
        # Load the same owned environment before importing modules whose paths
        # are bound at import time. The retained run_gateway then performs its
        # normal identity checks and starts the native host in this process.
        script += (
            "import os;from pathlib import Path;"
            "from daemons_companion.runtime.installation_scope import InstallationScope;"
            "from daemons_companion.runtime.private_environment import private_process_environment;"
            "scope=InstallationScope(Path(sys.argv[1]));"
            "environment=private_process_environment(scope,hermes=True);"
            "os.environ.clear();os.environ.update(environment);"
            "from dotenv import load_dotenv;load_dotenv(scope.profile/'.env',override=False);"
            + private_diagnostic_setup(runtime.state_root)
        )
    script += ("from daemons_companion.hermes.gateway import run_gateway;"
               "run_gateway(sys.argv[1],sys.argv[2])")
    return BackgroundJob(scoped_job_code(HERMES_GATEWAY_JOB, namespace), str(layout.python) if legacy else layout.launch_python,
                         ("-B", "-c", script, str(scope.attempt), str(layout.executable),
                          "gateway", "run", "--external-supervisor"),
                         str(layout.attempt), str(runtime.state_root / "gateway.log"), True)


def run_gateway(attempt: str, executable: str):
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.runtime.acquisition import _RELEASES
    scope = InstallationScope(Path(attempt))
    environment = private_process_environment(scope, hermes=True)
    suffix = "Scripts/hermes.exe" if os.name == "nt" else "bin/hermes"
    expected = scope.attempt / "source" / ("hermes-agent-" + _RELEASES["hermes"].revision) / ".venv" / suffix
    if Path(executable) != expected or not expected.is_file():
        raise ValueError("Gateway executable is outside the owned installation.")
    os.environ.clear()
    os.environ.update(environment)
    from dotenv import load_dotenv
    load_dotenv(scope.profile / ".env", override=False)
    from daemon_core.installation_profile import profile_status, require_profile
    owner = environment.get("DAEMON_CORE_INSTALLATION_PROFILE_USER_ID", "")
    memory = Path(os.environ.get("DAEMON_CORE_MEMORY_ROOT", ""))
    require_profile(os.environ, user_ids=(owner,), memory_root=memory)
    profile = profile_status(os.environ, user_ids=(owner,), memory_root=memory)
    if profile is not None:
        from daemon_core.memory.integrity import atomic_write_text
        path = scope.attempt / "gateway-continuity.json"
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("Gateway continuity receipt must remain private and direct.")
        atomic_write_text(path, json.dumps({"pid": os.getpid(), "profile": profile}) + "\n")
        path.chmod(0o600)
    os.chdir(scope.attempt)
    # Stay in the same supervised process on Windows too (execve spawns there).
    import sys
    from hermes_cli.main import main
    sys.argv = [str(expected), "gateway", "run", "--external-supervisor"]
    main()
