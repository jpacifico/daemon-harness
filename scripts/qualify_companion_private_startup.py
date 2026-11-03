"""Native Mac startup checks using synthetic credentials and disposable state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("This qualification requires macOS.")
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "src"))
    from daemons_companion.setup.credentials import MacOSKeychainStore
    from daemons_companion.hermes.gateway import PrivateHermesControl
    store = MacOSKeychainStore()
    reference = "provider-" + str(uuid.uuid4())
    protected = json.dumps({"schema": "daemon.companion-credential.v1", "owner_token": "synthetic", "value": "synthetic-key-only"})
    started = time.monotonic()
    report = {"personal_services_changed": 0, "external_model_calls": 0, "telegram_messages": 0}
    worker = None
    try:
        store.put(reference, protected)
        with tempfile.TemporaryDirectory(prefix="daemon-native-startup-") as directory:
            root = Path(directory).resolve()
            memory, state, home, profile = [root / name for name in ("memory", "state", "home", "profile")]
            for path in (memory, state, home, profile):
                path.mkdir(mode=0o700)
            env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
            env.update({"HOME": str(home), "HERMES_HOME": str(profile), "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo))),
                        "DAEMON_CORE_MEMORY_ROOT": str(memory), "DAEMON_CORE_STATE_ROOT": str(state),
                        "DAEMON_CORE_ENV_FILE": str(root / "empty.env"), "DAEMON_CORE_AFTER_TURN_USER_IDS": "12345",
                        "DAEMON_CORE_USER_ID": "12345", "DAEMON_CORE_AFTER_TURN_OWNER": "external",
                        "DAEMON_CORE_AFTER_TURN_SPOOL": "on", "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(state / "spool"),
                        "DAEMON_CORE_RUNTIME_REVISION": "synthetic-native-startup", "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION": "synthetic-native-startup"})
            (root / "empty.env").touch()
            check = "from daemons_companion.setup.credentials import MacOSKeychainStore;import sys;assert MacOSKeychainStore().get(sys.argv[1]) is not None"
            subprocess.run([sys.executable, "-B", "-c", check, reference], env=env, check=True, capture_output=True, timeout=40)
            report["private_home_keychain"] = True
            provider = state / "provider.json"
            provider.write_text(json.dumps({"schema": "daemon.companion-provider.v1", "kind": "mistral_api", "model": "mistral-large-4", "base_url": "", "reasoning": {"routing": "medium", "conversation": "medium", "memory": "high"}, "credential": {"name": "MISTRAL_API_KEY", "reference": reference}}))
            provider.chmod(0o600)
            env["DAEMON_CORE_PROVIDER_CONFIG"] = str(provider)
            # A real neutral worker starts with an empty queue. Block every socket
            # connect so an accidental external operation fails instead of escaping.
            worker_code = "import socket;exec(\"def blocked(*args, **kwargs): raise RuntimeError('Network forbidden in startup qualification')\");socket.socket.connect=blocked;from daemon_core.after_turn_daemon import run_daemon;run_daemon()"
            with (root / "worker.log").open("w") as log:
                worker_started = time.monotonic()
                worker = subprocess.Popen([sys.executable, "-B", "-c", worker_code], env=env, cwd=root, stdout=log, stderr=log)
                from daemon_core.after_turn_service import read_after_turn_service_status
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    status = read_after_turn_service_status(expected_revision="synthetic-native-startup", memory_root=memory, spool_root=state / "spool")
                    if status["ok"]:
                        break
                    if worker.poll() is not None:
                        raise RuntimeError("Synthetic worker failed: " + (root / "worker.log").read_text()[-3000:])
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Synthetic worker readiness timed out: " + str(status))
                report["native_worker_ready_seconds"] = round(time.monotonic() - worker_started, 3)
                worker.terminate(); worker.wait(timeout=15); worker = None
            # Run the actual pinned Hermes plugin CLI with an unrelated profile.
            config = profile / "config.yaml"
            config.write_text("model:\n  provider: mistral\n  default: mistral-large-4\n")
            original = config.read_bytes()
            plugins = profile / "plugins"; plugins.mkdir()
            for name, folder in (("daemon_core", "hermes_plugin"), ("daemon_voice", "hermes_voice_plugin")):
                (plugins / name).symlink_to(repo / "integrations" / folder, target_is_directory=True)
            control = object.__new__(PrivateHermesControl)
            control.host_layout = SimpleNamespace(profile=profile)
            control.scope = SimpleNamespace(validate_profile=lambda path: None)
            control._config_snapshot = None
            snapshot = root / "snapshot"; snapshot.mkdir()
            control.track_config(snapshot, create=True)
            for action in ("enable", "disable"):
                for name in ("daemon_core", "daemon_voice"):
                    control._check_config_snapshot()
                    result = subprocess.run([str(args.hermes_python), "-B", "-c", "from hermes_cli.main import main;main()", "plugins", action, name], env=env, cwd=root, capture_output=True, timeout=45)
                    if result.returncode:
                        raise RuntimeError("Native plugin command failed: " + result.stderr.decode(errors="replace")[-1000:])
                    control._record_config_snapshot()
            report["native_cli_changes_configuration"] = config.read_bytes() != original
            control.restore_config()
            assert config.read_bytes() == original
            report["native_cli_exact_restoration"] = True
            snapshot2 = root / "snapshot2"; snapshot2.mkdir(); control.track_config(snapshot2, create=True)
            config.write_bytes(original + b"external_change: true\n")
            changed = config.read_bytes()
            try:
                control.restore_config()
            except RuntimeError:
                assert config.read_bytes() == changed
                report["concurrent_edit_preserved"] = True
            else:
                raise AssertionError("Concurrent edit was overwritten")
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate(); worker.wait(timeout=15)
        store.delete(reference)
    assert store.get(reference) is None
    report.update(status="passed", elapsed_seconds=round(time.monotonic() - started, 3), synthetic_credential_removed=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
