import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from daemons_companion.setup import embeddings as module
from tests.test_companion_installation_scope import scope_fixture


def synthetic_receipt(scope, target_os='macos'):
    root = module.root_for(scope)
    package = root / 'assets/package'
    package.mkdir(parents=True)
    binary = package / ('ollama.exe' if target_os == 'windows' else 'ollama')
    binary.write_bytes(b'synthetic')
    data = dict(schema=module.SCHEMA, version=module.VERSION, target_os=target_os,
                model=module.DEFAULT_MODEL, model_digest=module.DEFAULT_MODEL_DIGEST,
                installation_id=scope.installation_id, port=33333,
                files={binary.name: module.sha256(binary)})
    (root / 'ready.json').write_text(json.dumps(data))
    return data


def test_environment_bootstrap_never_freezes_personal_runtime_paths():
    result = subprocess.run([sys.executable, '-B', '-c',
        'import sys; import daemons_companion.runtime.private_environment; '
        'import daemons_companion.setup.embeddings; '
        'assert "daemon_core.paths" not in sys.modules; '
        'assert "daemon_core.semantic_atlas" not in sys.modules'],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_receipt_is_owned_and_modified_binary_is_refused(tmp_path):
    scope = scope_fixture(tmp_path)
    assert module.configuration(scope) is None
    synthetic_receipt(scope)
    config = module.configuration(scope, verify_files=True)
    Path(config['binary']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        module.configuration(scope, verify_files=True)
    receipt = module.root_for(scope) / 'ready.json'
    data = json.loads(receipt.read_text())
    data['installation_id'] = 'b' * 32
    receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='match'):
        module.client_environment(scope, '12345')


def test_voice_and_worker_share_private_embedding_endpoint(tmp_path):
    from tests.test_companion_private_setup import connection
    from daemons_companion.runtime.private_environment import private_process_environment
    scope = scope_fixture(tmp_path)
    connection(scope, tmp_path)
    synthetic_receipt(scope)
    worker = private_process_environment(scope)
    voice = private_process_environment(scope, hermes=True)
    expected = module.client_environment(scope, '12345')
    assert expected and all(worker[k] == voice[k] == v for k, v in expected.items())
    assert worker['CODEX_HOME'] != voice['CODEX_HOME']
    server = module.server_environment(module.root_for(scope), 33333)
    assert server['OLLAMA_NO_CLOUD'] == '1'
    assert server['OLLAMA_HOST'] == '127.0.0.1:33333'
    assert Path(server['OLLAMA_MODELS']).is_relative_to(scope.attempt)


@pytest.mark.parametrize('name', ['../escape', '/escape', 'x/../../escape', 'x\\escape', 'C:escape', 'x.'])
@pytest.mark.parametrize('target_os', ['macos', 'windows'])
def test_archive_paths_refused_before_extraction(tmp_path, name, target_os):
    path = tmp_path / 'archive'
    if target_os == 'windows':
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(name, b'payload')
    else:
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo(name)
            member.size = 7
            archive.addfile(member, io.BytesIO(b'payload'))
    with pytest.raises(ValueError, match='Unsafe'):
        module.extract(path, tmp_path / 'out', target_os)
    assert not (tmp_path / 'out').exists()


def test_mac_library_aliases_materialized_without_links(tmp_path):
    path = tmp_path / 'archive'
    with tarfile.open(path, 'w:gz') as archive:
        entry = tarfile.TarInfo('library.1.dylib')
        entry.size = 7
        entry.mode = 0o755
        archive.addfile(entry, io.BytesIO(b'library'))
        for name, link in [('library.0.dylib', 'library.1.dylib'), ('library.dylib', 'library.0.dylib')]:
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.SYMTYPE
            entry.linkname = link
            archive.addfile(entry)
    files = module.extract(path, tmp_path / 'out', 'macos')
    assert len(files) == 3 and len(set(files.values())) == 1
    assert all(not p.is_symlink() for p in (tmp_path / 'out').iterdir())


@pytest.mark.parametrize('vector', [None, [], [0] * 1024, [float('nan')] * 1024, [1] * 1023, [True] * 1024])
def test_readiness_rejects_invalid_vectors(monkeypatch, vector):
    monkeypatch.setattr(module, 'request', lambda port, endpoint, *a, **kw:
        {'models': [{'name': module.DEFAULT_MODEL, 'digest': module.DEFAULT_MODEL_DIGEST}]}
        if endpoint == '/api/tags' else {'embeddings': [vector]})
    with pytest.raises(RuntimeError, match='valid embedding'):
        module.probe(33333)


@pytest.fixture
def ample_disk(monkeypatch):
    """The free-space check reads the real disk; a full CI machine must not decide these tests."""
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(total=1 << 50, used=0, free=1 << 50))


def test_bad_download_never_published(tmp_path, ample_disk):
    scope = scope_fixture(tmp_path)
    with pytest.raises(RuntimeError, match='integrity'):
        module.prepare(scope, 'macos', lambda _: None,
                       downloader=lambda url, path, *args: path.write_bytes(b'bad'))
    root = module.root_for(scope)
    assert not (root / 'assets').exists()
    assert not (root / 'ready.json').exists()
    assert not list(root.glob('stage-*'))


@pytest.mark.parametrize('target_os', ['macos', 'windows'])
@pytest.mark.parametrize('failure', ['occupied-port', 'readiness'])
def test_scheduler_failure_restores_only_new_jobs(tmp_path, monkeypatch, target_os, failure):
    from daemons_companion.runtime.services import (
        BackgroundJob, CompanionBackgroundServicesAdapter, EMBEDDINGS_JOB, AFTER_TURN_JOB,
        scoped_job_code,
    )
    from daemons_companion.setup.plan import PlanStep
    from tests.test_companion_background_services import FakeScheduler
    scope = scope_fixture(tmp_path)
    synthetic_receipt(scope, target_os)
    layout = SimpleNamespace(target_os=target_os, runtime=SimpleNamespace(installation_scope=scope))
    scheduler = FakeScheduler(target_os, {'foreign': b'untouched'})
    adapter = CompanionBackgroundServicesAdapter(layout, backend=scheduler)
    jobs = tuple(BackgroundJob(scoped_job_code(code, scope.installation_id), str(tmp_path / 'python'),
        (), str(tmp_path), str(tmp_path / 'log'), True) for code in (EMBEDDINGS_JOB, AFTER_TURN_JOB))
    monkeypatch.setattr(adapter, '_validate', lambda _: jobs)
    def fail(*args):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(module, 'require_free_port', fail if failure == 'occupied-port' else lambda *a: None)
    monkeypatch.setattr(module, 'wait_ready', fail)
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    step = PlanStep('background-services', 'synthetic', ())
    adapter.snapshot(step, snapshot)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        adapter.apply(step)
        adapter.verify(step)
    adapter.rollback(step, snapshot)
    assert scheduler.state == {'foreign': b'untouched'}
    assert (module.root_for(scope) / 'ready.json').exists()
    assert len(scheduler.installs) == (0 if failure == 'occupied-port' else 2)


def test_probe_failure_keeps_verified_assets_and_stops_owned_process(tmp_path, monkeypatch, ample_disk):
    scope = scope_fixture(tmp_path)
    archive = tmp_path / 'package.tgz'
    with tarfile.open(archive, 'w:gz') as out:
        item = tarfile.TarInfo('ollama')
        item.size = 3
        out.addfile(item, io.BytesIO(b'bin'))
    blob = b'synthetic model'
    digest = hashlib.sha256(blob).hexdigest()
    manifest = json.dumps({'config': {'digest': 'sha256:' + digest, 'size': len(blob)}, 'layers': []}).encode()
    monkeypatch.setattr(module, 'DEFAULT_MODEL_DIGEST', hashlib.sha256(manifest).hexdigest())
    monkeypatch.setattr(module, 'MANIFEST_SIZE', len(manifest))
    monkeypatch.setitem(module.PACKAGES, 'macos', ('package.tgz', archive.stat().st_size, module.sha256(archive)))
    calls = []
    def download(url, destination, *args):
        calls.append(url)
        destination.write_bytes(archive.read_bytes() if url.endswith('.tgz') else manifest if '/manifests/' in url else blob)
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: process)
    stopped = []
    monkeypatch.setattr(module, 'stop_process_tree', lambda child, env: stopped.append(child))
    def failed(*args):
        raise RuntimeError('probe failed')
    monkeypatch.setattr(module, 'wait_ready', failed)
    with pytest.raises(RuntimeError, match='probe failed'):
        module.prepare(scope, 'macos', lambda _: None, downloader=download)
    root = module.root_for(scope)
    assert (root / 'assets').is_dir() and not (root / 'ready.json').exists()
    assert stopped == [process]
    monkeypatch.setattr(module, 'wait_ready', lambda *args: None)
    assert module.prepare(scope, 'macos', lambda _: None, downloader=download)['port'] >= 1024
    assert len(calls) == 3 and stopped == [process, process]
    module.prepare(scope, 'macos', lambda _: None, downloader=download)
    assert len(calls) == 3
