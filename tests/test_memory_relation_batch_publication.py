from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from daemon_core.memory.graph import load_memory_graph_index, parse_souvenir_graph
from daemon_core.memory.relation_publication import (
    RelationMutation,
    RelationPublicationError,
    apply_relation_mutation_batch,
    plan_relation_mutation_batch,
    render_relation_mutation,
)


BOOKS = "theme/items/books"
OASIS = "theme/items/oasis"
SAVED = "theme/collections/saved"
MUSIC = "theme/collections/music"
TURN_1 = "levels/L0_ground_zero/conversations/example#turn-1"
TURN_2 = "levels/L0_ground_zero/conversations/example#turn-2"
TURN_3 = "levels/L0_ground_zero/conversations/example#turn-3"


def _vault(tmp_path: Path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    paths = {
        BOOKS: souvenirs_root / f"{BOOKS}.md",
        OASIS: souvenirs_root / f"{OASIS}.md",
        SAVED: souvenirs_root / f"{SAVED}.md",
        MUSIC: souvenirs_root / f"{MUSIC}.md",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    paths[BOOKS].write_text("# Books\n\n- Five books.\n", encoding="utf-8")
    paths[OASIS].write_text("# Oasis\n\n- Two box sets.\n", encoding="utf-8")
    for name in (SAVED, MUSIC):
        paths[name].write_text(
            "---\ncollection:\n  coverage: open\n---\n# Collection\n",
            encoding="utf-8",
        )
    l0 = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "example.md"
    )
    l0.parent.mkdir(parents=True, exist_ok=True)
    l0.write_text(
        "# L0\n\n## turn-1\n\nBooks.\n\n"
        "## turn-2\n\nOasis.\n\n## turn-3\n\nMusic.\n",
        encoding="utf-8",
    )
    return memory_root, souvenirs_root, paths


def _mutation(
    source: str,
    target: str,
    source_ref: str,
    *,
    action: str = "upsert",
    role: str = "member",
) -> RelationMutation:
    return RelationMutation(
        action=action,
        source=source,
        target=target,
        role=role,
        source_ref=source_ref,
    )


def _distinct_mutations() -> tuple[RelationMutation, ...]:
    return (
        _mutation(BOOKS, SAVED, TURN_1),
        _mutation(OASIS, SAVED, TURN_2),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_batch_dry_run_publish_and_replay_are_coherent(tmp_path):
    memory_root, souvenirs_root, paths = _vault(tmp_path)
    originals = {name: _digest(path) for name, path in paths.items()}
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )

    dry_run = apply_relation_mutation_batch(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
        dry_run=True,
    )

    assert dry_run.status == "planned"
    assert dry_run.planned_count == 2
    assert {name: _digest(path) for name, path in paths.items()} == originals

    published = apply_relation_mutation_batch(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
    )
    assert published.status == "published"
    assert published.published_count == 2
    assert parse_souvenir_graph(
        BOOKS, paths[BOOKS].read_text(encoding="utf-8")
    ).relations[0].target == SAVED
    assert parse_souvenir_graph(
        OASIS, paths[OASIS].read_text(encoding="utf-8")
    ).relations[0].target == SAVED
    assert load_memory_graph_index(
        memory_root / "levels" / "memory_graph.json"
    ).library_fingerprint == published.index_fingerprint

    after = {name: _digest(path) for name, path in paths.items()}
    replay_plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    replay = apply_relation_mutation_batch(
        replay_plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
    )
    assert replay.status == "unchanged"
    assert replay.unchanged_count == 2
    assert {name: _digest(path) for name, path in paths.items()} == after


def test_batch_renders_multiple_relations_into_one_source_once(tmp_path):
    memory_root, souvenirs_root, paths = _vault(tmp_path)
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=(
            _mutation(OASIS, SAVED, TURN_2),
            _mutation(OASIS, MUSIC, TURN_3),
        ),
    )

    assert len(plan.source_plans) == 1
    result = apply_relation_mutation_batch(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
    )

    assert result.published_count == 2
    relations = parse_souvenir_graph(
        OASIS, paths[OASIS].read_text(encoding="utf-8")
    ).relations
    assert {relation.target for relation in relations} == {SAVED, MUSIC}


def test_invalid_second_mutation_writes_nothing(tmp_path):
    memory_root, souvenirs_root, paths = _vault(tmp_path)
    originals = {name: _digest(path) for name, path in paths.items()}

    with pytest.raises(RelationPublicationError) as raised:
        plan_relation_mutation_batch(
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            mutations=(
                _mutation(BOOKS, SAVED, TURN_1),
                _mutation(OASIS, "theme/collections/missing", TURN_2),
            ),
        )

    assert raised.value.reason_code == "unknown_target_capsule"
    assert {name: _digest(path) for name, path in paths.items()} == originals


def test_stale_source_blocks_before_any_batch_write(tmp_path):
    memory_root, souvenirs_root, paths = _vault(tmp_path)
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    paths[OASIS].write_text(
        paths[OASIS].read_text(encoding="utf-8") + "\n- concurrent\n",
        encoding="utf-8",
    )

    with pytest.raises(RelationPublicationError) as raised:
        apply_relation_mutation_batch(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=memory_root / "levels" / "memory_graph.json",
        )

    assert raised.value.reason_code == "source_changed_after_batch_plan"
    assert "member_of" not in paths[BOOKS].read_text(encoding="utf-8")
    assert "concurrent" in paths[OASIS].read_text(encoding="utf-8")


@pytest.mark.parametrize("failure_at", [1, 2])
@pytest.mark.parametrize("failure", [OSError("write"), KeyboardInterrupt()])
def test_source_interruption_rolls_back_every_source(
    tmp_path,
    monkeypatch,
    failure_at,
    failure,
):
    import daemon_core.memory.relation_publication as publication

    memory_root, souvenirs_root, paths = _vault(tmp_path)
    originals = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    original_write = publication.atomic_write_text
    writes = 0

    def fail_once(path, content):
        nonlocal writes
        if path in {paths[BOOKS], paths[OASIS]} and "member_of" in content:
            writes += 1
            if writes == failure_at:
                raise failure
        return original_write(path, content)

    monkeypatch.setattr(publication, "atomic_write_text", fail_once)
    with pytest.raises(type(failure)):
        apply_relation_mutation_batch(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=memory_root / "levels" / "memory_graph.json",
        )

    assert {
        name: path.read_text(encoding="utf-8") for name, path in paths.items()
    } == originals


def test_replay_completes_a_prefix_left_by_a_hard_crash(tmp_path):
    memory_root, souvenirs_root, paths = _vault(tmp_path)
    initial = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    first_source = next(item for item in initial.source_plans if item.source == BOOKS)
    paths[BOOKS].write_text(first_source.next_content, encoding="utf-8")

    replay_plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    result = apply_relation_mutation_batch(
        replay_plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
    )

    assert [item.status for item in result.items] == ["unchanged", "published"]
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1
    assert paths[OASIS].read_text(encoding="utf-8").count("member_of") == 1
    graph = load_memory_graph_index(memory_root / "levels" / "memory_graph.json")
    assert len(graph.souvenirs[BOOKS].relations) == 1
    assert len(graph.souvenirs[OASIS].relations) == 1


def test_final_validation_failure_rolls_back_every_source(tmp_path, monkeypatch):
    import daemon_core.memory.relation_publication as publication

    memory_root, souvenirs_root, paths = _vault(tmp_path)
    originals = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )

    def fail_build(*_args, **_kwargs):
        raise OSError("validation")

    monkeypatch.setattr(publication, "build_memory_graph", fail_build)
    with pytest.raises(OSError, match="validation"):
        apply_relation_mutation_batch(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=memory_root / "levels" / "memory_graph.json",
        )

    assert {
        name: path.read_text(encoding="utf-8") for name, path in paths.items()
    } == originals


def test_index_failure_keeps_complete_markdown_and_replay_repairs_index(
    tmp_path,
    monkeypatch,
):
    import daemon_core.memory.relation_publication as publication

    memory_root, souvenirs_root, paths = _vault(tmp_path)
    index_path = memory_root / "levels" / "memory_graph.json"
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    original_writer = publication.write_memory_graph_index

    def fail_index(*_args, **_kwargs):
        raise OSError("index")

    monkeypatch.setattr(publication, "write_memory_graph_index", fail_index)
    with pytest.raises(OSError, match="index"):
        apply_relation_mutation_batch(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=index_path,
        )
    assert "member_of" in paths[BOOKS].read_text(encoding="utf-8")
    assert "member_of" in paths[OASIS].read_text(encoding="utf-8")
    assert not index_path.exists()

    monkeypatch.setattr(publication, "write_memory_graph_index", original_writer)
    replay_plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    replay = apply_relation_mutation_batch(
        replay_plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=index_path,
    )

    assert replay.status == "unchanged"
    assert index_path.is_file()
    assert paths[BOOKS].read_text(encoding="utf-8").count("member_of") == 1
    assert paths[OASIS].read_text(encoding="utf-8").count("member_of") == 1


def test_batch_reports_satisfied_and_missing_relations_separately(tmp_path):
    memory_root, souvenirs_root, paths = _vault(tmp_path)
    paths[BOOKS].write_text(
        render_relation_mutation(
            paths[BOOKS].read_text(encoding="utf-8"),
            _mutation(BOOKS, SAVED, TURN_1),
        ),
        encoding="utf-8",
    )
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )

    result = apply_relation_mutation_batch(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
        dry_run=True,
    )

    assert [item.status for item in result.items] == ["unchanged", "planned"]
    assert result.unchanged_count == 1
    assert result.planned_count == 1


def test_batch_rebuilds_the_graph_once_during_publication(tmp_path, monkeypatch):
    import daemon_core.memory.relation_publication as publication

    memory_root, souvenirs_root, _paths = _vault(tmp_path)
    plan = plan_relation_mutation_batch(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutations=_distinct_mutations(),
    )
    original_build = publication.build_memory_graph
    calls = 0

    def counted_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(publication, "build_memory_graph", counted_build)
    apply_relation_mutation_batch(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
    )

    assert calls == 1


def test_batch_rejects_contradictory_actions_for_one_pair(tmp_path):
    memory_root, souvenirs_root, _paths = _vault(tmp_path)

    with pytest.raises(RelationPublicationError) as raised:
        plan_relation_mutation_batch(
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            mutations=(
                _mutation(BOOKS, SAVED, TURN_1),
                _mutation(BOOKS, SAVED, TURN_2, action="remove", role=""),
            ),
        )

    assert raised.value.reason_code == "contradictory_relation_batch_pair"
