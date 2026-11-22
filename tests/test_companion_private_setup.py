from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from daemons_companion.setup.codex_install import (
    extract_package,
    install_private_codex,
    private_codex,
)
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.private_environment import private_process_environment
from daemons_companion.hermes.first_install import first_exchange, configure_telegram
from daemons_companion.runtime.host_install import installed_host
from tests.test_companion_host_install import fixture, execute


def owned(tmp_path, monkeypatch, target_os="macos"):
    layout, runner, adapter = fixture(tmp_path, target_os=target_os)
    execute(layout, adapter)
    import daemons_companion.runtime.host_install as host_module

    monkeypatch.setattr(
        host_module, "installed_host", lambda item: installed_host(item, runner=runner)
    )
    scope = InstallationScope.from_verified_host(layout)
    scope.initialize()
    return layout, scope


def archive(path, name="bin/codex", body=b"official synthetic binary"):
    with tarfile.open(path, "w:gz") as output:
        member = tarfile.TarInfo(name)
        member.mode = 0o755
        member.size = len(body)
        output.addfile(member, io.BytesIO(body))


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_private_codex_download_verified_and_reused(tmp_path, monkeypatch, target_os):
    import daemons_companion.setup.codex_install as module

    layout, _ = owned(tmp_path, monkeypatch, target_os)
    source = tmp_path / "codex.tar.gz"
    archive(source, "bin/codex.exe" if target_os == "windows" else "bin/codex")
    monkeypatch.setitem(
        module.PACKAGES,
        target_os,
        (
            "synthetic",
            source.stat().st_size,
            hashlib.sha256(source.read_bytes()).hexdigest(),
        ),
    )
    calls = []

    def download(url, destination, size, digest):
        calls.append(url)
        destination.write_bytes(source.read_bytes())

    binary = install_private_codex(layout, lambda _: None, downloader=download)
    assert binary.is_relative_to(layout.attempt)
    assert private_codex(layout) == binary
    assert install_private_codex(layout, lambda _: None, downloader=download) == binary
    assert len(calls) == 1
    binary.write_bytes(b"altered")
    with pytest.raises(ValueError, match="changed"):
        private_codex(layout)


def test_damaged_codex_download_never_published(tmp_path, monkeypatch):
    layout, _ = owned(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="integrity"):
        install_private_codex(
            layout,
            lambda _: None,
            downloader=lambda url, path, *args: path.write_bytes(b"bad"),
        )
    assert private_codex(layout) is None


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "bin/../../escape", "bin\\escape", "C:escape"]
)
def test_codex_archive_rejects_unsafe_paths(tmp_path, name):
    source = tmp_path / "package.tar.gz"
    archive(source, name)
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ValueError):
        extract_package(source, destination)
    assert list(destination.iterdir()) == []


def connection(scope, tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"codex")
    for name in (".codex", ".hermes-codex"):
        (scope.attempt / "os-home" / name).mkdir()
    data = {
        "schema": "daemon.private-connection.v1",
        "codex": str(binary),
        "codex_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "user_id": "12345",
    }
    (scope.attempt / "connection.json").write_text(json.dumps(data))
    return binary


def test_background_environment_keeps_independent_auth_and_excludes_personal_secrets(
    tmp_path, monkeypatch
):
    _, scope = owned(tmp_path, monkeypatch)
    binary = connection(scope, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "personal-secret")
    monkeypatch.setenv("CODEX_HOME", "/personal/codex")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "personal-bot")
    worker = private_process_environment(scope)
    gateway = private_process_environment(scope, hermes=True)
    assert worker["CODEX_HOME"] != gateway["CODEX_HOME"]
    for env in (worker, gateway):
        assert env["HERMES_HOME"] == str(scope.profile)
        assert Path(env["CODEX_HOME"]).is_relative_to(scope.attempt)
        assert env["CODEX_CLI_BIN"] == str(binary)
        assert "OPENAI_API_KEY" not in env and "TELEGRAM_BOT_TOKEN" not in env
    binary.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        private_process_environment(scope)


def event(
    turn, stage, status="completed", *, user="12345", platform="telegram", at=None
):
    return {
        "schema": "daemon.continuity-receipt.event.v1",
        "turn_id": turn,
        "at": at or datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "status": status,
        "identity": {
            "platform": platform,
            "user_ref": hashlib.sha256(user.encode()).hexdigest()[:16],
        },
    }


def receipt(root, events):
    root.mkdir(exist_ok=True)
    (root / "continuity_receipts.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )


def test_first_exchange_requires_one_completed_telegram_turn(tmp_path):
    root = tmp_path / "receipts"
    receipt(root, [event("one", "response"), event("two", "l0"), event("two", "cycle")])
    assert not first_exchange(root, since=0, user_id="12345")
    receipt(root, [event("one", stage) for stage in ("response", "l0", "cycle")])
    assert first_exchange(root, since=0, user_id="12345")
    assert not first_exchange(root, since=0, user_id="999")
    assert not first_exchange(
        root, since=datetime.now(timezone.utc).timestamp() + 10, user_id="12345"
    )
    receipt(
        root,
        [
            event("one", stage, platform="api_server")
            for stage in ("response", "l0", "cycle")
        ],
    )
    assert not first_exchange(root, since=0, user_id="12345")
    receipt(
        root,
        [event("one", "response"), event("one", "l0"), event("one", "cycle", "failed")],
    )
    assert not first_exchange(root, since=0, user_id="12345")


def test_telegram_configuration_masks_native_failure_and_uses_stdin(
    tmp_path, monkeypatch
):
    import daemons_companion.hermes.first_install as module

    layout, _ = owned(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "PrivateSubscriptionSetup",
        lambda *a, **k: SimpleNamespace(
            environment=lambda **kw: {"HOME": str(layout.attempt)}
        ),
    )
    captured = []
    token = "123456:" + "a" * 30

    def run(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="secret: " + token)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        configure_telegram(
            layout,
            Path("/synthetic/codex"),
            token=token,
            user_id="12345",
            model="synthetic",
        )
    assert token not in str(error.value)
    assert token not in str(captured[0][0])
    assert json.loads(captured[0][1]["input"])["token"] == token


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize(
    "failed_step",
    ["", "runtime-environment", "background-services", "hermes-activation"],
)
@pytest.mark.parametrize("root_folders", [False, True])
def test_complete_ui_executes_real_transaction_with_isolated_native_boundaries(
    tmp_path, monkeypatch, target_os, failed_step, root_folders
):
    import os

    if os.name == "nt" and target_os == "macos":
        pytest.skip(
            "The POSIX filesystem/job variant runs on macOS; Windows runs its native variant."
        )
    from dataclasses import replace
    import daemons_companion.hermes.first_install as module
    from daemons_companion.setup.plan import (
        HostKind,
        ProviderKind,
        ProviderSelection,
        SetupRequest,
        build_setup_plan,
    )
    from daemons_companion.setup.install_session import prepare_installation
    from daemons_companion.desktop.package import write_package_manifest
    from daemons_companion.hermes.ready_composition import (
        HermesReadyCompositionAdapter,
    )
    from tests.test_companion_app import _preparation
    from tests.test_companion_hermes_ready_composition import (
        FakeControl,
        FakeScheduler,
        FakeRunner,
    )

    host_dir = tmp_path / "host"
    host_dir.mkdir()
    layout, scope = owned(host_dir, monkeypatch, target_os)
    layout.uv_executable.chmod(0o755)
    package_dir = tmp_path / "package-root"
    package_dir.mkdir()
    context = replace(
        _preparation(package_dir, target_os=target_os),
        companion_home=layout.companion_home,
        installation_scope=scope,
        root_folder_memory=root_folders,
    )
    template = context.bundle_root / "memory/templates/users/conversation.md"
    template.parent.mkdir(parents=True)
    template.write_text("Canonical conversation template\n")
    template.with_name("persona.md").write_text("Canonical persona template\n")
    (context.bundle_root / "src/daemon_core/loop.py").write_text("# synthetic loop\n")
    (context.bundle_root / "requirements-runtime.lock").write_text(
        "example==1 --hash=sha256:" + "0" * 64 + "\n"
    )
    (context.package_root / "companion-manifest.json").unlink()
    write_package_manifest(context.package_root)
    host = __import__(
        "daemons_companion.runtime.host_install", fromlist=["installed_host"]
    ).installed_host(layout)
    monkeypatch.setattr(module, "installed_host", lambda _: host)
    monkeypatch.setattr(module, "ensure_setup_dependencies", lambda *a: None)
    from daemons_companion.setup import embeddings
    from tests.test_companion_embeddings import synthetic_receipt

    embedding_calls = []

    def prepare_embeddings(scope, target, progress):
        embedding_calls.append((scope, target))
        return synthetic_receipt(scope, target)

    monkeypatch.setattr(embeddings, "prepare", prepare_embeddings)
    monkeypatch.setattr(embeddings, "wait_ready", lambda *args: None)
    binary = connection(scope, tmp_path)
    monkeypatch.setattr(
        module,
        "PrivateSubscriptionSetup",
        lambda *a, **k: SimpleNamespace(
            status=lambda: SimpleNamespace(codex=True, hermes=True)
        ),
    )

    def telegram(*args, **kwargs):
        (scope.profile / "config.yaml").write_text(
            "model:\n  provider: openai-codex\n  default: synthetic\n"
        )
        return {"username": "synthetic_bot", "bot_id": 123}

    monkeypatch.setattr(module, "configure_telegram", telegram)
    scheduler = FakeScheduler(target_os)
    scheduler.job_namespace = scope.installation_id
    monkeypatch.setattr(module, "MacLaunchdSchedulerBackend", lambda *a, **k: scheduler)
    monkeypatch.setattr(
        module, "WindowsTaskSchedulerBackend", lambda *a, **k: scheduler
    )
    control = FakeControl(scope.profile)
    monkeypatch.setattr(module, "PrivateHermesControl", lambda *a: control)

    def adapter(composition, **kwargs):
        kwargs["runner"] = FakeRunner(target_os)
        kwargs["readiness_probe"] = lambda *a: True
        delegate = HermesReadyCompositionAdapter(composition, **kwargs)
        if failed_step:
            apply = delegate.apply

            def fail(step):
                apply(step)
                if step.code == failed_step:
                    error = OSError(22, "Synthetic Windows path failure")
                    error.winerror = 123
                    raise error

            delegate.apply = fail
        return delegate

    monkeypatch.setattr(module, "HermesReadyCompositionAdapter", adapter)
    monkeypatch.setattr(module, "first_exchange", lambda *a, **k: True)
    selection = ProviderSelection(ProviderKind.CODEX_CLI, "synthetic")
    from daemon_core.onboarding_user import UserPreferences

    preferences = UserPreferences("Morgan", "they/them") if root_folders else None
    reviewed = build_setup_plan(
        SetupRequest(HostKind.HERMES, "Aster", "feminine", selection, preferences),
        (host,),
        codex_available=True,
    )
    prepared = prepare_installation(
        reviewed, context.install_layout(HostKind.HERMES, scope.profile)
    )
    request = {
        "host": "hermes",
        "daemon_name": "Aster",
        "daemon_gender": "feminine",
        "provider": {"kind": "codex_cli", "model": "synthetic"},
    }
    if preferences:
        request["user_preferences"] = preferences.to_dict()
    owner = SimpleNamespace(
        layout=lambda: layout,
        codex_executable=lambda _: binary,
        context=context,
        daemon_installed=False,
    )
    from contextlib import contextmanager

    class UI:
        output = []
        active = False
        phases = []

        @contextmanager
        def activity(self, label):
            assert not self.active
            self.active = True
            self.phases.append(label)
            try:
                yield self.say
            finally:
                self.active = False

        defaults = {}

        def choose(self, title, *a, **kw):
            assert not self.active
            self.defaults[title] = kw.get("default", 0)
            return (
                0
                if title
                in (
                    "Create your Telegram bot",
                    "Install and start this private daemon?",
                )
                else 1
            )

        def say(self, text):
            self.output.append(text)

        def field(self, *a, **kw):
            return "12345"

        def secret(self, *a):
            assert not self.active
            return "synthetic-hidden-token"

    ui = UI()
    if failed_step:
        from daemons_companion.setup.transaction import TransactionApplyError

        with pytest.raises(TransactionApplyError):
            module.complete_private_setup(owner, ui, request, prepared.public_dict())
        diagnostic = json.loads(
            (scope.attempt / "installation-diagnostic.json").read_text()
        )
        assert diagnostic["step"] == failed_step and diagnostic["phase"] == "apply"
        assert diagnostic["causes"][0]["winerror"] == 123
        assert diagnostic["transaction_status"] == "rolled_back"
        assert (
            not owner.daemon_installed
            and not (scope.attempt / "daemon-setup.json").exists()
        )
        assert not scheduler.state and control.gateway != "running"
        assert any("Token accepted" in line for line in ui.output)
        assert any("Installation cause:" in line for line in ui.output)
        assert not any("Setup complete" in line for line in ui.output)
        assert not ui.active
        return
    assert module.complete_private_setup(owner, ui, request, prepared.public_dict())
    assert embedding_calls == [(scope, target_os)]
    assert owner.daemon_installed
    assert "Installing runtime, plugins and private services..." in ui.phases
    assert "Waiting for your Telegram reply and memory work..." in ui.phases
    # A verified reply proposes "Yes" rather than "Not yet" (2026-10-30).
    assert ui.defaults["Did you receive the reply in Telegram?"] == 1
    assert not ui.active
    assert control.gateway == "running"
    assert len(scheduler.state) == 3
    assert next(iter(scheduler.state)).endswith(".embeddings")
    assert all(scope.installation_id in key for key in scheduler.state)
    memory = context.install_layout(HostKind.HERMES, scope.profile).runtime.memory_root
    assert (memory / "users/12345/conversation.md").read_text() == template.read_text()
    from daemon_core.daemon_identity import load_daemon_identity

    assert load_daemon_identity(memory).user == (
        preferences.for_user("12345") if preferences else None
    )
    if root_folders:
        assert memory.name == ".daemon"
        from daemon_core.memory.library_layout import vault_root, FOLDERS

        assert vault_root(memory) == memory.parent
        assert all((memory.parent / folder).is_dir() for folder in FOLDERS)
        env = private_process_environment(scope)
        assert env["DAEMON_CORE_MEMORY_STRUCTURE"] == "v1"
        assert env["DAEMON_CORE_OBSIDIAN_FORMAT"] == "1"
    assert (scope.attempt / "daemon-setup.json").exists()
    assert scope.profile.joinpath("plugins/daemon_core").is_symlink() == (
        target_os == "macos"
    )
    assert any("Setup complete" in line for line in ui.output)
    # Simulate a hard stop after commit but before the completion marker.
    (scope.attempt / "daemon-setup.json").unlink()
    before = (scope.profile / "config.yaml").read_bytes()
    with pytest.raises(RuntimeError, match="committed installation"):
        module.complete_private_setup(owner, ui, request, prepared.public_dict())
    assert (scope.profile / "config.yaml").read_bytes() == before


def test_degraded_reply_never_confirms_setup(tmp_path):
    events = [event("one", stage) for stage in ("response", "l0", "cycle")]
    events[0]["details"] = {"degraded": True}
    receipt(tmp_path, events)
    assert not first_exchange(tmp_path, since=0, user_id="12345")


def test_failed_memory_stage_blocks_even_a_later_completed_cycle(tmp_path):
    receipt(
        tmp_path,
        [event("one", stage) for stage in ("response", "l0", "cycle")]
        + [event("one", "memory", "failed")],
    )
    assert not first_exchange(tmp_path, since=0, user_id="12345")


def test_setup_dependency_install_is_private_reused_and_tamper_checked(
    tmp_path, monkeypatch
):
    from daemons_companion.setup.dependencies import ensure_setup_dependencies
    import sys

    initial_path = list(sys.path)
    layout, scope = owned(tmp_path, monkeypatch)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "requirements-companion.lock").write_text(
        "synthetic==1 --hash=sha256:" + "0" * 64 + "\n"
    )
    calls = []

    def runner(command, environment, **kwargs):
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        (target / "yaml").mkdir(parents=True)
        (target / "yaml/__init__.py").write_text("# synthetic verified module")
        assert target.is_relative_to(scope.attempt)
        assert "--require-hashes" in command and "--no-build" in command

    target = ensure_setup_dependencies(layout, bundle, lambda _: None, runner=runner)
    try:
        assert (
            ensure_setup_dependencies(layout, bundle, lambda _: None, runner=runner)
            == target
        )
        assert len(calls) == 1
        (target / "yaml/__init__.py").write_text("changed")
        with pytest.raises(ValueError, match="changed"):
            ensure_setup_dependencies(layout, bundle, lambda _: None, runner=runner)
    finally:
        sys.path[:] = initial_path


@pytest.mark.parametrize(
    ("code", "explanation"),
    [
        (20, "rejected this token"),
        (21, "Cannot reach Telegram"),
        (22, "already has a webhook"),
        (23, "private configuration"),
        (24, "could not verify this bot"),
        (1, "could not start correctly"),
    ],
)
def test_telegram_failure_categories_never_expose_native_output(
    tmp_path, monkeypatch, code, explanation
):
    import daemons_companion.hermes.first_install as module
    from daemons_companion.setup.telegram import TelegramSetupError

    layout, _ = owned(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "PrivateSubscriptionSetup",
        lambda *a, **k: SimpleNamespace(environment=lambda **kw: {}),
    )
    token = "123456:" + "a" * 30
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=code, stdout=token, stderr=token),
    )
    with pytest.raises(TelegramSetupError, match=explanation) as error:
        configure_telegram(
            layout, Path("/codex"), token=token, user_id="12345", model="synthetic"
        )
    assert token not in str(error.value)


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "invalid",
        "network",
        "webhook",
        "config",
        "api",
        "saved-success",
        "saved-mismatch",
        "saved-revoked",
    ],
)
def test_native_telegram_script_reports_fixed_codes_without_credentials(
    tmp_path, monkeypatch, capsys, outcome
):
    import sys
    from types import ModuleType
    from daemons_companion.hermes.first_install import _TELEGRAM_CONFIGURE

    telegram = ModuleType("telegram")
    errors = ModuleType("telegram.error")

    class TelegramError(Exception):
        pass

    class InvalidToken(TelegramError):
        pass

    class NetworkError(TelegramError):
        pass

    errors.TelegramError, errors.InvalidToken, errors.NetworkError = (
        TelegramError,
        InvalidToken,
        NetworkError,
    )
    token = "123456:" + "a" * 30

    class Bot:
        def __init__(self, value):
            assert value == token
            if outcome in ("invalid", "saved-revoked"):
                raise InvalidToken(token)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_me(self):
            if outcome == "network":
                raise NetworkError(token)
            if outcome == "api":
                raise TelegramError(token)
            return SimpleNamespace(username="synthetic_bot", id=123)

        async def get_webhook_info(self):
            return SimpleNamespace(
                url="https://synthetic.invalid" if outcome == "webhook" else ""
            )

    telegram.Bot = Bot
    dotenv = ModuleType("dotenv")
    saved = []
    dotenv.set_key = lambda *args: saved.append(args)
    dotenv.dotenv_values = lambda *args: {
        "TELEGRAM_ALLOWED_USERS": "999" if outcome == "saved-mismatch" else "12345",
        "TELEGRAM_BOT_TOKEN": token,
    }
    constants = ModuleType("hermes_constants")
    home = tmp_path / "profile"
    if outcome == "config":
        home.write_text("not a directory")
    else:
        home.mkdir()
    constants.get_hermes_home = lambda: home
    for name, module in [
        ("telegram", telegram),
        ("telegram.error", errors),
        ("dotenv", dotenv),
        ("hermes_constants", constants),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "token": None if outcome.startswith("saved-") else token,
                    "model_config": {
                        "provider": "openai-codex",
                        "default": "synthetic",
                    },
                    "user_id": "12345",
                }
            )
        ),
    )
    if outcome in ("success", "saved-success"):
        exec(_TELEGRAM_CONFIGURE, {})
        assert len(saved) == 3
        assert saved[0][2] == token
    else:
        with pytest.raises(SystemExit) as exit_error:
            exec(_TELEGRAM_CONFIGURE, {})
        assert (
            exit_error.value.code
            == {
                "invalid": 20,
                "network": 21,
                "webhook": 22,
                "config": 23,
                "api": 24,
                "saved-mismatch": 25,
                "saved-revoked": 20,
            }[outcome]
        )
        assert not saved
    output = capsys.readouterr()
    assert token not in output.out + output.err
    if outcome in ("success", "saved-success"):
        assert json.loads(output.out)["username"] == "synthetic_bot"


def test_first_launch_resume_does_not_create_an_installation(tmp_path):
    from daemons_companion.hermes.first_install import resume_private_setup

    layout, _, _ = fixture(tmp_path)
    assert not resume_private_setup(SimpleNamespace(layout=lambda: layout), None)
    assert not layout.companion_home.exists()


def test_resume_waits_for_connected_telegram_not_only_process(monkeypatch):
    from daemons_companion.hermes import gateway as module
    import time

    statuses = iter(
        [
            {},
            {"pid": 10, "status": {"gateway_state": "running"}},
            {
                "pid": 10,
                "status": {
                    "gateway_state": "running",
                    "platforms": {"telegram": {"state": "connected"}},
                },
            },
        ]
    )
    sleeps = []
    monkeypatch.setattr(module, "private_gateway_status", lambda _: next(statuses))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    module.wait_private_gateway_ready(None)
    assert sleeps == [1, 1]


def test_resume_does_not_claim_telegram_ready_on_timeout(monkeypatch):
    from daemons_companion.hermes import gateway as module

    monkeypatch.setattr(module, "private_gateway_status", lambda _: {})
    with pytest.raises(RuntimeError, match="has not reconnected"):
        module.wait_private_gateway_ready(None, timeout=0)


def test_native_config_owner_restores_exact_bytes_and_refuses_concurrent_change(
    tmp_path, monkeypatch
):
    from daemons_companion.hermes.gateway import PrivateHermesControl

    layout, scope = owned(tmp_path, monkeypatch)
    control = object.__new__(PrivateHermesControl)
    control.host_layout, control.scope = layout, scope
    control._config_snapshot = None
    path = scope.profile / "config.yaml"
    original = b"model: synthetic\r\n"
    path.write_bytes(original)
    snapshot = tmp_path / "native-snapshot"
    snapshot.mkdir()
    control.track_config(snapshot, create=True)
    path.write_bytes(
        b"model: synthetic\nplugins:\n  enabled: []\n_config_version: 12\n"
    )
    control._record_config_snapshot()
    # Recreated control reads its saved authority when recovering a transaction.
    recovered = object.__new__(PrivateHermesControl)
    recovered.host_layout, recovered.scope = layout, scope
    recovered.track_config(snapshot)
    recovered.restore_config()
    assert path.read_bytes() == original
    control._config_snapshot = snapshot
    control._record_config_snapshot()
    control.track_config(snapshot)
    path.write_text("external: true\n")
    with pytest.raises(RuntimeError, match="concurrently"):
        control.restore_config()
    assert path.read_text() == "external: true\n"


def test_private_status_does_not_require_rolled_back_api_configuration(
    tmp_path, monkeypatch
):
    layout, scope = owned(tmp_path, monkeypatch)
    (scope.attempt / "connection.json").write_text(
        json.dumps(
            {
                "schema": "daemon.private-connection.v2",
                "provider_kind": "mistral_api",
                "user_id": "12345",
            }
        )
    )
    environment = private_process_environment(scope, hermes=True, credentials=False)
    assert "MISTRAL_API_KEY" not in environment
    assert Path(environment["HOME"]).is_relative_to(scope.attempt)


def test_first_exchange_merges_an_openclaw_turn_split_across_its_receipt_dirs(tmp_path):
    from daemons_companion.hermes.first_install import daemon_receipt_dirs

    worker, live = daemon_receipt_dirs(tmp_path)
    live.parent.mkdir()
    # The live service writes the identity and reply; the worker, without
    # identity, writes the memory stages of the same turn.
    receipt(live, [event("turn", "response", platform="openclaw")])
    bare = []
    for stage in ("l0", "cycle"):
        entry = event("turn", stage)
        del entry["identity"]
        bare.append(entry)
    receipt(worker, bare)
    assert not first_exchange(live, since=0, user_id="12345", platform="openclaw")
    assert not first_exchange(worker, since=0, user_id="12345", platform="openclaw")
    assert first_exchange((worker, live), since=0, user_id="12345", platform="openclaw")
    assert not first_exchange((worker, live), since=0, user_id="999", platform="openclaw")
    assert not first_exchange((worker, live), since=0, user_id="12345")
    # A missing directory is simply not read.
    assert first_exchange((worker, live, tmp_path / "absent"), since=0, user_id="12345",
                          platform="openclaw")
    failed = dict(bare[1], status="failed")
    receipt(worker, [bare[0], failed])
    assert not first_exchange((worker, live), since=0, user_id="12345", platform="openclaw")


def test_first_exchange_keeps_host_receipts_separate(tmp_path):
    receipt(tmp_path, [event("openclaw-turn", stage, platform="openclaw")
                       for stage in ("response", "l0", "cycle")])
    assert first_exchange(tmp_path, since=0, user_id="12345", platform="openclaw")
    assert not first_exchange(tmp_path, since=0, user_id="12345")
    assert not first_exchange(tmp_path, since=0, user_id="999", platform="openclaw")
