from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("enabled", [None, "1", "0", "false", "no"])
@pytest.mark.parametrize("observe,global_dry", [("0", "0"), ("1", "0"), ("0", "1"), ("yes", "true")])
@pytest.mark.parametrize("recent,retry", [(True, False), (False, True), (False, False)])
def test_nightly_controls_preserve_other_faculties(tmp_path, enabled, observe, global_dry, recent, retry):
    memory = tmp_path / "memory"
    user = memory / "users" / "4242"
    user.mkdir(parents=True)
    (user / "agenda.md").write_text("# Agenda\n", encoding="utf-8")
    l0 = memory / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True)
    if recent:
        (l0 / "4242_2026-09-06.md").write_text("Synthetic turn\n", encoding="utf-8")
    env_file = tmp_path / "night.env"
    env_file.write_text(
        f"DREAMING_OBSERVE_ONLY={observe}\nDREAMING_DRY_RUN={global_dry}\n"
        + (f"DREAMING_ENABLED={enabled}\n" if enabled is not None else ""),
        encoding="utf-8",
    )
    calls_file = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        '#!/bin/bash\n'
        'printf "%s\\n" "$*" >> "$CRON_CAPTURE"\n'
        'case "$*" in *daemon_core.registre*) exit "$RETRY_EXIT" ;; esac\n'
        'exit 0\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = os.environ.copy()
    for key in ("DREAMING_ENABLED", "DREAMING_OBSERVE_ONLY", "DREAMING_DRY_RUN", "DREAMING_SINCE_HOURS"):
        env.pop(key, None)
    env.update(
        DAEMON_CORE_STATE_ROOT=str(tmp_path),
        DAEMON_CORE_MEMORY_ROOT=str(memory),
        DAEMON_CORE_ENV_FILE=str(env_file),
        PYTHON_BIN=str(fake_python),
        NIGHTLY_ORCHESTRATOR_PYTHON=sys.executable,
        DREAMING_CAFFEINATED="1",
        CRON_CAPTURE=str(calls_file),
        RETRY_EXIT="0" if retry else "1",
    )
    before = {str(path): path.read_bytes() for path in memory.rglob("*.md")}
    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/dreaming_cron.sh")], env=env,
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    calls = calls_file.read_text().splitlines()
    active = enabled in (None, "1")
    assert sum("--needs-retry dreaming" in call for call in calls) == int(active)
    assert sum("--needs-retry rem" in call for call in calls) == 1
    dream = [call for call in calls if "-m daemon_core.dreaming " in call]
    rem = [call for call in calls if "-m daemon_core.rem " in call]
    agenda = [call for call in calls if "-m daemon_core.agenda_maintenance " in call]
    assert len(dream) == int(active and (recent or retry))
    assert len(rem) == int(recent or retry)
    assert len(agenda) == 1
    assert ("--apply" in agenda[0]) == (global_dry == "0")
    if dream:
        assert ("--apply" in dream[0]) == (global_dry == "0" and observe == "0")
    if rem:
        assert ("--dry-run" in rem[0]) == (global_dry != "0")
    assert ("Dreaming suspendu" in completed.stdout) == (not active)
    assert {str(path): path.read_bytes() for path in memory.rglob("*.md")} == before
