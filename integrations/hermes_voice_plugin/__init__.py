"""Daemon Voice: the Hermes middleware for the canonical daemon voice.

This standalone plugin is separate from the memory provider because Hermes'
general plugin loader excludes exclusive memory plugins. It registers as
llm_execution middleware and delegates the rewritten request through next_call.
Hermes retains streaming, tools, native session history and channel delivery.

The sovereign system context is restored on each tool-loop iteration.
If composition fails, Hermes keeps its native system and tools, receives the
static evidence contract, and exposes fallback status to the transport.

The memory prefetch verdict is reused through a file: one scene, one judgment.
Enable with DAEMON_CORE_SOVEREIGN_VOICE=on in the gateway environment."""

import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import uuid
import weakref

logger = logging.getLogger("daemon_voice")
_DEGRADED_NOTICE_FALLBACK = (
    "⚠️ System notice · Personal memory was unavailable for this response."
)
_DEGRADED_CONTRACT_FALLBACK = (
    "The daemon's personal memory is temporarily unavailable for this turn. "
    "Respond with the visible dialogue and native tools. Assert no personal "
    "fact absent from this dialogue."
)
_HERMES_PROGRESS_PREAMBLE_CONTRACT = (
    "## Hermes progress preamble\n\n"
    "In the same generation as the first tool, first write as visible commentary "
    "one very brief sentence in the user's language: say only what you are "
    "checking, without an unverified fact or tool name, then call it immediately. "
    "Without a tool, respond directly. Do not repeat this sentence in the final response."
)
_latency_local = threading.local()
_turn_latency: dict[str, dict] = {}
_active_latency_lock = threading.Lock()
_active_latency: dict[str, dict] = {}


def _new_trace_id() -> str:
    try:
        from daemon_core.latency_probe import new_trace_id

        return new_trace_id()
    except Exception:
        return uuid.uuid4().hex[:16]


def _observe_continuity(
    trace_id: str,
    stage: str,
    status: str,
    *,
    reason_code: str,
    **details,
) -> None:
    try:
        from daemon_core.continuity import record_continuity_stage

        record_continuity_stage(
            trace_id,
            stage,
            status,
            reason_code=reason_code,
            **details,
        )
    except Exception:
        pass


def _latency_fields(record: dict) -> dict:
    now = time.time()
    prefetch_started_at = record.get("prefetch_started_at")
    voice_started_at = record.get("voice_started_at")
    return {
        "turn_id": record.get("turn_id") or None,
        "api_request_id": record.get("api_request_id") or None,
        "api_call_count": record.get("api_call_count"),
        "platform": record.get("platform") or None,
        "voice_path": record.get("voice_path") or None,
        "prompt_variant": record.get("prompt_variant") or None,
        "since_prefetch": (
            round(max(0.0, now - prefetch_started_at), 4)
            if prefetch_started_at
            else None
        ),
        "since_voice": (
            round(max(0.0, now - voice_started_at), 4)
            if voice_started_at
            else None
        ),
    }


def _append_voice_event(event: str, record: dict, **fields) -> None:
    try:
        from daemon_core.latency_probe import append_latency_event

        append_latency_event(
            event,
            record["trace_id"],
            **_latency_fields(record),
            **fields,
        )
    except Exception:
        pass


def _latency_record_key(record: dict) -> str:
    return str(record.get("api_request_id") or record.get("trace_id") or id(record))


def _register_active_latency(record: dict) -> None:
    with _active_latency_lock:
        _active_latency[_latency_record_key(record)] = record


def _unregister_active_latency(record: dict) -> None:
    key = _latency_record_key(record)
    with _active_latency_lock:
        if _active_latency.get(key) is record:
            _active_latency.pop(key, None)


def _stream_latency_record(consumer) -> dict | None:
    attached = getattr(consumer, "_daemon_latency_record", None)
    if attached is not None:
        return attached
    local = getattr(_latency_local, "current", None)
    if local is not None:
        return local
    with _active_latency_lock:
        records = list(_active_latency.values())
    # Without a request identifier on Hermes' stream callback, concurrent
    # generations are deliberately left uncorrelated instead of guessed.
    return records[0] if len(records) == 1 else None


# Preambles still awaiting cleanup, by (platform, chat). A final answer that
# Hermes delivers outside the stream consumer (no Hermes tool ran, e.g. after
# model-native search) must still remove them.
_PENDING_PREAMBLES: dict = {}


def _preamble_key(adapter, chat_id) -> tuple[str, str]:
    platform = getattr(adapter, "platform", "")
    platform = getattr(platform, "value", platform)
    return str(platform or "").strip().lower(), str(chat_id or "").strip()


def _managed_provider_kind() -> str:
    try:
        path = os.environ.get("DAEMON_CORE_PROVIDER_CONFIG", "").strip()
        return str(json.loads(Path(path).read_text(encoding="utf-8")).get("kind") or "") if path else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _install_managed_anthropic_key(credentials=None) -> bool:
    """Keep the Anthropic key entered in Companion on API-key authentication.

    Hermes treats every ``sk-ant-*`` secret that is not ``sk-ant-api*`` as a
    Claude subscription token and sends it with Claude Code identity. Anthropic
    refused that for an owner's ``sk-ant-usr*`` Console key ("credit balance is
    too low") while accepting the same key as ``x-api-key`` (2026-10-29). Only the
    exact key of a daemon whose managed provider is ``anthropic_api`` changes;
    every other token keeps Hermes' own classification.
    """
    try:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key or _managed_provider_kind() != "anthropic_api":
            return False
        if credentials is None:
            import agent.anthropic_credentials as credentials
        original = credentials._is_oauth_token
        if getattr(original, "_daemon_managed_key", False):
            return True

        def _is_oauth_token(value) -> bool:
            if isinstance(value, str) and value.strip() == key:
                return False
            return original(value)

        _is_oauth_token._daemon_managed_key = True
        # Modules that imported the function by name keep their own reference.
        for module in list(sys.modules.values()):
            if getattr(module, "_is_oauth_token", None) is original:
                module._is_oauth_token = _is_oauth_token
        credentials._is_oauth_token = _is_oauth_token
        return True
    except Exception as exc:
        logger.warning(
            "Daemon Voice : authentification Anthropic par clé indisponible | error=%s",
            exc.__class__.__name__,
        )
        return False


def _install_final_delivery_cleanup(adapter_cls=None) -> bool:
    """Clean tracked preambles after a ledgered final send, without changing it."""
    try:
        if adapter_cls is None:
            from gateway.platforms.base import BasePlatformAdapter

            adapter_cls = BasePlatformAdapter
        if getattr(adapter_cls, "_daemon_final_cleanup_installed", False):
            return True
        original = getattr(adapter_cls, "send_final_ledgered", None)
        if original is None:
            return False

        async def send_final_ledgered(self, event, *args, **kwargs):
            outcome = await original(self, event, *args, **kwargs)
            try:
                result = outcome[0] if isinstance(outcome, tuple) else outcome
                if getattr(result, "success", False):
                    source = getattr(event, "source", None)
                    key = _preamble_key(self, getattr(source, "chat_id", ""))
                    pending = _PENDING_PREAMBLES.pop(key, None)
                    consumer = pending[0]() if pending else None
                    if consumer is not None:
                        await pending[1](
                            consumer,
                            final_message_id=str(getattr(result, "message_id", "") or ""),
                        )
            except Exception as exc:
                logger.warning(
                    "Daemon Voice : nettoyage du préambule après envoi final indisponible | error=%s",
                    exc.__class__.__name__,
                )
            return outcome

        adapter_cls.send_final_ledgered = send_final_ledgered
        adapter_cls._daemon_final_cleanup_installed = True
        return True
    except Exception as exc:
        logger.warning(
            "Daemon Voice : nettoyage après envoi final indisponible | error=%s",
            exc.__class__.__name__,
        )
        return False


def _install_stream_probe(consumer_cls=None) -> bool:
    """Observe Hermes streaming without changing its delivery behavior.

    The execution middleware only regains control after Hermes consumed the
    complete stream. The consumer is therefore the narrowest host boundary
    where first model content and first successful platform delivery are both
    observable. The patch is idempotent and fail-open.
    """
    try:
        if consumer_cls is None:
            from gateway.stream_consumer import GatewayStreamConsumer

            consumer_cls = GatewayStreamConsumer
        if getattr(consumer_cls, "_daemon_latency_probe_installed", False):
            return True

        original_on_delta = consumer_cls.on_delta
        original_on_segment_break = getattr(
            consumer_cls,
            "on_segment_break",
            None,
        )
        original_finish = getattr(consumer_cls, "finish", None)
        original_send_commentary = getattr(
            consumer_cls,
            "_send_commentary",
            None,
        )
        original_send_or_edit = consumer_cls._send_or_edit

        def progress_preamble_cleanup_enabled(consumer) -> bool:
            adapter = getattr(consumer, "adapter", None)
            platform = getattr(adapter, "platform", "")
            platform = getattr(platform, "value", platform)
            return _hermes_progress_preamble_canary_enabled(
                str(platform or "").strip().lower()
            )

        def remember_progress_preamble_ids(consumer, candidates) -> None:
            ids = list(
                getattr(consumer, "_daemon_progress_preamble_message_ids", [])
            )
            added = 0
            for message_id in candidates:
                value = str(message_id or "").strip()
                if value and value != "__no_edit__" and value not in ids:
                    ids.append(value)
                    added += 1
            consumer._daemon_progress_preamble_message_ids = ids
            if ids:
                _PENDING_PREAMBLES[_preamble_key(consumer.adapter, consumer.chat_id)] = (
                    weakref.ref(consumer), cleanup_progress_preamble)
            if added:
                logger.info(
                    "Daemon Voice : préambule Telegram suivi | added=%s total=%s",
                    added,
                    len(ids),
                )

        async def send_commentary(self, text):
            if (
                original_send_commentary is None
                or not progress_preamble_cleanup_enabled(self)
            ):
                if original_send_commentary is None:
                    return False
                return await original_send_commentary(self, text)

            cleaned = self._clean_for_display(text)
            if not cleaned.strip():
                return False
            try:
                result = await self.adapter.send(
                    chat_id=self.chat_id,
                    content=cleaned,
                    metadata=self.metadata,
                )
            except Exception as exc:
                logger.error("Commentary send error: %s", exc)
                return False
            if not getattr(result, "success", False):
                return False

            remember_progress_preamble_ids(
                self,
                [
                    getattr(result, "message_id", None),
                    *list(
                        getattr(result, "continuation_message_ids", ()) or ()
                    ),
                ],
            )
            self._notify_new_message()
            return True

        async def cleanup_progress_preamble(self, final_message_id=None):
            ids = list(
                getattr(self, "_daemon_progress_preamble_message_ids", [])
            )
            if not ids or not progress_preamble_cleanup_enabled(self):
                return
            delete_message = getattr(self.adapter, "delete_message", None)
            if delete_message is None:
                return
            key = _preamble_key(self.adapter, self.chat_id)
            pending = _PENDING_PREAMBLES.get(key)
            if pending and pending[0]() is self:
                _PENDING_PREAMBLES.pop(key, None)
            if final_message_id is None:
                final_message_id = str(getattr(self, "_message_id", "") or "")
            retained = []
            deleted = 0
            for message_id in ids:
                if message_id == final_message_id:
                    retained.append(message_id)
                    continue
                try:
                    success = await delete_message(self.chat_id, message_id)
                except Exception:
                    success = False
                if success:
                    deleted += 1
                else:
                    retained.append(message_id)
            self._daemon_progress_preamble_message_ids = retained
            logger.info(
                "Daemon Voice : préambule Telegram nettoyé | deleted=%s retained=%s",
                deleted,
                len(retained),
            )

        def on_delta(self, text):
            if text:
                try:
                    from daemon_core.voice_protocol import TurnMetadataStreamFilter

                    protocol_filter = getattr(
                        self,
                        "_daemon_turn_metadata_filter",
                        None,
                    )
                    if protocol_filter is None:
                        protocol_filter = TurnMetadataStreamFilter()
                        self._daemon_turn_metadata_filter = protocol_filter
                    text = protocol_filter.feed(text)
                except Exception:
                    pass
                if not text:
                    return None
            record = _stream_latency_record(self)
            if text and record:
                if getattr(self, "_daemon_latency_record", None) is None:
                    self._daemon_latency_record = record
                if not record.get("first_delta_logged"):
                    record["first_delta_logged"] = True
                    _append_voice_event("voice_first_delta", record)
            return original_on_delta(self, text)

        def on_segment_break(self):
            # A tool call ends one model generation. The next generation
            # starts its own private turn metadata and must be filtered anew.
            self._daemon_turn_metadata_filter = None
            if original_on_segment_break is not None:
                return original_on_segment_break(self)
            return None

        def finish(self, final_text=None):
            protocol_filter = getattr(
                self,
                "_daemon_turn_metadata_filter",
                None,
            )
            if protocol_filter is not None:
                tail = protocol_filter.finish()
                if tail:
                    on_delta(self, tail)
            if original_finish is None:
                return None
            if final_text is None:
                return original_finish(self)
            return original_finish(
                self,
                _strip_internal_turn_metadata(final_text),
            )

        async def send_or_edit(self, text, *, finalize=False, is_turn_final=True):
            # Defense in depth at the last host boundary. The delta filter
            # normally removes private first lines before accumulation, but a
            # provider may emit transport whitespace in a separate fragment.
            # Sanitize the complete frame again before Telegram can display it.
            text = _strip_internal_turn_metadata(text)
            record = (
                getattr(self, "_daemon_latency_record", None)
                or _stream_latency_record(self)
            )
            if record and record.get("degraded"):
                text = _add_degraded_response_notice(text)
            before_sent = bool(getattr(self, "_already_sent", False))
            before_text = getattr(self, "_last_sent_text", "")
            result = await original_send_or_edit(
                self,
                text,
                finalize=finalize,
                is_turn_final=is_turn_final,
            )
            delivered = bool(result) and (
                bool(getattr(self, "_already_sent", False)) != before_sent
                or getattr(self, "_last_sent_text", "") != before_text
            )
            if record and delivered and not record.get("first_visible_logged"):
                record["first_visible_logged"] = True
                _append_voice_event(
                    "voice_first_visible",
                    record,
                    transport=self.__class__.__name__,
                )
            # With Telegram streaming enabled, Codex commentary is delivered
            # as the first normal stream segment. Hermes then finalizes that
            # segment at the tool boundary instead of calling
            # ``_send_commentary``. Capture the exact ids here, after the host
            # send succeeds and before its run loop resets segment state.
            if (
                delivered
                and finalize
                and not is_turn_final
                and progress_preamble_cleanup_enabled(self)
            ):
                remember_progress_preamble_ids(
                    self,
                    [
                        getattr(self, "_message_id", None),
                        *list(getattr(self, "_preview_message_ids", set()) or ()),
                    ],
                )
            if delivered and is_turn_final:
                await cleanup_progress_preamble(self)
            return result

        consumer_cls.on_delta = on_delta
        if original_on_segment_break is not None:
            consumer_cls.on_segment_break = on_segment_break
        if original_finish is not None:
            consumer_cls.finish = finish
        if original_send_commentary is not None:
            consumer_cls._send_commentary = send_commentary
        consumer_cls._send_or_edit = send_or_edit
        consumer_cls._daemon_latency_probe_installed = True
        return True
    except Exception as exc:
        logger.warning("Daemon Voice : sonde streaming indisponible | error=%s", exc.__class__.__name__)
        return False


def _install_visible_stream_tracking(agent_cls=None) -> bool:
    """Make Hermes track and replay only user-visible stream text.

    Hermes records a delta as delivered before the gateway consumer can hide
    the daemon's private turn commitment.  A response containing only that
    commitment is consequently mistaken for a partially delivered answer and
    bypasses Hermes' native empty-response retry.  Completed Codex commentary
    also follows a dedicated live callback before the sanitized response is
    replayed by the loop; filtering only the latter leaks the marker once and
    makes Hermes treat the cleaned replay as a second message.  Apply the same
    protocol before both pieces of host bookkeeping while leaving the provider
    response, prompt and successful visible stream unchanged.
    """

    try:
        if agent_cls is None:
            # Hermes can register plugins while ``run_agent`` is still being
            # imported.  Do not force a circular import: the middleware calls
            # this installer again immediately before the first provider call.
            run_agent_module = sys.modules.get("run_agent")
            agent_cls = getattr(run_agent_module, "AIAgent", None)
            if agent_cls is None:
                return False
        if getattr(agent_cls, "_daemon_visible_stream_tracking_installed", False):
            return True

        original_reset = agent_cls._reset_stream_delivery_tracking
        original_record = agent_cls._record_streamed_assistant_text
        original_fire_commentary = getattr(
            agent_cls,
            "_fire_streamed_codex_commentary",
            None,
        )

        def reset_stream_delivery_tracking(self):
            result = original_reset(self)
            try:
                from daemon_core.voice_protocol import TurnMetadataStreamFilter

                self._daemon_visible_stream_tracking_filter = (
                    TurnMetadataStreamFilter()
                )
            except Exception:
                self._daemon_visible_stream_tracking_filter = None
            return result

        def record_streamed_assistant_text(self, text):
            if not isinstance(text, str) or not text:
                return None
            protocol_filter = getattr(
                self,
                "_daemon_visible_stream_tracking_filter",
                None,
            )
            if protocol_filter is None:
                try:
                    from daemon_core.voice_protocol import (
                        TurnMetadataStreamFilter,
                    )

                    protocol_filter = TurnMetadataStreamFilter()
                    self._daemon_visible_stream_tracking_filter = protocol_filter
                except Exception:
                    return original_record(self, text)
            visible = protocol_filter.feed(text)
            if visible:
                return original_record(self, visible)
            return None

        def fire_streamed_codex_commentary(self, text):
            if original_fire_commentary is None:
                return None
            if not isinstance(text, str):
                return original_fire_commentary(self, text)
            visible = _strip_internal_turn_metadata(text)
            if not visible or not visible.strip():
                return None
            return original_fire_commentary(self, visible)

        agent_cls._reset_stream_delivery_tracking = reset_stream_delivery_tracking
        agent_cls._record_streamed_assistant_text = record_streamed_assistant_text
        if original_fire_commentary is not None:
            agent_cls._fire_streamed_codex_commentary = (
                fire_streamed_codex_commentary
            )
        agent_cls._daemon_visible_stream_tracking_installed = True
        return True
    except Exception as exc:
        logger.warning(
            "Daemon Voice : suivi du flux visible indisponible | error=%s",
            exc.__class__.__name__,
        )
        return False


def _platforms() -> set:
    raw = os.getenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _flag_on() -> bool:
    return os.getenv("DAEMON_CORE_SOVEREIGN_VOICE", "").strip().lower() in ("1", "on", "true")


def _hermes_progress_preamble_canary_enabled(platform: str) -> bool:
    if (
        os.getenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "")
        .strip()
        .lower()
        != "canary"
    ):
        return False
    platforms = {
        value.strip()
        for value in os.getenv(
            "DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS",
            "telegram",
        ).split(",")
        if value.strip()
    }
    return bool(platform and platform in platforms)


def _with_hermes_progress_preamble(system: str, *, enabled: bool) -> str:
    prompt = str(system or "").strip()
    if not enabled or not prompt:
        return prompt
    if _HERMES_PROGRESS_PREAMBLE_CONTRACT in prompt:
        return prompt
    return f"{prompt}\n\n{_HERMES_PROGRESS_PREAMBLE_CONTRACT}"


def _ensure_src_on_path() -> bool:
    src = os.getenv("DAEMON_CORE_SRC", "").strip()
    if not src:
        return False
    if src not in sys.path:
        sys.path.insert(0, src)
    return True


def _last_user_text(request) -> str:
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    parts.append(str(p.get("text", "")))
                else:
                    parts.append(str(getattr(p, "text", "")))
            return " ".join(parts)
        return str(content or "")

    items = (request or {}).get("messages") or (request or {}).get("input") or []
    for m in reversed(list(items)):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        if role == "user":
            return _text_of(m.get("content") if isinstance(m, dict) else getattr(m, "content", ""))
    return ""


def _strip_internal_turn_metadata(text):
    if not isinstance(text, str):
        return text
    try:
        from daemon_core.voice_protocol import strip_internal_turn_metadata

        return strip_internal_turn_metadata(text)
    except Exception:
        return text


def _add_degraded_response_notice(text):
    if not isinstance(text, str):
        return text
    try:
        from daemon_core.runtime_degradation import (
            add_degraded_response_notice,
        )

        return add_degraded_response_notice(text)
    except Exception:
        if not text or text.startswith(_DEGRADED_NOTICE_FALLBACK):
            return text
        return f"{_DEGRADED_NOTICE_FALLBACK}\n\n{text}"


def _sanitize_response_payload(response, *, degraded=False):
    """Clean private prompt annotations from common host response shapes."""

    def read(container, key, default=None):
        if isinstance(container, dict):
            return container.get(key, default)
        return getattr(container, key, default)

    def write(container, key, value):
        try:
            if isinstance(container, dict):
                container[key] = value
            else:
                setattr(container, key, value)
        except Exception:
            pass

    def clean(text):
        cleaned = _strip_internal_turn_metadata(text)
        return _add_degraded_response_notice(cleaned) if degraded else cleaned

    output_text = read(response, "output_text")
    if isinstance(output_text, str):
        write(
            response,
            "output_text",
            clean(output_text),
        )

    for item in read(response, "output", []) or []:
        for part in read(item, "content", []) or []:
            text = read(part, "text")
            if isinstance(text, str):
                write(part, "text", clean(text))

    for choice in read(response, "choices", []) or []:
        message = read(choice, "message")
        content = read(message, "content") if message is not None else None
        if isinstance(content, str):
            write(message, "content", clean(content))

    # Anthropic Messages: text blocks beside thinking and tool-use blocks.
    content = read(response, "content")
    if isinstance(content, list):
        for block in content:
            text = read(block, "text")
            if read(block, "type") == "text" and isinstance(text, str):
                write(block, "text", clean(text))
    return response


# Preserve sovereign context for each turn. Hermes rebuilds tool iterations
# from its own history, which lacks the L0 dialogue injected at the first call.
# Retain the exact system and sovereign prefix so the voice keeps
# its referents and the turn boundary after using a tool.
_turn_systems: dict = {}
# Exact inbound text per owner, seen at gateway ingress. Hermes skips memory
# prefetch (and so the turn handoff) for slash text and bare greetings, but
# still appends its own first-contact notes to the user message.
_INBOUND_EXACT_TTL_SECONDS = 600
_inbound_exact: dict[str, tuple[str, float]] = {}
_turn_prefixes: dict = {}


def _split_system_turns(messages: list) -> tuple[str, list]:
    """Split the leading system while preserving a near-turn system message."""
    system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    turns = [
        message
        for message in messages[1:]
        if message.get("role") in {"user", "assistant", "developer", "system"}
    ]
    return system, turns


def _turn_prefix(turns: list) -> list:
    """Keep everything before the current user message for later tool calls."""

    for index in range(len(turns) - 1, -1, -1):
        if turns[index].get("role") == "user":
            return [dict(turn) for turn in turns[:index]]
    return [dict(turn) for turn in turns]


def _remember_turn_context(turn_id: str, system: str, turns: list) -> None:
    if len(_turn_systems) > 64 or len(_turn_prefixes) > 64:
        _turn_systems.clear()
        _turn_prefixes.clear()
    _turn_systems[turn_id] = system
    _turn_prefixes[turn_id] = _turn_prefix(turns)


def _tail_from_current_user(items: list) -> list:
    """Discard host history while retaining the current turn and tool records."""

    for index in range(len(items) - 1, -1, -1):
        if items[index].get("role") == "user":
            return list(items[index:])
    return list(items)


def _responses_turns(turns: list) -> list:
    return [
        {
            "role": turn["role"],
            "content": [
                {
                    "type": (
                        "output_text"
                        if turn["role"] == "assistant"
                        else "input_text"
                    ),
                    "text": turn["content"],
                }
            ],
        }
        for turn in turns
        if turn.get("role") in {"user", "assistant"}
    ]


def _responses_instructions(system: str, turns: list) -> str:
    """Move instruction-role turns to the Responses instruction channel."""

    parts = [system]
    for turn in turns:
        if turn.get("role") not in {"developer", "system"}:
            continue
        content = turn.get("content")
        if content is None:
            continue
        text = content if isinstance(content, str) else str(content)
        if text.strip():
            parts.append(text)
    return "\n\n".join(part for part in parts if part.strip())


def _text_blocks(content) -> list:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _anthropic_turns(turns: list) -> list:
    """Dialogue for the Messages API: instruction roles go to ``system``; the
    API requires a leading user turn and alternating roles."""

    messages: list = []
    for turn in turns:
        if turn.get("role") not in {"user", "assistant"}:
            continue
        if messages and messages[-1]["role"] == turn["role"]:
            messages[-1] = {
                "role": turn["role"],
                "content": _text_blocks(messages[-1]["content"]) + _text_blocks(turn["content"]),
            }
        else:
            messages.append({"role": turn["role"], "content": turn["content"]})
    if messages and messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(empty)"})
    return messages


def _anthropic_tail_from_current_user(items: list) -> list:
    """Tool results are user messages in this API; the current turn starts at
    the last user message that is not only tool results."""

    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, list) and content and all(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            continue
        return list(items[index:])
    return list(items)


def _anthropic_system(system: str, native):
    """Replace the native system prompt, keeping its prompt-cache marker."""

    cache = next(
        (
            block["cache_control"]
            for block in (native if isinstance(native, list) else ())
            if isinstance(block, dict) and block.get("cache_control")
        ),
        None,
    )
    return [{"type": "text", "text": system, "cache_control": cache}] if cache else system


def _host_tool_name(tool: dict) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(tool.get("name") or "")


def _web_extract_available() -> bool:
    """Reflect Hermes' real extraction backend instead of its static schema."""

    try:
        from agent.web_search_registry import get_active_extract_provider

        provider = get_active_extract_provider()
        return bool(provider is not None and provider.supports_extract())
    except Exception:
        # Capability discovery must never disable a host tool on uncertainty.
        return True


def _prune_unavailable_host_tools(request: dict) -> dict:
    tools = list(request.get("tools") or [])
    tool_names = {_host_tool_name(tool) for tool in tools}
    hidden_names = {"memory"} & tool_names
    if "web_extract" in tool_names and not _web_extract_available():
        hidden_names.add("web_extract")
    if not hidden_names:
        return request

    pruned = dict(request)
    pruned["tools"] = [
        tool for tool in tools if _host_tool_name(tool) not in hidden_names
    ]
    if not pruned["tools"]:
        pruned.pop("tool_choice", None)
        pruned.pop("parallel_tool_calls", None)
    if "memory" in hidden_names:
        logger.info(
            "Daemon Voice : tool mémoire natif Hermes masqué, outils daemon-core conservés"
        )
    if "web_extract" in hidden_names:
        logger.info(
            "Daemon Voice : web_extract masqué, aucun backend d'extraction disponible"
        )
    return pruned


def _rewrite_request(
    request: dict,
    system: str,
    turns: list | None,
    api_mode: str,
    *,
    tool_loop_prefix: list | None = None,
) -> dict:
    """Copy a request while preserving sovereign context across tool calls.

    turns replaces host history on the first call. Later calls prepend
    tool_loop_prefix to the current user message and Hermes tool records,
    preserving injected dialogue and the current system context.
    Tools remain unchanged.

    Keep dialogue as actual alternating chat messages, not one flattened block."""
    new_request = dict(request)
    if api_mode == "codex_responses":
        if turns is not None:
            new_request["instructions"] = _responses_instructions(system, turns)
            new_request["input"] = _responses_turns(turns)
        elif tool_loop_prefix:
            new_request["instructions"] = _responses_instructions(
                system,
                tool_loop_prefix,
            )
            current_tail = _tail_from_current_user(
                list(new_request.get("input") or [])
            )
            new_request["input"] = [
                *_responses_turns(tool_loop_prefix),
                *current_tail,
            ]
        else:
            new_request["instructions"] = system
        return _prune_unavailable_host_tools(new_request)
    if api_mode == "anthropic_messages":
        # The native system prompt is a top-level field here, not a message.
        instruction_turns = turns if turns is not None else (tool_loop_prefix or [])
        new_request["system"] = _anthropic_system(
            _responses_instructions(system, instruction_turns),
            request.get("system"),
        )
        if turns is not None:
            new_request["messages"] = _anthropic_turns(turns)
        elif tool_loop_prefix:
            tail = _anthropic_tail_from_current_user(list(new_request.get("messages") or []))
            new_request["messages"] = _anthropic_turns([*tool_loop_prefix, *tail[:1]]) + tail[1:]
        return _prune_unavailable_host_tools(new_request)
    messages = list(new_request.get("messages") or [])
    if turns is not None:
        messages = [{"role": "system", "content": system}, *turns]
    elif tool_loop_prefix:
        current_tail = _tail_from_current_user(messages)
        messages = [
            {"role": "system", "content": system},
            *tool_loop_prefix,
            *current_tail,
        ]
    elif messages and messages[0].get("role") == "system":
        messages = [{"role": "system", "content": system}] + messages[1:]
    else:
        messages = [{"role": "system", "content": system}] + messages
    new_request["messages"] = messages
    return _prune_unavailable_host_tools(new_request)


def _degraded_host_contract() -> str:
    try:
        from daemon_core.runtime_degradation import DEGRADED_HOST_CONTRACT

        return DEGRADED_HOST_CONTRACT
    except Exception:
        return _DEGRADED_CONTRACT_FALLBACK


def _append_degraded_host_contract(request: dict, api_mode: str) -> tuple[dict, str]:
    """Keep Hermes' native prompt and usable tools, adding the safety boundary."""

    contract = _degraded_host_contract()
    degraded = dict(request)
    if api_mode == "anthropic_messages":
        native = degraded.get("system")
        current = (
            "\n\n".join(str(block.get("text", "")) for block in native if isinstance(block, dict))
            if isinstance(native, list)
            else str(native or "")
        ).rstrip()
        system = current if contract in current else "\n\n".join(
            part for part in (current, contract) if part
        )
        degraded["system"] = _anthropic_system(system, native)
        return _prune_unavailable_host_tools(degraded), system
    if api_mode == "codex_responses":
        current = str(degraded.get("instructions") or "").rstrip()
        system = current if contract in current else "\n\n".join(
            part for part in (current, contract) if part
        )
        degraded["instructions"] = system
        return _prune_unavailable_host_tools(degraded), system

    messages = [dict(message) for message in degraded.get("messages") or []]
    if messages and messages[0].get("role") == "system":
        current = str(messages[0].get("content") or "").rstrip()
        system = current if contract in current else "\n\n".join(
            part for part in (current, contract) if part
        )
        messages[0] = {**messages[0], "content": system}
    else:
        system = contract
        messages.insert(0, {"role": "system", "content": system})
    degraded["messages"] = messages
    return _prune_unavailable_host_tools(degraded), system


def _delegated_request_or_none(request, **kwargs):
    """Build the delegated request, or return None to use the unmodified host path."""
    if not _flag_on():
        return None
    if str(kwargs.get("platform") or "") not in _platforms():
        return None
    user_id = os.getenv("DAEMON_CORE_USER_ID", "").strip()
    if not user_id or not _ensure_src_on_path():
        return None
    api_mode = str(kwargs.get("api_mode") or "")
    turn_id = str(kwargs.get("turn_id") or "")

    # During a tool loop, restore the sovereign prefix and retain
    # the current message and every tool record produced by Hermes.
    if kwargs.get("api_call_count", 0) > 1:
        if _turn_latency.get(turn_id, {}).get("degraded"):
            degraded, _ = _append_degraded_host_contract(request, api_mode)
            return degraded
        system = _turn_systems.get(turn_id)
        if not system:
            return None
        return _rewrite_request(
            request,
            system,
            None,
            api_mode,
            tool_loop_prefix=_turn_prefixes.get(turn_id),
        )

    prompt_user_text = _last_user_text(request)
    if not prompt_user_text.strip():
        return None
    canary_trace_id = ""
    canary_prefetch_started_at = None
    try:
        from daemon_core.turn_handoff import read_turn_handoff

        trace = read_turn_handoff(user_id, prompt_user_text)
    except Exception:
        trace = None
    if trace:
        message, canary_trace_id, canary_prefetch_started_at = trace
    else:
        message = prompt_user_text
        exact = _inbound_exact.get(user_id)
        if (
            exact
            and time.monotonic() - exact[1] <= _INBOUND_EXACT_TTL_SECONDS
            and exact[0]
            and prompt_user_text.lstrip().startswith(exact[0])
        ):
            message = exact[0]

    try:
        from daemon_core.turn_runtime import TurnRequest, prepare_turn

        plan = prepare_turn(
            TurnRequest(
                user_id=user_id,
                message=message,
                platform=str(kwargs.get("platform") or ""),
                for_delegation=True,
                message_received_at=canary_prefetch_started_at,
                trace_id=canary_trace_id,
            )
        )
        candidate = plan.voice_context
    except Exception as exc:
        trace_id = canary_trace_id or _new_trace_id()
        if len(_turn_latency) > 64:
            _turn_latency.clear()
        _turn_latency[turn_id] = {
            "trace_id": trace_id,
            "prefetch_started_at": canary_prefetch_started_at,
            "turn_id": turn_id,
            "voice_path": "host_fallback",
            "degraded": True,
            "degradation_reason": "canonical_prepare_failed",
            "error_type": exc.__class__.__name__,
        }
        degraded, system = _append_degraded_host_contract(request, api_mode)
        _remember_turn_context(turn_id, system, [])
        logger.warning(
            "Daemon Voice : compositeur canonique indisponible, repli hôte signalé | error=%s",
            exc.__class__.__name__,
        )
        return degraded
    else:
        trace_id = canary_trace_id or _new_trace_id()
        if canary_trace_id:
            try:
                from daemon_core.turn_handoff import write_turn_hot_souvenirs

                if not write_turn_hot_souvenirs(
                    user_id,
                    canary_trace_id,
                    plan.hot_souvenirs,
                ):
                    logger.warning(
                        "Daemon Voice : souvenirs chaudes non corrélées | trace_id=%s",
                        canary_trace_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Daemon Voice : provenance souvenirs indisponible | trace_id=%s | error=%s",
                    canary_trace_id,
                    exc.__class__.__name__,
                )
        if len(_turn_latency) > 64:
            _turn_latency.clear()
        _turn_latency[turn_id] = {
            "trace_id": trace_id,
            "prefetch_started_at": canary_prefetch_started_at,
            "turn_id": turn_id,
            "voice_path": plan.voice_path,
            "prompt_variant": plan.prompt_variant,
            "situation_source": plan.situation_source,
            "memory_source": plan.memory_source,
        }
        system = _with_hermes_progress_preamble(
            plan.leading_system_prompt,
            enabled=_hermes_progress_preamble_canary_enabled(
                str(kwargs.get("platform") or "")
            ),
        )
        turns = plan.turns
        _remember_turn_context(turn_id, system, turns)
        scene_model = getattr(candidate, "scene_model", None)
        working_state = (
            f"scene:r{scene_model.revision}"
            if scene_model is not None
            else plan.situation_source
        )
        logger.info(
            "Daemon Voice : délégation canonique | prompt_variant=%s | catalog_souvenirs=%s | catalog_chars=%s | working_state=%s | scene_delta_chars=%s | hot_souvenirs=%s | hot_chars=%s | api_mode=%s",
            plan.prompt_variant,
            len(candidate.atlas.souvenir_names),
            candidate.atlas.rendered_chars,
            working_state,
            len(getattr(candidate, "scene_delta", "")),
            list(candidate.hot_memory.souvenir_names),
            candidate.hot_memory.rendered_chars,
            api_mode,
        )
        return _rewrite_request(request, system, turns, api_mode)


def _sovereign_middleware(request=None, next_call=None, **kwargs):
    # next_call is single-use under the Hermes contract: call it exactly once,
    # never from an exception handler that could follow its own failure.
    _install_visible_stream_tracking()
    new_request = None
    turn_id = str(kwargs.get("turn_id") or "")
    t0 = time.perf_counter()
    try:
        new_request = _delegated_request_or_none(request, **kwargs)
    except Exception as exc:
        logger.warning(
            "Daemon Voice : frontière canonique indisponible, repli hôte signalé | error=%s",
            exc.__class__.__name__,
        )
        try:
            new_request, _ = _append_degraded_host_contract(
                request,
                str(kwargs.get("api_mode") or ""),
            )
        except Exception:
            new_request = None
        _turn_latency[turn_id] = {
            "trace_id": _new_trace_id(),
            "turn_id": turn_id,
            "voice_path": "host_fallback",
            "degraded": True,
            "degradation_reason": "canonical_prepare_failed",
            "error_type": exc.__class__.__name__,
        }
    t_compose = time.perf_counter() - t0
    base_record = _turn_latency.get(turn_id, {})
    record = {
        **base_record,
        "trace_id": base_record.get("trace_id") or _new_trace_id(),
        "turn_id": turn_id,
        "api_request_id": str(kwargs.get("api_request_id") or ""),
        "api_call_count": int(kwargs.get("api_call_count") or 0),
        "platform": str(kwargs.get("platform") or ""),
        "voice_started_at": time.time(),
        "first_delta_logged": False,
    }
    degraded = bool(record.get("degraded"))
    voice_path = (
        "host_fallback"
        if degraded or new_request is None
        else str(base_record.get("voice_path") or "canonical")
    )
    _observe_continuity(
        record["trace_id"],
        "voice_path",
        "failed" if degraded else "completed" if new_request is not None else "skipped",
        reason_code=(
            str(record.get("degradation_reason"))
            if degraded
            else "delegated" if new_request is not None else "plugin_bypassed"
        ),
        voice_path=voice_path,
        error_type=record.get("error_type"),
    )
    _append_voice_event(
        "voice_start",
        record,
        compose=round(t_compose, 4),
        delegated=new_request is not None,
        degraded=degraded,
    )
    _latency_local.current = record
    _register_active_latency(record)
    # Hermes exposes the actual provider endpoint at this boundary. Adapt only
    # the composed Mistral request, including subsequent native tool calls.
    if new_request is not None and str(kwargs.get("api_mode") or "") == "chat_completions":
        from daemon_core.provider_messages import is_mistral_endpoint, mistral_messages

        if is_mistral_endpoint(str(kwargs.get("base_url") or "")):
            new_request = {
                **new_request,
                "messages": mistral_messages(list(new_request.get("messages") or [])),
            }
    effective_request = new_request if new_request is not None else request
    private_capture = None
    if new_request is not None and not degraded:
        try:
            from daemon_core.voice_request_capture import (
                begin_voice_request_capture,
            )

            private_capture = begin_voice_request_capture(
                effective_request,
                metadata={
                    **record,
                    "provider": str(kwargs.get("provider") or ""),
                    "model": str(kwargs.get("model") or ""),
                    "api_mode": str(kwargs.get("api_mode") or ""),
                    "voice_path": voice_path,
                },
            )
        except Exception as exc:
            # Auditing must remain observational: a bad path or a failed local
            # write can never change the provider call or the visible reply.
            logger.warning(
                "Daemon Voice : capture privée indisponible | error=%s",
                exc.__class__.__name__,
            )
    t0 = time.perf_counter()
    try:
        response = next_call(effective_request)
    except Exception as exc:
        if private_capture is not None:
            try:
                private_capture.fail(
                    exc,
                    duration_s=time.perf_counter() - t0,
                )
            except Exception as capture_exc:
                logger.warning(
                    "Daemon Voice : finalisation de capture privée échouée | error=%s",
                    capture_exc.__class__.__name__,
                )
        _observe_continuity(
            record["trace_id"],
            "response",
            "failed",
            reason_code="host_generation_failed",
            error_type=exc.__class__.__name__,
        )
        _append_voice_event(
            "voice_failed",
            record,
            compose=round(t_compose, 4),
            generation=round(time.perf_counter() - t0, 4),
            delegated=new_request is not None,
            degraded=degraded,
            error=exc.__class__.__name__,
        )
        raise
    finally:
        _latency_local.current = None
        _unregister_active_latency(record)
    if private_capture is not None:
        try:
            private_capture.complete(
                response,
                duration_s=time.perf_counter() - t0,
            )
        except Exception as exc:
            logger.warning(
                "Daemon Voice : finalisation de capture privée échouée | error=%s",
                exc.__class__.__name__,
            )
    _append_voice_event(
        "voice_complete",
        record,
        compose=round(t_compose, 4),
        generation=round(time.perf_counter() - t0, 4),
        delegated=new_request is not None,
        degraded=degraded,
        first_delta_seen=bool(record.get("first_delta_logged")),
    )
    _observe_continuity(
        record["trace_id"],
        "response",
        "completed",
        reason_code="host_generation_completed",
        voice_path=voice_path,
        degraded=degraded,
    )
    return _sanitize_response_payload(response, degraded=degraded)


def _first_meeting_start(event=None, **_kwargs):
    """Observe the owner's exact words; let Telegram Start open the first meeting.

    The exact text lets the voice drop host notes appended to it when no turn
    handoff exists. Hermes answers ``/start`` with an empty reply before the
    model. During the first meeting only, that event stays conversational: the
    voice receives the exact ``/start`` text, as on OpenClaw. Authorization
    still follows.
    """
    try:
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None)
        user_id = os.getenv("DAEMON_CORE_USER_ID", "").strip()
        if (
            event is None
            or getattr(event, "internal", False)
            or not user_id
            or str(getattr(source, "user_id", "")) != user_id
        ):
            return None
        if platform in _platforms() and isinstance(event.text, str):
            _inbound_exact[user_id] = (event.text.strip(), time.monotonic())
        if (
            not getattr(event, "allow_gateway_control", False)
            or platform != "telegram"
            or getattr(source, "chat_type", None) != "dm"
            or (event.text or "").strip() != "/start"
        ):
            return None
        if not _ensure_src_on_path():
            return None
        from daemon_core.first_meeting import is_active
        from daemon_core.paths import MEMORY_ROOT

        if is_active(MEMORY_ROOT, user_id):
            event.allow_gateway_control = False
            logger.info("Daemon Voice : Start ouvre la première rencontre")
    except Exception as exc:
        logger.warning(
            "Daemon Voice : Start laissé à Hermes | %s", exc.__class__.__name__
        )
    return None


def register(ctx) -> None:
    stream_probe = _install_stream_probe()
    final_cleanup = _install_final_delivery_cleanup()
    visible_stream_tracking = _install_visible_stream_tracking()
    managed_anthropic_key = _install_managed_anthropic_key()
    ctx.register_middleware("llm_execution", _sovereign_middleware)
    ctx.register_hook("pre_gateway_dispatch", _first_meeting_start)
    logger.info(
        "Daemon Voice : middleware llm_execution enregistré | "
        "stream_probe=%s final_cleanup=%s visible_stream_tracking=%s "
        "first_meeting_start=on anthropic_api_key=%s",
        stream_probe,
        final_cleanup,
        visible_stream_tracking,
        managed_anthropic_key,
    )
