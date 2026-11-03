"""Host addition must not mistake Windows MAX_PATH errors for an old runtime."""
import json
from pathlib import Path

import pytest

from daemons_companion.hosts import attachment_command as command
from daemons_companion.hosts import creation
from daemons_companion.hosts import attachment
from daemons_companion.openclaw import host_install as acquisition
from daemons_companion.runtime import release as runtime_module
from daemons_companion.runtime import environment
from tests.companion_windows_io import WindowsPathGuard
from tests.test_companion_private_setup import owned
from tests.test_companion_runtime_environment import _bundle, FakeRunner
from tests.test_companion_source_upgrade import apply


@pytest.fixture
def installed(tmp_path, monkeypatch, request):
    root = tmp_path.resolve() / ('private-installation-' + 'x' * 70)
    root.mkdir()
    host, scope = owned(root, monkeypatch, 'windows')
    bundle = _bundle(root / 'bundle')
    for name, text in {
        'src/daemons_companion/hosts/attachment_entry.py': '# synthetic entry',
        'src/daemons_companion/hosts/creation.py': '# synthetic creation',
        'src/daemon_core/host_bindings.py': "SCHEMA = 'daemon.host-execution.v1'\n",
        'src/daemon_core/canonical_voice.py': '# synthetic voice',
        'integrations/openclaw_plugin/openclaw.plugin.json': '{}',
    }.items():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_text(text)
    if getattr(request, "param", None) == "old":
        (bundle / "src/daemons_companion/hosts/attachment_entry.py").unlink()
    runtime = runtime_module.CompanionRuntimeLayout(scope.home, bundle, scope)
    apply(runtime_module.CompanionRuntimeAdapter(runtime), 'runtime-layout', root / 'copy')
    env = environment.CompanionRuntimeEnvironmentLayout(
        runtime, host.uv_executable, host.uv_sha256, 'windows'
    )
    apply(environment.CompanionRuntimeEnvironmentAdapter(env, runner=FakeRunner('windows')),
          'runtime-environment', root / 'environment')
    (scope.attempt / 'connection.json').write_text(json.dumps({'user_id': '777001'}))
    (runtime.memory_root / 'core').mkdir(exist_ok=True)
    (runtime.memory_root / 'core/daemon_identity.json').write_text('{"name":"Aster"}')
    for name in ('after-turn-spool', 'receipts'):
        (runtime.state_root / name).mkdir()
    monkeypatch.setattr(command, 'native_target', lambda: 'windows')
    monkeypatch.setenv('DAEMON_HARNESS_COMPANION_BUNDLE_ROOT', str(Path(__file__).resolve().parents[1]))
    source = runtime_module.verify_current_release(runtime)
    assert len(str(source)) > 260
    return scope, runtime, source


def test_verified_current_source_can_start_host_creation_on_long_paths(installed, monkeypatch):
    scope, runtime, source = installed
    # Real files and ownership/manifests; only external credentials/services are absent.
    before = command.source_binding(scope.profile)[2].descriptor()
    guard = WindowsPathGuard(monkeypatch, source)
    guard.prefix = '/__extended_win32__'
    for module, name in ((runtime_module, '_io_path'), (environment, '_io_path'),
                         (command, '_source_io_path'), (creation, '_source_io_path'),
                         (attachment, '_source_io_path'), (acquisition, '_source_io_path')):
        monkeypatch.setattr(module, name, guard.io_path, raising=False)
    monkeypatch.setattr(creation, 'system_credential_store', lambda: object())
    assert command.source_binding(scope.profile)[2].descriptor() == before
    result = creation.dispatch({'operation': 'create-start', 'base': str(scope.profile.parents[4]),
                                'source_profile': str(scope.profile),
                                'installation_id': 'b' * 32, 'port': 18901})
    assert result['phase'] == 'download'
    assert not guard.denied
    assert str(source / 'src/daemons_companion/hosts/attachment_entry.py') in guard.extended
    assert str(source / 'src/daemons_companion/hosts/creation.py') in guard.extended


@pytest.mark.parametrize('kind', ['missing', 'tampered'])
def test_real_absence_or_tampering_still_refused(installed, kind):
    scope, runtime, source = installed
    path = source / 'src/daemons_companion/hosts/attachment_entry.py'
    if kind == 'missing':
        path.unlink()
    else:
        path.write_text('# changed')
    with pytest.raises(RuntimeError):
        command.source_binding(scope.profile)


@pytest.mark.parametrize('kind', ['symlink', 'parent-link', 'hardlink', 'oversize', 'traversal'])
def test_long_metadata_access_keeps_ownership_guards(tmp_path, monkeypatch, kind):
    root = tmp_path.resolve() / ('x' * 100) / ('y' * 100)
    root.mkdir(parents=True)
    file = root / 'metadata.json'
    file.write_text('{}')
    if kind == 'symlink':
        target = root / 'original.json'
        file.rename(target)
        file.symlink_to(target)
    elif kind == 'parent-link':
        link = root.parent / 'linked'
        link.symlink_to(root, target_is_directory=True)
        file = link / file.name
    elif kind == 'hardlink':
        (root / 'duplicate').hardlink_to(file)
    elif kind == 'traversal':
        file = root / '..' / root.name / file.name
    guard = WindowsPathGuard(monkeypatch, root.parent)
    guard.prefix = '/__extended_win32__'
    monkeypatch.setattr(acquisition, '_source_io_path', guard.io_path)
    with pytest.raises(ValueError):
        acquisition.checked_file(file, 1 if kind == 'oversize' else 100)
    # A rejected parent symlink can resolve to an ordinary absolute target in
    # this POSIX simulator. Rejection itself, before reading, is the contract.


@pytest.mark.parametrize('installed', ['old'], indirect=True)
def test_genuinely_old_verified_runtime_still_requests_upgrade(installed):
    scope, _, _ = installed
    with pytest.raises(command.Refused, match='upgrade'):
        command.source_binding(scope.profile)
