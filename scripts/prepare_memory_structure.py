#!/usr/bin/env python3
"""Prepare and format an authorized private copy, never the source memory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

REPO = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def hashes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Snapshot must not follow symlinks")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def snapshot(args):
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a fresh root outside the source memory")
    output.mkdir(parents=True, mode=0o700)
    before = hashes(source)
    shutil.copytree(source, output / "original-memory")
    copied = hashes(output / "original-memory")
    after = hashes(source)
    manifest = {"schema": 1, "source": str(source), "root": str(output),
                "source_hashes": before, "snapshot_complete": before == copied == after}
    write_json(output / "manifest.json", manifest)
    if not manifest["snapshot_complete"]:
        raise RuntimeError("Source changed during snapshot; incomplete copy retained, not activated")
    shutil.copytree(output / "original-memory", output / "memory")
    write_json(output / "memory/.memory-structure-prototype.json",
               {"schema": 1, "memory_root": str(output / "memory")})
    print(json.dumps({"snapshot_complete": True, "files": len(before)}), flush=True)


def configure(args):
    root = args.output.resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    if not manifest.get("snapshot_complete") or manifest.get("root") != str(root):
        raise ValueError("Complete recognized private snapshot required")
    # Importing daemon code must happen only after installing its private paths.
    for key in tuple(os.environ):
        if key.startswith(("DAEMON_CORE_", "LIQUID_HARNESS_", "CODEX_CLI_", "HERMES_",
                           "MEMORY_", "ROUTING_", "CONVERSATION_", "TELEGRAM_")):
            os.environ.pop(key)
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    from benchmarks.productivity_v1.runner.common import isolated_daemon_env
    env = isolated_daemon_env({}, run_root=root / "format-runtime",
        memory_root=root / "memory", user_id="isolated-structure-formatting", platform="offline",
        telemetry_path=root / "format-telemetry.jsonl", latency_path=root / "format-latency.jsonl")
    env.update({"DAEMON_CORE_MEMORY_STRUCTURE": "v1", "DAEMON_CORE_OBSIDIAN_FORMAT": "1",
                "LLM_PROVIDER": "codex_cli", "CODEX_CLI_MODEL": "gpt-6-astra",
                "CODEX_CLI_BIN": str(args.codex_bin.resolve()) if args.codex_bin else "",
                "CODEX_CLI_TIMEOUT_SECONDS": "180", "MEMORY_REASONING_EFFORT": "high",
                "CODEX_HOME": str(root / "format-auth"), "PYTHONDONTWRITEBYTECODE": "1"})
    os.environ.update(env)
    (root / "format-runtime").mkdir(exist_ok=True)
    (root / "format-runtime/empty.env").touch()
    return root


def plan_batches(root):
    from daemon_core.memory.context_page_formatting import prepare_format_batch
    memory = root / "memory"
    names, excluded = [], []
    for path in sorted((memory / "capsules/theme").rglob("*.md")):
        name = path.relative_to(memory / "capsules").with_suffix("").as_posix()
        try:
            prepare_format_batch(memory, [name])
            names.append(name)
        except ValueError:
            excluded.append(name)
    batches, current = [], []
    for name in names:
        try:
            prepare_format_batch(memory, current + [name])
        except ValueError:
            batches.append(current)
            current = []
        current.append(name)
    if current:
        batches.append(current)
    return {"batches": batches, "excluded": excluded, "eligible": len(names),
            "model": "gpt-6-astra", "effort": "high", "max_calls": len(batches),
            "max_seconds_per_call": 180, "max_wall_seconds": 3600,
            "reported_tokens_stop_between_calls": 1_500_000}


def reported_tokens(root):
    total = 0
    path = root / "format-telemetry.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            usage = json.loads(line).get("provider_usage") or {}
            total += usage.get("total_tokens") or 0
    return total


def format_copy(args, root):
    from daemon_core.memory.context_page_formatting import prepare_format_batch, publish_format_batch
    from daemon_core.memory.context_pages import rebuild_navigation
    from daemon_core.providers.factory import build_memory_provider
    from benchmarks.productivity_v1.runner.common import prepare_codex_cli_home, source_revision
    revision = source_revision(REPO)
    if revision["dirty"]:
        raise ValueError("Commit code and protocol before model calls")
    if not args.codex_bin or not args.codex_bin.is_file():
        raise ValueError("Pinned Codex binary required")
    plan_path = root / "format-plan.json"
    if not plan_path.exists():
        raise ValueError("Run plan before model calls")
    plan = json.loads(plan_path.read_text())
    auth = root / "format-auth"
    if auth.exists():
        raise ValueError("Stale formatting profile; inspect it before resuming")
    prepare_codex_cli_home(auth, source=args.auth_source)
    provider = build_memory_provider()
    started = time.monotonic()
    completed, failed = 0, []
    try:
        for number, names in enumerate(plan["batches"], 1):
            record_path = root / "format-batches" / f"{number:03d}.json"
            if record_path.exists():
                record = json.loads(record_path.read_text())
                if record.get("complete"):
                    completed += 1
                    continue
                failed.append(number)
                continue  # No quality reroll or implicit overwrite on resume.
            if time.monotonic() - started >= plan["max_wall_seconds"] or reported_tokens(root) >= plan["reported_tokens_stop_between_calls"]:
                raise RuntimeError("Registered formatting budget reached")
            messages, sources = prepare_format_batch(root / "memory", names)
            record = {"number": number, "source_revision": revision, "messages": messages,
                      "source_hashes": {n: hashlib.sha256(b).hexdigest() for n,b in sources.items()},
                      "complete": False}
            write_json(record_path, record)
            t0 = time.perf_counter()
            try:
                record["response"] = provider.complete(messages=messages, temperature=0.0)
                result = publish_format_batch(root / "memory", sources, record["response"])
                record["publication"] = result
                record["complete"] = not result["conflicted"]
            except Exception as error:
                record["error"] = type(error).__name__ + ": " + str(error)
                failed.append(number)
            finally:
                record["seconds"] = time.perf_counter() - t0
                write_json(record_path, record)
            if record["complete"]:
                completed += 1
            print(json.dumps({"batch": number, "of": len(plan["batches"]),
                              "complete": record["complete"], "seconds": round(record["seconds"], 2)}), flush=True)
        write_json(root / "format-result.json", {"completed_batches": completed,
            "failed_batches": failed, "seconds": time.monotonic() - started,
            "navigation": rebuild_navigation(root / "memory"),
            "final_hashes": hashes(root / "memory")})
    finally:
        shutil.rmtree(auth)


def recover_valid_notes(root):
    """Publish valid notes from frozen failed responses, without another model call."""
    from daemon_core.memory.context_page_formatting import publish_format_batch, render_format_decision
    from benchmarks.productivity_v1.runner.common import source_revision
    revision = source_revision(REPO)
    if revision["dirty"]:
        raise ValueError("Commit recovery mechanism before publication")
    results = []
    for record_path in sorted((root / "format-batches").glob("*.json")):
        record = json.loads(record_path.read_text())
        if record.get("complete") or not record.get("response"):
            continue
        target = root / "format-recovery" / record_path.name
        if target.exists():
            continue
        decisions = json.loads(record["response"])["notes"]
        names = [n["name"] for n in decisions]
        if len(names) != len(set(names)) or set(names) != set(record["source_hashes"]):
            raise ValueError("Invalid frozen batch identities")
        safe, sources, refused = [], {}, []
        for decision in decisions:
            name = decision["name"]
            # The original formatter verifies every name before recording it.
            relative = Path("capsules") / (name + ".md")
            original = root / "original-memory" / relative
            if not original.resolve().is_relative_to((root / "original-memory/capsules/theme").resolve()):
                raise ValueError("Frozen source path escaped")
            raw = original.read_bytes()
            if hashlib.sha256(raw).hexdigest() != record["source_hashes"][name]:
                raise ValueError("Frozen source changed")
            try:
                render_format_decision(name, raw, decision)
            except (ValueError, TypeError) as error:
                refused.append({"name": name, "error": type(error).__name__ + ": " + str(error)})
                continue
            safe.append(decision)
            sources[name] = raw
        response = json.dumps({"notes": safe}, ensure_ascii=False)
        publication = publish_format_batch(root / "memory", sources, response) if safe else {"changed": [], "conflicted": []}
        recovery = {"original_batch": record_path.name, "source_revision": revision,
                    "original_response_sha256": hashlib.sha256(record["response"].encode()).hexdigest(),
                    "response": response, "source_hashes": {n: record["source_hashes"][n] for n in sources},
                    "publication": publication, "refused": refused,
                    "complete": not publication["conflicted"], "model_calls": 0}
        write_json(target, recovery)
        results.append({"batch": record_path.name, "published": len(publication["changed"]),
                        "refused": len(refused), "conflicted": len(publication["conflicted"])})
    print(json.dumps(results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("snapshot", "plan", "format", "recover", "views"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--auth-source", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.stage == "snapshot":
        if not args.source:
            parser.error("snapshot requires --source")
        snapshot(args)
        return
    root = configure(args)
    if args.stage == "plan":
        plan = plan_batches(root)
        if (root / "format-plan.json").exists():
            raise ValueError("Format plan already frozen")
        write_json(root / "format-plan.json", plan)
        print(json.dumps({"batches": len(plan["batches"]), "eligible": plan["eligible"],
                          "excluded": len(plan["excluded"])}))
    elif args.stage == "format":
        format_copy(args, root)
    elif args.stage == "recover":
        recover_valid_notes(root)
    else:
        from daemon_core.memory.context_pages import rebuild_navigation
        print(json.dumps(rebuild_navigation(root / "memory")))


if __name__ == "__main__":
    main()
