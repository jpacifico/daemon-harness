import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datetime import datetime
from zoneinfo import ZoneInfo

from daemon_core.prompt_composer import build_scene_section, current_time_context


def test_time_context_uses_paris_timezone():
    value = current_time_context(datetime(2026, 4, 26, 9, 30, tzinfo=ZoneInfo("Europe/Paris")))
    assert "2026" in value
    assert "09:30" in value
    assert "Europe/Paris" in value


def test_compatibility_scene_section_is_empty_without_material():
    assert build_scene_section("", "none") == ""


def test_compatibility_scene_preserves_evidence_uncertainty_and_closure():
    scene = build_scene_section(
        "Choisir le jour", "material", "La présence reste inconnue",
        fil_clos=True,
        scene_established=["Le lieu est réservé"],
        scene_uncertain=["La présence n'est pas confirmée"],
        scene_decision_effect="Le jour dépend de cette présence",
        scene_resolution_mode="clarify",
    )
    assert "Active request: Choisir le jour" in scene
    assert "Material ambiguity: yes" in scene
    assert "Useful established elements: Le lieu est réservé" in scene
    assert "Elements to keep uncertain: La présence n'est pas confirmée" in scene
    assert "Resolution strategy: clarify" in scene
    assert "not a source of facts" in scene
    assert "leave room for silence" in scene
