#!/usr/bin/env python3
"""Stage, switch and restore the explicitly authorized Hermes Telegram canary.

No Telegram messages are sent by this controller. Runtime data stays private.
The original Hermes profile, production worker and OpenClaw are never edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
WORKER_LABEL = "com.daemonharness.memory-structure-worker"
GATEWAY_LABEL = "ai.hermes.gateway"
PROFILE_FILES = {".env", "config.yaml", "auth.json", "SOUL.md", "install_id",
                 "channel_directory.json", "context_length_cache.yaml",
                 "provider_models_cache.json", ".skills_prompt_snapshot.json"}
PROFILE_DIRS = {"skills", "hooks", "plugins", "memories", "cron", "platforms",
                "pairing", "state", "sessions", "pending_messages", "kanban", "gmail_oauth"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def durable(path):
    path = path.resolve()
    if any(path.is_relative_to(Path(p)) for p in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/folders")):
        raise ValueError("Persistent canary requires a durable directory")
    return path


def profile_location(root, requested=None):
    # Hermes binds a Unix socket below HERMES_HOME/state. Keep the physical
    # profile path short; the user-facing memory vault may remain in Documents.
    target = durable(requested) if requested else durable(
        Path.home() / ".daemon-canaries" / hashlib.sha256(str(root).encode()).hexdigest()[:8])
    socket_path = target / "state/gateway.loop-tick.9999999999.sock"
    if len(str(socket_path).encode()) >= 104:
        raise ValueError("Choose a shorter durable Hermes profile path for its Unix socket")
    return target


def voice_capture_file(root):
    """Keep the one-shot request inside the capture writer's private boundary."""
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return REPO / "exports/private/memory-structure-canary" / key / "first-request.json"


def checked_root(path):
    root = durable(path)
    manifest = json.loads((root / "manifest.json").read_text())
    if not manifest.get("snapshot_complete") or manifest.get("root") != str(root):
        raise ValueError("Complete recognized memory snapshot required")
    marker = json.loads((root / "memory/.memory-structure-prototype.json").read_text())
    if marker != {"schema": 1, "memory_root": str(root / "memory")}:
        raise ValueError("Wrong prototype memory marker")
    if Path(manifest["source"]).resolve() == (root / "memory").resolve():
        raise ValueError("Canary must not use original memory")
    return root


def sqlite_copy(source, target):
    source_db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        target_db = sqlite3.connect(target)
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
    finally:
        source_db.close()


def ignore_runtime_ipc(directory, names):
    """Live sockets/FIFOs are process endpoints, never durable profile data."""
    ignored = []
    for name in names:
        try:
            mode = (Path(directory) / name).lstat().st_mode
        except FileNotFoundError:
            ignored.append(name)  # The live process already removed its endpoint.
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            ignored.append(name)
    return ignored


def copy_profile(source, target):
    target.mkdir(mode=0o700)
    for path in source.iterdir():
        if path.name in PROFILE_FILES or path.name.startswith("telegram_update_receipts_"):
            shutil.copy2(path, target / path.name)
        elif path.name in PROFILE_DIRS and path.is_dir():
            shutil.copytree(path, target / path.name, symlinks=True, ignore=ignore_runtime_ipc)
        elif path.suffix == ".db" and not path.is_symlink():
            sqlite_copy(path, target / path.name)
    for name in ("logs", "plugins", "sessions", "state", "skills", "memories"):
        (target / name).mkdir(exist_ok=True)


def replace_prefix(value, source, target):
    if isinstance(value, str):
        return value.replace(str(source) + "/", str(target) + "/") if value != str(source) else str(target)
    if isinstance(value, list):
        return [replace_prefix(v, source, target) for v in value]
    if isinstance(value, dict):
        return {k: replace_prefix(v, source, target) for k, v in value.items()}
    return value


def prepare_persistent_codex_home(target):
    """Long-lived services need their own login, never a copied refresh token."""
    target.mkdir(mode=0o700)
    config = target / "config.toml"
    config.write_text('cli_auth_credentials_store = "file"\n')
    config.chmod(0o600)


def require_codex_login(runtime):
    from dotenv import dotenv_values

    values = dotenv_values(runtime / "canary.env")
    home = Path(values["CODEX_HOME"])
    if not (home / "auth.json").is_file():
        raise RuntimeError("Sign in with codex login using the canary CODEX_HOME before activation")
    result = subprocess.run(
        [values.get("CODEX_CLI_BIN") or "codex", "login", "status"],
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise RuntimeError("The canary Codex login is unavailable")


def prepare(args):
    from dotenv import dotenv_values
    from benchmarks.productivity_v1.runner.common import (
        isolated_daemon_env, source_revision, write_dotenv,
    )
    root = checked_root(args.root)
    runtime = root / "canary"
    if runtime.exists():
        raise ValueError("Canary already staged; inspect its manifest")
    revision = source_revision(REPO)
    if revision["dirty"]:
        raise ValueError("Commit candidate before staging canary")
    source = args.hermes_home.resolve()
    gateway_path = args.launchagents / (GATEWAY_LABEL + ".plist")
    original_gateway = plistlib.loads(gateway_path.read_bytes())
    if original_gateway.get("Label") != GATEWAY_LABEL or Path(original_gateway.get("EnvironmentVariables", {}).get("HERMES_HOME", "")).resolve() != source:
        raise ValueError("Gateway definition does not match the authorized Hermes profile")
    worker_path = args.launchagents / (WORKER_LABEL + ".plist")
    if worker_path.exists():
        raise ValueError("Canary worker label already exists")
    runtime.mkdir(mode=0o700)
    (runtime / "logs").mkdir()
    profile = profile_location(root, args.profile_home)
    profile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    copy_profile(source, profile)
    shutil.copy2(gateway_path, runtime / "original-gateway.plist")
    for name, target in (("daemon_core", REPO / "integrations/hermes_plugin"),
                         ("daemon_voice", REPO / "integrations/hermes_voice_plugin")):
        link = profile / "plugins" / name
        if link.exists() and not link.is_symlink():
            raise ValueError("Expected linked daemon plugin in copied profile")
        link.unlink(missing_ok=True)
        link.symlink_to(target, target_is_directory=True)
    values = {k: v for k, v in dotenv_values(source / ".env").items() if v is not None}
    user_id = values.get("DAEMON_CORE_USER_ID", "")
    if not user_id:
        raise ValueError("Explicit Hermes user identity required")
    isolated = isolated_daemon_env(values, run_root=runtime, memory_root=root / "memory",
        user_id=user_id, platform="telegram", telemetry_path=runtime / "llm.jsonl",
        latency_path=runtime / "latency.jsonl")
    # Preserve behavior settings. Only mutable destinations are redirected.
    for key in ("DAEMON_CORE_MEMORY_ROOT", "DAEMON_CORE_STATE_ROOT", "DAEMON_CORE_LOG_DIR",
                "DAEMON_CORE_ATLAS_EMBEDDING_CACHE", "DAEMON_CORE_AFTER_TURN_SPOOL_DIR",
                "DAEMON_CORE_CONTINUITY_RECEIPT_DIR", "DAEMON_CORE_OPENCLAW_HEALTH_FILE",
                "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_RECEIPT_DIR",
                "DAEMON_CORE_LLM_TELEMETRY_FILE", "DAEMON_CORE_LATENCY_LOG"):
        values[key] = isolated[key]
    values.update({"DAEMON_CORE_SRC": str(REPO / "src"), "DAEMON_CORE_ENV_FILE": str(runtime / "canary.env"),
        "DAEMON_CORE_MEMORY_STRUCTURE": "v1", "DAEMON_CORE_OBSIDIAN_FORMAT": "1",
        "DAEMON_CORE_AFTER_TURN_OWNER": "external", "DAEMON_CORE_AFTER_TURN_SPOOL": "on",
        "DAEMON_CORE_AFTER_TURN_USER_IDS": user_id, "DAEMON_CORE_RUNTIME_REVISION": revision["commit"],
        "DAEMON_CORE_VOICE_CAPTURE_FILE": str(voice_capture_file(root)),
        "DAEMON_CORE_SCENE_MODEL_SHADOW_PATH": str(runtime / "scene-shadow.jsonl"),
        "CODEX_HOME": str(runtime / "codex-home"), "HERMES_HOME": str(profile),
        "HERMES_CONFIG": str(profile / "config.yaml"), "HERMES_ENV": str(profile / ".env"),
        "PYTHONDONTWRITEBYTECODE": "1"})
    prepare_persistent_codex_home(runtime / "codex-home")
    write_dotenv(profile / ".env", values)
    write_dotenv(runtime / "canary.env", values)
    gateway = replace_prefix(original_gateway, source, profile)
    gateway.setdefault("EnvironmentVariables", {}).update({"HERMES_HOME": str(profile),
        "HERMES_CONFIG": str(profile / "config.yaml"), "HERMES_ENV": str(profile / ".env"),
        "PYTHONDONTWRITEBYTECODE": "1"})
    worker = plistlib.loads((args.launchagents / "com.daemonharness.after-turn-worker.plist").read_bytes())
    worker.update({"Label": WORKER_LABEL, "WorkingDirectory": str(REPO),
        "StandardOutPath": str(runtime / "logs/worker.log"),
        "StandardErrorPath": str(runtime / "logs/worker.log")})
    worker["EnvironmentVariables"] = {k: v for k, v in values.items()
        if k.startswith(("DAEMON_CORE_", "CODEX_", "HERMES_", "PYTHON"))}
    worker["EnvironmentVariables"]["PYTHONPATH"] = ":".join((str(REPO), str(REPO / "src"), str(args.hermes_source.resolve())))
    # Secrets stay in the private env/profile, not the LaunchAgent definition.
    worker["EnvironmentVariables"] = {k: v for k, v in worker["EnvironmentVariables"].items()
                                        if not any(s in k for s in ("TOKEN", "SECRET", "API_KEY"))}
    for name, payload in (("candidate-gateway.plist", gateway), ("candidate-worker.plist", worker)):
        (runtime / name).write_bytes(plistlib.dumps(payload))
    record = {"schema": 1, "revision": revision, "repo": str(REPO), "source_profile": str(source),
        "profile": str(profile), "gateway_path": str(gateway_path), "worker_path": str(worker_path),
        "original_gateway_sha256": digest(gateway_path),
        "candidate_gateway_sha256": digest(runtime / "candidate-gateway.plist"),
        "candidate_worker_sha256": digest(runtime / "candidate-worker.plist"), "status": "prepared"}
    write_json(runtime / "deployment.json", record)
    print(json.dumps({"status": "prepared", "revision": revision["commit"],
                      "codex_login_required": True, "codex_home": str(runtime / "codex-home")}))


def launchctl(*args, check=True):
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError("launchctl " + args[0] + " failed, exit " + str(result.returncode))
    return result


def stop_job(label):
    target = f"gui/{os.getuid()}/{label}"
    launchctl("bootout", target, check=False)
    # bootout acknowledges the request before launchd finishes asynchronous
    # teardown. Wait for absence, never start another consumer merely on exit 0.
    deadline = time.monotonic() + 15
    while launchctl("print", target, check=False).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError("Job still loaded; refuse a second consumer")
        time.sleep(0.2)


def gateway_ready(profile):
    try:
        state = json.loads((profile / "gateway_state.json").read_text())
        os.kill(int(state["pid"]), 0)
        return state.get("gateway_state") == "running" and state.get("platforms", {}).get("telegram", {}).get("state") == "connected"
    except (OSError, KeyError, ValueError):
        return False


def worker_ready(runtime):
    payload = plistlib.loads((runtime / "candidate-worker.plist").read_bytes())
    env = {**os.environ, **payload["EnvironmentVariables"]}
    result = subprocess.run(payload["ProgramArguments"] + ["--check"], cwd=payload["WorkingDirectory"],
                            env=env, capture_output=True, text=True, timeout=20)
    return result.returncode == 0


def atomic_copy(source, target):
    temporary = target.with_name(target.name + ".memory-structure-new")
    if temporary.exists():
        raise ValueError("Interrupted definition publication requires inspection")
    temporary.write_bytes(source.read_bytes())
    temporary.chmod(0o600)
    temporary.replace(target)


def restore(root, record):
    # OpenClaw and Hermes can now share the same P1 worker.
    openclaw = root / "canary/openclaw/deployment.json"
    if openclaw.exists() and json.loads(openclaw.read_text()).get("status") in {"switching", "active"}:
        raise ValueError("Restore the OpenClaw canary before stopping the shared P1 worker")
    runtime = root / "canary"
    domain = f"gui/{os.getuid()}"
    gateway = Path(record["gateway_path"])
    worker = Path(record["worker_path"])
    if gateway.exists() and digest(gateway) not in {record["original_gateway_sha256"], record["candidate_gateway_sha256"]}:
        raise ValueError("Gateway changed externally; refuse rollback overwrite")
    if worker.exists() and digest(worker) != record["candidate_worker_sha256"]:
        raise ValueError("Worker changed externally; refuse rollback overwrite")
    stop_job(GATEWAY_LABEL)
    stop_job(WORKER_LABEL)
    atomic_copy(runtime / "original-gateway.plist", gateway)
    if worker.exists():
        worker.unlink()
    launchctl("bootstrap", domain, str(gateway))
    record["status"] = "rolled_back"
    write_json(runtime / "deployment.json", record)


def verify_source_snapshot(root):
    """A canary fork must not silently omit dialogue arriving since the copy."""
    manifest = json.loads((root / "manifest.json").read_text())
    source = Path(manifest["source"])
    expected = {n: h for n, h in manifest["source_hashes"].items() if n.endswith(".md")}
    actual = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*.md")}
    if actual != expected:
        raise ValueError("Original memory changed since snapshot; refresh the private copy before switching")


def activate(root, record):
    runtime = root / "canary"
    require_codex_login(runtime)
    gateway = Path(record["gateway_path"])
    worker = Path(record["worker_path"])
    if record["status"] != "prepared" or digest(gateway) != record["original_gateway_sha256"] or worker.exists():
        raise ValueError("Prepared deployment no longer matches original services")
    qualification = json.loads((root / "qualification.json").read_text())
    if qualification != {"candidate_commit": record["revision"]["commit"],
                         "isolated_lifecycle_passed": True, "context_page_writes_passed": True,
                         "content_integrity_passed": True}:
        raise ValueError("Candidate qualification is incomplete or belongs to another revision")
    if digest(runtime / "candidate-gateway.plist") != record["candidate_gateway_sha256"] or digest(runtime / "candidate-worker.plist") != record["candidate_worker_sha256"]:
        raise ValueError("Prepared candidate definitions changed")
    verify_source_snapshot(root)
    state = json.loads((Path(record["source_profile"]) / "gateway_state.json").read_text())
    if state.get("active_agents") or state.get("active_work"):
        raise RuntimeError("Hermes is handling a turn; wait before switching")
    domain = f"gui/{os.getuid()}"
    record["status"] = "switching"
    write_json(runtime / "deployment.json", record)
    try:
        stop_job(GATEWAY_LABEL)
        # Refresh exact host history and polling receipts only after the old
        # gateway stops. The source profile is never written by the controller.
        source, profile = Path(record["source_profile"]), Path(record["profile"])
        for path in source.glob("*.db"):
            sqlite_copy(path, profile / path.name)
        for path in source.glob("telegram_update_receipts_*.json"):
            shutil.copy2(path, profile / path.name)
        atomic_copy(runtime / "candidate-worker.plist", worker)
        launchctl("bootstrap", domain, str(worker))
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline and not worker_ready(runtime):
            time.sleep(1)
        if not worker_ready(runtime):
            raise RuntimeError("Candidate worker did not become ready")
        atomic_copy(runtime / "candidate-gateway.plist", gateway)
        launchctl("bootstrap", domain, str(gateway))
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline and not gateway_ready(profile):
            time.sleep(1)
        if not gateway_ready(profile):
            raise RuntimeError("Candidate Telegram gateway did not become connected")
        record["status"] = "active"
        write_json(runtime / "deployment.json", record)
    except BaseException:
        restore(root, record)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "activate", "rollback", "status"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--profile-home", type=Path)
    parser.add_argument("--launchagents", type=Path, default=Path.home() / "Library/LaunchAgents")
    args = parser.parse_args()
    os.umask(0o077)
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    if args.stage == "prepare":
        if not args.hermes_source:
            parser.error("prepare requires --hermes-source")
        prepare(args)
        return
    root = checked_root(args.root)
    record = json.loads((root / "canary/deployment.json").read_text())
    if args.stage == "activate":
        activate(root, record)
    elif args.stage == "rollback":
        restore(root, record)
    print(json.dumps({"status": record["status"], "revision": record["revision"]["commit"],
        "gateway_connected": gateway_ready(Path(record["profile"])),
        "worker_ready": worker_ready(root / "canary") if record["status"] == "active" else False}))


if __name__ == "__main__":
    main()
