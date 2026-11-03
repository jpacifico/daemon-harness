#!/usr/bin/env python3
"""Private, voice-only availability ablation using the native Hermes loop."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "src"), str(REPO / "scripts")]


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_manner(manner, spec, ablation):
    """Omit registered instruction text in a test process, never edit its source."""
    if ablation == "none":
        return manner
    if ablation not in {"omit-paragraph", "omit-manner"}:
        raise ValueError("Unknown manner ablation")
    assert spec and hashlib.sha256(manner.encode()).hexdigest() == spec["text_sha256"]
    parts = manner.split("\n\n")
    index = spec["paragraph_index"]
    assert isinstance(index, int) and 0 <= index < len(parts)
    assert hashlib.sha256(parts[index].encode()).hexdigest() == spec["paragraph_sha256"]
    if ablation == "omit-manner":
        return ""
    return "\n\n".join(parts[:index] + parts[index + 1 :])


def run(args):
    os.umask(0o077)
    private = (REPO / "exports/private").resolve()
    manifest_path = args.manifest.resolve()
    assert manifest_path.is_relative_to(private)
    manifest = json.loads(manifest_path.read_text())
    frozen = Path(manifest["memory"]).resolve()
    assert frozen.is_relative_to(private) and not any(
        p.is_symlink() for p in frozen.rglob("*")
    )
    assert all(
        sha(frozen / name) == expected for name, expected in manifest["hashes"].items()
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
    ).strip()
    if not args.preflight:
        assert not subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=REPO, text=True
        ).strip()
    assert Path(args.run).name == args.run
    root = manifest_path.parent / args.run
    assert root.resolve().is_relative_to(private)
    root.mkdir(exist_ok=False)
    (root / "empty.env").touch()
    shutil.copytree(frozen, root / "memory", copy_function=shutil.copyfile)
    if manifest.get("cache"):
        shutil.copyfile(manifest["cache"], root / "atlas-embeddings.sqlite3")
    user = manifest["user_id"]
    from benchmarks.productivity_v1.runner.common import (
        isolated_daemon_env,
        durable_after_turn_env,
    )

    values = durable_after_turn_env(
        isolated_daemon_env(
            {},
            run_root=root,
            memory_root=root / "memory",
            user_id=user,
            platform="telegram",
            telemetry_path=root / "llm.jsonl",
            latency_path=root / "latency.jsonl",
        ),
        user_id=user,
    )
    values.update(
        {
            "DAEMON_CORE_SRC": str(REPO / "src"),
            "DAEMON_CORE_SHARED_WORLD_STATE": "on",
            "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY": "on",
            "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS": user,
            "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS": "telegram,openclaw",
            "DAEMON_CORE_CURRENT_TOPICS": "canary",
            "DAEMON_CORE_CURRENT_TOPICS_USER_IDS": user,
            "DAEMON_CORE_COMMON_GROUND_LIBRARY": "on",
            "DAEMON_CORE_COMMON_GROUND_LIBRARY_USER_IDS": user,
            "DAEMON_CORE_COMMON_GROUND_LIBRARY_PLATFORMS": "telegram,openclaw",
            "DAEMON_CORE_REM_EPISODIC_SCENES": "canary",
            "DAEMON_CORE_REM_EPISODIC_SCENE_USER_IDS": user,
            "DAEMON_CORE_REM_EPISODIC_SCENE_PLATFORMS": "telegram,openclaw",
            "DAEMON_CORE_ATLAS_EMBEDDING_TOP12": "canary",
            "DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS": user,
            "DAEMON_CORE_ATLAS_EMBEDDING_MODEL": "qwen3-embedding:0.6b",
            "DAEMON_CORE_ATLAS_EMBEDDING_URL": "http://127.0.0.1:11434",
            "DAEMON_CORE_VOICE_CAPTURE_FILE": str(root / "first-request.json"),
            "DAEMON_CORE_SCENE_MODEL_SHADOW_PATH": str(root / "scene-shadow.jsonl"),
            "CODEX_CLI_BIN": manifest["codex_bin"],
            "CODEX_HOME": str(root / "codex-home"),
            "DAEMON_CORE_EVAL_NOW": manifest.get(
                "eval_now", "2026-10-10T14:33:31+02:00"
            ),
            "LLM_PROVIDER": "codex_cli",
            "CODEX_CLI_MODEL": "gpt-6-astra",
        }
    )
    if getattr(args, "context_continuity", False):
        values.update(DAEMON_CORE_PERSONAL_CONTEXT="canary",
                      DAEMON_CORE_PERSONAL_CONTEXT_USER_IDS=user,
                      DAEMON_CORE_PERSONAL_CONTEXT_PLATFORMS="telegram,openclaw")
    for key in list(os.environ):
        if key.startswith(
            (
                "DAEMON_CORE_",
                "HERMES_",
                "CODEX_CLI_",
                "TELEGRAM_",
                "DISCORD_",
                "SLACK_",
                "WHATSAPP_",
                "SIGNAL_",
                "MEMORY_",
                "ROUTING_",
                "CONVERSATION_",
            )
        ):
            del os.environ[key]
    os.environ.update(values)
    profile = root / "hermes-home"
    os.environ.update(
        HERMES_HOME=str(profile),
        HERMES_CONFIG=str(profile / "config.yaml"),
        HERMES_ENV=str(profile / ".env"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    logging.basicConfig(
        filename=root / "runtime.log",
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    from daemon_core.paths import MEMORY_ROOT

    assert MEMORY_ROOT == root / "memory"
    from daemon_core import thin_sovereign

    contract = getattr(args, "context_use_contract", "current")
    assert contract in {"current", "clarified"}
    base_builder = thin_sovereign.build_thin_sovereign_messages

    manner_ablation = getattr(args, "manner_ablation", "none")

    def selected_contract(**kwargs):
        kwargs["conversation_manner"] = project_manner(
            kwargs["conversation_manner"], manifest.get("manner_test"), manner_ablation
        )
        kwargs["clarify_personal_context_use"] = contract == "clarified"
        kwargs["recognize_shared_context"] = getattr(args, "recognize_shared_context", False)
        return base_builder(**kwargs)

    thin_sovereign.build_thin_sovereign_messages = selected_contract
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    from daemon_core.semantic_atlas import prewarm_semantic_atlas_cache

    # Hermes records the exact user message before canonical composition.
    # Reproduce that order in this local preview, then restore the original
    # journal bytes before the native host records its own turn.
    from diagnose_memory_organization import patch_reception_clock
    from daemon_core.after_turn import record_user_turn
    from daemon_core.memory.writer import ground_zero_conversation_path

    stamp = manifest["received_at"]
    patch_reception_clock(stamp)
    journal = ground_zero_conversation_path(user, occurred_at=stamp)
    journal_at_cutoff = journal.read_bytes()
    current_entry = (
        "\n## " + manifest["cutoff"] + " — user\n\n" + manifest["message"] + "\n"
    ).encode()
    offset = journal_at_cutoff.rfind(current_entry)
    assert offset >= 0 and not journal_at_cutoff[offset + len(current_entry) :].strip()
    # The frozen export includes this user message. Give the native writer the
    # exact prefix before it, so the user's words are never duplicated.
    journal_before = journal_at_cutoff[:offset]
    journal.write_bytes(journal_before)
    record_user_turn(user, manifest["message"], source_trace_id="availability-preview")
    started = time.perf_counter()
    warmed = prewarm_semantic_atlas_cache(MEMORY_ROOT)
    messages, context = compose_canonical_voice_messages(
        user, manifest["message"], platform="telegram", message_received_at=stamp
    )
    compose_seconds = time.perf_counter() - started
    scene_revision = getattr(context.scene_model, "revision", None)
    assert scene_revision == manifest.get("expected_scene_revision", 99)
    assert context.situation_source == (
        "scene" if scene_revision is not None else "seed"
    )
    assert list(context.hot_memory.souvenir_names) == manifest.get("expected_hot_paths", [])
    catalog = list(context.atlas.souvenir_names)
    # Generic, fixed diagnostic rule: first four nominal Atlas hypotheses.
    # No expected-source name participates in this selection.
    selected = manifest["oracle_paths"] if args.arm == "oracle" else catalog[:4]
    from daemon_core.held_atlas_sovereign import read_exact_active_souvenirs

    started = time.perf_counter()
    preload = read_exact_active_souvenirs(
        selected, MEMORY_ROOT, max_chars=16000, user_id=user, platform="telegram"
    )
    read_seconds = time.perf_counter() - started
    assert len(preload.souvenirs) == len(selected)
    selected_hashes = {
        name: sha(MEMORY_ROOT / "capsules" / (name + ".md")) for name in selected
    }
    flat = json.dumps(messages, ensure_ascii=False)
    assert all(text not in flat for text in manifest["forbidden_future_messages"])
    summary = {
        "revision": revision,
        "arm": args.arm,
        "catalog": catalog,
        "initial_hot_paths": list(context.hot_memory.souvenir_names),
        "preloaded_paths": selected,
        "selected_hashes": selected_hashes,
        "preload_chars": len(preload.text),
        "prewarm": warmed,
        "compose_and_warm_seconds": compose_seconds,
        "read_seconds": read_seconds,
        "expected_in_catalog": manifest["expected_source"] in catalog,
        "expected_in_preload": manifest["expected_source"] in selected,
        "scene_revision": scene_revision,
        "context_use_contract": contract,
        "manner_ablation": manner_ablation,
        "complete": False,
    }
    dump(root / "composition-reference.json", {"messages": messages})
    dump(root / "preflight.json", summary)
    # Override only the test process's already-open-memory slot. Canonical code
    # on disk, source notes, instructions and host loop remain unchanged.
    import daemon_core.canonical_voice as canonical
    from daemon_core import thin_sovereign

    original_compose = canonical.compose_canonical_voice_messages
    original_build = thin_sovereign.build_thin_held_atlas_messages

    def build(**kwargs):
        if args.arm != "reference":
            kwargs["hot_memory"] = preload
        return original_build(**kwargs)

    thin_sovereign.build_thin_held_atlas_messages = build

    def compose(uid, message, **kwargs):
        kwargs["message_received_at"] = stamp
        rows, ctx = original_compose(uid, message, **kwargs)
        if list(ctx.atlas.souvenir_names) != catalog:
            dump(
                root / "composition-mismatch.json",
                {
                    "expected_catalog": catalog,
                    "actual_catalog": list(ctx.atlas.souvenir_names),
                },
            )
            raise SystemExit("Replay catalog mismatch before provider generation")
        if args.arm != "reference":
            ctx = replace(ctx, hot_memory=preload)
        return rows, ctx

    canonical.compose_canonical_voice_messages = compose
    candidate_messages, candidate_context = compose(
        user, manifest["message"], platform="telegram"
    )
    block = re.compile(
        r"(# Exact memories already open\n).*?(?=\n# Durable memory catalog)", re.S
    )

    def without_hot(rows):
        return [
            {**row, "content": block.sub(r"\1[EXACT BODIES]", row["content"])}
            for row in rows
        ]

    assert without_hot(messages) == without_hot(candidate_messages)
    assert (
        sum(row["content"].count(manifest["message"]) for row in candidate_messages)
        == 1
    )
    assert all(
        text not in json.dumps(candidate_messages, ensure_ascii=False)
        for text in manifest["forbidden_future_messages"]
    )
    summary["outside_hot_block_identical"] = True
    summary["candidate_message_chars"] = sum(
        len(row["content"]) for row in candidate_messages
    )
    summary["reference_message_chars"] = sum(len(row["content"]) for row in messages)
    dump(root / "composition-candidate.json", {"messages": candidate_messages})
    if journal_before is None:
        journal.unlink()
    else:
        journal.write_bytes(journal_before)
    summary["preview_user_capture_restored_byte_exact"] = True
    summary["current_message_count"] = 1
    summary["historical_prefix_before_current_sha256"] = hashlib.sha256(
        journal_before
    ).hexdigest()
    dump(root / "preflight.json", summary)
    if args.preflight:
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return
    from benchmarks.productivity_v1.runner import hermes
    from benchmarks.productivity_v1.runner.common import prepare_codex_cli_home

    prepare_codex_cli_home(root / "codex-home")
    hermes._prepare_profile(
        source_home=Path(manifest["hermes_auth_source"]),
        profile=profile,
        arm="C",
        model="gpt-6-astra",
        provider="openai-codex",
        daemon_values=values,
    )
    import yaml

    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["plugins"]["enabled"] = ["daemon_core", "daemon_voice"]
    config["timezone"] = "Europe/Paris"
    if getattr(args, "native_web", False):
        config["web"] = manifest["native_web_config"]
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    (profile / "plugins/productivity_benchmark_observer").unlink()
    sys.path.insert(0, manifest["hermes_source"])
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent
    if getattr(args, "native_web", False):
        from replay_web_boundary import install_public_web_boundary
        install_public_web_boundary(manifest, root / "public-web-egress.jsonl")

    runtime = resolve_runtime_provider(requested="openai-codex")
    deltas = []
    started = time.perf_counter()
    agent = None
    try:
        agent = AIAgent(
            model="gpt-6-astra",
            provider=runtime["provider"],
            api_mode=runtime["api_mode"],
            base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            user_id=user,
            platform="telegram",
            session_id="isolated-availability-" + args.run,
            reasoning_config={"enabled": True, "effort": "medium"},
            enabled_toolsets=(["memory", "web"] if getattr(args, "native_web", False) else ["memory"]),
            max_iterations=(6 if getattr(args, "native_web", False) else 4),
            run_budget_seconds=120,
            quiet_mode=True,
            skip_background_review=True,
            skip_context_files=True,
            stream_delta_callback=lambda text: deltas.append(
                (time.perf_counter() - started, text)
            )
            if text
            else None,
        )
        assert {"daemon_read_souvenirs", "daemon_recall_l0"} <= agent.valid_tool_names
        if getattr(args, "native_web", False):
            assert {"web_search", "web_extract"} <= agent.valid_tool_names
        summary["native_tool_names"] = sorted(agent.valid_tool_names)
        summary["native_web"] = getattr(args, "native_web", False)
        summary["context_continuity"] = getattr(args, "context_continuity", False)
        summary["recognize_shared_context"] = getattr(args, "recognize_shared_context", False)
        # A benchmark must stop before transport if the canonical path degrades.
        # This process-local guard never changes an installed host or plugin.
        voice_modules = [
            module
            for module in list(sys.modules.values())
            if getattr(module, "__file__", None)
            and Path(module.__file__).resolve()
            == (REPO / "integrations/hermes_voice_plugin/__init__.py").resolve()
            and hasattr(module, "_delegated_request_or_none")
        ]
        assert len(voice_modules) == 1
        voice_module = voice_modules[0]
        delegate = voice_module._delegated_request_or_none

        def guarded_delegate(request, **kwargs):
            rewritten = delegate(request, **kwargs)
            record = voice_module._turn_latency.get(
                str(kwargs.get("turn_id") or ""), {}
            )
            if rewritten is None or record.get("degraded"):
                raise SystemExit("Degraded benchmark request refused before transport")
            return rewritten

        voice_module._delegated_request_or_none = guarded_delegate
        initialized = time.perf_counter() - started
        result = agent.run_conversation(
            user_message=manifest["message"], conversation_history=[]
        )
        elapsed = time.perf_counter() - started
        agent.shutdown_memory_provider(result.get("messages", []))
        # Preserve outcome and counters even if a later audit check fails.
        dump(
            root / "native-result.json",
            {
                "result": result,
                "response_seconds": elapsed,
                "first_delta_seconds": deltas[0][0] if deltas else None,
            },
        )
        capture = json.loads((root / "first-request.json").read_text())
        assert (
            capture["metadata"]["prompt_variant"]
            == "voice_brief_authority_hierarchy_v3"
        )
        body = json.dumps(capture["request"]["body"], ensure_ascii=False)
        assert all(text not in body for text in manifest["forbidden_future_messages"])
        clarification = thin_sovereign.PERSONAL_CONTEXT_USE_CLARIFICATION
        actual_instructions = capture["request"]["body"].get("instructions", "")
        assert actual_instructions.count(clarification) == (
            1 if contract == "clarified" else 0
        )
        summary.update(
            complete=bool(result.get("completed") and not result.get("failed")),
            first_delta_seconds=deltas[0][0] if deltas else None,
            response_seconds=elapsed,
            initialization_seconds=initialized,
            result=result,
            capture_measurements=capture["measurements"],
        )
        dump(root / "result.json", summary)
        print(
            json.dumps(
                {
                    "arm": args.arm,
                    "complete": summary["complete"],
                    "seconds": elapsed,
                    "answer": result.get("final_response"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        (profile / "auth.json").unlink(missing_ok=True)
        (profile / ".env").unlink(missing_ok=True)
        if (root / "codex-home").exists():
            shutil.rmtree(root / "codex-home")
        spool = root / "after-turn-spool"
        if spool.exists():
            spool.rename(root / "undrained-after-turn-evidence")
        dump(
            root / "cleanup.json",
            {
                "credentials_removed": True,
                "worker_started": False,
                "queued_after_turn_archived": (
                    root / "undrained-after-turn-evidence"
                ).exists(),
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--arm", choices=["reference", "preloaded", "oracle"], required=True
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--native-web", action="store_true")
    parser.add_argument("--context-continuity", action="store_true")
    parser.add_argument("--recognize-shared-context", action="store_true")
    parser.add_argument(
        "--context-use-contract", choices=["current", "clarified"], default="current"
    )
    parser.add_argument(
        "--manner-ablation",
        choices=["none", "omit-paragraph", "omit-manner"],
        default="none",
    )
    run(parser.parse_args())
