"""Back up a running daemon's memory without stopping it.

The conversation keeps going. A backup waits until the after-turn queue holds
no work, archives the vault, and keeps the archive only if every file read
still has the fingerprint it had at the start. Every memory write is atomic, so
a copy taken at a stable instant is a state the daemon already survives after
a crash. A moving queue is waited for: filing one exchange and revising the
scene can take a few minutes with some providers. A file that changes during
the copy is retried a bounded number of times. Otherwise it reports busy.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from daemons_companion.backup.archive import BackupError, create_archive

# Spool states holding turns whose memory work has not reached the vault yet.
BUSY_STATES = ("handoff", "pending", "running", "scene_pending", "scene_running")
ATTEMPTS = 6
WAIT_SECONDS = 5.0
MAX_SPOOL_ENTRIES = 100_000
# A queue whose oldest turn has waited this long with nothing running is stuck.
STALLED_SECONDS = 600
# How long a backup waits for a moving queue. It stays below the app's
# ten-minute limit for the whole backup, archive and encryption included.
QUEUE_WAIT_SECONDS = 420.0
RETRIED = {"archive.source-changed", "archive.integrity", "archive.size-mismatch", "archive.file-unreadable"}


def pending_work(spool: Path | None) -> int:
    """Count queued after-turn jobs. A missing queue holds no work."""
    if spool is None:
        return 0
    count = 0
    for state in BUSY_STATES:
        base = Path(spool) / state
        if not base.is_dir() or base.is_symlink():
            continue
        for _parent, _directories, names in os.walk(base, followlinks=False):
            count += sum(1 for name in names if name.endswith(".json"))
            if count > MAX_SPOOL_ENTRIES:
                raise BackupError("backup.queue-unreadable")
    return count


def failed_work(spool: Path | None) -> int:
    """Turns the worker gave up on: reported, not waited for."""
    if spool is None or not (Path(spool) / "failed").is_dir():
        return 0
    return sum(
        1
        for _parent, _directories, names in os.walk(Path(spool) / "failed", followlinks=False)
        for name in names
        if name.endswith(".json")
    )


def stalled(spool: Path | None, *, now: float | None = None) -> bool:
    """Queued turns that waiting will not settle.

    The worker takes turns in order and keeps a failed one at the head until a
    retry succeeds, so a failed turn blocks every later one; a queue with
    nothing running whose oldest turn has waited for long is not moving either.
    A backup then copies the memory as it is and counts those turns.
    """
    if spool is None or not pending_work(spool):
        return False
    if failed_work(spool):
        return True
    root = Path(spool)
    if any(name.endswith(".json") for state in ("running", "scene_running") if (root / state).is_dir()
           for _p, _d, names in os.walk(root / state, followlinks=False) for name in names):
        return False
    current = time.time() if now is None else now
    oldest = min(
        (
            (Path(parent) / name).stat().st_mtime
            for state in ("handoff", "pending", "scene_pending") if (root / state).is_dir()
            for parent, _d, names in os.walk(root / state, followlinks=False)
            for name in names if name.endswith(".json")
        ),
        default=current,
    )
    return current - oldest > STALLED_SECONDS


def back_up_running_vault(
    vault: Path,
    destination: Path,
    *,
    spool: Path | None = None,
    attempts: int = ATTEMPTS,
    wait: float = WAIT_SECONDS,
    queue_wait: float = QUEUE_WAIT_SECONDS,
    sleep=time.sleep,
    preserve_unknown: bool = False,
    identity: bytes | None = None,
) -> dict:
    """Write an unencrypted archive of a settled vault to a new file."""
    last, where, waited = None, "", 0.0
    for attempt in range(attempts):
        if attempt:
            sleep(wait)
        # The daemon is filing a recent exchange: wait for it, within the budget.
        while pending_work(spool) and not stalled(spool):
            if waited >= queue_wait:
                raise BackupError("backup.daemon-busy", where)
            sleep(wait)
            waited += wait
        try:
            manifest = create_archive(vault, destination, preserve_unknown=preserve_unknown, identity=identity)
        except BackupError as error:
            # A write during the copy: the daemon is answering. Try again.
            if str(error) in RETRIED:
                last, where = str(error), error.area
                continue
            raise
        # Turns whose memory work had not reached the vault are not in the backup.
        missing = failed_work(spool) + (pending_work(spool) if stalled(spool) else 0)
        return {"manifest": manifest, "attempts": attempt + 1, "failed_turns": missing}
    # A file still held open by another program is not a busy daemon.
    if last == "archive.file-unreadable":
        raise BackupError("backup.file-unreadable", where)
    raise BackupError("backup.daemon-busy", where)
