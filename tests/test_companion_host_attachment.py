"""Association changes host configuration, never daemon-owned memory/state."""

import json
from pathlib import Path
import sys
import pytest
from daemons_companion.hosts.attachment import (
    RuntimeBinding,
    AttachmentLayout,
    inspect,
    apply,
    rollback,
    public_review,
)


class Controller:
    def __init__(self, fail=None):
        self.events = []
        self.fail = fail

    def snapshot(self):
        self.events.append("snapshot")
        return {"running": False}

    def quiesce(self, capture):
        self.events.append("quiesce")

    def prepare(self, *args):
        self.events.append("prepare")

    def activate(self):
        self.events.append("activate")

    def verify(self, *args):
        self.events.append("verify")
        if self.fail:
            raise RuntimeError("injected activation failure")

    def stop(self):
        self.events.append("stop")

    def restore(self, capture):
        self.events.append("restore")


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    source = root / "source"
    memory = root / "memory/.daemon"
    state = root / "state"
    profile = root / "host"
    records = root / "records"
    for p in (source, memory / "core", state, profile, records):
        p.mkdir(parents=True)
    for name, content in {
        "integrations/openclaw_plugin/openclaw.plugin.json": "{}",
        "integrations/openclaw_plugin/index.js": "// synthetic plugin",
        "integrations/openclaw_plugin/workspace/AGENTS.md": "Synthetic canonical voice",
        "src/daemon_core/canonical_voice.py": "# synthetic core",
        "src/daemon_core/host_bindings.py": "SCHEMA = 'daemon.host-execution.v1'\n",
    }.items():
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (memory / "core/daemon_identity.json").write_text(
        '{"name":"Aster","gender":"feminine"}'
    )
    (memory / "core/context.md").write_text("Synthetic source. Must remain exact.")
    (state / "maintenance-proof").write_text("One existing owner")
    (profile / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openai/synthetic"}},
                "channels": {"telegram": {"botToken": "synthetic-private-token"}},
                "tools": {"profile": "coding"},
            }
        )
    )
    (profile / ".env").write_text("OPENAI_API_KEY=synthetic-private-api\n")
    (root / "node").write_text("synthetic node")
    binding = RuntimeBinding(
        source,
        memory,
        state,
        Path(sys.executable),
        "synthetic-user",
        "com.daemonharness.worker.synthetic",
        state,
        state,
    )
    return AttachmentLayout(
        binding,
        profile,
        records / "attachment.json",
        18555,
        "2026.9.1-beta.1",
        (root / "node",),
    )


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_inspection_is_read_only_and_public_review_has_no_secrets(layout):
    before = snapshot(layout.profile.parent)
    p = inspect(layout)
    assert snapshot(layout.profile.parent) == before
    public = json.dumps(public_review(p))
    assert (
        "synthetic-private" not in public
        and "before" not in public
        and "expected" not in public
    )
    assert not public_review(p)["copies_memory"]


def test_association_and_undo_preserve_canonical_data_and_native_settings(layout):
    native = snapshot(layout.profile)
    memory = snapshot(layout.binding.memory)
    state = snapshot(layout.binding.state)
    plan = inspect(layout)
    c = Controller()
    result = apply(layout, plan["revision"], c)
    assert result["status"] == "active"
    config = json.loads((layout.profile / "openclaw.json").read_text())
    assert config["agents"]["defaults"]["model"] == "openai/synthetic"
    assert config["channels"]["telegram"]["botToken"] == "synthetic-private-token"
    assert config["tools"]["profile"] == "coding"
    assert (
        config["plugins"]["entries"]["daemon-harness"]["config"]["userId"]
        == "synthetic-user"
    )
    assert c.events == ["snapshot", "quiesce", "prepare", "activate", "verify"]
    assert snapshot(layout.binding.memory) == memory
    assert all(snapshot(layout.binding.state)[k] == v for k, v in state.items())
    # Simulate app restart with a fresh controller and durable record.
    rollback(layout, plan["revision"], Controller())
    assert snapshot(layout.profile) == native
    assert (
        snapshot(layout.binding.memory) == memory
        and snapshot(layout.binding.state) == state
    )
    assert json.loads(layout.record.read_text())["status"] == "rolled_back"


@pytest.mark.parametrize(
    "boundary", ["config", "environment", "agents", "host-binding", "activation"]
)
def test_failure_at_every_mutation_restores_original_profile(layout, boundary):
    before = snapshot(layout.profile)
    plan = inspect(layout)

    def fail(name):
        if name == boundary:
            raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError):
        apply(
            layout,
            plan["revision"],
            Controller(fail=boundary == "activation"),
            mutation_hook=fail,
        )
    assert snapshot(layout.profile) == before
    record = json.loads(layout.record.read_text())
    assert record["status"] == "rolled_back"
    assert record["failure"]["error_type"] == "RuntimeError"
    assert record["failure"]["frames"]
    assert "injected" not in json.dumps(record["failure"])
    assert str(layout.profile) not in json.dumps(record["failure"])


def test_conflicting_edit_survives_failed_activation(layout):
    plan = inspect(layout)

    def mutate(name):
        if name == "agents":
            (layout.profile / "openclaw.json").write_text('{"user_change":true}')
            raise RuntimeError("fail")

    with pytest.raises(RuntimeError, match="target changed"):
        apply(layout, plan["revision"], Controller(), mutation_hook=mutate)
    assert json.loads((layout.profile / "openclaw.json").read_text()) == {
        "user_change": True
    }
    assert json.loads(layout.record.read_text())["status"] == "recovery_required"


def test_stale_confirmation_does_not_mutate_or_stop_host(layout):
    plan = inspect(layout)
    (layout.profile / ".env").write_text("OPENAI_API_KEY=changed\n")
    c = Controller()
    with pytest.raises(ValueError, match="changed"):
        apply(layout, plan["revision"], c)
    assert c.events == [] and not layout.record.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "other-daemon",
        "other-engine",
        "env-binding",
        "occupied-workspace",
        "linked-config",
        "incompatible",
    ],
)
def test_unsafe_destinations_are_rejected_before_service_action(layout, kind):
    if kind == "other-daemon":
        c = json.loads((layout.profile / "openclaw.json").read_text())
        c["plugins"] = {"entries": {"daemon-harness": {"enabled": False}}}
        (layout.profile / "openclaw.json").write_text(json.dumps(c))
    elif kind == "other-engine":
        c = json.loads((layout.profile / "openclaw.json").read_text())
        c["plugins"] = {"slots": {"contextEngine": "another"}}
        (layout.profile / "openclaw.json").write_text(json.dumps(c))
    elif kind == "env-binding":
        (layout.profile / ".env").write_text("DAEMON_CORE_MEMORY_ROOT=/existing\n")
    elif kind == "occupied-workspace":
        p = layout.profile / "workspace-daemon-harness"
        p.mkdir()
        (p / "USER.md").write_text("Keep this")
    elif kind == "linked-config":
        p = layout.profile / "openclaw.json"
        p.rename(layout.profile / "original")
        p.symlink_to(layout.profile / "original")
    else:
        from dataclasses import replace

        layout = replace(layout, host_version="2026.10.99")
    with pytest.raises(ValueError):
        inspect(layout)
    assert not layout.record.exists()


def test_old_runtime_cannot_silently_lose_followup_routing(layout):
    p = layout.binding.source / "src/daemon_core/host_bindings.py"
    p.write_text("SCHEMA = 'old-version'\n")
    with pytest.raises(ValueError, match="upgraded"):
        inspect(layout)
    assert not layout.record.exists()


def test_tampered_recovery_snapshot_refused(layout):
    p = inspect(layout)
    apply(layout, p["revision"], Controller())
    data = json.loads(layout.record.read_text())
    data["before"]["config"] = "e30="
    layout.record.write_text(json.dumps(data))
    c = Controller()
    with pytest.raises(ValueError, match="integrity"):
        rollback(layout, p["revision"], c)
    assert c.events == []


def test_public_review_exposes_vault_instead_of_hidden_runtime(layout):
    plan = inspect(layout)
    plan["binding"]["memory"] = str(layout.binding.memory / ".daemon")
    assert public_review(plan)["memory"] == str(layout.binding.memory)
    assert plan["binding"]["memory"].endswith("/.daemon")
