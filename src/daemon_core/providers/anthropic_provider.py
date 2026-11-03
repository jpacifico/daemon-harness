"""Anthropic Messages adapter for daemon maintenance, not the native host loop."""
import os

from daemon_core.api_providers import api_request, anthropic_payload, completion_text
from .base import LLMProvider
from .telemetry import record_provider_usage


class AnthropicProvider(LLMProvider):
    def __init__(self, model=None, reasoning_effort=None, api_key=None):
        self.model = model or os.getenv('ANTHROPIC_MODEL', '')
        self.reasoning_effort = reasoning_effort
        self._key = api_key or os.getenv('ANTHROPIC_API_KEY', '')
        if not self._key or not self.model:
            raise RuntimeError('Anthropic API key and model are required.')

    def complete(self, messages, temperature=0.4):
        # Adaptive thinking owns sampling; temperature is deliberately omitted.
        result = api_request('anthropic_api', self._key, '/messages',
                             anthropic_payload(self.model, messages, self.reasoning_effort))
        record_provider_usage(result.get('usage'))
        return completion_text('anthropic_api', result)
