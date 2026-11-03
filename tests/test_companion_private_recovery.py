import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemons_companion.hermes.ready_composition import HERMES_READY_STEP_CODES
from daemons_companion.setup.recovery import retain_failed_python_build
from daemons_companion.setup.transaction import TRANSACTION_SCHEMA
from tests.test_companion_private_setup import owned


def recovery_fixture(tmp_path, monkeypatch):
    _, scope = owned(tmp_path, monkeypatch, "windows")
    transaction = scope.home / "transactions" / ("b" * 32)
    snapshots = transaction / "snapshots"
    journal = {
        "schema": TRANSACTION_SCHEMA,
        "transaction_id": transaction.name,
        "plan_digest": "f" * 64,
        "host": "hermes",
        "status": "rollback_failed",
        "step_codes": list(HERMES_READY_STEP_CODES),
        "snapshotted_steps": ["runtime-layout", "runtime-environment"],
        "completed_steps": ["runtime-layout"],
        "rolled_back_steps": ["runtime-layout"],
        "active_step": "",
        "active_phase": "",
        "failure_type": "RuntimeError",
        "rollback_failed_steps": ["runtime-environment"],
    }
    release = "a" * 64
    runtime = {
        "schema": "daemon.companion-runtime-snapshot.v1",
        "release_id": release,
        "before": {key: {"kind": "absent"} for key in ("current", "release", "stage")},
    }
    environment = {
        "schema": "daemon.companion-environment-snapshot.v1",
        "release_id": release,
        "target_os": "windows",
        "before": {key: {"kind": "absent"} for key in ("final", "stage")},
    }
    for name, content in [
        ("runtime-layout", runtime),
        ("runtime-environment", environment),
    ]:
        path = snapshots / name / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(content))
    path = transaction / "journal.json"
    path.write_text(json.dumps(journal))
    stage = scope.home / "data/runtime" / (".environment-" + release)
    stage.mkdir(parents=True)
    (stage / "leftover").write_bytes(b"bytes remaining after partial cleanup")
    (stage / "user-added-file").write_bytes(b"must be retained too")
    scheduler = SimpleNamespace(
        target_os="windows", job_namespace=scope.installation_id, capture=lambda _: None
    )
    return scope, path, stage, scheduler


def test_recovery_retains_every_byte_and_original_failure_and_can_resume(
    tmp_path, monkeypatch
):
    scope, journal, stage, scheduler = recovery_fixture(tmp_path, monkeypatch)
    before = journal.read_bytes()
    unrelated = scope.home / "data/memory/preserve.txt"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"exact synthetic memory")
    target = retain_failed_python_build(scope, journal, scheduler)
    assert not stage.exists()
    assert (
        target / "leftover"
    ).read_bytes() == b"bytes remaining after partial cleanup"
    assert (target / "user-added-file").read_bytes() == b"must be retained too"
    assert journal.read_bytes() == before
    assert unrelated.read_bytes() == b"exact synthetic memory"
    assert retain_failed_python_build(scope, journal, scheduler) == target
    assert json.loads(journal.read_text())["status"] == "rollback_failed"


def test_interrupted_rename_resumes_from_durable_receipt(tmp_path, monkeypatch):
    import daemons_companion.setup.recovery as module

    scope, journal, stage, scheduler = recovery_fixture(tmp_path, monkeypatch)
    original = module.os.replace

    def interrupted(source, destination):
        if Path(source) == stage:
            raise KeyboardInterrupt
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        retain_failed_python_build(scope, journal, scheduler)
    assert stage.exists() and (journal.parent / "retained-python.json").exists()
    monkeypatch.setattr(module.os, "replace", original)
    assert retain_failed_python_build(scope, journal, scheduler).is_dir()


def test_interrupted_after_rename_resumes_without_second_move(tmp_path, monkeypatch):
    import daemons_companion.setup.recovery as module

    scope, journal, stage, scheduler = recovery_fixture(tmp_path, monkeypatch)
    original = module.os.replace

    def interrupted(source, destination):
        original(source, destination)
        if Path(source) == stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(module.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        retain_failed_python_build(scope, journal, scheduler)
    monkeypatch.setattr(module.os, "replace", original)
    assert retain_failed_python_build(scope, journal, scheduler).is_dir()


@pytest.mark.parametrize(
    "mutation",
    [
        "later-step",
        "active-pointer",
        "published-python",
        "job",
        "other-platform",
        "runtime-before",
        "compact",
        "root-link",
        "journal-link",
        "both-trees",
        "receipt-modified",
        "retained-modified",
    ],
)
def test_recovery_refuses_unsafe_or_ambiguous_state_without_deletion(
    tmp_path, monkeypatch, mutation
):
    scope, journal, stage, scheduler = recovery_fixture(tmp_path, monkeypatch)
    protected = stage / "leftover"
    if mutation in ("receipt-modified", "retained-modified"):
        target = retain_failed_python_build(scope, journal, scheduler)
        protected = target / "leftover"
        if mutation == "receipt-modified":
            data = json.loads(journal.read_text())
            data["failure_type"] = "Different"
            journal.write_text(json.dumps(data))
        else:
            protected.write_bytes(b"changed by user")
    elif mutation == "later-step":
        data = json.loads(journal.read_text())
        data["snapshotted_steps"].append("identity")
        journal.write_text(json.dumps(data))
    elif mutation == "active-pointer":
        (stage.parent / "current.json").write_text("{}")
    elif mutation == "published-python":
        (stage.parent / "environments" / ("a" * 64)).mkdir(parents=True)
    elif mutation == "job":
        scheduler.capture = lambda _: b"existing job"
    elif mutation == "other-platform":
        scheduler.target_os = "macos"
    elif mutation in ("runtime-before", "compact"):
        path = (
            journal.parent
            / "snapshots"
            / (
                "runtime-layout"
                if mutation == "runtime-before"
                else "runtime-environment"
            )
            / "manifest.json"
        )
        data = json.loads(path.read_text())
        if mutation == "compact":
            data["compact_paths"] = True
        else:
            data["before"]["current"] = {"kind": "file"}
        path.write_text(json.dumps(data))
    elif mutation == "root-link":
        elsewhere = tmp_path / "retained-original"
        stage.rename(elsewhere)
        stage.symlink_to(elsewhere, target_is_directory=True)
    elif mutation == "journal-link":
        elsewhere = tmp_path / "original-journal"
        journal.rename(elsewhere)
        journal.symlink_to(elsewhere)
    elif mutation == "both-trees":
        (stage.parent / (".retained-" + journal.parent.name)).mkdir()
    before = protected.read_bytes()
    with pytest.raises((RuntimeError, ValueError)):
        retain_failed_python_build(scope, journal, scheduler)
    assert protected.read_bytes() == before


def test_recovery_does_not_follow_internal_link_or_touch_its_target(
    tmp_path, monkeypatch
):
    scope, journal, stage, scheduler = recovery_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"external")
    (stage / "alias").symlink_to(outside, target_is_directory=True)
    target = retain_failed_python_build(scope, journal, scheduler)
    assert (target / "alias").is_symlink()
    assert (outside / "data").read_bytes() == b"external"
