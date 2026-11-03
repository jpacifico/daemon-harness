from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / "apps/companion/macos/main.swift"
MAC_BUILD = ROOT / "apps/companion/macos/build.sh"
WINDOWS = ROOT / "apps/companion/windows/DaemonHarnessCompanion.ps1"
MAC_PACKAGER = ROOT / "apps/companion/macos/build-app.sh"
WINDOWS_PACKAGER = ROOT / "apps/companion/windows/Build-CompanionPackage.ps1"


def test_macos_shell_is_a_thin_locked_facade() -> None:
    source = MAC.read_text(encoding="utf-8")
    assert "AppKit" in source
    assert '"-m", "daemons_companion.desktop.app", "request"' in source
    assert "standardInput" in source and "JSONSerialization" in source
    assert "The install action is not enabled in this preview." in source
    assert "installButton.isEnabled = false" in source
    assert "I have reviewed and accept these exact changes." in source
    assert "Hermes (recommended)" in source
    assert "Install Hermes" not in source
    assert "Install OpenClaw" not in source
    assert "COMPANION_MACOS_SMOKE_OK" in source
    assert "COMPANION_MACOS_PACKAGED_BACKEND_OK" in source
    assert '"action": "prepare"' in source
    assert '"DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"' in source
    assert "verifyPackage(at: resources)" in source
    assert '"--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress"' in source
    assert 'uvEnvironment["UV_CACHE_DIR"] = cacheRoot.path' in source
    assert '?? "/usr/bin/python3"' not in source
    builder = MAC_BUILD.read_text(encoding="utf-8")
    assert "swiftc" in builder
    assert "-framework AppKit" in builder


def test_windows_shell_is_a_thin_locked_facade() -> None:
    source = WINDOWS.read_text(encoding="utf-8")
    assert "System.Windows.Forms" in source
    assert '$start.Arguments = "-m daemons_companion.desktop.app request"' in source
    assert "$process.StandardInput.Write($json)" in source
    assert "$process.WaitForExit(12000)" in source
    assert "$installButton.Enabled = $false" in source
    assert "I have reviewed and accept these exact changes." in source
    assert "Hermes (recommended)" in source
    assert "Install-Hermes" not in source
    assert "Install-OpenClaw" not in source
    assert "COMPANION_WINDOWS_SMOKE_OK" in source
    assert "COMPANION_WINDOWS_PACKAGED_BACKEND_OK" in source
    assert 'action = "prepare"' in source
    assert "DAEMON_HARNESS_COMPANION_BUNDLE_ROOT" in source
    assert "Test-CompanionPackage $PSScriptRoot" in source
    assert '"--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress"' in source
    assert "UV_CACHE_DIR = $cacheRoot" in source
    assert '$python = "python.exe"' not in source


def test_shells_do_not_collect_or_forward_secrets() -> None:
    combined = MAC.read_text(encoding="utf-8") + WINDOWS.read_text(encoding="utf-8")
    for forbidden in ("api_key", "password", "secret_value", "credential_value"):
        assert forbidden not in combined.lower()
    assert "credential_available" in combined


def test_both_shells_expose_host_setup_review_without_enabling_installation() -> None:
    for path in (MAC, WINDOWS):
        source = path.read_text(encoding="utf-8")
        assert "Review host setup" in source
        assert "host_setup" in source
        assert "No download or installation has started." in source
        assert "This preview cannot install it yet." in source
    assert "foreach ($host in" not in WINDOWS.read_text(encoding="utf-8")


def test_readable_review_with_opt_in_exact_details() -> None:
    for path in (MAC, WINDOWS):
        source = path.read_text(encoding="utf-8")
        assert "Technical details" in source
        assert "review_text" in source
        assert "Review ready. This preview cannot install yet." in source
    mac = MAC.read_text(encoding="utf-8")
    assert "self.planView.string = summary" in mac
    assert "detailed ? reviewDetails : reviewSummary" in mac
    assert 'reviewDetails = ""' in mac
    windows = WINDOWS.read_text(encoding="utf-8")
    assert "$planView.Text = $script:ReviewSummary" in windows
    assert "$script:ReviewDetails = $response | ConvertTo-Json" in windows
    assert "Reset-Review" in windows


def test_portable_packagers_require_verified_uv_and_tracked_payload() -> None:
    mac = MAC_PACKAGER.read_text(encoding="utf-8")
    windows = WINDOWS_PACKAGER.read_text(encoding="utf-8")
    assert "uv 0.12.16" in mac and "uv SHA-256 mismatch" in mac
    assert "git -C \"$REPO_ROOT\" archive" in mac
    assert "daemons_companion.desktop.package create" in mac
    assert 'codesign --force --sign - --timestamp=none "$APP"' in mac
    assert 'codesign --verify --deep --strict' in mac
    assert "--deep --sign" not in mac
    assert "uv 0.12.16" in windows and "uv SHA-256 mismatch" in windows
    assert "git -C $repo archive" in windows
    assert "daemons_companion.desktop.package create" in windows
    assert "MakeAppx" not in windows


def test_macos_default_entrypoint_rejects_missing_terminal_without_gui(tmp_path):
    import subprocess
    import sys
    import pytest
    if sys.platform != "darwin":
        pytest.skip("Native macOS entrypoint test")
    binary = tmp_path / "DaemonHarnessCompanion"
    subprocess.run([str(MAC_BUILD), str(binary)], check=True, capture_output=True, timeout=120)
    for arguments in ([], ["--terminal"]):
        result = subprocess.run([str(binary), *arguments], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 2
        assert "requires an interactive terminal" in result.stderr
        assert "Preparing DAEMONS" not in result.stdout
