from abc import ABC, abstractmethod


OPTIONAL_COMPLETION_PARAMS = ("temperature", "reasoning_effort")


def unsupported_optional_param(
    exc: Exception,
    params: dict,
) -> str | None:
    """Name an explicitly rejected optional completion parameter."""

    text = str(exc).lower()
    rejection_markers = (
        "unsupported",
        "not supported",
        "unknown parameter",
        "unrecognized",
        "not permitted",
        "invalid parameter",
        "unexpected keyword",
    )
    if not any(marker in text for marker in rejection_markers):
        return None
    for param in OPTIONAL_COMPLETION_PARAMS:
        if param in params and param in text:
            return param
    return None


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise NotImplementedError

    def stream_complete(self, messages: list[dict], temperature: float = 0.4):
        yield self.complete(messages=messages, temperature=temperature)
