"""Native syntax and real writer boundaries, always in synthetic roots."""
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from daemon_core.memory.graph import build_memory_graph, parse_souvenir_graph
from daemon_core.memory.metadata import (
    parse_souvenir_frontmatter, preserve_existing_frontmatter, with_open_collection,
)
from daemon_core.memory.obsidian import prepare_souvenir_format
from daemon_core.memory_agent import _apply_memory_agent_result
from daemon_core.providers.base import LLMProvider


@pytest.mark.parametrize("link", [
    "[[capsules/theme/person/alex|Alex]]",
    "[[capsules/theme/person/alex#History|Alex's history]]",
    "[[capsules/theme/person/alex#^source-1]]",
    "[Alex](capsules/theme/person/alex.md)",
    "[Alex](../person/alex.md#History%20and%20sources)",
    "[Alex](<../person/alex.md#History>)",
    "[[../person/alex.md#History]]",
    "[[theme/person/alex]]",
])
def test_native_relation_formats_resolve_to_same_identity(link):
    record = parse_souvenir_graph(
        "theme/project/demo", "# Project\n\n## Liens proches\n- " + link + " : reviewer\n",
    )
    assert not record.issues
    assert [(r.target, r.description) for r in record.relations] == [
        ("theme/person/alex", "reviewer"),
    ]


@pytest.mark.parametrize("link", [
    "[Alex](../../../../outside.md)",
    "[Alex](%2e%2e/%2e%2e/%2e%2e/outside.md)",
    "[Alex](file:///private/memory.md)",
    "[Alex](https://example.org/alex.md)",
    "[[capsules/../../../outside]]",
    "[[capsules/theme/person/alex|bad\nlabel]]",
    "[Alex](../person/alex.md?unknown=1)",
])
def test_external_and_escaping_links_never_become_souvenir_relations(link):
    record = parse_souvenir_graph(
        "theme/project/demo", "# Project\n\n## Liens proches\n- " + link + "\n",
    )
    assert not record.relations


def test_typed_relation_preserves_kind_role_and_exact_source_with_display_labels():
    record = parse_souvenir_graph("theme/objects/demo", """# Object

## Relations
- member_of [Moving](../project/moving.md#Objects): retained object
  - source: [[levels/L0_ground_zero/conversations/synthetic#2026-01-01T10:00:00 — user|Original words]]
""")
    assert not record.issues
    relation = record.relations[0]
    assert (relation.kind, relation.target, relation.description) == (
        "member_of", "theme/project/moving", "retained object",
    )
    assert relation.source_refs == (
        "levels/L0_ground_zero/conversations/synthetic#2026-01-01T10:00:00 — user",
    )


def test_opt_in_render_changes_only_explicit_reference_and_preserves_code(monkeypatch):
    text = """# Project

The phrase theme/person/alex here is prose.

## Liens proches
- theme/person/alex: reviewer

~~~md
## Liens proches
- theme/person/example: just an example
~~~
"""
    monkeypatch.delenv("DAEMON_CORE_OBSIDIAN_FORMAT", raising=False)
    assert prepare_souvenir_format(text, "theme/project/demo") == text
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    rendered = prepare_souvenir_format(text, "theme/project/demo")
    assert rendered == text.replace(
        "- theme/person/alex: reviewer", "- [[capsules/theme/person/alex]]: reviewer",
    )
    assert prepare_souvenir_format(rendered, "theme/project/demo") == rendered


def test_flat_collection_round_trip_retains_coverage_and_provenance(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    ref = "levels/L0_ground_zero/conversations/synthetic#2026-01-01T10:00:00 — user"
    original = "---\ncollection:\n  coverage: open\n  source_refs: []\n---\n# Moving\n"
    rendered = with_open_collection(original, source_refs=[ref])
    fm, body = parse_souvenir_frontmatter(rendered)
    assert "collection" not in fm
    assert fm["collection_coverage"] == "open"
    assert fm["collection_source_refs"] == [f"[[{ref}]]"]
    assert body == "# Moving\n"
    assert with_open_collection(rendered, source_refs=[ref]) == rendered
    record = parse_souvenir_graph("theme/project/moving", rendered)
    assert not record.issues
    assert record.collection.coverage == "open"
    assert record.collection.source_refs == (ref,)


@pytest.mark.parametrize("metadata", [
    "collection:\n  coverage: open\n  unknown_field: preserve me\n",
    "collection:\n  coverage: open\ncollection_coverage: closed\n",
])
def test_unsupported_or_conflicting_collection_properties_are_not_discarded(monkeypatch, metadata):
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    with pytest.raises(ValueError):
        prepare_souvenir_format("---\n" + metadata + "---\n# Moving\n", "theme/project/moving")


def test_unknown_user_properties_win_over_generated_frontmatter(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    current = """---
aliases: [Old name, New name]
tags: [work/project]
cssclasses: [wide]
rating: 4
reviewed: false
custom_link: "[[capsules/theme/person/alex|Alex]]"
subjects: [Alex]
---
# Project
"""
    proposed = "---\naliases: [Invented name]\nrating: 1\nsubjects: [Alex, Sam]\n---\n# Project revised\n"
    merged = preserve_existing_frontmatter(current, proposed)
    before, _ = parse_souvenir_frontmatter(current)
    after, body = parse_souvenir_frontmatter(merged)
    assert {k: after[k] for k in before if k != "subjects"} == {
        k: v for k, v in before.items() if k != "subjects"
    }
    assert after["subjects"] == ["Alex", "Sam"]
    assert body == "# Project revised\n"


def test_native_date_time_keeps_iso_separator_and_precision(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    content = "---\nreviewed_at: 2026-10-10T09:30:00.123456+02:00\n---\n# Review\n"
    rendered = prepare_souvenir_format(content, "theme/project/demo")
    assert "2026-10-10T09:30:00.123456+02:00" in rendered
    assert parse_souvenir_frontmatter(rendered) == parse_souvenir_frontmatter(content)


def test_new_nested_discovery_property_is_refused_without_guessing(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    with pytest.raises(ValueError, match="nested"):
        prepare_souvenir_format("---\nsubjects:\n- person: Alex\n---\n# Demo\n", "theme/project/demo")


class MergeProvider(LLMProvider):
    def __init__(self, result, during_merge=lambda: None):
        self.result = result
        self.during_merge = during_merge

    def complete(self, messages, temperature=0.4):
        self.during_merge()
        return json.dumps({"content_md": self.result, "removed_existing_lines": [], "reason": "Synthetic merge."})


@pytest.fixture
def isolated_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    for module in ("daemon_core.memory_agent", "daemon_core.memory.reader",
                   "daemon_core.memory.writer", "daemon_core.dreaming"):
        monkeypatch.setattr(module + ".MEMORY_ROOT", tmp_path)
    path = tmp_path / "capsules/theme/project/demo.md"
    path.parent.mkdir(parents=True)
    return path


def apply_update(content, provider=None):
    return _apply_memory_agent_result(
        "synthetic-obsidian", {"capsule_updates": [{
            "name": "theme/project/demo", "content_md": content,
        }]}, user_message="Alex also reviews the project.", provider=provider,
    )


def test_canonical_writer_generates_native_links_then_preserves_user_properties(isolated_writer):
    note = "---\nsubjects: [Alex]\nfacets: [work]\n---\n# Demo\n\n## Faits\n- Initial fact.\n\n## Liens proches\n- theme/person/alex: reviewer\n"
    result = apply_update(note)
    assert result.updated_souvenirs == ["theme/project/demo"]
    assert "[[capsules/theme/person/alex]]" in isolated_writer.read_text()
    edited = isolated_writer.read_text().replace("subjects:", "aliases: [Demo]\ntags: [work/project]\nsubjects:")
    isolated_writer.write_text(edited)
    proposed = note.replace("- Initial fact.", "- Initial fact.\n- Alex reviews the project.")
    result = apply_update(proposed, MergeProvider(proposed))
    assert result.updated_souvenirs == ["theme/project/demo"]
    fm, body = parse_souvenir_frontmatter(isolated_writer.read_text())
    assert fm["aliases"] == ["Demo"]
    assert fm["tags"] == ["work/project"]
    assert "Alex reviews the project." in body
    assert "[[capsules/theme/person/alex]]" in body


@pytest.mark.parametrize("change", ["edit", "delete", "move"])
def test_external_edit_during_merge_is_never_overwritten(isolated_writer, change):
    initial = "# Demo\n\n## Faits\n- Initial fact.\n"
    apply_update(initial)
    moved = isolated_writer.with_name("renamed.md")
    def mutate():
        if change == "edit":
            isolated_writer.write_text(initial + "- Owner correction.\n")
        elif change == "delete":
            isolated_writer.unlink()
        else:
            isolated_writer.rename(moved)
    proposed = initial + "- Alex reviews the project.\n"
    result = apply_update(proposed, MergeProvider(proposed, mutate))
    assert result.updated_souvenirs == []
    assert result.skipped_souvenirs == ["theme/project/demo"]
    if change == "edit":
        assert isolated_writer.read_text() == initial + "- Owner correction.\n"
    else:
        assert not isolated_writer.exists()
        if change == "move":
            assert moved.read_text() == initial


def test_dreaming_keeps_owner_properties_and_native_links(isolated_writer):
    from daemon_core.dreaming import apply_dreaming_result
    current = "---\naliases: [Demo]\ntags: [work]\n---\n# Demo\n\n## Faits\n- Initial fact.\n"
    isolated_writer.write_text(current)
    proposed = "# Demo\n\n## Faits\n- Initial fact.\n- Alex reviews the project.\n\n## Liens proches\n- theme/person/alex: reviewer\n"
    result = apply_dreaming_result(user_id="synthetic-obsidian", dry_run=False, data={"updates": [{
        "path": "memory/capsules/theme/project/demo.md", "action": "update",
        "reason": "Synthetic maintenance.", "content_md": proposed,
    }]})
    assert not result.rejected_updates
    assert result.updates[0].applied
    fm, body = parse_souvenir_frontmatter(isolated_writer.read_text())
    assert fm["aliases"] == ["Demo"]
    assert fm["tags"] == ["work"]
    assert "[[capsules/theme/person/alex]]" in body


def test_restored_vault_keeps_graph_and_user_files(isolated_writer, tmp_path):
    apply_update("# Demo\n\n## Liens proches\n- theme/person/alex: reviewer\n")
    root = isolated_writer.parents[3]
    target = root / "capsules/theme/person/alex.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Alex\n")
    (root / "personal.canvas").write_text('{"nodes": [], "edges": []}')
    original = build_memory_graph(root / "capsules", memory_root=root)
    restored = tmp_path.parent / (tmp_path.name + "-restored")
    shutil.copytree(root, restored)
    rebuilt = build_memory_graph(restored / "capsules", memory_root=restored)
    assert rebuilt.index.as_dict() == original.index.as_dict()
    assert (restored / "personal.canvas").read_bytes() == (root / "personal.canvas").read_bytes()
    assert rebuilt.audit.valid_relation_count == 1
