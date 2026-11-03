"""Private startup paths and actual imports across a simulated MAX_PATH boundary."""
import importlib
import importlib._bootstrap_external as external
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from daemons_companion.runtime import windows_python_paths as paths
from tests.companion_windows_io import WindowsPathGuard


@pytest.mark.parametrize('path,expected', [
    (r'C:\private\Lib', '\\\\?\\C:\\private\\Lib'),
    ('C:/private/Lib/../Lib', '\\\\?\\C:\\private\\Lib'),
    (r'\\server\share\Lib', '\\\\?\\UNC\\server\\share\\Lib'),
    (r'\\?\C:\private\Lib', '\\\\?\\C:\\private\\Lib'),
    ('relative', 'relative'), ('', ''), (r'C:relative', r'C:relative'),
    (r'\relative-to-drive', r'\relative-to-drive'),
])
def test_import_path_spelling(path, expected):
    assert paths.extended_import_path(path) == expected
    assert paths.extended_import_path(expected) == expected


def test_preloaded_package_import_survives_max_path(tmp_path, monkeypatch):
    name = 'daemon_path_fixture'
    suffix = '/' + name + '/metadata/_collections.py'
    lib = tmp_path / ('x' * (260 - len(str(tmp_path)) - 1 - len(suffix)))
    package = lib / name
    (package / 'metadata').mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'metadata/__init__.py').write_text('')
    leaf = package / 'metadata/_collections.py'
    leaf.write_text('value = "exact module recovered"\n')
    assert len(str(leaf)) == 260
    monkeypatch.syspath_prepend(str(lib))
    try:
        # Load the parent first, as site may already have loaded importlib.
        parent = importlib.import_module(name)
        with pytest.MonkeyPatch.context() as patch:
            guard = WindowsPathGuard(patch, lib)
            old_stat, old_open, old_listdir = external._os.stat, external._io.open_code, external._os.listdir
            patch.setattr(external._os, 'stat', lambda p, *a, **k: old_stat(guard.translate(p), *a, **k))
            patch.setattr(external._io, 'open_code', lambda p: old_open(guard.translate(p)))
            patch.setattr(external._os, 'listdir', lambda p: old_listdir(guard.translate(p)))
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(name + '.metadata._collections')
            assert guard.denied
            modules = {k: v for k, v in sys.modules.items() if k.startswith(name)}
            state = SimpleNamespace(path=[str(lib), ''], modules=modules)
            patch.setattr(paths, 'sys', state)
            patch.setattr(paths, 'os', SimpleNamespace(name='nt'))
            patch.setattr(paths, 'extended_import_path', lambda p: str(guard.io_path(Path(p))) if p else p)
            paths.enable()
            importlib.invalidate_caches()
            loaded = importlib.import_module(name + '.metadata._collections')
            assert loaded.value == 'exact module recovered'
            assert parent.__path__[0].startswith(guard.prefix)
    finally:
        for key in list(sys.modules):
            if key.startswith(name):
                del sys.modules[key]
        importlib.invalidate_caches()


def test_hook_runs_in_isolated_venv_without_daemon_package(tmp_path):
    import venv
    venv.EnvBuilder(with_pip=False).create(tmp_path / 'venv')
    python = tmp_path / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    probe = subprocess.check_output([str(python), '-I', '-B', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
    site = Path(probe)
    site.joinpath('_daemons_windows_paths.py').write_bytes(Path(paths.__file__).read_bytes())
    site.joinpath('000_daemons_paths.pth').write_text('import _daemons_windows_paths; _daemons_windows_paths.enable()\n')
    output = subprocess.check_output([str(python), '-I', '-B', '-c',
        'import sys, importlib.metadata; assert "_daemons_windows_paths" in sys.modules; print("ok")'], text=True)
    assert output.strip() == 'ok'
