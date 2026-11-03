from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from daemon_core.dreaming import run_dreaming
from daemon_core.memory import (
    relation_gardening,
    relation_gardening_publication as publication,
    relation_publication,
    writer,
)
from daemon_core.memory.graph import load_memory_graph_index
from daemon_core.memory.relation_dreaming import RELATION_DREAMING_SCHEMA
from daemon_core.memory.relation_gardening import (
    prepare_relation_gardening_publication,
    record_relation_gardening_event,
    run_relation_gardening_event_dry_run,
)
from daemon_core.memory.writer import GroundZeroReference, append_ground_zero
from daemon_core.providers.base import LLMProvider


USER_ID = "personal-gardening-user"
BOOKS = "theme/items/books"
OASIS = "theme/items/oasis"
COLLECTION = "theme/collections/saved"


class PersonalGardeningProvider(LLMProvider):
    def __init__(
        self,
        repairs: set[tuple[str, str]],
        *,
        dreaming_updates: list[dict] | None = None,
    ) -> None:
        self.repairs = repairs
        self.dreaming_updates = dreaming_updates or []
        self.calls: list[str] = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        if "DreamingAgent" in messages[0]["content"]:
            self.calls.append("dreaming")
            return json.dumps(
                {
                    "reason": "Ordinary maintenance.",
                    "confidence": 1.0,
                    "report_md": "Main maintenance completed.",
                    "unresolved_conflicts": [],
                    "updates": self.dreaming_updates,
                }
            )
        self.calls.append("relation_gardening")
        items = json.loads(messages[-1]["content"].split("\n\n", 1)[1])
        return json.dumps(
            {
                "schema": RELATION_DREAMING_SCHEMA,
                "decisions": [
                    {
                        "item_id": item["item_id"],
                        "disposition": (
                            "repair"
                            if (item["source"], item["target"]) in self.repairs
                            else "reject"
                        ),
                        "action": (
                            "upsert"
                            if (item["source"], item["target"]) in self.repairs
                            else "none"
                        ),
                        "reason": "The exact direct statement establishes it.",
                    }
                    for item in items
                ],
            }
        )


def _write_souvenir(memory_root: Path, name: str, content: str) -> Path:
    path = memory_root / "capsules" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _vault(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    memory_root = tmp_path / "memory"
    paths = {
        BOOKS: _write_souvenir(
            memory_root,
            BOOKS,
            "# Books\n\n## Facts\n- Five books must be saved.\n",
        ),
        OASIS: _write_souvenir(
            memory_root,
            OASIS,
            "# Oasis\n\n## Facts\n- Two box sets must be saved.\n",
        ),
        COLLECTION: _write_souvenir(
            memory_root,
            COLLECTION,
            "---\ncollection:\n  coverage: open\n---\n"
            "# Saved objects\n\n## Facts\n- Personal objects to save.\n",
        ),
    }
    return memory_root, paths


def _enable(
    monkeypatch,
    memory_root: Path,
    *,
    activated_at: float = 10.0,
) -> None:
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "active")
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(relation_gardening, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(publication, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    publication.activate_personal_relation_gardening(
        user_id=USER_ID,
        activated_at=activated_at,
        memory_root=memory_root,
    )


def _event(
    memory_root: Path,
    *,
    turn_id: str,
    started_at: float,
    context: tuple[str, ...],
) -> str:
    user_l0 = append_ground_zero(
        USER_ID,
        "user",
        "These exact items belong to the saved objects collection.",
        turn_id=turn_id,
        return_reference=True,
    )
    assistant_l0 = append_ground_zero(
        USER_ID,
        "assistant",
        "Understood.",
        turn_id=turn_id,
        return_reference=True,
    )
    assert isinstance(user_l0, GroundZeroReference)
    assert isinstance(assistant_l0, GroundZeroReference)
    capture = record_relation_gardening_event(
        user_id=USER_ID,
        source_trace_id=turn_id,
        source_turn_started_at=started_at,
        user_l0=user_l0,
        assistant_l0=assistant_l0,
        source_posture="direct_fact",
        changed_souvenirs=(),
        context_souvenirs=context,
        memory_root=memory_root,
        require_enabled=False,
    )
    return capture.event_id


def test_active_mode_requires_one_identity_and_private_marker(monkeypatch, tmp_path):
    memory_root, _paths = _vault(tmp_path)
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "active")
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)

    assert publication.relation_gardening_personal_publication_enabled(USER_ID)
    assert not publication.relation_gardening_personal_publication_enabled(
        "another-user"
    )
    marker = publication.activate_personal_relation_gardening(
        user_id=USER_ID,
        activated_at=10.0,
        memory_root=memory_root,
    )
    replay = publication.activate_personal_relation_gardening(
        user_id=USER_ID,
        activated_at=20.0,
        memory_root=memory_root,
    )

    assert marker == replay
    assert marker["activated_at"] == 10.0
    assert publication.relation_gardening_personal_activation_path(
        USER_ID,
        memory_root=memory_root,
    ).stat().st_mode & 0o777 == 0o600

    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", "*")
    assert not publication.relation_gardening_personal_publication_enabled(USER_ID)


def test_events_before_activation_are_never_published(monkeypatch, tmp_path):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root, activated_at=10.0)
    event_id = _event(
        memory_root,
        turn_id="turn-before",
        started_at=9.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})

    assert publication.pending_personal_relation_gardening_event_ids(
        USER_ID,
        memory_root=memory_root,
    ) == ()
    result = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert result.status == "skipped"
    assert result.reason_code == "event_before_personal_activation"
    assert "member_of" not in paths[BOOKS].read_text(encoding="utf-8")
    assert provider.calls == []


def test_active_configuration_without_marker_cannot_publish(monkeypatch, tmp_path):
    memory_root, paths = _vault(tmp_path)
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "active")
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-no-marker",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})

    assert publication.run_pending_personal_relation_gardening(
        user_id=USER_ID,
        provider=provider,
        memory_root=memory_root,
    ) == ()
    direct = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert direct.status == "skipped"
    assert direct.reason_code == "personal_activation_missing"
    assert "member_of" not in paths[BOOKS].read_text(encoding="utf-8")
    assert provider.calls == []


def test_one_event_publishes_a_complete_batch_then_replays(monkeypatch, tmp_path):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-batch",
        started_at=11.0,
        context=(BOOKS, OASIS, COLLECTION),
    )
    provider = PersonalGardeningProvider(
        {(BOOKS, COLLECTION), (OASIS, COLLECTION)}
    )
    collection_before = paths[COLLECTION].read_bytes()
    l0_before = {
        path.relative_to(memory_root): path.read_bytes()
        for path in (memory_root / "levels" / "L0_ground_zero").rglob("*.md")
    }

    results = publication.run_pending_personal_relation_gardening(
        user_id=USER_ID,
        provider=provider,
        memory_root=memory_root,
    )

    assert len(results) == 1
    assert results[0].status == "planned"
    assert results[0].publication_status == "published"
    assert results[0].published_count == 2
    assert provider.calls == ["relation_gardening"]
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1
    assert paths[OASIS].read_text(encoding="utf-8").count("member_of") == 1
    assert paths[COLLECTION].read_bytes() == collection_before
    assert {
        path.relative_to(memory_root): path.read_bytes()
        for path in (memory_root / "levels" / "L0_ground_zero").rglob("*.md")
    } == l0_before
    graph = load_memory_graph_index(memory_root / "levels" / "memory_graph.json")
    assert sum(
        relation.target == COLLECTION
        for name in (BOOKS, OASIS)
        for relation in graph.souvenirs[name].relations
    ) == 2

    replay = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )
    assert replay.status == "published"
    assert replay.replayed is True
    assert publication.pending_personal_relation_gardening_event_ids(
        USER_ID,
        memory_root=memory_root,
    ) == ()
    assert provider.calls == ["relation_gardening"]


def test_successive_events_are_judged_against_successive_graphs(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    first_id = _event(
        memory_root,
        turn_id="turn-books",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    second_id = _event(
        memory_root,
        turn_id="turn-oasis",
        started_at=12.0,
        context=(OASIS, COLLECTION),
    )
    provider = PersonalGardeningProvider(
        {(BOOKS, COLLECTION), (OASIS, COLLECTION)}
    )

    results = publication.run_pending_personal_relation_gardening(
        user_id=USER_ID,
        provider=provider,
        memory_root=memory_root,
    )

    assert [result.event_id for result in results] == [first_id, second_id]
    assert [result.published_count for result in results] == [1, 1]
    assert provider.calls == ["relation_gardening", "relation_gardening"]
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1
    assert paths[OASIS].read_text(encoding="utf-8").count("member_of") == 1


def test_existing_dry_run_is_resumed_without_another_provider_call(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-resume",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})
    judged = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        provider=provider,
        memory_root=memory_root,
    )
    assert judged.status == "planned"
    assert provider.calls == ["relation_gardening"]

    result = publication.run_pending_personal_relation_gardening(
        user_id=USER_ID,
        provider=provider,
        memory_root=memory_root,
    )

    assert result[0].published_count == 1
    assert provider.calls == ["relation_gardening"]
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1


def test_remove_is_terminally_blocked_without_writing(monkeypatch, tmp_path):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-remove",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})
    run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        provider=provider,
        memory_root=memory_root,
    )
    proof = prepare_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )
    monkeypatch.setattr(
        publication,
        "prepare_relation_gardening_publication",
        lambda **_kwargs: replace(
            proof,
            mutations=(replace(proof.mutations[0], action="remove", role=""),),
        ),
    )
    before = paths[BOOKS].read_bytes()

    blocked = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )
    replay = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert blocked.status == "blocked"
    assert blocked.reason_code == "personal_publication_upsert_only"
    assert replay.replayed is True
    assert paths[BOOKS].read_bytes() == before


def test_collection_change_after_judgment_blocks_publication(monkeypatch, tmp_path):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-closed-target",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})
    run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        provider=provider,
        memory_root=memory_root,
    )
    paths[COLLECTION].write_text(
        paths[COLLECTION]
        .read_text(encoding="utf-8")
        .replace("coverage: open", "coverage: closed"),
        encoding="utf-8",
    )

    result = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert result.status == "blocked"
    assert result.reason_code == "graph_changed_after_decision"
    assert "member_of" not in paths[BOOKS].read_text(encoding="utf-8")


def test_index_failure_reuses_intent_without_second_judgment(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-index-failure",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})
    run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        provider=provider,
        memory_root=memory_root,
    )
    original_writer = relation_publication.write_memory_graph_index

    def fail_index(*_args, **_kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(relation_publication, "write_memory_graph_index", fail_index)
    with pytest.raises(OSError, match="index unavailable"):
        publication.run_personal_relation_gardening_publication(
            user_id=USER_ID,
            event_id=event_id,
            memory_root=memory_root,
        )
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1
    assert provider.calls == ["relation_gardening"]

    monkeypatch.setattr(
        relation_publication,
        "write_memory_graph_index",
        original_writer,
    )
    recovered = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert recovered.status == "unchanged"
    assert recovered.unchanged_count == 1
    assert provider.calls == ["relation_gardening"]


def test_receipt_failure_completes_without_duplicate_or_second_judgment(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-receipt-failure",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})
    run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        provider=provider,
        memory_root=memory_root,
    )
    receipt_path = publication.relation_gardening_publication_receipt_path(
        USER_ID,
        event_id,
        memory_root=memory_root,
    )
    original_write = publication.atomic_write_text

    def fail_receipt(path, content):
        if path == receipt_path:
            raise OSError("receipt unavailable")
        return original_write(path, content)

    monkeypatch.setattr(publication, "atomic_write_text", fail_receipt)
    with pytest.raises(OSError, match="receipt unavailable"):
        publication.run_personal_relation_gardening_publication(
            user_id=USER_ID,
            event_id=event_id,
            memory_root=memory_root,
        )
    source_after = paths[BOOKS].read_bytes()
    assert source_after.decode("utf-8").count("member_of") == 1
    assert provider.calls == ["relation_gardening"]

    monkeypatch.setattr(publication, "atomic_write_text", original_write)
    recovered = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert recovered.status == "unchanged"
    assert recovered.unchanged_count == 1
    assert paths[BOOKS].read_bytes() == source_after
    assert provider.calls == ["relation_gardening"]


def test_partial_batch_failure_rolls_back_then_replays_whole_batch(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    event_id = _event(
        memory_root,
        turn_id="turn-batch-failure",
        started_at=11.0,
        context=(BOOKS, OASIS, COLLECTION),
    )
    provider = PersonalGardeningProvider(
        {(BOOKS, COLLECTION), (OASIS, COLLECTION)}
    )
    run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=event_id,
        provider=provider,
        memory_root=memory_root,
    )
    before = {name: path.read_bytes() for name, path in paths.items()}
    original_write = relation_publication.atomic_write_text
    failed = False

    def fail_second_source(path, content):
        nonlocal failed
        if path == paths[OASIS] and not failed:
            failed = True
            raise OSError("second source unavailable")
        return original_write(path, content)

    monkeypatch.setattr(
        relation_publication,
        "atomic_write_text",
        fail_second_source,
    )
    with pytest.raises(OSError, match="second source unavailable"):
        publication.run_personal_relation_gardening_publication(
            user_id=USER_ID,
            event_id=event_id,
            memory_root=memory_root,
        )
    assert {name: path.read_bytes() for name, path in paths.items()} == before

    monkeypatch.setattr(
        relation_publication,
        "atomic_write_text",
        original_write,
    )
    recovered = publication.run_personal_relation_gardening_publication(
        user_id=USER_ID,
        event_id=event_id,
        memory_root=memory_root,
    )

    assert recovered.status == "published"
    assert recovered.published_count == 2
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1
    assert paths[OASIS].read_text(encoding="utf-8").count("member_of") == 1
    assert provider.calls == ["relation_gardening"]


def test_dreaming_dry_run_never_publishes_but_active_run_does(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    _event(
        memory_root,
        turn_id="turn-dreaming",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider({(BOOKS, COLLECTION)})

    dry = run_dreaming(USER_ID, provider=provider, dry_run=True)
    assert dry.relation_gardening_results[0].status == "planned"
    assert "member_of" not in paths[BOOKS].read_text(encoding="utf-8")

    active = run_dreaming(USER_ID, provider=provider, dry_run=False)

    assert active.relation_gardening_results[0].published_count == 1
    assert "Jardinage relationnel (active)" in active.report_md
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1


def test_same_pass_dreaming_update_cannot_overwrite_published_relation(
    monkeypatch,
    tmp_path,
):
    memory_root, paths = _vault(tmp_path)
    _enable(monkeypatch, memory_root)
    _event(
        memory_root,
        turn_id="turn-dreaming-conflict",
        started_at=11.0,
        context=(BOOKS, COLLECTION),
    )
    provider = PersonalGardeningProvider(
        {(BOOKS, COLLECTION)},
        dreaming_updates=[
            {
                "path": f"memory/capsules/{BOOKS}.md",
                "action": "update",
                "reason": "Concurrent ordinary Dreaming proposal.",
                "content_md": "# Books\n\n## Facts\n- Rewritten proposal.\n",
                "index_line": "- theme/items/books: Books -- saved items",
            }
        ],
    )

    result = run_dreaming(USER_ID, provider=provider, dry_run=False)

    assert result.relation_gardening_results[0].published_count == 1
    assert any(
        "modifié depuis la lecture Dreaming" in item.reason
        for item in result.rejected_updates
    )
    content = paths[BOOKS].read_text(encoding="utf-8")
    assert content.count("member_of") == 1
    assert "Rewritten proposal" not in content
