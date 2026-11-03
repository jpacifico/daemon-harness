"""Shared verified host extraction and private bootstrap, without host services."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import tarfile

from daemon_core.platform_support import (
    io_path as _source_io_path,
    windows_extended_path as _windows_extended_path,  # noqa: F401 - re-exported for Windows callers
)

def same_interpreter(running: Path, declared: Path) -> bool:
    """Whether a service runs the interpreter its definition declares.

    Windows services start the windowless pythonw.exe beside the declared
    python.exe (see services._windowless_action); both are the same interpreter.
    """
    running, declared = _source_io_path(running).resolve(), _source_io_path(declared).resolve()
    if running == declared:
        return True
    return (
        os.name == "nt"
        and running.parent == declared.parent
        and running.name.lower() == "pythonw.exe"
        and declared.name.lower() == "python.exe"
    )


_PRIVATE_RUNTIME_PATH_KEYS = frozenset({
    "DAEMON_CORE_MEMORY_ROOT", "DAEMON_CORE_STATE_ROOT", "DAEMON_CORE_PROVIDER_CONFIG",
    "DAEMON_CORE_PYTHON", "DAEMON_CORE_AFTER_TURN_SPOOL_DIR", "DAEMON_CORE_LATENCY_LOG",
})


def private_runtime_path_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep owned Windows file roots extended through runtime reads and writes.

    Called only after the companion has validated the private layout. Both the
    gateway and worker must use the same spelling, including memory lock keys.
    """
    return {key: str(_source_io_path(Path(value))) if key in _PRIVATE_RUNTIME_PATH_KEYS else value
            for key, value in environment.items()}


def extract_source(archive: Path, destination: Path, prefix: str) -> dict[str, str]:
    """Validate the entire bounded tar before extracting into a new directory."""
    destination = _source_io_path(destination)
    archive = _source_io_path(archive)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Source destination must be absent.")
    with tarfile.open(archive, "r:gz") as source:
        entries = []
        seen = set()
        expanded = 0
        for member in source:
            path = PurePosixPath(member.name)
            folded = str(path).casefold()
            if (
                not path.parts or path.parts[0] != prefix or path.is_absolute()
                or ".." in path.parts or "\\" in member.name or ":" in member.name
                or not (member.isfile() or member.isdir()) or folded in seen
                or member.size < 0 or member.size > 128 * 1024 * 1024
            ):
                raise ValueError("Unsafe or duplicate source archive entry.")
            expanded += member.size
            entries.append(member)
            seen.add(folded)
            if len(entries) > 25000 or expanded > 512 * 1024 * 1024:
                raise ValueError("Source archive exceeds expansion limits.")
        if not entries:
            raise ValueError("Empty source archive.")
        destination.mkdir(mode=0o700)
        # tarfile normalizes separators during writes, but not when it later
        # applies directory metadata. Extended Win32 paths reject the mixed
        # separators at that point. Normalize extraction copies, keeping the
        # original archive names intact for the manifest and integrity checks.
        native_entries = [member.replace(name=os.path.normpath(member.name), deep=False)
                          for member in entries]
        source.extractall(destination, members=native_entries, filter="data")
    return {
        member.name: hashlib.sha256((destination / member.name).read_bytes()).hexdigest()
        for member in entries if member.isfile()
    }


def source_unchanged(destination: Path, hashes: dict[str, str]) -> bool:
    destination = _source_io_path(destination)
    return all(
        (destination / relative).is_file()
        and not (destination / relative).is_symlink()
        and hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest
        for relative, digest in hashes.items()
    )


def clean_environment(root: Path) -> dict[str, str]:
    keep = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    environment = {key: value for key, value in os.environ.items() if key in keep}
    environment.update({
        "HERMES_HOME": str(root / "probe-profile"),
        "UV_CACHE_DIR": str(root / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_PYTHON_INSTALL_BIN": "0",
        "UV_PYTHON_NO_REGISTRY": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_CACHE_HOME": str(root / "cache"),
        "TMPDIR": str(root / "temp"),
        "TEMP": str(root / "temp"),
        "TMP": str(root / "temp"),
    })
    return environment


def private_home_environment(home: Path) -> dict[str, str]:
    """Use the same owned home for POSIX and Windows native path resolution."""
    return {
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
    }


def stop_process_tree(process, environment, *, windows: bool | None = None):
    windows = os.name == "nt" if windows is None else windows
    if windows:
        # taskkill targets only this child and its descendants. Never enumerate
        # or kill processes by name (other personal instances may be running).
        executable = Path(environment["SYSTEMROOT"]) / "System32" / "taskkill.exe"
        result = subprocess.run([str(executable), "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=15, check=False)
        if result.returncode and process.poll() is None:
            raise RuntimeError("The interrupted private installer process could not be stopped.")
        process.communicate(timeout=5)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)


class HostCommandError(RuntimeError):
    """Keep bounded subprocess diagnostics separate from the public summary."""
    def __init__(self, returncode: int, stderr: str):
        super().__init__(f"Host setup command failed (exit {returncode}).")
        self.diagnostic = stderr[-8000:]
        self.exit_code = returncode


class HostCommandTimeout(HostCommandError):
    """A bounded setup command was stopped, with its final output retained."""
    def __init__(self, timeout, output):
        super().__init__(-1, output)
        self.timeout_seconds = timeout
        self.args = (f"Host setup exceeded its {timeout:g}-second time limit and was stopped.",)


def run(command: list[str], environment: dict[str, str], *, timeout: int = 600, progress=None) -> str:
    import locale
    import tempfile
    import time
    # File-backed pipes expose activity on Windows too: communicate(timeout=...)
    # there may not return partial output until its reader threads have finished.
    # TemporaryFile is private and deleted on close, including failures/cancel.
    encoding = locale.getpreferredencoding(False)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=errors,
                                       **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
        except OSError as error:
            # Popen did not create a usable child. The caller may clear its
            # durable active-command intent without assuming a live process.
            error.private_command_stopped = True
            raise
        def tail(stream):
            size = os.fstat(stream.fileno()).st_size
            # pread is unavailable on Windows; a separate file position would be
            # needed for seek/read while a child writes. Read only after stopping.
            stream.seek(max(0, size - 8000))
            return stream.read().decode(encoding, errors="replace")
        deadline = time.monotonic() + timeout
        last_size = 0
        last_notice = 0.0
        stopping = False
        try:
            while process.poll() is None:
                now = time.monotonic()
                size = os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size
                if progress and size != last_size and now - last_notice >= 1:
                    progress("Installer output received.")
                    last_size, last_notice = size, now
                if now >= deadline:
                    stopping = True
                    stop_process_tree(process, environment)
                    stopping = False
                    raise HostCommandTimeout(timeout, tail(output) + tail(errors))
                time.sleep(min(.1, max(0, deadline - now)))
        except BaseException as error:
            if stopping:
                # Even if the parent exited, a failed tree stop does not prove
                # that descendants released the private installation files.
                raise
            if process.poll() is None:
                stop_process_tree(process, environment)
            # Never mark this before cleanup succeeds. A hard-killed parent or
            # failed process-tree stop must still block destructive recovery.
            error.private_command_stopped = True
            raise
        stderr = tail(errors)
        if process.returncode:
            raise HostCommandError(process.returncode, tail(output) + stderr)
        output.seek(0)
        return output.read().decode(encoding, errors="replace").strip()
