from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


def test_launchd_service_does_not_mask_dotenv_with_empty_values():
    root = Path(__file__).resolve().parents[1]
    script = (root / "integrations" / "openclaw_plugin" / "service.sh").read_text(
        encoding="utf-8"
    )

    assert 'MEMORY_ROOT="${DAEMON_CORE_MEMORY_ROOT:-}"' in script
    assert '<key>DAEMON_CORE_MEMORY_ROOT</key>' not in script
    assert '<key>DAEMON_CORE_CONTINUITY_RECEIPTS</key>' not in script
    assert 'existing_plist_value "ProgramArguments.0"' in script
    assert "EnvironmentVariables.DAEMON_CORE_RELATION_GARDENING" in script
    assert '[[ -n "$value" ]] || return 0' in script
    assert 'insert_plist_env "DAEMON_CORE_RUNTIME_REVISION"' in script
    assert 'insert_plist_env "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION"' in script
    assert "http://127.0.0.1:8000/ready" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd service targets macOS")
def test_launchd_reinstall_preserves_external_state_overrides(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist_path = agents / "com.daemonharness.openclaw-api.plist"
    env_file = tmp_path / "runtime.env"
    env_file.write_text("LLM_PROVIDER=echo\n", encoding="utf-8")
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    existing = {
        "Label": "com.daemonharness.openclaw-api",
        "ProgramArguments": [sys.executable],
        "EnvironmentVariables": {
            "DAEMON_CORE_ENV_FILE": str(env_file),
            "DAEMON_CORE_MEMORY_ROOT": str(memory_root),
            "DAEMON_CORE_CONTINUITY_RECEIPTS": "shadow",
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(receipt_root),
            "DAEMON_CORE_RELATION_GARDENING": "active",
            "DAEMON_CORE_RELATION_GARDENING_USER_IDS": "private-test-id",
        },
    }
    with plist_path.open("wb") as stream:
        plistlib.dump(existing, stream)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "uname": "#!/bin/sh\necho Darwin\n",
        "launchctl": "#!/bin/sh\nexit 0\n",
        "curl": "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"ready\",\"promotable\":true}'\n",
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
        ["/bin/bash", str(root / "integrations" / "openclaw_plugin" / "service.sh"), "install"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    with plist_path.open("rb") as stream:
        rendered = plistlib.load(stream)
    rendered_env = rendered["EnvironmentVariables"]
    assert rendered["ProgramArguments"][0] == sys.executable
    assert rendered_env["DAEMON_CORE_ENV_FILE"] == str(env_file)
    assert rendered_env["DAEMON_CORE_MEMORY_ROOT"] == str(memory_root)
    assert rendered_env["DAEMON_CORE_CONTINUITY_RECEIPTS"] == "shadow"
    assert rendered_env["DAEMON_CORE_CONTINUITY_RECEIPT_DIR"] == str(receipt_root)
    assert rendered_env["DAEMON_CORE_RELATION_GARDENING"] == "active"
    assert rendered_env["DAEMON_CORE_RELATION_GARDENING_USER_IDS"] == "private-test-id"


def test_launchd_service_requires_a_promotable_readiness_report():
    root = Path(__file__).resolve().parents[1]
    script = (root / "integrations" / "openclaw_plugin" / "service.sh").read_text(
        encoding="utf-8"
    )

    assert 'report.get("promotable") is True' in script
    assert "if ready_for_promotion" in script
    assert 'launchctl kickstart -k "gui/$(id -u)/$LABEL"' in script


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd service targets macOS")
def test_launchd_reinstall_reads_the_deployed_after_turn_worker_revision(
    tmp_path: Path,
):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    api_plist = agents / "com.daemonharness.openclaw-api.plist"
    worker_plist = agents / "com.daemonharness.after-turn-worker.plist"
    env_file = tmp_path / "runtime.env"
    env_file.write_text("LLM_PROVIDER=echo\n", encoding="utf-8")
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    with api_plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "com.daemonharness.openclaw-api",
                "ProgramArguments": [sys.executable],
                "EnvironmentVariables": {
                    "DAEMON_CORE_ENV_FILE": str(env_file),
                    "DAEMON_CORE_MEMORY_ROOT": str(memory_root),
                    "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION": "old-revision",
                },
            },
            stream,
        )
    with worker_plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "com.daemonharness.after-turn-worker",
                "EnvironmentVariables": {
                    "DAEMON_CORE_RUNTIME_REVISION": "deployed-worker-revision"
                },
            },
            stream,
        )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "uname": "#!/bin/sh\necho Darwin\n",
        "launchctl": "#!/bin/sh\nexit 0\n",
        "curl": "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"ready\",\"promotable\":true}'\n",
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
            str(root / "integrations" / "openclaw_plugin" / "service.sh"),
            "install",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    with api_plist.open("rb") as stream:
        rendered = plistlib.load(stream)
    assert (
        rendered["EnvironmentVariables"]["DAEMON_CORE_AFTER_TURN_SERVICE_REVISION"]
        == "deployed-worker-revision"
    )
