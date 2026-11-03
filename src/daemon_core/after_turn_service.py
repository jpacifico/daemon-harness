"""Neutral process owner for durable after-turn work."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daemon_core.after_turn_executor import (
    execute_after_turn_job,
    execute_after_turn_projection_batch,
    execute_scene_projection,
)
from daemon_core.after_turn_spool import (
    AfterTurnSpool,
    ensure_after_turn_worker,
    scene_coalescing_enabled,
    spool_namespace_status,
    stop_after_turn_workers,
)
from daemon_core.continuity import runtime_revision, technical_ref
from daemon_core.memory.integrity import atomic_write_text, memory_file_try_lock
from daemon_core.platform_support import is_private_file
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT
from daemon_core.semantic_atlas import coalesced_semantic_atlas_prewarmer


AFTER_TURN_SERVICE_SCHEMA = "daemon.after-turn-service.v1"
AFTER_TURN_OWNER_ENV = "DAEMON_CORE_AFTER_TURN_OWNER"
AFTER_TURN_OWNER_EXTERNAL = "external"
AFTER_TURN_SERVICE_HEARTBEAT = "service.json"
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 15.0
SUPPORTED_HOSTS = ("openclaw", "hermes")

logger = logging.getLogger("daemon_core.after_turn_service")

BatchObserver = Callable[[Any, tuple[Any, ...]], None]


def after_turn_service_path(spool_root: Path | None = None) -> Path:
    return (spool_root or AfterTurnSpool().root) / AFTER_TURN_SERVICE_HEARTBEAT


def read_after_turn_service_status(
    *,
    spool_root: Path | None = None,
    memory_root: Path | None = None,
    expected_revision: str = "",
    now: float | None = None,
    max_age_seconds: float = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    expected_profile: dict | None = None,
) -> dict[str, Any]:
    """Return a privacy-safe health verdict for the neutral owner."""

    path = after_turn_service_path(spool_root)
    current = time.time() if now is None else float(now)
    failures: list[str] = []
    namespace = spool_namespace_status(path.parent, memory_root or MEMORY_ROOT)
    if namespace != "ok":
        failures.append(namespace)
    payload: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
        if not is_private_file(path):
            failures.append("after_turn_service_heartbeat_permissions_too_open")
    except (OSError, UnicodeError, ValueError):
        failures.append("after_turn_service_heartbeat_unavailable")

    if payload:
        if payload.get("schema") != AFTER_TURN_SERVICE_SCHEMA:
            failures.append("after_turn_service_heartbeat_invalid")
        if payload.get("status") != "running":
            failures.append("after_turn_service_not_running")
        try:
            heartbeat_at = float(payload.get("heartbeat_at", 0.0))
        except (TypeError, ValueError):
            heartbeat_at = 0.0
        if (
            heartbeat_at <= 0.0
            or current - heartbeat_at > max_age_seconds
            or heartbeat_at - current > max_age_seconds
        ):
            failures.append("after_turn_service_heartbeat_stale")
        expected_memory = technical_ref(
            str((memory_root or MEMORY_ROOT).expanduser().resolve())
        )
        if payload.get("memory_root_ref") != expected_memory:
            failures.append("after_turn_service_memory_mismatch")
        revision = expected_revision.strip()
        if revision and payload.get("runtime_revision") != revision:
            failures.append("after_turn_service_revision_mismatch")
        if set(payload.get("hosts") or ()) != set(SUPPORTED_HOSTS):
            failures.append("after_turn_service_hosts_missing")
        if payload.get("worker_errors"):
            failures.append("after_turn_service_worker_error")

    try:
        worker_count = max(0, int(payload.get("worker_count", 0) or 0))
    except (TypeError, ValueError):
        worker_count = 0
    if payload and worker_count < len(SUPPORTED_HOSTS):
        failures.append("after_turn_service_workers_missing")
    if expected_profile is not None and (
        not expected_profile.get("ok") or payload.get("continuity_profile") != expected_profile
    ):
        failures.append("after_turn_service_profile_mismatch")

    return {
        "ok": not failures,
        "code": failures[0] if failures else "ok",
        "failures": tuple(sorted(set(failures))),
        "worker_count": worker_count,
    }


class AfterTurnService:
    """Own all durable workers independently from conversational hosts."""

    def __init__(
        self,
        *,
        spool: AfterTurnSpool | None = None,
        user_ids: tuple[str, ...] | None = None,
        batch_observers: Mapping[str, BatchObserver] | None = None,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 2.0,
        harness_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.spool = spool or AfterTurnSpool()
        self.user_ids = user_ids or configured_after_turn_user_ids()
        if not self.user_ids:
            raise RuntimeError("after-turn service requires an explicit user scope")
        self.batch_observers = dict(batch_observers or {})
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.heartbeat_seconds = max(0.1, float(heartbeat_seconds))
        self._harness_factory = harness_factory or self._build_harness
        self._harnesses: dict[str, Any] = {}
        self._workers: dict[tuple[str, str], Any] = {}
        self._started_at = time.time()

    def _build_harness(self) -> Any:
        from daemon_core.loop import DaemonCore
        from daemon_core.providers.factory import (
            build_conversation_provider,
            build_memory_provider,
            build_routing_provider,
        )

        return DaemonCore(
            provider=build_conversation_provider(),
            routing_provider=build_routing_provider(),
            memory_provider=build_memory_provider(),
        )

    def _harness(self, user_id: str) -> Any:
        harness = self._harnesses.get(user_id)
        if harness is None:
            harness = self._harness_factory()
            self._harnesses[user_id] = harness
        return harness

    def ensure_workers(self) -> int:
        coalesce_scene = scene_coalescing_enabled()
        for user_id in self.user_ids:
            harness = self._harness(user_id)
            atlas_prewarmer = coalesced_semantic_atlas_prewarmer(
                user_id,
                MEMORY_ROOT,
            )
            for host in SUPPORTED_HOSTS:
                key = (host, user_id)
                if key in self._workers and self._workers[key].alive:
                    continue

                def _handle(job, *, active_harness=harness):
                    return execute_after_turn_job(
                        job,
                        harness=active_harness,
                        logger=logger,
                        defer_scene=coalesce_scene,
                        defer_replaceable_projections=True,
                    )

                def _project(job, *, active_harness=harness) -> None:
                    execute_scene_projection(
                        job,
                        harness=active_harness,
                        logger=logger,
                    )

                def _finish_batch(
                    executions,
                    *,
                    active_host=host,
                    active_harness=harness,
                    active_prewarmer=atlas_prewarmer,
                ) -> None:
                    result = execute_after_turn_projection_batch(
                        executions,
                        harness=active_harness,
                        logger=logger,
                    )
                    observer = self.batch_observers.get(active_host)
                    if observer is not None and result is not None:
                        observer(result, executions)
                    if active_prewarmer is not None:
                        active_prewarmer.process_batch(executions)

                self._workers[key] = ensure_after_turn_worker(
                    host=host,
                    user_id=user_id,
                    handler=_handle,
                    projection_handler=_project if coalesce_scene else None,
                    batch_handler=_finish_batch,
                    spool=self.spool,
                )
        return len(self._workers)

    def write_heartbeat(self, *, status: str = "running") -> None:
        revision = runtime_revision(project_root=PROJECT_ROOT)
        payload = {
            "schema": AFTER_TURN_SERVICE_SCHEMA,
            "status": status,
            "pid": os.getpid(),
            "started_at": datetime.fromtimestamp(
                self._started_at,
                timezone.utc,
            ).isoformat(timespec="seconds"),
            "heartbeat_at": time.time(),
            "runtime_revision": revision,
            "memory_root_ref": technical_ref(
                str(MEMORY_ROOT.expanduser().resolve())
            ),
            "spool_root_ref": technical_ref(
                str(self.spool.root.expanduser().resolve())
            ),
            "hosts": list(SUPPORTED_HOSTS),
            "worker_count": len(self._workers),
            "worker_errors": sum(bool(getattr(worker, "last_error", ""))
                                 for worker in self._workers.values()),
        }
        path = after_turn_service_path(self.spool.root)
        from daemon_core.installation_profile import profile_status
        profile = profile_status(os.environ, user_ids=self.user_ids, memory_root=MEMORY_ROOT)
        if profile is not None:
            payload["continuity_profile"] = profile
        atomic_write_text(
            path,
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        path.chmod(0o600)

    def run(self, stop_event: threading.Event) -> None:
        ownership_lock = self.spool.root / "locks" / "service-owner.lock"
        with memory_file_try_lock(ownership_lock) as acquired:
            if not acquired:
                raise RuntimeError("another after-turn service owns this spool")
            try:
                next_heartbeat = 0.0
                while not stop_event.is_set():
                    self.ensure_workers()
                    current = time.monotonic()
                    if current >= next_heartbeat:
                        self.write_heartbeat()
                        next_heartbeat = current + self.heartbeat_seconds
                    stop_event.wait(self.poll_seconds)
            finally:
                stop_after_turn_workers()
                self.write_heartbeat(status="stopped")


def configured_after_turn_user_ids() -> tuple[str, ...]:
    configured = os.getenv(
        "DAEMON_CORE_AFTER_TURN_USER_IDS",
        "",
    ).strip()
    values = [value.strip() for value in configured.split(",") if value.strip()]
    personal = os.getenv("DAEMON_CORE_USER_ID", "").strip()
    if not values and personal:
        values.append(personal)
    return tuple(dict.fromkeys(values))


__all__ = [
    "AFTER_TURN_OWNER_ENV",
    "AFTER_TURN_OWNER_EXTERNAL",
    "AFTER_TURN_SERVICE_SCHEMA",
    "AfterTurnService",
    "configured_after_turn_user_ids",
    "read_after_turn_service_status",
]
