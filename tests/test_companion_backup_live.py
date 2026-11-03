"""Backups of a running vault, with synthetic vaults and queues only."""

import json
import os
import time
import zipfile

import pytest

from daemons_companion.backup import archive, live
from daemons_companion.desktop.memory import initialize_empty_vault


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    initialize_empty_vault(root)
    for name, text in {
        ".daemon/core/daemon_identity.json": json.dumps({"name": "Nova", "gender": "feminine"}),
        ".daemon/core/persona.md": "# Synthetic persona\n",
        "Projects/Bicycle.md": "# Bicycle\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def queue(spool, state, name="turn.json"):
    path = spool / state / "hermes" / "12345" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def test_waits_for_queued_turns_then_backs_up(vault, tmp_path):
    spool = tmp_path / "spool"
    job = queue(spool, "pending")
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        job.unlink()  # the worker finishes the turn

    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=sleep)
    assert result["attempts"] == 1 and waits == [live.WAIT_SECONDS]
    assert archive.inspect_archive(tmp_path / "backup.zip")["files"].keys() >= {"Projects/Bicycle.md"}


def test_a_queue_filing_for_minutes_is_waited_for(vault, tmp_path):
    # With Mistral, filing one exchange then revising the scene took 1 to 3 minutes:
    # a backup started right after a conversation must not give up after 25 seconds.
    spool = tmp_path / "spool"
    job = queue(spool, "running")
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if sum(waits) >= 180:
            job.unlink()

    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=sleep)
    assert result["failed_turns"] == 0 and sum(waits) == 180


def test_a_queue_that_never_settles_is_reported_busy(vault, tmp_path):
    spool = tmp_path / "spool"
    queue(spool, "scene_running")
    with pytest.raises(archive.BackupError, match="daemon-busy"):
        live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=lambda _: None)
    assert not (tmp_path / "backup.zip").exists()


def test_a_write_during_the_copy_retries_and_keeps_the_new_state(vault, tmp_path, monkeypatch):
    original = zipfile.ZipFile.write
    writes = []

    def answering(self, file, *args, **kwargs):
        result = original(self, file, *args, **kwargs)
        if str(file).endswith("Bicycle.md") and not writes:
            writes.append(file)
            (vault / "Projects/Bicycle.md").write_text("# Bicycle\nRepaired.\n")
        return result

    monkeypatch.setattr(zipfile.ZipFile, "write", answering)
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    assert result["attempts"] == 2
    restored = tmp_path / "restored"
    archive.restore_archive(tmp_path / "backup.zip", restored)
    assert (restored / "Projects/Bicycle.md").read_text() == "# Bicycle\nRepaired.\n"


def test_failed_turns_are_counted_not_waited_for(vault, tmp_path):
    spool = tmp_path / "spool"
    queue(spool, "failed")
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=lambda _: None)
    assert result["attempts"] == 1 and result["failed_turns"] == 1


def test_other_refusals_are_not_retried(vault, tmp_path):
    (vault / ".daemon/core/provider.json").write_text("{}")
    calls = []
    with pytest.raises(archive.BackupError, match="unknown-technical"):
        live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=calls.append)
    assert calls == []


def test_atomic_write_staging_is_never_read(vault, tmp_path, monkeypatch):
    staging = vault / ".daemon/core/.persona.md.3f2a9c"
    staging.write_text("half-written")
    read = []
    original = archive.digest_file
    monkeypatch.setattr(archive, "digest_file", lambda path: read.append(path.name) or original(path))
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    assert staging.name not in read
    assert not any(name.endswith(staging.name) for name in {**result["manifest"]["files"], **result["manifest"]["excluded"]})


def test_a_file_removed_while_listing_retries(vault, tmp_path, monkeypatch):
    original = archive.digest_file
    calls = []

    def vanishing(path):
        calls.append(path.name)
        if len(calls) == 1:
            raise FileNotFoundError(path)
        return original(path)

    monkeypatch.setattr(archive, "digest_file", vanishing)
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    assert result["attempts"] == 2


def test_a_projection_held_open_elsewhere_is_never_read(vault, tmp_path, monkeypatch):
    projection = vault / ".daemon/levels/memory_graph.json"
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text("{}")
    original = archive.digest_file

    def locked(path):
        if path.name == "memory_graph.json":
            raise PermissionError(13, "sharing violation")
        return original(path)

    monkeypatch.setattr(archive, "digest_file", locked)
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    assert result["manifest"]["excluded"][".daemon/levels/memory_graph.json"] == {"size": 2, "reason": "rebuild"}


def test_a_memory_file_held_open_elsewhere_is_named_after_retries(vault, tmp_path, monkeypatch):
    original = archive.digest_file

    def locked(path):
        if path.name == "Bicycle.md":
            raise PermissionError(13, "sharing violation")
        return original(path)

    monkeypatch.setattr(archive, "digest_file", locked)
    waits = []
    with pytest.raises(archive.BackupError, match="backup.file-unreadable"):
        live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=waits.append)
    assert len(waits) == live.ATTEMPTS - 1
    assert not (tmp_path / "backup.zip").exists()


def test_a_backup_with_a_projection_held_open_restores(vault, tmp_path, monkeypatch):
    projection = vault / ".daemon/levels/memory_graph.json"
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text("{}")
    original = archive.digest_file
    monkeypatch.setattr(archive, "digest_file", lambda path: (_ for _ in ()).throw(PermissionError(13, "x")) if path.name == "memory_graph.json" else original(path))
    live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    monkeypatch.setattr(archive, "digest_file", original)
    archive.restore_archive(tmp_path / "backup.zip", tmp_path / "restored")
    assert (tmp_path / "restored/Projects/Bicycle.md").read_text() == "# Bicycle\n"


def test_projections_changing_all_the_time_do_not_block_a_backup(vault, tmp_path, monkeypatch):
    workspace = vault / ".obsidian/workspace.json"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    original = archive.inventory

    def editor_writing(root, **options):
        workspace.write_text(str(workspace.stat().st_mtime_ns if workspace.exists() else 0))
        return original(root, **options)

    monkeypatch.setattr(archive, "inventory", editor_writing)
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    assert result["attempts"] == 1


def test_a_memory_that_never_settles_names_where_it_changes(vault, tmp_path, monkeypatch):
    note = vault / ".daemon/core/persona.md"
    original = archive.inventory

    def writing(root, **options):
        note.write_text(note.read_text() + "x")
        return original(root, **options)

    monkeypatch.setattr(archive, "inventory", writing)
    with pytest.raises(archive.BackupError, match="daemon-busy") as raised:
        live.back_up_running_vault(vault, tmp_path / "backup.zip", sleep=lambda _: None)
    assert raised.value.area == ".daemon/core"


def test_a_failed_turn_blocking_the_queue_does_not_block_a_backup(vault, tmp_path):
    spool = tmp_path / "spool"
    queue(spool, "failed", "head.json")
    for n in range(3):
        queue(spool, "pending", f"turn{n}.json")
    waits = []
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=waits.append)
    assert waits == [] and result["failed_turns"] == 4


def test_an_old_queue_with_nothing_running_is_backed_up_and_counted(vault, tmp_path):
    spool = tmp_path / "spool"
    job = queue(spool, "pending")
    old = time.time() - live.STALLED_SECONDS - 60
    os.utime(job, (old, old))
    result = live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=lambda _: None)
    assert result["failed_turns"] == 1


def test_a_turn_being_processed_is_still_waited_for(vault, tmp_path):
    spool = tmp_path / "spool"
    job = queue(spool, "pending")
    old = time.time() - live.STALLED_SECONDS - 60
    os.utime(job, (old, old))
    queue(spool, "running", "current.json")
    with pytest.raises(archive.BackupError, match="daemon-busy"):
        live.back_up_running_vault(vault, tmp_path / "backup.zip", spool=spool, sleep=lambda _: None)
