from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from daemons_companion.desktop.daemon_settings import inspect_settings, update_settings, SettingsError
from daemon_core.daemon_identity import load_daemon_identity, save_daemon_identity
from daemon_core.onboarding_user import UserPreferences

SOURCE = str(Path(__file__).resolve().parents[1] / 'src')
BINDING = {'user_id': '12345', 'platform': 'telegram'}
VALUES = {'name': 'Étoile', 'gender': 'feminine', 'preferred_name': 'Morgan', 'pronouns': 'they/them'}


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def inspect(root, binding=BINDING, source=SOURCE):
    return inspect_settings(root, source=source, binding=binding)


def save(root, snapshot, values=VALUES, binding=BINDING, source=SOURCE):
    return update_settings(root, source=source, binding=binding, revision=snapshot['revision'], values=values)


@pytest.mark.parametrize('layout', ['legacy', 'root-folders'])
def test_existing_settings_atomic_edit_reopen_and_exact_markdown_preservation(tmp_path, layout):
    root = tmp_path / 'vault' / ('.daemon' if layout == 'root-folders' else 'memory')
    root.mkdir(parents=True)
    save_daemon_identity(root, 'Aster', 'masculine', user=UserPreferences('Before', 'he/him').for_user('12345'))
    for relative in ['core/persona.md', 'users/12345/context.md', 'users/12345/conversation.md', 'users/12345/agenda.md', 'capsules/example.md']:
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('Synthetic exact content: do not change.\n')
    before = tree(root)
    snap = inspect(root)
    assert tree(root) == before
    result = save(root, snap)
    assert result['values'] == VALUES
    assert result['revision'] != snap['revision']
    assert inspect(root) == result
    after = tree(root)
    assert {k: v for k, v in after.items() if k != 'core/daemon_identity.json'} == {k: v for k, v in before.items() if k != 'core/daemon_identity.json'}
    assert load_daemon_identity(root).name == 'Étoile'
    assert load_daemon_identity(root).user['preferred_name'] == 'Morgan'
    assert save(root, result)['revision'] == result['revision']
    if os.name != 'nt':
        assert stat.S_IMODE((root / 'core/daemon_identity.json').stat().st_mode) == 0o600


def test_absent_identity_requires_explicit_values_and_creates_only_metadata(tmp_path):
    snap = inspect(tmp_path)
    assert snap['configured'] is False and snap['values']['name'] == ''
    result = save(tmp_path, snap)
    assert result['values'] == VALUES
    assert list(tree(tmp_path)) == ['core/daemon_identity.json']


def test_stale_metadata_and_source_change_cannot_overwrite(tmp_path):
    save(tmp_path, inspect(tmp_path))
    snap = inspect(tmp_path)
    save_daemon_identity(tmp_path, 'Other', 'feminine')
    before = tree(tmp_path)
    with pytest.raises(SettingsError, match='changed'):
        save(tmp_path, snap)
    assert tree(tmp_path) == before
    snap = inspect(tmp_path)
    with pytest.raises(SettingsError, match='changed'):
        save(tmp_path, snap, source=None)
    assert tree(tmp_path) == before


def test_legacy_runtime_supports_name_but_never_receives_user_fields(tmp_path):
    source = tmp_path / 'source' / 'daemon_core'
    source.mkdir(parents=True)
    (source / 'daemon_identity.py').write_text('def load_daemon_identity(): pass\n')
    (source / 'canonical_voice.py').write_text('identity = load_daemon_identity(root)\n')
    root = tmp_path / 'memory'
    root.mkdir()
    snap = inspect(root, source=str(source.parent))
    assert snap['canEditName'] and not snap['canEditUser']
    with pytest.raises(SettingsError):
        save(root, snap, source=str(source.parent))
    assert not tree(root)
    save(root, snap, values={**VALUES, 'preferred_name': '', 'pronouns': ''}, source=str(source.parent))
    assert 'user' not in json.loads((root / 'core/daemon_identity.json').read_text())


def test_unknown_source_is_read_only_and_absent_account_never_inferred(tmp_path):
    snap = inspect(tmp_path, source=None)
    assert not snap['canEditName'] and not snap['canEditUser']
    with pytest.raises(SettingsError, match='unsupported'):
        save(tmp_path, snap, source=None)
    snap = inspect(tmp_path, binding=None)
    assert snap['canEditName'] and not snap['canEditUser']
    with pytest.raises(SettingsError):
        save(tmp_path, snap, binding=None)
    assert not tree(tmp_path)


def test_shared_host_preserves_original_user_and_platform_binding(tmp_path):
    save_daemon_identity(tmp_path, 'Aster', 'feminine', user=UserPreferences('Before').for_user('12345', 'telegram'))
    other = {'user_id': 'other-user', 'platform': 'openclaw'}
    result = save(tmp_path, inspect(tmp_path, binding=other), binding=other)
    assert result['userScope'] == BINDING
    assert load_daemon_identity(tmp_path).user == UserPreferences('Morgan', 'they/them').for_user('12345', 'telegram')


@pytest.mark.parametrize('content', ['bad', '{"name":"Aster"}', 'x'*8193, '{"name":"Aster","gender":"other"}'])
def test_malformed_identity_remains_untouched(tmp_path, content):
    target = tmp_path / 'core/daemon_identity.json'
    target.parent.mkdir()
    target.write_text(content)
    before = tree(tmp_path)
    with pytest.raises(SettingsError, match='invalid_metadata'):
        inspect(tmp_path)
    assert tree(tmp_path) == before


@pytest.mark.parametrize('field,value', [('name', ''), ('name', 'bad\nname'), ('gender', 'other'), ('preferred_name', 'x'*41), ('pronouns', '\u200b')])
def test_invalid_edit_is_rejected_without_any_memory_write(tmp_path, field, value):
    with pytest.raises(SettingsError, match='invalid_values'):
        save(tmp_path, inspect(tmp_path), values={**VALUES, field: value})
    assert not tree(tmp_path)


def test_lock_and_atomic_failure_preserve_previous_identity(tmp_path, monkeypatch):
    import daemons_companion.desktop.daemon_settings as settings
    from daemon_core.memory.integrity import memory_file_lock
    save(tmp_path, inspect(tmp_path))
    before = tree(tmp_path)
    snapshot = inspect(tmp_path)
    with memory_file_lock(tmp_path / 'core/daemon_identity.json'):
        with pytest.raises(SettingsError, match='busy'):
            save(tmp_path, snapshot)
    def fail(*args):
        raise OSError('Synthetic write failure')
    monkeypatch.setattr(settings, 'atomic_write_text', fail)
    with pytest.raises(OSError):
        save(tmp_path, snapshot, values={**VALUES, 'name': 'Nova'})
    assert tree(tmp_path) == before


@pytest.mark.parametrize('component', ['root', 'core', 'identity'])
def test_symlinked_settings_paths_are_refused(tmp_path, component):
    real = tmp_path / 'real'
    real.mkdir()
    save(real, inspect(real))
    alias = tmp_path / 'alias'
    before = tree(real)
    try:
        if component == 'root':
            alias.symlink_to(real, target_is_directory=True)
        elif component == 'core':
            alias.mkdir()
            (alias / 'core').symlink_to(real / 'core', target_is_directory=True)
        else:
            (alias / 'core').mkdir(parents=True)
            (alias / 'core/daemon_identity.json').symlink_to(real / 'core/daemon_identity.json')
    except OSError:
        pytest.skip('Symlink creation is unavailable for this account')
    with pytest.raises(SettingsError):
        inspect(alias)
    assert tree(real) == before


@pytest.mark.parametrize('platform', ['telegram', 'openclaw'])
def test_next_canonical_composition_reads_saved_settings_for_each_host(tmp_path, monkeypatch, platform):
    from tests.test_canonical_voice import _freeze_inputs
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    import daemon_core.held_atlas_sovereign as library
    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setattr(library, 'MEMORY_ROOT', tmp_path)
    monkeypatch.setattr(library, 'load_user_context', lambda _: '')
    binding = {'user_id': '12345', 'platform': platform}
    def compose(user='12345'):
        return compose_canonical_voice_messages(user, 'Hello', platform=platform, message_received_at=123.0)[0]
    before = compose()
    save(tmp_path, inspect(tmp_path, binding=binding), binding=binding)
    after = compose()
    text = json.dumps(after, ensure_ascii=False)
    assert 'Étoile' in text and 'Morgan' in text and 'they/them' in text
    assert after[-1] == before[-1]
    assert 'Morgan' not in json.dumps(compose('54321'))
