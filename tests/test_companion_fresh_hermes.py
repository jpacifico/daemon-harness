from __future__ import annotations

import io
from pathlib import Path
import tarfile

import pytest

from scripts.qualify_companion_fresh_hermes import clean_environment, extract_source, source_unchanged


def archive(tmp_path, entries):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as stream:
        for name, kind, body in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = len(body) if kind == tarfile.REGTYPE else 0
            entry.linkname = "/outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            stream.addfile(entry, io.BytesIO(body) if entry.isfile() else None)
    return path


def test_exact_sources_and_existing_destination_preserved(tmp_path):
    source = archive(tmp_path, [("host", tarfile.DIRTYPE, b""), ("host/main.py", tarfile.REGTYPE, b"original")])
    destination = tmp_path / "output"
    hashes = extract_source(source, destination, "host")
    assert source_unchanged(destination, hashes)
    with pytest.raises(ValueError, match="absent"):
        extract_source(source, destination, "host")
    (destination / "host/main.py").write_bytes(b"changed")
    assert not source_unchanged(destination, hashes)


@pytest.mark.parametrize("name,kind", [
    ("/outside", tarfile.REGTYPE), ("host/../../outside", tarfile.REGTYPE),
    ("other/main.py", tarfile.REGTYPE), ("host\\outside", tarfile.REGTYPE),
    ("host/c:outside", tarfile.REGTYPE), ("host/link", tarfile.SYMTYPE),
    ("host/link", tarfile.LNKTYPE), ("host/pipe", tarfile.FIFOTYPE),
    ("host/device", tarfile.CHRTYPE),
])
def test_unsafe_archive_refused_before_writing(tmp_path, name, kind):
    source = archive(tmp_path, [(name, kind, b"test")])
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="Unsafe"):
        extract_source(source, destination, "host")
    assert not destination.exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("names", [("host/file", "host/file"), ("host/File", "host/file")])
def test_duplicate_and_case_alias_refused(tmp_path, names):
    source = archive(tmp_path, [(name, tarfile.REGTYPE, b"test") for name in names])
    with pytest.raises(ValueError, match="duplicate"):
        extract_source(source, tmp_path / "output", "host")


def test_environment_does_not_borrow_python_or_credentials(tmp_path, monkeypatch):
    for name in ("UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTHONPATH", "VIRTUAL_ENV", "OPENAI_API_KEY", "CODEX_HOME", "HERMES_HOME"):
        monkeypatch.setenv(name, "must-not-be-inherited")
    environment = clean_environment(tmp_path)
    assert "must-not-be-inherited" not in environment.values()
    assert Path(environment["HERMES_HOME"]).is_relative_to(tmp_path)
    assert Path(environment["UV_CACHE_DIR"]).is_relative_to(tmp_path)
    assert Path(environment["UV_PYTHON_INSTALL_DIR"]).is_relative_to(tmp_path)


@pytest.mark.parametrize('source,expected', [
    ('C:/Users/Test/source/file.ts', r'\\?\C:\Users\Test\source\file.ts'),
    (r'\\server\share\source\file.ts', r'\\?\UNC\server\share\source\file.ts'),
    (r'\\?\C:\source\file.ts', r'\\?\C:\source\file.ts'),
    ('C:/source/nested/../file.ts', r'\\?\C:\source\file.ts'),
])
def test_windows_source_paths_use_extended_namespace(source, expected):
    from daemons_companion.runtime.bootstrap import _windows_extended_path
    assert _windows_extended_path(source) == expected


@pytest.mark.parametrize('source', ['relative/file.ts', r'C:relative\file.ts', r'\rooted\file.ts'])
def test_extended_windows_paths_require_explicit_absolute_root(source):
    from daemons_companion.runtime.bootstrap import _windows_extended_path
    with pytest.raises(ValueError, match='absolute'):
        _windows_extended_path(source)


def test_source_extraction_and_hash_verification_beyond_max_path(tmp_path):
    from daemons_companion.runtime.bootstrap import _source_io_path
    member = 'host/apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/input-requests.approval-timeout.test.ts'
    source = archive(tmp_path, [(member, tarfile.REGTYPE, b'original upstream bytes')])
    destination = tmp_path / ('private-' + 'a' * 64) / ('attempt-' + 'b' * 64) / 'source'
    _source_io_path(destination.parent).mkdir(parents=True)
    full = destination / member
    assert len(str(full)) > 260
    hashes = extract_source(source, destination, 'host')
    assert _source_io_path(full).read_bytes() == b'original upstream bytes'
    assert source_unchanged(destination, hashes)
    _source_io_path(full).write_bytes(b'altered')
    assert not source_unchanged(destination, hashes)


@pytest.mark.parametrize('windows_root', [
    r'\\?\C:\Users\Test User\private\source',
    r'\\?\UNC\server\share\private\source',
])
def test_directory_metadata_uses_native_separators_after_extraction(tmp_path, monkeypatch, windows_root):
    """Replay tarfile's real fixup loop against strict Windows path semantics.

    Mac permits mixed separators in a different filename; extended Win32 paths
    do not normalize '/'. The filesystem guard models that rule, not a native
    Windows execution. File extraction and integrity checks still run on disk.
    """
    import ntpath
    import os
    import stat
    from types import SimpleNamespace
    from daemons_companion.runtime import bootstrap

    source = archive(tmp_path, [
        ('host', tarfile.DIRTYPE, b''),
        ('host/assets', tarfile.DIRTYPE, b''),
        ('host/assets/oauth', tarfile.DIRTYPE, b''),
        ('host/assets/oauth/client.json', tarfile.REGTYPE, b'original bytes'),
    ])
    real_extractall = tarfile.TarFile.extractall
    checked = []

    def strict_directory(path, *args, **kwargs):
        assert path.startswith('\\\\?\\')
        if '/' in path:
            error = OSError(22, 'WinError 123: invalid name', path)
            error.winerror = 123
            raise error
        checked.append(path)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)

    def extract_with_windows_fixups(stream, destination, *, members, filter):
        members = list(members)
        assert filter == 'data'  # The fix must preserve the standard safety filter.
        win_os = SimpleNamespace(**vars(os))
        win_os.name, win_os.path, win_os.sep = 'nt', ntpath, '\\'
        win_os.lstat = strict_directory
        with monkeypatch.context() as context:
            context.setattr(tarfile, 'os', win_os)
            context.setattr(stream, '_extract_one', lambda *a, **kw: None)
            for method in ('chown', 'chmod', 'utime'):
                context.setattr(stream, method, lambda member, path, *a, **kw: strict_directory(path))
            real_extractall(stream, windows_root, members=members, filter=filter)
        # Exercise actual file writes as well, using this test host's separators.
        local = [member.replace(name=os.path.normpath(member.name.replace('\\', '/')), deep=False)
                 for member in members]
        real_extractall(stream, destination, members=local, filter=filter)

    native_paths = SimpleNamespace(**vars(os))
    native_paths.path = ntpath
    monkeypatch.setattr(bootstrap, 'os', native_paths)
    monkeypatch.setattr(tarfile.TarFile, 'extractall', extract_with_windows_fixups)
    destination = tmp_path / 'output'
    hashes = bootstrap.extract_source(source, destination, 'host')
    assert checked and any(path.endswith(r'host\assets\oauth') for path in checked)
    assert set(hashes) == {'host/assets/oauth/client.json'}
    assert (destination / 'host/assets/oauth/client.json').read_bytes() == b'original bytes'
    assert bootstrap.source_unchanged(destination, hashes)
