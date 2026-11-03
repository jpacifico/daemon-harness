"""Translate a host-independent FollowUp intent into a one-shot Hermes cron job.

The relay runs the judgment and recreates a job if the LLM defers the intent.
Its stdout, either a message or nothing, is delivered to the original chat.
The cron agent is not used because it lacks the daemon's memory and voice."""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import stat
import shlex
import sys
from pathlib import Path

from daemon_core import followup as fu_mod
from daemon_core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)


def _scripts_dir(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = Path(home) / "scripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _relay_script_body(followup_id: str, hermes_home: str | None = None) -> str:
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser().absolute()
    # Profiles contain state, not necessarily an embedded Hermes installation.
    # Keep the venv entry point: resolving its symlink loses its dependencies.
    python = Path(os.environ.get("DAEMON_CORE_PYTHON") or sys.executable).expanduser().absolute()
    if not python.is_file():
        raise ValueError("The configured follow-up interpreter is unavailable")
    python_paths = [str(PROJECT_ROOT), str(PROJECT_ROOT / "src"),
                    str(PROJECT_ROOT / "integrations/hermes_plugin")]
    # The separate daemon worker imports native cron from the Hermes source.
    try:
        cron_spec = importlib.util.find_spec("cron")
    except (ImportError, ValueError):
        cron_spec = None
    if cron_spec and cron_spec.submodule_search_locations:
        python_paths.extend(str(Path(path).parent) for path in cron_spec.submodule_search_locations)
    prefix = shlex.quote(os.pathsep.join(dict.fromkeys(python_paths)))
    q = shlex.quote
    return "\n".join((
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {q(str(PROJECT_ROOT))}",
        "set -a",
        "if [ -f .env ]; then . ./.env; fi",
        f"if [ -f {q(str(home / '.env'))} ]; then . {q(str(home / '.env'))}; fi",
        "set +a",
        f"export HERMES_HOME={q(str(home))}",
        'export CONVERSATION_LLM_PROVIDER="${FOLLOWUP_LLM_PROVIDER:-codex_cli}"',
        'export CONVERSATION_CODEX_MODEL="${FOLLOWUP_CODEX_MODEL:-${CONVERSATION_CODEX_MODEL:-${CODEX_CLI_MODEL:-}}}"',
        f"default_memory={q(str(PROJECT_ROOT / 'memory'))}",
        'log_dir="${DAEMON_CORE_LOG_DIR:-${DAEMON_CORE_MEMORY_ROOT:-$default_memory}/levels}"',
        'mkdir -p "$log_dir"',
        f'export PYTHONPATH={prefix}"${{PYTHONPATH:+{os.pathsep}$PYTHONPATH}}"',
        f'{q(str(python))} -m followup_cron fire {q(followup_id)} 2>> "$log_dir/followups.log"',
        "",
    ))



def _write_relay_script(followup_id: str, hermes_home: str | None = None) -> str:
    path = _scripts_dir(hermes_home) / f"followup_{followup_id}.sh"
    path.write_text(_relay_script_body(followup_id, hermes_home), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path.name  # create_job resolves names under ~/.hermes/scripts/.


def _remove_relay_script(followup_id: str, hermes_home: str | None = None) -> None:
    try:
        (_scripts_dir(hermes_home) / f"followup_{followup_id}.sh").unlink(missing_ok=True)
    except OSError:
        pass


def schedule_followup(fu, *, hermes_home: str | None = None, _create_job=None) -> str | None:
    """Create a one-shot cron job for this intent; return its ID or None.

    fu.origin must contain {platform, chat_id, thread_id?} for deliver="origin"."""
    if _create_job is None:  # Import lazily, only when running inside Hermes.
        from cron.jobs import create_job as _create_job

    script_name = _write_relay_script(fu.id, hermes_home)
    try:
        job = _create_job(
            prompt=None,  # Ignored in no_agent mode.
            schedule=fu.fire_at,  # ISO -> kind "once"
            name=f"followup_{fu.id}",
            repeat=1,
            deliver="origin",
            origin=fu.origin or None,
            no_agent=True,
            script=script_name,
        )
    except Exception as exc:
        logger.warning("Relance %s : création du job cron impossible (%s)", fu.id, exc.__class__.__name__)
        _remove_relay_script(fu.id, hermes_home)
        return None

    job_id = job.get("id") if isinstance(job, dict) else None
    if job_id:
        fu.job_id = job_id
        fu_mod.save_intent(fu)
        logger.info("Relance %s programmée | job=%s | fire_at=%s", fu.id, job_id, fu.fire_at)
    return job_id


def cancel_followup(fu, *, hermes_home: str | None = None, _remove_job=None) -> bool:
    """Cancel the cron job and mark the intent cancelled."""
    removed = False
    if fu.job_id:
        if _remove_job is None:
            from cron.jobs import remove_job as _remove_job
        try:
            removed = bool(_remove_job(fu.job_id))
        except Exception as exc:
            logger.warning("Relance %s : remove_job a échoué (%s)", fu.id, exc.__class__.__name__)
    _remove_relay_script(fu.id, hermes_home)
    fu_mod.cancel(fu.id)
    return removed


def fire_and_reconcile(
    followup_id: str,
    *,
    hermes_home: str | None = None,
    provider=None,
    now=None,
    _create_job=None,
) -> str:
    """Fire once, then recreate the one-shot job after an LLM reschedule."""
    message = fu_mod.fire(followup_id, provider=provider, now=now)
    fu = fu_mod.load_intent(followup_id)
    if fu is not None and fu.state == "pending" and fu.job_id is None:
        job_id = schedule_followup(
            fu,
            hermes_home=hermes_home,
            _create_job=_create_job,
        )
        if not job_id:
            logger.warning("Relance %s reportée mais nouveau job cron absent", followup_id)
    elif fu is None or fu.state != "pending":
        _remove_relay_script(followup_id, hermes_home)
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes relay for proactive followups.")
    sub = parser.add_subparsers(dest="command", required=True)
    fire_parser = sub.add_parser("fire")
    fire_parser.add_argument("followup_id")
    args = parser.parse_args()
    if args.command == "fire":
        print(fire_and_reconcile(args.followup_id), end="")


if __name__ == "__main__":
    main()
