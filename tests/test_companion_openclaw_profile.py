from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.openclaw.profile import (
    OPENCLAW_PROFILE_STEP,
    OpenClawProfileAdapter,
    OpenClawProfileLayout,
    create_disposable_openclaw_marker,
)
from daemons_companion.runtime.release import (
    RUNTIME_LAYOUT_STEP,
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


PROFILE_STEP = PlanStep(
    OPENCLAW_PROFILE_STEP,
    "Configure an isolated OpenClaw profile.",
    ("Context engine", "Minimal workspace", "Native memory exclusions"),
)
RUNTIME_STEP = PlanStep(
    RUNTIME_LAYOUT_STEP,
    "Install a synthetic private runtime.",
    ("Synthetic bundle",),
)


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "# synthetic\n",
        "requirements.txt": "# synthetic\n",
        "src/daemon_core/__init__.py": "\n",
        "src/daemons_companion/runtime/service_entry.py": "\n",
        "src/daemon_core/nightly_maintenance.py": "\n",
        "src/daemons_companion/runtime/services.py": "\n",
        "integrations/hermes_plugin/__init__.py": "\n",
        "integrations/hermes_voice_plugin/__init__.py": "\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/openclaw_plugin/openclaw.plugin.json": '{"id":"daemon-harness"}\n',
        "integrations/openclaw_plugin/package.json": '{"name":"openclaw-daemon-harness"}\n',
        "integrations/openclaw_plugin/workspace/AGENTS.md": "# Canonical OpenClaw workspace\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for relative in (
        "integrations/after_turn_service/service.sh",
        "integrations/dreaming_service/service.sh",
    ):
        (root / relative).chmod(0o755)
    return root


def _runtime(tmp_path: Path) -> CompanionRuntimeLayout:
    home = (tmp_path / "companion").absolute()
    bundle = _bundle((tmp_path / "bundle").absolute())
    create_disposable_companion_marker(home)
    layout = CompanionRuntimeLayout(home, bundle)
    adapter = CompanionRuntimeAdapter(layout)
    snapshot = (tmp_path / "runtime-snapshot").absolute()
    snapshot.mkdir()
    adapter.snapshot(RUNTIME_STEP, snapshot)
    adapter.apply(RUNTIME_STEP)
    adapter.verify(RUNTIME_STEP)
    return layout


def _layout(tmp_path: Path) -> OpenClawProfileLayout:
    state = (tmp_path / "openclaw").absolute()
    create_disposable_openclaw_marker(state)
    return OpenClawProfileLayout(_runtime(tmp_path), state)


def _plan() -> SetupPlan:
    return SetupPlan(
        selected_host=HostProbe(
            HostKind.OPENCLAW,
            True,
            True,
            "/synthetic/openclaw",
            "/synthetic/state",
            "OpenClaw synthetic",
            "verified",
        ),
        daemon_name="Aster",
        daemon_gender="masculine",
        provider={"kind": "synthetic", "settings": {}},
        steps=(PROFILE_STEP,),
    )


def _adapter(
    layout: OpenClawProfileLayout,
    adapter_type=OpenClawProfileAdapter,
    **kwargs,
) -> OpenClawProfileAdapter:
    return adapter_type(
        layout,
        selected_host=HostKind.OPENCLAW,
        user_id="synthetic-user",
        **kwargs,
    )


def _execute(
    tmp_path: Path,
    adapter: OpenClawProfileAdapter,
    transaction_id: str = "openclaw-profile-test",
):
    plan = _plan()
    return TransactionEngine(tmp_path / "transactions").execute(
        plan,
        adapter,
        transaction_id=transaction_id,
        confirmed_plan_digest=public_plan_digest(plan),
    )


def _tree(root: Path) -> dict[str, tuple[object, ...]]:
    result: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[relative] = ("dir", stat.S_IMODE(path.stat().st_mode))
        else:
            result[relative] = (
                "file",
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
    return result


class FailingAdapter(OpenClawProfileAdapter):
    def __init__(
        self,
        layout: OpenClawProfileLayout,
        *,
        fail_after: str,
        concurrent: bool = False,
    ) -> None:
        super().__init__(
            layout,
            selected_host=HostKind.OPENCLAW,
            user_id="synthetic-user",
        )
        self.fail_after = fail_after
        self.concurrent = concurrent

    def _after_mutation(self, artifact: str) -> None:
        if artifact != self.fail_after:
            return
        if self.concurrent:
            if artifact == "config":
                self.layout.config_path.write_text('{"thirdParty":true}\n', encoding="utf-8")
            else:
                (self.layout.workspace / "third-party.txt").write_text(
                    "keep me", encoding="utf-8"
                )
        raise RuntimeError("synthetic failure containing secret-token")


class InterruptingAdapter(OpenClawProfileAdapter):
    def __init__(self, layout: OpenClawProfileLayout, *, interrupt_after: str) -> None:
        super().__init__(
            layout,
            selected_host=HostKind.OPENCLAW,
            user_id="synthetic-user",
        )
        self.interrupt_after = interrupt_after

    def _after_mutation(self, artifact: str) -> None:
        if artifact == self.interrupt_after:
            raise KeyboardInterrupt("synthetic interruption")


def test_populated_profile_preserves_unmanaged_configuration(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    original = {
        "channels": {"telegram": {"token": "do-not-log", "enabled": True}},
        "mcp": {"servers": {"notes": {
            "url": "https://notes.example.invalid/mcp",
            "transport": "streamable-http",
            "auth": "oauth",
            "oauth": {"scope": "notes.read"},
            "toolFilter": {"include": ["read_*"], "exclude": ["write_*"]},
        }}},
        "plugins": {
            "allow": ["existing"],
            "entries": {
                "existing": {"enabled": True, "config": {"value": 7}},
                "codex": {"config": {"codexDynamicToolsLoading": "direct"}},
            },
            "load": {"paths": ["/existing/plugin"]},
            "slots": {"other": "existing"},
        },
        "tools": {
            "deny": ["existing-deny"],
            "alsoAllow": ["existing-tool", "browser"],
        },
        "browser": {"headless": False, "custom": "keep"},
        "unrelated": {"array": [3, 2, 1], "flag": True},
    }
    layout.config_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=4).replace("\n", "\r\n") + "\r\n",
        encoding="utf-8",
        newline="",
    )
    layout.config_path.chmod(0o640)
    result = _execute(tmp_path, _adapter(layout))

    assert result.status == "committed"
    raw = layout.config_path.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert stat.S_IMODE(layout.config_path.stat().st_mode) == 0o640
    config = json.loads(raw)
    assert config["channels"] == original["channels"]
    assert config["mcp"] == original["mcp"]
    assert config["unrelated"] == original["unrelated"]
    assert config["browser"] == {"headless": False, "custom": "keep"}
    assert config["plugins"]["entries"]["existing"] == original["plugins"]["entries"]["existing"]
    assert config["plugins"]["entries"]["codex"]["config"]["codexDynamicToolsLoading"] == "searchable"
    assert config["plugins"]["allow"] == ["existing", "browser"]
    assert config["tools"]["deny"] == ["existing-deny", "memory_search", "memory_get"]
    assert config["tools"]["alsoAllow"] == [
        "existing-tool",
        "browser",
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    ]
    assert config["plugins"]["slots"]["contextEngine"] == "daemon-harness"
    assert config["plugins"]["entries"]["daemon-harness"]["config"] == {
        "daemonUrl": "http://127.0.0.1:8000",
        "runtimeHealthPath": str(
            layout.runtime.state_root / "openclaw-runtime-health.json"
        ),
        "userId": "synthetic-user",
    }
    assert config["plugins"]["entries"]["daemon-harness"]["hooks"] == {
        "allowConversationAccess": True,
    }
    assert layout.workspace_agents.read_text(encoding="utf-8") == "# Canonical OpenClaw workspace\n"


def test_profile_accepts_only_explicit_non_privileged_loopback_url(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    OpenClawProfileAdapter(
        layout,
        selected_host=HostKind.OPENCLAW,
        user_id="synthetic-user",
        daemon_url="http://127.0.0.1:18000",
    )

    for invalid in (
        "http://localhost:18000",
        "https://127.0.0.1:18000",
        "http://127.0.0.1:80",
        "http://127.0.0.1:18000/path",
        "http://user@127.0.0.1:18000",
    ):
        with pytest.raises(ValueError, match="loopback"):
            OpenClawProfileAdapter(
                layout,
                selected_host=HostKind.OPENCLAW,
                user_id="synthetic-user",
                daemon_url=invalid,
            )


def test_absent_config_does_not_create_plugin_allowlist(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _execute(tmp_path, _adapter(layout))
    config = json.loads(layout.config_path.read_text(encoding="utf-8"))
    assert "allow" not in config["plugins"]
    assert config["browser"]["headless"] is True
    assert config["agents"]["defaults"]["workspace"] == str(layout.workspace)
    assert config["agents"]["defaults"]["skipBootstrap"] is True
    assert config["hooks"]["internal"]["entries"]["session-memory"]["enabled"] is False
    assert config["plugins"]["entries"]["memory-core"]["enabled"] is False
    assert config["skills"]["workshop"]["autonomous"]["mode"] == "off"


def test_second_transaction_is_byte_for_byte_idempotent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _execute(tmp_path, _adapter(layout), "first")
    before = _tree(layout.openclaw_state)
    _execute(tmp_path, _adapter(layout), "second")
    assert _tree(layout.openclaw_state) == before


@pytest.mark.parametrize(
    "artifact",
    ["workspace", "workspace_marker", "workspace_agents", "config"],
)
def test_failure_after_each_mutation_restores_profile(
    tmp_path: Path,
    artifact: str,
) -> None:
    layout = _layout(tmp_path)
    layout.config_path.write_text('{"unrelated":"keep"}\n', encoding="utf-8")
    before = _tree(layout.openclaw_state)
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, FailingAdapter(layout, fail_after=artifact), f"fail-{artifact}")
    assert _tree(layout.openclaw_state) == before


@pytest.mark.parametrize(
    "artifact",
    ["workspace", "workspace_marker", "workspace_agents", "config"],
)
def test_interruption_recovers_without_continuing(
    tmp_path: Path,
    artifact: str,
) -> None:
    layout = _layout(tmp_path)
    before = _tree(layout.openclaw_state)
    plan = _plan()
    engine = TransactionEngine(tmp_path / "transactions")
    transaction_id = f"interrupt-{artifact}"
    with pytest.raises(KeyboardInterrupt):
        engine.execute(
            plan,
            InterruptingAdapter(layout, interrupt_after=artifact),
            transaction_id=transaction_id,
            confirmed_plan_digest=public_plan_digest(plan),
        )
    with pytest.raises(TransactionRecoveryError):
        engine.execute(
            plan,
            _adapter(layout),
            transaction_id=transaction_id,
            confirmed_plan_digest=public_plan_digest(plan),
        )
    assert _tree(layout.openclaw_state) == before


@pytest.mark.parametrize("artifact", ["workspace_agents", "config"])
def test_concurrent_change_is_preserved_and_rollback_fails(
    tmp_path: Path,
    artifact: str,
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            FailingAdapter(layout, fail_after=artifact, concurrent=True),
            f"conflict-{artifact}",
        )
    journal = json.loads(
        (tmp_path / "transactions" / f"conflict-{artifact}" / "journal.json").read_text()
    )
    assert journal["status"] == "rollback_failed"
    if artifact == "config":
        assert json.loads(layout.config_path.read_text()) == {"thirdParty": True}
    else:
        assert (layout.workspace / "third-party.txt").read_text() == "keep me"


def test_marked_existing_workspace_preserves_unrelated_file(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.workspace.mkdir()
    layout.workspace_marker.write_text(
        "daemon-harness managed OpenClaw workspace v1\n", encoding="utf-8"
    )
    unrelated = layout.workspace / "operator-note.txt"
    unrelated.write_text("keep", encoding="utf-8")
    _execute(tmp_path, _adapter(layout))
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_unowned_existing_workspace_is_refused_before_write(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.workspace.mkdir()
    (layout.workspace / "personal.txt").write_text("keep", encoding="utf-8")
    before = _tree(layout.openclaw_state)
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, _adapter(layout))
    assert _tree(layout.openclaw_state) == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda layout: layout.config_path.symlink_to(layout.openclaw_state / "elsewhere"), "symbolic"),
        (lambda layout: layout.workspace.symlink_to(layout.openclaw_state / "elsewhere"), "symbolic"),
    ],
)
def test_symlinked_managed_paths_are_refused_before_write(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    layout = _layout(tmp_path)
    mutation(layout)
    before = _tree(layout.openclaw_state)
    with pytest.raises(TransactionApplyError, match="rolled back"):
        _execute(tmp_path, _adapter(layout))
    assert _tree(layout.openclaw_state) == before


def test_wrong_host_and_wrong_step_are_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError, match="requires the OpenClaw host"):
        OpenClawProfileAdapter(
            layout,
            selected_host=HostKind.HERMES,
            user_id="synthetic-user",
        )
    snapshot = (tmp_path / "snapshot").absolute()
    snapshot.mkdir()
    with pytest.raises(ValueError, match="only accepts"):
        _adapter(layout).snapshot(PlanStep("other", "other", ()), snapshot)


def test_duplicate_keys_and_non_array_managed_paths_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.config_path.write_text('{"tools":{},"tools":{}}\n', encoding="utf-8")
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, _adapter(layout), "duplicates")
    layout.config_path.write_text('{"tools":{"deny":"all"}}\n', encoding="utf-8")
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, _adapter(layout), "wrong-array")


def test_secret_content_stays_out_of_journal_and_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.config_path.write_text(
        '{"channels":{"telegram":{"token":"private-channel-token"}}}\n',
        encoding="utf-8",
    )
    with pytest.raises(TransactionApplyError) as caught:
        _execute(
            tmp_path,
            FailingAdapter(layout, fail_after="config"),
            "secret-boundary",
        )
    journal = (
        tmp_path / "transactions" / "secret-boundary" / "journal.json"
    ).read_text(encoding="utf-8")
    assert "private-channel-token" not in journal
    assert "secret-token" not in journal
    assert "private-channel-token" not in str(caught.value)
    assert "secret-token" not in str(caught.value)


def test_release_files_beyond_windows_max_path_are_read_through_extended_paths(
    tmp_path: Path, monkeypatch
) -> None:
    import daemons_companion.openclaw.profile as profile_module
    from daemons_companion.runtime import release as release_module
    from daemons_companion.runtime.release import verify_current_release
    from tests.companion_windows_io import WindowsPathGuard

    # The field OpenClaw release reaches 269 characters for its AGENTS.md.
    root = tmp_path.resolve() / ("p" * max(1, 160 - len(str(tmp_path.resolve()))))
    root.mkdir()
    layout = _layout(root)
    release = verify_current_release(layout.runtime)
    agents = release / "integrations/openclaw_plugin/workspace/AGENTS.md"
    assert len(str(agents)) >= 260
    guard = WindowsPathGuard(monkeypatch, release)
    guard.prefix = "/__extended_win32__"
    for module in (profile_module, release_module):
        monkeypatch.setattr(module, "_io_path", guard.io_path, raising=False)
    _execute(root, _adapter(layout))
    assert not guard.denied
    assert str(agents) in guard.extended
    assert layout.workspace_agents.read_bytes() == b"# Canonical OpenClaw workspace\n"


class _WindowsModes:
    """How Windows reports modes: 0o777 directories, 0o666 or 0o444 files."""

    def __getattr__(self, attribute):
        return getattr(stat, attribute)

    @staticmethod
    def S_IMODE(mode):
        if stat.S_ISDIR(mode):
            return 0o777
        return 0o666 if mode & 0o200 else 0o444


class _WindowsOs:
    name = "nt"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


@pytest.mark.parametrize("existing_config", [False, True])
def test_windows_mode_reports_neither_block_verification_nor_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_config: bool,
) -> None:
    # The field PC failed verification ("projection is incomplete"), then
    # refused rollback, because new files read 0o666 instead of 0o600.
    from daemons_companion.openclaw import profile

    layout = _layout(tmp_path)
    if existing_config:
        layout.config_path.write_text('{"unrelated":"keep"}\n', encoding="utf-8")
    monkeypatch.setattr(profile, "stat", _WindowsModes())
    monkeypatch.setattr(profile, "os", _WindowsOs())
    # Windows keeps no Unix modes: compare what exists and its content.
    def contents():
        return {name: value[:2] for name, value in _tree(layout.openclaw_state).items()}
    before = contents()
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, FailingAdapter(layout, fail_after="config"), "windows-rollback")
    assert contents() == before
    assert _execute(tmp_path, _adapter(layout), "windows-commit").status == "committed"
