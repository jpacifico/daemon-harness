"""Minimal controller dependency, installed only inside the owned host attempt."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile

from daemons_companion.runtime.bootstrap import clean_environment, run, _source_io_path
from daemons_companion.runtime.installation_scope import InstallationScope


def _files(root):
    root = _source_io_path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('Private setup library paths must be direct.')
        if path.is_file() and path.name != 'daemons-libraries.json':
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def ensure_setup_dependencies(layout, bundle: Path, progress, *, runner=run):
    scope = InstallationScope.from_verified_host(layout)
    scope.validate_home(scope.home)
    lock = bundle / 'requirements-companion.lock'
    if lock.is_symlink() or not lock.is_file() or lock.stat().st_size > 65536:
        raise ValueError('Verified setup dependency lock is missing.')
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    parent = layout.attempt / 'setup-libraries'
    if parent.resolve() != parent or parent.is_symlink():
        raise ValueError('Private setup library directory must be direct.')
    parent.mkdir(mode=0o700, exist_ok=True)
    destination = parent / digest
    if not destination.exists():
        progress('Preparing the private setup library...')
        with tempfile.TemporaryDirectory(prefix='stage-', dir=parent) as raw:
            stage = Path(raw) / 'library'
            environment = clean_environment(layout.attempt)
            runner([str(layout.uv_executable), 'pip', 'sync', '--require-hashes', '--no-build',
                    '--no-config', '--index-url', 'https://pypi.org/simple', '--python', str(layout.python),
                    '--target', str(stage), str(lock)], environment, timeout=300)
            if not (stage / 'yaml/__init__.py').is_file():
                raise RuntimeError('The private setup library did not install.')
            (stage / 'daemons-libraries.json').write_text(json.dumps(_files(stage)), encoding='utf-8')
            if destination.exists() or destination.is_symlink():
                raise RuntimeError('Private setup library destination became occupied.')
            os.rename(stage, destination)
    receipt = destination / 'daemons-libraries.json'
    if (destination.resolve() != destination or receipt.is_symlink()
            or not receipt.is_file() or receipt.stat().st_size > 262144):
        raise ValueError('Private setup library receipt is invalid.')
    if json.loads(receipt.read_text(encoding='utf-8')) != _files(destination):
        raise ValueError('Private setup library changed; execution refused.')
    import_path = str(_source_io_path(destination))
    if import_path not in sys.path:
        sys.path.insert(0, import_path)
        importlib.invalidate_caches()
    return destination
