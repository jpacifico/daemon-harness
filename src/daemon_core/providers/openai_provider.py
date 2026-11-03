import os
from .base import LLMProvider, unsupported_optional_param
from .telemetry import record_provider_usage
class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        api_key: str | None = None,
    ) -> None:
        from openai import OpenAI

        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            raise RuntimeError("OPENAI_API_KEY est manquant.")
        self.client = OpenAI(api_key=resolved_key)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.reasoning_effort = reasoning_effort
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        result = self._create(messages=messages, temperature=temperature)
        record_provider_usage(getattr(result, "usage", None))
        return result.choices[0].message.content.strip()

    def stream_complete(self, messages: list[dict], temperature: float = 0.4):
        stream = self._create(messages=messages, temperature=temperature, stream=True)
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

    def _create(self, messages: list[dict], temperature: float, stream: bool = False):
        params: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        if stream:
            params["stream"] = True
        # Depending on the model, temperature or reasoning_effort may
        # be rejected. Remove the rejected optional parameter and retry
        # until no optional parameters remain.
        while True:
            try:
                return self.client.chat.completions.create(**params)
            except Exception as exc:
                param = unsupported_optional_param(exc, params)
                if param is None:
                    raise
                params.pop(param)
                if param == "reasoning_effort":
                    self.reasoning_effort = None
