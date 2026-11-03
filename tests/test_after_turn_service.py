from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from daemon_core import after_turn_service as service_module


REVISION = "a" * 40


class Spool:
    def __init__(self, root: Path):
        self.root = root


def test_neutral_service_owns_both_hosts_and_shared_batch_effects(
    tmp_path,
    monkeypatch,
):
    observed = []
    prewarmed = []
    observer_calls = []

    class Worker:
        alive = True

    def ensure_worker(**kwargs):
        observed.append(kwargs)
        return Worker()

    monkeypatch.setattr(service_module, "ensure_after_turn_worker", ensure_worker)
    monkeypatch.setattr(service_module, "scene_coalescing_enabled", lambda: False)
    monkeypatch.setattr(
        service_module,
        "execute_after_turn_projection_batch",
        lambda executions, **_kwargs: "memory-result",
    )
    monkeypatch.setattr(
        service_module,
        "coalesced_semantic_atlas_prewarmer",
        lambda *_args: SimpleNamespace(
            process_batch=lambda executions: prewarmed.append(executions)
        ),
    )
    harness = SimpleNamespace(memory_provider=object())
    service = service_module.AfterTurnService(
        spool=Spool(tmp_path / "spool"),
        user_ids=("personal",),
        batch_observers={
            "hermes": lambda result, executions: observer_calls.append(
                (result, executions)
            )
        },
        harness_factory=lambda: harness,
    )

    assert service.ensure_workers() == 2
    assert {(item["host"], item["user_id"]) for item in observed} == {
        ("openclaw", "personal"),
        ("hermes", "personal"),
    }
    assert all(item["spool"] is service.spool for item in observed)

    execution = SimpleNamespace(job=SimpleNamespace(user_id="personal"))
    hermes = next(item for item in observed if item["host"] == "hermes")
    hermes["batch_handler"]((execution,))

    assert observer_calls == [("memory-result", (execution,))]
    assert prewarmed == [(execution,)]


def test_service_heartbeat_is_private_current_and_revision_bound(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    spool_root = tmp_path / "spool"
    monkeypatch.setattr(service_module, "MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_RUNTIME_REVISION", REVISION)
    from daemon_core import after_turn_spool

    monkeypatch.setattr(after_turn_spool, "MEMORY_ROOT", memory_root)
    after_turn_spool.AfterTurnSpool(spool_root)._ensure_runtime_namespace()
    service = service_module.AfterTurnService(
        spool=Spool(spool_root),
        user_ids=("private-user",),
        harness_factory=lambda: object(),
    )
    service._workers = {
        ("openclaw", "private-user"): SimpleNamespace(alive=True),
        ("hermes", "private-user"): SimpleNamespace(alive=True),
    }

    service.write_heartbeat()

    heartbeat = json.loads(
        (spool_root / "service.json").read_text(encoding="utf-8")
    )
    assert heartbeat["schema"] == service_module.AFTER_TURN_SERVICE_SCHEMA
    assert heartbeat["runtime_revision"] == REVISION
    assert "private-user" not in str(heartbeat)
    assert str(memory_root) not in str(heartbeat)
    assert service_module.read_after_turn_service_status(
        spool_root=spool_root,
        memory_root=memory_root,
        expected_revision=REVISION,
    )["ok"] is True
    assert service_module.read_after_turn_service_status(
        spool_root=spool_root,
        memory_root=memory_root,
        expected_revision="b" * 40,
    )["code"] == "after_turn_service_revision_mismatch"

    marker = spool_root / "runtime.json"
    original = marker.read_bytes()
    marker.write_text(json.dumps({"schema": after_turn_spool.AFTER_TURN_RUNTIME_SCHEMA,
                                  "memory_root_ref": "old-vault"}))
    # A fresh heartbeat and two live threads do not prove usable storage.
    assert service_module.read_after_turn_service_status(
        spool_root=spool_root, memory_root=memory_root,
    )["code"] == "after_turn_spool_namespace_mismatch"
    marker.write_bytes(original)
    service._workers[("hermes", "private-user")].last_error = "RuntimeError"
    service.write_heartbeat()
    assert service_module.read_after_turn_service_status(
        spool_root=spool_root, memory_root=memory_root,
    )["code"] == "after_turn_service_worker_error"


def test_configured_user_scope_is_explicit(monkeypatch):
    monkeypatch.delenv("DAEMON_CORE_AFTER_TURN_USER_IDS", raising=False)
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "personal")

    assert service_module.configured_after_turn_user_ids() == ("personal",)

    monkeypatch.setenv(
        "DAEMON_CORE_AFTER_TURN_USER_IDS",
        "one,two,one",
    )
    assert service_module.configured_after_turn_user_ids() == ("one", "two")
