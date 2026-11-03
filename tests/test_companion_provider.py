from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    PlanStep,
    ProviderKind,
    ProviderSelection,
    SetupPlan,
)
from daemons_companion.setup.credentials import (
    KEYCHAIN_SERVICE,
    WINDOWS_TARGET_PREFIX,
    MacOSKeychainStore,
    WindowsCredentialStore,
)
from daemons_companion.setup.provider import (
    CompanionProviderAdapter,
    CompanionProviderLayout,
    PROVIDER_STATE_STEP,
)
from daemons_companion.runtime.release import create_disposable_companion_marker
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


PROVIDER_STEP = PlanStep(
    PROVIDER_STATE_STEP,
    "Store non-secret provider state and a protected credential.",
    ("provider.json", "Operating-system credential store"),
)


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.put_calls = 0
        self.delete_calls = 0

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def put(self, reference: str, protected_value: str) -> None:
        self.put_calls += 1
        self.values[reference] = protected_value

    def delete(self, reference: str) -> None:
        self.delete_calls += 1
        self.values.pop(reference, None)


def _plan(step: PlanStep = PROVIDER_STEP) -> SetupPlan:
    return SetupPlan(
        selected_host=HostProbe(
            HostKind.HERMES,
            detected=True,
            usable=True,
            executable="/synthetic/hermes",
        ),
        daemon_name="Lyra",
        daemon_gender="feminine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(step,),
    )


def _layout(tmp_path: Path) -> CompanionProviderLayout:
    home = (tmp_path / "disposable-companion").absolute()
    create_disposable_companion_marker(home)
    layout = CompanionProviderLayout(home)
    layout.state_root.mkdir(parents=True, mode=0o700)
    return layout


def _openai() -> ProviderSelection:
    return ProviderSelection(
        ProviderKind.OPENAI_API,
        "gpt-test",
        credential_available=True,
    )


def _execute(
    tmp_path: Path,
    adapter: CompanionProviderAdapter,
    *,
    transaction_id: str = "provider-test",
    plan: SetupPlan | None = None,
):
    chosen = plan or _plan()
    return TransactionEngine(tmp_path / "transactions").execute(
        chosen,
        adapter,
        transaction_id=transaction_id,
        confirmed_plan_digest=public_plan_digest(chosen),
    )


def test_openai_secret_exists_only_in_credential_store(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()
    secret = "sk-super-secret-value"

    result = _execute(
        tmp_path,
        CompanionProviderAdapter(
            layout,
            selection=_openai(),
            credential_store=store,
            credential_value=secret,
        ),
    )

    assert result.status == "committed"
    config = json.loads(layout.provider_file.read_text())
    assert config["kind"] == "openai_api"
    assert config["model"] == "gpt-test"
    assert config["reasoning"] == {
        "routing": "medium",
        "conversation": "medium",
        "memory": "high",
    }
    reference = config["credential"]["reference"]
    assert reference in store.values
    assert secret in store.values[reference]
    assert secret not in layout.provider_file.read_text()
    transaction = tmp_path / "transactions/provider-test"
    assert all(secret not in path.read_text(errors="ignore") for path in transaction.rglob("*") if path.is_file())


@pytest.mark.parametrize(
    "selection,secret,codex_available,expected_kind,credential_name",
    [
        (
            ProviderSelection(
                ProviderKind.OPENAI_COMPATIBLE_API,
                "grok-compatible",
                base_url="https://provider.example/v1",
                credential_available=True,
            ),
            "provider-key",
            False,
            "openai_compatible_api",
            "LOCAL_LLM_API_KEY",
        ),
        (
            ProviderSelection(
                ProviderKind.OPENAI_COMPATIBLE_API,
                "local-model",
                base_url="http://127.0.0.1:1234/v1",
            ),
            None,
            False,
            "openai_compatible_api",
            None,
        ),
        (
            ProviderSelection(ProviderKind.CODEX_CLI, "gpt-test"),
            None,
            True,
            "codex_cli",
            None,
        ),
    ],
)
def test_supported_provider_modes_store_one_model(
    tmp_path: Path,
    selection: ProviderSelection,
    secret: str | None,
    codex_available: bool,
    expected_kind: str,
    credential_name: str | None,
) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()
    _execute(
        tmp_path,
        CompanionProviderAdapter(
            layout,
            selection=selection,
            credential_store=store,
            credential_value=secret,
            codex_available=codex_available,
        ),
    )
    config = json.loads(layout.provider_file.read_text())
    assert config["kind"] == expected_kind
    assert config["model"] == selection.model
    if credential_name is None:
        assert config["credential"] is None
        assert store.values == {}
    else:
        assert config["credential"]["name"] == credential_name


def test_same_selection_and_secret_are_byte_idempotent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()
    first = CompanionProviderAdapter(
        layout,
        selection=_openai(),
        credential_store=store,
        credential_value="same-secret",
    )
    _execute(tmp_path, first, transaction_id="first")
    before = layout.provider_file.read_bytes()
    before_references = set(store.values)

    second = CompanionProviderAdapter(
        layout,
        selection=_openai(),
        credential_store=store,
        credential_value="same-secret",
    )
    _execute(tmp_path, second, transaction_id="second")

    assert layout.provider_file.read_bytes() == before
    assert set(store.values) == before_references
    assert store.put_calls == 1


def test_changed_selection_keeps_old_credential_for_rollback(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()
    _execute(
        tmp_path,
        CompanionProviderAdapter(
            layout,
            selection=_openai(),
            credential_store=store,
            credential_value="old-secret",
        ),
        transaction_id="old",
    )
    old_reference = json.loads(layout.provider_file.read_text())["credential"]["reference"]

    changed = ProviderSelection(
        ProviderKind.OPENAI_COMPATIBLE_API,
        "other-model",
        base_url="https://provider.example/v1",
        credential_available=True,
    )
    _execute(
        tmp_path,
        CompanionProviderAdapter(
            layout,
            selection=changed,
            credential_store=store,
            credential_value="new-secret",
        ),
        transaction_id="new",
    )
    new_reference = json.loads(layout.provider_file.read_text())["credential"]["reference"]
    assert new_reference != old_reference
    assert set(store.values) == {old_reference, new_reference}


@pytest.mark.parametrize("boundary", ["credential", "config"])
def test_failure_boundaries_restore_absence(
    tmp_path: Path,
    boundary: str,
) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()

    def fail(artifact: str) -> None:
        if artifact == boundary:
            raise RuntimeError("synthetic failure containing sk-do-not-log")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionProviderAdapter(
                layout,
                selection=_openai(),
                credential_store=store,
                credential_value="sk-do-not-log",
                mutation_hook=fail,
            ),
        )

    assert not layout.provider_file.exists()
    assert store.values == {}
    journal = (tmp_path / "transactions/provider-test/journal.json").read_text()
    assert "sk-do-not-log" not in journal


@pytest.mark.parametrize("boundary", ["credential", "config"])
def test_interruptions_recover_with_fresh_adapter(tmp_path: Path, boundary: str) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()

    def interrupt(artifact: str) -> None:
        if artifact == boundary:
            raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt):
        _execute(
            tmp_path,
            CompanionProviderAdapter(
                layout,
                selection=_openai(),
                credential_store=store,
                credential_value="secret",
                mutation_hook=interrupt,
            ),
        )

    plan = _plan()
    result = TransactionEngine(tmp_path / "transactions").recover(
        plan,
        CompanionProviderAdapter.for_recovery(
            layout,
            selection=_openai(),
            credential_store=store,
        ),
        transaction_id="provider-test",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert not layout.provider_file.exists()
    assert store.values == {}


def test_concurrent_config_change_is_preserved(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()

    def conflict(artifact: str) -> None:
        if artifact == "config":
            layout.provider_file.write_text('{"third_party":true}\n', encoding="utf-8")
            raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            CompanionProviderAdapter(
                layout,
                selection=_openai(),
                credential_store=store,
                credential_value="secret",
                mutation_hook=conflict,
            ),
        )
    assert json.loads(layout.provider_file.read_text()) == {"third_party": True}


def test_concurrent_credential_change_is_preserved(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = MemoryCredentialStore()

    def conflict(artifact: str) -> None:
        if artifact == "credential":
            reference = next(iter(store.values))
            store.values[reference] = '{"third_party":true}'
            raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            CompanionProviderAdapter(
                layout,
                selection=_openai(),
                credential_store=store,
                credential_value="secret",
                mutation_hook=conflict,
            ),
        )
    assert next(iter(store.values.values())) == '{"third_party":true}'


@pytest.mark.parametrize(
    "selection,store,secret,codex_available",
    [
        (_openai(), None, "secret", False),
        (_openai(), MemoryCredentialStore(), None, False),
        (ProviderSelection(ProviderKind.CODEX_CLI, "model"), None, None, False),
    ],
)
def test_invalid_or_incomplete_selection_fails_before_transaction(
    tmp_path: Path,
    selection: ProviderSelection,
    store: MemoryCredentialStore | None,
    secret: str | None,
    codex_available: bool,
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError):
        CompanionProviderAdapter(
            layout,
            selection=selection,
            credential_store=store,
            credential_value=secret,
            codex_available=codex_available,
        )
    assert not layout.provider_file.exists()


def test_malformed_state_and_symlink_paths_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "malformed")
    layout.config_dir.mkdir()
    layout.provider_file.write_text('{"unexpected":true}\n', encoding="utf-8")
    before = layout.provider_file.read_bytes()
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path / "malformed",
            CompanionProviderAdapter(
                layout,
                selection=_openai(),
                credential_store=MemoryCredentialStore(),
                credential_value="secret",
            ),
        )
    assert layout.provider_file.read_bytes() == before

    link_layout = _layout(tmp_path / "link")
    external = tmp_path / "external"
    external.mkdir()
    link_layout.config_dir.symlink_to(external, target_is_directory=True)
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path / "link",
            CompanionProviderAdapter(
                link_layout,
                selection=_openai(),
                credential_store=MemoryCredentialStore(),
                credential_value="secret",
            ),
        )
    assert list(external.iterdir()) == []


def test_public_provider_step_remains_unsupported(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    public = _plan(PlanStep("provider", "Connect provider", ("Protected credential",)))
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionProviderAdapter(
                layout,
                selection=_openai(),
                credential_store=MemoryCredentialStore(),
                credential_value="secret",
            ),
            plan=public,
        )
    assert not layout.provider_file.exists()


class _Result:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_macos_keychain_uses_stdin_and_one_system_accessor() -> None:
    calls, values = [], {}
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == '-i':
            command = kwargs['input']
            assert command.count('\n') == 1
            assert '-T /usr/bin/security' in command
            values['encoded'] = command.split(' -w ')[1].strip()
        elif args[1] == 'find-generic-password':
            return _Result(0, values['encoded'] + '\n') if values else _Result(44)
        else:
            values.clear()
        return _Result(0)
    store = MacOSKeychainStore(runner=runner)
    reference = "provider-00000000-0000-0000-0000-000000000000"
    value = 'synthetic \"quoted\" \n another command; é'
    store.put(reference, value)
    assert store.get(reference) == value
    assert value not in str([args for args, _ in calls])
    assert value not in calls[0][1]['input']
    assert KEYCHAIN_SERVICE in calls[0][1]['input']
    store.delete(reference)
    assert store.get(reference) is None


class FakeWindowsApi:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def read(self, target: str) -> bytes | None:
        return self.values.get(target)

    def write(self, target: str, value: bytes) -> None:
        self.values[target] = value

    def delete(self, target: str) -> None:
        self.values.pop(target, None)


def test_windows_store_uses_native_api_boundary_and_utf8_payload() -> None:
    api = FakeWindowsApi()
    store = WindowsCredentialStore(api=api)
    reference = "provider-00000000-0000-0000-0000-000000000000"
    store.put(reference, "clé-protégée")
    target = WINDOWS_TARGET_PREFIX + reference
    assert api.values[target] == "clé-protégée".encode("utf-8")
    assert store.get(reference) == "clé-protégée"
    store.delete(reference)
    assert store.get(reference) is None
