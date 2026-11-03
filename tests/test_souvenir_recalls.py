import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.context_router import ContextRoute
from daemon_core.loop import select_souvenirs_for_route
from daemon_core.memory.writer import record_souvenir_recalls
from daemon_core.providers.base import LLMProvider


class ForbiddenProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise AssertionError("Aucun appel LLM attendu.")


def _patch_memory_root(tmp_path, monkeypatch) -> Path:
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    return memory_root


def test_record_souvenir_recalls_creates_then_increments(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)

    record_souvenir_recalls(["theme/famille/organisation"], when=datetime(2026, 7, 1))
    record_souvenir_recalls(["theme/famille/organisation", "theme/solo"], when=datetime(2026, 7, 2))

    content = (memory_root / "levels" / "capsule_recalls.md").read_text(encoding="utf-8")
    assert "- theme/famille/organisation: dernier rappel 2026-07-02 | rappels 2" in content
    assert "- theme/solo: dernier rappel 2026-07-02 | rappels 1" in content


def test_record_souvenir_recalls_ignores_empty_selection(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)

    assert record_souvenir_recalls([]) is None
    assert not (memory_root / "levels" / "capsule_recalls.md").exists()


def test_select_souvenirs_for_route_records_scene_recalls(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    theme_root = memory_root / "capsules" / "theme"
    theme_root.mkdir(parents=True)
    (theme_root / "solo.md").write_text("# Note isolée\n", encoding="utf-8")
    (memory_root / "capsules" / "index.md").write_text("- theme/solo: Note isolée\n", encoding="utf-8")

    route = ContextRoute(mode="contextual", context_depth="contextual", reason="Test.", candidate_souvenirs=["theme/solo"])
    selection = select_souvenirs_for_route(route=route, message="Question", provider=ForbiddenProvider())

    assert selection.selected == ["theme/solo"]
    content = (memory_root / "levels" / "capsule_recalls.md").read_text(encoding="utf-8")
    assert "- theme/solo: dernier rappel " in content
    assert "| rappels 1" in content


def _route_with_radar_final(complete: bool, souvenirs: list[str]) -> ContextRoute:
    return ContextRoute(
        mode="contextual",
        context_depth="contextual",
        reason="Test P1c.",
        candidate_souvenirs=["theme/current"],
        radar_final_souvenirs=souvenirs,
        radar_final_complete=complete,
        radar_final_reason="Couverture jugée entière.",
    )


def _memory_with_two_souvenirs(tmp_path, monkeypatch) -> Path:
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    theme_root = memory_root / "capsules" / "theme"
    theme_root.mkdir(parents=True)
    (theme_root / "current.md").write_text("# Sélection actuelle\n", encoding="utf-8")
    (theme_root / "shadow.md").write_text("# Proposition radar\n", encoding="utf-8")
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/current: Sélection actuelle\n- theme/shadow: Proposition radar\n",
        encoding="utf-8",
    )
    return memory_root


def test_radar_final_on_skips_selector_when_complete(tmp_path, monkeypatch):
    memory_root = _memory_with_two_souvenirs(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")

    selection = select_souvenirs_for_route(
        route=_route_with_radar_final(complete=True, souvenirs=["theme/shadow"]),
        message="Question",
        provider=ForbiddenProvider(),
    )

    assert selection.selected == ["theme/shadow"]
    assert selection.source == "radar_final"
    content = (memory_root / "levels" / "capsule_recalls.md").read_text(encoding="utf-8")
    assert "- theme/shadow: dernier rappel " in content


def test_radar_final_on_keeps_selector_when_incomplete(tmp_path, monkeypatch):
    _memory_with_two_souvenirs(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")

    selection = select_souvenirs_for_route(
        route=_route_with_radar_final(complete=False, souvenirs=["theme/shadow"]),
        message="Question",
        provider=ForbiddenProvider(),
    )

    assert selection.selected == ["theme/current"]


def test_radar_final_on_keeps_selector_when_proposal_empty(tmp_path, monkeypatch):
    _memory_with_two_souvenirs(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")

    selection = select_souvenirs_for_route(
        route=_route_with_radar_final(complete=True, souvenirs=[]),
        message="Question",
        provider=ForbiddenProvider(),
    )

    assert selection.selected == ["theme/current"]


def test_radar_final_selection_remains_shadow_only(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    theme_root = memory_root / "capsules" / "theme"
    theme_root.mkdir(parents=True)
    (theme_root / "current.md").write_text("# Sélection actuelle\n", encoding="utf-8")
    (theme_root / "shadow.md").write_text("# Proposition radar\n", encoding="utf-8")
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/current: Sélection actuelle\n- theme/shadow: Proposition radar\n",
        encoding="utf-8",
    )
    route = ContextRoute(
        mode="contextual",
        context_depth="contextual",
        reason="Test ombre.",
        candidate_souvenirs=["theme/current"],
        radar_final_souvenirs=["theme/shadow"],
    )

    selection = select_souvenirs_for_route(
        route=route,
        message="Question",
        provider=ForbiddenProvider(),
    )

    assert selection.selected == ["theme/current"]
