from datetime import date
from pathlib import Path

from daemon_core.sovereign_library import (
    isolated_active_memory_library,
    read_exact_hot_memory,
)


def _memory_root(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    theme = root / "capsules" / "theme"
    (theme / "chroniques").mkdir(parents=True)
    (theme / "active.md").write_text(
        "---\nsubjects:\n- Alex\nfacets:\n- repas\n---\n# Active\n\nSecret exact.",
        encoding="utf-8",
    )
    (theme / "chroniques" / "old.md").write_text("# Retrospective", encoding="utf-8")
    (theme / "expired.md").write_text(
        "---\nvalid_until: 2026-07-01\n---\n# Expired",
        encoding="utf-8",
    )
    return root


def test_isolated_library_contains_only_exact_active_souvenirs(tmp_path):
    memory_root = _memory_root(tmp_path)

    with isolated_active_memory_library(
        memory_root,
        today=date(2026, 7, 19),
    ) as library:
        assert library.souvenir_names == ("theme/active",)
        assert library.expired_souvenir_names == ("theme/expired",)
        assert (library.root / "theme" / "active.md").read_text(encoding="utf-8") == (
            "---\nsubjects:\n- Alex\nfacets:\n- repas\n---\n# Active\n\nSecret exact.\n"
        )
        assert library.routing_metadata == (
            ("theme/active", ("Alex",), ("repas",)),
        )
        assert not (library.root / "theme" / "chroniques").exists()


def test_hot_memory_is_exact_and_bounded(tmp_path):
    memory_root = _memory_root(tmp_path)
    with isolated_active_memory_library(memory_root, today=date(2026, 7, 19)) as library:
        hot = read_exact_hot_memory(
            library,
            ["theme/active", "theme/active", "../outside"],
        )
        assert hot.souvenir_names == ("theme/active",)
        assert hot.rejected_paths == ("../outside",)
        assert hot.text == (
            "## [[theme/active]]\n\n---\nsubjects:\n- Alex\nfacets:\n- repas\n"
            "---\n# Active\n\nSecret exact."
        )
