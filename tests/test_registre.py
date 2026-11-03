import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.registre import (
    catchup_window_hours,
    faculty_lock,
    last_ok,
    needs_retry,
    read_registre,
    record_pass,
)


def _patch(tmp_path, monkeypatch) -> Path:
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.registre.MEMORY_ROOT", memory_root)
    return memory_root


def test_record_and_read_passes(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    record_pass("dreaming", "garden", "ok", window_hours=48, detail="3 maj", now=datetime(2026, 7, 4, 4, 0))
    record_pass("dreaming", "garden", "echec", window_hours=48, detail="ConnectError", now=datetime(2026, 7, 5, 4, 0))

    entries = read_registre()
    assert len(entries) == 2
    assert entries[0]["status"] == "ok"
    assert entries[1]["detail"] == "ConnectError"
    assert last_ok("dreaming", "garden") == datetime(2026, 7, 4, 4, 0)


def test_catchup_window_stretches_to_last_ok(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    record_pass("dreaming", "garden", "ok", now=datetime(2026, 7, 1, 4, 0))
    record_pass("dreaming", "garden", "echec", now=datetime(2026, 7, 2, 4, 0))
    record_pass("dreaming", "garden", "echec", now=datetime(2026, 7, 3, 4, 0))

    # Three days since success: use 72 hours plus margin, not the configured 48.
    hours = catchup_window_hours(48, "dreaming", "garden", now=datetime(2026, 7, 4, 4, 0))
    assert 72 < hours <= 168


def test_catchup_window_uses_configured_when_all_ok(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    record_pass("dreaming", "garden", "ok", now=datetime(2026, 7, 3, 4, 0))

    hours = catchup_window_hours(48, "dreaming", "garden", now=datetime(2026, 7, 4, 4, 0))
    assert hours == 48


def test_catchup_window_is_capped_at_one_week(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    record_pass("dreaming", "garden", "ok", now=datetime(2026, 6, 1, 4, 0))

    hours = catchup_window_hours(48, "dreaming", "garden", now=datetime(2026, 7, 4, 4, 0))
    assert hours == 168  # Beyond a week, the recovery limit intentionally accepts the gap.


def test_catchup_window_without_history_keeps_configured(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    assert catchup_window_hours(48, "dreaming", "garden") == 48


def test_failure_without_previous_success_extends_bootstrap_window(
    tmp_path,
    monkeypatch,
):
    _patch(tmp_path, monkeypatch)
    record_pass(
        "dreaming",
        "garden",
        "echec",
        now=datetime(2026, 7, 2, 4, 0),
    )

    hours = catchup_window_hours(
        48,
        "dreaming",
        "garden",
        now=datetime(2026, 7, 4, 4, 0),
    )

    assert 96 < hours <= 168


def test_needs_retry_only_tracks_latest_pass(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    record_pass("rem", "garden", "echec")
    assert needs_retry("rem", "garden") is True

    record_pass("rem", "garden", "ok")
    assert needs_retry("rem", "garden") is False


def test_faculty_lock_is_exclusive_and_released(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    with faculty_lock("rem", "garden") as outer:
        assert outer is True
        # A second acquisition is refused while the first is held, even in one process.
        with faculty_lock("rem", "garden") as inner:
            assert inner is False
    # Released on exit: it can be acquired again.
    with faculty_lock("rem", "garden") as again:
        assert again is True


def test_faculty_lock_is_scoped_per_faculty_and_user(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    with faculty_lock("rem", "garden") as a, faculty_lock("dreaming", "garden") as b, faculty_lock("rem", "other") as c:
        assert a is True and b is True and c is True


def test_faculty_lock_steals_stale_dead_pid(tmp_path, monkeypatch):
    from daemon_core.registre import _lock_path

    _patch(tmp_path, monkeypatch)
    lock = _lock_path("rem", "garden")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999 2026-07-04T04:00:00", encoding="utf-8")  # Dead PID: reclaim the lock.
    with faculty_lock("rem", "garden") as acquired:
        assert acquired is True
