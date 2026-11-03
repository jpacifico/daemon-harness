#!/usr/bin/env python3
"""Bounded native baseline for the approved French memory-organization grid."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from zoneinfo import ZoneInfo

import qualify_memory_retrieval_crowding as harness

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "docs/evaluations/fixtures/memory_organization_baseline_fr.json"
ORIGINAL_ENVIRONMENT = harness.environment


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observations(fixture):
    result = []
    for case in fixture["cases"]:
        last_source = None
        for kind, field in (("source", "sources"), ("probe", "probes")):
            for index, turn in enumerate(case[field], 1):
                identifier = f'{case["id"]}-{kind}-{index:02d}'
                result.append({"id": identifier, "case": case["id"], "kind": kind,
                               "previous": last_source, **turn})
                if kind == "source":
                    last_source = identifier
    return result


def run_path(output, observation):
    return output / (observation["id"] + "-top12")


def copy_bytes(source, target):
    if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Memory snapshots must not contain symlinks")
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def prepare_memory(args, observation, run):
    memory = run / "memory"
    previous = observation["previous"]
    copied = {}
    if previous:
        source_run = args.output / (previous + "-top12")
        if not read_json(source_run / "result.json").get("complete"):
            raise RuntimeError("Previous after-turn is incomplete; no invented replacement state")
        source = source_run / "memory"
        if observation["kind"] == "source":
            copy_bytes(source, memory)
        else:
            memory.mkdir()
            # Keep exact durable evidence. Do not carry the learning session's scene.
            paths = [Path("capsules"), Path("archive"),
                     Path("levels/L0_ground_zero"), Path("persona.md"), Path("conversation.md"),
                     Path("users") / harness.USER / "context.md",
                     Path("users") / harness.USER / "agenda.md"]
            for relative in paths:
                if (source / relative).exists():
                    copy_bytes(source / relative, memory / relative)
        for path in sorted(memory.rglob("*.md")):
            relative = path.relative_to(memory)
            if path.read_bytes() != (source / relative).read_bytes():
                raise AssertionError("Memory copy changed source bytes")
            copied[str(relative)] = digest(path)
    else:
        memory.mkdir()
    if observation["kind"] == "probe":
        append_neutral_dialogue(args)
    harness.write_json(run / "memory-input.json", {
        "previous": previous, "copied_sha256": copied,
        "neutral_turns": 32 if observation["kind"] == "probe" else 0,
        "short_state_copied": observation["kind"] == "source" and bool(previous),
        "cold_context": args.cold_context,
    })


def append_neutral_dialogue(args):
    from daemon_core.memory.writer import append_ground_zero
    now = datetime.fromisoformat(os.environ["DAEMON_CORE_EVAL_NOW"])
    for index in range(16):
        if args.cold_context.get("spread_over_days"):
            moment = now - timedelta(days=4) + timedelta(hours=6 * index)
        else:
            moment = now - timedelta(hours=8) + timedelta(minutes=15 * index)
        append_ground_zero(harness.USER, "user",
            f"J’ai dessiné {index + 1} petits carrés dans mon carnet.", occurred_at=moment.timestamp())
        append_ground_zero(harness.USER, "assistant", "Une page qui se remplit.",
                           occurred_at=moment.timestamp() + 1)


def source_exposure(value, case):
    from summarize_retrieval_crowding import strings
    text = "\n".join(strings(value))
    return [turn["date"] for turn in case["sources"]
            if turn["text"] in text or f'{turn["date"]}T10:00:00 — user' in text]


def prepare_cold_layout(args, run):
    """Unscored preflight only: exact fictitious L0, no model-created notes."""
    from daemon_core.memory.writer import append_ground_zero
    (run / "memory").mkdir()
    for turn in args.case_fixture["sources"]:
        stamp = datetime.fromisoformat(turn["date"] + "T10:00:00").replace(tzinfo=ZoneInfo("Europe/Paris"))
        append_ground_zero(harness.USER, "user", turn["text"], occurred_at=stamp.timestamp())
    append_neutral_dialogue(args)
    harness.write_json(run / "memory-input.json", {
        "layout_only": True, "copied_sha256": {}, "neutral_turns": 32,
        "short_state_copied": False, "cold_context": args.cold_context})


def configure(args, observation):
    harness.USER = "synthetic-organization-" + observation["case"]
    args.case, args.arm = observation["id"], "top12"

    def environment(child_args):
        run, values = ORIGINAL_ENVIRONMENT(child_args)
        stamp = datetime.fromisoformat(observation["date"] + "T10:00:00").replace(
            tzinfo=ZoneInfo("Europe/Paris"))
        values.update({"DAEMON_CORE_MEMORY_SITUATIONS": "canary",
                       "DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS": harness.USER,
                       "DAEMON_CORE_EVAL_NOW": stamp.isoformat(),
                       "DAEMON_CORE_ATLAS_EMBEDDING_URL": child_args.embedding_url})
        if args.structure:
            values.update({"DAEMON_CORE_MEMORY_STRUCTURE": "v1", "DAEMON_CORE_OBSIDIAN_FORMAT": "1"})
        os.environ.update(values)
        if args.cold_context.get("fixed_l0_calendar"):
            # Test-only semantic day; auth expiry, leases and monotonic timers stay real.
            from daemon_core.memory import writer
            original = writer._ground_zero_datetime
            writer._ground_zero_datetime = lambda occurred_at=0.0: (
                original(occurred_at) if occurred_at > 0 else stamp.replace(tzinfo=None))
        return run, values

    harness.environment = environment
    harness.MAX_BACKGROUND_CALLS = args.bounds["background_calls"]


def child_command(args, observation):
    command = [str(args.hermes_source / ".venv/bin/python"), str(Path(__file__).resolve()),
               "--output", str(args.output), "--hermes-source", str(args.hermes_source),
               "--hermes-home", str(args.hermes_home), "--codex-bin", str(args.codex_bin),
               "--embedding-url", args.embedding_url, "--fixture", str(args.fixture),
               "--observation", observation["id"]]
    if args.structure:
        command.append("--structure")
    return command


def patch_reception_clock(stamp):
    # Test-only clock injection. Keep timeouts, credential expiry and leases on real time.
    import daemon_core.turn_handoff as handoff
    original_handoff = handoff.write_turn_handoff
    handoff.write_turn_handoff = lambda user, message, trace, started: original_handoff(
        user, message, trace, stamp)
    import daemon_core.after_turn as after
    original_record = after.record_turn_message

    def historical_record(*args, **kwargs):
        kwargs["occurred_at"] = stamp
        return original_record(*args, **kwargs)

    after.record_turn_message = historical_record


def cleanup_credentials(run):
    (run / "hermes-home/auth.json").unlink(missing_ok=True)
    if (run / "codex-home").exists():
        shutil.rmtree(run / "codex-home")


def child(args, observation):
    run, values = harness.environment(args)
    from benchmarks.productivity_v1.runner.common import (
        source_revision, prepare_codex_cli_home, wait_for_after_turn_service,
        wait_for_durable_after_turn)
    revision = source_revision(REPO)
    if revision["dirty"] and not args.preflight_only:
        raise RuntimeError("Commit protocol, fixture and runner before provider calls")
    run.mkdir(parents=True, exist_ok=False)
    (run / "empty.env").touch()
    logging.basicConfig(filename=run / "runtime.log", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", force=True)
    if args.layout_only:
        assert args.preflight_only
        prepare_cold_layout(args, run)
    else:
        prepare_memory(args, observation, run)
    if args.structure:
        from daemon_core.memory.context_pages import MARKER
        harness.write_json(run / "memory" / MARKER,
            {"schema": 1, "memory_root": str((run / "memory").resolve())})
    before = harness.snapshot(run, "before")
    outcome = {"observation": observation, "source": revision,
               "fixture_sha256": digest(args.fixture), "complete": False,
               "cold_context": args.cold_context, "layout_only": args.layout_only}
    process, log = None, None
    try:
        from daemon_core.memory_agent import memory_situation_notes_enabled
        from daemon_core.paths import MEMORY_ROOT
        assert MEMORY_ROOT == run / "memory"
        assert memory_situation_notes_enabled(harness.USER)
        from daemon_core.semantic_atlas import prewarm_semantic_atlas_cache
        from daemon_core.single_pass_sovereign import load_searchable_memory_entries
        entries, _expired = load_searchable_memory_entries(MEMORY_ROOT)
        outcome["embedding_prewarm"] = (prewarm_semantic_atlas_cache(MEMORY_ROOT)
            if entries else {"capsules": 0, "skipped": "empty_initial_memory"})
        from daemon_core.canonical_voice import compose_canonical_voice_messages
        stamp = datetime.fromisoformat(values["DAEMON_CORE_EVAL_NOW"]).timestamp()
        preflight = {}
        for platform in ("telegram", "openclaw"):
            started = time.perf_counter()
            messages, context = compose_canonical_voice_messages(harness.USER,
                observation["text"], platform=platform, message_received_at=stamp)
            preflight[platform] = {"catalog": context.atlas.souvenir_names,
                                  "situation_source": context.situation_source,
                                  "seconds": time.perf_counter() - started}
            if observation["kind"] == "probe":
                assert not context.active_context.strip()
                assert context.situation_source == "seed"
                from daemon_core.memory.reader import load_recent_dialogue
                recent = load_recent_dialogue(harness.USER, max_turns=24)
                assert all(turn["text"] not in recent for turn in args.case_fixture["sources"])
                exposure = source_exposure(messages, args.case_fixture)
                preflight[platform]["earlier_sources_in_context"] = exposure
                if args.cold_context.get("require_no_source_exposure"):
                    assert not exposure, f"Historical source exposed before voice: {exposure}"
            harness.write_json(run / f"composition-{platform}.json", {
                "messages": messages, **preflight[platform]})
        outcome["preflight"] = preflight
        if args.preflight_only:
            outcome["preflight_complete"] = True
            return
        from benchmarks.productivity_v1.runner import hermes
        prepare_codex_cli_home(run / "codex-home")
        hermes._prepare_profile(source_home=args.hermes_home, profile=run / "hermes-home",
            arm="C", model="gpt-6-astra", provider="openai-codex", daemon_values=values)
        # Prior audits recorded an import timeout in this optional observer.
        # Native results, exact request capture and daemon telemetry are sufficient here.
        import yaml
        config_path = run / "hermes-home/config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["plugins"]["enabled"] = ["daemon_core", "daemon_voice"]
        config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        (run / "hermes-home/plugins/productivity_benchmark_observer").unlink()
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent
        runtime = resolve_runtime_provider(requested="openai-codex")
        patch_reception_clock(stamp)
        deltas = []
        started = time.perf_counter()
        agent = AIAgent(model="gpt-6-astra", provider=runtime["provider"],
            api_mode=runtime["api_mode"], base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"), user_id=harness.USER, platform="telegram",
            session_id="isolated-organization-" + observation["id"],
            reasoning_config={"enabled": True, "effort": "high"}, enabled_toolsets=["memory"],
            max_iterations=args.bounds["voice_iterations"],
            run_budget_seconds=args.bounds["voice_seconds"], quiet_mode=True,
            skip_background_review=True, skip_context_files=True,
            stream_delta_callback=lambda text: deltas.append((time.perf_counter() - started, text)) if text else None)
        assert {"daemon_read_souvenirs", "daemon_recall_l0"} <= agent.valid_tool_names
        initialized = time.perf_counter() - started
        result = agent.run_conversation(user_message=observation["text"], conversation_history=[])
        elapsed = time.perf_counter() - started
        agent.shutdown_memory_provider(result.get("messages", []))
        harness.write_json(run / "voice.json", {"result": result, "prompt": observation["text"],
            "response_seconds": elapsed, "initialization_seconds": initialized,
            "first_delta_seconds": deltas[0][0] if deltas else None})
        print(json.dumps({"id": observation["id"], "stage": "voice", "seconds": round(elapsed, 3),
                          "answer": result.get("final_response")}, ensure_ascii=False), flush=True)
        capture = read_json(Path(values["DAEMON_CORE_VOICE_CAPTURE_FILE"]))
        harness.write_json(run / "first-request.json", capture)
        assert capture["metadata"]["prompt_variant"] == "voice_brief_authority_hierarchy_v3"
        assert capture["metadata"]["model"] == "gpt-6-astra"
        assert observation["date"] in json.dumps(capture["request"]["body"], ensure_ascii=False)
        if observation["kind"] == "probe" and args.cold_context.get("require_no_source_exposure"):
            assert not source_exposure(capture["request"]["body"], args.case_fixture)
        log = (run / "worker.log").open("w")
        process = subprocess.Popen(child_command(args, observation) + ["--worker"],
            cwd=run, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
        started = time.perf_counter()
        wait_for_after_turn_service(process=process, spool_root=run / "after-turn-spool",
                                   log_path=run / "worker.log")
        lifecycle = wait_for_durable_after_turn(process=process,
            spool_root=run / "after-turn-spool",
            receipt_path=run / "continuity-receipts/continuity_receipts.jsonl",
            telemetry_path=run / "llm.jsonl", host="hermes", user_id=harness.USER,
            expected_turns=1, timeout_seconds=args.bounds["after_turn_seconds"])
        outcome.update(lifecycle=lifecycle, after_turn_seconds=time.perf_counter() - started,
            complete=bool(result.get("completed") and not result.get("failed")
                          and lifecycle["complete"] and not any(lifecycle["spool_counts"].values())))
    except Exception as error:
        outcome.update(error_type=type(error).__name__, error=str(error))
        raise
    finally:
        harness.stop_process(process)
        if log:
            log.close()
        cleanup_credentials(run)
        after = harness.snapshot(run, "after")
        outcome["changed_paths"] = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        harness.write_json(run / "result.json", outcome)
        print(json.dumps({"id": observation["id"], "stage": "complete",
                          "complete": outcome.get("complete"), "error": outcome.get("error_type")}), flush=True)


def reported_usage(output):
    total, missing = 0, 0
    for path in output.glob("*-top12/voice.json"):
        value = read_json(path)["result"].get("total_tokens")
        if isinstance(value, (int, float)):
            total += value
        else:
            missing += 1
    for path in output.glob("*-top12/llm.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            value = (json.loads(line).get("provider_usage") or {}).get("total_tokens")
            if isinstance(value, (int, float)):
                total += value
            else:
                missing += 1
    return {"reported_total_tokens": total, "usage_rows_missing": missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "hermes-source", "hermes-home", "codex-bin"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--embedding-url", default="http://127.0.0.1:11434")
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--layout-only", action="store_true")
    parser.add_argument("--observation")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--structure", action="store_true", help="Exercise the isolated P1 format")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if Path("/private/tmp") not in args.output.parents or not args.output.name.startswith("daemon-memory-organization-"):
        raise ValueError("Use a dedicated /private/tmp/daemon-memory-organization-* root")
    os.umask(0o077)
    args.fixture = args.fixture.resolve()
    fixture = read_json(args.fixture)
    args.cold_context = fixture.get("cold_context", {})
    args.bounds = fixture["bounds"]
    plan = observations(fixture)
    if args.observation:
        observation = next(item for item in plan if item["id"] == args.observation)
        args.case_fixture = next(case for case in fixture["cases"] if case["id"] == observation["case"])
        configure(args, observation)
        if args.worker:
            harness.worker(args)
            return
        if args.child:
            child(args, observation)
            return
    args.output.mkdir(parents=True, exist_ok=False)
    if args.preflight_only:
        plan = [item for item in plan if (item["kind"] == "source" and not item["previous"])
                or (item["kind"] == "probe" and args.cold_context.get("require_no_source_exposure"))]
    harness.write_json(args.output / "plan.json", {"fixture_sha256": digest(args.fixture), "cold_context": args.cold_context,
        "runner_sha256": digest(Path(__file__)), "observations": plan, "bounds": args.bounds,
        "preflight_only": args.preflight_only})
    start = time.monotonic()
    outcomes = []
    try:
        for observation in plan:
            usage = reported_usage(args.output)
            if time.monotonic() - start >= args.bounds["campaign_seconds"]:
                raise RuntimeError("Campaign wall-time bound reached")
            if usage["reported_total_tokens"] >= args.bounds["reported_tokens_stop_between_turns"]:
                raise RuntimeError("Reported token bound reached between observations")
            command = child_command(args, observation) + ["--child"]
            if args.preflight_only:
                command.append("--preflight-only")
                if observation["kind"] == "probe":
                    command.append("--layout-only")
            process = None
            try:
                with (args.output / (observation["id"] + ".log")).open("w") as log:
                    process = subprocess.Popen(command, cwd=args.output,
                        env={**os.environ, "PYTHONPATH": str(args.hermes_source), "PYTHONDONTWRITEBYTECODE": "1"},
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    remaining = args.bounds["campaign_seconds"] - (time.monotonic() - start)
                    code = process.wait(timeout=min(args.bounds["observation_seconds"], remaining))
                outcome = read_json(run_path(args.output, observation) / "result.json")
                outcomes.append({"id": observation["id"], "exit_code": code,
                                 "complete": outcome.get("complete"),
                                 "preflight_complete": outcome.get("preflight_complete")})
                print(json.dumps(outcomes[-1]), flush=True)
                if code or not (outcome.get("complete") or outcome.get("preflight_complete")):
                    raise RuntimeError("Incomplete observation retained; no automatic reroll")
            finally:
                harness.stop_process(process, group=True)
                cleanup_credentials(run_path(args.output, observation))
    finally:
        harness.write_json(args.output / "campaign.json", {"outcomes": outcomes,
            "planned": len(plan), "seconds": time.monotonic() - start,
            "usage": reported_usage(args.output), "complete": len(outcomes) == len(plan)
            and all(row["complete"] or row.get("preflight_complete") for row in outcomes)})


if __name__ == "__main__":
    main()
