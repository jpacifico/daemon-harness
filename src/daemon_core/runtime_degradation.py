"""Deterministic contract for host replies produced without daemon memory."""

from __future__ import annotations


CANONICAL_PREPARE_FAILED = "canonical_prepare_failed"
DAEMON_BRIDGE_UNAVAILABLE = "daemon_bridge_unavailable"
DEGRADED_RESPONSE_NOTICE = (
    "⚠️ System notice · Personal memory was unavailable for this response."
)
DEGRADED_HOST_CONTRACT = """## Daemon fallback mode

The daemon's personal memory is temporarily unavailable for this turn.
Respond using the host's capabilities and the currently visible dialogue.
Assert no personal fact absent from this dialogue; if such a fact is necessary,
simply say you cannot verify it at the moment.
Keep the available native tools and use them normally."""


def add_degraded_response_notice(text: str) -> str:
    """Add the transport-owned notice once, without involving the model."""

    if not text or text.startswith(DEGRADED_RESPONSE_NOTICE):
        return text
    return f"{DEGRADED_RESPONSE_NOTICE}\n\n{text}"


def strip_degraded_response_notice(text: str) -> str:
    """Remove transport metadata before L0 and memory ingestion."""

    if not text or not text.startswith(DEGRADED_RESPONSE_NOTICE):
        return text
    return text[len(DEGRADED_RESPONSE_NOTICE):].lstrip("\r\n")


__all__ = [
    "CANONICAL_PREPARE_FAILED",
    "DAEMON_BRIDGE_UNAVAILABLE",
    "DEGRADED_HOST_CONTRACT",
    "DEGRADED_RESPONSE_NOTICE",
    "add_degraded_response_notice",
    "strip_degraded_response_notice",
]
