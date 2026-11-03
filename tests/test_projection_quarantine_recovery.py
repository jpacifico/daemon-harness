"""Repeated scopes and interrupted archival must never erase pending work."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import daemon_core.after_turn_spool as module
import daemon_core.scene_model as scene_model
from daemon_core.after_turn_spool import AfterTurnSpool, SceneProjectionJob


def fixture(monkeypatch, tmp_path, host="hermes", turn="old"):
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "off")
    spool = AfterTurnSpool(tmp_path / "spool")
    spool._ensure_runtime_namespace()
    job = SceneProjectionJob(
        host=host, user_id="synthetic-user", scene_scope="same-scope",
        source_trace_id=turn, source_turn_started_at=1,
        covered_turn_ids=(turn,), attempts=3, dialogue_turn_limit=1,
        last_error="ValueError", created_at="2026-09-13T00:00:00+00:00",
    )
    source = spool._projection_path("scene_failed", job)
    source.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately preserve noncanonical whitespace/CRLF in immutable archives.
    raw = (json.dumps(job.as_dict(), indent=2) + "\n").replace("\n", "\r\n").encode()
    source.write_bytes(raw)
    monkeypatch.setattr(scene_model, "read_scene_model", lambda *a: SimpleNamespace(source_turn_started_at=2))
    return spool, job, source, raw


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_legacy_archive_and_repeated_batches_do_not_block_next_scene(monkeypatch, tmp_path, host):
    spool, job, source, raw = fixture(monkeypatch, tmp_path, host)
    legacy = spool._projection_path("scene_quarantined", job)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy archive must remain exact")
    legacy_receipt = tmp_path / "spool/projection_quarantine_receipts" / host / job.user_id / legacy.name
    legacy_receipt.parent.mkdir(parents=True)
    legacy_receipt.write_bytes(b"legacy receipt must remain exact")
    assert spool.recover_failed_projections(host, job.user_id) == 1
    assert not source.exists()
    spool, newer, source, raw2 = fixture(monkeypatch, tmp_path, host, "second-batch")
    pending = replace(newer, source_trace_id="next-scene", covered_turn_ids=("next-scene",),
                      attempts=0, last_error="", source_turn_started_at=3)
    spool.enqueue_projection(pending)
    called = []
    result = spool.drain_projections(host, job.user_id, lambda j: called.append(j.source_trace_id))
    assert result.recovered == result.completed == 1
    assert called == ["next-scene"]
    assert legacy.read_bytes() == b"legacy archive must remain exact"
    assert legacy_receipt.read_bytes() == b"legacy receipt must remain exact"
    archives = [p for p in legacy.parent.glob("*.json") if p != legacy]
    assert sorted(p.read_bytes() for p in archives) == sorted([raw, raw2])
    for p in archives:
        receipt = json.loads((legacy_receipt.parent / p.name).read_text())
        assert receipt["status"] == "completed"
        assert receipt["job_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert not source.exists()


@pytest.mark.parametrize("phase", ["pending-receipt", "archive", "finalize", "completed-receipt", "unlink"])
def test_restart_after_each_commit_boundary_converges(monkeypatch, tmp_path, phase):
    spool, job, source, raw = fixture(monkeypatch, tmp_path)
    write = module.atomic_write_text
    replace_file = module.os.replace
    unlink = Path.unlink
    calls = []
    def close(*args, **kwargs):
        calls.append(args[0])
        if phase == "finalize":
            raise KeyboardInterrupt("simulated process exit")
        return True
    def crash_write(path, content):
        write(path, content)
        if path.parent.parent.parent.name == "projection_quarantine_receipts":
            status = json.loads(content)["status"]
            if phase == status + "-receipt":
                raise KeyboardInterrupt("simulated process exit")
    def crash_replace(src, dst):
        replace_file(src, dst)
        if phase == "archive" and "scene_quarantined" in str(dst):
            raise KeyboardInterrupt("simulated process exit")
    def crash_unlink(path, *args, **kwargs):
        if phase == "unlink" and path == source:
            raise KeyboardInterrupt("simulated process exit")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_text", crash_write)
        patch.setattr(module.os, "replace", crash_replace)
        patch.setattr(module, "finalize_deferred_scene", close)
        patch.setattr(Path, "unlink", crash_unlink)
        with pytest.raises(KeyboardInterrupt):
            spool.recover_failed_projections(job.host, job.user_id)
    assert source.read_bytes() == raw
    restarted = AfterTurnSpool(tmp_path / "spool")
    assert restarted.recover_failed_projections(job.host, job.user_id) == 1
    assert restarted.recover_failed_projections(job.host, job.user_id) == 0
    assert not source.exists()
    archived = list((tmp_path / "spool/scene_quarantined").rglob("*.json"))
    assert len(archived) == 1 and archived[0].read_bytes() == raw
    receipt = next((tmp_path / "spool/projection_quarantine_receipts").rglob("*.json"))
    assert json.loads(receipt.read_text())["status"] == "completed"


def test_unfinished_continuity_keeps_discoverable_source(monkeypatch, tmp_path):
    spool, job, source, raw = fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "finalize_deferred_scene", lambda *a, **kw: False)
    with pytest.raises(RuntimeError, match="finalization incomplete"):
        spool.recover_failed_projections(job.host, job.user_id)
    assert source.read_bytes() == raw
    monkeypatch.setattr(module, "finalize_deferred_scene", lambda *a, **kw: True)
    assert spool.recover_failed_projections(job.host, job.user_id) == 1


def test_corrupt_digest_destination_is_not_overwritten(monkeypatch, tmp_path):
    spool, job, source, raw = fixture(monkeypatch, tmp_path)
    dest = spool._projection_path("scene_quarantined", job).with_name(
        f"{job.projection_ref}-{hashlib.sha256(raw).hexdigest()}.json")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"unexpected bytes")
    with pytest.raises(RuntimeError, match="integrity mismatch"):
        spool.recover_failed_projections(job.host, job.user_id)
    assert source.read_bytes() == raw and dest.read_bytes() == b"unexpected bytes"


def test_concurrent_archival_reuses_one_exact_archive(monkeypatch, tmp_path):
    spool, job, source, raw = fixture(monkeypatch, tmp_path)
    def archive(_):
        return spool._quarantine_projection_job(source, job, reason_code="superseded", now=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(archive, range(2)))
    archived = list((tmp_path / "spool/scene_quarantined").rglob("*.json"))
    assert len(archived) == 1 and archived[0].read_bytes() == raw
    assert not source.exists()
