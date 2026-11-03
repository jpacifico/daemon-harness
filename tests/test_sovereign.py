import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.context_router import ContextRoute, _parse_radar_route
from daemon_core.sovereign import (
    read_scene_verdict,
    read_scene_verdict_with_trace,
    write_scene_verdict,
)


def _patch_memory_root(tmp_path, monkeypatch) -> Path:
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.sovereign.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.persona.MEMORY_ROOT", memory_root)
    return memory_root


def test_radar_parses_needs_host_tools():
    raw = json.dumps({
        "context_depth": "light", "interaction_mode": "conversation",
        "scene_register": "pratique", "candidate_capsules": [],
        "active_request": "chercher les horaires du centre commercial",
        "ambiguity": "none", "needs_host_tools": True,
        "reason": "recherche web requise", "confidence": 0.9,
    })
    route = _parse_radar_route(raw, [])
    assert route.needs_host_tools is True
    # Missing means False, preserving historical behavior.
    raw_legacy = json.dumps({"context_depth": "light", "reason": "x"})
    assert _parse_radar_route(raw_legacy, []).needs_host_tools is False


def test_scene_verdict_roundtrip_with_polluted_prompt_text(tmp_path, monkeypatch):
    _patch_memory_root(tmp_path, monkeypatch)
    route = ContextRoute(mode="fast", reason="scène simple", context_depth="light")
    write_scene_verdict(
        "garden",
        "Salut, bien dormi ?",
        route,
        ["theme/loisirs/musique"],
        trace_id="trace-roundtrip",
        prefetch_started_at=123.5,
    )

    # The LLM request includes injected context around the user message.
    polluted = '# Personal context\nblabla\n\nSalut, bien dormi ?\n\n[fin]'
    verdict = read_scene_verdict("garden", polluted)

    assert verdict is not None
    message, restored, selected = verdict
    assert message == "Salut, bien dormi ?"
    assert restored.context_depth == "light"
    assert selected == ["theme/loisirs/musique"]
    traced = read_scene_verdict_with_trace("garden", polluted)
    assert traced is not None
    assert traced[3:] == ("trace-roundtrip", 123.5)


def test_scene_verdict_rejects_stale_or_mismatched(tmp_path, monkeypatch):
    _patch_memory_root(tmp_path, monkeypatch)
    route = ContextRoute(mode="fast", reason="x")
    write_scene_verdict("garden", "Message A", route, [])
    assert read_scene_verdict("garden", "un tout autre message") is None
    monkeypatch.setattr("daemon_core.sovereign.time", type("T", (), {"time": staticmethod(lambda: time.time() + 999)}))
    assert read_scene_verdict("garden", "Message A") is None


def test_radar_parses_fil_clos():
    raw = json.dumps({"context_depth": "minimal", "fil_clos": True, "reason": "acquiescement final"})
    assert _parse_radar_route(raw, []).fil_clos is True
    assert _parse_radar_route(json.dumps({"context_depth": "minimal", "reason": "x"}), []).fil_clos is False


def test_scene_verdict_has_no_closure_counter(tmp_path, monkeypatch):
    # Hysteresis stays removed: the verdict carries only the fil_clos judgment,
    # without creating a counter or choosing the response faculty.
    import json
    from daemon_core.sovereign import _verdict_path
    _patch_memory_root(tmp_path, monkeypatch)
    route = ContextRoute(mode="fast", reason="clôture", context_depth="light", fil_clos=True)
    write_scene_verdict("garden", "Oui", route, [])
    data = json.loads(_verdict_path("garden").read_text(encoding="utf-8"))
    assert "consecutive_closures" not in data
    assert data["route"]["fil_clos"] is True



def test_gate_keeps_focus_and_agenda_at_light():
    # No minimal depth: current dialogue and agenda accompany all turns.
    # Orientation is not a performed role.
    from daemon_core.loop import gate_scene_memories
    route = ContextRoute(mode="fast", reason="bref", context_depth="light")
    traj, stance_register, active, focus, agenda = gate_scene_memories(
        route,
        trajectory="Il entre en vacances.",
        stance_register="- un pari.",
        active_context="La semaine est caniculaire à Lyon.",
        immediate_focus="Sujet courant : pas sortis cet après-midi.",
        user_agenda="Sam ping-pong mercredi.",
    )
    assert active == "La semaine est caniculaire à Lyon."
    assert focus == "Sujet courant : pas sortis cet après-midi."
    assert agenda == "Sam ping-pong mercredi."
    # The Stance Register is suspended; the radar decides whether to use trajectory.
    assert stance_register == "" and traj == ""



def test_gate_light_keeps_everything_ungated():
    from daemon_core.loop import gate_scene_memories
    route = ContextRoute(mode="fast", reason="continuation", context_depth="light")
    traj, stance_register, active, focus, agenda = gate_scene_memories(
        route, trajectory="T", stance_register="C",
        active_context="A", immediate_focus="F", user_agenda="AG",
    )
    # Light depth includes focus and agenda. The Stance Register stays suspended;
    # trajectory remains the radar's decision.
    assert active == "A" and focus == "F" and agenda == "AG"
    assert stance_register == "" and traj == ""
