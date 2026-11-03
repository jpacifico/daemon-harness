import os
from .base import LLMProvider, unsupported_optional_param
from .telemetry import record_provider_usage
class LocalOpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        from openai import OpenAI

        resolved_base_url = base_url or os.getenv(
            "LOCAL_LLM_BASE_URL",
            "http://localhost:1234/v1",
        )
        resolved_key = api_key or os.getenv("LOCAL_LLM_API_KEY", "local-key")
        self.model = model or os.getenv("LOCAL_LLM_MODEL", "local-model-name")
        self.reasoning_effort = reasoning_effort
        self.client = OpenAI(base_url=resolved_base_url, api_key=resolved_key)
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        result = self._create(messages=messages, temperature=temperature)
        record_provider_usage(getattr(result, "usage", None))
        return result.choices[0].message.content.strip()

    def stream_complete(self, messages: list[dict], temperature: float = 0.4):
        stream = self._create(
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

    def _create(
        self,
        messages: list[dict],
        temperature: float,
        stream: bool = False,
    ):
        params: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        if stream:
            params["stream"] = True
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
