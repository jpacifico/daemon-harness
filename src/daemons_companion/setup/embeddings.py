"""Pinned local embeddings owned by one private installation, never a global app."""
from __future__ import annotations

import hashlib
import errno
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile

from daemons_companion.runtime.bootstrap import (
    _source_io_path as io_path, clean_environment, private_home_environment,
    stop_process_tree,
)
from daemons_companion.runtime.acquisition import open_with_retry
from daemons_companion.runtime.installation_scope import InstallationScope
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.embedding_defaults import DEFAULT_MODEL, DEFAULT_MODEL_DIGEST

VERSION = '0.40.0'
PACKAGES = {
    'macos': ('ollama-darwin.tgz', 167494179,
              'b490b4925a95c5f3dfcd889e566cf3dcd727848d59057fb00b03f1d6630326dc'),
    'windows': ('ollama-windows-amd64.zip', 1468064949,
                '3623e256762ca89bd6fa99b0cc4106401919ce9df926411673e632e3ea287bb5'),
}
SCHEMA = 'daemon.private-embeddings.v1'
REGISTRY = 'https://registry.ollama.ai/v2/library/qwen3-embedding'
MANIFEST_SIZE = 530
MAX_EXPANDED = 8 * 1024**3
MAX_FILES = 20000


def sha256(path):
    checksum = hashlib.sha256()
    with io_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def direct(path):
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError('Embedding paths must stay inside a direct private installation.')
    return path


def root_for(scope):
    scope.validate_home(scope.home)
    return direct(scope.attempt / 'embeddings')


def download(url, destination, size, digest, progress=lambda _: None):
    deadline = time.monotonic() + 1800
    checksum = hashlib.sha256()
    count = 0
    shown = -1
    with open_with_retry(lambda: urlopen(Request(url, headers={'Accept-Encoding': 'identity'}), timeout=30)) as response, io_path(destination).open('xb') as stream:
        if not response.geturl().startswith('https://'):
            raise RuntimeError('Embedding downloads require HTTPS.')
        while chunk := response.read1(1024**2):
            count += len(chunk)
            if count > size or time.monotonic() > deadline:
                raise RuntimeError('Embedding download exceeded its bounds.')
            checksum.update(chunk)
            stream.write(chunk)
            percent = count * 100 // size
            if percent // 5 != shown:
                progress(f'Downloading local memory search: {percent}%')
                shown = percent // 5
    if count != size or checksum.hexdigest() != digest:
        raise RuntimeError('Embedding download integrity check failed.')


def relative_name(name):
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or '..' in path.parts
            or '\\' in name or ':' in name or '\0' in name
            or any(p.endswith((' ', '.')) for p in path.parts)):
        raise ValueError('Unsafe embedding archive path.')
    return path.as_posix()


def extract(archive, destination, target_os):
    """Materialize verified archives without links or platform metadata writes."""
    if destination.exists():
        raise ValueError('Embedding extraction destination is occupied.')
    source = zipfile.ZipFile(io_path(archive)) if target_os == 'windows' else tarfile.open(io_path(archive), 'r:gz')
    with source:
        members = source.infolist() if target_os == 'windows' else source.getmembers()
        if len(members) > MAX_FILES:
            raise ValueError('Embedding archive contains too many entries.')
        entries = {}
        folded = set()
        total = 0
        for member in members:
            name = relative_name(member.filename if target_os == 'windows' else member.name)
            size = member.file_size if target_os == 'windows' else member.size
            mode = member.external_attr >> 16 if target_os == 'windows' else member.mode
            kind = ('dir' if member.is_dir() else 'file') if target_os == 'windows' else (
                'dir' if member.isdir() else 'file' if member.isfile() else 'link' if member.issym() else 'invalid')
            if (name.casefold() in folded or size < 0 or kind == 'invalid'
                    or (target_os == 'windows' and stat.S_ISLNK(mode))):
                raise ValueError('Unsafe or duplicate embedding archive entry.')
            total += size
            if total > MAX_EXPANDED:
                raise ValueError('Embedding archive expansion limit exceeded.')
            folded.add(name.casefold())
            entries[name] = (member, kind, mode)
        # Upstream macOS dylib aliases are expanded to regular files. Never
        # follow an archive-provided link on disk, even a seemingly safe one.
        def actual(name, seen=()):
            if name in seen or name not in entries:
                raise ValueError('Invalid embedding library alias.')
            member, kind, mode = entries[name]
            if kind == 'link':
                target = relative_name((PurePosixPath(name).parent / member.linkname).as_posix())
                return actual(target, (*seen, name))
            if kind != 'file':
                raise ValueError('Embedding alias must refer to a regular file.')
            return member, mode
        destination.mkdir(mode=0o700)
        result = {}
        written = 0
        for name, (_, kind, _) in entries.items():
            target = io_path(destination / name)
            if kind == 'dir':
                target.mkdir(parents=True, exist_ok=True)
                continue
            member, mode = actual(name)
            written += member.file_size if target_os == 'windows' else member.size
            if written > MAX_EXPANDED:
                raise ValueError('Expanded embedding aliases exceed bounds.')
            target.parent.mkdir(parents=True, exist_ok=True)
            with (source.open(member) if target_os == 'windows' else source.extractfile(member)) as incoming, target.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024**2)
            target.chmod(0o755 if mode & 0o111 else 0o644)
            result[name] = sha256(target)
    return result


def configuration(scope, *, required=False, verify_files=False):
    root = root_for(scope)
    receipt = direct(root / 'ready.json')
    if not receipt.exists():
        if required:
            raise RuntimeError('Local memory search has not been prepared.')
        return None
    if not receipt.is_file() or receipt.stat().st_size > 4 * 1024**2:
        raise ValueError('Invalid embedding receipt.')
    data = json.loads(receipt.read_text())
    if (data.get('schema') != SCHEMA or data.get('version') != VERSION
            or data.get('model_digest') != DEFAULT_MODEL_DIGEST
            or data.get('model') != DEFAULT_MODEL or data.get('target_os') not in PACKAGES
            or type(data.get('port')) is not int or not 1024 <= data['port'] <= 65535
            or data.get('installation_id') != scope.installation_id):
        raise ValueError('Embedding receipt does not match this installation.')
    package = direct(root / 'assets/package')
    binary = direct(package / ('ollama.exe' if data['target_os'] == 'windows' else 'ollama'))
    files = data.get('files')
    if not isinstance(files, dict) or binary.name not in files or not binary.is_file():
        raise ValueError('Verified embedding executable is missing.')
    if verify_files:
        for name, digest in files.items():
            path = direct(package / relative_name(name))
            if not path.is_file() or sha256(path) != digest:
                raise ValueError('Private embedding package changed; execution refused.')
    data['binary'] = str(binary)
    return data


def client_environment(scope, user_id):
    data = configuration(scope)
    if data is None:
        return {}
    from daemon_core.installation_profile import EMBEDDING_SELECTION
    return {**EMBEDDING_SELECTION,
        'DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS': str(user_id),
        'DAEMON_CORE_ATLAS_EMBEDDING_URL': f"http://127.0.0.1:{data['port']}",
        'DAEMON_CORE_ATLAS_EMBEDDING_MODEL': DEFAULT_MODEL,
        'DAEMON_CORE_ATLAS_EMBEDDING_CACHE': str(io_path(scope.home / 'data/state/atlas.sqlite3'))}


def server_environment(root, port):
    environment = clean_environment(root)
    environment.update(private_home_environment(root / 'home'))
    environment.update(OLLAMA_HOST=f'127.0.0.1:{port}', OLLAMA_MODELS=str(io_path(root / 'assets/models')),
        OLLAMA_NO_CLOUD='1', OLLAMA_KEEP_ALIVE='-1', OLLAMA_MAX_LOADED_MODELS='1',
        OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_QUEUE='16')
    return environment


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def require_free_port(port):
    with socket.socket() as sock:
        # Windows may need about two seconds to report a refused loopback
        # connection. A one-second timeout yields WSAEWOULDBLOCK, not evidence
        # that another process owns the endpoint.
        sock.settimeout(5)
        result = sock.connect_ex(('127.0.0.1', port))
        # A recently stopped server leaves TIME_WAIT sockets. Binding without
        # reuse would incorrectly reject its restart even with no listener.
        if result not in (errno.ECONNREFUSED, 10061):
            raise RuntimeError(f'The private memory-search port is unavailable (socket result {result}); no existing service was changed.')


def request(port, endpoint, body=None, *, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(f'http://127.0.0.1:{port}{endpoint}', data=data,
                        headers={'Content-Type': 'application/json'}), timeout=timeout) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError('Embedding readiness response exceeded bounds.')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('Invalid embedding readiness response.')
    return result


def probe(port, *, deadline=None):
    def remaining():
        if deadline is None:
            return 60
        return max(.01, min(60, deadline - time.monotonic()))
    tags = request(port, '/api/tags', timeout=remaining())
    models = tags.get('models')
    if not isinstance(models, list) or not any(isinstance(m, dict)
            and m.get('name') == DEFAULT_MODEL and m.get('digest') == DEFAULT_MODEL_DIGEST
            for m in models):
        raise RuntimeError('The pinned local embedding model is not ready.')
    vectors = request(port, '/api/embed', {'model': DEFAULT_MODEL, 'input': ['Local memory search readiness probe.']}, timeout=remaining()).get('embeddings')
    if (not isinstance(vectors, list) or len(vectors) != 1 or not isinstance(vectors[0], list)
            or len(vectors[0]) != 1024
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in vectors[0])
            or not any(v != 0 for v in vectors[0])):
        raise RuntimeError('Local memory search did not return a valid embedding.')


def wait_ready(port, process=None, timeout=90):
    end = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise RuntimeError('The private embedding process stopped before readiness.')
        try:
            probe(port, deadline=end)
            if process is not None and process.poll() is not None:
                raise RuntimeError('The private embedding process stopped during readiness.')
            return
        except (OSError, ValueError, RuntimeError):
            if time.monotonic() >= end:
                raise RuntimeError('Local memory search readiness timed out.') from None
            time.sleep(.5)


def prepare(scope, target_os, progress, *, downloader=download):
    """Acquire assets before the service transaction; preserve them for retry."""
    root = root_for(scope)
    root.mkdir(mode=0o700, exist_ok=True)
    with memory_file_lock(root / 'prepare.lock'):
        existing = configuration(scope, verify_files=True)
        if existing:
            if existing['target_os'] != target_os:
                raise ValueError('Private embeddings target another OS.')
            verify_model_files(root)
            return existing
        assets = direct(root / 'assets')
        if not assets.exists():
            name, size, digest = PACKAGES[target_os]
            if shutil.disk_usage(root).free < size + MAX_EXPANDED + 1024**3:
                raise RuntimeError('Local memory search needs 11 GB of temporary free disk space.')
            with tempfile.TemporaryDirectory(prefix='stage-', dir=root) as raw:
                stage = Path(raw)
                payload = stage / 'assets'
                payload.mkdir()
                archive = stage / name
                progress(f'Downloading and verifying Ollama {VERSION} for local memory search...')
                downloader(f'https://github.com/ollama/ollama/releases/download/v{VERSION}/{name}', archive, size, digest, progress)
                if archive.stat().st_size != size or sha256(archive) != digest:
                    raise RuntimeError('Embedding archive integrity check failed.')
                files = extract(archive, payload / 'package', target_os)
                models = payload / 'models'
                manifest = models / 'manifests/registry.ollama.ai/library/qwen3-embedding/0.6b'
                io_path(manifest.parent).mkdir(parents=True)
                downloader(REGISTRY + '/manifests/0.6b', manifest, MANIFEST_SIZE, DEFAULT_MODEL_DIGEST, progress)
                if manifest.stat().st_size != MANIFEST_SIZE or sha256(manifest) != DEFAULT_MODEL_DIGEST:
                    raise RuntimeError('Embedding model manifest integrity check failed.')
                model = json.loads(manifest.read_text())
                blobs = [model['config'], *model['layers']]
                for blob in blobs:
                    digest = blob['digest'].removeprefix('sha256:')
                    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or not 0 < blob['size'] < 2 * 1024**3:
                        raise ValueError('Invalid pinned model blob.')
                    destination = models / 'blobs' / ('sha256-' + digest)
                    destination.parent.mkdir(exist_ok=True)
                    progress('Downloading and verifying the local memory-search model...')
                    downloader(REGISTRY + '/blobs/sha256:' + digest, destination, blob['size'], digest, progress)
                    if destination.stat().st_size != blob['size'] or sha256(destination) != digest:
                        raise RuntimeError('Embedding model blob integrity check failed.')
                for folder in ('home', 'temp'):
                    (root / folder).mkdir(mode=0o700, exist_ok=True)
                # Publish assets before starting: Ollama must never depend on a
                # staging path which disappears after an apparently successful check.
                atomic_write_text(payload / 'manifest.json', json.dumps({'target_os': target_os, 'files': files}) + '\n')
                os.rename(payload, assets)
        asset_receipt = direct(assets / 'manifest.json')
        asset_data = json.loads(asset_receipt.read_text())
        if asset_data['target_os'] != target_os:
            raise ValueError('Private embedding assets target another OS.')
        files = asset_data['files']
        for name, digest in files.items():
            if sha256(direct(assets / 'package' / relative_name(name))) != digest:
                raise ValueError('Private embedding package changed.')
        verify_model_files(root)
        port = free_port()
        binary = root / 'assets/package' / ('ollama.exe' if target_os == 'windows' else 'ollama')
        settings = {'schema': SCHEMA, 'version': VERSION, 'target_os': target_os,
            'model': DEFAULT_MODEL, 'model_digest': DEFAULT_MODEL_DIGEST,
            'installation_id': scope.installation_id, 'port': port, 'files': files}
        environment = server_environment(root, port)
        progress('Starting and checking local memory search...')
        with (root / 'preparation.log').open('ab') as log:
            process = subprocess.Popen([str(binary), 'serve'], env=environment, cwd=binary.parent,
                stdout=log, stderr=log, **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                    if os.name == 'nt' else {'start_new_session': True}))
            try:
                wait_ready(port, process)
            finally:
                stop_process_tree(process, environment)
        atomic_write_text(root / 'ready.json', json.dumps(settings, sort_keys=True) + '\n')
        return configuration(scope, required=True)


def verify_model_files(root):
    models = direct(root / 'assets/models')
    manifest = direct(models / 'manifests/registry.ollama.ai/library/qwen3-embedding/0.6b')
    if sha256(manifest) != DEFAULT_MODEL_DIGEST:
        raise ValueError('Private embedding manifest changed.')
    data = json.loads(manifest.read_text())
    for blob in (data['config'], *data['layers']):
        path = direct(models / 'blobs' / blob['digest'].replace(':', '-'))
        if path.stat().st_size != blob['size'] or sha256(path) != blob['digest'].split(':')[1]:
            raise ValueError('Private embedding model changed.')


def build_service_job(scope, python, release, namespace):
    from daemons_companion.runtime.services import BackgroundJob, EMBEDDINGS_JOB, scoped_job_code
    scope.validate_namespace(namespace)
    configuration(scope, required=True)
    command = ('import sys;sys.dont_write_bytecode=True;'
        f'sys.stdout=sys.stderr=open({str(io_path(root_for(scope) / "supervisor.log"))!r},"a",buffering=1,encoding="utf-8");'
        f'sys.path.insert(0,{str(io_path(release / "src"))!r});'
        'from daemons_companion.setup.embeddings import serve;'
        f'serve({str(scope.attempt)!r})')
    return BackgroundJob(scoped_job_code(EMBEDDINGS_JOB, namespace), str(python),
        ('-B', '-c', command), str(release), str(root_for(scope) / 'supervisor.log'), True)


def serve(attempt):
    """Native scheduler entry; supervise only children created by this service."""
    scope = InstallationScope(Path(attempt))
    data = configuration(scope, required=True, verify_files=True)
    root = root_for(scope)
    environment = server_environment(root, data['port'])
    verify_model_files(root)
    if os.name != 'nt':
        # launchd supervises the server directly. Keep its process group so
        # bootout also terminates model runners, including after forced exits.
        require_free_port(data['port'])
        os.chdir(Path(data['binary']).parent)
        os.execve(data['binary'], [data['binary'], 'serve'], environment)
    from daemons_companion.runtime.windows_job import OwnedProcessJob
    stopped = False
    child = None
    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True
        if child is not None and child.poll() is None:
            stop_process_tree(child, environment)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (root / 'service.log').open('ab') as log:
        while not stopped:
            require_free_port(data['port'])
            with OwnedProcessJob() as owned:
                child = subprocess.Popen([data['binary'], 'serve'], cwd=Path(data['binary']).parent,
                    env=environment, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                try:
                    owned.assign(child)
                    while child.poll() is None and not stopped:
                        time.sleep(.25)
                finally:
                    if child.poll() is None:
                        stop_process_tree(child, environment)
            if not stopped:
                time.sleep(2)
