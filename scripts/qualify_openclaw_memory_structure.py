#!/usr/bin/env python3
"""Bounded native OpenClaw compatibility check, using synthetic memory only."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

USER = "synthetic-openclaw-memory-p1"
REPO = Path(__file__).resolve().parents[1]
CODEX_BIN = Path.home() / ".local/share/daemon-harness/host-tools/codex/0.159.2/bin/codex"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def child_worker():
    sys.argv = [sys.argv[0]]
    from daemon_core.providers.telemetry import TelemetryProvider
    original = TelemetryProvider._recorded_complete
    count = 0
    lock = threading.Lock()

    def bounded(self, *args, **kwargs):
        nonlocal count
        with lock:
            if count >= 8:
                raise RuntimeError("Synthetic qualification: eight internal calls exhausted")
            count += 1
        return original(self, *args, **kwargs)

    TelemetryProvider._recorded_complete = bounded
    capture = os.getenv("DAEMON_ROOT_QA_CAPTURE", "")
    if capture:
        capture_path = Path(capture)
        if not capture_path.is_relative_to(Path("/private/tmp")):
            raise ValueError("Synthetic capture must remain in its disposable root")
        import daemon_core.memory_agent as memory_agent
        write = memory_agent._write_souvenir_update_safely
        def captured_write(*args, **kwargs):
            result = write(*args, **kwargs)
            with capture_path.open("a") as stream:
                stream.write(json.dumps({"name":kwargs.get("name"),
                    "proposed_content":kwargs.get("proposed_content"),"result":result})+"\n")
            return result
        memory_agent._write_souvenir_update_safely = captured_write
    from daemon_core.after_turn_daemon import main
    main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=["A", "B"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--codex-plugin", type=Path, help="Verified installed plugin path; avoids invoking discovery against a personal profile")
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--fixture", type=Path, default=REPO / "docs/evaluations/fixtures/openclaw_memory_structure_en.json")
    parser.add_argument("--root-folders", action="store_true")
    parser.add_argument("--transition-case", choices=("finished_project","idea_becomes_project","history_and_silence","idea_inspires_separate_project"))
    parser.add_argument("--transition-mode", choices=("off","v1"), default="off")
    parser.add_argument("--reference-case", help="Optional synthetic reference-engagement observation")
    parser.add_argument("--voice-only", action="store_true", help="Keep isolated after-turn obligations without running a worker")
    args = parser.parse_args()
    if args.voice_only and not args.reference_case:
        parser.error("--voice-only is reserved for the reference comparison")
    root = args.output.resolve()
    source = args.source.resolve()
    if not root.is_relative_to(Path("/private/tmp")) or root.exists():
        raise ValueError("Use a fresh /private/tmp directory")
    if not args.archive.resolve().is_relative_to(REPO / "exports/private"):
        raise ValueError("Evidence must remain in the private local archive")
    os.umask(0o077)
    root.mkdir(parents=True, mode=0o700)
    sys.path[:0] = [str(source), str(source / "src")]
    from benchmarks.productivity_v1.runner import openclaw as r
    from benchmarks.productivity_v1.runner.common import (
        base_process_env, prepare_codex_cli_home, isolated_daemon_env,
        durable_after_turn_env, safe_dotenv, wait_for_after_turn_service,
        wait_for_durable_after_turn, read_jsonl,
    )
    fixture_path = args.fixture.resolve()
    fixture = json.loads(fixture_path.read_text())
    if args.transition_case:
        from scripts.qualify_parga_note_transitions import case_fixture
        fixture = case_fixture(args.transition_case)
        if args.arm != 'B' or not args.root_folders:
            raise ValueError('Transition cases require real PARGA folders')
    reference_case = None
    if args.reference_case:
        reference_fixture = json.loads((REPO / "docs/evaluations/fixtures/reference_engagement_fr.json").read_text())
        reference_case = next(item for item in reference_fixture["cases"] if item["id"] == args.reference_case)
        fixture["prompt"] = reference_case["message"]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    result = {"arm": args.arm, "source_revision": revision, "model": "openai/gpt-6-astra",
              "effort": "medium", "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
              "preflight_only": args.preflight_only, "passed": False}
    services = []
    logs = []
    for name in ("workspace", "state", "home", "tmp", "memory", "hermes-home"):
        (root / name).mkdir()
    (root / "empty.env").touch()
    note = root / "memory/capsules/theme/work/harbor-workshop.md"
    note.parent.mkdir(parents=True)
    original_body = fixture["structured_note" if args.arm == "B" else "legacy_note"]
    note.write_text(original_body)
    (root / "memory/capsules/index.md").write_text(fixture["index"])
    if args.arm == "B":
        save(root / "memory/.memory-structure-prototype.json", {"schema": 1, "memory_root": str(root / "memory")})
    user = root / "memory/users" / USER
    user.mkdir(parents=True)
    shutil.copy2(source / "memory/templates/users/conversation.md", user / "conversation.md")
    memory_runtime = root / "memory"
    if args.transition_case:
        from scripts.qualify_parga_note_transitions import seed_case
        memory_runtime, note = seed_case(root, args.transition_case)
    elif args.root_folders:
        from scripts.qualify_memory_root_folders import convert_fixture
        memory_runtime, note = convert_fixture(root)
    shutil.copy2(source / "integrations/openclaw_plugin/workspace/AGENTS.md", root / "workspace/AGENTS.md")
    binary = r.DEFAULT_OPENCLAW_BINARY.absolute()
    r.DAEMON_PLUGIN = source / "integrations/openclaw_plugin"
    plugin = args.codex_plugin.resolve() if args.codex_plugin else r._discover_codex_plugin(binary)
    if not plugin.is_dir():
        raise ValueError("The installed Codex plugin must exist")
    codex_home = root / "state/agents/main/agent/codex-home"
    api_port, gateway_port = r._free_port(), r._free_port()
    url = f"http://127.0.0.1:{api_port}"
    config = r._config(arm="C", workspace=root / "workspace", daemon_url=url,
                       model=result["model"], gateway_port=gateway_port)
    current = json.loads((Path.home() / ".openclaw/openclaw.json").read_text())
    config["plugins"]["entries"]["codex"]["config"] = copy.deepcopy(current["plugins"]["entries"]["codex"]["config"])
    config["plugins"]["entries"]["codex"]["config"].setdefault("appServer", {})["command"] = str(CODEX_BIN)
    config["models"] = copy.deepcopy(current["models"])
    if reference_case:
        config["tools"] = {
            "profile": "full",
            "allow": ["web_search", "web_fetch", "daemon_read_souvenirs", "daemon_recall_l0"],
            "web": copy.deepcopy(current["tools"]["web"]),
        }
        config["plugins"]["allow"].append("google")
        config["plugins"]["entries"]["google"] = copy.deepcopy(current["plugins"]["entries"]["google"])
    config["agents"]["defaults"].setdefault("models", {})[result["model"]] = {}
    config["plugins"]["entries"]["daemon-harness"]["config"].update(
        userId=USER, runtimeHealthPath=str(root / "openclaw-health.json"))
    save(root / "openclaw.json", config)
    r._prepare_official_codex_install(state_dir=root / "state", codex_plugin_root=plugin)
    sandbox = root / "host.sb"
    r._write_outer_sandbox_profile(path=sandbox, runtime_root=root, openclaw_binary=binary, codex_plugin_root=plugin)
    with sandbox.open("a") as stream:
        stream.write("\n(allow file-read* (subpath " + json.dumps(str(CODEX_BIN.parents[1])) + "))\n")
    host = r._isolate_host_environment(base_process_env(), home=root / "home")
    host.update(OPENCLAW_STATE_DIR=str(root / "state"), OPENCLAW_CONFIG_PATH=str(root / "openclaw.json"),
                OPENCLAW_SKIP_CHANNELS="1", TMPDIR=str(root / "tmp"), TMP=str(root / "tmp"), TEMP=str(root / "tmp"),
                NO_COLOR="1", HOST_CAPABILITY_TRACE_FILE=str(root / "host-trace.jsonl"), PYTHONDONTWRITEBYTECODE="1")
    overlay = durable_after_turn_env(isolated_daemon_env(
        safe_dotenv(args.source_env), run_root=root, memory_root=memory_runtime, user_id=USER,
        platform="openclaw", telemetry_path=root / "telemetry.jsonl", latency_path=root / "latency.jsonl"), user_id=USER)
    overlay.update(DAEMON_CORE_RUNTIME_REVISION=revision, DAEMON_CORE_AFTER_TURN_SERVICE_REVISION=revision,
        DAEMON_CORE_OPENCLAW_AGENT_DB=str(root / "state/agents/main/agent/openclaw-agent.sqlite"),
        DAEMON_CORE_TEMPORAL_CURATOR="on", DAEMON_CORE_MEMORY_STRUCTURE="v1" if args.arm == "B" else "",
        DAEMON_CORE_OBSIDIAN_FORMAT="1" if args.arm == "B" else "", DAEMON_CORE_SRC=str(source / "src"),
        CODEX_HOME=str(root / "daemon-codex"), HERMES_HOME=str(root / "hermes-home"),
        HERMES_CONFIG=str(root / "hermes-home/config.yaml"), HERMES_ENV=str(root / "empty.env"),
        DAEMON_CORE_SCENE_MODEL_SHADOW_PATH=str(root / "scene-shadow.jsonl"), PYTHONPATH=os.pathsep.join([str(source), str(source / "src")]),
        PYTHONDONTWRITEBYTECODE="1")
    for key, value in overlay.items():
        if key.startswith("DAEMON_CORE_") and key != "DAEMON_CORE_SRC" and value.startswith("/"):
            if not Path(value).resolve().is_relative_to(root):
                raise ValueError(f"Non-isolated daemon path: {key}")
    env = dict(host)
    env.update(overlay)
    if args.transition_case:
        env.update(DAEMON_CORE_MEMORY_RELOCATION=args.transition_mode,
                   DAEMON_CORE_MEMORY_SITUATIONS='canary', DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS=USER)
    if args.root_folders:
        env["DAEMON_ROOT_QA_CAPTURE"] = str(root / "memory-writes.jsonl")
    def start(command, log, environment, cwd):
        handle = (root / log).open("w")
        logs.append(handle)
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=handle, stderr=subprocess.STDOUT, text=True)
        services.append(process)
        return process
    try:
        r._validate_config(binary=binary, workspace=root / "workspace", env=host, sandbox_profile=sandbox)
        if args.preflight_only:
            result["passed"] = True
            return
        prepare_codex_cli_home(codex_home)
        prepare_codex_cli_home(root / "daemon-codex")
        result["auth_import"] = r._import_codex_auth(binary=binary, codex_home=codex_home,
            workspace=root / "workspace", env=host, sandbox_profile=sandbox)
        python = r.DEFAULT_DAEMON_PYTHON
        api = start([str(python), "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1", "--port", str(api_port), "--log-level", "warning"], "api.log", env, source)
        r._wait_for_health(url, api, root / "api.log")
        worker = None
        if not args.voice_only:
            worker = start([str(python), str(Path(__file__).resolve()), "worker"], "worker.log", env, source)
            wait_for_after_turn_service(process=worker, spool_root=root / "after-turn-spool", log_path=root / "worker.log")
        gateway = start(r._host_command(sandbox_profile=sandbox, binary=binary,
            arguments=["gateway", "run", "--port", str(gateway_port), "--bind", "loopback", "--auth", "none"]), "gateway.log", host, root / "workspace")
        r._wait_for_gateway(process=gateway, log_path=root / "gateway.log", binary=binary,
            workspace=root / "workspace", env=host, sandbox_profile=sandbox, port=gateway_port)
        began = time.perf_counter()
        response = subprocess.run(r._host_command(sandbox_profile=sandbox, binary=binary,
            arguments=["agent", "--session-key", "agent:main:explicit:" + uuid.uuid4().hex, "--json",
                       "--thinking", "medium", "--timeout", "120", "--message", fixture["prompt"]]),
            cwd=root / "workspace", env=host, capture_output=True, text=True, timeout=150)
        result["voice_seconds"] = time.perf_counter() - began
        result["returncode"] = response.returncode
        save(root / "response.json", {"stdout": response.stdout, "stderr": response.stderr})
        if response.returncode:
            raise RuntimeError("Native OpenClaw failed; no quality reroll")
        envelope = json.loads(response.stdout)
        answer = "\n".join(item.get("text", "") for item in envelope.get("result", envelope).get("payloads", []))
        result["answer"] = answer
        began = time.perf_counter()
        if args.voice_only:
            result["lifecycle"] = {"status": "not_run", "complete": None}
            result["after_turn_wait_seconds"] = None
        else:
            result["lifecycle"] = wait_for_durable_after_turn(process=worker, spool_root=root / "after-turn-spool",
                receipt_path=root / "continuity-receipts/continuity_receipts.jsonl", telemetry_path=root / "telemetry.jsonl",
                host="openclaw", user_id=USER, expected_turns=1, timeout_seconds=300)
            result["after_turn_wait_seconds"] = time.perf_counter() - began
        result["voice_telemetry"] = r._rollout_telemetry(codex_home)
        events = read_jsonl(root / "telemetry.jsonl")
        result["daemon_token_scopes"] = r._daemon_token_scopes(events, arm="C")
        lifecycle = result["lifecycle"]
        result["host_tool_events"] = [event for event in read_jsonl(root / "host-trace.jsonl")
                                      if event.get("event") == "tool_completed"]
        if args.transition_case:
            from scripts.qualify_parga_note_transitions import assess_case
            from scripts.qualify_parga_note_transitions import FIXTURE as transition_fixture
            result.update(transition_case=args.transition_case, transition_mode=args.transition_mode,
                          fixture_sha256=hashlib.sha256(transition_fixture.read_bytes()).hexdigest())
            result['checks'] = assess_case(root, result, args.transition_case, args.transition_mode)
            result['passed'] = all(result['checks'].values())
            result['quality_judgment'] = 'pending_operator_review'
            return
        if reference_case:
            result["reference_case"] = reference_case["id"]
            result["reference_expected"] = reference_case["expected"]
            result["reference_fixture_sha256"] = hashlib.sha256(
                (REPO / "docs/evaluations/fixtures/reference_engagement_fr.json").read_bytes()).hexdigest()
            result["checks"] = {
                "native_response_completed": bool(answer.strip()),
            }
            if not args.voice_only:
                result["checks"].update(
                    after_turn_complete=lifecycle["complete"] and lifecycle["cycle_status_counts"].get("completed", 0) == 1,
                    no_failed_or_pending_work=not any(lifecycle["spool_counts"].values()))
            # Technical completeness is not a quality judgment. The operator
            # must compare the exact answer with the native tool results.
            result["quality_judgment"] = "pending_operator_review"
            result["passed"] = all(result["checks"].values())
            return
        if args.root_folders:
            from daemon_core.memory.library_layout import unique_note_locations
            note_files = [item.path for item in unique_note_locations(memory_runtime)]
        else:
            note_files = list((root / "memory/capsules/theme").rglob("*.md"))
        terms = fixture.get("new_fact_terms", ["cedar", "14"])
        contains_new_fact = lambda path: all(term in path.read_text().lower() for term in terms)
        checks = {
            "source_recalled": "reclaimed maple" in answer.lower(),
            "history_preserved": all(fact in note.read_text() for fact in fixture["preserved_facts"]),
            "new_fact_stored": any(contains_new_fact(path) for path in note_files),
            "after_turn_complete": lifecycle["complete"] and lifecycle["cycle_status_counts"].get("completed", 0) == 1,
            "no_failed_or_pending_work": not any(lifecycle["spool_counts"].values()),
            "native_memory_tool_called": any(call.get("tool_name") == "daemon_read_souvenirs" and call.get("status") == "ok"
                                              for call in result["host_tool_events"]),
        }
        if fixture.get("require_no_rejected_mutation"):
            receipts = read_jsonl(root / "continuity-receipts/continuity_receipts.jsonl")
            checks["no_rejected_mutation"] = not any(event.get("stage") == "memory_mutation" and event.get("status") == "failed" for event in receipts)
        if args.arm == "B":
            from daemon_core.memory.context_pages import validate_context_page
            from daemon_core.memory.metadata import parse_souvenir_frontmatter
            stored = [path for path in note_files if contains_new_fact(path)]
            checks["structured_new_state"] = bool(stored)
            for path in stored:
                validate_context_page(path.read_text())
                properties, body = parse_souvenir_frontmatter(path.read_text())
                checks["structured_new_state"] &= bool(properties.get("kind") and properties.get("memory_views")
                    and "## Scope" in body and "## Last confirmed state" in body)
            if args.root_folders:
                checks["project_navigation"] = (root / "memory/Projects").is_dir() and not (root / "memory/Home.md").exists()
            else:
                checks["project_navigation"] = (root / "memory/views/projects.md").is_file()
        result["checks"] = checks
        result["passed"] = all(checks.values())
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for process in reversed(services):
            r._stop_process(process)
        for handle in logs:
            handle.close()
        # Native SQLite stores imported OAuth credentials. Keep synthetic rollouts,
        # source notes and receipts, but remove the auth database after shutdown.
        removed = []
        for path in root.rglob("*"):
            if path.is_file() and (path.name in {"auth.json", "auth-profiles.json", "auth-credentials.json"}
                                  or path.name.startswith("openclaw-agent.sqlite")):
                removed.append(str(path.relative_to(root)))
                path.unlink()
        result["credential_artifacts_removed"] = removed
        if reference_case:
            # A native public Web provider can require a copied API key. Keep
            # its configuration private during the run, remove it before the
            # evidence is archived, and leave the user's original untouched.
            removed_keys = []
            for config_path in root.glob("openclaw.json*"):
                if not config_path.is_file():
                    continue
                stored_config = json.loads(config_path.read_text())
                web_search = stored_config.get("plugins", {}).get("entries", {}).get("google", {}).get("config", {}).get("webSearch", {})
                if web_search.pop("apiKey", None) is not None:
                    removed_keys.append(config_path.name)
                    save(config_path, stored_config)
            result["copied_web_key_removed"] = bool(removed_keys)
            result["web_key_artifacts_removed"] = removed_keys
        result["all_test_processes_stopped"] = all(p.poll() is not None for p in services)
        save(root / "result.json", result)
        args.archive.mkdir(parents=True, exist_ok=True)
        destination = args.archive / root.name
        shutil.copytree(root, destination, symlinks=True, ignore=shutil.ignore_patterns("node_modules", "cache"))
        print(json.dumps({k: v for k, v in result.items() if k not in {"voice_telemetry", "daemon_token_scopes", "answer"}}, ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        child_worker()
    else:
        main()
