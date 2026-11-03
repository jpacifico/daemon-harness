"""Host-neutral contract and ordering for work performed after a visible turn."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Callable

from daemon_core.providers.telemetry import llm_telemetry_phase


SILENT_ASSISTANT_MARKERS = frozenset(
    {"NO_REPLY", "[SILENT]", "SILENT", "NO REPLY"}
)


def unique_souvenir_names(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result = []
    seen = set()
    for group in groups:
        for value in group:
            name = str(value).strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
    return tuple(result)


def record_turn_message(
    user_id: str,
    role: str,
    content: str,
    *,
    writer: Callable[..., Any] | None = None,
    source_trace_id: str = "",
    idempotency_key: str = "",
    occurred_at: float = 0.0,
):
    """Write one exact L0 message under the adapter's idempotency scope."""

    stage = f"l0_{role}" if role in {"user", "assistant"} else "l0_message"
    try:
        if writer is None:
            from daemon_core.memory.writer import append_ground_zero

            result = append_ground_zero(
                user_id=user_id,
                role=role,
                content=content,
                turn_id=source_trace_id,
                idempotency_key=(
                    idempotency_key
                    or (
                        f"{source_trace_id}:{role}"
                        if source_trace_id
                        else ""
                    )
                ),
                occurred_at=occurred_at,
                return_reference=True,
            )
        else:
            writer_kwargs = dict(
                user_id=user_id,
                role=role,
                content=content,
            )
            if occurred_at > 0:
                writer_kwargs["occurred_at"] = occurred_at
            result = writer(**writer_kwargs)
    except Exception as exc:
        from daemon_core.continuity import record_continuity_stage

        record_continuity_stage(
            source_trace_id,
            stage,
            "failed",
            reason_code="writer_failed",
            error_type=exc.__class__.__name__,
            role=role,
        )
        raise
    from daemon_core.continuity import record_continuity_stage

    record_continuity_stage(
        source_trace_id,
        stage,
        "completed",
        reason_code="writer_completed",
        evidence="exact_writer_returned",
        role=role,
    )
    return result


def record_user_turn(
    user_id: str,
    message: str,
    *,
    writer: Callable[..., Any] | None = None,
    source_trace_id: str = "",
    idempotency_key: str = "",
):
    return record_turn_message(
        user_id,
        "user",
        message,
        writer=writer,
        source_trace_id=source_trace_id,
        idempotency_key=idempotency_key,
    )


@dataclass(frozen=True)
class AfterTurnRequest:
    user_id: str
    user_message: str
    assistant_response: str
    scene_scope: str
    source_trace_id: str
    source_turn_started_at: float
    route: Any = None
    selected_souvenirs: tuple[str, ...] | None = None
    read_souvenirs: tuple[str, ...] = ()
    user_l0_recorded: bool = False
    assistant_l0_recorded: bool = False
    library_active: bool = False

    def __post_init__(self) -> None:
        if self.selected_souvenirs is not None:
            object.__setattr__(
                self,
                "selected_souvenirs",
                unique_souvenir_names(self.selected_souvenirs),
            )
        object.__setattr__(
            self,
            "read_souvenirs",
            unique_souvenir_names(self.read_souvenirs),
        )

    @property
    def assistant_is_visible(self) -> bool:
        return (
            self.assistant_response.strip().upper()
            not in SILENT_ASSISTANT_MARKERS
        )

    @property
    def memory_souvenirs(self) -> tuple[str, ...] | None:
        if self.library_active:
            return unique_souvenir_names(
                self.read_souvenirs,
                self.selected_souvenirs or (),
            )
        return self.selected_souvenirs


@dataclass(frozen=True)
class AfterTurnHooks:
    update_memory: Callable[[AfterTurnRequest], Any]
    record_l0: Callable[[AfterTurnRequest], None] | None = None
    revise_working_state: Callable[[AfterTurnRequest], None] | None = None
    revise_scene: Callable[[AfterTurnRequest], None] | None = None
    after_memory: Callable[[Any], None] | None = None
    on_error: Callable[[str, Exception], None] | None = None


@dataclass(frozen=True)
class AfterTurnExecution:
    memory_result: Any
    working_state_started: bool
    scene_started: bool


class AfterTurnStageError(RuntimeError):
    """One asynchronous after-turn stage failed before acknowledgement."""

    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(f"after-turn stage failed: {stage}")
        self.stage = stage
        self.cause = cause


ThreadSpawner = Callable[[Callable[[], None], str], None]


def spawn_daemon_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def complete_turn(
    request: AfterTurnRequest,
    hooks: AfterTurnHooks,
    *,
    spawn: ThreadSpawner = spawn_daemon_thread,
    defer_scene: bool = False,
) -> AfterTurnExecution:
    """Run the promoted ordering: working state || memory, then scene."""

    from daemon_core.continuity import ContinuityCycleObserver

    receipt = ContinuityCycleObserver(request.source_trace_id)

    if hooks.record_l0 is None:
        receipt.transition(
            "l0",
            (
                "completed"
                if request.user_l0_recorded
                and (
                    request.assistant_l0_recorded
                    or not request.assistant_is_visible
                )
                else "skipped"
            ),
            reason_code=(
                "adapter_reported"
                if request.user_l0_recorded
                and (
                    request.assistant_l0_recorded
                    or not request.assistant_is_visible
                )
                else "hook_not_configured"
            ),
            user_l0_recorded=request.user_l0_recorded,
            assistant_l0_recorded=request.assistant_l0_recorded,
            hook_present=False,
        )
    else:
        receipt.transition(
            "l0",
            "pending",
            reason_code="hook_started",
            user_l0_recorded=request.user_l0_recorded,
            assistant_l0_recorded=request.assistant_l0_recorded,
            hook_present=True,
        )
        try:
            hooks.record_l0(request)
        except Exception as exc:
            receipt.transition(
                "l0",
                "failed",
                reason_code="hook_failed",
                error_type=exc.__class__.__name__,
                hook_present=True,
            )
            receipt.skip_remaining(reason_code="l0_dependency_failed")
            raise
        receipt.transition(
            "l0",
            "completed",
            reason_code="hook_completed",
            user_l0_recorded=True,
            assistant_l0_recorded=(
                request.assistant_is_visible
                or request.assistant_l0_recorded
            ),
            hook_present=True,
        )

    working_state_done = threading.Event()
    working_state_started = False

    if hooks.revise_working_state is None:
        working_state_done.set()
        receipt.transition(
            "working_state",
            "skipped",
            reason_code="hook_not_configured",
            hook_present=False,
        )
    else:
        def _revise_working_state() -> None:
            try:
                with llm_telemetry_phase("after_turn.working_state"):
                    hooks.revise_working_state(request)
            except Exception as exc:
                receipt.transition(
                    "working_state",
                    "failed",
                    reason_code="hook_failed",
                    error_type=exc.__class__.__name__,
                    hook_present=True,
                )
                if hooks.on_error is not None:
                    hooks.on_error("working_state", exc)
            else:
                receipt.transition(
                    "working_state",
                    "completed",
                    reason_code="hook_completed",
                    hook_present=True,
                )
            finally:
                working_state_done.set()

        try:
            receipt.transition(
                "working_state",
                "pending",
                reason_code="thread_starting",
                hook_present=True,
            )
            spawn(
                _revise_working_state,
                f"daemon-working-state-{request.source_trace_id}",
            )
            working_state_started = True
        except Exception as exc:
            working_state_done.set()
            receipt.transition(
                "working_state",
                "failed",
                reason_code="thread_start_failed",
                error_type=exc.__class__.__name__,
                hook_present=True,
            )
            if hooks.on_error is not None:
                hooks.on_error("working_state_start", exc)

    try:
        receipt.transition(
            "memory",
            "pending",
            reason_code="hook_started",
            hook_present=True,
        )
        with llm_telemetry_phase("after_turn.memory"):
            memory_result = hooks.update_memory(request)
    except Exception as exc:
        receipt.transition(
            "memory",
            "failed",
            reason_code="hook_failed",
            error_type=exc.__class__.__name__,
            hook_present=True,
        )
        receipt.transition(
            "scene_schedule",
            "skipped",
            reason_code="memory_dependency_failed",
        )
        receipt.transition(
            "followup",
            "skipped",
            reason_code="memory_dependency_failed",
        )
        if hooks.on_error is not None:
            hooks.on_error("memory", exc)
        raise
    receipt.transition(
        "memory",
        "completed",
        reason_code="hook_completed",
        hook_present=True,
    )

    scene_started = False
    if hooks.revise_scene is not None:
        def _revise_scene() -> None:
            working_state_done.wait()
            try:
                with llm_telemetry_phase("after_turn.scene_schedule"):
                    hooks.revise_scene(request)
            except Exception as exc:
                receipt.transition(
                    "scene_schedule",
                    "failed",
                    reason_code="hook_failed",
                    error_type=exc.__class__.__name__,
                    hook_present=True,
                )
                if hooks.on_error is not None:
                    hooks.on_error("scene", exc)
            else:
                receipt.transition(
                    "scene_schedule",
                    "completed",
                    reason_code="hook_completed",
                    hook_present=True,
                )

        try:
            receipt.transition(
                "scene_schedule",
                "pending",
                reason_code="thread_starting",
                hook_present=True,
            )
            spawn(_revise_scene, f"daemon-scene-{request.source_trace_id}")
            scene_started = True
        except Exception as exc:
            receipt.transition(
                "scene_schedule",
                "failed",
                reason_code="thread_start_failed",
                error_type=exc.__class__.__name__,
                hook_present=True,
            )
            if hooks.on_error is not None:
                hooks.on_error("scene_start", exc)
    elif defer_scene:
        receipt.transition(
            "scene_schedule",
            "pending",
            reason_code="durable_projection_deferred",
            hook_present=False,
        )
    else:
        receipt.transition(
            "scene_schedule",
            "skipped",
            reason_code="hook_not_configured",
            hook_present=False,
        )

    if hooks.after_memory is not None:
        try:
            receipt.transition(
                "followup",
                "pending",
                reason_code="hook_started",
                hook_present=True,
            )
            with llm_telemetry_phase("after_turn.followup"):
                hooks.after_memory(memory_result)
        except Exception as exc:
            receipt.transition(
                "followup",
                "failed",
                reason_code="hook_failed",
                error_type=exc.__class__.__name__,
                hook_present=True,
            )
            raise
        receipt.transition(
            "followup",
            "completed",
            reason_code="hook_completed",
            hook_present=True,
        )
    else:
        receipt.transition(
            "followup",
            "skipped",
            reason_code="hook_not_configured",
            hook_present=False,
        )

    return AfterTurnExecution(
        memory_result=memory_result,
        working_state_started=working_state_started,
        scene_started=scene_started,
    )


def complete_turn_and_wait(
    request: AfterTurnRequest,
    hooks: AfterTurnHooks,
    *,
    defer_scene: bool = False,
) -> AfterTurnExecution:
    """Run one cycle and wait for its child stages before acknowledging it."""

    workers: list[threading.Thread] = []
    failures: list[tuple[str, Exception]] = []
    failures_lock = threading.Lock()

    def _spawn(target: Callable[[], None], name: str) -> None:
        worker = threading.Thread(
            target=target,
            name=name,
            daemon=True,
        )
        workers.append(worker)
        worker.start()

    def _on_error(stage: str, exc: Exception) -> None:
        with failures_lock:
            failures.append((stage, exc))
        if hooks.on_error is not None:
            hooks.on_error(stage, exc)

    try:
        execution = complete_turn(
            request,
            replace(hooks, on_error=_on_error),
            spawn=_spawn,
            defer_scene=defer_scene,
        )
    finally:
        for worker in workers:
            worker.join()
    if failures:
        stage, cause = failures[0]
        raise AfterTurnStageError(stage, cause) from cause
    return execution


__all__ = [
    "AfterTurnExecution",
    "AfterTurnHooks",
    "AfterTurnRequest",
    "AfterTurnStageError",
    "SILENT_ASSISTANT_MARKERS",
    "complete_turn",
    "complete_turn_and_wait",
    "record_turn_message",
    "record_user_turn",
    "spawn_daemon_thread",
    "unique_souvenir_names",
]
