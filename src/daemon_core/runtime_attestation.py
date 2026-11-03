"""Privacy-safe deployment readiness for the Daemon Harness runtime."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from daemon_core.continuity import runtime_revision, technical_ref
from daemon_core.paths import PROJECT_ROOT


READINESS_SCHEMA = "daemon.runtime-readiness.v1"
_REVISION_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)
_CONTINUITY_MODES = frozenset({"1", "on", "shadow", "true"})
_SPOOL_MODES = frozenset({"1", "canary", "on", "true"})
_AFTER_TURN_OWNER_MODES = frozenset({"external"})
_AFTER_TURN_AUTO_RECOVERY_GRACE_SECONDS = 60.0
_OPENCLAW_TURN_OUTBOX_GRACE_SECONDS = 60.0
_PROMOTION_BLOCKING_WARNINGS = frozenset(
    {"memory_root_defaulted", "memory_root_inside_project"}
)


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _directory_access(path: Path, *, must_exist: bool) -> bool:
    try:
        if path.exists():
            return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
        if must_exist:
            return False
        parent = path.parent
        while parent != parent.parent and not parent.exists():
            parent = parent.parent
        return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except OSError:
        return False


def _configured_values(raw: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _safe_user_id(user_id: str) -> str:
    return "".join(
        character if character.isalnum() or character in ("-", "_") else "_"
        for character in user_id
    )


def _readable_nonempty_file(path: Path) -> bool:
    try:
        if not path.is_file() or not os.access(path, os.R_OK):
            return False
        with path.open("r", encoding="utf-8") as stream:
            return bool(stream.read(4096).strip())
    except (OSError, UnicodeError):
        return False


def _is_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _after_turn_processing_failed(spool_root: Path, user_id: str) -> bool:
    """Known retrying failures are degraded even while their worker is alive."""
    for state in ("pending", "running", "failed", "scene_pending", "scene_running", "scene_failed"):
        for path in (spool_root / state).glob(f"*/{_safe_user_id(user_id)}/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("last_error") or int(payload.get("attempts", 0)) > 0:
                    return True
            except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                return True
    return False


def _after_turn_auto_recovery_stalled(
    spool_root: Path,
    user_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Detect an exhausted head that the background worker failed to resume."""

    current = time.time() if now is None else float(now)
    safe_user = _safe_user_id(user_id)
    failed_paths = (
        path
        for state in ("failed", "scene_failed")
        for path in (spool_root / state).glob(
            f"*/{safe_user}/*.json"
        )
    )
    for path in failed_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            available_at = max(
                0.0,
                float(payload.get("available_at", 0.0)),
            )
            retry_due = available_at or path.stat().st_mtime
        except (OSError, UnicodeError, ValueError, TypeError):
            try:
                retry_due = path.stat().st_mtime
            except OSError:
                continue
        if current - retry_due > _AFTER_TURN_AUTO_RECOVERY_GRACE_SECONDS:
            return True
    return False


def _openclaw_turn_outbox_health(
    database_path: Path,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Detect a durable OpenClaw turn that would force every next turn to legacy."""

    try:
        if not database_path.is_file() or not os.access(database_path, os.R_OK):
            return False, "openclaw_turn_outbox_unavailable"
        connection = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            rows = connection.execute(
                """
                SELECT attempt_count, last_error, created_at, payload_json
                FROM context_engine_turn_outbox
                WHERE engine_id = 'daemon-harness'
                """
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False, "openclaw_turn_outbox_unavailable"

    current_ms = (time.time() if now is None else float(now)) * 1000.0
    for attempt_count, last_error, created_at, payload_json in rows:
        try:
            state = str(json.loads(payload_json).get("state", ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False, "openclaw_turn_outbox_invalid"
        if state == "blocked":
            return False, "openclaw_turn_outbox_blocked"
        if int(attempt_count or 0) > 0 or str(last_error or "").strip():
            return False, "openclaw_turn_outbox_stalled"
        try:
            age_ms = current_ms - float(created_at)
        except (TypeError, ValueError):
            return False, "openclaw_turn_outbox_invalid"
        if age_ms > _OPENCLAW_TURN_OUTBOX_GRACE_SECONDS * 1000.0:
            return False, "openclaw_turn_outbox_stalled"
    return True, "ok"


def _openclaw_runtime_health(health_path: Path) -> tuple[bool, str]:
    """Read the content-free status emitted by the live OpenClaw plugin."""

    try:
        payload = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False, "openclaw_runtime_health_unavailable"
    if payload.get("schema") != "daemon.openclaw-runtime-health.v1":
        return False, "openclaw_runtime_health_invalid"
    if payload.get("status") != "healthy":
        return False, "openclaw_canonical_context_degraded"
    return True, "ok"


def _public_mode(value: str, allowed: frozenset[str]) -> str:
    if not value or value == "off":
        return "off"
    return value if value in allowed else "invalid"


def build_runtime_attestation(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Describe readiness without returning paths, identities, keys or content."""

    env = os.environ if environ is None else environ
    root = _path(project_root or PROJECT_ROOT)
    source_root = _path(env.get("DAEMON_CORE_SRC", "") or root / "src")
    loaded_source_root = _path(Path(__file__).resolve().parents[1])
    memory_configured = env.get("DAEMON_CORE_MEMORY_ROOT", "").strip()
    memory_root = _path(memory_configured or root / "memory")
    user_id = env.get("DAEMON_CORE_USER_ID", "").strip()
    user_root = memory_root / "users" / _safe_user_id(user_id)
    personal_persona = user_root / "persona.md"
    shared_persona = memory_root / "core" / "persona.md"
    conversation_manner = user_root / "conversation.md"
    explicit_env = env.get("DAEMON_CORE_ENV_FILE", "").strip()
    env_file = _path(explicit_env or root / ".env")
    openclaw_database_configured = env.get(
        "DAEMON_CORE_OPENCLAW_AGENT_DB",
        "",
    ).strip()
    openclaw_database = _path(openclaw_database_configured) if (
        openclaw_database_configured
    ) else None
    openclaw_health_configured = env.get(
        "DAEMON_CORE_OPENCLAW_HEALTH_FILE",
        "",
    ).strip()
    openclaw_health = _path(openclaw_health_configured) if (
        openclaw_health_configured
    ) else None

    continuity_mode = (
        env.get("DAEMON_CORE_CONTINUITY_RECEIPTS", "").strip().lower()
    )
    receipt_configured = env.get("DAEMON_CORE_CONTINUITY_RECEIPT_DIR", "").strip()
    receipts_enabled = bool(receipt_configured) or continuity_mode in _CONTINUITY_MODES
    receipt_root = _path(
        receipt_configured or source_root.parent / "exports" / "continuity_receipts"
    )

    spool_mode = env.get("DAEMON_CORE_AFTER_TURN_SPOOL", "").strip().lower()
    spool_configured = env.get("DAEMON_CORE_AFTER_TURN_SPOOL_DIR", "").strip()
    spool_enabled = spool_mode in _SPOOL_MODES
    spool_root = _path(spool_configured or root / "exports" / "after_turn_spool")
    after_turn_owner = env.get(
        "DAEMON_CORE_AFTER_TURN_OWNER",
        "",
    ).strip().lower()
    after_turn_service_revision = env.get(
        "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION",
        "",
    ).strip().lower()

    coalescing_mode = (
        env.get("DAEMON_CORE_SCENE_COALESCING", "").strip().lower()
    )
    coalescing_requested = coalescing_mode in _SPOOL_MODES
    relation_mode = env.get("DAEMON_CORE_RELATION_GARDENING", "off").strip().lower()
    relation_users = _configured_values(
        env.get("DAEMON_CORE_RELATION_GARDENING_USER_IDS", "")
    )

    checks: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    warnings: list[str] = []

    def check(name: str, ok: bool, failure_code: str) -> None:
        checks[name] = {"ok": ok, "code": "ok" if ok else failure_code}
        if not ok:
            failures.append(failure_code)

    def check_mode(name: str, value: str, allowed: frozenset[str]) -> None:
        check(
            f"{name}_mode",
            not value or value == "off" or value in allowed,
            f"{name}_mode_invalid",
        )

    check(
        "source_root",
        source_root.is_dir()
        and _readable_nonempty_file(source_root / "daemon_core" / "__init__.py")
        and _readable_nonempty_file(source_root / "daemon_core" / "loop.py")
        and os.access(source_root, os.R_OK | os.X_OK),
        "source_root_unavailable",
    )
    check(
        "source_root_loaded",
        source_root == loaded_source_root,
        "source_root_mismatch",
    )
    check(
        "memory_root",
        _directory_access(memory_root, must_exist=True),
        "memory_root_unavailable",
    )
    if not memory_configured:
        warnings.append("memory_root_defaulted")
    if _is_below(memory_root, root):
        warnings.append("memory_root_inside_project")
    check(
        "personal_user",
        bool(user_id),
        "personal_user_not_configured",
    )
    check(
        "personal_profile",
        bool(user_id)
        and _readable_nonempty_file(conversation_manner),
        "personal_profile_unavailable",
    )
    if user_id and not (
        _readable_nonempty_file(personal_persona)
        or _readable_nonempty_file(shared_persona)
    ):
        warnings.append("persona_defaulted")

    env_ok = True
    env_code = "ok"
    try:
        if explicit_env and not env_file.is_file():
            env_ok = False
            env_code = "env_file_unavailable"
        elif env_file.exists() and (
            not env_file.is_file() or not os.access(env_file, os.R_OK)
        ):
            env_ok = False
            env_code = "env_file_unreadable"
        elif env_file.is_file() and os.name == "posix":
            permissions = stat.S_IMODE(env_file.stat().st_mode)
            if permissions & 0o077:
                env_ok = False
                env_code = "env_file_permissions_too_open"
    except OSError:
        env_ok = False
        env_code = "env_file_unavailable"
    check("environment", env_ok, env_code)

    if openclaw_database is not None:
        outbox_ok, outbox_code = _openclaw_turn_outbox_health(openclaw_database)
        check("openclaw_turn_outbox", outbox_ok, outbox_code)
    if openclaw_health is not None:
        host_health_ok, host_health_code = _openclaw_runtime_health(openclaw_health)
        check("openclaw_canonical_context", host_health_ok, host_health_code)

    if receipts_enabled:
        check(
            "continuity_receipts_configured",
            bool(receipt_configured),
            "continuity_receipt_dir_not_explicit",
        )
        check(
            "continuity_receipts_storage",
            _directory_access(receipt_root, must_exist=False),
            "continuity_receipt_dir_unavailable",
        )
    if spool_enabled:
        from daemon_core.after_turn_spool import spool_namespace_status

        namespace = spool_namespace_status(spool_root, memory_root)
        check("after_turn_spool_namespace", namespace == "ok", namespace)
        check(
            "after_turn_spool_configured",
            bool(spool_configured),
            "after_turn_spool_dir_not_explicit",
        )
        check(
            "after_turn_spool_storage",
            _directory_access(spool_root, must_exist=False),
            "after_turn_spool_dir_unavailable",
        )
        if user_id:
            check("after_turn_processing", not _after_turn_processing_failed(spool_root, user_id),
                  "after_turn_work_pending_retry")
            check(
                "after_turn_auto_recovery",
                not _after_turn_auto_recovery_stalled(
                    spool_root,
                    user_id,
                ),
                "after_turn_auto_recovery_stalled",
            )
    if env.get("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "").strip().lower() in _SPOOL_MODES:
        from daemon_core.semantic_atlas import (
            DEFAULT_BASE_URL, DEFAULT_MODEL, OllamaEmbeddingProvider,
        )

        try:
            # Diagnostics only: no personal content, generation, or voice-path
            # request. A fresh client must not reuse an old successful probe.
            OllamaEmbeddingProvider(
                model=env.get("DAEMON_CORE_ATLAS_EMBEDDING_MODEL", DEFAULT_MODEL),
                base_url=env.get("DAEMON_CORE_ATLAS_EMBEDDING_URL", DEFAULT_BASE_URL),
                timeout_s=0.5,
            ).verify_model()
            embedding_ok = True
        except Exception:
            embedding_ok = False
        check("semantic_atlas_dependency", embedding_ok, "semantic_atlas_dependency_unavailable")

    if coalescing_requested:
        check(
            "scene_coalescing_prerequisites",
            spool_enabled and receipts_enabled,
            "scene_coalescing_prerequisites_missing",
        )

    check_mode(
        "after_turn_owner",
        after_turn_owner,
        _AFTER_TURN_OWNER_MODES,
    )
    if after_turn_owner == "external":
        check(
            "after_turn_owner_prerequisites",
            spool_enabled,
            "after_turn_external_owner_requires_spool",
        )
        if spool_enabled:
            from daemon_core.after_turn_service import (
                read_after_turn_service_status,
            )

            service = read_after_turn_service_status(
                spool_root=spool_root,
                memory_root=memory_root,
                expected_revision=after_turn_service_revision,
            )
            check(
                "after_turn_service",
                service["ok"],
                service["code"],
            )
        check(
            "after_turn_service_revision_configured",
            bool(_REVISION_RE.fullmatch(after_turn_service_revision)),
            "after_turn_service_revision_not_configured",
        )

    check_mode("continuity_receipts", continuity_mode, _CONTINUITY_MODES)
    check_mode("after_turn_spool", spool_mode, _SPOOL_MODES)
    check_mode("scene_coalescing", coalescing_mode, _SPOOL_MODES)
    check_mode(
        "relation_gardening",
        relation_mode,
        frozenset({"dry-run", "active", "canary"}),
    )
    if relation_mode in {"dry-run", "active", "canary"}:
        check(
            "relation_gardening_identity_scope",
            len(relation_users) == 1 and "*" not in relation_users,
            "relation_gardening_identity_scope_invalid",
        )
        check(
            "relation_gardening_identity_match",
            bool(user_id) and relation_users == (user_id,),
            "relation_gardening_identity_mismatch",
        )

    revision_value = runtime_revision(environ=env, project_root=root)
    revision_known = bool(_REVISION_RE.fullmatch(revision_value))
    check("runtime_revision", revision_known, "runtime_revision_unknown")
    public_revision = revision_value.lower() if revision_known else "unknown"

    refs = {
        "project_root": technical_ref(str(root)),
        "source_root": technical_ref(str(source_root)),
        "memory_root": technical_ref(str(memory_root)),
        "personal_profile": technical_ref(str(user_root)),
        "environment": technical_ref(str(env_file)),
    }
    if receipts_enabled:
        refs["continuity_receipts"] = technical_ref(str(receipt_root))
    if spool_enabled:
        refs["after_turn_spool"] = technical_ref(str(spool_root))
    if openclaw_database is not None:
        refs["openclaw_turn_outbox"] = technical_ref(str(openclaw_database))
    if openclaw_health is not None:
        refs["openclaw_runtime_health"] = technical_ref(str(openclaw_health))

    promotion_blockers = sorted(
        warning for warning in set(warnings) if warning in _PROMOTION_BLOCKING_WARNINGS
    )

    return {
        "schema": READINESS_SCHEMA,
        "status": "ready" if not failures else "degraded",
        "promotable": not failures and not promotion_blockers,
        "runtime_revision": public_revision,
        "checks": checks,
        "failures": sorted(set(failures)),
        "warnings": sorted(set(warnings)),
        "promotion_blockers": promotion_blockers,
        "features": {
            "continuity_receipts": {
                "enabled": receipts_enabled,
                "mode": _public_mode(continuity_mode, _CONTINUITY_MODES),
            },
            "after_turn_spool": {
                "enabled": spool_enabled,
                "mode": _public_mode(spool_mode, _SPOOL_MODES),
            },
            "after_turn_owner": {
                "mode": _public_mode(
                    after_turn_owner,
                    _AFTER_TURN_OWNER_MODES,
                ),
            },
            "scene_coalescing": {
                "enabled": coalescing_requested and spool_enabled and receipts_enabled,
                "mode": _public_mode(coalescing_mode, _SPOOL_MODES),
            },
            "relation_gardening": {
                "mode": _public_mode(
                    relation_mode,
                    frozenset({"dry-run", "active", "canary"}),
                ),
                "configured_identity_count": len(relation_users),
                "wildcard_configured": "*" in relation_users,
            },
        },
        "refs": refs,
    }


__all__ = ["READINESS_SCHEMA", "build_runtime_attestation"]
