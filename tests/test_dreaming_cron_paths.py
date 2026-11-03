from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("process_override", "expected_hours"),
    [(None, "48"), ("72", "72")],
)
def test_dreaming_cron_uses_canonical_code_with_external_state(
    tmp_path: Path,
    process_override: str | None,
    expected_hours: str,
):
    root = Path(__file__).resolve().parents[1]
    state_root = tmp_path / "state"
    memory_root = state_root / "memory"
    user_id = "4242"
    (memory_root / "users" / user_id).mkdir(parents=True)
    (memory_root / "users" / user_id / "agenda.md").write_text(
        "# Agenda\n",
        encoding="utf-8",
    )
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    (l0_root / f"{user_id}_2026-08-06.md").write_text(
        "recent turn\n",
        encoding="utf-8",
    )
    (state_root / ".env").write_text(
        "CRON_STATE_MARKER=loaded\n"
        "DREAMING_SINCE_HOURS=48\n"
        "CODEX_CLI_TIMEOUT_SECONDS=300\n"
        "DREAMING_CONTINUITY_RECEIPT_DIRS=/tmp/hermes:/tmp/openclaw\n",
        encoding="utf-8",
    )

    capture_path = tmp_path / "calls.log"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "printf '%s|%s|%s|%s|%s|%s|%s\\n' \"$PWD\" \"$PYTHONPATH\" "
        "\"$DAEMON_CORE_MEMORY_ROOT\" \"$CRON_STATE_MARKER\" "
        "\"$DREAMING_CONTINUITY_RECEIPT_DIRS\" \"$CODEX_CLI_TIMEOUT_SECONDS\" \"$*\" "
        ">> \"$CRON_CAPTURE\"\n"
        "case \"$*\" in\n"
        "  *daemon_core.registre*) exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "CRON_CAPTURE": str(capture_path),
            "DREAMING_DRY_RUN": "1",
            "DREAMING_CAFFEINATED": "1",
            "HERMES_ENV": str(tmp_path / "missing-hermes.env"),
            "DAEMON_CORE_STATE_ROOT": str(state_root),
            "NIGHTLY_ORCHESTRATOR_PYTHON": sys.executable,
            "PYTHON_BIN": str(fake_python),
        }
    )
    if process_override is not None:
        env["DREAMING_SINCE_HOURS"] = process_override
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "dreaming_cron.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    calls = [line.split("|", 6) for line in capture_path.read_text().splitlines()]
    assert len(calls) == 5
    assert {call[0] for call in calls} == {str(root)}
    assert {call[1] for call in calls} == {str(root / "src")}
    assert {call[2] for call in calls} == {str(memory_root)}
    assert {call[3] for call in calls} == {"loaded"}
    assert {call[4] for call in calls} == {"/tmp/hermes:/tmp/openclaw"}
    assert {call[5] for call in calls} == {"600"}

    commands = [call[6] for call in calls]
    agenda_call = next(
        command
        for command in commands
        if "daemon_core.agenda_maintenance" in command
    )
    dreaming_call = next(command for command in commands if "daemon_core.dreaming" in command)
    rem_call = next(command for command in commands if "daemon_core.rem " in command)
    assert "--apply" not in agenda_call
    assert "--apply" not in dreaming_call
    assert f"--since-hours {expected_hours}" in dreaming_call
    assert "--dry-run" in rem_call


def test_dreaming_cron_keeps_internal_failures_out_of_user_channels():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "dreaming_cron.sh").read_text(encoding="utf-8")
    owner = (root / "src/daemon_core/nightly_maintenance.py").read_text(
        encoding="utf-8"
    )

    assert "api.telegram.org" not in script + owner
    assert "sendMessage" not in script + owner
    assert "TELEGRAM_BOT_TOKEN" not in script + owner
    assert "failed = False" in owner
    assert '"--needs-retry"' in owner
    assert '"dreaming"' in owner
    assert '"rem"' in owner


def test_dreaming_cron_holds_a_sleeping_mac_awake_for_the_cycle():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "dreaming_cron.sh").read_text(encoding="utf-8")
    owner = (root / "src/daemon_core/nightly_maintenance.py").read_text(
        encoding="utf-8"
    )

    assert 'export DREAMING_CAFFEINATED=1' in script
    assert 'exec /usr/bin/caffeinate -s /bin/bash "$0" "$@"' in script
    assert '"DREAMING_CODEX_TIMEOUT_SECONDS", "600"' in owner


def test_dreaming_cron_reports_a_failed_faculty_to_the_supervisor(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    state_root = tmp_path / "state"
    memory_root = state_root / "memory"
    user_id = "4242"
    (memory_root / "users" / user_id).mkdir(parents=True)
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    (l0_root / f"{user_id}_2026-08-08.md").write_text(
        "recent turn\n",
        encoding="utf-8",
    )

    capture_path = tmp_path / "calls.log"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "printf '%s\n' \"$*\" >> \"$CRON_CAPTURE\"\n"
        "case \"$*\" in\n"
        "  *daemon_core.registre*) exit 1 ;;\n"
        "  *daemon_core.dreaming*) exit 2 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "CRON_CAPTURE": str(capture_path),
            "DREAMING_CAFFEINATED": "1",
            "HERMES_ENV": str(tmp_path / "missing-hermes.env"),
            "DAEMON_CORE_STATE_ROOT": str(state_root),
            "NIGHTLY_ORCHESTRATOR_PYTHON": sys.executable,
            "PYTHON_BIN": str(fake_python),
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "dreaming_cron.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "FAILED 4242 (rc=2)" in result.stdout
    assert "REM ok 4242" in result.stdout


def test_agenda_maintenance_runs_even_without_recent_conversation(
    tmp_path: Path,
):
    root = Path(__file__).resolve().parents[1]
    state_root = tmp_path / "state"
    memory_root = state_root / "memory"
    (memory_root / "users" / "4242").mkdir(parents=True)
    (memory_root / "users" / "4242" / "agenda.md").write_text(
        "# Agenda\n",
        encoding="utf-8",
    )
    (
        memory_root / "levels" / "L0_ground_zero" / "conversations"
    ).mkdir(parents=True)
    capture_path = tmp_path / "calls.log"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$CRON_CAPTURE\"\n"
        "case \"$*\" in\n"
        "  *daemon_core.registre*) exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "CRON_CAPTURE": str(capture_path),
            "DREAMING_CAFFEINATED": "1",
            "HERMES_ENV": str(tmp_path / "missing-hermes.env"),
            "DAEMON_CORE_STATE_ROOT": str(state_root),
            "NIGHTLY_ORCHESTRATOR_PYTHON": sys.executable,
            "PYTHON_BIN": str(fake_python),
        }
    )

    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "dreaming_cron.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    calls = capture_path.read_text(encoding="utf-8").splitlines()
    agenda_calls = [
        call for call in calls if "daemon_core.agenda_maintenance" in call
    ]
    assert len(agenda_calls) == 1
    assert "--apply" in agenda_calls[0]
    assert not any("daemon_core.dreaming " in call for call in calls)
    assert not any("daemon_core.rem " in call for call in calls)
