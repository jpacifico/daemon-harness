import json
from pathlib import Path
from unittest.mock import patch
import pytest
from daemons_companion.runtime.host_interface import plan


def fixture(tmp_path, host='openclaw'):
    profile = tmp_path / 'Companion/instance-test/hosts' / host / 'installations' / ('a' * 32) / 'profile'
    profile.mkdir(parents=True)
    return profile, dict(host=host, profile=str(profile), base=str(tmp_path))


def test_openclaw_exact_port_and_fragment_not_remote(tmp_path):
    profile, request = fixture(tmp_path)
    config = {'gateway': {'bind': 'loopback', 'port': 23456, 'auth': {'mode': 'token', 'token': 'synthetic&token'}}}
    (profile / 'openclaw.json').write_text(json.dumps(config))
    assert plan(request)['url'] == 'http://127.0.0.1:23456/#token=synthetic%26token'
    config['gateway']['bind'] = 'lan'
    (profile / 'openclaw.json').write_text(json.dumps(config))
    with pytest.raises(ValueError):
        plan(request)


def test_arbitrary_paths_commands_and_symlinks_refused(tmp_path):
    profile, request = fixture(tmp_path)
    with pytest.raises(ValueError):
        plan(request | {'command': 'anything'})
    with pytest.raises(ValueError):
        plan(request | {'base': str(tmp_path / 'other')})
    linked = tmp_path / 'linked'
    linked.symlink_to(profile)
    with pytest.raises(ValueError):
        plan(request | {'profile': str(linked)})


def test_hermes_pinned_interpreter_and_scoped_environment(tmp_path):
    from daemons_companion.runtime.acquisition import _RELEASES
    profile, request = fixture(tmp_path, 'hermes')
    python = profile.parent / 'source' / ('hermes-agent-' + _RELEASES['hermes'].revision) / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'synthetic')
    with patch('daemons_companion.runtime.installation_scope.InstallationScope.receipt'), patch(
        'daemons_companion.runtime.private_environment.private_process_environment', return_value={'HERMES_HOME': str(profile)}
    ):
        result = plan(request)
    assert result['profile'] == str(profile)
    assert Path(result['executable']) == python
    assert result['environment']['HERMES_HOME'] == str(profile)


def test_refusals_name_their_reason_without_details(tmp_path, capsys):
    import io
    from daemons_companion.runtime import host_interface
    profile, request = fixture(tmp_path, 'hermes')
    outside = tmp_path / 'elsewhere'
    with pytest.raises(ValueError, match='hostInterface.external'):
        plan(request | {'base': str(outside.mkdir() or outside)})
    with patch('daemons_companion.runtime.installation_scope.InstallationScope.receipt'), patch(
        'daemons_companion.runtime.private_environment.private_process_environment', return_value={}
    ):
        with pytest.raises(ValueError, match='hostInterface.older'):
            plan(request)
        # The reply carries the fixed code only.
        with patch('sys.stdin', io.StringIO(json.dumps(request) + '\n')):
            host_interface.main()
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'code': 'hostInterface.older'}
    with patch('sys.stdin', io.StringIO(json.dumps(request | {'host': 'other'}) + '\n')):
        host_interface.main()
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'code': 'hostInterface.unavailable'}
