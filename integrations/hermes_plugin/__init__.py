"""Daemon-core adapter for Hermes Agent (NousResearch/hermes-agent).

Install this directory under $HERMES_HOME/plugins/daemon_core/ to register
an external memory provider without modifying Hermes. Configure:

    memory:
      provider: daemon_core
      memory_enabled: false
      user_profile_enabled: false

DAEMON_CORE_SRC points to the importable src directory. The canonical path
records exact user and assistant messages in the conversation journal (L0)
and submits their references to the durable after-turn worker. That worker
runs maintenance judgments and updates memory projections and scene state.

The voice composes its initial context from local reads and prepared
projections, with no additional LLM call before the host response.
route_context_depth is retained only as a compatibility fallback when
the canonical voice is disabled. Optional session-end maintenance and
the static persona block remain part of the provider interface."""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

L0_RECALL_TOOL_NAME = "daemon_recall_l0"
SOUVENIR_READ_TOOL_NAME = "daemon_read_souvenirs"


def _canonical_voice_enabled(platform: str | None = None) -> bool:
    if os.getenv("DAEMON_CORE_SOVEREIGN_VOICE", "").strip().lower() not in {
        "1",
        "on",
        "true",
    }:
        return False
    if platform:
        platforms = {
            value.strip()
            for value in os.getenv(
                "DAEMON_CORE_SOVEREIGN_PLATFORMS",
                "telegram",
            ).split(",")
            if value.strip()
        }
        return platform in platforms
    return True


def _begin_continuity(
    trace_id: str,
    *,
    user_id: str,
    platform: str,
    session_id: str,
) -> None:
    try:
        from daemon_core.continuity import begin_continuity_turn

        begin_continuity_turn(
            trace_id,
            user_id=user_id,
            platform=platform,
            session_id=session_id,
        )
    except Exception:
        pass


def _observe_continuity(
    trace_id: str,
    stage: str,
    status: str,
    *,
    reason_code: str,
    **details: Any,
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


def _l0_recall_tool_enabled(
    user_id: str | None = None,
    platform: str | None = None,
) -> bool:
    if _canonical_voice_enabled(platform):
        return True
    enabled = os.getenv("DAEMON_CORE_L0_RECALL_TOOL", "").strip().lower()
    if enabled not in {"1", "on", "true"}:
        return False
    if user_id is None:
        return True
    configured = os.getenv(
        "DAEMON_CORE_L0_RECALL_USER_IDS",
        "",
    )
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    return bool(user_id and user_id in allowed)


def _souvenir_read_tool_enabled(
    user_id: str | None = None,
    platform: str | None = None,
) -> bool:
    return _canonical_voice_enabled(platform)


def _verify_l0_evidence_coverage(
    need: str,
    evidence: str,
    provider,
    evidence_context: str = "",
) -> tuple[bool, str]:
    from daemon_core.held_atlas_sovereign import verify_l0_evidence_coverage

    return verify_l0_evidence_coverage(
        need=need, evidence=evidence, provider=provider, evidence_context=evidence_context
    )


# ---------------------------------------------------------------------------
# Bootstrap: make daemon_core importable.
# ---------------------------------------------------------------------------
def _ensure_daemon_core_importable() -> bool:
    configured = os.getenv("DAEMON_CORE_SRC", "").strip()
    source_root = Path(configured).expanduser().resolve(strict=False) if configured else None
    if source_root is not None:
        required = (
            source_root / "daemon_core" / "__init__.py",
            source_root / "daemon_core" / "loop.py",
        )
        if not all(path.is_file() for path in required):
            logger.error(
                "DAEMON_CORE_SRC ne contient pas un runtime daemon-core complet."
            )
            return False
        source = str(source_root)
    else:
        source = ""
    if source and source not in sys.path:
        # The gateway canary deliberately points at an isolated worktree. The
        # configured source must win over an editable install from main.
        sys.path.insert(0, source)
    importlib.invalidate_caches()
    try:
        runtime_module = importlib.import_module("daemon_core.loop")
    except ImportError:
        logger.exception(
            "Le noyau daemon_core.loop est introuvable ou incomplet."
        )
        return False
    module_file = getattr(runtime_module, "__file__", None)
    if not module_file:
        logger.error("daemon_core.loop n'est pas chargé depuis un fichier réel.")
        return False
    if source_root is not None:
        loaded_file = Path(module_file).resolve(strict=False)
        if not loaded_file.is_relative_to(source_root / "daemon_core"):
            logger.error(
                "Le runtime daemon-core chargé ne correspond pas à DAEMON_CORE_SRC."
            )
            return False
    return True


def _wait_for_host_capability_lifecycle(worker: threading.Thread) -> None:
    """Evaluation-only barrier; normal Hermes turns remain non-blocking."""

    raw_timeout = os.getenv(
        "HOST_CAPABILITY_AFTER_TURN_WAIT_SECONDS",
        "",
    ).strip()
    if not raw_timeout or not os.getenv(
        "DAEMON_CORE_LLM_TELEMETRY_FILE",
        "",
    ).strip():
        return
    try:
        timeout = min(420.0, max(0.0, float(raw_timeout)))
    except ValueError:
        return
    started = time.monotonic()
    worker.join(timeout=timeout)
    remaining = max(0.0, timeout - (time.monotonic() - started))
    quiescent = not worker.is_alive()
    try:
        from daemon_core.providers.telemetry import (
            record_lifecycle_marker,
            wait_for_llm_quiescence,
        )

        if quiescent:
            quiescent = wait_for_llm_quiescence(
                timeout_seconds=remaining,
                idle_seconds=1.0,
            )
        record_lifecycle_marker(
            quiescent=quiescent,
            timeout_seconds=timeout,
        )
    except Exception:
        logger.info("Barrière de télémétrie ignorée", exc_info=True)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class DaemonCoreMemoryProvider(MemoryProvider):
    """Hermes external memory provider backed by Daemon Harness."""

    def __init__(self) -> None:
        self._harness = None              # daemon_core.loop.DaemonCore
        self._initialized: bool = False   # Hermes indexes tools before initialize()
        self._hermes_home: str = ""
        self._user_id: str = "default"
        self._platform: str = ""
        self._writes_enabled: bool = True
        self._dream_on_session_end: bool = False
        # Handoff from prefetch to sync_turn: one scene, one judgment.
        # Hermes passes an EMPTY session_id to prefetch and the REAL one to sync_turn:
        # connect them through the current message rather than session_id.
        self._pending = None              # Latest prefetch context, keyed by message.
        self._turn_number = None          # Supplied by on_turn_start even when prefetch receives session_id="".
        self._turn_started_at = None      # Closest available timestamp to message receipt in Hermes.
        self._last_scene = None           # Scene continuity: (frozen candidates, expanded memories, timestamp).
        self._turn_read_souvenirs: list[str] = []
        self._turn_souvenir_read_calls = 0
        self._turn_library_preflight_calls = 0

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return "daemon_core"

    # -- lifecycle ---------------------------------------------------------
    def is_available(self) -> bool:
        # No network call here: only check that the package loads.
        return _ensure_daemon_core_importable()

    def initialize(self, session_id: str, **kwargs) -> None:
        if not _ensure_daemon_core_importable():
            raise RuntimeError("daemon-core runtime contract is unavailable.")
        from daemon_core.loop import DaemonCore

        self._hermes_home = kwargs.get("hermes_home", "")
        # Hermes provides user_id only for gateway sessions; CLI sessions
        # fall back to the profile, then "default".
        self._user_id = (
            kwargs.get("user_id")
            or kwargs.get("agent_identity")
            or "default"
        )
        self._platform = kwargs.get("platform", "") or ""
        # Never write memory from a non-primary context.
        # This follows the ABC contract for subagent, cron and flush contexts.
        self._writes_enabled = kwargs.get("agent_context", "primary") == "primary"
        self._dream_on_session_end = (
            os.getenv("DAEMON_CORE_DREAM_ON_SESSION_END", "").lower() == "true"
        )
        # Build LLM providers (ROUTING_* / CONVERSATION_* / MEMORY_*)
        # from the environment through the project factory.
        self._harness = DaemonCore()
        self._initialized = True
        logger.info(
            "daemon-core initialisé | user_id=%s | platform=%s | writes=%s",
            self._user_id, kwargs.get("platform"), self._writes_enabled,
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Open a new idempotency scope before Hermes calls prefetch()."""
        self._turn_number = turn_number
        self._turn_started_at = time.time()
        self._pending = None
        self._turn_read_souvenirs = []
        self._turn_souvenir_read_calls = 0
        self._turn_library_preflight_calls = 0

    # -- reading: inject personal context before each turn -----------------
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._harness is None:
            return ""
        # Retrying the same turn reuses the assembled context. This avoids
        # duplicate L0 entries and prevents treating the current message
        # as part of the preceding history.
        if self._pending and self._pending[0] == query:
            return self._pending[4]
        from daemon_core.latency_probe import append_latency_event, new_trace_id

        trace_id = new_trace_id()
        _begin_continuity(
            trace_id,
            user_id=self._user_id,
            platform=self._platform,
            session_id=session_id,
        )
        prefetch_started_at = self._turn_started_at or time.time()
        prefetch_t0 = time.perf_counter()
        # Receipt reaction: if preparation takes longer than about three seconds,
        # mark the message with an eyes emoji to acknowledge receipt.
        # This stays outside the conversation and makes no claim about its content.
        # Turns completed before the delay produce no reaction.
        reaction_cancel = None
        try:
            from daemon_core.receipt_reaction import schedule_receipt_reaction
            reaction_cancel = schedule_receipt_reaction(
                self._capture_origin(), self._session_message_id()
            )
        except Exception:
            pass
        canonical_voice_active = _canonical_voice_enabled(self._platform)
        try:
            if canonical_voice_active:
                # The sovereign voice reads the exact active memory itself.
                # Routing remains available after the response for memory
                # maintenance, outside the user's critical path.
                self._last_scene = None
                block, route, selected = "", None, []
                logger.info(
                    "prefetch canonical voice | user_id=%s | radar=skipped",
                    self._user_id,
                )
            else:
                from daemon_core.providers.telemetry import (
                    llm_telemetry_phase,
                )

                with llm_telemetry_phase("critical_path.context"):
                    block, route, selected = self._assemble_context(
                        query,
                        trace_id=trace_id,
                        prefetch_started_at=prefetch_started_at,
                    )
        except Exception as exc:  # Never block the host's turn.
            logger.warning("prefetch daemon-core échoué | %s", exc.__class__.__name__)
            append_latency_event(
                "prefetch_failed",
                trace_id,
                prefetch_total=round(time.perf_counter() - prefetch_t0, 4),
                error=exc.__class__.__name__,
                warmup=False,
            )
            block, route, selected = "", None, None
        finally:
            if reaction_cancel is not None:
                reaction_cancel.set()

        # L0 is the exact record: write after reading the scene,
        # but before requesting the response. The message survives provider failure
        # without being duplicated in the current turn's prompt.
        user_l0_written = False
        user_l0_reference = None
        if self._writes_enabled:
            try:
                from daemon_core.after_turn import record_user_turn

                user_l0_reference = record_user_turn(
                    self._user_id,
                    query,
                    source_trace_id=trace_id,
                )
                user_l0_written = True
            except Exception as exc:
                logger.warning("écriture L0 user au prefetch échouée | %s", exc.__class__.__name__)
        try:
            from daemon_core.held_thought import scene_scope_from_origin

            scene_scope = scene_scope_from_origin(self._capture_origin(), session_id)
        except Exception:
            scene_scope = "primary"
        self._pending = (
            query,
            route,
            selected,
            user_l0_written,
            block,
            trace_id,
            prefetch_started_at,
            scene_scope,
            user_l0_reference,
        )
        # Keep the judged scene for sync_turn, matched through the message.
        # Store the verdict for the daemon_voice plugin:
        # one scene, one judgment; files form the interface between faculties.
        if canonical_voice_active:
            try:
                from daemon_core.turn_handoff import write_turn_handoff

                write_turn_handoff(
                    self._user_id,
                    query,
                    trace_id,
                    prefetch_started_at,
                )
            except Exception:
                pass
        elif route is not None:
            try:
                from daemon_core.sovereign import write_scene_verdict
                write_scene_verdict(
                    self._user_id,
                    query,
                    route,
                    selected,
                    trace_id=trace_id,
                    prefetch_started_at=prefetch_started_at,
                )
            except Exception:
                pass
        append_latency_event(
            "prefetch_return",
            trace_id,
            prefetch_started_at=round(prefetch_started_at, 6),
            since_turn_start=round(max(0.0, time.time() - prefetch_started_at), 4),
            prefetch_call=round(time.perf_counter() - prefetch_t0, 4),
            depth=getattr(route, "context_depth", None),
            souvenir_count=len(selected or []),
            block_chars=len(block),
            canonical_voice=canonical_voice_active,
            warmup=False,
        )
        if route is None:
            return ""
        logger.info(
            "prefetch | session_id=%r | turn=%r | depth=%s | register=%s | souvenirs=%s | block_chars=%s",
            session_id, self._turn_number, route.context_depth, route.scene_register, selected, len(block),
        )
        return block

    # -- writing: consolidate memory after each turn -----------------------
    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if self._harness is None or not self._writes_enabled:
            return
        try:
            from daemon_core.runtime_degradation import (
                strip_degraded_response_notice,
            )

            assistant_content = strip_degraded_response_notice(
                assistant_content
            )
        except Exception:
            pass
        origin = self._capture_origin()
        pending = self._pending
        self._pending = None
        if pending and pending[0] == user_content:
            route, selected = pending[1], pending[2]
            user_l0_written = bool(pending[3])
            source_trace_id = pending[5]
            source_turn_started_at = pending[6]
            scene_scope = pending[7]
            user_l0_reference = (
                pending[8] if len(pending) > 8 else None
            )
        else:
            route, selected = None, None  # Fallback: the harness will judge again.
            user_l0_written = False
            from daemon_core.held_thought import scene_scope_from_origin
            from daemon_core.latency_probe import new_trace_id

            source_trace_id = new_trace_id()
            source_turn_started_at = self._turn_started_at or time.time()
            scene_scope = scene_scope_from_origin(origin, session_id)
            user_l0_reference = None
        tool_read_souvenirs = tuple(self._turn_read_souvenirs)
        read_souvenirs = list(tool_read_souvenirs)
        self._turn_read_souvenirs = []
        self._turn_souvenir_read_calls = 0
        try:
            from daemon_core.turn_handoff import read_turn_hot_souvenirs

            hot_souvenirs = read_turn_hot_souvenirs(
                self._user_id,
                source_trace_id,
            )
        except Exception as exc:
            hot_souvenirs = ()
            logger.warning(
                "Provenance des souvenirs de la voix indisponible | source=%s | error=%s",
                source_trace_id,
                exc.__class__.__name__,
            )
        for name in hot_souvenirs:
            if name not in read_souvenirs:
                read_souvenirs.append(name)
        canonical_library_active = _souvenir_read_tool_enabled(
            self._user_id,
            self._platform,
        )
        assistant_l0_written = False
        durable_enabled = False
        try:
            from daemon_core.after_turn import (
                SILENT_ASSISTANT_MARKERS,
                record_turn_message,
                record_user_turn,
            )
            from daemon_core.after_turn_spool import (
                AfterTurnSpool,
                AfterTurnJob,
                after_turn_spool_enabled,
            )
            from daemon_core.memory.writer import GroundZeroReference

            durable_enabled = after_turn_spool_enabled()
            assistant_visible = (
                assistant_content.strip().upper()
                not in SILENT_ASSISTANT_MARKERS
            )
            if durable_enabled and assistant_visible:
                if not isinstance(
                    user_l0_reference,
                    GroundZeroReference,
                ):
                    user_l0_reference = record_user_turn(
                        self._user_id,
                        user_content,
                        source_trace_id=source_trace_id,
                    )
                    user_l0_written = True
                assistant_l0_reference = record_turn_message(
                    self._user_id,
                    "assistant",
                    assistant_content,
                    source_trace_id=source_trace_id,
                )
                assistant_l0_written = True
                if not isinstance(
                    user_l0_reference,
                    GroundZeroReference,
                ) or not isinstance(
                    assistant_l0_reference,
                    GroundZeroReference,
                ):
                    raise RuntimeError("missing durable L0 reference")
                queued = AfterTurnSpool().handoff(
                    AfterTurnJob(
                        turn_id=source_trace_id,
                        host="hermes",
                        user_id=self._user_id,
                        source_turn_started_at=source_turn_started_at,
                        scene_scope=scene_scope,
                        user_l0=user_l0_reference,
                        assistant_l0=assistant_l0_reference,
                        selected_souvenirs=(
                            tuple(selected)
                            if selected is not None
                            else None
                        ),
                        read_souvenirs=tuple(read_souvenirs),
                        tool_read_souvenirs=tool_read_souvenirs,
                        library_active=canonical_library_active,
                        origin=origin,
                    )
                )
                logger.info(
                    "Après-tour durable confié | source=%s | state=%s | created=%s",
                    source_trace_id,
                    queued.state,
                    queued.created,
                )
                return
        except Exception as exc:
            if durable_enabled:
                logger.error("Après-tour durable non accepté ; L0 conservé | error=%s",
                             exc.__class__.__name__)
                return
            logger.warning(
                "Après-tour durable non confié, repli historique | %s",
                exc.__class__.__name__,
            )
        logger.info(
            "sync_turn | session_id=%r | route_reuse=%s (sinon le radar sera recalculé)",
            session_id, route is not None,
        )

        # The ABC requires a nonblocking call: use a daemon thread.
        def _run() -> None:
            try:
                from daemon_core.after_turn import (
                    AfterTurnHooks,
                    AfterTurnRequest,
                    complete_turn,
                    record_turn_message,
                    record_user_turn,
                )

                turn = AfterTurnRequest(
                    user_id=self._user_id,
                    user_message=user_content,
                    assistant_response=assistant_content,
                    scene_scope=scene_scope,
                    source_trace_id=source_trace_id,
                    source_turn_started_at=source_turn_started_at,
                    route=route,
                    selected_souvenirs=(
                        tuple(selected)
                        if selected is not None
                        else None
                    ),
                    read_souvenirs=tuple(read_souvenirs),
                    user_l0_recorded=user_l0_written,
                    assistant_l0_recorded=assistant_l0_written,
                    library_active=canonical_library_active,
                )

                def _record_l0(request) -> None:
                    # Prefetch normally writes the user message already. This fallback
                    # covers consumers that call sync_turn alone.
                    if not request.user_l0_recorded:
                        record_user_turn(
                            request.user_id,
                            request.user_message,
                            source_trace_id=request.source_trace_id,
                        )
                    if (
                        request.assistant_is_visible
                        and not request.assistant_l0_recorded
                    ):
                        record_turn_message(
                            request.user_id,
                            "assistant",
                            request.assistant_response,
                            source_trace_id=request.source_trace_id,
                        )

                scene_runner = None
                try:
                    from daemon_core.scene_model import (
                        schedule_scene_model_revision,
                        scene_model_shadow_enabled,
                    )

                    if (
                        canonical_library_active
                        or scene_model_shadow_enabled(self._user_id)
                    ):
                        def _revise_scene_model() -> None:
                            scheduled = schedule_scene_model_revision(
                                user_id=self._user_id,
                                scene_scope=scene_scope,
                                provider=self._harness.memory_provider,
                                source_trace_id=source_trace_id,
                                source_turn_started_at=source_turn_started_at,
                                read_souvenirs=read_souvenirs,
                            )
                            logger.info(
                                "Scene model planifié | source=%s | scope=%s | accepted=%s",
                                source_trace_id,
                                scene_scope,
                                scheduled,
                            )
                        scene_runner = _revise_scene_model
                except Exception as exc:
                    logger.info(
                        "Scene model shadow ignoré | error=%s",
                        exc.__class__.__name__,
                    )

                def _update_memory(request):
                    return self._harness.update_memory_after_response(
                        user_id=request.user_id,
                        message=request.user_message,
                        response=request.assistant_response,
                        route=request.route,
                        selected_souvenirs=(
                            list(request.selected_souvenirs)
                            if request.selected_souvenirs is not None
                            else None
                        ),
                    )

                def _after_memory(result) -> None:
                    self._maybe_schedule_followup(result, origin)
                    try:
                        from daemon_core.paths import MEMORY_ROOT
                        from daemon_core.semantic_atlas import (
                            coalesced_semantic_atlas_prewarmer,
                        )

                        atlas_prewarmer = coalesced_semantic_atlas_prewarmer(
                            self._user_id,
                            MEMORY_ROOT,
                        )
                        if atlas_prewarmer is not None:
                            atlas_prewarmer.process_batch((result,))
                    except Exception as exc:
                        logger.warning(
                            "Préchauffage Atlas après-tour différé | %s",
                            exc.__class__.__name__,
                        )

                def _on_error(stage: str, exc: Exception) -> None:
                    if stage.startswith("scene"):
                        logger.info(
                            "Scene model shadow ignoré | error=%s",
                            exc.__class__.__name__,
                        )

                def _spawn(target, name: str) -> None:
                    threading.Thread(
                        target=target,
                        name=name,
                        daemon=True,
                    ).start()

                complete_turn(
                    turn,
                    AfterTurnHooks(
                        record_l0=_record_l0,
                        update_memory=_update_memory,
                        revise_working_state=None,
                        revise_scene=(
                            (lambda request: scene_runner())
                            if scene_runner is not None
                            else None
                        ),
                        after_memory=_after_memory,
                        on_error=_on_error,
                    ),
                    spawn=_spawn,
                )
            except Exception as exc:
                _observe_continuity(
                    source_trace_id,
                    "cycle",
                    "failed",
                    reason_code="after_turn_worker_failed",
                    error_type=exc.__class__.__name__,
                )
                logger.warning("sync_turn daemon-core échoué | %s", exc.__class__.__name__)

        worker = threading.Thread(target=_run, name="daemon-sync-turn", daemon=True)
        try:
            worker.start()
        except Exception as exc:
            _observe_continuity(
                source_trace_id,
                "cycle",
                "failed",
                reason_code="after_turn_thread_start_failed",
                error_type=exc.__class__.__name__,
            )
            raise
        _wait_for_host_capability_lifecycle(worker)

    # -- relances proactives (followup) ------------------------------------
    def _session_message_id(self) -> Optional[str]:
        """Identifiant Telegram du message entrant (contexte live uniquement)."""
        try:
            from gateway.session_context import get_session_env
            return get_session_env("HERMES_SESSION_MESSAGE_ID", "") or None
        except Exception:
            return None

    def _capture_origin(self) -> Dict[str, Any]:
        """Chat origin for deliver=origin: {platform, chat_id, thread_id?}."""
        platform = chat_id = thread_id = ""
        try:
            from gateway.session_context import get_session_env
            platform = get_session_env("HERMES_SESSION_PLATFORM", "")
            chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
            thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "")
        except Exception:
            pass
        # Safe DM fallback: without a session origin, a Telegram DM's
        # chat_id equals user_id. Groups rely on the session origin.
        if not platform:
            platform = self._platform
        if not chat_id and platform and self._user_id not in ("", "default"):
            chat_id = self._user_id
        if not platform or not chat_id:
            logger.info("Relance : origine introuvable (platform=%r chat_id=%r) — livraison impossible", platform, chat_id)
            return {}
        origin = {"platform": platform, "chat_id": str(chat_id)}
        if thread_id:
            origin["thread_id"] = str(thread_id)
        return origin

    def _load_followup_cron(self):
        if getattr(self, "_followup_cron_mod", None) is None:
            import importlib.util
            import os as _os
            path = _os.path.join(_os.path.dirname(__file__), "followup_cron.py")
            spec = importlib.util.spec_from_file_location("daemon_followup_cron", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._followup_cron_mod = mod
        return self._followup_cron_mod

    def _maybe_schedule_followup(self, result, origin: Dict[str, Any]) -> None:
        from daemon_core import followup as fu
        if result is None or not fu.followups_enabled():
            return
        uid = self._user_id
        expired = fu.expire_overdue_pending_intents(uid)
        if expired:
            logger.info(
                "Relances expirées avant programmation | user=%s | count=%d",
                uid,
                len(expired),
            )

        # 1) Follow-up preference, judged by MemoryAgent without keywords.
        pref = getattr(result, "followups_pref", None)
        if pref and pref.get("action") == "disable":
            fu.set_disabled(uid, True)
            cron = self._load_followup_cron()
            for old in fu.list_intents(uid, state="pending"):
                cron.cancel_followup(old, hermes_home=self._hermes_home)
            logger.info("Relances coupées (off-switch) | user=%s | evidence=%r", uid, pref.get("evidence"))
            return
        if pref and pref.get("action") == "enable":
            fu.set_disabled(uid, False)
            logger.info("Relances réactivées | user=%s | evidence=%r", uid, pref.get("evidence"))

        # 2) Spontaneous follow-up intent.
        intent = getattr(result, "followup", None)
        if not intent:
            return
        if fu.is_disabled(uid):
            logger.info("Relance ignorée | off-switch actif | user=%s", uid)
            return
        fire_at = fu.clamp_to_waking(intent["fire_at"])
        if fu.followups_dry_run():
            logger.info(
                "Relance [DRY-RUN] non programmée | user=%s | fire_at=%s | angle=%s | topic=%r | evidence=%r",
                uid, fire_at, intent.get("angle"), intent.get("topic"), intent.get("evidence"),
            )
            return
        active = fu.active_pending_intents(uid)
        if len(active) >= fu.MAX_ACTIVE_FOLLOWUPS:
            logger.info("Relance ignorée | déjà %d en attente | user=%s", len(active), uid)
            return
        if fu.delivered_today_count(uid) >= fu.MAX_DELIVERED_PER_DAY:
            logger.info("Relance ignorée | quota du jour (%d) atteint | user=%s", fu.MAX_DELIVERED_PER_DAY, uid)
            return
        cron = self._load_followup_cron()
        new = fu.new_followup(uid, fire_at, intent["topic"], intent["evidence"], intent.get("angle", "check-in"), origin)
        job_id = cron.schedule_followup(new, hermes_home=self._hermes_home)
        logger.info("Relance programmée | user=%s | followup=%s | job=%s | fire_at=%s", uid, new.id, job_id, fire_at)

    # -- daemon voice in the static system prompt -------------------------
    def system_prompt_block(self) -> str:
        # The plugin supplies the daemon voice with priority over other personas
        # such as Hermes' SOUL.md. No host file needs to be changed:
        # installing the plugin enables the voice.
        if self._harness is None:
            return ""
        try:
            from daemon_core.persona import MAX_DAEMON_PERSONA_CHARS, load_daemon_persona
            persona = load_daemon_persona(self._user_id).strip()
        except Exception:
            return ""
        if not persona:
            return ""
        # Historical latent-memory decision (2026-07-05): the notebook is no longer
        # injected permanently, since always-visible memory encourages performance.
        # The host path needs only the persona; on conversation scenes,
        # the sovereign voice loads the notebook when the radar judges it useful.
        return (
            f"""# Your voice and character (TAKE PRECEDENCE)
Adopt this voice and character in all your responses. They take precedence over any other description of personality or voice elsewhere in this prompt, and over the style of your own previous messages. Never describe them explicitly: embody them.

{persona[:MAX_DAEMON_PERSONA_CHARS]}

# Skills scope (firm rule)
NEVER create, modify or self-improve a skill concerning your personality, voice, conversational style, relationship with the user, companion experience or user memory. These aspects are managed by the daemon outside the skills system. Skills remain reserved for tool-based capabilities (code, research, integrations…)."""
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas: List[Dict[str, Any]] = []
        # Hermes indexes provider tool names before initialize() supplies the
        # gateway identity. The exact-memory tools are constitutive whenever
        # the canonical voice is enabled for the host platform.
        souvenir_tool_enabled = (
            _souvenir_read_tool_enabled()
            if not self._initialized
            else _souvenir_read_tool_enabled(self._user_id, self._platform)
        )
        if souvenir_tool_enabled:
            schemas.append(
                {
                    "name": SOUVENIR_READ_TOOL_NAME,
                    "description": (
                        'Open the exact content of active personal souvenirs from exact paths selected in the catalog supplied to the voice. Reading is bounded to eight souvenirs; the catalog alone is never a source of facts.'
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 8,
                                "description": (
                                    'One to eight canonical paths copied exactly from the catalog, without brackets or the .md extension.'
                                ),
                            },
                            "collection_path": {
                                "type": "string",
                                "description": (
                                    'Exact path of a collection marked in the catalog, also present in paths. The same call opens a bounded page of its proven members.'
                                ),
                            },
                            "collection_offset": {
                                "type": "integer",
                                "minimum": 0,
                                "default": 0,
                                "description": (
                                    'Explicit offset for a subsequent page announced by the previous read.'
                                ),
                            },
                            "scene": {
                                "type": "object",
                                "description": (
                                    'Minimal assessment of the whole situation before reading, drawn from the message and the entire scene rather than just the selected paths.'
                                ),
                                "properties": {
                                    "established": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 8,
                                        "description": (
                                            'People, place, time, constraints or other established conditions that may change the response. Include the current social and physical setting, not only the subjects of the latest sentence. A still-unknown property neither downgrades nor removes an already-established condition.'
                                        ),
                                    },
                                    "uncertain": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 8,
                                        "description": (
                                            'Contingent unknowns in the current scene that would materially change the response, one per entry and only if the response must expose them at the requested level of precision. Do not include only the durable preferences the read must retrieve. Name the exact missing detail: an unknown about one property does not make another already-established fact uncertain or remove it from the decision scope. The scope itself is uncertain only if several readings of the episode genuinely remain possible.'
                                        ),
                                    },
                                    "need": {
                                        "type": "string",
                                        "description": (
                                            'Precise item the souvenirs must establish or complete before responding.'
                                        ),
                                    },
                                    "handling": {
                                        "type": "object",
                                        "description": (
                                            'Pre-reading conduct hypothesis. It will be reassessed from the exact bodies and must never prevent opening them.'
                                        ),
                                        "properties": {
                                            "mode": {
                                                "type": "string",
                                                "enum": [
                                                    "direct",
                                                    "clarify",
                                                    "condition",
                                                ],
                                                "description": (
                                                    'Pre-reading hypothesis: if an unknown remains after the exact bodies, clarify asks the single question then waits without composing the decision that depends on it, and condition states the condition first. If reading resolves the unknown, direct becomes possible again.'
                                                ),
                                            },
                                            "plan": {
                                                "type": "string",
                                                "description": (
                                                    'With clarify, the single exact natural question; the voice then waits without composing any dependent choice. With condition, the exact condition preceding any dependent proposal. With direct, a brief internal justification.'
                                                ),
                                            },
                                        },
                                        "required": ["mode", "plan"],
                                        "additionalProperties": False,
                                    },
                                },
                                "required": [
                                    "established",
                                    "uncertain",
                                    "need",
                                    "handling",
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["paths", "scene"],
                        "additionalProperties": False,
                    },
                }
            )
        if _l0_recall_tool_enabled(
            None if not self._initialized else self._user_id,
            None if not self._initialized else self._platform,
        ):
            schemas.append(
                {
                    "name": L0_RECALL_TOOL_NAME,
                    "description": (
                        'Exceptional, bounded search in exact past personal exchanges. It can verify a missing personal fact or recover a precise chronological boundary. Use only with a defensible temporal or conversational basis. Not for the web or external facts.'
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "need": {
                                "type": "string",
                                "description": (
                                    'A single atomic personal item missing from the response, with its attribution. Split distinct needs into separate calls.'
                                ),
                            },
                            "lookup_kind": {
                                "type": "string",
                                "enum": [
                                    "personal_fact",
                                    "conversation_chronology",
                                ],
                                "description": (
                                    '`personal_fact` verifies an attributed item and first consults plausible souvenirs. `conversation_chronology` retrieves what was discussed immediately before or after a branching point and accesses the exact L0 order directly.'
                                ),
                            },
                        },
                        "required": ["need"],
                        "additionalProperties": False,
                    },
                }
            )
        return schemas

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        started_at = time.perf_counter()
        try:
            raw = self._handle_tool_call(tool_name, args, **kwargs)
        except Exception:
            try:
                from daemon_core.evidence_observability import (
                    observe_evidence_tool_result,
                )

                trace_id = self._pending[5] if self._pending else ""
                observe_evidence_tool_result(
                    trace_id,
                    tool_name=tool_name,
                    arguments=args,
                    result={"status": "error"},
                    started_at=started_at,
                )
            except Exception:
                pass
            raise
        try:
            from daemon_core.evidence_observability import (
                observe_evidence_tool_result,
            )

            trace_id = self._pending[5] if self._pending else ""
            parsed = json.loads(raw)
            observe_evidence_tool_result(
                trace_id,
                tool_name=tool_name,
                arguments=args,
                result=parsed if isinstance(parsed, dict) else {"status": "error"},
                started_at=started_at,
            )
        except Exception:
            pass
        return raw

    def _handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        if tool_name == SOUVENIR_READ_TOOL_NAME:
            if self._harness is None or not _souvenir_read_tool_enabled(
                self._user_id,
                self._platform,
            ):
                return json.dumps(
                    {
                        "status": "unavailable",
                        "reason": "Souvenir reading is not active for this session.",
                    },
                    ensure_ascii=False,
                )
            paths = args.get("paths") if isinstance(args, dict) else None
            if (
                not isinstance(paths, list)
                or not paths
                or any(not isinstance(path, str) or not path.strip() for path in paths)
            ):
                return json.dumps(
                    {
                        "status": "error",
                        "reason": "A list of non-empty exact paths is required.",
                    },
                    ensure_ascii=False,
                )
            try:
                from daemon_core.held_atlas_sovereign import (
                    collection_coverage_checkpoint,
                    parse_scene_coverage,
                    read_exact_active_souvenirs,
                    scene_coverage_checkpoint,
                )

                coverage = parse_scene_coverage(args)
                result = read_exact_active_souvenirs(
                    paths,
                    user_id=self._user_id,
                    platform=self._platform,
                    collection_path=args.get("collection_path", ""),
                    collection_offset=args.get("collection_offset", 0),
                )
                self._turn_souvenir_read_calls += 1
                for name in result.souvenir_names:
                    if name not in self._turn_read_souvenirs:
                        self._turn_read_souvenirs.append(name)
                status = (
                    "ok"
                    if not result.rejected_paths
                    and (
                        result.collection_page is None
                        or result.collection_page.next_offset is None
                    )
                    else "partial"
                    if result.souvenirs
                    else "not_found"
                )
                logger.info(
                    "Lecture souvenirs voix | user_id=%s | appel=%s | status=%s | paths=%s | chars=%s | scene_established=%s | scene_uncertain=%s",
                    self._user_id,
                    self._turn_souvenir_read_calls,
                    status,
                    list(result.souvenir_names),
                    result.rendered_chars,
                    len(coverage.established),
                    len(coverage.uncertain),
                )
                try:
                    from daemon_core.latency_probe import append_latency_event

                    trace_id = self._pending[5] if self._pending else ""
                    if trace_id:
                        append_latency_event(
                            "sovereign_library_read",
                            trace_id,
                            read_call=self._turn_souvenir_read_calls,
                            requested_paths=list(paths),
                            read_paths=list(result.souvenir_names),
                            rejected_paths=list(result.rejected_paths),
                            chars=result.rendered_chars,
                            scene_established=list(coverage.established),
                            scene_uncertain=list(coverage.uncertain),
                            scene_need=coverage.need,
                            scene_handling_mode=coverage.handling_mode,
                            scene_handling_plan=coverage.handling_plan,
                            collection=(
                                result.collection_page.as_dict()
                                if result.collection_page is not None
                                else None
                            ),
                        )
                except Exception:
                    pass
                return json.dumps(
                    {
                        "status": status,
                        "capsules": [
                            {"path": name, "content": content}
                            for name, content in result.souvenirs
                        ],
                        "rejected_paths": list(result.rejected_paths),
                        "total_chars": result.rendered_chars,
                        "collection": (
                            result.collection_page.as_dict()
                            if result.collection_page is not None
                            else None
                        ),
                        "scene_assessment": coverage.as_dict(),
                        "coverage_checkpoint": " ".join(
                            part
                            for part in (
                                scene_coverage_checkpoint(coverage),
                                collection_coverage_checkpoint(
                                    result.collection_page
                                ),
                            )
                            if part
                        ),
                    },
                    ensure_ascii=False,
                )
            except ValueError as exc:
                return json.dumps(
                    {"status": "error", "reason": str(exc)},
                    ensure_ascii=False,
                )
            except Exception as exc:
                logger.warning(
                    "Lecture souvenirs voix échouée | user_id=%s | error=%s",
                    self._user_id,
                    exc.__class__.__name__,
                )
                return json.dumps(
                    {
                        "status": "error",
                        "reason": "Exact reading failed; infer nothing from this.",
                    },
                    ensure_ascii=False,
                )
        if tool_name != L0_RECALL_TOOL_NAME:
            return json.dumps({"status": "error", "reason": "Unknown tool."})
        if self._harness is None or not _l0_recall_tool_enabled(
            self._user_id,
            self._platform,
        ):
            return json.dumps(
                {"status": "unavailable", "reason": "L0 recall is not active for this session."},
                ensure_ascii=False,
            )
        need = args.get("need") if isinstance(args, dict) else None
        lookup_kind = (
            args.get("lookup_kind", "personal_fact")
            if isinstance(args, dict)
            else ""
        )
        if not isinstance(need, str) or not need.strip():
            return json.dumps(
                {"status": "error", "reason": "The exact personal recall need is required."},
                ensure_ascii=False,
            )
        if lookup_kind not in {"personal_fact", "conversation_chronology"}:
            return json.dumps(
                {"status": "error", "reason": "The L0 recall kind is invalid."},
                ensure_ascii=False,
            )

        if (
            lookup_kind == "personal_fact"
            and _souvenir_read_tool_enabled(self._user_id, self._platform)
            and self._turn_library_preflight_calls < 2
        ):
            self._turn_library_preflight_calls += 1
            preflight_started = time.perf_counter()
            try:
                from daemon_core.held_atlas_sovereign import (
                    build_exact_memory_atlas,
                    judge_unread_souvenirs_before_l0,
                    read_exact_active_souvenirs,
                )
                from daemon_core.memory.reader import load_recent_dialogue

                preflight = judge_unread_souvenirs_before_l0(
                    need.strip(),
                    build_exact_memory_atlas(
                        user_id=self._user_id,
                        platform=self._platform,
                    ),
                    self._turn_read_souvenirs,
                    self._harness.routing_provider,
                    recent_dialogue=load_recent_dialogue(self._user_id, max_turns=14),
                )
                logger.info(
                    "Pré-vol bibliothèque voix | user_id=%s | appel=%s | paths=%s | confidence=%.2f | elapsed=%.3fs",
                    self._user_id,
                    self._turn_library_preflight_calls,
                    list(preflight.paths),
                    preflight.confidence,
                    time.perf_counter() - preflight_started,
                )
                if preflight.paths:
                    self._turn_souvenir_read_calls += 1
                    exact = read_exact_active_souvenirs(
                        list(preflight.paths),
                        user_id=self._user_id,
                        platform=self._platform,
                    )
                    if not exact.souvenirs:
                        raise ValueError("preflight souvenirs unavailable")
                    for name in exact.souvenir_names:
                        if name not in self._turn_read_souvenirs:
                            self._turn_read_souvenirs.append(name)
                    try:
                        from daemon_core.latency_probe import append_latency_event

                        trace_id = self._pending[5] if self._pending else ""
                        if trace_id:
                            append_latency_event(
                                "sovereign_library_preflight",
                                trace_id,
                                preflight_call=self._turn_library_preflight_calls,
                                need=need.strip()[:500],
                                read_paths=list(exact.souvenir_names),
                                rejected_paths=list(exact.rejected_paths),
                                chars=exact.rendered_chars,
                                confidence=preflight.confidence,
                                elapsed=round(time.perf_counter() - preflight_started, 4),
                            )
                    except Exception:
                        pass
                    return json.dumps(
                        {
                            "status": "capsule_sources",
                            "l0_searched": False,
                            "reason": (
                                "Plausible durable sources remained unread "
                                "before the L0 fallback. Reassess their exact coverage."
                            ),
                            "selection_reason": preflight.reason,
                            "capsules": [
                                {"path": name, "content": content}
                                for name, content in exact.souvenirs
                            ],
                            "rejected_paths": list(exact.rejected_paths),
                            "total_chars": exact.rendered_chars,
                        },
                        ensure_ascii=False,
                    )
            except Exception as exc:
                logger.info(
                    "Pré-vol bibliothèque ignoré | user_id=%s | error=%s",
                    self._user_id,
                    exc.__class__.__name__,
                )

        from daemon_core.memory.coverage import RecoveryNeed
        from daemon_core.memory.forensics import (
            attempt_l0_chronology_recovery,
            attempt_l0_forensic_recovery,
            chronology_tool_payload,
            failed_recall_tool_payload,
            personal_fact_tool_payload,
        )
        from daemon_core.memory.reader import load_recent_dialogue

        started_at = time.perf_counter()
        try:
            recall_message = f"Recall need: {need.strip()}"
            recent_dialogue = load_recent_dialogue(self._user_id, max_turns=14)
            if lookup_kind == "conversation_chronology":
                recovery = attempt_l0_chronology_recovery(
                    user_id=self._user_id,
                    message=recall_message,
                    recent_dialogue=recent_dialogue,
                    provider=self._harness.routing_provider,
                )
            else:
                recovery = attempt_l0_forensic_recovery(
                    user_id=self._user_id,
                    message=recall_message,
                    recent_dialogue=recent_dialogue,
                    recovery_needs=[RecoveryNeed(description=need.strip())],
                    provider=self._harness.routing_provider,
                )
            status = recovery.status
            confidence = recovery.confidence
            if (
                status == "found"
                and lookup_kind == "personal_fact"
                and recovery.relation_status == "established"
            ):
                complete, coverage_reason = _verify_l0_evidence_coverage(
                    need=need.strip(),
                    evidence=recovery.evidence,
                    evidence_context=getattr(recovery, "evidence_context", ""),
                    provider=self._harness.routing_provider,
                )
                if not complete:
                    status = "partial"
                    logger.info(
                        "Rappel L0 partiel | user_id=%s | reason=%s",
                        self._user_id,
                        coverage_reason
                        or "La citation exacte ne couvre pas toute la pièce demandée.",
                    )
                    confidence = 0.0
            if lookup_kind == "conversation_chronology":
                payload = chronology_tool_payload(recovery)
            else:
                payload = personal_fact_tool_payload(
                    recovery,
                    status=status,
                    confidence=confidence,
                )
            logger.info(
                "Rappel L0 voix | user_id=%s | kind=%s | status=%s | elapsed=%.3fs",
                self._user_id,
                lookup_kind,
                status,
                time.perf_counter() - started_at,
            )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.warning(
                "Rappel L0 voix échoué | user_id=%s | error=%s",
                self._user_id,
                exc.__class__.__name__,
            )
            return json.dumps(
                failed_recall_tool_payload(evidence_kind=lookup_kind),
                ensure_ascii=False,
            )
    # -- optional session-end consolidation --------------------------------
    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._harness is None or not self._writes_enabled or not self._dream_on_session_end:
            return

        def _run() -> None:
            try:
                from daemon_core.dreaming import run_dreaming
                run_dreaming(
                    user_id=self._user_id,
                    provider=self._harness.provider,
                    dry_run=False,
                )
            except Exception as exc:
                logger.warning("dreaming daemon-core échoué | %s", exc.__class__.__name__)

        threading.Thread(target=_run, name="daemon-dreaming", daemon=True).start()

    def shutdown(self) -> None:
        # Daemon threads need no explicit shutdown.
        self._pending = None

    # Scene continuity window: beyond it, the scene is considered cold
    # even when the radar proposes the same candidates.
    SELECTION_REUSE_TTL_SECONDS = 900

    def _reused_selection(self, route) -> Optional[List[str]]:
        """Reuse only an identical leaf selection; domains always need a fresh zoom."""
        if os.getenv("DAEMON_CORE_SELECTION_REUSE", "on").lower() in {"off", "0", "false"}:
            self._last_scene = None
            return None
        if self._last_scene is None or route.mode != "contextual" or not route.candidate_souvenirs:
            if route.mode != "contextual" or not route.candidate_souvenirs:
                self._last_scene = None
            return None
        candidates, selected, ts = self._last_scene
        current_candidates = frozenset(route.candidate_souvenirs)
        if current_candidates != candidates:
            self._last_scene = None
            return None
        from daemon_core.memory.reader import souvenir_domain_names
        if current_candidates.intersection(souvenir_domain_names()):
            self._last_scene = None
            return None
        # A leaf candidate is reusable only when it is exactly what was loaded.
        # This guards against a stale zoom result masquerading as continuity.
        if frozenset(selected) != current_candidates:
            self._last_scene = None
            return None
        if time.time() - ts > self.SELECTION_REUSE_TTL_SECONDS:
            self._last_scene = None
            return None
        self._last_scene = (candidates, selected, time.time())  # Keep the scene warm.
        try:
            from daemon_core.memory.writer import record_souvenir_recalls
            if selected:
                record_souvenir_recalls(selected)  # Keep recall tracking accurate.
        except Exception:
            pass
        return list(selected)

    # ------------------------------------------------------------------
    # Reading core: radar, memory selection and Markdown context.
    # ------------------------------------------------------------------
    def _assemble_context(
        self,
        query: str,
        *,
        trace_id: str = "",
        prefetch_started_at: float | None = None,
        warmup: bool = False,
    ):
        """Run the reading half of handle_message without generating a response.

        Route, select memories and build an injectable Markdown block.
        Return (text_block, route, memory_names)."""
        from daemon_core.context_router import route_context_depth
        from daemon_core.loop import ROUTER_HISTORY_TURNS, select_souvenirs_for_route
        from daemon_core.memory.reader import (
            load_active_context, load_souvenir, load_immediate_focus,
            load_recent_dialogue, load_trajectory, load_user_agenda,
            load_user_context,
        )
        from daemon_core.prompt_composer import (
            build_scene_section,
            current_time_context,
        )

        from daemon_core.latency_probe import append_latency_event, new_trace_id

        trace_id = trace_id or new_trace_id()
        prefetch_started_at = prefetch_started_at or time.time()
        preparation_t0 = time.perf_counter()
        uid = self._user_id
        user_context = load_user_context(uid)
        user_preferences = ""  # legacy assembly slot; preferences.md is archival
        user_agenda = load_user_agenda(uid)
        active_context = load_active_context(uid)
        immediate_focus = load_immediate_focus(uid)
        recent_dialogue = load_recent_dialogue(uid, max_turns=8)
        router_dialogue = load_recent_dialogue(
            uid,
            max_turns=ROUTER_HISTORY_TURNS,
        )

        radar_agenda = user_agenda

        t0 = time.perf_counter()
        route = route_context_depth(
            message=query,
            provider=self._harness.routing_provider,
            recent_dialogue=router_dialogue,
            user_context=user_context,
            user_preferences=user_preferences,
            user_agenda=radar_agenda,
            moment_memory=active_context,
            current_discussion=immediate_focus,
        )
        t_radar = time.perf_counter() - t0

        # Scene continuity: only exactly matching leaf notes can be reused.
        # A domain depends on the current message and must be expanded again,
        # even when its name is unchanged. Full rollback:
        # DAEMON_CORE_SELECTION_REUSE=off.
        t0 = time.perf_counter()
        selected = self._reused_selection(route)
        reused = selected is not None
        if not reused:
            selection = select_souvenirs_for_route(
                route=route,
                message=query,
                provider=self._harness.provider,
                recent_dialogue=recent_dialogue,
                user_context=user_context,
                user_preferences=user_preferences,
                user_agenda=radar_agenda,
                moment_memory=active_context,
                current_discussion=immediate_focus,
            )
            selected = selection.selected if selection else []
            if route.mode == "contextual" and route.candidate_souvenirs:
                from daemon_core.memory.reader import souvenir_domain_names
                candidates = frozenset(route.candidate_souvenirs)
                reusable = (
                    not candidates.intersection(souvenir_domain_names())
                    and frozenset(selected) == candidates
                )
                self._last_scene = (
                    (candidates, list(selected), time.time()) if reusable else None
                )
        t_selection = time.perf_counter() - t0
        t_coverage = 0.0  # Set even if synchronous coverage fails.
        coverage_attempted = False
        coverage_error = ""
        logger.info(
            "prefetch timings | radar=%.1fs | selection=%.1fs%s | depth=%s | souvenirs=%s",
            t_radar, t_selection, " (réutilisée)" if reused else "", route.context_depth, selected,
        )

        sections: List[str] = []
        # Put the actual time first. The radar already calculates it, but
        # without passing it on, the responder can misread the time of day.
        # This informs tone and advice as well as scheduling questions.
        sections.append(
            f"## Current time\n{current_time_context()}\nGround your responses in this actual moment (tone, advice, what is plausible now), not only when answering questions about time."
        )
        # Forward the radar's scene reading (request, register, ambiguity);
        # otherwise Hermes loses that judgment after it is computed.
        scene = build_scene_section(
            active_request=route.active_request,
            ambiguity=route.ambiguity,
            ambiguity_reason=route.reason if route.ambiguity == "material" else "",
            scene_register=route.scene_register,
            fil_clos=getattr(route, "fil_clos", False),
        )
        # Voice autonomy (2026-07-03): the scene informs rather than coaches.
        # Remove register instructions from the Hermes path:
        # provide scene facts, not directions for performing a character.
        if scene.strip():
            sections.append(scene)
        trajectory = load_trajectory(uid)
        if trajectory.strip():
            sections.append(f"## Trajectory (your interlocutor's current arc)\n{trajectory.strip()}")
        if user_preferences.strip():
            sections.append(f"## Practical notes\n{user_preferences.strip()}")
        if user_context.strip():
            sections.append(f'## Stable facts\n{user_context.strip()}')
        if user_agenda.strip():
            sections.append(f'## Short agenda\n{user_agenda.strip()}')
        if active_context.strip():
            sections.append(f'## Background context\n{active_context.strip()}')
        if immediate_focus.strip():
            sections.append(f'## Current thread\n{immediate_focus.strip()}')
        loaded_souvenirs = {}
        for nom in selected:
            contenu = load_souvenir(nom).strip()
            if contenu:
                loaded_souvenirs[nom] = contenu
                sections.append(f"## {nom}\n{contenu[:1200]}")

        # Coverage runs synchronously behind COVERAGE_TO_VOICE for factual
        # recall scenes, adding its dossier to the voice context.
        # Otherwise it remains shadow observation outside the critical path.
        if route.mode == "contextual" and not query.startswith("(préchauffage"):
            try:
                from daemon_core.memory.coverage import (
                    assess_memory_coverage,
                    append_memory_coverage_trace,
                    build_coverage_sources,
                    build_coverage_voice_dossier,
                    coverage_to_voice_enabled,
                    coverage_voice_gate,
                    schedule_memory_coverage_shadow,
                )

                coverage_sources = build_coverage_sources(
                    user_context=user_context,
                    user_preferences=user_preferences,
                    user_agenda=radar_agenda,
                    active_context=active_context,
                    current_discussion=immediate_focus,
                    trajectory=trajectory,
                    souvenirs=loaded_souvenirs,
                    souvenir_limit=1200,
                )
                if coverage_to_voice_enabled() and coverage_voice_gate(
                    route.scene_register, route.memory_plan
                ):
                    coverage_attempted = True
                    t0 = time.perf_counter()
                    try:
                        assessment = assess_memory_coverage(
                            message=query,
                            recent_dialogue=recent_dialogue,
                            plan=route.memory_plan,
                            sources=coverage_sources,
                            provider=self._harness.memory_provider,
                        )
                        dossier = build_coverage_voice_dossier(assessment, route.memory_plan)
                        append_memory_coverage_trace(
                            user_id=uid,
                            message=query,
                            plan=route.memory_plan,
                            selected_souvenirs=selected,
                            assessment=assessment,
                            scene_register=route.scene_register,
                            interaction_mode=route.interaction_mode,
                            sync=True,
                        )
                    finally:
                        t_coverage = time.perf_counter() - t0
                    logger.info(
                        "Couverture SYNCHRONE | overall=%s | dossier=%s | temps=%.1fs",
                        assessment.overall_status,
                        "oui" if dossier else "non",
                        t_coverage,
                    )
                    if dossier:
                        sections.append("## State of the memory dossier for this response\n" + dossier)
                else:
                    schedule_memory_coverage_shadow(
                        user_id=uid,
                        message=query,
                        recent_dialogue=recent_dialogue,
                        plan=route.memory_plan,
                        selected_souvenirs=selected,
                        sources=coverage_sources,
                        provider=self._harness.memory_provider,
                        scene_register=route.scene_register,
                        interaction_mode=route.interaction_mode,
                    )
            except Exception as exc:
                coverage_error = exc.__class__.__name__
                logger.info(
                    "Couverture mémoire non exécutée | error=%s",
                    exc.__class__.__name__,
                )

        def _organ(provider) -> str:
            return (
                f"{provider.__class__.__name__}/{getattr(provider, 'model', '?')}"
                f"/{getattr(provider, 'reasoning_effort', None) or 'default'}"
            )

        preparation_total = time.perf_counter() - preparation_t0
        measured_stages = t_radar + t_selection + t_coverage
        append_latency_event(
            "context_prepared",
            trace_id,
            prefetch_started_at=round(prefetch_started_at, 6),
            preparation_total=round(preparation_total, 4),
            radar=round(t_radar, 4),
            selection=round(t_selection, 4),
            coverage=round(t_coverage, 4),
            other=round(max(0.0, preparation_total - measured_stages), 4),
            coverage_attempted=coverage_attempted,
            coverage_error=coverage_error or None,
            selection_reused=reused,
            depth=route.context_depth,
            souvenir_count=len(selected),
            souvenirs=list(selected),
            selection_source=getattr(selection, "source", None) if not reused else "scene_reuse",
            radar_final_complete=getattr(route, "radar_final_complete", None),
            radar_final_reason=getattr(route, "radar_final_reason", "") or None,
            routing=_organ(self._harness.routing_provider),
            conversation=_organ(self._harness.provider),
            memory=_organ(self._harness.memory_provider),
            message_chars=len(query),
            warmup=warmup,
        )

        if not sections:
            return "", route, selected
        block = (
            """# Personal context
Treat these items as things you already naturally know about your interlocutor. Use them only if they help with the present request, without reciting them as an inventory. Never speak of memory, souvenirs, injected context, recording, or "native" or "disabled" memory, even if the interlocutor themselves mentions how you operate.

"""
            + "\n\n".join(sections)
        )
        return block, route, selected


# ---------------------------------------------------------------------------
# Plugin entry point expected by the Hermes loader.
# ---------------------------------------------------------------------------
# Voice sovereignty (llm_execution middleware) lives in a SEPARATE
# plugin, integrations/hermes_voice_plugin. Hermes' general loader
# excludes memory plugins (kind='exclusive'), so it would never load
# middleware declared here.
def register(ctx) -> None:
    """Called by Hermes' memory-plugin discovery mechanism."""
    ctx.register_memory_provider(DaemonCoreMemoryProvider())
