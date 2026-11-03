"""Maintenance pass journal and bounded recovery windows.

Windowed faculties such as Dreaming and REM record success, failure or skip
in registre_passes.jsonl. The journal supports inspection and extends a
subsequent recovery window to the last successful pass, subject to a cap.

Per-turn narration is not recorded here: a later turn can recover it without
losing a maintenance window. The Stance Register is historical and has been
suspended from the canonical runtime since 2026-07-30."""

import argparse
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT
from daemon_core.platform_support import process_is_alive as _pid_alive

logger = logging.getLogger("daemon_core.registre")

MAX_CATCHUP_HOURS = 168  # Accept a gap beyond one week rather than extending recovery indefinitely.
CATCHUP_MARGIN_HOURS = 6
LOCK_STALE_SECONDS = 3600  # Passes take minutes; a lock older than an hour is considered stale.


def registre_path() -> Path:
    return MEMORY_ROOT / "levels" / "registre_passes.jsonl"


def record_pass(faculte: str, user_id: str, status: str, window_hours: float = 0, detail: str = "", now: datetime | None = None) -> None:
    entry = {
        "ts": (now or datetime.now()).isoformat(timespec="seconds"),
        "faculte": faculte,
        "user_id": user_id,
        "status": status,  # ok | echec | skip
        "window_hours": round(window_hours, 1),
        "detail": detail[:300],
    }
    path = registre_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_registre() -> list[dict]:
    path = registre_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def last_ok(faculte: str, user_id: str) -> datetime | None:
    for entry in reversed(read_registre()):
        if entry.get("faculte") == faculte and entry.get("user_id") == user_id and entry.get("status") == "ok":
            try:
                return datetime.fromisoformat(entry["ts"])
            except (KeyError, ValueError):
                continue
    return None


def last_pass(faculte: str, user_id: str) -> dict | None:
    for entry in reversed(read_registre()):
        if (
            entry.get("faculte") == faculte
            and entry.get("user_id") == user_id
        ):
            return entry
    return None


def needs_retry(faculte: str, user_id: str) -> bool:
    latest = last_pass(faculte, user_id)
    return bool(latest and latest.get("status") == "echec")


def catchup_window_hours(configured_hours: float, faculte: str, user_id: str, now: datetime | None = None) -> float:
    """Extend the configured window to the last successful pass, capped at one week."""
    now = now or datetime.now()
    last = last_ok(faculte, user_id)
    if last is None:
        failures = []
        for entry in read_registre():
            if (
                entry.get("faculte") != faculte
                or entry.get("user_id") != user_id
                or entry.get("status") != "echec"
            ):
                continue
            try:
                failures.append(datetime.fromisoformat(entry["ts"]))
            except (KeyError, ValueError):
                continue
        if failures:
            gap_hours = (
                (now - min(failures)).total_seconds() / 3600
                + configured_hours
                + CATCHUP_MARGIN_HOURS
            )
            return min(
                max(configured_hours, gap_hours),
                MAX_CATCHUP_HOURS,
            )
        return configured_hours
    gap_hours = (now - last).total_seconds() / 3600 + CATCHUP_MARGIN_HOURS
    return min(max(configured_hours, gap_hours), MAX_CATCHUP_HOURS)


# ---------------------------------------------------------------------------
# Faculty lock: shared protection against duplicate Dreaming, REM and follow-ups.
# ---------------------------------------------------------------------------
# Overlapping manual and scheduled passes must not write concurrently.
# Use one lock file per faculty and user,
# reclaiming it when its process is gone or it is too old.


def _lock_path(faculte: str, user_id: str) -> Path:
    return MEMORY_ROOT / "levels" / f"lock_{faculte}_{safe_memory_user_id(user_id)}.lock"


def _lock_is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    if age > LOCK_STALE_SECONDS:
        return True
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
    except (ValueError, IndexError, FileNotFoundError):
        return True
    return not _pid_alive(pid)


@contextmanager
def faculty_lock(faculte: str, user_id: str):
    """Yield True when the lock is acquired, or False when another live pass owns it."""
    path = _lock_path(faculte, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            if attempt == 1 and _lock_is_stale(path):
                logger.warning("Verrou %s périmé — récupération", path.name)
                path.unlink(missing_ok=True)
                continue
            break
    if not acquired:
        logger.info("Verrou %s déjà tenu — passe ignorée", path.name)
        yield False
        return
    try:
        yield True
    finally:
        path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Registre des passes du daemon.")
    parser.add_argument("--tail", type=int, default=15, help="Nombre d'entrées à afficher.")
    parser.add_argument("--faculte", help="Filtrer par faculté.")
    parser.add_argument(
        "--needs-retry",
        choices=("dreaming", "rem"),
        help="Sort avec 0 si la dernière passe de cette faculté a échoué.",
    )
    parser.add_argument("--user-id")
    args = parser.parse_args()

    if args.needs_retry:
        if not args.user_id:
            parser.error("--user-id est requis avec --needs-retry.")
        raise SystemExit(
            0 if needs_retry(args.needs_retry, args.user_id) else 1
        )

    entries = read_registre()
    if args.faculte:
        entries = [e for e in entries if e.get("faculte") == args.faculte]
    for entry in entries[-args.tail :]:
        print(f"{entry['ts']} | {entry['faculte']:<15} | {entry.get('user_id', '?'):<12} | {entry['status']:<6} | {entry.get('window_hours', '')}h | {entry.get('detail', '')}")


if __name__ == "__main__":
    main()
