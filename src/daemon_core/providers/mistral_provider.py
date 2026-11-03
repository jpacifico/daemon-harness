"""Mistral's native chat-completion protocol for shared daemon faculties."""
import os

from daemon_core.api_providers import api_request, completion_text
from daemon_core.provider_messages import mistral_messages
from .base import LLMProvider
from .telemetry import record_provider_usage


class MistralProvider(LLMProvider):
    def __init__(self, model=None, reasoning_effort=None, api_key=None):
        self.model = model or os.getenv('MISTRAL_MODEL', '')
        self.reasoning_effort = reasoning_effort
        self._key = api_key or os.getenv('MISTRAL_API_KEY', '')
        if not self._key or not self.model:
            raise RuntimeError('Mistral API key and model are required.')

    def complete(self, messages, temperature=0.4):
        # Keep native sampling; this transport does not change the model's reasoning settings.
        payload = {'model': self.model, 'messages': mistral_messages(messages), 'temperature': temperature}
        if temperature == 0:
            # Mistral rejects greedy sampling unless top_p is exactly 1 (error 3054).
            payload['top_p'] = 1
        result = api_request('mistral_api', self._key, '/chat/completions', payload)
        record_provider_usage(result.get('usage'))
        return completion_text('mistral_api', result)
