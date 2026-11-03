"""Expose the already verified bundled uv at Hermes' native private location."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


def private_uv_path(layout):
    layout.validate()
    target = layout.profile / "bin" / ("uv.exe" if layout.target_os == "windows" else "uv")
    for path in (target.parent, target):
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("Hermes dependency installer must remain private and direct.")
    if target.exists() and (not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != layout.uv_sha256):
        raise ValueError("Hermes dependency installer differs from the verified package; preserved.")
    return target


def install_private_uv(layout):
    target = private_uv_path(layout)
    if target.is_file():
        return target
    data = layout.uv_executable.read_bytes()
    if hashlib.sha256(data).hexdigest() != layout.uv_sha256:
        raise ValueError("Bundled uv changed before publication.")
    target.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".uv-", delete=False) as stream:
        staged = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        staged.chmod(0o755)
        os.link(staged, target)  # Never overwrite a concurrent file.
    finally:
        staged.unlink(missing_ok=True)
    private_uv_path(layout)
    return target


class PrivateUvRepair:
    def __init__(self, layout):
        self.layout = layout

    def snapshot(self, step, directory):
        target = private_uv_path(self.layout)
        (directory / "uv-before.json").write_text(json.dumps({"file": target.exists(), "directory": target.parent.exists()}))

    def apply(self, step):
        install_private_uv(self.layout)

    def verify(self, step):
        if not private_uv_path(self.layout).is_file():
            raise RuntimeError("Private Hermes dependency installer was not published.")

    def rollback(self, step, directory):
        from daemons_companion.runtime.installation_scope import _read
        before = _read(directory / "uv-before.json")
        if set(before) != {"file", "directory"} or not all(isinstance(v, bool) for v in before.values()):
            raise ValueError("Dependency installer repair snapshot is invalid.")
        target = private_uv_path(self.layout)
        if not before["file"]:
            target.unlink(missing_ok=True)
        if not before["directory"] and target.parent.is_dir() and not any(target.parent.iterdir()):
            target.parent.rmdir()
