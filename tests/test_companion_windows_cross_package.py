from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.build_companion_windows_package import build


def test_windows_cross_package_contains_only_tracked_payload_and_verified_uv(tmp_path: Path):
    repo = tmp_path / "repo"
    sources = repo / "apps/companion/windows"
    sources.mkdir(parents=True)
    for name in ("Start-Daemons.ps1", "DaemonHarnessCompanion.ps1"):
        (sources / name).write_text("# synthetic launcher\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"MZsynthetic uv - never execute")
    digest = hashlib.sha256(uv.read_bytes()).hexdigest()
    output = tmp_path / "package"
    launcher = tmp_path / "Start-Daemons.exe"
    launcher.write_bytes(b"MZsynthetic compiled launcher")
    build(uv, digest, output, repo=repo, launcher=launcher)
    manifest = json.loads((output / "companion-manifest.json").read_text())
    assert manifest["files"]["Start-Daemons.exe"]["executable"] is True
    assert (output / "Start-Daemons.exe").read_bytes() == launcher.read_bytes()
    assert manifest["files"]["bin/uv.exe"]["executable"] is True
    assert manifest["files"]["bin/uv.exe"]["sha256"] == digest
    assert manifest["files"]["Start-Daemons.ps1"]["executable"] is False
    assert (output / "payload/apps/companion/windows/Start-Daemons.ps1").is_file()
    assert not (output / "payload/.git").exists()
    with pytest.raises(ValueError, match="new absolute"):
        build(uv, digest, output, repo=repo)
    with pytest.raises(ValueError, match="SHA-256"):
        build(uv, "0" * 64, tmp_path / "bad-hash", repo=repo)
    (repo / "untracked-private").write_text("do not package")
    with pytest.raises(ValueError, match="clean committed"):
        build(uv, digest, tmp_path / "dirty", repo=repo)
    assert not (tmp_path / "dirty").exists()
