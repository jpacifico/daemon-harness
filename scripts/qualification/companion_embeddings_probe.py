"""Real acquisition, embedding and native supervision; synthetic owner, no host account."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'src'))

from daemons_companion.setup import embeddings
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.acquisition import _RELEASES
from daemons_companion.runtime.services import (
    MacLaunchdSchedulerBackend, WindowsTaskSchedulerBackend, render_job_definition,
    _powershell_command,
)


def make_scope(root):
    """Only fixtures use synthetic host receipts; no Hermes installation is claimed."""
    scope = InstallationScope(root / 'hosts/hermes/installations' / uuid.uuid4().hex)
    scope.profile.mkdir(parents=True)
    release = _RELEASES['hermes']
    receipt = dict(schema='daemon.companion-host-install.v1', installation_id=scope.installation_id,
                   version=release.version, revision=release.revision)
    for path in (scope.attempt / 'owner.json', scope.attempt.parent.parent / 'current.json'):
        path.write_text(json.dumps(receipt))
    scope.initialize()
    return scope


def owned_processes(package):
    if os.name == 'nt':
        script = ("$ErrorActionPreference='Stop';$root=$args[0];"
                  "@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
                  "$_.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) } | "
                  "ForEach-Object {$_.ProcessId}) | ConvertTo-Json -Compress")
        output = subprocess.check_output(_powershell_command(script, str(package)), text=True)
        data = json.loads(output) if output.strip() else []
        return data if isinstance(data, list) else [data]
    output = subprocess.check_output(['/bin/ps', '-axo', 'pid=,command='], text=True)
    return [int(row.split(None, 1)[0]) for row in output.splitlines()
            if len(row.split(None, 1)) == 2 and row.split(None, 1)[1].startswith(str(package) + '/')]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    args = parser.parse_args()
    report = {'platform': platform.platform(), 'python': platform.python_version(),
              'scope': 'synthetic owner; real model and scheduler; no Hermes/Telegram/ChatGPT',
              'checks': {}, 'timings_seconds': {}}
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = tempfile.mkdtemp(prefix='daemon-embedding-probe-')
    root = Path(raw).resolve()
    backend = job = settings = None
    cleanup_ok = False
    started = time.monotonic()
    try:
        target = 'windows' if os.name == 'nt' else 'macos'
        if os.name == 'nt':
            phase = time.monotonic()
            embeddings.require_free_port(embeddings.free_port())
            report['timings_seconds']['refused_loopback_connection'] = round(time.monotonic() - phase, 3)
            from daemons_companion.runtime.windows_job import OwnedProcessJob
            child = None
            try:
                with OwnedProcessJob() as owned:
                    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
                    owned.assign(child)
                child.wait(timeout=5)
                report['checks']['windows_job_object_cleanup'] = True
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        scope = make_scope(root)
        def downloader(url, destination, size, digest, progress):
            cached = args.cache / url.rsplit('/', 1)[1].replace(':', '-') if args.cache else None
            if cached is not None and cached.is_file():
                shutil.copyfile(cached, destination)
            else:
                embeddings.download(url, destination, size, digest, progress)
        phase = time.monotonic()
        settings = embeddings.prepare(scope, target, print, downloader=downloader)
        report['timings_seconds']['acquisition_and_probe'] = round(time.monotonic() - phase, 3)
        report['checks']['acquisition_real_vector'] = True
        phase = time.monotonic()
        reused = embeddings.prepare(scope, target, print,
            downloader=lambda *a: (_ for _ in ()).throw(AssertionError('Unexpected second download')))
        assert reused == settings
        report['timings_seconds']['verified_reuse'] = round(time.monotonic() - phase, 3)
        job = embeddings.build_service_job(scope, Path(sys.executable), SOURCE, scope.installation_id)
        backend = (WindowsTaskSchedulerBackend(job_namespace=scope.installation_id) if os.name == 'nt'
                   else MacLaunchdSchedulerBackend(root / 'agents', user_id=os.getuid(), job_namespace=scope.installation_id))
        assert backend.capture(job.code) is None, 'Disposable scheduler name unexpectedly exists'
        definition = render_job_definition(job, target)
        phase = time.monotonic()
        backend.install(job, definition)
        embeddings.wait_ready(settings['port'])
        assert backend.matches(job, definition)
        report['timings_seconds']['native_registration_and_ready'] = round(time.monotonic() - phase, 3)
        report['checks']['native_registration'] = True
        phase = time.monotonic()
        embeddings.probe(settings['port'])
        report['timings_seconds']['warm_embedding'] = round(time.monotonic() - phase, 3)
        package = embeddings.root_for(scope) / 'assets/package'
        pids = owned_processes(package)
        assert pids, 'No owned Ollama process found'
        phase = time.monotonic()
        if os.name == 'nt':
            # Kill only the owned server, not the Python supervisor. The Job
            # Object and native task stop are exercised during rollback below.
            script = ("$ErrorActionPreference='Stop';$exe=$args[0];"
                      "$p=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $exe -and $_.CommandLine -match 'serve'});"
                      "if($p.Count -ne 1){throw 'Expected one owned server'};Stop-Process -Id $p[0].ProcessId -Force")
            subprocess.run(_powershell_command(script, settings['binary']), check=True)
        else:
            subprocess.run(['/bin/launchctl', 'kill', 'SIGKILL', f'gui/{os.getuid()}/{job.code}'], check=True)
        # Wait for the former server to disappear, not just one HTTP response.
        time.sleep(1)
        embeddings.wait_ready(settings['port'])
        new_pids = owned_processes(package)
        assert new_pids and set(new_pids) != set(pids)
        report['timings_seconds']['automatic_restart_and_ready'] = round(time.monotonic() - phase, 3)
        report['checks']['automatic_restart'] = True
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            phase = time.monotonic()
            if backend is not None and job is not None:
                backend.restore(job.code, None)
                assert backend.capture(job.code) is None
            if settings is not None:
                end = time.monotonic() + 15
                while True:
                    try:
                        embeddings.require_free_port(settings['port'])
                        assert not owned_processes(embeddings.root_for(scope) / 'assets/package')
                        break
                    except (RuntimeError, AssertionError):
                        if time.monotonic() >= end:
                            raise RuntimeError('Disposable embedding process survived rollback') from None
                        time.sleep(.5)
            cleanup_ok = True
            report['checks']['native_rollback_no_orphans'] = True
            report['timings_seconds']['native_rollback'] = round(time.monotonic() - phase, 3)
        except BaseException as error:
            report['cleanup_error'] = f'{type(error).__name__}: {error}'
            if settings is not None:
                remaining = owned_processes(embeddings.root_for(scope) / 'assets/package')
                report['remaining_owned_pids'] = remaining
                # Preserve the failed cleanup result, but do not leave test
                # processes behind on a runner when investigating a failure.
                if os.name == 'nt':
                    for pid in remaining:
                        subprocess.run(['taskkill.exe', '/PID', str(pid), '/T', '/F'], check=False)
            raise
        finally:
            report['timings_seconds']['total'] = round(time.monotonic() - started, 3)
            for log in root.glob('hosts/hermes/installations/*/embeddings/*.log'):
                shutil.copyfile(log, output.parent / ('embeddings-' + log.name))
            output.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2), flush=True)
            if cleanup_ok:
                shutil.rmtree(root)
            else:
                print('Preserved disposable fixture for cleanup:', root, flush=True)


if __name__ == '__main__':
    main()
