"""The after-turn radar does not receive the flat index it never reads."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from daemon_core.context_router import ContextRoute, route_context_depth  # noqa: E402
from daemon_core.providers.base import LLMProvider  # noqa: E402


class _Capture(LLMProvider):
    def __init__(self):
        self.prompts = []

    def complete(self, messages, temperature=0.4):
        self.prompts.append([m["content"] for m in messages])
        return json.dumps({"context_depth": "light", "reason": "synthetic", "confidence": 0.9})


def _memory(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    leaf = root / "capsules" / "theme" / "family" / "sam_tastes.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("---\nsubjects: [Sam]\nfacets: [food]\n---\n# Sam's tastes\n", encoding="utf-8")
    (root / "capsules" / "index.md").write_text("- theme/family/sam_tastes: What Sam likes to eat\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", root)
    monkeypatch.setattr("daemon_core.context_router.current_time_context", lambda: "fixed time")


def test_profile_switch_still_governs_callers_that_read_the_proposal(tmp_path, monkeypatch):
    _memory(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")
    provider = _Capture()
    route_context_depth("What should I order for Sam?", provider)
    route_context_depth("What should I order for Sam?", provider, final_selection=True)
    system, user = provider.prompts[0]
    assert "final_selection_complete" in system
    assert "Flat index of souvenirs" in user
    assert provider.prompts[0] == provider.prompts[1]


def test_final_selection_false_sends_the_switch_off_prompt(tmp_path, monkeypatch):
    _memory(tmp_path, monkeypatch)
    provider = _Capture()
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")
    route_context_depth("What should I order for Sam?", provider, final_selection=False)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "off")
    route_context_depth("What should I order for Sam?", provider)
    without, switch_off = provider.prompts
    assert without == switch_off
    assert "final_selection_complete" not in without[0]
    assert "Flat index of souvenirs" not in without[1]
    assert "theme/family" in without[1]


def test_after_turn_radar_asks_for_no_final_selection(monkeypatch):
    from daemon_core.loop import DaemonCore

    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")
    seen = []

    def fake_route(**kwargs):
        seen.append(kwargs)
        return ContextRoute(mode="light", reason="synthetic")

    monkeypatch.setattr("daemon_core.loop.route_context_depth", fake_route)
    provider = _Capture()
    core = DaemonCore(provider=provider, memory_provider=provider, routing_provider=provider)
    core.update_memory_after_response(user_id="910000002", message="Hello", response="Hi")
    (call,) = seen
    assert call["final_selection"] is False
