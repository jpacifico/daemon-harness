from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from daemons_companion.runtime.bootstrap import HostCommandError
from daemons_companion.desktop.install_diagnostics import InstallationProgress, report_installation_failure
from daemons_companion.setup.transaction import TransactionEngine, TransactionError, public_plan_digest
from tests.test_companion_transaction import _plan, DisposableProfileAdapter
from tests.test_companion_terminal import FakeUI


@pytest.mark.parametrize('rollback_fails', [False, True])
@pytest.mark.parametrize('cannot_save', [False, True])
def test_real_rollback_retains_original_failure_and_saves_only_scrubbed_details(tmp_path, rollback_fails, cannot_save):
    # This wrapper must preserve both the engine and delegate's rollback behavior.
    scope = SimpleNamespace(home=tmp_path / 'home', attempt=tmp_path,
                            validate_home=lambda _: None)
    plan = _plan()
    step = replace(plan.steps[0], code='runtime-environment')
    plan = replace(plan, steps=(step,))
    token = '123456:' + 'a'*30
    secret = 'sk-private-key-for-test'
    class Adapter(DisposableProfileAdapter):
        def apply(self, step):
            super().apply(step)
            raise HostCommandError(2, '\n'.join([
                'Dependency download failed: certificate verification failed.',
                'https://user:pass@synthetic.invalid/token?' + token,
                'Authorization: Bearer account-credential',
                'TELEGRAM_BOT_TOKEN=' + token, secret,
                str(scope.home / 'runtime/venv/python.exe'),
            ]))
        def finalize_rollback(self, plan, snapshots):
            events.append('finalized')
    adapter = Adapter(tmp_path / 'profile', fail_rollback=step.code if rollback_fails else '')
    events = []
    observed = InstallationProgress(adapter, events.append)
    if cannot_save:
        # A symlink is refused, never followed or overwritten.
        outside = tmp_path / 'preserved'
        outside.write_text('untouched')
        (tmp_path / 'installation-diagnostic.json').symlink_to(outside)
    with pytest.raises(TransactionError) as captured:
        TransactionEngine(scope.home / 'transactions').execute(
            plan, observed, transaction_id='synthetic-test', confirmed_plan_digest=public_plan_digest(plan))
    ui = FakeUI()
    import os
    os.environ['DAEMON_HARNESS_COMPANION_REVISION'] = 'c' * 40
    try:
        report_installation_failure(ui, scope, observed, captured.value, 'synthetic-test')
    finally:
        del os.environ['DAEMON_HARNESS_COMPANION_REVISION']
    output = '\n'.join(ui.output)
    assert 'Install Python and dependencies (apply)' in output
    assert 'certificate verification failed' in output
    assert 'finalized' in events
    assert 'runtime-environment' in observed.failure
    assert any('rollback...' in event for event in events)
    assert ('Rollback needs recovery' in output) == rollback_fails
    if cannot_save:
        assert outside.read_text() == 'untouched'
        assert 'could not be saved' in output
        document = ''
    else:
        document = (tmp_path / 'installation-diagnostic.json').read_text()
        data = json.loads(document)
        assert data['step'] == 'runtime-environment' and data['phase'] == 'apply'
        assert data['revision'] == 'c' * 40
        assert data['transaction_status'] == ('rollback_failed' if rollback_fails else 'rolled_back')
        assert bool(data['rollback_causes']) == rollback_fails
        assert data['causes'][0]['type'] == 'HostCommandError'
    for value in (token, secret, 'account-credential', 'user:pass', 'TELEGRAM_BOT_TOKEN'):
        assert value not in document + output


def test_progress_preserves_interruption_without_turning_it_into_failure():
    def interrupt(*a): raise KeyboardInterrupt
    observed = InstallationProgress(SimpleNamespace(apply=interrupt), lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        observed.apply(SimpleNamespace(code='runtime-layout'))
    assert observed.failure is None


def test_windows_native_error_survives_scheduler_boundary():
    from daemons_companion.runtime.services import WindowsTaskSchedulerBackend, SchedulerCommandResult, AFTER_TURN_JOB
    backend = WindowsTaskSchedulerBackend(runner=lambda *a: SchedulerCommandResult(5, '', 'Access is denied.'))
    with pytest.raises(RuntimeError, match='capture failed') as error:
        backend.capture(AFTER_TURN_JOB)
    assert isinstance(error.value.__cause__, HostCommandError)
    assert error.value.__cause__.diagnostic == 'Access is denied.'


@pytest.mark.parametrize('operation', ['inventory', 'enable'])
def test_hermes_native_error_survives_plugin_boundary(tmp_path, operation):
    from daemons_companion.hermes.activation import HermesCliBackend, HermesCommandResult
    executable = tmp_path / 'hermes'
    executable.touch()
    backend = HermesCliBackend(executable, tmp_path / 'profile',
                               runner=lambda *a: HermesCommandResult(1, '', 'Missing plugin dependency.'))
    with pytest.raises(RuntimeError) as error:
        backend.state() if operation == 'inventory' else backend.enable_plugin('daemon_core')
    assert isinstance(error.value.__cause__, HostCommandError)
    assert error.value.__cause__.diagnostic == 'Missing plugin dependency.'


def test_diagnostic_observer_cannot_fail_the_transaction():
    from types import SimpleNamespace
    from daemons_companion.desktop.install_diagnostics import InstallationProgress
    def broken(*args):
        raise RuntimeError("observer failed")
    adapter = SimpleNamespace(apply=lambda step: "applied")
    observed = InstallationProgress(adapter, lambda message: None, broken)
    assert observed.apply(SimpleNamespace(code="identity")) == "applied"
