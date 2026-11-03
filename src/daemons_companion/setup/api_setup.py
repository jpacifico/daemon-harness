"""API key/model onboarding; secrets never enter the public installation plan."""
from dataclasses import dataclass, field, replace

from daemon_core.api_providers import API_PROVIDERS, ApiConnectionError, model_catalog, validate_key, verify_model
from daemons_companion.runtime.host_install import installed_host


@dataclass
class ApiConnection:
    kind: str
    attempt: str
    key: str = field(repr=False)
    models: tuple[str, ...] = ()
    verified_model: str = ''


def connect_api(owner, ui, host, kind):
    from pathlib import Path
    from daemons_companion.desktop.terminal import Back, Quit
    owner.api_connection = None
    if host == 'openclaw':
        from daemons_companion.openclaw.first_install import reconnect_api
        return reconnect_api(owner, ui, kind)
    if host != 'hermes' or kind not in API_PROVIDERS:
        return None
    with ui.activity('Checking your private installation...'):
        layout = owner.layout()
        managed = installed_host(layout)
        if managed is None:
            ui.say('Create a separate private Hermes installation before connecting an API key.')
            return False
        layout = replace(layout, installation_id=Path(managed.config_path).parent.name)
    if kind == 'mistral_api':
        ui.say('Use your Mistral API or Vibe key for conversations and memory maintenance. Usage follows the Mistral plan, allowance and billing settings linked to your key.')
    else:
        ui.say('Your API key is used for conversations and memory maintenance. API usage is billed by your provider, separately from subscriptions.')
    ui.say('The key is stored in your operating system credential store when installation completes.')
    reused = reused_key(ui, kind)
    while True:
        key = ''
        try:
            key = validate_key(reused or ui.secret('Paste your Mistral API or Vibe key (hidden)' if kind == 'mistral_api' else 'Paste your API key (hidden)'))
            reused = ''
            with ui.activity('Checking your API key and available models...'):
                models = model_catalog(kind, key)
            owner.api_connection = ApiConnection(kind, str(layout.attempt), key, models)
            ui.say('API key accepted. Choose your model next.')
            return True
        except ApiConnectionError as error:
            key = reused = ''
            ui.say(str(error))
            action = ui.choose('API connection not ready', ('Enter API key again', 'Back to model access', 'Quit'))
            if action == 1:
                raise Back
            if action == 2:
                raise Quit


def reused_key(ui, kind):
    """A host change first tries the key of the daemon's current host."""
    from daemons_companion.hosts.change import pending_change
    change = pending_change()
    if change is None or change.identity['kind'] != kind:
        return ''
    key = change.key()
    if key:
        ui.say('Using the API key of your daemon\'s current host.')
    return key


def choose_api_model(owner, ui, previous='', *, resume=False):
    from daemons_companion.desktop.terminal import Back, Quit
    while True:
        connection = owner.api_connection
        if connection is None:
            raise ValueError('Connect your API key first.')
        models = connection.models
        labels = []
        for model in models:
            label = model
            if model == 'gpt-6-astra':
                label = 'Astra — Recommended'
            labels.append(label)
        index = (models.index(previous) if resume and previous in models else
                 ui.choose('Choose your model', tuple(labels), default=models.index(previous) if previous in models else 0))
        resume = False
        model = models[index]
        connection.verified_model = ''
        ui.say('A short connection test will send only “Reply with OK.” to this model. Your provider’s usage limits and billing settings apply.')
        previous = model
        while True:
            try:
                with ui.activity('Testing your model connection...'):
                    verify_model(connection.kind, connection.key, model)
                connection.verified_model = model
                ui.say('Model connection verified. You can continue to Telegram.')
                return model
            except ApiConnectionError as error:
                ui.say(str(error))
                ui.say(f'Model not connected: {model}. Your API key is retained for another attempt.')
                action = ui.choose('Model connection not ready', ('Try this model again', 'Choose another model', 'Enter API key again', 'Quit'))
                if action == 0:
                    continue
                if action == 2:
                    if not connect_api(owner, ui, getattr(owner, 'selected_host', 'hermes'), connection.kind):
                        raise Back
                elif action == 3:
                    raise Quit
                break
