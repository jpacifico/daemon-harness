from pathlib import Path

import pytest

from daemon_core.memory.collection_read import (
    CollectionReadError,
    read_collection_page,
)
from daemon_core.memory.graph import build_memory_graph


def _write(root: Path, name: str, content: str) -> None:
    path = root / "capsules" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _memory(tmp_path: Path, *, coverage="open", members=3):
    root = tmp_path / "memory"
    l0 = root / "levels" / "L0_ground_zero" / "conversations" / "example.md"
    l0.parent.mkdir(parents=True)
    l0.write_text("# L0\n\n## turn\n", encoding="utf-8")
    _write(
        root,
        "theme/episode",
        f"---\ncollection:\n  coverage: {coverage}\n"
        + (
            "  closure_source_refs:\n"
            "  - levels/L0_ground_zero/conversations/example#turn\n"
            if coverage == "closed"
            else ""
        )
        + "---\n# Episode\n",
    )
    for index in range(members):
        _write(
            root,
            f"theme/member_{index}",
            "# Member\n\n## Relations\n"
            f"- member_of [[theme/episode]] : role {index}\n"
            "  - source: [[levels/L0_ground_zero/conversations/example#turn]]\n",
        )
    _write(
        root,
        "theme/nearby",
        "# Nearby\n\n## Liens proches\n- [[theme/episode]] : voisin generique\n",
    )
    return root, build_memory_graph(root / "capsules", memory_root=root).index


def test_open_collection_returns_only_established_members(tmp_path):
    root, index = _memory(tmp_path)

    page = read_collection_page(index, "theme/episode", memory_root=root)

    assert page.coverage == "open"
    assert page.known_member_count == 3
    assert [member.path for member in page.members] == [
        "theme/member_0",
        "theme/member_1",
        "theme/member_2",
    ]
    assert "theme/nearby" not in [member.path for member in page.members]
    assert page.known_members_complete is True
    assert page.claimed_exhaustive is False


def test_closed_collection_can_claim_exhaustive_only_after_last_page(tmp_path):
    root, index = _memory(tmp_path, coverage="closed", members=10)

    first = read_collection_page(
        index,
        "theme/episode",
        memory_root=root,
        limit=8,
    )
    second = read_collection_page(
        index,
        "theme/episode",
        memory_root=root,
        offset=first.next_offset,
        limit=8,
    )

    assert len(first.members) == 8
    assert first.next_offset == 8
    assert first.claimed_exhaustive is False
    assert len(second.members) == 2
    assert second.next_offset is None
    assert second.claimed_exhaustive is True


def test_invalid_or_missing_provenance_is_not_returned(tmp_path):
    root, index = _memory(tmp_path, members=1)
    l0 = root / "levels" / "L0_ground_zero" / "conversations" / "example.md"
    l0.write_text("# L0\n\n## another-turn\n", encoding="utf-8")

    page = read_collection_page(index, "theme/episode", memory_root=root)

    assert page.known_member_count == 0
    assert page.members == ()


@pytest.mark.parametrize(
    ("center", "offset", "limit"),
    [
        ("../episode", 0, 8),
        ("theme/missing", 0, 8),
        ("theme/episode", -1, 8),
        ("theme/episode", 0, 9),
    ],
)
def test_collection_read_fails_closed_on_invalid_bounds(
    tmp_path,
    center,
    offset,
    limit,
):
    root, index = _memory(tmp_path)

    with pytest.raises(CollectionReadError):
        read_collection_page(
            index,
            center,
            memory_root=root,
            offset=offset,
            limit=limit,
        )
