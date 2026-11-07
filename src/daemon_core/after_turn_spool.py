"""Small durable filesystem spool for host-neutral after-turn work."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from daemon_core.continuity import (
    continuity_receipts_enabled,
    finalize_deferred_scene,
    read_continuity_receipt,
    record_continuity_stage,
    technical_ref,
)
from daemon_core.memory.integrity import (
    atomic_write_text,
    memory_file_lock,
    memory_file_try_lock,
)
from daemon_core.memory.writer import (
    GroundZeroReference,
    read_ground_zero_reference,
    read_ground_zero_record,
    safe_memory_user_id,
)
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT
from daemon_core.platform_support import process_is_alive as _pid_alive


AFTER_TURN_SPOOL_ENV = "DAEMON_CORE_AFTER_TURN_SPOOL"
AFTER_TURN_SPOOL_DIR_ENV = "DAEMON_CORE_AFTER_TURN_SPOOL_DIR"
SCENE_COALESCING_ENV = "DAEMON_CORE_SCENE_COALESCING"
AFTER_TURN_SPOOL_SCHEMA = "daemon.after-turn-job.v1"
AFTER_TURN_QUARANTINE_SCHEMA = "daemon.after-turn-quarantine.v1"
WORKER_INTERRUPTED = "WorkerInterrupted"
AFTER_TURN_RUNTIME_SCHEMA = "daemon.after-turn-spool-runtime.v1"
SCENE_PROJECTION_SCHEMA = "daemon.scene-projection-job.v2"
SCENE_PROJECTION_QUARANTINE_SCHEMA = (
    "daemon.scene-projection-quarantine.v1"
)
_SUPPORTED_SCENE_PROJECTION_SCHEMAS = frozenset(
    {"daemon.scene-projection-job.v1", SCENE_PROJECTION_SCHEMA}
)
DEFAULT_LEASE_SECONDS = 30 * 60
DEFAULT_MAX_ATTEMPTS = 3
# After the quick attempts, a turn whose sources are intact is retried at
# growing intervals (about a day and a half in all), then set aside with an
# exact receipt so the turns queued behind it can proceed. Every attempt calls
# the model, so the number of attempts must stay finite.
TERMINAL_RETRY_DELAYS_SECONDS = (300.0, 1800.0, 7200.0, 21600.0, 86400.0)
DEFAULT_POLL_SECONDS = 1.0
MAX_COALESCED_SCENE_TURNS = 64
MAX_COALESCED_SCENE_BYTES = 16_000
MAX_COALESCED_SCENE_AGE_SECONDS = 3 * 60
MAX_RECENT_SOUVENIR_AGE_SECONDS = 30 * 60
MAX_RECENT_SOUVENIR_DOCUMENTS = 64
MAX_RECENT_SOUVENIR_DOCUMENT_BYTES = 256 * 1024

_ENABLED_VALUES = frozenset({"1", "canary", "on", "true"})
_STATES = ("pending", "running", "failed")
_JOB_STATES = _STATES + ("quarantined",)
_PROJECTION_STATES = ("scene_pending", "scene_running", "scene_failed")
_WORKERS: dict[tuple[str, str, str], "AfterTurnWorker"] = {}
_WORKERS_LOCK = threading.Lock()
logger = logging.getLogger("daemon_core.after_turn_spool")


def spool_namespace_status(root: Path, memory_root: Path) -> str:
    """Inspect identity without initializing, repairing or rebinding storage."""
    marker = root / "runtime.json"
    try:
        if marker.is_symlink() or marker.stat().st_size > 4096:
            return "after_turn_spool_namespace_invalid"
        current = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "after_turn_spool_namespace_missing"
    except (OSError, UnicodeError, ValueError):
        return "after_turn_spool_namespace_invalid"
    if not isinstance(current, dict) or current.get("schema") != AFTER_TURN_RUNTIME_SCHEMA:
        return "after_turn_spool_namespace_invalid"
    expected = technical_ref(str(memory_root.expanduser().resolve()))
    return ("ok" if current.get("memory_root_ref") == expected
            else "after_turn_spool_namespace_mismatch")


class RetriesExhausted(RuntimeError):
    """Every allowed attempt of one after-turn job failed."""


@dataclass(frozen=True)
class SouvenirReadSource:
    """Tool-opened paths attached to their completed turn, never copied facts."""

    assistant_l0: GroundZeroReference
    paths: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"assistant_l0": self.assistant_l0.as_dict(), "paths": list(self.paths)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SouvenirReadSource":
        paths = value["paths"]
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("invalid tool souvenir paths")
        reference = GroundZeroReference.from_dict(value["assistant_l0"])
        if reference.role != "assistant" or not reference.turn_id:
            raise ValueError("tool read requires a completed turn reference")
        return cls(reference, tuple(_unique_strings(paths)))


@dataclass(frozen=True)
class AfterTurnJob:
    turn_id: str
    host: str
    user_id: str
    source_turn_started_at: float
    scene_scope: str
    user_l0: GroundZeroReference
    assistant_l0: GroundZeroReference
    selected_souvenirs: tuple[str, ...] | None = None
    read_souvenirs: tuple[str, ...] = ()
    library_active: bool = False
    origin: dict[str, Any] | None = None
    created_at: str = ""
    attempts: int = 0
    available_at: float = 0.0
    last_error: str = ""
    lease_pid: int = 0
    tool_read_souvenirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.turn_id.strip():
            raise ValueError("after-turn job requires a turn_id")
        if not self.host.strip():
            raise ValueError("after-turn job requires a host")
        if not self.user_id.strip():
            raise ValueError("after-turn job requires a user_id")
        if self.user_l0.role != "user":
            raise ValueError("after-turn job user reference has wrong role")
        if self.assistant_l0.role != "assistant":
            raise ValueError("after-turn job assistant reference has wrong role")
        if (
            self.user_l0.turn_id
            and self.user_l0.turn_id != self.turn_id
        ):
            raise ValueError("after-turn user reference belongs to another turn")
        if (
            self.assistant_l0.turn_id
            and self.assistant_l0.turn_id != self.turn_id
        ):
            raise ValueError(
                "after-turn assistant reference belongs to another turn"
            )
        object.__setattr__(
            self,
            "host",
            _safe_component(self.host),
        )
        object.__setattr__(
            self,
            "selected_souvenirs",
            (
                tuple(_unique_strings(self.selected_souvenirs))
                if self.selected_souvenirs is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "read_souvenirs",
            tuple(_unique_strings(self.read_souvenirs)),
        )
        object.__setattr__(self, "tool_read_souvenirs", tuple(
            name for name in _unique_strings(self.tool_read_souvenirs)
            if name in self.read_souvenirs
        ))
        object.__setattr__(
            self,
            "origin",
            _json_object(self.origin or {}),
        )
        if not self.created_at:
            object.__setattr__(
                self,
                "created_at",
                datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": AFTER_TURN_SPOOL_SCHEMA,
            "turn_id": self.turn_id,
            "host": self.host,
            "user_id": self.user_id,
            "source_turn_started_at": self.source_turn_started_at,
            "scene_scope": self.scene_scope,
            "user_l0": self.user_l0.as_dict(),
            "assistant_l0": self.assistant_l0.as_dict(),
            "selected_capsules": (
                list(self.selected_souvenirs)
                if self.selected_souvenirs is not None
                else None
            ),
            "read_capsules": list(self.read_souvenirs),
            "tool_read_capsules": list(self.tool_read_souvenirs),
            "library_active": self.library_active,
            "origin": self.origin,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "available_at": self.available_at,
            "last_error": self.last_error,
            "lease_pid": self.lease_pid,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AfterTurnJob":
        if value.get("schema") != AFTER_TURN_SPOOL_SCHEMA:
            raise ValueError("unsupported after-turn job schema")
        selected = value.get("selected_capsules")
        return cls(
            turn_id=str(value.get("turn_id", "")),
            host=str(value.get("host", "")),
            user_id=str(value.get("user_id", "")),
            source_turn_started_at=float(
                value.get("source_turn_started_at", 0.0)
            ),
            scene_scope=str(value.get("scene_scope", "")),
            user_l0=GroundZeroReference.from_dict(
                value.get("user_l0", {})
            ),
            assistant_l0=GroundZeroReference.from_dict(
                value.get("assistant_l0", {})
            ),
            selected_souvenirs=(
                tuple(selected)
                if isinstance(selected, list)
                else None
            ),
            read_souvenirs=tuple(value.get("read_capsules") or ()),
            tool_read_souvenirs=tuple(value.get("tool_read_capsules") or ()),
            library_active=value.get("library_active") is True,
            origin=_json_object(value.get("origin") or {}),
            created_at=str(value.get("created_at", "")),
            attempts=max(0, int(value.get("attempts", 0))),
            available_at=max(
                0.0,
                float(value.get("available_at", 0.0)),
            ),
            last_error=str(value.get("last_error", ""))[:160],
            lease_pid=max(0, int(value.get("lease_pid", 0))),
        )


@dataclass(frozen=True)
class SceneProjectionJob:
    host: str
    user_id: str
    scene_scope: str
    source_trace_id: str
    source_turn_started_at: float
    covered_turn_ids: tuple[str, ...]
    read_souvenirs: tuple[str, ...] = ()
    library_active: bool = False
    dialogue_bytes: int = 0
    dialogue_turn_limit: int = 0
    created_at: str = ""
    attempts: int = 0
    available_at: float = 0.0
    last_error: str = ""
    lease_pid: int = 0
    souvenir_read_sources: tuple[SouvenirReadSource, ...] = ()

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("scene projection requires a host")
        if not self.user_id.strip():
            raise ValueError("scene projection requires a user_id")
        if not self.scene_scope.strip():
            raise ValueError("scene projection requires a scene_scope")
        if not self.source_trace_id.strip():
            raise ValueError("scene projection requires a source_trace_id")
        covered = tuple(_unique_strings(self.covered_turn_ids))
        if self.source_trace_id not in covered:
            covered = covered + (self.source_trace_id,)
        object.__setattr__(self, "host", _safe_component(self.host))
        object.__setattr__(self, "covered_turn_ids", covered)
        object.__setattr__(
            self,
            "read_souvenirs",
            tuple(_unique_strings(self.read_souvenirs)),
        )
        object.__setattr__(
            self,
            "dialogue_bytes",
            max(0, int(self.dialogue_bytes)),
        )
        object.__setattr__(
            self,
            "dialogue_turn_limit",
            max(0, int(self.dialogue_turn_limit)),
        )
        if not self.created_at:
            object.__setattr__(
                self,
                "created_at",
                datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
            )

    @property
    def projection_ref(self) -> str:
        return technical_ref(
            "\0".join((self.host, self.user_id, self.scene_scope))
        )

    @classmethod
    def from_turn(cls, job: AfterTurnJob) -> "SceneProjectionJob":
        return cls(
            host=job.host,
            user_id=job.user_id,
            scene_scope=job.scene_scope,
            source_trace_id=job.turn_id,
            source_turn_started_at=job.source_turn_started_at,
            covered_turn_ids=(job.turn_id,),
            read_souvenirs=job.read_souvenirs,
            library_active=job.library_active,
            dialogue_bytes=(
                job.user_l0.length + job.assistant_l0.length
            ),
            created_at=job.created_at,
            souvenir_read_sources=(
                (SouvenirReadSource(job.assistant_l0, job.tool_read_souvenirs),)
                if job.tool_read_souvenirs else ()
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCENE_PROJECTION_SCHEMA,
            "host": self.host,
            "user_id": self.user_id,
            "scene_scope": self.scene_scope,
            "source_trace_id": self.source_trace_id,
            "source_turn_started_at": self.source_turn_started_at,
            "covered_turn_ids": list(self.covered_turn_ids),
            "read_capsules": list(self.read_souvenirs),
            "capsule_read_sources": [source.as_dict() for source in self.souvenir_read_sources],
            "library_active": self.library_active,
            "dialogue_bytes": self.dialogue_bytes,
            "dialogue_turn_limit": self.dialogue_turn_limit,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "available_at": self.available_at,
            "last_error": self.last_error,
            "lease_pid": self.lease_pid,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SceneProjectionJob":
        if value.get("schema") not in _SUPPORTED_SCENE_PROJECTION_SCHEMAS:
            raise ValueError("unsupported scene projection schema")
        return cls(
            host=str(value.get("host", "")),
            user_id=str(value.get("user_id", "")),
            scene_scope=str(value.get("scene_scope", "")),
            source_trace_id=str(value.get("source_trace_id", "")),
            source_turn_started_at=float(
                value.get("source_turn_started_at", 0.0)
            ),
            covered_turn_ids=tuple(value.get("covered_turn_ids") or ()),
            read_souvenirs=tuple(value.get("read_capsules") or ()),
            souvenir_read_sources=tuple(
                SouvenirReadSource.from_dict(source)
                for source in value.get("capsule_read_sources", ())
            ),
            library_active=value.get("library_active") is True,
            dialogue_bytes=max(0, int(value.get("dialogue_bytes", 0))),
            dialogue_turn_limit=max(
                0,
                int(value.get("dialogue_turn_limit", 0)),
            ),
            created_at=str(value.get("created_at", "")),
            attempts=max(0, int(value.get("attempts", 0))),
            available_at=max(
                0.0,
                float(value.get("available_at", 0.0)),
            ),
            last_error=str(value.get("last_error", ""))[:160],
            lease_pid=max(0, int(value.get("lease_pid", 0))),
        )


@dataclass(frozen=True)
class EnqueueResult:
    created: bool
    state: str
    path: Path


@dataclass(frozen=True)
class DrainResult:
    completed: int = 0
    retried: int = 0
    failed: int = 0
    recovered: int = 0
    quarantined: int = 0
    executions: tuple[Any, ...] = ()


AfterTurnHandler = Callable[[AfterTurnJob], Any]
SceneProjectionHandler = Callable[[SceneProjectionJob], None]
AfterTurnBatchHandler = Callable[[tuple[Any, ...]], None]


def after_turn_spool_enabled() -> bool:
    configured = os.getenv(AFTER_TURN_SPOOL_ENV, "").strip().lower()
    return configured in _ENABLED_VALUES


def scene_coalescing_enabled() -> bool:
    configured = os.getenv(SCENE_COALESCING_ENV, "").strip().lower()
    return (
        configured in _ENABLED_VALUES
        and continuity_receipts_enabled()
    )


def after_turn_spool_root() -> Path:
    configured = os.getenv(AFTER_TURN_SPOOL_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / "exports" / "after_turn_spool"


class AfterTurnSpool:
    def __init__(
        self,
        root: Path | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_pending_scene_age_seconds: float = (
            MAX_COALESCED_SCENE_AGE_SECONDS
        ),
    ) -> None:
        self.root = (root or after_turn_spool_root()).expanduser()
        self.max_attempts = max(1, int(max_attempts))
        self.max_total_attempts = (
            self.max_attempts + len(TERMINAL_RETRY_DELAYS_SECONDS) + 1
        )
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_pending_scene_age_seconds = max(
            1.0,
            float(max_pending_scene_age_seconds),
        )
        self._runtime_namespace_ref = ""

    def read_recent_souvenir_sources(
        self, user_id: str, scene_scope: str, *, memory_root: Path,
        recent_turn_ids: tuple[str, ...], now: float,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Read pending tool provenance without mutating or waiting on the queue.

        Call before inspecting the scene: a projection publishes its scene before
        removing its job. The composer then excludes sources covered by that scene.
        An unavailable supplement never takes down the canonical voice.
        """
        if not recent_turn_ids or not math.isfinite(now):
            return ()
        try:
            marker = json.loads((self.root / "runtime.json").read_text(encoding="utf-8"))
            if (marker.get("schema") != AFTER_TURN_RUNTIME_SCHEMA or
                    marker.get("memory_root_ref") != technical_ref(str(memory_root.resolve()))):
                return ()
            sources = []
            documents = 0
            safe_user = safe_memory_user_id(user_id)
            # A turn is removed only after its replacement projection is durable.
            for state in (*_STATES, *_PROJECTION_STATES):
                for path in (self.root / state).glob(f"*/{safe_user}/*.json"):
                    documents += 1
                    if documents > MAX_RECENT_SOUVENIR_DOCUMENTS:
                        logger.warning("Recent souvenir handoff skipped: queue scan bound")
                        return ()
                    try:
                        path.resolve().relative_to(self.root.resolve())
                        with path.open("rb") as stream:
                            raw = stream.read(MAX_RECENT_SOUVENIR_DOCUMENT_BYTES + 1)
                        if len(raw) > MAX_RECENT_SOUVENIR_DOCUMENT_BYTES:
                            logger.warning("Recent souvenir handoff skipped: document bound")
                            return ()
                        value = json.loads(raw)
                        job = (SceneProjectionJob.from_dict(value) if state in _PROJECTION_STATES
                               else AfterTurnJob.from_dict(value))
                        if (job.user_id != user_id or job.scene_scope != scene_scope or
                                job.host != path.parent.parent.name):
                            continue
                        projection = job if isinstance(job, SceneProjectionJob) else SceneProjectionJob.from_turn(job)
                        sources.extend(
                            source for source in projection.souvenir_read_sources[:MAX_COALESCED_SCENE_TURNS]
                            if source.assistant_l0.turn_id in projection.covered_turn_ids
                            and set(source.paths).issubset(projection.read_souvenirs)
                        )
                    except (OSError, ValueError, TypeError, KeyError, AttributeError):
                        continue
            recent = set(recent_turn_ids)
            admitted = {}
            from daemon_core.voice_brief import dialogue_turn_id

            sources = tuple(dict.fromkeys(sources))
            if len(sources) > MAX_COALESCED_SCENE_TURNS:
                logger.warning("Recent souvenir handoff skipped: source scan bound")
                return ()
            for source in sources:
                try:
                    ref = source.assistant_l0
                    relative = Path(ref.path)
                    if (ref.role != "assistant" or not ref.turn_id or
                            relative.parent != Path("levels/L0_ground_zero/conversations") or
                            not relative.name.startswith(safe_user + "_") or
                            not relative.name.endswith(".md") or
                            ref.length > MAX_RECENT_SOUVENIR_DOCUMENT_BYTES):
                        continue
                    datetime.strptime(relative.name[len(safe_user) + 1:-3], "%Y-%m-%d")
                    record = read_ground_zero_record(ref, memory_root=memory_root)
                    stamp = datetime.fromisoformat(record.timestamp).timestamp()
                    if not 0 <= now - stamp <= MAX_RECENT_SOUVENIR_AGE_SECONDS:
                        continue
                    identity = dialogue_turn_id(record.timestamp, record.role, record.body)
                    if identity in recent:
                        admitted[identity] = tuple(_unique_strings(
                            admitted.get(identity, ()) + source.paths
                        ))
                except (OSError, UnicodeError, ValueError, TypeError):
                    continue
            # Most recent explicit consultations get the remaining hot-memory slots.
            return tuple((key, admitted[key]) for key in reversed(recent_turn_ids) if key in admitted)
        except (OSError, ValueError, TypeError, AttributeError):
            logger.info("Recent souvenir handoff unavailable; canonical sources retained")
            return ()

    def _handoff_path(self, job: AfterTurnJob) -> Path:
        namespace = technical_ref(str(MEMORY_ROOT.expanduser().resolve()))
        return self._job_path("handoff/" + namespace, job)

    def handoff(self, job: AfterTurnJob) -> EnqueueResult:
        """Persist ingress before queue validation; never use a volatile fallback.

        A moved vault cannot write another vault's queue. Its exact references
        stay in a separate namespace until an operator fixes the configuration.
        Crash recovery also covers a process dying between enqueue and return.
        """
        path = self._handoff_path(job)
        with memory_file_lock(self._lock_path("handoff", job.host, job.user_id)):
            created = not path.exists()
            if created:
                _write_job(path, job)
            else:
                original = _read_job(path)
                if original != job:
                    # Host delivery retries may have a different creation time;
                    # the first durable source reference always wins.
                    if (original.user_l0, original.assistant_l0) != (job.user_l0, job.assistant_l0):
                        raise ValueError("conflicting after-turn handoff")
                    job = original
            try:
                result = self.enqueue(job)
            except (OSError, RuntimeError):
                record_continuity_stage(job.turn_id, "outbox", "pending",
                                        reason_code="durable_handoff_deferred")
                return EnqueueResult(created=created, state="handoff", path=path)
            path.unlink(missing_ok=True)
            return result

    def recover_handoffs(self, user_id: str) -> None:
        namespace = technical_ref(str(MEMORY_ROOT.expanduser().resolve()))
        base = self.root / "handoff" / namespace
        with memory_file_lock(self._lock_path("handoff-recovery", "", user_id)):
            for path in sorted(base.glob(f"*/{safe_memory_user_id(user_id)}/*.json")):
                job = _read_job(path)
                if job.user_id != user_id or path != self._handoff_path(job):
                    raise ValueError("invalid after-turn handoff identity")
                self.handoff(job)

    def enqueue(self, job: AfterTurnJob) -> EnqueueResult:
        self._ensure_runtime_namespace()
        lock = self._lock_path("enqueue", job.host, job.user_id)
        with memory_file_lock(lock):
            existing = self._find(job)
            if existing is not None:
                return EnqueueResult(
                    created=False,
                    state=existing.parent.parent.parent.name,
                    path=existing,
                )
            path = self._job_path("pending", job)
            _write_job(path, job)
        record_continuity_stage(
            job.turn_id,
            "outbox",
            "pending",
            reason_code="durable_enqueued",
        )
        return EnqueueResult(created=True, state="pending", path=path)

    def enqueue_projection(
        self,
        job: SceneProjectionJob,
    ) -> EnqueueResult:
        self._ensure_runtime_namespace()
        lock = self._projection_lock_path(job)
        path = self._projection_path("scene_pending", job)
        created = True
        with memory_file_lock(lock):
            if path.exists():
                current = _read_projection_job(path)
                job = _merge_scene_projections(current, job)
                created = False
            _write_projection_job(path, job)
        record_continuity_stage(
            job.source_trace_id,
            "scene_schedule",
            "pending",
            reason_code="durable_projection_enqueued",
            projection_ref=job.projection_ref,
            covered_turn_count=len(job.covered_turn_ids),
        )
        return EnqueueResult(
            created=created,
            state="scene_pending",
            path=path,
        )

    def drain(
        self,
        host: str,
        user_id: str,
        handler: AfterTurnHandler,
        *,
        max_jobs: int | None = None,
        now: float | None = None,
    ) -> DrainResult:
        safe_host = _safe_component(host)
        self._ensure_runtime_namespace()
        self.recover_handoffs(user_id)
        current = time.time() if now is None else float(now)
        resolution = self.resolve_failed_head(
            user_id,
            now=current,
        )
        recovered = resolution.recovered + self.recover_stale(
            safe_host,
            user_id,
            now=current,
        )
        completed = failed = 0
        retried = resolution.retried
        quarantined = resolution.quarantined
        processed = 0
        executions: list[Any] = []
        pending_dir = self._state_dir(
            "pending",
            safe_host,
            user_id,
        )
        candidates = []
        for path in pending_dir.glob("*.json"):
            try:
                job = _read_job(path)
            except Exception:
                self._quarantine_invalid(path)
                failed += 1
                continue
            candidates.append((_job_order_key(job), path, job))
        candidates.sort(key=lambda item: (item[0], item[1].name))
        for _created_at, pending, job in candidates:
            if max_jobs is not None and processed >= max_jobs:
                break
            with memory_file_lock(
                self._lock_path("user", "", job.user_id)
            ):
                if not pending.exists():
                    continue
                oldest = self._oldest_unresolved_job(job.user_id)
                if oldest is None or oldest[1].resolve() != pending.resolve():
                    break
                if job.available_at > current:
                    break
                source_error = self._source_validation_error(job)
                if source_error is not None:
                    attempted = replace(
                        job,
                        attempts=job.attempts + 1,
                        available_at=(
                            current + _retry_delay(job.attempts + 1)
                        ),
                        last_error=source_error.__class__.__name__,
                        lease_pid=0,
                    )
                    _write_job(pending, attempted)
                    processed += 1
                    if attempted.attempts < self.max_attempts:
                        record_continuity_stage(
                            job.turn_id,
                            "outbox",
                            "pending",
                            reason_code="retry_scheduled",
                            error_type=source_error.__class__.__name__,
                        )
                        retried += 1
                        break
                    self._quarantine_job(
                        pending,
                        attempted,
                        source_state="pending",
                        error=source_error,
                        now=current,
                    )
                    quarantined += 1
                    continue
                running = self._job_path("running", job)
                running.parent.mkdir(parents=True, exist_ok=True)
                claimed = replace(
                    job,
                    attempts=job.attempts + 1,
                    available_at=0.0,
                    last_error="",
                    lease_pid=os.getpid(),
                )
                try:
                    os.replace(pending, running)
                except FileNotFoundError:
                    continue
                _write_job(running, claimed)
                processed += 1
                record_continuity_stage(
                    job.turn_id,
                    "outbox",
                    "pending",
                    reason_code="worker_claimed",
                )
                if _receipt_cycle_completed(job.turn_id):
                    running.unlink(missing_ok=True)
                    record_continuity_stage(
                        job.turn_id,
                        "outbox",
                        "recovered",
                        reason_code="receipt_already_completed",
                    )
                    recovered += 1
                    continue
                try:
                    execution = handler(claimed)
                    projection = getattr(
                        execution,
                        "scene_projection",
                        None,
                    )
                    if isinstance(projection, SceneProjectionJob):
                        self.enqueue_projection(projection)
                except Exception as exc:
                    failed_job = replace(
                        claimed,
                        available_at=(
                            current + self._failure_delay(claimed.attempts)
                        ),
                        last_error=exc.__class__.__name__,
                        lease_pid=0,
                    )
                    destination_state = (
                        "failed"
                        if claimed.attempts >= self.max_attempts
                        else "pending"
                    )
                    destination = self._job_path(
                        destination_state,
                        failed_job,
                    )
                    _write_job(running, failed_job)
                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    os.replace(running, destination)
                    record_continuity_stage(
                        job.turn_id,
                        "outbox",
                        (
                            "failed"
                            if destination_state == "failed"
                            else "pending"
                        ),
                        reason_code=(
                            "attempts_exhausted"
                            if destination_state == "failed"
                            else "retry_scheduled"
                        ),
                        error_type=exc.__class__.__name__,
                    )
                    if destination_state == "failed":
                        failed += 1
                    else:
                        retried += 1
                    # MemoryAgent is an ordered journal. A later turn must not
                    # overtake a source turn that still needs attention.
                    break
                else:
                    executions.append(execution)
                    running.unlink(missing_ok=True)
                    record_continuity_stage(
                        job.turn_id,
                        "outbox",
                        "completed",
                        reason_code=(
                            "projection_delegated"
                            if isinstance(
                                getattr(
                                    execution,
                                    "scene_projection",
                                    None,
                                ),
                                SceneProjectionJob,
                            )
                            else "worker_completed"
                        ),
                    )
                    completed += 1
        return DrainResult(
            completed=completed,
            retried=retried,
            failed=failed,
            recovered=recovered,
            quarantined=quarantined,
            executions=tuple(executions),
        )

    def resolve_failed_head(
        self,
        user_id: str,
        *,
        now: float | None = None,
    ) -> DrainResult:
        """Resolve an unreadable FIFO head without silently losing its job.

        Any live worker may retry and quarantine an unreadable pending head,
        including one left by an inactive host.  A failed job whose exact L0
        source is no longer readable can never succeed and is quarantined
        immediately because its normal retries are already exhausted.  A
        failed job with intact sources is a transient execution failure
        instead: requeue it only after its backoff has elapsed, and set it
        aside once its allowed attempts are spent.
        """

        current = time.time() if now is None else float(now)
        self._ensure_runtime_namespace()
        recovered = retried = quarantined = 0
        while True:
            with memory_file_lock(
                self._lock_path("user", "", user_id)
            ):
                oldest = self._oldest_unresolved_job(user_id)
                if oldest is None:
                    break
                _order, path, job = oldest
                state = self._state_from_path(path)
                if state not in ("pending", "failed"):
                    break
                source_error = self._source_validation_error(job)
                if state == "pending":
                    if source_error is None or job.available_at > current:
                        break
                    attempted = replace(
                        job,
                        attempts=job.attempts + 1,
                        available_at=(
                            current + _retry_delay(job.attempts + 1)
                        ),
                        last_error=source_error.__class__.__name__,
                        lease_pid=0,
                    )
                    _write_job(path, attempted)
                    if attempted.attempts < self.max_attempts:
                        record_continuity_stage(
                            job.turn_id,
                            "outbox",
                            "pending",
                            reason_code="retry_scheduled",
                            error_type=source_error.__class__.__name__,
                        )
                        retried += 1
                        break
                    self._quarantine_job(
                        path,
                        attempted,
                        source_state="pending",
                        error=source_error,
                        now=current,
                    )
                    quarantined += 1
                    continue
                if source_error is not None:
                    self._quarantine_job(
                        path,
                        job,
                        source_state="failed",
                        error=source_error,
                        now=current,
                    )
                    quarantined += 1
                    continue
                if job.attempts >= self.max_total_attempts:
                    self._quarantine_job(
                        path,
                        job,
                        source_state="failed",
                        error=RetriesExhausted(job.last_error or "unknown"),
                        now=current,
                        reason_code="retries_exhausted",
                    )
                    quarantined += 1
                    continue
                if job.available_at > current:
                    break
                destination = self._job_path("pending", job)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, destination)
                record_continuity_stage(
                    job.turn_id,
                    "outbox",
                    "recovered",
                    reason_code="terminal_retry_requeued",
                    error_type=job.last_error,
                )
                recovered += 1
                break
        return DrainResult(
            recovered=recovered,
            retried=retried,
            quarantined=quarantined,
        )

    def drain_projections(
        self,
        host: str,
        user_id: str,
        handler: SceneProjectionHandler,
        *,
        max_jobs: int | None = 1,
        now: float | None = None,
    ) -> DrainResult:
        safe_host = _safe_component(host)
        self._ensure_runtime_namespace()
        current = time.time() if now is None else float(now)
        recovered = self.recover_stale_projections(
            safe_host,
            user_id,
            now=current,
        )
        recovered += self.recover_failed_projections(
            safe_host,
            user_id,
        )
        completed = retried = failed = 0
        processed = 0
        pending_dir = self._projection_state_dir(
            "scene_pending",
            safe_host,
            user_id,
        )
        candidates = []
        for path in pending_dir.glob("*.json"):
            try:
                job = _read_projection_job(path)
            except Exception:
                self._quarantine_invalid_projection(path)
                failed += 1
                continue
            if job.available_at <= current:
                candidates.append((job.created_at, path, job))
        candidates.sort(key=lambda item: (item[0], item[1].name))

        for _created_at, pending, job in candidates:
            if max_jobs is not None and processed >= max_jobs:
                break
            scene_lock = self._lock_path("scene-user", "", job.user_id)
            with memory_file_try_lock(scene_lock) as scene_acquired:
                if not scene_acquired:
                    break
                with memory_file_lock(
                    self._lock_path("user", "", job.user_id)
                ):
                    if not pending.exists():
                        continue
                    # A scene is replaceable; the exact factual journal is not.
                    # Never start the long projection while any host still has
                    # an unresolved factual turn for this user.
                    if self._oldest_unresolved_job(job.user_id) is not None:
                        break
                    running = self._projection_path("scene_running", job)
                    running.parent.mkdir(parents=True, exist_ok=True)
                    claimed = replace(
                        job,
                        attempts=job.attempts + 1,
                        available_at=0.0,
                        last_error="",
                        lease_pid=os.getpid(),
                    )
                    try:
                        os.replace(pending, running)
                    except FileNotFoundError:
                        continue
                    _write_projection_job(running, claimed)
                    processed += 1
                for turn_id in claimed.covered_turn_ids:
                    record_continuity_stage(
                        turn_id,
                        "scene_schedule",
                        "pending",
                        reason_code="projection_claimed",
                        projection_ref=claimed.projection_ref,
                        covered_turn_count=len(
                            claimed.covered_turn_ids
                        ),
                    )
                if _projection_cycles_completed(claimed):
                    running.unlink(missing_ok=True)
                    recovered += 1
                    continue
                try:
                    handler(claimed)
                    finalized = [
                        finalize_deferred_scene(
                            turn_id,
                            status="completed",
                            reason_code=(
                                "coalesced_projection_completed"
                            ),
                            projection_ref=claimed.projection_ref,
                            covered_turn_count=len(
                                claimed.covered_turn_ids
                            ),
                        )
                        for turn_id in claimed.covered_turn_ids
                    ]
                    if not all(finalized):
                        raise RuntimeError(
                            "projection receipt finalization incomplete"
                        )
                except Exception as exc:
                    recovery_limit = (
                        _next_projection_recovery_limit(claimed)
                        if claimed.attempts >= self.max_attempts
                        else 0
                    )
                    failed_job = replace(
                        claimed,
                        attempts=(0 if recovery_limit else claimed.attempts),
                        available_at=(
                            current + _retry_delay(claimed.attempts)
                        ),
                        last_error=exc.__class__.__name__,
                        lease_pid=0,
                        dialogue_turn_limit=(
                            recovery_limit
                            or claimed.dialogue_turn_limit
                        ),
                    )
                    destination_state = (
                        "scene_failed"
                        if (
                            claimed.attempts >= self.max_attempts
                            and not recovery_limit
                        )
                        else "scene_pending"
                    )
                    destination = self._projection_path(
                        destination_state,
                        failed_job,
                    )
                    _write_projection_job(running, failed_job)
                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    os.replace(running, destination)
                    if destination_state == "scene_failed":
                        for turn_id in claimed.covered_turn_ids:
                            finalize_deferred_scene(
                                turn_id,
                                status="failed",
                                reason_code=(
                                    "projection_attempts_exhausted"
                                ),
                                projection_ref=claimed.projection_ref,
                                covered_turn_count=len(
                                    claimed.covered_turn_ids
                                ),
                            )
                        failed += 1
                    else:
                        reason_code = (
                            "projection_split_recovery_scheduled"
                            if recovery_limit
                            else "projection_retry_scheduled"
                        )
                        for turn_id in claimed.covered_turn_ids:
                            record_continuity_stage(
                                turn_id,
                                "scene_schedule",
                                "pending",
                                reason_code=reason_code,
                                error_type=exc.__class__.__name__,
                                projection_ref=claimed.projection_ref,
                                covered_turn_count=len(
                                    claimed.covered_turn_ids
                                ),
                                dialogue_turn_limit=(
                                    failed_job.dialogue_turn_limit
                                ),
                            )
                        retried += 1
                else:
                    running.unlink(missing_ok=True)
                    completed += 1
        return DrainResult(
            completed=completed,
            retried=retried,
            failed=failed,
            recovered=recovered,
        )

    def recover_stale(
        self,
        host: str,
        user_id: str,
        *,
        now: float | None = None,
    ) -> int:
        self._ensure_runtime_namespace()
        current = time.time() if now is None else float(now)
        running_dir = self._state_dir("running", host, user_id)
        recovered = 0
        for path in running_dir.glob("*.json"):
            try:
                age = current - path.stat().st_mtime
            except FileNotFoundError:
                continue
            try:
                job = _read_job(path)
            except Exception:
                self._quarantine_invalid(path)
                continue
            if (
                age < self.lease_seconds
                and job.lease_pid > 0
                and _pid_alive(job.lease_pid)
            ):
                continue
            with memory_file_lock(
                self._lock_path("user", "", job.user_id)
            ):
                if not path.exists():
                    continue
                if _receipt_cycle_completed(job.turn_id):
                    path.unlink(missing_ok=True)
                    record_continuity_stage(
                        job.turn_id,
                        "outbox",
                        "recovered",
                        reason_code="stale_receipt_completed",
                    )
                elif handoff_reason := self._recover_scene_handoff(job):
                    path.unlink(missing_ok=True)
                    record_continuity_stage(
                        job.turn_id,
                        "outbox",
                        "recovered",
                        reason_code=handoff_reason,
                    )
                else:
                    # An interrupted attempt already called the model. It
                    # counts like a failed one, so a process that dies on
                    # the same turn at every restart cannot loop forever.
                    exhausted = job.attempts >= self.max_attempts
                    recovered_job = replace(
                        job,
                        lease_pid=0,
                        available_at=current + self._failure_delay(job.attempts),
                        last_error=(
                            WORKER_INTERRUPTED if exhausted else job.last_error
                        ),
                    )
                    destination = self._job_path(
                        "failed" if exhausted else "pending",
                        recovered_job,
                    )
                    _write_job(path, recovered_job)
                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    os.replace(path, destination)
                    record_continuity_stage(
                        job.turn_id,
                        "outbox",
                        "failed" if exhausted else "recovered",
                        reason_code=(
                            "attempts_exhausted"
                            if exhausted
                            else "stale_lease_requeued"
                        ),
                        **({"error_type": WORKER_INTERRUPTED} if exhausted else {}),
                    )
                recovered += 1
        return recovered

    def _recover_scene_handoff(
        self,
        job: AfterTurnJob,
    ) -> str:
        projection = SceneProjectionJob.from_turn(job)
        for state in ("scene_pending", "scene_running"):
            path = self._projection_path(state, projection)
            if not path.exists():
                continue
            try:
                queued = _read_projection_job(path)
            except Exception:
                self._quarantine_invalid_projection(path)
                continue
            if job.turn_id in queued.covered_turn_ids:
                return "projection_handoff_recovered"
        if not _receipt_ready_for_deferred_projection(job.turn_id):
            return ""
        self.enqueue_projection(projection)
        return "projection_handoff_rebuilt"

    def recover_stale_projections(
        self,
        host: str,
        user_id: str,
        *,
        now: float | None = None,
    ) -> int:
        self._ensure_runtime_namespace()
        current = time.time() if now is None else float(now)
        running_dir = self._projection_state_dir(
            "scene_running",
            host,
            user_id,
        )
        recovered = 0
        for path in running_dir.glob("*.json"):
            try:
                age = current - path.stat().st_mtime
            except FileNotFoundError:
                continue
            try:
                job = _read_projection_job(path)
            except Exception:
                self._quarantine_invalid_projection(path)
                continue
            if (
                age < self.lease_seconds
                and job.lease_pid > 0
                and _pid_alive(job.lease_pid)
            ):
                continue
            with memory_file_lock(
                self._lock_path("user", "", job.user_id)
            ):
                if not path.exists():
                    continue
                if _projection_cycles_completed(job):
                    path.unlink(missing_ok=True)
                else:
                    recovered_job = replace(job, lease_pid=0)
                    destination = self._projection_path(
                        "scene_pending",
                        recovered_job,
                    )
                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    if destination.exists():
                        recovered_job = _merge_scene_projections(
                            recovered_job,
                            _read_projection_job(destination),
                        )
                    _write_projection_job(destination, recovered_job)
                    path.unlink(missing_ok=True)
                    for turn_id in recovered_job.covered_turn_ids:
                        record_continuity_stage(
                            turn_id,
                            "scene_schedule",
                            "pending",
                            reason_code="projection_stale_requeued",
                            projection_ref=(
                                recovered_job.projection_ref
                            ),
                            covered_turn_count=len(
                                recovered_job.covered_turn_ids
                            ),
                        )
                recovered += 1
        return recovered

    def recover_failed_projections(
        self,
        host: str,
        user_id: str,
    ) -> int:
        """Requeue legacy or exhausted multi-turn projections in smaller batches."""

        self._ensure_runtime_namespace()
        failed_dir = self._projection_state_dir(
            "scene_failed",
            host,
            user_id,
        )
        recovered = 0
        for path in failed_dir.glob("*.json"):
            try:
                job = _read_projection_job(path)
            except Exception:
                self._quarantine_invalid_projection(path)
                continue
            recovery_limit = _next_projection_recovery_limit(job)
            if not recovery_limit:
                if self._projection_is_superseded(job):
                    self._quarantine_projection_job(
                        path,
                        job,
                        reason_code="projection_superseded_by_current_scene",
                        now=time.time(),
                    )
                    recovered += 1
                continue
            with memory_file_lock(
                self._lock_path("user", "", job.user_id)
            ):
                if not path.exists():
                    continue
                pending_job = replace(
                    job,
                    attempts=0,
                    available_at=0.0,
                    last_error="",
                    lease_pid=0,
                    dialogue_turn_limit=recovery_limit,
                )
                destination = self._projection_path(
                    "scene_pending",
                    pending_job,
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    pending_job = _merge_scene_projections(
                        _read_projection_job(destination),
                        pending_job,
                    )
                _write_projection_job(destination, pending_job)
                path.unlink(missing_ok=True)
                for turn_id in pending_job.covered_turn_ids:
                    record_continuity_stage(
                        turn_id,
                        "scene_schedule",
                        "pending",
                        reason_code="projection_split_recovery_scheduled",
                        projection_ref=pending_job.projection_ref,
                        covered_turn_count=len(
                            pending_job.covered_turn_ids
                        ),
                        dialogue_turn_limit=recovery_limit,
                    )
                recovered += 1
        return recovered

    def user_ids(self, host: str) -> tuple[str, ...]:
        """Discover users with resumable jobs without trusting path names."""

        users: set[str] = set()
        for state in _STATES:
            host_dir = self.root / state / _safe_component(host)
            for path in host_dir.glob("*/*.json"):
                try:
                    users.add(_read_job(path).user_id)
                except Exception:
                    self._quarantine_invalid(path)
        for state in ("scene_pending", "scene_running", "scene_failed"):
            host_dir = self.root / state / _safe_component(host)
            for path in host_dir.glob("*/*.json"):
                try:
                    users.add(_read_projection_job(path).user_id)
                except Exception:
                    self._quarantine_invalid_projection(path)
        return tuple(sorted(users))

    def has_ready_turns(
        self,
        host: str,
        user_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        pending_dir = self._state_dir("pending", host, user_id)
        for path in pending_dir.glob("*.json"):
            try:
                if _read_job(path).available_at <= current:
                    return True
            except Exception:
                self._quarantine_invalid(path)
        return False

    def projection_should_flush(
        self,
        host: str,
        user_id: str,
        *,
        include_age: bool = True,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        pending_dir = self._projection_state_dir(
            "scene_pending",
            host,
            user_id,
        )
        for path in pending_dir.glob("*.json"):
            try:
                projection = _read_projection_job(path)
            except Exception:
                self._quarantine_invalid_projection(path)
                continue
            if (
                len(projection.covered_turn_ids)
                >= MAX_COALESCED_SCENE_TURNS
                or projection.dialogue_bytes
                >= MAX_COALESCED_SCENE_BYTES
                or (
                    include_age
                    and _projection_age_seconds(projection, current)
                    >= self.max_pending_scene_age_seconds
                )
            ):
                return True
        return False

    def counts(
        self,
        host: str,
        user_id: str,
    ) -> dict[str, int]:
        return {
            state: len(
                list(
                    self._state_dir(
                        state,
                        host,
                        user_id,
                    ).glob("*.json")
                )
            )
            for state in _STATES
        }

    def projection_counts(
        self,
        host: str,
        user_id: str,
    ) -> dict[str, int]:
        return {
            state: len(
                list(
                    self._projection_state_dir(
                        state,
                        host,
                        user_id,
                    ).glob("*.json")
                )
            )
            for state in _PROJECTION_STATES
        }

    def _state_dir(
        self,
        state: str,
        host: str,
        user_id: str,
    ) -> Path:
        return (
            self.root
            / state
            / _safe_component(host)
            / safe_memory_user_id(user_id)
        )

    def _job_path(self, state: str, job: AfterTurnJob) -> Path:
        return (
            self._state_dir(state, job.host, job.user_id)
            / f"{_safe_component(job.turn_id)}.json"
        )

    def _projection_state_dir(
        self,
        state: str,
        host: str,
        user_id: str,
    ) -> Path:
        return (
            self.root
            / state
            / _safe_component(host)
            / safe_memory_user_id(user_id)
        )

    def _projection_path(
        self,
        state: str,
        job: SceneProjectionJob,
    ) -> Path:
        return (
            self._projection_state_dir(
                state,
                job.host,
                job.user_id,
            )
            / f"{job.projection_ref}.json"
        )

    def _projection_lock_path(
        self,
        job: SceneProjectionJob,
    ) -> Path:
        return (
            self.root
            / "locks"
            / (
                "scene-"
                + job.host
                + "-"
                + safe_memory_user_id(job.user_id)
                + "-"
                + job.projection_ref
                + ".lock"
            )
        )

    def _find(self, job: AfterTurnJob) -> Path | None:
        for state in _JOB_STATES:
            candidate = self._job_path(state, job)
            if candidate.exists():
                return candidate
        return None

    def _oldest_unresolved_job(
        self,
        user_id: str,
    ) -> tuple[tuple[float, str, str], Path, AfterTurnJob] | None:
        safe_user = safe_memory_user_id(user_id)
        unresolved = []
        for state in _STATES:
            state_root = self.root / state
            if not state_root.exists():
                continue
            for path in state_root.glob(f"*/{safe_user}/*.json"):
                try:
                    job = _read_job(path)
                except Exception:
                    continue
                unresolved.append((_job_order_key(job), path, job))
        return min(
            unresolved,
            key=lambda item: (item[0], str(item[1])),
            default=None,
        )

    def _source_validation_error(
        self,
        job: AfterTurnJob,
    ) -> Exception | None:
        for reference in (job.user_l0, job.assistant_l0):
            try:
                read_ground_zero_reference(reference)
            except (FileNotFoundError, UnicodeError, ValueError) as exc:
                return exc
        return None

    def _quarantine_job(
        self,
        path: Path,
        job: AfterTurnJob,
        *,
        source_state: str,
        error: Exception,
        now: float,
        reason_code: str = "l0_reference_unrecoverable",
    ) -> Path:
        """Move one job aside, kept with an exact receipt.

        Either its sources are provably unreadable, or every allowed attempt
        failed. In both cases the turns queued behind it proceed.
        """

        if not path.exists():
            return self._job_path("quarantined", job)
        destination = self._job_path("quarantined", job)
        if destination.exists():
            raise RuntimeError("after-turn quarantine destination exists")
        raw = path.read_bytes()
        receipt_path = (
            self.root
            / "quarantine_receipts"
            / _safe_component(job.host)
            / safe_memory_user_id(job.user_id)
            / f"{_safe_component(job.turn_id)}.json"
        )
        resolution = {
            "schema": AFTER_TURN_QUARANTINE_SCHEMA,
            "status": "pending",
            "turn_id": job.turn_id,
            "host": job.host,
            "user_id": job.user_id,
            "source_state": source_state,
            "reason_code": reason_code,
            "error_type": error.__class__.__name__,
            "error": str(error)[:160],
            "job_sha256": hashlib.sha256(raw).hexdigest(),
            "quarantined_at": datetime.fromtimestamp(
                now,
                timezone.utc,
            ).isoformat(timespec="milliseconds"),
        }
        atomic_write_text(
            receipt_path,
            json.dumps(
                resolution,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)
        resolution["status"] = "completed"
        atomic_write_text(
            receipt_path,
            json.dumps(
                resolution,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        record_continuity_stage(
            job.turn_id,
            "outbox",
            "skipped",
            reason_code=reason_code,
            error_type=error.__class__.__name__,
        )
        logger.warning(
            "After-turn job set aside | host=%s | source=%s | reason=%s | error=%s",
            job.host,
            job.turn_id,
            reason_code,
            error.__class__.__name__,
        )
        return destination

    def _failure_delay(self, attempts: int) -> float:
        """Delay before the next attempt of a turn that just failed."""
        if attempts <= self.max_attempts:
            return _retry_delay(attempts)
        index = min(
            attempts - self.max_attempts - 1,
            len(TERMINAL_RETRY_DELAYS_SECONDS) - 1,
        )
        return TERMINAL_RETRY_DELAYS_SECONDS[index]

    def _state_from_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).parts[0]
        except (ValueError, IndexError):
            return ""

    def _projection_is_superseded(self, job: SceneProjectionJob) -> bool:
        try:
            from daemon_core.scene_model import read_scene_model

            current = read_scene_model(job.user_id, job.scene_scope)
        except Exception:
            return False
        return bool(
            current is not None
            and current.source_turn_started_at
            >= job.source_turn_started_at
        )

    def _quarantine_projection_job(
        self,
        path: Path,
        job: SceneProjectionJob,
        *,
        reason_code: str,
        now: float,
    ) -> Path:
        """Archive exact batches, retaining the source until closure is durable.

        Active work coalesces by scope; immutable archives must not. The digest
        also makes retries after a crash reuse the same bytes and receipt.
        """

        destination = self._projection_path("scene_quarantined", job)
        with memory_file_lock(self._lock_path("user", "", job.user_id)):
            if not path.exists():
                return destination
            raw = path.read_bytes()
            if SceneProjectionJob.from_dict(json.loads(raw)) != job:
                raise RuntimeError("scene projection changed before quarantine")
            digest = hashlib.sha256(raw).hexdigest()
            destination = destination.with_name(f"{job.projection_ref}-{digest}.json")
            receipt_path = (
                self.root
                / "projection_quarantine_receipts"
                / _safe_component(job.host)
                / safe_memory_user_id(job.user_id)
                / destination.name
            )
            if destination.exists() and destination.read_bytes() != raw:
                raise RuntimeError("scene projection quarantine integrity mismatch")
            resolution = {
                "schema": SCENE_PROJECTION_QUARANTINE_SCHEMA,
                "status": "pending",
                "host": job.host,
                "user_id": job.user_id,
                "projection_ref": job.projection_ref,
                "source_trace_id": job.source_trace_id,
                "source_turn_started_at": job.source_turn_started_at,
                "covered_turn_count": len(job.covered_turn_ids),
                "reason_code": reason_code,
                "error_type": job.last_error,
                "job_sha256": digest,
                "quarantined_at": datetime.fromtimestamp(
                    now, timezone.utc,
                ).isoformat(timespec="milliseconds"),
            }
            if receipt_path.exists():
                prior = json.loads(receipt_path.read_text(encoding="utf-8"))
                if any(prior.get(key) != resolution[key] for key in (
                    "schema", "host", "user_id", "projection_ref", "source_trace_id",
                    "source_turn_started_at", "covered_turn_count", "reason_code",
                    "error_type", "job_sha256",
                )) or prior.get("status") not in {"pending", "completed"}:
                    raise RuntimeError("scene projection quarantine receipt mismatch")
                resolution = prior
            else:
                atomic_write_text(receipt_path, json.dumps(resolution, sort_keys=True) + "\n")
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".projection-", dir=destination.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            # The source stays discoverable after a crash at any earlier step.
            for turn_id in job.covered_turn_ids:
                if not finalize_deferred_scene(
                    turn_id, status="completed", reason_code=reason_code,
                    projection_ref=job.projection_ref,
                    covered_turn_count=len(job.covered_turn_ids),
                ):
                    raise RuntimeError("projection quarantine finalization incomplete")
            if resolution["status"] != "completed":
                resolution["status"] = "completed"
                atomic_write_text(receipt_path, json.dumps(resolution, sort_keys=True) + "\n")
            path.unlink()
        logger.warning(
            "Scene projection quarantined | host=%s | projection=%s | reason=%s",
            job.host,
            job.projection_ref,
            reason_code,
        )
        return destination

    def _ensure_runtime_namespace(self) -> None:
        marker = self.root / "runtime.json"
        expected = technical_ref(str(MEMORY_ROOT.expanduser().resolve()))
        with memory_file_lock(self.root / "locks" / "runtime.lock"):
            if marker.exists():
                try:
                    current = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as exc:
                    raise RuntimeError(
                        "after-turn spool runtime marker invalid"
                    ) from exc
                if (not isinstance(current, dict) or
                    current.get("schema") != AFTER_TURN_RUNTIME_SCHEMA
                    or current.get("memory_root_ref") != expected
                ):
                    raise RuntimeError(
                        "after-turn spool memory namespace mismatch"
                    )
                self._runtime_namespace_ref = expected
                return
            if any(next((self.root / state).glob("*/*/*.json"), None)
                   for state in _JOB_STATES + _PROJECTION_STATES):
                raise RuntimeError("after-turn spool marker missing from nonempty queue")
            atomic_write_text(
                marker,
                json.dumps(
                    {
                        "schema": AFTER_TURN_RUNTIME_SCHEMA,
                        "memory_root_ref": expected,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )
            self._runtime_namespace_ref = expected

    def _lock_path(
        self,
        purpose: str,
        host: str,
        user_id: str,
    ) -> Path:
        parts = [purpose]
        if host:
            parts.append(_safe_component(host))
        if user_id:
            parts.append(safe_memory_user_id(user_id))
        return self.root / "locks" / ("-".join(parts) + ".lock")

    def _quarantine_invalid(self, path: Path) -> None:
        destination = self.root / "failed" / "invalid" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            return

    def _quarantine_invalid_projection(self, path: Path) -> None:
        destination = (
            self.root
            / "scene_failed"
            / "invalid"
            / path.name
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            return


class AfterTurnWorker:
    def __init__(
        self,
        *,
        spool: AfterTurnSpool,
        host: str,
        user_id: str,
        handler: AfterTurnHandler,
        projection_handler: SceneProjectionHandler | None = None,
        batch_handler: AfterTurnBatchHandler | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.spool = spool
        self.host = _safe_component(host)
        self.user_id = user_id
        self.handler = handler
        self.projection_handler = projection_handler
        self.batch_handler = batch_handler
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_error = ""
        self._consecutive_errors = 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"daemon-after-turn-{self.host}-{safe_memory_user_id(user_id)}",
            daemon=True,
        )

    def start(self) -> "AfterTurnWorker":
        self._thread.start()
        return self

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            batch_executions: list[Any] = []

            def _flush_batch() -> None:
                if self.batch_handler is None:
                    batch_executions.clear()
                    return
                try:
                    self.batch_handler(tuple(batch_executions))
                except Exception:
                    logger.exception(
                        "After-turn batch handler failed | host=%s | user=%s",
                        self.host,
                        self.user_id,
                    )
                finally:
                    batch_executions.clear()

            try:
                if self.projection_handler is None:
                    result = self.spool.drain(
                        self.host,
                        self.user_id,
                        self.handler,
                    )
                    batch_executions.extend(result.executions)
                else:
                    age_flush_used = False
                    while not self._stop.is_set():
                        result = self.spool.drain(
                            self.host,
                            self.user_id,
                            self.handler,
                            max_jobs=1,
                        )
                        batch_executions.extend(result.executions)
                        ready = self.spool.has_ready_turns(
                            self.host,
                            self.user_id,
                        )
                        should_flush = bool(
                            ready
                            and self.spool.projection_should_flush(
                                self.host,
                                self.user_id,
                                include_age=not age_flush_used,
                            )
                        )
                        if not ready or should_flush:
                            # Latest-state consumers are cheaper and fresher
                            # when refreshed once from the drained FIFO burst,
                            # before the slower scene projection starts.
                            _flush_batch()
                            projection_result = self.spool.drain_projections(
                                self.host,
                                self.user_id,
                                self.projection_handler,
                            )
                            if (
                                should_flush
                                and projection_result is not None
                                and (
                                    projection_result.completed
                                    or projection_result.recovered
                                )
                            ):
                                age_flush_used = True
                        if not ready:
                            break
                        if not any(
                            (
                                result.completed,
                                result.retried,
                                result.failed,
                                result.recovered,
                                result.quarantined,
                            )
                        ):
                            break
            except Exception as exc:
                code = exc.__class__.__name__
                self._consecutive_errors += 1
                if code != self.last_error or self._consecutive_errors % 60 == 1:
                    logger.error("After-turn spool worker unavailable | host=%s | error=%s",
                                 self.host, code)
                self.last_error = code
            else:
                self.last_error = ""
                self._consecutive_errors = 0
            if batch_executions or self.projection_handler is None:
                _flush_batch()
            self._wake.wait(timeout=max(self.poll_seconds, min(300, self._consecutive_errors * 2)))
            self._wake.clear()


def ensure_after_turn_worker(
    *,
    host: str,
    user_id: str,
    handler: AfterTurnHandler,
    projection_handler: SceneProjectionHandler | None = None,
    batch_handler: AfterTurnBatchHandler | None = None,
    spool: AfterTurnSpool | None = None,
) -> AfterTurnWorker:
    active_spool = spool or AfterTurnSpool()
    key = (
        str(active_spool.root.resolve()),
        _safe_component(host),
        safe_memory_user_id(user_id),
    )
    with _WORKERS_LOCK:
        existing = _WORKERS.get(key)
        if existing is not None and existing.alive:
            existing.handler = handler
            existing.projection_handler = projection_handler
            existing.batch_handler = batch_handler
            return existing
        worker = AfterTurnWorker(
            spool=active_spool,
            host=host,
            user_id=user_id,
            handler=handler,
            projection_handler=projection_handler,
            batch_handler=batch_handler,
        ).start()
        _WORKERS[key] = worker
        return worker


def stop_after_turn_workers() -> None:
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.stop()


def _write_job(path: Path, job: AfterTurnJob) -> None:
    atomic_write_text(
        path,
        json.dumps(
            job.as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    path.chmod(0o600)


def _read_job(path: Path) -> AfterTurnJob:
    return AfterTurnJob.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _job_order_key(job: AfterTurnJob) -> tuple[float, str, str]:
    return (
        max(0.0, float(job.source_turn_started_at)),
        job.created_at,
        job.turn_id,
    )


def _projection_age_seconds(
    job: SceneProjectionJob,
    now: float,
) -> float:
    value = job.created_at.strip()
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_at = created.timestamp()
    except (TypeError, ValueError, OverflowError):
        created_at = max(0.0, float(job.source_turn_started_at))
    return max(0.0, float(now) - created_at)


def _write_projection_job(
    path: Path,
    job: SceneProjectionJob,
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            job.as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    path.chmod(0o600)


def _read_projection_job(path: Path) -> SceneProjectionJob:
    return SceneProjectionJob.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _merge_scene_projections(
    current: SceneProjectionJob,
    incoming: SceneProjectionJob,
) -> SceneProjectionJob:
    if (
        current.host,
        current.user_id,
        current.scene_scope,
    ) != (
        incoming.host,
        incoming.user_id,
        incoming.scene_scope,
    ):
        raise ValueError("cannot merge projections from different scenes")
    latest = (
        incoming
        if incoming.source_turn_started_at
        >= current.source_turn_started_at
        else current
    )
    return replace(
        latest,
        covered_turn_ids=tuple(
            _unique_strings(
                current.covered_turn_ids + incoming.covered_turn_ids
            )
        ),
        read_souvenirs=tuple(
            _unique_strings(
                current.read_souvenirs + incoming.read_souvenirs
            )
        ),
        souvenir_read_sources=tuple(dict.fromkeys(
            current.souvenir_read_sources + incoming.souvenir_read_sources
        ))[-MAX_COALESCED_SCENE_TURNS:],
        library_active=(current.library_active or incoming.library_active),
        dialogue_bytes=(
            current.dialogue_bytes + incoming.dialogue_bytes
        ),
        dialogue_turn_limit=(
            current.dialogue_turn_limit
            or incoming.dialogue_turn_limit
        ),
        created_at=min(current.created_at, incoming.created_at),
        attempts=current.attempts,
        available_at=0.0,
        last_error="",
        lease_pid=0,
    )


def _receipt_cycle_completed(turn_id: str) -> bool:
    receipt = read_continuity_receipt(turn_id)
    if not isinstance(receipt, dict):
        return False
    stages = receipt.get("stages", {})
    if not isinstance(stages, dict):
        return False
    if any(
        isinstance(stage, dict) and stage.get("status") == "failed"
        for name, stage in stages.items()
        if name not in {"outbox", "turn"}
    ):
        return False
    cycle = stages.get("cycle", {})
    return (
        isinstance(cycle, dict)
        and cycle.get("status") in {"completed", "recovered"}
    )


def _receipt_ready_for_deferred_projection(turn_id: str) -> bool:
    receipt = read_continuity_receipt(turn_id)
    if not isinstance(receipt, dict):
        return False
    stages = receipt.get("stages", {})
    if not isinstance(stages, dict):
        return False
    for name in ("l0", "working_state", "memory", "followup"):
        stage = stages.get(name, {})
        if not isinstance(stage, dict):
            return False
        if stage.get("status") not in {"completed", "skipped", "recovered"}:
            return False
    memory = stages.get("memory", {})
    scene = stages.get("scene_schedule", {})
    return (
        isinstance(memory, dict)
        and memory.get("status") in {"completed", "recovered"}
        and isinstance(scene, dict)
        and scene.get("status") == "pending"
        and scene.get("reason_code")
        in {
            "durable_projection_deferred",
            "durable_projection_enqueued",
            "projection_claimed",
            "projection_retry_scheduled",
            "projection_stale_requeued",
        }
    )


def _projection_cycles_completed(job: SceneProjectionJob) -> bool:
    return all(
        _receipt_cycle_completed(turn_id)
        for turn_id in job.covered_turn_ids
    )


def _retry_delay(attempt: int) -> float:
    # The exponent is capped so a large attempt count can never overflow.
    return min(300.0, 5.0 * (2 ** min(max(0, attempt - 1), 6)))


def _next_projection_recovery_limit(job: SceneProjectionJob) -> int:
    """Return a strictly smaller batch, or zero once one turn still fails."""

    covered = len(job.covered_turn_ids)
    if covered <= 1:
        return 0
    current = int(job.dialogue_turn_limit)
    if current <= 0:
        return min(16, max(1, covered // 2))
    if current <= 1:
        return 0
    return max(1, current // 2)


def _safe_component(value: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in str(value)
    ).strip("_")
    if not safe:
        raise ValueError("empty spool path component")
    return safe[:160]


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    encoded = json.dumps(value, ensure_ascii=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        return {}
    return decoded


__all__ = [
    "AFTER_TURN_SPOOL_DIR_ENV",
    "AFTER_TURN_SPOOL_ENV",
    "AFTER_TURN_QUARANTINE_SCHEMA",
    "AFTER_TURN_RUNTIME_SCHEMA",
    "AFTER_TURN_SPOOL_SCHEMA",
    "MAX_COALESCED_SCENE_AGE_SECONDS",
    "MAX_COALESCED_SCENE_BYTES",
    "MAX_COALESCED_SCENE_TURNS",
    "SCENE_COALESCING_ENV",
    "SCENE_PROJECTION_QUARANTINE_SCHEMA",
    "SCENE_PROJECTION_SCHEMA",
    "AfterTurnJob",
    "AfterTurnSpool",
    "AfterTurnWorker",
    "DrainResult",
    "EnqueueResult",
    "SceneProjectionJob",
    "after_turn_spool_enabled",
    "after_turn_spool_root",
    "ensure_after_turn_worker",
    "scene_coalescing_enabled",
    "stop_after_turn_workers",
]
