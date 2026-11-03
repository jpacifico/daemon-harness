import hashlib
import json
from pathlib import Path

import pytest

from daemon_core.memory.graph import (
    audit_memory_graph,
    load_memory_graph_index,
    parse_souvenir_graph,
)
from daemon_core.memory.relation_publication_canary import (
    memory_relation_publication_canary_enabled,
    relation_publication_canary_receipt_path,
    run_relation_publication_canary,
)
from daemon_core.memory.relation_publication import (
    RelationMutation,
    render_relation_mutation,
)
from daemon_core.memory.relation_shadow import (
    RELATION_SHADOW_SCHEMA,
    RELATION_SHADOW_SCHEMA_V2,
    memory_relation_shadow_trace_path,
)


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
    l0_path = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "example.md"
    )
    source_path.parent.mkdir(parents=True)
    target_path.parent.mkdir(parents=True)
    l0_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# Coffrets Oasis\n\n## Faits\n- Deux coffrets sont conserves.\n",
        encoding="utf-8",
    )
    frontmatter = (
        "---\ncollection:\n  coverage: open\n---\n"
        if declared_collection
        else ""
    )
    target_path.write_text(
        frontmatter + "# Demenagement\n",
        encoding="utf-8",
    )
    l0_path.write_text(
        "# L0\n\n## turn-1\n\nPreuve exacte.\n"
        "\n## turn-2\n\nConfirmation exacte.\n",
        encoding="utf-8",
    )
    return memory_root, source_path, target_path, l0_path


def _proposal(**overrides) -> dict:
    proposal = {
        "action": "upsert",
        "kind": "member_of",
        "epistemic": "established",
        "source": SOURCE,
        "target": TARGET,
        "role": "objet sauve avant le demenagement",
        "evidence": "Preuve exacte.",
        "source_refs": [SOURCE_REF],
    }
    proposal.update(overrides)
    return proposal


def _write_event(
    memory_root: Path,
    *,
    event_id: str,
    accepted: list[dict] | None = None,
    accepted_collections: list[dict] | None = None,
) -> None:
    event = {
        "schema": (
            RELATION_SHADOW_SCHEMA_V2
            if accepted_collections is not None
            else RELATION_SHADOW_SCHEMA
        ),
        "event_id": event_id,
        "observed_at": "2026-08-08T08:00:00+00:00",
        "user_ref": _technical_ref(USER_ID),
        "source_trace_id": "turn-1",
        "provenance": {"source_ref": SOURCE_REF},
        "status": "recorded",
        "reason_code": "",
        "accepted": accepted if accepted is not None else [_proposal()],
        "rejected": [],
    }
    if accepted_collections is not None:
        event["accepted_collections"] = accepted_collections
        event["rejected_collections"] = []
    path = memory_relation_shadow_trace_path(USER_ID, memory_root=memory_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _enable(monkeypatch, event_id: str, receipt_root: Path) -> None:
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


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_canary_requires_one_explicit_user_and_event(monkeypatch):
    event_id = _event_id("allowed")
    _enable(monkeypatch, event_id, Path("unused"))

    assert memory_relation_publication_canary_enabled(USER_ID, event_id)
    assert not memory_relation_publication_canary_enabled("other", event_id)
    assert not memory_relation_publication_canary_enabled(
        USER_ID,
        _event_id("other"),
    )

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        "*",
    )
    assert not memory_relation_publication_canary_enabled(USER_ID, event_id)
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        f"{USER_ID},other",
    )
    assert not memory_relation_publication_canary_enabled(USER_ID, event_id)
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_USER_IDS",
        USER_ID,
    )
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_PUBLICATION_EVENT_IDS",
        f"{event_id},{_event_id('other')}",
    )
    assert not memory_relation_publication_canary_enabled(USER_ID, event_id)


def test_unlisted_identity_or_event_cannot_reach_the_vault(
    monkeypatch,
    tmp_path,
):
    memory_root, source_path, _target_path, _l0_path = _vault(tmp_path)
    event_id = _event_id("allowed")
    _write_event(memory_root, event_id=event_id)
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    source_before = source_path.read_bytes()

    wrong_user = run_relation_publication_canary(
        user_id="another-user",
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )
    wrong_event = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=_event_id("another-event"),
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert wrong_user.reason_code == "canary_not_enabled"
    assert wrong_event.reason_code == "canary_not_enabled"
    assert source_path.read_bytes() == source_before
    assert not list(receipt_root.rglob("*.json"))


def test_active_canary_publishes_once_and_replay_is_byte_identical(
    monkeypatch,
    tmp_path,
):
    memory_root, source_path, target_path, l0_path = _vault(tmp_path)
    event_id = _event_id("publish")
    _write_event(memory_root, event_id=event_id)
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    source_before = source_path.read_text(encoding="utf-8")
    target_before = target_path.read_bytes()
    l0_before = l0_path.read_bytes()

    first = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert first.status == "published"
    assert first.published_count == 1
    record = parse_souvenir_graph(
        SOURCE,
        source_path.read_text(encoding="utf-8"),
    )
    assert len(record.relations) == 1
    assert record.relations[0].target == TARGET
    assert record.relations[0].source_refs == (SOURCE_REF,)
    assert source_before.rstrip() in source_path.read_text(encoding="utf-8")
    assert target_path.read_bytes() == target_before
    assert l0_path.read_bytes() == l0_before
    index_path = memory_root / "levels" / "memory_graph.json"
    index = load_memory_graph_index(index_path)
    assert audit_memory_graph(index, memory_root=memory_root).issues == ()
    after_first = _tree_bytes(memory_root)
    receipt_path = relation_publication_canary_receipt_path(
        USER_ID,
        event_id,
        receipt_root=receipt_root,
    )
    receipt_before = receipt_path.read_bytes()

    second = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert second.status == "published"
    assert second.replayed is True
    assert _tree_bytes(memory_root) == after_first
    assert receipt_path.read_bytes() == receipt_before
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert SOURCE not in receipt_text
    assert TARGET not in receipt_text
    assert "Preuve exacte" not in receipt_text


def test_active_canary_preserves_canonical_role_on_confirmation(
    monkeypatch,
    tmp_path,
):
    memory_root, source_path, _target_path, _l0_path = _vault(tmp_path)
    source_path.write_text(
        render_relation_mutation(
            source_path.read_text(encoding="utf-8"),
            RelationMutation(
                action="upsert",
                source=SOURCE,
                target=TARGET,
                role="objet sauve avant le demenagement",
                source_ref=SOURCE_REF,
            ),
        ),
        encoding="utf-8",
    )
    confirmation_ref = (
        "levels/L0_ground_zero/conversations/example#turn-2"
    )
    event_id = _event_id("role-confirmation")
    _write_event(
        memory_root,
        event_id=event_id,
        accepted=[
            _proposal(
                role="description plus etroite",
                evidence="Confirmation exacte.",
                source_refs=[confirmation_ref],
            )
        ],
    )
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)

    result = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert result.status == "published"
    relation = parse_souvenir_graph(
        SOURCE,
        source_path.read_text(encoding="utf-8"),
    ).relations[0]
    assert relation.description == "objet sauve avant le demenagement"
    assert relation.source_refs == (SOURCE_REF, confirmation_ref)


@pytest.mark.parametrize(
    ("accepted", "collections", "reason_code"),
    [
        (
            [_proposal(), _proposal(role="second")],
            None,
            "exactly_one_relation_required",
        ),
        ([_proposal(action="remove", role="")], None, "active_canary_upsert_only"),
        (
            [_proposal()],
            [{"action": "declare_open", "target": "theme/new"}],
            "collection_publication_not_allowed",
        ),
    ],
)
def test_active_canary_blocks_out_of_scope_events(
    monkeypatch,
    tmp_path,
    accepted,
    collections,
    reason_code,
):
    memory_root, source_path, _target_path, _l0_path = _vault(tmp_path)
    event_id = _event_id(reason_code)
    _write_event(
        memory_root,
        event_id=event_id,
        accepted=accepted,
        accepted_collections=collections,
    )
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    source_before = source_path.read_bytes()

    result = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert (result.status, result.reason_code) == ("blocked", reason_code)
    assert source_path.read_bytes() == source_before
    assert not (memory_root / "levels" / "memory_graph.json").exists()


@pytest.mark.parametrize(
    ("proposal", "declared_collection", "reason_code"),
    [
        (
            _proposal(
                source_refs=[
                    "levels/L0_ground_zero/conversations/example#missing"
                ]
            ),
            True,
            "exact_l0_source_missing",
        ),
        (_proposal(), False, "target_not_declared_collection"),
    ],
)
def test_active_canary_keeps_integrity_failures_closed(
    monkeypatch,
    tmp_path,
    proposal,
    declared_collection,
    reason_code,
):
    memory_root, source_path, _target_path, _l0_path = _vault(
        tmp_path,
        declared_collection=declared_collection,
    )
    event_id = _event_id(reason_code)
    _write_event(memory_root, event_id=event_id, accepted=[proposal])
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    source_before = source_path.read_bytes()

    result = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert (result.status, result.reason_code) == ("blocked", reason_code)
    assert source_path.read_bytes() == source_before


def test_post_write_validation_failure_restores_markdown(
    monkeypatch,
    tmp_path,
):
    import daemon_core.memory.relation_publication as publication

    memory_root, source_path, _target_path, _l0_path = _vault(tmp_path)
    event_id = _event_id("rollback")
    _write_event(memory_root, event_id=event_id)
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    source_before = source_path.read_bytes()
    original_build = publication.build_memory_graph
    build_calls = 0

    def fail_second_build(*args, **kwargs):
        nonlocal build_calls
        build_calls += 1
        if build_calls == 1:
            return original_build(*args, **kwargs)
        assert "member_of" in source_path.read_text(encoding="utf-8")
        raise OSError("simulated validation interruption")

    monkeypatch.setattr(publication, "build_memory_graph", fail_second_build)
    with pytest.raises(OSError, match="validation interruption"):
        run_relation_publication_canary(
            user_id=USER_ID,
            event_id=event_id,
            memory_root=memory_root,
            receipt_root=receipt_root,
        )

    assert source_path.read_bytes() == source_before
    assert not relation_publication_canary_receipt_path(
        USER_ID,
        event_id,
        receipt_root=receipt_root,
    ).exists()


def test_retry_rebuilds_index_after_interrupted_derived_write(
    monkeypatch,
    tmp_path,
):
    import daemon_core.memory.relation_publication as publication

    memory_root, source_path, _target_path, _l0_path = _vault(tmp_path)
    event_id = _event_id("index-retry")
    _write_event(memory_root, event_id=event_id)
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    original_writer = publication.write_memory_graph_index

    def fail_index(*_args, **_kwargs):
        raise OSError("simulated index interruption")

    monkeypatch.setattr(publication, "write_memory_graph_index", fail_index)
    with pytest.raises(OSError, match="index interruption"):
        run_relation_publication_canary(
            user_id=USER_ID,
            event_id=event_id,
            memory_root=memory_root,
            receipt_root=receipt_root,
        )
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1
    assert not (memory_root / "levels" / "memory_graph.json").exists()

    monkeypatch.setattr(publication, "write_memory_graph_index", original_writer)
    replay = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert replay.status == "unchanged"
    assert replay.unchanged_count == 1
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1
    assert (memory_root / "levels" / "memory_graph.json").is_file()


def test_retry_reconciles_publication_after_receipt_interruption(
    monkeypatch,
    tmp_path,
):
    import daemon_core.memory.relation_publication_canary as canary

    memory_root, source_path, _target_path, _l0_path = _vault(tmp_path)
    event_id = _event_id("receipt-retry")
    _write_event(memory_root, event_id=event_id)
    receipt_root = tmp_path / "receipts"
    _enable(monkeypatch, event_id, receipt_root)
    original_write = canary.atomic_write_text
    attempts = 0

    def fail_receipt_once(path, content):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated receipt interruption")
        return original_write(path, content)

    monkeypatch.setattr(canary, "atomic_write_text", fail_receipt_once)
    with pytest.raises(OSError, match="receipt interruption"):
        run_relation_publication_canary(
            user_id=USER_ID,
            event_id=event_id,
            memory_root=memory_root,
            receipt_root=receipt_root,
        )
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1
    assert (memory_root / "levels" / "memory_graph.json").is_file()

    replay = run_relation_publication_canary(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
        receipt_root=receipt_root,
    )

    assert replay.status == "unchanged"
    assert replay.unchanged_count == 1
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1
    assert relation_publication_canary_receipt_path(
        USER_ID,
        event_id,
        receipt_root=receipt_root,
    ).is_file()
