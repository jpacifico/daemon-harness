"""Lossless message-role adaptation at a provider transport boundary."""
from urllib.parse import urlsplit


def is_mistral_endpoint(base_url: str) -> bool:
    """Match the actual endpoint, never a model name or an inherited account."""
    try:
        endpoint = urlsplit(base_url)
        return endpoint.scheme == "https" and endpoint.hostname == "api.mistral.ai"
    except ValueError:
        return False


def mistral_messages(messages: list[dict]) -> list[dict]:
    """Mistral has no developer role; retain its exact instruction content.

    Keep position and every other field intact, including tool call IDs and
    typed content. Canonical messages remain unmodified for other transports.
    """
    return [
        {**message, "role": "system"}
        if message.get("role") == "developer" else message
        for message in messages
    ]
