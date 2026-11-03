import json

from daemon_core.memory.metadata_maintenance import (
    apply_facet_merges,
    maybe_run_facet_maintenance,
)
from daemon_core.providers.base import LLMProvider


def _seed_memory(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    quotidien = memory_root / "capsules" / "theme" / "quotidien"
    quotidien.mkdir(parents=True)
    (quotidien / "sortie_vr.md").write_text(
        "---\nsubjects: [Alex, Sam]\nfacets: [sortie père-fils]\n---\n"
        "# Sortie VR\n\n- La base VR motive Sam.\n",
        encoding="utf-8",
    )
    (quotidien / "jeux.md").write_text(
        "---\nsubjects: [Alex, Sam]\nfacets: [loisirs partagés]\n---\n"
        "# Jeux\n\n- Sea of Thieves ensemble.\n",
        encoding="utf-8",
    )
    scene = (
        memory_root
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "08"
        / "scene.md"
    )
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "---\nsubjects: [Utilisateur]\nfacets: [sortie père-fils]\n---\n"
        "# Scène REM\n\n- Corps immuable.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr(
        "daemon_core.memory.metadata_maintenance.MEMORY_ROOT", memory_root
    )
    return memory_root


class MergingProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return json.dumps(
            {
                "merges": [
                    {
                        "from": "sortie père-fils",
                        "into": "loisirs partagés",
                        "reason": "Synonymes dans cette mémoire.",
                    }
                ]
            },
            ensure_ascii=False,
        )


def test_facet_merge_rewrites_frontmatter_and_keeps_body(tmp_path, monkeypatch):
    memory_root = _seed_memory(tmp_path, monkeypatch)
    souvenir = memory_root / "capsules" / "theme" / "quotidien" / "sortie_vr.md"
    scene = (
        memory_root
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "08"
        / "scene.md"
    )
    body_before = souvenir.read_text(encoding="utf-8").split("---\n", 2)[2]
    scene_before = scene.read_bytes()

    applied, skipped, backup_root = apply_facet_merges(
        [{"from": "sortie père-fils", "into": "loisirs partagés", "reason": "test"}]
    )

    assert applied == ["theme/quotidien/sortie_vr"]
    content = souvenir.read_text(encoding="utf-8")
    assert "loisirs partagés" in content
    assert "sortie père-fils" not in content
    assert content.split("---\n", 2)[2] == body_before
    assert backup_root is not None
    assert (backup_root / "theme/quotidien/sortie_vr.md").exists()
    assert "theme/quotidien/jeux" not in applied
    assert scene.read_bytes() == scene_before
    assert "theme/scenes_episodiques/2026/08/scene" not in applied


def test_maintenance_runs_once_then_waits_a_week(tmp_path, monkeypatch):
    _seed_memory(tmp_path, monkeypatch)

    first = maybe_run_facet_maintenance(provider=MergingProvider())
    assert first is not None
    assert first["applied"] == ["theme/quotidien/sortie_vr"]

    again = maybe_run_facet_maintenance(provider=MergingProvider())
    assert again is None
