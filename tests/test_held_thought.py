import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.held_thought import (
    scene_scope_from_origin,
    shared_world_state_enabled,
)


@pytest.mark.parametrize(
    "value,enabled",
    [(None, False), ("", False), ("off", False), ("shadow", False),
     ("1", True), ("true", True), ("yes", True), ("on", True),
     ("active", True), ("canary", True), (" CANARY ", True)],
)
def test_shared_scene_flag_keeps_its_exact_activation_contract(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("DAEMON_CORE_SHARED_WORLD_STATE", raising=False)
    else:
        monkeypatch.setenv("DAEMON_CORE_SHARED_WORLD_STATE", value)
    assert shared_world_state_enabled() is enabled
    origin = {"platform": "telegram", "chat_id": "42", "thread_id": "7"}
    assert scene_scope_from_origin(origin) == ("world" if enabled else "telegram__42__7")


def test_scene_scope_prefers_stable_origin_over_session():
    assert scene_scope_from_origin(
        {"platform": "telegram", "chat_id": "42", "thread_id": "7"},
        session_id="ephemeral",
    ) == "telegram__42__7"
    assert scene_scope_from_origin({}, session_id="abc") == "session__abc"
    assert scene_scope_from_origin({}) == "primary"


def test_shared_world_scope_is_host_and_session_neutral(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SHARED_WORLD_STATE", "canary")

    assert scene_scope_from_origin(
        {"platform": "telegram", "chat_id": "42", "thread_id": "7"},
        session_id="hermes-session",
    ) == "world"
    assert scene_scope_from_origin(
        {"platform": "openclaw", "chat_id": "42"},
        session_id="openclaw-session",
    ) == "world"
    assert scene_scope_from_origin({}, session_id="fallback-session") == "world"
