#!/usr/bin/env python3
"""Bounded private PARGA retrieval ablation; never a production launcher.

Input notes, questions, answers and captures are PRIVATE and are not fixtures to
commit. The protocol is docs/evaluations/2026-10-17_parga_embeddings_measurement.md.
Only ``index`` and ``preflight`` may run before a bounded private-transfer approval.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
USER = "private-parga-measurement"
FOLDERS = frozenset(("Projects", "Areas", "Resources", "Garden", "Archives"))
MODEL = "gpt-6-astra"
MAX_RESPONSES = 24
sys.path[:0] = [str(REPO), str(REPO / "src")]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contained(path, root):
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Input/output escaped its declared private root")
    return path


def inputs(args):
    manifest = json.loads((args.inputs / "snapshot-manifest.json").read_text())
    cases = json.loads((args.inputs / "cases.json").read_text())
    if cases["language"] != "fr" or len(cases["cases"]) != 6 or cases["repetitions"] != 2:
        raise ValueError("Expected preregistered six-case French protocol")
    if len({c["id"] for c in cases["cases"]}) != 6:
        raise ValueError("Duplicate case ID")
    for relative, digest in manifest["notes"].items():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] not in FOLDERS or rel.suffix != ".md":
            raise ValueError("Only exact notes in the five root folders are accepted")
        source = contained(args.inputs / "snapshot" / rel, args.inputs / "snapshot")
        if sha(source) != digest:
            raise ValueError("Snapshot no longer matches fixed manifest")
    for case in cases["cases"]:
        if not case["id"].isalnum():
            raise ValueError("Case ID is not a safe opaque identifier")
        for evidence in case["evidence"]:
            if manifest["notes"].get(evidence["path"]) != evidence["sha256"]:
                raise ValueError("Expected evidence does not match fixed corpus")
    return manifest, cases


def schedule(cases):
    result = []
    for repeat in (1, 2):
        for index, case in enumerate(cases):
            arms = ("top12", "full") if (index + repeat - 1) % 2 == 0 else ("full", "top12")
            result.extend((case["id"], repeat, arm) for arm in arms)
    assert len(result) == MAX_RESPONSES
    return result


def run_path(args):
    if args.phase == "index":
        return args.output / "index"
    prefix = "preflight" if args.phase == "preflight" else "runs"
    return args.output / prefix / f"{args.case}-r{args.repeat}-{args.arm}"


def environment(args):
    run = run_path(args)
    from benchmarks.productivity_v1.runner.common import isolated_daemon_env, durable_after_turn_env
    values = durable_after_turn_env(isolated_daemon_env(
        {}, run_root=run, memory_root=run / "memory/.daemon", user_id=USER,
        platform="telegram", telemetry_path=run / "llm.jsonl", latency_path=run / "latency.jsonl"), user_id=USER)
    values.update({
        "DAEMON_CORE_SRC": str(REPO / "src"),
        "DAEMON_CORE_VOICE_CAPTURE_FILE": str(REPO / "exports/private" / args.output.name / f"{run.name}.json"),
        "DAEMON_CORE_SHARED_WORLD_STATE": "on",
        "DAEMON_CORE_SCENE_MODEL_SHADOW_PATH": str(run / "scene-shadow.jsonl"),
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY": "on",
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS": USER,
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS": "telegram,openclaw",
        "DAEMON_CORE_CURRENT_TOPICS": "canary",
        "DAEMON_CORE_CURRENT_TOPICS_USER_IDS": USER,
        "DAEMON_CORE_TEMPORAL_CURATOR": "on",
        "DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS": USER,
        "DAEMON_CORE_MEMORY_SITUATIONS": "canary",
        "DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS": USER,
        "DAEMON_CORE_MEMORY_STRUCTURE": "v1",
        "DAEMON_CORE_OBSIDIAN_FORMAT": "1",
        "DAEMON_CORE_PERSONAL_CONTEXT": "canary",
        "DAEMON_CORE_PERSONAL_CONTEXT_USER_IDS": USER,
        "DAEMON_CORE_PERSONAL_CONTEXT_PLATFORMS": "telegram,openclaw",
        "DAEMON_CORE_REM_EPISODIC_SCENES": "canary",
        "DAEMON_CORE_REM_EPISODIC_SCENE_USER_IDS": USER,
        "DAEMON_CORE_REM_EPISODIC_SCENE_PLATFORMS": "telegram,openclaw",
        "DAEMON_CORE_ATLAS_EMBEDDING_TOP12": "canary" if args.arm == "top12" else "off",
        "DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS": USER,
        "DAEMON_CORE_ATLAS_EMBEDDING_MODEL": "qwen3-embedding:0.6b",
        "DAEMON_CORE_ATLAS_EMBEDDING_URL": args.embedding_url,
        "CODEX_HOME": str(run / "codex-home"),
        "CODEX_CLI_BIN": str(args.codex_bin),
        "CODEX_CLI_MODEL": MODEL,
        "CODEX_CLI_TIMEOUT_SECONDS": "180",
        "LLM_PROVIDER": "codex_cli",
        "CONVERSATION_REASONING_EFFORT": "medium",
        "ROUTING_REASONING_EFFORT": "medium",
        "MEMORY_REASONING_EFFORT": "high",
    })
    essentials = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE") if k in os.environ}
    os.environ.clear()
    os.environ.update(essentials)
    os.environ.update(values)
    profile = run / "hermes-home"
    os.environ.update({"HERMES_HOME": str(profile), "HERMES_CONFIG": str(profile / "config.yaml"),
        "HERMES_ENV": str(profile / ".env"), "PRODUCTIVITY_BENCH_TRACE_FILE": str(run / "host-trace.jsonl"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(args.hermes_source)})
    from daemon_core.paths import MEMORY_ROOT
    assert MEMORY_ROOT == run / "memory/.daemon"
    return run, values


def seed(run, args, manifest):
    vault, memory = run / "memory", run / "memory/.daemon"
    memory.mkdir(parents=True)
    for folder in FOLDERS:
        (vault / folder).mkdir()
    for relative in manifest["notes"]:
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.inputs / "snapshot" / relative, target)
    index = contained(args.inputs / "snapshot/.daemon/capsules/index.md", args.inputs / "snapshot")
    (memory / "capsules").mkdir()
    shutil.copy2(index, memory / "capsules/index.md")
    save(memory / "vault-layout.json", {"schema": 1, "layout": "root-folders-v1",
        "vault_root": str(vault), "runtime_root": str(memory)})
    save(memory / ".memory-structure-prototype.json", {"schema": 1, "memory_root": str(memory)})
    (memory / "users" / USER).mkdir(parents=True)
    shutil.copy2(REPO / "memory/templates/users/conversation.md", memory / "users" / USER / "conversation.md")
    # No personal persona, state, dialogue, agenda or synthetic fact is injected.
    for relative, digest in manifest["notes"].items():
        assert sha(vault / relative) == digest
    (run / "empty.env").touch()


def copy_cache(args, target):
    source = args.output / "index/atlas-embeddings.sqlite3"
    if not source.is_file():
        raise RuntimeError("Run local index phase first")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def preflight(run, case, args):
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    expected = "voice_brief_atlas_top12" if args.arm == "top12" else "voice_brief_reference"
    details = {}
    for host in ("telegram", "openclaw"):
        started = time.perf_counter()
        messages, context = compose_canonical_voice_messages(USER, case["prompt"], platform=host)
        checks = {
            "requested_arm_applied": context.prompt_variant == expected,
            "cold_dialogue": not context.prior_dialogue.strip(),
            "no_personal_active_state": not context.active_context.strip() and not context.personal_agenda.strip(),
            "no_personal_scene": context.scene_model is None and context.situation_source == "seed",
            "catalogue_nonempty": bool(context.atlas.souvenir_names),
        }
        details[host] = {"checks": checks, "seconds": time.perf_counter() - started,
            "catalog": context.atlas.souvenir_names, "catalogue_chars": context.atlas.rendered_chars,
            "direct_source_path": context.episodic_scene_path,
            "prompt_variant": context.prompt_variant}
        save(run / f"composition-{host}.json", {"messages": messages, **details[host]})
        if not all(checks.values()):
            raise RuntimeError("Preflight failed before any provider call")
    if details["telegram"]["catalog"] != details["openclaw"]["catalog"]:
        raise RuntimeError("Common retrieval differs across host preflights")
    return details


def index(args):
    run, _ = environment(args)
    run.mkdir(parents=True, exist_ok=False)
    manifest, _ = inputs(args)
    seed(run, args, manifest)
    from daemon_core.semantic_atlas import prewarm_semantic_atlas_cache
    result = prewarm_semantic_atlas_cache(run / "memory/.daemon")
    save(run / "result.json", result)
    print(json.dumps({k: result[k] for k in ("capsules", "embedded", "duration_s", "model_warmed")}), flush=True)


def cleanup_auth(run):
    for home in ("hermes-home", "codex-home"):
        for path in (run / home).rglob("*auth*.json"):
            path.unlink(missing_ok=True)


def child(args):
    run, values = environment(args)
    from benchmarks.productivity_v1.runner.common import source_revision, wait_for_after_turn_service, wait_for_durable_after_turn
    from benchmarks.productivity_v1.runner import hermes
    from scripts import qualify_memory_retrieval_crowding as lifecycle
    revision = source_revision(REPO)
    if revision["dirty"] and args.phase == "run":
        raise RuntimeError("Commit protocol and runner before model calls")
    manifest, cases = inputs(args)
    case = next(c for c in cases["cases"] if c["id"] == args.case)
    run.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(filename=run / "runtime.log", level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s", force=True)
    seed(run, args, manifest)
    if args.arm == "top12":
        copy_cache(args, run / "atlas-embeddings.sqlite3")
    before = lifecycle.snapshot(run, "before")
    outcome = {"case": args.case, "arm": args.arm, "repeat": args.repeat, "source": revision,
        "cases_sha256": sha(args.inputs / "cases.json"), "manifest_sha256": sha(args.inputs / "snapshot-manifest.json")}
    # Observe actual selector results without changing arguments, output or ranking.
    import daemon_core.semantic_atlas as semantic
    original = semantic.select_semantic_atlas_paths
    selections = []
    stage = ["preflight"]
    def observe(*a, **kw):
        result = original(*a, **kw)
        selections.append({"stage": stage[0], **asdict(result)})
        return result
    semantic.select_semantic_atlas_paths = observe
    process, log = None, None
    try:
        outcome["preflight"] = preflight(run, case, args)
        if args.phase == "preflight":
            outcome["preflight_complete"] = True
            return
        if not args.approval or not args.approval.is_file():
            raise ValueError("Explicit bounded private-transfer approval required")
        approval = json.loads(args.approval.read_text())
        if approval.get("approved") is not True or approval.get("cases_sha256") != outcome["cases_sha256"] or approval.get("manifest_sha256") != outcome["manifest_sha256"] or approval.get("max_responses") != MAX_RESPONSES:
            raise ValueError("Approval does not match this fixed campaign")
        from scripts.qualify_maintenance_recovery import disposable_access
        disposable_access(args.auth_home, run / "codex-home")
        hermes._prepare_profile(source_home=run / "no-auth-source", profile=run / "hermes-home",
            arm="C", model=MODEL, provider="openai-codex", daemon_values=values)
        from hermes_cli.auth_codex import _save_codex_tokens
        auth = json.loads((run / "codex-home/auth.json").read_text())
        _save_codex_tokens(auth["tokens"], last_refresh=auth.get("last_refresh"))
        del auth
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent
        stage[0] = "native_voice"
        runtime = resolve_runtime_provider(requested="openai-codex")
        deltas = []
        started = time.perf_counter()
        agent = AIAgent(model=MODEL, provider=runtime["provider"], api_mode=runtime["api_mode"],
            base_url=runtime.get("base_url"), api_key=runtime.get("api_key"), user_id=USER,
            platform="telegram", session_id=f"isolated-parga-{run.name}",
            reasoning_config={"enabled": True, "effort": "high"}, enabled_toolsets=["memory"],
            max_iterations=5, run_budget_seconds=180, quiet_mode=True,
            skip_background_review=True, skip_context_files=True,
            stream_delta_callback=lambda text: deltas.append((time.perf_counter() - started, text)) if text else None)
        tools = sorted(agent.valid_tool_names)
        if not {"daemon_read_souvenirs", "daemon_recall_l0"} <= set(tools):
            raise RuntimeError("Native memory tools unavailable")
        if not set(tools) <= {"memory", "daemon_read_souvenirs", "daemon_recall_l0"}:
            raise RuntimeError("Unexpected external tool exposed by isolated host")
        outcome["native_tool_names"] = tools
        initialized = time.perf_counter() - started
        result = agent.run_conversation(user_message=case["prompt"], conversation_history=[])
        elapsed = time.perf_counter() - started
        agent.shutdown_memory_provider(result.get("messages", []))
        save(run / "voice.json", {"result": result, "response_seconds": elapsed,
            "initialization_seconds": initialized, "first_delta_seconds": deltas[0][0] if deltas else None,
            "prompt": case["prompt"], "stream_deltas": deltas})
        print(json.dumps({"observation": run.name, "voice_seconds": round(elapsed, 3)}), flush=True)
        captured = json.loads(Path(values["DAEMON_CORE_VOICE_CAPTURE_FILE"]).read_text())
        outcome["capture_metadata"] = captured["metadata"]
        save(run / "first-request.json", captured)
        log = (run / "worker.log").open("w")
        process = subprocess.Popen(child_command(args) + ["--worker"], cwd=run,
            env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
        started = time.perf_counter()
        wait_for_after_turn_service(process=process, spool_root=run / "after-turn-spool", log_path=run / "worker.log")
        tail = wait_for_durable_after_turn(process=process, spool_root=run / "after-turn-spool",
            receipt_path=run / "continuity-receipts/continuity_receipts.jsonl", telemetry_path=run / "llm.jsonl",
            host="hermes", user_id=USER, expected_turns=1, timeout_seconds=360)
        outcome.update(lifecycle=tail, after_turn_seconds=time.perf_counter() - started)
        outcome["complete"] = bool(result.get("completed") and not result.get("failed")
            and tail["complete"] and not any(tail["spool_counts"].values())
            and not tail["cycle_status_counts"].get("failed", 0))
        if not outcome["complete"]:
            raise RuntimeError("Incomplete native observation; campaign must stop")
    except Exception as exc:
        outcome.update(complete=False, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        lifecycle.stop_process(process)
        if log:
            log.close()
        cleanup_auth(run)
        semantic.select_semantic_atlas_paths = original
        save(run / "selections.json", selections)
        after = lifecycle.snapshot(run, "after")
        outcome["changed_paths"] = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        save(run / "result.json", outcome)
        print(json.dumps({"observation": run.name, "complete": outcome.get("complete"),
            "preflight_complete": outcome.get("preflight_complete"), "error_type": outcome.get("error_type")}), flush=True)


def child_command(args):
    result = [str(args.hermes_source / ".venv/bin/python"), str(Path(__file__).resolve()),
        "--inputs", str(args.inputs), "--output", str(args.output), "--phase", args.phase,
        "--hermes-source", str(args.hermes_source), "--auth-home", str(args.auth_home),
        "--codex-bin", str(args.codex_bin), "--embedding-url", args.embedding_url,
        "--case", args.case, "--arm", args.arm, "--repeat", str(args.repeat)]
    if args.approval:
        result += ["--approval", str(args.approval)]
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "output", "hermes-source", "auth-home", "codex-bin"):
        p.add_argument("--" + name, type=lambda x: Path(x).expanduser().resolve(), required=True)
    p.add_argument("--phase", choices=("index", "preflight", "run"), required=True)
    p.add_argument("--embedding-url", default="http://127.0.0.1:11435")
    p.add_argument("--approval", type=Path)
    p.add_argument("--case")
    p.add_argument("--arm", choices=("top12", "full"))
    p.add_argument("--repeat", type=int, choices=(1, 2), default=1)
    p.add_argument("--child", action="store_true")
    p.add_argument("--worker", action="store_true")
    args = p.parse_args()
    if not args.output.is_relative_to(Path("/private/tmp")) or args.output.is_relative_to(args.inputs) or args.inputs.is_relative_to(args.output):
        raise ValueError("Fresh output under /private/tmp must be separate from immutable inputs")
    if args.embedding_url not in ("http://127.0.0.1:11434", "http://127.0.0.1:11435"):
        raise ValueError("Embeddings must remain on a local service")
    os.umask(0o077)
    if args.worker:
        from scripts import qualify_memory_retrieval_crowding as lifecycle
        lifecycle.environment = environment
        lifecycle.USER = USER
        lifecycle.worker(args)
        return
    if args.phase == "index":
        index(args)
        return
    if args.child:
        child(args)
        return
    _, cases = inputs(args)
    planned = schedule(cases["cases"])
    if args.phase == "preflight":
        planned = [entry for entry in planned if entry[1] == 1]
    if args.case or args.arm:
        raise ValueError("Single observations are reserved for the bounded parent schedule")
    args.output.mkdir(parents=True, exist_ok=True)
    fixed = {"schedule": planned, "cases_sha256": sha(args.inputs / "cases.json"),
        "manifest_sha256": sha(args.inputs / "snapshot-manifest.json")}
    plan_path = args.output / f"{args.phase}-plan.json"
    if plan_path.exists():
        raise FileExistsError("Campaign already started; preserve all attempts and do not reroll")
    save(plan_path, fixed)
    from scripts.qualify_memory_retrieval_crowding import stop_process
    for case, repeat, arm in planned:
        args.case, args.repeat, args.arm = case, repeat, arm
        run = run_path(args)
        if run.exists():
            raise FileExistsError("Refusing to overwrite an observation")
        run.parent.mkdir(parents=True, exist_ok=True)
        command = child_command(args) + ["--child"]
        essentials = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE") if k in os.environ}
        essentials.update(PYTHONPATH=str(args.hermes_source), PYTHONDONTWRITEBYTECODE="1")
        process = None
        try:
            with (run.parent / f"{run.name}.log").open("w") as log:
                process = subprocess.Popen(command, cwd=args.output, env=essentials,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                code = process.wait(timeout=650)
            print(json.dumps({"observation": run.name, "exit_code": code}), flush=True)
            if code:
                raise RuntimeError("Observation failed; preserve artifacts, no automatic reroll")
        finally:
            stop_process(process, group=True)
            cleanup_auth(run)


if __name__ == "__main__":
    main()
