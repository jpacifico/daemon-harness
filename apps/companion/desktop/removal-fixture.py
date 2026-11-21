from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import shutil
import uuid


from daemons_companion.removal.removal import Removal
from daemons_companion.runtime.acquisition import _RELEASES
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.hermes.profile import (
    HermesProfileAdapter,
    HermesProfileLayout,
    HERMES_PROFILE_STEP,
)
from daemons_companion.setup.plan import HostKind, PlanStep
from daemons_companion.runtime.services import (
    BackgroundJob,
    render_job_definition,
)


class Scheduler:
    def __init__(self):
        self.items = {}

    def capture(self, code):
        return self.items.get(code)

    def restore(self, code, value):
        self.items[code] = value


def fixture(root, *, scheduler=None, namespace=None, plugin_copies=None):
    root = root.resolve()
    base = root / "DaemonHarness"
    attempt = (
        base
        / "Companion/instance-ABC123/hosts/hermes/installations"
        / (namespace or uuid.uuid4().hex)
    )
    profile = attempt / "profile"
    profile.mkdir(parents=True)
    host = {
        "schema": "daemon.companion-host-install.v1",
        "installation_id": attempt.name,
        "version": _RELEASES["hermes"].version,
        "revision": _RELEASES["hermes"].revision,
    }
    (attempt / "owner.json").write_text(json.dumps(host))
    (attempt.parent.parent / "current.json").write_text(json.dumps(host))
    scope = InstallationScope(attempt)
    scope.initialize()
    memory = scope.home / "data/memory"
    (memory / ".daemon/core").mkdir(parents=True)
    (memory / ".daemon/core/daemon_identity.json").write_text(
        json.dumps({"name": "Nova"})
    )
    (memory / "Projects").mkdir()
    (memory / "Projects/synthetic.md").write_text(
        "# Synthetic note\nKeep every byte.\n"
    )
    (memory / ".obsidian").mkdir()
    (memory / ".obsidian/preferences.json").write_text('{"test":true}\n')
    release = scope.home / "data/runtime/releases" / ("f" * 64)
    for rel in (
        "src/daemon_core/__init__.py",
        "src/daemon_core/loop.py",
        "integrations/hermes_plugin/__init__.py",
        "integrations/hermes_voice_plugin/__init__.py",
    ):
        p = release / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# synthetic\n")
    (profile / "config.yaml").write_text(
        "# Keep this comment\nmodel: native\ncustom: 42\n"
    )
    (profile / ".env").write_text("TELEGRAM_BOT_TOKEN='synthetic-never-return'\n")
    (profile / "state.db").write_bytes(b"synthetic history")
    tx = scope.home / "transactions" / uuid.uuid4().hex
    snapshot = tx / "snapshots/hermes-profile"
    snapshot.mkdir(parents=True)
    (tx / "journal.json").write_text(json.dumps({"status": "committed"}))
    (attempt / "daemon-setup.json").write_text(
        json.dumps(
            {
                "schema": "daemon.private-setup.v1",
                "transaction_id": tx.name,
                "username": "synthetic_bot",
                "user_id": "12345",
            }
        )
    )
    adapter = HermesProfileAdapter(
        HermesProfileLayout(
            profile,
            release,
            scope,
            plugin_copies=os.name == "nt" if plugin_copies is None else plugin_copies,
        ),
        selected_host=HostKind.HERMES,
        user_id="12345",
        runtime_environment={"DAEMON_CORE_MEMORY_ROOT": str(memory / ".daemon")},
    )
    step = PlanStep(HERMES_PROFILE_STEP, "synthetic", ())
    adapter.snapshot(step, snapshot)
    adapter.apply(step)
    # Hermes enables plugins inside the existing `plugins` mapping, which the
    # profile already holds for its load deadline.
    config = (profile / "config.yaml").read_text()
    listed = "  enabled:\n    - daemon_core\n    - daemon_voice\n    - unrelated\n  disabled: []\n"
    assert config.count("\nplugins:\n") == 1
    (profile / "config.yaml").write_text(config.replace("\nplugins:\n", "\nplugins:\n" + listed, 1))
    source = attempt / "source"
    source.mkdir()
    executable = source / ("worker.exe" if os.name == "nt" else "worker")
    if os.name == "posix":
        shutil.copyfile("/bin/sleep", executable)
        executable.chmod(0o700)
    else:
        node = shutil.which("node")
        if not node:
            raise RuntimeError(
                "The isolated Windows test requires its bundled Node runtime"
            )
        shutil.copyfile(node, executable)
    for relative in ("python", "codex", "embeddings", "os-home"):
        folder = attempt / relative
        folder.mkdir()
        (folder / "retained-or-program").write_text(relative)
    agents = root / "home/Library/LaunchAgents"
    agents.mkdir(parents=True)
    backend = scheduler or Scheduler()
    engine = Removal(profile, base, agents, scheduler=backend)
    jobs = []
    for code in engine.codes:
        job = BackgroundJob(
            code,
            str(executable),
            ("60",) if os.name != "nt" else ("-e", "setInterval(()=>{},1000)"),
            str(attempt),
            str(attempt / (code + ".log")),
            code != engine.codes[1],
            4 if code == engine.codes[1] else None,
        )
        raw = render_job_definition(job, "windows" if os.name == "nt" else "macos")
        (agents / (code + ".plist")).write_bytes(raw)
        capture = json.dumps(
            {
                "schema": "daemon.windows-task-capture.v1"
                if os.name == "nt"
                else "daemon.launchd-capture.v1",
                ("running" if os.name == "nt" else "loaded"): True,
                "definition": base64.b64encode(raw).decode(),
            }
        ).encode()
        if scheduler is None:
            backend.items[code] = capture
        jobs.append((job, raw))
    return engine, memory, jobs


def tree(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def apply(engine, action):
    review = engine.inspect(action)
    return engine.apply(action, review["revision"], review["name"])


if __name__ == "__main__":
    import sys
    from daemons_companion.runtime.services import MacLaunchdSchedulerBackend

    if sys.argv[1] == "create":
        engine, memory, jobs = fixture(Path(sys.argv[2]))
        if os.name == "nt":
            from daemons_companion.removal.windows import WindowsRemovalScheduler

            backend = WindowsRemovalScheduler(
                job_namespace=engine.scope.installation_id
            )
        else:
            backend = MacLaunchdSchedulerBackend(
                engine.launch_agents,
                user_id=os.getuid(),
                job_namespace=engine.scope.installation_id,
            )

        try:
            for job, raw in jobs:
                backend.install(job, raw)
        except BaseException:
            for code in engine.codes:
                backend.restore(code, None)
            raise
        print(
            json.dumps(
                {
                    "profile": str(engine.profile),
                    "base": str(engine.base),
                    "home": str(engine.launch_agents.parents[1]),
                    "memory": str(memory),
                }
            )
        )
    elif sys.argv[1] == "stop":
        values = json.loads(Path(sys.argv[2]).read_text())
        engine = Removal(
            values["profile"],
            values["base"],
            Path(values["home"]) / "Library/LaunchAgents",
        )
        for code in engine.codes:
            engine.scheduler.restore(code, None)
            assert engine.scheduler.capture(code) is None
        print(json.dumps({"stopped": True}))
