from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from daemon_core.platform_support import create_private_file


@contextmanager
def memory_file_lock(path: Path):
    """Cross-process lock keyed by the absolute target path."""
    lock_root = Path(tempfile.gettempdir()) / "daemon_core_memory_locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()[:24]
    lock_path = lock_root / f"{digest}.lock"
    with lock_path.open("a+b") as handle:
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)


@contextmanager
def memory_file_try_lock(path: Path):
    """Try a cross-process lock without making the caller wait."""

    lock_root = Path(tempfile.gettempdir()) / "daemon_core_memory_locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()[:24]
    lock_path = lock_root / f"{digest}.lock"
    with lock_path.open("a+b") as handle:
        acquired = _try_lock(handle)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock(handle)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}")
        fd = create_private_file(temporary_path)
    else:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _lock(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    except ImportError:
        pass

    _windows_lock(handle, blocking=True)


def _try_lock(handle) -> bool:
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    except ImportError:
        pass

    return _windows_lock(handle, blocking=False)


def _unlock(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    except ImportError:
        pass

    from daemon_core.platform_support import windows_file_unlock
    windows_file_unlock(handle)


def _windows_lock(handle, *, blocking: bool) -> bool:
    from daemon_core.platform_support import windows_file_lock
    return windows_file_lock(handle, blocking=blocking)
