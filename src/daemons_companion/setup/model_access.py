"""Model access offered by setup, recommended choice first.

Only providers and models qualified for the complete daemon are offered
(docs/model_providers.md). Mistral and SpaceXSI were withdrawn on 2026-10-29; a
daemon created with either keeps working and keeps its provider when it changes
host.
"""
from __future__ import annotations

# The ChatGPT subscription with Astra is recommended on both hosts; OpenClaw
# 2026.10.7 reaches it through its own ChatGPT sign-in
# (docs/evaluations/2026-10-25_openclaw_2026_10_7.md).
_ACCESS = (
    ("codex_cli", "ChatGPT subscription (recommended) — sign in with your account"),
    ("openai_api", "OpenAI API key"),
    ("anthropic_api", "Anthropic API key"),
)


def options() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the provider kinds and their labels, in the order shown."""
    return tuple(kind for kind, _ in _ACCESS), tuple(label for _, label in _ACCESS)
