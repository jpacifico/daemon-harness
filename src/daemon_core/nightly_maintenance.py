"""Portable owner of the existing nightly Dreaming/REM maintenance cycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import subprocess
import time

from dotenv import dotenv_values


CommandRunner = Callable[[list[str], Path, Mapping[str, str]], int]
_TRUE = frozenset({"1", "true", "yes"})
_FALSE = frozenset({"0", "false", "no"})


def _default_runner(command: list[str], cwd: Path, environment: Mapping[str, str]) -> int:
    return subprocess.run(command, cwd=cwd, env=dict(environment), check=False).returncode


def run_nightly_cycle(
    *,
    environment: Mapping[str, str] | None = None,
    code_root: Path | None = None,
    runner: CommandRunner = _default_runner,
) -> int:
    """Run the historical cycle without changing any faculty's judgment."""

    inherited = dict(os.environ if environment is None else environment)
    root = (code_root or Path(__file__).resolve().parents[2]).resolve()
    state_root = Path(inherited.get("DAEMON_CORE_STATE_ROOT", str(root))).expanduser()
    env_file = Path(
        inherited.get("DAEMON_CORE_ENV_FILE", str(state_root / ".env"))
    ).expanduser()
    since_override = inherited.get("DREAMING_SINCE_HOURS")
    child_env = dict(inherited)
    if env_file.is_file() and not env_file.is_symlink():
        child_env.update({
            key: value
            for key, value in dotenv_values(env_file).items()
            if value is not None
        })
    if since_override is not None:
        child_env["DREAMING_SINCE_HOURS"] = since_override

    since_hours = _positive_int(child_env.get("DREAMING_SINCE_HOURS", "24"))
    python = Path(
        child_env.get(
            "PYTHON_BIN",
            child_env.get(
                "DAEMON_CORE_PYTHON",
                str(state_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
            ),
        )
    ).expanduser()
    memory_root = Path(
        child_env.get("DAEMON_CORE_MEMORY_ROOT", str(state_root / "memory"))
    ).expanduser()
    child_env["DAEMON_CORE_MEMORY_ROOT"] = str(memory_root)
    child_env["MEMORY_LLM_PROVIDER"] = child_env.get(
        "DREAMING_LLM_PROVIDER",
        child_env.get("MEMORY_LLM_PROVIDER", "codex_cli"),
    )
    child_env["CODEX_CLI_TIMEOUT_SECONDS"] = child_env.get(
        "DREAMING_CODEX_TIMEOUT_SECONDS", "600"
    )
    child_env["PYTHONPATH"] = str(root / "src")

    if not python.is_file() or (os.name != "nt" and not os.access(python, os.X_OK)):
        print(f"nightly | python unavailable: {python}")
        return 1
    users_dir = memory_root / "users"
    if not users_dir.is_dir():
        print("nightly | no memory/users directory — nothing to do")
        return 0

    dry_run = _enabled(child_env.get("DREAMING_DRY_RUN", "0"))
    observe_only = _enabled(child_env.get("DREAMING_OBSERVE_ONLY", "0"))
    dreaming_enabled = not _disabled(child_env.get("DREAMING_ENABLED", "1"))
    if not dreaming_enabled:
        print("nightly | Dreaming suspendu ; REM et agenda inchangés")

    failed = False
    for user_dir in sorted(users_dir.iterdir(), key=lambda path: path.name):
        if not user_dir.is_dir() or user_dir.name.startswith("test-"):
            continue
        user_id = user_dir.name
        agenda = user_dir / "agenda.md"
        if agenda.is_file():
            command = _module_command(python, "daemon_core.agenda_maintenance", "--user-id", user_id, "--memory-root", str(memory_root))
            if not dry_run:
                command.append("--apply")
            failed |= _run(command, root, child_env, runner, "agenda maintenance", user_id)

        recent = _has_recent_l0(memory_root, user_id, since_hours)
        retry_dreaming = dreaming_enabled and _needs_retry(
            python, root, child_env, runner, "dreaming", user_id
        )
        retry_rem = _needs_retry(python, root, child_env, runner, "rem", user_id)
        if not recent and not retry_dreaming and not retry_rem:
            print(f"nightly | skip {user_id} (no L0 activity < {since_hours}h)")
            continue

        if dreaming_enabled and (recent or retry_dreaming):
            command = _module_command(
                python,
                "daemon_core.dreaming",
                "--user-id",
                user_id,
                "--since-hours",
                str(since_hours),
            )
            if not dry_run and not observe_only:
                command.append("--apply")
            failed |= _run(command, root, child_env, runner, "dreaming", user_id)

        if recent or retry_rem:
            command = _module_command(python, "daemon_core.rem", "--user-id", user_id)
            if dry_run:
                command.append("--dry-run")
            failed |= _run(command, root, child_env, runner, "REM", user_id)
    return int(failed)


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError("DREAMING_SINCE_HOURS must be a positive integer.") from error
    if result <= 0:
        raise ValueError("DREAMING_SINCE_HOURS must be a positive integer.")
    return result


def _enabled(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _disabled(value: str) -> bool:
    return value.strip().lower() in _FALSE


def _module_command(python: Path, module: str, *arguments: str) -> list[str]:
    return [str(python), "-m", module, *arguments]


def _has_recent_l0(memory_root: Path, user_id: str, since_hours: int) -> bool:
    conversations = memory_root / "levels" / "L0_ground_zero" / "conversations"
    if not conversations.is_dir():
        return False
    threshold = since_hours * 60 * 60
    now = time.time()
    for path in conversations.glob(f"{user_id}_*.md"):
        try:
            if now - path.stat().st_mtime < threshold:
                return True
        except OSError:
            continue
    return False


def _needs_retry(
    python: Path,
    root: Path,
    environment: Mapping[str, str],
    runner: CommandRunner,
    faculty: str,
    user_id: str,
) -> bool:
    command = _module_command(
        python,
        "daemon_core.registre",
        "--needs-retry",
        faculty,
        "--user-id",
        user_id,
    )
    return runner(command, root, environment) == 0


def _run(
    command: list[str],
    root: Path,
    environment: Mapping[str, str],
    runner: CommandRunner,
    label: str,
    user_id: str,
) -> bool:
    print(f"nightly | {label} {user_id}…")
    result = runner(command, root, environment)
    if result == 0:
        print(f"nightly | {label} ok {user_id}")
        return False
    print(f"nightly | {label} FAILED {user_id} (rc={result})")
    return True


def main() -> None:
    raise SystemExit(run_nightly_cycle())


if __name__ == "__main__":
    main()
