"""Run a Productivity V1 scenario in disposable Hermes A/C profiles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid
import re

import yaml

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
    tool_events,
    wait_for_after_turn_service,
    wait_for_durable_after_turn,
    write_dotenv,
    write_json,
)
from benchmarks.productivity_v1.scenarios import (
    SCENARIOS,
    PreparedScenario,
    get_scenario,
    prepare_scenario,
)


ROOT = Path(__file__).resolve().parents[3]
OBSERVER = Path(__file__).resolve().parent / "hermes_observer"
DAEMON_MEMORY_PLUGIN = ROOT / "integrations" / "hermes_plugin"
DAEMON_VOICE_PLUGIN = ROOT / "integrations" / "hermes_voice_plugin"
DEFAULT_HERMES_HOME = Path.home() / ".hermes"
DEFAULT_HERMES_BINARY = (
    Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
)
DEFAULT_RUNTIME_PARENT = (
    Path.home() / ".cache" / "daemon-public-productivity-v1" / "runtime"
)
BENCHMARK_USER_ID = "productivity-benchmark"
HOST_USAGE_ALIASES = {
    "input_uncached": (
        "input_uncached_tokens",
        "input_tokens",
        "input",
        "prompt_tokens_uncached",
    ),
    "cache_read": (
        "cached_input_tokens",
        "cache_read_tokens",
        "cache_read",
        "cacheRead",
    ),
    "cache_write": (
        "cache_write_input_tokens",
        "cache_write_tokens",
        "cache_write",
        "cacheWrite",
    ),
    "output": ("output_tokens", "output", "completion_tokens"),
    "reasoning": ("reasoning_output_tokens", "reasoning_tokens", "reasoning"),
    "total": ("total_tokens", "total"),
}
INSTALLATION_MEMORY_TOOL_NAMES = {
    "memory",
    "daemon_read_souvenirs",
    "daemon_recall_l0",
}


def _variant_for_arm(seed: int, arm: str) -> str:
    reverse = seed % 2 == 0
    if (arm == "A") ^ reverse:
        return "left"
    return "right"


def _prepare_profile(
    *,
    source_home: Path,
    profile: Path,
    arm: str,
    model: str,
    provider: str,
    daemon_values: dict[str, str],
) -> None:
    profile.mkdir(parents=True)
    for directory in ("logs", "sessions", "memories", "plugins", "state", "skills"):
        (profile / directory).mkdir()

    enabled = ["productivity_benchmark_observer"]
    memory = {
        "memory_enabled": True,
        "user_profile_enabled": True,
        "provider": "",
        "write_approval": False,
    }
    skills = {"creation_nudge_interval": 10}
    auxiliary = {"background_review": {"enabled": True}}
    display: dict[str, Any] = {}
    if arm == "C":
        enabled.extend(("daemon_core", "daemon_voice"))
        memory.update(
            {
                "memory_enabled": False,
                "user_profile_enabled": False,
                "provider": "daemon_core",
            }
        )
        skills["creation_nudge_interval"] = 0
        auxiliary["background_review"]["enabled"] = False
        display["memory_notifications"] = "off"
    config = {
        "model": {"default": model, "provider": provider, "base_url": ""},
        "plugins": {
            "enabled": enabled,
            "disabled": [],
            "hook_callback_timeout": 600,
        },
        "memory": memory,
        "skills": skills,
        "auxiliary": auxiliary,
        "display": display,
        "toolsets": ["hermes-cli"],
        "streaming": True,
        "timezone": "UTC",
    }
    (profile / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    auth = source_home / "auth.json"
    if auth.is_file():
        shutil.copy2(auth, profile / "auth.json")
        (profile / "auth.json").chmod(0o600)
    write_dotenv(profile / ".env", daemon_values)
    (profile / "plugins" / "productivity_benchmark_observer").symlink_to(
        OBSERVER,
        target_is_directory=True,
    )
    if arm == "C":
        (profile / "plugins" / "daemon_core").symlink_to(
            DAEMON_MEMORY_PLUGIN,
            target_is_directory=True,
        )
        (profile / "plugins" / "daemon_voice").symlink_to(
            DAEMON_VOICE_PLUGIN,
            target_is_directory=True,
        )


def _trace_timing(
    events: list[dict[str, Any]],
    *,
    turn_started_epoch: float,
) -> dict[str, int | None]:
    turn_starts = [
        event
        for event in events
        if event.get("event") == "turn_start"
        and isinstance(event.get("epoch"), (int, float))
        and float(event["epoch"]) >= turn_started_epoch - 0.01
    ]
    first_texts = [
        event
        for event in events
        if event.get("event") == "first_visible_text"
        and isinstance(event.get("epoch"), (int, float))
        and float(event["epoch"]) >= turn_started_epoch - 0.01
    ]
    origin = (
        float(turn_starts[0]["epoch"])
        if turn_starts and isinstance(turn_starts[0].get("epoch"), (int, float))
        else turn_started_epoch
    )
    first_epoch = (
        float(first_texts[0]["epoch"])
        if first_texts and isinstance(first_texts[0].get("epoch"), (int, float))
        else None
    )
    return {
        "first_visible_ms": (
            max(0, round((first_epoch - origin) * 1000))
            if first_epoch is not None
            else None
        )
    }


def _daemon_compose_ms(latency_events: list[dict[str, Any]]) -> int | None:
    voice_starts = [
        event for event in latency_events if event.get("event") == "voice_start"
    ]
    compose_values = [
        float(event["compose"])
        for event in voice_starts
        if isinstance(event.get("compose"), (int, float))
    ]
    if compose_values:
        return max(0, round(sum(compose_values) * 1000))
    prefetches = [
        event for event in latency_events if event.get("event") == "prefetch_return"
    ]
    prefetch_values = [
        float(event["prefetch_call"])
        for event in prefetches
        if isinstance(event.get("prefetch_call"), (int, float))
    ]
    if prefetch_values:
        return max(0, round(sum(prefetch_values) * 1000))
    return None


def _quiet_chat_command(
    *,
    binary: Path,
    prompt: str,
    model: str,
    provider: str,
    resume: str | None = None,
) -> list[str]:
    command = [
        str(binary),
        "chat",
        "--query",
        prompt,
        "--quiet",
        "--model",
        model,
        "--provider",
        provider,
        "--toolsets",
        "hermes-cli",
    ]
    if resume:
        command.extend(("--resume", resume))
    return command


def _session_id_from_stderr(stderr: str) -> str:
    matches = re.findall(r"(?:^|\n)session_id:\s*([^\s]+)", stderr)
    if not matches:
        raise RuntimeError("Hermes quiet chat did not report its native session id")
    return matches[-1]


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
        usages = [
            event["provider_usage"]
            for event in items
            if isinstance(event.get("provider_usage"), dict)
        ]
        if not items:
            return empty_token_scope(exact_zero=True)
        result = token_scope_from_usage(
            usages,
            quality="provider_reported",
            aliases=HOST_USAGE_ALIASES,
        )
        result["call_count"] = len(items)
        return result

    return scope(critical), scope(background)


def _host_attestation(events: list[dict[str, Any]]) -> dict[str, Any]:
    requests = [event for event in events if event.get("event") == "api_request_start"]
    payload = requests[0].get("payload", {}) if requests else {}
    tool_names = payload.get("tool_names")
    external_tools = (
        sorted(
            name
            for name in tool_names
            if isinstance(name, str)
            and name not in INSTALLATION_MEMORY_TOOL_NAMES
        )
        if isinstance(tool_names, list)
        else []
    )
    installation_memory_tools = (
        sorted(
            name
            for name in tool_names
            if isinstance(name, str)
            and name in INSTALLATION_MEMORY_TOOL_NAMES
        )
        if isinstance(tool_names, list)
        else []
    )
    return {
        "context_sha256": str(payload.get("system_sha256") or "0" * 64),
        "tool_catalog_sha256": str(payload.get("tool_catalog_sha256") or "0" * 64),
        "external_tool_catalog_sha256": json_sha256(external_tools),
        "installation_memory_tools": installation_memory_tools,
    }


def _host_token_scopes(
    events: list[dict[str, Any]],
    *,
    expected_barriers: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    completed = [
        event for event in events if event.get("event") == "api_request_complete"
    ]
    visible_calls = [
        event
        for event in completed
        if event.get("payload", {}).get("phase") != "native_background"
    ]
    background_calls = [
        event
        for event in completed
        if event.get("payload", {}).get("phase") == "native_background"
    ]

    def measured_scope(calls: list[dict[str, Any]]) -> dict[str, Any]:
        usages = [
            event["payload"]["usage"]
            for event in calls
            if isinstance(event.get("payload", {}).get("usage"), dict)
        ]
        if len(usages) != len(calls):
            return empty_token_scope(call_count=len(calls))
        scope = token_scope_from_usage(
            usages,
            quality="provider_reported",
            aliases=HOST_USAGE_ALIASES,
        )
        scope["call_count"] = len(calls)
        return scope

    visible = measured_scope(visible_calls)
    barriers = [
        event.get("payload", {})
        for event in events
        if event.get("event") == "native_background_barrier"
    ]
    probe_installed = any(
        event.get("event") == "native_background_probe_installed" for event in events
    )
    complete = (
        expected_barriers > 0
        and probe_installed
        and len(barriers) == expected_barriers
        and all(item.get("complete") is True for item in barriers)
    )
    if background_calls:
        background = measured_scope(background_calls)
    else:
        background = empty_token_scope(exact_zero=complete)
    return (
        visible,
        background,
        {
            "probe_installed": probe_installed,
            "barrier_count": len(barriers),
            "expected_barrier_count": expected_barriers,
            "complete": complete,
            "scheduled": sum(int(item.get("scheduled", 0)) for item in barriers),
            "completed": sum(int(item.get("completed", 0)) for item in barriers),
            "failed": sum(int(item.get("failed", 0)) for item in barriers),
            "wait_ms": sum(int(item.get("wait_ms", 0)) for item in barriers),
            "embedding_usage": "unavailable",
        },
    )


def run_arm(
    *,
    campaign_id: str,
    arm: str,
    scenario_id: str,
    seed: int,
    pair_id: str,
    artifact_root: Path,
    runtime_parent: Path,
    hermes_binary: Path,
    hermes_home: Path,
    model: str,
    provider: str,
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
        tempfile.mkdtemp(prefix=f"hermes-{arm.lower()}-", dir=runtime_parent)
    ).resolve()
    run_root = runtime_root / "run"
    profile = run_root / "hermes-root" / "profiles" / BENCHMARK_USER_ID
    memory_root = run_root / "memory"
    daemon_codex_home = run_root / "daemon-codex-home"
    workspace = run_root / "workspace"
    control_path = run_root / "control.json"
    trace_path = run_root / "host-trace.jsonl"
    telemetry_path = run_root / "daemon-llm.jsonl"
    latency_path = run_root / "daemon-latency.jsonl"
    after_turn_log_path = run_root / "after-turn-service.log"
    scratch = run_root / "tmp"
    run_id = f"{campaign_id}-hermes-{seed}-{arm.lower()}"
    session_id = uuid.uuid4().hex
    prepared: PreparedScenario | None = None
    after_turn_service: subprocess.Popen[str] | None = None
    after_turn_log = None

    try:
        scratch.mkdir(parents=True)
        memory_root.mkdir(parents=True)
        (run_root / "empty.env").touch()
        telemetry_path.touch()
        prepared = prepare_scenario(
            scenario_id=scenario_id,
            seed=seed,
            variant=_variant_for_arm(seed, arm),
            workspace=workspace,
            control_path=control_path,
            run_root=run_root,
        )
        fixture = prepared.fixture
        base_values = safe_dotenv(hermes_home / ".env")
        if arm == "C":
            prepare_codex_cli_home(daemon_codex_home)
            profile_values = isolated_daemon_env(
                base_values,
                run_root=run_root,
                memory_root=memory_root,
                user_id=BENCHMARK_USER_ID,
                platform="cli",
                telemetry_path=telemetry_path,
                latency_path=latency_path,
            )
            if full_lifecycle:
                profile_values = durable_after_turn_env(
                    profile_values,
                    user_id=BENCHMARK_USER_ID,
                )
            profile_values["CODEX_HOME"] = str(daemon_codex_home)
        else:
            profile_values = dict(base_values)
            profile_values.update(
                {
                    "DAEMON_CORE_SOVEREIGN_VOICE": "off",
                    "DAEMON_CORE_SINGLE_PASS": "off",
                    "DAEMON_CORE_SOVEREIGN_LIBRARY": "off",
                    "DAEMON_CORE_THIN_SOVEREIGN_VOICE": "off",
                    "DAEMON_CORE_SCENE_MODEL": "off",
                    "DAEMON_CORE_L0_RECALL_TOOL": "off",
                }
            )
        _prepare_profile(
            source_home=hermes_home,
            profile=profile,
            arm=arm,
            model=model,
            provider=provider,
            daemon_values=profile_values,
        )
        startup_ms = round((time.perf_counter() - arm_started) * 1000)
        env = base_process_env()
        env.update(profile_values)
        env.update(
            {
                "HERMES_HOME": str(profile),
                "HERMES_CONFIG": str(profile / "config.yaml"),
                "HERMES_ENV": str(profile / ".env"),
                "PRODUCTIVITY_BENCH_TRACE_FILE": str(trace_path),
                "HERMES_WRITE_SAFE_ROOT": os.pathsep.join(
                    (str(workspace), str(scratch))
                ),
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOST_CAPABILITY_CAMPAIGN_ID": campaign_id,
                "HOST_CAPABILITY_RUN_ID": run_id,
                "HOST_CAPABILITY_ARM": arm,
                "HOST_CAPABILITY_CASE_ID": scenario_id,
                "PYTHONPATH": os.pathsep.join(
                    (str(ROOT), str(ROOT / "src"), env.get("PYTHONPATH", ""))
                ).rstrip(os.pathsep),
            }
        )
        if full_lifecycle:
            env["PRODUCTIVITY_BENCH_WAIT_NATIVE_BACKGROUND"] = "1"
            env["PRODUCTIVITY_BENCH_NATIVE_BACKGROUND_TIMEOUT_SECONDS"] = str(
                lifecycle_timeout_seconds
            )
        if full_lifecycle and arm == "C":
            after_turn_log = after_turn_log_path.open("w", encoding="utf-8")
            after_turn_service = subprocess.Popen(
                [sys.executable, "-m", "daemon_core.after_turn_daemon"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=after_turn_log,
                stderr=subprocess.STDOUT,
            )
            wait_for_after_turn_service(
                process=after_turn_service,
                spool_root=run_root / "after-turn-spool",
                log_path=after_turn_log_path,
            )
        history_prompts = tuple(fixture.get("history_user_prompts", ()))
        turn_results: list[subprocess.CompletedProcess[str]] = []
        turn_durations_ms: list[int] = []
        timed_out = False
        history_session_id: str | None = None

        for history_prompt in history_prompts:
            command = _quiet_chat_command(
                binary=hermes_binary,
                prompt=history_prompt,
                model=model,
                provider=provider,
                resume=history_session_id,
            )
            started = time.perf_counter()
            try:
                completed = run_command(
                    command,
                    cwd=workspace,
                    env=env,
                    timeout=(
                        timeout_ms / 1000 + lifecycle_timeout_seconds
                        if full_lifecycle
                        else timeout_ms / 1000
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    command,
                    124,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "timeout",
                )
                timed_out = True
            turn_durations_ms.append(round((time.perf_counter() - started) * 1000))
            turn_results.append(completed)
            if completed.returncode != 0:
                raise RuntimeError(
                    "Hermes history turn failed before the scored resumption: "
                    + completed.stderr[-1000:]
                )
            history_session_id = _session_id_from_stderr(completed.stderr)

        command = _quiet_chat_command(
            binary=hermes_binary,
            prompt=fixture["prompt"],
            model=model,
            provider=provider,
        )
        turn_started_epoch = time.time()
        turn_started = time.perf_counter()
        try:
            completed = run_command(
                command,
                cwd=workspace,
                env=env,
                timeout=(
                    timeout_ms / 1000 + lifecycle_timeout_seconds
                    if full_lifecycle
                    else timeout_ms / 1000
                ),
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
        if completed.returncode == 0:
            session_id = _session_id_from_stderr(completed.stderr)
        lifecycle_started = time.perf_counter()
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
                host="hermes",
                user_id=BENCHMARK_USER_ID,
                expected_turns=completed_turns,
                timeout_seconds=lifecycle_timeout_seconds,
            )
        lifecycle_ms = round((time.perf_counter() - lifecycle_started) * 1000)
        stderr = "\n".join(item.stderr for item in turn_results)
        assessment = prepared.assess()
        process_wall_ms = sum(turn_durations_ms)
        host_events = read_jsonl(trace_path)
        latency_events = read_jsonl(latency_path)
        internal_events = read_jsonl(telemetry_path)
        tools = tool_events(host_events)
        host_visible, host_background, native_background = _host_token_scopes(
            host_events,
            expected_barriers=len(turn_results) if full_lifecycle else 0,
        )
        wall_ms = max(0, process_wall_ms - native_background["wait_ms"])
        daemon_critical, daemon_background = _daemon_token_scopes(
            internal_events,
            arm=arm,
        )
        timing = _trace_timing(
            host_events,
            turn_started_epoch=turn_started_epoch,
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
            "host_id": "hermes",
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
                "first_visible_ms": timing["first_visible_ms"],
                "correct_result_ms": (
                    wall_ms
                    if success
                    else timeout_ms * prepared.definition.episode_turns
                ),
                "host_ms": None,
                "daemon_compose_ms": (
                    _daemon_compose_ms(latency_events) if arm == "C" else None
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
                "episode_turn_count": len(turn_results),
                "completed_host_turn_count": completed_turns,
                "history_message_count": prepared.definition.history_messages,
                "history_session_ref": (
                    public_session_ref(history_session_id)
                    if history_session_id is not None
                    else None
                ),
                "timed_out": timed_out,
                "stderr_tail": (
                    ""
                    if all(item.returncode == 0 for item in turn_results)
                    else redact_local_paths(stderr[-4000:], runtime_root)
                ),
                "response_chars": len(completed.stdout),
                "host_trace_sha256": file_sha256(trace_path)
                if trace_path.is_file()
                else None,
                "daemon_trace_event_count": len(latency_events),
                "internal_llm_event_count": len(internal_events),
                "full_lifecycle": full_lifecycle,
                "lifecycle_ms": lifecycle_ms if full_lifecycle else None,
                "total_lifecycle_ms": (
                    wall_ms + lifecycle_ms + native_background["wait_ms"]
                    if full_lifecycle
                    else None
                ),
                "lifecycle": lifecycle_snapshot,
                "native_background": native_background,
            },
        )
        append_jsonl(artifact_root / "runs.jsonl", result)
        attestation = _host_attestation(host_events)
        attestation["canonical_path_observed"] = (
            True
            if arm == "A"
            else any(
                event.get("event") == "voice_start"
                and event.get("delegated") is True
                and event.get("degraded") is not True
                for event in latency_events
            )
        )
        attestation["episode_turns_observed"] = len(turn_results)
        attestation["completed_host_turn_count"] = completed_turns
        attestation["durable_lifecycle_complete"] = (
            lifecycle_snapshot.get("complete") is True
            if full_lifecycle and arm == "C"
            else True
        )
        attestation["persistent_host_path"] = full_lifecycle
        attestation["native_background_complete"] = (
            native_background["complete"] if full_lifecycle else None
        )
        attestation["native_background_scheduled"] = (
            native_background["scheduled"] if full_lifecycle else None
        )
        attestation["native_background_call_count"] = (
            host_background["call_count"] if full_lifecycle else None
        )
        attestation["persistent_conversation_count"] = 2 if history_prompts else 1
        return result, attestation
    finally:
        if after_turn_service is not None:
            after_turn_service.terminate()
            try:
                after_turn_service.wait(timeout=10)
            except subprocess.TimeoutExpired:
                after_turn_service.kill()
                after_turn_service.wait(timeout=5)
        if after_turn_log is not None:
            after_turn_log.close()
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
    hermes_binary: Path,
    hermes_home: Path,
    model: str,
    provider: str,
    timeout_ms: int,
    arm_order: tuple[str, str] = ("A", "C"),
    full_lifecycle: bool = False,
    lifecycle_timeout_seconds: float = 420.0,
    keep_runtime: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    get_scenario(scenario_id)
    pair_id = f"hermes-{scenario_id.replace('.', '-')}-{seed}"
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
            hermes_binary=hermes_binary,
            hermes_home=hermes_home,
            model=model,
            provider=provider,
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
    parser.add_argument("--hermes-binary", type=Path, default=DEFAULT_HERMES_BINARY)
    parser.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME)
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--provider", default="openai-codex")
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
            hermes_binary=executable_path(args.hermes_binary),
            hermes_home=args.hermes_home.expanduser().resolve(),
            model=args.model,
            provider=args.provider,
            timeout_ms=args.timeout_ms,
            arm_order=("A", "C") if args.arm_order == "ac" else ("C", "A"),
            full_lifecycle=args.full_lifecycle,
            lifecycle_timeout_seconds=args.lifecycle_timeout_seconds,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Hermes benchmark failed: {exc}", file=sys.stderr)
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
