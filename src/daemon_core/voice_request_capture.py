"""One-shot private capture of the effective sovereign provider request.

The capture is deliberately opt-in and content-bearing.  It is intended for
local prompt audits, never for routine telemetry.  A configured target must
live below ``exports/private`` and is created with exclusive, private
permissions so a later turn cannot overwrite the captured request.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from daemon_core.platform_support import create_private_file


VOICE_CAPTURE_FILE_ENV = "DAEMON_CORE_VOICE_CAPTURE_FILE"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_CAPTURE_ROOT = Path("exports/private")
_TRANSPORT_ONLY_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "extra_headers",
        "headers",
        "http_client",
        "timeout",
    }
)
_METADATA_KEYS = (
    "trace_id",
    "turn_id",
    "api_request_id",
    "api_call_count",
    "platform",
    "provider",
    "model",
    "api_mode",
    "voice_path",
    "prompt_variant",
    "situation_source",
    "memory_source",
)


@dataclass
class VoiceRequestCapture:
    """A claimed capture file waiting for the provider outcome."""

    path: Path
    document: dict[str, Any]

    def complete(self, response: Any, *, duration_s: float) -> None:
        usage = _member(response, "usage")
        self.document["status"] = "completed"
        self.document["completed_at"] = _utc_now()
        self.document["outcome"] = {
            "duration_s": round(max(0.0, float(duration_s)), 4),
            "response": {
                "id": _plain_scalar(_member(response, "id")),
                "model": _plain_scalar(_member(response, "model")),
                "status": _plain_scalar(_member(response, "status")),
            },
            "provider_usage": _jsonable(usage) if usage is not None else None,
        }
        _atomic_private_json_write(self.path, self.document)

    def fail(self, error: BaseException, *, duration_s: float) -> None:
        self.document["status"] = "failed"
        self.document["completed_at"] = _utc_now()
        self.document["outcome"] = {
            "duration_s": round(max(0.0, float(duration_s)), 4),
            # Provider errors may echo request fragments or credentials.  The
            # class is sufficient for correlating the failed observation.
            "error_type": error.__class__.__name__,
            "provider_usage": None,
        }
        _atomic_private_json_write(self.path, self.document)


def begin_voice_request_capture(
    request: dict[str, Any],
    *,
    metadata: dict[str, Any],
) -> VoiceRequestCapture | None:
    """Claim and persist the next canonical first-call request, if enabled.

    Existing files are never overwritten.  Calls after the first provider
    iteration are excluded: a separate explicit capture can later target a
    complete tool loop without making the safe default content-hungry.
    """

    configured = os.getenv(VOICE_CAPTURE_FILE_ENV, "").strip()
    if not configured:
        return None
    try:
        api_call_count = int(metadata.get("api_call_count") or 0)
    except (TypeError, ValueError):
        return None
    if api_call_count != 1:
        return None

    path = _private_capture_path(configured)
    body, excluded_keys = _provider_body(request)
    json_body = _jsonable(body)
    metadata_snapshot = {
        key: _plain_scalar(metadata.get(key))
        for key in _METADATA_KEYS
        if metadata.get(key) is not None
    }
    document = {
        "schema": "daemon.private-provider-request.v1",
        "status": "pending",
        "captured_at": _utc_now(),
        "metadata": metadata_snapshot,
        "request": {
            "body": json_body,
            "excluded_transport_keys": excluded_keys,
        },
        "measurements": _measure_request(json_body),
        "outcome": None,
    }
    if not _exclusive_private_json_create(path, document):
        return None
    return VoiceRequestCapture(path=path, document=document)


def _private_capture_path(configured: str) -> Path:
    private_root = (_REPOSITORY_ROOT / _PRIVATE_CAPTURE_ROOT).resolve()
    raw_path = Path(configured).expanduser()
    target = raw_path if raw_path.is_absolute() else _REPOSITORY_ROOT / raw_path
    target = target.resolve(strict=False)
    if target == private_root or private_root not in target.parents:
        raise ValueError("voice capture target must live below exports/private")
    if target.suffix.lower() != ".json":
        raise ValueError("voice capture target must use a .json suffix")
    return target


def _provider_body(request: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    excluded = sorted(key for key in request if key in _TRANSPORT_ONLY_KEYS)
    body = {
        key: value
        for key, value in request.items()
        if key not in _TRANSPORT_ONLY_KEYS
    }
    return body, excluded


def _measure_request(body: dict[str, Any]) -> dict[str, Any]:
    serialized = _canonical_json(body)
    tools = body.get("tools") or []
    serialized_tools = _canonical_json(tools)
    dialogue = body.get("messages")
    if not isinstance(dialogue, list):
        dialogue = body.get("input")
    if not isinstance(dialogue, list):
        dialogue = []
    instructions = body.get("instructions")
    if not isinstance(instructions, str):
        instructions = ""
    return {
        "request_body_json_chars": len(serialized),
        "request_body_json_bytes": len(serialized.encode("utf-8")),
        "request_body_sha256": hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        "instructions_chars": len(instructions),
        "dialogue_item_count": len(dialogue),
        "message_text_chars": _text_chars(dialogue),
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "tool_schema_json_chars": len(serialized_tools),
        "tool_schema_json_bytes": len(serialized_tools.encode("utf-8")),
        "tool_names": _tool_names(tools),
    }


def _tool_names(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(
            _text_chars(item)
            for key, item in value.items()
            if key in {"content", "text", "output"}
        )
    if isinstance(value, (list, tuple)):
        return sum(_text_chars(item) for item in value)
    return 0


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(key): _jsonable(item)
            for key, item in attributes.items()
            if not str(key).startswith("_")
        }
    return str(value)


def _member(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _plain_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _render_document(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _exclusive_private_json_create(
    path: Path,
    document: dict[str, Any],
) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _render_document(document)
    try:
        fd = create_private_file(path)
    except FileExistsError:
        return False
    try:
        _write_and_close(fd, content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return True


def _atomic_private_json_write(path: Path, document: dict[str, Any]) -> None:
    if path.is_symlink():
        raise OSError("refusing to replace a symlinked voice capture")
    content = _render_document(document)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}")
    fd = create_private_file(temporary_path)
    try:
        _write_and_close(fd, content)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_and_close(fd: int, content: str) -> None:
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "VOICE_CAPTURE_FILE_ENV",
    "VoiceRequestCapture",
    "begin_voice_request_capture",
]
