"""Resolve an owned host interface for Companion's trusted main process.

The response may contain credentials. Never forward it to the renderer, persist
it as a diagnostic, or use user-provided commands/URLs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.bootstrap import _source_io_path


_CODES = {'hostInterface.unavailable', 'hostInterface.external', 'hostInterface.older', 'hostInterface.disabled'}


def plan(request):
    if set(request) != {'host', 'profile', 'base'} or request['host'] not in {'hermes', 'openclaw'}:
        raise ValueError('hostInterface.unavailable')
    base, profile = Path(request['base']), Path(request['profile'])
    if (not base.is_absolute() or not profile.is_absolute() or base.resolve() != base
            or profile.resolve() != profile or not profile.is_dir()):
        raise ValueError('hostInterface.unavailable')
    if not profile.is_relative_to(base):
        # Connected from elsewhere (a terminal install or a canary), not created here.
        raise ValueError('hostInterface.external')
    scope = InstallationScope(profile.parent)
    if scope.profile != profile or scope.host_kind != request['host']:
        raise ValueError('hostInterface.unavailable')
    if request['host'] == 'hermes':
        from daemons_companion.runtime.acquisition import _RELEASES
        from daemons_companion.runtime.private_environment import private_process_environment
        scope.receipt()
        env = private_process_environment(scope, hermes=True)
        source = scope.attempt / 'source' / ('hermes-agent-' + _RELEASES['hermes'].revision)
        python = source / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            # Daemons from earlier candidates keep Hermes outside their installation.
            raise ValueError('hostInterface.older')
        return {'host': 'hermes', 'revision': _RELEASES['hermes'].revision,
                'executable': str(_source_io_path(python)), 'cwd': str(scope.attempt),
                'environment': env, 'profile': str(profile)}
    config_path = profile / 'openclaw.json'
    if config_path.is_symlink() or config_path.stat().st_size > 1024 * 1024:
        raise ValueError('hostInterface.unavailable')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    gateway = config.get('gateway', {})
    port = gateway.get('port')
    if gateway.get('bind') != 'loopback' or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('hostInterface.unavailable')
    control = gateway.get('controlUi', {})
    if control.get('enabled') is False:
        raise ValueError('hostInterface.disabled')
    auth = gateway.get('auth', {})
    if auth.get('mode') != 'token':
        raise ValueError('hostInterface.unavailable')
    token = auth.get('token')
    if token == '${DAEMONS_GATEWAY_TOKEN}':
        if (scope.attempt / 'openclaw-connection.json').is_file():
            from daemons_companion.openclaw.gateway import host_environment
            env = host_environment(scope)
        else:
            from daemons_companion.hosts.attachment_entry import host_environment
            env = host_environment(profile)
        token = env.get('DAEMONS_GATEWAY_TOKEN')
    if not isinstance(token, str) or not token or '${' in token or len(token) > 4096:
        raise ValueError('hostInterface.unavailable')
    base_path = control.get('basePath', '/') or '/'
    if (not isinstance(base_path, str) or not base_path.startswith('/') or base_path.startswith('//')
            or any(c in base_path for c in '\\?#\r\n') or '..' in base_path.split('/')):
        raise ValueError('hostInterface.unavailable')
    from urllib.parse import quote
    return {'host': 'openclaw', 'url': f'http://127.0.0.1:{port}{base_path.rstrip("/")}/#token={quote(token, safe="")}',
            'profile': str(profile)}


def main():
    try:
        request = json.loads(sys.stdin.readline(65537))
        result = plan(request)
        print(json.dumps({'ok': True, **result}))
    except Exception as error:
        # Paths, environment and native exceptions can contain credentials:
        # only the fixed codes raised above are reported.
        code = str(error) if isinstance(error, ValueError) and str(error) in _CODES else 'hostInterface.unavailable'
        print(json.dumps({'ok': False, 'code': code}))


if __name__ == '__main__':
    main()
