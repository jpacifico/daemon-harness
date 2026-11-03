"""Real filesystem/process contracts, also run on POSIX as the reference."""

import multiprocessing
import os
from pathlib import Path

import pytest

from daemon_core.memory.integrity import (
    atomic_write_text,
    memory_file_lock,
    memory_file_try_lock,
)
from daemon_core.platform_support import is_private_file


@pytest.fixture(autouse=True)
def source_path_for_spawn(monkeypatch):
    # Earlier installer fixtures insert synthetic payload paths. A spawned
    # interpreter must import this test's real source, not an empty fake package.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src"))


def _wait_for_lock(path, connection):
    try:
        connection.send("attempting")
        with memory_file_lock(Path(path)):
            connection.send("acquired")
    except Exception as error:
        connection.send(type(error).__name__)
    finally:
        connection.close()


def _increment(path):
    path = Path(path)
    for _ in range(12):
        with memory_file_lock(path):
            value = int(path.read_text(encoding="utf-8"))
            atomic_write_text(path, str(value + 1))


def _hold_lock(path, connection):
    with memory_file_lock(Path(path)):
        connection.send("held")
        connection.recv()


def test_occupied_lock_is_nonblocking_and_reusable(tmp_path):
    path = tmp_path / "state.json"
    with memory_file_lock(path):
        with memory_file_try_lock(path) as acquired:
            assert acquired is False
        if os.name == "nt":
            with memory_file_try_lock(Path(str(path).upper())) as acquired:
                assert acquired is False
    with memory_file_try_lock(path) as acquired:
        assert acquired is True


def test_blocking_lock_waits_for_owner_then_acquires(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    path = tmp_path / "state.json"
    process = ctx.Process(target=_wait_for_lock, args=(str(path), child))
    try:
        with memory_file_lock(path):
            process.start()
            child.close()
            assert parent.poll(10)
            assert parent.recv() == "attempting"
            # Beyond the former Windows CRT locking retry limit, not an LLM wait.
            assert not parent.poll(11 if os.name == "nt" else 0.2)
        assert parent.poll(10)
        assert parent.recv() == "acquired"
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()


def test_concurrent_updates_are_not_lost_and_remain_private(
    tmp_path, assert_private_file
):
    path = tmp_path / "counter.json"
    atomic_write_text(path, "0")
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_increment, args=(str(path),)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert path.read_text(encoding="utf-8") == "24"
    assert_private_file(path)
    assert is_private_file(path)


def test_atomic_memory_write_is_private_before_replace(
    tmp_path, monkeypatch, assert_private_file
):
    path = tmp_path / "memory.md"
    original_replace = os.replace

    def inspect(source, destination):
        assert_private_file(source)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect)
    for content in ("# Mémoire\n\nExact é 🐈\n", "# Updated\n\nExact Ω\n"):
        atomic_write_text(path, content)
        assert path.read_text(encoding="utf-8") == content
        assert_private_file(path)
        assert is_private_file(path)


def test_owner_process_exit_releases_its_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    path = tmp_path / "state.json"
    process = ctx.Process(target=_hold_lock, args=(str(path), child))
    try:
        process.start()
        child.close()
        assert parent.poll(10)
        assert parent.recv() == "held"
        with memory_file_try_lock(path) as acquired:
            assert acquired is False
        process.terminate()
        process.join(5)
        assert not process.is_alive()
        with memory_file_try_lock(path) as acquired:
            assert acquired is True
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()


def test_private_health_check_rejects_permissions_open_to_other_users(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_text(path, "synthetic")
    assert is_private_file(path)
    if os.name == "nt":
        import win32con
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        acl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            win32con.GENERIC_READ,
            win32security.CreateWellKnownSid(win32security.WinWorldSid, None),
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    else:
        path.chmod(0o644)
    assert not is_private_file(path)


def test_sqlite_cache_and_rollback_journal_are_private(tmp_path, assert_private_file):
    from daemon_core.semantic_atlas import _cache_database

    path = tmp_path / "owned-cache" / "index.sqlite3"
    with _cache_database(path) as database:
        assert_private_file(path)
        assert_private_file(path.parent, directory=True)
        database.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?)", ("synthetic", 1, "[0.1]")
        )
        journal = path.with_name(path.name + "-journal")
        assert journal.exists()
        if os.name == "nt":
            import ntsecuritycon
            import win32security

            info = win32security.DACL_SECURITY_INFORMATION
            acl = win32security.GetFileSecurity(
                str(journal), info
            ).GetSecurityDescriptorDacl()
            owner_acl = win32security.GetFileSecurity(
                str(path), info
            ).GetSecurityDescriptorDacl()
            assert acl.GetAceCount() == 1
            assert acl.GetAce(0)[0] == (
                win32security.ACCESS_ALLOWED_ACE_TYPE,
                16,
            )  # inherited
            assert acl.GetAce(0)[1:] == (
                ntsecuritycon.FILE_ALL_ACCESS,
                owner_acl.GetAce(0)[2],
            )
        else:
            assert_private_file(journal)
        database.rollback()
    assert not journal.exists()
    # Reopening an existing cache preserves its privacy and its schema.
    with _cache_database(path) as database:
        assert database.execute("SELECT COUNT(*) FROM embeddings").fetchone() == (0,)
        assert_private_file(path)


@pytest.mark.parametrize("failure_at", ["fdopen", "fsync", "replace"])
def test_atomic_failure_preserves_original_and_cleans_up(
    tmp_path, monkeypatch, failure_at
):
    path = tmp_path / "memory.md"
    atomic_write_text(path, "original")

    def fail(*args, **kwargs):
        raise OSError("synthetic IO failure")

    monkeypatch.setattr(os, failure_at, fail)
    with pytest.raises(OSError, match="synthetic IO failure"):
        atomic_write_text(path, "replacement")
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content", ["Aster\nfeminine\n", "Étoile 🌟\r\nnext\n"])
def test_atomic_text_preserves_exact_utf8_bytes_and_line_endings(tmp_path, content):
    target = tmp_path / "exact.txt"
    atomic_write_text(target, content)
    assert target.read_bytes() == content.encode("utf-8")
