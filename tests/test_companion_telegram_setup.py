from types import SimpleNamespace

import pytest

from daemons_companion.setup.telegram import prepare_telegram
from daemons_companion.desktop.terminal import Back, Quit, Terminal, run_wizard
from tests.test_companion_terminal import FakeUI, backend


class TelegramUI(FakeUI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.links = []
        self.fields_seen = []

    def open_link(self, url):
        self.links.append(url)

    def field(self, label, previous='', **kwargs):
        self.fields_seen.append((label, previous))
        return super().field(label, previous, **kwargs)


def test_web_links_stay_local_and_explain_the_personal_id():
    ui = TelegramUI(choices=[0, 1, 1, 0, 0], fields=['12345'])
    assert prepare_telegram(ui, '/synthetic/private') == '12345'
    assert ui.links == ['https://web.telegram.org/', 'https://web.telegram.org/']
    assert len(ui.fields_seen) == 1
    assert any('not your phone number' in line for line in ui.output)
    assert any('only the digits' in line.lower() and '“Id” line' in line for line in ui.output)
    assert any('/start yourself' in line for line in ui.output)
    assert any('Search for @BotFather' in line for line in ui.output)


def test_invalid_telegram_id_stays_on_field_and_back_returns_to_botfather():
    ui = TelegramUI(choices=[1, 1, 0, 0], fields=['@name', 'secret-not-an-id', Back(), '12345'])
    assert prepare_telegram(ui, '/synthetic/private') == '12345'
    assert ui.output.count('Create your Telegram bot') == 1
    assert len(ui.fields_seen) == 4
    assert 'secret-not-an-id' not in '\n'.join(ui.output)


def test_back_at_confirmation_retains_telegram_id():
    ui = TelegramUI(choices=[1, 0, Back(), Back(), 0, 0], fields=['12345', '12345'])
    assert prepare_telegram(ui, '/synthetic/private') == '12345'
    assert ui.fields_seen[1][1] == '12345'


@pytest.mark.parametrize('choices', [[2], [Quit()], [1, 0, 2]])
def test_telegram_quit_or_later_stops(choices):
    ui = TelegramUI(choices=choices, fields=['12345'])
    with pytest.raises(Quit):
        prepare_telegram(ui, '/synthetic/private')


def test_telegram_back_returns_to_review_without_reloading_model():
    models = []
    attempts = []
    def choose_model(*a):
        models.append(1)
        return 'selected-model'
    def complete(*a):
        attempts.append(1)
        if len(attempts) == 1:
            raise Back
        return True
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'),
                            offer=lambda *a: False, connect_subscription=lambda *a: True,
                            choose_model=choose_model, complete=complete)
    ui = FakeUI(choices=[0, 0, 0, 0, 0, 0, 0], fields=['Avery'])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 0
    assert models == [1] and attempts == [1, 1]


@pytest.mark.parametrize('return_choice', [0, Back()])
def test_telegram_failure_offers_review_or_quit_and_keeps_selected_model(return_choice):
    attempts = []
    def complete(ui, request, response):
        attempts.append(request['provider']['model'])
        if len(attempts) == 1:
            raise RuntimeError('Telegram setup failed. Check the token and network.')
        return True
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'),
                            offer=lambda *a: False, connect_subscription=lambda *a: True,
                            choose_model=lambda *a: 'selected-model', complete=complete)
    ui = FakeUI(choices=[0, 0, 0, 0, 0, return_choice, 0, 0], fields=['Avery'])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 0
    assert attempts == ['selected-model', 'selected-model']
    assert 'Setup did not finish' in ui.output
    assert ui.output.count('3. Model access to prepare') == 1


def test_link_opening_uses_existing_browser_configuration_without_profile_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr('daemons_companion.desktop.terminal.webbrowser.open',
                        lambda url, **kwargs: calls.append((url, kwargs)) or True)
    ui = Terminal(plain=True)
    ui.open_link('https://web.telegram.org/')
    assert calls == [('https://web.telegram.org/', {'new': 2})]
    with pytest.raises(ValueError):
        ui.open_link('file:///somewhere')
    assert len(calls) == 1


def test_failed_browser_launch_gives_computer_url(monkeypatch, capsys):
    monkeypatch.setattr('daemons_companion.desktop.terminal.webbrowser.open', lambda *a, **kw: False)
    Terminal(plain=True).open_link('https://web.telegram.org/')
    assert 'https://web.telegram.org/ in your usual computer browser.' in ' '.join(capsys.readouterr().out.split())


def test_install_is_first_and_default_after_account_id():
    class UI(TelegramUI):
        def choose(self, title, options, *, default=0):
            if title == 'Install and start this private daemon?':
                assert options[default] == 'Install and start'
                return default
            return super().choose(title, options, default=default)
    assert prepare_telegram(UI(choices=[1, 0], fields=['12345']), '/private') == '12345'


def test_retry_token_stays_local_and_confirms_only_verified_bot():
    from daemons_companion.setup.telegram import TelegramSetupError, verify_telegram_token
    class UI(TelegramUI):
        def secret(self, label):
            return next(tokens)
    tokens = iter(['  wrong-token  ', '  123456:correct_token_here  '])
    ui = UI(choices=[1, 0])
    attempts = []
    def configure(token):
        attempts.append(token)
        assert not any('Token accepted' in line for line in ui.output)
        if len(attempts) == 1:
            raise TelegramSetupError('Telegram rejected this token.')
        return {'username': 'synthetic_bot', 'bot_id': 123}
    assert verify_telegram_token(ui, configure)['username'] == 'synthetic_bot'
    assert attempts == ['wrong-token', '123456:correct_token_here']
    assert ui.links == ['https://web.telegram.org/']
    assert not ui.fields_seen
    output = '\n'.join(ui.output)
    assert 'Token accepted — Telegram verified @synthetic_bot' in output
    assert all(token not in output for token in attempts)


@pytest.mark.parametrize('choice', [2, Quit()])
def test_quitting_bad_token_does_not_start_another_verification(choice):
    from daemons_companion.setup.telegram import TelegramSetupError, verify_telegram_token
    ui = TelegramUI(choices=[choice])
    ui.secret = lambda _: 'wrong'
    calls = []
    def configure(token):
        calls.append(token)
        raise TelegramSetupError('Invalid token.')
    with pytest.raises(Quit):
        verify_telegram_token(ui, configure)
    assert calls == ['wrong']
    assert not any('Token accepted' in line for line in ui.output)


def test_quit_from_hidden_field_never_submits_secret():
    from daemons_companion.setup.telegram import verify_telegram_token
    ui = TelegramUI()
    ui.secret = lambda _: ':quit'
    with pytest.raises(Quit):
        verify_telegram_token(ui, lambda _: pytest.fail('must not configure'))


def test_a_download_outage_explains_itself_and_offers_retry():
    from daemons_companion.runtime.acquisition import DOWNLOAD_UNREACHABLE, HostDownloadUnavailable
    attempts = []
    def complete(ui, request, response):
        attempts.append(1)
        if len(attempts) == 1:
            raise HostDownloadUnavailable(DOWNLOAD_UNREACHABLE.format(service='GitHub'))
        return True
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'),
                            offer=lambda *a: False, connect_subscription=lambda *a: True,
                            choose_model=lambda *a: 'selected-model', complete=complete)
    class Recording(FakeUI):
        offered = []
        def choose(self, title, options, *, default=0):
            if title == 'Setup did not finish':
                self.offered.append(options)
            return super().choose(title, options, default=default)
    ui = Recording(choices=[0, 0, 0, 0, 0, 0, 0, 0], fields=['Avery'])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 0
    assert attempts == [1, 1]
    assert ui.offered == [('Retry', 'Change model access', 'Quit')]
    assert DOWNLOAD_UNREACHABLE.format(service='GitHub') in ui.output
    assert not any(line.startswith('Please review:') for line in ui.output)
