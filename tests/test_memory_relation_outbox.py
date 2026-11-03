import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemon_core.after_turn_executor import execute_after_turn_job
from daemon_core.after_turn_spool import AfterTurnJob, AfterTurnSpool
from daemon_core.memory import (
    relation_outbox,
    relation_publication_canary,
    relation_shadow,
    writer,
)
from daemon_core.memory.graph import parse_souvenir_graph
from daemon_core.memory.relation_publication_canary import (
    relation_publication_canary_receipt_path,
)
from daemon_core.memory.relation_outbox import (
    memory_relation_publication_dry_run_enabled,
    relation_publication_dry_run_receipt_path,
    run_relation_publication_dry_run,
)
from daemon_core.memory.relation_shadow import (
    RELATION_SHADOW_SCHEMA,
    RELATION_SHADOW_SCHEMA_V2,
    memory_relation_shadow_trace_path,
    prepare_memory_relation_shadow_context,
    record_memory_relation_shadow_result,
)
from daemon_core.memory.writer import GroundZeroReference, append_ground_zero


SOURCE = "theme/loisirs/musique/coffrets_oasis"
TARGET = "theme/famille/demenagement_parents"
SOURCE_REF = "levels/L0_ground_zero/conversations/example#turn-1"
USER_ID = "relation-user"


def _technical_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _event_id(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _vault(tmp_path: Path, *, declared_collection: bool = True):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    source_path = souvenirs_root / f"{SOURCE}.md"
    target_path = souvenirs_root / f"{TARGET}.md"
    l0_path = memory_root / "levels" / "L0_ground_zero" / "conversations" / "example.md"
    source_path.parent.mkdir(parents=True)
    target_path.parent.mkdir(parents=True)
    l0_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# Coffrets Oasis\n\n## Faits\n- Deux coffrets sont conserves.\n",
        encoding="utf-8",
    )
    target_frontmatter = (
        "---\ncollection:\n  coverage: open\n---\n" if declared_collection else ""
    )
    target_path.write_text(
        target_frontmatter + "# Demenagement\n",
        encoding="utf-8",
    )
    l0_path.write_text(
        "# L0\n\n## turn-1\n\nPreuve exacte.\n",
        encoding="utf-8",
    )
    return memory_root, source_path, target_path


def _accepted_proposal(source_ref: str = SOURCE_REF) -> dict:
    return {
        "action": "upsert",
        "kind": "member_of",
        "epistemic": "established",
        "source": SOURCE,
        "target": TARGET,
        "role": "objet sauve avant le demenagement",
        "evidence": "Preuve exacte.",
        "source_refs": [source_ref],
    }


def _write_shadow_event(
    memory_root: Path,
    *,
    event_id: str,
    accepted: list[dict] | None = None,
    accepted_collections: list[dict] | None = None,
    source_trace_id: str = "turn-1",
) -> None:
    event = {
        "schema": (
            RELATION_SHADOW_SCHEMA_V2
            if accepted_collections is not None
            else RELATION_SHADOW_SCHEMA
        ),
        "event_id": event_id,
        "observed_at": "2026-08-06T10:00:00+00:00",
        "user_ref": _technical_ref(USER_ID),
        "source_trace_id": source_trace_id,
        "provenance": {"source_ref": SOURCE_REF},
        "status": (
            "recorded" if accepted or accepted_collections else "abstained"
        ),
        "reason_code": (
            "" if accepted or accepted_collections else "judge_abstained"
        ),
        "accepted": accepted or [],
        "rejected": [],
    }
    if accepted_collections is not None:
        event["accepted_collections"] = accepted_collections
        event["rejected_collections"] = []
    path = memory_relation_shadow_trace_path(
        USER_ID,
        memory_root=memory_root,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _enable_dry_run(monkeypatch, receipt_root: Path) -> None:
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION",
        "dry-run",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        USER_ID,
    )
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_RECEIPT_DIR",
        str(receipt_root),
    )


def _enable_canary(
    monkeypatch,
    receipt_root: Path,
    event_id: str,
) -> None:
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION",
        "canary",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        USER_ID,
    )
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_EVENT_IDS",
        event_id,
    )
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_RECEIPT_DIR",
        str(receipt_root),
    )


def test_dry_run_requires_explicit_mode_and_user_scope(monkeypatch):
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION",
        "dry-run",
    )
    monkeypatch.delenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        raising=False,
    )

    assert memory_relation_publication_dry_run_enabled(USER_ID) is False

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        USER_ID,
    )
    assert memory_relation_publication_dry_run_enabled(USER_ID) is True
    assert memory_relation_publication_dry_run_enabled("another-user") is False


def test_valid_shadow_event_is_planned_without_mutating_memory(tmp_path):
    memory_root, source_path, target_path = _vault(tmp_path)
    event_id = _event_id("planned")
    _write_shadow_event(
        memory_root,
        event_id=event_id,
        accepted=[_accepted_proposal()],
    )
    source_before = source_path.read_bytes()
    target_before = target_path.read_bytes()
    receipt_root = tmp_path / "exports" / "receipts"

    receipt = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert receipt.status == "planned"
    assert receipt.planned_count == 1
    assert receipt.blocked_count == 0
    assert source_path.read_bytes() == source_before
    assert target_path.read_bytes() == target_before
    assert not (memory_root / "levels" / "memory_graph.json").exists()
    raw_receipt = relation_publication_dry_run_receipt_path(
        USER_ID,
        event_id,
        receipt_root=receipt_root,
    ).read_text(encoding="utf-8")
    assert SOURCE not in raw_receipt
    assert TARGET not in raw_receipt
    assert "Preuve exacte" not in raw_receipt


def test_dry_run_replay_reuses_one_atomic_receipt(tmp_path):
    memory_root, _source_path, _target_path = _vault(tmp_path)
    event_id = _event_id("replay")
    _write_shadow_event(
        memory_root,
        event_id=event_id,
        accepted=[_accepted_proposal()],
    )
    receipt_root = tmp_path / "receipts"

    first = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )
    second = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.status == first.status == "planned"
    assert len(list(receipt_root.rglob("*.json"))) == 1


def test_deterministic_publication_refusal_is_recorded_not_raised(tmp_path):
    memory_root, source_path, _target_path = _vault(
        tmp_path,
        declared_collection=False,
    )
    event_id = _event_id("not-a-collection")
    _write_shadow_event(
        memory_root,
        event_id=event_id,
        accepted=[_accepted_proposal()],
    )
    original = source_path.read_bytes()

    receipt = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=tmp_path / "receipts",
    )

    assert receipt.status == "blocked"
    assert receipt.reason_code == "target_not_declared_collection"
    assert receipt.blocked_count == 1
    assert source_path.read_bytes() == original


def test_v2_dry_run_stages_new_collection_before_its_relation(tmp_path):
    memory_root, source_path, target_path = _vault(
        tmp_path,
        declared_collection=False,
    )
    event_id = _event_id("bootstrap-collection")
    _write_shadow_event(
        memory_root,
        event_id=event_id,
        accepted=[_accepted_proposal()],
        accepted_collections=[
            {
                "action": "declare_open",
                "epistemic": "established",
                "target": TARGET,
                "evidence": "Preuve exacte.",
                "source_refs": [SOURCE_REF],
            }
        ],
    )
    source_before = source_path.read_bytes()
    target_before = target_path.read_bytes()
    receipt_root = tmp_path / "receipts"

    receipt = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert receipt.status == "planned"
    assert receipt.proposal_count == 1
    assert receipt.collection_proposal_count == 1
    assert receipt.planned_count == 1
    assert receipt.collection_planned_count == 1
    assert receipt.blocked_count == 0
    assert source_path.read_bytes() == source_before
    assert target_path.read_bytes() == target_before
    assert not (memory_root / "levels" / "memory_graph.json").exists()
    payload = json.loads(
        relation_publication_dry_run_receipt_path(
            USER_ID,
            event_id,
            receipt_root=receipt_root,
        ).read_text(encoding="utf-8")
    )
    assert payload["schema"] == (
        "daemon.memory-relation-publication-dry-run.v2"
    )
    assert [outcome["kind"] for outcome in payload["outcomes"]] == [
        "collection",
        "relation",
    ]
    assert SOURCE not in json.dumps(payload)
    assert TARGET not in json.dumps(payload)


def test_blocked_collection_declaration_blocks_its_relation_without_retry(
    tmp_path,
):
    memory_root, _source_path, _target_path = _vault(
        tmp_path,
        declared_collection=False,
    )
    event_id = _event_id("blocked-bootstrap")
    _write_shadow_event(
        memory_root,
        event_id=event_id,
        accepted=[_accepted_proposal()],
        accepted_collections=[
            {
                "action": "declare_open",
                "epistemic": "established",
                "target": TARGET,
                "evidence": "Preuve exacte.",
                "source_refs": [
                    "levels/L0_ground_zero/conversations/example#missing"
                ],
            }
        ],
    )

    receipt = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=tmp_path / "receipts",
    )

    assert receipt.status == "blocked"
    assert receipt.blocked_count == 1
    assert receipt.collection_blocked_count == 1
    assert receipt.planned_count == 0


def test_abstention_and_missing_event_are_observable_noops(tmp_path):
    memory_root, _source_path, _target_path = _vault(tmp_path)
    abstention_id = _event_id("abstention")
    _write_shadow_event(memory_root, event_id=abstention_id)
    receipt_root = tmp_path / "receipts"

    abstention = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=abstention_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )
    missing = run_relation_publication_dry_run(
        user_id=USER_ID,
        event_id=_event_id("missing"),
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert (abstention.status, abstention.reason_code) == (
        "skipped",
        "no_accepted_relations",
    )
    assert (missing.status, missing.reason_code) == (
        "blocked",
        "shadow_event_missing",
    )


def _runtime_job(
    monkeypatch,
    memory_root: Path,
    *,
    turn_id: str,
    host: str,
    started_at: float,
    library_active: bool = False,
) -> tuple[AfterTurnJob, str]:
    message = f"Preuve exacte pour {turn_id}."
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(relation_shadow, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(relation_outbox, "MEMORY_ROOT", memory_root)
    user_l0 = append_ground_zero(
        USER_ID,
        "user",
        message,
        turn_id=turn_id,
        return_reference=True,
    )
    assistant_l0 = append_ground_zero(
        USER_ID,
        "assistant",
        "Reponse exacte.",
        turn_id=turn_id,
        return_reference=True,
    )
    assert isinstance(user_l0, GroundZeroReference)
    assert isinstance(assistant_l0, GroundZeroReference)
    context = prepare_memory_relation_shadow_context(
        current_user_l0=user_l0,
        user_message=message,
    )
    assert context is not None
    update = _accepted_proposal(context.source_ref)
    update["evidence"] = message
    shadow_receipt = record_memory_relation_shadow_result(
        user_id=USER_ID,
        context=context,
        user_message=message,
        raw_updates=[update],
        available_souvenirs=[SOURCE, TARGET],
    )
    return (
        AfterTurnJob(
            turn_id=turn_id,
            host=host,
            user_id=USER_ID,
            source_turn_started_at=started_at,
            scene_scope=f"telegram__{USER_ID}",
            user_l0=user_l0,
            assistant_l0=assistant_l0,
            selected_souvenirs=(SOURCE,),
            read_souvenirs=(SOURCE, TARGET),
            library_active=library_active,
            origin={"platform": "telegram", "chat_id": USER_ID},
        ),
        shadow_receipt.event_id,
    )


class _Harness:
    memory_provider = object()

    def __init__(self, event_ids: dict[str, str]):
        self.event_ids = event_ids
        self.calls: list[str] = []

    def update_memory_after_response(self, **kwargs):
        turn_id = str(kwargs["source_trace_id"])
        self.calls.append(turn_id)
        return SimpleNamespace(
            relation_shadow_event_id=self.event_ids[turn_id],
            changed_paths=[],
            unchanged_paths=[],
            rejected_paths=[],
            consulted_souvenirs=[],
        )


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_common_executor_has_host_parity(monkeypatch, tmp_path, host):
    memory_root, _source_path, _target_path = _vault(tmp_path)
    job, event_id = _runtime_job(
        monkeypatch,
        memory_root,
        turn_id=f"turn-{host}",
        host=host,
        started_at=1.0,
    )
    receipt_root = tmp_path / "receipts"
    _enable_dry_run(monkeypatch, receipt_root)
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    harness = _Harness({job.turn_id: event_id})

    execute_after_turn_job(job, harness=harness)

    assert harness.calls == [job.turn_id]
    payload = json.loads(
        relation_publication_dry_run_receipt_path(
            USER_ID,
            event_id,
            receipt_root=receipt_root,
        ).read_text(encoding="utf-8")
    )
    assert payload["status"] == "planned"


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_common_executor_publishes_exact_canary_event(
    monkeypatch,
    tmp_path,
    host,
):
    memory_root, source_path, _target_path = _vault(tmp_path)
    job, event_id = _runtime_job(
        monkeypatch,
        memory_root,
        turn_id=f"turn-active-{host}",
        host=host,
        started_at=1.0,
    )
    receipt_root = tmp_path / "receipts"
    _enable_canary(monkeypatch, receipt_root, event_id)
    monkeypatch.setattr(relation_publication_canary, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    harness = _Harness({job.turn_id: event_id})

    execute_after_turn_job(job, harness=harness)

    assert harness.calls == [job.turn_id]
    record = parse_souvenir_graph(
        SOURCE,
        source_path.read_text(encoding="utf-8"),
    )
    assert len(record.relations) == 1
    assert record.relations[0].target == TARGET
    payload = json.loads(
        relation_publication_canary_receipt_path(
            USER_ID,
            event_id,
            receipt_root=receipt_root,
        ).read_text(encoding="utf-8")
    )
    assert payload["status"] == "published"


def test_material_receipt_failure_retries_the_same_memory_job(
    monkeypatch,
    tmp_path,
):
    memory_root, _source_path, _target_path = _vault(tmp_path)
    job, event_id = _runtime_job(
        monkeypatch,
        memory_root,
        turn_id="turn-retry",
        host="hermes",
        started_at=1.0,
    )
    receipt_root = tmp_path / "receipts"
    _enable_dry_run(monkeypatch, receipt_root)
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    original_write = relation_outbox.atomic_write_text
    attempts = 0

    def fail_once(path, content):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated receipt interruption")
        return original_write(path, content)

    monkeypatch.setattr(relation_outbox, "atomic_write_text", fail_once)
    harness = _Harness({job.turn_id: event_id})
    spool = AfterTurnSpool(tmp_path / "spool", max_attempts=2)
    spool.enqueue(job)

    first = spool.drain(
        "hermes",
        USER_ID,
        lambda item: execute_after_turn_job(item, harness=harness),
        now=100.0,
    )
    second = spool.drain(
        "hermes",
        USER_ID,
        lambda item: execute_after_turn_job(item, harness=harness),
        now=106.0,
    )

    assert first.retried == 1
    assert second.completed == 1
    assert harness.calls == [job.turn_id, job.turn_id]
    assert len(list(receipt_root.rglob("*.json"))) == 1


def test_receipt_survives_failure_after_memory_before_outbox_ack(
    monkeypatch,
    tmp_path,
):
    memory_root, _source_path, _target_path = _vault(tmp_path)
    job, event_id = _runtime_job(
        monkeypatch,
        memory_root,
        turn_id="turn-after-receipt",
        host="hermes",
        started_at=1.0,
    )
    receipt_root = tmp_path / "receipts"
    _enable_dry_run(monkeypatch, receipt_root)
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    harness = _Harness({job.turn_id: event_id})
    followup_calls = 0

    def fail_followup_once(_result):
        nonlocal followup_calls
        followup_calls += 1
        if followup_calls == 1:
            raise RuntimeError("simulated failure after receipt")

    spool = AfterTurnSpool(tmp_path / "spool", max_attempts=2)
    spool.enqueue(job)

    def handler(item):
        return execute_after_turn_job(
            item,
            harness=harness,
            after_memory=fail_followup_once,
        )

    first = spool.drain("hermes", USER_ID, handler, now=100.0)
    second = spool.drain("hermes", USER_ID, handler, now=106.0)

    assert first.retried == 1
    assert second.completed == 1
    assert followup_calls == 2
    assert harness.calls == [job.turn_id, job.turn_id]
    assert len(list(receipt_root.rglob("*.json"))) == 1


def test_scene_coalescence_never_coalesces_relation_journal(
    monkeypatch,
    tmp_path,
):
    memory_root, _source_path, _target_path = _vault(tmp_path)
    first, first_event = _runtime_job(
        monkeypatch,
        memory_root,
        turn_id="turn-1",
        host="hermes",
        started_at=1.0,
        library_active=True,
    )
    second, second_event = _runtime_job(
        monkeypatch,
        memory_root,
        turn_id="turn-2",
        host="hermes",
        started_at=2.0,
        library_active=True,
    )
    receipt_root = tmp_path / "receipts"
    _enable_dry_run(monkeypatch, receipt_root)
    monkeypatch.setattr(
        "daemon_core.scene_model.run_scene_model_revision",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("scene must stay deferred")
        ),
    )
    harness = _Harness(
        {
            first.turn_id: first_event,
            second.turn_id: second_event,
        }
    )
    spool = AfterTurnSpool(tmp_path / "spool")
    spool.enqueue(first)
    spool.enqueue(second)

    result = spool.drain(
        "hermes",
        USER_ID,
        lambda item: execute_after_turn_job(
            item,
            harness=harness,
            defer_scene=True,
        ),
    )

    assert result.completed == 2
    assert harness.calls == ["turn-1", "turn-2"]
    assert len(list(receipt_root.rglob("*.json"))) == 2
    assert spool.projection_counts("hermes", USER_ID)["scene_pending"] == 1
