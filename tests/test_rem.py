import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.rem import (
    DEFAULT_BOOTSTRAP_HOURS,
    chronique_name,
    read_watermark,
    run_rem,
    write_watermark,
)
from daemon_core.providers.base import LLMProvider

NOW = datetime(2026, 7, 4, 21, 0)


def _patch_memory_root(tmp_path, monkeypatch) -> Path:
    memory_root = tmp_path / "memory"
    for module in ("daemon_core.memory.writer", "daemon_core.memory.reader", "daemon_core.dreaming", "daemon_core.rem"):
        monkeypatch.setattr(f"{module}.MEMORY_ROOT", memory_root)
    return memory_root


def _write_l0(memory_root: Path, user: str, *, repeat: int = 20) -> None:
    l0 = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True, exist_ok=True)
    body = "Enfin les vacances, la libération. " * repeat
    (l0 / f"{user}_2026-07-03.md").write_text(
        f"\n## 2026-07-03T20:00:00 — user\n\n{body}\n"
        "\n## 2026-07-03T20:00:10 — assistant\n\nTrois semaines devant toi.\n",
        encoding="utf-8",
    )


class RemProvider(LLMProvider):
    def __init__(self, index_line: str | None = None) -> None:
        self.messages = []
        self.index_line = index_line or "- theme/chroniques/x: libération"

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return json.dumps({
            "reason": "Période racontable.",
            "chronique_md": "# Chronique\n\nLa semaine de la lessiveuse : Paris, la canicule, puis vendredi soir, la libération.",
            "index_line": self.index_line,
        })


class ForbiddenProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise AssertionError("Le provider ne doit pas être appelé ici.")


def test_rem_writes_chronique_frontmatter_index_and_domain_line(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    span_start = datetime(2026, 6, 29, 0, 0)
    write_watermark("garden", span_start)
    _write_l0(memory_root, "garden")
    name = chronique_name(span_start)  # theme/chroniques/2026-06-29
    provider = RemProvider(index_line=f"- {name}: La semaine de la libération — Paris, canicule, restitution, vacances")
    narrator_calls = []
    monkeypatch.setattr(
        "daemon_core.rem.run_narrator",
        lambda **kwargs: narrator_calls.append(kwargs["user_id"])
        or "Il sort d'une période dense et entre en vacances.",
    )

    result = run_rem("garden", provider=provider, now=NOW, force=True)

    assert narrator_calls == ["garden"]
    assert result["status"] == "ecrite"
    chronique = memory_root / "capsules" / "theme" / "chroniques" / "2026-06-29.md"
    text = chronique.read_text(encoding="utf-8")
    assert "memory_type: episodic_archive" in text
    assert "truth_scope: past_only" in text
    assert "span_start: 2026-06-29T00:00:00" in text
    assert "span_end: 2026-07-04T21:00:00" in text
    assert "lessiveuse" in text
    index = (memory_root / "capsules" / "index.md").read_text(encoding="utf-8")
    assert f"- {name}:" in index
    assert "Paris, canicule" in index
    assert "- theme/chroniques: Past episodic archives" in index
    assert "2026-06-29 (La semaine de la libération" in index
    # Advance the watermark only after writing; it now points to now.
    assert read_watermark("garden", NOW) == NOW
    # The REM prompt sees L0 but not the Stance Register.
    assert "la libération" in provider.messages[1]["content"]
    assert "Il sort d'une période dense" in provider.messages[1]["content"]
    assert "Stance Register du daemon" not in provider.messages[1]["content"]


def test_rem_no_duplicate_when_span_already_written(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    span_start = datetime(2026, 6, 29, 0, 0)
    write_watermark("garden", span_start)
    _write_l0(memory_root, "garden")
    run_rem("garden", provider=RemProvider(), now=NOW, force=True)

    # Replay the SAME span after a simulated crash before watermark advancement:
    # the episode exists, so neither call the provider nor duplicate the note.
    write_watermark("garden", span_start)
    result = run_rem("garden", provider=ForbiddenProvider(), now=NOW)
    assert result["status"] == "deja-ecrite"
    files = list((memory_root / "capsules" / "theme" / "chroniques").glob("*.md"))
    assert len(files) == 1
    # Recovery aligns a watermark stuck at the span's beginning
    # with its end (span_end = NOW), allowing
    # future episodes to be written.
    assert read_watermark("garden", NOW) == NOW


def test_rem_repairs_missing_indexes_before_advancing_watermark(
    tmp_path,
    monkeypatch,
):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    span_start = datetime(2026, 6, 29, 0, 0)
    write_watermark("garden", span_start)
    name = chronique_name(span_start)
    chronique = (
        memory_root
        / "capsules"
        / "theme"
        / "chroniques"
        / "2026-06-29.md"
    )
    chronique.parent.mkdir(parents=True)
    chronique.write_text(
        "---\n"
        "memory_type: episodic_archive\n"
        "truth_scope: past_only\n"
        "span_start: 2026-06-29T00:00:00\n"
        "span_end: 2026-07-04T21:00:00\n"
        "---\n\n"
        "# Chronique réparée\n\n"
        "Une période mémorable.\n",
        encoding="utf-8",
    )

    result = run_rem(
        "garden",
        provider=ForbiddenProvider(),
        now=NOW,
    )

    assert result["status"] == "deja-ecrite"
    index = (
        memory_root / "capsules" / "index.md"
    ).read_text(encoding="utf-8")
    assert f"- {name}: Chronique réparée" in index
    assert "- theme/chroniques: Past episodic archives" in index
    assert read_watermark("garden", NOW) == NOW


def test_rem_fires_on_density(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    _write_l0(memory_root, "garden", repeat=1600)  # More than 50k characters: a dense episode.
    result = run_rem("garden", provider=RemProvider(), now=NOW)
    assert result["status"] == "ecrite"


def test_rem_fires_on_time_filet(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    write_watermark("garden", NOW - timedelta(days=11))  # Beyond the ten-day time fallback.
    _write_l0(memory_root, "garden", repeat=20)  # Low density, but elapsed time triggers the pass.
    result = run_rem("garden", provider=RemProvider(), now=NOW)
    assert result["status"] == "ecrite"


def test_rem_skips_immature_episode(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    write_watermark("garden", NOW - timedelta(days=2))  # Recent.
    _write_l0(memory_root, "garden", repeat=20)  # peu dense
    result = run_rem("garden", provider=ForbiddenProvider(), now=NOW)
    assert result["status"] == "pas-mur"


def test_rem_empty_is_silent(tmp_path, monkeypatch):
    _patch_memory_root(tmp_path, monkeypatch)
    result = run_rem("garden", provider=ForbiddenProvider(), now=NOW)
    assert result["status"] == "vide"


def test_rem_bootstrap_watermark_without_state_or_chronique(tmp_path, monkeypatch):
    _patch_memory_root(tmp_path, monkeypatch)
    assert read_watermark("garden", NOW) == NOW - timedelta(hours=DEFAULT_BOOTSTRAP_HOURS)


def test_rem_watermark_reconstructed_from_last_chronique(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    chron_dir = memory_root / "capsules" / "theme" / "chroniques"
    chron_dir.mkdir(parents=True)
    (chron_dir / "2026-06-29.md").write_text(
        "---\nmemory_type: episodic_archive\ntruth_scope: past_only\n"
        "span_start: 2026-06-29T00:00:00\nspan_end: 2026-07-06T00:00:00\n---\n\n# Chronique\n\nTexte.\n",
        encoding="utf-8",
    )
    # Without a state file, rebuild the watermark from span_end.
    assert read_watermark("garden", NOW) == datetime(2026, 7, 6, 0, 0)


def test_rem_prompt_frames_chroniques_as_past_archives(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    _write_l0(memory_root, "garden")
    provider = RemProvider()

    run_rem("garden", provider=provider, now=NOW, force=True)

    system = provider.messages[0]["content"]
    assert 'will still matter in a month' in system
    assert 'WHAT GOES' in system
    assert 'Episodic archive of the past' in system
    assert 'avoid times, tasks and current states' in system
    assert 'a memory is not a fact sheet' in system


def test_rem_does_not_truncate_chronique_output(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    _write_l0(memory_root, "garden")

    class LongProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            long_body = "# Chronique\n\n" + ("Une phrase complète sur la semaine. " * 120)
            long_body += "Dernière phrase complète."
            return json.dumps({
                "reason": "Période racontable.",
                "chronique_md": long_body,
                "index_line": "- theme/chroniques/2026-06-27: Chronique longue — libération, vacances",
            })

    result = run_rem("garden", provider=LongProvider(), now=NOW, force=True)

    assert result["status"] == "ecrite"
    span_start = NOW - timedelta(hours=DEFAULT_BOOTSTRAP_HOURS)
    chronique = memory_root / "capsules" / "theme" / "chroniques" / f"{span_start.date().isoformat()}.md"
    assert chronique.read_text(encoding="utf-8").rstrip().endswith("Dernière phrase complète.")
