"""Synthetic GUI transport fixture; no accounts, downloads, hosts or services."""
from pathlib import Path
import sys
import os
from daemons_companion.desktop.protocol import DesktopUI
from daemons_companion.desktop.terminal import Quit
from daemons_companion.setup.telegram import verify_telegram_token, TelegramSetupError

ui = DesktopUI(sys.stdin, sys.stdout)
ui.start()
try:
    ui.phase('identity')
    name = ui.field('What is your daemon’s name?', maximum=80)
    ui.say('Synthetic identity: ' + name)
    ui.first_meeting_preferences()
    ui.phase('model')
    # Exercise the real API onboarding transport with synthetic provider replies.
    from dataclasses import dataclass
    from types import SimpleNamespace
    import daemons_companion.setup.api_setup as api_setup
    from daemon_core.api_providers import ApiConnectionError, verify_model
    @dataclass
    class Layout:
        installation_id: str = 'synthetic'
        @property
        def attempt(self): return Path(sys.argv[1]).parent / self.installation_id
    owner = SimpleNamespace(layout=Layout)
    api_setup.installed_host = lambda layout: SimpleNamespace(config_path=str(layout.attempt / 'config.yaml'))
    def catalog(kind, key):
        if key != 'synthetic-valid-api-key':
            raise ApiConnectionError('The provider rejected this API key or its permissions. Check the key and try again.')
        return ('gpt-6-astra',)
    api_setup.model_catalog = catalog
    def completion(kind, key, path, payload):
        assert key == 'synthetic-valid-api-key' and payload['model'] == 'gpt-6-astra'
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}]}
    api_setup.verify_model = lambda kind, key, model: verify_model(kind, key, model, request=completion)
    from daemons_companion.setup.model_access import options
    kinds, labels = options()
    kind = kinds[ui.choose('3. Model access to prepare', labels)]
    assert kind == 'openai_api'
    assert api_setup.connect_api(owner, ui, 'hermes', kind)
    model = api_setup.choose_api_model(owner, ui)
    assert model == 'gpt-6-astra'
    # Use the actual packaged preparation and review formatter. Previously this
    # fixture skipped review and missed missing Mistral/Anthropic provider keys.
    from daemons_companion.desktop.app import handle_request, preparation_context_from_environment, REQUEST_SCHEMA
    from daemons_companion.setup.plan import HostKind, HostProbe
    context = preparation_context_from_environment(os.environ)
    profile = Path(sys.argv[1]).parent / 'synthetic-profile'
    prepared = handle_request({
        'schema': REQUEST_SCHEMA, 'action': 'prepare', 'host': 'hermes',
        'daemon_name': name, 'daemon_gender': 'feminine',
        'provider': {'kind': kind, 'model': model, 'base_url': '', 'credential_available': True},
    }, probes=(HostProbe(HostKind.HERMES, True, True, str(profile / 'hermes'), str(profile), '0.21.5', 'verified'),
               HostProbe(HostKind.OPENCLAW, False, False, '', '', '', 'unavailable')),
        preparation_context=context, finder=lambda _: None)
    assert model in prepared['review_text'] and 'OpenAI' in prepared['review_text']
    # The native credential store round-trip is synthetic and cleaned in finally.
    import uuid
    from daemons_companion.setup.credentials import system_credential_store
    store = system_credential_store()
    reference = 'provider-' + str(uuid.uuid4())
    try:
        store.put(reference, 'synthetic-store-check')
        assert store.get(reference) == 'synthetic-store-check'
        import subprocess, tempfile
        with tempfile.TemporaryDirectory(prefix='daemon-store-home-') as private_home:
            environment = dict(os.environ, HOME=private_home)
            result = subprocess.run([sys.executable, '-B', '-c',
                'import sys;sys.path[:0]=' + repr(sys.path) + ';'
                'from daemons_companion.setup.credentials import system_credential_store;'
                'assert system_credential_store().get(sys.argv[1]) == "synthetic-store-check"', reference],
                env=environment, capture_output=True, timeout=40)
            assert result.returncode == 0, 'Synthetic private interpreter failed: ' + result.stderr.decode(errors='replace')[-1000:]

    finally:
        store.delete(reference)
    assert store.get(reference) is None
    owner.api_connection = None
    ui.phase('telegram')
    ui.say('Open Telegram Web and sign in on this computer.')
    ui.choose('Connect Telegram Web', ('Open Telegram Web',))
    ui.open_link('https://web.telegram.org/')
    ui.choose('Connect Telegram Web', ('I am signed in — continue',))
    def configure(token):
        if token != 'synthetic-valid-token':
            raise TelegramSetupError('Telegram could not verify that token. Please try again.')
        return {'username': 'SyntheticDaemon_bot'}
    verify_telegram_token(ui, configure)
    ui.phase('installation')
    from daemons_companion.desktop.memory import initialize_empty_vault
    root = initialize_empty_vault(Path(sys.argv[1]))
    from daemon_core.daemon_identity import save_daemon_identity, load_daemon_identity
    save_daemon_identity(root, name, 'feminine')
    assert not load_daemon_identity(root).user
    assert ui.conversation_language == 'fr'
    ui.say('Synthetic empty vault created with five root folders.')
    ui.phase('conversation')
    ui.say('Open https://t.me/SyntheticDaemon_bot')
    with ui.activity('Waiting for a real Telegram reply and completed memory work…'):
        # A synthetic scenario never declares a verified conversation.
        pass
    ui.choose('No real conversation has been verified', ('Close test',))
    ui.emit('finished', result='unfinished', message='Transport test complete. No model or Telegram connection was made.')
except (Quit, KeyboardInterrupt):
    ui.emit('finished', result='cancelled', message='Synthetic setup cancelled.')
finally:
    ui.finished = True
