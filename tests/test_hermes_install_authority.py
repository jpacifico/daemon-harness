from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "integrations" / "hermes_plugin" / "install.sh"


@pytest.mark.parametrize(
    "provider_heading",
    (
        "# --- daemon-core providers ---\n",
        "# --- daemon-core providers (à renseigner selon tes modèles) ---\n",
        "# --- daemon-core providers (configure for your models) ---\n",
        "",  # New installation.
    ),
)
def test_installer_enforces_single_conversation_authority_idempotently(
    tmp_path: Path, provider_heading: str,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "# commentaire à préserver\n"
        "agent:\n"
        "  max_turns: 42\n"
        "memory:\n"
        "  provider: local\n"
        "  memory_enabled: true\n"
        "  user_profile_enabled: true\n"
        "telegram:\n"
        "  reply_to_mode: first\n",
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text(
        "DAEMON_CORE_SRC=/already/configured\n"
        "DAEMON_CORE_FOLLOWUPS=on\n"
        "FOLLOWUP_LLM_PROVIDER=codex_cli\n"
        "DAEMON_CORE_USER_ID=123\n"
        "DAEMON_CORE_SOVEREIGN_VOICE=on\n"
        + provider_heading,
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_path = tmp_path / "hermes-calls.txt"
    _write_executable(
        fake_bin / "hermes",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HERMES_TEST_CALLS"\n',
    )
    _write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
    _write_executable(fake_bin / "launchctl", "#!/bin/sh\nexit 0\n")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_TEST_CALLS"] = str(calls_path)
    env["DREAMING_PYTHON"] = sys.executable
    env["DREAMING_MEMORY_ROOT"] = str(tmp_path / "memory")
    env["DREAMING_STATE_ROOT"] = str(tmp_path / "state")
    env["DREAMING_ENV_FILE"] = str(tmp_path / "dreaming.env")
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    first = subprocess.run(
        [str(INSTALLER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "config.yaml mis à jour (OK)" in first.stdout
    assert "source et plugins actifs vérifiés ✓" in first.stdout
    assert "Installation daemon-core → Hermes" in first.stdout

    first_text = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(first_text)
    assert first_text.startswith("# commentaire à préserver\n")
    assert config["memory"] == {
        "provider": "daemon_core",
        "memory_enabled": False,
        "user_profile_enabled": False,
    }
    assert config["agent"]["max_turns"] == 42
    assert config["telegram"]["reply_to_mode"] == "off"
    env_text = (hermes_home / ".env").read_text(encoding="utf-8")
    assert f"DAEMON_CORE_SRC={REPO_ROOT / 'src'}\n" in env_text
    assert "/already/configured" not in env_text
    expected_heading = provider_heading or (
        "# --- daemon-core providers (configure for your models) ---\n"
    )
    assert env_text.count(expected_heading) == 1
    assert ("rappels providers ajoutés" in first.stdout) == (not provider_heading)
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert "tools disable memory --platform telegram" not in calls
    assert "config set skills.creation_nudge_interval 0" in calls
    assert "config set auxiliary.background_review.enabled false" in calls

    second = subprocess.run(
        [str(INSTALLER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "config.yaml mis à jour" in second.stdout
    assert config_path.read_text(encoding="utf-8") == first_text
    assert (hermes_home / ".env").read_text(encoding="utf-8") == env_text
    assert "rappels providers ajoutés" not in second.stdout


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
