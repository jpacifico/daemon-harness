"""Build the existing PowerShell preview package from a non-Windows host.

The Windows uv binary must already have an independently verified digest.
This builds a development artifact, not a signature or Windows qualification.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from daemons_companion.desktop.package import write_package_manifest  # noqa: E402


def build(uv: Path, digest: str, output: Path, *, repo: Path = ROOT, launcher: Path | None = None) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("An independently verified uv SHA-256 is required.")
    if uv.is_symlink() or not uv.is_file() or hashlib.sha256(uv.read_bytes()).hexdigest() != digest:
        raise ValueError("uv SHA-256 mismatch or unsafe binary.")
    if uv.read_bytes()[:2] != b"MZ":
        raise ValueError("A Windows uv executable is required.")
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError("A new absolute output directory is required.")
    if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip():
        raise ValueError("A clean committed candidate is required.")
    with tempfile.TemporaryDirectory(prefix="daemons-windows-build-") as temporary:
        stage = Path(temporary)
        archive = stage / "source.zip"
        subprocess.run(["git", "-C", str(repo), "archive", "--format=zip", "-o", str(archive), "HEAD"], check=True)
        package = stage / "DAEMONS-Windows"
        payload = package / "payload"
        payload.mkdir(parents=True)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                path = Path(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename or ":" in member.filename:
                    raise ValueError("Unsafe tracked package path.")
                if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("Tracked symbolic links are not allowed in a package.")
            source.extractall(payload)
        (package / "bin").mkdir()
        shutil.copyfile(uv, package / "bin/uv.exe")
        for filename in ("DaemonHarnessCompanion.ps1", "Start-Daemons.ps1"):
            shutil.copyfile(payload / "apps/companion/windows" / filename, package / filename)
        if launcher is not None:
            if launcher.is_symlink() or not launcher.is_file() or launcher.read_bytes()[:2] != b"MZ":
                raise ValueError("A directly compiled Windows launcher is required.")
            shutil.copyfile(launcher, package / "Start-Daemons.exe")
        write_package_manifest(package, target_os="windows")
        output.parent.mkdir(parents=True, exist_ok=True)
        # copytree refuses a destination created concurrently rather than merging.
        shutil.copytree(package, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--uv-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--launcher", type=Path, help="Self-contained native Windows terminal launcher")
    args = parser.parse_args()
    build(args.uv, args.uv_sha256, args.output, launcher=args.launcher)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
