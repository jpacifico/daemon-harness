#!/usr/bin/env python3
"""Opt-in native app distribution check, in an empty disposable destination."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Exercise the published private release download on the first install.')
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    repo = Path(__file__).resolve().parents[1]
    windows = sys.platform == 'win32'
    if sys.platform not in {'win32', 'darwin'}:
        raise SystemExit('Run on native macOS or Windows.')
    script = repo / 'apps/companion/distribution' / ('install-windows.ps1' if windows else 'install-macos.sh')
    runner = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command'] if windows else ['/bin/sh', str(script)]
    started = time.monotonic()
    result: dict[str, object] = {'platform': sys.platform, 'schema': 'daemon.companion.distribution.v1'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='daemons-app-install-') as raw:
        root = Path(raw).resolve()
        destination = root / 'Applications with spaces'
        sentinel = root / 'existing-daemon-state.txt'
        sentinel.write_text('synthetic state outside the app', encoding='utf-8')
        preserved = sentinel.read_bytes()

        def install(source: Path | None, *, expected_error: str | None = None, mac_script: Path | None = None) -> None:
            if windows:
                # Invoke a parsed script block: no execution-policy override, matching the README.
                def literal(value: Path) -> str:
                    return "'" + str(value).replace("'", "''") + "'"
                command = runner + [
                    "$ErrorActionPreference='Stop'; & ([scriptblock]::Create([IO.File]::ReadAllText(" + literal(script) + "))) "
                    + ('-Package ' + literal(source) + ' ' if source else '') + '-Destination ' + literal(destination) + ' -NoLaunch'
                ]
            else:
                command = (['/bin/sh', str(mac_script)] if mac_script else runner) + (['--package', str(source)] if source else []) + ['--destination', str(destination), '--no-launch']
            # Do not give Windows PowerShell 5.1 the parent PowerShell 7 module path.
            child_environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
            completed = subprocess.run(command, capture_output=True, text=True, timeout=240, env=child_environment)
            print(completed.stdout, end='', flush=True)
            if expected_error is None and completed.returncode:
                raise RuntimeError(completed.stderr)
            if expected_error is not None:
                if not completed.returncode or expected_error not in completed.stderr:
                    raise AssertionError('Expected refusal missing: ' + completed.stderr)
            assert sentinel.read_bytes() == preserved

        bad = root / ('altered.exe' if windows else 'altered.dmg')
        bad.write_bytes(b'not the pinned application')
        install(bad, expected_error='checksum mismatch')
        assert not destination.exists()
        result['tampered_package_rejected_before_mutation'] = True
        if windows:
            destination.mkdir()
            (destination / 'unrelated.txt').write_text('must survive', encoding='utf-8')
        else:
            destination.mkdir()
            (destination / 'Daemons Companion.app').write_text('must survive', encoding='utf-8')
        install(package, expected_error='unrecognized folder' if windows else 'unrelated item')
        unrelated = destination / ('unrelated.txt' if windows else 'Daemons Companion.app')
        assert unrelated.read_text(encoding='utf-8') == 'must survive'
        shutil.rmtree(destination)
        result['unrelated_destination_preserved'] = True
        executable = destination / ('Daemons Companion.exe' if windows else 'Daemons Companion.app/Contents/MacOS/Daemons Companion')
        try:
            install_started = time.monotonic()
            install(None if args.download else package)
            result['published_download_verified'] = args.download
            result['install_seconds'] = round(time.monotonic() - install_started, 3)
            assert executable.is_file()
            before = hashlib.sha256(executable.read_bytes()).hexdigest()
            if not windows:
                # Fault only the final app check in a test copy, after the native rename.
                # The original directory inode must come back, not merely identical bytes.
                app_directory = destination / 'Daemons Companion.app'
                original_inode = app_directory.stat().st_ino
                fault_script = root / 'fail-after-swap.sh'
                source = script.read_text(encoding='utf-8')
                check = '/usr/bin/codesign --verify --deep --strict "$daemon_target"'
                assert source.count(check) == 1
                fault_script.write_text(source.replace(check, "daemon_fail 'injected swap failure'"), encoding='utf-8')
                install(package, expected_error='injected swap failure', mac_script=fault_script)
                assert app_directory.stat().st_ino == original_inode
                assert hashlib.sha256(executable.read_bytes()).hexdigest() == before
                assert not list(destination.glob('.daemons-companion-*'))
                result['mac_swap_failure_restores_previous_app'] = True
            install(package)
            assert hashlib.sha256(executable.read_bytes()).hexdigest() == before
            result['repeat_preserves_app_and_external_state'] = True
            app_environment = {key: value for key, value in os.environ.items() if key not in {'GH_TOKEN', 'GITHUB_TOKEN'}}
            completed = subprocess.run([str(executable), '--smoke-test'], capture_output=True, text=True, timeout=180, env=app_environment)
            if completed.returncode:
                raise RuntimeError(completed.stderr)
            receipts = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{')]
            receipt = receipts[-1]
            assert receipt['status'] == 'passed'
            assert receipt['model_calls'] == receipt['telegram_calls'] == 0
            native = receipt.get('native_instances')
            if native is None:
                assert receipt['services_started'] == 0
            else:
                assert native['services_started'] == 2
                discard = receipt.get('discard')
                extra_services = 0
                if discard is not None and not windows:
                    assert discard['status'] == 'passed'
                    assert discard['native_launchd_jobs_removed'] == 4
                    assert discard['memory_exact'] and discard['reload_removed_card']
                    assert discard['filesystem_failure_and_retry']
                    assert discard['personal_services_changed'] == 0
                    extra_services = 4
                assert receipt.get('services_registered', receipt.get('services_started')) == (2 if windows else 6) + extra_services
                assert native['cleanup_verified'] and native['personal_services_changed'] == 0
                assert receipt['personal_services_started'] == 0
            result['installed_app_smoke'] = receipt
            assert sentinel.read_bytes() == preserved
        finally:
            if windows:
                # The only install touched is this run's empty, unique destination.
                uninstaller = destination / 'Uninstall Daemons Companion.exe'
                if uninstaller.exists():
                    subprocess.run([str(uninstaller), '/S', '/currentuser', '/KEEP_APP_DATA'], check=True, timeout=90)
                    for _ in range(50):
                        if not executable.exists():
                            break
                        time.sleep(0.2)
        result['status'] = 'passed'
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
