"""Exercise the packaged desktop wrapper without a live host or credentials."""
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('attachment_transport', Path(__file__).resolve().parents[1] / 'scripts/companion_attachment_transport.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def test_success_does_not_mix_native_output_with_json(monkeypatch, capsys):
    def execute(request):
        print('native output')
        return {'revision': 'synthetic', 'phase': 'download'}
    monkeypatch.setattr(transport, 'execute', execute)
    assert transport.respond(b'{}') == {'ok': True, 'result': {'revision': 'synthetic', 'phase': 'download'}}
    capture = capsys.readouterr()
    assert not capture.out and capture.err == 'native output\n'


@pytest.mark.parametrize('raw', [b'[]', b'{', b'x' * 32769])
def test_malformed_request_has_no_sensitive_output(raw):
    result = transport.respond(raw)
    assert not result['ok']
    assert 'message' not in result and 'traceback' not in result


def test_existing_environment_check_has_safe_cause_and_code(monkeypatch, tmp_path):
    from daemons_companion.runtime import environment
    layout = type('Layout', (), {'runtime': None, 'target_os': 'windows', 'environment_path': lambda self, rid: tmp_path / 'missing'})()
    monkeypatch.setattr(environment, 'verify_current_release', lambda _: tmp_path / 'release')
    monkeypatch.setattr(environment, '_file_sha256', lambda _: 'a' * 64)
    monkeypatch.setattr(transport, 'execute', lambda _: environment.resolve_release_environment(layout))
    result = transport.respond(b'{}')
    assert result['code'] == 'source-environment'
    assert result['diagnostic'] == {'attachment_stage': 'source-environment', 'exception_type': 'RuntimeError', 'failure_reason': 'environment-missing'}
    assert str(tmp_path) not in json.dumps(result)


def test_nested_refusal_keeps_numeric_error_not_secret(monkeypatch):
    def execute(_):
        try:
            e = OSError(2, 'secret /Users/PRIVATE token=PRIVATE')
            e.winerror = 206
            raise e
        except OSError:
            raise transport.Refused('creation') from None
    monkeypatch.setattr(transport, 'execute', execute)
    result = transport.respond(b'{}')
    assert result['code'] == 'creation'
    assert result['diagnostic']['errno'] == 2 and result['diagnostic']['winerror'] == 206
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('message,reason', [
    ('The associated daemon bridge did not become ready.', 'bridge-not-ready'),
    ('The associated host did not become ready.', 'host-not-ready'),
    ('The shared memory maintenance did not become ready.', 'maintenance-not-ready'),
])
def test_activation_timeouts_report_which_part_was_not_ready(monkeypatch, message, reason):
    def execute(request):
        raise RuntimeError(message)
    monkeypatch.setattr(transport, 'execute', execute)
    result = transport.respond(b'{"operation": "apply"}')
    assert result['diagnostic'] == {'attachment_stage': 'association', 'exception_type': 'RuntimeError', 'failure_reason': reason}
    assert message not in json.dumps(result)
