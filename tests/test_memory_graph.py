import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.memory.graph import (
    DISCOVERY_RELATION,
    MEMBERSHIP_RELATION,
    STRUCTURAL_RELATION,
    build_memory_graph,
    build_memory_graph_from_entries,
    load_memory_graph_index,
    normalize_souvenir_name,
    parse_souvenir_graph,
    render_memory_graph_audit,
    write_memory_graph_index,
)


def test_runtime_graph_reuses_unchanged_loaded_souvenirs(tmp_path):
    entries = [
        ("theme/a", "# A\n"),
        ("theme/b", "# B\n"),
    ]

    first = build_memory_graph_from_entries(entries, memory_root=tmp_path)
    unchanged = build_memory_graph_from_entries(
        entries,
        previous=first.index,
        memory_root=tmp_path,
    )
    changed = build_memory_graph_from_entries(
        [("theme/a", "# A modifié\n"), entries[1]],
        previous=unchanged.index,
        memory_root=tmp_path,
    )

    assert first.stats.parsed_souvenirs == 2
    assert unchanged.stats.parsed_souvenirs == 0
    assert unchanged.stats.reused_souvenirs == 2
    assert changed.stats.parsed_souvenirs == 1
    assert changed.stats.reused_souvenirs == 1


def test_parser_accepts_legacy_and_obsidian_discovery_links():
    content = """# Family

## Sous-capsules
- theme/famille/enfant : relation familiale

## Liens proches
- theme/preferences : préférences durables
- [[theme/quotidien/repas]] : repas partagés
"""

    record = parse_souvenir_graph("theme/famille", content)

    assert [(item.kind, item.target, item.syntax) for item in record.relations] == [
        (STRUCTURAL_RELATION, "theme/famille/enfant", "legacy"),
        (DISCOVERY_RELATION, "theme/preferences", "legacy"),
        (DISCOVERY_RELATION, "theme/quotidien/repas", "wikilink"),
    ]
    assert [item.description for item in record.relations] == [
        "relation familiale",
        "préférences durables",
        "repas partagés",
    ]
    assert record.issues == ()


def test_parser_accepts_display_label_but_rejects_non_link_bullets_without_guessing():
    content = """# Oasis

## Liens proches
- [[theme/loisirs/musique|Musique]] : libellé natif
- « Flashbax » et « Idler's Dream » appartiennent à des périodes différentes.
"""

    record = parse_souvenir_graph("theme/loisirs/oasis", content)

    assert len(record.relations) == 1
    assert record.relations[0].target == "theme/loisirs/musique"
    assert [issue.code for issue in record.issues] == [
        "malformed_relation",
    ]


def test_duplicate_relation_is_reported_once_and_not_projected_twice(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    _write_souvenir(
        souvenirs_root,
        "theme/source",
        """# Source

## Liens proches
- theme/target : premier lien
- [[theme/target]] : doublon wikilien
""",
    )
    _write_souvenir(souvenirs_root, "theme/target", "# Target\n")

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)
    backlinks = build.index.as_dict()["backlinks"]["theme/target"]

    assert build.audit.relation_count == 2
    assert build.audit.valid_relation_count == 1
    assert [issue.code for issue in build.audit.issues] == ["duplicate_relation"]
    assert backlinks == [{"source": "theme/source", "kind": DISCOVERY_RELATION}]


def test_parser_reads_sourced_membership_and_collection_coverage():
    collection = parse_souvenir_graph(
        "theme/famille/demenagement",
        """---
collection:
  coverage: open
  source_refs:
    - "[[levels/L0_ground_zero/conversations/example#2026-08-06T09:00:00 user]]"
---
# Déménagement
""",
    )
    member = parse_souvenir_graph(
        "theme/loisirs/ordinateur_retro",
        """# ordinateur rétro

## Relations
- member_of [[theme/famille/demenagement]] : objet sauvé
  - source: [[levels/L0_ground_zero/conversations/example#2026-08-06T10:00:00 user]]
""",
    )

    assert collection.collection is not None
    assert collection.collection.coverage == "open"
    assert collection.collection.source_refs == (
        "levels/L0_ground_zero/conversations/example#2026-08-06T09:00:00 user",
    )
    assert member.relations[0].kind == MEMBERSHIP_RELATION
    assert member.relations[0].source_refs == (
        "levels/L0_ground_zero/conversations/example#2026-08-06T10:00:00 user",
    )
    assert member.issues == ()


def test_collection_declaration_source_is_audited_against_exact_l0(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    _write_souvenir(
        souvenirs_root,
        "theme/collection",
        """---
collection:
  coverage: open
  source_refs:
    - "[[levels/L0_ground_zero/conversations/example#missing-turn]]"
---
# Collection
""",
    )

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)

    assert [issue.code for issue in build.audit.issues] == [
        "missing_collection_source"
    ]


def test_closed_collection_requires_explicit_closure_source():
    record = parse_souvenir_graph(
        "theme/collection",
        """---
collection:
  coverage: closed
---
# Collection
""",
    )

    assert record.collection is not None
    assert record.collection.coverage == "closed"
    assert [issue.code for issue in record.issues] == [
        "closed_collection_without_provenance"
    ]


def test_audit_derives_backlinks_and_collection_members(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    source = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "example.md"
    )
    source.parent.mkdir(parents=True)
    source.write_text("# L0\n\n## turn-1\n", encoding="utf-8")
    _write_souvenir(
        souvenirs_root,
        "theme/famille/demenagement",
        """---
collection:
  coverage: open
---
# Déménagement
""",
    )
    _write_souvenir(
        souvenirs_root,
        "theme/loisirs/ordinateur_retro",
        """# ordinateur rétro

## Relations
- member_of [[theme/famille/demenagement]] : objet sauvé
  - source: [[levels/L0_ground_zero/conversations/example#turn-1]]
""",
    )
    _write_souvenir(
        souvenirs_root,
        "theme/loisirs/musique",
        """# Musique

## Liens proches
- [[theme/loisirs/ordinateur_retro]] : souvenir voisin
""",
    )

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)
    projection = build.index.as_dict()

    assert build.audit.issues == ()
    assert build.audit.collection_count == 1
    assert build.audit.collection_member_count == 1
    assert projection["collections"]["theme/famille/demenagement"]["members"] == [
        {
            "path": "theme/loisirs/ordinateur_retro",
            "role": "objet sauvé",
        }
    ]
    relation = projection["capsules"]["theme/loisirs/ordinateur_retro"]["relations"][0]
    assert relation["source_refs"] == [
        "levels/L0_ground_zero/conversations/example#turn-1"
    ]
    assert projection["backlinks"]["theme/loisirs/ordinateur_retro"][0]["source"] == (
        "theme/loisirs/musique"
    )


def test_audit_distinguishes_missing_target_and_missing_provenance(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    _write_souvenir(
        souvenirs_root,
        "theme/source",
        """# Source

## Liens proches
- theme/missing : cible absente

## Relations
- member_of [[theme/source]] : boucle sans preuve
""",
    )

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)
    codes = {issue.code for issue in build.audit.issues}

    assert "missing_relation_target" in codes
    assert "missing_relation_provenance" in codes
    assert "self_relation" in codes
    assert "undeclared_collection_target" in codes


def test_audit_requires_the_exact_l0_heading_not_only_the_source_file(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    source = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "example.md"
    )
    source.parent.mkdir(parents=True)
    source.write_text("# L0\n\n## another-turn\n", encoding="utf-8")
    _write_souvenir(
        souvenirs_root,
        "theme/collection",
        """---
collection:
  coverage: open
---
# Collection
""",
    )
    _write_souvenir(
        souvenirs_root,
        "theme/member",
        """# Member

## Relations
- member_of [[theme/collection]] : membre sourcé
  - source: [[levels/L0_ground_zero/conversations/example#missing-turn]]
""",
    )

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)

    assert [issue.code for issue in build.audit.issues] == [
        "missing_relation_source"
    ]


def test_incremental_build_reuses_only_unchanged_souvenirs(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    first_path = _write_souvenir(souvenirs_root, "theme/first", "# First\n")
    second_path = _write_souvenir(souvenirs_root, "theme/second", "# Second\n")
    _write_souvenir(souvenirs_root, "theme/third", "# Third\n")

    first = build_memory_graph(souvenirs_root, memory_root=memory_root)
    unchanged = build_memory_graph(
        souvenirs_root,
        previous=first.index,
        memory_root=memory_root,
    )
    first_path.write_text("# First changed\n", encoding="utf-8")
    changed = build_memory_graph(
        souvenirs_root,
        previous=unchanged.index,
        memory_root=memory_root,
    )
    second_path.unlink()
    deleted = build_memory_graph(
        souvenirs_root,
        previous=changed.index,
        memory_root=memory_root,
    )

    assert first.stats.parsed_souvenirs == 3
    assert first.stats.reused_souvenirs == 0
    assert unchanged.stats.parsed_souvenirs == 0
    assert unchanged.stats.reused_souvenirs == 3
    assert changed.stats.parsed_souvenirs == 1
    assert changed.stats.reused_souvenirs == 2
    assert deleted.stats.removed_souvenirs == 1
    assert deleted.stats.reused_souvenirs == 2


def test_index_round_trip_preserves_reusable_souvenir_records(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    _write_souvenir(
        souvenirs_root,
        "theme/source",
        """# Source

## Liens proches
- [[theme/target]] : voisin
""",
    )
    _write_souvenir(souvenirs_root, "theme/target", "# Target\n")
    before = {
        path: path.read_bytes() for path in souvenirs_root.rglob("*.md")
    }

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)
    index_path = tmp_path / "exports" / "memory_graph.json"
    write_memory_graph_index(index_path, build.index)
    loaded = load_memory_graph_index(index_path)
    rebuilt = build_memory_graph(
        souvenirs_root,
        previous=loaded,
        memory_root=memory_root,
    )

    assert loaded.library_fingerprint == build.index.library_fingerprint
    assert rebuilt.stats.parsed_souvenirs == 0
    assert rebuilt.stats.reused_souvenirs == 2
    assert json.loads(index_path.read_text(encoding="utf-8"))["backlinks"]
    assert {path: path.read_bytes() for path in souvenirs_root.rglob("*.md")} == before


def test_audit_renderer_keeps_build_and_issue_counts_visible(tmp_path):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    _write_souvenir(
        souvenirs_root,
        "theme/source",
        """# Source

## Liens proches
- theme/missing : absent
""",
    )

    build = build_memory_graph(souvenirs_root, memory_root=memory_root)
    rendered = render_memory_graph_audit(build.audit, stats=build.stats)

    assert "souvenirs: 1" in rendered
    assert "related_to: 0/1 valid" in rendered
    assert "missing_relation_target: 1" in rendered
    assert "build: 1 parsed" in rendered


def test_souvenir_name_normalization_is_bounded_to_full_paths():
    assert normalize_souvenir_name("memory/capsules/theme/famille.md") == (
        "theme/famille"
    )
    assert normalize_souvenir_name("./theme/famille") == "theme/famille"
    assert normalize_souvenir_name("theme/famille#section") is None
    assert normalize_souvenir_name("theme/famille|Famille") is None
    assert normalize_souvenir_name("../outside") is None


def _write_souvenir(root: Path, name: str, content: str) -> Path:
    path = root / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
