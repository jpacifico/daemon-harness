"""Regression of the temporary continuity_decision filename, without a real bot."""
import os
import tempfile
from pathlib import Path

import pytest

from daemons_companion.runtime import release as runtime
from daemons_companion.runtime import environment
from tests.companion_windows_io import WindowsPathGuard
from tests.test_companion_runtime import _bundle


@pytest.fixture
def short_root():
    with tempfile.TemporaryDirectory(prefix='daemon-path-') as raw:
        yield Path(raw).resolve()


def bundle_with_long_name(tmp_path):
    source = _bundle(tmp_path / 'source')
    relative = 'benchmarks/productivity_v1/checkers/continuity_decision.py'
    path = source / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b'# synthetic benchmark, not an executed module\n')
    return source, relative


@pytest.mark.skipif(os.name == 'nt', reason='Explicit POSIX simulation; native case below runs on Windows')
@pytest.mark.parametrize('extended', [False, True])
def test_atomic_copy_verification_and_removal_across_max_path(short_root, monkeypatch, extended):
    tmp_path = short_root
    source, relative = bundle_with_long_name(tmp_path)
    bundle = runtime._bundle_manifest(source)
    # Reproduce the reported boundary: final file fits, its atomic temporary
    # name exceeds MAX_PATH. Then copy all entries and detect a changed byte.
    name = '.staging-' + 'b'*64
    padding = 255 - len(str(tmp_path / name / relative)) - 1
    assert padding > 0
    stage = tmp_path / ('p'*padding) / name
    assert len(str(stage / relative)) == 255
    with monkeypatch.context() as context:
        guard = WindowsPathGuard(context, tmp_path)
        if extended:
            context.setattr(runtime, '_io_path', guard.io_path, raising=False)
        else:
            context.setattr(runtime, '_io_path', lambda p: p, raising=False)
        if not extended:
            with pytest.raises(FileNotFoundError) as error:
                runtime._copy_bundle(source, stage, bundle)
            assert '.continuity_decision.py.' in str(error.value)
            assert guard.denied and len(guard.denied[0]) >= 260
            return
        runtime._copy_bundle(source, stage, bundle)
        assert runtime._tree_matches_bundle(stage, bundle, allow_partial=False)
        assert guard.extended and not guard.denied
        runtime._atomic_write(stage / relative, b'altered', 0o600)
        assert not runtime._tree_matches_bundle(stage, bundle, allow_partial=False)
        with pytest.raises(RuntimeError, match='removal was refused'):
            runtime._remove_owned_tree(stage, bundle, allow_partial=False)
        runtime._atomic_write(stage / relative, (source / relative).read_bytes(), 0o600)
        runtime._remove_owned_tree(stage, bundle, allow_partial=False)
        assert not stage.exists()


def test_native_copy_beyond_max_path_keeps_all_bytes_and_rollback_guards(tmp_path):
    source, relative = bundle_with_long_name(tmp_path)
    bundle = runtime._bundle_manifest(source)
    stage = tmp_path / ('private-'+'a'*64) / ('.staging-'+'b'*64)
    assert len(str(stage / relative)) > 260
    runtime._copy_bundle(source, stage, bundle)
    assert runtime._tree_matches_bundle(stage, bundle, allow_partial=False)
    runtime._remove_owned_tree(stage, bundle, allow_partial=False)
    assert not stage.exists()


@pytest.mark.skipif(os.name == 'nt', reason='Explicit POSIX MAX_PATH simulation')
def test_environment_tree_reads_and_removal_use_extended_paths(tmp_path, monkeypatch):
    # The padding keeps the path beyond MAX_PATH however short the temporary root is.
    root = tmp_path / ('pad-'+'p'*80) / ('env-'+'a'*64) / ('release-'+'b'*64)
    deep = root / 'venv/Lib/site-packages/library/deep/module.py'
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b'# dependency\n')
    assert len(str(deep)) > 260
    before = environment._tree_digest(root)
    with monkeypatch.context() as context:
        guard = WindowsPathGuard(context, tmp_path)
        context.setattr(environment, '_io_path', guard.io_path, raising=False)
        assert environment._tree_digest(root) == before
        assert environment._path_descriptor(root, recursive=True)['tree_sha256'] == before
        environment._atomic_json_write(root / 'venv/Lib/site-packages/library/deep/manifest.json', {'ok': True})
        environment._verify_internal_links(root)
        environment._remove_tree(root)
        assert not guard.denied and guard.extended
    assert not root.exists()


@pytest.mark.parametrize('module', [runtime, environment])
def test_io_conversion_cannot_authorize_a_relative_snapshot(tmp_path, monkeypatch, module):
    monkeypatch.setattr(module, '_io_path', lambda _: tmp_path)
    with pytest.raises(ValueError, match='absolute'):
        module._validate_snapshot_dir(Path('relative-snapshot'))


@pytest.mark.parametrize('kind', ['relative', 'parent'])
def test_io_conversion_cannot_authorize_an_indirect_plugin(tmp_path, monkeypatch, kind):
    from daemons_companion.hermes import profile
    monkeypatch.setattr(profile, '_io_path', lambda _: pytest.fail('validate before normalizing'))
    path = Path('relative-plugin') if kind == 'relative' else tmp_path / 'nested/../plugin'
    with pytest.raises(ValueError, match='direct'):
        profile._copy_descriptor(path)
