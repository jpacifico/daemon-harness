"""Deterministic, opt-in telemetry for internal LLM provider calls."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .base import LLMProvider


TELEMETRY_FILE_ENV = "DAEMON_CORE_LLM_TELEMETRY_FILE"
_PHASE: ContextVar[str] = ContextVar(
    "daemon_core_llm_phase",
    default="unclassified",
)
_USAGE_COLLECTOR: ContextVar[dict[str, Any] | None] = ContextVar(
    "daemon_core_llm_usage",
    default=None,
)
_WRITE_LOCK = threading.Lock()
_ACTIVITY = threading.Condition()
_ACTIVE_CALLS = 0
_STARTED_CALLS = 0
_LAST_ACTIVITY = 0.0


@contextmanager
def llm_telemetry_phase(phase: str) -> Iterator[None]:
    """Attach a technical lifecycle phase to nested provider calls."""

    token = _PHASE.set(str(phase).strip() or "unclassified")
    try:
        yield
    finally:
        _PHASE.reset(token)


def record_provider_usage(usage: Any) -> None:
    """Attach provider-native token usage to the active telemetry call."""

    collector = _USAGE_COLLECTOR.get()
    if collector is None or usage is None:
        return
    normalized = _numeric_usage(usage)
    if normalized:
        collector["provider_usage"] = normalized


def instrument_provider(
    provider: LLMProvider,
    *,
    faculty: str,
) -> LLMProvider:
    """Wrap a provider only when the opt-in telemetry path is configured."""

    if not os.getenv(TELEMETRY_FILE_ENV, "").strip():
        return provider
    if isinstance(provider, TelemetryProvider):
        return provider
    return TelemetryProvider(provider, faculty=faculty)


class TelemetryProvider(LLMProvider):
    """Transparent provider wrapper that records counts, never content."""

    def __init__(self, provider: LLMProvider, *, faculty: str) -> None:
        self._provider = provider
        self.faculty = str(faculty).strip() or "generic"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def complete(
        self,
        messages: list[dict],
        temperature: float = 0.4,
    ) -> str:
        return self._recorded_complete(
            messages,
            temperature=temperature,
            stream=False,
        )

    def stream_complete(
        self,
        messages: list[dict],
        temperature: float = 0.4,
    ):
        started = _begin_call()
        collector: dict[str, Any] = {}
        usage_token = _USAGE_COLLECTOR.set(collector)
        output_chars = 0
        status = "ok"
        error_type = ""
        try:
            for chunk in self._provider.stream_complete(
                messages=messages,
                temperature=temperature,
            ):
                output_chars += len(str(chunk))
                yield chunk
        except Exception as exc:
            status = "error"
            error_type = exc.__class__.__name__
            raise
        finally:
            _USAGE_COLLECTOR.reset(usage_token)
            self._finish_call(
                started,
                messages=messages,
                output_chars=output_chars,
                status=status,
                error_type=error_type,
                stream=True,
                provider_usage=collector.get("provider_usage"),
            )

    def _recorded_complete(
        self,
        messages: list[dict],
        *,
        temperature: float,
        stream: bool,
    ) -> str:
        started = _begin_call()
        collector: dict[str, Any] = {}
        usage_token = _USAGE_COLLECTOR.set(collector)
        output = ""
        status = "ok"
        error_type = ""
        try:
            output = self._provider.complete(
                messages=messages,
                temperature=temperature,
            )
            return output
        except Exception as exc:
            status = "error"
            error_type = exc.__class__.__name__
            raise
        finally:
            _USAGE_COLLECTOR.reset(usage_token)
            self._finish_call(
                started,
                messages=messages,
                output_chars=len(output),
                status=status,
                error_type=error_type,
                stream=stream,
                provider_usage=collector.get("provider_usage"),
            )

    def _finish_call(
        self,
        started: tuple[str, float, float],
        *,
        messages: list[dict],
        output_chars: int,
        status: str,
        error_type: str,
        stream: bool,
        provider_usage: dict[str, Any] | None,
    ) -> None:
        call_id, started_epoch, started_monotonic = started
        duration = max(0.0, time.perf_counter() - started_monotonic)
        input_chars = _message_chars(messages)
        phase = _PHASE.get()
        event = {
            "schema": "daemon.internal-llm-call.v1",
            "event": "llm_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "call_id": call_id,
            "started_epoch": round(started_epoch, 6),
            "duration_s": round(duration, 4),
            "campaign_id": os.getenv("HOST_CAPABILITY_CAMPAIGN_ID", ""),
            "run_id": os.getenv("HOST_CAPABILITY_RUN_ID", ""),
            "arm": os.getenv("HOST_CAPABILITY_ARM", ""),
            "case_id": os.getenv("HOST_CAPABILITY_CASE_ID", ""),
            "faculty": self.faculty,
            "phase": phase,
            "critical_path": phase.startswith("critical_path"),
            "provider": self._provider.__class__.__name__,
            "model": str(getattr(self._provider, "model", "") or ""),
            "reasoning_effort": str(
                getattr(self._provider, "reasoning_effort", "") or ""
            ),
            "stream": stream,
            "message_count": len(messages),
            "input_chars": input_chars,
            "output_chars": output_chars,
            "approx_input_tokens": _approx_tokens(input_chars),
            "approx_output_tokens": _approx_tokens(output_chars),
            "provider_usage": provider_usage,
            "status": status,
            "error_type": error_type,
            "thread_name": threading.current_thread().name,
        }
        try:
            _write_event(event)
        except (OSError, TypeError, ValueError):
            pass
        finally:
            _end_call()


def wait_for_llm_quiescence(
    *,
    timeout_seconds: float,
    idle_seconds: float = 1.0,
) -> bool:
    """Wait until no internal call has run for a short, bounded interval."""

    timeout = max(0.0, float(timeout_seconds))
    idle = max(0.0, float(idle_seconds))
    deadline = time.monotonic() + timeout
    with _ACTIVITY:
        while True:
            now = time.monotonic()
            idle_for = now - _LAST_ACTIVITY if _LAST_ACTIVITY else idle
            if _ACTIVE_CALLS == 0 and idle_for >= idle:
                return True
            remaining = deadline - now
            if remaining <= 0:
                return False
            wait_for = min(remaining, max(0.01, idle - idle_for))
            _ACTIVITY.wait(timeout=wait_for)


def record_lifecycle_marker(
    *,
    quiescent: bool,
    timeout_seconds: float,
) -> None:
    with _ACTIVITY:
        active_calls = _ACTIVE_CALLS
        started_calls = _STARTED_CALLS
    try:
        _write_event(
            {
                "schema": "daemon.internal-llm-lifecycle.v1",
                "event": "lifecycle_complete",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "campaign_id": os.getenv("HOST_CAPABILITY_CAMPAIGN_ID", ""),
                "run_id": os.getenv("HOST_CAPABILITY_RUN_ID", ""),
                "arm": os.getenv("HOST_CAPABILITY_ARM", ""),
                "case_id": os.getenv("HOST_CAPABILITY_CASE_ID", ""),
                "quiescent": bool(quiescent),
                "timeout_seconds": float(timeout_seconds),
                "active_calls": active_calls,
                "started_calls": started_calls,
            }
        )
    except (OSError, TypeError, ValueError):
        pass


def _begin_call() -> tuple[str, float, float]:
    global _ACTIVE_CALLS, _STARTED_CALLS, _LAST_ACTIVITY
    with _ACTIVITY:
        _ACTIVE_CALLS += 1
        _STARTED_CALLS += 1
        _LAST_ACTIVITY = time.monotonic()
        _ACTIVITY.notify_all()
    return str(uuid.uuid4()), time.time(), time.perf_counter()


def _end_call() -> None:
    global _ACTIVE_CALLS, _LAST_ACTIVITY
    with _ACTIVITY:
        _ACTIVE_CALLS = max(0, _ACTIVE_CALLS - 1)
        _LAST_ACTIVITY = time.monotonic()
        _ACTIVITY.notify_all()


def _write_event(event: dict[str, Any]) -> None:
    raw_path = os.getenv(TELEMETRY_FILE_ENV, "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _message_chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("role", "")))
        total += len(str(message.get("content", "")))
    return total


def _approx_tokens(chars: int) -> int:
    return math.ceil(max(0, int(chars)) / 4)


def _numeric_usage(value: Any) -> dict[str, Any] | None:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif not isinstance(value, dict) and hasattr(value, "__dict__"):
        value = vars(value)
    if not isinstance(value, dict):
        return None

    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            result[str(key)] = item
            continue
        nested = _numeric_usage(item)
        if nested:
            result[str(key)] = nested
    return result or None


__all__ = [
    "TELEMETRY_FILE_ENV",
    "TelemetryProvider",
    "instrument_provider",
    "llm_telemetry_phase",
    "record_lifecycle_marker",
    "record_provider_usage",
    "wait_for_llm_quiescence",
]
