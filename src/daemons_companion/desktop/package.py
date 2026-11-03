"""Deterministic integrity manifest for a portable companion payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


PACKAGE_MANIFEST_SCHEMA = "daemon.companion-package.v1"
PACKAGE_MANIFEST_NAME = "companion-manifest.json"
_MAX_FILES = 5_000
_MAX_FILE_BYTES = 128 * 1024 * 1024


def create_package_manifest(root: Path, *, target_os: str | None = None) -> dict[str, object]:
    if target_os not in {None, "macos", "windows"}:
        raise ValueError("Companion package target is unsupported.")
    base = _direct_root(root)
    entries = _actual_entries(base, target_os=target_os)
    return {
        "schema": PACKAGE_MANIFEST_SCHEMA,
        "files": entries,
    }


def write_package_manifest(root: Path, *, target_os: str | None = None) -> Path:
    base = _direct_root(root)
    path = base / PACKAGE_MANIFEST_NAME
    if path.exists() or path.is_symlink():
        raise ValueError("Companion package manifest already exists.")
    payload = create_package_manifest(base, target_os=target_os)
    _atomic_write(
        path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return path


def verify_package_manifest(root: Path) -> dict[str, object]:
    base = _direct_root(root)
    manifest_path = base / PACKAGE_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Companion package manifest is missing.")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Companion package manifest is invalid.") from error
    expected = _manifest_entries(payload)
    actual = _actual_entries(base)
    if actual != expected:
        raise RuntimeError("Companion package integrity verification failed.")
    return payload


def _actual_entries(root: Path, *, target_os: str | None = None) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == PACKAGE_MANIFEST_NAME:
            continue
        _safe_relative(relative)
        if path.is_symlink():
            raise ValueError("Companion package must not contain symbolic links.")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Companion package contains an unsupported entry.")
        if len(entries) >= _MAX_FILES:
            raise ValueError("Companion package contains too many files.")
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ValueError("Companion package file is too large.")
        entries[relative] = {
            "sha256": _sha256(path),
            "size": size,
            "executable": _is_executable(path, target_os=target_os),
        }
    if not entries:
        raise ValueError("Companion package is empty.")
    return entries


def _manifest_entries(payload: object) -> dict[str, dict[str, object]]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "files"}
        or payload.get("schema") != PACKAGE_MANIFEST_SCHEMA
        or not isinstance(payload.get("files"), dict)
        or not payload["files"]
        or len(payload["files"]) > _MAX_FILES
    ):
        raise ValueError("Companion package manifest shape is invalid.")
    result: dict[str, dict[str, object]] = {}
    for relative, descriptor in payload["files"].items():
        _safe_relative(relative)
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"sha256", "size", "executable"}
            or not _digest(descriptor["sha256"])
            or not isinstance(descriptor["size"], int)
            or isinstance(descriptor["size"], bool)
            or not 0 <= descriptor["size"] <= _MAX_FILE_BYTES
            or not isinstance(descriptor["executable"], bool)
        ):
            raise ValueError("Companion package manifest entry is invalid.")
        result[relative] = dict(descriptor)
    return result


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Companion package path is invalid.")
    path = Path(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Companion package path is unsafe.")
    return value


def _direct_root(root: Path) -> Path:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("Companion package root must be an absolute direct directory.")
    return root


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_executable(path: Path, *, target_os: str | None = None) -> bool:
    if target_os == "windows" or (target_os is None and os.name == "nt"):
        return path.suffix.lower() == ".exe"
    return bool(stat.S_IMODE(path.stat().st_mode) & 0o111)


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or verify a companion package manifest.")
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        print(write_package_manifest(args.root.resolve()))
    else:
        verify_package_manifest(args.root.resolve())
        print("COMPANION_PACKAGE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PACKAGE_MANIFEST_NAME",
    "PACKAGE_MANIFEST_SCHEMA",
    "create_package_manifest",
    "verify_package_manifest",
    "write_package_manifest",
]
