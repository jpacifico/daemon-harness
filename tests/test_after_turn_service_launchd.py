from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


def test_neutral_launchd_service_has_one_owner_and_no_host_server():
    root = Path(__file__).resolve().parents[1]
    script = (
        root / "integrations" / "after_turn_service" / "service.sh"
    ).read_text(encoding="utf-8")

    assert 'LABEL="com.daemonharness.after-turn-worker"' in script
    assert "daemon_core.after_turn_daemon" in script
    assert "server.main:app" not in script
    assert "DAEMON_CORE_AFTER_TURN_OWNER</key><string>external" in script
    assert "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION" in script
    assert 'launchctl kickstart -k "gui/$(id -u)/$LABEL"' in script
    assert "for attempt in {1..5}" in script
    assert "if bootstrap_plist" in script
    assert "service-owner.lock" not in script
    assert "rollback" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd service targets macOS")
def test_neutral_launchd_install_reuses_shared_state_and_is_independent(
    tmp_path: Path,
):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    env_file = tmp_path / "runtime.env"
    env_file.write_text("DAEMON_CORE_USER_ID=personal\n", encoding="utf-8")
    env_file.chmod(0o600)
    memory_root = tmp_path / "memory"
    spool_root = tmp_path / "spool"
    memory_root.mkdir()
    spool_root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    openclaw_plist = agents / "com.daemonharness.openclaw-api.plist"
    with openclaw_plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "com.daemonharness.openclaw-api",
                "ProgramArguments": [str(fake_python)],
                "EnvironmentVariables": {
                    "DAEMON_CORE_ENV_FILE": str(env_file),
                    "DAEMON_CORE_MEMORY_ROOT": str(memory_root),
                    "DAEMON_CORE_AFTER_TURN_SPOOL": "canary",
                    "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(spool_root),
                },
            },
            stream,
        )

    for name, body in {
        "uname": "#!/bin/sh\necho Darwin\n",
        "launchctl": "#!/bin/sh\nexit 0\n",
    }.items():
        executable = fake_bin / name
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    for key in tuple(env):
        if key.startswith("DAEMON_CORE_"):
            env.pop(key)

    result = subprocess.run(
        [
            "/bin/bash",
            str(root / "integrations" / "after_turn_service" / "service.sh"),
            "install",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    installed = agents / "com.daemonharness.after-turn-worker.plist"
    with installed.open("rb") as stream:
        rendered = plistlib.load(stream)
    rendered_env = rendered["EnvironmentVariables"]
    assert rendered["ProgramArguments"] == [
        str(fake_python),
        "-m",
        "daemon_core.after_turn_daemon",
    ]
    assert rendered["WorkingDirectory"] == str(root)
    assert rendered_env["DAEMON_CORE_ENV_FILE"] == str(env_file)
    assert rendered_env["DAEMON_CORE_MEMORY_ROOT"] == str(memory_root)
    assert rendered_env["DAEMON_CORE_AFTER_TURN_SPOOL_DIR"] == str(spool_root)
    assert rendered_env["DAEMON_CORE_AFTER_TURN_OWNER"] == "external"
    assert rendered_env["DAEMON_CORE_AFTER_TURN_SERVICE_REVISION"]
