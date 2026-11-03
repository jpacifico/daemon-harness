"""Run the Continuity Benchmark through the benchmark's own Hermes and, in arm C, the daemon as Companion installs it.

Reuses UCB's sandboxed turn loop and the Productivity V1 observer, accounting
and native fixtures. No memory is seeded beyond Companion's fixed voice
templates and identity, and no host source is patched.
Protocol: docs/50_continuity_benchmark.md.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time

import yaml

from benchmarks.continuity import VERSION
from benchmarks.continuity.corpus import ELSEWHERE, ENGINEERING, LIVES, Life
from benchmarks.productivity_v1.runner import hermes as shared
from benchmarks.productivity_v1.runner.common import (
    append_jsonl, file_sha256, prepare_codex_cli_home, read_jsonl, source_revision,
    tool_events, wait_for_after_turn_service, wait_for_durable_after_turn, write_json,
)
from benchmarks.useful_continuity.run import internal_scope, process_run, visible_response

ROOT = Path(__file__).resolve().parents[2]
REAL_HOME = Path.home()
BENCH_HOME = Path(os.environ.get("CONTINUUM_HOME", REAL_HOME / ".codex/worktrees/continuum-host")).absolute()
STORE = BENCH_HOME / "credentials"
CODEX_BINARY = BENCH_HOME / "codex/0.159.2/bin/codex"  # the official package Companion pins (setup_host.py)
MODEL = "gpt-6-astra"
USER = "100000001"  # Companion requires one numeric owner
DAEMON_NAME, DAEMON_GENDER = "Aster", "feminine"
SEED = 20261031
VOICE_TIMEOUT = 300
MAINTENANCE_TIMEOUT = 240
NATIVE = (("native.files.aggregate", (1101, 1102, 1103, 1104, 1105, 1106)),
          ("native.tools.extract", (1201, 1202, 1203, 1204, 1205, 1206)))
ENGINEERING_NATIVE = ("native.files.aggregate", 1101)


def host_install() -> Path:
    """The installation Companion's installer published under the benchmark home."""
    root = BENCH_HOME / "companion/hosts/hermes"
    pointer = json.loads((root / "current.json").read_text())
    return root / "installations" / pointer["installation_id"]


def hermes_binary() -> Path:
    found = sorted((host_install() / "source").glob("hermes-agent-*/.venv/bin/hermes"))
    if len(found) != 1:
        raise RuntimeError("The benchmark Hermes installation is not exactly one host")
    return found[0]


def sessions(life: Life) -> list[tuple[str, ...]]:
    """Telling, elsewhere, change (when any), asking: each one a new Hermes session."""
    parts = [life.telling, ELSEWHERE[life.elsewhere]]
    if life.change:
        parts.append(life.change)
    return [*parts, (life.asking,)]


def daemon_values(runtime: Path, memory_root: Path, telemetry: Path, latency: Path) -> dict[str, str]:
    """The switches a fresh Companion daemon receives, with its paths moved into this run."""
    from daemon_core.installation_profile import (
        CONTINUITY_RECEIPTS, DURABLE_AFTER_TURN, HOST_VOICE, SCENE_CONTINUITY, VAULT_FORMAT,
        profile_environment,
    )
    values = {**HOST_VOICE, **DURABLE_AFTER_TURN, **SCENE_CONTINUITY, **CONTINUITY_RECEIPTS,
              **VAULT_FORMAT, "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS": USER,
              **profile_environment(USER, memory_root)}
    # Companion's daemons talk through Telegram; this campaign talks through the Hermes CLI.
    # The canonical voice is scoped by an implicit "telegram" default, so it is named here.
    values["DAEMON_CORE_SOVEREIGN_PLATFORMS"] = "telegram"
    for key, value in list(values.items()):
        if key.endswith("_PLATFORMS"):
            values[key] = ",".join(sorted({*filter(None, value.split(",")), "cli"}))
    values.update({
        "DAEMON_CORE_SRC": str(ROOT / "src"), "DAEMON_CORE_USER_ID": USER,
        "DAEMON_CORE_FOLLOWUPS": "off",  # no channel exists to deliver a follow-up
        "DAEMON_CORE_MEMORY_ROOT": str(memory_root), "DAEMON_CORE_STATE_ROOT": str(runtime / "state"),
        "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(runtime / "after-turn-spool"),
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(runtime / "continuity-receipts"),
        "DAEMON_CORE_LATENCY_LOG": str(latency), "DAEMON_CORE_LLM_TELEMETRY_FILE": str(telemetry),
        "DAEMON_CORE_ENV_FILE": str(runtime / "empty.env"), "DAEMON_CORE_LOG_DIR": str(runtime / "logs"),
        "DAEMON_CORE_ATLAS_EMBEDDING_CACHE": str(runtime / "atlas.sqlite3"),
        "DAEMON_CORE_PYTHON": sys.executable, "DAEMON_CORE_RUNTIME_REVISION": source_revision(ROOT)["commit"],
        # Companion's ChatGPT defaults for the daemon's own calls.
        "LLM_PROVIDER": "codex_cli", "CODEX_CLI_BIN": str(CODEX_BINARY), "CODEX_CLI_MODEL": MODEL,
        "ROUTING_LLM_PROVIDER": "codex_cli", "CONVERSATION_LLM_PROVIDER": "codex_cli",
        "MEMORY_LLM_PROVIDER": "codex_cli", "ROUTING_CODEX_MODEL": MODEL,
        "CONVERSATION_CODEX_MODEL": MODEL, "MEMORY_CODEX_MODEL": MODEL,
        "ROUTING_REASONING_EFFORT": "medium", "CONVERSATION_REASONING_EFFORT": "medium",
        "MEMORY_REASONING_EFFORT": "high", "CODEX_CLI_TIMEOUT_SECONDS": "180",
    })
    return values


def initialize_daemon_memory(vault: Path) -> Path:
    """Companion's fresh vault: empty folders, fixed voice templates, the daemon's identity."""
    from daemons_companion.desktop.memory import initialize_empty_vault
    from daemon_core.daemon_identity import save_daemon_identity
    memory_root = initialize_empty_vault(vault)
    for name in ("persona.md", "conversation.md"):
        target = memory_root / "users" / USER / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes((ROOT / "memory/templates/users" / name).read_bytes())
    save_daemon_identity(memory_root, DAEMON_NAME, DAEMON_GENDER)
    return memory_root


def interpreter_paths() -> list[Path]:
    """Every link from this Python to its real installation, which the worker must be able to run."""
    hops, current = [], Path(sys.executable).absolute()
    while current.is_symlink() and len(hops) < 16:
        hops.append(current)
        target = current.readlink()
        current = target if target.is_absolute() else current.parent / target
    return [*hops, Path(sys.executable).absolute().parent.parent, current.resolve().parent.parent]


def write_sandbox(path: Path, runtime: Path, campaign: Path) -> None:
    readable = (
        runtime, host_install(), ROOT / "src", ROOT / "integrations", ROOT / "memory/templates",
        shared.OBSERVER, CODEX_BINARY.parent.parent, *interpreter_paths(),
    )
    traversal = set()
    for item in readable:
        traversal.update(item.parents)
    lines = ["(version 1)", "(allow default)"]
    for denied in (REAL_HOME, campaign, STORE):
        lines += [f"(deny file-read* (subpath {json.dumps(str(denied))}))",
                  f"(deny file-write* (subpath {json.dumps(str(denied))}))"]
    lines += [f"(allow file-read-metadata (literal {json.dumps(str(p))}))" for p in sorted(traversal)]
    lines += [f"(allow file-read* (subpath {json.dumps(str(p))}))" for p in readable]
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
                 STORE / "codex" / "auth.json", campaign / "plan.json",
                 ROOT / "benchmarks/continuity/corpus.py")
    for p in forbidden:
        if subprocess.run(command(sandbox, ["/usr/bin/head", "-c", "0", str(p)]), capture_output=True).returncode == 0:
            raise RuntimeError("Sandbox permits private state, credentials or criteria")
    return {"sandbox": "macos-sandbox-exec", "runtime_writable": True,
            "personal_files_store_and_criteria_blocked": True, "profile_sha256": file_sha256(sandbox)}


def return_credentials(runtime_copy: Path, store_copy: Path) -> None:
    """Sign-ins rotate their refresh token: keep the newest copy for the next episode."""
    if not runtime_copy.is_file():
        return
    data = runtime_copy.read_bytes()
    if data == store_copy.read_bytes():
        return
    json.loads(data)  # never store a truncated credential
    staged = store_copy.with_name(store_copy.name + ".next")
    staged.write_bytes(data)
    staged.chmod(0o600)
    os.replace(staged, store_copy)


def run_episode(episode: dict, arm: str, campaign: Path, *, dry: bool) -> dict:
    run_id = f"{episode['id']}-{arm}"
    runtime = campaign / "private" / run_id
    if runtime.exists():
        if any(e.get("event") == "api_request_start" for e in read_jsonl(runtime / "host-trace.jsonl")):
            raise RuntimeError(f"Refusing to overwrite an episode which reached a model: {run_id}")
        runtime.rename(runtime.with_name(f"{run_id}-preflight-{time.time_ns()}"))
    runtime.mkdir(parents=True)
    # The daemon reads its owner from the Hermes profile name, as in a Companion profile.
    profile = runtime / "hermes-root" / "profiles" / USER
    workspace, scratch = runtime / "workspace", runtime / "tmp"
    for p in (scratch, runtime / "home", runtime / "logs"):
        p.mkdir()
    (runtime / "empty.env").touch()
    trace, telemetry, latency = runtime / "host-trace.jsonl", runtime / "daemon-llm.jsonl", runtime / "daemon-latency.jsonl"
    telemetry.touch()
    prepared = None
    if episode["kind"] == "native":
        from benchmarks.productivity_v1.scenarios import prepare_scenario
        prepared = prepare_scenario(scenario_id=episode["scenario"], seed=episode["seed"],
                                    variant=shared._variant_for_arm(episode["seed"], arm),
                                    workspace=workspace, control_path=runtime / "control.json",
                                    run_root=runtime)
        prompts = [(prepared.fixture["prompt"],)]
    else:
        workspace.mkdir()
        prompts = sessions(episode["life"])
    codex_home = runtime / "codex-home"
    values: dict[str, str] = {}
    if arm == "C":
        memory_root = initialize_daemon_memory(runtime / "vault")
        values = daemon_values(runtime, memory_root, telemetry, latency)
        prepare_codex_cli_home(codex_home, source=STORE / "codex")
        values["CODEX_HOME"] = str(codex_home)
    shared._prepare_profile(source_home=STORE / "hermes", profile=profile, arm=arm, model=MODEL,
                            provider="openai-codex", daemon_values=values)
    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["agent"] = {"reasoning_effort": "medium", "max_turns": 30}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(runtime / "home"),
           "LANG": "en_US.UTF-8", "TZ": "UTC", "HERMES_HOME": str(profile),
           "HERMES_CONFIG": str(config_path), "HERMES_ENV": str(profile / ".env"),
           "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch), "NO_COLOR": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
           "PRODUCTIVITY_BENCH_TRACE_FILE": str(trace), "PRODUCTIVITY_BENCH_WAIT_NATIVE_BACKGROUND": "1",
           "PRODUCTIVITY_BENCH_NATIVE_BACKGROUND_TIMEOUT_SECONDS": str(MAINTENANCE_TIMEOUT),
           "HERMES_WRITE_SAFE_ROOT": os.pathsep.join((str(workspace), str(scratch))), **values}
    sandbox = runtime / "sandbox.sb"
    write_sandbox(sandbox, runtime, campaign)
    attestation = attest_sandbox(sandbox, runtime, campaign)
    worker = log = None
    turns: list[dict] = []
    completed = 0
    started = time.perf_counter()
    binary = hermes_binary()
    try:
        if arm == "C":
            log = (runtime / "worker.log").open("w")
            worker = subprocess.Popen(command(sandbox, [sys.executable, "-m", "daemon_core.after_turn_daemon"]),
                                      env=env, cwd=workspace, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            wait_for_after_turn_service(process=worker, spool_root=runtime / "after-turn-spool",
                                        log_path=runtime / "worker.log")
        failed = False
        for session_index, messages in enumerate(prompts):
            session = None
            for message_index, prompt in enumerate(messages):
                scored = session_index == len(prompts) - 1
                args = shared._quiet_chat_command(binary=binary, prompt=prompt, model=MODEL,
                                                   provider="openai-codex", resume=session)
                epoch, began = time.time(), time.perf_counter()
                code, out, err, timed_out = process_run(command(sandbox, args), env=env, cwd=workspace,
                                                       timeout=VOICE_TIMEOUT + MAINTENANCE_TIMEOUT)
                process_ms = round((time.perf_counter() - began) * 1000)
                (runtime / f"turn-{len(turns)}.stderr").write_text(err)
                if code == 0:
                    completed += 1
                    session = shared._session_id_from_stderr(err)
                    response, diagnostic = visible_response(profile, session, out)
                else:
                    response, diagnostic = out.strip(), ""
                lifecycle = {"complete": True, "not_applicable": arm == "A"}
                maintenance_began = time.perf_counter()
                if arm == "C" and completed:
                    lifecycle = wait_for_durable_after_turn(
                        process=worker, spool_root=runtime / "after-turn-spool",
                        receipt_path=runtime / "continuity-receipts/continuity_receipts.jsonl",
                        telemetry_path=telemetry, host="hermes", user_id=USER,
                        expected_turns=completed, timeout_seconds=MAINTENANCE_TIMEOUT)
                events = [e for e in read_jsonl(trace) if e.get("epoch", 0) >= epoch]
                native_wait = sum(e.get("payload", {}).get("wait_ms", 0) for e in events
                                  if e.get("event") == "native_background_barrier")
                turns.append({"index": len(turns), "session": session_index, "scored": scored,
                              "prompt": prompt, "response": response, "cli_diagnostic": diagnostic,
                              "returncode": code, "timed_out": timed_out, "process_ms": process_ms,
                              "response_ms": max(0, process_ms - native_wait),
                              "first_native_text_ms": shared._trace_timing(events, turn_started_epoch=epoch)["first_visible_ms"],
                              "native_background_wait_ms": native_wait,
                              "daemon_background_wait_ms": round((time.perf_counter() - maintenance_began) * 1000),
                              "lifecycle": lifecycle})
                write_json(runtime / "turns.json", turns)
                print(json.dumps({"run": run_id, "turn": len(turns), "returncode": code,
                                  "maintenance_complete": lifecycle.get("complete"),
                                  "response_ms": turns[-1]["response_ms"]}), flush=True)
                if code != 0:
                    failed = True
                    break
            if failed:
                break
        events = read_jsonl(trace)
        internal = read_jsonl(telemetry)
        visible, background, native = shared._host_token_scopes(events, expected_barriers=len(turns))
        critical, deferred = internal_scope(internal, arm)
        calls = [e for e in events if e.get("event") == "api_request_start"]
        expected_turns = sum(len(s) for s in prompts)
        outcome = prepared.assess() if prepared is not None and not failed else None
        result = {"version": VERSION, "run_id": run_id, "episode": episode["id"], "kind": episode["kind"],
                  "arm": arm, "dry_run": dry, "family": episode.get("family"), "turns": turns,
                  "episode_ms": round((time.perf_counter() - started) * 1000),
                  "tokens": {"host.visible": visible, "host.background": background,
                             "daemon.critical": critical, "daemon.background": deferred},
                  "native_background": native, "tools": tool_events(events),
                  "models_seen": sorted({e.get("payload", {}).get("model", "") for e in calls}),
                  "attestation": {**attestation, **shared._host_attestation(events)},
                  "daemon_compose_ms": shared._daemon_compose_ms(read_jsonl(latency)) if arm == "C" else None,
                  "canonical_voice_observed": arm == "A" or any(
                      e.get("event") == "voice_start" and e.get("delegated") is True and
                      e.get("degraded") is not True for e in read_jsonl(latency)),
                  "api_errors": sum(e.get("event") == "api_request_error" for e in events),
                  "native_outcome": outcome,
                  "status": "completed" if len(turns) == expected_turns and not failed else "failed"}
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
        if prepared is not None:
            prepared.close()
        return_credentials(profile / "auth.json", STORE / "hermes" / "auth.json")
        return_credentials(codex_home / "auth.json", STORE / "codex" / "auth.json")
        for p in (profile / "auth.json", profile / ".env", codex_home / "auth.json"):
            p.unlink(missing_ok=True)


def episodes() -> list[dict]:
    items = [{"id": life.id, "kind": "life", "family": life.family, "life": life} for life in LIVES]
    items += [{"id": f"{scenario}.{seed}", "kind": "native", "family": scenario,
               "scenario": scenario, "seed": seed} for scenario, seeds in NATIVE for seed in seeds]
    return items


def order(items: list[dict]) -> list[dict]:
    """Interleaved from the public seed; each item's arm order is drawn too."""
    rng = random.Random(SEED)
    shuffled = list(items)
    rng.shuffle(shuffled)
    return [{"id": item["id"], "arms": ["A", "C"] if rng.random() < 0.5 else ["C", "A"]} for item in shuffled]


def engineering_ok(results: list[dict]) -> bool:
    return len(results) == 4 and all(
        r["status"] == "completed" and r["canonical_voice_observed"]
        and r["native_background"]["complete"]
        and all(t["lifecycle"].get("complete") for t in r["turns"])
        and MODEL in r["models_seen"]
        and r["tokens"]["host.visible"]["total"]["value"] is not None
        and (r["arm"] == "A" or (r["tokens"]["daemon.background"]["call_count"] > 0
                                  and r["tokens"]["daemon.background"]["total"]["value"] is not None
                                  and r["tokens"]["daemon.critical"]["total"]["value"] == 0))
        and (r["kind"] != "native" or r["native_outcome"] is not None)
        for r in results) and len({r["attestation"]["external_tool_catalog_sha256"] for r in results}) == 1


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
        raise SystemExit("Commit the benchmark before generation")
    corpus = Path(__file__).with_name("corpus.py")
    plan_path = campaign / "plan.json"
    items = {item["id"]: item for item in episodes()}
    if plan_path.exists() and not args.resume:
        raise SystemExit("Existing campaign: use --resume; no episode is overwritten")
    if not plan_path.exists():
        campaign.mkdir(parents=True)
        campaign.chmod(0o700)
        binary = hermes_binary()
        write_json(plan_path, {
            "version": VERSION, "created": datetime.now(timezone.utc).isoformat(), "revision": revision,
            "model": MODEL, "voice_effort": "medium", "seed": SEED, "language": "en",
            "daemon_identity": {"name": DAEMON_NAME, "gender": DAEMON_GENDER},
            "corpus_sha256": file_sha256(corpus),
            "lives": [asdict(life) for life in LIVES], "native": [list(n) for n in NATIVE],
            "codex_binary_sha256": file_sha256(CODEX_BINARY),
            "codex_version": subprocess.check_output([str(CODEX_BINARY), "--version"], text=True).strip(),
            "hermes_binary_sha256": file_sha256(binary),
            "hermes_version": subprocess.check_output([str(binary), "--version"], text=True,
                                                      env={"PATH": "/usr/bin:/bin", "HOME": str(campaign)}).splitlines()[0],
            "hermes_source": json.loads((host_install().parent.parent / "current.json").read_text()),
            "order": order(list(items.values())),
        })
    plan = json.loads(plan_path.read_text())
    if plan["corpus_sha256"] != file_sha256(corpus):
        raise SystemExit("Corpus changed after preregistration")
    write_json(campaign / f"runner-revision-{time.time_ns()}.json", revision)
    recorded = {r["run_id"]: r for r in read_jsonl(campaign / "runs.jsonl")}
    suffix = "" if args.engineering_attempt == 1 else f"-{args.engineering_attempt}"
    scenario, seed = ENGINEERING_NATIVE
    engineering_items = (
        {"id": f"engineering{suffix}", "kind": "life", "family": "engineering", "life": ENGINEERING},
        {"id": f"engineering-native{suffix}", "kind": "native", "family": scenario, "scenario": scenario, "seed": seed},
    )
    engineering = []
    for item in engineering_items:
        for arm in ("A", "C"):
            engineering.append(recorded.get(f"{item['id']}-{arm}") or run_episode(item, arm, campaign, dry=True))
    if not engineering_ok(engineering):
        raise SystemExit("Engineering gates failed; inspect evidence before scored generation")
    if args.dry_run_only:
        return 0
    for entry in plan["order"]:
        for arm in entry["arms"]:
            if f"{entry['id']}-{arm}" not in recorded:
                run_episode(items[entry["id"]], arm, campaign, dry=False)
    print(json.dumps({"campaign": "complete", "episodes": 2 * len(items)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
