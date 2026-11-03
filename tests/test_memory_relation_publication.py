from pathlib import Path

import pytest

from daemon_core.memory.graph import (
    MEMBERSHIP_RELATION,
    load_memory_graph_index,
    parse_souvenir_graph,
)
from daemon_core.memory.relation_publication import (
    RelationMutation,
    RelationPublicationError,
    apply_relation_mutation,
    mutation_from_audit_item,
    plan_relation_mutation,
    render_relation_mutation,
)


SOURCE = "theme/loisirs/musique/coffrets_oasis"
TARGET = "theme/famille/demenagement_parents"
SOURCE_REF = "levels/L0_ground_zero/conversations/example#turn-1"


def _vault(tmp_path: Path):
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
    target_path.write_text(
        "---\ncollection:\n  coverage: open\n---\n# Demenagement\n",
        encoding="utf-8",
    )
    l0_path.write_text("# L0\n\n## turn-1\n\nPreuve.\n\n## turn-2\n", encoding="utf-8")
    return memory_root, souvenirs_root, source_path


def _mutation(**overrides):
    values = {
        "action": "upsert",
        "source": SOURCE,
        "target": TARGET,
        "role": "objet sauve avant le demenagement",
        "source_ref": SOURCE_REF,
    }
    values.update(overrides)
    return RelationMutation(**values)


def test_relation_publication_rejects_system_managed_scene(tmp_path):
    with pytest.raises(RelationPublicationError) as raised:
        plan_relation_mutation(
            souvenirs_root=tmp_path / "capsules",
            memory_root=tmp_path,
            mutation=_mutation(
                source="theme/scenes_episodiques/2026/08/scene"
            ),
        )

    assert raised.value.reason_code == "system_managed_capsule"


def test_upsert_dry_run_then_publish_and_replay_is_idempotent(tmp_path):
    memory_root, souvenirs_root, source_path = _vault(tmp_path)
    index_path = memory_root / "levels" / "memory_graph.json"
    original = source_path.read_text(encoding="utf-8")
    plan = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_mutation(),
    )

    dry_run = apply_relation_mutation(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=index_path,
        dry_run=True,
    )

    assert dry_run.status == "planned"
    assert source_path.read_text(encoding="utf-8") == original
    published = apply_relation_mutation(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=index_path,
    )
    assert published.status == "published"
    record = parse_souvenir_graph(SOURCE, source_path.read_text(encoding="utf-8"))
    assert len(record.relations) == 1
    assert record.relations[0].kind == MEMBERSHIP_RELATION
    assert record.relations[0].target == TARGET
    assert record.relations[0].source_refs == (SOURCE_REF,)
    assert load_memory_graph_index(index_path).library_fingerprint == (
        published.index_fingerprint
    )

    replay = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_mutation(),
    )
    replayed = apply_relation_mutation(
        replay,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=index_path,
    )
    assert replay.changed is False
    assert replayed.status == "unchanged"
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1


def test_existing_relation_adds_new_provenance_without_duplication(tmp_path):
    _memory_root, _souvenirs_root, source_path = _vault(tmp_path)
    once = render_relation_mutation(source_path.read_text(encoding="utf-8"), _mutation())
    twice = render_relation_mutation(
        once,
        _mutation(
            source_ref="levels/L0_ground_zero/conversations/example#turn-2"
        ),
    )

    record = parse_souvenir_graph(SOURCE, twice)
    assert len(record.relations) == 1
    assert record.relations[0].source_refs == (
        SOURCE_REF,
        "levels/L0_ground_zero/conversations/example#turn-2",
    )


def test_remove_is_sourced_and_idempotent(tmp_path):
    memory_root, souvenirs_root, source_path = _vault(tmp_path)
    source_path.write_text(
        render_relation_mutation(source_path.read_text(encoding="utf-8"), _mutation()),
        encoding="utf-8",
    )
    mutation = _mutation(
        action="remove",
        role="",
        source_ref="levels/L0_ground_zero/conversations/example#turn-2",
    )
    plan = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=mutation,
    )

    result = apply_relation_mutation(
        plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
    )

    assert result.status == "published"
    assert parse_souvenir_graph(
        SOURCE,
        source_path.read_text(encoding="utf-8"),
    ).relations == ()
    replay = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=mutation,
    )
    assert replay.changed is False


@pytest.mark.parametrize("action", ["upsert", "remove"])
def test_plan_rejects_missing_l0_provenance(tmp_path, action):
    memory_root, souvenirs_root, _source_path = _vault(tmp_path)

    with pytest.raises(RelationPublicationError, match="does not exist"):
        plan_relation_mutation(
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            mutation=_mutation(
                action=action,
                role="" if action == "remove" else "objet sauve",
                source_ref=(
                    "levels/L0_ground_zero/conversations/example#missing-turn"
                )
            ),
        )


def test_plan_rejects_target_that_is_not_a_collection(tmp_path):
    memory_root, souvenirs_root, _source_path = _vault(tmp_path)
    plain_target = souvenirs_root / "theme" / "plain.md"
    plain_target.write_text("# Plain\n", encoding="utf-8")

    with pytest.raises(
        RelationPublicationError,
        match="not a declared collection",
    ) as raised:
        plan_relation_mutation(
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            mutation=_mutation(target="theme/plain"),
        )
    assert raised.value.reason_code == "target_not_declared_collection"


def test_stale_plan_never_clobbers_a_concurrent_souvenir_change(tmp_path):
    memory_root, souvenirs_root, source_path = _vault(tmp_path)
    plan = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_mutation(),
    )
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "\n- ajout concurrent\n",
        encoding="utf-8",
    )

    with pytest.raises(RelationPublicationError, match="changed after"):
        apply_relation_mutation(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=memory_root / "levels" / "memory_graph.json",
        )

    assert "ajout concurrent" in source_path.read_text(encoding="utf-8")
    assert "member_of" not in source_path.read_text(encoding="utf-8")


def test_retry_recovers_when_index_write_failed_after_markdown_publish(
    tmp_path,
    monkeypatch,
):
    import daemon_core.memory.relation_publication as publication

    memory_root, souvenirs_root, source_path = _vault(tmp_path)
    index_path = memory_root / "levels" / "memory_graph.json"
    plan = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_mutation(),
    )
    original_writer = publication.write_memory_graph_index

    def fail_index(*_args, **_kwargs):
        raise OSError("simulated index interruption")

    monkeypatch.setattr(publication, "write_memory_graph_index", fail_index)
    with pytest.raises(OSError, match="simulated"):
        apply_relation_mutation(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=index_path,
        )
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1
    assert not index_path.exists()

    monkeypatch.setattr(publication, "write_memory_graph_index", original_writer)
    replay = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_mutation(),
    )
    result = apply_relation_mutation(
        replay,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=index_path,
    )

    assert replay.changed is False
    assert result.status == "unchanged"
    assert index_path.is_file()
    assert source_path.read_text(encoding="utf-8").count("member_of") == 1


def test_post_write_validation_failure_rolls_back_authoritative_markdown(
    tmp_path,
    monkeypatch,
):
    import daemon_core.memory.relation_publication as publication

    memory_root, souvenirs_root, source_path = _vault(tmp_path)
    original = source_path.read_text(encoding="utf-8")
    plan = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_mutation(),
    )

    def fail_build(*_args, **_kwargs):
        assert "member_of" in source_path.read_text(encoding="utf-8")
        raise OSError("simulated post-write validation failure")

    monkeypatch.setattr(publication, "build_memory_graph", fail_build)
    with pytest.raises(OSError, match="post-write"):
        apply_relation_mutation(
            plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=memory_root / "levels" / "memory_graph.json",
        )

    assert source_path.read_text(encoding="utf-8") == original


def test_existing_role_is_preserved_while_new_provenance_is_added(tmp_path):
    _memory_root, _souvenirs_root, source_path = _vault(tmp_path)
    current = render_relation_mutation(source_path.read_text(encoding="utf-8"), _mutation())
    second_source = "levels/L0_ground_zero/conversations/example#turn-2"

    rendered = render_relation_mutation(
        current,
        _mutation(
            role="interpretation plus etroite",
            source_ref=second_source,
        ),
    )

    relation = parse_souvenir_graph(SOURCE, rendered).relations[0]
    assert relation.description == "objet sauve avant le demenagement"
    assert relation.source_refs == (SOURCE_REF, second_source)


def test_audit_identity_binds_action_without_allowing_model_authored_paths():
    item = {
        "id": "move-oasis",
        "mode": "proposal",
        "proposed_action": "upsert",
        "source": SOURCE,
        "target": TARGET,
        "role": "objet sauve",
    }

    mutation = mutation_from_audit_item(
        item,
        {
            "item_id": "move-oasis",
            "disposition": "confirm",
            "action": "upsert",
        },
        source_ref=SOURCE_REF,
    )

    assert mutation == RelationMutation(
        action="upsert",
        source=SOURCE,
        target=TARGET,
        role="objet sauve",
        source_ref=SOURCE_REF,
    )
    assert mutation_from_audit_item(
        item,
        {
            "item_id": "move-oasis",
            "disposition": "reject",
            "action": "none",
        },
        source_ref=SOURCE_REF,
    ) is None


def test_audit_binding_rejects_identity_or_action_drift():
    item = {
        "id": "move-oasis",
        "mode": "proposal",
        "proposed_action": "upsert",
        "source": SOURCE,
        "target": TARGET,
        "role": "objet sauve",
    }

    with pytest.raises(RelationPublicationError, match="identity mismatch"):
        mutation_from_audit_item(
            item,
            {"item_id": "invented", "disposition": "confirm", "action": "upsert"},
            source_ref=SOURCE_REF,
        )
    with pytest.raises(RelationPublicationError, match="differs"):
        mutation_from_audit_item(
            item,
            {"item_id": "move-oasis", "disposition": "confirm", "action": "remove"},
            source_ref=SOURCE_REF,
        )
