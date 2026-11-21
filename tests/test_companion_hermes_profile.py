from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest
import yaml

from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    PlanStep,
    ProviderKind,
    ProviderSelection,
    SetupPlan,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.hermes.profile import (
    HERMES_CONFIG_VALUES,
    HERMES_PROFILE_STEP,
    HermesProfileAdapter,
    HermesProfileLayout,
    create_disposable_profile_marker,
)
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_STEP = PlanStep(
    HERMES_PROFILE_STEP,
    "Configure an isolated Hermes profile.",
    ("Hermes scalar settings", "Daemon environment", "Plugin links"),
)


def _plan() -> SetupPlan:
    return SetupPlan(
        selected_host=HostProbe(
            HostKind.HERMES,
            detected=True,
            usable=True,
            executable="/synthetic/hermes",
            version="synthetic",
            version_status="verified",
        ),
        daemon_name="Iris",
        daemon_gender="feminine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(PROFILE_STEP,),
    )


def _layout(tmp_path: Path) -> HermesProfileLayout:
    hermes_home = (tmp_path / "home" / ".hermes").absolute()
    create_disposable_profile_marker(hermes_home)
    return HermesProfileLayout(hermes_home, REPOSITORY_ROOT.absolute())


def _adapter(
    layout: HermesProfileLayout,
    adapter_type=HermesProfileAdapter,
    **kwargs,
):
    return adapter_type(
        layout,
        selected_host=HostKind.HERMES,
        user_id="synthetic-user",
        **kwargs,
    )


def _execute(
    engine: TransactionEngine,
    adapter: HermesProfileAdapter,
    *,
    transaction_id: str = "hermes-profile-test",
):
    plan = _plan()
    return engine.execute(
        plan,
        adapter,
        transaction_id=transaction_id,
        confirmed_plan_digest=public_plan_digest(plan),
    )


def _tree_state(root: Path) -> dict[str, tuple[object, ...]]:
    state: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[relative] = ("directory", stat.S_IMODE(path.stat().st_mode))
        else:
            state[relative] = (
                "file",
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
    return state


def _mapping_value(mapping: dict[str, object], dotted: str) -> object:
    current: object = mapping
    for part in dotted.split("."):
        assert isinstance(current, dict)
        current = current[part]
    return current


class FailingAdapter(HermesProfileAdapter):
    def __init__(
        self,
        layout: HermesProfileLayout,
        *,
        fail_after: str,
        concurrent_env_change: bool = False,
    ) -> None:
        super().__init__(
            layout,
            selected_host=HostKind.HERMES,
            user_id="synthetic-user",
        )
        self.fail_after = fail_after
        self.concurrent_env_change = concurrent_env_change

    def _after_mutation(self, artifact: str) -> None:
        if artifact != self.fail_after:
            return
        if self.concurrent_env_change:
            with self.layout.env_path.open("ab") as stream:
                stream.write(b"THIRD_PARTY_CHANGE=keep-me\n")
        raise RuntimeError("synthetic adapter failure containing secret-value")


class PluginDirectoryConflictAdapter(HermesProfileAdapter):
    def _after_mutation(self, artifact: str) -> None:
        if artifact != "memory_plugin":
            return
        (self.layout.plugins_dir / "third-party-plugin").write_text(
            "keep me",
            encoding="utf-8",
        )
        raise RuntimeError("synthetic plugin-directory conflict")


class InterruptingAdapter(HermesProfileAdapter):
    def __init__(self, layout: HermesProfileLayout, *, interrupt_after: str) -> None:
        super().__init__(
            layout,
            selected_host=HostKind.HERMES,
            user_id="synthetic-user",
        )
        self.interrupt_after = interrupt_after

    def _after_mutation(self, artifact: str) -> None:
        if artifact == self.interrupt_after:
            raise KeyboardInterrupt("synthetic process interruption")


def test_populated_profile_preserves_unmanaged_content_and_comments(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    unrelated_plugin = layout.plugins_dir / "unrelated" / "state.bin"
    unrelated_plugin.parent.mkdir(parents=True)
    unrelated_plugin.write_bytes(b"untouched-plugin-state")
    layout.config_path.write_bytes(
        b"# keep top comment\r\n"
        b"model: keep-model\r\n"
        b"memory:\r\n"
        b"  provider: native  # keep managed comment\r\n"
        b"  custom_key: keep-memory\r\n"
        b"display:\r\n"
        b"  custom_display: keep-display\r\n"
        b"telegram:\r\n"
        b"  token: keep-token\r\n"
    )
    layout.config_path.chmod(0o640)
    layout.env_path.write_bytes(
        b"# keep env comment\r\n"
        b"SECRET_TOKEN=do-not-copy-to-journal\r\n"
        b"DAEMON_CORE_FOLLOWUPS=off\r\n"
        b"UNRELATED=value=with=equals\r\n"
    )
    layout.env_path.chmod(0o600)

    result = _execute(
        TransactionEngine(tmp_path / "transactions"),
        _adapter(layout),
    )

    assert result.status == "committed"
    config_bytes = layout.config_path.read_bytes()
    assert b"# keep top comment\r\n" in config_bytes
    assert b"model: keep-model\r\n" in config_bytes
    assert b"  custom_key: keep-memory\r\n" in config_bytes
    assert b"  custom_display: keep-display\r\n" in config_bytes
    assert b"  token: keep-token\r\n" in config_bytes
    assert b"# keep managed comment\r\n" in config_bytes
    assert b"\n" not in config_bytes.replace(b"\r\n", b"")
    assert stat.S_IMODE(layout.config_path.stat().st_mode) == 0o640

    config = yaml.safe_load(config_bytes)
    for dotted, expected in HERMES_CONFIG_VALUES:
        assert _mapping_value(config, dotted) == expected

    env_bytes = layout.env_path.read_bytes()
    assert b"# keep env comment\r\n" in env_bytes
    assert b"SECRET_TOKEN=do-not-copy-to-journal\r\n" in env_bytes
    assert b"UNRELATED=value=with=equals\r\n" in env_bytes
    assert b"DAEMON_CORE_FOLLOWUPS=on\r\n" in env_bytes
    assert b"DAEMON_CORE_USER_ID=synthetic-user\r\n" in env_bytes
    assert b"PYTHONDONTWRITEBYTECODE=1\r\n" in env_bytes
    assert b"\n" not in env_bytes.replace(b"\r\n", b"")
    assert layout.memory_plugin_link.is_symlink()
    assert os.readlink(layout.memory_plugin_link) == str(layout.memory_plugin_source)
    assert layout.voice_plugin_link.is_symlink()
    assert os.readlink(layout.voice_plugin_link) == str(layout.voice_plugin_source)
    assert unrelated_plugin.read_bytes() == b"untouched-plugin-state"

    journal = (
        tmp_path / "transactions" / "hermes-profile-test" / "journal.json"
    ).read_text(encoding="utf-8")
    assert "do-not-copy-to-journal" not in journal
    assert "secret-value" not in journal


def test_absent_config_env_and_plugin_directory_are_created(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _execute(
        TransactionEngine(tmp_path / "transactions"),
        HermesProfileAdapter(
            layout,
            selected_host=HostKind.HERMES,
            user_id="channel:user",
        ),
    )

    assert layout.config_path.is_file()
    assert layout.env_path.is_file()
    assert stat.S_IMODE(layout.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(layout.env_path.stat().st_mode) == 0o600
    assert layout.plugins_dir.is_dir()
    assert layout.memory_plugin_link.is_symlink()
    assert layout.voice_plugin_link.is_symlink()


def test_second_transaction_is_file_idempotent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    adapter = _adapter(layout)
    engine = TransactionEngine(tmp_path / "transactions")
    _execute(engine, adapter, transaction_id="first")
    after_first = _tree_state(layout.hermes_home)

    _execute(engine, adapter, transaction_id="second")

    assert _tree_state(layout.hermes_home) == after_first


@pytest.mark.parametrize(
    "fail_after",
    ["config", "env", "memory_plugin", "voice_plugin"],
)
def test_each_failure_boundary_restores_profile_byte_for_byte(
    tmp_path: Path,
    fail_after: str,
) -> None:
    layout = _layout(tmp_path)
    layout.plugins_dir.mkdir()
    old_memory = tmp_path / "old-memory-plugin"
    old_voice = tmp_path / "old-voice-plugin"
    old_memory.mkdir()
    old_voice.mkdir()
    layout.memory_plugin_link.symlink_to(old_memory)
    layout.voice_plugin_link.symlink_to(old_voice)
    layout.config_path.write_bytes(b"# original\nmemory:\n  provider: native\ncustom: keep\n")
    layout.config_path.chmod(0o640)
    layout.env_path.write_bytes(b"PRIVATE_SECRET=preserve-me\nUNRELATED=keep\n")
    before = _tree_state(layout.hermes_home)
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionApplyError, match="rolled back"):
        _execute(engine, FailingAdapter(layout, fail_after=fail_after))

    assert engine.inspect("hermes-profile-test").status == "rolled_back"
    assert _tree_state(layout.hermes_home) == before


def test_failure_from_empty_profile_removes_created_plugin_directory(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    before = _tree_state(layout.hermes_home)

    with pytest.raises(TransactionApplyError):
        _execute(
            TransactionEngine(tmp_path / "transactions"),
            FailingAdapter(layout, fail_after="memory_plugin"),
        )

    assert _tree_state(layout.hermes_home) == before
    assert not layout.plugins_dir.exists()


@pytest.mark.parametrize(
    "interrupt_after",
    ["config", "env", "memory_plugin", "voice_plugin"],
)
def test_interruption_is_recovered_with_a_fresh_adapter(
    tmp_path: Path,
    interrupt_after: str,
) -> None:
    layout = _layout(tmp_path)
    layout.config_path.write_bytes(b"original: config\n")
    layout.env_path.write_bytes(b"ORIGINAL=environment\n")
    before = _tree_state(layout.hermes_home)
    engine = TransactionEngine(tmp_path / "transactions")
    plan = _plan()
    digest = public_plan_digest(plan)

    with pytest.raises(KeyboardInterrupt):
        engine.execute(
            plan,
            InterruptingAdapter(layout, interrupt_after=interrupt_after),
            transaction_id="interrupted",
            confirmed_plan_digest=digest,
        )

    result = engine.recover(
        plan,
        _adapter(layout),
        transaction_id="interrupted",
        confirmed_plan_digest=digest,
    )
    assert result.status == "rolled_back"
    assert _tree_state(layout.hermes_home) == before


def test_concurrent_profile_change_refuses_rollback(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.env_path.write_bytes(b"UNRELATED=before\n")
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionRecoveryError, match="manual recovery"):
        _execute(
            engine,
            FailingAdapter(
                layout,
                fail_after="voice_plugin",
                concurrent_env_change=True,
            ),
        )

    result = engine.inspect("hermes-profile-test")
    assert result.status == "rollback_failed"
    assert result.rollback_failed_steps == (HERMES_PROFILE_STEP,)
    assert b"THIRD_PARTY_CHANGE=keep-me" in layout.env_path.read_bytes()


def test_new_third_party_plugin_refuses_rollback_before_restoration(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    engine = TransactionEngine(tmp_path / "transactions")

    with pytest.raises(TransactionRecoveryError, match="manual recovery"):
        _execute(
            engine,
            _adapter(layout, PluginDirectoryConflictAdapter),
        )

    assert engine.inspect("hermes-profile-test").status == "rollback_failed"
    assert (layout.plugins_dir / "third-party-plugin").read_text() == "keep me"
    assert layout.memory_plugin_link.is_symlink()


def test_regular_plugin_destination_is_refused_without_mutation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.plugins_dir.mkdir()
    layout.memory_plugin_link.write_text("user-owned", encoding="utf-8")
    before = _tree_state(layout.hermes_home)

    with pytest.raises(TransactionApplyError):
        _execute(
            TransactionEngine(tmp_path / "transactions"),
            _adapter(layout),
        )

    assert _tree_state(layout.hermes_home) == before


@pytest.mark.parametrize("profile_file", ["config.yaml", ".env"])
def test_symlinked_profile_file_is_refused_without_mutation(
    tmp_path: Path,
    profile_file: str,
) -> None:
    layout = _layout(tmp_path)
    external = tmp_path / "external"
    external.write_bytes(b"outside")
    (layout.hermes_home / profile_file).symlink_to(external)

    with pytest.raises(TransactionApplyError):
        _execute(
            TransactionEngine(tmp_path / "transactions"),
            _adapter(layout),
        )

    assert external.read_bytes() == b"outside"


def test_unmarked_profile_and_wrong_step_are_refused(tmp_path: Path) -> None:
    unmarked = (tmp_path / "unmarked").absolute()
    unmarked.mkdir()
    adapter = HermesProfileAdapter(
        HermesProfileLayout(unmarked, REPOSITORY_ROOT.absolute()),
        selected_host=HostKind.HERMES,
        user_id="synthetic-user",
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    with pytest.raises(ValueError, match="not marked as disposable"):
        adapter.snapshot(PROFILE_STEP, snapshot)
    with pytest.raises(ValueError, match="only accepts hermes-profile"):
        adapter.snapshot(PlanStep("review", "Review", ()), snapshot)
    assert list(unmarked.iterdir()) == []

    marked = _layout(tmp_path / "wrong-host")
    with pytest.raises(ValueError, match="requires the Hermes host"):
        HermesProfileAdapter(
            marked,
            selected_host=HostKind.OPENCLAW,
            user_id="synthetic-user",
        )
    assert not marked.config_path.exists()


def test_public_complete_plan_cannot_be_mistaken_for_supported_install(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    probe = HostProbe(
        HostKind.HERMES,
        detected=True,
        usable=True,
        executable="/synthetic/hermes",
        version="synthetic",
        version_status="verified",
    )
    plan = build_setup_plan(
        SetupRequest(
            HostKind.HERMES,
            "Iris",
            "feminine",
            ProviderSelection(
                ProviderKind.OPENAI_API,
                model="synthetic-model",
                credential_available=True,
            ),
        ),
        (probe,),
    )
    before = _tree_state(layout.hermes_home)

    with pytest.raises(TransactionApplyError):
        TransactionEngine(tmp_path / "transactions").execute(
            plan,
            _adapter(layout),
            transaction_id="complete-plan-refused",
            confirmed_plan_digest=public_plan_digest(plan),
        )

    assert _tree_state(layout.hermes_home) == before


def test_profile_with_symlinked_parent_is_refused(tmp_path: Path) -> None:
    actual_parent = (tmp_path / "actual-home").absolute()
    hermes_home = actual_parent / ".hermes"
    create_disposable_profile_marker(hermes_home)
    linked_parent = (tmp_path / "linked-home").absolute()
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    indirect_home = linked_parent / ".hermes"
    adapter = HermesProfileAdapter(
        HermesProfileLayout(indirect_home, REPOSITORY_ROOT.absolute()),
        selected_host=HostKind.HERMES,
        user_id="synthetic-user",
    )
    snapshot = (tmp_path / "snapshot").absolute()
    snapshot.mkdir()

    with pytest.raises(ValueError, match="direct paths"):
        adapter.snapshot(PROFILE_STEP, snapshot)
    assert _tree_state(hermes_home) == {
        ".daemon-harness-disposable-profile": (
            "file",
            b"daemon-harness disposable Hermes profile v1\n",
            0o600,
        )
    }


def test_duplicate_managed_yaml_or_env_key_fails_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.config_path.write_text(
        "memory:\n  provider: one\nmemory:\n  provider: two\n",
        encoding="utf-8",
    )
    before = _tree_state(layout.hermes_home)

    with pytest.raises(TransactionApplyError):
        _execute(
            TransactionEngine(tmp_path / "yaml-transactions"),
            _adapter(layout),
        )
    assert _tree_state(layout.hermes_home) == before

    layout.config_path.unlink()
    layout.env_path.write_text(
        "DAEMON_CORE_FOLLOWUPS=off\nDAEMON_CORE_FOLLOWUPS=on\n",
        encoding="utf-8",
    )
    before = _tree_state(layout.hermes_home)
    with pytest.raises(TransactionApplyError):
        _execute(
            TransactionEngine(tmp_path / "env-transactions"),
            _adapter(layout),
        )
    assert _tree_state(layout.hermes_home) == before


def test_snapshot_tree_is_private_and_contains_no_symlinks(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.env_path.write_bytes(b"PRIVATE_SECRET=snapshot-only\n")
    transaction_root = tmp_path / "transactions"
    _execute(
        TransactionEngine(transaction_root),
        _adapter(layout),
    )

    snapshot_root = transaction_root / "hermes-profile-test" / "snapshots"
    assert b"snapshot-only" in (
        snapshot_root / HERMES_PROFILE_STEP / "env.before"
    ).read_bytes()
    for path in (snapshot_root, *snapshot_root.rglob("*")):
        assert not path.is_symlink()
        if os.name != "nt":
            expected = 0o700 if path.is_dir() else 0o600
            assert stat.S_IMODE(path.stat().st_mode) == expected
    journal = (transaction_root / "hermes-profile-test" / "journal.json").read_text()
    assert "snapshot-only" not in journal


@pytest.mark.parametrize("fail_after", ["config", "env", "memory_plugin", "voice_plugin"])
def test_windows_copy_failure_restores_empty_profile(tmp_path, fail_after):
    from dataclasses import replace
    layout = replace(_layout(tmp_path), plugin_copies=True)
    before = _tree_state(layout.hermes_home)
    engine = TransactionEngine(tmp_path / "transactions")
    with pytest.raises(TransactionApplyError, match="rolled back"):
        _execute(engine, FailingAdapter(layout, fail_after=fail_after))
    assert _tree_state(layout.hermes_home) == before


def test_windows_copy_concurrent_change_is_preserved(tmp_path):
    from dataclasses import replace
    layout = replace(_layout(tmp_path), plugin_copies=True)
    class ChangedCopy(HermesProfileAdapter):
        def _after_mutation(self, name):
            if name == "memory_plugin":
                (self.layout.memory_plugin_link / "concurrent.txt").write_text("keep me")
                raise RuntimeError("synthetic failure")
    engine = TransactionEngine(tmp_path / "transactions")
    with pytest.raises(TransactionRecoveryError, match="manual recovery"):
        _execute(engine, _adapter(layout, ChangedCopy))
    assert engine.inspect("hermes-profile-test").status == "rollback_failed"
    assert (layout.memory_plugin_link / "concurrent.txt").read_text() == "keep me"


def test_runtime_path_environment_extends_roots_without_changing_switches(tmp_path, monkeypatch):
    from daemons_companion.runtime import bootstrap
    monkeypatch.setattr(bootstrap, "_source_io_path", lambda path: Path("/extended") / str(path).lstrip("/"))
    environment = {
        "DAEMON_CORE_MEMORY_ROOT": str(tmp_path / "memory"),
        "DAEMON_CORE_STATE_ROOT": str(tmp_path / "state"),
        "DAEMON_CORE_PROVIDER_CONFIG": str(tmp_path / "state/config/provider.json"),
        "DAEMON_CORE_PYTHON": str(tmp_path / "python"),
        "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(tmp_path / "state/spool"),
        "DAEMON_CORE_LATENCY_LOG": str(tmp_path / "state/latency.jsonl"),
        "DAEMON_CORE_AFTER_TURN_OWNER": "external",
        "DAEMON_CORE_AFTER_TURN_SPOOL": "on",
    }
    adapter = _adapter(_layout(tmp_path), runtime_environment=environment)
    declared = adapter._environment_values()
    expected = bootstrap.private_runtime_path_environment(environment)
    assert all(declared[key] == value for key, value in expected.items())
    assert declared["DAEMON_CORE_MEMORY_ROOT"].startswith("/extended/")
    assert declared["DAEMON_CORE_AFTER_TURN_OWNER"] == "external"
    assert declared["DAEMON_CORE_AFTER_TURN_SPOOL"] == "on"
    assert environment["DAEMON_CORE_MEMORY_ROOT"] == str(tmp_path / "memory")


def test_private_hermes_receives_reference_conversation_settings():
    values = dict(HERMES_CONFIG_VALUES)
    # A Hermes-run search backend keeps tool progress visible; native model
    # search would bypass progress display and preamble cleanup.
    assert values["web.search_backend"] == "ddgs"
    assert values["agent.reasoning_effort"] == "medium"
    assert values["display.show_reasoning"] is False
    assert values["approvals.mode"] == "manual"
    # Explicit because the gateway does not merge Hermes defaults.
    assert values["display.platforms.telegram.streaming"] is True
    assert values["display.tool_preview_length"] == 0
    assert values["display.busy_ack_detail"] is True
    # A slow boot must not leave the gateway without its Telegram plugin.
    assert values["plugins.load_timeout_seconds"] == 120


def test_config_from_an_earlier_installation_is_current_without_later_settings():
    from daemons_companion.hermes.profile import (
        HERMES_LATER_CONFIG_KEYS,
        _patch_hermes_config,
        hermes_config_current,
    )

    earlier = _patch_hermes_config(
        b"", tuple(v for v in HERMES_CONFIG_VALUES if v[0] not in HERMES_LATER_CONFIG_KEYS)
    )
    current = _patch_hermes_config(b"")
    assert hermes_config_current(earlier)
    assert hermes_config_current(current)
    assert not hermes_config_current(current.replace(b"load_timeout_seconds: 120", b"load_timeout_seconds: 5"))
    assert not hermes_config_current(earlier.replace(b"manual", b"off"))
