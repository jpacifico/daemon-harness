"""Keep failed durable memory work retryable without changing the voice path."""

from contextlib import contextmanager
from contextvars import ContextVar


_required = ContextVar("memory_update_success_required", default=False)


@contextmanager
def require_memory_update_success():
    """A durable executor may acknowledge work only after real completion."""
    token = _required.set(True)
    try:
        yield
    finally:
        _required.reset(token)


def reraise_memory_update_failure(error: Exception) -> None:
    """Retain graceful legacy callers, but never swallow a durable failure."""
    if _required.get():
        raise error
