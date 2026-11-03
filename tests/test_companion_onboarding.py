"""Onboarding gates use isolated host metadata and synthetic native sign-ins."""
from types import SimpleNamespace

import pytest

from daemons_companion.desktop.host_terminal import PackagedHostSetup
from daemons_companion.setup.subscription import SubscriptionStatus
from daemons_companion.desktop.terminal import Quit, run_wizard
from tests.test_companion_host_install import fixture
from tests.test_companion_terminal import FakeUI, backend


@pytest.mark.parametrize('exit_key', [Quit()])
def test_quitting_provider_menu_never_installs_helper_or_signs_in(exit_key):
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'), offer=lambda *a: False,
                            connect_subscription=lambda *a: pytest.fail('no provider was selected'))
    with pytest.raises(Quit):
        run_wizard(FakeUI(choices=[0, 0, exit_key], fields=['Avery']), backend=backend(), host_setup=setup)


def private_setup(tmp_path, monkeypatch, target_os):
    layout, _, _ = fixture(tmp_path, target_os=target_os)
    setup = PackagedHostSetup(SimpleNamespace(target_os=target_os, validate=lambda: None))
    monkeypatch.setattr(setup, 'layout', lambda: layout)
    monkeypatch.setattr(setup, 'codex_executable', lambda _: None)
    monkeypatch.setattr('daemons_companion.desktop.host_terminal.installed_host',
                        lambda _: SimpleNamespace(config_path=str(layout.profile)))
    return setup


@pytest.mark.parametrize('target_os', ['macos', 'windows'])
def test_chatgpt_consent_prepares_helper_then_requires_both_native_signins(tmp_path, monkeypatch, target_os):
    setup = private_setup(tmp_path, monkeypatch, target_os)
    events = []
    binary = tmp_path / 'synthetic-codex'
    def install(layout, progress):
        events.append('helper')
        progress('Downloading and verifying Codex...')
        return binary
    def authenticate(layout, *, codex):
        assert codex == binary
        def sign_in(progress, *, activity):
            events.append('sign-in')
            return SubscriptionStatus(True, True)
        return SimpleNamespace(sign_in=sign_in)
    monkeypatch.setattr('daemons_companion.setup.codex_install.install_private_codex', install)
    monkeypatch.setattr('daemons_companion.setup.subscription.PrivateSubscriptionSetup', authenticate)
    ui = FakeUI()  # No further confirmation after choosing the provider.
    assert setup.connect_subscription(ui, 'hermes', 'codex_cli') is True
    assert setup.subscription_connected
    assert events == ['helper', 'sign-in']
    assert 'Install private Codex?' not in ui.output
    assert 'Connect ChatGPT' not in ui.output


@pytest.mark.parametrize('status', [SubscriptionStatus(False, True), SubscriptionStatus(True, False)])
def test_partial_signin_cannot_set_connected_state(tmp_path, monkeypatch, status):
    setup = private_setup(tmp_path, monkeypatch, 'windows')
    setup.subscription_connected = True  # A previous visit is not proof for this attempt.
    monkeypatch.setattr(setup, 'codex_executable', lambda _: tmp_path / 'codex')
    monkeypatch.setattr('daemons_companion.setup.subscription.PrivateSubscriptionSetup',
                        lambda *a, **k: SimpleNamespace(sign_in=lambda _, **kw: status))
    with pytest.raises(RuntimeError, match='sign-ins'):
        setup.connect_subscription(FakeUI(choices=[0]), 'hermes', 'codex_cli')
    assert not setup.subscription_connected


@pytest.mark.parametrize('outcome', [False, RuntimeError('Native sign-in cancelled'), Quit()])
def test_unfinished_connection_never_advances_to_model_or_telegram(outcome):
    def connect(*args):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'),
                            offer=lambda *a: False, connect_subscription=connect,
                            complete=lambda *a: pytest.fail('Telegram reached without sign-in'))
    ui = FakeUI(choices=[0, 0, 0, Quit()], fields=['Avery'])
    with pytest.raises(Quit):
        run_wizard(ui, backend=backend(), host_setup=setup)
    assert 'Model identifier (advanced preview only)' not in ui.output
    assert 'Continue installation' not in ui.output
    assert 'Next' not in ui.output


def test_connected_wizard_reaches_completion_with_identity_and_provider_intact():
    calls = []
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'),
                            offer=lambda *a: False, connect_subscription=lambda *a: True,
                            choose_model=lambda *a: 'test-model', complete=lambda ui, request, response: calls.append(request) or True)
    ui = FakeUI(choices=[0, 1, 0, 0, 0], fields=['Avery'])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 0
    assert calls[0]['daemon_gender'] == 'masculine'
    assert calls[0]['provider']['kind'] == 'codex_cli'
    assert calls[0]['provider']['model'] == 'test-model'
    assert not any('planning assumption' in line for line in ui.output)


@pytest.mark.parametrize('kind', [0, 1])
def test_review_only_connection_never_offers_executable_telegram(kind):
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'), changed=False,
                            subscription_connected=False, offer=lambda *a: False,
                            connect_subscription=lambda *a: None,
                            complete=lambda *a: pytest.fail('preview must not execute'))
    ui = FakeUI(choices=[0, 0, kind, 0, 2], fields=['Avery', 'test-model'])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 0
    assert 'Continue installation' not in ui.output
    assert any('Preview only' in line for line in ui.output)


@pytest.mark.parametrize('status', [SubscriptionStatus(False, True), SubscriptionStatus(True, False)])
def test_backend_rechecks_auth_before_any_telegram_input_or_configuration(tmp_path, monkeypatch, status):
    import daemons_companion.hermes.first_install as module
    layout, _, _ = fixture(tmp_path)
    owner = SimpleNamespace(layout=lambda: layout, codex_executable=lambda _: tmp_path / 'codex')
    monkeypatch.setattr(module, 'installed_host', lambda _: SimpleNamespace(config_path=str(layout.profile)))
    monkeypatch.setattr(module, 'PrivateSubscriptionSetup',
                        lambda *a, **k: SimpleNamespace(status=lambda: status))
    monkeypatch.setattr(module, 'configure_telegram', lambda *a, **k: pytest.fail('no auth'))
    ui = FakeUI()
    with pytest.raises(ValueError, match='both native ChatGPT sign-ins'):
        module.complete_private_setup(owner, ui, {'host': 'hermes', 'provider': {'kind': 'codex_cli'}}, {})
    assert not any('Telegram' in line for line in ui.output)


def test_slow_backend_calls_are_animated_and_prompts_are_not():
    from contextlib import contextmanager
    class UI(FakeUI):
        busy = False
        @contextmanager
        def activity(self, label):
            assert not self.busy
            self.busy = True
            try:
                yield self.say
            finally:
                self.busy = False
        def choose(self, *args, **kwargs):
            assert not self.busy
            return super().choose(*args, **kwargs)
        def field(self, *args, **kwargs):
            assert not self.busy
            return super().field(*args, **kwargs)
    ui = UI(choices=[0, 0, 1, 0, 2], fields=['Avery', 'synthetic'])
    underlying = backend()
    seen = []
    def slow(request):
        assert ui.busy
        seen.append(request['action'])
        return underlying(request)
    assert run_wizard(ui, backend=slow) == 0
    assert seen == ['inspect', 'host_setup', 'plan']
    assert not ui.busy
