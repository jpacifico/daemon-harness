import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_repository_agent_contract_preserves_project_invariants():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "Les hôtes restent propriétaires de leurs capacités" in agents
    assert "Le daemon ne pilote pas les outils natifs" in agents
    assert "Hermes et OpenClaw restent à parité" in agents
    assert "Expériences et latence" in agents
    assert "docs/evaluations/" in agents
    assert "Frontière avec le workspace OpenClaw" in agents
    assert "!/AGENTS.md" in ignore_rules


def test_docker_context_excludes_secrets_state_and_private_memory():
    rules = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert {".env", ".env.*", ".venv", "exports", "notes", "memory/*"} <= rules
    assert {"**/__pycache__", "**/*.py[cod]"} <= rules
    assert "!.env.example" in rules
    assert {
        "docs",
        "tests",
        "evals",
        "results",
        "benchmarks",
        "integrations",
    } <= rules
    assert "scripts/*" in rules
    assert "!scripts/memory_library.py" in rules
    assert "!memory/capsules/examples/*.md" in rules
    assert "!memory/capsules/templates/*.md" in rules
    assert "!memory/templates/**" in rules


def test_docker_image_has_revision_and_readiness_contract():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.11.15-slim\n")
    assert "ARG DAEMON_CORE_RUNTIME_REVISION=unknown" in dockerfile
    assert "DAEMON_CORE_RUNTIME_REVISION=${DAEMON_CORE_RUNTIME_REVISION}" in dockerfile
    assert "http://127.0.0.1:8000/ready" in dockerfile
    assert "report.get('promotable') is True" in dockerfile


def test_docker_runbook_preserves_the_built_revision_over_dotenv():
    runbook = (ROOT / "docs" / "05_deploiement.md").read_text(encoding="utf-8")

    assert '--env-file .env' in runbook
    assert '-e DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION"' in runbook
    assert 'test -d "$DAEMON_STATE_DIR/memory/users"' in runbook
    assert "docker run --detach --restart unless-stopped" in runbook


def test_direct_dependencies_are_exactly_pinned():
    requirements = [
        line
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert requirements
    assert all("==" in requirement for requirement in requirements)
    assert all(">=" not in requirement and "~=" not in requirement for requirement in requirements)
    assert all("EventKit" not in requirement for requirement in requirements)


def test_macos_installers_use_state_outside_the_git_checkout():
    openclaw = (ROOT / "integrations" / "openclaw_plugin" / "service.sh").read_text(
        encoding="utf-8"
    )
    dreaming = (ROOT / "integrations" / "dreaming_service" / "service.sh").read_text(
        encoding="utf-8"
    )

    stable_python = "$HOME/.local/share/daemon-harness/runtime/venv/bin/python"
    stable_memory = "$HOME/.local/share/daemon-harness/memory"
    stable_logs = "$HOME/Library/Logs/daemon-harness"
    assert stable_python in openclaw
    assert stable_memory in openclaw
    assert stable_logs in openclaw
    assert stable_python in dreaming
    assert stable_memory in dreaming
    assert stable_logs in dreaming


def test_both_host_installers_use_the_same_nightly_service():
    openclaw = (ROOT / "integrations" / "openclaw_plugin" / "service.sh").read_text(
        encoding="utf-8"
    )
    hermes = (ROOT / "integrations" / "hermes_plugin" / "install.sh").read_text(
        encoding="utf-8"
    )

    assert "integrations/dreaming_service/service.sh" in hermes
    assert "integrations/dreaming_service/service.sh" in openclaw


def test_host_uninstallers_do_not_remove_the_shared_nightly_service():
    openclaw = (ROOT / "integrations" / "openclaw_plugin" / "service.sh").read_text(
        encoding="utf-8"
    )
    hermes = (ROOT / "integrations" / "hermes_plugin" / "uninstall.sh").read_text(
        encoding="utf-8"
    )

    assert 'rm -f "$HOME/Library/LaunchAgents/com.daemonharness.dreaming.plist"' not in openclaw
    assert 'rm -f "$HOME/Library/LaunchAgents/com.daemonharness.dreaming.plist"' not in hermes
    assert "Dreaming/REM commun est conservé" in openclaw
    assert "conservé : il peut servir OpenClaw" in hermes


def test_openclaw_workspace_installer_activates_native_browser_idempotently():
    installer = (
        ROOT / "integrations" / "openclaw_plugin" / "workspace.sh"
    ).read_text(encoding="utf-8")

    assert "enable_native_browser_tool()" in installer
    assert 'openclaw config get plugins.allow' in installer
    assert '"daemon_read_souvenirs"' in installer
    assert '"daemon_recall_l0"' in installer
    assert '"browser"' in installer
    assert (
        "openclaw config set plugins.entries.browser.enabled true --strict-json"
        in installer
    )
    assert "openclaw config set browser.headless true --strict-json" in installer
    assert 'openclaw config get tools.alsoAllow' in installer
    assert 'openclaw config set tools.alsoAllow "$merged_also_allowed_tools" --strict-json' in installer
    assert "enable_native_browser_tool" in installer.rsplit(
        'case "$ACTION" in', maxsplit=1
    )[-1]
    assert "enable_daemon_conversation_hooks()" in installer
    assert (
        "plugins.entries.daemon-harness.hooks.allowConversationAccess"
        in installer
    )


def test_openclaw_workspace_installer_enforces_one_personal_memory_authority():
    installer = (
        ROOT / "integrations" / "openclaw_plugin" / "workspace.sh"
    ).read_text(encoding="utf-8")

    assert "disable_parallel_personal_memory()" in installer
    assert '"path":"plugins.entries.memory-core.enabled","value":false' in installer
    assert '"path":"skills.workshop.autonomous.mode","value":"off"' in installer


def test_openclaw_workspace_leaves_native_tool_orchestration_to_host():
    agents = (
        ROOT / "integrations" / "openclaw_plugin" / "workspace" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    manifest = (
        ROOT / "integrations" / "openclaw_plugin" / "openclaw.plugin.json"
    ).read_text(encoding="utf-8")

    assert "## Tools" in agents
    assert "OpenClaw owns the choice, ordering and retry policy" in agents
    assert "impose a search sequence" in agents
    assert "result is not confirmation" in agents
    assert "at most one successful" not in agents
    assert "text(r)" not in agents
    assert '"browser"' not in manifest


def test_openclaw_workspace_uses_current_beta_state_contract():
    installer = (
        ROOT / "integrations" / "openclaw_plugin" / "workspace.sh"
    ).read_text(encoding="utf-8")

    assert "openclaw config set agents.defaults.skipBootstrap true --strict-json" in installer
    assert "openclaw doctor --fix --non-interactive" in installer
    assert "Migration du workspace OpenClaw incomplète" in installer
    assert "normalize_legacy_workspace_state" in installer
    assert 'value.replace(/Z$/, ".000Z")' in installer
    assert "oldInstallerMarker" in installer
    assert 'install_file "$SCRIPT_DIR/workspace/TOOLS.md"' not in installer
    assert 'openclaw-workspace-state.json" ]]; then' not in installer
    assert not (
        ROOT / "integrations" / "openclaw_plugin" / "workspace" / "TOOLS.md"
    ).exists()
    assert "!integrations/openclaw_plugin/workspace/AGENTS.md" in (
        ROOT / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()


def _run_openclaw_workspace_installer(
    tmp_path: Path,
    state: dict,
    *,
    legacy_workspace: bool = False,
    migration_stuck: bool = False,
) -> dict:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    state_path = tmp_path / "openclaw-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_openclaw = fake_bin / "openclaw"
    fake_openclaw.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_OPENCLAW_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]

def get_path(path):
    current = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current

def set_path(path, value):
    current = state
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value
    state_path.write_text(json.dumps(state), encoding="utf-8")

if args[:2] == ["config", "get"]:
    try:
        value = get_path(args[2])
    except KeyError:
        raise SystemExit(1)
    print(json.dumps(value) if isinstance(value, (dict, list, bool)) else value)
elif args[:2] == ["config", "set"]:
    if "--batch-json" in args:
        position = args.index("--batch-json")
        for operation in json.loads(args[position + 1]):
            set_path(operation["path"], operation["value"])
    else:
        raw = args[3]
        value = json.loads(raw) if "--strict-json" in args else raw
        set_path(args[2], value)
elif args[:2] == ["hooks", "disable"]:
    set_path(f"hooks.internal.entries.{args[2]}.enabled", False)
elif args == ["doctor", "--fix", "--non-interactive"]:
    workspace = Path(os.environ["OPENCLAW_DAEMON_WORKSPACE"])
    if os.environ.get("FAKE_OPENCLAW_MIGRATION_STUCK") != "1":
        legacy_state = json.loads(
            (workspace / "openclaw-workspace-state.json").read_text(encoding="utf-8")
        )
        if legacy_state != {"version": 1}:
            raise SystemExit("old installer marker was not normalized")
        (workspace / "openclaw-workspace-state.json").unlink(missing_ok=True)
        (workspace / "TOOLS.md").unlink(missing_ok=True)
else:
    raise SystemExit(f"unexpected fake openclaw command: {args}")
""",
        encoding="utf-8",
    )
    fake_openclaw.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_OPENCLAW_STATE": str(state_path),
        "OPENCLAW_DAEMON_WORKSPACE": str(tmp_path / "workspace"),
    }
    if migration_stuck:
        env["FAKE_OPENCLAW_MIGRATION_STUCK"] = "1"
    workspace = tmp_path / "workspace"
    if legacy_workspace:
        workspace.mkdir()
        (workspace / "openclaw-workspace-state.json").write_text(
            '{"version":1,"setupCompletedAt":"2026-08-29T11:17:10Z"}',
            encoding="utf-8",
        )
        (workspace / "TOOLS.md").write_text("legacy notes", encoding="utf-8")
    installer = ROOT / "integrations" / "openclaw_plugin" / "workspace.sh"
    if migration_stuck:
        result = subprocess.run(
            [str(installer), "install"],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "Migration du workspace OpenClaw incomplète" in result.stderr
        return json.loads(state_path.read_text(encoding="utf-8"))
    subprocess.run([str(installer), "install"], check=True, env=env, capture_output=True)
    subprocess.run([str(installer), "install"], check=True, env=env, capture_output=True)
    status = subprocess.run(
        [str(installer), "status"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, status.stderr or status.stdout
    assert "Contrat du workspace daemon : correct" in status.stdout
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_openclaw_workspace_installer_preserves_existing_allowlists(tmp_path):
    state = _run_openclaw_workspace_installer(
        tmp_path,
        {
            "browser": {"headless": False},
            "plugins": {"allow": ["codex", "daemon-harness"]},
            "tools": {
                "alsoAllow": ["daemon_read_souvenirs", "daemon_recall_l0"],
                "deny": ["existing_denial"],
            },
        },
    )

    assert state["plugins"]["allow"] == ["codex", "daemon-harness", "browser"]
    assert state["plugins"]["entries"]["browser"]["enabled"] is True
    assert state["agents"]["defaults"]["skipBootstrap"] is True
    assert state["plugins"]["entries"]["memory-core"]["enabled"] is False
    assert state["plugins"]["entries"]["daemon-harness"]["hooks"] == {
        "allowConversationAccess": True,
    }
    assert state["skills"]["workshop"]["autonomous"]["mode"] == "off"
    assert state["browser"]["headless"] is False
    assert state["tools"]["alsoAllow"] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
        "browser",
    ]
    assert state["tools"]["deny"] == [
        "existing_denial",
        "memory_search",
        "memory_get",
    ]


def test_openclaw_workspace_installer_keeps_an_open_plugin_policy_open(tmp_path):
    state = _run_openclaw_workspace_installer(tmp_path, {"tools": {}})

    assert "allow" not in state["plugins"]
    assert state["plugins"]["entries"]["browser"]["enabled"] is True
    assert state["agents"]["defaults"]["skipBootstrap"] is True
    assert state["plugins"]["entries"]["memory-core"]["enabled"] is False
    assert state["plugins"]["entries"]["daemon-harness"]["hooks"] == {
        "allowConversationAccess": True,
    }
    assert state["skills"]["workshop"]["autonomous"]["mode"] == "off"
    assert state["browser"]["headless"] is True
    assert state["tools"]["alsoAllow"] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
        "browser",
    ]


def test_openclaw_workspace_status_detects_memory_authority_drift(tmp_path):
    state = _run_openclaw_workspace_installer(tmp_path, {"tools": {}})
    state["plugins"]["entries"]["memory-core"]["enabled"] = True
    state_path = tmp_path / "openclaw-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE_OPENCLAW_STATE": str(state_path),
        "OPENCLAW_DAEMON_WORKSPACE": str(tmp_path / "workspace"),
    }

    status = subprocess.run(
        [str(ROOT / "integrations" / "openclaw_plugin" / "workspace.sh"), "status"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )

    assert status.returncode != 0
    assert "Contrat du workspace daemon : incorrect" in status.stdout


def test_openclaw_workspace_installer_migrates_retired_beta_files(tmp_path):
    _run_openclaw_workspace_installer(
        tmp_path,
        {"tools": {}},
        legacy_workspace=True,
    )

    workspace = tmp_path / "workspace"
    assert not (workspace / "openclaw-workspace-state.json").exists()
    assert not (workspace / "TOOLS.md").exists()
    agents = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "OpenClaw owns the choice, ordering and retry policy" in agents
    assert "impose a search sequence" in agents


def test_openclaw_workspace_installer_rejects_an_incomplete_migration(tmp_path):
    _run_openclaw_workspace_installer(
        tmp_path,
        {"tools": {}},
        legacy_workspace=True,
        migration_stuck=True,
    )

    workspace = tmp_path / "workspace"
    assert (workspace / "openclaw-workspace-state.json").exists()
