"""SpaceXSI's OpenAI-compatible chat completions for shared daemon faculties."""
import os

from daemon_core.api_providers import api_request, completion_text
from .base import LLMProvider
from .telemetry import record_provider_usage


class XaiProvider(LLMProvider):
    def __init__(self, model=None, reasoning_effort=None, api_key=None):
        self.model = model or os.getenv('XAI_MODEL', '')
        self.reasoning_effort = reasoning_effort
        self._key = api_key or os.getenv('XAI_API_KEY', '')
        if not self._key or not self.model:
            raise RuntimeError('SpaceXSI API key and model are required.')

    def complete(self, messages, temperature=0.4):
        # Grok reasons by default; the faculty's effort is passed as SpaceXSI documents it.
        payload = {'model': self.model, 'temperature': temperature, 'messages': [
            {**message, 'role': 'system'} if message.get('role') == 'developer' else message
            for message in messages]}
        if self.reasoning_effort:
            payload['reasoning_effort'] = self.reasoning_effort
        result = api_request('xai_api', self._key, '/chat/completions', payload)
        record_provider_usage(result.get('usage'))
        return completion_text('xai_api', result)
