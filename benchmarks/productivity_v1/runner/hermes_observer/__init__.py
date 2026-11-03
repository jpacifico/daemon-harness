"""Content-safe Hermes telemetry for the public productivity benchmark."""

from __future__ import annotations

from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import time
from typing import Any


_WRITE_LOCK = threading.Lock()
_TURN_LOCK = threading.Lock()
_ACTIVE_TURNS: dict[str, dict[str, Any]] = {}
_BACKGROUND_CONDITION = threading.Condition()
_BACKGROUND_ACTIVE = ContextVar("productivity_benchmark_background", default=False)
_BACKGROUND_PENDING = 0
_BACKGROUND_SCHEDULED = 0
_BACKGROUND_COMPLETED = 0
_BACKGROUND_FAILED = 0


def _trace_path() -> Path | None:
    raw = os.getenv("PRODUCTIVITY_BENCH_TRACE_FILE", "").strip()
    return Path(raw).expanduser() if raw else None


def _write(event: str, **payload: Any) -> None:
    path = _trace_path()
    if path is None:
        return
    record = {
        "schema": "daemon.public-productivity.host-trace.v1",
        "event": event,
        "epoch": time.time(),
        "monotonic": time.perf_counter(),
        "payload": payload,
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _identity(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: kwargs.get(key)
        for key in (
            "session_id",
            "turn_id",
            "api_request_id",
            "api_call_count",
            "tool_call_id",
            "model",
            "provider",
        )
        if kwargs.get(key) is not None
    }


def _phase() -> str:
    return "native_background" if _BACKGROUND_ACTIVE.get() else "visible"


def _wait_for_native_background() -> bool:
    return os.getenv("PRODUCTIVITY_BENCH_WAIT_NATIVE_BACKGROUND", "").strip() == "1"


def _background_timeout_seconds() -> float:
    raw = os.getenv("PRODUCTIVITY_BENCH_NATIVE_BACKGROUND_TIMEOUT_SECONDS", "420")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 420.0


def _request_body(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    body = request.get("body")
    return body if isinstance(body, dict) else {}


def _system_text(body: dict[str, Any]) -> str:
    instructions = body.get("instructions")
    if isinstance(instructions, str):
        return instructions
    messages = body.get("messages") or body.get("input") or []
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
    return ""


def _tool_names(body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return sorted(set(names))


def _on_session_start(**kwargs: Any) -> None:
    _write("session_start", **_identity(kwargs), phase=_phase())


def _on_session_end(**kwargs: Any) -> None:
    _write("session_end", **_identity(kwargs), phase=_phase())
    if not _wait_for_native_background() or _phase() == "native_background":
        return
    started = time.perf_counter()
    deadline = started + _background_timeout_seconds()
    with _BACKGROUND_CONDITION:
        while _BACKGROUND_PENDING:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            _BACKGROUND_CONDITION.wait(timeout=remaining)
        snapshot = {
            "complete": _BACKGROUND_PENDING == 0,
            "pending": _BACKGROUND_PENDING,
            "scheduled": _BACKGROUND_SCHEDULED,
            "completed": _BACKGROUND_COMPLETED,
            "failed": _BACKGROUND_FAILED,
        }
    _write(
        "native_background_barrier",
        **_identity(kwargs),
        **snapshot,
        wait_ms=max(0, round((time.perf_counter() - started) * 1000)),
    )


def _on_pre_llm_call(**kwargs: Any) -> None:
    session_id = str(kwargs.get("session_id") or "")
    with _TURN_LOCK:
        _ACTIVE_TURNS[session_id] = {"first_text_seen": False}
    _write("turn_start", **_identity(kwargs), phase=_phase())


def _on_post_llm_call(**kwargs: Any) -> None:
    _write("turn_complete", **_identity(kwargs), phase=_phase())
    session_id = str(kwargs.get("session_id") or "")
    with _TURN_LOCK:
        _ACTIVE_TURNS.pop(session_id, None)


def _on_pre_api_request(**kwargs: Any) -> None:
    body = _request_body(kwargs.get("request"))
    system = _system_text(body)
    tools = _tool_names(body)
    _write(
        "api_request_start",
        **_identity(kwargs),
        phase=_phase(),
        approx_input_tokens=kwargs.get("approx_input_tokens"),
        request_char_count=kwargs.get("request_char_count"),
        system_chars=len(system),
        system_sha256=sha256(system.encode("utf-8")).hexdigest(),
        tool_names=tools,
        tool_catalog_sha256=sha256(
            json.dumps(tools, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


def _on_post_api_request(**kwargs: Any) -> None:
    usage = kwargs.get("usage")
    _write(
        "api_request_complete",
        **_identity(kwargs),
        phase=_phase(),
        api_duration=kwargs.get("api_duration"),
        finish_reason=kwargs.get("finish_reason"),
        usage=usage if isinstance(usage, dict) else None,
    )


def _on_api_request_error(**kwargs: Any) -> None:
    _write(
        "api_request_error",
        **_identity(kwargs),
        phase=_phase(),
        api_duration=kwargs.get("api_duration"),
        status_code=kwargs.get("status_code"),
        retry_count=kwargs.get("retry_count"),
        error_type=type(kwargs.get("error")).__name__,
    )


def _on_pre_tool_call(**kwargs: Any) -> None:
    _write(
        "tool_start",
        **_identity(kwargs),
        phase=_phase(),
        tool_name=kwargs.get("tool_name"),
    )


def _on_post_tool_call(**kwargs: Any) -> None:
    _write(
        "tool_complete",
        **_identity(kwargs),
        phase=_phase(),
        tool_name=kwargs.get("tool_name"),
        duration_ms=kwargs.get("duration_ms"),
        status=kwargs.get("status"),
        error_type=kwargs.get("error_type"),
    )


def _record_first_visible_text(agent: Any, text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        return
    session_id = str(getattr(agent, "session_id", "") or "")
    with _TURN_LOCK:
        state = _ACTIVE_TURNS.get(session_id)
        if state is None or state["first_text_seen"]:
            return
        state["first_text_seen"] = True
    _write("first_visible_text", session_id=session_id, phase=_phase())


def _install_background_probe() -> None:
    """Observe and join Hermes' native review without changing its cadence."""

    try:
        from agent import background_review
    except Exception:
        return
    if getattr(background_review, "_productivity_benchmark_probe", False):
        return
    original = background_review.spawn_background_review_thread

    def observed(*args: Any, **kwargs: Any):
        global _BACKGROUND_PENDING, _BACKGROUND_SCHEDULED
        target, prompt = original(*args, **kwargs)
        with _BACKGROUND_CONDITION:
            _BACKGROUND_PENDING += 1
            _BACKGROUND_SCHEDULED += 1
            scheduled_index = _BACKGROUND_SCHEDULED
        _write(
            "native_background_scheduled",
            phase="native_background",
            review_index=scheduled_index,
        )

        def observed_target() -> None:
            global _BACKGROUND_PENDING, _BACKGROUND_COMPLETED, _BACKGROUND_FAILED
            failed = False
            phase_token = _BACKGROUND_ACTIVE.set(True)
            _write(
                "native_background_start",
                phase="native_background",
                review_index=scheduled_index,
            )
            try:
                target()
            except BaseException:
                failed = True
                raise
            finally:
                with _BACKGROUND_CONDITION:
                    _BACKGROUND_PENDING -= 1
                    _BACKGROUND_COMPLETED += 1
                    if failed:
                        _BACKGROUND_FAILED += 1
                    _BACKGROUND_CONDITION.notify_all()
                _write(
                    "native_background_end",
                    phase="native_background",
                    review_index=scheduled_index,
                    failed=failed,
                )
                _BACKGROUND_ACTIVE.reset(phase_token)

        return observed_target, prompt

    background_review.spawn_background_review_thread = observed
    background_review._productivity_benchmark_probe = True
    _write("native_background_probe_installed")


def _install_stream_probe() -> None:
    try:
        from run_agent import AIAgent
    except Exception:
        return
    if getattr(AIAgent, "_productivity_benchmark_stream_probe", False):
        return
    original = AIAgent._fire_stream_delta

    def observed(self: Any, text: str) -> None:
        existing = getattr(self, "stream_delta_callback", None)

        def callback(visible: str) -> None:
            _record_first_visible_text(self, visible)
            if existing is not None:
                existing(visible)

        self.stream_delta_callback = callback
        try:
            original(self, text)
        finally:
            self.stream_delta_callback = existing

    AIAgent._fire_stream_delta = observed
    AIAgent._productivity_benchmark_stream_probe = True


def register(ctx: Any) -> None:
    _install_stream_probe()
    _install_background_probe()
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
