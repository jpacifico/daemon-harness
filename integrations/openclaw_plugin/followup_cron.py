"""OpenClaw-native scheduler and delivery adapter for proactive follow-ups."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from daemon_core import followup as fu_mod
from daemon_core.paths import PROJECT_ROOT


logger = logging.getLogger(__name__)
TZ_NAME = "Europe/Paris"
DECLARATION_PREFIX = "daemon-harness:followup"
RunCommand = Callable[..., Any]


def _openclaw_bin() -> str:
    configured = os.getenv("OPENCLAW_BIN", "").strip()
    discovered = shutil.which("openclaw")
    fallback = Path.home() / ".local" / "bin" / "openclaw"
    return configured or discovered or str(fallback)


def _command_environment() -> dict[str, str]:
    plugin_dir = Path(__file__).resolve().parent
    values = {
        "PYTHONPATH": os.pathsep.join(
            (str(PROJECT_ROOT), str(PROJECT_ROOT / "src"), str(plugin_dir))
        ),
        "DAEMON_CORE_ENV_FILE": os.getenv(
            "DAEMON_CORE_ENV_FILE",
            str(PROJECT_ROOT / ".env"),
        ),
        "DAEMON_CORE_MEMORY_ROOT": os.getenv(
            "DAEMON_CORE_MEMORY_ROOT",
            "",
        ),
    }
    return {key: value for key, value in values.items() if value}


def _declaration_key(followup: fu_mod.FollowUp) -> str:
    return f"{DECLARATION_PREFIX}:{followup.id}:r{followup.reschedule_count}"


def _run_openclaw(
    arguments: list[str],
    *,
    _run: RunCommand | None = None,
    execution=None,
) -> dict[str, Any]:
    runner = _run or subprocess.run
    completed = runner(
        [*(execution.argv if execution is not None else (_openclaw_bin(),)), *arguments],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        **({"env": execution.environment(dict(os.environ))} if execution is not None else {}),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "cron command failed").strip()
        raise RuntimeError(detail[:500])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenClaw cron returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("OpenClaw cron returned an invalid result")
    return payload


def _job_id(payload: dict[str, Any]) -> str | None:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else payload
    value = job.get("id") if isinstance(job, dict) else None
    normalized = str(value or "").strip()
    return normalized or None


def _execution(followup):
    from daemon_core import paths
    from daemon_core.host_bindings import resolve_host_execution
    return resolve_host_execution(followup.origin or {}, host="openclaw",
                                  user_id=followup.user_id, memory=paths.MEMORY_ROOT)


def schedule_followup(
    followup: fu_mod.FollowUp,
    *,
    _run: RunCommand | None = None,
) -> str | None:
    """Create or reconcile one idempotent OpenClaw one-shot command job."""
    try:
        execution = _execution(followup)
    except (OSError, ValueError, TypeError, KeyError):
        logger.warning("Follow-up %s: originating OpenClaw binding is unavailable", followup.id)
        return None
    origin = followup.origin or {}
    channel = str(origin.get("channel") or "").strip()
    destination = str(origin.get("to") or "").strip()
    if not channel or not destination:
        logger.warning("Relance %s : destination OpenClaw absente", followup.id)
        return None
    command_argv = json.dumps(
        [sys.executable, "-m", "followup_cron", "fire", followup.id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    arguments = [
        "cron",
        "add",
        "--json",
        "--name",
        f"followup_{followup.id}_r{followup.reschedule_count}",
        "--declaration-key",
        _declaration_key(followup),
        "--at",
        followup.fire_at,
        "--tz",
        TZ_NAME,
        "--delete-after-run",
        "--session",
        "isolated",
        "--command-argv",
        command_argv,
        "--command-cwd",
        str(PROJECT_ROOT),
        "--timeout-seconds",
        "900",
        "--output-max-bytes",
        "16384",
        "--announce",
        "--channel",
        channel,
        "--to",
        destination,
    ]
    account_id = str(origin.get("account_id") or "").strip()
    if account_id:
        arguments.extend(("--account", account_id))
    thread_id = str(origin.get("thread_id") or "").strip()
    if thread_id:
        arguments.extend(("--thread-id", thread_id))
    relay = _command_environment()
    if execution is not None:
        relay.update(execution.relay_environment())
        for key in ("DAEMON_CORE_PROVIDER_CONFIG", "DAEMON_CORE_USER_ID"):
            if os.getenv(key):
                relay[key] = os.environ[key]
    for key, value in relay.items():
        arguments.extend(("--command-env", f"{key}={value}"))
    try:
        job_id = _job_id(_run_openclaw(arguments, _run=_run, execution=execution))
    except Exception as exc:
        logger.warning(
            "Relance %s : création du job OpenClaw impossible (%s)",
            followup.id,
            exc.__class__.__name__,
        )
        return None
    if not job_id:
        logger.warning("Relance %s : job OpenClaw sans identifiant", followup.id)
        return None
    followup.job_id = job_id
    fu_mod.save_intent(followup)
    logger.info(
        "Relance %s programmée par OpenClaw | job=%s | fire_at=%s",
        followup.id,
        job_id,
        followup.fire_at,
    )
    return job_id


def cancel_followup(
    followup: fu_mod.FollowUp,
    *,
    _run: RunCommand | None = None,
) -> bool:
    try:
        execution = _execution(followup)
    except (OSError, ValueError, TypeError, KeyError):
        logger.warning("Follow-up %s: originating OpenClaw binding is unavailable", followup.id)
        return False
    removed = False
    if followup.job_id:
        try:
            payload = _run_openclaw(
                ["cron", "rm", "--json", followup.job_id],
                _run=_run, execution=execution,
            )
            removed = payload.get("ok") is not False
        except Exception as exc:
            logger.warning(
                "Relance %s : suppression du job OpenClaw impossible (%s)",
                followup.id,
                exc.__class__.__name__,
            )
    fu_mod.cancel(followup.id)
    return removed


def _load_runtime_environment() -> None:
    configured = os.getenv("DAEMON_CORE_ENV_FILE", "").strip()
    load_dotenv(Path(configured).expanduser() if configured else PROJECT_ROOT / ".env")
    os.environ["CONVERSATION_LLM_PROVIDER"] = os.getenv(
        "FOLLOWUP_LLM_PROVIDER",
        os.getenv("CONVERSATION_LLM_PROVIDER", "codex_cli"),
    )
    followup_model = os.getenv("FOLLOWUP_CODEX_MODEL", "").strip()
    if followup_model:
        os.environ["CONVERSATION_CODEX_MODEL"] = followup_model


def fire_and_reconcile(
    followup_id: str,
    *,
    provider=None,
    now=None,
    _run: RunCommand | None = None,
) -> str:
    message = fu_mod.fire(followup_id, provider=provider, now=now)
    followup = fu_mod.load_intent(followup_id)
    if followup is not None and followup.state == "pending" and followup.job_id is None:
        if not schedule_followup(followup, _run=_run):
            logger.warning(
                "Relance %s reportée mais nouveau job OpenClaw absent",
                followup_id,
            )
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw relay for proactive followups.")
    sub = parser.add_subparsers(dest="command", required=True)
    fire_parser = sub.add_parser("fire")
    fire_parser.add_argument("followup_id")
    args = parser.parse_args()
    if args.command == "fire":
        _load_runtime_environment()
        print(fire_and_reconcile(args.followup_id), end="")


if __name__ == "__main__":
    main()


__all__ = ["cancel_followup", "fire_and_reconcile", "schedule_followup"]
