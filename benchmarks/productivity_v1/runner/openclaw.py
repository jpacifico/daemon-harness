"""Run a Productivity V1 scenario in disposable OpenClaw A/C profiles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from benchmarks.productivity_v1 import (
    LANGUAGE,
    PROTOCOL_VERSION,
    RUN_SCHEMA_VERSION,
)
from benchmarks.productivity_v1.runner.common import (
    append_jsonl,
    artifact_records,
    base_process_env,
    completed_host_turn_count,
    durable_after_turn_env,
    empty_token_scope,
    executable_path,
    file_sha256,
    isolated_daemon_env,
    json_sha256,
    prepare_codex_cli_home,
    public_session_ref,
    read_jsonl,
    redact_local_paths,
    run_command,
    safe_dotenv,
    token_scope_from_usage,
    wait_for_after_turn_service,
    wait_for_durable_after_turn,
    write_json,
)
from benchmarks.productivity_v1.scenarios import (
    SCENARIOS,
    PreparedScenario,
    get_scenario,
    prepare_scenario,
)


ROOT = Path(__file__).resolve().parents[3]
DAEMON_PLUGIN = ROOT / "integrations" / "openclaw_plugin"
DEFAULT_OPENCLAW_BINARY = Path.home() / ".local" / "bin" / "openclaw"
DEFAULT_DAEMON_PYTHON = (
    Path.home()
    / ".local"
    / "share"
    / "daemon-harness"
    / "runtime"
    / "venv"
    / "bin"
    / "python"
)
DEFAULT_DAEMON_ENV = Path.home() / ".openclaw" / "daemon-harness-main.env"
DEFAULT_RUNTIME_PARENT = (
    Path.home() / ".cache" / "daemon-public-productivity-v1" / "runtime"
)
BENCHMARK_USER_ID = "productivity-benchmark"
HOST_USAGE_ALIASES = {
    "input_uncached": ("input_uncached_tokens", "input", "input_tokens"),
    "cache_read": (
        "cached_input_tokens",
        "cacheRead",
        "cache_read",
        "cache_read_tokens",
    ),
    "cache_write": (
        "cache_write_input_tokens",
        "cacheWrite",
        "cache_write",
        "cache_write_tokens",
    ),
    "output": ("output", "output_tokens"),
    "reasoning": ("reasoning_output_tokens", "reasoning", "reasoning_tokens"),
    "total": ("total", "total_tokens"),
}
INSTALLATION_MEMORY_TOOL_NAMES = {
    "daemon_read_souvenirs",
    "daemon_recall_l0",
    "intent",
    "memory_get",
    "memory_search",
}
AMBIENT_CODEX_PREFIX = "CODEX_"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


def _variant_for_arm(seed: int, arm: str) -> str:
    reverse = seed % 2 == 0
    if (arm == "A") ^ reverse:
        return "left"
    return "right"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[str], log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = (
                log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                if log_path.is_file()
                else ""
            )
            raise RuntimeError(f"isolated daemon server exited during startup\n{tail}")
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("isolated daemon server did not become healthy")


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _discover_codex_plugin(binary: Path) -> Path:
    completed = run_command(
        [str(binary), "plugins", "info", "codex", "--json"],
        cwd=ROOT,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError(
            "cannot inspect the official OpenClaw Codex plugin: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        root = Path(json.loads(completed.stdout)["plugin"]["rootDir"]).resolve()
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("malformed OpenClaw Codex plugin metadata") from exc
    if not root.is_dir():
        raise RuntimeError(f"OpenClaw Codex plugin root does not exist: {root}")
    return root


def _prepare_codex_home(target: Path) -> None:
    prepare_codex_cli_home(target)


def _prepare_official_codex_install(
    *,
    state_dir: Path,
    codex_plugin_root: Path,
) -> None:
    source_project = codex_plugin_root.parents[2]
    source_manifest = source_project / "package.json"
    if not source_manifest.is_file():
        raise RuntimeError(f"managed Codex npm manifest not found: {source_manifest}")
    target_project = state_dir / "npm" / "projects" / source_project.name
    target_package = target_project / "node_modules" / "@openclaw" / "codex"
    target_package.parent.mkdir(parents=True)
    shutil.copy2(source_manifest, target_project / "package.json")
    target_package.symlink_to(codex_plugin_root, target_is_directory=True)


def _import_codex_auth(
    *,
    binary: Path,
    codex_home: Path,
    workspace: Path,
    env: dict[str, str],
    sandbox_profile: Path,
) -> dict[str, int]:
    """Import Codex auth into the disposable OpenClaw store via its public CLI."""

    auth_path = codex_home / "auth.json"
    command = _host_command(
        sandbox_profile=sandbox_profile,
        binary=binary,
        arguments=[
            "migrate",
            "apply",
            "codex",
            "--from",
            str(codex_home),
            "--agent",
            "main",
            "--include-secrets",
            "--item",
            "auth:openai",
            "--yes",
        ],
    )
    started = time.perf_counter()
    timed_out_attempts = 0
    for attempt in range(1, 4):
        try:
            completed = run_command(
                command,
                cwd=workspace,
                env=env,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            timed_out_attempts += 1
            if attempt < 3:
                continue
            raise RuntimeError(
                "cannot import Codex authentication into the disposable "
                "OpenClaw profile after 3 timed-out attempts"
            ) from None
        if completed.returncode:
            raise RuntimeError(
                "cannot import Codex authentication into the disposable "
                "OpenClaw profile: "
                + (completed.stderr.strip() or completed.stdout.strip())[-2000:]
            )
        auth_path.unlink(missing_ok=True)
        return {
            "attempts": attempt,
            "timed_out_attempts": timed_out_attempts,
            "duration_ms": max(0, round((time.perf_counter() - started) * 1000)),
        }
    raise AssertionError("unreachable Codex auth import state")


def _config(
    *,
    arm: str,
    workspace: Path,
    daemon_url: str,
    model: str,
    gateway_port: int | None = None,
) -> dict[str, Any]:
    load_paths: list[str] = []
    entries: dict[str, Any] = {
        "openai": {"enabled": True},
        "codex": {
            "enabled": True,
            "config": {
                "codexDynamicToolsLoading": "direct",
                "appServer": {
                    "homeScope": "agent",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "auto_review",
                    "sandbox": "danger-full-access",
                },
            },
        },
    }
    plugins: dict[str, Any] = {
        "allow": ["openai", "codex"],
        "load": {"paths": load_paths},
        "entries": entries,
    }
    tools: dict[str, Any] = {"profile": "coding"}
    if arm == "A":
        plugins["allow"].extend(("memory-core", "active-memory"))
        plugins["slots"] = {"memory": "memory-core"}
        entries["memory-core"] = {"enabled": True}
        entries["active-memory"] = {
            "enabled": True,
            "config": {
                "enabled": True,
                "mode": "escalate",
                "agents": ["main"],
                "allowedChatTypes": ["explicit"],
                "persistTranscripts": False,
            },
        }
        tools["alsoAllow"] = ["memory_search", "memory_get"]
    if arm == "C":
        load_paths.append(str(DAEMON_PLUGIN))
        plugins["slots"] = {"contextEngine": "daemon-harness"}
        entries["daemon-harness"] = {
            "enabled": True,
            "config": {"daemonUrl": daemon_url, "userId": BENCHMARK_USER_ID},
            "hooks": {"allowConversationAccess": True},
        }
        plugins["allow"].append("daemon-harness")
        tools["deny"] = ["memory_search", "memory_get"]
        tools["alsoAllow"] = ["daemon_read_souvenirs", "daemon_recall_l0"]
    config = {
        "agents": {
            "defaults": {
                "model": {"primary": model},
                "workspace": str(workspace),
                "skipBootstrap": True,
                "timeoutSeconds": 600,
            }
        },
        "memory": {
            "search": {
                "rememberAcrossConversations": arm == "A",
                "sources": ["memory", "sessions"] if arm == "A" else ["memory"],
            }
        },
        "hooks": {
            "internal": {
                "enabled": arm == "A",
                "entries": {"session-memory": {"enabled": arm == "A"}},
            }
        },
        "tools": tools,
        "plugins": plugins,
    }
    if gateway_port is not None:
        config["gateway"] = {
            "mode": "local",
            "port": gateway_port,
            "bind": "loopback",
            "auth": {"mode": "none"},
        }
    return config


def _wait_for_gateway(
    *,
    process: subprocess.Popen[str],
    log_path: Path,
    binary: Path,
    workspace: Path,
    env: dict[str, str],
    sandbox_profile: Path,
    port: int,
) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = (
                log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                if log_path.is_file()
                else ""
            )
            raise RuntimeError(
                f"isolated OpenClaw Gateway exited during startup\n{tail}"
            )
        try:
            completed = run_command(
                _host_command(
                    sandbox_profile=sandbox_profile,
                    binary=binary,
                    arguments=[
                        "gateway",
                        "health",
                        "--json",
                        "--port",
                        str(port),
                        "--timeout",
                        "1000",
                    ],
                ),
                cwd=workspace,
                env=env,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            time.sleep(0.2)
            continue
        if completed.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("isolated OpenClaw Gateway did not become healthy")


def _isolate_host_environment(
    values: dict[str, str],
    *,
    home: Path,
) -> dict[str, str]:
    """Remove ambient Codex task identity and redirect user-level state."""

    isolated = {
        key: value
        for key, value in values.items()
        if not key.startswith(AMBIENT_CODEX_PREFIX)
    }
    isolated.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        }
    )
    return isolated


def _write_outer_sandbox_profile(
    *,
    path: Path,
    runtime_root: Path,
    openclaw_binary: Path,
    codex_plugin_root: Path,
) -> None:
    """Deny the real home while preserving the installed host and runtime."""

    readable = (
        runtime_root,
        DAEMON_PLUGIN,
        openclaw_binary.parent,
        openclaw_binary.resolve().parent,
        codex_plugin_root.parents[2],
    )
    real_home = Path.home()
    traversal: set[Path] = {real_home}
    for item in readable:
        current = item.parent
        while current != real_home and real_home in current.parents:
            traversal.add(current)
            current = current.parent
    lines = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* (subpath {json.dumps(str(real_home))}))",
        f"(deny file-write* (subpath {json.dumps(str(real_home))}))",
        *(
            f"(allow file-read-metadata (literal {json.dumps(str(item))}))"
            for item in sorted(traversal)
        ),
        *(f"(allow file-read* (subpath {json.dumps(str(item))}))" for item in readable),
        f"(allow file-write* (subpath {json.dumps(str(runtime_root))}))",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _host_command(
    *,
    sandbox_profile: Path,
    binary: Path,
    arguments: list[str],
) -> list[str]:
    return [str(SANDBOX_EXEC), "-f", str(sandbox_profile), str(binary), *arguments]


def _split_process_envs(
    host_values: dict[str, str],
    daemon_values: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Keep daemon-only credentials and feature flags out of OpenClaw."""

    host_env = dict(host_values)
    daemon_env = dict(host_values)
    daemon_env.update(daemon_values)
    return host_env, daemon_env


def _validate_config(
    *,
    binary: Path,
    workspace: Path,
    env: dict[str, str],
    sandbox_profile: Path,
) -> None:
    completed = run_command(
        _host_command(
            sandbox_profile=sandbox_profile,
            binary=binary,
            arguments=["config", "validate", "--json"],
        ),
        cwd=workspace,
        env=env,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError(
            "OpenClaw rejected the isolated config: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        valid = json.loads(completed.stdout).get("valid") is True
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "OpenClaw config validation returned malformed JSON"
        ) from exc
    if not valid:
        raise RuntimeError("OpenClaw reported an invalid isolated config")


def _sync_native_memory(
    *,
    binary: Path,
    workspace: Path,
    env: dict[str, str],
    sandbox_profile: Path,
) -> tuple[int, str, int]:
    """Synchronize A's native transcript index through OpenClaw's public CLI."""

    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, 4):
        completed = run_command(
            _host_command(
                sandbox_profile=sandbox_profile,
                binary=binary,
                arguments=["memory", "index", "--force", "--agent", "main"],
            ),
            cwd=workspace,
            env=env,
            timeout=120,
        )
        attempts.append(
            {
                "attempt": attempt,
                "returncode": completed.returncode,
                "output_sha256": json_sha256(
                    {"stdout": completed.stdout, "stderr": completed.stderr}
                ),
            }
        )
        if completed.returncode == 0:
            return (
                round((time.perf_counter() - started) * 1000),
                json_sha256(attempts),
                attempt,
            )
        if attempt < 3:
            time.sleep(0.5)
    assert completed is not None
    raise RuntimeError(
        "OpenClaw native memory synchronization failed after 3 attempts: "
        + (completed.stderr.strip() or completed.stdout.strip())[-2000:]
    )


def _outer_sandbox_attestation(
    *,
    sandbox_profile: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    writable_probe = runtime_root / "sandbox-write-probe"
    writable = run_command(
        [
            str(SANDBOX_EXEC),
            "-f",
            str(sandbox_profile),
            "/usr/bin/touch",
            str(writable_probe),
        ],
        cwd=runtime_root,
        timeout=30,
    )
    blocked = run_command(
        [
            str(SANDBOX_EXEC),
            "-f",
            str(sandbox_profile),
            "/usr/bin/stat",
            str(Path.home() / ".openclaw" / "agents"),
        ],
        cwd=runtime_root,
        timeout=30,
    )
    probe_exists = writable_probe.is_file()
    writable_probe.unlink(missing_ok=True)
    if writable.returncode or not probe_exists or blocked.returncode == 0:
        raise RuntimeError("macOS did not attest the required outer filesystem sandbox")
    return {
        "host_process_is_sandboxed": True,
        "backend": "macos-sandbox-exec",
        "runtime_is_writable": True,
        "real_home_is_blocked": True,
        "attestation_sha256": json_sha256(
            {
                "profile": file_sha256(sandbox_profile),
                "write_returncode": writable.returncode,
                "blocked_returncode": blocked.returncode,
            }
        ),
    }


def _extract_usage(envelope: dict[str, Any]) -> dict[str, Any]:
    meta = envelope.get("meta")
    agent_meta = meta.get("agentMeta", {}) if isinstance(meta, dict) else {}
    usage = agent_meta.get("usage", {}) if isinstance(agent_meta, dict) else {}
    return usage if isinstance(usage, dict) else {}


def _extract_host_ms(envelope: dict[str, Any]) -> int | None:
    meta = envelope.get("meta")
    value = meta.get("durationMs") if isinstance(meta, dict) else None
    return max(0, round(value)) if isinstance(value, (int, float)) else None


def _rollout_token_scope(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    snapshots: list[dict[str, Any]] = []
    observed_totals: set[tuple[int, int, int, int, int, int]] = set()
    for record in records:
        payload = record.get("payload")
        if (
            record.get("type") != "event_msg"
            or not isinstance(payload, dict)
            or payload.get("type") != "token_count"
        ):
            continue
        info = payload.get("info")
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(usage, dict):
            continue
        values = tuple(
            max(0, int(usage.get(key, 0)))
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
        )
        if values in observed_totals:
            continue
        observed_totals.add(values)
        snapshots.append(usage)
    if not snapshots:
        return None

    usage = snapshots[-1]
    input_tokens = max(0, int(usage.get("input_tokens", 0)))
    cache_read = max(0, int(usage.get("cached_input_tokens", 0)))
    cache_write = max(0, int(usage.get("cache_write_input_tokens", 0)))
    values = {
        "input_uncached": max(0, input_tokens - cache_read - cache_write),
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": max(0, int(usage.get("output_tokens", 0))),
        "reasoning": max(0, int(usage.get("reasoning_output_tokens", 0))),
        "total": max(0, int(usage.get("total_tokens", 0))),
    }
    scope = token_scope_from_usage(
        [values],
        quality="host_reported",
        aliases={field: (field,) for field in values},
    )
    scope["call_count"] = len(snapshots)
    return scope


def _rollout_paths(codex_home: Path) -> list[Path]:
    return sorted(
        (codex_home / "sessions").rglob("rollout-*.jsonl"),
        key=lambda path: (path.stat().st_mtime_ns, path.as_posix()),
    )


def _rollout_snapshot(codex_home: Path) -> dict[Path, tuple[int, int]]:
    return {
        path.resolve(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in _rollout_paths(codex_home)
    }


def _openclaw_tasks(
    *,
    binary: Path,
    workspace: Path,
    env: dict[str, str],
    sandbox_profile: Path,
) -> list[dict[str, Any]]:
    completed = run_command(
        _host_command(
            sandbox_profile=sandbox_profile,
            binary=binary,
            arguments=["tasks", "list", "--json"],
        ),
        cwd=workspace,
        env=env,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError(
            "cannot inspect OpenClaw native background tasks: "
            + (completed.stderr.strip() or completed.stdout.strip())[-2000:]
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenClaw tasks list returned malformed JSON") from exc
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    return (
        [item for item in tasks if isinstance(item, dict)]
        if isinstance(tasks, list)
        else []
    )


def _task_belongs_to_sessions(task: dict[str, Any], session_keys: set[str]) -> bool:
    if not session_keys:
        return False
    candidates = {
        str(value)
        for key, value in task.items()
        if key
        in {
            "ownerKey",
            "requesterSessionKey",
            "sessionKey",
            "sourceSessionKey",
        }
        and isinstance(value, str)
    }
    return bool(candidates & session_keys)


def _wait_for_native_background(
    *,
    binary: Path,
    workspace: Path,
    env: dict[str, str],
    sandbox_profile: Path,
    codex_home: Path,
    foreground_snapshot: dict[Path, tuple[int, int]],
    session_keys: set[str],
    minimum_observation_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Keep the Gateway alive until attributable native maintenance settles."""

    started = time.monotonic()
    minimum_deadline = started + max(0.0, minimum_observation_seconds)
    deadline = started + max(minimum_observation_seconds, timeout_seconds)
    stable_since = time.monotonic()
    previous_snapshot = _rollout_snapshot(codex_home)
    terminal_tasks: list[dict[str, Any]] = []
    active_tasks: list[dict[str, Any]] = []
    task_poll_count = 0
    settled = False
    while True:
        task_poll_count += 1
        tasks = _openclaw_tasks(
            binary=binary,
            workspace=workspace,
            env=env,
            sandbox_profile=sandbox_profile,
        )
        attributable = [
            task for task in tasks if _task_belongs_to_sessions(task, session_keys)
        ]
        active_tasks = [
            task for task in attributable if task.get("status") in {"queued", "running"}
        ]
        terminal_tasks = [
            task
            for task in attributable
            if task.get("status")
            in {"succeeded", "failed", "timed_out", "cancelled", "lost", "blocked"}
        ]
        snapshot = _rollout_snapshot(codex_home)
        if snapshot != previous_snapshot:
            previous_snapshot = snapshot
            stable_since = time.monotonic()
        now = time.monotonic()
        stable = now - stable_since >= 2.0
        if now >= minimum_deadline and not active_tasks and stable:
            settled = True
            break
        if now >= deadline:
            break
        time.sleep(min(0.5, max(0.0, deadline - now)))

    final_snapshot = _rollout_snapshot(codex_home)
    overlapping_growth = sorted(
        path.as_posix()
        for path, before in foreground_snapshot.items()
        if path in final_snapshot and final_snapshot[path] != before
    )
    background_paths = sorted(
        (path for path in final_snapshot if path not in foreground_snapshot),
        key=lambda path: (final_snapshot[path][1], path.as_posix()),
    )
    timed_out = not settled
    complete = settled and not overlapping_growth
    return {
        "complete": complete,
        "timed_out": timed_out,
        "wait_ms": max(0, round((time.monotonic() - started) * 1000)),
        "minimum_observation_ms": max(0, round(minimum_observation_seconds * 1000)),
        "task_poll_count": task_poll_count,
        "terminal_task_count": len(terminal_tasks),
        "active_task_count": len(active_tasks),
        "terminal_task_statuses": sorted(
            str(task.get("status")) for task in terminal_tasks
        ),
        "background_paths": background_paths,
        "background_rollout_count": len(background_paths),
        "overlapping_foreground_rollout_count": len(overlapping_growth),
        "embedding_usage": "unavailable",
    }


def _daemon_compose_ms(events: list[dict[str, Any]]) -> int | None:
    assembled = [
        event for event in events if event.get("event") == "assemble_completed"
    ]
    if not assembled:
        return None
    values = [
        float(event["duration_ms"])
        for event in assembled
        if isinstance(event.get("duration_ms"), (int, float))
    ]
    return max(0, round(sum(values))) if values else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _catalog_entries(dynamic_tools: Any) -> list[dict[str, str]]:
    if not isinstance(dynamic_tools, list):
        return []
    entries: list[dict[str, str]] = []
    for item in dynamic_tools:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        kind = item.get("type")
        if isinstance(name, str) and name:
            entries.append(
                {"name": name, "type": kind if isinstance(kind, str) else ""}
            )
        nested = item.get("tools")
        if not isinstance(nested, list):
            continue
        for tool in nested:
            if not isinstance(tool, dict):
                continue
            child_name = tool.get("name")
            child_kind = tool.get("type")
            if isinstance(child_name, str) and child_name:
                entries.append(
                    {
                        "name": child_name,
                        "parent": name if isinstance(name, str) else "",
                        "type": child_kind if isinstance(child_kind, str) else "",
                    }
                )
    return sorted(entries, key=lambda item: json.dumps(item, sort_keys=True))


def _single_rollout_telemetry(path: Path) -> dict[str, Any]:
    records = read_jsonl(path)
    session_meta = next(
        (record for record in records if record.get("type") == "session_meta"),
        None,
    )
    payload = session_meta.get("payload") if isinstance(session_meta, dict) else None
    if not isinstance(payload, dict) or "base_instructions" not in payload:
        raise RuntimeError("native Codex rollout is missing its context attestation")
    catalog = _catalog_entries(payload.get("dynamic_tools"))
    if not catalog:
        raise RuntimeError("native Codex rollout is missing its tool catalog")
    external_catalog = [
        item for item in catalog if item["name"] not in INSTALLATION_MEMORY_TOOL_NAMES
    ]
    started: dict[str, dict[str, Any]] = {}
    tools: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") != "response_item":
            continue
        item = record.get("payload")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if item_type == "custom_tool_call":
            name = item.get("name")
            if isinstance(name, str) and name:
                started[call_id] = {
                    "name": name,
                    "status": item.get("status"),
                    "timestamp": _timestamp(record.get("timestamp")),
                }
        elif item_type == "custom_tool_call_output" and call_id in started:
            call = started.pop(call_id)
            finished = _timestamp(record.get("timestamp"))
            began = call["timestamp"]
            duration = (
                max(0, round((finished - began).total_seconds() * 1000))
                if isinstance(finished, datetime) and isinstance(began, datetime)
                else 0
            )
            tools.append(
                {
                    "name": call["name"],
                    "status": "ok" if call["status"] == "completed" else "error",
                    "duration_ms": duration,
                }
            )

    task_metrics: list[dict[str, int]] = []
    for record in records:
        item = record.get("payload")
        if (
            record.get("type") == "event_msg"
            and isinstance(item, dict)
            and item.get("type") == "task_complete"
        ):
            metrics: dict[str, int] = {}
            for key in ("duration_ms", "time_to_first_token_ms"):
                value = item.get(key)
                if isinstance(value, (int, float)):
                    metrics[key] = max(0, round(value))
            task_metrics.append(metrics)

    return {
        "context_sha256": json_sha256(payload["base_instructions"]),
        "tool_catalog_sha256": json_sha256(catalog),
        "external_tool_catalog_sha256": json_sha256(external_catalog),
        "installation_memory_tools": sorted(
            {
                item["name"]
                for item in catalog
                if item["name"] in INSTALLATION_MEMORY_TOOL_NAMES
            }
        ),
        "tool_calls": tools,
        "rollout_sha256": file_sha256(path),
        "event_count": len(records),
        "task_metrics": task_metrics,
        "token_scope": _rollout_token_scope(records),
    }


def _rollout_telemetry(
    codex_home: Path,
    *,
    paths: list[Path] | None = None,
) -> dict[str, Any]:
    paths = _rollout_paths(codex_home) if paths is None else paths
    if not paths:
        raise RuntimeError("native Codex rollout telemetry is unavailable")
    rollouts = [_single_rollout_telemetry(path) for path in paths]
    final = rollouts[-1]
    token_scopes = [
        rollout["token_scope"]
        for rollout in rollouts
        if rollout["token_scope"] is not None
    ]
    token_scope: dict[str, Any] | None = None
    if token_scopes:
        token_scope = {
            "call_count": sum(scope["call_count"] for scope in token_scopes),
        }
        for field in (
            "input_uncached",
            "cache_read",
            "cache_write",
            "output",
            "reasoning",
            "total",
        ):
            values = [scope[field]["value"] for scope in token_scopes]
            if all(isinstance(value, int) for value in values):
                token_scope[field] = {
                    "value": sum(values),
                    "quality": "host_reported",
                }
            else:
                token_scope[field] = {"value": None, "quality": "unavailable"}
    return {
        "context_sha256": final["context_sha256"],
        "tool_catalog_sha256": final["tool_catalog_sha256"],
        "external_tool_catalog_sha256": final["external_tool_catalog_sha256"],
        "installation_memory_tools": final["installation_memory_tools"],
        "tool_calls": [tool for rollout in rollouts for tool in rollout["tool_calls"]],
        "rollout_sha256": json_sha256(
            [rollout["rollout_sha256"] for rollout in rollouts]
        ),
        "rollout_count": len(rollouts),
        "event_count": sum(rollout["event_count"] for rollout in rollouts),
        "task_metrics": [
            metrics for rollout in rollouts for metrics in rollout["task_metrics"]
        ],
        "turn_count": sum(len(rollout["task_metrics"]) for rollout in rollouts),
        "token_scope": token_scope,
    }


def _daemon_token_scopes(
    events: list[dict[str, Any]],
    *,
    arm: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if arm == "A":
        return empty_token_scope(exact_zero=True), empty_token_scope(exact_zero=True)
    calls = [event for event in events if event.get("event") == "llm_call"]

    def is_after_turn(event: dict[str, Any]) -> bool:
        return str(event.get("phase") or "").startswith("after_turn") or str(
            event.get("thread_name") or ""
        ).startswith("daemon-after-turn-")

    critical = [event for event in calls if not is_after_turn(event)]
    background = [event for event in calls if is_after_turn(event)]

    def scope(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return empty_token_scope(exact_zero=True)
        usages = [
            event["provider_usage"]
            for event in items
            if isinstance(event.get("provider_usage"), dict)
        ]
        result = token_scope_from_usage(
            usages,
            quality="provider_reported",
            aliases=HOST_USAGE_ALIASES,
        )
        result["call_count"] = len(items)
        return result

    return scope(critical), scope(background)


def run_arm(
    *,
    campaign_id: str,
    arm: str,
    scenario_id: str,
    seed: int,
    pair_id: str,
    artifact_root: Path,
    runtime_parent: Path,
    openclaw_binary: Path,
    daemon_python: Path,
    daemon_env_path: Path,
    codex_plugin_root: Path,
    model: str,
    thinking: str,
    timeout_ms: int,
    full_lifecycle: bool = False,
    lifecycle_timeout_seconds: float = 420.0,
    keep_runtime: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if arm not in {"A", "C"}:
        raise ValueError(f"unsupported arm: {arm}")
    arm_started = time.perf_counter()
    runtime_parent.mkdir(parents=True, exist_ok=True)
    runtime_root = Path(
        tempfile.mkdtemp(prefix=f"openclaw-{arm.lower()}-", dir=runtime_parent)
    ).resolve()
    run_root = runtime_root / "run"
    state_dir = run_root / "openclaw-state"
    codex_home = state_dir / "agents" / "main" / "agent" / "codex-home"
    daemon_codex_home = run_root / "daemon-codex-home"
    memory_root = run_root / "memory"
    workspace = run_root / "workspace"
    control_path = run_root / "control.json"
    config_path = run_root / "openclaw.json"
    sandbox_profile = run_root / "openclaw-host.sb"
    daemon_trace_path = run_root / "daemon-plugin-trace.jsonl"
    telemetry_path = run_root / "daemon-llm.jsonl"
    latency_path = run_root / "daemon-latency.jsonl"
    daemon_log_path = run_root / "daemon-server.log"
    gateway_log_path = run_root / "openclaw-gateway.log"
    after_turn_log_path = run_root / "after-turn-service.log"
    scratch = run_root / "tmp"
    disposable_home = run_root / "home"
    run_id = f"{campaign_id}-openclaw-{seed}-{arm.lower()}"
    session_id = uuid.uuid4().hex
    server: subprocess.Popen[str] | None = None
    gateway: subprocess.Popen[str] | None = None
    after_turn_service: subprocess.Popen[str] | None = None
    server_log = None
    gateway_log = None
    after_turn_log = None
    prepared: PreparedScenario | None = None

    try:
        state_dir.mkdir(parents=True)
        scratch.mkdir(parents=True)
        memory_root.mkdir(parents=True)
        disposable_home.mkdir(parents=True)
        (run_root / "empty.env").touch()
        telemetry_path.touch()
        prepared = prepare_scenario(
            scenario_id=scenario_id,
            seed=seed,
            variant=_variant_for_arm(seed, arm),
            workspace=workspace,
            control_path=control_path,
            run_root=run_root,
            additional_baseline_files=("AGENTS.md",) if arm == "C" else (),
        )
        if arm == "C":
            workspace_agents = DAEMON_PLUGIN / "workspace" / "AGENTS.md"
            if not workspace_agents.is_file():
                raise RuntimeError(
                    f"daemon workspace AGENTS.md not found: {workspace_agents}"
                )
            shutil.copy2(workspace_agents, workspace / "AGENTS.md")
        fixture = prepared.fixture
        _prepare_codex_home(codex_home)
        if arm == "C":
            prepare_codex_cli_home(daemon_codex_home)
        _prepare_official_codex_install(
            state_dir=state_dir,
            codex_plugin_root=codex_plugin_root,
        )
        daemon_port = _free_port()
        gateway_port = _free_port() if full_lifecycle else None
        daemon_url = f"http://127.0.0.1:{daemon_port}"
        write_json(
            config_path,
            _config(
                arm=arm,
                workspace=workspace,
                daemon_url=daemon_url,
                model=model,
                gateway_port=gateway_port,
            ),
        )
        config_path.chmod(0o600)
        _write_outer_sandbox_profile(
            path=sandbox_profile,
            runtime_root=runtime_root,
            openclaw_binary=openclaw_binary,
            codex_plugin_root=codex_plugin_root,
        )
        common_env = _isolate_host_environment(
            base_process_env(),
            home=disposable_home,
        )
        common_env.update(
            {
                "OPENCLAW_STATE_DIR": str(state_dir),
                "OPENCLAW_CONFIG_PATH": str(config_path),
                "HOST_CAPABILITY_TRACE_FILE": str(daemon_trace_path),
                "HOST_CAPABILITY_CAMPAIGN_ID": campaign_id,
                "HOST_CAPABILITY_RUN_ID": run_id,
                "HOST_CAPABILITY_ARM": arm,
                "HOST_CAPABILITY_CASE_ID": scenario_id,
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        daemon_overlay: dict[str, str] = {}
        if arm == "C":
            daemon_overlay = isolated_daemon_env(
                safe_dotenv(daemon_env_path),
                run_root=run_root,
                memory_root=memory_root,
                user_id=BENCHMARK_USER_ID,
                platform="openclaw",
                telemetry_path=telemetry_path,
                latency_path=latency_path,
            )
            if full_lifecycle:
                daemon_overlay = durable_after_turn_env(
                    daemon_overlay,
                    user_id=BENCHMARK_USER_ID,
                )
        host_env, daemon_process_env = _split_process_envs(
            common_env,
            daemon_overlay,
        )
        daemon_process_env["PYTHONPATH"] = os.pathsep.join(
            (
                str(ROOT),
                str(ROOT / "src"),
                daemon_process_env.get("PYTHONPATH", ""),
            )
        ).rstrip(os.pathsep)
        if arm == "C":
            daemon_process_env["CODEX_HOME"] = str(daemon_codex_home)
        _validate_config(
            binary=openclaw_binary,
            workspace=workspace,
            env=host_env,
            sandbox_profile=sandbox_profile,
        )
        auth_import = _import_codex_auth(
            binary=openclaw_binary,
            codex_home=codex_home,
            workspace=workspace,
            env=host_env,
            sandbox_profile=sandbox_profile,
        )
        sandbox_attestation = _outer_sandbox_attestation(
            sandbox_profile=sandbox_profile,
            runtime_root=runtime_root,
        )
        if arm == "C":
            server_log = daemon_log_path.open("w", encoding="utf-8")
            server = subprocess.Popen(
                [
                    str(daemon_python),
                    "-m",
                    "uvicorn",
                    "server.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(daemon_port),
                    "--log-level",
                    "warning",
                ],
                cwd=ROOT,
                env=daemon_process_env,
                text=True,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            _wait_for_health(daemon_url, server, daemon_log_path)
            if full_lifecycle:
                after_turn_log = after_turn_log_path.open("w", encoding="utf-8")
                after_turn_service = subprocess.Popen(
                    [str(daemon_python), "-m", "daemon_core.after_turn_daemon"],
                    cwd=ROOT,
                    env=daemon_process_env,
                    text=True,
                    stdout=after_turn_log,
                    stderr=subprocess.STDOUT,
                )
                wait_for_after_turn_service(
                    process=after_turn_service,
                    spool_root=run_root / "after-turn-spool",
                    log_path=after_turn_log_path,
                )
        if full_lifecycle:
            assert gateway_port is not None
            gateway_log = gateway_log_path.open("w", encoding="utf-8")
            gateway_env = dict(host_env)
            gateway_env["OPENCLAW_SKIP_CHANNELS"] = "1"
            gateway = subprocess.Popen(
                _host_command(
                    sandbox_profile=sandbox_profile,
                    binary=openclaw_binary,
                    arguments=[
                        "gateway",
                        "run",
                        "--port",
                        str(gateway_port),
                        "--bind",
                        "loopback",
                        "--auth",
                        "none",
                    ],
                ),
                cwd=workspace,
                env=gateway_env,
                text=True,
                stdout=gateway_log,
                stderr=subprocess.STDOUT,
            )
            _wait_for_gateway(
                process=gateway,
                log_path=gateway_log_path,
                binary=openclaw_binary,
                workspace=workspace,
                env=host_env,
                sandbox_profile=sandbox_profile,
                port=gateway_port,
            )
        startup_ms = round((time.perf_counter() - arm_started) * 1000)
        history_prompts = tuple(fixture.get("history_user_prompts", ()))
        history_session_id = uuid.uuid4().hex if history_prompts else session_id
        turn_results: list[subprocess.CompletedProcess[str]] = []
        envelopes: list[dict[str, Any]] = []
        parse_errors: list[str] = []
        turn_durations_ms: list[int] = []
        native_memory_sync_ms = 0
        native_memory_sync_sha256: str | None = None
        native_memory_sync_attempts = 0
        timed_out = False
        prompts_and_sessions = [
            *((prompt, history_session_id) for prompt in history_prompts),
            (fixture["prompt"], session_id),
        ]
        foreground_rollouts: dict[Path, tuple[int, int]] = {}
        background_rollout_paths: set[Path] = set()
        native_background_windows: list[dict[str, Any]] = []
        for turn_index, (prompt, native_session_id) in enumerate(prompts_and_sessions):
            if arm == "A" and history_prompts and turn_index == len(history_prompts):
                (
                    native_memory_sync_ms,
                    native_memory_sync_sha256,
                    native_memory_sync_attempts,
                ) = _sync_native_memory(
                    binary=openclaw_binary,
                    workspace=workspace,
                    env=host_env,
                    sandbox_profile=sandbox_profile,
                )
            session_arguments = (
                ["--session-key", f"agent:main:explicit:{native_session_id}"]
                if full_lifecycle
                else ["--local", "--session-id", native_session_id]
            )
            command = _host_command(
                sandbox_profile=sandbox_profile,
                binary=openclaw_binary,
                arguments=[
                    "agent",
                    "--json",
                    *session_arguments,
                    "--model",
                    model,
                    "--thinking",
                    thinking,
                    "--timeout",
                    str(max(1, timeout_ms // 1000)),
                    "--message",
                    prompt,
                ],
            )
            turn_started = time.perf_counter()
            try:
                completed = run_command(
                    command,
                    cwd=workspace,
                    env=host_env,
                    timeout=timeout_ms / 1000 + 30,
                )
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    command,
                    124,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "timeout",
                )
                timed_out = True
            turn_durations_ms.append(round((time.perf_counter() - turn_started) * 1000))
            turn_results.append(completed)
            try:
                envelopes.append(json.loads(completed.stdout))
                parse_errors.append("")
            except json.JSONDecodeError as exc:
                envelopes.append({})
                parse_errors.append(str(exc))
            if completed.returncode != 0 and len(turn_results) <= len(history_prompts):
                raise RuntimeError(
                    "OpenClaw history turn failed before the scored resumption: "
                    + completed.stderr[-1000:]
                )
            returned_snapshot = _rollout_snapshot(codex_home)
            if full_lifecycle:
                window = _wait_for_native_background(
                    binary=openclaw_binary,
                    workspace=workspace,
                    env=host_env,
                    sandbox_profile=sandbox_profile,
                    codex_home=codex_home,
                    foreground_snapshot=returned_snapshot,
                    session_keys={
                        f"agent:main:explicit:{session_id}",
                        f"agent:main:explicit:{history_session_id}",
                    },
                    minimum_observation_seconds=(
                        35.0 if turn_index == len(prompts_and_sessions) - 1 else 0.0
                    ),
                    timeout_seconds=lifecycle_timeout_seconds,
                )
                paths = window.pop("background_paths")
                background_rollout_paths.update(paths)
                native_background_windows.append(window)
            current_snapshot = _rollout_snapshot(codex_home)
            foreground_rollouts = {
                path: details
                for path, details in current_snapshot.items()
                if path not in background_rollout_paths
            }
        lifecycle_started = time.perf_counter()
        gateway_shutdown_ms = 0
        native_background: dict[str, Any] = {
            "complete": bool(native_background_windows)
            and all(item["complete"] for item in native_background_windows),
            "timed_out": any(item["timed_out"] for item in native_background_windows),
            "wait_ms": sum(item["wait_ms"] for item in native_background_windows),
            "minimum_observation_ms": sum(
                item["minimum_observation_ms"] for item in native_background_windows
            ),
            "task_poll_count": sum(
                item["task_poll_count"] for item in native_background_windows
            ),
            "terminal_task_count": max(
                (item["terminal_task_count"] for item in native_background_windows),
                default=0,
            ),
            "active_task_count": sum(
                item["active_task_count"] for item in native_background_windows
            ),
            "terminal_task_statuses": (
                native_background_windows[-1]["terminal_task_statuses"]
                if native_background_windows
                else []
            ),
            "background_rollout_count": len(background_rollout_paths),
            "overlapping_foreground_rollout_count": sum(
                item["overlapping_foreground_rollout_count"]
                for item in native_background_windows
            ),
            "embedding_usage": "unavailable",
            "window_count": len(native_background_windows),
        }
        if full_lifecycle:
            gateway_shutdown_started = time.perf_counter()
            _stop_process(gateway)
            gateway = None
            gateway_shutdown_ms = round(
                (time.perf_counter() - gateway_shutdown_started) * 1000
            )
            post_shutdown_snapshot = _rollout_snapshot(codex_home)
            shutdown_background_paths = {
                path
                for path in post_shutdown_snapshot
                if path not in foreground_rollouts
                and path not in background_rollout_paths
            }
            shutdown_foreground_growth = {
                path
                for path, details in foreground_rollouts.items()
                if path in post_shutdown_snapshot
                and post_shutdown_snapshot[path] != details
            }
            background_rollout_paths.update(shutdown_background_paths)
            native_background["background_rollout_count"] = len(
                background_rollout_paths
            )
            native_background["gateway_shutdown_background_rollout_count"] = len(
                shutdown_background_paths
            )
            native_background["gateway_shutdown_foreground_growth_count"] = len(
                shutdown_foreground_growth
            )
            if shutdown_foreground_growth:
                native_background["complete"] = False
        lifecycle_snapshot: dict[str, Any] = {
            "complete": True,
            "timed_out": False,
            "terminal_turn_count": 0,
            "cycle_status_counts": {},
            "spool_counts": {},
            "telemetry_bytes": telemetry_path.stat().st_size,
        }
        completed_turns = completed_host_turn_count(turn_results)
        if full_lifecycle and arm == "C":
            assert after_turn_service is not None
            lifecycle_snapshot = wait_for_durable_after_turn(
                process=after_turn_service,
                spool_root=run_root / "after-turn-spool",
                receipt_path=run_root
                / "continuity-receipts"
                / "continuity_receipts.jsonl",
                telemetry_path=telemetry_path,
                host="openclaw",
                user_id=BENCHMARK_USER_ID,
                expected_turns=completed_turns,
                timeout_seconds=lifecycle_timeout_seconds,
            )
        lifecycle_ms = round((time.perf_counter() - lifecycle_started) * 1000)
        completed = turn_results[-1]
        assessment = prepared.assess()
        wall_ms = sum(turn_durations_ms) + native_memory_sync_ms
        daemon_events = read_jsonl(daemon_trace_path)
        internal_events = read_jsonl(telemetry_path)
        rollout = _rollout_telemetry(
            codex_home,
            paths=sorted(
                foreground_rollouts,
                key=lambda path: (
                    foreground_rollouts[path][1],
                    path.as_posix(),
                ),
            ),
        )
        background_rollout: dict[str, Any] | None = None
        background_paths = sorted(
            background_rollout_paths,
            key=lambda path: (path.stat().st_mtime_ns, path.as_posix()),
        )
        if background_paths:
            background_rollout = _rollout_telemetry(
                codex_home,
                paths=background_paths,
            )
        tools = rollout["tool_calls"]
        usages = [usage for item in envelopes if (usage := _extract_usage(item))]
        host_visible = rollout["token_scope"]
        if host_visible is None:
            host_visible = token_scope_from_usage(
                usages,
                quality="host_reported",
                aliases=HOST_USAGE_ALIASES,
            )
            if usages:
                host_visible["call_count"] = len(usages)
        if background_rollout is not None and background_rollout["token_scope"]:
            host_background = background_rollout["token_scope"]
        else:
            host_background = empty_token_scope(
                exact_zero=(
                    full_lifecycle
                    and native_background["complete"]
                    and not background_paths
                )
            )
        daemon_critical, daemon_background = _daemon_token_scopes(
            internal_events,
            arm=arm,
        )
        success = bool(assessment["passed"])
        output_path = prepared.definition.output_path
        bundle_output = artifact_root / "artifacts" / run_id / output_path
        source_output = workspace / output_path
        if source_output.is_file():
            bundle_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_output, bundle_output)
        result = {
            "schema_version": RUN_SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "pair_id": pair_id,
            "host_id": "openclaw",
            "arm_id": arm,
            "scenario_id": scenario_id,
            "seed": seed,
            "fixture_ref": fixture["fixture_ref"],
            "session_ref": public_session_ref(session_id),
            "language": LANGUAGE,
            "attempt": 1,
            "status": "completed",
            "timing": {
                "wall_ms": wall_ms,
                "startup_ms": startup_ms,
                "first_visible_ms": None,
                "correct_result_ms": (
                    wall_ms
                    if success
                    else timeout_ms * prepared.definition.episode_turns
                ),
                "host_ms": (
                    sum(
                        value for item in envelopes if (value := _extract_host_ms(item))
                    )
                    or None
                ),
                "daemon_compose_ms": (
                    _daemon_compose_ms(daemon_events) if arm == "C" else None
                ),
                "tool_ms": sum(item["duration_ms"] for item in tools),
            },
            "tokens": {
                "host.visible": host_visible,
                "daemon.critical": daemon_critical,
                "host.background": host_background,
                "daemon.background": daemon_background,
            },
            "tool_calls": tools,
            "outcome": {
                "authoritative": True,
                "checks": assessment["checks"],
                "success_first_attempt": success,
                "success_within_episode": success,
            },
            "exclusion": None,
            "artifacts": artifact_records(workspace, (output_path,)),
        }
        write_json(
            artifact_root / "diagnostics" / f"{run_id}.json",
            {
                "run_id": run_id,
                "returncode": completed.returncode,
                "turn_returncodes": [item.returncode for item in turn_results],
                "timed_out": timed_out,
                "stdout_json_parse_errors": parse_errors,
                "stderr_tail": (
                    ""
                    if all(item.returncode == 0 for item in turn_results)
                    else redact_local_paths(
                        "\n".join(item.stderr for item in turn_results)[-4000:],
                        runtime_root,
                    )
                ),
                "native_rollout_sha256": rollout["rollout_sha256"],
                "native_rollout_count": rollout["rollout_count"],
                "native_rollout_event_count": rollout["event_count"],
                "native_task_metrics": rollout["task_metrics"],
                "native_background": native_background,
                "native_background_rollout_sha256": (
                    background_rollout["rollout_sha256"]
                    if background_rollout is not None
                    else None
                ),
                "episode_turn_count": len(turn_results),
                "completed_host_turn_count": completed_turns,
                "history_message_count": prepared.definition.history_messages,
                "history_session_ref": (
                    public_session_ref(history_session_id) if history_prompts else None
                ),
                "native_memory_sync_ms": native_memory_sync_ms,
                "native_memory_sync_sha256": native_memory_sync_sha256,
                "native_memory_sync_attempts": native_memory_sync_attempts,
                "auth_import": auth_import,
                "daemon_trace_event_count": len(daemon_events),
                "internal_llm_event_count": len(internal_events),
                "first_visible_boundary": "unavailable_in_local_json_runner",
                "full_lifecycle": full_lifecycle,
                "gateway_shutdown_ms": gateway_shutdown_ms,
                "lifecycle_ms": lifecycle_ms if full_lifecycle else None,
                "total_lifecycle_ms": (
                    wall_ms + lifecycle_ms + native_background["wait_ms"]
                    if full_lifecycle
                    else None
                ),
                "lifecycle": lifecycle_snapshot,
            },
        )
        append_jsonl(artifact_root / "runs.jsonl", result)
        return result, {
            "context_sha256": rollout["context_sha256"],
            "tool_catalog_sha256": rollout["tool_catalog_sha256"],
            "external_tool_catalog_sha256": rollout["external_tool_catalog_sha256"],
            "canonical_path_observed": (
                True
                if arm == "A"
                else any(
                    event.get("event") == "assemble_completed"
                    and event.get("voice_path") == "canonical"
                    and event.get("degraded") is not True
                    for event in daemon_events
                )
            ),
            "episode_turns_observed": rollout["turn_count"],
            "completed_host_turn_count": completed_turns,
            "installation_memory_tools": rollout["installation_memory_tools"],
            "native_memory_sync_observed": (
                not history_prompts
                or arm == "C"
                or native_memory_sync_sha256 is not None
            ),
            "durable_lifecycle_complete": (
                lifecycle_snapshot.get("complete") is True
                if full_lifecycle and arm == "C"
                else True
            ),
            "persistent_host_path": full_lifecycle,
            "native_background_complete": (
                native_background["complete"] if full_lifecycle else None
            ),
            "persistent_conversation_count": 2 if history_prompts else 1,
            "sandbox": sandbox_attestation,
            "disposable_home": True,
        }
    finally:
        _stop_process(gateway)
        _stop_process(after_turn_service)
        _stop_process(server)
        if gateway_log is not None:
            gateway_log.close()
        if after_turn_log is not None:
            after_turn_log.close()
        if server_log is not None:
            server_log.close()
        if prepared is not None:
            prepared.close()
        if not keep_runtime:
            shutil.rmtree(runtime_root, ignore_errors=True)


def run_pair(
    *,
    campaign_id: str,
    scenario_id: str,
    seed: int,
    artifact_root: Path,
    runtime_parent: Path,
    openclaw_binary: Path,
    daemon_python: Path,
    daemon_env_path: Path,
    model: str,
    thinking: str,
    timeout_ms: int,
    arm_order: tuple[str, str] = ("A", "C"),
    full_lifecycle: bool = False,
    lifecycle_timeout_seconds: float = 420.0,
    keep_runtime: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    codex_plugin_root = _discover_codex_plugin(openclaw_binary)
    get_scenario(scenario_id)
    pair_id = f"openclaw-{scenario_id.replace('.', '-')}-{seed}"
    runs: list[dict[str, Any]] = []
    attestations: dict[str, dict[str, Any]] = {}
    for arm in arm_order:
        run, attestation = run_arm(
            campaign_id=campaign_id,
            arm=arm,
            scenario_id=scenario_id,
            seed=seed,
            pair_id=pair_id,
            artifact_root=artifact_root,
            runtime_parent=runtime_parent,
            openclaw_binary=openclaw_binary,
            daemon_python=daemon_python,
            daemon_env_path=daemon_env_path,
            codex_plugin_root=codex_plugin_root,
            model=model,
            thinking=thinking,
            timeout_ms=timeout_ms,
            full_lifecycle=full_lifecycle,
            lifecycle_timeout_seconds=lifecycle_timeout_seconds,
            keep_runtime=keep_runtime,
        )
        runs.append(run)
        attestations[arm] = attestation
    return runs, attestations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument(
        "--scenario-id",
        choices=tuple(sorted(SCENARIOS)),
        default="native.files.aggregate",
    )
    parser.add_argument("--seed", type=int, default=1101)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--runtime-parent", type=Path, default=DEFAULT_RUNTIME_PARENT)
    parser.add_argument("--openclaw-binary", type=Path, default=DEFAULT_OPENCLAW_BINARY)
    parser.add_argument("--daemon-python", type=Path, default=DEFAULT_DAEMON_PYTHON)
    parser.add_argument("--daemon-env", type=Path, default=DEFAULT_DAEMON_ENV)
    parser.add_argument("--model", default="openai/gpt-5.6-terra")
    parser.add_argument("--thinking", default="medium")
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--arm-order", choices=("ac", "ca"), default="ac")
    parser.add_argument("--keep-runtime", action="store_true")
    parser.add_argument("--full-lifecycle", action="store_true")
    parser.add_argument("--lifecycle-timeout-seconds", type=float, default=420.0)
    args = parser.parse_args()
    try:
        runs, attestations = run_pair(
            campaign_id=args.campaign_id,
            scenario_id=args.scenario_id,
            seed=args.seed,
            artifact_root=args.artifact_root.expanduser().resolve(),
            runtime_parent=args.runtime_parent.expanduser().resolve(),
            openclaw_binary=executable_path(args.openclaw_binary),
            daemon_python=executable_path(args.daemon_python),
            daemon_env_path=args.daemon_env.expanduser().resolve(),
            model=args.model,
            thinking=args.thinking,
            timeout_ms=args.timeout_ms,
            arm_order=("A", "C") if args.arm_order == "ac" else ("C", "A"),
            full_lifecycle=args.full_lifecycle,
            lifecycle_timeout_seconds=args.lifecycle_timeout_seconds,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"OpenClaw benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "runs": [run["run_id"] for run in runs],
                "attestations": attestations,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
