"""Pinned official Codex package, confined to a verified private host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import time
from urllib.request import urlopen

from daemons_companion.runtime.acquisition import open_with_retry

from daemons_companion.runtime.installation_scope import InstallationScope

# gpt-6-astra, the recommended ChatGPT model, refuses older Codex releases.
VERSION = "0.159.2"
PACKAGES = {
    "macos": ("aarch64-apple-darwin", 129522674, "38aaf6dce63099fd10988948d03bbc6c0474253aef6961fcbe60f8d154b39101"),
    "windows": ("x86_64-pc-windows-msvc", 156614559, "a7ab591043d99e8a88d0c2bebf15f810b15599a71148c4af944a755af3c71eef"),
}


def download_package(url, destination, size, digest, progress=None):
    end = time.monotonic() + 300
    checksum = hashlib.sha256()
    count = 0
    with open_with_retry(lambda: urlopen(url, timeout=30), url=url, notice=progress) as response, destination.open("xb") as stream:
        if not response.geturl().startswith("https://"):
            raise RuntimeError("Codex download must use HTTPS.")
        while chunk := response.read1(1024 * 1024):
            count += len(chunk)
            if count > size or time.monotonic() > end:
                raise RuntimeError("Codex download exceeded its bounds.")
            checksum.update(chunk)
            stream.write(chunk)
    if count != size or checksum.hexdigest() != digest:
        raise RuntimeError("Official Codex package integrity check failed.")


def extract_package(archive, destination):
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        seen = set()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            total += member.size
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or "\\" in member.name or ":" in member.name
                    or not (member.isfile() or member.isdir())
                    or str(path).casefold() in seen or member.size < 0
                    or total > 800 * 1024 * 1024 or len(members) > 1000):
                raise ValueError("Unsafe Codex package entry.")
            seen.add(str(path).casefold())
        source.extractall(destination, members=members, filter="data")
    return {m.name: hashlib.sha256((destination / m.name).read_bytes()).hexdigest()
            for m in members if m.isfile()}


def private_codex(layout):
    from daemons_companion.openclaw.host_install import OpenClawHostLayout, verify_installed
    if isinstance(layout, OpenClawHostLayout):
        verify_installed(layout)
        InstallationScope(layout.attempt).receipt()
    else:
        InstallationScope.from_verified_host(layout).receipt()
    destination = layout.attempt / "codex" / VERSION
    receipt = destination / "daemons-package.json"
    for path in (destination, receipt):
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("Private Codex paths must be direct.")
    if not destination.exists():
        return None
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if data.get("version") != VERSION or data.get("target_os") != layout.target_os:
        raise ValueError("Private Codex receipt differs from the selected package.")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Private Codex receipt is empty.")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        path = destination / relative
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name
                or path.resolve() != path or path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("Private Codex files changed; nothing will be executed.")
    binary = destination / "bin" / ("codex.exe" if layout.target_os == "windows" else "codex")
    if binary.relative_to(destination).as_posix() not in files:
        raise ValueError("Private Codex executable is missing from the receipt.")
    return binary


def install_private_codex(layout, progress, *, downloader=None):
    existing = private_codex(layout)
    if existing:
        return existing
    triple, size, digest = PACKAGES[layout.target_os]
    parent = layout.attempt / "codex"
    if parent.resolve() != parent or parent.is_symlink():
        raise ValueError("Private Codex directory must be direct.")
    parent.mkdir(mode=0o700, exist_ok=True)
    url = f"https://github.com/openai/codex/releases/download/rust-v{VERSION}/codex-package-{triple}.tar.gz"
    progress(f"Downloading and verifying official Codex {VERSION} in the private installation...")
    with tempfile.TemporaryDirectory(prefix="download-", dir=parent) as raw:
        temporary = Path(raw)
        archive = temporary / "codex.tar.gz"
        if downloader is None:
            download_package(url, archive, size, digest, progress)
        else:
            downloader(url, archive, size, digest)
        # Verify independently even when the downloader is substituted by a local cache.
        if archive.stat().st_size != size or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Codex package integrity check failed.")
        package = temporary / "package"
        package.mkdir(mode=0o700)
        files = extract_package(archive, package)
        receipt = {"version": VERSION, "target_os": layout.target_os, "files": files}
        (package / "daemons-package.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        destination = parent / VERSION
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Private Codex destination became occupied.")
        os.rename(package, destination)
    return private_codex(layout)
