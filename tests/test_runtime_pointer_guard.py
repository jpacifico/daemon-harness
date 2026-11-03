from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_runtime_pointers.py"


def _runtime(root: Path) -> Path:
    source = root / "src"
    package = source / "daemon_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# package\n", encoding="utf-8")
    (package / "loop.py").write_text("# loop\n", encoding="utf-8")
    for plugin in ("hermes_plugin", "hermes_voice_plugin"):
        plugin_root = root / "integrations" / plugin
        plugin_root.mkdir(parents=True)
        (plugin_root / "__init__.py").write_text("# plugin\n", encoding="utf-8")
    openclaw = root / "integrations" / "openclaw_plugin"
    openclaw.mkdir(parents=True)
    (openclaw / "index.js").write_text("export default {};\n", encoding="utf-8")
    return root


def _homes(tmp_path: Path, runtime: Path) -> tuple[Path, Path]:
    hermes = tmp_path / "hermes"
    plugins = hermes / "plugins"
    plugins.mkdir(parents=True)
    (hermes / ".env").write_text(
        f"DAEMON_CORE_SRC={runtime / 'src'}\n",
        encoding="utf-8",
    )
    (plugins / "daemon_core").symlink_to(
        runtime / "integrations" / "hermes_plugin",
        target_is_directory=True,
    )
    (plugins / "daemon_voice").symlink_to(
        runtime / "integrations" / "hermes_voice_plugin",
        target_is_directory=True,
    )
    openclaw = tmp_path / "openclaw"
    openclaw.mkdir()
    (openclaw / "openclaw.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "load": {
                        "paths": [str(runtime / "integrations" / "openclaw_plugin")]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return hermes, openclaw


def _run(hermes: Path, openclaw: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--hermes-home",
            str(hermes),
            "--openclaw-home",
            str(openclaw),
            "--launchagents-dir",
            str(hermes.parent / "LaunchAgents"),
            "--allow-temporary",
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_runtime_pointer_guard_accepts_complete_active_runtime(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "stable")
    hermes, openclaw = _homes(tmp_path, runtime)

    result = _run(hermes, openclaw)

    assert result.returncode == 0
    assert "4 active references" in result.stdout


def test_runtime_pointer_guard_blocks_referenced_worktree_removal(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "candidate")
    hermes, openclaw = _homes(tmp_path, runtime)

    result = _run(hermes, openclaw, "--before-remove", str(runtime))

    assert result.returncode == 1
    assert result.stderr.count("still points inside the worktree") == 4


def test_runtime_pointer_guard_rejects_an_empty_namespace_package(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "broken")
    hermes, openclaw = _homes(tmp_path, runtime)
    (runtime / "src" / "daemon_core" / "loop.py").unlink()

    result = _run(hermes, openclaw)

    assert result.returncode == 1
    assert "missing daemon_core/loop.py" in result.stderr


def test_persistent_install_rejects_temporary_source(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "candidate")
    hermes, openclaw = _homes(tmp_path, runtime)
    command = list(_run(hermes, openclaw).args)
    command.remove("--allow-temporary")
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 1
    assert "temporary storage" in result.stderr


def test_services_block_removal_even_without_host_pointer(tmp_path: Path) -> None:
    import plistlib
    runtime = _runtime(tmp_path / "service-only")
    homes = _homes(tmp_path, _runtime(tmp_path / "host"))
    jobs = tmp_path / "LaunchAgents"
    jobs.mkdir()
    job = jobs / "com.daemonharness.after-turn-worker.plist"
    job.write_bytes(plistlib.dumps({
        "WorkingDirectory": str(runtime), "ProgramArguments": [sys.executable, "-m", "daemon_core.after_turn_daemon"],
        "EnvironmentVariables": {"PYTHONPATH": str(runtime / "src")},
    }))
    assert _run(*homes).returncode == 0
    result = _run(*homes, "--before-remove", str(runtime))
    assert result.returncode == 1
    assert "still points inside" in result.stderr
    (runtime / "src/daemon_core/loop.py").unlink()
    assert "source is incomplete" in _run(*homes).stderr


def test_nightly_missing_entrypoint_is_rejected(tmp_path: Path) -> None:
    import plistlib
    runtime = _runtime(tmp_path / "stable")
    homes = _homes(tmp_path, runtime)
    jobs = tmp_path / "LaunchAgents"
    jobs.mkdir()
    (jobs / "com.daemonharness.dreaming.plist").write_bytes(plistlib.dumps({
        "WorkingDirectory": str(runtime),
        "ProgramArguments": [sys.executable, str(runtime / "missing-nightly.py")],
    }))
    assert "unavailable" in _run(*homes).stderr



def test_quoted_runtime_path_is_checked_and_still_blocks_removal(tmp_path):
    runtime = _runtime(tmp_path / "candidate with spaces")
    hermes, openclaw = _homes(tmp_path, runtime)
    for quote in ('"', "'"):
        (hermes / '.env').write_text(f"DAEMON_CORE_SRC={quote}{runtime / 'src'}{quote}\n")
        assert _run(hermes, openclaw).returncode == 0
        result = _run(hermes, openclaw, '--before-remove', str(runtime))
        assert result.returncode == 1
        assert result.stderr.count('still points inside the worktree') == 4



def test_guard_blocks_removal_of_openclaw_prototype_api_source(tmp_path):
    import plistlib
    production = _runtime(tmp_path / "production")
    prototype = _runtime(tmp_path / "prototype")
    hermes, openclaw = _homes(tmp_path, production)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    label = "com.daemonharness.memory-structure-openclaw-api"
    (agents / (label + ".plist")).write_bytes(plistlib.dumps({
        "Label": label, "WorkingDirectory": str(prototype),
        "ProgramArguments": [sys.executable, "-m", "uvicorn", "server.main:app"],
        "EnvironmentVariables": {"PYTHONPATH": str(prototype / "src")},
    }))
    result = _run(hermes, openclaw, "--before-remove", str(prototype))
    assert result.returncode != 0
    assert label in result.stdout + result.stderr
