"""Native fresh PARGA installation from the exact desktop payload, Mac/Windows.

Only account authentication and remote endpoints are substituted. Hermes' real
Telegram adapter, provider HTTP transport, plugins, L0, worker, scene, embeddings,
nightly entry and native jobs run. This is infrastructure evidence, not a model
quality test or proof of remote ChatGPT/Telegram access. No personal data or jobs.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform not in {'darwin', 'win32'}:
        raise ValueError('Native macOS or Windows required.')
    sys.dont_write_bytecode = True
    repo = Path(__file__).resolve().parents[2]
    package = args.package.resolve(strict=True)
    bundle = package / 'payload'
    # Load the same committed installer that is shipped in the application.
    sys.path[:0] = [str(bundle / 'src'), str(repo)]
    from daemons_companion.desktop.app import CompanionPreparationContext
    from daemons_companion.runtime.services import (
        MacLaunchdSchedulerBackend, WindowsTaskSchedulerBackend, AFTER_TURN_JOB,
        NIGHTLY_JOB, HERMES_GATEWAY_JOB, EMBEDDINGS_JOB, scoped_job_code,
        render_job_definition, build_background_jobs,
    )
    from daemons_companion.setup.plan import HostKind, ProviderKind, ProviderSelection, SetupRequest, build_setup_plan
    from daemons_companion.runtime.host_install import HostInstallAdapter, HostInstallLayout, execute_host_install, host_install_plan, installed_host
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.setup.install_session import prepare_installation
    from daemons_companion.hermes.first_install import complete_private_setup, resume_private_setup, first_exchange
    from daemons_companion.hermes.gateway import private_gateway_status, _gateway_ready
    from daemons_companion.runtime.private_environment import private_process_environment
    from daemons_companion.hermes.ready_composition import _services_layout
    from daemons_companion.hermes.composition import HermesCompositionLayout
    from daemons_companion.runtime.release import verify_current_release
    from daemons_companion.setup.transaction import public_plan_digest
    from daemon_core.after_turn_service import read_after_turn_service_status
    from daemons_companion.runtime.bootstrap import _source_io_path
    from daemon_core.installation_profile import profile_status
    from scripts.qualify_companion_hermes_e2e import FixedProvider, SYNTHETIC_MESSAGE, EXPECTED_RESPONSE, _profile_config
    from scripts.qualification.companion_telegram_fixture import TelegramFixture, TOKEN, model_completion
    target = 'windows' if os.name == 'nt' else 'macos'
    uv = package / 'bin' / ('uv.exe' if os.name == 'nt' else 'uv')
    metadata = json.loads((package.parent / 'runtime.json').read_text())
    assert hashlib.sha256(uv.read_bytes()).hexdigest() == metadata['uv_sha256']
    assert hashlib.sha256((package / 'companion-manifest.json').read_bytes()).hexdigest() == metadata['manifest_sha256']
    root = Path(tempfile.mkdtemp(prefix='daemon-alpha-', dir=os.getenv('RUNNER_TEMP'))).resolve()
    namespace = uuid.uuid4().hex
    report = {'schema': 'daemon.alpha-native.v1', 'source': metadata['revision'], 'target': target,
              'remote_telegram': False, 'real_model': False, 'oauth': False,
              'tokens': {'value': None, 'source': 'unavailable; fixed fixture is not token usage'},
              'passed': False, 'cleanup': False, 'phases': []}
    started = time.monotonic()
    scheduler = (WindowsTaskSchedulerBackend(job_namespace=namespace) if os.name == 'nt' else
                 MacLaunchdSchedulerBackend(root / 'agents', user_id=os.getuid(), job_namespace=namespace))
    jobs = [scoped_job_code(code, namespace) for code in (HERMES_GATEWAY_JOB, NIGHTLY_JOB, AFTER_TURN_JOB, EMBEDDINGS_JOB)]
    provider = FixedProvider(completion_callback=model_completion)
    telegram = TelegramFixture()
    scope = None
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    def progress(message):
        print(message, flush=True)
        report['phases'].append(message)
        save()
    try:
        provider.start()
        telegram.start()
        layout = HostInstallLayout(root / 'c', uv, metadata['uv_sha256'], namespace, target)
        execute_host_install(layout, confirmed_digest=public_plan_digest(host_install_plan(layout)),
                             adapter=HostInstallAdapter(layout, progress=progress))
        report['host_ready_seconds'] = round(time.monotonic() - started, 3)
        host = installed_host(layout)
        scope = InstallationScope.from_verified_host(layout)
        scope.initialize()
        # This file is never executed: the provider configuration uses loopback.
        codex = layout.attempt / ('synthetic-codex.exe' if os.name == 'nt' else 'synthetic-codex')
        codex.write_bytes(b'fixture; not an executable credential tool')
        for name in ('.codex', '.hermes-codex'):
            (layout.attempt / 'os-home' / name).mkdir()
        context = CompanionPreparationContext(package, layout.companion_home, bundle, uv,
            metadata['uv_sha256'], target, installation_scope=scope, root_folder_memory=True)
        selection = ProviderSelection(ProviderKind.OPENAI_COMPATIBLE_API, 'synthetic-model', f'http://127.0.0.1:{provider.port}/v1')
        plan = build_setup_plan(SetupRequest(HostKind.HERMES, 'Aster', 'feminine', selection), (host,), codex_available=True)
        prepared = prepare_installation(plan, context.install_layout(HostKind.HERMES, scope.profile))
        def configure(*a, **kw):
            import yaml
            config = yaml.safe_load(_profile_config(provider.port))
            config['platforms'] = {'telegram': {'enabled': True, 'extra': {
                'base_url': f'http://127.0.0.1:{telegram.port}/bot', 'drop_pending_on_cold_boot': True}}}
            (scope.profile / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
            (scope.profile / '.env').write_text(f'TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USERS=12345\n', encoding='utf-8')
            return {'username': 'synthetic_bot', 'bot_id': 123456789}
        owner = SimpleNamespace(layout=lambda: layout, context=context, codex_executable=lambda _: codex, daemon_installed=False)
        class UI:
            sent = False
            @contextmanager
            def activity(self, label):
                progress(label)
                yield progress
            def say(self, message):
                progress(message)
                if (message.startswith('Press Start') or message.startswith('Open https://t.me/')) and not self.sent:
                    self.sent = True
                    telegram.submit(SYNTHETIC_MESSAGE)
            def choose(self, title, *a, **kw):
                if title == 'Connect Telegram Web':
                    return 1
                if title == 'Did you receive the reply in Telegram?':
                    assert EXPECTED_RESPONSE in telegram.displayed
                    return 1
                return 0
            def field(self, *a, **kw):
                return '12345'
            def secret(self, *a):
                return TOKEN
        scheduler_name = 'WindowsTaskSchedulerBackend' if os.name == 'nt' else 'MacLaunchdSchedulerBackend'
        with (
            patch('daemons_companion.hermes.first_install.PrivateSubscriptionSetup', lambda *a, **kw: SimpleNamespace(status=lambda: SimpleNamespace(codex=True, hermes=True))),
            patch('daemons_companion.hermes.first_install.ProviderSelection', lambda *a, **kw: selection),
            patch('daemons_companion.hermes.first_install.configure_telegram', configure),
            patch('daemons_companion.hermes.first_install.' + scheduler_name, lambda *a, **kw: scheduler),
        ):
            # Do not patch readiness or first_exchange: both must pass unchanged.
            assert complete_private_setup(owner, UI(), {'host': 'hermes', 'daemon_name': 'Aster',
                'daemon_gender': 'feminine', 'provider': {'kind': 'codex_cli', 'model': 'synthetic-model'}}, prepared.public_dict())
            report['first_exchange_and_memory_seconds'] = round(time.monotonic() - telegram.submitted_at, 3)
            runtime = context.install_layout(HostKind.HERMES, scope.profile).runtime
            expected = profile_status(private_process_environment(scope), user_ids=('12345',), memory_root=runtime.memory_root)
            assert expected and expected['ok']
            worker = read_after_turn_service_status(spool_root=_source_io_path(runtime.state_root / 'after-turn-spool'),
                memory_root=_source_io_path(runtime.memory_root), expected_revision=verify_current_release(runtime).name,
                expected_profile=expected)
            assert worker['ok'], worker
            gateway = private_gateway_status(layout)
            assert _gateway_ready(gateway), gateway
            report['profile_verified_in_gateway_and_worker'] = True
            # Capture actual job definitions then restart the worker and gateway.
            from daemons_companion.hermes.gateway import private_gateway_job
            composition = HermesCompositionLayout(runtime, scope.profile, uv, metadata['uv_sha256'], target, namespace)
            background = {job.code: job for job in build_background_jobs(_services_layout(composition, '12345'))}
            worker_job = background[scoped_job_code(AFTER_TURN_JOB, namespace)]
            nightly = background[scoped_job_code(NIGHTLY_JOB, namespace)]
            gateway_job = private_gateway_job(layout, runtime, namespace)
            for job in (gateway_job, worker_job):
                scheduler.restore(job.code, None)
            for job in (worker_job, gateway_job):
                scheduler.install(job, render_job_definition(job, target))
            report['restarted_jobs'] = [worker_job.code, gateway_job.code]
            telegram.displayed.clear()
            from daemons_companion.hermes.gateway import wait_private_gateway_ready
            wait_private_gateway_ready(layout)
            resume_started = time.time()
            assert resume_private_setup(owner, UI())
            deadline = time.monotonic() + 180
            while not first_exchange(runtime.state_root / 'receipts', since=resume_started, user_id='12345') and time.monotonic() < deadline:
                time.sleep(1)
            assert first_exchange(runtime.state_root / 'receipts', since=resume_started, user_id='12345')
            report['restart_and_resume_verified'] = True
            scheduler.restore(worker_job.code, None)
            # Nightly uses the same actual bootstrap and subprocesses as its job.
            night = subprocess.run([nightly.executable, *nightly.arguments], cwd=nightly.working_directory,
                capture_output=True, text=True, timeout=180)
            report['nightly'] = {'exit': night.returncode, 'stdout': night.stdout[-8000:], 'stderr': night.stderr[-8000:]}
            assert night.returncode == 0, report['nightly']
            root_io = _source_io_path(runtime.memory_root)
            assert list((root_io / 'users/12345/scenes').rglob('scene_model.md'))
            assert list((root_io / 'levels/L0_ground_zero').rglob('*.md'))
            report['scene_and_l0_verified'] = True
            report['receipts'] = {p.name: p.read_text(encoding='utf-8') for p in (runtime.state_root / 'receipts').glob('*.jsonl')}
            assert not telegram.unknown, telegram.unknown
            report['passed'] = True
    except Exception as error:
        import traceback
        report['error'] = str(error)
        report['traceback'] = traceback.format_exc()
        if scope:
            report['logs'] = {str(p.relative_to(scope.attempt)): p.read_text(encoding='utf-8', errors='replace')[-16000:]
                for p in [scope.attempt / 'installation-diagnostic.json', *list((scope.home / 'data/state').glob('*.log')),
                          *list((scope.profile / 'logs').glob('*.log'))] if p.is_file()}
    finally:
        cleanup_errors = []
        for code in jobs:
            try:
                if scheduler.capture(code) is not None:
                    scheduler.restore(code, None)
            except Exception as error:
                cleanup_errors.append(str(error))
        report['cleanup'] = not cleanup_errors and all(scheduler.capture(code) is None for code in jobs)
        report['cleanup_errors'] = cleanup_errors
        report['delivery_seconds'] = telegram.delivery_seconds
        report['provider_requests'] = len(provider.requests)
        report['provider_systems'] = [str(m.get('content', ''))[:140] for r in provider.requests for m in r.get('messages', []) if m.get('role') == 'system']
        report['native_telegram_adapter'] = bool(telegram.methods)
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        telegram.stop()
        provider.stop()
        save()
        if report['cleanup']:
            shutil.rmtree(_source_io_path(root))
    return 0 if report['passed'] and report['cleanup'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
