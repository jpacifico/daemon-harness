"""Uninstalling an OpenClaw daemon created by the app, on a synthetic installation."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import plistlib
import shutil
import uuid

import pytest

import daemons_companion.removal.removal as removal
from daemons_companion.removal.removal import OPENCLAW_PROGRAMS, Refused, Removal
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.services import BackgroundJob, render_job_definition

CONNECTION_REF = "provider-" + str(uuid.UUID(int=1))
PROVIDER_REF = "provider-" + str(uuid.UUID(int=2))


class Scheduler:
    def __init__(self):
        self.items = {}

    def capture(self, code):
        return self.items.get(code)

    def restore(self, code, value):
        self.items[code] = value


class Store:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, reference):
        return self.values.get(reference)

    def delete(self, reference):
        self.values.pop(reference)


class Scope(InstallationScope):
    # The real receipt verifies a downloaded OpenClaw; this one is synthetic.
    def receipt(self):
        return {"schema": "daemon.companion-installation-scope.v1", "host": {"synthetic": True}}


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(removal, "InstallationScope", Scope)
    store = Store({CONNECTION_REF: "{}", PROVIDER_REF: "{}", "provider-" + str(uuid.UUID(int=3)): "other"})
    import daemons_companion.setup.credentials as credentials

    monkeypatch.setattr(credentials, "system_credential_store", lambda: store)

    def build(prefix=("Companion", "instance-ABC123")):
        base = tmp_path.resolve() / "DaemonHarness"
        attempt = base.joinpath(*prefix, "hosts/openclaw/installations", uuid.uuid4().hex)
        profile = attempt / "profile"
        profile.mkdir(parents=True)
        scope = Scope(attempt)
        scope.initialize()
        memory = scope.home / "data/memory"
        (memory / ".daemon/core").mkdir(parents=True)
        (memory / ".daemon/core/daemon_identity.json").write_text(json.dumps({"name": "Riven"}))
        (memory / "Projects").mkdir()
        (memory / "Projects/synthetic.md").write_text("# Keep every byte\n")
        (profile / "openclaw.json").write_text('{"gateway": {"port": 18789}}\n')
        (profile / "agents").mkdir()
        (profile / "agents/sessions.json").write_text("synthetic history")
        (attempt / "openclaw-connection.json").write_text(json.dumps(
            {"schema": "daemon.openclaw-connection.v1", "credential": CONNECTION_REF, "kind": "mistral_api"}))
        config = scope.home / "data/state/config"
        config.mkdir(parents=True)
        (config / "provider.json").write_text(json.dumps(
            {"credential": {"name": "MISTRAL_API_KEY", "reference": PROVIDER_REF}}))
        for relative in OPENCLAW_PROGRAMS:
            (attempt / relative).mkdir(parents=True, exist_ok=True)
            (attempt / relative / "program").write_text(relative)
        for relative in ("os-home", "logs"):
            (attempt / relative).mkdir()
            (attempt / relative / "kept").write_text(relative)
        executable = attempt / "node/worker"
        shutil.copyfile("/bin/sleep", executable)
        executable.chmod(0o700)
        tx = scope.home / "transactions" / uuid.uuid4().hex
        tx.mkdir(parents=True)
        (tx / "journal.json").write_text(json.dumps({"status": "committed"}))
        (attempt / "daemon-setup.json").write_text(json.dumps(
            {"schema": "daemon.private-setup.v1", "transaction_id": tx.name}))
        agents = tmp_path / "home/Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        scheduler = Scheduler()
        engine = Removal(profile, base, agents, scheduler=scheduler)
        for code in engine.codes:
            job = BackgroundJob(code, str(executable), ("60",), str(attempt),
                                str(attempt / "logs" / (code + ".log")), code != engine.codes[1],
                                4 if code == engine.codes[1] else None)
            raw = render_job_definition(job, "macos")
            (agents / (code + ".plist")).write_bytes(raw)
            scheduler.items[code] = json.dumps({"schema": "daemon.launchd-capture.v1", "loaded": True,
                                                "definition": base64.b64encode(raw).decode()}).encode()
        engine.store = store
        return engine, memory

    return build


@pytest.mark.skipif(os.name == "nt", reason="launchd definitions")
def test_uninstall_removes_services_programs_and_credentials_and_keeps_memory(daemon):
    engine, memory = daemon()
    assert engine.openclaw and len(engine.codes) == 5
    kept = tree(memory), tree(engine.profile), tree(engine.attempt / "os-home"), tree(engine.attempt / "logs")
    review = engine.inspect("uninstall")
    assert review["name"] == "Riven" and set(review["services"]) == set(engine.codes)
    result = engine.apply("uninstall", review["revision"], "Riven")
    assert result["done"] and result["memory"] == str(memory)
    assert not any(engine.scheduler.capture(code) for code in engine.codes)
    assert not any((engine.attempt / p).exists() for p in OPENCLAW_PROGRAMS)
    assert (tree(memory), tree(engine.profile), tree(engine.attempt / "os-home"),
            tree(engine.attempt / "logs")) == kept
    # Only this daemon's two credentials are deleted.
    assert list(engine.store.values) == ["provider-" + str(uuid.UUID(int=3))]
    with pytest.raises(Refused, match="removed"):
        engine.inspect("uninstall")


def test_keeping_openclaw_without_the_daemon_is_not_offered(daemon):
    engine, _ = daemon()
    with pytest.raises(Refused, match="request"):
        engine.inspect("detach")


@pytest.mark.skipif(os.name == "nt", reason="launchd definitions")
def test_a_failure_while_stopping_restores_every_service(daemon):
    engine, memory = daemon()
    before = dict(engine.scheduler.items), tree(engine.attempt)

    def fail(step):
        if step == "service:" + engine.codes[2]:
            raise RuntimeError("synthetic failure")

    engine.hook = fail
    review = engine.inspect("uninstall")
    with pytest.raises(Refused, match="rolled_back"):
        engine.apply("uninstall", review["revision"], "Riven")
    assert engine.scheduler.items == before[0]
    assert {k: v for k, v in tree(engine.attempt).items() if k != removal.MARKER} == before[1]
    assert len(engine.store.values) == 3
    engine.hook = lambda _: None
    review = engine.inspect("uninstall")
    assert engine.apply("uninstall", review["revision"], "Riven")["done"]


def test_an_openclaw_added_to_a_hermes_daemon_is_refused_on_its_own(daemon):
    with pytest.raises(Refused, match="ownership"):
        daemon(prefix=("Companion", "private"))


@pytest.mark.skipif(os.name == "nt", reason="launchd definitions")
def test_another_service_using_the_memory_blocks_the_uninstall(daemon):
    engine, memory = daemon()
    (engine.launch_agents / "foreign.plist").write_bytes(
        plistlib.dumps({"Label": "foreign", "ProgramArguments": ["/bin/echo", str(memory)]}))
    before = tree(engine.attempt)
    with pytest.raises(Refused, match="shared"):
        engine.inspect("uninstall")
    assert tree(engine.attempt) == before and len(engine.store.values) == 3


@pytest.mark.skipif(os.name == "nt", reason="launchd definitions")
def test_a_redirected_program_folder_is_refused(daemon, tmp_path):
    engine, _ = daemon()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("keep")
    shutil.rmtree(engine.attempt / "npm-cache")
    (engine.attempt / "npm-cache").symlink_to(foreign)
    with pytest.raises((ValueError, OSError)):
        engine.inspect("uninstall")
    assert (foreign / "keep").read_text() == "keep"
    assert Path(foreign / "keep").exists()
