import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from daemon_core.memory.metadata import (
    SouvenirMetadata,
    parse_souvenir_frontmatter,
    parse_souvenir_metadata,
    preserve_existing_frontmatter,
    with_souvenir_metadata,
)
from daemon_core.memory.reader import (
    souvenir_facet_projection,
    souvenir_subject_projection,
    list_souvenirs_for_metadata,
    load_souvenir_structural_catalog,
    load_souvenir_projection_map,
    load_souvenir_metadata_label_catalog,
    load_metadata_souvenir_index,
)


def test_frontmatter_parser_ignores_indented_fence_inside_yaml_scalar():
    content = """---
schema_version: 3
sources:
- excerpt: '---

    subjects:

    - Alex

    ---

    # Capsule'
---
# Scene
"""

    frontmatter, body = parse_souvenir_frontmatter(content)

    assert frontmatter["schema_version"] == 3
    assert frontmatter["sources"][0]["excerpt"].startswith("---")
    assert body == "# Scene\n"


def test_metadata_can_be_added_without_changing_markdown_body():
    content = "# Capsule\n\n## Faits\n- Sam aime les pâtes.\n"

    updated = with_souvenir_metadata(
        content,
        SouvenirMetadata(
            subjects=["Sam"],
            facets=["goûts alimentaires", "préférences personnelles"],
        ),
    )
    frontmatter, body = parse_souvenir_frontmatter(updated)

    assert body == content
    assert frontmatter["subjects"] == ["Sam"]
    assert frontmatter["facets"] == [
        "goûts alimentaires",
        "préférences personnelles",
    ]


def test_metadata_merge_preserves_existing_chronicle_fields_and_body():
    content = """---
memory_type: episodic_archive
truth_scope: past_only
span_start: 2026-06-15T00:00:00
---

# Chronique

Texte vécu.
"""
    _frontmatter, original_body = parse_souvenir_frontmatter(content)

    updated = with_souvenir_metadata(
        content,
        SouvenirMetadata(subjects=["Alex", "Sam"], facets=["souvenir vécu"]),
    )
    frontmatter, body = parse_souvenir_frontmatter(updated)

    assert frontmatter["memory_type"] == "episodic_archive"
    assert frontmatter["truth_scope"] == "past_only"
    assert str(frontmatter["span_start"]).startswith("2026-06-15")
    assert frontmatter["subjects"] == ["Alex", "Sam"]
    assert body == original_body


def test_metadata_parser_cleans_duplicates_and_invalid_values():
    content = """---
subjects: [Sam, " sam ", 12]
facets: [lecture, " lecture ", histoire]
---
# Capsule
"""

    metadata = parse_souvenir_metadata(content)

    assert metadata.subjects == ["Sam"]
    assert metadata.facets == ["lecture", "histoire"]


def test_malformed_frontmatter_is_refused_before_any_rewrite():
    with pytest.raises(ValueError, match="Unclosed YAML frontmatter"):
        with_souvenir_metadata(
            "---\nsubjects: [Sam]\n# Capsule\n",
            SouvenirMetadata(subjects=["Sam"]),
        )


def test_structural_catalog_keeps_invalid_souvenir_as_unqualified_path(
    tmp_path,
    monkeypatch,
    caplog,
):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules" / "theme"
    souvenir_root.mkdir(parents=True)
    malformed = "---\nsubjects:\n  - @claycreations\n---\n# Sculpture\n"
    malformed_path = souvenir_root / "sculpture.md"
    malformed_path.write_text(malformed, encoding="utf-8")
    (souvenir_root / "lecture.md").write_text(
        "---\nsubjects: [Sam]\nfacets: [lecture]\n---\n# Lecture\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    catalog = load_souvenir_structural_catalog()

    assert "- theme/lecture | subjects: Sam | facets: lecture" in catalog
    assert "- theme/sculpture" in catalog
    assert "@claycreations" not in catalog
    assert malformed_path.read_text(encoding="utf-8") == malformed
    assert "frontmatter invalide" in caplog.text


def test_existing_frontmatter_survives_a_full_content_proposal():
    current = """---
subjects: [Sam]
facets: [lecture]
truth_scope: durable
---
# Ancien corps
"""
    proposed = "# Nouveau corps\n\n- Information ajoutée.\n"

    merged = preserve_existing_frontmatter(current, proposed)
    frontmatter, body = parse_souvenir_frontmatter(merged)

    assert frontmatter == {
        "subjects": ["Sam"],
        "facets": ["lecture"],
        "truth_scope": "durable",
    }
    assert body == proposed


def test_subject_and_facet_projections_are_regenerated_from_frontmatter(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules" / "theme"
    souvenir_root.mkdir(parents=True)
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/gouts_sam: Goûts alimentaires de Sam\n"
        "- theme/lectures_partagees: Lectures partagées\n"
        "- theme/legacy: Souvenir sans métadonnées\n",
        encoding="utf-8",
    )
    (souvenir_root / "gouts_sam.md").write_text(
        "---\nsubjects: [Sam]\nfacets: [goûts alimentaires]\n---\n"
        "# Goûts de Sam\n",
        encoding="utf-8",
    )
    (souvenir_root / "lectures_partagees.md").write_text(
        "---\nsubjects: [Alex, Sam]\nfacets: [lecture]\n---\n"
        "# Lectures partagées\n",
        encoding="utf-8",
    )
    (souvenir_root / "legacy.md").write_text(
        "# Capsule legacy\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    assert souvenir_subject_projection() == {
        "Alex": ["theme/lectures_partagees"],
        "Sam": ["theme/gouts_sam", "theme/lectures_partagees"],
    }
    assert souvenir_facet_projection() == {
        "goûts alimentaires": ["theme/gouts_sam"],
        "lecture": ["theme/lectures_partagees"],
    }
    assert list_souvenirs_for_metadata(subjects=["sam"]) == [
        "theme/gouts_sam",
        "theme/lectures_partagees",
    ]
    assert load_metadata_souvenir_index(facets=["lecture"]) == (
        "- theme/lectures_partagees: Lectures partagées"
    )
    projection = load_souvenir_projection_map()
    assert "## Sujets" in projection
    assert "- Sam: theme/gouts_sam, theme/lectures_partagees" in projection
    assert "theme/legacy" not in projection
    labels = load_souvenir_metadata_label_catalog()
    assert "- Sam (2)" in labels
    assert "- lecture (1)" in labels
