"""Desktop-only attachment transport with bounded, secret-free failure details.

Does not change the installed daemon or weaken any ownership/integrity check.
Exception text, paths, requests and native command output are never exported.
"""
from contextlib import redirect_stdout
import json
import sys

from daemons_companion.hosts.attachment_command import Refused, execute

STAGES = {
    'source-profile', 'source-recovery', 'source-runtime', 'source-environment',
    'source-connection', 'source-preflight', 'destination-layout',
    'credential-store', 'creation', 'association', 'request',
}
ERRORS = {'ValueError', 'RuntimeError', 'OSError', 'FileNotFoundError',
          'PermissionError', 'TimeoutExpired', 'HostCommandError', 'KeyError',
          'TypeError', 'JSONDecodeError', 'Refused'}
REASONS = {
    'Private runtime environment is unavailable.': 'environment-missing',
    'Private runtime environment manifest is unavailable.': 'environment-manifest-missing',
    'Private runtime environment manifest is invalid.': 'environment-manifest-invalid',
    'Private runtime environment manifest is inconsistent.': 'environment-manifest-mismatch',
    'Private runtime environment content changed.': 'environment-content-changed',
    'Private runtime interpreter is unavailable.': 'interpreter-missing',
    'Private runtime contains an external or broken link.': 'environment-link-invalid',
    'Host installation paths must be direct and absolute.': 'path-not-direct',
    'The associated daemon bridge did not become ready.': 'bridge-not-ready',
    'The associated host services changed during activation.': 'host-services-changed',
    'The associated host did not become ready.': 'host-not-ready',
    'The shared memory maintenance did not become ready.': 'maintenance-not-ready',
    'The associated host did not stop completely.': 'host-not-stopped',
}


def details(error):
    # Inspect only our stack's function names, never frame locals or arguments.
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__ or error.__context__
    frames = []
    for item in chain:
        tb = item.__traceback__
        while tb:
            frames.append((tb.tb_frame.f_globals.get('__name__'), tb.tb_frame.f_code.co_name))
            tb = tb.tb_next
    def has(module, *names):
        return any(m == module and n in names for m, n in frames)
    stage = 'association'
    if has('daemons_companion.hosts.creation', 'dispatch', 'start', 'perform'):
        stage = 'creation'
    if has('daemons_companion.hosts.attachment_command', 'source_binding'):
        stage = 'source-preflight'
    if has('daemons_companion.runtime.installation_scope', 'validate_profile', 'validate_home', 'receipt'):
        stage = 'source-profile'
    if has('daemons_companion.runtime.upgrade', 'require_settled_upgrade'):
        stage = 'source-recovery'
    if has('daemons_companion.runtime.release', 'verify_current_release', 'resolve_current_release'):
        stage = 'source-runtime'
    if has('daemons_companion.runtime.environment', 'resolve_release_environment', '_verify_environment'):
        stage = 'source-environment'
    if has('daemons_companion.openclaw.host_install', 'validate'):
        stage = 'destination-layout'
    if has('daemons_companion.setup.credentials', 'system_credential_store', '__init__'):
        stage = 'credential-store'
    root = chain[-1]
    result = {'attachment_stage': stage,
              'exception_type': type(root).__name__ if type(root).__name__ in ERRORS else 'UnexpectedError'}
    for item in chain:
        for name in ('errno', 'winerror', 'exit_code'):
            value = getattr(item, name, None)
            if type(value) is int and -(2**31) <= value < 2**31:
                result.setdefault(name, value)
        if type(item).__name__ in ERRORS and str(item) in REASONS:
            result['failure_reason'] = REASONS[str(item)]
    return result


def respond(raw):
    try:
        if len(raw) > 32768:
            raise Refused('request')
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise Refused('request')
        # Native output belongs to the child, not the JSON channel or report.
        with redirect_stdout(sys.stderr):
            result = execute(request)
        return {'ok': True, 'result': result}
    except Exception as error:
        diagnostic = details(error)
        code = error.code if isinstance(error, Refused) else 'unconfirmed'
        if code == 'unconfirmed' and diagnostic['attachment_stage'] in {
            'source-profile', 'source-recovery', 'source-runtime',
            'source-environment', 'destination-layout', 'credential-store',
        }:
            code = diagnostic['attachment_stage']
        return {'ok': False, 'code': code, 'diagnostic': diagnostic}


if __name__ == '__main__':
    print(json.dumps(respond(sys.stdin.buffer.read(32769))))
