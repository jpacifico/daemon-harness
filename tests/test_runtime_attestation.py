from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from daemon_core.runtime_attestation import build_runtime_attestation
from daemon_core.continuity import technical_ref


REVISION = "a" * 40
USER_ID = "test-user"


def _ready_env(tmp_path: Path) -> dict[str, str]:
    memory_root = tmp_path / "memory"
    user_root = memory_root / "users" / USER_ID
    user_root.mkdir(parents=True)
    (user_root / "persona.md").write_text("# Persona\n", encoding="utf-8")
    (user_root / "conversation.md").write_text(
        "# Conversation\n",
        encoding="utf-8",
    )
    return {
        "DAEMON_CORE_MEMORY_ROOT": str(memory_root),
        "DAEMON_CORE_RUNTIME_REVISION": REVISION,
        "DAEMON_CORE_SRC": str(Path(__file__).resolve().parents[1] / "src"),
        "DAEMON_CORE_USER_ID": USER_ID,
    }


def _configure_openclaw_guard(
    tmp_path: Path,
    env: dict[str, str],
    *,
    health_status: str = "healthy",
) -> tuple[Path, Path]:
    database_path = tmp_path / "openclaw-agent.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE context_engine_turn_outbox (
              advancement_key TEXT PRIMARY KEY,
              engine_id TEXT NOT NULL,
              owner_plugin_id TEXT,
              session_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              attempt_count INTEGER NOT NULL,
              last_attempt_at INTEGER,
              last_error TEXT,
              created_at INTEGER NOT NULL
            )
            """
        )
    health_path = tmp_path / "openclaw-runtime-health.json"
    health_path.write_text(
        json.dumps(
            {
                "schema": "daemon.openclaw-runtime-health.v1",
                "status": health_status,
                "updated_at_ms": int(time.time() * 1000),
                "sessions": {},
            }
        ),
        encoding="utf-8",
    )
    env.update(
        {
            "DAEMON_CORE_OPENCLAW_AGENT_DB": str(database_path),
            "DAEMON_CORE_OPENCLAW_HEALTH_FILE": str(health_path),
        }
    )
    return database_path, health_path


def test_runtime_attestation_is_ready_and_hides_paths(tmp_path: Path):
    env = _ready_env(tmp_path)

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "ready"
    assert report["promotable"] is True
    assert report["runtime_revision"] == REVISION
    assert report["failures"] == []
    rendered = str(report)
    assert str(tmp_path) not in rendered
    assert str(Path(__file__).resolve().parents[1]) not in rendered
    assert all(len(value) == 16 for value in report["refs"].values())


def test_runtime_attestation_rejects_unknown_revision(tmp_path: Path):
    env = _ready_env(tmp_path)
    env["DAEMON_CORE_RUNTIME_REVISION"] = "unknown"

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert report["runtime_revision"] == "unknown"
    assert "runtime_revision_unknown" in report["failures"]


def test_runtime_attestation_accepts_a_healthy_openclaw_context_path(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    _configure_openclaw_guard(tmp_path, env)

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "ready"
    assert report["checks"]["openclaw_turn_outbox"] == {
        "ok": True,
        "code": "ok",
    }
    assert report["checks"]["openclaw_canonical_context"] == {
        "ok": True,
        "code": "ok",
    }


def test_runtime_attestation_rejects_a_stalled_openclaw_turn(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    database_path, _ = _configure_openclaw_guard(tmp_path, env)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO context_engine_turn_outbox VALUES (
              ?, 'daemon-harness', 'daemon-harness', ?, ?, 3, ?, ?, ?
            )
            """,
            (
                "stalled-turn",
                "session",
                json.dumps({"state": "ready"}),
                int(time.time() * 1000),
                "accepted turn incomplete",
                int(time.time() * 1000),
            ),
        )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert report["checks"]["openclaw_turn_outbox"] == {
        "ok": False,
        "code": "openclaw_turn_outbox_stalled",
    }


def test_runtime_attestation_rejects_a_degraded_openclaw_prompt_path(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    _configure_openclaw_guard(tmp_path, env, health_status="degraded")

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert report["checks"]["openclaw_canonical_context"] == {
        "ok": False,
        "code": "openclaw_canonical_context_degraded",
    }


def test_runtime_attestation_rejects_missing_memory_root(tmp_path: Path):
    env = {
        "DAEMON_CORE_MEMORY_ROOT": str(tmp_path / "missing"),
        "DAEMON_CORE_RUNTIME_REVISION": REVISION,
        "DAEMON_CORE_SRC": str(Path(__file__).resolve().parents[1] / "src"),
    }

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "memory_root_unavailable" in report["failures"]


def test_runtime_attestation_rejects_implicit_worktree_memory(tmp_path: Path):
    env = _ready_env(tmp_path)
    env.pop("DAEMON_CORE_MEMORY_ROOT")
    report = build_runtime_attestation(environ=env, project_root=tmp_path)

    assert report["status"] == "ready"
    assert report["promotable"] is False
    assert report["failures"] == []
    assert "memory_root_defaulted" in report["warnings"]
    assert "memory_root_inside_project" in report["warnings"]
    assert report["promotion_blockers"] == [
        "memory_root_defaulted",
        "memory_root_inside_project",
    ]


def test_runtime_attestation_rejects_missing_personal_profile(tmp_path: Path):
    env = _ready_env(tmp_path)
    env["DAEMON_CORE_USER_ID"] = "missing-user"

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "personal_profile_unavailable" in report["failures"]


def test_runtime_attestation_rejects_a_different_existing_source_root(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    different_source = tmp_path / "old-worktree" / "src"
    (different_source / "daemon_core").mkdir(parents=True)
    env["DAEMON_CORE_SRC"] = str(different_source)

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "source_root_mismatch" in report["failures"]


def test_runtime_attestation_rejects_an_empty_source_package(tmp_path: Path):
    env = _ready_env(tmp_path)
    source = tmp_path / "empty-runtime" / "src"
    (source / "daemon_core").mkdir(parents=True)
    env["DAEMON_CORE_SRC"] = str(source)

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "source_root_unavailable" in report["failures"]


def test_runtime_attestation_rejects_a_whitespace_only_profile(tmp_path: Path):
    env = _ready_env(tmp_path)
    profile = tmp_path / "memory" / "users" / USER_ID / "conversation.md"
    profile.write_text(" \n\t\n", encoding="utf-8")

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "personal_profile_unavailable" in report["failures"]


def test_runtime_attestation_accepts_the_versioned_persona_fallback(tmp_path: Path):
    env = _ready_env(tmp_path)
    persona = tmp_path / "memory" / "users" / USER_ID / "persona.md"
    persona.unlink()

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "ready"
    assert report["promotable"] is True
    assert "persona_defaulted" in report["warnings"]
    assert report["promotion_blockers"] == []


def test_runtime_attestation_requires_explicit_durable_storage(tmp_path: Path):
    env = _ready_env(tmp_path)
    env.update(
        {
            "DAEMON_CORE_AFTER_TURN_SPOOL": "canary",
            "DAEMON_CORE_CONTINUITY_RECEIPTS": "shadow",
            "DAEMON_CORE_SCENE_COALESCING": "canary",
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "after_turn_spool_dir_not_explicit" in report["failures"]
    assert "continuity_receipt_dir_not_explicit" in report["failures"]
    assert report["checks"]["scene_coalescing_prerequisites"]["ok"] is True


def test_runtime_attestation_exposes_stalled_after_turn_recovery(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    spool_root = tmp_path / "spool"
    failed = spool_root / "failed" / "openclaw" / USER_ID / "turn.json"
    failed.parent.mkdir(parents=True)
    failed.write_text(
        json.dumps({"available_at": 1.0}),
        encoding="utf-8",
    )
    env.update(
        {
            "DAEMON_CORE_AFTER_TURN_SPOOL": "canary",
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(spool_root),
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert report["checks"]["after_turn_auto_recovery"] == {
        "ok": False,
        "code": "after_turn_auto_recovery_stalled",
    }
    assert "after_turn_auto_recovery_stalled" in report["failures"]
    assert str(spool_root) not in str(report)


def test_runtime_attestation_exposes_stalled_scene_recovery(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    spool_root = tmp_path / "spool"
    failed = (
        spool_root
        / "scene_failed"
        / "hermes"
        / USER_ID
        / "scene.json"
    )
    failed.parent.mkdir(parents=True)
    failed.write_text(
        json.dumps({"available_at": 1.0}),
        encoding="utf-8",
    )
    env.update(
        {
            "DAEMON_CORE_AFTER_TURN_SPOOL": "canary",
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(spool_root),
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert report["checks"]["after_turn_auto_recovery"]["code"] == (
        "after_turn_auto_recovery_stalled"
    )


def test_runtime_attestation_requires_the_external_owner_heartbeat(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    spool_root = tmp_path / "spool"
    env.update(
        {
            "DAEMON_CORE_AFTER_TURN_SPOOL": "canary",
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(spool_root),
            "DAEMON_CORE_AFTER_TURN_OWNER": "external",
            "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION": REVISION,
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "after_turn_spool_namespace_missing" in report["failures"]


def test_runtime_attestation_accepts_matching_external_owner(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    memory_root = Path(env["DAEMON_CORE_MEMORY_ROOT"])
    spool_root = tmp_path / "spool"
    spool_root.mkdir()
    heartbeat = spool_root / "service.json"
    (spool_root / "runtime.json").write_text(json.dumps({
        "schema": "daemon.after-turn-spool-runtime.v1",
        "memory_root_ref": technical_ref(str(memory_root.resolve())),
    }))
    heartbeat.write_text(
        json.dumps(
            {
                "schema": "daemon.after-turn-service.v1",
                "status": "running",
                "heartbeat_at": time.time(),
                "runtime_revision": REVISION,
                "memory_root_ref": technical_ref(str(memory_root.resolve())),
                "hosts": ["openclaw", "hermes"],
                "worker_count": 2,
            }
        ),
        encoding="utf-8",
    )
    heartbeat.chmod(0o600)
    env.update(
        {
            "DAEMON_CORE_AFTER_TURN_SPOOL": "canary",
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(spool_root),
            "DAEMON_CORE_AFTER_TURN_OWNER": "external",
            "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION": REVISION,
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "ready"
    assert report["checks"]["after_turn_service"] == {
        "ok": True,
        "code": "ok",
    }
    assert report["features"]["after_turn_owner"]["mode"] == "external"


def test_runtime_attestation_rejects_broad_env_permissions(tmp_path: Path):
    env = _ready_env(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    env_file.chmod(0o644)
    env["DAEMON_CORE_ENV_FILE"] = str(env_file)

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "env_file_permissions_too_open" in report["failures"]
    assert "secret" not in str(report)


def test_runtime_attestation_does_not_echo_invalid_modes(tmp_path: Path):
    env = _ready_env(tmp_path)
    env["DAEMON_CORE_CONTINUITY_RECEIPTS"] = "accidental-secret"
    env["DAEMON_CORE_RELATION_GARDENING"] = "another-secret"

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "continuity_receipts_mode_invalid" in report["failures"]
    assert "relation_gardening_mode_invalid" in report["failures"]
    assert report["features"]["continuity_receipts"]["mode"] == "invalid"
    assert report["features"]["relation_gardening"]["mode"] == "invalid"
    assert "accidental-secret" not in str(report)
    assert "another-secret" not in str(report)


@pytest.mark.parametrize(
    ("variable", "failure_code"),
    [
        ("DAEMON_CORE_AFTER_TURN_SPOOL", "after_turn_spool_mode_invalid"),
        ("DAEMON_CORE_SCENE_COALESCING", "scene_coalescing_mode_invalid"),
    ],
)
def test_runtime_attestation_rejects_other_invalid_modes(
    tmp_path: Path,
    variable: str,
    failure_code: str,
):
    env = _ready_env(tmp_path)
    env[variable] = "typo"

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert failure_code in report["failures"]


@pytest.mark.parametrize("relation_mode", ["dry-run", "active", "canary"])
@pytest.mark.parametrize("identity_scope", ["", "*", "one,two"])
def test_runtime_attestation_rejects_unsafe_relation_scope(
    tmp_path: Path,
    relation_mode: str,
    identity_scope: str,
):
    env = _ready_env(tmp_path)
    env.update(
        {
            "DAEMON_CORE_RELATION_GARDENING": relation_mode,
            "DAEMON_CORE_RELATION_GARDENING_USER_IDS": identity_scope,
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "relation_gardening_identity_scope_invalid" in report["failures"]


def test_runtime_attestation_rejects_a_different_active_relation_identity(
    tmp_path: Path,
):
    env = _ready_env(tmp_path)
    env.update(
        {
            "DAEMON_CORE_RELATION_GARDENING": "active",
            "DAEMON_CORE_RELATION_GARDENING_USER_IDS": "another-user",
        }
    )

    report = build_runtime_attestation(environ=env)

    assert report["status"] == "degraded"
    assert "relation_gardening_identity_mismatch" in report["failures"]
