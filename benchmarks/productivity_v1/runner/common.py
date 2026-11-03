"""Shared, content-safe utilities for live productivity benchmark runners."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Iterable

from dotenv import dotenv_values


TOKEN_FIELDS = (
    "input_uncached",
    "cache_read",
    "cache_write",
    "output",
    "reasoning",
    "total",
)
MESSAGING_PREFIXES = (
    "TELEGRAM_",
    "DISCORD_",
    "SLACK_",
    "WHATSAPP_",
    "SIGNAL_",
    "SMS_",
    "DINGTALK_",
    "FEISHU_",
    "WECOM_",
    "LINE_",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value), encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def completed_host_turn_count(
    results: Iterable[subprocess.CompletedProcess[str]],
) -> int:
    """Count host turns that can have emitted an accepted after-turn event."""

    return sum(result.returncode == 0 for result in results)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def public_session_ref(session_id: str) -> str:
    return sha256(session_id.encode("utf-8")).hexdigest()[:16]


def safe_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {
        str(key): str(value)
        for key, value in dotenv_values(path).items()
        if key and value is not None
    }
    return {
        key: value
        for key, value in values.items()
        if not key.startswith(MESSAGING_PREFIXES)
    }


def prepare_codex_cli_home(target: Path, *, source: Path | None = None) -> None:
    """Copy only the Codex login material needed by one disposable runtime."""

    source_home = source or (Path.home() / ".codex")
    auth = source_home / "auth.json"
    if not auth.is_file():
        raise RuntimeError("Codex authentication is unavailable for isolation")
    target.mkdir(parents=True)
    shutil.copy2(auth, target / "auth.json")
    (target / "auth.json").chmod(0o600)
    models_cache = source_home / "models_cache.json"
    if models_cache.is_file():
        shutil.copy2(models_cache, target / "models_cache.json")
        (target / "models_cache.json").chmod(0o600)


def write_dotenv(path: Path, values: dict[str, str]) -> None:
    lines: list[str] = []
    for key in sorted(values):
        value = values[key]
        if "\n" in value:
            raise ValueError(f"multiline environment value is unsupported: {key}")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def isolated_daemon_env(
    source: dict[str, str],
    *,
    run_root: Path,
    memory_root: Path,
    user_id: str,
    platform: str,
    telemetry_path: Path,
    latency_path: Path,
) -> dict[str, str]:
    """Redirect every mutable daemon surface into one disposable run."""

    values = dict(source)
    for key, value in list(values.items()):
        if key.startswith("DAEMON_CORE_") and key.endswith("_USER_IDS"):
            if value.strip():
                values[key] = user_id
        if key.startswith("DAEMON_CORE_") and key.endswith("_PLATFORMS"):
            if value.strip():
                platforms = {item.strip() for item in value.split(",") if item.strip()}
                platforms.add(platform)
                values[key] = ",".join(sorted(platforms))

    values.update(
        {
            "DAEMON_CORE_ENV_FILE": str(run_root / "empty.env"),
            "DAEMON_CORE_MEMORY_ROOT": str(memory_root),
            "DAEMON_CORE_USER_ID": user_id,
            "DAEMON_CORE_FOLLOWUPS": "off",
            "DAEMON_CORE_DREAM_ON_SESSION_END": "false",
            "DAEMON_CORE_RECEIPT_REACTION": "off",
            "DAEMON_CORE_MEMORY_COVERAGE_SHADOW": "off",
            "DAEMON_CORE_MEMORY_FORENSIC_SHADOW": "off",
            "DAEMON_CORE_MEMORY_RECOVERY_SHADOW": "off",
            "DAEMON_CORE_HELD_THOUGHT": "off",
            "DAEMON_CORE_TEMPORAL_CURATOR": "off",
            "DAEMON_CORE_CONTINUITY_RECEIPTS": "off",
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(
                run_root / "continuity-receipts"
            ),
            "DAEMON_CORE_AFTER_TURN_SPOOL": "off",
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(run_root / "after-turn-spool"),
            "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_RECEIPT_DIR": str(
                run_root / "relation-receipts"
            ),
            "DAEMON_CORE_ATLAS_EMBEDDING_CACHE": str(
                run_root / "atlas-embeddings.sqlite3"
            ),
            "DAEMON_CORE_STATE_ROOT": str(run_root / "daemon-state"),
            "DAEMON_CORE_SHARED_WORLD_STATE": "off",
            "DAEMON_CORE_OPENCLAW_HEALTH_FILE": str(
                run_root / "openclaw-health.json"
            ),
            "DAEMON_CORE_LOG_DIR": str(run_root / "logs"),
            "DAEMON_CORE_VOICE_CAPTURE_FILE": "",
            "DAEMON_CORE_LLM_TELEMETRY_FILE": str(telemetry_path),
            "DAEMON_CORE_LATENCY_LOG": str(latency_path),
            "HOST_CAPABILITY_AFTER_TURN_WAIT_SECONDS": "0",
            "DAEMON_CORE_SOVEREIGN_VOICE": "on",
            "DAEMON_CORE_SOVEREIGN_MODE": "delegate",
            "DAEMON_CORE_SOVEREIGN_PLATFORMS": platform,
            "DAEMON_CORE_SINGLE_PASS": "on",
            "DAEMON_CORE_SINGLE_PASS_USER_IDS": user_id,
            "DAEMON_CORE_SOVEREIGN_LIBRARY": "on",
            "DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS": user_id,
            "DAEMON_CORE_THIN_SOVEREIGN_VOICE": "on",
            "DAEMON_CORE_THIN_SOVEREIGN_USER_IDS": user_id,
            "DAEMON_CORE_SCENE_MODEL": "canary",
            "DAEMON_CORE_SCENE_MODEL_USER_IDS": user_id,
            "DAEMON_CORE_L0_RECALL_TOOL": "on",
            "DAEMON_CORE_L0_RECALL_USER_IDS": user_id,
        }
    )
    return values


def durable_after_turn_env(
    values: dict[str, str],
    *,
    user_id: str,
) -> dict[str, str]:
    """Enable the production-shaped durable owner in an already isolated run."""

    configured = dict(values)
    configured.update(
        {
            "DAEMON_CORE_AFTER_TURN_SPOOL": "on",
            "DAEMON_CORE_AFTER_TURN_OWNER": "external",
            "DAEMON_CORE_AFTER_TURN_USER_IDS": user_id,
            "DAEMON_CORE_CONTINUITY_RECEIPTS": "shadow",
            "DAEMON_CORE_SCENE_COALESCING": "canary",
            "HOST_CAPABILITY_AFTER_TURN_WAIT_SECONDS": "0",
        }
    )
    return configured


def wait_for_after_turn_service(
    *,
    process: subprocess.Popen[str],
    spool_root: Path,
    log_path: Path,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Wait for a fresh external-owner heartbeat without importing its process."""

    heartbeat_path = spool_root / "service.json"
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = (
                log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                if log_path.is_file()
                else ""
            )
            raise RuntimeError(f"after-turn service exited during startup\n{tail}")
        try:
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            heartbeat = {}
        try:
            worker_count = int(heartbeat.get("worker_count", 0) or 0)
            heartbeat_age = time.time() - float(
                heartbeat.get("heartbeat_at", 0) or 0
            )
        except (AttributeError, TypeError, ValueError):
            worker_count = 0
            heartbeat_age = float("inf")
        if (
            isinstance(heartbeat, dict)
            and heartbeat.get("status") == "running"
            and worker_count >= 2
            and 0 <= heartbeat_age < 15
        ):
            return heartbeat
        time.sleep(0.1)
    raise RuntimeError("after-turn service did not become healthy")


def wait_for_durable_after_turn(
    *,
    process: subprocess.Popen[str],
    spool_root: Path,
    receipt_path: Path,
    telemetry_path: Path,
    host: str,
    user_id: str,
    expected_turns: int,
    timeout_seconds: float,
    idle_seconds: float = 1.0,
) -> dict[str, Any]:
    """Close one episode only after every durable turn reaches a terminal cycle."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_activity = time.monotonic()
    previous_signature: tuple[Any, ...] | None = None
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("after-turn service exited before lifecycle closure")
        snapshot = durable_after_turn_snapshot(
            spool_root=spool_root,
            receipt_path=receipt_path,
            telemetry_path=telemetry_path,
            host=host,
            user_id=user_id,
        )
        signature = (
            snapshot["terminal_turn_count"],
            tuple(sorted(snapshot["spool_counts"].items())),
            snapshot["telemetry_bytes"],
        )
        if signature != previous_signature:
            last_activity = time.monotonic()
            previous_signature = signature
        no_active_work = all(
            snapshot["spool_counts"].get(state, 0) == 0
            for state in ("pending", "running", "scene_pending", "scene_running")
        )
        if (
            snapshot["terminal_turn_count"] >= expected_turns
            and no_active_work
            and time.monotonic() - last_activity >= idle_seconds
        ):
            snapshot["complete"] = True
            snapshot["timed_out"] = False
            return snapshot
        time.sleep(0.2)
    snapshot["complete"] = False
    snapshot["timed_out"] = True
    return snapshot


def durable_after_turn_snapshot(
    *,
    spool_root: Path,
    receipt_path: Path,
    telemetry_path: Path,
    host: str,
    user_id: str,
) -> dict[str, Any]:
    states = (
        "pending",
        "running",
        "failed",
        "quarantined",
        "scene_pending",
        "scene_running",
        "scene_failed",
        "scene_quarantined",
    )
    counts = {
        state: len(list((spool_root / state / host / user_id).glob("*.json")))
        for state in states
    }
    latest_cycles: dict[str, str] = {}
    for event in read_jsonl(receipt_path):
        turn_id = event.get("turn_id")
        if (
            event.get("stage") == "cycle"
            and isinstance(turn_id, str)
            and turn_id
        ):
            latest_cycles[turn_id] = str(event.get("status") or "")
    terminal = {"completed", "skipped", "failed", "recovered"}
    return {
        "terminal_turn_count": sum(
            status in terminal for status in latest_cycles.values()
        ),
        "cycle_status_counts": {
            status: sum(value == status for value in latest_cycles.values())
            for status in sorted(set(latest_cycles.values()))
        },
        "spool_counts": counts,
        "telemetry_bytes": (
            telemetry_path.stat().st_size if telemetry_path.is_file() else 0
        ),
    }


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def executable_path(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.absolute()
    discovered = shutil.which(str(value))
    if not discovered:
        raise FileNotFoundError(f"executable not found: {value}")
    return Path(discovered).absolute()


def unavailable_value() -> dict[str, Any]:
    return {"value": None, "quality": "unavailable"}


def measured_value(value: int, quality: str) -> dict[str, Any]:
    return {"value": max(0, int(value)), "quality": quality}


def empty_token_scope(
    *,
    call_count: int = 0,
    exact_zero: bool = False,
) -> dict[str, Any]:
    value = measured_value(0, "host_reported") if exact_zero else unavailable_value()
    return {
        "call_count": call_count,
        **{field: dict(value) for field in TOKEN_FIELDS},
    }


def token_scope_from_usage(
    usages: Iterable[dict[str, Any]],
    *,
    quality: str,
    aliases: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    items = list(usages)

    def total_for(field: str) -> dict[str, Any]:
        keys = aliases[field]
        observed = False
        total = 0
        for usage in items:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    observed = True
                    total += int(value)
                    break
        return measured_value(total, quality) if observed else unavailable_value()

    return {
        "call_count": len(items),
        **{field: total_for(field) for field in TOKEN_FIELDS},
    }


def tool_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") not in {"tool_complete", "tool_completed"}:
            continue
        payload = event.get("payload")
        source = payload if isinstance(payload, dict) else event
        name = source.get("tool_name") or source.get("toolName")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_duration = source.get("duration_ms") or source.get("durationMs") or 0
        duration = (
            int(round(float(raw_duration)))
            if isinstance(raw_duration, (int, float))
            else 0
        )
        raw_status = str(source.get("status") or "").casefold()
        status = "error" if raw_status in {"error", "failed", "failure"} else "ok"
        results.append(
            {
                "name": name.strip(),
                "status": status,
                "duration_ms": max(0, duration),
            }
        )
    return results


def artifact_records(
    workspace: Path, relative_paths: Iterable[str]
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for relative in relative_paths:
        path = workspace / relative
        if path.is_file():
            records.append({"name": relative, "sha256": file_sha256(path)})
    return records


def source_revision(root: Path) -> dict[str, Any]:
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=root)
    branch = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    status = run_command(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=root,
    )
    if any(item.returncode for item in (commit, branch, status)):
        raise RuntimeError("cannot resolve the evaluated source revision")
    return {
        "commit": commit.stdout.strip(),
        "branch": branch.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
    }


def base_process_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(MESSAGING_PREFIXES):
            env.pop(key, None)
    return env


def redact_local_paths(text: str, *extra_roots: Path) -> str:
    replacements = [
        (str(Path.home()), "<HOME>"),
        *((str(path), "<RUNTIME>") for path in extra_roots),
    ]
    value = text
    for raw, replacement in sorted(replacements, key=lambda item: -len(item[0])):
        if raw:
            value = value.replace(raw, replacement)
    return value
