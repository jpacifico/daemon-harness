"""Radar replay: the journal is cut at the daemon's reply, and the copy is the only root."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/replay_radar_turn.py"
JOURNAL = """# Conversation

## 2026-10-30T18:18:34 — user

Gisborne, quel méchant.

## 2026-10-30T18:19:20 — user

Demain grasse mat :)

## 2026-10-30T18:19:28 — assistant

Profitez-en !

## 2026-10-30T18:20:05 — user

Autre chose.
"""


@pytest.fixture
def replay():
    spec = importlib.util.spec_from_file_location("replay_radar_turn", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_window_stops_at_the_reply_and_names_its_message(replay):
    window, message = replay.turn_through(JOURNAL, "2026-10-30T18:19:28")
    assert message == "Demain grasse mat :)"
    assert window.rstrip().endswith("Profitez-en !") and "Autre chose" not in window


def test_a_missing_or_orphan_reply_is_refused(replay):
    with pytest.raises(ValueError):
        replay.turn_through(JOURNAL, "2026-10-30T18:20:05")
    orphan = "## 2026-10-30T18:00:00 — assistant\n\nBonjour.\n"
    with pytest.raises(ValueError):
        replay.turn_through(orphan, "2026-10-30T18:00:00")


def test_the_diagnostic_window_stops_before_the_latest_exchange(replay):
    window, _ = replay.turn_through(JOURNAL, "2026-10-30T18:19:28")
    entries = list(replay.ENTRY.finditer(window))
    cut = window[:entries[-2].start()]
    assert "Gisborne" in cut and "grasse mat" not in cut and "Profitez-en" not in cut


def test_a_subscription_model_replaces_the_routing_provider(replay):
    provider = replay.routing_provider("gpt-6-astra")
    assert type(provider).__name__ == "CodexCliProvider"
    assert provider.model == "gpt-6-astra" and provider.reasoning_effort == "medium"
