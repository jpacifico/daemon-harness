from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemons_companion.desktop.package import (
    create_package_manifest,
    verify_package_manifest,
    write_package_manifest,
)


def _package(tmp_path: Path) -> Path:
    root = (tmp_path / "package").absolute()
    (root / "payload/src").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "payload/src/app.py").write_text("print('ok')\n", encoding="utf-8")
    uv = root / "bin/uv"
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o755)
    return root


def test_manifest_is_deterministic_and_verifies_exact_tree(tmp_path: Path) -> None:
    root = _package(tmp_path)
    path = write_package_manifest(root)
    first = path.read_bytes()
    assert verify_package_manifest(root)["schema"] == "daemon.companion-package.v1"
    path.unlink()
    write_package_manifest(root)
    assert path.read_bytes() == first
    payload = json.loads(first)
    assert list(payload["files"]) == ["bin/uv", "payload/src/app.py"]
    assert payload["files"]["bin/uv"]["executable"] is True
    assert payload["files"]["payload/src/app.py"]["executable"] is False


@pytest.mark.parametrize("mutation", ["content", "mode", "missing", "extra"])
def test_manifest_rejects_every_tree_difference(tmp_path: Path, mutation: str) -> None:
    root = _package(tmp_path)
    write_package_manifest(root)
    target = root / "payload/src/app.py"
    if mutation == "content":
        target.write_text("changed\n", encoding="utf-8")
    elif mutation == "mode":
        target.chmod(0o755)
    elif mutation == "missing":
        target.unlink()
    else:
        (root / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(RuntimeError, match="integrity"):
        verify_package_manifest(root)


def test_manifest_rejects_symlink_and_unsafe_or_duplicate_shape(tmp_path: Path) -> None:
    root = _package(tmp_path)
    (root / "payload/link").symlink_to(root / "payload/src/app.py")
    with pytest.raises(ValueError, match="symbolic"):
        write_package_manifest(root)
    (root / "payload/link").unlink()
    manifest = write_package_manifest(root)
    payload = json.loads(manifest.read_text())
    payload["files"]["../escape"] = payload["files"]["bin/uv"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        verify_package_manifest(root)


def test_manifest_refuses_replacement_and_non_direct_root(tmp_path: Path) -> None:
    root = _package(tmp_path)
    write_package_manifest(root)
    with pytest.raises(ValueError, match="already exists"):
        write_package_manifest(root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="direct"):
        verify_package_manifest(alias.absolute())


def test_cross_built_windows_manifest_uses_native_windows_executable_semantics(tmp_path):
    root = _package(tmp_path)
    (root / "bin/uv.exe").write_bytes(b"MZsynthetic")
    manifest = create_package_manifest(root, target_os="windows")
    assert manifest["files"]["bin/uv.exe"]["executable"] is True
    assert manifest["files"]["bin/uv"]["executable"] is False
    assert manifest["files"]["payload/src/app.py"]["executable"] is False
    with pytest.raises(ValueError, match="unsupported"):
        write_package_manifest(root, target_os="unknown")
    assert not (root / "companion-manifest.json").exists()
