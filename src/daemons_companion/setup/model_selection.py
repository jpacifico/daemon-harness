"""Bounded native model metadata and a paged installer picker."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from daemons_companion.desktop.terminal import Back, Quit, safe_text


@dataclass(frozen=True)
class ModelChoice:
    identifier: str
    label: str
    description: str = ''


def parse_catalog(raw: str) -> tuple[ModelChoice, ...]:
    if len(raw) > 256 * 1024:
        raise ValueError('The model list exceeded its size limit.')
    data = json.loads(raw)
    rows = data.get('models') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError('The model list format is invalid.')
    choices = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('visibility') != 'list':
            continue
        identifier = row.get('slug')
        if (not isinstance(identifier, str) or
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', identifier) is None):
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        label = row.get('display_name') or identifier
        description = row.get('description') or ''
        if not isinstance(label, str) or not isinstance(description, str):
            raise ValueError('The model display metadata is invalid.')
        label = ' '.join(safe_text(label).split())[:80] or identifier
        description = ' '.join(safe_text(description).split())[:96]
        priority = row.get('priority')
        rank = priority if type(priority) is int else 10_000
        choices.append((rank, ModelChoice(identifier, label, description)))
    if not choices:
        raise RuntimeError('The account returned no selectable models.')
    return tuple(item for _, item in sorted(choices, key=lambda pair: pair[0]))


# Product selection, explicitly approved for this installer. Availability still
# comes from the connected account; this is never an offline catalog fallback.
SUPPORTED_MODELS = (
    ModelChoice('gpt-6-astra', 'Astra', 'Recommended'),
    ModelChoice('gpt-6.1-sol', 'GPT-6.1-Sol'),
)


def select_model(ui, load, previous: str = '', *, resume=False, show_unavailable=True) -> str:
    while True:
        try:
            with ui.activity('Loading your available models...'):
                available = {item.identifier for item in load()}
                models = tuple(item for item in SUPPORTED_MODELS if item.identifier in available)
            if not models:
                raise RuntimeError('No supported models available.')
        except (OSError, RuntimeError, ValueError):
            ui.say('None of the three supported models could be loaded from your account. Check your connection and ChatGPT access.')
            choice = ui.choose('Model list unavailable', ('Try again', 'Back to model access', 'Quit'))
            if choice == 1:
                raise Back
            if choice == 2:
                raise Quit
            continue
        if resume and previous in {item.identifier for item in models}:
            ui.say('Saved model is available. Continuing setup.')
            return previous
        resume = False
        ui.say('Choose your daemon model. Astra is recommended; account limits still apply.')
        missing = [item.label for item in SUPPORTED_MODELS if item.identifier not in available]
        if missing and show_unavailable:
            ui.say('Not listed by the connected host: ' + ', '.join(missing) + '.')
        selected = next((i for i, item in enumerate(models) if item.identifier == previous), 0)
        labels = [item.label + (' — ' + item.description if item.description else '') for item in models]
        choice = ui.choose('Choose your model', tuple(labels + ['Refresh model list']), default=selected)
        if choice == len(models):
            continue
        result = models[choice]
        ui.say(f'Selected: {result.label} ({result.identifier})')
        return result.identifier
