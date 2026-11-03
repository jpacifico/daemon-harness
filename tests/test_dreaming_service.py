from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "integrations" / "dreaming_service" / "service.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_shared_nightly_service_is_idempotent_and_preserves_daemon_flags(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist_path = agents / "com.daemonharness.dreaming.plist"
    memory_root = tmp_path / "memory"
    state_root = tmp_path / "state"
    env_file = tmp_path / "dreaming.env"
    env_file.write_text("DREAMING_ENABLED=0\nDREAMING_OBSERVE_ONLY=1\n", encoding="utf-8")
    log_file = tmp_path / "logs" / "dreaming.log"
    existing = {
        "Label": "com.daemonharness.dreaming",
        "ProgramArguments": ["/bin/bash", "/old/repo/scripts/dreaming_cron.sh"],
        "WorkingDirectory": "/old/repo",
        "EnvironmentVariables": {
            "DAEMON_CORE_PYTHON": sys.executable,
            "DAEMON_CORE_ENV_FILE": str(env_file),
            "DAEMON_CORE_MEMORY_ROOT": str(memory_root),
            "DAEMON_CORE_STATE_ROOT": str(state_root),
            "DAEMON_CORE_RELATION_GARDENING": "active",
            "DAEMON_CORE_RELATION_GARDENING_USER_IDS": "test-user",
        },
        "StartCalendarInterval": {"Hour": 4, "Minute": 0},
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
    }
    with plist_path.open("wb") as stream:
        plistlib.dump(existing, stream)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl.log"
    _write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
    _write_executable(
        fake_bin / "launchctl",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHCTL_CALLS"\nexit 0\n',
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "LAUNCHCTL_CALLS": str(calls),
    }

    for _ in range(2):
        result = subprocess.run(
            [str(SERVICE), "install"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    with plist_path.open("rb") as stream:
        rendered = plistlib.load(stream)
    assert env_file.read_text(encoding="utf-8") == "DREAMING_ENABLED=0\nDREAMING_OBSERVE_ONLY=1\n"
    assert rendered["EnvironmentVariables"]["DAEMON_CORE_ENV_FILE"] == str(env_file)
    assert rendered["ProgramArguments"] == [
        "/bin/bash",
        str(ROOT / "scripts" / "dreaming_cron.sh"),
    ]
    assert rendered["WorkingDirectory"] == str(ROOT)
    assert rendered["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert rendered["EnvironmentVariables"]["DAEMON_CORE_PYTHON"] == sys.executable
    assert rendered["EnvironmentVariables"]["DAEMON_CORE_MEMORY_ROOT"] == str(
        memory_root
    )
    assert rendered["EnvironmentVariables"]["DAEMON_CORE_RELATION_GARDENING"] == "active"
    assert (
        rendered["EnvironmentVariables"]["DAEMON_CORE_RELATION_GARDENING_USER_IDS"]
        == "test-user"
    )

    status = subprocess.run(
        [str(SERVICE), "status"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr or status.stdout
    assert "Alignement du code nocturne : correct" in status.stdout

    uninstall = subprocess.run(
        [str(SERVICE), "uninstall"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert uninstall.returncode == 0, uninstall.stderr or uninstall.stdout
    assert not plist_path.exists()


def test_shared_nightly_service_installs_one_stable_linux_cron_entry(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    memory_root = tmp_path / "memory"
    state_root = tmp_path / "state"
    env_file = tmp_path / "dreaming.env"
    log_file = tmp_path / "logs" / "dreaming.log"
    cron_file = tmp_path / "crontab"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
    _write_executable(
        fake_bin / "crontab",
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  -l) [ -f "$CRON_FILE" ] || exit 1; cat "$CRON_FILE" ;;\n'
        '  -) cat > "$CRON_FILE" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CRON_FILE": str(cron_file),
        "DREAMING_PYTHON": sys.executable,
        "DREAMING_MEMORY_ROOT": str(memory_root),
        "DREAMING_STATE_ROOT": str(state_root),
        "DREAMING_ENV_FILE": str(env_file),
        "DREAMING_LOG_FILE": str(log_file),
        "DREAMING_CRON_HOUR": "08",
    }

    for _ in range(2):
        result = subprocess.run(
            [str(SERVICE), "install"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    lines = [line for line in cron_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0].startswith("0 8 * * * ")
    assert str(ROOT / "scripts" / "dreaming_cron.sh") in lines[0]
    assert "daemonharness-dreaming" in lines[0]
