"""Whole first OpenClaw transaction at the field Windows path lengths.

The owner's PC places the daemon home at
C:\\Users\\Alice\\AppData\\Local\\DaemonHarness\\Companion\\instance-k7mqzt\\hosts\\openclaw\\installations\\<32 hex>\\daemon
(137 characters). Every ordinary file operation whose field-equivalent path
reaches MAX_PATH is refused, as Windows does without long-path support. The
profile step also sees Windows mode reports (0o666 files, 0o777 directories).
"""
import json
import os
import sys
from dataclasses import replace

import pytest

from daemon_core import platform_support
from daemons_companion.runtime.installation_scope import InstallationScope
from tests.companion_windows_io import WindowsPathGuard

FIELD_HOME = 137
MAX_PATH = 260


class FieldGuard(WindowsPathGuard):
    prefix = "/__extended_win32__"

    def __init__(self, monkeypatch, root, offset):
        self.offset = offset
        super().__init__(monkeypatch, root)

    def translate(self, path):
        if isinstance(path, int):
            return path
        value = os.fspath(path)
        if not isinstance(value, str):
            return value
        if value == self.prefix:
            return "/"
        extended = value.startswith(self.prefix + "/")
        plain = value[len(self.prefix):] if extended else value
        if plain.startswith(self.root):
            if extended:
                self.extended.append(plain)
            elif len(plain) - self.offset >= MAX_PATH:
                self.denied.append(plain)
                raise FileNotFoundError(2, "Win32 MAX_PATH guard (simulated)", plain)
        return plain


class _WindowsOs:
    """The os module as daemon_core's atomic writer sees it on Windows."""

    name = "nt"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


def _windows_atomic_writes(monkeypatch, guard):
    # On Windows the temporary name carries 32 hex digits and is created
    # through the extended namespace; its rename uses ordinary paths.
    from daemon_core.memory import integrity

    def create(path):
        return os.open(guard.io_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    monkeypatch.setattr(integrity, "os", _WindowsOs())
    monkeypatch.setattr(integrity, "create_private_file", create)


def _extend_everywhere(monkeypatch, guard):
    original = platform_support.io_path
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if not name.startswith(("daemons_companion", "daemon_core")):
            continue
        for attribute, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, attribute, guard.io_path)


@pytest.mark.parametrize("subscription", [False, True])
def test_first_openclaw_transaction_fits_the_field_windows_paths(tmp_path, monkeypatch, subscription):
    from tests.test_companion_openclaw_first import owned_host
    from tests.test_companion_openclaw_ready_composition import _fixture, FakeScheduler, FakeControl
    from daemons_companion.openclaw.host_install import OPENCLAW_VERSION
    from daemons_companion.openclaw.ready_composition import (
        OpenClawReadyCompositionAdapter,
        build_dormant_openclaw_ready_plan,
    )
    from daemons_companion.runtime.release import CompanionRuntimeLayout
    from daemons_companion.setup.plan import HostKind, HostProbe, SetupRequest, build_setup_plan
    from daemons_companion.setup.transaction import TransactionEngine, public_plan_digest

    host = owned_host(tmp_path / "instance-k7mqzt", "windows")
    scope = InstallationScope(host.attempt)
    scope.initialize()
    (host.attempt / "connection.json").write_text(json.dumps(dict(
        schema="daemon.private-connection.v2", provider_kind="openai_api", user_id="777001")))
    (host.profile / "openclaw.json").write_text('{"gateway":{"mode":"local"}}\n')
    layout, _, selection, store, runner = _fixture(tmp_path / "fixture", "windows")
    runtime = CompanionRuntimeLayout(scope.home, layout.runtime.bundle_root, scope)
    runtime.memory_root.mkdir(parents=True)
    layout = replace(layout, runtime=runtime, openclaw_state=host.profile,
                     job_namespace=scope.installation_id)
    if subscription:
        import hashlib
        from daemons_companion.setup.plan import ProviderSelection, ProviderKind
        selection = ProviderSelection(ProviderKind.CODEX_CLI, "gpt-6-astra")
        (host.attempt / "os-home/.codex").mkdir()
        binary = host.attempt / "codex"
        binary.write_bytes(b"synthetic codex")
        (host.attempt / "connection.json").write_text(json.dumps(dict(
            schema="daemon.private-connection.v1", codex=str(binary),
            codex_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), user_id="777001")))
    reviewed = build_setup_plan(
        SetupRequest(HostKind.OPENCLAW, "Aster", "feminine", selection),
        (HostProbe(HostKind.OPENCLAW, True, True, str(host.entry), str(host.profile),
                   OPENCLAW_VERSION, "verified"),),
        codex_available=subscription,
    )
    prepared = build_dormant_openclaw_ready_plan(reviewed, layout)
    adapter = OpenClawReadyCompositionAdapter(
        layout, plan=prepared, provider_selection=selection, credential_store=store,
        credential_value=None if subscription else "synthetic-secret",
        codex_available=subscription, user_id="777001",
        scheduler=FakeScheduler("windows"), control=FakeControl(host.profile), runner=runner,
        readiness_probe=lambda *_: True, api_probe=lambda *_: True,
    )

    guard = FieldGuard(monkeypatch, str(tmp_path), len(str(scope.home)) - FIELD_HOME)
    _extend_everywhere(monkeypatch, guard)
    _windows_atomic_writes(monkeypatch, guard)
    # Windows mode reports: the profile step compares them (field c20 failure).
    from daemons_companion.openclaw import profile
    from tests.test_companion_openclaw_profile import _WindowsModes
    monkeypatch.setattr(profile, "stat", _WindowsModes())
    monkeypatch.setattr(profile, "os", _WindowsOs())
    result = TransactionEngine(scope.home / "transactions").execute(
        prepared, adapter, transaction_id="7445bb0125834c8881aa455b0c49b397",
        confirmed_plan_digest=public_plan_digest(prepared),
    )
    assert result.status == "committed"
    assert not guard.denied
