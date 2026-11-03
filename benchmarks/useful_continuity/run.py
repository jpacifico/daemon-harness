"""Run UCB through native Hermes CLI and the canonical after-turn service.

Reuses the qualified Productivity V1 observer and accounting utilities.
No private memory is seeded, and no application source is patched.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import yaml

from benchmarks.useful_continuity import VERSION
from benchmarks.useful_continuity.corpus import CASES, Case
from benchmarks.productivity_v1.runner import hermes as shared
from benchmarks.productivity_v1.runner.common import (
    append_jsonl, durable_after_turn_env, file_sha256, isolated_daemon_env,
    prepare_codex_cli_home, read_jsonl, safe_dotenv, source_revision,
    tool_events, wait_for_after_turn_service, wait_for_durable_after_turn,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]
REAL_HOME = Path.home()
MODEL = "gpt-6-astra"
CODEX_BINARY = Path(os.environ.get("UCB_CODEX_BINARY", "/opt/homebrew/bin/codex")).absolute()
USER = "ucb-evaluation"
VOICE_TIMEOUT = 120
MAINTENANCE_TIMEOUT = 240
ENGINEERING = Case(
    "engineering", "engineering", False,
    ("I am making a small scrapbook for my cousin's birthday. I have printed the photos already.",
     "The photos are sorted into three envelopes. I still need to choose a simple cover and write a short opening note."),
    "What would you start with?",
    ("Suggest a useful next step.", "Do not reprint the photos.", "Avoid repeating resolved questions."),
)


def write_sandbox(path: Path, runtime: Path, campaign: Path, host_root: Path) -> None:
    readable = (
        runtime, host_root, ROOT / "src", ROOT / "integrations",
        shared.OBSERVER, Path(sys.executable).absolute().parent.parent,
        Path(sys.executable).resolve().parent.parent,
        CODEX_BINARY.parent.parent, CODEX_BINARY.resolve().parent.parent,
        REAL_HOME / ".local/bin/python3.11",
        REAL_HOME / ".local/share/uv/python/cpython-3.11-macos-aarch64-none",
    )
    traversal = set()
    for item in readable:
        traversal.update(item.parents)
    lines = ["(version 1)", "(allow default)"]
    for denied in (REAL_HOME, campaign):
        lines += [f"(deny file-read* (subpath {json.dumps(str(denied))}))",
                  f"(deny file-write* (subpath {json.dumps(str(denied))}))"]
    lines += [f"(allow file-read-metadata (literal {json.dumps(str(p))}))"
              for p in sorted(traversal)]
    lines += [f"(allow file-read* (subpath {json.dumps(str(p))}))" for p in readable]
    # Python discovers namespace packages by listing its import root. This
    # permits directory enumeration, not reading the benchmark corpus below it.
    lines += [f"(allow file-read-data (literal {json.dumps(str(ROOT))}))"]
    lines += [f"(allow file-write* (subpath {json.dumps(str(runtime))}))"]
    path.write_text("\n".join(lines) + "\n")


def command(sandbox: Path, args: list[str]) -> list[str]:
    return ["/usr/bin/sandbox-exec", "-f", str(sandbox), *args]


def attest_sandbox(sandbox: Path, runtime: Path, campaign: Path) -> dict:
    probe = runtime / "write-probe"
    result = subprocess.run(command(sandbox, ["/usr/bin/touch", str(probe)]), capture_output=True)
    if result.returncode or not probe.exists():
        raise RuntimeError("Sandbox runtime write probe failed")
    probe.unlink()
    forbidden = (REAL_HOME / ".hermes" / "config.yaml", REAL_HOME / ".codex" / "auth.json",
                 campaign / "plan.json", ROOT / "benchmarks/useful_continuity/corpus.py")
    for p in forbidden:
        result = subprocess.run(command(sandbox, ["/usr/bin/head", "-c", "0", str(p)]), capture_output=True)
        if result.returncode == 0:
            raise RuntimeError("Sandbox permits private state or evaluation criteria")
    return {"sandbox": "macos-sandbox-exec", "runtime_writable": True,
            "personal_files_and_criteria_blocked": True, "profile_sha256": file_sha256(sandbox)}


def process_run(args: list[str], *, env: dict, cwd: Path, timeout: int) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(args, env=env, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
    return proc.returncode, out, err, timed_out


def internal_scope(events: list[dict], arm: str) -> tuple[dict, dict]:
    critical, background = shared._daemon_token_scopes(events, arm=arm)
    for scope, is_background in ((critical, False), (background, True)):
        calls = [e for e in events if e.get("event") == "llm_call" and (
            str(e.get("phase", "")).startswith("after_turn") or
            str(e.get("thread_name", "")).startswith("daemon-after-turn-")) == is_background]
        if any(not isinstance(e.get("provider_usage"), dict) for e in calls):
            # Partial sums must never masquerade as a complete token total.
            for k, v in scope.items():
                if isinstance(v, dict):
                    scope[k] = {"value": None, "quality": "unavailable"}
    return critical, background


def visible_response(profile: Path, session_id: str, stdout: str) -> tuple[str, str]:
    """Separate CLI startup diagnostics only when the exact reply is a suffix."""
    database = profile / "state.db"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' "
            "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    content = row[0].strip() if row else ""
    output = stdout.strip()
    if content and output.endswith(content):
        return content, output[:-len(content)].strip()
    return output, ""


def run_episode(case: Case, arm: str, campaign: Path, *, dry: bool) -> dict:
    run_id = f"{case.id}-{arm}"
    runtime = campaign / "private" / run_id
    if runtime.exists():
        if any(e.get("event") == "api_request_start" for e in read_jsonl(runtime / "host-trace.jsonl")) or read_jsonl(runtime / "daemon-llm.jsonl"):
            raise RuntimeError(f"Refusing to overwrite an episode which reached a model: {run_id}")
        archive = runtime.with_name(f"{run_id}-preflight-{time.time_ns()}")
        runtime.rename(archive)
    runtime.mkdir(parents=True)
    profile = runtime / "hermes-root" / "profiles" / USER
    memory = runtime / "memory"
    workspace = runtime / "workspace"
    scratch = runtime / "tmp"
    for p in (memory, workspace, scratch, runtime / "home"):
        p.mkdir()
    (runtime / "empty.env").touch()
    trace = runtime / "host-trace.jsonl"
    telemetry = runtime / "daemon-llm.jsonl"
    latency = runtime / "daemon-latency.jsonl"
    telemetry.touch()
    source = {k: v for k, v in safe_dotenv(shared.DEFAULT_HERMES_HOME / ".env").items()
              if k.startswith("DAEMON_CORE_") and "/" not in v}
    values = isolated_daemon_env(source, run_root=runtime, memory_root=memory,
                                user_id=USER, platform="cli", telemetry_path=telemetry,
                                latency_path=latency)
    values = durable_after_turn_env(values, user_id=USER)
    values.update({
        "DAEMON_CORE_SRC": str(ROOT / "src"),
        "DAEMON_CORE_TEMPORAL_CURATOR": "on",
        "DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS": USER,
        "DAEMON_CORE_RUNTIME_REVISION": source_revision(ROOT)["commit"],
        "LLM_PROVIDER": "codex_cli", "CODEX_CLI_BIN": str(CODEX_BINARY),
        "CODEX_CLI_MODEL": MODEL, "ROUTING_CODEX_MODEL": MODEL,
        "CONVERSATION_CODEX_MODEL": MODEL, "MEMORY_CODEX_MODEL": MODEL,
        "ROUTING_LLM_PROVIDER": "codex_cli", "CONVERSATION_LLM_PROVIDER": "codex_cli",
        "MEMORY_LLM_PROVIDER": "codex_cli", "CODEX_CLI_TIMEOUT_SECONDS": "180",
        "ROUTING_REASONING_EFFORT": "medium", "CONVERSATION_REASONING_EFFORT": "medium",
        "MEMORY_REASONING_EFFORT": "high",
    })
    codex_home = runtime / "codex-home"
    prepare_codex_cli_home(codex_home)
    values["CODEX_HOME"] = str(codex_home)
    shared._prepare_profile(source_home=shared.DEFAULT_HERMES_HOME, profile=profile,
                            arm=arm, model=MODEL, provider="openai-codex", daemon_values=values)
    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["agent"] = {"reasoning_effort": "medium", "max_turns": 20}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(runtime / "home"), "LANG": "en_US.UTF-8", "TZ": "UTC",
           "HERMES_HOME": str(profile), "HERMES_CONFIG": str(config_path),
           "HERMES_ENV": str(profile / ".env"), "TMPDIR": str(scratch), "TMP": str(scratch),
           "TEMP": str(scratch), "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
           "PRODUCTIVITY_BENCH_TRACE_FILE": str(trace),
           "PRODUCTIVITY_BENCH_WAIT_NATIVE_BACKGROUND": "1",
           "PRODUCTIVITY_BENCH_NATIVE_BACKGROUND_TIMEOUT_SECONDS": str(MAINTENANCE_TIMEOUT),
           "HERMES_WRITE_SAFE_ROOT": os.pathsep.join((str(workspace), str(scratch))),
           **values}
    sandbox = runtime / "sandbox.sb"
    host_root = shared.DEFAULT_HERMES_BINARY.parents[2]
    write_sandbox(sandbox, runtime, campaign, host_root)
    attestation = attest_sandbox(sandbox, runtime, campaign)
    worker = None
    log = None
    turns = []
    completed_count = 0
    started_episode = time.perf_counter()
    try:
        if arm == "C":
            log = (runtime / "worker.log").open("w")
            worker = subprocess.Popen(command(sandbox, [sys.executable, "-m", "daemon_core.after_turn_daemon"]),
                                      env=env, cwd=workspace, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            wait_for_after_turn_service(process=worker, spool_root=runtime / "after-turn-spool",
                                        log_path=runtime / "worker.log")
        previous_session = None
        for index, prompt in enumerate((*case.history, case.prompt)):
            args = shared._quiet_chat_command(binary=shared.DEFAULT_HERMES_BINARY, prompt=prompt,
                                               model=MODEL, provider="openai-codex",
                                               resume=previous_session if index == 1 else None)
            epoch = time.time()
            began = time.perf_counter()
            code, out, err, timeout = process_run(command(sandbox, args), env=env, cwd=workspace,
                                                 timeout=VOICE_TIMEOUT + MAINTENANCE_TIMEOUT)
            process_ms = round((time.perf_counter() - began) * 1000)
            (runtime / f"turn-{index}.stderr").write_text(err)
            if code == 0:
                completed_count += 1
                previous_session = shared._session_id_from_stderr(err)
                response, diagnostic = visible_response(profile, previous_session, out)
            else:
                response, diagnostic = out.strip(), ""
            lifecycle = {"complete": True, "not_applicable": arm == "A"}
            maintenance_began = time.perf_counter()
            if arm == "C" and completed_count:
                lifecycle = wait_for_durable_after_turn(
                    process=worker, spool_root=runtime / "after-turn-spool",
                    receipt_path=runtime / "continuity-receipts/continuity_receipts.jsonl",
                    telemetry_path=telemetry, host="hermes", user_id=USER,
                    expected_turns=completed_count, timeout_seconds=MAINTENANCE_TIMEOUT)
            events = [e for e in read_jsonl(trace) if e.get("epoch", 0) >= epoch]
            native_wait = sum(e.get("payload", {}).get("wait_ms", 0) for e in events
                              if e.get("event") == "native_background_barrier")
            turns.append({"index": index, "phase": "scored" if index == 2 else "history",
                          "prompt": prompt, "response": response, "cli_diagnostic": diagnostic, "returncode": code,
                          "timed_out": timeout, "process_ms": process_ms,
                          "response_ms": max(0, process_ms - native_wait),
                          "first_native_text_ms": shared._trace_timing(events, turn_started_epoch=epoch)["first_visible_ms"],
                          "native_background_wait_ms": native_wait,
                          "daemon_background_wait_ms": round((time.perf_counter() - maintenance_began) * 1000),
                          "lifecycle": lifecycle})
            write_json(runtime / "turns.json", turns)
            print(json.dumps({"run": run_id, "turn": index + 1, "returncode": code,
                              "maintenance_complete": lifecycle.get("complete"), "response_ms": turns[-1]["response_ms"]}), flush=True)
            if code != 0:
                break
        events = read_jsonl(trace)
        internal = read_jsonl(telemetry)
        visible, background, native = shared._host_token_scopes(events, expected_barriers=len(turns))
        critical, deferred = internal_scope(internal, arm)
        calls = [e for e in events if e.get("event") == "api_request_start"]
        model_seen = sorted({e.get("payload", {}).get("model", "") for e in calls})
        result = {"version": VERSION, "run_id": run_id, "case_id": case.id, "arm": arm,
                  "dry_run": dry, "family": case.family, "control": case.control,
                  "turns": turns, "episode_ms": round((time.perf_counter() - started_episode) * 1000),
                  "tokens": {"host.visible": visible, "host.background": background,
                             "daemon.critical": critical, "daemon.background": deferred},
                  "native_background": native, "tools": tool_events(events),
                  "models_seen": model_seen, "attestation": {**attestation, **shared._host_attestation(events)},
                  "daemon_compose_ms": shared._daemon_compose_ms(read_jsonl(latency)) if arm == "C" else None,
                  "canonical_voice_observed": arm == "A" or any(
                      e.get("event") == "voice_start" and e.get("delegated") is True and
                      e.get("degraded") is not True for e in read_jsonl(latency)),
                  "api_errors": sum(e.get("event") == "api_request_error" for e in events),
                  "status": "completed" if len(turns) == 3 and all(t["returncode"] == 0 for t in turns) else "failed"}
        write_json(campaign / "episodes" / f"{run_id}.json", result)
        append_jsonl(campaign / "runs.jsonl", result)
        for filename in ("host-trace.jsonl", "daemon-llm.jsonl", "daemon-latency.jsonl"):
            p = runtime / filename
            if p.exists():
                write_json(campaign / "evidence" / run_id / f"{filename}.json", read_jsonl(p))
        return result
    finally:
        if worker is not None and worker.poll() is None:
            os.killpg(worker.pid, signal.SIGTERM)
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=5)
        if log is not None:
            log.close()
        for p in (profile / "auth.json", profile / ".env", codex_home / "auth.json"):
            p.unlink(missing_ok=True)


def engineering_ok(results: list[dict]) -> bool:
    return len(results) == 2 and all(
        r["status"] == "completed" and r["canonical_voice_observed"]
        and r["native_background"]["complete"]
        and all(t["lifecycle"].get("complete") for t in r["turns"])
        and MODEL in r["models_seen"]
        and r["tokens"]["host.visible"]["total"]["value"] is not None
        and (r["arm"] == "A" or (r["tokens"]["daemon.background"]["call_count"] > 0
                                  and r["tokens"]["daemon.background"]["total"]["value"] is not None))
        for r in results) and (
            results[0]["attestation"]["external_tool_catalog_sha256"]
            == results[1]["attestation"]["external_tool_catalog_sha256"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--engineering-attempt", type=int, default=1)
    args = parser.parse_args()
    campaign = args.output.resolve()
    revision = source_revision(ROOT)
    if revision["dirty"]:
        raise SystemExit("Commit benchmark implementation before generation")
    plan_path = campaign / "plan.json"
    if plan_path.exists() and not args.resume:
        raise SystemExit("Existing campaign: use --resume; no episode is overwritten")
    if not plan_path.exists():
        campaign.mkdir(parents=True)
        campaign.chmod(0o700)
        write_json(plan_path, {"version": VERSION, "created": datetime.now(timezone.utc).isoformat(),
                              "revision": revision, "model": MODEL, "voice_effort": "medium",
                              "token_ceiling": None, "language": "en", "cases": [asdict(c) for c in CASES],
                              "codex_binary_sha256": file_sha256(CODEX_BINARY),
                              "codex_version": subprocess.check_output([str(CODEX_BINARY), "--version"], text=True).strip(),
                              "corpus_sha256": file_sha256(Path(__file__).with_name("corpus.py")),
                              "host_revision": subprocess.check_output(
                                  ["git", "-C", str(shared.DEFAULT_HERMES_BINARY.parents[2]), "rev-parse", "HEAD"], text=True).strip(),
                              "order": [{"case": c.id, "arms": ["A", "C"] if i % 2 == 0 else ["C", "A"]}
                                        for i, c in enumerate(CASES)]})
    else:
        plan = json.loads(plan_path.read_text())
        if plan["corpus_sha256"] != file_sha256(Path(__file__).with_name("corpus.py")):
            raise SystemExit("Corpus changed after preregistration")
        write_json(campaign / "runner-revision.json", revision)
    recorded = {r["run_id"]: r for r in read_jsonl(campaign / "runs.jsonl")}
    engineering = []
    engineering_case = ENGINEERING if args.engineering_attempt == 1 else Case(
        f"engineering-{args.engineering_attempt}", ENGINEERING.family, False,
        ENGINEERING.history, ENGINEERING.prompt, ENGINEERING.criteria)
    for arm in ("A", "C"):
        run_id = f"{engineering_case.id}-{arm}"
        engineering.append(recorded.get(run_id) or run_episode(engineering_case, arm, campaign, dry=True))
    if not engineering_ok(engineering):
        raise SystemExit("Engineering gates failed; inspect evidence before scored generation")
    if args.dry_run_only:
        return 0
    for index, case in enumerate(CASES):
        for arm in (("A", "C") if index % 2 == 0 else ("C", "A")):
            if f"{case.id}-{arm}" not in recorded:
                run_episode(case, arm, campaign, dry=False)
    print(json.dumps({"campaign": "complete", "scored_episodes": 24}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
