from pathlib import Path

import pytest

from daemon_core.memory.collection_publication import (
    CollectionDeclaration,
    CollectionPublicationError,
    plan_collection_declaration,
)
from daemon_core.memory.graph import parse_souvenir_graph
from daemon_core.memory.metadata import parse_souvenir_frontmatter
from daemon_core.memory.relation_publication import (
    RelationMutation,
    RelationPublicationError,
    apply_relation_mutation,
    plan_relation_mutation,
)


MEMBER = "theme/loisirs/musique/coffrets_oasis"
CENTER = "theme/famille/demenagement_parents"
SOURCE_REF = "levels/L0_ground_zero/conversations/example#turn-1"


def _vault(tmp_path: Path, *, collection: str = ""):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    member_path = souvenirs_root / f"{MEMBER}.md"
    center_path = souvenirs_root / f"{CENTER}.md"
    l0_path = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "example.md"
    )
    member_path.parent.mkdir(parents=True)
    center_path.parent.mkdir(parents=True)
    l0_path.parent.mkdir(parents=True)
    member_path.write_text(
        "# Coffrets Oasis\n\n## Faits\n- Deux coffrets sont conserves.\n",
        encoding="utf-8",
    )
    frontmatter = ""
    if collection:
        frontmatter = f"---\ncollection:\n  coverage: {collection}\n---\n"
    center_path.write_text(
        frontmatter
        + "# Demenagement\n\n## Resume\n- Objets sauves.\n\n",
        encoding="utf-8",
    )
    l0_path.write_text(
        "# L0\n\n## turn-1\n\nPreuve.\n",
        encoding="utf-8",
    )
    return memory_root, souvenirs_root, member_path, center_path


def _declaration(**overrides):
    values = {
        "action": "declare_open",
        "target": CENTER,
        "source_ref": SOURCE_REF,
    }
    values.update(overrides)
    return CollectionDeclaration(**values)


def test_collection_publication_rejects_system_managed_scene(tmp_path):
    with pytest.raises(CollectionPublicationError) as raised:
        plan_collection_declaration(
            souvenirs_root=tmp_path / "capsules",
            memory_root=tmp_path,
            declaration=_declaration(
                target="theme/scenes_episodiques/2026/08/scene"
            ),
        )

    assert raised.value.reason_code == "system_managed_capsule"


def _relation():
    return RelationMutation(
        action="upsert",
        source=MEMBER,
        target=CENTER,
        role="objet sauve avant le demenagement",
        source_ref=SOURCE_REF,
    )


def test_declaration_plan_preserves_body_and_does_not_write(tmp_path):
    memory_root, souvenirs_root, _member_path, center_path = _vault(tmp_path)
    before = center_path.read_bytes()
    _frontmatter, body_before = parse_souvenir_frontmatter(before.decode("utf-8"))

    plan = plan_collection_declaration(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        declaration=_declaration(),
    )

    assert plan.changed is True
    assert center_path.read_bytes() == before
    frontmatter, body_after = parse_souvenir_frontmatter(plan.next_content)
    assert body_after == body_before
    assert frontmatter["collection"] == {
        "coverage": "open",
        "source_refs": [f"[[{SOURCE_REF}]]"],
    }
    assert plan.collection.coverage == "open"
    assert plan.collection.source_refs == (SOURCE_REF,)


def test_declaration_plan_is_idempotent_when_source_is_already_present(tmp_path):
    memory_root, souvenirs_root, _member_path, center_path = _vault(tmp_path)
    first = plan_collection_declaration(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        declaration=_declaration(),
    )
    center_path.write_text(first.next_content, encoding="utf-8")

    replay = plan_collection_declaration(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        declaration=_declaration(),
    )

    assert replay.changed is False
    assert replay.next_content.count("source_refs") == 1


def test_declaration_never_reopens_a_closed_collection(tmp_path):
    memory_root, souvenirs_root, _member_path, _center_path = _vault(
        tmp_path,
        collection="closed",
    )

    with pytest.raises(CollectionPublicationError) as raised:
        plan_collection_declaration(
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            declaration=_declaration(),
        )

    assert raised.value.reason_code == "collection_already_closed"


def test_relation_can_be_planned_against_the_staged_collection_only(tmp_path):
    memory_root, souvenirs_root, member_path, center_path = _vault(tmp_path)
    member_before = member_path.read_bytes()
    center_before = center_path.read_bytes()
    collection_plan = plan_collection_declaration(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        declaration=_declaration(),
    )

    relation_plan = plan_relation_mutation(
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        mutation=_relation(),
        staged_collections={CENTER: collection_plan.collection},
    )
    preview = apply_relation_mutation(
        relation_plan,
        souvenirs_root=souvenirs_root,
        memory_root=memory_root,
        index_path=memory_root / "levels" / "memory_graph.json",
        dry_run=True,
    )

    assert preview.status == "planned"
    assert parse_souvenir_graph(MEMBER, relation_plan.next_content).relations
    assert member_path.read_bytes() == member_before
    assert center_path.read_bytes() == center_before
    with pytest.raises(RelationPublicationError) as raised:
        apply_relation_mutation(
            relation_plan,
            souvenirs_root=souvenirs_root,
            memory_root=memory_root,
            index_path=memory_root / "levels" / "memory_graph.json",
        )
    assert raised.value.reason_code == "staged_collection_not_published"
