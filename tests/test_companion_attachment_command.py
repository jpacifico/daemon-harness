"""Channel ownership must be established before preparing a native consumer."""

import pytest
from daemons_companion.hosts.attachment_command import Refused, validate_channel

SOURCE = "123456:" + "a" * 35
TARGET = "987654:" + "b" * 35


def channel(**overrides):
    return {
        "botToken": TARGET,
        "allowFrom": ["777001"],
        "dmPolicy": "allowlist",
        **overrides,
    }


def test_distinct_literal_channel_or_private_env():
    validate_channel(channel(), {}, {"TELEGRAM_BOT_TOKEN": SOURCE}, "777001")
    validate_channel(
        channel(botToken=None),
        {"TELEGRAM_BOT_TOKEN": TARGET},
        {"TELEGRAM_BOT_TOKEN": SOURCE},
        "777001",
    )


@pytest.mark.parametrize(
    "config,source",
    [
        (channel(botToken=SOURCE), SOURCE),
        (channel(botToken="${TELEGRAM_BOT_TOKEN}"), SOURCE),
        (channel(tokenFile="/arbitrary"), SOURCE),
        (channel(accounts={"second": {"botToken": SOURCE}}), SOURCE),
        (channel(allowFrom=["*"]), SOURCE),
        (channel(allowFrom=["777001", "888002"]), SOURCE),
        (channel(dmPolicy="open"), SOURCE),
        (channel(enabled=False), SOURCE),
        (channel(), "${SOURCE_TOKEN}"),
        (channel(), None),
    ],
)
def test_ambiguous_or_shared_channel_refused(config, source):
    with pytest.raises(Refused):
        validate_channel(config, {}, {"TELEGRAM_BOT_TOKEN": source}, "777001")


@pytest.mark.parametrize("platform,target", [("darwin", "macos"), ("win32", "windows")])
def test_native_platform_and_scheduler_selection(monkeypatch, platform, target):
    from daemons_companion.hosts import attachment_command as command

    monkeypatch.setattr(command.sys, "platform", platform)
    assert command.native_target() == target
    backend = command.native_scheduler(target, "d" * 32)
    assert backend.target_os == target
    assert backend.job_namespace == "d" * 32


def test_unsupported_platform_fails_closed(monkeypatch):
    from daemons_companion.hosts import attachment_command as command

    monkeypatch.setattr(command.sys, "platform", "linux")
    with pytest.raises(Refused, match="platform"):
        command.native_target()
    with pytest.raises(Refused, match="platform"):
        command.native_scheduler("linux", "d" * 32)


def test_windows_registry_paths_match_extended_spelling_only(monkeypatch):
    from daemon_core import host_bindings

    monkeypatch.setattr(host_bindings.os, "name", "nt")
    assert host_bindings._same_location(
        "C:/Users/Test/memory", "\\\\?\\C:\\Users\\Test\\memory"
    )
    assert not host_bindings._same_location(
        "C:/Users/Test/memory", "C:/Users/Other/memory"
    )
