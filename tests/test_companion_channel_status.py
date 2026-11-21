"""OpenClaw's Telegram state from its own gateway health answer (synthetic answers only)."""

import pytest

from daemons_companion.hosts.channel_status import dispatch, telegram_state


def health(**telegram):
    return {"ok": True, "channels": {"telegram": telegram}}


def test_a_ready_running_channel_is_connected():
    assert telegram_state(health(running=True, configured=True, lifecycle="ready"), True) == "connected"


def test_a_rejected_token_is_named_whatever_the_lifecycle():
    for status in (401, 404):
        answer = health(running=False, terminalDisconnect=True, probe={"ok": False, "status": status})
        assert telegram_state(answer, True) == "token-rejected"


def test_terminal_or_stopped_channels_are_disconnected():
    assert telegram_state(health(running=True, terminalDisconnect=True), True) == "disconnected"
    assert telegram_state(health(running=False, lifecycle="stopped"), True) == "disconnected"
    assert telegram_state({"ok": True, "channels": {}}, True) == "disconnected"


def test_a_starting_channel_is_connecting():
    assert telegram_state(health(running=False, lifecycle="starting"), True) == "connecting"


def test_no_answer_proves_nothing_and_no_telegram_means_none():
    assert telegram_state(None, True) == "unknown"
    assert telegram_state({"ok": False}, True) == "unknown"
    assert telegram_state(health(running=True), False) == "none"


def test_requests_outside_an_owned_openclaw_installation_are_refused(tmp_path):
    with pytest.raises(ValueError):
        dispatch({"operation": "channel-status", "source_profile": str(tmp_path), "base": str(tmp_path), "extra": 1})
    with pytest.raises((ValueError, OSError)):
        dispatch({"operation": "channel-status", "source_profile": str(tmp_path / "x"), "base": str(tmp_path / "y")})
