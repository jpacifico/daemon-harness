"""Fixed hosted API endpoints and bounded, credential-safe setup requests."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class ApiSpec:
    label: str
    base_url: str
    secret_name: str
    host_provider: str


API_PROVIDERS = {
    'mistral_api': ApiSpec('Mistral AI', 'https://api.mistral.ai/v1', 'MISTRAL_API_KEY', 'custom:daemon-mistral'),
    'openai_api': ApiSpec('OpenAI', 'https://api.openai.com/v1', 'OPENAI_API_KEY', 'openai-api'),
    'anthropic_api': ApiSpec('Anthropic', 'https://api.anthropic.com/v1', 'ANTHROPIC_API_KEY', 'anthropic'),
    'xai_api': ApiSpec('SpaceXSI', 'https://api.x.ai/v1', 'XAI_API_KEY', 'custom:daemon-xai'),
}
# Only models the owner approved are offered (docs/model_providers.md); Opus 5.5
# is under test since 2026-10-29. Availability is always checked against the
# account, never assumed from this list.
CLAUDE_MODELS = ('claude-opus-5-5',)
OPENAI_MODELS = ('gpt-6-astra', 'gpt-6.1-sol')
MISTRAL_RECOMMENDED = 'mistral-large-4'
# Under qualification (docs/proposals/2026-10-29_model_qualification.md).
XAI_MODELS = ('grok-4.7',)


class ApiConnectionError(RuntimeError):
    """Only safe, fixed messages may cross the desktop/diagnostic boundary."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_key(value: str) -> str:
    key = value.strip()
    if not key or len(key.encode('utf-8')) > 1500 or not key.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in key):
        raise ApiConnectionError('Enter the complete API key without spaces.')
    return key


def api_request(kind, key, path, payload=None, *, opener=None):
    spec = API_PROVIDERS[kind]
    key = validate_key(key)
    if path not in ('/models', '/chat/completions', '/messages') and not re.fullmatch(r'/models\?limit=100&after_id=[A-Za-z0-9._-]{1,160}', path):
        raise ValueError('Unsupported API operation.')
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    if kind == 'anthropic_api':
        headers['anthropic-version'] = '2023-06-01'
    request = Request(spec.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        with (opener or build_opener(_NoRedirects())).open(request, timeout=120 if payload else 30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ApiConnectionError('The provider response exceeded its size limit.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
    except HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            message = 'The provider rejected this API key or its permissions. Check the key and try again.'
        elif status == 429:
            message = 'The provider reports a usage limit. Check your API balance or try again later.'
        elif status in (400, 404, 422):
            message = 'The provider cannot use this model with the requested settings. Choose another model or check access.'
        else:
            message = 'The provider is unavailable. Try again later.'
        raise ApiConnectionError(message) from None
    except (URLError, TimeoutError, OSError):
        raise ApiConnectionError('Cannot reach the provider. Check your internet connection and try again.') from None
    except (ValueError, UnicodeError):
        raise ApiConnectionError('The provider returned an invalid response. Try again later.') from None


def model_catalog(kind, key, *, request=api_request):
    rows, path, seen_pages = [], '/models', set()
    for _ in range(10):
        data = request(kind, key, path)
        page = data.get('data')
        if not isinstance(page, list) or len(page) > 2048:
            raise ApiConnectionError('The provider returned an invalid model list.')
        rows.extend(page)
        if kind != 'anthropic_api' or not data.get('has_more'):
            break
        last = data.get('last_id')
        if not isinstance(last, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,160}', last) or last in seen_pages:
            raise ApiConnectionError('The provider returned an invalid model list.')
        seen_pages.add(last)
        path = '/models?limit=100&after_id=' + last
    else:
        raise ApiConnectionError('The provider model list exceeded its size limit.')
    available = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', identifier):
            continue
        if kind == 'mistral_api':
            capabilities = row.get('capabilities') or {}
            if not isinstance(capabilities, dict) or capabilities.get('completion_chat') is not True or capabilities.get('function_calling') is not True:
                continue
        available[identifier] = row
        if kind == 'mistral_api' and isinstance(row.get('aliases'), list) and MISTRAL_RECOMMENDED in row['aliases']:
            available[MISTRAL_RECOMMENDED] = row
    if kind == 'openai_api':
        identifiers = [m for m in OPENAI_MODELS if m in available]
    elif kind == 'anthropic_api':
        identifiers = [m for m in CLAUDE_MODELS if m in available]
    elif kind == 'xai_api':
        identifiers = [m for m in XAI_MODELS if m in available]
    else:
        identifiers = sorted(available, key=lambda m: (m != MISTRAL_RECOMMENDED, m))[:128]
    if not identifiers:
        raise ApiConnectionError('No supported conversation models are available with this key.')
    return tuple(identifiers)


def anthropic_payload(model, messages, effort='high', *, max_tokens=32768):
    system, dialogue = [], []
    for message in messages:
        role, content = message.get('role'), message.get('content')
        if not isinstance(content, str):
            raise ValueError('Daemon maintenance expects text messages.')
        if role in ('system', 'developer'):
            system.append({'type': 'text', 'text': content})
        elif role in ('user', 'assistant'):
            dialogue.append({'role': role, 'content': content})
        else:
            raise ValueError('Unsupported maintenance message role.')
    if not dialogue:
        raise ValueError('A user message is required.')
    payload = {'model': model, 'max_tokens': max_tokens, 'messages': dialogue,
               'thinking': {'type': 'adaptive'}, 'output_config': {'effort': effort or 'high'}}
    if system:
        payload['system'] = system
    return payload


def completion_text(kind, data):
    try:
        if kind == 'anthropic_api':
            if data.get('stop_reason') != 'end_turn':
                raise ValueError()
            value = ''.join(block['text'] for block in data['content'] if block.get('type') == 'text')
        else:
            choice = data['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ApiConnectionError('The model reached its response limit before finishing. Try again or choose another model; your API key is still connected.')
            if choice.get('finish_reason') != 'stop':
                raise ValueError()
            value = choice['message']['content']
            if kind == 'mistral_api' and isinstance(value, list):
                # Mistral can return typed thinking and text chunks. Only final
                # text is an answer; nested reasoning must never be substituted.
                value = ''.join(block['text'] for block in value if block.get('type') == 'text')
        if not isinstance(value, str) or not value.strip():
            raise ValueError()
        return value.strip()
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        raise ApiConnectionError('The provider did not return a complete text response. Try another model.') from None


def verify_model(kind, key, model, *, request=api_request):
    messages = [{'role': 'user', 'content': 'Reply with OK.'}]
    if kind == 'mistral_api':
        from daemon_core.provider_messages import mistral_messages
        messages = mistral_messages([
            {'role': 'system', 'content': 'Connection test. Answer briefly.'},
            {'role': 'developer', 'content': 'Synthetic background for the connection test.'},
            *messages,
        ])
    if kind == 'anthropic_api':
        payload = anthropic_payload(model, messages, 'medium', max_tokens=4096)
        path = '/messages'
    else:
        payload = {'model': model, 'messages': messages,
                   ('max_completion_tokens' if kind == 'openai_api' else 'max_tokens'): 4096}
        # Do not cap below the reasoning budget or use sampling parameters that
        # reasoning models reject. No private content is sent by this probe.
        path = '/chat/completions'
    return bool(completion_text(kind, request(kind, key, path, payload)))


def hermes_model_config(kind, model):
    spec = API_PROVIDERS[kind]
    config = {'provider': spec.host_provider, 'default': model}
    if kind in ('mistral_api', 'xai_api'):
        config.update({'base_url': spec.base_url, 'api_mode': 'chat_completions', 'key_env': spec.secret_name})
    return config


def hermes_provider_entries(kind, model):
    if kind not in ('mistral_api', 'xai_api'):
        return {}
    spec = API_PROVIDERS[kind]
    return {spec.host_provider.removeprefix('custom:'): {'name': spec.label, 'base_url': spec.base_url,
            'api_key_env': spec.secret_name, 'default_model': model, 'transport': 'chat_completions'}}
