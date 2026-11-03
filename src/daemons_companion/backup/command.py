"""Backup commands for the desktop app: one JSON request in, one JSON line out.

Only bounded codes and counts leave this process: never paths read from the
vault, file names, Markdown or exception text.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from daemons_companion.backup.archive import BackupError
from daemons_companion.backup.live import back_up_running_vault
from daemons_companion.backup.restore import read_source


def _path(value, *, optional=False) -> Path | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 8192 or not Path(value).is_absolute():
        raise BackupError("backup.invalid-request")
    return Path(value)


def handle_request(request: object) -> dict:
    if not isinstance(request, dict):
        raise BackupError("backup.invalid-request")
    if request.get("action") == "backup" and set(request) in (
        {"action", "vault", "spool", "destination"},
        {"action", "vault", "spool", "destination", "preserve_unknown"},
    ):
        if request.get("preserve_unknown", False) is not True and "preserve_unknown" in request:
            raise BackupError("backup.invalid-request")
        result = back_up_running_vault(
            _path(request["vault"]), _path(request["destination"]),
            spool=_path(request["spool"], optional=True),
            preserve_unknown=request.get("preserve_unknown", False),
        )
        files = result["manifest"]["files"]
        return {"files": len(files), "bytes": sum(entry["size"] for entry in files.values()),
                "attempts": result["attempts"], "failed_turns": result["failed_turns"]}
    if request.get("action") == "restore-inspect" and set(request) == {"action", "archive"}:
        source = read_source(_path(request["archive"]))
        return {"name": source.name, "gender": source.gender, "language": source.language}
    raise BackupError("backup.invalid-request")


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise BackupError("backup.invalid-request")
        result = {"ok": True, "result": handle_request(json.loads(raw))}
    except BackupError as error:
        result = {"ok": False, "code": str(error)}
        if error.area:
            result["diagnostic"] = {"type": "Changed", "area": error.area}
    except (OSError, ValueError, TypeError, KeyError) as error:
        # The exception class and system error numbers only: no path or text.
        diagnostic = {"type": type(error).__name__}
        if isinstance(error, OSError):
            diagnostic.update(errno=error.errno, winerror=getattr(error, "winerror", None))
        result = {"ok": False, "code": "backup.unavailable", "diagnostic": diagnostic}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
