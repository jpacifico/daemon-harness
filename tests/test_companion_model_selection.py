from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from daemons_companion.setup.model_selection import ModelChoice, parse_catalog, select_model
from daemons_companion.desktop.terminal import Back, Quit, run_wizard
from tests.test_companion_terminal import FakeUI, backend
from tests.test_companion_subscription import operation


BRIDGE = Path(__file__).resolve().parents[1] / 'integrations/hermes_plugin/companion_models.py'


def row(identifier, **kwargs):
    return {'slug': identifier, 'display_name': identifier, 'visibility': 'list', **kwargs}


def test_live_catalog_preserves_identifiers_and_provider_order_without_synthesis():
    choices = parse_catalog(json.dumps({'models': [
        row('future-one', priority=3), row('future-two', priority=1, supported_in_api=False),
        row('internal', visibility='hide'), row('future-one', priority=0),
        row('bad\x1bid'), row('undisclosed', visibility=None),
    ]}))
    assert [item.identifier for item in choices] == ['future-two', 'future-one']


def test_catalog_display_is_safe_but_exact_identifier_is_not_rewritten():
    choice, = parse_catalog(json.dumps({'models': [row('future-exact', display_name='Name\x1b[2J',
                                                      description='One\nTwo\u202e')]}))
    assert choice.identifier == 'future-exact'
    assert '\x1b' not in choice.label and '\u202e' not in choice.description
    assert '\n' not in choice.description


@pytest.mark.parametrize('payload', ['not-json', '{}', '[]', '{"models":[]}',
                                    json.dumps({'models': [row('hidden', visibility='hide')]}),
                                    json.dumps({'models': [row('x')] * 257}), ' ' * 262145])
def test_absent_invalid_or_oversized_catalog_never_becomes_a_static_list(payload):
    with pytest.raises((ValueError, RuntimeError)):
        parse_catalog(payload)


def models(count=3):
    ids = ('gpt-6-astra', 'gpt-6.1-sol', 'gpt-5.6-terra')
    return tuple(ModelChoice(identifier, 'Remote label') for identifier in ids[:count])


@pytest.mark.parametrize('choice,expected', [(0, 'gpt-6-astra'), (1, 'gpt-6.1-sol')])
def test_picker_limits_and_orders_supported_models_with_requested_guidance(choice, expected):
    class UI(FakeUI):
        def choose(self, title, options, *, default=0):
            # GPT-5.6-Terra was withdrawn on 2026-10-29 even when the account lists it.
            assert options == ('Astra — Recommended', 'GPT-6.1-Sol', 'Refresh model list')
            assert default == 0
            return super().choose(title, options, default=default)
    ui = UI(choices=[choice])
    def catalog():
        return (ModelChoice('other', 'Other'), *reversed(models()))
    assert select_model(ui, catalog) == expected
    assert not any('degraded' in line for line in ui.output)


def test_picker_refreshes_and_never_offers_a_model_missing_from_the_account():
    calls = []
    class UI(FakeUI):
        def choose(self, title, options, *, default=0):
            calls.append((options, default))
            return super().choose(title, options, default=default)
    catalogs = iter([models(), (models()[1],)])
    ui = UI(choices=[2, 0])
    assert select_model(ui, lambda: next(catalogs), previous='gpt-6.1-sol') == 'gpt-6.1-sol'
    assert calls[0][1] == 1
    assert calls[1] == (('GPT-6.1-Sol', 'Refresh model list'), 0)
    assert any('Not listed by the connected host: Astra.' == line for line in ui.output)


def test_catalog_without_supported_models_offers_retry_back_quit_only():
    ui = FakeUI(choices=[2])
    with pytest.raises(Quit):
        select_model(ui, lambda: (ModelChoice('unknown', 'Unknown'),))
    assert 'Choose your model' not in ui.output


@pytest.mark.parametrize('exit_choice,error', [(1, Back), (2, Quit)])
def test_loading_failure_has_no_manual_fallback_and_respects_exit(exit_choice, error):
    def load():
        raise RuntimeError('secret-http-detail')
    ui = FakeUI(choices=[exit_choice])
    with pytest.raises(error):
        select_model(ui, load)
    assert 'secret-http-detail' not in '\n'.join(ui.output)


def test_loading_failure_can_retry():
    calls = []
    def load():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('offline')
        return models(1)
    assert select_model(FakeUI(choices=[0, 0]), load) == 'gpt-6-astra'
    assert len(calls) == 2


def test_selected_live_model_reaches_plan_and_installation_without_a_model_text_field():
    seen = []
    setup = SimpleNamespace(context=SimpleNamespace(target_os='windows'),
                            offer=lambda *a: False, connect_subscription=lambda *a: True,
                            choose_model=lambda ui, previous: select_model(ui, models, previous),
                            complete=lambda ui, request, response: seen.append(request) or True)
    ui = FakeUI(choices=[0, 0, 0, 1, 0, 0], fields=['Avery'])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 0
    assert seen[0]['provider']['model'] == 'gpt-6.1-sol'
    assert 'Model identifier (advanced preview only)' not in ui.output


def test_native_catalog_uses_only_the_private_host_environment(tmp_path):
    setup, _ = operation(tmp_path)
    calls = []
    def runner(command, env, **kwargs):
        calls.append((command, env))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'models': [row('account-model')]}))
    setup.runner = runner
    assert setup.model_choices(BRIDGE)[0].identifier == 'account-model'
    command, env = calls[0]
    assert command == [str(setup.layout.python), '-B', str(BRIDGE)]
    assert env['HERMES_HOME'] == str(setup.layout.profile)
    assert env['CODEX_HOME'] == str(setup.layout.attempt / 'os-home/.hermes-codex')
    assert not any('auth.json' in arg for arg in command)


@pytest.mark.parametrize('code,stdout', [(1, 'private-secret'), (0, 'private-secret')])
def test_native_catalog_failures_never_expose_output(tmp_path, code, stdout):
    setup, _ = operation(tmp_path)
    setup.runner = lambda *a, **k: SimpleNamespace(returncode=code, stdout=stdout)
    with pytest.raises(RuntimeError) as error:
        setup.model_choices(BRIDGE)
    assert 'private-secret' not in str(error.value)


def bridge_module():
    spec = importlib.util.spec_from_file_location('companion_models_bridge', BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_modules(monkeypatch, *, entries, status=200):
    token = 'synthetic-private-token'
    def resolve(*, read_only):
        assert read_only is True
        return {'api_key': token}
    def headers(value):
        assert value == token
        return {'ChatGPT-Account-ID': 'synthetic-account', 'x-data-residency': 'synthetic-region'}
    def get(url, *, headers, timeout):
        assert headers['Authorization'] == 'Bearer ' + token
        assert headers['ChatGPT-Account-ID'] == 'synthetic-account'
        assert headers['x-data-residency'] == 'synthetic-region'
        assert timeout == 10
        return SimpleNamespace(status_code=status)
    def fetch(get):
        result = get('https://example.invalid/native-catalog')
        return entries, result.status_code
    for name, attrs in {
        'httpx': {'get': get},
        'hermes_cli.auth': {'resolve_codex_runtime_credentials': resolve,
                            '_codex_access_token_is_expiring': lambda *a: False},
        'agent.codex_headers': {'codex_account_headers': headers},
        'agent.model_metadata': {'fetch_codex_catalog_entries': fetch},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)


def test_host_bridge_delegates_auth_headers_and_urls_and_exports_only_display_metadata(monkeypatch):
    entries = [row('account-specific', priority=1, description='Description',
                   base_instructions='do not export', api_key='do not export'),
               row('hidden', visibility='hide')]
    native_modules(monkeypatch, entries=entries)
    raw = bridge_module().read_catalog()
    assert 'do not export' not in json.dumps(raw)
    assert 'synthetic-private-token' not in json.dumps(raw)
    assert parse_catalog(json.dumps(raw)) == (ModelChoice('account-specific', 'account-specific', 'Description'),)


@pytest.mark.parametrize('entries,status', [([], 200), ([row('model')], 403)])
def test_empty_or_denied_live_catalog_never_falls_back_to_host_defaults(monkeypatch, entries, status):
    native_modules(monkeypatch, entries=entries, status=status)
    with pytest.raises(RuntimeError, match='No live catalog'):
        bridge_module().read_catalog()


def test_host_bridge_suppresses_private_exception_details(monkeypatch, capsys):
    module = bridge_module()
    def fail():
        raise RuntimeError('synthetic-private-token')
    monkeypatch.setattr(module, 'read_catalog', fail)
    monkeypatch.setattr(module.logging, 'disable', lambda _: None)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert 'synthetic-private-token' not in captured.err


def test_resume_rechecks_live_catalog_before_reusing_model():
    calls = []
    def load():
        calls.append(1)
        return models()
    assert select_model(FakeUI(), load, "gpt-6-astra", resume=True) == "gpt-6-astra"
    assert calls == [1]
    ui = FakeUI(choices=[0])
    assert select_model(ui, lambda: models(1), "gpt-6.1-sol", resume=True) == "gpt-6-astra"
    assert "Choose your model" in ui.output
