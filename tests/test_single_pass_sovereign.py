from datetime import date
from pathlib import Path

from daemon_core.single_pass_sovereign import (
    load_active_memory_entries,
    load_searchable_memory_entries,
    read_single_pass_trace,
    write_single_pass_trace,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_active_entries_keep_exact_souvenirs_and_exclude_cold_episodes(tmp_path):
    _write(tmp_path / "capsules" / "index.md", "index should stay out")
    _write(tmp_path / "capsules" / "theme" / "zeta.md", "# Zeta\n\n- Exact Z.")
    _write(tmp_path / "capsules" / "theme" / "alpha.md", "# Alpha\n\n- Exact A.\n")
    _write(
        tmp_path / "capsules" / "theme" / "chroniques" / "2026-W01.md",
        "# Chronicle that should stay cold",
    )
    _write(
        tmp_path
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "08"
        / "scene.md",
        "# Episodic scene that should stay cold",
    )

    entries, expired = load_active_memory_entries(tmp_path)

    assert entries == [
        ("theme/alpha", "# Alpha\n\n- Exact A."),
        ("theme/zeta", "# Zeta\n\n- Exact Z."),
    ]
    assert expired == []


def test_past_archives_stay_out_of_active_entries_but_are_searchable(tmp_path):
    _write(
        tmp_path / "capsules" / "theme" / "active.md",
        "# Active\n\n- Fait courant.",
    )
    _write(
        tmp_path / "capsules" / "theme" / "chroniques" / "2026-W01.md",
        "---\ntruth_scope: past_only\n---\n\n# Semaine 1\n\n- Fait passé.",
    )
    _write(
        tmp_path
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "08"
        / "scene.md",
        "---\nmemory_type: episodic_scene\ntruth_scope: past_only\n---\n\n"
        "# Scène\n\n- Fait passé sourcé.",
    )

    active, _ = load_active_memory_entries(tmp_path)
    searchable, expired = load_searchable_memory_entries(tmp_path)

    assert active == [("theme/active", "# Active\n\n- Fait courant.")]
    assert [name for name, _content in searchable] == [
        "theme/active",
        "theme/chroniques/2026-W01",
        "theme/scenes_episodiques/2026/08/scene",
    ]
    assert expired == []


def test_active_entries_exclude_only_explicitly_expired_souvenirs(tmp_path):
    _write(
        tmp_path / "capsules" / "theme" / "expired.md",
        "# Voyage\nvalid_until: 2026-07-13\n\n- Situation terminée.",
    )
    _write(
        tmp_path / "capsules" / "theme" / "today.md",
        "# Aujourd'hui\nvalid_until: 2026-07-19\n\n- Encore valide aujourd'hui.",
    )
    _write(
        tmp_path / "capsules" / "theme" / "durable.md",
        "# Durable\n\n- Aucun terme.",
    )

    entries, expired = load_active_memory_entries(tmp_path, today=date(2026, 7, 19))

    assert [name for name, _ in entries] == ["theme/durable", "theme/today"]
    assert expired == ["theme/expired"]
    assert "Situation terminée" not in str(entries)
    assert "Encore valide aujourd'hui" in str(entries)


def test_single_pass_trace_roundtrip_rejects_mismatch_and_staleness(
    tmp_path,
    monkeypatch,
):
    import daemon_core.single_pass_sovereign as single_pass
    import daemon_core.turn_handoff as turn_handoff

    monkeypatch.setattr(turn_handoff, "MEMORY_ROOT", tmp_path)
    write_single_pass_trace("garden", "Bonjour", "trace-1", 123.5)

    assert read_single_pass_trace("garden", "Contexte\nBonjour") == (
        "Bonjour",
        "trace-1",
        123.5,
    )
    assert read_single_pass_trace("garden", "Autre message") is None

    now = turn_handoff.time.time()
    monkeypatch.setattr(
        turn_handoff.time,
        "time",
        lambda: now + single_pass.SINGLE_PASS_TRACE_MAX_AGE_SECONDS + 1,
    )
    assert read_single_pass_trace("garden", "Bonjour") is None
