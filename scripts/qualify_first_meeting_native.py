#!/usr/bin/env python3
"""Three first-meeting turns, real native hosts and canonical after-turn, no live channel."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
USER = "999000112233"
HERMES = Path(
    os.environ.get(
        "DAEMONS_QA_HERMES_ROOT",
        str(
            Path.home() / ".local/share/daemon-harness/host-tools/hermes/0.21.5-f97608f"
        ),
    )
)
CODEX = Path(
    os.environ.get(
        "CODEX_CLI_BIN",
        str(
            Path.home()
            / ".local/share/daemon-harness/host-tools/codex/0.159.2/bin/codex"
        ),
    )
)
PYTHON = Path(sys.executable)
PROMPTS = [
    "Salut !",
    "Tu peux m'appeler Morgan. Je prépare une petite exposition de mes photographies, sous le titre Lantern Atlas. Le code de réservation de la salle est LANTERN-583. J'aimerais qu'on travaille ensemble sur le choix des photos.",
    "Let's continue in English. Before we go on, remind me what you call me, which project I am preparing and its venue booking code.",
]



def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def hermes_turn(root, turn):
    sys.path[:0] = [str(SOURCE), str(SOURCE / "src"), str(HERMES)]
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    runtime = resolve_runtime_provider(requested="openai-codex")
    began, deltas = time.monotonic(), []
    agent = AIAgent(
        model="gpt-6-astra",
        provider=runtime["provider"],
        api_mode=runtime["api_mode"],
        base_url=runtime.get("base_url"),
        api_key=runtime.get("api_key"),
        user_id=USER,
        platform="telegram",
        session_id="isolated-first-meeting",
        reasoning_config={"enabled": True, "effort": "medium"},
        enabled_toolsets=["memory"],
        max_iterations=5,
        run_budget_seconds=120,
        quiet_mode=True,
        skip_background_review=True,
        skip_context_files=True,
        stream_delta_callback=lambda text: deltas.append(time.monotonic() - began)
        if text
        else None,
    )
    assert {"daemon_read_souvenirs", "daemon_recall_l0"} <= agent.valid_tool_names
    response = agent.run_conversation(
        user_message=PROMPTS[turn - 1], conversation_history=(
            json.loads((root / f"voice-{turn-1}.json").read_text()).get("messages", [])
            if turn == 2 else []
        )
    )
    seconds = time.monotonic() - began
    agent.shutdown_memory_provider(response.get("messages", []))
    save(root / f"voice-{turn}.json", response)
    save(
        root / f"timing-{turn}.json",
        {
            "voice_seconds": seconds,
            "first_delta_seconds": deltas[0] if deltas else None,
        },
    )
    if not response.get("completed") or response.get("failed"):
        raise RuntimeError("Native Hermes did not complete; no quality reroll")


def worker():
    import threading
    from daemon_core.providers.telemetry import TelemetryProvider

    original = TelemetryProvider._recorded_complete
    telemetry = Path(os.environ["DAEMON_CORE_LLM_TELEMETRY_FILE"])
    count = len(telemetry.read_text().splitlines()) if telemetry.exists() else 0
    lock = threading.Lock()

    def bounded(self, *args, **kwargs):
        nonlocal count
        with lock:
            if count >= 32:
                raise RuntimeError(
                    "Qualification budget: 32 maintenance calls exhausted"
                )
            count += 1
        return original(self, *args, **kwargs)

    TelemetryProvider._recorded_complete = bounded
    from daemon_core.after_turn_daemon import main

    sys.argv = [sys.argv[0]]
    main()


def exact_user_counts(memory, prompts):
    from daemon_core.scene_model import _parse_dialogue_turns
    turns = [turn for path in (memory / "levels/L0_ground_zero/conversations").glob("*.md")
             for turn in _parse_dialogue_turns(path.read_text())]
    return [sum(role == "user" and body.strip() == prompt for _, role, body in turns)
            for prompt in prompts]


def main(root, first_host, arm, resume=False, telegram_adapter=False):
    global USER
    transport = None
    if telegram_adapter:
        if resume:
            raise ValueError("Transport tests cannot resume a previous campaign")
        from scripts.qualification.first_meeting_transport import TelegramAdapterProbe
        transport = TelegramAdapterProbe()
        USER = str(transport.user_id)
    if first_host not in {"hermes", "openclaw"} or arm not in {"baseline", "candidate"}:
        raise ValueError("Select a host and comparison arm")
    from benchmarks.productivity_v1.runner import openclaw as oc, hermes as he
    from benchmarks.productivity_v1.runner.common import (
        base_process_env,
        isolated_daemon_env,
        durable_after_turn_env,
        prepare_codex_cli_home,
        wait_for_after_turn_service,
        wait_for_durable_after_turn,
        read_jsonl,
    )

    root = root.resolve()
    if (root.exists() != resume) or not root.is_relative_to(Path("/private/tmp")):
        raise ValueError("A fresh disposable /private/tmp root is required")
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=SOURCE, text=True
    ).strip():
        raise ValueError(
            "Commit the qualification protocol and script before calling models"
        )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
    ).strip()
    os.umask(0o077)
    from types import SimpleNamespace
    from daemons_companion.hermes.first_install import initialize_private_memory
    from daemon_core.daemon_identity import save_daemon_identity
    from daemon_core.installation_profile import profile_environment
    vault = root / "data/memory"
    memory = vault / ".daemon"
    if not resume:
        root.mkdir(mode=0o700)
        for name in ("workspace", "state", "home", "tmp"):
            (root / name).mkdir()
        (root / "empty.env").touch()
        scope = SimpleNamespace(home=root, attempt=root, validate_home=lambda _: None)
        initialize_private_memory(scope, SOURCE, USER, root_folder_memory=True,
                                  conversation_language="fr" if arm == "candidate" else None)
        save_daemon_identity(memory, "Aster", "feminine")
    shutil.copy2(
        SOURCE / "integrations/openclaw_plugin/workspace/AGENTS.md",
        root / "workspace/AGENTS.md",
    )
    identity_hash = hashlib.sha256(
        (memory / "core/daemon_identity.json").read_bytes()
    ).hexdigest()
    binary = oc.DEFAULT_OPENCLAW_BINARY.absolute()
    candidates = list(
        (Path.home() / ".openclaw/npm/projects").glob(
            "*/node_modules/@openclaw/codex/package.json"
        )
    )
    candidates = [
        p
        for p in candidates
        if json.loads(p.read_text()).get("version") == "2026.9.1-beta.1"
    ]
    if len(candidates) != 1:
        raise RuntimeError("Ambiguous installed native Codex plugin")
    plugin = candidates[0].parent
    oc.DAEMON_PLUGIN = SOURCE / "integrations/openclaw_plugin"
    he.DAEMON_MEMORY_PLUGIN = SOURCE / "integrations/hermes_plugin"
    he.DAEMON_VOICE_PLUGIN = SOURCE / "integrations/hermes_voice_plugin"
    api_port, gateway_port = oc._free_port(), oc._free_port()
    url = f"http://127.0.0.1:{api_port}"
    config_path = root / f"openclaw-{revision[:8]}.json"
    config = oc._config(
        arm="C",
        workspace=root / "workspace",
        daemon_url=url,
        model="openai/gpt-6-astra",
        gateway_port=gateway_port,
    )
    config["plugins"]["entries"]["codex"]["config"]["appServer"]["command"] = str(CODEX)
    config["plugins"]["entries"]["daemon-harness"]["config"].update(
        userId=USER, runtimeHealthPath=str(root / "openclaw-health.json")
    )
    config["agents"]["defaults"]["models"] = {"openai/gpt-6-astra": {}}
    config["models"] = {
        "mode": "merge",
        "providers": {
            "openai": {
                "api": "openai-chatgpt-responses",
                "baseUrl": "https://chatgpt.com/backend-api/codex",
                "models": [
                    {
                        "id": "gpt-6-astra",
                        "name": "GPT-6 Astra",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 272000,
                        "maxTokens": 128000,
                    }
                ],
            }
        },
    }
    config["logging"] = {"file": str(root / "openclaw-native.log")}
    if transport:
        transport.configure_openclaw(config)
    save(config_path, config)
    if not resume:
        oc._prepare_official_codex_install(
            state_dir=root / "state", codex_plugin_root=plugin
        )
    sandbox = root / "host.sb"
    oc._write_outer_sandbox_profile(
        path=sandbox,
        runtime_root=root,
        openclaw_binary=binary,
        codex_plugin_root=plugin,
    )
    with sandbox.open("a") as f:
        f.write(
            "\n(allow file-read* (subpath " + json.dumps(str(CODEX.parents[1])) + "))\n"
        )
    host = oc._isolate_host_environment(base_process_env(), home=root / "home")
    host.update(
        OPENCLAW_STATE_DIR=str(root / "state"),
        OPENCLAW_CONFIG_PATH=str(config_path),
        OPENCLAW_SKIP_CHANNELS="1",
        TMPDIR=str(root / "tmp"),
        TMP=str(root / "tmp"),
        TEMP=str(root / "tmp"),
        NO_COLOR="1",
        HOST_CAPABILITY_TRACE_FILE=str(root / "openclaw-trace.jsonl"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    if transport:
        host["OPENCLAW_SKIP_CHANNELS"] = "0"
    values = durable_after_turn_env(
        isolated_daemon_env(
            {},
            run_root=root,
            memory_root=memory,
            user_id=USER,
            platform="telegram,openclaw",
            telemetry_path=root / "telemetry.jsonl",
            latency_path=root / "latency.jsonl",
        ),
        user_id=USER,
    )
    values.update(
        DAEMON_CORE_SRC=str(SOURCE / "src"),
        DAEMON_CORE_MEMORY_STRUCTURE="v1",
        DAEMON_CORE_OBSIDIAN_FORMAT="1",
        DAEMON_CORE_TEMPORAL_CURATOR="on",
        DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS=USER,
        DAEMON_CORE_RUNTIME_REVISION=revision,
        DAEMON_CORE_AFTER_TURN_SERVICE_REVISION=revision,
        DAEMON_CORE_MEMORY_RELOCATION="v1",
        DAEMON_CORE_MEMORY_SITUATIONS="canary",
        DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS=USER,
        DAEMON_CORE_SCENE_MODEL_SHADOW_PATH=str(root / "scene-shadow.jsonl"),
        DAEMON_CORE_OPENCLAW_AGENT_DB=str(
            root / "state/agents/main/agent/openclaw-agent.sqlite"
        ),
        HERMES_HOME=str(root / "hermes-home"),
        HERMES_CONFIG=str(root / "hermes-home/config.yaml"),
        HERMES_ENV=str(root / "empty.env"),
        CODEX_HOME=str(root / "daemon-codex"),
        CODEX_CLI_BIN=str(CODEX),
        CODEX_CLI_MODEL="gpt-6-astra",
        LLM_PROVIDER="codex_cli",
        DAEMONS_QA_HERMES_ROOT=str(HERMES),
        PRODUCTIVITY_BENCH_TRACE_FILE=str(root / "hermes-trace.jsonl"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join([str(SOURCE), str(SOURCE / "src"), str(HERMES)]),
    )
    for key, value in values.items():
        if (
            key.startswith("DAEMON_CORE_")
            and key != "DAEMON_CORE_SRC"
            and value.startswith("/")
        ):
            assert Path(value).resolve().is_relative_to(root), key
    values.update(profile_environment(USER, memory))
    env = {**host, **values}
    if not resume:
        he._prepare_profile(
            source_home=Path.home() / ".hermes",
            profile=root / "hermes-home",
            arm="C",
            model="gpt-6-astra",
            provider="openai-codex",
            daemon_values=values,
        )
    else:
        from benchmarks.productivity_v1.runner.common import write_dotenv

        write_dotenv(root / "hermes-home/.env", values)
        shutil.copy2(Path.home() / ".hermes/auth.json", root / "hermes-home/auth.json")
        (root / "hermes-home/auth.json").chmod(0o600)
    if transport:
        transport.configure_hermes(root / "hermes-home", env)
    services, logs = [], []
    report = {
        "source_revision": revision,
        "arm": arm, "starting_host": first_host,
        "model": "gpt-6-astra",
        "effort": "medium",
        "turns": [],
        "passed": False,
        "remote_telegram_messages": 0,
        "native_scheduler": bool(telegram_adapter),
        "desktop_workflow": False,
        "native_telegram_adapter": telegram_adapter,
        "remote_telegram": False,
        "shared_memory": str(memory),
    }

    if resume:
        previous = json.loads((root / "result.json").read_text())
        if previous.get("arm") != arm or previous.get("starting_host") != first_host:
            raise ValueError("Resume requires the same arm and host")
        if len(previous["turns"]) != 1 or previous["turns"][0]["checks"].get("one_l0_pair_per_turn") is not False:
            raise ValueError("Only the greeting-count harness incident can be resumed")
        snapshot = root / "snapshot-1"
        for path in vault.rglob("*"):
            if path.is_file() and (path.suffix == ".md" or path.name in {"daemon_identity.json", "first_meeting.json"}):
                if path.read_bytes() != (snapshot / path.relative_to(vault)).read_bytes():
                    raise ValueError("Memory changed after the completed cycle")
        counts = exact_user_counts(memory, PROMPTS[:1])
        if counts != [1]:
            raise ValueError("The original user turn is not unique")
        shutil.copy2(root / "result.json", root / "greeting-count-incident.json")
        report["turns"] = previous["turns"]
        report["turns"][0]["checks"]["one_l0_pair_per_turn"] = True
        report["turns"][0]["l0_prompt_counts"] = counts
        report["initial_source_revision"] = previous["source_revision"]
        report["resumed_after_checker_error"] = True
        if not all(report["turns"][0]["checks"].values()):
            raise ValueError("The first turn has another failed gate")

    def start(command, log, environment, cwd):
        handle = (root / log).open("w")
        logs.append(handle)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        services.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def prepare_auth(target):
        if resume:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path.home() / ".codex/auth.json", target / "auth.json")
            (target / "auth.json").chmod(0o600)
        else:
            prepare_codex_cli_home(target)

    try:
        if transport:
            transport.start()
        oc._validate_config(
            binary=binary,
            workspace=root / "workspace",
            env=host,
            sandbox_profile=sandbox,
        )
        prepare_auth(root / "daemon-codex")
        codex_home = root / "state/agents/main/agent/codex-home"
        prepare_auth(codex_home)
        report["auth_import"] = oc._import_codex_auth(
            binary=binary,
            codex_home=codex_home,
            workspace=root / "workspace",
            env=host,
            sandbox_profile=sandbox,
        )
        api = start(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "server.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            "api.log",
            env,
            SOURCE,
        )
        oc._wait_for_health(url, api, root / "api.log")
        service = start(
            [str(PYTHON), str(Path(__file__).resolve()), "--worker"],
            "worker.log",
            env,
            SOURCE,
        )
        wait_for_after_turn_service(
            process=service,
            spool_root=root / "after-turn-spool",
            log_path=root / "worker.log",
        )
        gateway = None
        sequence = (first_host,) * 3 if transport else (first_host, first_host, "openclaw" if first_host == "hermes" else "hermes")
        for turn, host_name in enumerate(sequence, 1):
            if resume and turn == 1:
                continue
            if turn == 3:
                stop(service)
                stop(api)
                api = start([str(PYTHON), "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1", "--port", str(api_port), "--log-level", "warning"], "api-restarted.log", env, SOURCE)
                oc._wait_for_health(url, api, root / "api-restarted.log")
                service = start([str(PYTHON), str(Path(__file__).resolve()), "--worker"], "worker-restarted.log", env, SOURCE)
                wait_for_after_turn_service(process=service, spool_root=root / "after-turn-spool", log_path=root / "worker-restarted.log")
            print(
                json.dumps({"turn": turn, "host": host_name, "stage": "voice"}),
                flush=True,
            )
            began = time.monotonic()
            if transport:
                if host_name == "hermes":
                    command = [str(HERMES / ".venv/bin/python"), "-m", "gateway.run"]
                    gateway = start(command, f"telegram-gateway-{turn}.log", env, HERMES)
                else:
                    gateway = start(oc._host_command(sandbox_profile=sandbox, binary=binary,
                        arguments=["gateway", "run", "--port", str(gateway_port), "--bind", "loopback", "--auth", "none"]),
                        f"telegram-gateway-{turn}.log", host, root / "workspace")
                transport.wait_ready(gateway)
                if turn == 1 and host_name == "hermes":
                    transport.hermes_start(gateway, root / f"telegram-gateway-{turn}.log")
                prompt = PROMPTS[turn - 1]
                if turn == 1 and host_name == "openclaw":
                    prompt = "/start"
                    PROMPTS[0] = prompt
                answer, timing = transport.exchange(prompt, memory, gateway)
                calls = None  # Gateway tool calls are retained in native traces, not guessed.
                save(root / f"voice-{turn}.json", {"final_response": answer, "transport": "loopback_bot_api"})
            elif host_name == "hermes":
                child = start(
                    [
                        str(HERMES / ".venv/bin/python"),
                        str(Path(__file__).resolve()),
                        "--hermes-turn",
                        str(turn),
                        "--output",
                        str(root),
                    ],
                    f"hermes-{turn}.log",
                    env,
                    root / "workspace",
                )
                if child.wait(timeout=160):
                    raise RuntimeError(
                        f"Hermes turn {turn} failed; see its isolated log"
                    )
                response = json.loads((root / f"voice-{turn}.json").read_text())
                answer = str(
                    response.get("final_response", response.get("response", ""))
                )
                calls = [
                    c.get("function", {}).get("name")
                    for m in response.get("messages", [])
                    for c in m.get("tool_calls", [])
                ]
                timing = json.loads((root / f"timing-{turn}.json").read_text())
            else:
                gateway = start(
                    oc._host_command(
                        sandbox_profile=sandbox,
                        binary=binary,
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
                    "gateway.log",
                    host,
                    root / "workspace",
                )
                oc._wait_for_gateway(
                    process=gateway,
                    log_path=root / "gateway.log",
                    binary=binary,
                    workspace=root / "workspace",
                    env=host,
                    sandbox_profile=sandbox,
                    port=gateway_port,
                )
                deadline = time.monotonic() + 45
                while True:
                    if gateway.poll() is not None:
                        raise RuntimeError(
                            "Isolated gateway exited before accepting connections"
                        )
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", gateway_port), timeout=1
                        ):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "Gateway health command returned success but its exact port never accepted connections"
                            )
                        time.sleep(0.25)
                began = time.monotonic()
                child = start(
                    oc._host_command(
                        sandbox_profile=sandbox,
                        binary=binary,
                        arguments=[
                            "agent",
                            "--session-key",
                            "agent:main:explicit:first-meeting",
                            "--json",
                            "--thinking",
                            "medium",
                            "--timeout",
                            "120",
                            "--message",
                            PROMPTS[turn - 1],
                        ],
                    ),
                    "openclaw-response.log",
                    host,
                    root / "workspace",
                )
                if child.wait(timeout=150):
                    raise RuntimeError("Native OpenClaw failed; no quality reroll")
                envelope = json.loads((root / "openclaw-response.log").read_text())
                save(root / f"voice-{turn}.json", envelope)
                answer = "\n".join(
                    x.get("text", "")
                    for x in envelope.get("result", envelope).get("payloads", [])
                )
                calls = [
                    x.get("tool_name")
                    for x in read_jsonl(root / "openclaw-trace.jsonl")
                    if x.get("event") == "tool_completed"
                ]
                timing = {
                    "voice_seconds": time.monotonic() - began,
                    "first_delta_seconds": None,
                }
            item = {
                "turn": turn,
                "host": host_name,
                "answer": answer,
                "tools": calls,
                **timing,
            }
            report["turns"].append(item)
            print(json.dumps({"turn": turn, "stage": "after_turn"}), flush=True)
            began = time.monotonic()
            lifecycle = wait_for_durable_after_turn(
                process=service,
                spool_root=root / "after-turn-spool",
                receipt_path=root / "continuity-receipts/continuity_receipts.jsonl",
                telemetry_path=root / "telemetry.jsonl",
                host=host_name,
                user_id=USER,
                expected_turns=turn,
                timeout_seconds=300,
            )
            item.update(
                lifecycle=lifecycle,
                after_turn_seconds=time.monotonic() - began,
                l0_prompt_counts=exact_user_counts(memory, PROMPTS[:turn]),
            )
            shutil.copytree(vault, root / f"snapshot-{turn}")
            state_path = memory / "users" / USER / "first_meeting.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else None
            item["meeting_state"] = state
            notes = [q for category in ("Projects", "Areas", "Resources", "Garden", "Archives") for q in (vault / category).rglob("*.md")]
            item["notes"] = [q.relative_to(vault).as_posix() for q in notes]
            checks = {
                "cycle_completed": lifecycle["complete"] and lifecycle["cycle_status_counts"].get("completed") == turn,
                "no_failed_or_pending_work": not any(lifecycle["spool_counts"].values()),
                "one_l0_pair_per_turn": item["l0_prompt_counts"] == [1] * turn,
                "identity_preserved": hashlib.sha256((memory / "core/daemon_identity.json").read_bytes()).hexdigest() == identity_hash,
                "meeting_lifecycle": (state is None if arm == "baseline" else state is not None and state["phase"] == ("active" if turn == 1 else "complete")),
            }
            if turn >= 2:
                checks["durable_project"] = any("LANTERN-583" in q.read_text() for q in notes)
            if turn == 3:
                checks["recall_after_restart" if telegram_adapter else "recall_after_restart_and_host_change"] = all(word in answer.casefold() for word in ("morgan", "lantern atlas", "lantern-583"))
            item["checks"] = checks
            save(root / "result.json", report)
            print(json.dumps({"turn": turn, "checks": checks}), flush=True)
            if not all(checks.values()):
                raise RuntimeError(f"Turn {turn} continuity gate failed; no reroll")
            if gateway:
                stop(gateway)
                gateway = None
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for process in reversed(services):
            stop(process)
        if transport:
            report["transport"] = transport.summary()
            transport.stop()
        for handle in logs:
            handle.close()
        for path in root.rglob("*"):
            if path.is_file() and (
                path.name
                in {"auth.json", "auth-profiles.json", "auth-credentials.json"}
                or path.name.startswith("openclaw-agent.sqlite")
            ):
                path.unlink()
        report["all_test_processes_stopped"] = all(
            p.poll() is not None for p in services
        )
        report["daemon_telemetry"] = read_jsonl(root / "telemetry.jsonl")
        try:
            report["openclaw_telemetry"] = oc._rollout_telemetry(
                root / "state/agents/main/agent/codex-home"
            )
        except RuntimeError as exc:
            report["openclaw_telemetry"] = {"available": False, "reason": str(exc)}
        save(root / "result.json", report)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in {"turns", "daemon_telemetry", "openclaw_telemetry"}
                }
            ),
            flush=True,
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hermes-turn", type=int, choices=(1, 2, 3))
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--first-host", choices=("hermes", "openclaw"))
    parser.add_argument("--arm", choices=("baseline", "candidate"))
    parser.add_argument("--resume-checker-incident", action="store_true")
    parser.add_argument("--telegram-adapter", action="store_true")
    args = parser.parse_args()
    sys.path[:0] = [str(SOURCE), str(SOURCE / "src")]
    if args.worker:
        worker()
    elif args.hermes_turn:
        hermes_turn(args.output, args.hermes_turn)
    else:
        raise SystemExit(main(args.output, args.first_host, args.arm, args.resume_checker_incident, args.telegram_adapter))
