"""Pinned downloads, isolated environment and hostile archive boundaries."""

import io
import json
from pathlib import Path
import tarfile
import zipfile
import pytest
from daemons_companion.openclaw.host_install import (
    OpenClawHostLayout,
    plan,
    install,
    extract_node,
    verify_installed,
    NODE_VERSION,
    OPENCLAW_VERSION,
)


@pytest.fixture
def layout(tmp_path):
    lock = Path(__file__).resolve().parents[1] / "apps/companion/openclaw-runtime"
    return OpenClawHostLayout(tmp_path.resolve() / "companion", "c" * 32, "macos", lock)


def node_archive(destination, prefix):
    with tarfile.open(destination, "w:gz") as a:
        for name, raw, mode in [
            ("bin/node", b"fake-node", 0o755),
            ("lib/node_modules/npm/bin/npm-cli.js", b"fake-npm", 0o644),
        ]:
            i = tarfile.TarInfo(prefix + "/" + name)
            i.size = len(raw)
            i.mode = mode
            a.addfile(i, io.BytesIO(raw))


def test_plan_read_only_and_exact_version(layout):
    p = plan(layout)
    assert not layout.companion_home.exists()
    assert p["openclaw_version"] == OPENCLAW_VERSION
    assert p["node_version"] == NODE_VERSION
    assert not p["starts_services"] and not p["configures_channel"]
    assert len(p["lock_sha256"]) == 64


def test_install_uses_only_private_runtime_and_frozen_dependency_lock(layout):
    calls = []

    def downloader(url, destination, sha):
        node_archive(destination, layout.node_root.name)

    def runner(command, env, **kwargs):
        calls.append(command)
        assert env["HOME"] == str(layout.attempt / "os-home")
        assert env["OPENCLAW_STATE_DIR"] == str(layout.profile)
        assert "OPENAI_API_KEY" not in env and "NPM_TOKEN" not in env
        if command[-1] == "--version":
            return "v" + NODE_VERSION if len(command) == 2 else OPENCLAW_VERSION
        assert command[:3] == [str(layout.node), str(layout.npm), "ci"]
        assert "--ignore-scripts" in command and "--no-audit" in command
        p = layout.host / "node_modules/openclaw"
        p.mkdir(parents=True)
        (p / "package.json").write_text(json.dumps({"version": OPENCLAW_VERSION}))
        (p / "openclaw.mjs").write_text("synthetic")
        return ""

    r = install(layout, plan(layout)["revision"], runner=runner, downloader=downloader)
    assert r["status"] == "installed" and len(calls) == 3
    assert verify_installed(layout) == r
    assert not (layout.profile / "openclaw.json").exists()
    with pytest.raises(ValueError, match="already exists"):
        install(layout, plan(layout)["revision"], runner=runner, downloader=downloader)


def test_wrong_confirmation_creates_nothing(layout):
    with pytest.raises(ValueError, match="changed"):
        install(layout, "0" * 64)
    assert not layout.companion_home.exists()


def test_download_failure_retains_scoped_diagnostic(layout):
    def fail(*args):
        raise ValueError("synthetic integrity error")

    with pytest.raises(ValueError):
        install(layout, plan(layout)["revision"], downloader=fail)
    r = json.loads(layout.receipt.read_text())
    assert r["status"] == "failed" and r["error_type"] == "ValueError"
    assert not layout.node.exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "node/../../escape",
        "/absolute",
        "node/C:invalid",
        "node/back\\slash",
    ],
)
def test_tar_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as a:
        i = tarfile.TarInfo(name)
        i.size = 1
        a.addfile(i, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        extract_node(archive, tmp_path / "out", "node")


def test_tar_refuses_links_outside_npm_aliases(tmp_path):
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as a:
        i = tarfile.TarInfo("node/bin/evil")
        i.type = tarfile.SYMTYPE
        i.linkname = "/tmp/outside"
        a.addfile(i)
    with pytest.raises(ValueError):
        extract_node(archive, tmp_path / "out", "node")


def test_zip_windows_layout(tmp_path):
    archive = tmp_path / "node.zip"
    with zipfile.ZipFile(archive, "w") as a:
        a.writestr("node/node.exe", b"MZ-synthetic")
    extract_node(archive, tmp_path / "out", "node")
    assert (tmp_path / "out/node/node.exe").read_bytes() == b"MZ-synthetic"


def test_archive_duplicates_refused(tmp_path):
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as a:
        for _ in range(2):
            i = tarfile.TarInfo("node/bin/node")
            i.size = 1
            a.addfile(i, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        extract_node(archive, tmp_path / "out", "node")


def test_extract_node_with_companion_length_paths(tmp_path, monkeypatch):
    from tests.companion_windows_io import WindowsPathGuard
    import daemons_companion.openclaw.host_install as host
    destination = tmp_path / ('x' * 90) / 'node'
    destination.parent.mkdir()
    archive = tmp_path / 'node.zip'
    member = 'node-v24.15.0-win-x64/node_modules/npm/node_modules/@sigstore/protobuf-specs/dist/__generated__/google/protobuf/descriptor.js'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(member, 'verified bytes')
    with monkeypatch.context() as patch:
        guard = WindowsPathGuard(patch, destination)
        patch.setattr(host, '_source_io_path', guard.io_path)
        host.extract_node(archive, destination, 'node-v24.15.0-win-x64')
        assert guard.extended and not guard.denied
        assert (guard.io_path(destination) / member).read_text() == 'verified bytes'


def test_failed_extraction_retains_stage_without_sensitive_text(layout, monkeypatch):
    import daemons_companion.openclaw.host_install as host
    def downloader(url, destination, sha):
        node_archive(destination, layout.node_root.name)
    def fail(*args):
        raise OSError(2, 'private detail not for report')
    monkeypatch.setattr(host, 'extract_node', fail)
    with pytest.raises(OSError) as failure:
        install(layout, plan(layout)['revision'], downloader=downloader)
    assert failure.value.host_stage == 'node-extract'
    receipt = layout.receipt.read_text()
    assert json.loads(receipt)['host_stage'] == 'node-extract'
    assert 'private detail' not in receipt


@pytest.mark.parametrize("fault", ["spawn", "cancel", "unconfirmed"])
def test_failed_native_command_retry_requires_proven_cleanup(layout, tmp_path, fault):
    import sys
    import time
    from daemons_companion.runtime.bootstrap import run
    marker = tmp_path / "child-survived"
    def downloader(url, destination, sha):
        node_archive(destination, layout.node_root.name)
    def failing(command, environment, **kwargs):
        if fault == "unconfirmed":
            raise KeyboardInterrupt
        if fault == "spawn":
            return run([str(tmp_path / "missing-executable")], {}, timeout=2)
        def cancel(message):
            raise KeyboardInterrupt
        return run([sys.executable, "-c", "import time,sys,pathlib;print('ready',flush=True);time.sleep(.5);pathlib.Path(sys.argv[1]).touch()", str(marker)], {}, timeout=2, progress=cancel)
    with pytest.raises(FileNotFoundError if fault == "spawn" else KeyboardInterrupt):
        install(layout, plan(layout)["revision"], runner=failing, downloader=downloader)
    receipt = json.loads(layout.receipt.read_text())
    assert bool(receipt.get("command_active")) == (fault == "unconfirmed")
    if fault == "cancel":
        time.sleep(.6)
        assert not marker.exists()
    calls = []
    def retry_download(*args):
        calls.append(1)
        raise RuntimeError("retry reached")
    with pytest.raises(ValueError if fault == "unconfirmed" else RuntimeError,
                       match="needs recovery" if fault == "unconfirmed" else "retry reached"):
        install(layout, plan(layout)["revision"], downloader=retry_download, resume=True)
    assert bool(calls) == (fault != "unconfirmed")


def _lock_directory(root, version, packages):
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"dependencies": {"openclaw": version}}))
    (root / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {}, "node_modules/openclaw": {
            "version": version, "resolved": "https://registry.npmjs.org/openclaw/-/x.tgz",
            "integrity": "sha512-synthetic"}, **packages}}))
    return root


REGISTRY = {"resolved": "https://registry.npmjs.org/npm/-/npm.tgz", "integrity": "sha512-npm"}


@pytest.mark.parametrize("packages, accepted", [
    ({"node_modules/npm": REGISTRY, "node_modules/npm/node_modules/abbrev": {"inBundle": True}}, True),
    ({"node_modules/npm": REGISTRY,
      "node_modules/npm/node_modules/a/node_modules/b": {"inBundle": True},
      "node_modules/npm/node_modules/a": {"inBundle": True}}, True),
    ({"node_modules/npm": {"version": "1"}, "node_modules/npm/node_modules/abbrev": {"inBundle": True}}, False),
    ({"node_modules/npm": REGISTRY,
      "node_modules/npm/node_modules/abbrev": {"inBundle": True, "resolved": "https://example.com/a.tgz"}}, False),
    ({"node_modules/stray": {"inBundle": True}}, False),
])
def test_bundled_dependencies_ride_on_a_registry_parent_only(tmp_path, packages, accepted):
    lock = _lock_directory(tmp_path / "lock", OPENCLAW_VERSION, packages)
    layout = OpenClawHostLayout(tmp_path / "companion", "c" * 32, "macos", lock)
    if accepted:
        layout.validate()
    else:
        with pytest.raises(ValueError, match="immutable npm registry"):
            layout.validate()


def test_a_host_installed_by_the_earlier_release_keeps_its_own_pins(tmp_path):
    from daemons_companion.openclaw.host_install import CURRENT, RELEASES

    bundled = Path(__file__).resolve().parents[1] / "apps/companion/openclaw-runtime"
    fresh = OpenClawHostLayout.existing(tmp_path / "companion", "c" * 32, "macos", bundled)
    assert fresh.release is CURRENT and fresh.lock_directory == bundled
    earlier = RELEASES["2026.9.1-beta.1"]
    staged = fresh.attempt / "host"
    _lock_directory(staged, earlier.version, {})
    reopened = OpenClawHostLayout.existing(tmp_path / "companion", "c" * 32, "macos", bundled)
    assert reopened.lock_directory == staged and reopened.release is earlier
    assert reopened.node_root.name == "node-v24.15.0-darwin-arm64"
    reviewed = plan(reopened)
    assert reviewed["openclaw_version"] == earlier.version and reviewed["node_version"] == "24.15.0"
    with pytest.raises(ValueError, match="current qualified release"):
        install(reopened, reviewed["revision"])


def test_an_unqualified_version_is_refused(tmp_path):
    lock = _lock_directory(tmp_path / "lock", "2026.10.8", {})
    with pytest.raises(ValueError, match="not a qualified release"):
        OpenClawHostLayout(tmp_path / "companion", "c" * 32, "macos", lock).validate()


def test_new_daemons_get_2026_11_6_and_2026_10_7_hosts_stay_recognised(tmp_path):
    from daemons_companion.openclaw.host_install import CURRENT, RELEASES
    from daemons_companion.runtime.acquisition import recognized_version

    bundled = Path(__file__).resolve().parents[1] / "apps/companion/openclaw-runtime"
    assert CURRENT.version == "2026.11.6" and CURRENT.node_version == "24.22.1"
    fresh = OpenClawHostLayout.existing(tmp_path / "companion", "c" * 32, "macos", bundled)
    assert fresh.release is CURRENT and plan(fresh)["openclaw_version"] == "2026.11.6"
    # A daemon created with 0.11.19 keeps the release its staged manifest names.
    previous = RELEASES["2026.10.7"]
    _lock_directory(fresh.attempt / "host", previous.version, {})
    reopened = OpenClawHostLayout.existing(tmp_path / "companion", "c" * 32, "macos", bundled)
    assert reopened.release is previous and plan(reopened)["openclaw_version"] == "2026.10.7"
    assert recognized_version("openclaw", "OpenClaw 2026.11.6 (06afb10)", "verified") == "2026.11.6"
    assert recognized_version("openclaw", "OpenClaw 2026.10.7 (abc1234)", "verified") == "2026.10.7"
